import json
from pathlib import Path
import re

from omniverse_scene_mcp import help as tool_guide
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter, RouteTarget
from omniverse_scene_mcp.service import SceneMCPService

SERVER = Path(__file__).resolve().parents[1] / "src" / "omniverse_scene_mcp" / "server.py"
GROUPS = [
    "Scene: look",
    "Scene: change safely",
    "Live insight",
    "Lights/cameras/materials",
    "Assets & physics",
    "Simulation & screenshots",
    "Undo",
    "Knowledge & docs",
    "Exam prep",
    "Learning",
    "Health & connection",
]
REQUIRED_PLANNED = {
    "scene_set_attribute",
    "scene_create_material",
    "scene_bind_material",
    "scene_undo_last",
    "scene_undo_history",
    "router_help",
    "router_scene_targets",
    "router_set_scene_target",
}


def _server_tool_names():
    """Tool names registered with @app.tool(name=...) in server.py, read as text (the server is not built)."""
    text = SERVER.read_text(encoding="utf-8")
    return re.findall(r"@app\.tool\(\s*name\s*=\s*[\"']([^\"']+)[\"']", text)


def _router():
    return OmniverseRouter(SceneMCPService(FakeSceneBridge()))


def test_catalog_lists_every_current_server_tool():
    registered = _server_tool_names()
    assert len(registered) >= 72, len(registered)
    assert len(registered) == len(set(registered)), "server.py registers a tool name twice"
    catalog = set(tool_guide.tool_names())
    missing = sorted(set(registered) - catalog)
    assert not missing, f"Add these server tools to help.TOOL_CATALOG: {missing}"
    stale = sorted(catalog - set(registered) - tool_guide.PLANNED_TOOLS)
    assert not stale, f"help.TOOL_CATALOG lists tools server.py does not register: {stale}"
    now_registered = sorted(set(registered) & tool_guide.PLANNED_TOOLS)
    assert not now_registered, f"server.py now registers these; remove them from help.PLANNED_TOOLS: {now_registered}"


def test_catalog_includes_the_planned_tools():
    names = tool_guide.tool_names()
    assert REQUIRED_PLANNED <= set(names)
    assert tool_guide.PLANNED_TOOLS <= set(names)
    assert tool_guide.PLANNED_TOOLS <= REQUIRED_PLANNED  # only the integrator's announced names are "planned"


def test_catalog_entries_are_well_formed_and_grouped_by_task():
    assert list(tool_guide.TOOL_CATALOG) == GROUPS
    assert set(tool_guide.GROUP_ABOUT) == set(GROUPS) and set(tool_guide.GROUP_PHRASES) == set(GROUPS)
    names = tool_guide.tool_names()
    assert len(names) == len(set(names)), "a tool appears in two groups"
    for group, entries in tool_guide.TOOL_CATALOG.items():
        assert entries, group
        for entry in entries:
            assert set(entry) == {"name", "what", "example", "mutates", "needs_bridge"}, entry
            assert isinstance(entry["mutates"], bool) and isinstance(entry["needs_bridge"], bool), entry
            assert entry["what"].endswith(".") and "\n" not in entry["what"], entry["name"]
            assert len(entry["what"]) <= 200, entry["name"]
            assert entry["example"].startswith(entry["name"] + "("), entry["name"]
    by_name = {entry["name"]: entry for entries in tool_guide.TOOL_CATALOG.values() for entry in entries}
    for name in ("scene_summary", "scene_inspect", "scene_stage_info", "usd_docs_search", "router_help"):
        assert by_name[name]["mutates"] is False, name
    for name in ("scene_create", "scene_delete", "scene_create_light", "scene_set_attribute", "scene_undo_last",
                 "learning_exam_answer", "learning_mock_exam_start"):
        assert by_name[name]["mutates"] is True, name
    # Previewable scene edits say how to preview in their example.
    for group in ("Scene: change safely", "Lights/cameras/materials"):
        for entry in tool_guide.TOOL_CATALOG[group]:
            assert "dry_run=True" in entry["example"], entry["name"]
    # Physics and asset edits preview by default too, and their examples say so.
    for entry in tool_guide.TOOL_CATALOG["Assets & physics"]:
        assert ("dry_run=True" in entry["example"]) is entry["mutates"], entry["name"]
    for name in ("sim_play", "sim_stop", "sim_step", "scene_capture_viewport", "learning_exam_dashboard"):
        assert by_name[name]["mutates"] is True, name  # simulation state, a PNG, or dashboard.html
    for name in ("asset_search", "asset_info", "scene_physics_report", "sim_status", "scene_capture_status"):
        assert by_name[name]["mutates"] is False, name


def test_help_without_a_topic_returns_every_group_and_the_quick_start():
    guide = tool_guide.help()
    assert guide["ok"] is True and guide["topic"] is None
    assert [group["group"] for group in guide["groups"]] == GROUPS
    assert sum(len(group["tools"]) for group in guide["groups"]) == len(tool_guide.tool_names())
    planned = {tool["name"] for group in guide["groups"] for tool in group["tools"] if tool.get("planned")}
    assert planned == set(tool_guide.PLANNED_TOOLS)
    assert len(guide["quick_start"]) == 8
    assert set(guide["legend"]) == {"mutates", "needs_bridge", "planned"}
    text = json.dumps(guide)
    assert len(text) < 60000, len(text)
    # The result is a copy: changing it never changes the catalog.
    guide["groups"][0]["tools"][0]["name"] = "changed"
    assert tool_guide.TOOL_CATALOG["Scene: look"][0]["name"] == "scene_summary"
    assert tool_guide.help() == tool_guide.help()


def test_help_topic_matches_a_group_a_tool_or_a_search():
    for topic, group in (("lights", "Lights/cameras/materials"), ("Undo", "Undo"), ("exam", "Exam prep"),
                         ("health", "Health & connection"), ("Live insight", "Live insight"),
                         ("the docs", "Knowledge & docs"), ("dry run", "Scene: change safely")):
        guide = tool_guide.help(topic)
        assert guide["ok"] is True and guide["match"] == "group" and guide["group"] == group, topic
        assert guide["examples"] == [tool["example"] for tool in guide["tools"]], topic
    tool = tool_guide.help("scene_create_light")
    assert tool["match"] == "tool" and tool["group"] == "Lights/cameras/materials"
    assert tool["tool"]["name"] == "scene_create_light" and "scene_aim" in tool["see_also"]
    assert tool_guide.help("scene create light")["tool"]["name"] == "scene_create_light"
    # Every catalog tool is registered now, so nothing is marked planned.
    assert "planned" not in tool_guide.help("router_help")["tool"]
    search = tool_guide.help("variant")
    assert search["match"] == "search"
    assert {"scene_variants", "scene_variant_set"} <= {item["name"] for item in search["tools"]}
    assert search["count"] <= tool_guide.MAX_SEARCH_RESULTS
    missing = tool_guide.help("flux capacitor")
    assert missing["ok"] is False and missing["groups"] == GROUPS and len(missing["quick_start"]) == 8
    assert tool_guide.help("  ")["topic"] is None


def test_resolve_topic_is_strict_unless_search_is_requested():
    assert tool_guide.resolve_topic("lights") == {"kind": "group", "name": "Lights/cameras/materials"}
    assert tool_guide.resolve_topic("scene_undo_last") == {"kind": "tool", "name": "scene_undo_last", "group": "Undo"}
    assert tool_guide.resolve_topic("add a light") is None
    assert tool_guide.resolve_topic("variant") is None
    assert tool_guide.resolve_topic("variant", search=True)["kind"] == "search"
    assert tool_guide.resolve_topic(None) is None and tool_guide.resolve_topic("") is None


def test_quick_start_phrases_route_and_parse():
    router = _router()
    phrases = [item["say"] for item in tool_guide.quick_start()]
    assert len(phrases) == 8 and len(set(phrases)) == 8
    assert all(item["tool"] == "router_dispatch" and item["does"] for item in tool_guide.quick_start())
    for phrase in phrases:
        target = router.route(phrase).target
        assert target is not RouteTarget.UNKNOWN, phrase
        if target is RouteTarget.SCENE:
            parsed = router._parse_scene_command(phrase)
            assert parsed is not None and parsed["action"] not in {"unsupported"}, phrase
    assert router.route("what can you do").target is RouteTarget.LEARN
    assert router.route("next lesson").target is RouteTarget.LEARN


def test_group_phrases_route_where_their_group_says():
    router = _router()
    expected = {
        "Scene: look": RouteTarget.SCENE,
        "Scene: change safely": RouteTarget.SCENE,
        "Lights/cameras/materials": RouteTarget.SCENE,
        "Assets & physics": RouteTarget.SCENE,
        "Simulation & screenshots": RouteTarget.SCENE,
        "Undo": RouteTarget.SCENE,
        "Exam prep": RouteTarget.LEARN,
        "Learning": RouteTarget.LEARN,
    }
    for group, phrases in tool_guide.GROUP_PHRASES.items():
        for phrase in phrases:
            target = router.route(phrase).target
            assert target is not RouteTarget.UNKNOWN, (group, phrase)
            if group in expected:
                assert target is expected[group], (group, phrase, target)
    assert router.route("what is a payload in USD?").target is RouteTarget.USD
    assert router.route("help").target is RouteTarget.LEARN
    assert router.route("status").target is RouteTarget.SCENE


def test_the_advertised_kit_example_is_answered_by_the_offline_kit_docs():
    phrase = "how do I subscribe to stage events in Kit?"
    assert phrase in tool_guide.GROUP_PHRASES["Knowledge & docs"]
    kit_tool = next(entry for group in tool_guide.TOOL_CATALOG.values() for entry in group
                    if entry["name"] == "kit_knowledge")
    assert kit_tool["example"] == 'kit_knowledge(query="How do I subscribe to stage events in Kit?")'
    assert "write a Kit extension" not in json.dumps(tool_guide.help())
    assert _router().route(phrase).target is RouteTarget.KIT
    real_root = Path(__file__).resolve().parents[2] / "isaacsim-6.1.0"
    if not (real_root / "extscache").is_dir():
        return  # Isaac Sim 6.1.0 (and its Kit docs) is not installed next to omniverse_scene_mcp
    from omniverse_scene_mcp.offline_docs_knowledge import OfflineDocsKnowledge, clear_cache

    try:
        hits = OfflineDocsKnowledge("kit", roots=real_root).search(phrase)
        assert hits and "stage events" in hits[0]["title"].lower(), [hit["title"] for hit in hits[:3]]
    finally:
        clear_cache()
