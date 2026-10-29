"""Camera sensor dry-run invariants on an isolated in-memory stage."""
import json
try:
    from pxr import Usd, UsdGeom
except ImportError:
    import unittest
    raise unittest.SkipTest('pxr unavailable')
from omniverse_scene_mcp import sensors


def test_camera_sensor_preview_and_explicit_apply():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.Xform.Define(stage, '/World/Link')
    before = [layer.ExportToString() for layer in stage.GetLayerStack()]
    preview = sensors.add_camera_sensor(stage, '/World/Link', name='Camera')
    assert preview['ok'] and preview['dry_run'], preview
    assert before == [layer.ExportToString() for layer in stage.GetLayerStack()]
    done = sensors.add_camera_sensor(stage, '/World/Link', name='Camera', dry_run=False)
    assert done['ok'], done
    assert stage.GetPrimAtPath('/World/Link/Camera').IsA(UsdGeom.Camera)
    report = sensors.list_sensors(stage)
    assert report['ok'], report
    json.dumps(report, allow_nan=False)


def test_sensor_plan_and_invalid_parent_do_not_author():
    stage = Usd.Stage.CreateInMemory()
    before = stage.GetRootLayer().ExportToString()
    assert not sensors.add_camera_sensor(stage, '/Missing')['ok']
    assert stage.GetRootLayer().ExportToString() == before
    catalog = sensors.sensor_catalog()
    assert catalog and sensors.plan_sensor('rgb_camera', '/World/Link')['ok']
    assert not sensors.plan_sensor('not_a_sensor', '/World/Link')['ok']
    assert not sensors.read_sensor_live('/Missing')['ok']


def _fake_kit(stage, created):
    """_kit_module stand-in: omni.usd returns `stage`; IMU/Contact.create define a prim and record their kwargs."""
    class _Creator:
        def __init__(self, kind):
            self.kind = kind

        def create(self, path, **kwargs):
            created.append((self.kind, path, kwargs))
            return stage.DefinePrim(path, 'Xform')

    class _Context:
        def get_stage(self):
            return stage

    class _Usd:
        def get_context(self):
            return _Context()

    class _Physics:
        IMU = _Creator('imu')
        Contact = _Creator('contact')

    modules = {'omni.usd': _Usd(), 'isaacsim.sensors.experimental.physics': _Physics()}
    return lambda name: modules.get(name)


def test_live_imu_and_contact_create_use_the_full_plan():
    # Seen live in Isaac 6.1: these failed with a bare "'values'" because the public plan lacks the internals.
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.Xform.Define(stage, '/World/Link')
    created = []
    original = sensors._kit_module
    sensors._kit_module = _fake_kit(stage, created)
    try:
        preview = sensors.create_sensor_live('imu', '/World/Link', name='Imu', offset=(0, 0, 0.1))
        assert preview['ok'] and preview['dry_run'] and not created, preview
        imu = sensors.create_sensor_live('imu', '/World/Link', name='Imu', offset=(0, 0, 0.1), dry_run=False)
        contact = sensors.create_sensor_live('contact', '/World/Link', name='Contact', dry_run=False)
    finally:
        sensors._kit_module = original
    assert imu['ok'] and imu['prim_path'] == '/World/Link/Imu', imu
    assert contact['ok'] and contact['prim_path'] == '/World/Link/Contact', contact
    assert created[0][0] == 'imu' and created[0][2]['translations'] == [[0.0, 0.0, 0.1]], created
    assert {'min_threshold', 'max_threshold', 'radius'} <= set(created[1][2]), created


def test_camera_hints_explain_a_dark_picture_and_empty_depth():
    # The live Isaac numbers for the unlit physics test cell: mean 0.97, 296499 of 307200 depth pixels inf.
    hints = sensors._camera_hints({'mean': 0.97}, {'non_finite': 296499}, 640 * 480)
    assert len(hints) == 2 and 'no light' in hints[0] and '97%' in hints[1], hints
    assert sensors._camera_hints({'mean': 120.0}, {'non_finite': 10}, 640 * 480) == []
    assert sensors._camera_hints(None, None, 640 * 480) == []


def test_unevenly_scaled_parent_is_warned_about():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    cube = UsdGeom.Cube.Define(stage, '/World/Base')
    cube.AddScaleOp().Set((0.5, 0.3, 0.1))
    UsdGeom.Xform.Define(stage, '/World/Link')
    squashed = sensors.plan_sensor('rgb_camera', '/World/Base', stage=stage, offset=(0, 0, 2))
    assert any('scaled unevenly (0.5, 0.3, 0.1)' in w for w in squashed['warnings']), squashed['warnings']
    plain = sensors.plan_sensor('rgb_camera', '/World/Link', stage=stage)
    assert not any('scaled unevenly' in w for w in plain['warnings']), plain['warnings']
