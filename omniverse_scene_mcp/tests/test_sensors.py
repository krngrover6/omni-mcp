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
