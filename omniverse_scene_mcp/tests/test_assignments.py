"""Assignment coach: rubric files and the session 02 warehouse checker on synthetic temporary stages."""

import json
import os
import shutil
import tempfile
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    # unittest.SkipTest at import is a module skip for pytest and tools/run_tests.py alike, with or without pytest.
    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import assignments

WORKSPACE = Path(__file__).resolve().parents[2]
REAL_SCENE = WORKSPACE / "isaacsim-mentorship" / "session02" / "main.usda"
# The bridge reload scripts run this file inside Isaac Sim / Composer as a self-test; skip the real-scene smoke test
# there so a reload never opens (and composes the references of) the user's homework scene on Kit's main thread.
_IN_KIT = "carb" in __import__("sys").modules or "omni.kit.app" in __import__("sys").modules
RULE_IDS = [
    "floor",
    "three_walls",
    "open_front_top",
    "two_shelves_on_wall",
    "objects_on_shelves",
    "ten_cube_stack",
    "camera_aimed",
    "key_light_aimed",
    "extra_lights_shelves",
    "clear_names",
    "grouping",
    "static_scene",
]


# ----------------------------------------------------------------------------------------------- scene builder


def _mapper(up):
    """Positions are written Z-up (x, depth, height); for a Y-up stage rotate them to (x, height, -depth)."""
    if up == "Z":
        return lambda p: Gf.Vec3d(*p), lambda s: Gf.Vec3f(*s)
    return (lambda p: Gf.Vec3d(p[0], p[2], -p[1])), (lambda s: Gf.Vec3f(s[0], s[2], s[1]))


def _build(
    up="Z",
    *,
    drop_wall=None,
    front_wall=False,
    rows=(4, 3, 2, 1),
    camera_target=(0, 0, 1.2),
    grouped=True,
    shelf_box_lift=0.0,
):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if up == "Y" else UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    pos, scl = _mapper(up)

    def group(path):
        if grouped:
            UsdGeom.Xform.Define(stage, path)
            return path
        return "/World"

    env = group("/World/Warehouse")
    env = group(f"{env}/Environment") if grouped else env
    shelves = group(f"{env}/Shelves") if grouped else env
    lights = group("/World/Warehouse/Lights") if grouped else "/World"
    stack_root = group("/World/Warehouse/BoxStack") if grouped else "/World"
    cam_root = "/World/Warehouse" if grouped else "/World"

    floor = UsdGeom.Mesh.Define(stage, f"{env}/Floor")
    corners = [pos(c) for c in ((-10, -10, 0), (10, -10, 0), (10, 10, 0), (-10, 10, 0))]
    floor.CreatePointsAttr([Gf.Vec3f(*c) for c in corners])
    floor.CreateFaceVertexCountsAttr([4])
    floor.CreateFaceVertexIndicesAttr([0, 1, 2, 3])

    def cube(path, center, scale):
        prim = UsdGeom.Cube.Define(stage, path)
        prim.CreateSizeAttr(1.0)
        prim.AddTranslateOp().Set(pos(center))
        prim.AddScaleOp().Set(scl(scale))
        return prim

    walls = {
        "BackWall": ((0, 6.9, 3), (16, 0.2, 6)),
        "LeftWall": ((-7.9, -0.1, 3), (0.2, 13.8, 6)),
        "RightWall": ((7.9, -0.1, 3), (0.2, 13.8, 6)),
    }
    if front_wall:
        walls["FrontWall"] = ((0, -7.1, 3), (16, 0.2, 6))
    for name, (center, scale) in walls.items():
        if name != drop_wall:
            cube(f"{env}/{name}", center, scale)

    for shelf_name, height in (("LowerShelf", 1.2), ("UpperShelf", 2.5)):
        cube(f"{shelves}/{shelf_name}", (0, 6, height), (5, 1.6, 0.15))
        top = height + 0.075
        for index, x in enumerate((-1.5, 0.0, 1.5), start=1):
            cube(f"{shelves}/{shelf_name}Box_{index:02d}", (x, 6, top + 0.3 + shelf_box_lift), (0.6, 0.6, 0.6))
    cube(f"{shelves}/LeftSupport", (-2.58, 6, 1.7), (0.16, 1.6, 3.4))
    cube(f"{shelves}/RightSupport", (2.58, 6, 1.7), (0.16, 1.6, 3.4))

    number = 0
    for row, count in enumerate(rows):
        for i in range(count):
            number += 1
            x = (i - (count - 1) / 2) * 0.6
            cube(f"{stack_root}/Box_{number:02d}", (x, 0, 0.3 + 0.6 * row), (0.6, 0.6, 0.6))

    def aim(prim, eye, target, up_hint=(0, 1, 0)):
        view = Gf.Matrix4d().SetLookAt(pos(eye), pos(target), Gf.Vec3d(*pos(up_hint)))
        UsdGeom.Xformable(prim).AddTransformOp().Set(view.GetInverse())

    main = UsdLux.RectLight.Define(stage, f"{lights}/MainLight")
    main.CreateIntensityAttr(5000.0)
    aim(main.GetPrim(), (0, 0, 6.3), (0, 0, 0))
    shelf_light = UsdLux.RectLight.Define(stage, f"{lights}/LowerShelfLight")
    shelf_light.CreateIntensityAttr(2000.0)
    aim(shelf_light.GetPrim(), (0, 5.35, 2.35), (0, 5.35, 0))
    camera = UsdGeom.Camera.Define(stage, f"{cam_root}/AssignmentCamera")
    aim(camera.GetPrim(), (0, -26, 7), camera_target, (0, 0, 1))
    return stage


def _statuses(report):
    return {item["rule"]: item["status"] for item in report["results"]}


def _expect(report, **overrides):
    expected = {rule: "pass" for rule in RULE_IDS}
    expected.update(overrides)
    assert _statuses(report) == expected, json.dumps(
        [(item["rule"], item["status"], item["detail"]) for item in report["results"]], indent=1
    )


# ------------------------------------------------------------------------------------------------------ rubric


def test_list_assignments_includes_session02_with_weights_summing_to_100():
    found = {item["id"]: item for item in assignments.list_assignments()}
    assert "isaac_session02_warehouse" in found
    entry = found["isaac_session02_warehouse"]
    assert entry["rule_count"] == len(RULE_IDS)
    assert entry["total_weight"] == 100


def test_every_rule_has_plain_english_why_steps_and_a_checker():
    rubric = assignments.load_assignment("isaac_session02_warehouse")
    assert [rule["id"] for rule in rubric["rules"]] == RULE_IDS
    for rule in rubric["rules"]:
        assert rule["title"] and rule["why"] and rule["weight"] > 0
        assert isinstance(rule["how_to_fix"], list) and rule["how_to_fix"]
        assert rule["id"] in assignments._CHECKERS


def test_unknown_assignment_and_missing_stage_are_not_ok():
    report = assignments.check_assignment(_build(), "no_such_homework")
    assert report["ok"] is False and "Unknown assignment" in report["error"]
    assert assignments.check_assignment(None)["ok"] is False
    assert assignments.check_assignment(_build(), "../etc/passwd")["ok"] is False


# ---------------------------------------------------------------------------------------------- full warehouse


def test_complete_warehouse_passes_every_rule_z_up():
    report = assignments.check_assignment(_build("Z"))
    assert report["ok"] is True
    _expect(report)
    assert report["score_pct"] == 100.0 and report["passed"] is True
    assert report["next_step"].startswith("Every check passes")
    stack = next(item for item in report["results"] if item["rule"] == "ten_cube_stack")
    assert stack["evidence"]["rows_bottom_to_top"] == [4, 3, 2, 1]
    assert stack["evidence"]["depth_axis"] == "Y"
    walls = next(item for item in report["results"] if item["rule"] == "three_walls")
    assert sorted(walls["evidence"]["walls_by_side"]) == ["+X", "+Y", "-X"]
    front = next(item for item in report["results"] if item["rule"] == "open_front_top")
    assert front["evidence"]["open_sides"] == ["-Y"] and front["evidence"]["camera_looks_in_from"] == "-Y"


def test_complete_warehouse_passes_every_rule_y_up():
    report = assignments.check_assignment(_build("Y"))
    _expect(report)
    assert report["scene"]["up_axis"] == "Y"
    front = next(item for item in report["results"] if item["rule"] == "open_front_top")
    assert front["evidence"]["open_sides"] == ["+Z"]


def test_check_never_changes_the_stage():
    stage = _build()
    before = stage.GetRootLayer().ExportToString()
    report = assignments.check_assignment(stage)
    assert report["read_only"] is True
    assert stage.GetRootLayer().ExportToString() == before
    session = stage.GetSessionLayer()
    assert not session.rootPrims and not session.dirty


# ------------------------------------------------------------------------------------------------ broken scenes


def test_missing_wall_is_partial_with_two_open_sides():
    report = assignments.check_assignment(_build(drop_wall="RightWall"))
    _expect(report, three_walls="partial", open_front_top="partial")
    assert report["passed"] is False
    assert report["next_step"].startswith("Three walls around the floor")


def test_four_walls_close_the_front():
    report = assignments.check_assignment(_build(front_wall=True))
    _expect(report, three_walls="partial", open_front_top="fail")
    assert report["next_step"].startswith("Front and top left open")


def test_four_four_two_stack_is_partial_and_names_the_rows():
    for up in ("Z", "Y"):
        report = assignments.check_assignment(_build(up, rows=(4, 4, 2)))
        _expect(report, ten_cube_stack="partial")
        stack = next(item for item in report["results"] if item["rule"] == "ten_cube_stack")
        assert stack["evidence"]["rows_bottom_to_top"] == [4, 4, 2]
        assert "4-4-2" in stack["detail"]


def test_unaimed_camera_fails_only_the_camera_rule():
    report = assignments.check_assignment(_build(camera_target=(-30, -26, 7)))
    _expect(report, camera_aimed="fail")
    camera = next(item for item in report["results"] if item["rule"] == "camera_aimed")
    assert camera["evidence"]["cameras"][0]["angle_to_stack_deg"] > 45
    assert report["next_step"].startswith("A camera aimed at the stack")


def test_slightly_off_camera_is_partial():
    report = assignments.check_assignment(_build(camera_target=(14, 0, 1.2)))  # about 28 degrees off
    _expect(report, camera_aimed="partial")


def test_no_grouping_fails_the_grouping_rule():
    report = assignments.check_assignment(_build(grouped=False))
    _expect(report, grouping="fail")
    grouping = next(item for item in report["results"] if item["rule"] == "grouping")
    assert grouping["evidence"]["top_container"] == "/World"


def test_boxes_floating_above_shelves_are_partial_with_the_gap():
    report = assignments.check_assignment(_build(shelf_box_lift=0.3))
    _expect(report, objects_on_shelves="partial")
    rule = next(item for item in report["results"] if item["rule"] == "objects_on_shelves")
    floating = rule["evidence"]["per_shelf"]["/World/Warehouse/Environment/Shelves/LowerShelf"]["floating"]
    assert len(floating) == 3 and all(abs(entry["gap"] - 0.3) < 1e-3 for entry in floating)


def test_default_names_and_animation_are_reported():
    stage = _build()
    renamed = UsdGeom.Cube.Define(stage, "/World/Warehouse/Cube_01")
    renamed.CreateSizeAttr(0.2)
    renamed.AddTranslateOp().Set(Gf.Vec3d(5, -5, 0.1))
    op = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Warehouse/Cube_01")).GetOrderedXformOps()[0]
    op.Set(Gf.Vec3d(5, -5, 0.1), 0)
    op.Set(Gf.Vec3d(5, -4, 0.1), 10)
    report = assignments.check_assignment(stage)
    statuses = _statuses(report)
    assert statuses["clear_names"] == "partial" and statuses["static_scene"] == "partial"
    names = next(item for item in report["results"] if item["rule"] == "clear_names")
    assert names["evidence"]["default_looking_names"] == ["/World/Warehouse/Cube_01"]


def test_empty_stage_starts_with_the_floor():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    report = assignments.check_assignment(stage)
    statuses = _statuses(report)
    assert report["ok"] is True and report["passed"] is False
    assert statuses["floor"] == "fail" and statuses["three_walls"] == "fail"
    assert statuses["open_front_top"] == "unknown" and statuses["objects_on_shelves"] == "unknown"
    assert statuses["camera_aimed"] == "fail" and statuses["grouping"] == "unknown"
    assert report["next_step"].startswith("One floor")
    assert report["score_pct"] < 20


# --------------------------------------------------------------------------------------------- files on disk


def test_check_scene_file_reads_a_saved_temp_scene_without_touching_it():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "warehouse.usda")
        stage = _build()  # keep the stage alive while its layer is used
        stage.GetRootLayer().Export(path)
        before = (os.stat(path).st_mtime_ns, Path(path).read_bytes())
        report = assignments.check_scene_file(path, workspace_root=tmp)
        assert (os.stat(path).st_mtime_ns, Path(path).read_bytes()) == before
    _expect(report)
    assert report["scene_file"].endswith("warehouse.usda")


def test_smoke_real_session02_scene_on_a_temp_copy():
    """The user's real homework: report what the checker says, without asserting a score."""
    if _IN_KIT or not REAL_SCENE.is_file():
        return
    with tempfile.TemporaryDirectory() as tmp:
        copy = os.path.join(tmp, "main.usda")
        shutil.copyfile(REAL_SCENE, copy)
        original = REAL_SCENE.stat().st_mtime_ns
        report = assignments.check_scene_file(copy, workspace_root=tmp)
        assert REAL_SCENE.stat().st_mtime_ns == original
    assert report["ok"] is True and len(report["results"]) == len(RULE_IDS)
    assert {item["status"] for item in report["results"]} <= {"pass", "partial", "fail", "unknown"}
    print(f"    real session02 copy: score {report['score_pct']}%, passed={report['passed']}")
    for item in report["results"]:
        print(f"      {item['status']:8} {item['rule']}: {item['detail']}")
    print(f"    next step: {report['next_step']}")


# ------------------------------------------------------------------------------------- review fixes (round 6)


def test_many_scattered_boxes_check_quickly_with_the_same_stack():
    # The cube-stack clustering compared every pair of boxes: 4000 props took ~9 s (minutes for a big referenced
    # warehouse), all on Kit's main thread in the live check.
    import random
    import time

    stage = _build("Z")
    UsdGeom.Xform.Define(stage, "/World/Props")
    rng = random.Random(7)
    for index in range(4000):
        prop = UsdGeom.Cube.Define(stage, f"/World/Props/Crate_{index}")
        prop.CreateSizeAttr(0.2)
        prop.AddTranslateOp().Set(Gf.Vec3d(rng.uniform(-200, -20), rng.uniform(-200, -20), rng.uniform(0.1, 5)))
    started = time.perf_counter()
    report = assignments.check_assignment(stage)
    assert time.perf_counter() - started < 5.0
    stack = next(item for item in report["results"] if item["rule"] == "ten_cube_stack")
    assert stack["status"] == "pass" and stack["evidence"]["cube_count"] == 10, stack


def test_non_finite_boxes_are_ignored_not_called_shelves():
    stage = _build("Z")
    for name, offset in (("NaNBox", float("nan")), ("InfBox", float("inf"))):
        cube = UsdGeom.Cube.Define(stage, f"/World/Warehouse/Environment/Shelves/{name}")
        cube.AddTranslateOp().Set(Gf.Vec3d(offset, 0, 2))
    report = assignments.check_assignment(stage)
    _expect(report)
    shelves = next(item for item in report["results"] if item["rule"] == "two_shelves_on_wall")
    assert {record["shelf"].rsplit("/", 1)[-1] for record in shelves["evidence"]["shelves"]} == {"LowerShelf", "UpperShelf"}
