"""Handoff adapters: schemas, missing runtimes, previews and main-thread RPC parity."""
import asyncio
import contextlib
import json
import os
from omniverse_scene_mcp import features, nl_tier8, profiles


def test_allowlist_functions_have_valid_json_signatures_and_mutators_default_to_preview():
    for name, spec in features.FEATURES.items():
        assert callable(features.function(name))
        sig = features.signature(name)
        assert not {'stage', 'runner', 'env', 'undo_stack'} & set(sig.parameters)
        if spec.mutates:
            assert sig.parameters['dry_run'].default is True, name


def test_natural_language_never_applies_mutations_even_with_false_in_json():
    result = nl_tier8.parse('animation_turntable {"prim_path":"/World/A","dry_run":false}')
    assert result['arguments']['dry_run'] is True
    assert nl_tier8.parse('preview camera sensor on /World/Robot')['tool'] == 'sensor_add_camera'
    assert nl_tier8.parse('move /World/Box 1 2 3') is None
    assert nl_tier8.parse('What is a USD prim?') is None


def test_feature_rpc_rejects_unknown_operations_internal_args_and_router_local_operations():
    assert not features.rpc_call(object(), {'tool': 'os.system', 'arguments': {}})['ok']
    assert not features.rpc_call(object(), {'tool': 'ros2_environment', 'arguments': {}})['ok']
    assert not features.invoke('sensor_add_camera', {'parent_path': '/World', 'stage': 'injection'})['ok']
    assert not features.invoke('graph_apply', {'name': 'x', 'options': {'dry_run': False}})['ok']
    assert not features.invoke('robot_live_move', {'robot_root': '/World', 'targets': {}})['ok']


@contextlib.contextmanager
def _profile(value):
    before = os.environ.get(profiles.PROFILE_ENV)
    try:
        if value is None:
            os.environ.pop(profiles.PROFILE_ENV, None)
        else:
            os.environ[profiles.PROFILE_ENV] = value
        yield
    finally:
        if before is None:
            os.environ.pop(profiles.PROFILE_ENV, None)
        else:
            os.environ[profiles.PROFILE_ENV] = before


def test_small_default_discovers_and_calls_hidden_tools_with_validation():
    from omniverse_scene_mcp.server import build_server
    with _profile(None):
        app = build_server()
    names = [t.name for t in asyncio.run(app.list_tools())]
    assert 18 <= len(names) <= 25
    assert 'router_call_tool' in names and 'sensor_catalog' not in names
    meta = app._tool_manager.get_tool('router_call_tool')
    result = asyncio.run(meta.run({'name': 'sensor_catalog'}))
    assert isinstance(result, dict)
    listed = app._tool_manager.get_tool('router_list_tools')
    hidden = asyncio.run(listed.run({'pattern': 'sensor_*'}))
    assert any(t['name'] == 'sensor_add_camera' for t in hidden['tools'])
    schema = asyncio.run(listed.run({'pattern': 'sensor_add_camera', 'include_schema': True}))
    assert 'parent_path' in schema['tools'][0]['inputSchema']['required']
    assert not asyncio.run(meta.run({'name': 'router_call_tool'}))['ok']
    assert not asyncio.run(meta.run({'name': 'sensor_add_camera', 'arguments': {}}))['ok']


def test_workflows_are_a_normal_always_on_tool_for_clients_without_prompts():
    import tomllib
    from pathlib import Path
    from omniverse_scene_mcp.server import build_server
    with _profile(None):
        app = build_server()
    names = [t.name for t in asyncio.run(app.list_tools())]
    assert 'router_workflow' in names and len(names) <= 25
    tool = app._tool_manager.get_tool('router_workflow')
    listed = asyncio.run(tool.run({}))
    assert {'why_value', 'study_session'} <= {w['name'] for w in listed['workflows']}
    steps = asyncio.run(tool.run({'name': 'why_value', 'params': {'prim_path': '/World/Box', 'attribute': 'size'}}))
    assert steps['ok'] and "scene_why_value(prim_path='/World/Box', attribute='size')" in steps['steps']
    assert not asyncio.run(tool.run({'name': 'no_such_workflow'}))['ok']
    assert not asyncio.run(tool.run({'name': 'why_value', 'params': {'bogus': 1}}))['ok']
    # the new study tools are reachable from the starter profile through the meta tools
    hidden = asyncio.run(app._tool_manager.get_tool('router_list_tools').run({'pattern': 'scene_wh*'}))
    assert {'scene_why_value', 'scene_what_if'} <= {t['name'] for t in hidden['tools']}
    example = Path(__file__).resolve().parents[1] / 'docs' / 'client-configs' / 'codex-config.toml.example'
    enabled = tomllib.loads(example.read_text(encoding='utf-8'))['mcp_servers']['omniverse_router']['enabled_tools']
    for name in ('router_list_tools', 'router_call_tool', 'router_workflow'):
        assert name in enabled, name


def test_internal_feature_name_cannot_be_overridden_by_a_routed_argument():
    from unittest.mock import patch
    from omniverse_scene_mcp.server import build_server
    from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
    from omniverse_scene_mcp.router import OmniverseRouter
    from omniverse_scene_mcp.service import SceneMCPService
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    with _profile('all'), patch('omniverse_scene_mcp.server.build_router', return_value=router):
        build_server()
    result = asyncio.run(router.handle('sensor_catalog {"_name":"robot_save_pose","dry_run":false}'))
    assert result.payload['ok'] is False
    assert not features.rpc_call(object(), {'tool': [], 'arguments': {}})['ok']


def test_windows_socketpair_retry_keeps_connect_guard_active():
    import conftest
    from unittest.mock import patch
    attempts = []
    def fake_pair():
        attempts.append(1)
        if len(attempts) == 1:
            conftest.BLOCKED_ATTEMPTS.append('test pair listener')
            raise conftest.NetworkBlockedError('protected ephemeral port')
        return ('server', 'client')
    before = list(conftest.BLOCKED_ATTEMPTS)
    with patch.dict(conftest._ORIGINALS, {'socketpair': fake_pair}):
        assert conftest._guarded_socketpair() == ('server', 'client')
    assert len(attempts) == 2 and conftest.BLOCKED_ATTEMPTS == before
    assert conftest.blocked_reason(('127.0.0.1', 9911))


def test_all_profile_registers_every_handoff_tool_and_workflow_prompts():
    from omniverse_scene_mcp.server import build_server
    with _profile('all'):
        app = build_server()
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert set(features.FEATURES) <= names
    assert asyncio.run(app.list_prompts())
    for name, spec in features.FEATURES.items():
        if spec.mutates:
            assert app._tool_manager.get_tool(name).parameters['properties']['dry_run']['default'] is True


def test_legacy_edit_tools_also_preview_by_default():
    from omniverse_scene_mcp.server import build_server
    with _profile('all'):
        app = build_server()
    for name in ('scene_create', 'scene_delete', 'scene_duplicate', 'scene_move', 'scene_save', 'router_dispatch'):
        assert app._tool_manager.get_tool(name).parameters['properties']['dry_run']['default'] is True
    result = asyncio.run(app._tool_manager.get_tool('scene_create').run({'path': '/World/DryBox'}))
    assert result['dry_run'] is True
    for flag in (None, 0, '', 'false', [], {}):
        result = asyncio.run(app._tool_manager.get_tool('scene_create').run({
            'path': '/World/DryBox', 'dry_run': flag}))
        assert result['dry_run'] is True, flag


def test_feature_routes_preserve_existing_phrase_routing():
    import ast
    from pathlib import Path
    from unittest.mock import patch
    from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
    from omniverse_scene_mcp.router import OmniverseRouter
    from omniverse_scene_mcp.service import SceneMCPService
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    phrases = set()
    for path in Path(__file__).parent.glob('test_*.py'):
        if path.name == 'test_features.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        phrases.update(n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                       and isinstance(n.value, str) and '\n' not in n.value and 3 < len(n.value) < 180)
    checked = 0
    for phrase in sorted(phrases):
        if nl_tier8.parse(phrase) is not None:
            continue  # newly supported phrases deliberately get a more precise route
        current = router.route(phrase)
        with patch.object(nl_tier8, 'parse', return_value=None):
            assert router.route(phrase) == current, phrase
        checked += 1
    assert checked > 1000


def test_sensor_preview_is_identical_through_adapter_and_rpc_even_for_malformed_flags():
    try:
        from pxr import Usd, UsdGeom
    except ImportError:
        import unittest
        raise unittest.SkipTest('pxr unavailable')
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, '/World/Link')
    class Bridge:
        def _require_stage(self):
            return stage
    before = [layer.ExportToString() for layer in stage.GetLayerStack()]
    for flag in (None, True, 0, '', 'false', [], {}):
        args = {'parent_path': '/World/Link', 'dry_run': flag}
        direct = features.invoke('sensor_add_camera', args, stage=stage)
        result = features.rpc_call(Bridge(), {'tool': 'sensor_add_camera', 'arguments': args})
        assert direct['ok'] and result['ok'], (flag, direct, result)
        assert result['dry_run'] is True
        assert before == [layer.ExportToString() for layer in stage.GetLayerStack()]
    result = features.rpc_call(Bridge(), {'tool': 'sensor_add_camera', 'arguments': {
        'parent_path': '/World/Link', 'name': 'Camera', 'dry_run': False}})
    assert result['ok'] and stage.GetPrimAtPath('/World/Link/Camera')
    json.dumps(result, allow_nan=False)


def test_feature_tool_reaches_http_rpc_and_journal_without_a_live_app():
    try:
        from pxr import Usd, UsdGeom
    except ImportError:
        import unittest
        raise unittest.SkipTest('pxr unavailable')
    from omniverse_scene_mcp.http_bridge import HttpSceneBridge
    from omniverse_scene_mcp.service import SceneMCPService
    from test_round5_integration import _live
    from test_twelve_gaps_integration import _server, ISAAC, _layers_text
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, '/World/Link')
    before = _layers_text(stage)
    with _profile('all'), _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call:
        with _live(stage) as (_, _, sent):
            preview = call('sensor_add_camera', parent_path='/World/Link', name='Camera')
            assert preview['ok'] and preview['dry_run'] and _layers_text(stage) == before
            applied = call('sensor_add_camera', parent_path='/World/Link', name='Camera', dry_run=False)
            assert applied['ok'] and stage.GetPrimAtPath('/World/Link/Camera')
            listed = call('sensor_list')
            assert listed['ok']
            assert any(c['method'] == 'feature_call' for c in sent)
            history = call('scene_journal')
            assert history['ok'] and 'sensor_add_camera' in json.dumps(history)
