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
