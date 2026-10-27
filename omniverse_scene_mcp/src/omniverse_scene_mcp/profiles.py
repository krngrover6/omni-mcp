"""Tool profiles and compact results: make the router cheap to use from any MCP client.

Why this exists: tool names, descriptions and argument schemas add context for clients that expose them to a model.
Client caching and discovery differ. A *profile* is a named, smaller tool set; the server registers all
tools as before and then keeps only the profile's tools in the list clients see.

Pure helpers (no FastMCP, no network, no files):

* ``PROFILES``: profile name -> tuple of tool names or fnmatch patterns ("scene_layout_*"). Names that are not
  registered (yet) are tolerated, so a profile can name tools other modules will add later.
* ``resolve_profile(names, available_tools)`` -> {"tools", "unknown", "missing_groups", ...}.
* ``profile_from_env(environ)`` reads ``OMNIVERSE_MCP_PROFILE`` (comma list; default "starter").
* ``apply_profile(tool_names_in_registration_order, profile)`` -> the names to keep, in registration order.
* ``estimate_schema_tokens(tool_schemas)`` / ``estimate_profile_tokens(...)``: rough chars/4 cost of a tool list.
* ``compact_result(result, ...)`` and ``compact_enabled(environ)`` (``OMNIVERSE_MCP_COMPACT=1``).

FastMCP glue (optional; duck-typed against mcp 1.28's FastMCP, tolerant of other versions):

* ``prune_app_tools(app, keep)`` removes non-profile tools from the list clients see and returns a ``ToolStash``
  holding them, so a meta tool can still call them (``ToolStash.call``) or re-list them (``ToolStash.enable``).
* ``install_compact_outputs(app)`` wraps each tool so its dict result goes through ``compact_result``.

Every profile always includes ``ALWAYS_ON`` (the natural-language front door, help and status), so a client with a
tiny profile can discover and call other tools through the always-on meta tools.
"""

from __future__ import annotations

import fnmatch
import functools
import inspect
import json
import os
from typing import Any, Iterable, Mapping, Sequence

PROFILE_ENV = "OMNIVERSE_MCP_PROFILE"
COMPACT_ENV = "OMNIVERSE_MCP_COMPACT"
DEFAULT_PROFILE = "starter"
_TRUE = {"1", "true", "yes", "on"}

# Kept in every profile (when registered). router_enable_tools / router_list_tools / router_call_tool are the
# meta tools (see docs/USING_WITH_CODEX_AND_CLAUDE_PRO.md).
ALWAYS_ON: tuple[str, ...] = (
    "router_dispatch",
    "router_route",
    "router_help",
    "router_status",
    "router_enable_tools",
    "router_list_tools",
    "router_call_tool",
)

PROFILES: dict[str, tuple[str, ...]] = {
    # ~18 tools: ask in plain English (router_dispatch), look at the scene, explain things, undo.
    "starter": (
        "router_dispatch",
        "router_route",
        "router_help",
        "router_status",
        "router_health",
        "router_set_scene_target",
        "scene_capabilities",
        "scene_summary",
        "scene_inspect",
        "scene_children",
        "scene_find",
        "scene_explain",
        "explain_error",
        "python_show",
        "learning_today",
        "scene_undo_last",
        "usd_knowledge",
        "workspace_context",
    ),
    # Exam prep, practice, drills, Python lessons, homework and class notes.
    "study": (
        "learning_today",
        "learning_reminder_setup",
        "learning_exam_*",
        "learning_mock_exam_*",
        "learning_composition_puzzle*",
        "learning_drill_*",
        "learning_practice*",
        "learning_tutor",
        "learning_scene_quiz",
        "learning_study_mode",
        "learning_translate",
        "learning_history",
        "learning_cheatsheet",
        "learning_roadmap",
        "learning_glossary",
        "usd_playground_compose",
        "usd_knowledge",
        "usd_docs_search",
        "python_*",
        "explain_error",
        "assignment_*",
        "class_*",
        "scene_explain",
        "scene_tour",
        "scene_why_value",
        "scene_what_if",
        "workspace_learning_index",
    ),
    # Editing and authoring: prims, transforms, layout, materials, lights, cameras, layers, checkpoints, undo.
    "build": (
        "animation_*",
        "variant_*",
        "scene_summary",
        "scene_inspect",
        "scene_children",
        "scene_find",
        "scene_select",
        "scene_create",
        "scene_delete",
        "scene_duplicate",
        "scene_reparent",
        "scene_move",
        "scene_rename",
        "scene_set_attribute",
        "scene_save",
        "scene_layout_*",
        "scene_material",
        "scene_create_material",
        "scene_bind_material",
        "scene_create_light",
        "scene_create_camera",
        "scene_aim",
        "scene_look_at",
        "scene_variants",
        "scene_variant_set",
        "scene_properties",
        "scene_bounds",
        "scene_stage_info",
        "scene_composition",
        "scene_explain_value",
        "scene_why_value",
        "scene_what_if",
        "usd_validate",
        "scene_layers",
        "scene_where_is",
        "scene_flatten_preview",
        "scene_add_sublayer",
        "scene_remove_sublayer",
        "scene_set_edit_target",
        "scene_mute_layer",
        "scene_unmute_layer",
        "scene_move_opinions",
        "scene_checkpoint*",
        "scene_diff",
        "scene_restore_checkpoint",
        "scene_prune_checkpoints",
        "scene_undo_*",
        "scene_journal",
        "scene_journal_explain",
        "scene_journal_summary",
        "asset_search",
        "asset_info",
        "scene_add_asset",
        "scene_capture_viewport",
        "scene_capture_status",
        "project_templates",
        "project_plan",
        "project_create",
        "project_list",
    ),
    # Physics, robots, sensors, joints, pick-and-place, ROS 2 (several of these do not exist yet; tolerated).
    "robotics": (
        "robot_*",
        "sim_*",
        "scene_physics_report",
        "scene_add_physics_scene",
        "scene_make_*",
        "scene_remove_physics",
        "*physics*",
        "*joint*",
        "*sensor*",
        "*lidar*",
        "*gripper*",
        "*articulation*",
        "*pick_place*",
        "*pick_and_place*",
        "*conveyor*",
        "*ros2*",
        "isaac_sim_knowledge",
        "scene_inspect",
        "scene_properties",
        "scene_bounds",
        "scene_capture_viewport",
        "scene_capture_status",
        "scene_undo_last",
    ),
    # Assets, import, health, performance, variants, OmniGraph, message bus, animation, reports, projects, logs.
    "pipeline": (
        "dataset_*", "sdg_*", "semantic_*", "render_*", "blender_*", "extension_*", "rubric_*",
        "event_*",
        "animation_*",
        "asset_*",
        "scene_add_asset",
        "usd_asset_health",
        "usd_health_checks",
        "*import*",
        "*perf*",
        "*instanc*",
        "scene_variant*",
        "*variant*",
        "*omnigraph*",
        "*action_graph*",
        "graph_*",
        "*message_bus*",
        "bus_*",
        "scene_animation_*",
        "*keyframe*",
        "*timeline*",
        "*turntable*",
        "project_*",
        "scene_journal_to_script",
        "kit_log*",
        "kit_explain_recent_errors",
        "docs_docker_*",
        "docs_search",
        "extension_info",
        "kit_knowledge",
        "omni_ui_knowledge",
        "usd_validate",
    ),
    "all": ("*",),
}

# Short aliases people are likely to type.
PROFILE_ALIASES: dict[str, str] = {
    "full": "all",
    "everything": "all",
    "default": "starter",
    "minimal": "starter",
    "basic": "starter",
    "learn": "study",
    "exam": "study",
    "edit": "build",
    "author": "build",
    "robot": "robotics",
    "isaac": "robotics",
    "assets": "pipeline",
}

_GLOB_CHARS = set("*?[")


def _is_pattern(name: str) -> bool:
    return any(ch in _GLOB_CHARS for ch in name)


def _split(names: Iterable[str] | str | None) -> list[str]:
    if names is None:
        return []
    if isinstance(names, str):
        names = [names]
    tokens: list[str] = []
    for item in names:
        for part in str(item).split(","):
            part = part.strip()
            if part and part not in tokens:
                tokens.append(part)
    return tokens


def profile_names() -> list[str]:
    """The defined profile names (aliases not included)."""
    return list(PROFILES)


def resolve_profile(names: Sequence[str] | str, available_tools: Iterable[str]) -> dict[str, Any]:
    """Expand profile names (and/or explicit tool names or patterns) against the tools that really exist.

    ``names`` is a profile name, a comma string ("starter,study") or a list. A token that is a profile (or alias)
    expands to that profile; a token that is not a profile but contains "_" or a glob character is treated as an
    explicit tool name / pattern ("scene_layers", "scene_layout_*"), so ``OMNIVERSE_MCP_PROFILE=starter,scene_layers``
    works. ALWAYS_ON tools are added whenever they exist.

    Returns:
        tools: sorted names to keep (only existing tools);
        unknown: explicitly listed tool names (not patterns) that are not registered;
        unmatched_patterns: patterns that matched nothing (normal for tools that do not exist yet);
        missing_groups: tokens that are neither a profile nor a plausible tool name;
        profiles: the profile names that were applied, in order.
    """
    available = set(available_tools)
    keep: set[str] = {name for name in ALWAYS_ON if name in available}
    unknown: list[str] = []
    unmatched: list[str] = []
    missing_groups: list[str] = []
    applied: list[str] = []

    for token in _split(names):
        key = token.lower()
        key = PROFILE_ALIASES.get(key, key)
        if key in PROFILES:
            if key not in applied:
                applied.append(key)
            entries: Sequence[str] = PROFILES[key]
        elif token in available or "_" in token or _is_pattern(token):
            entries = (token,)
        else:
            if token not in missing_groups:
                missing_groups.append(token)
            continue
        for entry in entries:
            if _is_pattern(entry):
                matched = {name for name in available if fnmatch.fnmatchcase(name, entry)}
                keep |= matched
                if not matched and entry not in unmatched:
                    unmatched.append(entry)
            elif entry in available:
                keep.add(entry)
            elif entry not in unknown:
                unknown.append(entry)
    return {
        "tools": sorted(keep),
        "unknown": unknown,
        "unmatched_patterns": unmatched,
        "missing_groups": missing_groups,
        "profiles": applied,
    }


def profile_from_env(environ: Mapping[str, str] | None = None) -> list[str]:
    """The requested profile tokens from ``OMNIVERSE_MCP_PROFILE`` (comma list). Empty/unset -> ["starter"]."""
    env = os.environ if environ is None else environ
    tokens = _split(env.get(PROFILE_ENV, ""))
    return tokens or [DEFAULT_PROFILE]


def apply_profile(tool_names_in_registration_order: Sequence[str], profile: Sequence[str] | str) -> list[str]:
    """Names to keep, in registration order (no duplicates). ``set(...)`` it if you only need membership.

    Backward compatible: the "all" profile (the env default) keeps every tool. If the profile resolves to no tools
    at all (every token misspelled), everything is kept rather than leaving the client with an empty server.
    """
    order: list[str] = []
    for name in tool_names_in_registration_order:
        if name not in order:
            order.append(name)
    resolved = resolve_profile(profile, order)
    keep = set(resolved["tools"])
    if not keep:
        return order
    return [name for name in order if name in keep]


# ------------------------------------------------------------------------------------------------ token estimates


def _schema_chars(schema: Mapping[str, Any]) -> int:
    return len(json.dumps(schema, separators=(",", ":"), ensure_ascii=False, default=str))


def estimate_schema_tokens(tool_schemas: Iterable[Mapping[str, Any]]) -> int:
    """Rough token cost of listing these tools to a model: compact JSON characters / 4.

    Each schema is what tools/list returns per tool ({name, description, inputSchema[, outputSchema]}). Real
    tokenizers differ by model; use the number to compare profiles, not as an exact bill.
    """
    chars = sum(_schema_chars(schema) for schema in tool_schemas)
    return (chars + 3) // 4


def estimate_profile_tokens(
    tool_schemas: Sequence[Mapping[str, Any]],
    profiles: Iterable[Sequence[str] | str] | None = None,
) -> list[dict[str, Any]]:
    """One row per profile: {profile, tools, tokens, percent_of_all}. ``profiles`` defaults to every PROFILES key
    plus the combination "starter,study"."""
    by_name = {str(schema.get("name")): schema for schema in tool_schemas}
    order = list(by_name)
    total = estimate_schema_tokens(by_name.values()) or 1
    wanted = list(profiles) if profiles is not None else [*PROFILES, "starter,study"]
    rows = []
    for profile in wanted:
        keep = apply_profile(order, profile)
        tokens = estimate_schema_tokens(by_name[name] for name in keep)
        label = profile if isinstance(profile, str) else ",".join(profile)
        rows.append({"profile": label, "tools": len(keep), "tokens": tokens, "percent_of_all": round(100 * tokens / total)})
    return rows


# -------------------------------------------------------------------------------------------------- compact output

DEFAULT_DROP_KEYS: tuple[str, ...] = ("glossary", "python", "preview")
_MAX_DEPTH = 12


def compact_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when ``OMNIVERSE_MCP_COMPACT`` is 1/true/yes/on."""
    env = os.environ if environ is None else environ
    return str(env.get(COMPACT_ENV, "")).strip().lower() in _TRUE


def compact_result(
    result: Any,
    *,
    max_list: int = 10,
    max_str: int = 400,
    drop_keys: Iterable[str] = DEFAULT_DROP_KEYS,
    keep_keys: Iterable[str] = (),
) -> Any:
    """Return a trimmed copy of a tool result (the input is never modified).

    * lists/tuples longer than ``max_list`` keep their first ``max_list`` items plus a "... N more" string;
    * strings longer than ``max_str`` are cut and end with "... [cut N chars]";
    * keys in ``drop_keys`` are removed at every level unless also in ``keep_keys`` (pass keep_keys=("python",)
      when the user asked for the Python);
    * when anything was trimmed, a top-level dict gets ``_trimmed``: a one-line note saying what and how to get
      the full result.
    """
    drop = set(drop_keys) - set(keep_keys)
    stats = {"lists": 0, "strings": 0, "dropped": set()}

    def walk(value: Any, depth: int) -> Any:
        if depth > _MAX_DEPTH:
            return value
        if isinstance(value, Mapping):
            out = {}
            for key, item in value.items():
                if key in drop:
                    stats["dropped"].add(str(key))
                    continue
                out[key] = walk(item, depth + 1)
            return out
        if isinstance(value, (list, tuple)):
            items = [walk(item, depth + 1) for item in value[:max_list]]
            if len(value) > max_list:
                stats["lists"] += 1
                items.append(f"... {len(value) - max_list} more")
            return items
        if isinstance(value, str) and len(value) > max_str:
            stats["strings"] += 1
            return value[:max_str] + f"... [cut {len(value) - max_str} chars]"
        return value

    trimmed = walk(result, 0)
    if isinstance(trimmed, dict) and (stats["lists"] or stats["strings"] or stats["dropped"]):
        parts = []
        if stats["lists"]:
            parts.append(f"{stats['lists']} list(s) cut to {max_list} items")
        if stats["strings"]:
            parts.append(f"{stats['strings']} long text(s) cut to {max_str} chars")
        if stats["dropped"]:
            parts.append("dropped: " + ", ".join(sorted(stats["dropped"])))
        trimmed["_trimmed"] = (
            "Compact mode (OMNIVERSE_MCP_COMPACT=1): " + "; ".join(parts) + ". Ask for the full result "
            "(for example show_python=True, or run the router without OMNIVERSE_MCP_COMPACT) if you need it."
        )
    return trimmed


# --------------------------------------------------------------------------------------- optional FastMCP glue


def _tool_registry(app: Any) -> dict[str, Any] | None:
    """FastMCP keeps tools in app._tool_manager._tools (name -> Tool) in mcp 1.x. None when not found."""
    manager = getattr(app, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    return tools if isinstance(tools, dict) else None


def registered_tool_names(app: Any) -> list[str]:
    """Tool names in registration order (empty when the app does not look like FastMCP)."""
    tools = _tool_registry(app)
    return list(tools) if tools is not None else []


class ToolStash:
    """Tools hidden from tools/list but kept in memory, so they can be called or re-listed later."""

    def __init__(self, app: Any, hidden: dict[str, Any]):
        self.app = app
        self.hidden = hidden

    def names(self) -> list[str]:
        return list(self.hidden)

    def describe(self, pattern: str = "*") -> list[dict[str, str]]:
        """Name and first description line of hidden tools matching an fnmatch pattern (cheap to show a model)."""
        rows = []
        for name, tool in self.hidden.items():
            if fnmatch.fnmatchcase(name, pattern):
                first = (getattr(tool, "description", "") or "").strip().splitlines()
                rows.append({"name": name, "about": first[0] if first else ""})
        return rows

    def enable(self, names_or_profile: Sequence[str] | str) -> list[str]:
        """Put hidden tools back into tools/list (profile names, tool names or patterns). Returns names enabled.

        The caller must then notify clients (``await ctx.session.send_tool_list_changed()``) so they re-list.
        """
        registry = _tool_registry(self.app)
        if registry is None:
            return []
        wanted = resolve_profile(names_or_profile, self.hidden)["tools"]
        enabled = []
        for name in wanted:
            tool = self.hidden.pop(name, None)
            if tool is not None:
                registry[name] = tool
                enabled.append(name)
        return enabled

    async def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Run a hidden (or listed) tool by name with its normal argument validation; returns its raw result."""
        tool = self.hidden.get(name)
        if tool is None:
            registry = _tool_registry(self.app) or {}
            tool = registry.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        return await tool.run(dict(arguments or {}))


def prune_app_tools(app: Any, keep: Iterable[str]) -> ToolStash:
    """Hide every registered tool not in ``keep`` from tools/list; the hidden Tool objects go into a ToolStash."""
    registry = _tool_registry(app)
    keep_set = set(keep)
    hidden: dict[str, Any] = {}
    if registry is None:
        return ToolStash(app, hidden)
    for name in list(registry):
        if name not in keep_set:
            hidden[name] = registry.pop(name)
    return ToolStash(app, hidden)


def apply_profile_to_app(app: Any, environ: Mapping[str, str] | None = None) -> tuple[ToolStash, dict[str, Any]]:
    """Read OMNIVERSE_MCP_PROFILE and prune the app's tool list. Returns (stash, report). "all" changes nothing."""
    requested = profile_from_env(environ)
    order = registered_tool_names(app)
    keep = apply_profile(order, requested)
    resolved = resolve_profile(requested, order)
    stash = prune_app_tools(app, keep)
    report = {
        "requested": requested,
        "listed": len(keep),
        "hidden": len(stash.hidden),
        "unknown": resolved["unknown"],
        "missing_groups": resolved["missing_groups"],
    }
    return stash, report


def advertise_tool_list_changed(app: Any) -> bool:
    """Make the server advertise ``tools.listChanged = true`` so clients act on notifications/tools/list_changed.

    FastMCP (mcp 1.28.1) calls ``create_initialization_options()`` without NotificationOptions for stdio, SSE and
    streamable HTTP, so it advertises listChanged=false. This wraps that method on the low-level server. Returns
    False (and changes nothing) when the app or the SDK does not look as expected.
    """
    low = getattr(app, "_mcp_server", None)
    original = getattr(low, "create_initialization_options", None)
    if original is None or getattr(original, "_omniverse_list_changed", False):
        return bool(original)
    try:
        from mcp.server.lowlevel.server import NotificationOptions
    except ImportError:
        return False

    def create_initialization_options(notification_options: Any = None, experimental_capabilities: Any = None) -> Any:
        options = notification_options or NotificationOptions(tools_changed=True)
        return original(options, experimental_capabilities)

    create_initialization_options._omniverse_list_changed = True  # type: ignore[attr-defined]
    low.create_initialization_options = create_initialization_options
    return True


def _wrap_compact(fn: Any, is_async: bool, environ: Mapping[str, str] | None, options: dict[str, Any]) -> Any:
    def compact(result: Any, kwargs: Mapping[str, Any]) -> Any:
        if not isinstance(result, dict) or not compact_enabled(environ):
            return result
        keep = ("python",) if kwargs.get("show_python") else ()
        return compact_result(result, keep_keys=keep, **options)

    if is_async:

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            return compact(await fn(*args, **kwargs), kwargs)

        async_wrapper._omniverse_compact = True  # type: ignore[attr-defined]
        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return compact(fn(*args, **kwargs), kwargs)

    wrapper._omniverse_compact = True  # type: ignore[attr-defined]
    return wrapper


# Tools whose whole point is long text or code: never compacted.
COMPACT_EXEMPT: tuple[str, ...] = (
    "python_show",
    "python_practice_get",
    "workspace_document",
    "workspace_context",
    "learning_glossary",
    "learning_cheatsheet",
    "scene_journal_to_script",
    "project_report",
    "router_help",
    "router_list_tools",
    "router_call_tool",
    "robot_pick_place_script",
    "sdg_script",
    "blender_export_plan",
)


def install_compact_outputs(
    app: Any,
    *,
    environ: Mapping[str, str] | None = None,
    exempt: Iterable[str] = COMPACT_EXEMPT,
    **options: Any,
) -> list[str]:
    """Wrap every registered tool (except ``exempt``) so dict results are compacted while COMPACT is on.

    The env var is read on every call, so the switch can change without re-registering. Returns wrapped names.
    Call it AFTER all tools are registered (journal wrapping happens at registration) and BEFORE prune_app_tools,
    so hidden tools are wrapped too. Calling it twice never wraps a tool twice.
    """
    registry = _tool_registry(app)
    if registry is None:
        return []
    skip = set(exempt)
    wrapped = []
    for name, tool in registry.items():
        fn = getattr(tool, "fn", None)
        if name in skip or fn is None or getattr(fn, "_omniverse_compact", False):
            continue
        is_async = bool(getattr(tool, "is_async", inspect.iscoroutinefunction(fn)))
        try:
            tool.fn = _wrap_compact(fn, is_async, environ, options)
        except (AttributeError, TypeError, ValueError):
            continue
        wrapped.append(name)
    return wrapped
