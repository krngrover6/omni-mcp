"""python_teacher: every snippet compiles, is explained line by line, and really does its job.

Standalone snippets are run with usd-core against a fixture .usda file in a fresh temp dir; script-editor snippets
are run against the same kind of stage through a tiny stand-in for omni.usd (only get_context().get_stage()).
User values must end up as harmless literals or be refused. No network, no ports, no Kit; only temp files.
"""

import ast
import contextlib
import io
import shutil
import sys
import tempfile
import types
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade
except ImportError:  # pragma: no cover - depends on the local environment
    Usd = None  # the compile / explanation / injection tests still run without pxr

from omniverse_scene_mcp import python_teacher as pt

ISAAC_ROT = (10.0, 20.0, 30.0)
_TEMP_DIRS: list = []


def _new_folder() -> Path:
    folder = Path(tempfile.mkdtemp(prefix="python_teacher_"))
    _TEMP_DIRS.append(folder)
    return folder


@contextlib.contextmanager
def _workspace_guard():
    """Remove the temp dirs afterwards and prove no snippet wrote into the current directory."""
    before = sorted(path.name for path in Path.cwd().iterdir())
    try:
        yield
    finally:
        while _TEMP_DIRS:
            shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)
    assert sorted(path.name for path in Path.cwd().iterdir()) == before, "a snippet wrote into the current directory"


# --------------------------------------------------------------------------------------------- fixtures


def _write_fixture(folder: Path) -> Path:
    """A Z-up, metre stage with the prims the snippets act on, plus a sublayer and a referenceable asset."""
    asset = Usd.Stage.CreateNew(str(folder / "asset.usda"))
    chair = UsdGeom.Xform.Define(asset, "/Chair")
    UsdGeom.Cube.Define(asset, "/Chair/Seat")
    asset.SetDefaultPrim(chair.GetPrim())
    asset.GetRootLayer().Save()

    extra = Sdf.Layer.CreateNew(str(folder / "extra.usda"))
    Sdf.CreatePrimInLayer(extra, "/World/FromSublayer").specifier = Sdf.SpecifierDef
    extra.Save()

    path = folder / "scene.usda"
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.GetRootLayer().subLayerPaths.append("extra.usda")
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.Cube.Define(stage, "/World/Cube")
    mesh = UsdGeom.Mesh.Define(stage, "/World/Mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    mesh.CreateFaceVertexCountsAttr([3])
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2])
    robot = UsdGeom.Xformable(UsdGeom.Xform.Define(stage, "/World/Robot"))  # Isaac-style stack, float orient
    robot.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(1, 2, 3))
    robot.AddOrientOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Quatf(1, 0, 0, 0))
    robot.AddScaleOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(1, 1, 1))
    UsdShade.Material.Define(stage, "/World/Looks/Existing")
    variant_prim = UsdGeom.Xform.Define(stage, "/World/Variant").GetPrim()
    vset = variant_prim.GetVariantSets().AddVariantSet("size")
    vset.AddVariant("small")
    vset.AddVariant("large")
    vset.SetVariantSelection("small")
    stage.GetRootLayer().Save()
    return path


class _FakeContext:
    def __init__(self, stage):
        self._stage = stage

    def get_stage(self):
        return self._stage


@contextlib.contextmanager
def _fake_omni(stage):
    """Just enough of omni.usd for 'import omni.usd' and omni.usd.get_context().get_stage()."""
    saved = {name: sys.modules.get(name) for name in ("omni", "omni.usd")}
    omni = types.ModuleType("omni")
    omni_usd = types.ModuleType("omni.usd")
    omni_usd.get_context = lambda: _FakeContext(stage)
    omni.usd = omni_usd
    omni.__path__ = []  # a package, so 'import omni.usd' finds the submodule in sys.modules
    sys.modules["omni"], sys.modules["omni.usd"] = omni, omni_usd
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _exec(code: str, name: str) -> tuple[dict, str]:
    namespace = {"__name__": "__python_for__"}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, f"<{name}>", "exec"), namespace)
    return namespace, out.getvalue()


def _run(action, args=None, context="standalone"):
    """Generate the snippet, run it against a fresh fixture stage, return (stage, namespace, stdout, folder)."""
    folder = _new_folder()
    path = _write_fixture(folder)
    args = dict(args or {})
    if context == "standalone":
        args["stage_path"] = str(path)
    result = pt.python_for(action, args, context=context)
    assert result["ok"], result
    if context == "standalone":
        namespace, out = _exec(result["code"], action)
        stage = namespace["stage"]
    else:
        stage = Usd.Stage.Open(str(path))
        with _fake_omni(stage):
            namespace, out = _exec(result["code"], action)
        assert namespace["stage"] is stage
    return stage, namespace, out, folder


def _close(a, b, tol=1e-4):
    return all(abs(x - y) < tol for x, y in zip(a, b))


# ---------------------------------------------------------------------------------- per-action checks


def _c_inspect(stage, ns, out, folder):
    assert "Path: /World/Cube" in out and "Type: Cube" in out and "size" in out


def _c_children(stage, ns, out, folder):
    assert "/World/Cube Cube" in out and "/World/Robot Xform" in out


def _c_traverse(stage, ns, out, folder):
    assert "/World/Mesh Mesh" in out and "/World/FromSublayer" in out


def _c_create(stage, ns, out, folder):
    assert stage.GetPrimAtPath("/World/Ball").GetTypeName() == "Sphere"


def _c_move(stage, ns, out, folder):
    translate = UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Cube")).GetXformVectors(Usd.TimeCode.Default())[0]
    assert _close(translate, (1, 2, 3))


def _c_rotate(stage, ns, out, folder):
    rotate = UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Cube")).GetXformVectors(Usd.TimeCode.Default())[1]
    assert _close(rotate, (0, 0, 30))


def _c_scale(stage, ns, out, folder):
    scale = UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Cube")).GetXformVectors(Usd.TimeCode.Default())[2]
    assert _close(scale, (2, 3, 4))


def _c_isaac(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Robot")
    xformable = UsdGeom.Xformable(prim)
    assert list(xformable.GetXformOpOrderAttr().Get()) == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]
    assert _close(prim.GetAttribute("xformOp:translate").Get(), (4, 5, 6))
    assert prim.GetAttribute("xformOp:orient").GetTypeName() == Sdf.ValueTypeNames.Quatf  # kept its precision
    scratch = Usd.Stage.CreateInMemory()  # keep the stage alive while its prim is used
    reference = UsdGeom.Xform.Define(scratch, "/R")
    UsdGeom.XformCommonAPI(reference).SetRotate(Gf.Vec3f(*ISAAC_ROT))
    expected = reference.GetLocalTransformation().ExtractRotationQuat()
    got = xformable.GetLocalTransformation().ExtractRotationQuat()
    assert abs(abs(Gf.Dot(expected, got)) - 1.0) < 1e-5


def _c_rename(stage, ns, out, folder):
    assert stage.GetPrimAtPath("/World/Box").GetTypeName() == "Cube"
    assert not stage.GetPrimAtPath("/World/Cube").IsValid()


def _c_delete(stage, ns, out, folder):
    assert not stage.GetPrimAtPath("/World/Cube").IsValid()


def _c_deactivate(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Cube")
    assert prim.IsValid() and not prim.IsActive()


def _c_duplicate(stage, ns, out, folder):
    assert stage.GetPrimAtPath("/World/Cube_copy").GetTypeName() == "Cube"
    assert stage.GetPrimAtPath("/World/Cube").IsValid()


def _c_set_attribute(stage, ns, out, folder):
    assert stage.GetPrimAtPath("/World/Cube").GetAttribute("size").Get() == 3.5


def _c_time_samples(stage, ns, out, folder):
    attr = stage.GetPrimAtPath("/World/Cube").GetAttribute("xformOp:translate")
    assert list(attr.GetTimeSamples()) == [1.0, 11.0]
    assert _close(attr.Get(Usd.TimeCode(6)), (0, 0, 5))
    assert stage.GetEndTimeCode() == 11.0


def _c_light(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/SphereLight")
    assert prim.GetTypeName() == "SphereLight"
    assert prim.GetAttribute("inputs:intensity").Get() == 1234.0
    assert _close(UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())[0], (0, 0, 3))


def _c_dome(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Sky")
    assert prim.GetTypeName() == "DomeLight"
    assert prim.GetAttribute("inputs:texture:file").Get().path == "./sky.hdr"


def _c_camera(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Cam")
    assert prim.GetTypeName() == "Camera"
    world = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    assert _close(world.ExtractTranslation(), (6, -6, 4))
    forward = world.TransformDir(Gf.Vec3d(0, 0, -1)).GetNormalized()
    wanted = (Gf.Vec3d(1, 1, 0) - Gf.Vec3d(6, -6, 4)).GetNormalized()
    assert _close(forward, wanted)
    up = world.TransformDir(Gf.Vec3d(0, 1, 0))
    assert up[2] > 0  # camera +Y leans toward the stage's +Z


def _c_material(stage, ns, out, folder):
    cube = stage.GetPrimAtPath("/World/Cube")
    bound = UsdShade.MaterialBindingAPI(cube).ComputeBoundMaterial()[0]
    assert bound.GetPath() == Sdf.Path("/World/Looks/Red")
    shader = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/Red/PreviewSurface"))
    assert shader.GetIdAttr().Get() == "UsdPreviewSurface"
    assert _close(shader.GetInput("diffuseColor").Get(), (0.9, 0.1, 0.1))
    source = bound.GetSurfaceOutput().GetConnectedSource()
    assert source and source[0].GetPath() == shader.GetPath()


def _c_bind(stage, ns, out, folder):
    bound = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/World/Cube")).ComputeBoundMaterial()[0]
    assert bound.GetPath() == Sdf.Path("/World/Looks/Existing")


def _c_reference(stage, ns, out, folder):
    assert stage.GetPrimAtPath("/World/Chair/Seat").GetTypeName() == "Cube"


def _c_reference_offset(stage, ns, out, folder):
    assert stage.GetPrimAtPath("/World/Chair/Seat").IsValid()
    items = stage.GetPrimAtPath("/World/Chair").GetMetadata("references").GetAddedOrExplicitItems()
    assert items[0].primPath == Sdf.Path("/Chair") and items[0].layerOffset.offset == 10.0


def _c_payload(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Chair")
    assert prim.HasAuthoredPayloads() and stage.GetPrimAtPath("/World/Chair/Seat").IsValid()


def _c_variant_set(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Cube")
    vset = prim.GetVariantSets().GetVariantSet("look")
    assert vset.GetVariantNames() == ["green", "red"] and vset.GetVariantSelection() == "green"
    color = prim.GetAttribute("primvars:displayColor").Get()[0]
    assert _close(color, (0.1, 0.6, 0.2))  # the second palette entry, from the selected variant


def _c_select_variant(stage, ns, out, folder):
    vset = stage.GetPrimAtPath("/World/Variant").GetVariantSets().GetVariantSet("size")
    assert vset.GetVariantSelection() == "large"


def _c_sublayer(stage, ns, out, folder):
    root = stage.GetRootLayer()
    assert list(root.subLayerPaths)[0] == "lighting.usda"  # strongest first
    assert (folder / "lighting.usda").exists()  # created next to the root layer, not in the cwd


def _c_edit_target(stage, ns, out, folder):
    assert Path(stage.GetEditTarget().GetLayer().realPath).name == "extra.usda"


def _c_physics_scene(stage, ns, out, folder):
    scene = UsdPhysics.Scene(stage.GetPrimAtPath("/physicsScene"))
    assert scene and abs(scene.GetGravityMagnitudeAttr().Get() - 9.81) < 1e-6
    assert _close(scene.GetGravityDirectionAttr().Get(), (0, 0, -1))


def _c_rigid(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Cube")
    assert prim.HasAPI(UsdPhysics.RigidBodyAPI) and prim.HasAPI(UsdPhysics.CollisionAPI)
    assert UsdPhysics.MassAPI(prim).GetMassAttr().Get() == 2.5


def _c_collider(stage, ns, out, folder):
    prim = stage.GetPrimAtPath("/World/Mesh")
    assert prim.HasAPI(UsdPhysics.CollisionAPI) and not prim.HasAPI(UsdPhysics.RigidBodyAPI)
    assert UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get() == "convexDecomposition"


def _c_save(stage, ns, out, folder):
    assert "Saved" in out and not stage.GetRootLayer().dirty


def _c_export(stage, ns, out, folder):
    layer = Sdf.Layer.OpenAsAnonymous(str(folder / "flat.usda"))
    assert layer.GetPrimAtPath("/World/FromSublayer") and not list(layer.subLayerPaths)


def _c_export_root(stage, ns, out, folder):
    layer = Sdf.Layer.OpenAsAnonymous(str(folder / "root_only.usdc"))
    assert not layer.GetPrimAtPath("/World/FromSublayer") and list(layer.subLayerPaths) == ["extra.usda"]


def _c_undo(stage, ns, out, folder):
    assert "After the edit: True" in out and "After undo: False" in out
    assert not stage.GetPrimAtPath("/World/UndoDemo").IsValid()


# action -> list of (args, check, also run in the fake script editor)
CASES = {
    "inspect_prim": [({"prim_path": "/World/Cube"}, _c_inspect, True)],
    "list_children": [({}, _c_children, True)],
    "traverse": [({}, _c_traverse, True)],
    "create_prim": [({"prim_path": "/World/Ball", "type_name": "sphere"}, _c_create, True)],
    "move": [({"translate": [1, 2, 3]}, _c_move, True)],
    "rotate": [({"rotate": [0, 0, 30]}, _c_rotate, True)],
    "scale": [({"scale": [2, 3, 4]}, _c_scale, True)],
    "set_transform_isaac": [
        ({"prim_path": "/World/Robot", "translate": [4, 5, 6], "rotate": list(ISAAC_ROT)}, _c_isaac, True)],
    "rename": [({"new_name": "Box"}, _c_rename, True)],
    "delete": [({}, _c_delete, True), ({"mode": "deactivate"}, _c_deactivate, True)],
    "duplicate": [({}, _c_duplicate, True)],
    "set_attribute": [({"value": 3.5}, _c_set_attribute, True)],
    "time_samples": [({"start_frame": 1, "end_frame": 11, "end_position": [0, 0, 10]}, _c_time_samples, True)],
    "create_light": [
        ({"light_type": "sphere", "intensity": 1234, "translate": [0, 0, 3]}, _c_light, True),
        ({"light_type": "dome", "prim_path": "/World/Sky", "texture_file": "./sky.hdr"}, _c_dome, False),
    ],
    "create_camera": [({"prim_path": "/World/Cam", "position": [6, -6, 4], "target": [1, 1, 0]}, _c_camera, True)],
    "create_material": [({"material_path": "/World/Looks/Red", "color": [0.9, 0.1, 0.1]}, _c_material, True)],
    "bind_material": [({"material_path": "/World/Looks/Existing", "strength": "strongerThanDescendants"}, _c_bind, True)],
    "add_reference": [
        ({"prim_path": "/World/Chair", "asset_path": "./asset.usda"}, _c_reference, True),
        ({"prim_path": "/World/Chair", "asset_path": "./asset.usda", "ref_prim_path": "/Chair", "time_offset": 10},
         _c_reference_offset, False),
    ],
    "add_payload": [({"prim_path": "/World/Chair", "asset_path": "./asset.usda"}, _c_payload, True)],
    "create_variant_set": [
        ({"set_name": "look", "variants": ["red", "green"], "selection": "green"}, _c_variant_set, True)],
    "select_variant": [({"prim_path": "/World/Variant", "set_name": "size", "selection": "large"}, _c_select_variant, True)],
    "add_sublayer": [({"layer_path": "lighting.usda", "position": "strongest"}, _c_sublayer, True)],
    "set_edit_target": [({"layer_path": "extra.usda"}, _c_edit_target, True)],
    "add_physics_scene": [({}, _c_physics_scene, True)],
    "make_rigid_body": [({"mass": 2.5}, _c_rigid, True)],
    "make_collider": [({"prim_path": "/World/Mesh", "approximation": "convexDecomposition"}, _c_collider, True)],
    "save": [({}, _c_save, True)],
    "export": [({"export_path": "flat.usda"}, _c_export, False),
               ({"export_path": "root_only.usdc", "flatten": False}, _c_export_root, False)],
    "undo_hint": [({}, _c_undo, False)],  # the script-editor version needs omni.kit.commands
}


def _absolute_export(args, folder):
    if "export_path" in args:
        return {**args, "export_path": str(folder / args["export_path"])}
    return args


# ---------------------------------------------------------------------------------------------- tests


def test_every_action_compiles_in_both_contexts_and_explains_every_line():
    for action in pt.supported_actions():
        for context in pt.CONTEXTS:
            result = pt.python_for(action, context=context)
            assert result["ok"], (action, context, result)
            compile(result["code"], action, "exec")
            code_lines = [line for line in result["code"].splitlines() if line.strip()]
            assert [row["line"] for row in result["lines"]] == code_lines, action
            for row in result["lines"]:
                assert isinstance(row["explanation"], str) and len(row["explanation"]) > 10, (action, row)
            assert result["where_to_run"] == pt.WHERE_TO_RUN[context]
            assert result["concepts"] and all(item["meaning"] for item in result["concepts"])
            assert result["safety_note"] and result["title"]


def test_contexts_use_the_right_stage_entry_point():
    for action in pt.supported_actions():
        editor = pt.python_for(action, context="script_editor")["code"]
        standalone = pt.python_for(action, context="standalone")["code"]
        assert "stage = omni.usd.get_context().get_stage()" in editor, action
        assert "Usd.Stage.Open(stage_path)" in standalone and "omni" not in standalone, action
    assert pt.WHERE_TO_RUN["script_editor"] == "Isaac Sim / Composer: Window > Script Editor, paste, Ctrl+Enter"
    assert pt.WHERE_TO_RUN["standalone"] == "a plain Python file with usd-core"
    assert "CreateNew" in pt.python_for("create_prim", context="standalone")["code"]


def test_safety_notes_say_the_scene_changes_in_memory_until_saved():
    note = pt.python_for("move")["safety_note"]
    assert "in memory until saved" in note and "xformOp:translate" in note
    assert "stage.Save()" in pt.python_for("move", context="standalone")["safety_note"]
    assert "nothing is written" in pt.python_for("inspect_prim")["safety_note"]


def test_exam_links_point_at_real_blueprint_objectives():
    assert any(item["id"] == "1.6" and "LIVERPS" in item["objective"]
               for item in pt.python_for("create_variant_set")["exam_link"])
    for action in pt.supported_actions():
        for item in pt.python_for(action)["exam_link"]:
            assert item["objective"], (action, item["id"])


def test_list_python_topics_groups_every_action_once():
    topics = pt.list_python_topics()
    assert topics["ok"] and topics["count"] == len(pt.supported_actions())
    names = [topic["action"] for group in topics["groups"] for topic in group["topics"]]
    assert sorted(names) == sorted(pt.supported_actions())
    for group in topics["groups"]:
        for topic in group["topics"]:
            assert topic["description"] and isinstance(topic["args"], dict)
    required = {"inspect_prim", "list_children", "create_prim", "move", "rotate", "scale", "set_transform_isaac",
                "rename", "delete", "duplicate", "set_attribute", "create_light", "create_camera", "create_material",
                "bind_material", "add_reference", "create_variant_set", "select_variant", "add_sublayer",
                "add_physics_scene", "make_rigid_body", "make_collider", "save", "export", "undo_hint", "time_samples"}
    assert required <= set(names)


def test_unknown_action_and_bad_context_are_refused_with_help():
    result = pt.python_for("make_coffee")
    assert result["ok"] is False and "inspect_prim" in result["supported_actions"]
    assert pt.python_for("move", context="kit")["ok"] is False
    assert pt.python_for("move", ["not", "a", "dict"])["ok"] is False
    unknown = pt.python_for("move", {"colour": [1, 0, 0]})
    assert unknown["ok"] is False and "translate" in unknown["accepted_args"]


def test_aliases_and_router_tool_names_map_to_actions():
    assert pt.python_for("scene_create_light")["action"] == "create_light"
    assert pt.python_for("Add Reference")["action"] == "add_reference"
    assert pt.python_for("scene_move", {"path": "/World/Car"})["args"]["prim_path"] == "/World/Car"


def test_every_action_has_a_standalone_run_case():
    assert set(CASES) == set(pt.supported_actions())


def test_standalone_snippets_run_and_do_what_they_say():
    if Usd is None:
        return
    with _workspace_guard():
        for action, cases in CASES.items():
            for args, check, _ in cases:
                folder = _new_folder()
                path = _write_fixture(folder)
                result = pt.python_for(action, {**_absolute_export(args, folder), "stage_path": str(path)},
                                       context="standalone")
                assert result["ok"], (action, result)
                namespace, out = _exec(result["code"], action)
                stage = namespace["stage"]
                if pt.ACTIONS[action].mode == "edit":
                    assert not stage.GetRootLayer().dirty, f"{action}: standalone edits must be saved"
                try:
                    check(stage, namespace, out, folder)
                except AssertionError as exc:
                    raise AssertionError(f"{action} {args}: {exc}\n{out}") from exc


def test_standalone_edits_reach_the_file_on_disk():
    if Usd is None:
        return
    with _workspace_guard():
        for action, args, needle in (("create_prim", {"prim_path": "/World/Ball", "type_name": "Sphere"}, 'def Sphere "Ball"'),
                                     ("set_attribute", {"value": 3.5}, "double size = 3.5"),
                                     ("make_rigid_body", {}, "PhysicsRigidBodyAPI")):
            folder = _new_folder()
            path = _write_fixture(folder)
            result = pt.python_for(action, {**args, "stage_path": str(path)}, context="standalone")
            _exec(result["code"], action)
            assert needle in path.read_text(encoding="utf-8"), action


def test_script_editor_snippets_run_against_a_stand_in_omni_usd():
    if Usd is None:
        return
    with _workspace_guard():
        for action, cases in CASES.items():
            for args, check, in_editor in cases:
                if not in_editor:
                    continue
                stage, namespace, out, folder = _run(action, args, context="script_editor")
                try:
                    check(stage, namespace, out, folder)
                except AssertionError as exc:
                    raise AssertionError(f"script_editor {action} {args}: {exc}\n{out}") from exc


def test_script_editor_undo_uses_kit_commands():
    code = pt.python_for("undo_hint")["code"]
    assert "omni.kit.commands.execute('CreatePrim'" in code and "omni.kit.undo.undo()" in code


def test_xform_common_api_snippet_explains_isaac_orient_failure():
    if Usd is None:
        return
    with _workspace_guard():
        try:
            _run("move", {"prim_path": "/World/Robot", "translate": [1, 1, 1]})
        except RuntimeError as exc:
            assert "set_transform_isaac" in str(exc)
        else:
            raise AssertionError("XformCommonAPI should refuse an orient stack")


def test_set_attribute_value_types_round_trip():
    if Usd is None:
        return
    with _workspace_guard():
        cases = [
            ("flag", "bool", False, False),
            ("count", "int", 7, 7),
            ("ratio", "float", 0.25, 0.25),
            ("label", "string", "it's \"quoted\"\nnext line", "it's \"quoted\"\nnext line"),
            ("mode", "token", "render", "render"),
            ("tint", "color3f", [0.1, 0.2, 0.3], (0.1, 0.2, 0.3)),
            ("offset", "double3", [1, 2, 3], (1, 2, 3)),
        ]
        for name, value_type, value, expected in cases:
            stage, _, _, _ = _run("set_attribute", {"attr_name": f"userProperties:{name}", "value_type": value_type,
                                                    "value": value})
            got = stage.GetPrimAtPath("/World/Cube").GetAttribute(f"userProperties:{name}").Get()
            if isinstance(expected, tuple):
                assert _close(got, expected), (value_type, got)
            else:
                assert got == expected, (value_type, got)
        stage, _, _, _ = _run("set_attribute", {"attr_name": "tex", "value_type": "asset", "value": "./wood.png"})
        assert stage.GetPrimAtPath("/World/Cube").GetAttribute("tex").Get().path == "./wood.png"
        try:
            _run("set_attribute", {"attr_name": "size", "value_type": "float", "value": 1.0})  # size is a double
        except RuntimeError as exc:
            assert "already has type" in str(exc)
        else:
            raise AssertionError("a type mismatch should be refused")


INJECTIONS = [
    "/World/Cube'); __import__('os').system('echo pwned'); ('",
    "/World/Cube\nimport os",
    "/World/__import__('os')",
    "World/Relative",
    "/World/Cube.size",
    "/",
    "",
]


def test_injection_in_paths_names_and_numbers_is_refused():
    for bad in INJECTIONS:
        assert pt.python_for("inspect_prim", {"prim_path": bad})["ok"] is False, bad
    assert pt.python_for("rename", {"new_name": "Box\n__import__('os')"})["ok"] is False
    assert pt.python_for("rename", {"new_name": "a b"})["ok"] is False
    assert pt.python_for("set_attribute", {"attr_name": "size = 1; import os"})["ok"] is False
    assert pt.python_for("create_prim", {"type_name": "Cube; import os"})["ok"] is False
    assert pt.python_for("move", {"translate": ["__import__('os')", 0, 0]})["ok"] is False
    assert pt.python_for("move", {"translate": [float("nan"), 0, 0]})["ok"] is False
    assert pt.python_for("move", {"translate": [True, 0, 0]})["ok"] is False
    assert pt.python_for("create_camera", {"focal_length": "1+1"})["ok"] is False
    assert pt.python_for("add_reference", {"asset_path": "x.usd\n__import__('os')"})["ok"] is False
    assert pt.python_for("create_variant_set", {"variants": ["ok", "bad name"]})["ok"] is False
    assert pt.python_for("export", {"export_path": "evil.py"})["ok"] is False
    assert pt.python_for("create_light", {"light_type": "rect", "texture_file": "./sky.hdr"})["ok"] is False


def _no_import_calls(code: str) -> bool:
    tree = ast.parse(code)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    return "__import__" not in names and "eval" not in names and "exec" not in names


def test_free_text_values_become_harmless_literals():
    payload = "evil'); __import__('os').system('echo pwned'); x=('.usd"
    for context in pt.CONTEXTS:
        result = pt.python_for("add_reference", {"asset_path": payload}, context=context)
        assert result["ok"] and _no_import_calls(result["code"])
        assignment = next(line for line in result["code"].splitlines() if line.startswith("asset_path = "))
        namespace: dict = {}
        exec(assignment, {"__builtins__": {}}, namespace)  # the settings line is a pure literal
        assert namespace["asset_path"] == payload
    text = "line1\n\"); __import__('os').system('x') #"
    result = pt.python_for("set_attribute", {"attr_name": "note", "value_type": "string", "value": text})
    assert result["ok"] and _no_import_calls(result["code"])
    assignment = next(line for line in result["code"].splitlines() if line.startswith("value = "))
    namespace = {}
    exec(assignment, {"__builtins__": {}}, namespace)
    assert namespace["value"] == text
    if Usd is not None:
        with _workspace_guard():
            stage, _, out, _ = _run("set_attribute", {"attr_name": "note", "value_type": "string", "value": text})
            assert stage.GetPrimAtPath("/World/Cube").GetAttribute("note").Get() == text
            assert "pwned" not in out


def test_cross_field_checks():
    assert pt.python_for("duplicate", {"target_path": "/World/Cube/Inner"})["ok"] is False
    assert pt.python_for("create_camera", {"position": [1, 1, 1], "target": [1, 1, 1]})["ok"] is False
    assert pt.python_for("create_variant_set", {"variants": ["a", "b"], "selection": "c"})["ok"] is False
    assert pt.python_for("time_samples", {"start_frame": 10, "end_frame": 5})["ok"] is False
    assert pt.python_for("create_light", {"light_type": "dome"})["args"]["prim_path"] == "/World/DomeLight"


# ------------------------------------------------------------------------------------- review fixes (round 6)


def test_undo_demo_refuses_an_existing_prim_in_both_contexts():
    # Kit's CreatePrim re-types an existing prim and its undo DELETES that path: the demo must never touch one.
    if Usd is None:
        return
    with _workspace_guard():
        for context in ("standalone", "script_editor"):
            try:
                stage, _, _, _ = _run("undo_hint", {"prim_path": "/World/Cube"}, context=context)
            except RuntimeError as exc:
                assert "already exists" in str(exc), (context, exc)
            else:
                raise AssertionError(f"{context}: the undo demo ran on an existing prim")
        code = pt.python_for("undo_hint", {"prim_path": "/World/Cube"})["code"]
        assert code.index("already exists") < code.index("omni.kit.commands.execute")


def test_isaac_transform_refuses_pivot_ops_with_a_clear_message():
    # A translate:pivot op has the translate op TYPE; it used to be picked as "the" translate op (Tf error, or the
    # inverse pivot op written to).
    if Usd is None:
        return
    with _workspace_guard():
        folder = _new_folder()
        path = _write_fixture(folder)
        stage = Usd.Stage.Open(str(path))
        robot = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Robot"))
        robot.AddTranslateOp(opSuffix="pivot").Set(Gf.Vec3f(0.5, 0, 0))
        robot.AddTranslateOp(opSuffix="pivot", isInverseOp=True)
        stage.Save()
        before = list(robot.GetXformOpOrderAttr().Get())
        code = pt.python_for("set_transform_isaac", {"prim_path": "/World/Robot", "stage_path": str(path)},
                             context="standalone")["code"]
        try:
            _exec(code, "isaac_pivot")
        except RuntimeError as exc:
            assert "xformOp:translate:pivot" in str(exc), exc
        else:
            raise AssertionError("a pivot stack must be refused")
        assert list(robot.GetXformOpOrderAttr().Get()) == before  # same layer the snippet opened: nothing changed


def test_relative_export_lands_next_to_the_scene_not_in_the_current_folder():
    # stage.Export('exported_scene.usda') used to write into the process's current folder (Kit: its install folder).
    if Usd is None:
        return
    with _workspace_guard():
        for flatten in (True, False):
            name = f"relative_export_{int(flatten)}.usda"
            stray = Path.cwd() / name
            assert not stray.exists()
            stage, namespace, out, folder = _run("export", {"export_path": name, "flatten": flatten})
            assert (folder / name).is_file() and not stray.exists(), out
        anonymous = pt.python_for("export", {"export_path": "x.usda"})["code"]
        assert "Save the scene first" in anonymous
