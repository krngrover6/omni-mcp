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
    'custom rubric template': 'rubric_template',
    'audit semantic labels': 'semantic_audit',
    'list class transcripts': 'class_transcripts', 'which class transcripts do i have': 'class_transcripts',
    'list payloads': 'scene_list_payloads', 'show payloads': 'scene_list_payloads',
    'which payloads are loaded': 'scene_list_payloads',
}

_SESSION_NUMBER = r'(?:session|class)\s*0*(\d{1,3})'
_TEACH = re.compile(r'(?:teach me|study|plan|lessons? for|split)\s+' + _SESSION_NUMBER + r'(?:\s+in\s+(\d{1,2})\s*min(?:ute)?s?)?', re.I)
_LESSON = re.compile(_SESSION_NUMBER + r'\s+(?:lesson|part|block)\s*(\d{1,2})|(?:lesson|part|block)\s*(\d{1,2})\s+(?:of|from)\s+' + _SESSION_NUMBER, re.I)
_CLAIMS = re.compile(r'(?:check|fact[\s\-]?check)\s+' + _SESSION_NUMBER + r'(?:\s+for\s+(?:mistakes|errors))?', re.I)


_ATTR = r'(/[A-Za-z0-9_/]+)\.([A-Za-z_][A-Za-z0-9_:]*)'
_WHY = re.compile(r'why (?:is|does|has) ' + _ATTR + r'(?:\s.*)?', re.I)
_WHY_OF = re.compile(r'why (?:is|does) (?:the )?([A-Za-z_][A-Za-z0-9_:]*) (?:of|on) (/[A-Za-z0-9_/]+)(?:\s.*)?', re.I)
_WHAT_IF = re.compile(r'what if (?:i )?set ' + _ATTR + r' to (.+?)(?: in (?:the )?([A-Za-z0-9_.\-]+?)(?: layer)?)?', re.I)


def _value_text(text: str):
    """'5' -> 5, '[1, 0, 0]' -> list, 'true' -> True, anything else stays text."""
    try:
        return json.loads(text)
    except ValueError:
        lowered = text.strip().lower()
        if lowered in ('true', 'false'):
            return lowered == 'true'
        return text.strip().strip('"\'')


_HOMEWORK = re.compile(r'(?:check|grade) (?:my )?(?:session|class) ?0*([345]) homework|'
                       r'(?:check|grade) (?:my )?homework (?:for|of) (?:session|class) ?0*([345])', re.I)
_JOINT_KINDS = r'(fixed|revolute|prismatic|spherical|distance)'
_JOINT_PAIR = re.compile(r'(?:create|add|make) an? ' + _JOINT_KINDS + r' joint (?:between|from) (/[A-Za-z0-9_/]+) '
                         r'(?:and|to) (/[A-Za-z0-9_/]+)(?: on (?:the )?([xyz])(?: axis)?)?', re.I)
_JOINT_WORLD = re.compile(r'(?:create|add|make) an? ' + _JOINT_KINDS + r' joint (?:on|for) (/[A-Za-z0-9_/]+)'
                          r'(?: to the world)?(?: on (?:the )?([xyz])(?: axis)?)?', re.I)


_PAYLOAD = re.compile(r'(unload|load) (?:the )?payload (?:at |of |on )?(/[A-Za-z0-9_/]+)', re.I)


def _physics_route(clean: str) -> tuple[str, dict] | None:
    """'check my session 5 homework', 'create a revolute joint between /World/Base and /World/Wheel on the y axis',
    'unload payload /World/Factory' (previews)."""
    if len(clean) > 200:
        return None
    match = _PAYLOAD.fullmatch(clean)
    if match:
        tool = 'scene_unload_payloads' if match[1].lower() == 'unload' else 'scene_load_payloads'
        return tool, {'paths': [match[2]]}
    match = _HOMEWORK.fullmatch(clean)
    if match:
        return 'homework_check', {'session': int(match[1] or match[2])}
    match = _JOINT_PAIR.fullmatch(clean)
    if match:
        arguments = {'kind': match[1].lower(), 'body0': match[2], 'body1': match[3]}
        if match[4]:
            arguments['axis'] = match[4].upper()
        return 'scene_create_joint', arguments
    match = _JOINT_WORLD.fullmatch(clean)
    if match:
        arguments = {'kind': match[1].lower(), 'body1': match[2]}
        if match[3]:
            arguments['axis'] = match[3].upper()
        return 'scene_create_joint', arguments
    return None


def _value_route(clean: str) -> tuple[str, dict] | None:
    """'why is /World/Box.size 2', 'why is the size of /World/Box ...', 'what if I set /World/Box.size to 5 in root'."""
    if len(clean) > 200:
        return None
    match = _WHAT_IF.fullmatch(clean)
    if match:
        arguments = {'prim_path': match[1], 'attribute': match[2], 'value': _value_text(match[3])}
        if match[4]:
            arguments['layer'] = match[4]
        return 'scene_what_if', arguments
    match = _WHY.fullmatch(clean)
    if match:
        return 'scene_why_value', {'prim_path': match[1], 'attribute': match[2]}
    match = _WHY_OF.fullmatch(clean)
    if match:
        return 'scene_why_value', {'prim_path': match[2], 'attribute': match[1]}
    return None


def _session_route(clean: str) -> tuple[str, dict] | None:
    """Teacher-transcript lessons: 'teach me session 3', 'session 3 lesson 2', 'check session 3 for mistakes'."""
    if len(clean) > 120:
        return None
    match = _LESSON.fullmatch(clean)
    if match:
        session, number = (match[1], match[2]) if match[1] else (match[4], match[3])
        return 'class_lesson', {'session': int(session), 'number': int(number)}
    match = _CLAIMS.fullmatch(clean)
    if match:
        return 'class_check_claims', {'session': int(match[1])}
    match = _TEACH.fullmatch(clean)
    if match:
        arguments = {'session': int(match[1])}
        if match[2]:
            arguments['minutes'] = int(match[2])
        return 'class_session_plan', arguments
    return None


def parse(text: str) -> dict | None:
    if not isinstance(text, str):
        return None
    clean = re.sub(r'\s+', ' ', text.strip()).rstrip('?.!')
    tool = _READ.get(clean.lower())
    arguments = {}
    if tool is None:
        routed = _session_route(clean) or _value_route(clean) or _physics_route(clean)
        if routed:
            tool, arguments = routed
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
