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


# --- live insight, capabilities and authoring forwarding (extended RPC, see kit_rpc.py) ----------------------

import json  # noqa: E402 - appended section
import sys  # noqa: E402

_AUTHORING_MODULE = "omniverse_scene_mcp.authoring"


@contextlib.contextmanager
def _authoring_module(module):
    """Temporarily replace omniverse_scene_mcp.authoring in sys.modules (None makes the import fail)."""
    previous = sys.modules.get(_AUTHORING_MODULE, _AUTHORING_MODULE)
    sys.modules[_AUTHORING_MODULE] = module
    try:
        yield
    finally:
        if previous is _AUTHORING_MODULE:
            sys.modules.pop(_AUTHORING_MODULE, None)
        else:
            sys.modules[_AUTHORING_MODULE] = previous


def test_capabilities_lists_live_methods_and_insight_queries():
    stage = Usd.Stage.CreateInMemory()
    with _bridge(stage) as bridge:
        info = bridge.capabilities()
    assert info["ok"] is True and info["version"] == "tier2-1"
    assert {"capabilities", "insight", "create_light", "create_camera", "aim", "ping", "save"} <= set(info["methods"])
    assert info["insight_queries"] == ["stage_info", "composition", "explain_value", "properties", "bounds", "validate"]
    assert isinstance(info["authoring"]["available"], bool)
    assert info["stage_open"] is True and info["dry_run_default"] is True
    json.dumps(info)


def test_insight_reports_live_mode_and_unsaved_layers():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Sphere.Define(stage, "/World/Ball").CreateRadiusAttr(0.5)
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Sphere(stage.GetPrimAtPath("/World/Ball")).GetRadiusAttr().Set(2.0)
    before = stage.GetRootLayer().ExportToString()

    with _bridge(stage) as bridge:
        info = bridge.insight("stage_info")
        value = bridge.insight("explain_value", {"prim_path": "/World/Ball", "attribute": "radius"})
        bound = bridge.insight("BOUNDS", {"path": "/World/Ball"})
        selection = bridge.get_selection()

    root = stage.GetRootLayer()
    assert info["mode"] == "live" and info["query"] == "stage_info"
    assert info["stage_identifier"] == root.identifier and info["root_layer_anonymous"] is True
    assert info["has_unsaved_changes"] == root.dirty
    assert info["session_layer_has_edits"] is True
    assert value["value"] == 2.0  # the session-layer opinion wins and is reported live
    assert value["winning_opinion"]["layer"] == stage.GetSessionLayer().identifier
    assert bound["query"] == "bounds" and bound["world_bound"]["size"] == [4.0, 4.0, 4.0]
    assert root.ExportToString() == before and selection == []
    json.dumps([info, value, bound])


def test_insight_rejects_names_outside_the_allowlist():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/Cube")
    with _bridge(stage) as bridge:
        for query in ("snapshot", "save_scene", "GetRootLayer", "__dict__", "_stage", "stage_info ;", None):
            try:
                bridge.insight(query)
            except ValueError as exc:
                assert "Unknown insight query" in str(exc)
            else:
                raise AssertionError(f"insight should reject {query!r}")
        for args in (["/Cube"], {"prim_path": "/Cube", "save": True}, {"prim_path": "/Cube", "time": [1]}):
            try:
                bridge.insight("properties", args)
            except ValueError:
                pass
            else:
                raise AssertionError(f"insight should reject args {args!r}")
    assert not stage.GetSessionLayer().dirty


def test_live_authoring_passes_the_live_stage_and_dry_run():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    calls = []

    def create_light(stage_arg, path, light_type, **options):
        calls.append(("create_light", stage_arg, path, light_type, options))
        if not options["dry_run"]:
            stage_arg.DefinePrim(path, light_type)
        return {"ok": True, "dry_run": options["dry_run"], "path": path, "light_type": light_type}

    def create_camera(stage_arg, path, **options):
        calls.append(("create_camera", stage_arg, path, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path, "focal_length": (options["focal_length"],)}

    def aim(stage_arg, path, target, **options):
        calls.append(("aim", stage_arg, path, target, options))
        return {"ok": True, "dry_run": options["dry_run"], "path": path}

    fake = types.ModuleType(_AUTHORING_MODULE)
    fake.create_light, fake.create_camera, fake.aim = create_light, create_camera, aim
    with _authoring_module(fake), _bridge(stage) as bridge:
        preview = bridge.create_light("/World/Key", "DistantLight", intensity=500.0)
        assert stage.GetPrimAtPath("/World/Key").IsValid() is False and bridge.get_selection() == []
        created = bridge.create_light("/World/Key", "DistantLight", color=(1.0, 0.9, 0.8), dry_run=False)
        selected_after_create = bridge.get_selection()
        camera = bridge.create_camera("/World/Cam", focal_length=35.0, aim_at="/World/Key")
        aimed = bridge.aim("/World/Key", (0.0, 0.0, 0.0), up_axis="Z", dry_run=False)
        selected_after_aim = bridge.get_selection()

    assert preview == {"ok": True, "dry_run": True, "path": "/World/Key", "light_type": "DistantLight", "mode": "live"}
    assert created["dry_run"] is False and stage.GetPrimAtPath("/World/Key").GetTypeName() == "DistantLight"
    assert selected_after_create == ["/World/Key"] and selected_after_aim == ["/World/Key"]
    assert camera["focal_length"] == [35.0]  # tuples come back as plain JSON lists
    assert aimed["dry_run"] is False
    assert all(call[1] is stage for call in calls)
    assert calls[0][4]["dry_run"] is True and calls[0][4]["intensity"] == 500.0
    assert calls[2][3]["aim_at"] == "/World/Key" and calls[2][3]["dry_run"] is True
    assert calls[3][3] == (0.0, 0.0, 0.0) and calls[3][4] == {"up_axis": "Z", "dry_run": False}


def test_live_authoring_without_the_module_returns_needs_bridge_update():
    stage = Usd.Stage.CreateInMemory()
    before = stage.GetRootLayer().ExportToString()
    with _authoring_module(None), _bridge(stage) as bridge:
        results = [
            bridge.create_light("/World/Key", "SphereLight"),
            bridge.create_camera("/World/Cam"),
            bridge.aim("/World/Cam", "/World"),
        ]
    for result, method in zip(results, ("create_light", "create_camera", "aim")):
        assert result["ok"] is False and result["needs_bridge_update"] is True and result["method"] == method
        assert "authoring" in result["message"]
    assert stage.GetRootLayer().ExportToString() == before
