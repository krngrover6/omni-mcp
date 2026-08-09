from __future__ import annotations

from .bridge import PrimRecord, SceneSnapshot

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError as exc:  # pragma: no cover - only available with USD bindings
    Gf = Sdf = Usd = UsdGeom = UsdShade = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR = exc
else:  # pragma: no cover - only available with USD bindings
    _PXR_IMPORT_ERROR = None

try:
    import omni.usd
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
        clone = self._duplicate_subtree(stage, source, target_path)
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
        xform = UsdGeom.XformCommonAPI(prim)
        if translation is not None:
            xform.SetTranslate(Gf.Vec3d(*translation))
        if rotation is not None:
            xform.SetRotate(Gf.Vec3f(*rotation), UsdGeom.XformCommonAPI.RotationOrderXYZ)
        if scale is not None:
            xform.SetScale(Gf.Vec3f(*scale))
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
        xformable = UsdGeom.Xformable(prim)
        if xformable:
            for op in xformable.GetOrderedXformOps():
                try:
                    op_type = op.GetOpType()
                    value = op.Get()
                except Exception:
                    continue

                if op_type == UsdGeom.XformOp.TypeTranslate and value is not None:
                    translation = (float(value[0]), float(value[1]), float(value[2]))
                elif op_type == UsdGeom.XformOp.TypeScale and value is not None:
                    scale = (float(value[0]), float(value[1]), float(value[2]))
                elif op_type in {
                    UsdGeom.XformOp.TypeRotateXYZ,
                    UsdGeom.XformOp.TypeRotateXZY,
                    UsdGeom.XformOp.TypeRotateYXZ,
                    UsdGeom.XformOp.TypeRotateYZX,
                    UsdGeom.XformOp.TypeRotateZXY,
                    UsdGeom.XformOp.TypeRotateZYX,
                } and value is not None:
                    rotation = (float(value[0]), float(value[1]), float(value[2]))

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

    def _duplicate_subtree(self, stage: Usd.Stage, source_prim, target_path: str):
        target_prim = stage.DefinePrim(target_path, source_prim.GetTypeName() or "Xform")
        self._copy_basic_state(source_prim, target_prim)
        for child in source_prim.GetChildren():
            child_target = str(target_prim.GetPath().AppendChild(child.GetName()))
            self._duplicate_subtree(stage, child, child_target)
        return target_prim

    def _copy_basic_state(self, source_prim, target_prim) -> None:
        source_record = self._prim_to_record(source_prim)
        self._apply_record_state(target_prim, source_record)

    def _apply_record_state(self, prim, record: PrimRecord) -> None:
        xform = UsdGeom.XformCommonAPI(prim)
        xform.SetTranslate(Gf.Vec3d(*record.translation))
        xform.SetRotate(Gf.Vec3f(*record.rotation), UsdGeom.XformCommonAPI.RotationOrderXYZ)
        xform.SetScale(Gf.Vec3f(*record.scale))
        if record.material:
            stage = prim.GetStage()
            material_prim = stage.GetPrimAtPath(record.material)
            if material_prim and material_prim.IsValid():
                UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material(material_prim))
