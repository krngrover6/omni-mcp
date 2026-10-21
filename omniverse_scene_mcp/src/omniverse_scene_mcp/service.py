from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
import os
import posixpath
import re
from typing import Any

from .bridge import INSIGHT_QUERIES, ChangeRecord, PrimRecord, SceneBridge, unsupported_result


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
        self._backend_info = dict(backend_info) if backend_info else {
            "mode": "direct",
            "bridge": type(bridge).__name__,
            "endpoint": getattr(bridge, "endpoint", None),
            "reason": "Service built directly around a bridge, not through runtime",
            "silent_fallback": False,
            "warnings": [],
        }

    @property
    def backend_info(self) -> dict[str, Any]:
        """How the scene bridge was chosen: mode, bridge class, endpoint, reason and warnings."""
        return {**self._backend_info, "warnings": list(self._backend_info.get("warnings", []))}

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
            f"The {name} scene bridge has no {method}(); connect an up-to-date live Isaac Sim bridge.",
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


def _error_text(exc: Exception) -> str:
    text = str(exc) or type(exc).__name__
    return text if isinstance(exc, ValueError) else f"{type(exc).__name__}: {text}"
