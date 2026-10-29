"""Every tool must work in Codex (OpenAI function calling), not only in Claude.

OpenAI's function-calling rules that Codex inherits: the name matches ^[a-zA-Z0-9_-]{1,64}$, the description is at
most 1024 characters, and parameters are a JSON-schema object. The starter profile must also stay small (OpenAI
caps a request at 128 tools) and keep the meta tools that reach everything else.
"""
import json
import re

from omniverse_scene_mcp import profiles, workflows
from test_twelve_gaps_integration import _server

_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_DESCRIPTION = 1024
MAX_TOOLS_PER_REQUEST = 128


def _tools():
    with _server() as call:
        return {tool.name: tool for tool in call.app._tool_manager.list_tools()}


def test_every_tool_fits_openai_function_calling_limits():
    problems = []
    for name, tool in _tools().items():
        if not _NAME.match(name):
            problems.append(f"{name}: name")
        description = tool.description or ""
        if not description.strip():
            problems.append(f"{name}: empty description")
        if len(description) > MAX_DESCRIPTION:
            problems.append(f"{name}: description {len(description)} chars")
        schema = tool.parameters
        if not isinstance(schema, dict) or schema.get("type") != "object":
            problems.append(f"{name}: parameters are not an object schema")
        else:
            for param in (schema.get("properties") or {}):
                if not _NAME.match(param):
                    problems.append(f"{name}: parameter {param}")
        json.dumps(schema)
    assert not problems, problems


def test_starter_profile_is_small_and_keeps_the_way_to_everything():
    names = list(_tools())
    starter = profiles.apply_profile(names, ["starter"])
    assert len(starter) <= MAX_TOOLS_PER_REQUEST and len(starter) <= 30, len(starter)
    for needed in ("router_call_tool", "router_list_tools", "router_workflow", "router_doctor", "scene_safe_edit"):
        assert needed in starter, needed


def test_read_only_tools_say_so_and_editing_tools_do_not():
    # Codex asks before every MCP call unless the tool is annotated read-only; edits must keep asking.
    from omniverse_scene_mcp.help import CHANGES_STATE, TOOL_CATALOG

    tools = _tools()
    mutating = {entry["name"] for entries in TOOL_CATALOG.values() for entry in entries if entry["mutates"]}
    for name, tool in tools.items():
        hint = getattr(tool.annotations, "readOnlyHint", None) if tool.annotations else None
        if name in mutating or name in CHANGES_STATE:
            assert hint is not True, f"{name} changes things but claims read-only"
    for name in ("scene_summary", "router_doctor", "router_workflow", "learning_isaac_lab", "learning_exam_week"):
        assert tools[name].annotations and tools[name].annotations.readOnlyHint is True, name
    for name in ("scene_safe_edit", "scene_move", "router_call_tool", "scene_save", "learning_exam_answer"):
        assert not (tools[name].annotations and tools[name].annotations.readOnlyHint), name


def test_every_workflow_is_reachable_without_mcp_prompt_support():
    # Codex does not show MCP prompts; router_workflow must render each one, naming only real tools.
    names = set(_tools())
    for name in workflows.prompt_names():
        text = workflows.render_prompt(name)
        assert text and "Safety rules" in text, name
        mentioned = set(re.findall(r"\b((?:scene|learning|router|robot|sensor|sim|class|variant|usd|python|"
                                   r"homework|assignment|project|kit|asset|render|sdg|dataset|graph|event)_[a-z_]+)\(",
                                   text))
        assert mentioned <= names, (name, sorted(mentioned - names))
