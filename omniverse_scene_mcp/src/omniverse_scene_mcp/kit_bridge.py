from __future__ import annotations

import os
import re

from .bridge import PrimRecord, SceneSnapshot

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


class LiveKitSceneBridge:
    """Bridge a live Omniverse Kit stage into the MCP service layer."""

    def __init__(self, usd_context_name: str = "") -> None:
        if omni is None or Gf is None or Usd is None or UsdGeom is None or UsdShade is None:
            raise ImportError(
                "LiveKitSceneBridge requires Omniverse Kit's USD bindings. "
                "Run this inside Kit or an Omniverse-enabled Python environment."
            ) from (_OMNI_USD_IMPORT_ERROR or _PXR_IMPORT_ERROR)
        self._usd_context = omni.usd.get_context(usd_context_name)

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

    def create_prim(self, path: str, type_name: str = "Xform") -> PrimRecord:
        stage = self._require_stage()
        prim = stage.DefinePrim(path, type_name)
        self._select([path])
        return self._prim_to_record(prim)

    def delete_prim(self, path: str) -> None:
        stage = self._require_stage()
        prim = self._require_prim(path)
        stage.RemovePrim(prim.GetPath())
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
        clone = self._copy_prim_specs(stage, source, target)
        self._select([target_path])
        return self._prim_to_record(clone)

    def reparent_prim(self, path: str, new_parent: str) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)
        new_parent_prim = self._require_prim(new_parent)
        editor = Usd.NamespaceEditor(stage)
        if not editor.ReparentPrim(prim, new_parent_prim):
            raise ValueError(f"Failed to reparent {path} under {new_parent}")
        if not editor.ApplyEdits():
            raise ValueError(f"Unable to apply namespace edit for {path}")
        moved = stage.GetPrimAtPath(new_parent_prim.GetPath().AppendChild(prim.GetName()))
        if not moved or not moved.IsValid():
            raise ValueError(f"Reparented prim is not accessible at the expected path under {new_parent}")
        self._select([str(moved.GetPath())])
        return self._prim_to_record(moved)

    def rename_prim(self, path: str, new_name: str) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)
        editor = Usd.NamespaceEditor(stage)
        if not editor.RenamePrim(prim, new_name):
            raise ValueError(f"Failed to rename {path} to {new_name}")
        if not editor.ApplyEdits():
            raise ValueError(f"Unable to apply rename for {path}")
        parent = prim.GetParent()
        renamed_path = parent.GetPath().AppendChild(new_name) if parent and parent.IsValid() else f"/{new_name}"
        renamed = stage.GetPrimAtPath(renamed_path)
        if not renamed or not renamed.IsValid():
            raise ValueError(f"Renamed prim is not accessible at the expected path: {renamed_path}")
        self._select([str(renamed.GetPath())])
        return self._prim_to_record(renamed)

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
    ) -> PrimRecord:
        prim = self._require_prim(path)
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
        return self._prim_to_record(prim)

    def set_material(self, path: str, material_path: str) -> PrimRecord:
        stage = self._require_stage()
        prim = self._require_prim(path)
        material_prim = stage.GetPrimAtPath(material_path)
        if not material_prim or not material_prim.IsValid():
            raise ValueError(f"Material prim does not exist: {material_path}")
        material = UsdShade.Material(material_prim)
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)
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
        if not variants.SetVariantSelection(selection):
            raise ValueError(f"Failed to select {variant_set}={selection} on {path}")
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

        material_path = "/World/Looks/RedMaterial"
        looks_path = stage.GetPrimAtPath("/World/Looks")
        if not looks_path or not looks_path.IsValid():
            stage.DefinePrim("/World/Looks", "Scope")

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
        return self._prim_to_record(prim)

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
