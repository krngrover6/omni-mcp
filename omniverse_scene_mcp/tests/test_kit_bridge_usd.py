"""Run LiveKitSceneBridge against temporary USD stages, with only Kit's omni.usd context faked."""

import contextlib
import tempfile
import types
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    # unittest.SkipTest at import is a module skip for pytest and tools/run_tests.py alike, with or without pytest.
    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

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
    assert info["ok"] is True and info["version"] == "tier4-1"
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


# --- undo for MCP edits (undo.py, kit_commands.py); Kit's command modules are faked, never the real ones -----


def _undo_scene():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    UsdGeom.Cube.Define(stage, "/World/Box").CreateSizeAttr(1.0)
    UsdGeom.Xform.Define(stage, "/World/Shelf")
    UsdGeom.Sphere.Define(stage, "/World/Ball")
    stage.GetPrimAtPath("/World/Ball").CreateRelationship("follow").AddTarget("/World/Box")
    UsdShade.Material.Define(stage, "/World/Looks/Wood")
    sizes = stage.DefinePrim("/World/Variants", "Xform").GetVariantSets().AddVariantSet("size")
    sizes.AddVariant("small")
    sizes.AddVariant("large")
    sizes.SetVariantSelection("small")
    return stage


def _fake_kit_modules():
    """Stand-ins for omni.kit.commands and omni.kit.undo: execute() runs do() and stacks it like Kit does."""
    log = types.SimpleNamespace(executed=[], undo_stack=[], redo_stack=[])
    registry = {}
    commands = types.ModuleType("fake_omni_kit_commands")

    class Command:
        def do(self):
            raise NotImplementedError

    def register(cls):
        registry[cls.__name__[: -len("Command")]] = cls

    def unregister(cls):
        for name in [name for name, value in registry.items() if value is cls]:
            del registry[name]

    def execute(name, **kwargs):
        log.executed.append((name, kwargs))
        command = registry[name](**kwargs)
        command.do()
        log.undo_stack.append(types.SimpleNamespace(command=command, level=0))
        log.redo_stack.clear()
        return (True, None)

    commands.Command, commands.register, commands.unregister, commands.execute = Command, register, unregister, execute
    kit_undo = types.ModuleType("fake_omni_kit_undo")

    def undo_once():
        entry = log.undo_stack.pop()
        entry.command.undo()
        log.redo_stack.append(entry)

    def redo_once():
        entry = log.redo_stack.pop()
        entry.command.do()
        log.undo_stack.append(entry)

    kit_undo.get_undo_stack, kit_undo.undo, kit_undo.redo = (lambda: log.undo_stack), undo_once, redo_once
    return commands, kit_undo, log


@contextlib.contextmanager
def _kit_bridge(stage, commands, kit_undo):
    previous = kit_bridge.omni
    context = _FakeContext(stage)
    kit_bridge.omni = types.SimpleNamespace(
        usd=types.SimpleNamespace(get_context=lambda _name="": context),
        kit=types.SimpleNamespace(commands=commands, undo=kit_undo),
    )
    try:
        yield kit_bridge.LiveKitSceneBridge()
    finally:
        kit_bridge.omni = previous


def test_every_mutating_bridge_edit_undoes_to_the_exact_layer():
    stage = _undo_scene()
    layer = stage.GetRootLayer()
    texts = [layer.ExportToString()]
    with _bridge(stage) as bridge:
        edits = [
            lambda: bridge.create_prim("/World/New/Deep", "Cube"),
            lambda: bridge.move_prim("/World/Box", translation=(1.0, 2.0, 3.0), rotation=(0.0, 0.0, 30.0)),
            lambda: bridge.rename_prim("/World/Box", "Crate"),
            lambda: bridge.reparent_prim("/World/Crate", "/World/Shelf"),
            lambda: bridge.duplicate_prim("/World/Shelf", "/World/Shelf2"),
            lambda: bridge.set_material("/World/Ball", "/World/Looks/Wood"),
            lambda: bridge.set_variant_selection("/World/Variants", "size", "large"),
            lambda: bridge.make_red("/World/Ball"),
            lambda: bridge.delete_prim("/World/Shelf"),
            lambda: bridge.create_light("/World/Key", "sphere", intensity=500.0, translation=(0.0, 0.0, 5.0), dry_run=False),
            lambda: bridge.create_camera("/World/Cam", translation=(0.0, -10.0, 2.0), aim_at="/World/Ball", dry_run=False),
            lambda: bridge.aim("/World/Key", "/World/Cam", dry_run=False),
        ]
        results = []
        for edit in edits:
            results.append(edit())
            texts.append(layer.ExportToString())
        assert len(set(texts)) == len(texts), "every edit should change the layer"
        assert all(result.get("undoable") is True for result in results[-3:]), results[-3:]
        history = bridge.undo_history(limit=50)
        assert history["ok"] and history["count"] == len(edits) and history["kit_ctrl_z"] is False
        assert history["entries"][0]["label"] == "Aim /World/Key"
        assert history["entries"][-1]["label"] == "Create Cube /World/New/Deep"
        assert "not undoable" in history["note"]
        json.dumps(history)

        preview = bridge.undo_last()
        assert preview["ok"] and preview["dry_run"] is True and layer.ExportToString() == texts[-1]
        for expected in reversed(texts[:-1]):
            result = bridge.undo_last(dry_run=False)
            assert result["ok"] and result["verified"] is True, result
            assert layer.ExportToString() == expected
        assert bridge.undo_last(dry_run=False)["ok"] is False
        assert bridge.get_selection() == []  # undone prims leave the selection


def test_bridge_edits_land_on_kit_undo_stack_and_ctrl_z_restores():
    stage = _undo_scene()
    layer = stage.GetRootLayer()
    commands, kit_undo, log = _fake_kit_modules()
    before = layer.ExportToString()
    with _kit_bridge(stage, commands, kit_undo) as bridge:
        info = bridge.capabilities()["undo"]
        assert info["available"] and info["kit_ctrl_z"] is True and info["methods"] == ["undo_last", "undo_history"]
        bridge.move_prim("/World/Box", translation=(0.0, 0.0, 4.0))
        moved = layer.ExportToString()
        assert [name for name, _ in log.executed] == ["MCPEdit"] and log.executed[0][1]["label"] == "Move /World/Box"
        kit_undo.undo()  # Ctrl+Z in Kit
        assert layer.ExportToString() == before
        assert bridge.undo_last(dry_run=False)["ok"] is False  # Ctrl+Z already undid it
        kit_undo.redo()  # Ctrl+Y in Kit
        assert layer.ExportToString() == moved and bridge.undo_history()["count"] == 1
        result = bridge.undo_last(dry_run=False)
        assert result["ok"] and result["via"] == "kit_undo" and layer.ExportToString() == before
        try:
            bridge.delete_prim("/World/Nope")
        except ValueError:
            pass
        assert len(log.executed) == 1  # refused edits never reach Kit's undo history


def test_bridge_records_no_reads_dry_runs_or_saves_and_refuses_stale_undos():
    with tempfile.TemporaryDirectory() as tmp:
        scene = Path(tmp) / "scene.usda"
        stage = Usd.Stage.CreateNew(str(scene))
        UsdGeom.Xform.Define(stage, "/World")
        UsdGeom.Cube.Define(stage, "/World/Box")
        stage.GetRootLayer().Save()
        layer = stage.GetRootLayer()
        with _bridge(stage) as bridge:
            bridge.get_prim("/World/Box")
            bridge.snapshot()
            bridge.find_prims("box")
            bridge.insight("stage_info")
            bridge.create_light("/World/Key", "sphere")  # dry run by default
            bridge.aim("/World/Box", (0.0, 0.0, -5.0))
            assert bridge.undo_history()["count"] == 0
            assert bridge.undo_last()["ok"] is False

            bridge.create_prim("/World/Added", "Xform")
            bridge.save_scene(str(scene))
            assert bridge.undo_history()["count"] == 1  # the save added nothing to undo
            undone = bridge.undo_last(dry_run=False)
            assert undone["ok"] and "save again" in undone["note"]
            assert not stage.GetPrimAtPath("/World/Added").IsValid() and layer.dirty
            assert "Added" in scene.read_text()  # the saved file keeps the edit until the next save

            bridge.move_prim("/World/Box", translation=(0.0, 0.0, 2.0))
            UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Box")).SetTranslate(Gf.Vec3d(0.0, 0.0, 9.0))
            text = layer.ExportToString()
            refused = bridge.undo_last(dry_run=False)
            assert refused["ok"] is False and refused["refused"] == "changed" and layer.ExportToString() == text

            bridge._usd_context._stage = Usd.Stage.CreateInMemory()  # the user opened another stage
            stale = bridge.undo_last(dry_run=False)
            assert stale["refused"] == "stale" and stale["discarded"] is True
            assert layer.ExportToString() == text
            json.dumps([undone, refused, stale])


def test_redefining_an_existing_prim_snapshots_only_that_prim():
    stage = _undo_scene()
    layer = stage.GetRootLayer()
    before = layer.ExportToString()
    with _bridge(stage) as bridge:
        bridge.create_prim("/World/Shelf", "Scope")  # already exists: only its type changes
        assert stage.GetPrimAtPath("/World/Shelf").GetTypeName() == "Scope"
        snapshot = bridge.undo_history()["entries"][0]["snapshots"][0]
        assert snapshot["shallow"] == ["/World/Shelf"] and snapshot["absent"] == []
        assert bridge.undo_last(dry_run="false")["ok"] is True  # authoring-style string flag writes too
        assert layer.ExportToString() == before


# --- tier3-1: safe attribute/material edits (edits.py) through the live bridge, dry run by default and undoable ----

_EDITS_MODULE = "omniverse_scene_mcp.edits"


def _layered_edit_scene(folder):
    """A root layer (the edit target) over a sublayer that defines /World and /World/Box."""
    base = Sdf.Layer.CreateNew(str(Path(folder) / "base.usda"))
    base_stage = Usd.Stage.Open(base)
    UsdGeom.Xform.Define(base_stage, "/World")
    UsdGeom.Cube.Define(base_stage, "/World/Box").CreateSizeAttr(1.0)
    base.Save()
    root = Sdf.Layer.CreateNew(str(Path(folder) / "shot.usda"))
    root.subLayerPaths.append("./base.usda")
    root.Save()
    return Usd.Stage.Open(root), root, base


def test_bridge_edits_preview_by_default_and_undo_to_the_exact_layers():
    with tempfile.TemporaryDirectory() as tmp:
        stage, root, base = _layered_edit_scene(tmp)
        texts = lambda: (root.ExportToString(), base.ExportToString())  # noqa: E731
        original = texts()
        with _bridge(stage) as bridge:
            info = bridge.capabilities()
            assert info["edits"] == {"available": True, "methods": ["set_attribute", "create_material", "bind_material"],
                                     "dry_run_default": True}
            assert {"set_attribute", "create_material", "bind_material", "undo_last", "undo_history"} <= set(info["methods"])
            previews = [
                bridge.set_attribute("/World/Box", "size", 3),
                bridge.create_material("/World/Looks/Steel", diffuse_color=(0.8, 0.8, 0.8), metallic=1.0, create_parent=True),
                bridge.bind_material("/World/Box", "/World/Looks/Steel"),  # refused: the material does not exist yet
                bridge.set_attribute("/World/Box", "size", 3, dry_run=None),
            ]
            assert texts() == original and bridge.undo_history()["count"] == 0
            assert previews[0]["ok"] and previews[0]["dry_run"] is True and previews[0]["would_change"] is True
            assert previews[0]["mode"] == "live" and previews[0]["before"]["value"] == 1.0
            assert previews[1]["would_create"]["creates_parent"] == {"path": "/World/Looks", "type": "Scope"}
            assert previews[2]["ok"] is False and "does not exist" in previews[2]["error"]
            assert previews[3]["dry_run"] is True

            sized = bridge.set_attribute("/World/Box", "size", 3, dry_run=False)
            made = bridge.create_material("/World/Looks/Steel", diffuse_color=(0.8, 0.8, 0.8), metallic=1.0,
                                          create_parent=True, dry_run=False)
            bound = bridge.bind_material("/World/Box", "/World/Looks/Steel", strength="stronger", dry_run=False)
            refused = bridge.set_attribute("/World/Nope", "size", 2, dry_run=False)
            assert sized["written"] and sized["undoable"] is True and made["undoable"] is True and bound["undoable"] is True
            assert refused["ok"] is False and refused["written"] is False and "undoable" not in refused
            assert base.ExportToString() == original[1], "edits go to the edit target (root layer) only"
            box = stage.GetPrimAtPath("/World/Box")
            assert box.GetAttribute("size").Get() == 3.0
            assert str(UsdShade.MaterialBindingAPI(box).ComputeBoundMaterial()[0].GetPath()) == "/World/Looks/Steel"
            history = bridge.undo_history()
            assert history["count"] == 3 and [entry["label"] for entry in history["entries"]] == [
                "Bind /World/Looks/Steel to /World/Box", "Create material /World/Looks/Steel", "Set size on /World/Box"]
            json.dumps([previews, sized, made, bound, refused, history])
            for _ in range(3):
                assert bridge.undo_last(dry_run=False)["verified"] is True
            assert texts() == original
            assert bridge.undo_last(dry_run=False)["ok"] is False


def test_bridge_edits_land_on_kit_undo_stack_and_ctrl_z_restores_them():
    stage = _undo_scene()
    layer = stage.GetRootLayer()
    commands, kit_undo, log = _fake_kit_modules()
    before = layer.ExportToString()
    with _kit_bridge(stage, commands, kit_undo) as bridge:
        bridge.set_attribute("/World/Box", "size", 4.0, dry_run=False)
        after_set = layer.ExportToString()
        bridge.bind_material("/World/Ball", "/World/Looks/Wood", dry_run=False)
        assert [kwargs["label"] for _, kwargs in log.executed] == [
            "Set size on /World/Box", "Bind /World/Looks/Wood to /World/Ball"]
        kit_undo.undo()  # Ctrl+Z in Kit undoes the binding
        assert layer.ExportToString() == after_set
        result = bridge.undo_last(dry_run=False)  # the attribute edit is Kit's top entry now
        assert result["ok"] and result["via"] == "kit_undo" and layer.ExportToString() == before
        bridge.set_attribute("/World/Box", "size", 9.0)  # a dry run never reaches Kit's undo history
        assert len(log.executed) == 2


def test_bridge_edits_without_the_edits_module_need_a_bridge_update():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/World/Box")
    before = stage.GetRootLayer().ExportToString()
    previous = sys.modules.get(_EDITS_MODULE, _EDITS_MODULE)
    sys.modules[_EDITS_MODULE] = None  # the import fails, as with a package copy that predates edits.py
    try:
        with _bridge(stage) as bridge:
            results = [
                bridge.set_attribute("/World/Box", "size", 2.0, dry_run=False),
                bridge.create_material("/World/Looks/M", dry_run=False),
                bridge.bind_material("/World/Box", "/World/Looks/M", dry_run=False),
            ]
            edits_info = bridge.capabilities()["edits"]
    finally:
        if previous is _EDITS_MODULE:
            sys.modules.pop(_EDITS_MODULE, None)
        else:
            sys.modules[_EDITS_MODULE] = previous
    for result, method in zip(results, ("set_attribute", "create_material", "bind_material")):
        assert result["ok"] is False and result["needs_bridge_update"] is True and result["method"] == method
    assert edits_info["available"] is False and edits_info["error"]
    assert stage.GetRootLayer().ExportToString() == before
