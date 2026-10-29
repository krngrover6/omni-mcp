"""Robot planning uses synthetic files only; no importer or live scene is started."""
import json
import tempfile
from pathlib import Path
from omniverse_scene_mcp import robot_import as ri


def test_urdf_summary_and_import_preview_leave_project_unchanged():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / 'arm.urdf'
        source.write_text('<robot name="arm"><link name="base"/><link name="tip"/>'
                          '<joint name="hinge" type="revolute"><parent link="base"/><child link="tip"/>'
                          '<axis xyz="0 0 1"/><limit lower="-1" upper="1" effort="10" velocity="1"/></joint></robot>')
        before = {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        summary = ri.parse_urdf_summary(str(source))
        assert summary['ok'], summary
        plan = ri.plan_robot_import(str(source), project_dir=folder)
        assert plan['ok'] and plan['dry_run'], plan
        result = ri.import_robot(str(source), project_dir=folder)
        assert result.get('dry_run') is True or result.get('ok') is False, result
        assert before == {str(p): p.read_bytes() for p in root.rglob('*') if p.is_file()}
        json.dumps([summary, plan, result], allow_nan=False)


def test_invalid_import_sources_and_configuration_are_refused():
    for source in ['', 'https://example.invalid/arm.urdf', '../missing.urdf']:
        result = ri.plan_robot_import(source)
        assert not result['ok'], result
    assert ri.importer_info()['ok']


def _robot_asset(path, ops):
    """A tiny robot file whose root prim brings its own xform ops, like Isaac's franka.usd."""
    from pxr import Gf, Usd, UsdGeom
    asset = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(asset, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(asset, 1)
    root = UsdGeom.Xform.Define(asset, '/panda')
    if 'translate' in ops:
        root.AddTranslateOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Vec3f(0, 0, 0))
    if 'orient' in ops:
        root.AddOrientOp().Set(Gf.Quatf(1, 0, 0, 0))
    if 'scale' in ops:
        root.AddScaleOp().Set(Gf.Vec3f(1, 1, 1))
    UsdGeom.Xform.Define(asset, '/panda/link0')
    asset.SetDefaultPrim(root.GetPrim())
    asset.Save()


def test_ready_robot_placement_respects_the_assets_own_xform_ops():
    # Seen live in Isaac 6.1: franka.usd brings [translate, orient, scale]; adding another translate failed.
    _require_pxr()
    from pxr import Gf, Usd, UsdGeom
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        cases = {'full.usda': ['translate', 'orient', 'scale'], 'turned.usda': ['orient', 'scale'], 'bare.usda': []}
        for file_name, ops in cases.items():
            _robot_asset(root / file_name, ops)
        stage = Usd.Stage.CreateNew(str(root / 'scene.usda'))
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        UsdGeom.SetStageMetersPerUnit(stage, 1)
        UsdGeom.Xform.Define(stage, '/World')
        for index, file_name in enumerate(cases):
            target = f'/World/Robot{index}'
            result = ri.add_ready_robot(stage, str(root / file_name), target, position=(1.5, 2, 0), dry_run=False)
            assert result['ok'], result
            prim = stage.GetPrimAtPath(target)
            world = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            assert Gf.IsClose(world.ExtractTranslation(), Gf.Vec3d(1.5, 2, 0), 1e-6), (file_name, world)
            names = [op.GetOpName() for op in UsdGeom.Xformable(prim).GetOrderedXformOps()]
            assert names[0] == 'xformOp:translate' and names.count('xformOp:translate') == 1, (file_name, names)
            assert prim.GetChildren(), file_name


def test_failed_ready_robot_add_leaves_nothing_behind():
    _require_pxr()
    from pxr import Usd, UsdGeom
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        _robot_asset(root / 'arm.usda', ['translate'])
        stage = Usd.Stage.CreateNew(str(root / 'scene.usda'))
        UsdGeom.SetStageMetersPerUnit(stage, 1)
        original = ri._place_referenced
        def broken(xform, pos):
            raise RuntimeError('simulated authoring failure')
        ri._place_referenced = broken
        try:
            result = ri.add_ready_robot(stage, str(root / 'arm.usda'), '/World/Arm', dry_run=False)
        finally:
            ri._place_referenced = original
        assert not result['ok'] and 'nothing was left behind' in result['error'], result
        assert not stage.GetPrimAtPath('/World/Arm') and not stage.GetPrimAtPath('/World'), stage.ExportToString()


def _require_pxr() -> None:
    """Skip (not fail) a test that needs the USD Python bindings when pxr is missing or does not import."""
    try:
        import pxr  # noqa: F401
    except ImportError:
        import unittest

        raise unittest.SkipTest("USD Python bindings (pxr) are not installed")
