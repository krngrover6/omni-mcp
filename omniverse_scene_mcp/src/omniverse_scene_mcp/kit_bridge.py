from __future__ import annotations

import importlib
import json
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any

from .bridge import (
    ASSET_RPC_METHODS,
    BASIC_RPC_METHODS,
    CAPTURE_RPC_METHODS,
    EDIT_RPC_METHODS,
    HEALTH_RPC_METHODS,
    HOMEWORK_RPC_METHODS,
    INSIGHT_QUERIES,
    LAYER_RPC_METHODS,
    LAYOUT_RPC_METHODS,
    LIVE_RPC_METHODS,
    PHYSICS_RPC_METHODS,
    ROBOT_RPC_METHODS,
    RPC_VERSION,
    SIMULATION_RPC_METHODS,
    PrimRecord,
    SceneSnapshot,
    unsupported_result,
)

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, UsdUtils
except ImportError as exc:  # pragma: no cover - only available with USD bindings
    Gf = Sdf = Usd = UsdGeom = UsdShade = UsdUtils = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR = exc
else:  # pragma: no cover - only available with USD bindings
    _PXR_IMPORT_ERROR = None

try:
    import omni.usd
    import omni.timeline
except ImportError as exc:  # pragma: no cover - only available inside Kit
    omni = None  # type: ignore[assignment]
    _OMNI_USD_IMPORT_ERROR = exc
else:  # pragma: no cover - only available inside Kit
    _OMNI_USD_IMPORT_ERROR = None
    try:
        import omni.kit.commands  # noqa: F401 - makes omni.kit.commands/omni.kit.undo reachable for Ctrl+Z undo
    except ImportError:
        pass

try:
    from . import kit_commands
except Exception as exc:  # pragma: no cover - keep the bridge working (without undo) if undo support fails to load
    kit_commands = None  # type: ignore[assignment]
    _UNDO_IMPORT_ERROR: Exception | None = exc
else:
    _UNDO_IMPORT_ERROR = None

# Live insight runs on Kit's main thread and the RPC server stops waiting after 15 s, so keep it bounded.
LIVE_NOTE = "Read from the LIVE stage in Kit, including unsaved edits; nothing was changed, selected or saved."
_KIT_LAYER_NOTE = (
    "This layer change was made with raw USD calls, outside Kit's own layer commands: Kit's Layer window may not show "
    "it until it refreshes (or the stage is reopened), and Kit's Ctrl+Z does not undo it. Use restore_with (or "
    "scene_undo_last for a move of opinions) to put it back."
)
LIVE_MAX_RESULT_BYTES = 1_000_000
LIVE_SLOW_MS = 5_000
LIVE_DEFAULT_MAX_PRIMS = 20_000
LIVE_MAX_PRIMS = 100_000
LIVE_DEFAULT_MAX_ISSUES = 100
LIVE_MAX_ISSUES = 500
_MAX_UNSAVED_LAYERS = 50
_MAX_PATH_CHARS = 4096
_MAX_NAME_CHARS = 1024
_VALIDATE_PROFILES = ("exam", "usd", "all")
_PURPOSES = ("default", "render", "proxy", "guide")
# Strict allowlist: query name -> the only argument names it accepts.
_INSIGHT_ARGS: dict[str, tuple[str, ...]] = {
    "stage_info": (),
    "composition": ("prim_path",),
    "explain_value": ("prim_path", "attribute", "time"),
    "properties": ("prim_path", "include_values", "time", "authored_only"),
    "bounds": ("prim_path", "purposes", "time", "use_extents_hint"),
    "validate": ("profile", "max_issues", "max_prims"),
}
_ARG_ALIASES = {"path": "prim_path"}


class LiveKitSceneBridge:
    """Bridge a live Omniverse Kit stage into the MCP service layer."""

    def __init__(self, usd_context_name: str = "") -> None:
        if omni is None or Gf is None or Usd is None or UsdGeom is None or UsdShade is None:
            raise ImportError(
                "LiveKitSceneBridge requires Omniverse Kit's USD bindings. "
                "Run this inside Kit or an Omniverse-enabled Python environment."
            ) from (_OMNI_USD_IMPORT_ERROR or _PXR_IMPORT_ERROR)
        self._usd_context = omni.usd.get_context(usd_context_name)
        # MCP edits recorded for undo_last; inside Kit they are also on Kit's undo stack (Ctrl+Z).
        self._undo_stack = kit_commands.UndoStack() if kit_commands is not None else None

    def ping(self) -> str:
        stage = self._stage()
        if stage is None:
            return "no-stage"
        return "ok"

    def snapshot(self) -> SceneSnapshot:
        stage = self._stage()
        if stage is None:
            return SceneSnapshot(stage_name="NoStage", root_path="/", selection=[], prim_count=0, prims=[])

        root_layer = stage.GetRootLayer()
        default_prim = stage.GetDefaultPrim()
        root_path = str(default_prim.GetPath()) if default_prim and default_prim.IsValid() else "/"
        selection = self.get_selection()

        prims: list[PrimRecord] = []
        for prim in stage.Traverse():
            prims.append(self._prim_to_record(prim))

        stage_name = getattr(root_layer, "identifier", "") or getattr(root_layer, "realPath", "") or "LiveStage"
        return SceneSnapshot(
            stage_name=stage_name,
            root_path=root_path,
            selection=selection,
            prim_count=len(prims),
            prims=prims,
        )

    def get_selection(self) -> list[str]:
        selection = self._usd_context.get_selection()
        return list(selection.get_selected_prim_paths())

    def set_selection(self, paths: list[str]) -> None:
        self._select(paths)

    def get_prim(self, path: str) -> PrimRecord | None:
        stage = self._require_stage()
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            return None
        return self._prim_to_record(prim)

    def list_children(self, path: str) -> list[str]:
        prim = self._require_prim(path)
        return [str(child.GetPath()) for child in prim.GetChildren()]

    # Every mutating method below runs its edit through _undoable (Ctrl+Z in Kit, or undo_last); reads do not.

    def create_prim(self, path: str, type_name: str = "Xform") -> PrimRecord:
        stage = self._require_stage()
        # Defining a prim that already exists only changes its specifier/type: snapshot just the prim, not its subtree.
        existing = stage.GetPrimAtPath(path) if Sdf.Path.IsValidPathString(path) else None
        full, shallow = ([], [path]) if existing and existing.IsValid() else ([path], [])
        prim = self._undoable(
            f"Create {type_name} {path}", stage, full, lambda: stage.DefinePrim(path, type_name), shallow=shallow
        )
        self._select([path])
        return self._prim_to_record(prim)

    def delete_prim(self, path: str) -> None:
        stage = self._require_stage()
        prim = self._require_prim(path)
        prim_path = prim.GetPath()
        # The snapshot holds the whole deleted subtree of the edit target layer.
        self._undoable(f"Delete {prim_path}", stage, [str(prim_path)], lambda: stage.RemovePrim(prim_path))
        self._clear_selection_if_present(path)

    def duplicate_prim(self, source_path: str, target_path: str) -> PrimRecord:
        stage = self._require_stage()
        source = self._require_prim(source_path)
        if stage.GetPrimAtPath(target_path).IsValid():
            raise ValueError(f"Target prim already exists: {target_path}")
        target = Sdf.Path(target_path)
        parent_path = target.GetParentPath()
        if parent_path != Sdf.Path.absoluteRootPath and not stage.GetPrimAtPath(parent_path).IsValid():
            raise ValueError(f"Target parent does not exist: {parent_path}")
        clone = self._undoable(
            f"Duplicate {source_path} to {target_path}",
            stage,
            [str(target)],
            lambda: self._copy_prim_specs(stage, source, target),
        )
        self._select([target_path])
        return self._prim_to_record(clone)

    def reparent_prim(self, path: str, new_parent: str) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)
        new_parent_prim = self._require_prim(new_parent)
        old_path = prim.GetPath()
        new_path = new_parent_prim.GetPath().AppendChild(prim.GetName())

        def apply():
            editor = Usd.NamespaceEditor(stage)
            if not editor.ReparentPrim(prim, new_parent_prim):
                raise ValueError(f"Failed to reparent {path} under {new_parent}")
            if not editor.ApplyEdits():
                raise ValueError(f"Unable to apply namespace edit for {path}")
            moved_prim = stage.GetPrimAtPath(new_path)
            if not moved_prim or not moved_prim.IsValid():
                raise ValueError(f"Reparented prim is not accessible at the expected path under {new_parent}")
            return moved_prim

        moved = self._undoable(
            f"Reparent {old_path} under {new_parent}", stage, [str(old_path), str(new_path)], apply, namespace_edit=True
        )
        self._select([str(moved.GetPath())])
        return self._prim_to_record(moved)

    def rename_prim(self, path: str, new_name: str) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)
        parent = prim.GetParent()
        paths = [str(prim.GetPath())]
        new_path = _child_path_or_none(parent, new_name)
        if new_path:
            paths.append(new_path)

        def apply():
            editor = Usd.NamespaceEditor(stage)
            if not editor.RenamePrim(prim, new_name):
                raise ValueError(f"Failed to rename {path} to {new_name}")
            if not editor.ApplyEdits():
                raise ValueError(f"Unable to apply rename for {path}")
            renamed_path = parent.GetPath().AppendChild(new_name) if parent and parent.IsValid() else f"/{new_name}"
            renamed_prim = stage.GetPrimAtPath(renamed_path)
            if not renamed_prim or not renamed_prim.IsValid():
                raise ValueError(f"Renamed prim is not accessible at the expected path: {renamed_path}")
            return renamed_prim

        renamed = self._undoable(f"Rename {path} to {new_name}", stage, paths, apply, namespace_edit=True)
        self._select([str(renamed.GetPath())])
        return self._prim_to_record(renamed)

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
    ) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)
        self._undoable(
            f"Move {path}",
            stage,
            [],
            lambda: self._apply_move(path, prim, translation, rotation, scale),
            shallow=[str(prim.GetPath())],
        )
        return self._prim_to_record(prim)

    def _apply_move(self, path: str, prim, translation, rotation, scale) -> None:
        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            raise ValueError(f"Prim is not transformable: {path}")
        ops = xformable.GetOrderedXformOps()
        animated = [op.GetOpName() for op in ops if op.GetNumTimeSamples() > 0]
        if animated:
            raise ValueError(f"Cannot move {path}: transform ops {animated} have time samples")

        common = UsdGeom.XformCommonAPI(prim)
        if common:
            rotation_order = common.GetXformVectors(Usd.TimeCode.Default())[4]
            applied = True
            if translation is not None:
                applied &= common.SetTranslate(Gf.Vec3d(*translation))
            if rotation is not None:
                applied &= common.SetRotate(Gf.Vec3f(*rotation), rotation_order)
            if scale is not None:
                applied &= common.SetScale(Gf.Vec3f(*scale))
            if not applied:
                raise ValueError(f"Failed to update the transform of {path}")
        else:
            self._move_with_existing_ops(path, xformable, ops, translation, rotation, scale)

    def set_material(self, path: str, material_path: str) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)
        material_prim = stage.GetPrimAtPath(material_path)
        if not material_prim or not material_prim.IsValid():
            raise ValueError(f"Material prim does not exist: {material_path}")
        material = UsdShade.Material(material_prim)
        self._undoable(
            f"Bind {material_path} to {path}",
            stage,
            [],
            lambda: UsdShade.MaterialBindingAPI.Apply(prim).Bind(material),
            shallow=[str(prim.GetPath())],
        )
        return self._prim_to_record(prim)

    def get_variant_selections(self, path: str) -> dict[str, str]:
        prim = self._require_prim(path)
        variant_sets = prim.GetVariantSets()
        return {
            name: variant_sets.GetVariantSet(name).GetVariantSelection()
            for name in variant_sets.GetNames()
        }

    def set_variant_selection(self, path: str, variant_set: str, selection: str) -> dict[str, str]:
        prim = self._require_prim(path)
        variants = prim.GetVariantSet(variant_set)
        if not variants.IsValid():
            raise ValueError(f"Variant set does not exist on {path}: {variant_set}")
        names = variants.GetVariantNames()
        if selection not in names:
            raise ValueError(
                f"Invalid selection '{selection}' for {variant_set} on {path}; expected one of {names}"
            )

        def apply() -> None:
            if not variants.SetVariantSelection(selection):
                raise ValueError(f"Failed to select {variant_set}={selection} on {path}")

        label = f"Select {variant_set}={selection} on {path}"
        self._undoable(label, self._require_stage(), [], apply, shallow=[str(prim.GetPath())])
        return self.get_variant_selections(path)

    def get_timeline_state(self) -> dict[str, object]:
        stage = self._require_stage()
        timeline = omni.timeline.get_timeline_interface()
        rate = float(stage.GetTimeCodesPerSecond() or 24.0)
        return {
            "current_time_code": timeline.get_current_time() * rate,
            "start_time_code": timeline.get_start_time() * rate,
            "end_time_code": timeline.get_end_time() * rate,
            "time_codes_per_second": rate,
            "playing": bool(timeline.is_playing()),
        }

    def set_timeline_time(self, time_code: float) -> dict[str, object]:
        stage = self._require_stage()
        timeline = omni.timeline.get_timeline_interface()
        rate = float(stage.GetTimeCodesPerSecond() or 24.0)
        timeline.stop()
        timeline.set_current_time(float(time_code) / rate)
        return self.get_timeline_state()

    def play_timeline(self, start_time_code: float, end_time_code: float) -> dict[str, object]:
        if end_time_code <= start_time_code:
            raise ValueError("Timeline end must be greater than its start")
        stage = self._require_stage()
        timeline = omni.timeline.get_timeline_interface()
        rate = float(stage.GetTimeCodesPerSecond() or 24.0)
        timeline.stop()
        timeline.set_start_time(float(start_time_code) / rate)
        timeline.set_end_time(float(end_time_code) / rate)
        timeline.set_current_time(float(start_time_code) / rate)
        timeline.play()
        return self.get_timeline_state()

    def stop_timeline(self) -> dict[str, object]:
        omni.timeline.get_timeline_interface().stop()
        return self.get_timeline_state()

    def find_prims(self, query: str, scope_path: str | None = None) -> list[PrimRecord]:
        stage = self._require_stage()
        query_lower = query.lower().strip()
        scope_prefix = f"{scope_path.rstrip('/')}/" if scope_path else None
        matches: list[PrimRecord] = []
        for prim in stage.Traverse():
            prim_path = str(prim.GetPath())
            if scope_prefix and not (prim_path == scope_path or prim_path.startswith(scope_prefix)):
                continue
            haystack = " ".join(filter(None, [prim_path, prim.GetTypeName() or "", str(prim.GetPath())])).lower()
            if query_lower in haystack:
                matches.append(self._prim_to_record(prim))
        return matches

    def save_scene(self, path: str) -> str:
        """Save the layers (or Save As the root layer). Saving is not undoable: undo_last only reverts in-memory
        edits, so save again afterwards to write the reverted state to disk."""
        stage = self._require_stage()
        if not path:
            raise ValueError("Save path is required")
        # Save layers rather than Stage.Export, which would flatten references and sublayers into one file.
        root_layer = stage.GetRootLayer()
        if not root_layer.anonymous and _same_file(root_layer.realPath, path):
            failed = [
                layer.identifier
                for layer in stage.GetLayerStack(includeSessionLayers=False)
                if not layer.anonymous and (layer.dirty or layer == root_layer) and not layer.Save()
            ]
            if failed:
                raise ValueError(f"Failed to save layers: {failed}")
            return path

        # Save As: write a copy of the root layer only; dirty sublayers keep their unsaved edits in memory.
        copy = Sdf.Layer.CreateAnonymous()
        copy.TransferContent(root_layer)
        target_dir = os.path.dirname(os.path.abspath(path))
        UsdUtils.ModifyAssetPaths(copy, lambda asset_path: _reanchor_asset_path(root_layer, asset_path, target_dir))
        if not copy.Export(path):
            raise ValueError(f"Failed to export the root layer to {path}")
        return path

    def make_red(self, path: str) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)

        looks = "/World/Looks"
        material_path = f"{looks}/RedMaterial"

        def apply() -> None:
            looks_path = stage.GetPrimAtPath(looks)
            if not looks_path or not looks_path.IsValid():
                stage.DefinePrim(looks, "Scope")

            material_prim = stage.GetPrimAtPath(material_path)
            if not material_prim or not material_prim.IsValid():
                material = UsdShade.Material.Define(stage, material_path)
                shader = UsdShade.Shader.Define(stage, f"{material_path}/Shader")
                shader.CreateIdAttr("UsdPreviewSurface")
                shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(1.0, 0.0, 0.0))
                shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.4)
                material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
            else:
                material = UsdShade.Material(material_prim)

            UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)

        self._undoable(f"Make {path} red", stage, [material_path], apply, shallow=[str(prim.GetPath()), looks])
        return self._prim_to_record(prim)

    # --- live insight and light/camera authoring (extended RPC, see kit_rpc.py) ---------------------------------

    def capabilities(self) -> dict[str, Any]:
        """What this live bridge can do: RPC version, insight queries and whether light/camera authoring loads."""
        try:
            from . import authoring  # noqa: F401 - only checks that the module imports
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            authoring_info: dict[str, Any] = {"available": False, "error": _clip(f"{type(exc).__name__}: {exc}")}
        else:
            authoring_info = {"available": True}
        try:
            from .edits import set_attribute  # noqa: F401 - the same import the edit methods make
        except Exception as exc:  # noqa: BLE001
            edits_info: dict[str, Any] = {"available": False, "error": _clip(f"{type(exc).__name__}: {exc}")}
        else:
            edits_info = {"available": True, "methods": list(EDIT_RPC_METHODS), "dry_run_default": True}
        return {
            "ok": True,
            "bridge": type(self).__name__,
            "version": RPC_VERSION,
            "methods": list(BASIC_RPC_METHODS) + list(LIVE_RPC_METHODS),
            "insight_queries": list(INSIGHT_QUERIES),
            "insight_args": {name: list(keys) for name, keys in _INSIGHT_ARGS.items()},
            "authoring": authoring_info,
            "edits": edits_info,
            "undo": self._undo_info(),
            "assets": _module_info("asset_library", ASSET_RPC_METHODS, dry_run_default=True),
            "physics": _module_info("physics", PHYSICS_RPC_METHODS, dry_run_default=True),
            "capture": _module_info("kit_live_extras", CAPTURE_RPC_METHODS),
            "simulation": _module_info("kit_live_extras", SIMULATION_RPC_METHODS),
            "homework": _homework_info(),
            "layout": _module_info("layout", LAYOUT_RPC_METHODS, dry_run_default=True),
            "robots": _module_info("robot_inspector", ROBOT_RPC_METHODS),
            "layers": _module_info("layer_manager", LAYER_RPC_METHODS, dry_run_default=True),
            "health": _module_info("asset_health", HEALTH_RPC_METHODS),
            "dry_run_default": True,
            "stage_open": self._stage() is not None,
        }

    def insight(self, query: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one allowlisted, read-only usd_insight/usd_validate query on the LIVE stage (unsaved edits included)."""
        name = query.strip().lower() if isinstance(query, str) else ""
        runner = _INSIGHT_RUNNERS.get(name)
        if runner is None:
            raise ValueError(f"Unknown insight query {_clip(repr(query), 80)}; use one of {list(INSIGHT_QUERIES)}")
        clean = _insight_args(name, args)
        stage = self._require_stage()
        dirty_before = _dirty_layer_ids(stage)
        started = time.perf_counter()
        result = runner(stage, clean)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        layers = _live_layer_state(stage)
        existing = result.get("warnings")
        warnings = list(existing) if isinstance(existing, list) else []
        newly_dirty = sorted(_dirty_layer_ids(stage) - dirty_before)
        if newly_dirty:
            warnings.append(f"Read-only violation: these layers became dirty during the query: {newly_dirty[:10]}")
        if elapsed_ms > LIVE_SLOW_MS:
            warnings.append(
                f"This query took {elapsed_ms} ms on Kit's main thread; the bridge gives up waiting after 15 s, "
                "so narrow it (one prim, smaller max_prims) on big scenes."
            )
        if name == "validate" and clean.get("profile", "exam") != "exam":
            warnings.append("OpenUSD's validators ran on the whole live stage; only the exam checks honor max_prims.")
        payload: dict[str, Any] = {"ok": True}
        payload.update(result)
        payload.update(layers)
        payload.update(
            {
                "mode": "live",
                "query": name,
                "args": clean,
                "elapsed_ms": elapsed_ms,
                "note": LIVE_NOTE,
            }
        )
        if warnings:
            payload["warnings"] = warnings
        return _fit_json(payload, LIVE_MAX_RESULT_BYTES)

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
    ) -> dict[str, Any]:
        """Create (or with dry_run, preview) a UsdLux light on the live stage via authoring.create_light."""
        try:
            from .authoring import create_light as author_light
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            return _authoring_unavailable("create_light", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Create {light_type} light {path}",
            stage,
            path,
            dry_run,
            lambda: author_light(
                stage,
                path,
                light_type,
                intensity=intensity,
                exposure=exposure,
                color=color,
                size=size,
                translation=translation,
                aim_at=aim_at,
                up_axis=up_axis,
                dry_run=dry_run,
            ),
            creates=True,
        )
        return self._authoring_result(result, path, dry_run, select=True)

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
    ) -> dict[str, Any]:
        """Create (or with dry_run, preview) a UsdGeom camera on the live stage via authoring.create_camera."""
        try:
            from .authoring import create_camera as author_camera
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            return _authoring_unavailable("create_camera", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Create camera {path}",
            stage,
            path,
            dry_run,
            lambda: author_camera(
                stage,
                path,
                focal_length=focal_length,
                horizontal_aperture=horizontal_aperture,
                clipping_range=clipping_range,
                translation=translation,
                aim_at=aim_at,
                copy_optics_from=copy_optics_from,
                up_axis=up_axis,
                dry_run=dry_run,
            ),
            creates=True,
        )
        return self._authoring_result(result, path, dry_run, select=True)

    def aim(
        self,
        path: str,
        target: str | tuple[float, float, float],
        *,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Rotate (or with dry_run, preview rotating) a live prim to face a target prim or point via authoring.aim."""
        try:
            from .authoring import aim as author_aim
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            return _authoring_unavailable("aim", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Aim {path}",
            stage,
            path,
            dry_run,
            lambda: author_aim(stage, path, target, up_axis=up_axis, dry_run=dry_run),
            creates=False,
        )
        return self._authoring_result(result, path, dry_run, select=False)

    # --- safe attribute and material edits (edits.py); dry_run=True by default, undoable when they write ---------

    def set_attribute(
        self,
        path: str,
        name: str,
        value: Any,
        *,
        time: float | None = None,
        create_type: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Set (or with dry_run, preview setting) one attribute on the live stage via edits.set_attribute."""
        try:
            from .edits import set_attribute as edit_attribute
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            return _edits_unavailable("set_attribute", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Set {name} on {path}",
            stage,
            path,
            dry_run,
            lambda: edit_attribute(stage, path, name, value, time=time, create_type=create_type, dry_run=dry_run),
            creates=False,
        )
        return self._authoring_result(result, path, dry_run, select=False)

    def create_material(
        self,
        path: str,
        *,
        diffuse_color: Any = None,
        roughness: float | None = None,
        metallic: float | None = None,
        opacity: float | None = None,
        emissive_color: Any = None,
        ior: float | None = None,
        create_parent: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Create (or with dry_run, preview) a UsdPreviewSurface material on the live stage via edits.create_material."""
        try:
            from .edits import create_material as edit_material
        except Exception as exc:  # noqa: BLE001
            return _edits_unavailable("create_material", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Create material {path}",
            stage,
            path,
            dry_run,
            lambda: edit_material(
                stage,
                path,
                diffuse_color=diffuse_color,
                roughness=roughness,
                metallic=metallic,
                opacity=opacity,
                emissive_color=emissive_color,
                ior=ior,
                create_parent=create_parent is True,
                dry_run=dry_run,
            ),
            creates=True,
        )
        return self._authoring_result(result, path, dry_run, select=False)

    def bind_material(
        self,
        prim_path: str,
        material_path: str,
        *,
        strength: str | None = None,
        purpose: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Bind (or with dry_run, preview binding) a material to a live prim via edits.bind_material."""
        try:
            from .edits import bind_material as edit_binding
        except Exception as exc:  # noqa: BLE001
            return _edits_unavailable("bind_material", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Bind {material_path} to {prim_path}",
            stage,
            prim_path,
            dry_run,
            lambda: edit_binding(stage, prim_path, material_path, strength=strength, purpose=purpose, dry_run=dry_run),
            creates=False,
        )
        return self._authoring_result(result, prim_path, dry_run, select=False)

    # --- RPC tier4-1: reference an asset, UsdPhysics setup (dry_run=True by default, undoable when they write) ---

    def add_reference(
        self,
        prim_path: str,
        asset_path: str,
        *,
        translation: Any = None,
        rotation: Any = None,
        scale: Any = None,
        instanceable: bool | None = None,
        asset_prim_path: str | None = None,
        correct_units: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Add (or with dry_run, preview adding) an Xform that references an asset file via asset_library.add_reference.

        The router resolves library-relative paths, so asset_path is normally absolute; the asset file is only read
        (a metadata-only anonymous open of its header), never written.
        """
        try:
            from .asset_library import add_reference as author_reference
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            return _module_unavailable("add_reference", "asset_library", exc)
        stage = self._require_stage()
        name = os.path.basename(str(asset_path or "")) or "asset"
        result = self._authored(
            f"Add {name} at {prim_path}",
            stage,
            prim_path,
            dry_run,
            lambda: author_reference(
                stage,
                prim_path,
                asset_path,
                translation=translation,
                rotation=rotation,
                scale=scale,
                instanceable=instanceable,
                asset_prim_path=asset_prim_path,
                correct_units=correct_units is True,
                dry_run=dry_run,
            ),
            creates=True,
        )
        return self._authoring_result(result, prim_path, dry_run, select=True)

    def physics_report(self, prim_path: str | None = None) -> dict[str, Any]:
        """Read-only UsdPhysics summary of the live stage (or one subtree) via physics.physics_report."""
        try:
            from .physics import physics_report as report
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("physics_report", "physics", exc)
        stage = self._require_stage()
        dirty_before = _dirty_layer_ids(stage)
        payload: dict[str, Any] = {"mode": "live", **report(stage, prim_path)}
        newly_dirty = sorted(_dirty_layer_ids(stage) - dirty_before)
        if newly_dirty:
            payload.setdefault("warnings", []).append(
                f"Read-only violation: these layers became dirty during the report: {newly_dirty[:10]}"
            )
        payload["note"] = LIVE_NOTE
        return _fit_json(payload, LIVE_MAX_RESULT_BYTES)

    def add_physics_scene(self, path: str = "/physicsScene", *, gravity: Any = None, dry_run: bool = True) -> dict[str, Any]:
        """Add (or with dry_run, preview adding) a PhysicsScene with explicit gravity (m/s^2; default 9.81 down)."""
        try:
            from . import physics
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("add_physics_scene", "physics", exc)
        stage = self._require_stage()
        wanted = physics.DEFAULT_GRAVITY if gravity is None else gravity
        result = self._authored(
            f"Add physics scene {path}",
            stage,
            path,
            dry_run,
            lambda: physics.add_physics_scene(stage, path, gravity=wanted, dry_run=dry_run),
            creates=True,
        )
        return self._authoring_result(result, path, dry_run, select=False)

    def make_rigid_body(
        self,
        prim_path: str,
        *,
        mass: float | None = None,
        density: float | None = None,
        kinematic: bool = False,
        add_collider: bool = True,
        approximation: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Apply (or with dry_run, preview applying) RigidBodyAPI, optional MassAPI and colliders below the prim."""
        try:
            from .physics import make_rigid_body as author_body
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("make_rigid_body", "physics", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Make {prim_path} a rigid body",
            stage,
            prim_path,
            dry_run,
            lambda: author_body(
                stage,
                prim_path,
                mass=mass,
                density=density,
                kinematic=kinematic,
                add_collider=add_collider,
                approximation=approximation,
                dry_run=dry_run,
            ),
            creates=True,  # colliders may go on descendants, so the whole subtree is snapshotted for undo
        )
        return self._authoring_result(result, prim_path, dry_run, select=False)

    def make_collider(self, prim_path: str, *, approximation: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        """Apply (or with dry_run, preview applying) CollisionAPI to a gprim or every gprim below a group prim."""
        try:
            from .physics import make_collider as author_collider
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("make_collider", "physics", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Add collision to {prim_path}",
            stage,
            prim_path,
            dry_run,
            lambda: author_collider(stage, prim_path, approximation=approximation, dry_run=dry_run),
            creates=True,  # subtree snapshot: colliders go on the gprims below a group prim
        )
        return self._authoring_result(result, prim_path, dry_run, select=False)

    def remove_physics(self, prim_path: str, *, include_descendants: bool = False, dry_run: bool = True) -> dict[str, Any]:
        """Remove (or with dry_run, preview removing) the RigidBody, Collision, MeshCollision and Mass APIs."""
        try:
            from .physics import remove_physics as author_removal
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("remove_physics", "physics", exc)
        stage = self._require_stage()
        result = self._authored(
            f"Remove physics from {prim_path}",
            stage,
            prim_path,
            dry_run,
            lambda: author_removal(stage, prim_path, include_descendants=include_descendants, dry_run=dry_run),
            creates=True,  # subtree snapshot: include_descendants edits every prim below
        )
        return self._authoring_result(result, prim_path, dry_run, select=False)

    # --- RPC tier4-1: viewport capture and simulation (kit_live_extras.py; Kit only, no authored scene data) -----

    def capture_viewport(self, output_dir: str | None = None, *, width: int | None = None, height: int | None = None) -> dict[str, Any]:
        """Schedule a PNG capture of the ACTIVE viewport (camera and render settings untouched); returns at once."""
        return self._extras("capture_viewport", lambda extras: extras.capture_viewport(output_dir, width=width, height=height))

    def capture_status(self, path: str) -> dict[str, Any]:
        """Whether a capture_viewport PNG has been written yet; never waits."""
        return self._extras("capture_status", lambda extras: extras.capture_status(path))

    def simulation_status(self) -> dict[str, Any]:
        """Timeline/physics state: playing, paused, stopped, current time. Read-only."""
        return self._extras("simulation_status", lambda extras: extras.simulation_status())

    def simulation_play(self) -> dict[str, Any]:
        """Play the timeline: physics simulates and moves simulated prims until stop restores them."""
        return self._extras("simulation_play", lambda extras: extras.simulation_play())

    def simulation_pause(self) -> dict[str, Any]:
        """Pause the timeline at the current time."""
        return self._extras("simulation_pause", lambda extras: extras.simulation_pause())

    def simulation_stop(self) -> dict[str, Any]:
        """Stop the timeline: it resets to the start time and PhysX restores pre-play poses."""
        return self._extras("simulation_stop", lambda extras: extras.simulation_stop())

    def simulation_step(self, frames: int = 1) -> dict[str, Any]:
        """Advance a paused (or stopped) simulation by frames (capped at 600)."""
        return self._extras("simulation_step", lambda extras: extras.simulation_step(frames))

    # --- RPC tier5-1: read-only homework check and scene tour (assignments.py, scene_tour.py; pure pxr) ----------

    def assignment_check(self, assignment_id: str = "isaac_session02_warehouse") -> dict[str, Any]:
        """Check the LIVE stage (unsaved edits included) against a homework rubric; reads only, never writes."""
        try:
            from .assignments import check_assignment
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("assignment_check", "assignments", exc)
        return self._read_only("assignment_check", lambda stage: check_assignment(stage, assignment_id))

    def scene_tour(self, focus_path: str | None = None, *, depth: int = 3, max_prims: int = 400) -> dict[str, Any]:
        """A beginner tour of the LIVE stage (or one subtree); reads only, never writes."""
        try:
            from .scene_tour import scene_tour as tour
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("scene_tour", "scene_tour", exc)
        return self._read_only("scene_tour", lambda stage: tour(stage, focus_path=focus_path, depth=depth, max_prims=max_prims))

    # --- RPC tier6-1: smart layout (layout.py; dry run by default, one undoable edit) and the robot inspector ----

    def layout_snap(self, paths: list[str], *, surface: Any = None, dry_run: bool = True) -> dict[str, Any]:
        """Drop (or lift) prims along the up axis onto the floor, a surface prim or a height; dry run by default."""
        return self._layout(
            "layout_snap", ("Snap", " onto a surface"), paths, dry_run,
            lambda layout, stage: layout.snap_to_surface(stage, paths, surface=surface, dry_run=dry_run),
        )

    def layout_stack(
        self,
        paths: list[str],
        *,
        rows: Any = None,
        anchor: Any = None,
        gap: float = 0.0,
        facing: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Stack prims in rows (bottom row first, default 4 3 2 1); dry run by default."""
        wanted_rows = (4, 3, 2, 1) if rows is None else rows
        return self._layout(
            "layout_stack", ("Stack", " in rows"), paths, dry_run,
            lambda layout, stage: layout.stack_rows(
                stage, paths, rows=wanted_rows, anchor=anchor, gap=gap, facing=facing, dry_run=dry_run
            ),
        )

    def layout_distribute(
        self, paths: list[str], surface_path: str, *, margin: float | None = None, dry_run: bool = True
    ) -> dict[str, Any]:
        """Spread prims evenly along a shelf or table top, resting on it; dry run by default."""
        return self._layout(
            "layout_distribute", ("Spread", f" on {surface_path}"), paths, dry_run,
            lambda layout, stage: layout.distribute_on(stage, paths, surface_path, margin=margin, dry_run=dry_run),
        )

    def layout_space(
        self, paths: list[str], *, axis: str = "x", spacing: float | None = None, dry_run: bool = True
    ) -> dict[str, Any]:
        """Make the gaps between prims equal along one axis; dry run by default."""
        return self._layout(
            "layout_space", ("Space", f" evenly along {axis}"), paths, dry_run,
            lambda layout, stage: layout.space_evenly(stage, paths, axis=axis, spacing=spacing, dry_run=dry_run),
        )

    def layout_align(self, paths: list[str], *, axis: str = "x", mode: str = "min", dry_run: bool = True) -> dict[str, Any]:
        """Line prims up along one axis (min sides, centers or max sides); dry run by default."""
        return self._layout(
            "layout_align", ("Align", f" along {axis} ({mode})"), paths, dry_run,
            lambda layout, stage: layout.align(stage, paths, axis=axis, mode=mode, dry_run=dry_run),
        )

    def robot_inspect(self, root_path: str | None = None, *, max_prims: int = 5000) -> dict[str, Any]:
        """Describe the robots (articulations) on the LIVE stage, or under root_path; reads only, never writes."""
        try:
            from .robot_inspector import inspect_robot
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("robot_inspect", "robot_inspector", exc)
        return self._read_only("robot_inspect", lambda stage: inspect_robot(stage, root_path, max_prims=max_prims))

    # --- RPC tier7-1: the layer manager (layer_manager.py) and the read-only asset health check ----------------

    def layer_stack(self) -> dict[str, Any]:
        """Every layer of the LIVE stage's layer stack, strongest first, plus referenced layers; reads only."""
        return self._layer_read("layer_stack", lambda manager, stage: manager.layer_stack(stage))

    def layer_where_is(self, prim_path: str, attribute: str | None = None, *, time: Any = None) -> dict[str, Any]:
        """Which layers of the LIVE stage hold opinions on a prim (or attribute) and which one wins; reads only."""
        return self._layer_read(
            "layer_where_is", lambda manager, stage: manager.where_is(stage, prim_path, attribute, time=time)
        )

    def layer_flatten_preview(self, prim_path: str, *, max_chars: int = 4000) -> dict[str, Any]:
        """One prim of the LIVE stage with every layer and arc baked in, as capped .usda text; reads only."""
        if not _is_prim_path(prim_path) or str(prim_path).strip() == "/":
            raise ValueError("layer_flatten_preview needs one prim path such as '/World/Cube'")
        return self._layer_read(
            "layer_flatten_preview",
            lambda manager, stage: manager.flatten_preview(stage, prim_path, max_chars=max_chars),
        )

    def layer_add_sublayer(
        self,
        layer_path: str,
        *,
        position: Any = "strongest",
        create_if_missing: bool = False,
        relative: bool = True,
        allow_outside: bool = False,
        offset: float = 0.0,
        scale: float = 1.0,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Plan (dry run, the default) or insert a sublayer into the LIVE stage's root layer."""
        return self._layer_write(
            "layer_add_sublayer", dry_run,
            lambda manager, stage: manager.add_sublayer(
                stage, layer_path, position=position, create_if_missing=create_if_missing, relative=relative,
                allow_outside=allow_outside, offset=offset, scale=scale, dry_run=dry_run,
            ),
        )

    def layer_remove_sublayer(self, layer: str, *, discard_unsaved_edits: bool = False, dry_run: bool = True) -> dict[str, Any]:
        """Plan (dry run, the default) or take a sublayer out of its parent's list (the file is never deleted)."""
        return self._layer_write(
            "layer_remove_sublayer", dry_run,
            lambda manager, stage: manager.remove_sublayer(
                stage, layer, discard_unsaved_edits=discard_unsaved_edits, dry_run=dry_run
            ),
        )

    def layer_set_edit_target(self, layer: str, *, dry_run: bool = True) -> dict[str, Any]:
        """Plan (dry run, the default) or make one layer of the LIVE stage the edit target."""
        return self._layer_write(
            "layer_set_edit_target", dry_run,
            lambda manager, stage: manager.set_edit_target(stage, layer, dry_run=dry_run),
        )

    def layer_mute(self, layer: str, *, discard_unsaved_edits: bool = False, dry_run: bool = True) -> dict[str, Any]:
        """Plan (dry run, the default) or mute a layer of the LIVE stage (stage state; unmute brings it back)."""
        return self._layer_write(
            "layer_mute", dry_run,
            lambda manager, stage: manager.mute_layer(
                stage, layer, discard_unsaved_edits=discard_unsaved_edits, dry_run=dry_run
            ),
        )

    def layer_unmute(self, layer: str, *, dry_run: bool = True) -> dict[str, Any]:
        """Plan (dry run, the default) or unmute a muted layer of the LIVE stage."""
        return self._layer_write(
            "layer_unmute", dry_run, lambda manager, stage: manager.unmute_layer(stage, layer, dry_run=dry_run)
        )

    def layer_move_opinions(
        self,
        prim_path: str,
        src_layer: str,
        dst_layer: str,
        *,
        include_children: bool = True,
        properties: list[str] | None = None,
        allow_changes: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Plan (dry run, the default) or move a prim's opinions between two local layers; the move goes on the
        bridge's MCP undo stack (scene_undo_last), not on Kit's Ctrl+Z history."""
        stack = self._undo_stack if _writes(dry_run) else None
        result = self._layer_write(
            "layer_move_opinions", dry_run,
            lambda manager, stage: manager.move_opinions(
                stage, prim_path, src_layer, dst_layer, include_children=include_children, properties=properties,
                allow_changes=allow_changes, undo_stack=stack, dry_run=dry_run,
            ),
        )
        undo_info = result.get("undo") if isinstance(result, dict) else None
        if isinstance(undo_info, dict) and undo_info.get("recorded"):
            result["undoable"] = True
        return result

    def asset_health(self, root_path: str | None = None, *, max_prims: int = 50_000) -> dict[str, Any]:
        """Health-check the LIVE stage (or the subtree at root_path) for asset problems; reads only, never fixes."""
        try:
            from .asset_health import asset_health
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable("asset_health", "asset_health", exc)
        return self._read_only("asset_health", lambda stage: asset_health(stage, root_path=root_path, max_prims=max_prims))

    def _layer_read(self, method: str, run: Callable[[Any, Any], Any]) -> dict[str, Any]:
        try:
            from . import layer_manager
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            return _module_unavailable(method, "layer_manager", exc)
        return self._read_only(method, lambda stage: run(layer_manager, stage))

    def _layer_write(self, method: str, dry_run: Any, run: Callable[[Any, Any], Any]) -> dict[str, Any]:
        """Run one writing layer_manager function on the live stage; it plans first and writes only for dry_run=False.

        These are raw USD calls that bypass Kit's own layer commands, so Kit's Layer window may lag behind and Kit's
        Ctrl+Z does not undo them: results carry restore_with / undo (the inverse call) and a kit_note saying so.
        """
        try:
            from . import layer_manager
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable(method, "layer_manager", exc)
        stage = self._require_stage()
        result = run(layer_manager, stage)
        payload: dict[str, Any] = dict(result) if isinstance(result, dict) else {"ok": True, "result": result}
        payload.setdefault("mode", "live")
        payload["kit_note"] = _KIT_LAYER_NOTE
        if payload.get("applied"):
            warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
            payload["warnings"] = [*warnings, _KIT_LAYER_NOTE]
        return _fit_json(payload, LIVE_MAX_RESULT_BYTES)

    def _layout(self, method: str, label: tuple[str, str], paths: Any, dry_run: Any, run: Callable[[Any, Any], Any]) -> dict[str, Any]:
        """Run one layout function: directly for a dry run, as one undoable edit (prim-only snapshots) when it writes."""
        try:
            from . import layout
        except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
            return _module_unavailable(method, "layout", exc)
        stage = self._require_stage()
        listed = [paths] if isinstance(paths, str) else list(paths) if isinstance(paths, (list, tuple)) else []
        targets = [path for path in listed if _is_prim_path(path)]
        text = f"{label[0]} {len(listed)} prim(s){label[1]}"
        if not _writes(dry_run) or kit_commands is None or self._undo_stack is None or not targets:
            result = run(layout, stage)
        else:
            top_before = self._undo_stack.peek()
            # Only each prim's own translate op (and xformOpOrder) changes, so prim-only snapshots are enough.
            result = self._undoable(text, stage, [], lambda: run(layout, stage), shallow=targets)
            if isinstance(result, dict) and result.get("ok") and self._undo_stack.peek() is not top_before:
                result = {**result, "undoable": True}
        return self._authoring_result(result, targets[0] if targets else "", dry_run, select=False)

    def _read_only(self, method: str, call: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        """Run one read-only pxr function on the live stage and report any layer it dirtied (it never should)."""
        stage = self._require_stage()
        dirty_before = _dirty_layer_ids(stage)
        started = time.perf_counter()
        result = call(stage)
        payload: dict[str, Any] = dict(result) if isinstance(result, dict) else {"ok": True, "result": result}
        payload.update(_live_layer_state(stage))
        newly_dirty = sorted(_dirty_layer_ids(stage) - dirty_before)
        if newly_dirty:
            warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
            payload["warnings"] = [*warnings, f"Read-only violation: these layers became dirty during {method}: "
                                   f"{newly_dirty[:10]}"]
        payload.update(mode="live", action=method, elapsed_ms=int((time.perf_counter() - started) * 1000), note=LIVE_NOTE)
        return _fit_json(payload, LIVE_MAX_RESULT_BYTES)

    def _extras(self, method: str, call: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
        try:
            from . import kit_live_extras
        except Exception as exc:  # noqa: BLE001
            return _module_unavailable(method, "kit_live_extras", exc)
        result = call(kit_live_extras)
        payload = dict(result) if isinstance(result, dict) else {"ok": True, "result": result}
        payload.setdefault("mode", "live")
        return _fit_json(payload, LIVE_MAX_RESULT_BYTES)

    # --- undo (see undo.py and kit_commands.py) -----------------------------------------------------------------

    def undo_last(self, dry_run: bool = True) -> dict[str, Any]:
        """Undo (dry_run=True, the default: only preview) the newest MCP edit recorded on the open stage.

        Refuses when the stage was closed/replaced or the edited prims changed since (undo Kit's later edits with
        Ctrl+Z first; a change made outside Kit's undo history, such as a script, blocks Ctrl+Z on this edit too).
        Saving is not undoable: this reverts the in-memory layers; save again to write the reverted state.
        """
        if kit_commands is None or self._undo_stack is None:
            return _undo_unavailable("undo_last")
        dry = not _writes(dry_run)
        stage = self._stage()
        if stage is None:
            return {"ok": False, "dry_run": dry, "undone": None, "mode": "live", "message": "No live USD stage is open"}
        result = kit_commands.undo_last(stage, dry_run=dry, stack=self._undo_stack, undo_module=_kit_module("undo"))
        result["mode"] = "live"
        if result.get("ok") and not dry:
            self._drop_missing_from_selection(stage)
        return _fit_json(result, LIVE_MAX_RESULT_BYTES)

    def undo_history(self, limit: int = 20) -> dict[str, Any]:
        """The newest recorded MCP edits (up to limit, 1-50), newest first. Read-only."""
        if kit_commands is None or self._undo_stack is None:
            return _undo_unavailable("undo_history")
        stack = self._undo_stack
        payload = {
            "ok": True,
            "mode": "live",
            "count": len(stack),
            "max_entries": stack.max_entries,
            "dropped": stack.dropped,
            "entries": stack.history(limit, stage=self._stage()),
            "kit_ctrl_z": _kit_module("commands") is not None,
            "note": kit_commands.SAVE_NOTE,
        }
        if stack.notes:
            payload["notes"] = stack.notes
        return _fit_json(payload, LIVE_MAX_RESULT_BYTES)

    def _undo_info(self) -> dict[str, Any]:
        if kit_commands is None or self._undo_stack is None:
            error = _UNDO_IMPORT_ERROR
            detail = f"{type(error).__name__}: {error}" if error is not None else "the bridge has no undo stack"
            return {"available": False, "error": _clip(detail)}
        return {
            "available": kit_commands.undo.Sdf is not None,
            "kit_ctrl_z": _kit_module("commands") is not None,
            "methods": ["undo_last", "undo_history"],
            "recorded": len(self._undo_stack),
            "max_entries": self._undo_stack.max_entries,
            "dry_run_default": True,
            "note": kit_commands.SAVE_NOTE,
        }

    def _undoable(self, label: str, stage, paths, fn, *, shallow=(), namespace_edit: bool = False):
        """Run one mutating edit so it can be undone; dry runs and read-only methods never come here."""
        if kit_commands is None or self._undo_stack is None:
            return fn()
        return kit_commands.run_undoable(
            label,
            stage,
            paths,
            fn,
            shallow=shallow,
            namespace_edit=namespace_edit,
            stack=self._undo_stack,
            commands=_kit_module("commands"),
        )

    def _authored(self, label: str, stage, path: str, dry_run: Any, run, *, creates: bool) -> Any:
        """Call an authoring function: directly for a dry run, as one undoable edit when it writes."""
        if not _writes(dry_run) or kit_commands is None or self._undo_stack is None or not _is_prim_path(path):
            return run()
        top_before = self._undo_stack.peek()
        paths, shallow = ([path], []) if creates else ([], [path])
        result = self._undoable(label, stage, paths, run, shallow=shallow)
        if isinstance(result, dict) and result.get("ok") and self._undo_stack.peek() is not top_before:
            result = {**result, "undoable": True}
        return result

    def _drop_missing_from_selection(self, stage) -> None:
        """After an undo, keep only selected prims that still exist (a UI nicety; never fails the undo)."""
        try:
            selection = self._usd_context.get_selection()
            selected = list(selection.get_selected_prim_paths())
            kept = [p for p in selected if stage.GetPrimAtPath(p).IsValid()]
            if kept != selected:
                selection.set_selected_prim_paths(kept, True)
        except Exception:  # noqa: BLE001
            pass

    def _authoring_result(self, result: Any, path: str, dry_run: bool, select: bool) -> dict[str, Any]:
        payload = dict(result) if isinstance(result, dict) else {"result": result}
        payload.setdefault("mode", "live")
        if select and not dry_run and payload.get("ok", True):
            created = payload.get("path") if isinstance(payload.get("path"), str) else path
            stage = self._stage()
            try:
                if stage is not None and Sdf.Path.IsValidPathString(created) and stage.GetPrimAtPath(created).IsValid():
                    self._select([created])
            except Exception:  # noqa: BLE001 - selecting the new prim is a UI nicety; the edit already happened
                pass
        return _fit_json(payload, LIVE_MAX_RESULT_BYTES)

    def _stage(self) -> Usd.Stage | None:
        return self._usd_context.get_stage()

    def _require_stage(self) -> Usd.Stage:
        stage = self._stage()
        if stage is None:
            raise ValueError("No live USD stage is open")
        return stage

    def _require_prim(self, path: str):
        stage = self._require_stage()
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            raise ValueError(f"Prim does not exist: {path}")
        return prim

    def _select(self, paths: list[str]) -> None:
        selection = self._usd_context.get_selection()
        selection.set_selected_prim_paths(paths, True)

    def _clear_selection_if_present(self, path: str) -> None:
        selection = self._usd_context.get_selection()
        selected = [p for p in selection.get_selected_prim_paths() if p != path]
        selection.set_selected_prim_paths(selected, True)

    def _prim_to_record(self, prim) -> PrimRecord:
        parent = prim.GetParent()
        parent_path = str(parent.GetPath()) if parent and parent.IsValid() and not parent.IsPseudoRoot() else None
        translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
        rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
        scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
        if UsdGeom.Xformable(prim):
            # Decomposes orient and matrix op stacks to XYZ rotation; compatible stacks keep their own op values.
            vectors = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
            if vectors:
                translation = tuple(float(value) for value in vectors[0])
                rotation = tuple(float(value) for value in vectors[1])
                scale = tuple(float(value) for value in vectors[2])

        material_path = None
        try:
            bound_material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
            if bound_material:
                material_path = str(bound_material.GetPath())
        except Exception:
            material_path = None

        return PrimRecord(
            path=str(prim.GetPath()),
            type_name=prim.GetTypeName() or "Xform",
            parent=parent_path,
            children=[str(child.GetPath()) for child in prim.GetChildren()],
            translation=translation,
            rotation=rotation,
            scale=scale,
            material=material_path,
        )

    def _move_with_existing_ops(self, path: str, xformable, ops, translation, rotation, scale) -> None:
        """Edit op stacks XformCommonAPI rejects (Isaac's translate/orient/scale, or one matrix) in place."""
        op_types = [op.GetOpType() for op in ops]
        if op_types == [UsdGeom.XformOp.TypeTransform]:
            transform = Gf.Transform(ops[0].Get() or Gf.Matrix4d(1.0))
            if translation is not None:
                transform.SetTranslation(Gf.Vec3d(*translation))
            if rotation is not None:
                transform.SetRotation(_rotation_from_xyz(rotation))
            if scale is not None:
                transform.SetScale(Gf.Vec3d(*scale))
            ops[0].Set(transform.GetMatrix())
            return

        canonical = [UsdGeom.XformOp.TypeTranslate, UsdGeom.XformOp.TypeOrient, UsdGeom.XformOp.TypeScale]
        if any(op.IsInverseOp() for op in ops) or op_types != [t for t in canonical if t in op_types]:
            order = [op.GetOpName() for op in ops]
            raise ValueError(
                f"Cannot move {path}: unsupported xformOpOrder {order}. Supported stacks are "
                "XformCommonAPI-compatible ops, translate/orient/scale, or a single transform matrix."
            )

        by_type = dict(zip(op_types, ops))
        if translation is not None and UsdGeom.XformOp.TypeTranslate not in by_type:
            by_type[UsdGeom.XformOp.TypeTranslate] = xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
        if scale is not None and UsdGeom.XformOp.TypeScale not in by_type:
            by_type[UsdGeom.XformOp.TypeScale] = xformable.AddScaleOp(UsdGeom.XformOp.PrecisionDouble)
        xformable.SetXformOpOrder([by_type[t] for t in canonical if t in by_type])

        if translation is not None:
            translate_op = by_type[UsdGeom.XformOp.TypeTranslate]
            translate_op.Set(_vec3_for_op(translate_op, translation))
        if rotation is not None:
            orient_op = by_type[UsdGeom.XformOp.TypeOrient]
            orient_op.Set(_quat_for_op(orient_op, _rotation_from_xyz(rotation).GetQuat()))
        if scale is not None:
            scale_op = by_type[UsdGeom.XformOp.TypeScale]
            scale_op.Set(_vec3_for_op(scale_op, scale))

    def _copy_prim_specs(self, stage: Usd.Stage, source_prim, target: Sdf.Path):
        """Copy the source's authored opinions (attributes, schemas, arcs, children) into the edit target."""
        source_path = source_prim.GetPath()
        edit_target = stage.GetEditTarget()
        edit_layer = edit_target.GetLayer()
        layers = [layer for layer in stage.GetLayerStack() if layer.GetPrimAtPath(source_path)]
        if not layers:
            raise ValueError(
                f"Cannot duplicate {source_path}: it has no opinions in this stage's layers "
                "(it comes from a reference, payload or instance). Duplicate the prim that holds the reference."
            )
        if layers == [edit_layer]:
            source_layer = edit_layer
        else:
            target_dir = None if edit_layer.anonymous else os.path.dirname(edit_layer.realPath)
            source_layer = UsdUtils.FlattenLayerStack(
                stage, lambda layer, asset_path: _reanchor_asset_path(layer, asset_path, target_dir)
            )

        target_spec_path = edit_target.MapToSpecPath(target)
        parent_spec_path = target_spec_path.GetParentPath()
        if parent_spec_path != Sdf.Path.absoluteRootPath and not edit_layer.GetPrimAtPath(parent_spec_path):
            Sdf.CreatePrimInLayer(edit_layer, parent_spec_path)
        if not Sdf.CopySpec(source_layer, source_path, edit_layer, target_spec_path):
            raise ValueError(f"Failed to copy {source_path} to {target}")

        clone = stage.GetPrimAtPath(target)
        if not clone or not clone.IsDefined():
            edit = Sdf.BatchNamespaceEdit()
            edit.Add(Sdf.NamespaceEdit.Remove(target_spec_path))
            edit_layer.Apply(edit)
            raise ValueError(
                f"Cannot duplicate {source_path}: its local opinions are only overrides on content defined "
                "by a reference or payload. Duplicate the prim that holds the reference."
            )
        return clone


def _run_stage_info(stage: Any, _args: dict[str, Any]) -> dict[str, Any]:
    from . import usd_insight

    return usd_insight.stage_info(stage)


def _run_composition(stage: Any, args: dict[str, Any]) -> dict[str, Any]:
    from . import usd_insight

    return usd_insight.composition(stage, args["prim_path"])


def _run_explain_value(stage: Any, args: dict[str, Any]) -> dict[str, Any]:
    from . import usd_insight

    return usd_insight.explain_value(stage, args["prim_path"], args.get("attribute"), args.get("time"))


def _run_properties(stage: Any, args: dict[str, Any]) -> dict[str, Any]:
    from . import usd_insight

    return usd_insight.properties(
        stage,
        args["prim_path"],
        include_values=args.get("include_values", True),
        time=args.get("time"),
        authored_only=args.get("authored_only", False),
    )


def _run_bounds(stage: Any, args: dict[str, Any]) -> dict[str, Any]:
    from . import usd_insight

    return usd_insight.bounds(
        stage,
        args["prim_path"],
        purposes=args.get("purposes", ("default", "render")),
        time=args.get("time"),
        use_extents_hint=args.get("use_extents_hint", False),
    )


def _run_validate(stage: Any, args: dict[str, Any]) -> dict[str, Any]:
    from . import usd_validate

    return usd_validate.validate(stage, args["profile"], args["max_issues"], max_prims=args["max_prims"])


# Strict allowlist: query name -> the function that runs it. Never resolved with getattr on caller input.
_INSIGHT_RUNNERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "stage_info": _run_stage_info,
    "composition": _run_composition,
    "explain_value": _run_explain_value,
    "properties": _run_properties,
    "bounds": _run_bounds,
    "validate": _run_validate,
}


def _insight_args(query: str, args: Any) -> dict[str, Any]:
    """Check args against the query's allowlist and coerce each value; unknown names are rejected."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError(f"Insight args must be a JSON object, not {type(args).__name__}")
    allowed = _INSIGHT_ARGS[query]
    clean: dict[str, Any] = {}
    for key, value in args.items():
        name = _ARG_ALIASES.get(key, key) if isinstance(key, str) else key
        if name not in allowed:
            raise ValueError(
                f"Unknown argument {_clip(repr(key), 60)} for insight query '{query}'; allowed: {list(allowed) or 'none'}"
            )
        if name in clean:
            raise ValueError(f"Argument '{name}' was given twice (prim_path and its alias path)")
        if value is not None:
            clean[name] = _ARG_PARSERS[name](name, value)
    if "prim_path" in allowed and "prim_path" not in clean:
        raise ValueError(f"Insight query '{query}' needs prim_path")
    if query == "validate":
        clean.setdefault("profile", "exam")
        clean.setdefault("max_issues", LIVE_DEFAULT_MAX_ISSUES)
        clean.setdefault("max_prims", LIVE_DEFAULT_MAX_PRIMS)
    return clean


def _path_arg(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty prim path string")
    text = value.strip()
    if len(text) > _MAX_PATH_CHARS:
        raise ValueError(f"{name} is longer than {_MAX_PATH_CHARS} characters")
    return text


def _name_arg(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_NAME_CHARS:
        raise ValueError(f"{name} must be a non-empty string of at most {_MAX_NAME_CHARS} characters")
    return value.strip()


def _time_arg(name: str, value: Any) -> float | int | str:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number, 'default' or 'earliest'")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be a finite number")
        return value
    if isinstance(value, str) and len(value) <= 32:
        return value.strip()
    raise ValueError(f"{name} must be a number, 'default' or 'earliest'")


def _bool_arg(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"{name} must be true or false")


def _purposes_arg(name: str, value: Any) -> tuple[str, ...]:
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not items or len(items) > len(_PURPOSES):
        raise ValueError(f"{name} must list 1-4 of {list(_PURPOSES)}")
    purposes = []
    for item in items:
        word = item.strip().lower() if isinstance(item, str) else None
        if word not in _PURPOSES:
            raise ValueError(f"{name} must only contain {list(_PURPOSES)}, not {_clip(repr(item), 40)}")
        purposes.append(word)
    return tuple(purposes)


def _profile_arg(name: str, value: Any) -> str:
    word = value.strip().lower() if isinstance(value, str) else None
    if word not in _VALIDATE_PROFILES:
        raise ValueError(f"{name} must be one of {list(_VALIDATE_PROFILES)}")
    return word


def _bounded_int(low: int, high: int) -> Callable[[str, Any], int]:
    def parse(name: str, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be a whole number")
        return max(low, min(int(value), high))

    return parse


_ARG_PARSERS: dict[str, Callable[[str, Any], Any]] = {
    "prim_path": _path_arg,
    "attribute": _name_arg,
    "time": _time_arg,
    "include_values": _bool_arg,
    "authored_only": _bool_arg,
    "use_extents_hint": _bool_arg,
    "purposes": _purposes_arg,
    "profile": _profile_arg,
    "max_issues": _bounded_int(0, LIVE_MAX_ISSUES),
    "max_prims": _bounded_int(1, LIVE_MAX_PRIMS),
}


def _stage_layers(stage: Any) -> list[Any]:
    layers = list(stage.GetUsedLayers())
    known = {layer.identifier for layer in layers}
    # GetUsedLayers() leaves muted layers out, but their unsaved edits still exist (a released one is gone: None).
    muted = [Sdf.Layer.Find(identifier) for identifier in stage.GetMutedLayers()]
    for extra in (stage.GetRootLayer(), stage.GetSessionLayer(), *muted):
        if extra is not None and extra.identifier not in known:
            layers.append(extra)
            known.add(extra.identifier)
    return layers


def _dirty_layer_ids(stage: Any) -> set[str]:
    return {layer.identifier for layer in _stage_layers(stage) if layer.dirty}


def _live_layer_state(stage: Any) -> dict[str, Any]:
    """Which layers hold unsaved edits (session layer reported separately: Kit never saves it)."""
    root = stage.GetRootLayer()
    session = stage.GetSessionLayer()
    session_id = session.identifier if session is not None else None
    unsaved = [
        {"identifier": layer.identifier, "anonymous": bool(layer.anonymous), "is_root_layer": layer.identifier == root.identifier}
        for layer in _stage_layers(stage)
        if layer.dirty and layer.identifier != session_id
    ]
    return {
        "stage_identifier": root.identifier,
        "root_layer_anonymous": bool(root.anonymous),
        "has_unsaved_changes": bool(unsaved),
        "unsaved_layers": unsaved[:_MAX_UNSAVED_LAYERS],
        "unsaved_layer_count": len(unsaved),
        "session_layer_has_edits": bool(session is not None and not session.empty),
    }


def _fit_json(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Return a plain-JSON copy of payload, trimming lists (then long strings) until it fits in max_bytes."""
    text = json.dumps(payload, default=str)
    if len(text) <= max_bytes:
        return json.loads(text)
    data = json.loads(text)
    for list_cap, text_cap in ((50, 2000), (20, 1000), (10, 500), (5, 200), (2, 100), (1, 80), (0, 80)):
        trimmed = _trim(data, list_cap, text_cap)
        trimmed["size_truncated"] = {"max_bytes": max_bytes, "original_bytes": len(text), "list_cap": list_cap}
        if len(json.dumps(trimmed)) <= max_bytes:
            return trimmed
    raise ValueError(f"The live result is larger than {max_bytes} bytes even after trimming; narrow the query.")


def _trim(value: Any, list_cap: int, text_cap: int) -> Any:
    if isinstance(value, dict):
        return {key: _trim(item, list_cap, text_cap) for key, item in value.items()}
    if isinstance(value, list):
        return [_trim(item, list_cap, text_cap) for item in value[:list_cap]]
    if isinstance(value, str) and len(value) > text_cap:
        return value[:text_cap] + "..."
    return value


def _clip(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _authoring_unavailable(method: str, exc: Exception) -> dict[str, Any]:
    return unsupported_result(
        method,
        "Live light/camera authoring is not available in this bridge: omniverse_scene_mcp.authoring could not be "
        f"imported ({_clip(f'{type(exc).__name__}: {exc}', 200)}). Update the package and reload the bridge.",
        needs_bridge_update=True,
    )


def _edits_unavailable(method: str, exc: Exception) -> dict[str, Any]:
    return unsupported_result(
        method,
        "Live attribute/material edits are not available in this bridge: omniverse_scene_mcp.edits could not be "
        f"imported ({_clip(f'{type(exc).__name__}: {exc}', 200)}). Update the package and reload the bridge.",
        needs_bridge_update=True,
    )


def _module_unavailable(method: str, module: str, exc: Exception) -> dict[str, Any]:
    return unsupported_result(
        method,
        f"'{method}' is not available in this bridge: omniverse_scene_mcp.{module} could not be imported "
        f"({_clip(f'{type(exc).__name__}: {exc}', 200)}). Update the package and reload the bridge.",
        needs_bridge_update=True,
    )


def _module_info(module: str, methods: tuple[str, ...], *, dry_run_default: bool | None = None) -> dict[str, Any]:
    """Whether omniverse_scene_mcp.<module> imports here (capabilities); only checks the import."""
    try:
        importlib.import_module(f"{__package__}.{module}")
    except Exception as exc:  # noqa: BLE001 - ImportError, or an API missing in this USD build
        info: dict[str, Any] = {"available": False, "error": _clip(f"{type(exc).__name__}: {exc}")}
    else:
        info = {"available": True}
    info["methods"] = list(methods)
    if dry_run_default is not None:
        info["dry_run_default"] = dry_run_default
    return info


def _homework_info() -> dict[str, Any]:
    """Capabilities entry for the tier5-1 homework methods: both modules must import here."""
    first = _module_info("assignments", HOMEWORK_RPC_METHODS)
    second = _module_info("scene_tour", HOMEWORK_RPC_METHODS)
    if not first["available"]:
        return first
    return second


def _undo_unavailable(method: str) -> dict[str, Any]:
    error = _UNDO_IMPORT_ERROR
    detail = f" ({_clip(f'{type(error).__name__}: {error}', 200)})" if error is not None else ""
    return unsupported_result(
        method,
        f"Undo is not available in this bridge: omniverse_scene_mcp.kit_commands could not be loaded{detail}. "
        "Update the package and reload the bridge; Ctrl+Z in Kit still undoes Kit's own edits.",
        needs_bridge_update=True,
    )


def _kit_module(name: str) -> Any:
    """omni.kit.<name> as reachable from this module's omni import; None outside Kit (or when omni is faked)."""
    kit = getattr(omni, "kit", None) if omni is not None else None
    return getattr(kit, name, None) if kit is not None else None


def _writes(dry_run: Any) -> bool:
    """Mirror authoring._dry: only an explicit false value (False or 'false'/'0'/'no'/'off') writes."""
    if isinstance(dry_run, str):
        return dry_run.strip().lower() in ("false", "0", "no", "off")
    return dry_run is False


def _is_prim_path(path: Any) -> bool:
    try:
        kit_commands.undo.prim_path(path)
    except Exception:  # noqa: BLE001 - UndoError or no pxr: let the authoring call report the bad path itself
        return False
    return True


def _child_path_or_none(parent, name: str) -> str | None:
    """The path a rename would produce, or None when name is not a valid prim name (the rename then fails)."""
    try:
        base = parent.GetPath() if parent and parent.IsValid() else Sdf.Path.absoluteRootPath
        child = base.AppendChild(name)
    except Exception:  # noqa: BLE001 - Tf error for an invalid name
        return None
    return str(child) if child and not child.isEmpty else None


def _rotation_from_xyz(rotation: tuple[float, float, float]):
    """Match xformOp:rotateXYZ: rotate about X, then Y, then Z (degrees)."""
    return (
        Gf.Rotation(Gf.Vec3d.XAxis(), rotation[0])
        * Gf.Rotation(Gf.Vec3d.YAxis(), rotation[1])
        * Gf.Rotation(Gf.Vec3d.ZAxis(), rotation[2])
    )


def _vec3_for_op(op, values: tuple[float, float, float]):
    precision = op.GetPrecision()
    if precision == UsdGeom.XformOp.PrecisionFloat:
        return Gf.Vec3f(*values)
    if precision == UsdGeom.XformOp.PrecisionHalf:
        return Gf.Vec3h(*values)
    return Gf.Vec3d(*values)


def _quat_for_op(op, quat):
    precision = op.GetPrecision()
    if precision == UsdGeom.XformOp.PrecisionFloat:
        return Gf.Quatf(quat.GetReal(), Gf.Vec3f(quat.GetImaginary()))
    if precision == UsdGeom.XformOp.PrecisionHalf:
        return Gf.Quath(quat.GetReal(), Gf.Vec3h(quat.GetImaginary()))
    return Gf.Quatd(quat.GetReal(), Gf.Vec3d(quat.GetImaginary()))


def _same_file(first: str, second: str) -> bool:
    # Accept 'file:/C:/...' style paths the same way the dry-run preview (service._layer_key) does.
    if second[:5].lower() == "file:":
        second = re.sub(r"^/+(?=[A-Za-z]:)", "", second[5:])
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def _reanchor_asset_path(source_layer, asset_path: str, target_dir: str | None) -> str:
    """Rewrite a layer-relative asset path so it still finds the same file when authored in target_dir."""
    if not asset_path or source_layer.anonymous or "://" in asset_path or os.path.isabs(asset_path):
        return asset_path
    if Sdf.Layer.IsAnonymousLayerIdentifier(asset_path):
        return asset_path
    source_dir = os.path.dirname(source_layer.realPath)
    if target_dir is not None and os.path.normcase(os.path.abspath(source_dir)) == os.path.normcase(
        os.path.abspath(target_dir)
    ):
        return asset_path
    absolute = source_layer.ComputeAbsolutePath(asset_path)
    # Plain names may be resolver search paths; only rewrite them when they resolve next to the layer.
    if not asset_path.startswith(("./", "../", ".\\", "..\\")) and not os.path.exists(absolute):
        return asset_path
    if target_dir is None:
        return absolute.replace("\\", "/")
    try:
        relative = os.path.relpath(absolute, target_dir).replace("\\", "/")
    except ValueError:  # different Windows drive
        return absolute.replace("\\", "/")
    return relative if relative.startswith("../") else f"./{relative}"
