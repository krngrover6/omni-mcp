"""Tool profiles, token estimates, compact results, FastMCP glue and the example client configs (no network)."""

from __future__ import annotations

import asyncio
import copy
import json
import re
import tomllib
from pathlib import Path

from omniverse_scene_mcp import help as router_help
from omniverse_scene_mcp import profiles

DOCS = Path(__file__).resolve().parents[1] / "docs"
CONFIGS = DOCS / "client-configs"
REGISTRATION_ORDER = [
    "router_dispatch",
    "router_route",
    "router_help",
    "scene_summary",
    "scene_move",
    "learning_exam_quiz",
    "learning_exam_answer",
    "robot_inspect",
    "sim_play",
    "scene_layout_snap",
    "scene_layout_stack",
    "kit_logs",
]


def test_resolve_profile_keeps_only_existing_tools_and_reports_unknown_names():
    available = {"router_dispatch", "router_help", "scene_summary", "scene_inspect"}
    result = profiles.resolve_profile("starter", available)
    assert result["tools"] == sorted(available)
    assert "scene_children" in result["unknown"]  # in the profile, not registered: tolerated, reported
    assert "router_dispatch" not in result["unknown"]
    assert result["missing_groups"] == []
    assert result["profiles"] == ["starter"]


def test_resolve_profile_patterns_explicit_names_aliases_and_missing_groups():
    available = set(REGISTRATION_ORDER)
    result = profiles.resolve_profile(["robot, scene_layout_*", "kit_logs", "no_such_tool", "nonsense"], available)
    assert {"robot_inspect", "sim_play", "scene_layout_snap", "scene_layout_stack", "kit_logs"} <= set(result["tools"])
    assert "no_such_tool" in result["unknown"]
    assert result["missing_groups"] == ["nonsense"]
    assert result["profiles"] == ["robotics"]
    assert "*ros2*" in result["unmatched_patterns"]  # future robotics tools: tolerated
    assert result["tools"] == sorted(result["tools"])


def test_always_on_tools_survive_every_profile():
    available = set(REGISTRATION_ORDER)
    for name in profiles.PROFILES:
        tools = profiles.resolve_profile(name, available)["tools"]
        assert {"router_dispatch", "router_route", "router_help"} <= set(tools), name


def test_profile_from_env_parses_comma_lists_and_defaults_to_starter():
    assert profiles.profile_from_env({}) == ["starter"]
    assert profiles.profile_from_env({"OMNIVERSE_MCP_PROFILE": "   "}) == ["starter"]
    assert profiles.profile_from_env({"OMNIVERSE_MCP_PROFILE": " starter , study,,starter "}) == ["starter", "study"]
    assert profiles.profile_from_env({"OMNIVERSE_MCP_PROFILE": "build"}) == ["build"]


def test_apply_profile_keeps_registration_order_without_duplicates():
    order = REGISTRATION_ORDER + ["scene_summary"]
    kept = profiles.apply_profile(order, "starter")
    assert kept == ["router_dispatch", "router_route", "router_help", "scene_summary"]
    kept = profiles.apply_profile(order, "study,robotics")
    assert kept == [name for name in REGISTRATION_ORDER if name in set(kept)]
    assert "learning_exam_quiz" in kept and "sim_play" in kept and "scene_move" not in kept


def test_apply_profile_all_and_misspelled_profiles_keep_everything():
    assert profiles.apply_profile(REGISTRATION_ORDER, "all") == REGISTRATION_ORDER
    assert profiles.apply_profile(REGISTRATION_ORDER, profiles.profile_from_env({"OMNIVERSE_MCP_PROFILE": "all"})) == REGISTRATION_ORDER
    # Nothing matches at all (and no ALWAYS_ON tool is registered): keep everything rather than an empty server.
    assert profiles.apply_profile(["a_tool", "b_tool"], "stater") == ["a_tool", "b_tool"]


def test_starter_profile_is_small_and_names_real_catalog_tools():
    starter = profiles.PROFILES["starter"]
    assert 15 <= len(starter) <= 20
    catalog = set(router_help.tool_names())
    assert [name for name in starter if name not in catalog] == []


def test_estimate_schema_tokens_grows_with_more_tools():
    schemas = [
        {"name": f"tool_{i}", "description": "Does a thing. " * (i + 1), "inputSchema": {"type": "object"}}
        for i in range(6)
    ]
    estimates = [profiles.estimate_schema_tokens(schemas[:count]) for count in range(len(schemas) + 1)]
    assert estimates[0] == 0
    assert all(later > earlier for earlier, later in zip(estimates, estimates[1:]))
    one = schemas[0]
    chars = len(json.dumps(one, separators=(",", ":")))
    assert profiles.estimate_schema_tokens([one]) == (chars + 3) // 4


def test_estimate_profile_tokens_rows_are_consistent():
    schemas = [{"name": name, "description": name * 3, "inputSchema": {"type": "object"}} for name in REGISTRATION_ORDER]
    rows = {row["profile"]: row for row in profiles.estimate_profile_tokens(schemas)}
    assert rows["all"]["tools"] == len(REGISTRATION_ORDER) and rows["all"]["percent_of_all"] == 100
    assert rows["starter"]["tokens"] < rows["all"]["tokens"]
    assert rows["starter,study"]["tokens"] >= rows["starter"]["tokens"]
    assert set(profiles.PROFILES) <= set(rows)


def test_compact_result_trims_lists_strings_and_drop_keys_without_touching_input():
    result = {
        "ok": True,
        "items": list(range(25)),
        "text": "x" * 1000,
        "python": "print('hi')",
        "nested": {"glossary": {"a": 1}, "rows": [{"preview": "big", "name": "n"}] * 3},
    }
    original = copy.deepcopy(result)
    trimmed = profiles.compact_result(result, max_list=10, max_str=50)
    assert result == original
    assert trimmed["items"][:10] == list(range(10)) and trimmed["items"][10] == "... 15 more"
    assert trimmed["text"].startswith("x" * 50) and "[cut 950 chars]" in trimmed["text"]
    assert "python" not in trimmed and "glossary" not in trimmed["nested"]
    assert trimmed["nested"]["rows"][0] == {"name": "n"}
    assert "_trimmed" in trimmed and "python" in trimmed["_trimmed"] and "glossary" in trimmed["_trimmed"]


def test_compact_result_keep_keys_and_untrimmed_results():
    kept = profiles.compact_result({"python": "code", "ok": True}, keep_keys=("python",))
    assert kept == {"python": "code", "ok": True}
    assert profiles.compact_result({"ok": True, "items": [1, 2]}) == {"ok": True, "items": [1, 2]}
    assert profiles.compact_result(["a"] * 12, max_list=2) == ["a", "a", "... 10 more"]


def test_compact_enabled_reads_the_env_switch():
    assert profiles.compact_enabled({"OMNIVERSE_MCP_COMPACT": "1"})
    assert profiles.compact_enabled({"OMNIVERSE_MCP_COMPACT": " Yes "})
    assert not profiles.compact_enabled({"OMNIVERSE_MCP_COMPACT": "0"})
    assert not profiles.compact_enabled({})


def _small_app():
    from mcp.server.fastmcp import FastMCP

    app = FastMCP(name="profiles-test")

    @app.tool(name="router_dispatch")
    def router_dispatch(request: str) -> dict:
        return {"request": request}

    @app.tool(name="scene_layers")
    def scene_layers(count: int = 3) -> dict:
        return {"layers": list(range(count)), "python": "code"}

    @app.tool(name="learning_exam_quiz")
    async def learning_exam_quiz(count: int = 2) -> dict:
        return {"questions": list(range(count))}

    return app


def _listed(app) -> list[str]:
    return [tool.name for tool in asyncio.run(app.list_tools())]


def test_prune_stash_enable_and_call_with_real_fastmcp():
    app = _small_app()
    stash, report = profiles.apply_profile_to_app(app, {"OMNIVERSE_MCP_PROFILE": "starter"})
    assert _listed(app) == ["router_dispatch"]
    assert report["listed"] == 1 and report["hidden"] == 2 and report["requested"] == ["starter"]
    assert sorted(stash.names()) == ["learning_exam_quiz", "scene_layers"]
    assert stash.describe("scene_*") == [{"name": "scene_layers", "about": ""}] or stash.describe("scene_*")[0]["name"] == "scene_layers"
    # Hidden tools stay callable, with normal argument validation (the string "4" becomes int 4).
    assert asyncio.run(stash.call("scene_layers", {"count": "4"})) == {"layers": [0, 1, 2, 3], "python": "code"}
    assert asyncio.run(stash.call("learning_exam_quiz", {})) == {"questions": [0, 1]}
    assert stash.enable("study") == ["learning_exam_quiz"]
    assert _listed(app) == ["router_dispatch", "learning_exam_quiz"]
    assert stash.enable("scene_layers") == ["scene_layers"] and stash.names() == []
    try:
        asyncio.run(stash.call("missing_tool"))
    except KeyError:
        pass
    else:
        raise AssertionError("unknown tool must raise KeyError")


def test_profile_all_leaves_the_app_unchanged():
    app = _small_app()
    stash, report = profiles.apply_profile_to_app(app, {"OMNIVERSE_MCP_PROFILE": "all"})
    assert _listed(app) == ["router_dispatch", "scene_layers", "learning_exam_quiz"]
    assert stash.names() == [] and report["hidden"] == 0


def test_install_compact_outputs_wraps_once_and_follows_the_switch():
    app = _small_app()
    environ = {"OMNIVERSE_MCP_COMPACT": "1"}
    wrapped = profiles.install_compact_outputs(app, environ=environ, max_list=2)
    assert sorted(wrapped) == ["learning_exam_quiz", "router_dispatch", "scene_layers"]
    assert profiles.install_compact_outputs(app, environ=environ) == []  # never wraps twice
    stash = profiles.prune_app_tools(app, {"router_dispatch"})
    compact = asyncio.run(stash.call("scene_layers", {"count": 5}))
    assert compact["layers"] == [0, 1, "... 3 more"] and "python" not in compact and "_trimmed" in compact
    assert asyncio.run(stash.call("learning_exam_quiz", {"count": 3}))["questions"] == [0, 1, "... 1 more"]
    environ["OMNIVERSE_MCP_COMPACT"] = "0"
    assert asyncio.run(stash.call("scene_layers", {"count": 5})) == {"layers": [0, 1, 2, 3, 4], "python": "code"}


def test_advertise_tool_list_changed_sets_the_capability_once():
    app = _small_app()
    assert app._mcp_server.create_initialization_options().capabilities.tools.listChanged is False
    assert profiles.advertise_tool_list_changed(app) is True
    assert profiles.advertise_tool_list_changed(app) is True
    assert app._mcp_server.create_initialization_options().capabilities.tools.listChanged is True
    assert profiles.advertise_tool_list_changed(object()) is False


def test_fastmcp_glue_tolerates_non_fastmcp_objects():
    thing = object()
    assert profiles.registered_tool_names(thing) == []
    assert profiles.install_compact_outputs(thing) == []
    stash, report = profiles.apply_profile_to_app(thing, {"OMNIVERSE_MCP_PROFILE": "starter"})
    assert stash.names() == [] and report["listed"] == 0 and stash.enable("study") == []


# ------------------------------------------------------------------------------------------ example client configs

_SECRET_HINTS = re.compile(
    r"((?:api[_-]?key|secret|password|passwd|authorization|token)\b\s*[:=]\s*[\"']?[^\s\"']{8,}"
    r"|bearer\s+[a-z0-9]{8,}|sk-[a-z0-9]{8,}|ghp_[a-z0-9]{8,})",
    re.IGNORECASE,
)


def _assert_no_secrets_or_foreign_user_paths(text: str, name: str) -> None:
    assert not _SECRET_HINTS.search(text), f"{name} looks like it contains a secret"
    for match in re.finditer(r"[A-Za-z]:[/\\]+Users[/\\]+([^/\\\"']+)", text):
        assert match.group(1).lower() == "krngr", f"{name}: unexpected user path {match.group(0)}"
    assert "AppData" not in text, f"{name} must not contain AppData paths"


def test_codex_example_parses_as_toml_in_both_options_and_has_no_secrets():
    text = (CONFIGS / "codex-config.toml.example").read_text(encoding="utf-8")
    _assert_no_secrets_or_foreign_user_paths(text, "codex-config.toml.example")
    option_a = tomllib.loads(text)
    server = option_a["mcp_servers"]["omniverse_router"]
    assert server["url"] == "http://127.0.0.1:9905/mcp"
    # exactly what the router's starter profile shows: the starter list plus the always-on meta tools
    starter = list(profiles.PROFILES["starter"])
    expected = starter + [name for name in profiles.ALWAYS_ON if name not in starter]
    assert list(server["enabled_tools"]) == expected
    uncommented = "\n".join(line[3:] if line.startswith("#B ") else ("" if line == "#B" else line) for line in text.splitlines())
    option_b = tomllib.loads(uncommented)["mcp_servers"]["omniverse_router_stdio"]
    assert option_b["args"] == ["-m", "omniverse_scene_mcp", "serve", "--transport", "stdio"]
    assert option_b["env"]["OMNIVERSE_MCP_PROFILE"] == "starter,study"


def test_claude_example_configs_parse_as_json_and_have_no_secrets():
    desktop_text = (CONFIGS / "claude-desktop.json.example").read_text(encoding="utf-8")
    code_text = (CONFIGS / "claude-code.mcp.json.example").read_text(encoding="utf-8")
    for name, text in (("claude-desktop", desktop_text), ("claude-code", code_text)):
        _assert_no_secrets_or_foreign_user_paths(text, name)
    desktop = json.loads(desktop_text)["mcpServers"]["omniverse_router"]
    assert desktop["args"][-2:] == ["--transport", "stdio"]
    assert desktop["env"]["OMNIVERSE_MCP_PROFILE"] in {"starter", "starter,study"}
    assert set(desktop["env"]) == {"PYTHONPATH", "OMNIVERSE_ROOT", "OMNIVERSE_MCP_PROFILE", "OMNIVERSE_MCP_COMPACT"}
    code = json.loads(code_text)["mcpServers"]["omniverse_router"]
    assert code == {"type": "http", "url": "http://127.0.0.1:9905/mcp"}


def test_usage_doc_mentions_every_profile_and_the_env_switches():
    text = (DOCS / "USING_WITH_CODEX_AND_CLAUDE_PRO.md").read_text(encoding="utf-8")
    for name in profiles.PROFILES:
        assert f"`{name}`" in text, name
    assert profiles.PROFILE_ENV in text and profiles.COMPACT_ENV in text
    _assert_no_secrets_or_foreign_user_paths(text, "USING_WITH_CODEX_AND_CLAUDE_PRO.md")
