"""The lesson generates code, never runs a robot simulation."""
import json
from omniverse_scene_mcp import pick_place


def test_pick_place_scripts_compile_for_both_modes_without_execution():
    for mode in ('standalone', 'script_editor'):
        result = pick_place.generate_script(mode=mode)
        assert result['ok'], result
        compile(result['code'], '<pick-place-preview>', 'exec')
        json.dumps(result, allow_nan=False)
    assert pick_place.lesson_plan()


def test_pick_place_refuses_invalid_geometry_and_missing_stage():
    for values in ({'mode': 'invalid'}, {'cube_size': -1}, {'cube_position': [float('nan'), 0, 0]}):
        assert not pick_place.generate_script(**values)['ok']
    assert not pick_place.readiness_check(None)['ok']


def test_auto_pick_skips_bodies_held_by_a_joint():
    # Seen live in Isaac 6.1: the check picked a door hinged to the world instead of the free ball.
    try:
        from pxr import Usd, UsdGeom, UsdPhysics
    except ImportError:
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    for name in ('Door', 'Ball'):
        prim = UsdGeom.Cube.Define(stage, f'/World/{name}').GetPrim()
        UsdPhysics.RigidBodyAPI.Apply(prim)
        UsdPhysics.CollisionAPI.Apply(prim)
    hinge = UsdPhysics.RevoluteJoint.Define(stage, '/World/Door/Hinge')
    hinge.CreateBody1Rel().SetTargets(['/World/Door'])
    report = pick_place.readiness_check(stage)
    found = next(check for check in report['checks'] if check['id'] == 'object_found')
    assert found['status'] == 'pass' and found['detail']['path'] == '/World/Ball', found
