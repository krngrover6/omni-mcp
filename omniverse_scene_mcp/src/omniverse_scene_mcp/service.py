from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import os
import posixpath
import re
from typing import Any

from .bridge import INSIGHT_QUERIES, ChangeRecord, PrimRecord, SceneBridge, unsupported_result
from .fake_bridge import SAVE_NOTE

# The SceneBridge protocol methods every service call relies on; swap_bridge refuses an object without them.
_BRIDGE_METHODS: tuple[str, ...] = (
    "ping",
    "snapshot",
    "get_selection",
    "set_selection",
    "get_prim",
    "list_children",
    "create_prim",
    "delete_prim",
    "duplicate_prim",
    "reparent_prim",
    "rename_prim",
    "move_prim",
    "set_material",
    "get_variant_selections",
    "set_variant_selection",
    "get_timeline_state",
    "set_timeline_time",
    "play_timeline",
    "stop_timeline",
    "find_prims",
    "save_scene",
)


@dataclass(slots=True)
class OperationResult:
    ok: bool
    dry_run: bool
    changes: list[ChangeRecord]
    payload: dict[str, Any]


class SceneMCPService:
    """Deterministic scene operations that can back MCP tools."""

    def __init__(self, bridge: SceneBridge, backend_info: dict[str, Any] | None = None) -> None:
        self._bridge = bridge
        self._backend_info = (
            dict(backend_info)
            if backend_info
            else _direct_backend_info(bridge, "Service built directly around a bridge, not through runtime")
        )

    @property
    def backend_info(self) -> dict[str, Any]:
        """How the scene bridge was chosen: mode, bridge class, endpoint, reason and warnings."""
        return {**self._backend_info, "warnings": list(self._backend_info.get("warnings", []))}

    # --- bridge swapping (runtime.switch_scene_target: Composer <-> Isaac Sim <-> fake without a restart) --------

    def bridge_info(self) -> dict[str, Any]:
        """The bridge this service uses right now: backend_info with the live class and endpoint; never calls it."""
        info = self.backend_info
        info["bridge"] = type(self._bridge).__name__
        endpoint = getattr(self._bridge, "endpoint", None)
        info["endpoint"] = endpoint if isinstance(endpoint, str) else None
        return info

    def swap_bridge(self, bridge: SceneBridge, backend_info: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send every later call to another scene bridge; return the previous bridge_info().

        The router and server tools hold this same service, so they switch with it (no restart). The old bridge is
        dropped, not closed; nothing is sent to either bridge. Raises TypeError, changing nothing, when bridge lacks a
        basic SceneBridge method. A call already running may finish on the old bridge.
        """
        missing = [name for name in _BRIDGE_METHODS if not callable(getattr(bridge, name, None))]
        if bridge is None or missing:
            raise TypeError(f"{type(bridge).__name__} is not a scene bridge; missing {missing[:5]}")
        previous = self.bridge_info()
        self._backend_info = (
            dict(backend_info) if backend_info else _direct_backend_info(bridge, "Bridge swapped in with swap_bridge")
        )
        self._bridge = bridge
        return previous

    def ping(self) -> dict[str, Any]:
        return {"status": self._bridge.ping()}

    def current_selection(self) -> list[str]:
        return self._bridge.get_selection()

    def set_selection(self, paths: list[str]) -> dict[str, Any]:
        self._bridge.set_selection(paths)
        return {"selection": self._bridge.get_selection()}

    def unique_child_path(self, parent_path: str, base_name: str) -> str:
        snapshot = self._bridge.snapshot()
        existing_paths = {prim.path for prim in snapshot.prims}

        parent = parent_path.rstrip("/") if parent_path != "/" else ""
        candidate = f"{parent}/{base_name}" if parent else f"/{base_name}"
        if candidate not in existing_paths:
            return candidate

        index = 1
        while True:
            suffixed = f"{candidate}_{index}"
            if suffixed not in existing_paths:
                return suffixed
            index += 1

    def scene_summary(self) -> dict[str, Any]:
        snapshot = self._bridge.snapshot()
        return {
            "stage_name": snapshot.stage_name,
            "root_path": snapshot.root_path,
            "selection": snapshot.selection,
            "prim_count": snapshot.prim_count,
            "top_level_prims": [prim.path for prim in snapshot.prims if prim.parent == snapshot.root_path],
        }

    def inspect_prim(self, path: str) -> dict[str, Any]:
        prim = self._require_prim(path)
        return asdict(prim)

    def list_children(self, path: str) -> dict[str, Any]:
        prim = self._require_prim(path)
        return {"path": prim.path, "children": self._bridge.list_children(path)}

    def rename_prim(self, path: str, new_name: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        new_path = self._renamed_path(path, new_name)
        changes = [
            ChangeRecord(
                "rename",
                path,
                f"Rename {path} to {new_name}",
                before=asdict(prim),
                after={"path": new_path, "new_name": new_name},
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "new_name": new_name, "new_path": new_path})
        updated = self._bridge.rename_prim(path, new_name)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def create_prim(self, path: str, type_name: str = "Xform", dry_run: bool = False) -> OperationResult:
        self._ensure_available(path, must_exist=False)
        changes = [ChangeRecord("create", path, f"Create {type_name} at {path}")]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "type_name": type_name})
        prim = self._bridge.create_prim(path, type_name)
        return OperationResult(True, False, changes, {"prim": asdict(prim)})

    def delete_prim(self, path: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        changes = [ChangeRecord("delete", path, f"Delete prim at {path}", before=asdict(prim))]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path})
        self._bridge.delete_prim(path)
        return OperationResult(True, False, changes, {"path": path})

    def duplicate_prim(self, source_path: str, target_path: str, dry_run: bool = False) -> OperationResult:
        source = self._require_prim(source_path)
        changes = [
            ChangeRecord(
                "duplicate",
                target_path,
                f"Duplicate {source_path} to {target_path}",
                before=asdict(source),
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"source_path": source_path, "target_path": target_path})
        prim = self._bridge.duplicate_prim(source_path, target_path)
        return OperationResult(True, False, changes, {"prim": asdict(prim)})

    def reparent_prim(self, path: str, new_parent: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        changes = [
            ChangeRecord(
                "reparent",
                path,
                f"Move {path} under {new_parent}",
                before=asdict(prim),
                after={"parent": new_parent},
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "new_parent": new_parent})
        updated = self._bridge.reparent_prim(path, new_parent)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
        dry_run: bool = False,
    ) -> OperationResult:
        prim = self._require_prim(path)
        requested = {"translation": translation, "rotation": rotation, "scale": scale}
        changes = [ChangeRecord("transform", path, f"Update transform for {path}", before=asdict(prim), after=requested)]
        if dry_run:
            return OperationResult(True, True, changes, requested)
        updated = self._bridge.move_prim(path, translation=translation, rotation=rotation, scale=scale)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def set_material(self, path: str, material_path: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        changes = [
            ChangeRecord(
                "material",
                path,
                f"Bind {material_path} to {path}",
                before=asdict(prim),
                after={"material": material_path},
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "material_path": material_path})
        updated = self._bridge.set_material(path, material_path)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def get_variant_selections(self, path: str) -> dict[str, Any]:
        self._require_prim(path)
        return {"path": path, "selections": self._bridge.get_variant_selections(path)}

    def set_variant_selection(
        self,
        path: str,
        variant_set: str,
        selection: str,
        dry_run: bool = False,
    ) -> OperationResult:
        self._require_prim(path)
        before = self._bridge.get_variant_selections(path)
        changes = [
            ChangeRecord(
                "variant",
                path,
                f"Set {variant_set}={selection} on {path}",
                before={"selections": before},
                after={"variant_set": variant_set, "selection": selection},
            )
        ]
        if dry_run:
            return OperationResult(
                True,
                True,
                changes,
                {"path": path, "variant_set": variant_set, "selection": selection},
            )
        selections = self._bridge.set_variant_selection(path, variant_set, selection)
        return OperationResult(True, False, changes, {"path": path, "selections": selections})

    def get_timeline_state(self) -> dict[str, object]:
        return self._bridge.get_timeline_state()

    def set_timeline_time(self, time_code: float) -> dict[str, object]:
        return self._bridge.set_timeline_time(time_code)

    def play_timeline(self, start_time_code: float, end_time_code: float) -> dict[str, object]:
        return self._bridge.play_timeline(start_time_code, end_time_code)

    def stop_timeline(self) -> dict[str, object]:
        return self._bridge.stop_timeline()

    def find_prims(self, query: str, scope_path: str | None = None) -> dict[str, Any]:
        matches = self._bridge.find_prims(query, scope_path=scope_path)
        return {
            "query": query,
            "scope_path": scope_path,
            "count": len(matches),
            "prims": [asdict(prim) for prim in matches],
        }

    def save_scene(self, path: str, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return self.preview_save(path)
        saved_path = self._bridge.save_scene(path)
        return {"saved_path": saved_path}

    def preview_save(self, path: str) -> dict[str, Any]:
        """Describe what save_scene(path) would do without calling the bridge's save."""
        if not path or not path.strip():
            raise ValueError("Save path is required")
        stage_name = str(self.scene_summary()["stage_name"])
        local = "://" not in path
        relative = local and not _is_absolute(path) and path.strip()[:5].lower() != "file:"
        # A relative path is resolved by the bridge against its own working directory, which this process cannot
        # see, so whether it hits the open root layer is unknown (an anonymous root can only be a Save As).
        in_place = None if relative and not stage_name.startswith("anon:") else _layer_key(stage_name) == _layer_key(path)
        warnings: list[str] = []
        if in_place is None:
            would = (
                "depends on the scene bridge's working directory: if the path resolves to the open root layer, this "
                "overwrites it and saves every dirty sublayer; otherwise it exports a root-layer-only copy"
            )
            warnings.append(
                f"May overwrite {stage_name} and save every dirty sublayer; use an absolute path to know for sure."
            )
        elif in_place:
            would = "save the root layer in place"
            warnings.append(f"Overwrites {path} and also saves every dirty sublayer of the root layer stack to its own file.")
        else:
            would = "Save As: export a copy of the root layer only"
            warnings.append(
                "Only the root layer is copied (asset paths re-anchored to the new folder); "
                "sublayer and reference files are not written and dirty sublayers stay unsaved in memory."
            )
            warnings.append(f"The open stage stays on {stage_name}; later edits keep going there, not to {path}.")
            if local and _is_absolute(path) and os.path.exists(path):
                warnings.append(f"{path} already exists and would be overwritten.")
        if stage_name.startswith("anon:"):
            warnings.append("The open stage's root layer is anonymous (never saved), so only a Save As is possible.")
        if stage_name == "NoStage":
            warnings.append("No stage is open; a real save would fail.")
        if local and not _is_absolute(path):
            warnings.append("Relative path: the scene bridge resolves it against its own working directory.")
        if type(self._bridge).__name__ == "FakeSceneBridge":
            warnings.append("Scene backend is the in-memory fake bridge; saving would not write any file.")
        return {
            "dry_run": True,
            "path": path,
            "stage_name": stage_name,
            "in_place": in_place,
            "would": would,
            "warnings": warnings,
        }

    # --- live insight and light/camera authoring (needs an extended live bridge, see kit_rpc.py) --------------

    def live_capabilities(self) -> dict[str, Any]:
        """What the scene bridge supports beyond the basic methods; never raises."""
        getter = getattr(self._bridge, "capabilities", None)
        if not callable(getter):
            return self._bridge_lacks("capabilities")
        try:
            return dict(getter())
        except Exception as exc:  # noqa: BLE001 - a down bridge is reported, not raised
            return {"ok": False, "method": "capabilities", "message": _error_text(exc)}

    def live_insight(self, query: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one read-only insight query on the LIVE stage (unsaved edits included); never raises.

        query: stage_info, composition, explain_value, properties, bounds or validate. args: that query's
        arguments, e.g. {"prim_path": "/World/Cube", "attribute": "size", "time": 24}.
        """
        name = query.strip().lower() if isinstance(query, str) else ""
        base = {"mode": "live", "query": name or query}
        if name not in INSIGHT_QUERIES:
            return {"ok": False, **base, "message": f"Unknown insight query {query!r}; use one of {list(INSIGHT_QUERIES)}"}
        if args is not None and not isinstance(args, dict):
            return {"ok": False, **base, "message": "args must be an object (dict) of that query's arguments"}
        runner = getattr(self._bridge, "insight", None)
        if not callable(runner):
            return {**base, **self._bridge_lacks("insight")}
        try:
            result = runner(name, dict(args or {}))
        except Exception as exc:  # noqa: BLE001 - USD and bridge errors become a structured result
            return {"ok": False, **base, "message": _error_text(exc)}
        payload = dict(result) if isinstance(result, dict) else {"ok": True, "result": result}
        for key, value in base.items():
            payload.setdefault(key, value)
        return payload

    def create_light(
        self,
        path: str,
        light_type: str,
        *,
        intensity: float | None = None,
        exposure: float | None = None,
        color: tuple[float, float, float] | None = None,
        size: float | None = None,
        translation: tuple[float, float, float] | None = None,
        aim_at: str | tuple[float, float, float] | None = None,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> OperationResult:
        """Create a UsdLux light on the live stage; dry_run (the default) previews it without authoring."""
        dry_run = dry_run is not False  # only an explicit False authors (None, 0 and [] stay a preview)
        _require_text(path, "Light path")
        _require_text(light_type, "Light type")
        options = _without_none(
            intensity=intensity,
            exposure=exposure,
            color=color,
            size=size,
            translation=translation,
            aim_at=aim_at,
            up_axis=up_axis,
        )
        summary = f"Create {light_type} at {path}" + (f" aimed at {aim_at}" if aim_at is not None else "")
        return self._live_authoring(
            "create_light",
            path,
            summary,
            {"light_type": light_type, **options},
            lambda author: author(path, light_type, dry_run=dry_run, **options),
            dry_run,
        )

    def create_camera(
        self,
        path: str,
        *,
        focal_length: float | None = None,
        horizontal_aperture: float | None = None,
        clipping_range: tuple[float, float] | None = None,
        translation: tuple[float, float, float] | None = None,
        aim_at: str | tuple[float, float, float] | None = None,
        copy_optics_from: str | None = None,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> OperationResult:
        """Create a camera on the live stage; dry_run (the default) previews it without authoring."""
        dry_run = dry_run is not False  # only an explicit False authors (None, 0 and [] stay a preview)
        _require_text(path, "Camera path")
        options = _without_none(
            focal_length=focal_length,
            horizontal_aperture=horizontal_aperture,
            clipping_range=clipping_range,
            translation=translation,
            aim_at=aim_at,
            copy_optics_from=copy_optics_from,
            up_axis=up_axis,
        )
        summary = f"Create camera at {path}" + (f" aimed at {aim_at}" if aim_at is not None else "")
        return self._live_authoring(
            "create_camera",
            path,
            summary,
            options,
            lambda author: author(path, dry_run=dry_run, **options),
            dry_run,
        )

    def aim(
        self,
        path: str,
        target: str | tuple[float, float, float],
        *,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> OperationResult:
        """Rotate a live prim to face a target prim or point; dry_run (the default) previews the rotation."""
        dry_run = dry_run is not False  # only an explicit False authors (None, 0 and [] stay a preview)
        _require_text(path, "Prim path")
        if target is None or (isinstance(target, str) and not target.strip()):
            raise ValueError("Aim target is required: a prim path or an (x, y, z) point")
        options = _without_none(up_axis=up_axis)
        return self._live_authoring(
            "aim",
            path,
            f"Aim {path} at {target}",
            {"target": target, **options},
            lambda author: author(path, target, dry_run=dry_run, **options),
            dry_run,
        )

    # --- safe attribute/material edits and undo (RPC tier3-1; the fake scene does them in memory) ---------------

    def set_attribute(
        self,
        path: str,
        name: str,
        value: Any,
        time: float | None = None,
        create_type: str | None = None,
        dry_run: bool = True,
    ) -> OperationResult:
        """Set one attribute on the scene; dry_run (the default) previews the coerced value and what wins after it.

        time=None sets the default value, a number writes a time sample; create_type (an Sdf type name such as
        'float' or 'color3f') creates a missing custom attribute. Bridge refusals (missing prim, value of the wrong
        type) come back as ok=False; only an empty path or name, or value=None, raises ValueError.
        """
        dry_run = dry_run is not False  # only an explicit False authors (None, 0 and [] stay a preview)
        _require_text(path, "Prim path")
        _require_text(name, "Attribute name")
        if value is None:
            raise ValueError("value is required (None cannot be authored as an attribute value)")
        options = _without_none(time=time, create_type=create_type)
        return self._live_authoring(
            "set_attribute",
            path,
            f"Set {name} on {path} to {_short(value)}",
            {"name": name, "value": value, **options},
            lambda author: author(path, name, value, dry_run=dry_run, **options),
            dry_run,
        )

    def create_material(
        self,
        path: str,
        diffuse_color: tuple[float, float, float] | str | None = None,
        roughness: float | None = None,
        metallic: float | None = None,
        opacity: float | None = None,
        emissive_color: tuple[float, float, float] | str | None = None,
        ior: float | None = None,
        dry_run: bool = True,
        *,
        create_parent: bool = True,
    ) -> OperationResult:
        """Create a UsdPreviewSurface material at path; dry_run (the default) previews the material and its inputs.

        Only the given inputs are authored. create_parent (on by default) also plans a missing direct parent such
        as /World/Looks as a Scope; the preview lists it under would_create.creates_parent.
        """
        dry_run = dry_run is not False  # only an explicit False authors (None, 0 and [] stay a preview)
        _require_text(path, "Material path")
        options = _without_none(
            diffuse_color=diffuse_color,
            roughness=roughness,
            metallic=metallic,
            opacity=opacity,
            emissive_color=emissive_color,
            ior=ior,
        )
        if create_parent is True:
            options["create_parent"] = True
        return self._live_authoring(
            "create_material",
            path,
            f"Create material {path}",
            dict(options),
            lambda author: author(path, dry_run=dry_run, **options),
            dry_run,
        )

    def bind_material(
        self,
        prim_path: str,
        material_path: str,
        strength: str | None = None,
        purpose: str | None = None,
        dry_run: bool = True,
    ) -> OperationResult:
        """Bind a material to a prim; dry_run (the default) previews the binding and the material it resolves to.

        strength: weakerThanDescendants (default) or strongerThanDescendants; purpose: allPurpose (default), preview
        or full. On a bridge that predates bind_material, a plain binding (no strength or purpose) falls back to the
        basic 'material' method (set_material); the result then says fallback='set_material'.
        """
        dry_run = dry_run is not False  # only an explicit False authors (None, 0 and [] stay a preview)
        _require_text(prim_path, "Prim path")
        _require_text(material_path, "Material path")
        options = _without_none(strength=strength, purpose=purpose)
        result = self._live_authoring(
            "bind_material",
            prim_path,
            f"Bind {material_path} to {prim_path}",
            {"material_path": material_path, **options},
            lambda author: author(prim_path, material_path, dry_run=dry_run, **options),
            dry_run,
        )
        if result.ok or not result.payload.get("needs_bridge_update") or options:
            return result
        fallback = self.set_material(prim_path, material_path, dry_run=dry_run)
        fallback.payload = {
            **fallback.payload,
            "fallback": "set_material",
            "note": "This scene bridge has no bind_material yet, so the basic material binding was used (no "
            "strength, purpose or binding preview); reload the bridge for the full preview.",
        }
        return fallback

    def undo_last(self, dry_run: bool = True) -> dict[str, Any]:
        """Undo the newest MCP edit the scene bridge recorded; dry_run (the default) only says what would be undone.

        Saving is not undoable: undo reverts the open layers in memory, so save again to write the reverted state.
        Never raises: a bridge without undo returns needs_bridge_update.
        """
        dry = dry_run is not False
        payload = self._call_undo("undo_last", lambda undo: undo(dry_run=dry))
        payload.setdefault("dry_run", dry)
        return payload

    def undo_history(self, limit: int = 20) -> dict[str, Any]:
        """The newest recorded MCP edits (newest first, 1-50); read-only and never raises."""
        try:
            count = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            count = 20
        return self._call_undo("undo_history", lambda history: history(count))

    def _call_undo(self, method: str, call: Callable[[Callable[..., Any]], Any]) -> dict[str, Any]:
        runner = getattr(self._bridge, method, None)  # one of the two fixed undo names, never user input
        if not callable(runner):
            payload = self._bridge_lacks(method)
        else:
            try:
                result = call(runner)
            except Exception as exc:  # noqa: BLE001 - a down bridge or refused undo is reported, not raised
                result = {"ok": False, "method": method, "message": _error_text(exc)}
            payload = dict(result) if isinstance(result, dict) else {"ok": True, "result": result}
        payload.setdefault("ok", True)
        payload.setdefault("note", SAVE_NOTE)
        payload["action"] = method
        return payload

    def _live_authoring(
        self,
        method: str,
        path: str,
        summary: str,
        requested: dict[str, Any],
        call: Callable[[Callable[..., Any]], Any],
        dry_run: bool,
    ) -> OperationResult:
        """Forward to the bridge's live authoring method; an unsupported bridge gives ok=False, not an exception."""
        author = getattr(self._bridge, method, None)  # method is one of this class's fixed names, never user input
        if not callable(author):
            return OperationResult(False, bool(dry_run), [], self._bridge_lacks(method))
        result = call(author)
        payload = dict(result) if isinstance(result, dict) else {"ok": True, "result": result}
        if payload.get("unsupported") or payload.get("needs_bridge_update"):
            return OperationResult(False, bool(dry_run), [], payload)
        ok = bool(payload.get("ok", True))
        applied_dry_run = bool(payload.get("dry_run", dry_run))
        changes = [ChangeRecord(method, path, summary, after=requested)] if ok else []
        return OperationResult(ok, applied_dry_run, changes, payload)

    def _bridge_lacks(self, method: str) -> dict[str, Any]:
        name = type(self._bridge).__name__
        return unsupported_result(
            method,
            f"The {name} scene bridge has no {method}(); connect an up-to-date live bridge (Isaac Sim: run "
            "isaacsim-study/reload_mcp_bridge.py; Composer: run composer-study/reload_composer_bridge.py).",
            needs_bridge_update=True,
            bridge=name,
        )

    def preview_changes(self, result: OperationResult) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "dry_run": result.dry_run,
            "changes": [asdict(change) for change in result.changes],
            "payload": result.payload,
        }

    def _require_prim(self, path: str) -> PrimRecord:
        prim = self._bridge.get_prim(path)
        if prim is None:
            raise ValueError(f"Prim does not exist: {path}")
        return prim

    def _ensure_available(self, path: str, must_exist: bool) -> None:
        exists = self._bridge.get_prim(path) is not None
        if must_exist and not exists:
            raise ValueError(f"Prim does not exist: {path}")
        if not must_exist and exists:
            raise ValueError(f"Prim already exists: {path}")

    @staticmethod
    def _renamed_path(path: str, new_name: str) -> str:
        if "/" in new_name or not new_name:
            raise ValueError("Prim name must be a single path token")
        parent = path.rsplit("/", 1)[0] if "/" in path.strip("/") else ""
        return f"{parent}/{new_name}" if parent else f"/{new_name}"


def _direct_backend_info(bridge: Any, reason: str) -> dict[str, Any]:
    return {
        "mode": "direct",
        "bridge": type(bridge).__name__,
        "endpoint": getattr(bridge, "endpoint", None),
        "reason": reason,
        "silent_fallback": False,
        "warnings": [],
    }


def _layer_key(path: str) -> str:
    """Comparable form of a layer identifier or file path: no file: prefix, forward slashes, lower case."""
    text = path.strip()
    if text[:5].lower() == "file:":
        text = re.sub(r"^/+(?=[A-Za-z]:)", "", text[5:])
    text = text.replace("\\", "/")
    return posixpath.normpath(text).lower() if text else ""


def _is_absolute(path: str) -> bool:
    return os.path.isabs(path) or bool(re.match(r"^[A-Za-z]:[\\/]", path))


def _require_text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")


def _without_none(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _error_text(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    return text if isinstance(exc, ValueError) else f"{type(exc).__name__}: {text}"
