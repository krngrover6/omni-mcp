"""UsdPhysics report and setup edits, run on temporary in-memory stages (Z-up and Y-up, meters and centimeters)."""

import json

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    # unittest.SkipTest at import is a module skip for pytest and tools/run_tests.py alike, with or without pytest.
    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import physics


def _stage(up_axis="Z", meters_per_unit=1.0):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up_axis)
    UsdGeom.SetStageMetersPerUnit(stage, meters_per_unit)
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Xform.Define(stage, "/World/Box")
    mesh = UsdGeom.Mesh.Define(stage, "/World/Box/Mesh")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    UsdGeom.Cube.Define(stage, "/World/Box/Cube")
    UsdGeom.Cube.Define(stage, "/World/Ground")
    UsdGeom.Xform.Define(stage, "/World/Empty")
    return stage


def _snapshot(stage):
    return stage.GetRootLayer().ExportToString(), stage.GetSessionLayer().ExportToString()


def _json(result):
    json.dumps(result, allow_nan=False)
    return result


def _names(changes, kind=None):
    return [(item["path"], item["name"]) for item in changes if kind is None or item["kind"] == kind]


def _has(stage, path, api):
    return api in stage.GetPrimAtPath(path).GetAppliedSchemas()


def _contains(messages, text):
    return any(text in message for message in messages)


def _instanced_stage():
    stage = _stage()
    UsdGeom.Xform.Define(stage, "/Proto")
    mesh = UsdGeom.Mesh.Define(stage, "/Proto/Part")
    mesh.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    UsdPhysics.CollisionAPI.Apply(mesh.GetPrim())
    inst = UsdGeom.Xform.Define(stage, "/World/Inst").GetPrim()
    inst.GetReferences().AddInternalReference("/Proto")
    inst.SetInstanceable(True)
    return stage


# --- physics_report ---------------------------------------------------------------------------------------


def test_report_empty_stage_has_no_scene_note_and_zero_counts():
    stage = _stage()
    before = _snapshot(stage)
    result = _json(physics.physics_report(stage))
    assert result["ok"] and result["read_only"]
    assert result["has_physics_scene"] is False and result["gravity"] is None
    assert result["prims"] == [] and result["joints"] == []
    assert all(value == 0 for value in result["counts"].values())
    assert _contains(result["notes"], "No PhysicsScene")
    assert result["warnings"] == []
    assert "next_step" not in result
    assert _snapshot(stage) == before


def test_report_lists_bodies_colliders_mass_joints_and_default_gravity_zup_meters():
    stage = _stage()
    UsdPhysics.Scene.Define(stage, "/physicsScene")  # gravity left at its schema defaults
    box = stage.GetPrimAtPath("/World/Box")
    body = UsdPhysics.RigidBodyAPI.Apply(box)
    body.CreateKinematicEnabledAttr(True)
    UsdPhysics.MassAPI.Apply(box).CreateMassAttr(3.5)
    mesh = stage.GetPrimAtPath("/World/Box/Mesh")
    UsdPhysics.CollisionAPI.Apply(mesh)
    UsdPhysics.MeshCollisionAPI.Apply(mesh).CreateApproximationAttr("convexHull")
    UsdPhysics.CollisionAPI.Apply(stage.GetPrimAtPath("/World/Ground"))
    UsdPhysics.ArticulationRootAPI.Apply(stage.GetPrimAtPath("/World"))
    joint = UsdPhysics.RevoluteJoint.Define(stage, "/World/Hinge")
    joint.CreateBody0Rel().SetTargets(["/World/Ground"])
    joint.CreateBody1Rel().SetTargets(["/World/Box"])
    before = _snapshot(stage)

    result = _json(physics.physics_report(stage))
    assert result["ok"] and result["has_physics_scene"]
    gravity = result["gravity"]
    assert gravity["direction"] == [0.0, 0.0, -1.0] and gravity["direction_source"].startswith("default")
    assert gravity["magnitude_stage_units"] == 9.81 and gravity["magnitude_m_per_s2"] == 9.81
    scene = result["physics_scenes"][0]
    assert scene["path"] == "/physicsScene" and scene["gravity_magnitude"] is None  # -inf is not JSON
    by_path = {entry["path"]: entry for entry in result["prims"]}
    assert by_path["/World/Box"]["rigid_body"] == {"enabled": True, "kinematic": True}
    assert by_path["/World/Box"]["mass"] == {"mass": 3.5, "density": None}
    assert by_path["/World/Box/Mesh"]["collider"]["approximation"] == "convexHull"
    assert by_path["/World/Box/Mesh"]["collider"]["points"] == 3
    assert "approximation" not in by_path["/World/Ground"]["collider"]
    assert by_path["/World"]["articulation_root"] is True
    assert result["articulation_roots"] == ["/World"]
    assert result["joints"] == [
        {
            "path": "/World/Hinge",
            "type": "PhysicsRevoluteJoint",
            "body0": "/World/Ground",
            "body1": "/World/Box",
            "enabled": True,
            "instance_proxy": False,
        }
    ]
    assert result["counts"] == {
        "rigid_bodies": 1,
        "colliders": 2,
        "mass": 1,
        "mesh_collision": 1,
        "articulation_roots": 1,
        "joints": 1,
    }
    assert result["warnings"] == []
    assert _contains(result["notes"], "1 collider(s) have no rigid body above them")
    assert _snapshot(stage) == before


def test_report_authored_gravity_on_yup_centimeter_stage():
    stage = _stage("Y", 0.01)
    scene = UsdPhysics.Scene.Define(stage, "/physicsScene")
    scene.CreateGravityDirectionAttr(Gf.Vec3f(0, -2, 0))
    scene.CreateGravityMagnitudeAttr(981.0)
    result = _json(physics.physics_report(stage))
    assert result["stage"] == {"up_axis": "Y", "meters_per_unit": 0.01, "kilograms_per_unit": 1.0}
    assert result["gravity"]["direction"] == [0.0, -1.0, 0.0]
    assert result["gravity"]["magnitude_stage_units"] == 981.0
    assert abs(result["gravity"]["magnitude_m_per_s2"] - 9.81) < 1e-6


def test_report_default_gravity_scales_with_centimeters_and_yup():
    stage = _stage("Y", 0.01)
    UsdPhysics.Scene.Define(stage, "/physicsScene")
    gravity = _json(physics.physics_report(stage))["gravity"]
    assert gravity["direction"] == [0.0, -1.0, 0.0]
    assert gravity["magnitude_stage_units"] == 981.0


def test_report_warnings_for_setup_mistakes():
    stage = _stage()
    UsdPhysics.RigidBodyAPI.Apply(stage.GetPrimAtPath("/World/Empty"))  # no collider anywhere
    UsdPhysics.RigidBodyAPI.Apply(stage.GetPrimAtPath("/World/Box"))
    UsdPhysics.RigidBodyAPI.Apply(stage.GetPrimAtPath("/World/Box/Cube"))  # nested inside /World/Box
    UsdPhysics.CollisionAPI.Apply(stage.GetPrimAtPath("/World/Box/Cube"))
    UsdPhysics.CollisionAPI.Apply(stage.GetPrimAtPath("/World/Box/Mesh"))  # dynamic + triangle mesh
    big = UsdGeom.Mesh.Define(stage, "/World/Terrain")
    big.CreatePointsAttr([(float(index), 0.0, 0.0) for index in range(physics.HUGE_MESH_POINTS + 1)])
    UsdPhysics.CollisionAPI.Apply(big.GetPrim())
    joint = UsdPhysics.FixedJoint.Define(stage, "/World/Weld")
    joint.CreateBody0Rel().SetTargets(["/World/Missing"])
    UsdPhysics.FixedJoint.Define(stage, "/World/Loose")
    warnings = _json(physics.physics_report(stage))["warnings"]
    assert _contains(warnings, "No PhysicsScene")
    assert _contains(warnings, "Rigid body /World/Empty has no collider")
    assert not _contains(warnings, "Rigid body /World/Box has no collider")
    assert _contains(warnings, "Rigid body /World/Box/Cube is nested inside the rigid body /World/Box")
    assert _contains(warnings, "/World/Box/Mesh is a triangle-mesh collider")
    assert _contains(warnings, f"/World/Terrain has {physics.HUGE_MESH_POINTS + 1} points and no collision approximation")
    assert _contains(warnings, "/World/Weld body0 targets /World/Missing, which does not exist")
    assert _contains(warnings, "Joint /World/Loose has neither body0 nor body1")


def test_report_multiple_scenes_warn():
    stage = _stage()
    UsdPhysics.Scene.Define(stage, "/SceneA")
    UsdPhysics.Scene.Define(stage, "/SceneB")
    result = physics.physics_report(stage)
    assert len(result["physics_scenes"]) == 2
    assert _contains(result["warnings"], "2 PhysicsScene prims")


def test_report_subtree_and_instance_proxies():
    stage = _instanced_stage()
    UsdPhysics.RigidBodyAPI.Apply(stage.GetPrimAtPath("/World/Inst"))
    UsdPhysics.CollisionAPI.Apply(stage.GetPrimAtPath("/World/Ground"))
    result = _json(physics.physics_report(stage, "/World/Inst"))
    assert result["root"] == "/World/Inst"
    paths = {entry["path"]: entry for entry in result["prims"]}
    assert set(paths) == {"/World/Inst", "/World/Inst/Part"}
    assert paths["/World/Inst/Part"]["instance_proxy"] is True
    assert not _contains(result["warnings"], "has no collider")  # the prototype's collider counts


def test_report_refuses_missing_prim_and_truncates():
    stage = _stage()
    missing = physics.physics_report(stage, "/World/Nope")
    assert missing["ok"] is False and "does not exist" in missing["error"]
    assert physics.physics_report(stage, "not a path")["ok"] is False
    original = physics.MAX_SCAN
    physics.MAX_SCAN = 2
    try:
        result = physics.physics_report(stage)
    finally:
        physics.MAX_SCAN = original
    assert result["ok"] and result["truncated"] is True and result["scanned"] == 2
    assert _contains(result["warnings"], "Stopped after 2 prims")


# --- add_physics_scene ------------------------------------------------------------------------------------


def test_add_physics_scene_dry_run_writes_nothing_zup_meters():
    stage = _stage()
    before = _snapshot(stage)
    result = _json(physics.add_physics_scene(stage))
    assert result["ok"] and result["dry_run"] and not result["written"]
    assert _snapshot(stage) == before
    assert not stage.GetPrimAtPath("/physicsScene")
    assert result["gravity"]["direction"] == [0.0, 0.0, -1.0]
    assert result["gravity"]["magnitude_stage_units"] == 9.81
    assert [(item["kind"], item.get("name")) for item in result["would_change"]] == [
        ("prim", None),
        ("attribute", "physics:gravityDirection"),
        ("attribute", "physics:gravityMagnitude"),
    ]
    assert result["would_change"][0] == {"kind": "prim", "op": "define", "path": "/physicsScene", "type": "PhysicsScene"}
    assert result["edit_target_layer"] == stage.GetRootLayer().identifier
    assert "next_step" in result
    # Any non-false dry_run value stays a dry run.
    for value in (None, "yes", 1, "true"):
        assert physics.add_physics_scene(stage, dry_run=value)["dry_run"] is True
    assert _snapshot(stage) == before


def test_add_physics_scene_writes_yup_centimeter_gravity():
    stage = _stage("Y", 0.01)
    result = _json(physics.add_physics_scene(stage, dry_run=False))
    assert result["ok"] and result["written"] and not result["dry_run"]
    scene = UsdPhysics.Scene(stage.GetPrimAtPath("/physicsScene"))
    assert scene
    assert tuple(scene.GetGravityDirectionAttr().Get()) == (0.0, -1.0, 0.0)
    assert abs(scene.GetGravityMagnitudeAttr().Get() - 981.0) < 1e-3
    assert result["gravity"]["stage_vector_m_per_s2"] == [0.0, -9.81, 0.0]
    assert [entry["kind"] for entry in result["changed"]] == ["prim", "attribute", "attribute"]
    # Calling again changes nothing.
    again = physics.add_physics_scene(stage)
    assert again["would_change"] == [] and len(again["unchanged"]) == 2
    assert _contains(again["notes"], "already a PhysicsScene")


def test_add_physics_scene_numeric_custom_and_zero_gravity():
    zup = _stage("Z", 0.01)
    moon = physics.add_physics_scene(zup, "/Moon", gravity=1.62)
    assert moon["gravity"]["direction"] == [0.0, 0.0, -1.0]
    assert moon["gravity"]["magnitude_stage_units"] == 162.0
    yup = _stage("Y", 1.0)
    sideways = physics.add_physics_scene(yup, gravity=(3.0, -4.0, 0.0))  # Z-up frame: +X and -Y
    assert sideways["gravity"]["stage_vector_m_per_s2"] == [3.0, 0.0, 4.0]
    assert sideways["gravity"]["direction"] == [0.6, 0.0, 0.8]
    assert sideways["gravity"]["magnitude_stage_units"] == 5.0
    zero = physics.add_physics_scene(yup, gravity=0, dry_run=False)
    assert zero["ok"] and zero["gravity"]["magnitude_stage_units"] == 0.0
    assert UsdPhysics.Scene(yup.GetPrimAtPath("/physicsScene")).GetGravityMagnitudeAttr().Get() == 0.0


def test_add_physics_scene_warns_when_a_gravity_vector_is_not_straight_down():
    # A Y-up user writing [0, -9.81, 0] gets sideways gravity (vectors are read Z-up); that must not be silent.
    yup = _stage("Y", 0.01)
    sideways = physics.add_physics_scene(yup, gravity=(0.0, -9.81, 0.0))
    assert sideways["ok"] and sideways["gravity"]["direction"] == [0.0, 0.0, 1.0]
    assert _contains(sideways["warnings"], "not straight down")
    for stage, gravity in ((yup, (0.0, 0.0, -9.81)), (yup, 9.81), (yup, 0), (_stage("Z"), (0.0, 0.0, -1.62))):
        plain = physics.add_physics_scene(stage, gravity=gravity)
        assert plain["ok"] and not _contains(plain["warnings"], "not straight down"), (gravity, plain["warnings"])


def test_add_physics_scene_updates_existing_scene_and_warns_for_second_scene():
    stage = _stage()
    UsdPhysics.Scene.Define(stage, "/physicsScene").CreateGravityMagnitudeAttr(5.0)
    update = physics.add_physics_scene(stage, dry_run=False)
    assert update["ok"] and [entry["kind"] for entry in update["changed"]] == ["attribute", "attribute"]
    magnitude = [entry for entry in update["changed"] if entry["name"] == "physics:gravityMagnitude"][0]
    assert magnitude["old"] == 5.0 and magnitude["new"] == 9.81
    second = physics.add_physics_scene(stage, "/World/OtherScene")
    assert second["ok"] and _contains(second["warnings"], "already has PhysicsScene prim(s) /physicsScene")


def test_add_physics_scene_refusals():
    stage = _stage()
    before = _snapshot(stage)
    taken = physics.add_physics_scene(stage, "/World/Box", dry_run=False)
    assert not taken["ok"] and "already exists" in taken["error"]
    orphan = physics.add_physics_scene(stage, "/No/Parent/scene", dry_run=False)
    assert not orphan["ok"] and "Parent prim does not exist" in orphan["error"]
    for bad in ("down", (0, 0), (0, 0, float("nan")), -9.81, None):
        refused = physics.add_physics_scene(stage, gravity=bad, dry_run=False)
        assert not refused["ok"], bad
    assert physics.add_physics_scene(stage, "relative/path", dry_run=False)["ok"] is False
    assert _snapshot(stage) == before


def test_read_only_edit_target_is_refused():
    stage = _stage()
    stage.GetRootLayer().SetPermissionToEdit(False)
    try:
        result = physics.add_physics_scene(stage, dry_run=False)
    finally:
        stage.GetRootLayer().SetPermissionToEdit(True)
    assert not result["ok"] and "read-only" in result["error"]


# --- make_rigid_body --------------------------------------------------------------------------------------


def test_make_rigid_body_dry_run_is_byte_identical():
    stage = _stage()
    before = _snapshot(stage)
    result = _json(
        physics.make_rigid_body(stage, "/World/Box", mass=2.0, kinematic=True, approximation="convexHull")
    )
    assert result["ok"] and result["dry_run"] and not result["written"]
    assert _snapshot(stage) == before
    assert ("/World/Box", "PhysicsRigidBodyAPI") in _names(result["would_change"], "api")
    assert ("/World/Box/Mesh", "PhysicsMeshCollisionAPI") in _names(result["would_change"], "api")
    assert ("/World/Box", "physics:kinematicEnabled") in _names(result["would_change"], "attribute")
    assert result["collider_prims"] == ["/World/Box/Mesh", "/World/Box/Cube"]


def test_make_rigid_body_on_group_applies_colliders_to_gprims_zup_meters():
    stage = _stage()
    UsdPhysics.Scene.Define(stage, "/physicsScene")
    result = _json(
        physics.make_rigid_body(
            stage, "/World/Box", mass=2.0, kinematic=True, approximation="convexhull", dry_run=False
        )
    )
    assert result["ok"] and result["written"]
    assert _has(stage, "/World/Box", "PhysicsRigidBodyAPI") and _has(stage, "/World/Box", "PhysicsMassAPI")
    assert not _has(stage, "/World/Box", "PhysicsCollisionAPI")  # colliders only go on geometry
    for path in ("/World/Box/Mesh", "/World/Box/Cube"):
        assert _has(stage, path, "PhysicsCollisionAPI")
    assert _has(stage, "/World/Box/Mesh", "PhysicsMeshCollisionAPI")
    assert not _has(stage, "/World/Box/Cube", "PhysicsMeshCollisionAPI")
    assert stage.GetPrimAtPath("/World/Box/Mesh").GetAttribute("physics:approximation").Get() == "convexHull"
    box = stage.GetPrimAtPath("/World/Box")
    assert box.GetAttribute("physics:kinematicEnabled").Get() is True
    assert box.GetAttribute("physics:mass").Get() == 2.0
    assert _contains(result["notes"], "approximation applies only to meshes")
    assert not _contains(result["warnings"], "PhysicsScene")
    assert not _contains(result["warnings"], "approximation 'none'")
    # Second call: nothing to change.
    again = physics.make_rigid_body(stage, "/World/Box", mass=2.0, kinematic=True, approximation="convexHull")
    assert again["ok"] and again["would_change"] == []
    assert _contains(again["notes"], "already in place")


def test_make_rigid_body_converts_mass_and_density_to_stage_units():
    stage = _stage("Y", 0.01)
    UsdPhysics.SetStageKilogramsPerUnit(stage, 0.001)  # grams
    result = _json(physics.make_rigid_body(stage, "/World/Ground", mass=1.5, density=1000.0, dry_run=False))
    assert result["ok"]
    assert result["mass"] == {
        "mass_kg": 1.5,
        "mass_stage_units": 1500.0,
        "density_kg_per_m3": 1000.0,
        "density_stage_units": 1.0,  # 1000 kg/m^3 = 1 g/cm^3
    }
    ground = stage.GetPrimAtPath("/World/Ground")
    assert abs(ground.GetAttribute("physics:mass").Get() - 1500.0) < 1e-3
    assert abs(ground.GetAttribute("physics:density").Get() - 1.0) < 1e-6
    assert _contains(result["warnings"], "uses mass and ignores density")
    assert _has(stage, "/World/Ground", "PhysicsCollisionAPI")  # a gprim gets its own collider


def test_make_rigid_body_warnings():
    stage = _stage()
    no_collider = physics.make_rigid_body(stage, "/World/Box", add_collider=False, approximation="convexHull")
    assert _contains(no_collider["warnings"], "approximation was ignored")
    assert _contains(no_collider["warnings"], "has no collider on itself or below it")
    assert _contains(no_collider["warnings"], "no PhysicsScene")
    empty = physics.make_rigid_body(stage, "/World/Empty")
    assert empty["ok"] and _contains(empty["warnings"], "no geometry")
    triangle = physics.make_rigid_body(stage, "/World/Box")
    assert _contains(triangle["warnings"], "approximation 'none'")
    kinematic = physics.make_rigid_body(stage, "/World/Box", kinematic=True)
    assert not _contains(kinematic["warnings"], "approximation 'none'")
    physics.make_rigid_body(stage, "/World/Box", dry_run=False)
    inner = physics.make_rigid_body(stage, "/World/Box/Cube")
    assert _contains(inner["warnings"], "is inside the rigid body /World/Box")
    outer = physics.make_rigid_body(stage, "/World")
    assert _contains(outer["warnings"], "would be nested inside this one")


def test_make_rigid_body_turns_kinematic_off():
    stage = _stage()
    physics.make_rigid_body(stage, "/World/Ground", kinematic=True, dry_run=False)
    result = physics.make_rigid_body(stage, "/World/Ground", kinematic=False, dry_run=False)
    change = [entry for entry in result["changed"] if entry["name"] == "physics:kinematicEnabled"][0]
    assert change["old"] is True and change["new"] is False
    assert stage.GetPrimAtPath("/World/Ground").GetAttribute("physics:kinematicEnabled").Get() is False


def test_make_rigid_body_refusals_write_nothing():
    stage = _stage()
    UsdPhysics.Scene.Define(stage, "/physicsScene")
    stage.DefinePrim("/World/Typeless")
    before = _snapshot(stage)
    cases = [
        ("/World/Missing", {}, "does not exist"),
        ("/physicsScene", {}, "not imageable"),
        ("/World/Typeless", {}, "not imageable"),
        ("/World/Box", {"approximation": "sdf"}, "approximation must be one of"),
        ("/World/Box", {"mass": 0}, "mass must be greater than 0"),
        ("/World/Box", {"mass": -1}, "mass must be greater than 0"),
        ("/World/Box", {"density": "heavy"}, "density must be a number"),
        ("/World/Box", {"kinematic": "maybe"}, "kinematic must be true or false"),
        ("/World/Box.size", {}, "property path"),
    ]
    for path, kwargs, text in cases:
        result = physics.make_rigid_body(stage, path, dry_run=False, **kwargs)
        assert not result["ok"] and text in result["error"], (path, kwargs, result.get("error"))
        assert not result["written"]
        _json(result)
    assert _snapshot(stage) == before


def test_instance_proxy_edits_are_refused_and_instances_warn():
    stage = _instanced_stage()
    before = _snapshot(stage)
    proxy = physics.make_rigid_body(stage, "/World/Inst/Part", dry_run=False)
    assert not proxy["ok"] and "instance proxy" in proxy["error"]
    assert not physics.make_collider(stage, "/World/Inst/Part", dry_run=False)["ok"]
    assert not physics.remove_physics(stage, "/World/Inst/Part", dry_run=False)["ok"]
    assert _snapshot(stage) == before
    body = physics.make_rigid_body(stage, "/World/Inst")
    assert body["ok"] and _contains(body["warnings"], "is an instance")
    assert not _contains(body["warnings"], "has no collider")  # the prototype already has one
    collider = physics.make_collider(stage, "/World/Inst")
    assert not collider["ok"] and "no geometry" in collider["error"]
    removal = physics.remove_physics(stage, "/World/Inst")
    assert _contains(removal["warnings"], "instance proxy prim(s) such as /World/Inst/Part")


# --- make_collider ----------------------------------------------------------------------------------------


def test_make_collider_static_mesh_with_approximation():
    stage = _stage("Y", 0.01)
    before = _snapshot(stage)
    dry = _json(physics.make_collider(stage, "/World/Box/Mesh", approximation="convexDecomposition"))
    assert dry["ok"] and _snapshot(stage) == before
    assert dry["rigid_body"] is None and _contains(dry["notes"], "static")
    assert _names(dry["would_change"]) == [
        ("/World/Box/Mesh", "PhysicsCollisionAPI"),
        ("/World/Box/Mesh", "PhysicsMeshCollisionAPI"),
        ("/World/Box/Mesh", "physics:approximation"),
    ]
    result = _json(physics.make_collider(stage, "/World/Box/Mesh", approximation="convexDecomposition", dry_run=False))
    assert result["ok"] and result["written"]
    mesh = stage.GetPrimAtPath("/World/Box/Mesh")
    assert UsdPhysics.MeshCollisionAPI(mesh).GetApproximationAttr().Get() == "convexDecomposition"
    change = physics.make_collider(stage, "/World/Box/Mesh", approximation="boundingCube", dry_run=False)
    assert _names(change["changed"]) == [("/World/Box/Mesh", "physics:approximation")]
    assert change["changed"][0]["old"] == "convexDecomposition"


def test_make_collider_group_and_dynamic_owner_warning():
    stage = _stage()
    UsdPhysics.RigidBodyAPI.Apply(stage.GetPrimAtPath("/World/Box"))
    result = physics.make_collider(stage, "/World/Box", dry_run=False)
    assert result["ok"] and result["rigid_body"] == "/World/Box"
    assert result["collider_prims"] == ["/World/Box/Mesh", "/World/Box/Cube"]
    assert _contains(result["warnings"], "approximation 'none'")
    assert not _has(stage, "/World/Box", "PhysicsCollisionAPI")


def test_make_collider_refusals_and_huge_mesh_warning():
    stage = _stage()
    empty = physics.make_collider(stage, "/World/Empty", dry_run=False)
    assert not empty["ok"] and "no geometry" in empty["error"]
    assert not physics.make_collider(stage, "/World/Box", approximation=5)["ok"]
    big = UsdGeom.Mesh.Define(stage, "/World/Terrain")
    big.CreatePointsAttr([(float(index), 0.0, 0.0) for index in range(physics.HUGE_MESH_POINTS + 1)])
    warned = physics.make_collider(stage, "/World/Terrain")
    assert _contains(warned["warnings"], "no collision approximation")
    quiet = physics.make_collider(stage, "/World/Terrain", approximation="meshSimplification")
    assert not _contains(quiet["warnings"], "no collision approximation")


def test_make_collider_cap_refuses_large_subtrees():
    stage = _stage()
    original = physics.MAX_EDIT_PRIMS
    physics.MAX_EDIT_PRIMS = 1
    try:
        result = physics.make_collider(stage, "/World/Box", dry_run=False)
    finally:
        physics.MAX_EDIT_PRIMS = original
    assert not result["ok"] and "more than 1" in result["error"]
    assert not _has(stage, "/World/Box/Mesh", "PhysicsCollisionAPI")


# --- remove_physics ---------------------------------------------------------------------------------------


def test_remove_physics_round_trip_restores_the_layer_exactly():
    stage = _stage("Y", 0.01)
    original = _snapshot(stage)
    physics.make_rigid_body(
        stage, "/World/Box", mass=4.0, density=500.0, kinematic=True, approximation="convexHull", dry_run=False
    )
    applied = _snapshot(stage)
    assert applied != original
    dry = _json(physics.remove_physics(stage, "/World/Box", include_descendants=True))
    assert dry["ok"] and _snapshot(stage) == applied
    assert {item["method"] for item in dry["would_change"] if item["kind"] == "api"} == {"list_op_edit"}
    assert ("/World/Box", "physics:mass") in _names(dry["would_change"], "attribute")
    assert ("/World/Box/Mesh", "physics:approximation") in _names(dry["would_change"], "attribute")
    result = _json(physics.remove_physics(stage, "/World/Box", include_descendants=True, dry_run=False))
    assert result["ok"] and result["written"]
    assert _snapshot(stage) == original
    assert physics.physics_report(stage)["counts"]["rigid_bodies"] == 0


def test_remove_physics_single_prim_notes_descendants_and_keeps_articulation_root():
    stage = _stage()
    physics.make_rigid_body(stage, "/World/Box", dry_run=False)
    UsdPhysics.ArticulationRootAPI.Apply(stage.GetPrimAtPath("/World/Box"))
    result = physics.remove_physics(stage, "/World/Box", dry_run=False)
    assert result["ok"] and _names(result["changed"]) == [("/World/Box", "PhysicsRigidBodyAPI")]
    assert _contains(result["notes"], "include_descendants=True")
    assert _contains(result["notes"], "keeps its PhysicsArticulationRootAPI")
    assert _has(stage, "/World/Box", "PhysicsArticulationRootAPI")
    assert _has(stage, "/World/Box/Mesh", "PhysicsCollisionAPI")
    nothing = physics.remove_physics(stage, "/World/Ground")
    assert nothing["ok"] and nothing["would_change"] == [] and _contains(nothing["notes"], "Nothing to remove")


def test_remove_physics_from_weaker_sublayer_authors_delete():
    stage = _stage()
    sub = Sdf.Layer.CreateAnonymous("physics_sub.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    stage.SetEditTarget(Usd.EditTarget(sub))
    UsdPhysics.CollisionAPI.Apply(stage.GetPrimAtPath("/World/Ground"))
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    sub_before = sub.ExportToString()
    result = physics.remove_physics(stage, "/World/Ground", dry_run=False)
    assert result["ok"] and result["changed"][0]["method"] == "remove_api"
    assert not _has(stage, "/World/Ground", "PhysicsCollisionAPI")
    assert "delete apiSchemas" in stage.GetRootLayer().ExportToString()
    assert sub.ExportToString() == sub_before  # the weaker layer is untouched
    assert not _contains(result["warnings"], "stronger layer")


def test_remove_physics_warns_when_a_stronger_layer_applies_the_api():
    stage = _stage()
    sub = Sdf.Layer.CreateAnonymous("physics_sub.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    UsdPhysics.CollisionAPI.Apply(stage.GetPrimAtPath("/World/Ground"))  # in the stronger root layer
    stage.SetEditTarget(Usd.EditTarget(sub))
    dry = physics.remove_physics(stage, "/World/Ground")
    assert _contains(dry["warnings"], "is applied in the stronger layer")
    result = physics.remove_physics(stage, "/World/Ground", dry_run=False)
    assert _contains(result["warnings"], "still has PhysicsCollisionAPI")
    assert _has(stage, "/World/Ground", "PhysicsCollisionAPI")


def test_edits_on_sublayer_edit_target_land_in_that_layer():
    stage = _stage()
    sub = Sdf.Layer.CreateAnonymous("physics_sub.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    stage.SetEditTarget(Usd.EditTarget(sub))
    root_before = stage.GetRootLayer().ExportToString()
    result = physics.make_rigid_body(stage, "/World/Ground", mass=1.0, dry_run=False)
    assert result["ok"] and result["edit_target_layer"] == sub.identifier
    assert stage.GetRootLayer().ExportToString() == root_before
    assert "PhysicsRigidBodyAPI" in sub.ExportToString()
