"""Conservative handoff routes. Sentences only preview edits; tool calls apply them explicitly."""
from __future__ import annotations
import json
import re
from .features import FEATURES

_READ = {
    'inspect action graphs': 'graph_inspect', 'list action graphs': 'graph_inspect',
    'list graph recipes': 'graph_recipes', 'list variant sets': 'variant_list',
    'check variants': 'variant_check', 'check scene performance': 'scene_performance',
    'performance report': 'scene_performance', 'show animation timeline': 'animation_info',
    'list ready robots': 'robot_catalog', 'robot importer info': 'robot_importer_info',
    'sensor catalog': 'sensor_catalog', 'list sensors': 'sensor_list',
    'pick and place lesson': 'robot_pick_place_lesson',
    'generate pick and place script': 'robot_pick_place_script',
    'check pick and place readiness': 'robot_pick_place_check',
    'ros2 environment check': 'ros2_environment', 'ros2 setup plan': 'ros2_setup_plan',
    'inspect ros2 graphs': 'ros2_graph_check', 'supported import formats': 'asset_import_formats',
    'show live frame stats': 'scene_frame_stats',
}


def parse(text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    clean = re.sub(r'\s+', ' ', text.strip()).rstrip('?.!')
    tool = _READ.get(clean.lower())
    arguments = {}
    if tool is None:
        match = re.fullmatch(r'(?:inspect joints|joint table)\s+(/[^\s]+)', clean, re.I)
        if match:
            tool, arguments = 'robot_joint_table', {'robot_root': match[1]}
        match = re.fullmatch(r'preview turntable\s+(/[^\s]+)', clean, re.I)
        if match:
            tool, arguments = 'animation_turntable', {'prim_path': match[1]}
        match = re.fullmatch(r'preview camera sensor on\s+(/[^\s]+)', clean, re.I)
        if match:
            tool, arguments = 'sensor_add_camera', {'parent_path': match[1]}
        match = re.fullmatch(r'plan robot import\s+(.+)', clean, re.I)
        if match:
            tool, arguments = 'robot_import_plan', {'source': match[1].strip('"')}
        # An exact allowlisted tool name + JSON also works from the natural-language front door.
        match = re.fullmatch(r'([a-z][a-z0-9_]+)\s+(\{.*\})', text.strip(), re.S)
        if match and match[1] in FEATURES:
            try:
                arguments = json.loads(match[2])
            except ValueError:
                return None
            tool = match[1]
    if tool is None:
        return None
    if FEATURES[tool].mutates:
        arguments['dry_run'] = True
    return {'tool': tool, 'arguments': arguments, 'scene': FEATURES[tool].location != 'local'}
