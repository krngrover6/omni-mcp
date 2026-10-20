"""Run LiveKitSceneBridge against temporary USD stages, with only Kit's omni.usd context faked."""

import contextlib
import tempfile
import types
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - depends on the local environment
    import pytest

    pytest.skip("USD Python bindings (pxr) are not installed", allow_module_level=True)

from omniverse_scene_mcp import kit_bridge


class _FakeSelection:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def get_selected_prim_paths(self) -> list[str]:
        return list(self.paths)

    def set_selected_prim_paths(self, paths, _expand) -> None:
        self.paths = list(paths)


class _FakeContext:
    def __init__(self, stage) -> None:
        self._stage = stage
        self._selection = _FakeSelection()

    def get_stage(self):
        return self._stage

    def get_selection(self):
        return self._selection


@contextlib.contextmanager
def _bridge(stage):
    previous = kit_bridge.omni
    context = _FakeContext(stage)
    kit_bridge.omni = types.SimpleNamespace(usd=types.SimpleNamespace(get_context=lambda _name="": context))
    try:
        yield kit_bridge.LiveKitSceneBridge()
    finally:
        kit_bridge.omni = previous


def _close(actual, expected, tolerance=1e-4) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))


def _matrices_close(first, second, tolerance=1e-5) -> bool:
    return all(abs(first[row][col] - second[row][col]) <= tolerance for row in range(4) for col in range(4))


def _isaac_box(stage, path, translation=(1.0, 2.0, 3.0), rotation_z=90.0):
    """Author the translate/orient/scale stack Isaac Sim uses for new prims."""
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    xformable = UsdGeom.Xformable(cube)
    xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(*translation))
    xformable.AddOrientOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Rotation(Gf.Vec3d.ZAxis(), rotation_z).GetQuat())
    xformable.AddScaleOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(1.2, 1.2, 1.2))
    return cube.GetPrim()


def _matrix_camera(stage, path, eye=(5.0, -25.0, 10.0)):
    camera = UsdGeom.Camera.Define(stage, path)
    camera.CreateFocalLengthAttr(22.0)
    matrix = Gf.Matrix4d().SetRotate(Gf.Rotation(Gf.Vec3d.XAxis(), 80.0)) * Gf.Matrix4d().SetTranslate(Gf.Vec3d(*eye))
    UsdGeom.Xformable(camera).AddTransformOp().Set(matrix)
    return camera.GetPrim()


def test_inspect_reads_orient_and_matrix_transforms():
    stage = Usd.Stage.CreateInMemory()
    _isaac_box(stage, "/World/Box")
    _matrix_camera(stage, "/World/Camera")
    rotated = UsdGeom.Xform.Define(stage, "/World/Rotated")
    UsdGeom.XformCommonAPI(rotated).SetRotate(Gf.Vec3f(80.0, 0.0, 0.0))

    with _bridge(stage) as bridge:
        box = bridge.get_prim("/World/Box")
        camera = bridge.get_prim("/World/Camera")
        rotated_record = bridge.get_prim("/World/Rotated")

    assert _close(box.translation, (1.0, 2.0, 3.0))
    assert _close(box.rotation, (0.0, 0.0, 90.0))
    assert _close(box.scale, (1.2, 1.2, 1.2))
    assert _close(camera.translation, (5.0, -25.0, 10.0))
    assert _close(camera.rotation, (80.0, 0.0, 0.0))
    assert _close(rotated_record.rotation, (80.0, 0.0, 0.0))


def test_move_edits_isaac_translate_orient_scale_stack_in_place():
    stage = Usd.Stage.CreateInMemory()
    prim = _isaac_box(stage, "/World/Box")
    reference = UsdGeom.Xform.Define(stage, "/World/Reference")
    UsdGeom.XformCommonAPI(reference).SetTranslate(Gf.Vec3d(4.0, 5.0, 6.0))
    UsdGeom.XformCommonAPI(reference).SetRotate(Gf.Vec3f(30.0, 45.0, 60.0))
    UsdGeom.XformCommonAPI(reference).SetScale(Gf.Vec3f(2.0, 2.0, 2.0))

    with _bridge(stage) as bridge:
        record = bridge.move_prim("/World/Box", translation=(4.0, 5.0, 6.0), rotation=(30.0, 45.0, 60.0), scale=(2.0, 2.0, 2.0))

    xformable = UsdGeom.Xformable(prim)
    assert [op.GetOpName() for op in xformable.GetOrderedXformOps()] == [
        "xformOp:translate",
        "xformOp:orient",
        "xformOp:scale",
    ]
    assert _close(record.translation, (4.0, 5.0, 6.0))
    assert _close(record.rotation, (30.0, 45.0, 60.0))
    assert _close(record.scale, (2.0, 2.0, 2.0))
    # The orient quaternion must match what an equivalent rotateXYZ op produces.
    assert _matrices_close(
        xformable.GetLocalTransformation(),
        UsdGeom.Xformable(reference).GetLocalTransformation(),
    )


def test_move_partial_update_keeps_other_components():
    stage = Usd.Stage.CreateInMemory()
    _isaac_box(stage, "/World/Box")

    with _bridge(stage) as bridge:
        record = bridge.move_prim("/World/Box", translation=(0.0, 0.0, 7.0))

    assert _close(record.translation, (0.0, 0.0, 7.0))
    assert _close(record.rotation, (0.0, 0.0, 90.0))
    assert _close(record.scale, (1.2, 1.2, 1.2))


def test_move_edits_single_matrix_op():
    stage = Usd.Stage.CreateInMemory()
    prim = _matrix_camera(stage, "/World/Camera")

    with _bridge(stage) as bridge:
        moved = bridge.move_prim("/World/Camera", translation=(0.0, -26.0, 7.0))
        assert _close(moved.translation, (0.0, -26.0, 7.0))
        assert _close(moved.rotation, (80.0, 0.0, 0.0))
        turned = bridge.move_prim("/World/Camera", rotation=(85.0, 0.0, 10.0))

    assert [op.GetOpName() for op in UsdGeom.Xformable(prim).GetOrderedXformOps()] == ["xformOp:transform"]
    assert _close(turned.translation, (0.0, -26.0, 7.0))
    assert _close(turned.rotation, (85.0, 0.0, 10.0))


def test_move_scale_uses_common_api_precision():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/Cube")

    with _bridge(stage) as bridge:
        record = bridge.move_prim("/Cube", scale=(0.16, 1.6, 3.4))

    assert _close(record.scale, (0.16, 1.6, 3.4))


def test_move_rejects_unsupported_or_animated_stacks_without_changes():
    stage = Usd.Stage.CreateInMemory()
    odd = UsdGeom.Xform.Define(stage, "/Odd")
    UsdGeom.Xformable(odd).AddRotateXOp().Set(10.0)
    UsdGeom.Xformable(odd).AddOrientOp().Set(Gf.Quatf(1.0))
    animated = UsdGeom.Xform.Define(stage, "/Animated")
    UsdGeom.Xformable(animated).AddTranslateOp().Set(Gf.Vec3d(1.0, 0.0, 0.0), 1.0)
    before = stage.GetRootLayer().ExportToString()

    with _bridge(stage) as bridge:
        for path in ("/Odd", "/Animated"):
            try:
                bridge.move_prim(path, translation=(9.0, 9.0, 9.0))
            except ValueError:
                pass
            else:
                raise AssertionError(f"move_prim should reject {path}")

    assert stage.GetRootLayer().ExportToString() == before


def test_duplicate_copies_authored_attributes_schemas_and_bindings():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    box = _isaac_box(stage, "/World/Box_07")
    UsdGeom.Cube(box).CreateDisplayColorAttr([Gf.Vec3f(0.8, 0.5, 0.2)])
    material = UsdShade.Material.Define(stage, "/World/Looks/Cardboard")
    UsdShade.MaterialBindingAPI.Apply(box).Bind(material)
    camera = _matrix_camera(stage, "/World/Camera")
    camera.AddAppliedSchema("OmniRtxCameraExposureAPI_1")
    UsdGeom.Xform.Define(stage, "/World/Group")
    UsdGeom.Sphere.Define(stage, "/World/Group/Ball").CreateRadiusAttr(0.25)
    UsdGeom.Sphere(stage.GetPrimAtPath("/World/Group/Ball")).GetPrim().CreateRelationship("peer").AddTarget("/World/Group")

    with _bridge(stage) as bridge:
        record = bridge.duplicate_prim("/World/Box_07", "/World/Box_08")
        bridge.duplicate_prim("/World/Camera", "/World/CameraCopy")
        bridge.duplicate_prim("/World/Group", "/World/GroupCopy")
        selection = bridge.get_selection()

    copy = stage.GetPrimAtPath("/World/Box_08")
    assert UsdGeom.Cube(copy).GetSizeAttr().Get() == 1.0
    assert record.material == "/World/Looks/Cardboard"
    assert _close(record.rotation, (0.0, 0.0, 90.0))
    assert list(UsdGeom.Cube(copy).GetDisplayColorAttr().Get()) == [Gf.Vec3f(0.8, 0.5, 0.2)]
    camera_copy = stage.GetPrimAtPath("/World/CameraCopy")
    assert UsdGeom.Camera(camera_copy).GetFocalLengthAttr().Get() == 22.0
    # Plain usd-core filters unregistered Omni schemas from GetAppliedSchemas, so check the authored list.
    assert "OmniRtxCameraExposureAPI_1" in camera_copy.GetMetadata("apiSchemas").GetAddedOrExplicitItems()
    ball_copy = stage.GetPrimAtPath("/World/GroupCopy/Ball")
    assert UsdGeom.Sphere(ball_copy).GetRadiusAttr().Get() == 0.25
    # Relationships that point inside the copied subtree follow the copy.
    assert ball_copy.GetRelationship("peer").GetTargets() == [Sdf.Path("/World/GroupCopy")]
    assert selection == ["/World/GroupCopy"]


def test_duplicate_flattens_sublayer_opinions_and_keeps_relative_assets():
    with tempfile.TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        (root_dir / "assets").mkdir()
        (root_dir / "layers").mkdir()
        asset = Usd.Stage.CreateNew(str(root_dir / "assets" / "crate.usda"))
        UsdGeom.Cube.Define(asset, "/Crate").CreateSizeAttr(0.5)
        asset.SetDefaultPrim(asset.GetPrimAtPath("/Crate"))
        asset.GetRootLayer().Save()

        sublayer = Sdf.Layer.CreateNew(str(root_dir / "layers" / "props.usda"))
        props = Usd.Stage.Open(sublayer)
        crate = props.DefinePrim("/World/Crate", "Xform")
        crate.GetReferences().AddReference("../assets/crate.usda")
        sublayer.Save()

        main = Usd.Stage.CreateNew(str(root_dir / "main.usda"))
        main.GetRootLayer().subLayerPaths.append("./layers/props.usda")
        UsdGeom.XformCommonAPI(main.GetPrimAtPath("/World/Crate")).SetTranslate(Gf.Vec3d(0.0, 3.0, 0.0))

        with _bridge(main) as bridge:
            bridge.duplicate_prim("/World/Crate", "/World/CrateCopy")

        copy = main.GetPrimAtPath("/World/CrateCopy")
        assert UsdGeom.Cube(copy).GetSizeAttr().Get() == 0.5  # reference still resolves from main.usda
        assert _close(UsdGeom.XformCommonAPI(copy).GetXformVectors(Usd.TimeCode.Default())[0], (0.0, 3.0, 0.0))
        authored = main.GetRootLayer().GetPrimAtPath("/World/CrateCopy").referenceList.GetAddedOrExplicitItems()
        assert [item.assetPath for item in authored] == ["./assets/crate.usda"]
        assert not sublayer.GetPrimAtPath("/World/CrateCopy")


def test_duplicate_rejects_prims_defined_only_by_a_reference():
    with tempfile.TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        asset = Usd.Stage.CreateNew(str(root_dir / "shelf.usda"))
        asset.DefinePrim("/Shelf", "Xform")
        UsdGeom.Cube.Define(asset, "/Shelf/Board")
        asset.SetDefaultPrim(asset.GetPrimAtPath("/Shelf"))
        asset.GetRootLayer().Save()

        main = Usd.Stage.CreateInMemory()
        main.DefinePrim("/World/Shelf").GetReferences().AddReference(str(root_dir / "shelf.usda"))
        UsdGeom.Cube(main.GetPrimAtPath("/World/Shelf/Board")).CreateSizeAttr(3.0)  # local override only

        with _bridge(main) as bridge:
            try:
                bridge.duplicate_prim("/World/Shelf/Board", "/World/BoardCopy")
            except ValueError:
                pass
            else:
                raise AssertionError("duplicate_prim should reject a referenced child")

        assert not main.GetPrimAtPath("/World/BoardCopy").IsValid()
        assert not main.GetRootLayer().GetPrimAtPath("/World/BoardCopy")


def test_save_keeps_composition_arcs_and_reanchors_save_as():
    with tempfile.TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        asset = Usd.Stage.CreateNew(str(root_dir / "asset.usda"))
        UsdGeom.Cube.Define(asset, "/Asset")
        asset.SetDefaultPrim(asset.GetPrimAtPath("/Asset"))
        asset.GetRootLayer().Save()

        main = Usd.Stage.CreateNew(str(root_dir / "main.usda"))
        main.DefinePrim("/World/Ref").GetReferences().AddReference("./asset.usda")
        with Usd.EditContext(main, main.GetSessionLayer()):
            main.DefinePrim("/SessionOnly", "Xform")

        with _bridge(main) as bridge:
            assert bridge.save_scene(str(root_dir / "main.usda")) == str(root_dir / "main.usda")
            (root_dir / "copies").mkdir()
            bridge.save_scene(str(root_dir / "copies" / "copy.usda"))

        saved = (root_dir / "main.usda").read_text()
        assert "./asset.usda" in saved
        assert "def Cube" not in saved and "SessionOnly" not in saved
        copied = Usd.Stage.Open(str(root_dir / "copies" / "copy.usda"))
        assert copied.GetPrimAtPath("/World/Ref").GetTypeName() == "Cube"
        assert "../asset.usda" in (root_dir / "copies" / "copy.usda").read_text()
        assert main.GetRootLayer().identifier.endswith("main.usda")


def test_save_with_file_uri_to_the_open_root_layer_saves_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        main = Usd.Stage.CreateNew(str(root_dir / "scene.usda"))
        main.GetRootLayer().Save()
        main.DefinePrim("/World/Added", "Xform")
        uri = "file:/" + str((root_dir / "scene.usda").resolve()).replace("\\", "/")
        assert kit_bridge._same_file(main.GetRootLayer().realPath, uri)
        with _bridge(main) as bridge:
            assert bridge.save_scene(uri) == uri
        assert "Added" in (root_dir / "scene.usda").read_text()
        assert not main.GetRootLayer().dirty
