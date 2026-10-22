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
