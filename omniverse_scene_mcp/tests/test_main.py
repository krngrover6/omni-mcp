import contextlib
import io
import json
import sys

from omniverse_scene_mcp import __main__ as cli
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter
from omniverse_scene_mcp.service import SceneMCPService


def test_cli_prints_routing_decision_and_payload():
    previous_build, previous_argv = cli.build_router, sys.argv
    cli.build_router = lambda: OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    sys.argv = ["omniverse_scene_mcp", "teach", "me", "schemas"]
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            assert cli.main() == 0
    finally:
        cli.build_router, sys.argv = previous_build, previous_argv

    printed = json.loads(output.getvalue())
    assert printed["decision"]["target"] == "learn"
    assert printed["payload"]["topic"] == "schemas"
