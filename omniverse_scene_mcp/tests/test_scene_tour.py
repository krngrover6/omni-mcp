"""scene_tour: the plain-language "explain my scene" tour, exercised on small temporary USDA scenes."""

import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp.scene_tour import scene_tour
from omniverse_scene_mcp.usd_insight import TimeBudget

SESSION02 = Path(r"C:\Users\krngr\Omniverse\isaacsim-mentorship\session02\main.usda")
# The bridge reload scripts run this file inside Isaac Sim / Composer as a self-test; skip the real-scene smoke test
# there so a reload never opens (and composes the references of) the user's homework scene on Kit's main thread.
_IN_KIT = "carb" in __import__("sys").modules or "omni.kit.app" in __import__("sys").modules

CRATE = """#usda 1.0
(
    defaultPrim = "Crate"
    metersPerUnit = 1
    upAxis = "Z"
)

def Xform "Crate"
{
    def Cube "Body"
    {
        double size = 1
        double3 xformOp:translate = (0, 0, 0.5)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
}
"""

LIGHTING = """#usda 1.0

over "World"
{
    over "Warehouse"
    {
        over "Lights"
        {
            over "MainLight"
            {
                float inputs:intensity = 2000
            }
        }
    }
}
"""

MAIN = """#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Z"
    subLayers = [@./lighting.usda@]
)

def Xform "World"
{
    def Xform "Warehouse"
    {
        def Xform "Environment"
        {
            def Cube "Floor" (
                prepend apiSchemas = ["PhysicsCollisionAPI"]
            )
            {
                double size = 1
                double3 xformOp:translate = (0, 0, -0.05)
                double3 xformOp:scale = (20, 20, 0.1)
                uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
            }

            def Cube "BackWall"
            {
                double size = 1
                double3 xformOp:translate = (0, 9.9, 2.5)
                double3 xformOp:scale = (20, 0.2, 5)
                uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
            }
        }

        def Xform "Shelves"
        {
            def Cube "Shelf"
            {
                double size = 1
                double3 xformOp:translate = (0, 9, 1)
                double3 xformOp:scale = (4, 1, 0.1)
                uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:scale"]
            }

            def "Crate" (
                prepend references = @./crate.usda@
            )
            {
                double3 xformOp:translate = (1, 9, 1.05)
                uniform token[] xformOpOrder = ["xformOp:translate"]
            }
        }

        def Xform "BoxStack" (
            prepend apiSchemas = ["MaterialBindingAPI"]
        )
        {
            rel material:binding = </World/Warehouse/Looks/Cardboard>
            double3 xformOp:translate = (2, 0, 0)
            uniform token[] xformOpOrder = ["xformOp:translate"]

            def Cube "Box_01" (
                prepend apiSchemas = ["PhysicsRigidBodyAPI", "PhysicsCollisionAPI"]
            )
            {
                double size = 1.2
                double3 xformOp:translate = (0, 0, 0.6)
                uniform token[] xformOpOrder = ["xformOp:translate"]
            }

            def Cube "Box_02"
            {
                double size = 1.2
                double3 xformOp:translate = (0, 0, 1.8)
                uniform token[] xformOpOrder = ["xformOp:translate"]
            }
        }

        def Xform "Lights"
        {
            def RectLight "MainLight"
            {
                float inputs:intensity = 5000
                float inputs:width = 2
                float inputs:height = 2
                double3 xformOp:translate = (2, 0, 5)
                uniform token[] xformOpOrder = ["xformOp:translate"]
            }
        }

        def Camera "MainCamera"
        {
            float focalLength = 24
            float horizontalAperture = 20.955
            double3 xformOp:translate = (2, -10, 1)
            float3 xformOp:rotateXYZ = (90, 0, 0)
            uniform token[] xformOpOrder = ["xformOp:translate", "xformOp:rotateXYZ"]
        }

        def Scope "Looks"
        {
            def Material "Cardboard"
            {
                token outputs:surface.connect = </World/Warehouse/Looks/Cardboard/Shader.outputs:surface>

                def Shader "Shader"
                {
                    uniform token info:id = "UsdPreviewSurface"
                    color3f inputs:diffuseColor = (0.6, 0.4, 0.2)
                    token outputs:surface
                }
            }
        }
    }

    def PhysicsScene "PhysicsScene"
    {
    }
}

def "Render"
{
    def "Vars"
    {
    }
}

over Camera "OmniverseKit_Persp"
{
    double3 xformOp:translate = (0, -20, 5)
}
"""


def _write_warehouse(folder: Path) -> Path:
    (folder / "crate.usda").write_text(CRATE, encoding="utf-8")
    (folder / "lighting.usda").write_text(LIGHTING, encoding="utf-8")
    main = folder / "main.usda"
    main.write_text(MAIN, encoding="utf-8")
    return main


def _hashes(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


def _items(result: dict) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for section in result["sections"]:
        for item in section["items"]:
            found.setdefault(item["path"], []).append(item)
    return found


def _plain(result: dict, path: str, kind: str | None = None) -> str:
    entries = [i for i in _items(result).get(path, []) if kind is None or i["kind"] == kind]
    assert entries, f"no item for {path} ({kind})"
    return entries[0]["plain"]


def _section(result: dict, title_start: str) -> dict:
    return next(s for s in result["sections"] if s["title"].startswith(title_start))


def _tour_warehouse(**kwargs):
    folder = Path(tempfile.mkdtemp(prefix="scene_tour_"))
    main = _write_warehouse(folder)
    stage = Usd.Stage.Open(str(main))
    return folder, stage, scene_tour(stage, **kwargs)


def test_overview_names_the_scene_size_and_units():
    folder, _stage, result = _tour_warehouse()
    try:
        assert result["ok"] is True
        overview = result["overview"]
        assert "main.usda" in overview and "/World (the default prim)" in overview
        assert "warehouse" in overview
        assert "Z is up and 1 unit = 1 metre" in overview
        assert "20 m wide" in overview
        assert "1 sublayer" in overview and "1 referenced asset" in overview
        assert "editor helper" in overview
        assert 2 <= overview.count(". ") + 1 <= 4
        assert result["units"] == {
            "up_axis": "Z", "meters_per_unit": 1.0, "in_words": "Z is up and 1 unit = 1 metre",
            "directions": "In this tour +Z is up, +Y is 'the back' and +X is 'the right'.",
        }
        assert result["truncated"] is False and result["truncated_by_time"] is False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_groups_describe_contents_scale_and_default_prim():
    folder, _stage, result = _tour_warehouse()
    try:
        world = _plain(result, "/World", "group")
        assert "World is a group (Xform) at the top of the scene (it is the default prim)" in world
        stack = _plain(result, "/World/Warehouse/BoxStack", "group")
        assert "holding Box_01 and Box_02" in stack
        assert "1.2 m x 1.2 m x 2.4 m" in stack
        looks = _plain(result, "/World/Warehouse/Looks", "group")
        assert "a folder (Scope)" in looks
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_geometry_sizes_and_relative_placement():
    folder, _stage, result = _tour_warehouse()
    try:
        box1 = _plain(result, "/World/Warehouse/BoxStack/Box_01", "cube")
        assert box1.startswith("A Cube named Box_01, 1.2 m on each side, sitting on the floor at x=2, y=0.")
        assert "uses the material Cardboard" in box1
        box2 = _plain(result, "/World/Warehouse/BoxStack/Box_02", "cube")
        assert "resting on top of Box_01" in box2
        assert "this looks like the floor" in _plain(result, "/World/Warehouse/Environment/Floor", "cube")
        assert "a wall along the back of the scene" in _plain(result, "/World/Warehouse/Environment/BackWall", "cube")
        shelf = _plain(result, "/World/Warehouse/Shelves/Shelf", "cube")
        assert "4 m wide, 1 m deep and 0.1 m tall" in shelf
        assert "raised 0.95 m above the floor against the back wall (BackWall)" in shelf
        crate = _plain(result, "/World/Warehouse/Shelves/Crate/Body", "cube")
        assert "resting on top of Shelf" in crate
        item = _items(result)["/World/Warehouse/BoxStack/Box_01"][0]
        assert item["facts"]["local_position_m"] == [0.0, 0.0, 0.6]
        assert item["facts"]["world_position_m"] == [2.0, 0.0, 0.6]
        assert "local" in item["tip"] and "BoxStack" in item["tip"]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_light_explains_type_aim_and_hidden_opinion():
    folder, _stage, result = _tour_warehouse()
    try:
        item = _items(result)["/World/Warehouse/Lights/MainLight"][0]
        plain = item["plain"]
        assert "rectangular panel light" in plain
        assert "Intensity 5000." in plain
        assert "The panel is 2 m x 2 m." in plain
        assert "5 m above the floor" in plain
        assert "points straight down at Box_02 in the BoxStack group" in plain
        assert item["facts"]["aimed_at"] == "/World/Warehouse/BoxStack/Box_02"
        assert "lighting.usda also says 2000" in item["tip"] and "main.usda" in item["tip"]
        questions = result["questions_to_explore"]
        assert 3 <= len(questions) <= 5
        assert any("MainLight's intensity" in q and "hidden by a stronger layer" in q for q in questions)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_camera_material_and_physics_sections():
    folder, _stage, result = _tour_warehouse()
    try:
        camera = _plain(result, "/World/Warehouse/MainCamera", "camera")
        assert "in front of the scene" in camera
        assert "looks level, toward the back at Box_01 in the BoxStack group, about 9.4 m away" in camera
        assert "24 mm lens" in camera
        material = _plain(result, "/World/Warehouse/Looks/Cardboard", "material")
        assert "using UsdPreviewSurface" in material and "orange" in material
        assert "bound to BoxStack" in material
        assert "is a rigid body" in _plain(result, "/World/Warehouse/BoxStack/Box_01", "rigid_body")
        assert "has a collider" in _plain(result, "/World/Warehouse/BoxStack/Box_01", "rigid_body")
        assert "static collider" in _plain(result, "/World/Warehouse/Environment/Floor", "collider")
        assert "Earth gravity" in _plain(result, "/World/PhysicsScene", "physics_scene")
        assert any("press Play" in q for q in result["questions_to_explore"]) or len(result["questions_to_explore"]) == 5
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_layers_references_and_editor_helpers():
    folder, _stage, result = _tour_warehouse()
    try:
        layers = _section(result, "Layers")
        texts = [i["plain"] for i in layers["items"]]
        assert any(t.startswith("main.usda is the root layer") for t in texts)
        assert any("lighting.usda is a sublayer of main.usda" in t and "opinions in main.usda win" in t for t in texts)
        assert "brings in crate.usda by reference" in _plain(result, "/World/Warehouse/Shelves/Crate", "reference")
        helpers = _section(result, "Editor helpers")
        paths = {i["path"] for i in helpers["items"]}
        assert paths == {"/Render", "/OmniverseKit_Persp"}
        persp = _plain(result, "/OmniverseKit_Persp")
        assert "Perspective camera" in persp and "not your content" in persp and "'over'" in persp
        terms = {g["term"] for g in result["glossary"]}
        assert {"prim", "Xform", "layer", "sublayer", "reference", "rigid body", "collider", "editor helper", "over"} <= terms
        assert all(g["meaning"] for g in result["glossary"])
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_session_layer_only_prims_are_editor_helpers():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Box")
    with Usd.EditContext(stage, stage.GetSessionLayer()):
        UsdGeom.Xform.Define(stage, "/Scratch")
    result = scene_tour(stage)
    assert "only in the session layer" in _plain(result, "/Scratch", "editor_helper")
    assert any(i["kind"] == "session_layer" for i in _section(result, "Layers")["items"])
    assert "/World/Box" in _items(result)


def test_y_up_centimetre_stage_wording():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y)
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    UsdGeom.Xform.Define(stage, "/World")
    floor = UsdGeom.Cube.Define(stage, "/World/Ground")
    floor.CreateSizeAttr(1.0)
    floor.AddTranslateOp().Set((0, -0.5, 0))
    floor.AddScaleOp().Set((1000, 1, 1000))
    box = UsdGeom.Cube.Define(stage, "/World/Box")
    box.CreateSizeAttr(100.0)
    box.AddTranslateOp().Set((150, 50, -200))
    result = scene_tour(stage)
    assert "Y is up and 1 unit = 1 centimetre (0.01 m), so a value of 100 means one metre" in result["overview"]
    assert result["units"]["directions"].startswith("In this tour +Y is up, -Z is 'the back'")
    plain = _plain(result, "/World/Box", "cube")
    assert plain.startswith("A Cube named Box, 1 m on each side, sitting on the floor at x=1.5, z=-2.")
    assert "this looks like the floor" in _plain(result, "/World/Ground", "cube")
    assert any("centimetre" in q for q in result["questions_to_explore"])


def test_max_prims_truncates_and_says_so():
    folder, _stage, result = _tour_warehouse(max_prims=3)
    try:
        assert result["ok"] is True
        assert result["truncated"] is True
        assert result["counts"]["prims_visited"] == 3
        assert "3+ prims" in result["overview"] and "partial" in result["overview"]
        assert any("max_prims=3" in note for note in result["notes"])
        assert result["sections"]
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_deadline_stops_the_walk():
    folder = Path(tempfile.mkdtemp(prefix="scene_tour_"))
    try:
        stage = Usd.Stage.Open(str(_write_warehouse(folder)))
        result = scene_tour(stage, deadline=TimeBudget(0))
        assert result["ok"] is True and result["truncated_by_time"] is True
        assert result["time_budget"]["truncated_by_time"] is True
        past = scene_tour(stage, deadline=time.monotonic() - 1.0)
        assert past["truncated_by_time"] is True
        relaxed = scene_tour(stage, deadline=TimeBudget(60_000))
        assert relaxed["truncated_by_time"] is False and relaxed["time_budget"]["truncated_by_time"] is False
        json.dumps(result, allow_nan=False)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_result_is_json_serializable():
    folder, _stage, result = _tour_warehouse()
    try:
        text = json.dumps(result, allow_nan=False)
        assert json.loads(text)["ok"] is True
        for section in result["sections"]:
            assert section["title"] and section["items"]
            for item in section["items"]:
                assert set(item) <= {"path", "kind", "plain", "tip", "facts"}
                assert item["plain"].endswith((".", ")", "?"))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_tour_is_read_only():
    folder = Path(tempfile.mkdtemp(prefix="scene_tour_"))
    try:
        main = _write_warehouse(folder)
        before = _hashes(folder)
        stage = Usd.Stage.Open(str(main))
        session_before = stage.GetSessionLayer().ExportToString()
        scene_tour(stage)
        scene_tour(stage, focus_path="/World/Warehouse/BoxStack", depth=1, max_prims=5)
        assert all(not layer.dirty for layer in stage.GetUsedLayers())
        assert stage.GetSessionLayer().ExportToString() == session_before
        del stage
        assert _hashes(folder) == before
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_focus_path_and_depth():
    folder, stage, _result = _tour_warehouse()
    try:
        focused = scene_tour(stage, focus_path="/World/Warehouse/BoxStack")
        assert focused["ok"] is True and focused["focus_path"] == "/World/Warehouse/BoxStack"
        assert "focuses on /World/Warehouse/BoxStack" in focused["overview"]
        geometry = {i["path"] for i in _section(focused, "Shapes")["items"]}
        assert geometry == {"/World/Warehouse/BoxStack/Box_01", "/World/Warehouse/BoxStack/Box_02"}
        shallow = scene_tour(stage, depth=1)
        groups = {i["path"] for i in _section(shallow, "Groups")["items"]}
        assert groups == {"/World"}
        missing = scene_tour(stage, focus_path="/World/Nope")
        assert missing["ok"] is False and "No prim" in missing["error"]
        bad = scene_tour(stage, focus_path="not a path")
        assert bad["ok"] is False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_bad_arguments_raise():
    stage = Usd.Stage.CreateInMemory()
    for kwargs in ({"depth": -1}, {"depth": True}, {"max_prims": 0}, {"max_prims": 2.5}):
        try:
            scene_tour(stage, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {kwargs}")
    empty = scene_tour(stage)
    assert empty["ok"] is True and "Y is up" in empty["overview"]
    assert _section(empty, "Layers")["items"]


def test_smoke_on_a_temp_copy_of_session02_main():
    if _IN_KIT or not SESSION02.is_file():
        return  # the mentorship scene is not on this machine
    folder = Path(tempfile.mkdtemp(prefix="scene_tour_session02_"))
    try:
        copy = folder / "main.usda"
        shutil.copyfile(SESSION02, copy)
        stage = Usd.Stage.Open(str(copy))
        result = scene_tour(stage)
        assert result["ok"] is True
        assert result["sections"] and all(section["items"] for section in result["sections"])
        json.dumps(result, allow_nan=False)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
