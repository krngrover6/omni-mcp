"""joints: create joints, drives, limits and articulation roots like Isaac Sim's menu, previewed first."""
from __future__ import annotations

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features, joints as j
from omniverse_scene_mcp.undo import UndoStack


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)

    def body(path, t, rigid=True, scale=(1, 1, 1), collider=True):
        cube = UsdGeom.Cube.Define(stage, path)
        api = UsdGeom.XformCommonAPI(cube)
        api.SetTranslate(Gf.Vec3d(*t))
        api.SetScale(Gf.Vec3f(*scale))
        if collider:
            UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
        if rigid:
            UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())

    body("/World/Base", (0, 0, 1), scale=(2, 1, 0.2))
    body("/World/Wheel", (1, 1, 1))
    body("/World/Door", (3, 0, 1), scale=(0.05, 1, 1))
    body("/World/Wall", (5, 0, 0), rigid=False)
    body("/World/Ghost", (7, 0, 1), collider=False)
    return stage


def _text(stage):
    return stage.GetRootLayer().ExportToString()


def test_preview_matches_isaacs_frame_maths_and_writes_nothing():
    stage = _stage()
    before = _text(stage)
    plan = j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", axis="Y")
    assert plan["ok"] and plan["dry_run"] and not plan["applied"]
    p = plan["plan"]
    assert p["joint_path"] == "/World/Wheel/RevoluteJoint"
    # Isaac: the frame sits at body1's origin; localPos0 is that point in body0's (scaled) frame
    assert p["local_pos1"] == [0.0, 0.0, 0.0] and p["local_rot1"] == [1.0, 0.0, 0.0, 0.0]
    assert p["local_pos0"] == [0.5, 1.0, 0.0]
    assert p["axis_world_direction"] == [0.0, 1.0, 0.0] and p["motion_left"].startswith("one turn")
    assert "UsdPhysics.RevoluteJoint.Define" in plan["python"]
    compile(plan["python"], "<snippet>", "exec")
    assert _text(stage) == before


def test_create_with_limits_is_undoable():
    stage = _stage()
    before = _text(stage)
    stack = UndoStack()
    made = j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", axis="Y", lower=-45, upper=90,
                          undo_stack=stack, dry_run=False)
    assert made["applied"] and made["undo"]["recorded"]
    joint = UsdPhysics.RevoluteJoint(stage.GetPrimAtPath("/World/Wheel/RevoluteJoint"))
    assert joint.GetAxisAttr().Get() == "Y" and joint.GetLowerLimitAttr().Get() == -45 and joint.GetUpperLimitAttr().Get() == 90
    assert [str(t) for t in joint.GetBody0Rel().GetTargets()] == ["/World/Base"]
    assert joint.GetBreakForceAttr().Get() > 1e38
    assert j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel")["plan"]["joint_path"] == \
        "/World/Wheel/RevoluteJoint_01"
    assert stack.undo_last(stage=stage, dry_run=False)["ok"]
    assert not stage.GetPrimAtPath("/World/Wheel/RevoluteJoint")
    assert _text(stage) == before


def test_pivot_on_an_edge_and_pinning_to_the_world():
    stage = _stage()
    door = j.create_joint(stage, "revolute", None, "/World/Door", pivot=[3, -1, 1], axis="Z", dry_run=False)
    p = door["plan"]
    assert p["pinned_to_world"] and p["pivot_world"] == [3.0, -1.0, 1.0]
    assert p["local_pos1"] == [0.0, -1.0, 0.0]  # the door's left edge in its own frame
    joint = UsdPhysics.Joint(stage.GetPrimAtPath(p["joint_path"]))
    assert joint.GetBody0Rel().GetTargets() == [] and joint.GetLocalPos0Attr().Get() == Gf.Vec3f(3, -1, 1)
    one_body = j.create_joint(stage, "fixed", "/World/Base")  # a single body is body1, pinned to the world
    assert one_body["plan"]["body1"] == "/World/Base" and one_body["plan"]["body0"] is None


def test_refusals_explain_what_to_do():
    stage = _stage()
    static = j.create_joint(stage, "revolute", "/World/Wall")
    assert not static["ok"] and "Cannot create a joint between static bodies" in static["error"]
    assert "Rigid Body with Colliders Preset" in static["error"]
    assert "same prim" in j.create_joint(stage, "fixed", "/World/Base", "/World/Base")["error"]
    assert not j.create_joint(stage, "hinge", "/World/Base", "/World/Wheel")["ok"]
    assert not j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", axis="W")["ok"]
    assert "smaller" in j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", lower=10, upper=5)["error"]
    assert not j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", lower=-400)["ok"]
    assert not j.create_joint(stage, "fixed", "/World/Base", "/World/Wheel", lower=0, upper=1)["ok"]
    assert not j.create_joint(stage, "distance", "/World/Base", "/World/Wheel", min_distance=3, max_distance=1)["ok"]
    assert not j.create_joint(stage, "revolute", "/World/Nope")["ok"]
    assert not j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", name="bad name")["ok"]
    ghost = j.create_joint(stage, "revolute", "/World/Base", "/World/Ghost")
    assert ghost["ok"] and any("no collider" in w for w in ghost["warnings"])


def test_spherical_and_distance_limits():
    stage = _stage()
    sph = j.create_joint(stage, "spherical", "/World/Base", "/World/Wheel", cone0=45, cone1=30, dry_run=False)
    joint = UsdPhysics.SphericalJoint(stage.GetPrimAtPath(sph["plan"]["joint_path"]))
    assert joint.GetConeAngle0LimitAttr().Get() == 45 and joint.GetConeAngle1LimitAttr().Get() == 30
    dist = j.create_joint(stage, "distance", "/World/Base", "/World/Wheel", min_distance=0.5, max_distance=2, dry_run=False)
    joint = UsdPhysics.DistanceJoint(stage.GetPrimAtPath(dist["plan"]["joint_path"]))
    assert joint.GetMaxDistanceAttr().Get() == 2


def test_drives_need_the_right_gain_and_fit_the_joint_type():
    stage = _stage()
    j.create_joint(stage, "revolute", None, "/World/Door", axis="Z", dry_run=False)
    path = "/World/Door/RevoluteJoint"
    assert "stiffness" in j.add_drive(stage, path, target_position=0)["error"]
    assert "damping" in j.add_drive(stage, path, target_velocity=90)["error"]
    assert not j.add_drive(stage, path)["ok"]
    assert not j.add_drive(stage, path, target_position=0, stiffness=-1)["ok"]
    preview = j.add_drive(stage, path, target_position=0, stiffness=1000, damping=50)
    assert preview["dry_run"] and not stage.GetPrimAtPath(path).HasAPI(UsdPhysics.DriveAPI, "angular")
    done = j.add_drive(stage, path, target_position=0, stiffness=1000, damping=50, dry_run=False)
    drive = UsdPhysics.DriveAPI(stage.GetPrimAtPath(path), "angular")
    assert done["applied"] and drive.GetStiffnessAttr().Get() == 1000 and drive.GetTypeAttr().Get() == "force"
    compile(done["python"], "<snippet>", "exec")
    j.create_joint(stage, "prismatic", "/World/Base", "/World/Wheel", dry_run=False)
    slide = j.add_drive(stage, "/World/Wheel/PrismaticJoint", target_velocity=0.2, damping=100, dry_run=False)
    assert slide["plan"]["drive"] == "linear"
    j.create_joint(stage, "fixed", "/World/Base", "/World/Ghost", dry_run=False)
    assert "revolute" in j.add_drive(stage, "/World/Ghost/FixedJoint", target_position=0, stiffness=1)["error"]


def test_limits_and_articulation_root():
    stage = _stage()
    j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", dry_run=False)
    path = "/World/Wheel/RevoluteJoint"
    assert j.set_joint_limits(stage, path, -10, 20, dry_run=False)["applied"]
    assert UsdPhysics.RevoluteJoint(stage.GetPrimAtPath(path)).GetUpperLimitAttr().Get() == 20
    assert "smaller" in j.set_joint_limits(stage, path, upper=-30)["error"]
    assert not j.set_joint_limits(stage, path)["ok"]
    root = j.add_articulation_root(stage, "/World", dry_run=False)
    assert root["applied"] and stage.GetPrimAtPath("/World").HasAPI(UsdPhysics.ArticulationRootAPI)
    assert "already an articulation root" in j.add_articulation_root(stage, "/World/Base")["error"]
    lonely = j.add_articulation_root(Usd.Stage.CreateInMemory(), "/")
    assert not lonely["ok"]


def test_read_only_edit_target_is_refused():
    stage = _stage()
    stage.GetRootLayer().SetPermissionToEdit(False)
    result = j.create_joint(stage, "revolute", "/World/Base", "/World/Wheel", dry_run=False)
    assert not result["ok"] and "read-only" in result["error"]
    stage.GetRootLayer().SetPermissionToEdit(True)


def test_registered_as_previewing_edit_tools():
    for name in ("scene_create_joint", "scene_add_drive", "scene_set_joint_limits", "scene_add_articulation_root"):
        spec = features.FEATURES[name]
        assert spec.location == "stage" and spec.mutates
        assert features.signature(name).parameters["dry_run"].default is True
        assert "undo_stack" not in features.signature(name).parameters
    stage = _stage()
    result = features.invoke("scene_create_joint", {"kind": "revolute", "body0": "/World/Base", "body1": "/World/Wheel",
                                                     "dry_run": "false"}, stage=stage)
    assert result["dry_run"] is True and not stage.GetPrimAtPath("/World/Wheel/RevoluteJoint")
