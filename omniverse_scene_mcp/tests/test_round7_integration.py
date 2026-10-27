"""Round 7 integration: timed drills, today's brief, the reminder text, the change journal, smart layout, the robot
inspector and the Docker docs helper wired through every layer (server tool -> router -> service -> HTTP client ->
extended RPC tier6-1 -> Kit bridge), plus the plain-English phrases in nl_tier6.py.

Offline: a test-mode server over a temp workspace, the in-memory fake scene or an in-process HTTP transport into the
extended RPC over a temporary pxr stage (only Kit's omni modules are faked). Every socket is refused and the real
exam-prep folder is checked after every server (test_twelve_gaps_integration's _guard). The journal is memory-only
(test switch) and Docker is never run (docker_status gets a fake 'which' with no docker on it). Plain fixture-free
test_* functions, so the stdlib runner runs them.
"""

import os

# The stdlib test runner may not load conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import asyncio
import contextlib
import functools
import json
import re
import tempfile
import time
from pathlib import Path

from omniverse_scene_mcp import docker_docs, journal, kit_rpc, nl_tier5, nl_tier6
from omniverse_scene_mcp import help as tool_guide
from omniverse_scene_mcp import router as router_module
from omniverse_scene_mcp.bridge import LAYOUT_RPC_METHODS, LIVE_RPC_METHODS, ROBOT_RPC_METHODS, RPC_VERSION
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.journal import JOURNALED_TOOLS, ChangeJournal
from omniverse_scene_mcp.kit_bridge import LiveKitSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter, RouteTarget
from omniverse_scene_mcp.service import SceneMCPService
from test_round5_integration import _live, _live_stage
from test_twelve_gaps_integration import COMPOSER, ISAAC, _BasicServer, _env, _in_process_transport, _layers_text, _server

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover - the pxr tests below then return early
    Gf = Sdf = Usd = UsdGeom = UsdPhysics = None

TESTS = Path(__file__).resolve().parent
WORKSPACE = TESTS.parents[1]
LAYOUT_TOOLS = ("scene_layout_snap", "scene_layout_stack", "scene_layout_distribute", "scene_layout_space",
                "scene_layout_align")
READ_ONLY_TOOLS = ("learning_drill_focuses", "learning_drill_status", "learning_today", "learning_reminder_setup",
                   "scene_journal", "scene_journal_explain", "scene_journal_summary", "robot_inspect",
                   "docs_docker_status", "docs_docker_setup_plan", "docs_docker_explain")
NEW_TOOLS = {*LAYOUT_TOOLS, *READ_ONLY_TOOLS, "learning_drill_start", "learning_drill_answer", "scene_journal_clear"}
PHRASES = {
    "start a drill on weak areas": ("learning_drill_start", RouteTarget.LEARN),
    "drill me on composition": ("learning_drill_start", RouteTarget.LEARN),
    "quick flash quiz": ("learning_drill_start", RouteTarget.LEARN),
    "what drills can I do": ("learning_drill_focuses", RouteTarget.LEARN),
    "what should I do today": ("learning_today", RouteTarget.LEARN),
    "set up a daily reminder at 7pm": ("learning_reminder_setup", RouteTarget.LEARN),
    "what did you change today": ("scene_journal_summary", RouteTarget.SCENE),
    "show the change journal": ("scene_journal", RouteTarget.SCENE),
    "stack these 10 cubes 4 3 2 1": ("scene_layout_stack", RouteTarget.SCENE),
    "snap /World/Box_01 to the floor": ("scene_layout_snap", RouteTarget.SCENE),
    "put boxes evenly on /World/Warehouse/Shelves/UpperShelf": ("scene_layout_distribute", RouteTarget.SCENE),
    "space /World/A /World/B /World/C evenly along x": ("scene_layout_space", RouteTarget.SCENE),
    "align /World/A /World/B on x": ("scene_layout_align", RouteTarget.SCENE),
    "inspect my robot": ("robot_inspect", RouteTarget.SCENE),
    "inspect /World/Franka": ("robot_inspect", RouteTarget.SCENE),
    "is docker worth it": ("docs_docker_explain", RouteTarget.LEARN),
    "docker status": ("docs_docker_status", RouteTarget.LEARN),
    "how do I set up docker": ("docs_docker_setup_plan", RouteTarget.LEARN),
}
# Phrases that must keep their old routes (and never parse as a round-7 request).
KEPT = (
    "move /World/Props/Cube by 0 0 1", "move /World/Cube to 1 2 3", "inspect /World/Props/Cube", "inspect /World",
    "what did you change", "undo", "confirm undo", "quiz me on prims", "drill me on prims", "teach me schemas",
    "practice transforms with me", "start day 1", "next lesson", "mock exam", "show my dashboard", "help", "status",
    "add a franka robot", "find a carter robot", "add an arm robot", "make /World/Props/Cube a rigid body",
    "put /World/A on /World/B", "move /World/Box to /World/Shelf", "put /World/Cube under /World/Props",
    "remind me what a payload is", "create a reminder to buy milk", "what is a payload in USD?",
    "check my homework", "explain my scene", "what changed in my scene", "align the camera to /World/Box",
)


# --------------------------------------------------------------------------- helpers


@contextlib.contextmanager
def _no_docker():
    """docker_docs.docker_status with no docker CLI anywhere: nothing is ever run, no socket is opened."""
    original = docker_docs.docker_status

    def refuse(*_args, **_kwargs):
        raise AssertionError("no command or connection may run in the tests")

    docker_docs.docker_status = functools.partial(original, which=lambda _name: None, candidates=[], runner=refuse,
                                                  probe=refuse)
    try:
        yield
    finally:
        docker_docs.docker_status = original


def _layout_stage(tmp: str):
    """The live test stage plus a thin floor (top at z = 0) and three 1-unit boxes floating at z = 3."""
    stage = _live_stage(tmp)
    floor = UsdGeom.Cube.Define(stage, "/World/Floor")
    floor.CreateSizeAttr(1.0)
    floor.AddTranslateOp().Set(Gf.Vec3d(0, 0, -0.05))
    floor.AddScaleOp().Set(Gf.Vec3f(20, 20, 0.1))
    for index, x in enumerate((-3.0, 0.5, 4.0), start=1):
        box = UsdGeom.Cube.Define(stage, f"/World/B{index}")
        box.CreateSizeAttr(1.0)
        box.AddTranslateOp().Set(Gf.Vec3d(x, 0, 3.0))
    stage.GetRootLayer().Save()
    return stage


def _z(stage, path):
    return float(stage.GetPrimAtPath(path).GetAttribute("xformOp:translate").Get()[2])


def _arm_stage():
    """A one-hinge arm on a fixed base (an articulation), with a floor, in memory."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.Xform.Define(stage, "/World")
    arm = UsdGeom.Xform.Define(stage, "/World/Franka")
    UsdPhysics.ArticulationRootAPI.Apply(arm.GetPrim())
    for name, z in (("base", 0.0), ("link1", 0.3)):
        body = UsdGeom.Xform.Define(stage, f"/World/Franka/{name}")
        body.AddTranslateOp().Set(Gf.Vec3d(0, 0, z))
        UsdPhysics.RigidBodyAPI.Apply(body.GetPrim())
        UsdPhysics.MassAPI.Apply(body.GetPrim()).CreateMassAttr(2.0)
        cube = UsdGeom.Cube.Define(stage, f"/World/Franka/{name}/geom")
        cube.CreateSizeAttr(0.2)
        UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    root = UsdPhysics.FixedJoint.Define(stage, "/World/Franka/root_joint")
    root.CreateBody1Rel().SetTargets([Sdf.Path("/World/Franka/base")])
    hinge = UsdPhysics.RevoluteJoint.Define(stage, "/World/Franka/shoulder")
    hinge.CreateBody0Rel().SetTargets([Sdf.Path("/World/Franka/base")])
    hinge.CreateBody1Rel().SetTargets([Sdf.Path("/World/Franka/link1")])
    hinge.CreateAxisAttr("Z")
    hinge.CreateLowerLimitAttr(-90.0)
    hinge.CreateUpperLimitAttr(90.0)
    return stage


def _test_phrases():
    """Every sentence the existing tests send through route()/dispatch()/handle() (string literals only)."""
    pattern = re.compile(r"""(?:\.route|\.dispatch|\.handle|_route)\(\s*(?P<q>["'])(?P<text>[^"'\\\n]{1,200})(?P=q)""")
    found = set()
    for path in TESTS.glob("test_*.py"):
        if path.name in ("test_round7_integration.py", "test_nl_tier6.py"):
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            found.add(match.group("text"))
    for group, phrases in tool_guide.GROUP_PHRASES.items():
        if group not in ("Layout", "Robots", "Docs (Docker)", "Practice & planning"):
            found.update(phrase for phrase in phrases if phrase not in ("what did you change today", "show the change journal"))
    found.update(item["say"] for item in tool_guide.quick_start())
    return sorted(found)


# --------------------------------------------------------------------------- registration and wiring


def test_round7_tools_are_registered_with_safe_defaults_and_catalogued():
    with _server() as call:
        tools = {tool.name: tool for tool in call.app._tool_manager.list_tools()}
        assert NEW_TOOLS <= set(tools) and len(tools) == 160 + len(__import__("omniverse_scene_mcp.features", fromlist=["FEATURES"]).FEATURES), len(tools)
        for name in LAYOUT_TOOLS:
            properties = tools[name].parameters["properties"]
            assert properties["dry_run"]["default"] is True, name
            description = " ".join(tools[name].description.split())
            assert description.startswith("DRY RUN BY DEFAULT") and "changes the user's open scene" in description, name
            assert "preview to the user first" in description, name
        for name in READ_ONLY_TOOLS:
            assert " ".join(tools[name].description.split()).startswith("Read-only"), name
        assert "creates NOTHING" in tools["learning_reminder_setup"].description
        assert tools["scene_journal_clear"].parameters["properties"]["confirm"]["default"] is False
        assert tools["learning_drill_start"].parameters["properties"]["focus"]["default"] == "weak"
        assert tools["learning_drill_start"].parameters["properties"]["count"]["default"] == 10
        assert tools["learning_drill_start"].parameters["properties"]["seconds_per_question"]["default"] == 45
        assert tools["robot_inspect"].parameters["properties"]["max_prims"]["default"] == 5000
        # Journaled tools keep their parameters (the journal wraps them with functools.wraps).
        assert set(tools["scene_move"].parameters["properties"]) >= {"path", "translation", "dry_run", "show_python"}
    catalog = {entry["name"]: (group, entry) for group, entries in tool_guide.TOOL_CATALOG.items() for entry in entries}
    assert NEW_TOOLS <= set(catalog)
    for name in LAYOUT_TOOLS:
        group, entry = catalog[name]
        assert group == "Layout" and entry["mutates"] is True and "dry_run=True" in entry["example"], name
    for name in READ_ONLY_TOOLS:
        assert catalog[name][1]["mutates"] is False, name
    for name in ("learning_drill_start", "learning_drill_answer", "scene_journal_clear"):
        assert catalog[name][1]["mutates"] is True, name
    assert catalog["robot_inspect"][0] == "Robots" and catalog["docs_docker_status"][0] == "Docs (Docker)"
    assert catalog["learning_today"][0] == "Practice & planning" and catalog["scene_journal"][0] == "Undo"
    for topic, group in (("layout", "Layout"), ("docker", "Docs (Docker)"), ("journal", "Undo"),
                         ("reminder", "Practice & planning"), ("joints", "Robots")):
        assert tool_guide.help(topic)["group"] == group, topic
    # The help phrases of the new groups route where their group says.
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for group, target in (("Layout", RouteTarget.SCENE), ("Robots", RouteTarget.SCENE),
                          ("Practice & planning", RouteTarget.LEARN), ("Docs (Docker)", RouteTarget.LEARN)):
        for phrase in tool_guide.GROUP_PHRASES[group]:
            assert router.route(phrase).target is target, (group, phrase)


def test_tier6_rpc_methods_line_up_across_the_protocol_the_rpc_and_every_bridge():
    assert RPC_VERSION == "tier8-1"
    assert set(LAYOUT_RPC_METHODS) == {"layout_snap", "layout_stack", "layout_distribute", "layout_space", "layout_align"}
    assert ROBOT_RPC_METHODS == ("robot_inspect",)
    assert set(LAYOUT_RPC_METHODS) | set(ROBOT_RPC_METHODS) <= set(LIVE_RPC_METHODS) <= set(kit_rpc._HANDLERS)
    for owner in (FakeSceneBridge, HttpSceneBridge, LiveKitSceneBridge, SceneMCPService):
        missing = [name for name in (*LAYOUT_RPC_METHODS, *ROBOT_RPC_METHODS) if not callable(getattr(owner, name, None))]
        assert not missing, (owner.__name__, missing)
    for name in LAYOUT_RPC_METHODS:  # writes: dropped when stale, never time-budgeted
        assert name in kit_rpc._WRITE_METHODS and name not in kit_rpc._BUDGETED_METHODS, name
    assert "robot_inspect" in kit_rpc._BUDGETED_METHODS and "robot_inspect" not in kit_rpc._WRITE_METHODS
    assert {f"scene_layout_{op}" for op in ("snap", "stack", "distribute", "space", "align")} <= JOURNALED_TOOLS
    assert not ({"scene_journal_clear", "scene_journal", "robot_inspect"} & JOURNALED_TOOLS)


def test_extended_rpc_parses_layout_and_robot_params_strictly():
    calls = []

    class Recorder:
        def layout_snap(self, paths, *, surface, dry_run):
            calls.append(("snap", paths, surface, dry_run))
            return {"ok": True, "dry_run": dry_run}

        def layout_stack(self, paths, *, rows, anchor, gap, facing, dry_run):
            calls.append(("stack", paths, rows, anchor, gap, facing, dry_run))
            return {"ok": True}

        def layout_distribute(self, paths, surface_path, *, margin, dry_run):
            calls.append(("distribute", paths, surface_path, margin, dry_run))
            return {"ok": True}

        def layout_space(self, paths, *, axis, spacing, dry_run):
            calls.append(("space", paths, axis, spacing, dry_run))
            return {"ok": True}

        def layout_align(self, paths, *, axis, mode, dry_run):
            calls.append(("align", paths, axis, mode, dry_run))
            return {"ok": True}

        def robot_inspect(self, root_path, *, max_prims):
            calls.append(("robot", root_path, max_prims))
            return {"ok": True, "truncated_by_time": False, "robots": []}

    server = kit_rpc.make_extended_server_class(_BasicServer)(Recorder())
    dispatch = server._dispatch
    dispatch({"method": "layout_snap", "params": {"paths": ["/World/A"]}})
    assert calls[-1] == ("snap", ["/World/A"], None, True)  # a missing dry_run is a dry run
    odd = dispatch({"method": "layout_snap", "params": {"paths": ["/World/A"], "surface": 0.5, "dry_run": "maybe"}})
    assert calls[-1] == ("snap", ["/World/A"], 0.5, True) and "dry_run='maybe'" in odd["result"]["warnings"][0]
    dispatch({"method": "layout_stack", "params": {"paths": ["/A", "/B", "/C"], "rows": [2, 1], "anchor": [0, 0, 1],
                                                    "dry_run": False}})
    assert calls[-1] == ("stack", ["/A", "/B", "/C"], [2, 1], (0.0, 0.0, 1.0), 0.0, None, False)
    dispatch({"method": "layout_distribute", "params": {"paths": ["/A"], "surface_path": "/Shelf", "margin": 0.1}})
    assert calls[-1] == ("distribute", ["/A"], "/Shelf", 0.1, True)
    dispatch({"method": "layout_space", "params": {"paths": ["/A", "/B"], "axis": "y", "spacing": 0.2}})
    assert calls[-1] == ("space", ["/A", "/B"], "y", 0.2, True)
    dispatch({"method": "layout_align", "params": {"paths": ["/A", "/B"]}})
    assert calls[-1] == ("align", ["/A", "/B"], "x", "min", True)
    dispatch({"method": "robot_inspect", "params": {"root_path": "/World/Franka", "budget_ms": 500}})
    assert calls[-1] == ("robot", "/World/Franka", 5000)
    for bad in (
        {"method": "layout_snap", "params": {}},
        {"method": "layout_snap", "params": {"paths": "/World/A"}},
        {"method": "layout_snap", "params": {"paths": ["World/A"]}},
        {"method": "layout_snap", "params": {"paths": ["/A"] * 501}},
        {"method": "layout_snap", "params": {"paths": ["/A"], "surface": True}},
        {"method": "layout_snap", "params": {"paths": ["/A"], "extra": 1}},
        {"method": "layout_stack", "params": {"paths": ["/A"], "rows": [0]}},
        {"method": "layout_stack", "params": {"paths": ["/A"], "rows": [1.5]}},
        {"method": "layout_stack", "params": {"paths": ["/A"], "anchor": [1, 2]}},
        {"method": "layout_distribute", "params": {"paths": ["/A"]}},
        {"method": "layout_space", "params": {"paths": ["/A"], "spacing": "far"}},
        {"method": "layout_align", "params": {"paths": ["/A"], "mode": "x" * 100}},
        {"method": "robot_inspect", "params": {"max_prims": 0}},
        {"method": "robot_inspect", "params": {"root_path": ""}},
    ):
        try:
            dispatch(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    lacking = kit_rpc.make_extended_server_class(_BasicServer)(object())._dispatch({"method": "robot_inspect", "params": {}})
    assert lacking["result"]["needs_bridge_update"] is True and lacking["result"]["method"] == "robot_inspect"
    info = dispatch({"method": "capabilities", "params": {}})["result"]
    assert info["version"] == "tier8-1" and info["layout_methods"] == list(LAYOUT_RPC_METHODS)
    assert info["robot_methods"] == ["robot_inspect"]


# --------------------------------------------------------------------------- smart layout


def test_layout_on_the_fake_scene_and_older_bridges_say_why_and_change_nothing():
    with _server() as call:
        state = repr(call.router._scene._bridge._state())
        for name, arguments in (
            ("scene_layout_snap", {"paths": ["/World/Props/Cube"]}),
            ("scene_layout_stack", {"paths": ["/World/Props/Cube"], "rows": [1]}),
            ("scene_layout_distribute", {"paths": ["/World/Props/Cube"], "surface_path": "/World/Props"}),
            ("scene_layout_space", {"paths": ["/World/Props/Cube", "/World/Props"]}),
            ("scene_layout_align", {"paths": ["/World/Props/Cube", "/World/Props"], "dry_run": False}),
        ):
            result = call(name, **arguments)
            assert result["ok"] is False and result["applied"] is False and result.get("fake_scene") is True, (name, result)
        assert call("scene_layout_snap", paths=[])["ok"] is False
        assert call("scene_layout_snap", paths=["no-slash"])["ok"] is False
        assert repr(call.router._scene._bridge._state()) == state
        # Previews are never journaled; a refused apply (dry_run=False) is kept as an attempt that did not go through.
        tried = call("scene_journal")["entries"]
        assert [(entry["tool"], entry["ok"]) for entry in tried] == [("scene_layout_align", False)], tried
    with _server(scene=lambda: SceneMCPService(HttpSceneBridge(COMPOSER))) as call:
        with _in_process_transport(_BasicServer(FakeSceneBridge())):
            old = call("scene_layout_snap", paths=["/World/Cube"])
            assert old["ok"] is False and old["needs_bridge_update"] is True and old["how_to_enable"], old
            capabilities = call("scene_capabilities")
            assert set(LAYOUT_RPC_METHODS) | {"robot_inspect"} <= set(capabilities["missing_live_methods"])


def test_live_layout_previews_applies_undoes_and_is_journaled():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        stage = _layout_stage(tmp)
        original = _layers_text(stage)
        with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage) as (_b, _s, sent):
            capabilities = call("scene_capabilities")
            assert capabilities["rpc_version"] == "tier8-1" and capabilities["needs_bridge_update"] is False
            assert capabilities["layout"]["available"] is True and capabilities["robots"]["available"] is True
            preview = call("scene_layout_snap", paths=["/World/B1", "/World/B2"])
            assert preview["ok"] is True and preview["preview"] is True and preview["applied"] is False, preview
            assert preview["explanation"].startswith("Dry run") and "/World/B1" in preview["preview_table"]
            assert _layers_text(stage) == original, "a layout dry run changed a layer"
            assert call("scene_journal")["count"] == 0

            applied = call("scene_layout_snap", paths=["/World/B1", "/World/B2"], dry_run=False)
            assert applied["ok"] is True and applied["applied"] is True and applied["undoable"] is True, applied
            assert abs(_z(stage, "/World/B1") - 0.5) < 1e-6 and abs(_z(stage, "/World/B2") - 0.5) < 1e-6
            sentences = call("scene_journal")["sentences"]
            assert len(sentences) == 1 and "snapped 2 prims (/World/B1, /World/B2) onto the floor in Isaac Sim" in sentences[0]
            assert call("scene_journal", prim="/World/B2")["count"] == 1 and call("scene_journal", prim="/World/B3")["count"] == 0

            stacked = call("scene_layout_stack", paths=["/World/B1", "/World/B2", "/World/B3"], rows=[2, 1],
                           anchor=[0, 0, 0], dry_run=False)
            assert stacked["applied"] is True and stacked["undoable"] is True, stacked
            assert abs(_z(stage, "/World/B3") - 1.5) < 1e-6  # the top box rests on the bottom row
            for _ in range(2):  # stack, then snap: newest first
                assert call("scene_undo_last", dry_run=False)["applied"] is True
            assert _layers_text(stage) == original, "undo restores the layout exactly"
            story = call("scene_journal_summary")
            assert story["ok"] is True and story["edits"] == 2 and story["undos"] == 2, story

            # A sentence only ever previews, even with dry_run=False and exact paths.
            said = call.dispatch("snap /World/B1 to the floor", dry_run=False)
            assert said["ok"] is True and said["preview"] is True and said["applied"] is False, said
            assert said["tool"] == "scene_layout_snap" and said["arguments"] == {"paths": ["/World/B1"], "dry_run": False}
            assert "scene_layout_snap(" in said["message"] and said["moves"]
            named = call.dispatch("stack the boxes 2 1", dry_run=False)
            assert named["ok"] is False or named["preview"] is True
            spread = call.dispatch("put /World/B1 /World/B2 evenly on /World/Floor")
            assert spread["preview"] is True and spread["arguments"]["surface_path"] == "/World/Floor", spread
            assert _layers_text(stage) == original
            flags = [item["params"]["dry_run"] for item in sent if item["method"] in LAYOUT_RPC_METHODS]
            assert flags[:3] == [True, False, False] and set(flags[3:]) == {True}, flags  # sentences: dry runs only
            assert len(call("scene_journal", limit=50)["entries"]) == 4  # 2 layouts + 2 undos; previews never


# --------------------------------------------------------------------------- robot inspector


def test_live_robot_inspect_through_the_rpc_reads_only():
    if Usd is None:
        return
    stage = _arm_stage()
    original = _layers_text(stage)
    with _server(scene=lambda: SceneMCPService(HttpSceneBridge(ISAAC))) as call, _live(stage) as (_b, _s, sent):
        report = call("robot_inspect")
        assert report["ok"] is True and report["mode"] == "live" and report["action"] == "robot_inspect", report
        assert report["robot_count"] == 1 and report["robots"][0]["root"] == "/World/Franka"
        assert "time_budget" in report and report["read_only"] is True
        said = call.dispatch("inspect /World/Franka")
        assert said["tool"] == "robot_inspect" and said["ok"] is True and said["robot_count"] == 1, said
        assert call.dispatch("inspect my robot")["robots"][0]["dof_count"] == 1
        assert all("dry_run" not in item["params"] for item in sent if item["method"] == "robot_inspect")
        assert call("robot_inspect", max_prims=0)["ok"] is False
    assert _layers_text(stage) == original


def test_robot_inspect_reads_a_saved_robot_file_and_falls_back_on_the_fake_scene():
    if Usd is None:
        return
    with _server() as call:
        root = Path(os.environ["OMNIVERSE_ROOT"])
        path = root / "robots" / "arm.usda"
        path.parent.mkdir(parents=True)
        source = _arm_stage()  # keep the stage alive while its layer is exported
        assert source.GetRootLayer().Export(str(path))
        del source
        before = (path.stat().st_mtime_ns, path.read_bytes())
        report = call("robot_inspect", scene_file="robots/arm.usda")
        assert report["ok"] is True and report["mode"] == "saved_file" and report["robot_count"] == 1, report
        focused = call("robot_inspect", scene_file=str(path), root_path="/World/Franka/link1")
        assert focused["ok"] is True and focused["robots"][0]["root"] == "/World/Franka"
        assert (path.stat().st_mtime_ns, path.read_bytes()) == before
        outside = call("robot_inspect", scene_file=str(WORKSPACE / "nowhere" / "x.usda"))
        assert outside["ok"] is False and outside["mode"] == "saved_file"
        fake = call("robot_inspect")
        assert fake["ok"] is False and "fake scene" in fake["live_fallback"]["reason"] and "scene_file=" in fake["message"]


def test_robot_inspect_reads_a_real_isaac_robot_asset_read_only():
    if Usd is None:
        return
    asset = WORKSPACE / "isaacsim-assets-6.1.0" / "Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
    if not asset.is_file():
        return  # the Isaac asset pack is local-only
    with _env(OMNIVERSE_ROOT=WORKSPACE):
        with _server() as call:
            os.environ["OMNIVERSE_ROOT"] = str(WORKSPACE)  # _server set a temp root; the asset lives in the real one
            before = (asset.stat().st_mtime_ns, asset.stat().st_size)
            report = call("robot_inspect", scene_file=str(asset))
            assert report["ok"] is True and report["mode"] == "saved_file", report.get("message")
            assert report["robot_count"] >= 1 or report["truncated"], report.get("notes")
            assert (asset.stat().st_mtime_ns, asset.stat().st_size) == before


# --------------------------------------------------------------------------- change journal


def test_journal_records_applied_edits_only_and_stays_in_memory_under_tests():
    # A legitimate live MCP session may already have a journal. Tests must leave
    # it unchanged, rather than require the user's runtime history to be absent.
    real_folder = WORKSPACE / journal.FOLDER_NAME
    def real_state():
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in real_folder.rglob('*') if p.is_file()}
    before_real = real_state()
    existed = real_folder.exists()
    with _server() as call:
        status = call("scene_journal")["journal"]
        assert status["memory_only"] is True and status["path"] is None
        call("scene_move", path="/World/Props/Cube", translation=[0, 0, 1], dry_run=True)
        assert call("scene_journal")["count"] == 0
        call("scene_move", path="/World/Props/Cube", translation=[0, 0, 2], dry_run=False)
        call.dispatch("move /World/Props/Cube by 0 0 1", dry_run=True)
        call.dispatch("move /World/Props/Cube by 0 0 1")
        entries = call("scene_journal")["entries"]
        assert [entry["tool"] for entry in entries] == ["router_dispatch", "scene_move"], entries
        assert entries[1]["target"] == "fake" and entries[1]["after"] == {"translation": [0, 0, 2]}
        explained = call("scene_journal_explain", tool="scene_move")
        assert explained["count"] == 1 and "You moved /World/Props/Cube" in explained["sentences"][0]
        assert call("scene_journal", since="today")["count"] == 2
        assert call("scene_journal", since="not a date")["ok"] is False
        assert call("scene_journal", limit=0)["ok"] is False
        summary = call("scene_journal_summary", day="today")
        assert summary["ok"] is True and summary["edits"] == 2 and len(summary["story"]) == 3
        assert call("scene_journal_summary", day="someday")["ok"] is False
        said = call.dispatch("what did you change today")
        assert said["tool"] == "scene_journal_summary" and said["edits"] == 2 and said["read_only"] is True
        shown = call.dispatch("what did you change on /World/Props/Cube")
        assert shown["tool"] == "scene_journal" and shown["count"] == 2
        kept = call("scene_journal_clear")
        assert kept["ok"] is False and call("scene_journal")["count"] == 2
        cleared = call("scene_journal_clear", confirm=True)
        assert cleared["ok"] is True and cleared["cleared"] == 2 and call("scene_journal")["count"] == 0
    assert real_folder.exists() == existed and real_state() == before_real, "tests changed the real scene journal"


def test_default_journal_folder_ignores_itself_in_git():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / journal.FOLDER_NAME
        book = ChangeJournal(folder / journal.FILE_NAME)
        assert not folder.exists()  # nothing is created until the first write
        book.record({"tool": "scene_move", "action": "move", "prims": ["/World/A"]})
        assert (folder / ".gitignore").read_text(encoding="utf-8").splitlines()[-1] == "*"
        elsewhere = ChangeJournal(Path(tmp) / "custom" / "j.jsonl")
        elsewhere.record({"tool": "scene_move"})
        assert sorted(p.name for p in (Path(tmp) / "custom").iterdir()) == ["j.jsonl"]
    with _env(OMNIVERSE_SCENE_MCP_JOURNAL=None, OMNIVERSE_SCENE_MCP_TEST="1"):
        assert journal.build_change_journal().memory_only is True


# --------------------------------------------------------------------------- drills, today, reminder


def test_drills_start_answer_and_status_use_the_memory_progress_store():
    with _server() as call:
        focuses = call("learning_drill_focuses")
        assert focuses["ok"] is True and {item["name"] for item in focuses["focuses"]} >= {"weak", "flash", "domain"}
        drill = call("learning_drill_start", focus="mixed", count=3)
        assert drill["ok"] is True and len(drill["questions"]) == 3 and drill["storage"]["memory_only"] is True, drill
        assert all("answer" not in question and "correct" not in question for question in drill["questions"])
        status = call("learning_drill_status", drill_id=drill["drill_id"])
        assert status["ok"] is True and status["status"] == "active"
        graded = call("learning_drill_answer", drill_id=drill["drill_id"], answers=["A", "A", "A"], elapsed_s=30)
        assert graded["ok"] is True and graded["total"] == 3, graded
        recent = call("learning_exam_progress")["recent"]
        assert recent and all(row["source"] == "drill" for row in recent[:3])
        assert call("learning_drill_status", drill_id=drill["drill_id"])["status"] == "finished"
        assert call("learning_drill_start", focus="objective")["ok"] is False  # objective= is needed
        flash = call.dispatch("quick flash quiz")
        assert flash["tool"] == "learning_drill_start" and flash["ok"] is True and flash["focus"] == "flash"
        domain = call.dispatch("drill me on composition")
        assert domain["arguments"] == {"focus": "domain", "domain": "Composition"} and domain["ok"] is True
        dry = call("router_dispatch", request="start a drill on weak areas", dry_run=True)["payload"]
        assert dry["executed"] is False and dry["dry_run"] is True and "no drill was started" in dry["message"]


def test_today_and_reminder_only_read_and_create_nothing():
    with _server() as call:
        before = call("learning_exam_progress")
        before.pop("as_of", None)
        brief = call("learning_today", minutes=30)
        assert brief["ok"] is True and brief["minutes"] == 30 and brief["plan"], brief
        assert call("learning_today", minutes=5)["ok"] is False
        reminder = call("learning_reminder_setup", at="07:30")
        assert reminder["ok"] is True and reminder["created"] is False and reminder["executed"] is False
        assert "07:30" in reminder["create_command"] and "Nothing was created" in reminder["note"]
        assert call("learning_reminder_setup", at="7 pm")["ok"] is False
        said = call.dispatch("set up a daily reminder at 7pm")
        assert said["tool"] == "learning_reminder_setup" and said["time"] == "19:00" and said["created"] is False
        assert said["executed"] is True and said["commands_run"] is False
        today = call.dispatch("what should I do today")
        assert today["tool"] == "learning_today" and today["ok"] is True
        after = call("learning_exam_progress")
        after.pop("as_of", None)
        assert after == before


# --------------------------------------------------------------------------- Docker docs


def test_docker_tools_are_read_only_and_never_run_docker_here():
    with _no_docker(), _server() as call:
        status = call("docs_docker_status")
        assert status["docker_installed"] is False and status["service"] == "usd", status
        assert call("docs_docker_status", service="nope")["ok"] is False
        plan = call("docs_docker_setup_plan", service="kit")
        assert plan["steps"] and any(step["needs_your_approval"] for step in plan["steps"])
        assert call("docs_docker_explain")["short_answer"]
        text = json.dumps(plan) + json.dumps(status)
        for name in docker_docs.SECRET_ENV_VARS:
            value = os.environ.get(name)
            assert not value or value not in text
        said = call.dispatch("is docker running")
        assert said["tool"] == "docs_docker_status" and said["docker_installed"] is False
        assert call.dispatch("is docker worth it")["tool"] == "docs_docker_explain"


# --------------------------------------------------------------------------- natural-language routing


def test_round7_phrases_route_to_the_new_tools():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for phrase, (tool, target) in PHRASES.items():
        decision = router.route(phrase)
        assert decision.target is target, (phrase, decision)
        spec = nl_tier6.parse(phrase)
        assert spec is not None, phrase
        name = f"scene_layout_{spec['op']}" if spec["kind"] == "layout" else router_module._ROUND7_TOOLS[spec["kind"]]
        assert name == tool, (phrase, name)
    # Without the server's helpers the pure answers still work and the rest name their tool.
    plan = asyncio.run(router.handle("how do I set up docker")).payload
    assert plan["executed"] is True and plan["steps"]
    drill = asyncio.run(router.handle("start a drill on weak areas")).payload
    assert drill["executed"] is False and "learning_drill_start(" in drill["message"]
    reminder = asyncio.run(router.handle("remind me every day at 19:00")).payload
    assert reminder["executed"] is True and reminder["created"] is False and reminder["time"] == "19:00"


def test_nl_tier6_parses_values():
    assert nl_tier6.parse("stack these 10 cubes 4 3 2 1")["options"] == {"rows": [4, 3, 2, 1]}
    spec = nl_tier6.parse("stack the boxes on /World/Pallet 3 2 1")
    assert spec["name"] == "boxes" and spec["options"] == {"anchor": "/World/Pallet", "rows": [3, 2, 1]}
    assert nl_tier6.parse("stack /World/A /World/B at 0 0 1")["options"]["anchor"] == [0.0, 0.0, 1.0]
    assert nl_tier6.parse("snap /World/A /World/B onto /World/Table")["options"] == {"surface": "/World/Table"}
    assert nl_tier6.parse("align the boxes on z by top")["options"] == {"axis": "z", "mode": "max"}
    assert nl_tier6.parse("give me a 5 question drill on my mistakes")["focus"] == "mistakes"
    assert nl_tier6.parse("drill me on objective 1.6")["objective"] == "1.6"
    assert nl_tier6.parse("what should i study today, i have 2 hours")["minutes"] == 120
    assert nl_tier6.parse("set up a reminder at 7:30 am")["at"] == "07:30"
    assert nl_tier6.parse("set up a reminder")["at"] == "19:00"
    assert nl_tier6.parse("inspect the robot in robots/arm.usda")["scene_file"] == "robots/arm.usda"
    assert nl_tier6.parse("docker status for kit")["service"] == "kit"


def test_existing_phrases_keep_their_routes_before_and_after_round7():
    """Every phrase the other tests send (and the help phrases) routes exactly as it did without nl_tier6."""
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    phrases = _test_phrases()
    assert len(phrases) > 60, len(phrases)
    after = {phrase: router.route(phrase) for phrase in phrases}
    original = nl_tier6.parse
    nl_tier6.parse = lambda _text: None
    try:
        before = {phrase: router.route(phrase) for phrase in phrases}
    finally:
        nl_tier6.parse = original
    changed = [(phrase, before[phrase].target, after[phrase].target, after[phrase].reason)
               for phrase in phrases if before[phrase] != after[phrase]]
    assert not changed, changed
    for phrase in KEPT:
        assert nl_tier6.parse(phrase) is None, phrase
    assert router.route("what did you change").target is RouteTarget.SCENE
    assert "Learning keywords" in router.route("drill me on prims").reason
    for phrase in PHRASES:  # round-7 phrases are not learning-helper (tier5) phrases either
        assert nl_tier5.parse(phrase) is None, phrase


def test_nl_tier6_stays_fast_on_long_input():
    for text in ("x" + "." * 20_000, "stack " + "a " * 290, "align " + "/A " * 190 + "on x",
                 "snap " + "/A " * 190 + "to the floor", "remind me " + "every day " * 11, "start a " + "drill " * 90,
                 "put " + "a " * 280 + "evenly on /X", "stack " + "1 " * 290, "drill me on " + "x" * 500,
                 "inspect my robot " + "/A" * 280, "space " + "/A " * 190 + "evenly along x"):
        started = time.perf_counter()
        nl_tier6.parse(text)
        assert time.perf_counter() - started < 0.5, text[:40]
    assert nl_tier6.parse("show the change journal please!!")["kind"] == "journal"
    assert nl_tier6.parse("is docker worth it " + "?" * 1000) is None  # over the phrase cap


# --------------------------------------------------------------------------- packaging and self-tests


def test_bridge_reload_self_tests_include_round7_files():
    for name in ("test_layout.py", "test_robot_inspector.py"):
        assert (TESTS / name).is_file(), name
    for script in (WORKSPACE / "isaacsim-study" / "reload_mcp_bridge.py",
                   WORKSPACE / "composer-study" / "reload_composer_bridge.py"):
        if script.is_file():
            text = script.read_text(encoding="utf-8")
            assert "test_layout.py" in text and "test_robot_inspector.py" in text, script.name
    smoke = (TESTS / "test_robot_inspector.py").read_text(encoding="utf-8")
    assert "if _IN_KIT:" in smoke  # the real-asset smoke test returns at once inside Kit
