import os

# The stdlib test runner has no conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import contextlib
import io
import json
import socket
import sys
import urllib.request

from omniverse_scene_mcp import __main__ as cli
from omniverse_scene_mcp import runtime
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter
from omniverse_scene_mcp.service import SceneMCPService


def _run_cli(argv, router=None):
    """Run the CLI with argv; returns (exit_code, printed_json, router_used)."""
    router = router or OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    previous_build, previous_argv = cli.build_router, sys.argv
    cli.build_router = lambda: router
    sys.argv = ["omniverse_scene_mcp", *argv]
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            code = cli.main()
    finally:
        cli.build_router, sys.argv = previous_build, previous_argv
    return code, json.loads(output.getvalue()), router


def test_cli_prints_routing_decision_and_payload():
    code, printed, _ = _run_cli(["teach", "me", "schemas"])
    assert code == 0
    assert printed["decision"]["target"] == "learn"
    assert printed["payload"]["topic"] == "schemas"


def test_cli_previews_scene_edits_by_default():
    code, printed, router = _run_cli(["delete", "/World/Props/Cube"])
    assert code == 0
    assert printed["decision"]["target"] == "scene"
    assert printed["dry_run"] is True
    assert printed["payload"]["dry_run"] is True
    assert "--apply" in printed["note"]
    assert router._scene._bridge.get_prim("/World/Props/Cube") is not None


def test_cli_apply_flag_runs_the_edit_and_is_removed_from_the_request():
    code, printed, router = _run_cli(["create", "/World/Lamp", "--apply"])
    assert code == 0
    assert printed["decision"]["target"] == "scene"
    assert printed["dry_run"] is False
    assert printed["payload"]["dry_run"] is False
    assert "note" not in printed
    assert "--apply" not in printed["decision"]["normalized_request"]
    assert router._scene._bridge.get_prim("/World/Lamp") is not None

    _, leading, router = _run_cli(["--apply", "delete", "/World/Props/Cube"])
    assert leading["payload"]["dry_run"] is False
    assert router._scene._bridge.get_prim("/World/Props/Cube") is None


def test_cli_without_request_prints_scene_summary():
    code, printed, _ = _run_cli(["--apply"])
    assert code == 0
    assert printed["stage_name"] == "FakeStage"


def test_cli_real_runtime_stays_offline_under_test_switch():
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("network access attempted")

    patched = [(socket, "create_connection"), (urllib.request, "urlopen"), (runtime, "urlopen")]
    originals = [(owner, name, getattr(owner, name)) for owner, name in patched]
    previous_argv = sys.argv
    sys.argv = ["omniverse_scene_mcp", "create", "/World/Lamp"]
    output = io.StringIO()
    try:
        for owner, name in patched:
            setattr(owner, name, refuse)
        with contextlib.redirect_stdout(output):
            assert cli.main() == 0
    finally:
        sys.argv = previous_argv
        for owner, name, original in originals:
            setattr(owner, name, original)
    assert attempts == []
    printed = json.loads(output.getvalue())
    assert printed["dry_run"] is True and printed["payload"]["dry_run"] is True
