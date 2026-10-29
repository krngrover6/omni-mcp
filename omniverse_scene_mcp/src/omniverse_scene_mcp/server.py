from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError

from . import assignments, docker_docs, error_explainer, physics, python_links, python_teacher, usd_insight, usd_validate
from . import robot_inspector
from . import scene_tour as scene_tour_module
from . import asset_health as asset_health_module
from . import checkpoints, journal_script, layer_manager, project_starter
from . import kit_logs as kit_log_reader
from . import project_report as project_report_module
from .bridge import (
    ASSET_RPC_METHODS,
    CAPTURE_RPC_METHODS,
    EDIT_RPC_METHODS,
    HEALTH_RPC_METHODS,
    HOMEWORK_RPC_METHODS,
    LAYER_RPC_METHODS,
    LAYOUT_RPC_METHODS,
    LIVE_RPC_METHODS,
    PHYSICS_RPC_METHODS,
    ROBOT_RPC_METHODS,
    RPC_VERSION,
    SIMULATION_RPC_METHODS,
    UNDO_RPC_METHODS,
)
from .exam import bank as exam_bank
from .exam import blueprint as exam_blueprint
from .exam import daily as exam_daily
from .exam import drills as exam_drills
from .exam import puzzles as exam_puzzles
from .exam import python_practice
from .exam import reminder as exam_reminder
from .exam import sandbox as exam_sandbox
from .class_notes import ClassNotes
from .exam.mock import MockExamError
from .file_stage import ReadOnlyViolation, SceneFileError, readonly_stage, resolve_scene_file
from .health import router_health as check_router_health
from .journal import build_change_journal, install_journal, target_from_backend
from .offline_docs_knowledge import ENV_ROOT as DOCS_ENV_ROOT
from .offline_docs_knowledge import normalize_domain as normalize_docs_domain
from .offline_knowledge import OFFLINE_NOTE, OfflineUSDKnowledge
from .router import MOCK_EXAM_DEFAULTS, OmniverseRouter, RouteTarget, _local_usd_file
from .runtime import (
    build_docs_knowledge,
    build_mock_store,
    build_progress_store,
    build_router,
    exam_dashboard_result,
    scene_targets,
    switch_scene_target,
)


def _coerce_vector(values: Sequence[float] | None) -> tuple[float, float, float] | None:
    if values is None:
        return None
    return (float(values[0]), float(values[1]), float(values[2]))


def _decision_dict(result) -> dict:
    return {
        "target": result.decision.target.value,
        "confidence": result.decision.confidence,
        "reason": result.decision.reason,
        "normalized_request": result.decision.normalized_request,
    }


def _workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def _read_workspace_document(name: str) -> str:
    allowed = {
        "context": "OMNIVERSE_WORKSPACE_CONTEXT.md",
        "new_project_template": "NEW_PROJECT_CONTEXT_TEMPLATE.md",
        "mcp_cheatsheet": "USD_COMPOSER_MCP_CHEATSHEET.md",
    }
    filename = allowed.get(name)
    if filename is None:
        raise ValueError(f"Unknown workspace document: {name}")
    path = _workspace_root() / filename
    return path.read_text(encoding="utf-8")


ZAZ_ANIMATION_ACTIONS = {
    "closed_pose": {"mode": "pose", "frame": 160.0, "description": "Closed panels and centered steering"},
    "open_pose": {"mode": "clip", "start": 1240.0, "end": 1408.0, "description": "Open both doors and both hoods together, then hold four seconds"},
    "open_hold": {"mode": "pose", "frame": 1408.0, "description": "Hold both doors and both hoods open"},
    "open_doors": {"mode": "clip", "start": 1440.0, "end": 1608.0, "description": "Open both doors together, then hold four seconds"},
    "hold_doors": {"mode": "pose", "frame": 1608.0, "description": "Hold both doors open with both hoods closed"},
    "open_hoods": {"mode": "clip", "start": 1640.0, "end": 1808.0, "description": "Open front and rear hoods together, then hold four seconds"},
    "hold_hoods": {"mode": "pose", "frame": 1808.0, "description": "Hold both hoods open with both doors closed"},
    "steering_demo": {"mode": "clip", "start": 0.0, "end": 160.0, "description": "Front-wheel and steering-wheel motion"},
    # Backward-compatible alias retained for existing MCP prompts and clients.
    "open_everything": {"mode": "clip", "start": 160.0, "end": 480.0, "description": "Open doors, front trunk, and rear hood"},
    "close_everything": {"mode": "clip", "start": 688.0, "end": 960.0, "description": "Close trunk, hood, and both doors"},
    "wheel_spin": {"mode": "clip", "start": 960.0, "end": 1200.0, "description": "Spin all four road wheels"},
    "full_demo": {"mode": "clip", "start": 0.0, "end": 1808.0, "description": "Run the complete ZAZ animation demo"},
}

SAVED_FILE_NOTE = (
    "File mode: this reads the SAVED USD file on disk, opened read-only. Unsaved edits in the live "
    "Composer/Isaac Sim stage are not included; save the scene first (scene_save) to see them."
)
_FAKE_SCENE_MODES = {"test", "fake", "fake_fallback"}
_PUZZLE_ID = re.compile(r"^liverps-(easy|medium|hard)-(\d{1,9})$")
_LIVERPS_KEYWORDS = {"liverps", "livrps", "strength", "strength ordering"}
_QUIZ_ANSWER_HINT = (
    "Answer with learning_exam_answer(question_id, chosen=[...]) using 0-based option indices or letters; "
    "multi-select questions need every correct option."
)
_PUZZLE_ANSWER_HINT = "Answer with learning_composition_puzzle_answer(puzzle_id, answer) using the option text or letter A-E."

# Insight tools read the LIVE stage through the bridge's 'insight' RPC, or the SAVED file read-only.
INSIGHT_MODES = ("auto", "live", "saved_file")
LIVE_ENABLE_STEPS = (
    "Isaac Sim: run C:/Users/krngr/Omniverse/isaacsim-study/reload_mcp_bridge.py in Isaac Sim's Script Editor; it "
    "runs self-tests on Isaac's USD and then restarts the scene bridge on port 9911 with the extended RPC "
    f"(isaacsim-study/bridge-reload-result.json should say status 'reloaded' and rpc_version '{RPC_VERSION}').",
    "Composer: run C:/Users/krngr/Omniverse/composer-study/reload_composer_bridge.py in Composer's Script Editor (or "
    "relaunch with start-composer-study.ps1); the extended bridge then serves 9910 and the stock one moves to 9909 "
    "(composer-study/bridge-reload-result.json says what happened).",
    "Router: restart it so it loads this code, or switch its scene target without a restart: "
    "router_set_scene_target(target='isaac') for 9911 or router_set_scene_target(target='composer') for 9910.",
)
_EDIT_PREVIEW = (
    "Preview only: nothing in the open scene changed. Show this plan to the user and apply it (same arguments plus "
    "dry_run=False) only after they agree."
)
_UNDO_PREVIEW = (
    "Preview only: nothing was undone. Show the user what would be undone and call scene_undo_last(dry_run=False) "
    "only after they agree."
)
_DOCS_NOTE = (
    "Offline search of the installed Isaac Sim / Kit extension docs (read-only, no network); the Kit, OmniUI and "
    "Isaac Sim docs MCPs are not needed."
)
MOCK_PAGE_SIZE = 10
MOCK_MAX_PAGE = 25
MOCK_MAX_OUTPUT_CHARS = 60_000
_MOCK_HOW_TO = (
    "Answer with learning_mock_exam_answer(session_id, question_id, chosen) using letters or 0-based indices (you can "
    "change answers, and chosen=[] clears one); learning_mock_exam_questions(session_id, offset) pages through the "
    "questions; learning_mock_exam_flag marks one to revisit; learning_mock_exam_status shows the time left; "
    "learning_mock_exam_finish(session_id) scores it and saves the answered questions to your exam progress."
)
_MOCK_TOOLS_HINT = (
    "Mock tools: learning_mock_exam_status(), learning_mock_exam_questions(session_id), learning_mock_exam_answer(...), "
    "learning_mock_exam_finish(session_id) or learning_mock_exam_abandon(session_id)."
)
_AUTHORING_PREVIEW = (
    "Preview only: nothing in the open scene changed. Show this plan to the user and apply it (same arguments plus "
    "dry_run=False) only after they agree."
)
_AUTHORING_APPLIED = "Applied to the user's open scene in memory: it is not saved (scene_save keeps it)."
_APPLIED_UNDOABLE = " scene_undo_last undoes it (preview first); saving is not undoable."
_APPLIED_NOT_RECORDED = (
    " This bridge did not record it for undo, so change it back with another edit if it is wrong."
)


def _error(message: str, **extra: Any) -> dict:
    return {"ok": False, "message": message, **extra}


def _numbers(values: Any, length: int, label: str) -> tuple[float, ...] | None:
    """[x, y, ...] of exactly ``length`` finite numbers as a float tuple; None passes through."""
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) != length:
        raise ValueError(f"{label} must be a list of {length} numbers")
    numbers = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{label} must be a list of {length} finite numbers")
        numbers.append(float(value))
    return tuple(numbers)


def _target_arg(value: Any, label: str) -> str | tuple[float, ...] | None:
    """A prim path string or an [x, y, z] world point."""
    if value is None or isinstance(value, str):
        return value.strip() if isinstance(value, str) and value.strip() else None
    return _numbers(value, 3, label)


def _size_arg(value: Any) -> float | tuple[float, ...] | None:
    """One number, or a pair: (width, height) for rect lights and (length, radius) for cylinder lights."""
    if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return None if value is None else float(value)
    return _numbers(value, 2, "size")


def _live_support(capabilities: dict, query: str | None = None) -> dict[str, Any]:
    """Whether the bridge can run live insight (one query, or any when None), why not, and if a reload would help."""
    if not capabilities.get("ok"):
        if capabilities.get("needs_bridge_update"):
            return {"available": False, "needs_bridge_update": True, "reason": capabilities.get("message")}
        return {
            "available": False,
            "needs_bridge_update": False,
            "reason": f"the live scene bridge did not answer: {capabilities.get('message') or 'no details'}",
        }
    if capabilities.get("fake_scene"):
        return {
            "available": False,
            "needs_bridge_update": False,
            "reason": "the router is on its in-memory fake scene, which has no live USD stage",
        }
    queries = list(capabilities.get("insight_queries") or [])
    if not queries or (query is not None and query not in queries):
        wanted = f"the '{query}' insight query" if query else "live insight"
        return {
            "available": False,
            "needs_bridge_update": True,
            "reason": f"the live bridge ({capabilities.get('bridge') or 'unknown'}, version "
            f"{capabilities.get('version') or 'unknown'}) does not offer {wanted}; reload it",
        }
    if capabilities.get("stage_open") is False:
        return {"available": False, "needs_bridge_update": False, "reason": "no stage is open in the live app"}
    return {"available": True, "needs_bridge_update": False, "reason": None}


def _feature_support(
    capabilities: dict, insight: dict, key: str, feature_methods: Sequence[str], label: str
) -> dict[str, Any]:
    """Whether the bridge serves one tier3-1 feature (edits or undo), from its capabilities; never calls it."""
    methods = set(capabilities.get("methods") or [])
    info = capabilities.get(key) if isinstance(capabilities.get(key), dict) else {}
    if not capabilities.get("ok"):
        support: dict[str, Any] = {"available": False, "reason": insight["reason"]}
    elif capabilities.get("fake_scene"):
        available = bool(info.get("available"))
        support = {
            "available": available,
            "in_memory": True,
            "reason": "in-memory fake scene only: nothing reaches Composer or Isaac Sim" if available
            else "the in-memory fake scene does not offer this",
        }
    elif not set(feature_methods) <= methods:
        support = {"available": False, "reason": f"the live bridge predates {label}; reload it"}
    elif info and not info.get("available"):
        support = {"available": False, "reason": info.get("error") or f"the bridge could not load {label}"}
    else:
        support = {"available": True, "reason": None}
    if key == "undo" and info.get("kit_ctrl_z") is not None:
        support["kit_ctrl_z"] = bool(info.get("kit_ctrl_z"))
    support["methods"] = list(feature_methods)
    support["dry_run_default"] = True
    return support


def _insight_mode(
    router: OmniverseRouter,
    mode: str | None,
    file: str | None,
    query: str,
    args: dict[str, Any],
    action: Callable[[Any], dict],
) -> dict:
    """Run one insight/validation query on the live stage or the saved file; the result always names its mode.

    auto: an explicit file means the saved file; otherwise the live bridge when its capabilities offer the query,
    else the saved file plus a fallback note. live / saved_file force one source.
    """
    wanted = mode.strip().lower() if isinstance(mode, str) and mode.strip() else "auto"
    if wanted not in INSIGHT_MODES:
        return _error(f"mode must be one of {list(INSIGHT_MODES)}, not {mode!r}", mode_requested=mode, query=query)
    has_file = isinstance(file, str) and bool(file.strip())
    if wanted == "saved_file" or (wanted == "auto" and has_file):
        return {**_file_mode(router, file, action), "mode_requested": wanted}
    if has_file:
        return _error(
            "file=... names a saved file, but mode='live' reads the open stage; drop file or use mode='saved_file'.",
            mode="live",
            mode_requested="live",
            query=query,
        )
    support = _live_support(router._scene.live_capabilities(), query)
    if support["available"]:
        result = router._scene.live_insight(query, args)
        if wanted == "live" or not (result.get("unsupported") or result.get("needs_bridge_update")):
            return {**result, "mode": "live", "mode_requested": wanted}
        support = {
            "available": False,
            "needs_bridge_update": bool(result.get("needs_bridge_update")),
            "reason": result.get("message") or "the live bridge refused the insight query",
        }
    reason = str(support["reason"] or "unknown reason").rstrip(". ")
    if wanted == "live":
        return _error(
            f"Live insight is not available: {reason}.",
            mode="live",
            mode_requested="live",
            query=query,
            needs_bridge_update=support["needs_bridge_update"],
            how_to_enable=list(LIVE_ENABLE_STEPS) if support["needs_bridge_update"] else [],
        )
    result = _file_mode(router, None, action)
    return {
        **result,
        "mode_requested": "auto",
        "live_fallback": {"reason": reason, "needs_bridge_update": support["needs_bridge_update"]},
        "fallback_note": f"Live insight is not available ({reason}), so this read the SAVED file instead.",
    }


def _authoring_payload(router: OmniverseRouter, result: Any) -> dict:
    """Scene-tool envelope for a light/camera/aim result, with a message saying whether anything changed."""
    payload = router._wrap_operation_result("scene", result)
    inner = result.payload if isinstance(result.payload, dict) else {}
    for key in (
        "unsupported", "needs_bridge_update", "fake_scene", "warnings", "notes", "next_step", "error", "undoable",
        "fallback",
    ):
        if key in inner:
            payload[key] = inner[key]
    if not result.ok:
        payload["applied"] = False
        payload["message"] = inner.get("error") or inner.get("message") or "The live bridge could not author this."
        if inner.get("needs_bridge_update"):
            payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
    elif result.dry_run:
        payload["applied"] = False
        payload["preview"] = True
        payload["message"] = _AUTHORING_PREVIEW
    else:
        payload["applied"] = True
        recorded = inner.get("undoable") is True or bool(inner.get("fake_scene"))
        payload["message"] = _AUTHORING_APPLIED + (_APPLIED_UNDOABLE if recorded else _APPLIED_NOT_RECORDED)
    return payload


def _scale_arg(value: Any, label: str = "scale") -> float | tuple[float, ...] | None:
    """One number (a uniform scale or a gravity magnitude) or [x, y, z]."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError(f"{label} must be a finite number or a list of 3 numbers")
        return float(value)
    return _numbers(value, 3, label)


def _live_payload(result: dict) -> dict:
    """Scene envelope for a capture/simulation result; an outdated bridge also gets how_to_enable."""
    payload: dict[str, Any] = {"target": "scene", **result}
    if result.get("needs_bridge_update") and not result.get("fallback"):
        payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
    return payload


def _physics_report_mode(router: OmniverseRouter, prim_path: Any, file: Any, mode: Any) -> dict:
    """The physics report from the live stage or the saved file (read-only), like _insight_mode."""
    wanted = mode.strip().lower() if isinstance(mode, str) and mode.strip() else "auto"
    target = prim_path.strip() if isinstance(prim_path, str) and prim_path.strip() else None
    base = {"action": "physics_report", "mode_requested": wanted}
    if wanted not in INSIGHT_MODES:
        return _error(f"mode must be one of {list(INSIGHT_MODES)}, not {mode!r}", **base)
    has_file = isinstance(file, str) and bool(file.strip())

    def report(stage: Any) -> dict:
        return physics.physics_report(stage, target)

    if wanted == "saved_file" or (wanted == "auto" and has_file):
        return {**_file_mode(router, file, report), **base}
    if has_file:
        return _error("file=... names a saved file, but mode='live' reads the open stage; drop file or use "
                      "mode='saved_file'.", mode="live", **base)
    result = router._scene.physics_report(target)
    unavailable = bool(result.get("unsupported") or result.get("needs_bridge_update"))
    if not unavailable or wanted == "live":
        payload = {**result, "mode": "live", **base}
        if result.get("needs_bridge_update"):
            payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
        return payload
    reason = str(result.get("message") or "the live bridge cannot report physics").rstrip(". ")
    fallback = _file_mode(router, None, report)
    return {
        **fallback,
        **base,
        "live_fallback": {"reason": reason, "needs_bridge_update": bool(result.get("needs_bridge_update"))},
        "fallback_note": f"The live physics report is not available ({reason}), so this read the SAVED file instead.",
    }


def _with_python(payload: Any, show_python: Any, tool: str, **arguments: Any) -> Any:
    """With show_python=True, attach the matching OpenUSD snippet as payload['python']; never fails the edit."""
    if show_python is not True or not isinstance(payload, dict):
        return payload
    return {**payload, "python": python_links.python_for_tool(tool, arguments)}


def _dispatch_python(router: OmniverseRouter, request: str, result: Any) -> dict:
    """The OpenUSD snippet for a router_dispatch sentence (scene edits only); never raises."""
    if result.decision.target is not RouteTarget.SCENE:
        return {"ok": False, "error": "This request is not a scene edit, so there is no matching Python snippet; "
                "python_show(action, args) or 'show me the python for ...' explains one."}
    try:
        parsed = router._parse_scene_command(request)
        if parsed is None:
            parsed, _candidates = router._resolve_by_name(request)
    except Exception as exc:  # noqa: BLE001 - the snippet is a bonus
        return {"ok": False, "error": f"Could not read the command for the snippet: {type(exc).__name__}: {exc}"}
    return python_links.python_for_command(dict(parsed) if isinstance(parsed, dict) else None)


# The homework check and scene tour read the LIVE stage through the bridge (RPC tier5-1), or a saved file read-only.
_HOMEWORK_FILE_HINT = (
    "Pass scene_file='isaacsim-mentorship/session02/main.usda' (a path relative to the workspace, or absolute inside "
    "it) to check the saved file instead; save the scene first so it has your latest work."
)
_TOUR_MAX_DEPTH = 50
_TOUR_MAX_PRIMS = 20_000
_HOMEWORK_LABELS = {
    "assignment_check": "homework check",
    "scene_tour": "scene tour",
    "robot_inspect": "robot inspector",
    "layer_stack": "layer stack",
    "layer_where_is": "layer lookup",
    "layer_flatten_preview": "flatten preview",
    "asset_health": "asset health check",
}
_LAYER_FILE_HINT = (
    "Pass scene_file='my_project/root.usda' (a saved scene, relative to the workspace or absolute inside it) to read "
    "its layers read-only instead."
)
_HEALTH_FILE_HINT = (
    "Pass scene_file='Bag_Assets/bag.usd' (any asset or saved scene, relative to the workspace or absolute inside it) "
    "to check a file read-only instead."
)
_HEALTH_MAX_BUDGET_MS = 10_000
_FLATTEN_MIN_CHARS = 200
_FLATTEN_MAX_CHARS = 20_000
_CHECKPOINT_TIP = (
    "Tip: scene_checkpoint(note='...', dry_run=False) keeps a restorable copy of the saved scene files; "
    "scene_diff shows what changed since the last one."
)
_LOG_LEVELS = ("verbose", "info", "warning", "error", "fatal")
_ROBOT_FILE_HINT = (
    "Pass scene_file='isaacsim-assets-6.1.0/Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd' "
    "(any robot asset or saved scene, relative to the workspace or absolute inside it) to inspect a file instead."
)
_ROBOT_MAX_PRIMS = 50_000


def _homework_live_or_file(
    router: OmniverseRouter, method: str, live: dict, file_action: Callable[[Any], dict], hint: str = _HOMEWORK_FILE_HINT
) -> dict:
    """A tier5-1 live result, or (bridge too old / fake scene) the live scene's SAVED file read-only, like physics."""
    if not (live.get("unsupported") or live.get("needs_bridge_update")):
        return {**live, "mode": "live", "action": method}
    reason = str(live.get("message") or f"the live bridge cannot run {method}").rstrip(". ")
    label = _HOMEWORK_LABELS.get(method, method)
    needs_update = bool(live.get("needs_bridge_update"))
    fallback = _file_mode(router, None, file_action)
    payload: dict[str, Any] = {
        **fallback,
        "action": method,
        "live_fallback": {"reason": reason, "needs_bridge_update": needs_update},
        "needs_bridge_update": needs_update,
        "suggestion": hint,
    }
    if needs_update:
        payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
    if fallback.get("ok"):
        payload["fallback_note"] = f"The live {label} is not available ({reason}), so this read the SAVED file instead."
    else:
        payload["saved_file_error"] = fallback.get("message")
        payload["message"] = (
            f"The live {label} is not available ({reason}). "
            + ("Reload the bridge (see how_to_enable), or p" if needs_update else "P")
            + hint[1:]
        )
    return payload


def _whole_arg(value: Any, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label} must be a whole number from {low} to {high}")
    return value


def _color_arg(value: Any, label: str) -> tuple[float, ...] | str | None:
    """[r, g, b] numbers, or a color string such as '#ff8800' that the bridge converts."""
    if value is None or (isinstance(value, str) and value.strip()):
        return value.strip() if isinstance(value, str) else None
    return _numbers(value, 3, label)


def _scene_targets_view(router: OmniverseRouter) -> dict[str, Any]:
    """The named scene targets, which one the router uses now, and how to switch (no probes, no sockets)."""
    current = router._scene.bridge_info()
    targets = []
    for name, endpoint in scene_targets().items():
        if endpoint is None:
            active = current.get("bridge") == "FakeSceneBridge"
            app = "in-memory fake scene (offline; nothing reaches Composer or Isaac Sim)"
        else:
            active = current.get("endpoint") == endpoint
            app = "Isaac Sim scene bridge" if name == "isaac" else "USD Composer scene bridge (upgraded bridge)"
        targets.append({"name": name, "endpoint": endpoint, "app": app, "active": bool(active)})
    return {
        "ok": True,
        "targets": targets,
        "active": next((item["name"] for item in targets if item["active"]), None),
        "current": current,
        "how_to_switch": "router_set_scene_target(target='isaac' | 'composer' | 'fake'); add force=True to switch to "
        "an app that is not answering yet.",
        "note": "Read-only: nothing was probed or switched. router_health probes the ports.",
    }


def _mock_error(exc: Exception, **extra: Any) -> dict:
    return _error(str(exc) or type(exc).__name__, hint=_MOCK_TOOLS_HINT, **extra)


def _fit_mock_report(report: dict) -> dict:
    """Keep a finished mock's report under MOCK_MAX_OUTPUT_CHARS: trim per-option explanations, then stems."""
    if len(json.dumps(report, default=str)) <= MOCK_MAX_OUTPUT_CHARS:
        return report
    wrong = report.get("wrong") or []
    for row in wrong:
        for option in row.get("correct_options") or []:
            option.pop("why", None)
        row.pop("your_wrong_choices", None)
    trimmed = "Per-option explanations were left out to keep this report small"
    if len(json.dumps(report, default=str)) > MOCK_MAX_OUTPUT_CHARS:
        for row in wrong:
            row.pop("correct_options", None)
            row.pop("citations", None)
            stem = row.get("stem")
            if isinstance(stem, str) and len(stem) > 160:
                row["stem"] = stem[:157] + "..."
        trimmed = "Explanations, options and citations were left out to keep this report small"
    report.setdefault("notes", []).append(
        f"{trimmed}. Wrong answers were saved to your progress, so learning_exam_review shows those questions again "
        "and learning_exam_answer explains every option."
    )
    return report


def _live_scene_file(router: OmniverseRouter) -> str:
    """The live scene's saved root layer as an absolute local path, or SceneFileError explaining why there is none."""
    try:
        stage_name = str(router.scene_summary().get("stage_name") or "")
    except Exception as exc:  # the live bridge may be down; say so instead of failing the tool
        raise SceneFileError(f"Could not read the live scene name ({type(exc).__name__}: {exc}); pass file=... instead.") from exc
    local = _local_usd_file(stage_name.split(":SDF_FORMAT_ARGS:", 1)[0])
    if local is None:
        detail = ""
        if router._scene.backend_info.get("mode") in _FAKE_SCENE_MODES:
            detail = " The router is using its in-memory fake scene, which has no file."
        raise SceneFileError(
            f"The live scene {stage_name or '(none)'!r} is not saved to a local USD file.{detail} "
            "Save it first or pass file=... (an absolute path, or a path relative to the workspace)."
        )
    # This is the scene the user opened, so it may live outside the workspace folder.
    return resolve_scene_file(local, allow_outside_workspace=True)


def _file_mode(router: OmniverseRouter, file: str | None, action: Callable[[Any], dict]) -> dict:
    """Open the saved file read-only, run one insight/validation action and wrap the result (never raises)."""
    wanted = file.strip() if isinstance(file, str) and file.strip() else None
    resolved: str | None = None
    base = {"mode": "saved_file", "note": SAVED_FILE_NOTE}
    try:
        resolved = resolve_scene_file(wanted) if wanted else _live_scene_file(router)
        with readonly_stage(resolved) as stage:
            dirty = sorted(layer.identifier for layer in stage.GetUsedLayers() if layer.dirty)
            result = action(stage)
    except (SceneFileError, ValueError, ImportError, ReadOnlyViolation) as exc:
        return _error(str(exc), file=resolved or wanted, **base)
    except Exception as exc:  # pxr raises Tf errors (RuntimeError subclasses) for unreadable layers
        return _error(f"{type(exc).__name__}: {exc}", file=resolved or wanted, **base)
    payload: dict[str, Any] = {"ok": True, "file": resolved, **base, **result}
    if dirty:
        payload["warnings"] = [
            "These layers already had unsaved in-memory edits in this process, so the result includes them: "
            + ", ".join(dirty[:10])
        ]
    return payload


def _liverps_objective_ids() -> list[str]:
    """Composition objectives about opinion strength / LIVERPS ordering, read from the blueprint."""
    composition = exam_blueprint.domain("Composition") or {"objectives": []}
    ids = [
        obj["id"]
        for obj in composition["objectives"]
        if _LIVERPS_KEYWORDS.intersection(keyword.lower() for keyword in obj.get("keywords", []))
    ]
    return ids or ["1.6"]


def _review_item(question_id: str, questions: dict[str, dict]) -> dict:
    """What to show for one due card: the bank question (answers withheld) or the regenerated puzzle."""
    puzzle = _PUZZLE_ID.match(question_id)
    if puzzle:
        try:
            return {"kind": "puzzle", "puzzle": exam_puzzles.generate_puzzle(int(puzzle.group(2)), puzzle.group(1))}
        except ValueError as exc:
            return {"kind": "puzzle", "message": str(exc)}
    item = questions.get(question_id)
    if item is None:
        return {"kind": "unknown", "message": "This id is no longer in the question bank."}
    return {"kind": "question", "question": exam_bank.public_view(item)}


def build_server(host: str | None = None, port: int | None = None) -> FastMCP:
    offline_usd = OfflineUSDKnowledge()
    # The installed Isaac Sim / Kit extension docs, one lazily indexed domain each, shared with the router's
    # Kit / OmniUI / Isaac Sim fallback backends (resolving the folders reads nothing yet).
    docs_knowledge = build_docs_knowledge()
    # One exam progress store and one mock-exam store per server, shared by the exam tools and the router.
    progress_store = build_progress_store()
    mock_store = build_mock_store()
    router = build_router(
        offline_knowledge=offline_usd,
        progress_store=progress_store,
        mock_store=mock_store,
        docs_knowledge=docs_knowledge,
    )

    def _open_mock_ids() -> set[str]:
        """Question ids of the open mock (empty when none, or when the mock file cannot be read)."""
        try:
            return mock_store.open_question_ids()
        except (MockExamError, ValueError, OSError):
            return set()

    app = FastMCP(
        name="omniverse-router-mcp",
        instructions="Single front door for Omniverse scene operations, workspace context, and routed knowledge lookups.",
        host=host or os.environ.get("MCP_HOST", "127.0.0.1"),
        port=port or int(os.environ.get("MCP_PORT", "9905")),
        streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
    )
    # The change journal records every APPLIED scene edit (never previews) made through the tools registered below
    # (journal.JOURNALED_TOOLS); memory-only under the test switch, else <workspace>/scene-journal/ or
    # $OMNIVERSE_SCENE_MCP_JOURNAL. Journal errors never break a tool.
    change_journal = build_change_journal()
    install_journal(app, change_journal, target=lambda: target_from_backend(router._scene.backend_info))

    @app.tool(name="router_dispatch")
    async def router_dispatch(request: str, dry_run: Any = True, show_python: bool = False) -> dict:
        """Route a natural-language or command-style request to the right Omniverse MCP backend. show_python=True
        also returns the OpenUSD Python for a scene edit (result['python'])."""
        dry_run = dry_run is not False
        result = await router.handle(request, dry_run=dry_run)
        response = {"decision": _decision_dict(result), "payload": result.payload}
        if show_python is True:
            response["python"] = _dispatch_python(router, request, result)
        return response

    @app.tool(name="router_route")
    def router_route(request: str) -> dict:
        """Inspect how the router would classify a request before executing it."""
        decision = router.route(request)
        return {
            "target": decision.target.value,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "normalized_request": decision.normalized_request,
        }

    @app.tool(name="router_help")
    def router_help(topic: str | None = None) -> dict:
        """Plain-English guide to every tool, grouped by task (scene, edits, insight, lights/cameras/materials, undo,
        docs, exam prep, learning, health), with examples and router_dispatch phrases. topic narrows it to a group
        ('lights', 'undo', 'exam'), one tool ('scene_set_attribute') or a keyword search. Reads nothing."""
        return router.tool_help(topic)

    @app.tool(name="router_scene_targets")
    def router_scene_targets() -> dict:
        """Read-only: the scene apps the router can talk to (isaac on 9911, composer on 9910, the in-memory fake
        scene), which one is active now, and how to switch. Nothing is probed or changed."""
        return _scene_targets_view(router)

    @app.tool(name="router_set_scene_target")
    def router_set_scene_target(target: str, force: bool = False) -> dict:
        """Point the scene tools at another app without restarting the router: 'isaac' (Isaac Sim bridge, 9911),
        'composer' (USD Composer bridge, 9910) or 'fake' (in-memory scene). The app's /health is probed first
        (300 ms, 127.0.0.1 only) and a target that does not answer is refused unless force=True. No scene, layer or
        file changes; the result reports the new bridge's capabilities."""
        result = switch_scene_target(router._scene, target, force=force)
        result["targets"] = _scene_targets_view(router)["targets"]
        if result.get("ok"):
            current = result.get("current") or {}
            where = current.get("endpoint") or "the in-memory fake scene"
            result["message"] = (
                f"Scene tools now talk to {where}." if result.get("changed") else f"Already using {where}; nothing changed."
            )
            capabilities = result.get("capabilities")
            if isinstance(capabilities, dict) and (
                capabilities.get("needs_bridge_update")
                or (capabilities.get("ok") and not capabilities.get("fake_scene")
                    and set(LIVE_RPC_METHODS) - set(capabilities.get("methods") or []))
            ):
                result["how_to_enable"] = list(LIVE_ENABLE_STEPS)
                result["message"] += " That bridge predates some live features; see how_to_enable."
        else:
            result["message"] = result.get("error") or "The scene target was not switched."
        return result

    @app.tool(name="router_status")
    def router_status() -> dict:
        """Summarize which Omniverse backends are configured."""
        return {
            "scene": router.scene_summary(),
            "backends": router.backend_status()["backends"],
            "targets": [target.value for target in RouteTarget],
        }

    @app.tool(name="router_health")
    def router_health() -> dict:
        """Read-only health check: scene backend choice, 300 ms TCP probes, router PID and redacted stderr tail."""
        return check_router_health(
            router._scene.backend_info,
            router.backend_status()["backends"],
            workspace_root=_workspace_root(),
        )

    @app.tool(name="workspace_context")
    def workspace_context() -> dict:
        """Return shared Omniverse setup and reusable lessons for any project in this workspace."""
        return {
            "scope": "shared_across_projects",
            "warning": "Do not assume the caller wants to continue the ZAZ demo.",
            "content": _read_workspace_document("context"),
        }

    @app.tool(name="workspace_document")
    def workspace_document(name: str = "new_project_template") -> dict:
        """Return an allowlisted shared document: new_project_template or mcp_cheatsheet."""
        return {"name": name, "content": _read_workspace_document(name)}

    @app.tool(name="workspace_learning_index")
    def workspace_learning_index() -> dict:
        """List reusable learning documents and explain which context belongs to an existing demo."""
        return {
            "shared_start": "OMNIVERSE_WORKSPACE_CONTEXT.md",
            "new_project_template": "NEW_PROJECT_CONTEXT_TEMPLATE.md",
            "learning": [
                "OPENUSD_FOUNDATIONS_ONE_BY_ONE.md",
                "OPENUSD_COMPOSITION_AND_PIPELINE_ONE_BY_ONE.md",
                "OPENUSD_BEYOND_BASICS_ONE_BY_ONE.md",
                "USD_COMPOSER_MCP_CHEATSHEET.md",
                "MINI_PROJECT_USD_CONCEPTS.md",
                "OMNIVERSE_MINI_DEMO_CONCEPTS.md",
            ],
            "existing_demo_only": ["RESUME_ON_NEW_PC.md", "mini_omk_demo/", "working_scene.usda"],
        }

    @app.tool(name="scene_summary")
    def scene_summary() -> dict:
        """Return a compact summary of the current scene."""
        return router.scene_summary()

    @app.tool(name="scene_select")
    def scene_select(paths: Sequence[str]) -> dict:
        """Select one or more prim paths in the active scene."""
        return router._scene.set_selection(list(paths))

    @app.tool(name="scene_inspect")
    def scene_inspect(path: str) -> dict:
        """Inspect a prim by path."""
        return router._scene.inspect_prim(path)

    @app.tool(name="scene_children")
    def scene_children(path: str) -> dict:
        """List the immediate children of a prim."""
        return router._scene.list_children(path)

    @app.tool(name="scene_create")
    def scene_create(path: str, type_name: str = "Xform", dry_run: Any = True, show_python: bool = False) -> dict:
        """Create a prim, optionally in dry-run mode.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        result = router._wrap_operation_result("scene", router._scene.create_prim(path, type_name, dry_run=dry_run))
        return _with_python(result, show_python, "scene_create", path=path, type_name=type_name)

    @app.tool(name="scene_delete")
    def scene_delete(path: str, dry_run: Any = True, show_python: bool = False) -> dict:
        """Delete a prim, optionally in dry-run mode.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        result = router._wrap_operation_result("scene", router._scene.delete_prim(path, dry_run=dry_run))
        return _with_python(result, show_python, "scene_delete", path=path)

    @app.tool(name="scene_duplicate")
    def scene_duplicate(source_path: str, target_path: str, dry_run: Any = True, show_python: bool = False) -> dict:
        """Duplicate one prim to another path.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        result = router._wrap_operation_result(
            "scene",
            router._scene.duplicate_prim(source_path, target_path, dry_run=dry_run),
        )
        return _with_python(result, show_python, "scene_duplicate", source_path=source_path, target_path=target_path)

    @app.tool(name="scene_reparent")
    def scene_reparent(path: str, new_parent: str, dry_run: Any = True) -> dict:
        """Reparent a prim under a new parent."""
        dry_run = dry_run is not False
        return router._wrap_operation_result("scene", router._scene.reparent_prim(path, new_parent, dry_run=dry_run))

    @app.tool(name="scene_move")
    def scene_move(
        path: str,
        translation: Sequence[float] | None = None,
        rotation: Sequence[float] | None = None,
        scale: Sequence[float] | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """Apply transform updates to a prim.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        result = router._wrap_operation_result(
            "scene",
            router._scene.move_prim(
                path,
                translation=_coerce_vector(translation),
                rotation=_coerce_vector(rotation),
                scale=_coerce_vector(scale),
                dry_run=dry_run,
            ),
        )
        return _with_python(result, show_python, "scene_move", path=path, translation=translation, rotation=rotation,
                            scale=scale)

    @app.tool(name="scene_material")
    def scene_material(path: str, material_path: str, dry_run: Any = True, show_python: bool = False) -> dict:
        """Bind a material to a prim.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        result = router._wrap_operation_result(
            "scene",
            router._scene.set_material(path, material_path, dry_run=dry_run),
        )
        return _with_python(result, show_python, "scene_material", path=path, material_path=material_path)

    @app.tool(name="scene_rename")
    def scene_rename(path: str, new_name: str, dry_run: Any = True, show_python: bool = False) -> dict:
        """Rename a prim and keep its subtree intact.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        result = router._wrap_operation_result("scene", router._scene.rename_prim(path, new_name, dry_run=dry_run))
        return _with_python(result, show_python, "scene_rename", path=path, new_name=new_name)

    @app.tool(name="scene_variants")
    def scene_variants(path: str = "/World/Car") -> dict:
        """List native USD variant selections on a prim."""
        return router._scene.get_variant_selections(path)

    @app.tool(name="scene_variant_set")
    def scene_variant_set(
        variant_set: str,
        selection: str,
        path: str = "/World/Car",
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """Select a native USD variant on a prim.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        result = router._wrap_operation_result(
            "scene",
            router._scene.set_variant_selection(path, variant_set, selection, dry_run=dry_run),
        )
        return _with_python(result, show_python, "scene_variant_set", path=path, variant_set=variant_set,
                            selection=selection)

    @app.tool(name="scene_animation_actions")
    def scene_animation_actions() -> dict:
        """List the named ZAZ-965 animation actions available in the live Composer scene."""
        return {"actions": ZAZ_ANIMATION_ACTIONS, "timeline": router._scene.get_timeline_state()}

    @app.tool(name="scene_animation_play")
    def scene_animation_play(action: str) -> dict:
        """Play or pose one named ZAZ-965 action such as open_everything or wheel_spin."""
        key = action.strip().lower()
        if key not in ZAZ_ANIMATION_ACTIONS:
            raise ValueError(f"Unknown animation action '{action}'; expected one of {sorted(ZAZ_ANIMATION_ACTIONS)}")
        spec = ZAZ_ANIMATION_ACTIONS[key]
        if spec["mode"] == "pose":
            timeline = router._scene.set_timeline_time(float(spec["frame"]))
        else:
            timeline = router._scene.play_timeline(float(spec["start"]), float(spec["end"]))
        return {"action": key, "description": spec["description"], "timeline": timeline}

    @app.tool(name="scene_animation_stop")
    def scene_animation_stop() -> dict:
        """Stop the active Composer timeline animation and keep the current pose."""
        return {"timeline": router._scene.stop_timeline()}

    @app.tool(name="scene_find")
    def scene_find(query: str, scope_path: str | None = None) -> dict:
        """Search for prims by path, type, or material text."""
        return router._scene.find_prims(query, scope_path=scope_path)

    @app.tool(name="scene_save")
    def scene_save(path: str, dry_run: Any = True) -> dict:
        """Save the current live scene to a USD file; dry_run previews in-place save versus Save As."""
        dry_run = dry_run is not False
        result = router._scene.save_scene(path, dry_run=dry_run)
        if isinstance(result, dict) and "tip" not in result:
            result = {**result, "tip": _CHECKPOINT_TIP}  # a suggestion only; saving itself is unchanged
        return result

    @app.tool(name="learning_tutor")
    def learning_tutor(topic: str = "schemas", style: str = "lesson") -> dict:
        """Get a small USD learning lesson, cheat sheet, or quiz."""
        request = f"{style} me about {topic}"
        return router._dispatch_learning(request)

    @app.tool(name="learning_practice")
    def learning_practice(topic: str = "schemas") -> dict:
        """Get a practical, scene-aware drill you can do in USD Composer."""
        return router._dispatch_learning(f"practice me about {topic}")

    @app.tool(name="learning_practice_start")
    def learning_practice_start(topic: str = "schemas") -> dict:
        """Start an interactive practice session you can step through one command at a time."""
        return router.start_practice(topic)

    @app.tool(name="learning_practice_next")
    def learning_practice_next(feedback: str | None = None) -> dict:
        """Advance the current practice session to the next step."""
        return router.next_practice_step(feedback=feedback)

    @app.tool(name="learning_scene_quiz")
    def learning_scene_quiz(topic: str = "schemas") -> dict:
        """Generate a quiz from the currently selected prim or the current scene."""
        return router.scene_quiz(topic)

    @app.tool(name="learning_study_mode")
    def learning_study_mode(topic: str | None = None) -> dict:
        """Get a study recommendation based on the current scene."""
        return router.study_mode(topic)

    @app.tool(name="learning_translate")
    def learning_translate(request: str) -> dict:
        """Translate natural language into the most likely Omniverse scene action."""
        return router.translate_command(request)

    @app.tool(name="learning_history")
    def learning_history(limit: int = 20) -> dict:
        """Show recent learning actions and progress."""
        return router.learning_history(limit=limit)

    @app.tool(name="learning_cheatsheet")
    def learning_cheatsheet() -> dict:
        """Return the current cheat sheet for scene and learning commands."""
        return router.cheat_sheet()

    @app.tool(name="learning_roadmap")
    def learning_roadmap() -> dict:
        """Return a simple study roadmap based on what you have already practiced."""
        return router.learning_roadmap()

    @app.tool(name="usd_knowledge")
    async def usd_knowledge(query: str) -> dict:
        """Query the USD/OpenUSD knowledge backend directly; when the USD docs MCP is not running, the answer comes
        from the bundled offline USD 25.11 docs (the result then carries a note and primary_error)."""
        return await router.ask_backend(RouteTarget.USD, query)

    @app.tool(name="kit_knowledge")
    async def kit_knowledge(query: str) -> dict:
        """Query the Kit knowledge backend directly."""
        return await router.ask_backend(RouteTarget.KIT, query)

    @app.tool(name="omni_ui_knowledge")
    async def omni_ui_knowledge(query: str) -> dict:
        """Query the OmniUI knowledge backend directly."""
        return await router.ask_backend(RouteTarget.OMNI_UI, query)

    @app.tool(name="isaac_sim_knowledge")
    async def isaac_sim_knowledge(query: str) -> dict:
        """Query the Isaac Sim knowledge backend directly."""
        return await router.ask_backend(RouteTarget.ISAAC_SIM, query)

    @app.tool(name="bowerbot_knowledge")
    async def bowerbot_knowledge(query: str) -> dict:
        """Query the Bowerbot knowledge backend directly."""
        return await router.ask_backend(RouteTarget.BOWERBOT, query)

    # --- offline USD docs ---------------------------------------------------------------------------------------

    @app.tool(name="usd_docs_search")
    async def usd_docs_search(query: str, top_k: int = 5) -> dict:
        """Search the bundled offline OpenUSD docs (USD 25.11 API atlas, openusd.org pages, Learn OpenUSD, workspace
        notes). Works with no network or docs MCP; each hit has a title, source, snippet and citation (top_k <= 25)."""
        try:
            results = await asyncio.to_thread(offline_usd.search, query, top_k)
        except Exception as exc:  # missing or unreadable bundled data
            return _error(f"Offline USD search failed: {type(exc).__name__}: {exc}", query=query, results=[])
        payload = {"ok": bool(results), "query": query, "count": len(results), "results": results, "note": OFFLINE_NOTE}
        if not results:
            payload["message"] = "No offline USD matches; try other words or usd_knowledge."
        return payload

    # --- offline Kit / Isaac Sim / omni.ui extension docs (the installed Isaac Sim, read-only) ---------------------

    @app.tool(name="docs_search")
    async def docs_search(query: str, domain: str = "isaac_sim", top_k: int = 5) -> dict:
        """Search the installed Isaac Sim / Kit extension docs offline (no network, no docs MCP): domain 'isaac_sim'
        (isaacsim.* extensions), 'kit' (Kit SDK and omni.kit.*) or 'omni_ui' (omni.ui widgets, including the .pyi
        class docs). Each hit has a title, extension, version, kind, snippet and a 'path:line' source; deprecated
        extensions are flagged. top_k <= 25."""
        try:
            name = normalize_docs_domain(domain)
        except ValueError as exc:
            return _error(str(exc), query=query, domain=domain, results=[])
        knowledge = docs_knowledge[name]
        base = {"query": query, "domain": name, "note": _DOCS_NOTE}
        if not knowledge.available:
            roots = ", ".join(str(path) for path in knowledge.requested_roots)
            return _error(
                f"No Kit/Isaac Sim extension docs found (looked in {roots}); set {DOCS_ENV_ROOT} to the Isaac Sim "
                "install folder.",
                results=[],
                **base,
            )
        try:
            count = max(1, min(int(top_k), 25))
            results = await asyncio.to_thread(knowledge.search, query, count)
        except (TypeError, ValueError) as exc:
            return _error(str(exc), results=[], **base)
        except Exception as exc:  # unreadable install files: report, never raise into the client
            return _error(f"Offline docs search failed: {type(exc).__name__}: {exc}", results=[], **base)
        payload = {
            "ok": bool(results),
            **base,
            "isaac_sim_version": knowledge.isaac_sim_version,
            "count": len(results),
            "results": results,
        }
        if not results:
            payload["message"] = (
                "No offline matches; try other words, another domain, or extension_info for an extension id."
            )
        return payload

    @app.tool(name="extension_info")
    async def extension_info(name: str) -> dict:
        """Facts about one installed Kit / Isaac Sim / omni.ui extension, read offline from its extension.toml and
        docs: version, title, description, python modules, dependencies, docs files, deprecation warning and the
        latest changelog entry. A module path (isaacsim.core.api.robots) maps to its extension; an unknown or
        renamed id (omni.isaac.core) returns close suggestions."""
        knowledge = docs_knowledge["isaac_sim"]
        if not isinstance(name, str) or not name.strip():
            return _error("name is required, e.g. 'isaacsim.sensors.rtx' or 'omni.ui'.", name=name)
        if not knowledge.available:
            return _error(
                f"No Kit/Isaac Sim extension docs found; set {DOCS_ENV_ROOT} to the Isaac Sim install folder.",
                name=name,
                found=False,
            )
        try:
            info = await asyncio.to_thread(knowledge.extension_info, name.strip())
        except Exception as exc:  # unreadable install files
            return _error(f"Extension lookup failed: {type(exc).__name__}: {exc}", name=name, found=False)
        payload = {"ok": bool(info.get("found")), **info, "note": _DOCS_NOTE}
        if not info.get("found"):
            payload["message"] = info.get("message") or f"{name} is not an installed extension."
        return payload

    @app.tool(name="learning_glossary")
    async def learning_glossary(term: str | None = None) -> dict:
        """Look up an OpenUSD glossary term (e.g. 'LIVERPS', 'payload', 'variant set') in the bundled Learn OpenUSD
        glossary; without a term, list every glossary term. A miss returns close suggestions."""
        try:
            entry = await asyncio.to_thread(offline_usd.glossary, term)
        except Exception as exc:
            return _error(f"Glossary lookup failed: {type(exc).__name__}: {exc}", query=term)
        if term is None or not str(term).strip():
            if not entry.get("count"):
                return _error("No glossary found; LearnOpenUSD-main/docs/glossary.md is missing.", **entry)
            return {"ok": True, **entry}
        if not entry.get("found"):
            return {"ok": False, "message": f"No glossary entry for {term!r}.", **entry}
        return {"ok": True, **entry}

    # --- NCP-OUSD exam prep -------------------------------------------------------------------------------------

    @app.tool(name="learning_exam_blueprint")
    def learning_exam_blueprint(include_objectives: bool = True) -> dict:
        """NCP-OUSD exam blueprint: exam facts, the 8 weighted domains and their objectives (study-guide numbering),
        readings and high-yield objectives, plus how many practice questions the bank has per domain."""
        data = exam_blueprint.blueprint(include_objectives)
        try:
            stats = exam_bank.stats()
            data["question_bank"] = {"total": stats["total"], "by_domain": stats["by_domain"]}
        except (OSError, ValueError) as exc:
            data["question_bank"] = {"error": str(exc)}
        return {"ok": True, **data}

    @app.tool(name="learning_exam_plan")
    def learning_exam_plan(days_until_exam: int | None = None, minutes_per_day: int = 60) -> dict:
        """Weighted NCP-OUSD study plan from the domain weights and your saved exam progress (weak or untested domains
        get more time); with days_until_exam it adds a day-by-day schedule (up to 60 days)."""
        summary = progress_store.summary(known_domains=list(exam_bank.DOMAINS))
        answered = summary["totals"]["answered"]
        plan = exam_blueprint.study_plan(summary if answered else None, days_until_exam, minutes_per_day)
        plan["progress"] = {
            "answered": answered,
            "accuracy": summary["totals"]["accuracy"],
            "due_count": summary["due_count"],
            "weakest_objectives": summary["weakest_objectives"],
        }
        return {"ok": True, **plan}

    @app.tool(name="learning_exam_quiz")
    def learning_exam_quiz(
        domain: str | None = None,
        objective: str | None = None,
        count: int = 5,
        review_due: bool = False,
    ) -> dict:
        """Original NCP-OUSD practice questions with answers withheld, optionally filtered by domain (name, number or
        prefix) or objective (e.g. '1.6'). review_due=True puts questions due for spaced-repetition review first."""
        due_ids: list[str] = []
        if review_due:
            due_ids = [row["question_id"] for row in progress_store.due_reviews(limit=100)]
        try:
            quiz = exam_bank.quiz(domain=domain, objective=objective, count=count, prefer_ids=due_ids,
                                  exclude_ids=sorted(_open_mock_ids()))
        except ValueError as exc:
            return _error(str(exc))
        payload: dict[str, Any] = {"ok": True, **quiz, "how_to_answer": _QUIZ_ANSWER_HINT}
        if review_due:
            due = set(due_ids)
            included = sum(1 for question in quiz["questions"] if question["id"] in due)
            payload["review_due"] = {"due_cards": len(due_ids), "included": included}
            if not included:
                payload["review_due"]["message"] = "No matching questions are due for review; these are fresh questions."
        return payload

    @app.tool(name="learning_exam_answer")
    def learning_exam_answer(question_id: str, chosen: list[int | str], seconds: float | None = None) -> dict:
        """Grade one practice question and save the result to your exam progress (spaced-repetition card, domain and
        objective stats). chosen holds 0-based option indices or letters; multi-select needs the exact correct set."""
        if str(question_id).strip() in _open_mock_ids():
            return _error(
                "This question is part of your open mock exam; answer it with learning_mock_exam_answer (graded at "
                "learning_mock_exam_finish), or finish/abandon the mock first.",
                question_id=question_id,
            )
        try:
            graded = exam_bank.grade(question_id, chosen)
        except ValueError as exc:
            return _error(str(exc), question_id=question_id)
        try:
            record = progress_store.record_answer(
                graded["question_id"],
                domain=graded["domain"],
                objective_ids=graded["objective_ids"],
                chosen=graded["chosen"],
                correct=graded["correct_answer"],
                is_correct=graded["correct"],
                seconds=seconds,
                source="quiz",
            )
        except (TypeError, ValueError, OSError) as exc:
            record = _error(f"Graded but not saved to progress: {exc}")
        return {"ok": True, **graded, "progress": record}

    @app.tool(name="learning_exam_progress")
    def learning_exam_progress() -> dict:
        """Your saved NCP-OUSD progress: totals, accuracy per domain, weakest objectives, reviews due, study streak,
        the five most recent answers and where progress is stored."""
        summary = progress_store.summary(known_domains=list(exam_bank.DOMAINS))
        return {"ok": True, **summary, "recent": progress_store.history(limit=5)}

    @app.tool(name="learning_exam_review")
    def learning_exam_review(limit: int = 10) -> dict:
        """Spaced-repetition cards due now, most overdue first, each with its question (answers withheld) or its
        composition puzzle so you can answer it again."""
        rows = progress_store.due_reviews(limit=limit)
        questions = {item["id"]: item for item in exam_bank.load_bank()} if rows else {}
        reviews = [{**row, **_review_item(row["question_id"], questions)} for row in rows]
        payload: dict[str, Any] = {
            "ok": True,
            "count": len(reviews),
            "reviews": reviews,
            "how_to_answer": f"{_QUIZ_ANSWER_HINT} {_PUZZLE_ANSWER_HINT}",
        }
        if not reviews:
            payload["message"] = "Nothing is due for review right now; learning_exam_quiz gives new questions."
        return payload

    @app.tool(name="learning_exam_reset")
    def learning_exam_reset(confirm: bool = False) -> dict:
        """Clear ALL saved NCP-OUSD exam progress. Does nothing unless confirm=True; an existing progress.json is
        first copied to progress.json.bak-<timestamp>."""
        try:
            return progress_store.reset(confirm=confirm)
        except OSError as exc:
            return _error(f"Could not reset progress: {exc}", reset=False)

    # --- daily lessons ------------------------------------------------------------------------------------------

    @app.tool(name="learning_exam_day")
    async def learning_exam_day(day: int | None = None) -> dict:
        """One day of your NCP-OUSD study plan as a lesson: objectives with plain explanations, offline doc hits,
        readings, a hands-on exercise (LIVERPS puzzle, 'predict the value' or a try-it layer stack for
        usd_playground_compose) and quiz questions (answers withheld). day=None gives the next day not yet marked
        complete. The first call saves the plan start date; nothing else is written."""
        try:
            return await asyncio.to_thread(
                exam_daily.build_day, day, store=progress_store, knowledge=offline_usd, exclude_ids=_open_mock_ids()
            )
        except (TypeError, ValueError, OSError) as exc:
            return _error(f"Could not build the lesson: {exc}", day=day)

    @app.tool(name="learning_exam_day_complete")
    def learning_exam_day_complete(day: int) -> dict:
        """Mark one study-plan day complete (calling it again changes nothing) and report the day streak, how many
        days remain and the next day to study."""
        try:
            return exam_daily.complete_day(day, progress_store)
        except (TypeError, ValueError, OSError) as exc:
            return _error(f"Could not mark day {day} complete: {exc}", day=day)

    @app.tool(name="learning_exam_set_date")
    def learning_exam_set_date(
        exam_date: str | None = None,
        days_until_exam: int | None = None,
        minutes_per_day: int | None = None,
    ) -> dict:
        """Re-plan the daily lessons from today: give exam_date ('YYYY-MM-DD') or days_until_exam (neither means 30
        days), and minutes_per_day (10-480; omit it to keep your saved value, 60 by default). Day numbering restarts
        at day 1 today, so completed-day marks are cleared; answers and spaced-repetition reviews are kept. Returns
        the new day-by-day outline."""
        has_date = isinstance(exam_date, str) and bool(exam_date.strip())
        if has_date and days_until_exam is not None:
            return _error("Give exam_date or days_until_exam, not both.")
        when: str | int | None = exam_date.strip() if has_date else days_until_exam
        try:
            return exam_daily.set_exam_date(when, minutes_per_day, progress_store)
        except (TypeError, ValueError, OSError) as exc:
            return _error(f"Could not set the exam date: {exc}")

    # --- timed mock exam ----------------------------------------------------------------------------------------

    @app.tool(name="learning_mock_exam_start")
    def learning_mock_exam_start(count: int = MOCK_EXAM_DEFAULTS["count"], minutes: int = MOCK_EXAM_DEFAULTS["minutes"]) -> dict:
        """Start a TIMED NCP-OUSD mock exam: the clock starts now. Questions are drawn per domain at the official
        weights (65 in 120 minutes like the real exam; smaller values give a shorter run). Returns the session_id
        and the first 10 questions (answers withheld); only one mock can be open at a time."""
        try:
            started = mock_store.start(count=count, minutes=minutes, include_questions=False)
        except (MockExamError, ValueError, TypeError, OSError) as exc:
            open_mock = None
            try:
                active = mock_store.active()
            except (MockExamError, ValueError, OSError):
                active = None
            if active:
                keys = ("session_id", "state", "deadline", "remaining", "answered", "total", "flagged_count")
                open_mock = {key: active.get(key) for key in keys}
            return _mock_error(exc, open_mock=open_mock)
        try:
            page = mock_store.questions(started["session_id"], offset=0, limit=MOCK_PAGE_SIZE)
        except (MockExamError, ValueError, OSError) as exc:
            page = {"questions": [], "total": started["count"], "error": str(exc)}
        started["questions"] = page["questions"]
        started["questions_shown"] = {"offset": 0, "count": len(page["questions"]), "total": page.get("total")}
        if started["count"] > len(page["questions"]):
            started["next_page"] = {"tool": "learning_mock_exam_questions", "offset": len(page["questions"])}
        started["how_to_answer"] = _MOCK_HOW_TO
        return started

    @app.tool(name="learning_mock_exam_questions")
    def learning_mock_exam_questions(session_id: str | None = None, offset: int = 0, limit: int = MOCK_PAGE_SIZE) -> dict:
        """Page through a mock exam's questions (answers withheld), each with your current choice and flag.
        session_id=None means the open mock; limit is at most 25."""
        try:
            page = mock_store.questions(session_id, offset=max(0, int(offset)), limit=max(1, min(MOCK_MAX_PAGE, int(limit))))
        except (MockExamError, ValueError, TypeError, OSError) as exc:
            return _mock_error(exc, session_id=session_id)
        shown_to = page["offset"] + len(page["questions"])
        if shown_to < page["total"]:
            page["next_page"] = {"tool": "learning_mock_exam_questions", "offset": shown_to}
        page["how_to_answer"] = _MOCK_HOW_TO
        return page

    @app.tool(name="learning_mock_exam_answer")
    def learning_mock_exam_answer(
        session_id: str,
        question_id: str | int,
        chosen: list[int | str] | int | str | None,
        allow_late: bool = False,
    ) -> dict:
        """Record or change one mock-exam answer while the clock runs. question_id is the id or the 1-based question
        number; chosen holds letters or 0-based indices ('A', ['A', 'C'], 'A,C'); [] clears it. Not graded until
        learning_mock_exam_finish. After time is up, answers are rejected unless allow_late=True (late answers are
        marked late in the report)."""
        try:
            return mock_store.answer(session_id, question_id, chosen, allow_late=allow_late)
        except (MockExamError, ValueError, TypeError, OSError) as exc:
            return _mock_error(exc, session_id=session_id, question_id=question_id)

    @app.tool(name="learning_mock_exam_flag")
    def learning_mock_exam_flag(session_id: str, question_id: str | int, flagged: bool = True) -> dict:
        """Flag a mock-exam question to revisit before finishing (flagged=False removes the flag)."""
        try:
            return mock_store.flag(session_id, question_id, flagged=flagged)
        except (MockExamError, ValueError, TypeError, OSError) as exc:
            return _mock_error(exc, session_id=session_id, question_id=question_id)

    @app.tool(name="learning_mock_exam_status")
    def learning_mock_exam_status(session_id: str | None = None) -> dict:
        """Time left, answered and unanswered questions, flags and per-domain counts of a mock (session_id=None is
        the open mock). With no mock open it lists recent mocks and their scores."""
        try:
            if session_id is None and mock_store.active() is None:
                return {
                    "ok": True,
                    "open": False,
                    "message": "No mock exam is open; learning_mock_exam_start starts a timed one.",
                    "recent": mock_store.list_sessions(limit=5),
                }
            status = mock_store.status(session_id)
        except (MockExamError, ValueError, TypeError, OSError) as exc:
            return _mock_error(exc, session_id=session_id)
        return {**status, "open": status.get("status") == "active"}

    @app.tool(name="learning_mock_exam_finish")
    def learning_mock_exam_finish(session_id: str) -> dict:
        """Finish and score a mock exam: overall and per-domain percentages, time used, every wrong or unanswered
        question with the correct answer and why, and a readiness verdict (NVIDIA publishes no pass mark). Answered
        questions are saved to your exam progress and spaced-repetition reviews."""
        try:
            report = mock_store.finish(session_id, store=progress_store)
        except (MockExamError, ValueError, TypeError, OSError) as exc:
            return _mock_error(exc, session_id=session_id)
        return _fit_mock_report(report)

    @app.tool(name="learning_mock_exam_abandon")
    def learning_mock_exam_abandon(session_id: str) -> dict:
        """Close an open mock exam without scoring it or saving anything to progress, so a new one can start."""
        try:
            return mock_store.abandon(session_id)
        except (MockExamError, ValueError, TypeError, OSError) as exc:
            return _mock_error(exc, session_id=session_id)

    # --- composition sandbox and puzzles ------------------------------------------------------------------------

    @app.tool(name="usd_playground_compose")
    def usd_playground_compose(layers: dict[str, str], root: str, queries: list[str] | None = None) -> dict:
        """Compose your own USDA layer text in a throwaway temp folder (never your scene files) and explain it:
        layer stack, prims, arcs and query answers. layers maps file names like 'shot.usda' to USDA text; root names
        the root layer. Queries: '/Prim', '/Prim.attr', '/Prim.attr@24' (value at time 24) or '/Prim.rel'."""
        result = exam_sandbox.compose(layers, root, queries)
        if not result.get("ok") and result.get("errors"):
            result["message"] = result["errors"][0].get("message")
        return result

    @app.tool(name="learning_composition_puzzle")
    def learning_composition_puzzle(difficulty: str = "easy", seed: int | None = None) -> dict:
        """A LIVERPS strength-ordering puzzle (easy, medium or hard): small USDA layers and a 'which value wins?'
        question with options. The answer is withheld; the same seed always gives the same puzzle."""
        try:
            puzzle = exam_puzzles.generate_puzzle(seed=seed, difficulty=difficulty)
        except ValueError as exc:
            return _error(str(exc))
        return {"ok": True, **puzzle, "objective_ids": _liverps_objective_ids(), "how_to_answer": _PUZZLE_ANSWER_HINT}

    @app.tool(name="learning_composition_puzzle_answer")
    def learning_composition_puzzle_answer(puzzle_id: str, answer: str | int | float) -> dict:
        """Check a composition puzzle answer (option text, number or letter A-E) by really composing its layers with
        USD, explain the winning opinion, and save the result to exam progress under the Composition domain."""
        result = exam_puzzles.check_puzzle(puzzle_id, answer)
        if not result.get("ok"):
            return {**result, "message": result.get("error") or "The puzzle could not be checked."}
        try:
            record = progress_store.record_answer(
                result["puzzle_id"],
                domain="Composition",
                objective_ids=_liverps_objective_ids(),
                chosen=result["your_answer"],
                correct=result["correct_answer"],
                is_correct=bool(result["correct"]),
                source="puzzle",
            )
        except (TypeError, ValueError, OSError) as exc:
            record = _error(f"Checked but not saved to progress: {exc}")
        return {**result, "progress": record}

    # --- live or saved-file insight and validation --------------------------------------------------------------
    # mode='auto' (default): the LIVE open stage when the bridge offers live insight, else the SAVED file with a
    # fallback note; an explicit file always means the saved file. Every result says which mode it used.

    @app.tool(name="scene_stage_info")
    def scene_stage_info(file: str | None = None, mode: str = "auto") -> dict:
        """Read-only stage overview: layer stack, default prim, upAxis, metersPerUnit, time codes, prim census, model
        hierarchy and composition errors. mode 'auto' (default) reads the live open stage (unsaved edits included)
        when the Isaac bridge supports it, else the SAVED file with a note; 'live' or 'saved_file' force one. file
        (saved-file mode) defaults to the live scene's saved root layer."""
        return _insight_mode(router, mode, file, "stage_info", {}, usd_insight.stage_info)

    @app.tool(name="scene_composition")
    def scene_composition(prim_path: str, file: str | None = None, mode: str = "auto") -> dict:
        """Read-only: how one prim is composed (arcs with their LIVERPS letter, variant selections, instancing,
        prim stack strongest first). mode 'auto' (default) reads the live open stage when the Isaac bridge supports
        it, else the SAVED file with a note; 'live' or 'saved_file' force one."""
        return _insight_mode(
            router, mode, file, "composition", {"prim_path": prim_path},
            lambda stage: usd_insight.composition(stage, prim_path),
        )

    @app.tool(name="scene_explain_value")
    def scene_explain_value(
        prim_path: str,
        attribute: str,
        time: float | str | None = None,
        file: str | None = None,
        mode: str = "auto",
    ) -> dict:
        """Read-only: resolve one attribute and explain which opinion wins and why (LIVERPS, layer order, offsets,
        default vs time samples); time is a number, 'default' or 'earliest'. mode 'auto' (default) answers from the
        live open stage, unsaved edits included, when the Isaac bridge supports it, else from the SAVED file with a
        note; 'live' or 'saved_file' force one."""
        args: dict[str, Any] = {"prim_path": prim_path}
        if isinstance(attribute, str) and attribute.strip():  # else prim_path '/Prim.attr' names it, like saved_file
            args["attribute"] = attribute
        if time is not None:
            args["time"] = time
        return _insight_mode(
            router, mode, file, "explain_value", args,
            lambda stage: usd_insight.explain_value(stage, prim_path, attribute, time),
        )

    @app.tool(name="scene_properties")
    def scene_properties(prim_path: str, file: str | None = None, mode: str = "auto") -> dict:
        """Read-only: a prim's attributes (types, values, time samples, primvars), relationships and metadata. mode
        'auto' (default) reads the live open stage when the Isaac bridge supports it, else the SAVED file with a
        note; 'live' or 'saved_file' force one."""
        return _insight_mode(
            router, mode, file, "properties", {"prim_path": prim_path},
            lambda stage: usd_insight.properties(stage, prim_path),
        )

    @app.tool(name="scene_bounds")
    def scene_bounds(prim_path: str, file: str | None = None, mode: str = "auto") -> dict:
        """Read-only: world/local bounds, authored vs computed extent, visibility and purpose for one prim. mode
        'auto' (default) reads the live open stage when the Isaac bridge supports it, else the SAVED file with a
        note; 'live' or 'saved_file' force one."""
        return _insight_mode(
            router, mode, file, "bounds", {"prim_path": prim_path},
            lambda stage: usd_insight.bounds(stage, prim_path),
        )

    @app.tool(name="usd_validate")
    def usd_validate_tool(file: str | None = None, profile: str = "exam", mode: str = "auto") -> dict:
        """Read-only lint: profile 'exam' (checks tied to NCP-OUSD objectives), 'usd' (OpenUSD validators) or 'all';
        ok is False when an error-level issue exists. mode 'auto' (default) checks the live open stage when the
        Isaac bridge supports it, else the SAVED file with a note; 'live' or 'saved_file' force one."""
        return _insight_mode(
            router, mode, file, "validate", {"profile": profile},
            lambda stage: usd_validate.validate(stage, profile),
        )

    @app.tool(name="scene_capabilities")
    def scene_capabilities() -> dict:
        """What the connected scene bridge can do beyond basic scene commands: live insight queries, light/camera
        authoring, attribute/material edits, undo, its RPC version, and whether a bridge reload (Isaac Sim or
        Composer) is needed for the live features."""
        capabilities = router._scene.live_capabilities()
        backend = router._scene.backend_info
        insight = _live_support(capabilities)
        methods = set(capabilities.get("methods") or [])
        authoring_info = capabilities.get("authoring") if isinstance(capabilities.get("authoring"), dict) else {}
        authoring_methods = {"create_light", "create_camera", "aim"}
        if not capabilities.get("ok") or capabilities.get("fake_scene"):
            authoring = {"available": False, "reason": insight["reason"]}
        elif not authoring_methods <= methods:
            authoring = {"available": False, "reason": "the live bridge predates light/camera authoring; reload it"}
        elif not authoring_info.get("available"):
            authoring = {"available": False, "reason": authoring_info.get("error") or "the authoring module did not load"}
        else:
            authoring = {"available": True, "reason": None}
        authoring["dry_run_default"] = True
        edits = _feature_support(capabilities, insight, "edits", EDIT_RPC_METHODS, "attribute/material edits")
        undo = _feature_support(capabilities, insight, "undo", UNDO_RPC_METHODS, "undo")
        tier4 = {
            "assets": _feature_support(capabilities, insight, "assets", ASSET_RPC_METHODS, "adding assets by reference"),
            "physics": _feature_support(capabilities, insight, "physics", PHYSICS_RPC_METHODS, "physics setup"),
            "viewport_capture": _feature_support(capabilities, insight, "capture", CAPTURE_RPC_METHODS, "viewport capture"),
            "simulation": _feature_support(capabilities, insight, "simulation", SIMULATION_RPC_METHODS,
                                           "simulation controls"),
            "homework": _feature_support(capabilities, insight, "homework", HOMEWORK_RPC_METHODS,
                                         "the live homework check and scene tour"),
            "layout": _feature_support(capabilities, insight, "layout", LAYOUT_RPC_METHODS,
                                       "smart layout (snap, stack, distribute, space, align)"),
            "robots": _feature_support(capabilities, insight, "robots", ROBOT_RPC_METHODS, "the live robot inspector"),
            "layers": _feature_support(capabilities, insight, "layers", LAYER_RPC_METHODS,
                                       "the layer manager (layer stack, where is, sublayers, mute, edit target)"),
            "health": _feature_support(capabilities, insight, "health", HEALTH_RPC_METHODS,
                                       "the live asset health check"),
        }
        tier4["viewport_capture"]["dry_run_default"] = tier4["simulation"]["dry_run_default"] = None
        tier4["homework"]["dry_run_default"] = tier4["robots"]["dry_run_default"] = None
        tier4["health"]["dry_run_default"] = None
        missing = sorted(set(LIVE_RPC_METHODS) - methods) if capabilities.get("ok") else list(LIVE_RPC_METHODS)
        needs_update = bool(capabilities.get("needs_bridge_update")) or (
            bool(capabilities.get("ok")) and not capabilities.get("fake_scene") and bool(missing)
        )
        everything = all(feature["available"] for feature in tier4.values())
        if insight["available"] and authoring["available"] and edits["available"] and undo["available"] and everything:
            message = (
                "Live insight, light/camera authoring, attribute/material edits, undo, assets, physics, viewport "
                "capture, simulation controls, the homework check, the scene tour, smart layout, the robot inspector, "
                "the layer manager and the asset health check are available on the open stage."
            )
        elif needs_update:
            message = (
                "The live bridge needs a reload before every live feature works (missing: "
                f"{', '.join(missing) or 'see capabilities'}); see how_to_enable."
            )
        elif capabilities.get("fake_scene"):
            message = (
                "In-memory fake scene: attribute/material edits, asset references, a physics-free timeline and undo "
                "work in memory only; live insight, light/camera authoring, physics and screenshots need a live bridge."
            )
        else:
            message = f"Live features are unavailable: {insight['reason'] or authoring['reason']}."
        payload: dict[str, Any] = {
            "ok": True,
            "backend": {key: backend.get(key) for key in ("mode", "bridge", "endpoint", "reason", "warnings", "target")},
            "rpc_version": capabilities.get("version"),
            "expected_rpc_version": RPC_VERSION,
            "live_insight": {**insight, "queries": list(capabilities.get("insight_queries") or [])},
            "live_authoring": authoring,
            "scene_edits": edits,
            "undo": undo,
            **tier4,
            "missing_live_methods": missing,
            "needs_bridge_update": needs_update,
            "message": message,
            "capabilities": capabilities,
        }
        if needs_update or backend.get("mode") in _FAKE_SCENE_MODES:
            payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
        return payload

    # --- live light/camera authoring (dry run by default) --------------------------------------------------------

    @app.tool(name="scene_create_light")
    def scene_create_light(
        path: str,
        light_type: str,
        intensity: float | None = None,
        exposure: float | None = None,
        color: list[float] | None = None,
        size: float | list[float] | None = None,
        translation: list[float] | None = None,
        aim_at: str | list[float] | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan a UsdLux light (rect, sphere, disk, distant, cylinder or dome) at path in the
        LIVE open scene and show the preview to the user first. dry_run=False changes the user's open scene (not
        saved; scene_undo_last can undo it). size: one number, or [width, height] for rect / [length, radius] for
        cylinder; aim_at: a prim path or an [x, y, z] point the light's -Z axis should face.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(path=path, light_type=light_type, intensity=intensity, color=color, translation=translation)
        try:
            result = router._scene.create_light(
                path,
                light_type,
                intensity=intensity,
                exposure=exposure,
                color=_numbers(color, 3, "color"),
                size=_size_arg(size),
                translation=_numbers(translation, 3, "translation"),
                aim_at=_target_arg(aim_at, "aim_at"),
                dry_run=dry_run,
            )
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=bool(dry_run), applied=False, path=path)
            return _with_python(failed, show_python, "scene_create_light", **python)
        return _with_python(_authoring_payload(router, result), show_python, "scene_create_light", **python)

    @app.tool(name="scene_create_camera")
    def scene_create_camera(
        path: str,
        focal_length: float | None = None,
        horizontal_aperture: float | None = None,
        clipping_range: list[float] | None = None,
        translation: list[float] | None = None,
        aim_at: str | list[float] | None = None,
        copy_optics_from: str | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan a camera at path in the LIVE open scene and show the preview to the user first.
        dry_run=False changes the user's open scene (not saved; scene_undo_last can undo it). clipping_range is
        [near, far]; aim_at is a prim path or an [x, y, z] point; copy_optics_from copies another camera's lens.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(path=path, focal_length=focal_length, translation=translation, aim_at=aim_at)
        try:
            result = router._scene.create_camera(
                path,
                focal_length=focal_length,
                horizontal_aperture=horizontal_aperture,
                clipping_range=_numbers(clipping_range, 2, "clipping_range"),
                translation=_numbers(translation, 3, "translation"),
                aim_at=_target_arg(aim_at, "aim_at"),
                copy_optics_from=copy_optics_from.strip() if isinstance(copy_optics_from, str) and copy_optics_from.strip() else None,
                dry_run=dry_run,
            )
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=bool(dry_run), applied=False, path=path)
            return _with_python(failed, show_python, "scene_create_camera", **python)
        return _with_python(_authoring_payload(router, result), show_python, "scene_create_camera", **python)

    @app.tool(name="scene_aim")
    def scene_aim(path: str, target: str | list[float], dry_run: Any = True, show_python: bool = False) -> dict:
        """DRY RUN BY DEFAULT: plan rotating a light, camera or other prim in the LIVE open scene so its -Z axis faces
        target (a prim path or an [x, y, z] point), and show the preview to the user first. dry_run=False changes
        the user's open scene (not saved; scene_undo_last can undo it).
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        try:
            aimed_at = _target_arg(target, "target")
            if aimed_at is None:
                raise ValueError("target is required: a prim path or an [x, y, z] point")
            result = router._scene.aim(path, aimed_at, dry_run=dry_run)
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=bool(dry_run), applied=False, path=path)
            return _with_python(failed, show_python, "scene_aim", path=path, target=target)
        return _with_python(_authoring_payload(router, result), show_python, "scene_aim", path=path, target=target)

    # --- safe attribute / material edits and undo (dry run by default) -------------------------------------------

    @app.tool(name="scene_set_attribute")
    def scene_set_attribute(
        path: str,
        name: str,
        value: Any,
        time: float | None = None,
        create_type: str | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan setting one attribute (e.g. inputs:intensity on a light, focalLength on a camera,
        primvars:displayColor as [[r, g, b]]) on a prim in the open scene and show the preview to the user first: it
        lists the value before and where it comes from, the value coerced to the attribute's USD type, and whether a
        stronger opinion would hide it. dry_run=False changes the user's open scene (not saved; scene_undo_last can
        undo it). time=None sets the default value, a number writes a time sample; create_type (e.g. 'float',
        'color3f') creates a missing custom attribute.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(path=path, name=name, value=value, create_type=create_type)
        try:
            result = router._scene.set_attribute(
                path, name, value, time=time, create_type=create_type, dry_run=dry_run
            )
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=path)
            return _with_python(failed, show_python, "scene_set_attribute", **python)
        payload = _authoring_payload(router, result)
        if payload.get("preview"):
            payload["message"] = _EDIT_PREVIEW
        inner = result.payload if isinstance(result.payload, dict) else {}
        if create_type is None and isinstance(inner.get("type_name"), str):
            # The attribute's real USD type (e.g. radius is a double): guessing from the JSON value would give
            # 'float' for 2.0, and the snippet would then refuse the existing attribute.
            python["create_type"] = inner["type_name"]
        return _with_python(payload, show_python, "scene_set_attribute", **python)

    @app.tool(name="scene_create_material")
    def scene_create_material(
        path: str,
        diffuse_color: list[float] | str | None = None,
        roughness: float | None = None,
        metallic: float | None = None,
        opacity: float | None = None,
        emissive_color: list[float] | str | None = None,
        ior: float | None = None,
        create_parent: bool = True,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan a UsdPreviewSurface material at path (a Material with a PreviewSurface shader
        wired to outputs:surface) in the open scene and show the preview to the user first. dry_run=False changes the
        user's open scene (not saved; scene_undo_last can undo it). Colors are [r, g, b] in 0-1 (or '#RRGGBB');
        roughness, metallic and opacity are 0-1; only the given inputs are authored. create_parent also plans a
        missing direct parent such as /World/Looks as a Scope. Bind it with scene_bind_material.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(path=path, diffuse_color=diffuse_color, roughness=roughness, metallic=metallic)
        try:
            result = router._scene.create_material(
                path,
                diffuse_color=_color_arg(diffuse_color, "diffuse_color"),
                roughness=roughness,
                metallic=metallic,
                opacity=opacity,
                emissive_color=_color_arg(emissive_color, "emissive_color"),
                ior=ior,
                dry_run=dry_run,
                create_parent=create_parent is not False,
            )
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=path)
            return _with_python(failed, show_python, "scene_create_material", **python)
        payload = _authoring_payload(router, result)
        if payload.get("preview"):
            payload["message"] = _EDIT_PREVIEW
        return _with_python(payload, show_python, "scene_create_material", **python)

    @app.tool(name="scene_bind_material")
    def scene_bind_material(
        prim_path: str,
        material_path: str,
        strength: str | None = None,
        purpose: str | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan binding a material to a prim (MaterialBindingAPI, material:binding) in the open
        scene and show the preview to the user first: it reports the material the prim resolves to now and predicts
        the one after, warning when another binding still wins. dry_run=False changes the user's open scene (not
        saved; scene_undo_last can undo it). strength: weakerThanDescendants (default) or strongerThanDescendants;
        purpose: allPurpose (default), preview or full.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(prim_path=prim_path, material_path=material_path, strength=strength)
        try:
            result = router._scene.bind_material(
                prim_path, material_path, strength=strength, purpose=purpose, dry_run=dry_run
            )
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=prim_path)
            return _with_python(failed, show_python, "scene_bind_material", **python)
        payload = _authoring_payload(router, result)
        if payload.get("preview"):
            payload["message"] = _EDIT_PREVIEW
        return _with_python(payload, show_python, "scene_bind_material", **python)

    @app.tool(name="scene_undo_last")
    def scene_undo_last(dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: preview which scene change made through the router (the most recent one) would be
        undone, and show that preview to the user first. dry_run=False changes the user's open scene: it restores
        the edited prims (in Kit the same edit is also on the Ctrl+Z history). It refuses when the stage was replaced
        or the prims changed since. Saving is not undoable: undo reverts the open layers in memory; save again to
        keep it."""
        dry_run = dry_run is not False
        result = router._scene.undo_last(dry_run=dry_run)
        payload: dict[str, Any] = {"target": "scene", **result}
        payload["applied"] = bool(result.get("ok")) and result.get("dry_run") is False
        if result.get("needs_bridge_update"):
            payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
        elif result.get("ok") and result.get("dry_run") is not False:
            payload["preview"] = True
            payload["message"] = _UNDO_PREVIEW
        return payload

    @app.tool(name="scene_undo_history")
    def scene_undo_history(limit: int = 20) -> dict:
        """Read-only: the recent scene changes made through the router that scene_undo_last can undo, newest first
        (label, prim paths, layer, age), and whether Kit's Ctrl+Z history also has them. limit is 1-50."""
        result = router._scene.undo_history(limit)
        payload: dict[str, Any] = {"target": "scene", **result}
        if result.get("needs_bridge_update"):
            payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
        return payload

    # --- assets (router-side library) and add_reference (dry run by default) --------------------------------------

    @app.tool(name="asset_search")
    def asset_search(query: str, top_k: int = 10, category: str | None = None) -> dict:
        """Read-only: search the local Isaac Sim 6.1 asset pack (robots, environments, props, sensors, materials,
        people) by file and folder name, e.g. 'franka', 'warehouse shelf', 'carter' or 'cube'. Returns ranked absolute
        USD file paths with category, size and thumbnail; category narrows it ('robots', 'props', ...). Nothing in the
        open scene or the asset pack changes; scene_add_asset previews adding one."""
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            return _error("top_k must be a whole number from 1 to 200", target="assets", query=query)
        return {"target": "assets", **router._scene.asset_search(query, top_k=top_k, category=category)}

    @app.tool(name="asset_info")
    def asset_info(asset_path: str) -> dict:
        """Read-only: one asset file's absolute path, category, size, thumbnail and (cheaply, from its header) its
        defaultPrim, upAxis and metersPerUnit. asset_path is absolute or relative to the asset pack. Nothing changes."""
        return {"target": "assets", **router._scene.asset_info(asset_path)}

    @app.tool(name="scene_add_asset")
    def scene_add_asset(
        prim_path: str,
        asset_path: str,
        translation: list[float] | None = None,
        rotation: list[float] | None = None,
        scale: float | list[float] | None = None,
        instanceable: bool | None = None,
        correct_units: bool = False,
        asset_prim_path: str | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan adding an asset file (find one with asset_search) to the open scene as a new Xform
        at prim_path that references it, and show the preview to the user first: it lists the reference, the
        translate/rotateXYZ/scale ops and any upAxis or metersPerUnit mismatch with the stage. dry_run=False changes
        the user's open scene (not saved; scene_undo_last can undo it). correct_units=True fills an unspecified
        rotation/scale with the suggested up-axis and unit correction; asset_prim_path picks a prim in an asset
        without a defaultPrim. The asset file itself is only read, never changed.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(prim_path=prim_path, asset_path=asset_path, asset_prim_path=asset_prim_path)
        try:
            result = router._scene.add_asset(
                prim_path,
                asset_path,
                translation=_numbers(translation, 3, "translation"),
                rotation=_numbers(rotation, 3, "rotation"),
                scale=_scale_arg(scale),
                instanceable=instanceable if isinstance(instanceable, bool) else None,
                asset_prim_path=asset_prim_path.strip() if isinstance(asset_prim_path, str) and asset_prim_path.strip() else None,
                correct_units=correct_units is True,
                dry_run=dry_run,
            )
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=prim_path)
            return _with_python(failed, show_python, "scene_add_asset", **python)
        payload = _authoring_payload(router, result)
        inner = result.payload if isinstance(result.payload, dict) else {}
        for key in ("suggestions", "asset_path"):
            if key in inner:
                payload[key] = inner[key]
        return _with_python(payload, show_python, "scene_add_asset", **python)

    # --- UsdPhysics setup (report read-only; edits dry run by default) ------------------------------------------

    @app.tool(name="scene_physics_report")
    def scene_physics_report(prim_path: str | None = None, file: str | None = None, mode: str = "auto") -> dict:
        """Read-only: the physics setup of the open scene (or one subtree): PhysicsScene prims and gravity, rigid
        bodies, colliders, mass, collision approximations, articulation roots and joints, with warnings about common
        mistakes (a body without a collider, no PhysicsScene, ...). mode auto/live/saved_file as for the insight
        tools: auto reads the live stage and falls back to the saved file when the bridge cannot. Nothing changes."""
        return _physics_report_mode(router, prim_path, file, mode)

    @app.tool(name="scene_add_physics_scene")
    def scene_add_physics_scene(
        path: str = "/physicsScene",
        gravity: float | list[float] | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan a UsdPhysics PhysicsScene (nothing simulates without one) in the open scene and
        show the preview to the user first. dry_run=False changes the user's open scene (not saved; scene_undo_last
        can undo it). gravity is m/s^2: a number pointing down the stage up axis, or [x, y, z] written Z-up (default
        9.81 down); it is converted to the stage's units.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        try:
            result = router._scene.add_physics_scene(path, gravity=_scale_arg(gravity, "gravity"), dry_run=dry_run)
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=path)
            return _with_python(failed, show_python, "scene_add_physics_scene", path=path, gravity=gravity)
        return _with_python(_authoring_payload(router, result), show_python, "scene_add_physics_scene", path=path,
                            gravity=gravity)

    @app.tool(name="scene_make_rigid_body")
    def scene_make_rigid_body(
        prim_path: str,
        mass: float | None = None,
        density: float | None = None,
        kinematic: bool = False,
        add_collider: bool = True,
        approximation: str | None = None,
        dry_run: Any = True,
        show_python: bool = False,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan making a prim a rigid body (RigidBodyAPI, optional MassAPI with mass in kg or
        density in kg/m^3, and colliders on its geometry when add_collider=True) in the open scene, and show the
        preview to the user first. dry_run=False changes the user's open scene (not saved; scene_undo_last can undo
        it). kinematic=True makes an animated body that pushes others but is not pushed; approximation is
        convexHull, convexDecomposition, boundingCube, boundingSphere, meshSimplification or none.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(prim_path=prim_path, mass=mass, add_collider=add_collider)
        try:
            result = router._scene.make_rigid_body(
                prim_path,
                mass=mass,
                density=density,
                kinematic=kinematic is True,
                add_collider=add_collider is not False,
                approximation=approximation,
                dry_run=dry_run,
            )
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=prim_path)
            return _with_python(failed, show_python, "scene_make_rigid_body", **python)
        return _with_python(_authoring_payload(router, result), show_python, "scene_make_rigid_body", **python)

    @app.tool(name="scene_make_collider")
    def scene_make_collider(
        prim_path: str, approximation: str | None = None, dry_run: Any = True, show_python: bool = False
    ) -> dict:
        """DRY RUN BY DEFAULT: plan adding collision (CollisionAPI) to a gprim, or to every gprim below a group prim,
        in the open scene and show the preview to the user first. dry_run=False changes the user's open scene (not
        saved; scene_undo_last can undo it). approximation (convexHull, convexDecomposition, boundingCube,
        boundingSphere, meshSimplification or none) applies to meshes; a collider without a rigid body is static.
        show_python=True also returns the OpenUSD Python for the same change (result['python'])."""
        dry_run = dry_run is not False
        python = dict(prim_path=prim_path, approximation=approximation)
        try:
            result = router._scene.make_collider(prim_path, approximation=approximation, dry_run=dry_run)
        except (ValueError, TypeError) as exc:
            failed = _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=prim_path)
            return _with_python(failed, show_python, "scene_make_collider", **python)
        return _with_python(_authoring_payload(router, result), show_python, "scene_make_collider", **python)

    @app.tool(name="scene_remove_physics")
    def scene_remove_physics(prim_path: str, include_descendants: bool = False, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan removing the RigidBody, Collision, MeshCollision and Mass APIs (and their physics
        attributes) from a prim in the open scene, and show the preview to the user first. dry_run=False changes the
        user's open scene (not saved; scene_undo_last can undo it). include_descendants=True also clears every prim
        below; articulation roots and joints are left alone."""
        dry_run = dry_run is not False
        try:
            result = router._scene.remove_physics(
                prim_path, include_descendants=include_descendants is True, dry_run=dry_run
            )
        except (ValueError, TypeError) as exc:
            return _error(str(exc), target="scene", dry_run=dry_run is not False, applied=False, path=prim_path)
        return _authoring_payload(router, result)

    # --- viewport screenshots and simulation controls (Kit only) -------------------------------------------------

    @app.tool(name="scene_capture_viewport")
    def scene_capture_viewport(output_dir: str | None = None, width: int | None = None, height: int | None = None) -> dict:
        """Screenshot of the live app's ACTIVE viewport, saved as a new PNG (never overwriting one) in output_dir
        (an absolute folder; default <temp>/omniverse_mcp_captures). It does not change the open scene, the camera or
        any render setting; it returns at once with status 'pending': call scene_capture_status(path) until ready.
        width/height make a resized copy once the PNG is written."""
        problem = None
        if output_dir is not None and (not isinstance(output_dir, str) or not os.path.isabs(output_dir.strip())):
            problem = "output_dir must be an absolute folder path (or omitted for the default capture folder)"
        for label, value in (("width", width), ("height", height)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 16384):
                problem = f"{label} must be a whole number of pixels from 1 to 16384"
        if problem:
            return _error(problem, target="scene", action="capture_viewport")
        directory = output_dir.strip() if isinstance(output_dir, str) else None
        return _live_payload(router._scene.capture_viewport(directory, width=width, height=height))

    @app.tool(name="scene_capture_status")
    def scene_capture_status(path: str) -> dict:
        """Read-only: whether a scene_capture_viewport PNG has been written yet (ready, size, and the resized copy when
        a size was asked for). Never waits; nothing changes."""
        if not isinstance(path, str) or not path.strip():
            return _error("path is required: the 'path' scene_capture_viewport returned", target="scene")
        return _live_payload(router._scene.capture_status(path.strip()))

    @app.tool(name="sim_status")
    def sim_status() -> dict:
        """Read-only: whether the live app's simulation (timeline and physics) is playing, paused or stopped, the
        current time, and whether PhysX is available. Nothing changes."""
        return _live_payload(router._scene.simulation_status())

    @app.tool(name="sim_play")
    def sim_play() -> dict:
        """CHANGES THE SIMULATION STATE (not authored scene data): plays the live app's timeline, so physics simulates
        and PhysX moves simulated prims on the open stage until sim_stop restores them. Call it only when the user
        asks to play or run the simulation; nothing is saved."""
        return _live_payload(router._scene.simulation_play())

    @app.tool(name="sim_pause")
    def sim_pause() -> dict:
        """CHANGES THE SIMULATION STATE (not authored scene data): pauses the live app's timeline at the current time;
        sim_step then advances it frame by frame. Call it only when the user asks to pause."""
        return _live_payload(router._scene.simulation_pause())

    @app.tool(name="sim_stop")
    def sim_stop() -> dict:
        """CHANGES THE SIMULATION STATE (not authored scene data): stops the live app's timeline, RESETS it to the
        start time and PhysX restores the poses simulated prims had before play. Call it only when the user asks to
        stop the simulation."""
        return _live_payload(router._scene.simulation_stop())

    @app.tool(name="sim_step")
    def sim_step(frames: int = 1) -> dict:
        """CHANGES THE SIMULATION STATE (not authored scene data): advances a paused simulation by frames
        (1-600; larger requests are capped), stepping physics each frame when PhysX is available. Refused while
        playing (sim_pause first) or stopped (sim_play, then sim_pause, then step, so sim_stop can restore every
        body). Call it only when the user asks to step."""
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
            return _error("frames must be a whole number from 1 to 600", target="scene", action="simulation_step")
        return _live_payload(router._scene.simulation_step(frames))

    # --- exam dashboard -------------------------------------------------------------------------------------------

    @app.tool(name="learning_exam_dashboard")
    def learning_exam_dashboard(write: bool = True) -> dict:
        """Your NCP-OUSD progress dashboard as data: accuracy and status per weighted domain, weighted accuracy,
        weakest objectives, reviews due, streak, today's plan, mock-exam history, question-bank coverage and what to
        focus on next. With write=True (the default) it also saves a self-contained page (no scripts or network) as
        dashboard.html in the exam-prep folder next to progress.json, replacing the previous dashboard. Your progress
        itself is only read, never changed."""
        return exam_dashboard_result(progress_store, mock_store, write=write is True)

    # --- learning Python, homework and class notes (read-only unless a docstring says otherwise) -----------------

    class_notes_holder: dict[str, ClassNotes] = {}

    def _class_notes() -> ClassNotes:
        """One lazily indexed ClassNotes per server (the folders are read on the first question)."""
        # setdefault is atomic, so two first questions on worker threads share one index (ClassNotes() reads nothing).
        return class_notes_holder.setdefault("notes", ClassNotes())

    def _practice_passed() -> set[str]:
        """Exercise ids with a passing python_practice attempt in the saved exam progress (empty when unreadable)."""
        try:
            history = progress_store.history(limit=200)
        except (OSError, ValueError, TypeError):
            return set()
        return {
            str(row.get("question_id")) for row in history
            if row.get("source") == python_practice.SOURCE and row.get("is_correct") is True
        }

    def _error_knowledge() -> list[Any]:
        """The offline USD docs plus the installed Isaac Sim / Kit docs that exist here (each indexes lazily)."""
        backends: list[Any] = [offline_usd]
        for name in ("isaac_sim", "kit"):
            knowledge = docs_knowledge.get(name)
            if knowledge is not None and getattr(knowledge, "available", False):
                backends.append(knowledge)
        return backends

    @app.tool(name="python_show")
    def python_show(action: str, args: dict[str, Any] | None = None, context: str = "script_editor") -> dict:
        """Read-only, runs nothing: "show me the Python" for one OpenUSD task, e.g. action='move' with
        args={'prim_path': '/World/Cube', 'translate': [0, 0, 1]}. Returns a short runnable snippet, a plain-English
        explanation of every line, the USD concepts it uses, where to run it, what it would change and the
        NCP-OUSD objectives it practices. context 'script_editor' (paste into Isaac Sim / Composer: Window > Script
        Editor; it edits the open scene) or 'standalone' (a .py file with usd-core that opens a .usda file). Router
        tool names work as actions too (scene_move, scene_create_light, ...); python_topics lists them all."""
        return python_teacher.python_for(action, args, context=context if isinstance(context, str) else "")

    @app.tool(name="python_topics")
    def python_topics() -> dict:
        """Read-only: every task python_show can explain (inspect, create, move/rotate/scale, attributes, lights,
        cameras, materials, references, variants, layers, physics, save/export, undo), grouped, with the
        arguments each one takes and their defaults."""
        return python_teacher.list_python_topics()

    @app.tool(name="python_practice_list")
    def python_practice_list(level: str | None = None) -> dict:
        """Read-only: the OpenUSD Python practice exercises in suggested order (level 'beginner' or 'intermediate'
        narrows it), each with its goal, exam objectives and whether you have passed it, plus the next one to do.
        Starter code and solutions are not included; python_practice_get shows one exercise."""
        listing = python_practice.list_exercises(level)
        if not listing.get("ok"):
            return listing
        passed = _practice_passed()
        for row in listing["exercises"]:
            row["passed"] = row["id"] in passed
        pending = [row["id"] for row in listing["exercises"] if not row["passed"]]
        return {
            **listing,
            "passed_count": sum(1 for row in listing["exercises"] if row["passed"]),
            "next_exercise": pending[0] if pending else None,
            "how_to_practice": "python_practice_get(exercise_id) shows the task and starter code; "
            "python_practice_run(exercise_id, code) runs your answer on a throwaway copy of the scene and checks it.",
        }

    @app.tool(name="python_practice_get")
    def python_practice_get(exercise_id: str | None = None, reveal: bool = False, level: str | None = None) -> dict:
        """Read-only: one Python practice exercise: goal, starter code, hints, what is checked and the starting
        scene. exercise_id=None picks your next exercise not yet passed (level narrows the choice). The solution is
        only included with reveal=True; try first, it is how you learn."""
        chosen = exercise_id.strip() if isinstance(exercise_id, str) and exercise_id.strip() else None
        picked = False
        if chosen is None:
            listing = python_practice.list_exercises(level)
            if not listing.get("ok"):
                return listing
            passed = _practice_passed()
            rows = listing["exercises"]
            if not rows:
                return {"ok": False, "error": "No exercises match that level.", "levels": listing.get("levels")}
            pending = [row for row in rows if row["id"] not in passed]
            chosen = (pending or rows)[0]["id"]
            picked = True
        result = python_practice.get_exercise(chosen, reveal=reveal is True)
        if result.get("ok"):
            result["passed_before"] = chosen in _practice_passed()
            if picked:
                result["why_this_one"] = "Your next exercise not passed yet, in the suggested order."
            result["how_to_answer"] = "python_practice_run(exercise_id, code) runs and checks your answer."
        return result

    @app.tool(name="python_practice_run")
    async def python_practice_run(exercise_id: str, code: str, timeout_s: float = 10, record: bool = True) -> dict:
        """Run YOUR answer to a Python practice exercise and check it. The code runs in a separate Python process on
        a throwaway temporary copy of the exercise scene (never your real scenes), is stopped after timeout_s
        seconds (1-60) and is graded by automatic checks with hints. With record=True (the default) the graded
        result is saved to your NCP-OUSD exam progress (source python_practice); record=False saves nothing."""
        result = await asyncio.to_thread(python_practice.run_attempt, exercise_id, code, timeout_s=timeout_s)
        if record is True and result.get("ok") and isinstance(result.get("passed"), bool):
            try:
                result["progress"] = python_practice.record_progress(progress_store, exercise_id, result["passed"])
            except (TypeError, ValueError, OSError) as exc:
                result["progress"] = _error(f"Checked but not saved to progress: {exc}")
        else:
            result["progress"] = {"saved": False, "reason": "record=False" if record is not True else "not graded"}
        return result

    @app.tool(name="explain_error")
    async def explain_error(text: str, code: str | None = None, search_docs: bool = True) -> dict:
        """Read-only: explain an error you pasted (a Python traceback, a Kit [Error] line, a USD/Tf message, a PhysX
        warning) in plain English: what happened, likely causes, numbered fix steps, a short example fix and where
        in your code it happened (code= is the script that failed, used only to show the line). Nothing in the text
        or code is ever run. search_docs=True adds related pages from the offline USD and Isaac Sim / Kit docs."""
        knowledge = _error_knowledge() if search_docs is True else None
        return await asyncio.to_thread(error_explainer.explain_error, text, code=code, knowledge=knowledge)

    @app.tool(name="assignment_list")
    def assignment_list() -> dict:
        """Read-only: the homework assignments the checker knows (id, title, class session, summary and how many
        rules it checks). Check one with assignment_check."""
        found = assignments.list_assignments()
        return {"ok": True, "count": len(found), "assignments": found, "default": assignments.DEFAULT_ASSIGNMENT}

    @app.tool(name="assignment_check")
    def assignment_check(assignment_id: str = assignments.DEFAULT_ASSIGNMENT, scene_file: str | None = None) -> dict:
        """Read-only: check your homework scene against the class rubric (default: Isaac Sim session 02, the
        warehouse with walls, shelves, a 10-cube stack, a camera and lights) and say in plain words what passes,
        what is missing and the ONE next step, with how-to-fix steps for each rule. Without scene_file it checks
        the LIVE open stage in Isaac Sim / Composer (unsaved work included); scene_file checks a saved .usda opened
        read-only (relative to the workspace). Nothing is ever changed or saved."""
        wanted = assignment_id.strip() if isinstance(assignment_id, str) and assignment_id.strip() else assignments.DEFAULT_ASSIGNMENT
        if isinstance(scene_file, str) and scene_file.strip():
            try:
                report = assignments.check_scene_file(scene_file.strip(), wanted, workspace_root=_workspace_root())
            except (SceneFileError, ValueError, ImportError, ReadOnlyViolation) as exc:
                return _error(str(exc), action="assignment_check", mode="saved_file", file=scene_file)
            except Exception as exc:  # noqa: BLE001 - pxr raises Tf errors for unreadable layers
                return _error(f"{type(exc).__name__}: {exc}", action="assignment_check", mode="saved_file", file=scene_file)
            return {**report, "mode": "saved_file", "action": "assignment_check", "note": SAVED_FILE_NOTE}
        live = router._scene.assignment_check(wanted)
        return _homework_live_or_file(
            router, "assignment_check", live, lambda stage: assignments.check_assignment(stage, wanted)
        )

    @app.tool(name="scene_explain")
    def scene_explain(
        focus_path: str | None = None, depth: int = 3, max_prims: int = 400, scene_file: str | None = None
    ) -> dict:
        """Read-only: explain my scene in plain language for a beginner: groups, geometry (size and where it sits),
        lights, cameras, materials, physics, layers and referenced assets, each with one friendly sentence, plus a
        short overview, a glossary of the USD words used and questions to explore next. focus_path limits it to one
        prim and everything below; depth (0-50) is how many group levels to list; max_prims (1-20000) caps the walk.
        Without scene_file it reads the LIVE open stage; scene_file reads a saved file read-only. Nothing changes."""
        focus = focus_path.strip() if isinstance(focus_path, str) and focus_path.strip() else None
        try:
            levels = _whole_arg(depth, "depth", 0, _TOUR_MAX_DEPTH)
            limit = _whole_arg(max_prims, "max_prims", 1, _TOUR_MAX_PRIMS)
        except ValueError as exc:
            return _error(str(exc), action="scene_explain")

        def tour(stage: Any) -> dict:
            budget = usd_insight.TimeBudget(10_000)  # file mode: stop a huge scene after 10 s with partial results
            return scene_tour_module.scene_tour(stage, focus_path=focus, depth=levels, max_prims=limit, deadline=budget)

        if isinstance(scene_file, str) and scene_file.strip():
            return {**_file_mode(router, scene_file, tour), "action": "scene_explain"}
        live = router._scene.scene_tour(focus, depth=levels, max_prims=limit)
        return {**_homework_live_or_file(router, "scene_tour", live, tour), "action": "scene_explain"}

    @app.tool(name="class_sessions")
    async def class_sessions() -> dict:
        """Read-only: the mentorship class sessions found in your class folder (subtitles, Gemini notes) and your
        revision notes: session number, date, sources and length. The folders are only read, never changed."""
        notes = _class_notes()
        try:
            sessions = await asyncio.to_thread(notes.sessions)
            stats = await asyncio.to_thread(notes.stats)
        except Exception as exc:  # noqa: BLE001 - unreadable folders are reported
            return _error(f"Could not read the class notes: {type(exc).__name__}: {exc}", sessions=[])
        payload: dict[str, Any] = {"ok": True, "count": len(sessions), "sessions": sessions, "sources": stats}
        if not sessions:
            payload["message"] = (
                "No class sessions found under " + "; ".join(stats.get("roots") or []) + ". Set "
                "OMNIVERSE_CLASS_NOTES_ROOT to your class recordings folder."
            )
        return payload

    @app.tool(name="class_ask")
    async def class_ask(question: str, session: int | str | None = None, top_k: int = 6) -> dict:
        """Read-only: what did the teacher say about something? Searches the class subtitles, Gemini meeting notes
        and your revision notes offline and returns the best passages grouped by session, each with its timestamp
        and source, plus a one-line hint. session (e.g. 2) limits it to one class; top_k is 1-50. Subtitles are
        auto-generated, so check odd words against the notes."""
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            return _error("top_k must be a whole number from 1 to 50", question=question)
        try:
            return await asyncio.to_thread(_class_notes().ask, question, top_k=top_k, session=session)
        except Exception as exc:  # noqa: BLE001
            return _error(f"Could not search the class notes: {type(exc).__name__}: {exc}", question=question)

    @app.tool(name="class_search")
    async def class_search(query: str, session: int | str | None = None, top_k: int = 5) -> dict:
        """Read-only: keyword search of the class subtitles, Gemini notes and revision notes; returns ranked
        passages with session, source, timestamp and text. session limits it to one class; top_k is 1-50."""
        if isinstance(top_k, bool) or not isinstance(top_k, int):
            return _error("top_k must be a whole number from 1 to 50", query=query, results=[])
        try:
            hits = await asyncio.to_thread(_class_notes().search, query, top_k=top_k, session=session)
        except Exception as exc:  # noqa: BLE001
            return _error(f"Could not search the class notes: {type(exc).__name__}: {exc}", query=query, results=[])
        payload: dict[str, Any] = {"ok": bool(hits), "query": query, "session": session, "count": len(hits), **hits.as_dict()}
        if not hits:
            payload["message"] = "Nothing in the indexed class sources matched; try other words or class_sessions."
        return payload

    # --- round 7: timed drills, today's plan, the reminder text, the change journal, smart layout, robots, Docker --

    @app.tool(name="learning_drill_focuses")
    def learning_drill_focuses() -> dict:
        """Read-only: the kinds of timed NCP-OUSD drill you can start (weak, mistakes, mixed, objective, domain,
        flash), what each one picks, the default and allowed question count and seconds per question, and how to
        answer."""
        return exam_drills.list_drill_focuses()

    @app.tool(name="learning_drill_start")
    def learning_drill_start(
        focus: str = "weak",
        objective: str | None = None,
        domain: str | None = None,
        count: int = 10,
        seconds_per_question: float = 45,
    ) -> dict:
        """Start a short TIMED NCP-OUSD drill (default 10 questions, 45 seconds each) and return the questions without
        their answers. focus: 'weak' (your weak spots and due reviews, the default), 'mistakes' (questions you got
        wrong), 'mixed' (whole exam), 'objective' (needs objective such as '1.6'), 'domain' (needs domain such as
        'Composition') or 'flash' (quick true/false and one-word prompts). It only saves the drill session (beside
        your exam progress); answer everything at once with learning_drill_answer, which grades and saves to
        progress. Questions of an open mock exam are never used."""
        return exam_drills.start_drill(
            progress_store,
            focus=focus,
            objective=objective,
            domain=domain,
            count=count,
            seconds_per_question=seconds_per_question,
            exclude_ids=_open_mock_ids(),
        )

    @app.tool(name="learning_drill_answer")
    def learning_drill_answer(drill_id: str, answers: dict[str, Any] | list[Any], elapsed_s: float | None = None) -> dict:
        """Grade a whole drill in one go and SAVE the answers to your NCP-OUSD exam progress (flash prompts are not
        saved). answers: a dict {question number or id: your answer} or a list in question order; an answer is a
        letter ('B'), true/false or one word, as each question says. elapsed_s is how long you took (else measured
        from the start). An unreadable answer leaves the drill open so you can send it again."""
        return exam_drills.answer_drill(drill_id, answers, elapsed_s=elapsed_s, store=progress_store)

    @app.tool(name="learning_drill_status")
    def learning_drill_status(drill_id: str) -> dict:
        """Read-only: time left and the questions again for an open drill, or the saved result of an answered one."""
        return exam_drills.drill_status(drill_id, store=progress_store)

    def _brief_store() -> Any:
        """The exam progress, read without ever writing: the memory store itself, or a read-only copy of the file."""
        if getattr(progress_store, "memory_only", False) or getattr(progress_store, "path", None) is None:
            return progress_store
        return exam_reminder.read_only_store(progress_store.path)

    @app.tool(name="learning_today")
    def learning_today(minutes: int = 60) -> dict:
        """Read-only: 'what should I do today?' A short study brief from your saved NCP-OUSD progress: today's plan
        day, days to the exam, a time-boxed plan for `minutes` (10-480) of study (lesson, due reviews, a drill on weak
        areas, Python practice), your streak and a catch-up hint. Nothing is written."""
        return exam_reminder.today_brief(_brief_store(), minutes=minutes)

    @app.tool(name="learning_exam_week")
    def learning_exam_week(days: int = 7) -> dict:
        """Read-only: the last `days` (3-14) before your NCP-OUSD exam date as a day-by-day agenda built from your
        saved progress: two timed full mocks each followed by a review day, drills on the domains with the most marks
        at risk (weight x missed), a light last day and an exam-day pacing checklist, plus a ready / not-yet verdict
        with reasons. Every step names the tool to call. Nothing is started or written."""
        from .exam import dashboard as exam_dashboard_module
        from .exam import exam_week

        try:
            data = exam_dashboard_module.dashboard_data(progress_store, mock_store=mock_store)
        except Exception as exc:  # noqa: BLE001 - an unreadable store is reported, not raised
            return _error(f"Could not read your exam progress: {type(exc).__name__}: {exc}")
        return exam_week.exam_week(data, days=days)

    @app.tool(name="learning_isaac_lab")
    def learning_isaac_lab(objective_id: str | None = None, lab_id: str | None = None) -> dict:
        """Read-only: the hands-on Isaac Sim lab for an NCP-OUSD objective (e.g. '1.8') or a lab id: a plain-language
        concept, then steps that each name the exact tool and arguments and what to look for in the viewport. Reads
        are called directly; edits go through scene_safe_edit. Labs run in the study stage isaac_study/study.usda,
        never a course scene. No arguments lists the labs. Code-only objectives (plugins, resolvers) say so."""
        from .exam import isaac_labs

        return isaac_labs.lab(objective_id=objective_id, lab_id=lab_id)

    @app.tool(name="learning_interview_questions")
    def learning_interview_questions(topic: str | None = None, level: str | None = None, count: int = 5,
                                     seed: int | None = None) -> dict:
        """Read-only: job-interview practice questions on OpenUSD, composition, performance, physics, Isaac Sim,
        pipelines, debugging and your own projects (answers withheld). topic and level ('junior' or 'mid') filter;
        seed fixes the shuffle. Answer out loud, then type it into learning_interview_answer for feedback."""
        from .exam import interview

        return interview.questions(topic=topic, level=level, count=count, seed=seed)

    @app.tool(name="learning_interview_answer")
    def learning_interview_answer(question_id: str, answer: str) -> dict:
        """Read-only coaching for one interview answer: the key points you covered and missed, a score and rating,
        tips (length, a concrete example) and a short model answer plus a likely follow-up question. Keyword-based:
        it checks which ideas came up, not whether every sentence is right. Nothing is saved."""
        from .exam import interview

        return interview.grade(question_id, answer)

    @app.tool(name="learning_reminder_setup")
    def learning_reminder_setup(at: str = "19:00") -> dict:
        """Read-only, creates NOTHING: returns the PowerShell commands YOU can copy to create (and later remove) a daily
        Windows reminder at `at` (24-hour 'HH:MM') that pops up today's study brief. No task is registered and no
        file is written by this tool; it only shows the text."""
        result = exam_reminder.scheduled_task_commands(at)
        if not isinstance(result, dict):
            result = {"ok": False, "message": "Could not build the reminder commands."}
        return {
            **result,
            "created": False,
            "message": result.get("message") or (
                "Nothing was created: no scheduled task, no file. Copy create_command into PowerShell yourself if you "
                "want the reminder; remove_command deletes it later."
            ),
        }

    def _journal_since(value: Any) -> Any:
        """'today' / 'yesterday' as dates for ChangeJournal.entries; anything else as given."""
        if isinstance(value, str) and value.strip().lower() in ("today", "yesterday"):
            day = datetime.now().astimezone().date()
            return (day - timedelta(days=1) if value.strip().lower() == "yesterday" else day).isoformat()
        return value

    @app.tool(name="scene_journal")
    def scene_journal(
        limit: int = 20,
        since: str | None = None,
        prim: str | None = None,
        tool: str | None = None,
        target: str | None = None,
    ) -> dict:
        """Read-only: the change journal, newest first: every scene edit the router actually APPLIED (previews are
        never recorded), each with its time, app (isaac / composer / fake), tool, prims, before/after values and undo
        id, plus one plain sentence per change. Filters: limit (1-500), since ('today', 'yesterday', '2026-10-22' or
        '2026-10-22T14:00'), prim (that prim and everything below it), tool ('scene_move' or 'move') and target."""
        try:
            count = _whole_arg(limit, "limit", 1, 500)
            entries = change_journal.entries(since=_journal_since(since), limit=count, prim=prim, tool=tool, target=target)
        except ValueError as exc:
            return _error(str(exc), entries=[])
        return {
            "ok": True,
            "count": len(entries),
            "entries": entries,
            "sentences": change_journal.explain(entries),
            "journal": change_journal.status(),
            "note": "Only applied edits, saves and undos are recorded; dry runs are not. scene_journal_summary tells "
                    "the story of one day.",
        }

    @app.tool(name="scene_journal_explain")
    def scene_journal_explain(
        limit: int = 10,
        since: str | None = None,
        prim: str | None = None,
        tool: str | None = None,
        target: str | None = None,
    ) -> dict:
        """Read-only: 'what did you change?' in plain sentences, newest first, such as '14:02 - You moved /World/Cube
        from (0, 0, 0) to (1, 2, 3) in Composer. Undo with scene_undo_last (id 7).' Same filters as scene_journal."""
        try:
            count = _whole_arg(limit, "limit", 1, 500)
            sentences = change_journal.explain(
                since=_journal_since(since), limit=count, prim=prim, tool=tool, target=target
            )
        except ValueError as exc:
            return _error(str(exc), sentences=[])
        return {"ok": True, "count": len(sentences), "sentences": sentences}

    @app.tool(name="scene_journal_summary")
    def scene_journal_summary(day: str | None = None) -> dict:
        """Read-only: the story of one day of scene changes (default today; 'yesterday' or 'YYYY-MM-DD'): how many
        edits, undos and failed attempts, counts by tool and prim, and three short sentences."""
        try:
            return {"ok": True, **change_journal.summary(day)}
        except ValueError as exc:
            return _error(str(exc))

    @app.tool(name="scene_journal_clear")
    def scene_journal_clear(confirm: bool = False) -> dict:
        """Empty the change journal (only the journal file; scenes are never touched). Nothing happens unless
        confirm=True, and the old entries are kept in a timestamped backup file next to the journal."""
        return change_journal.clear(confirm=confirm is True)

    def _layout_payload(result: Any) -> dict:
        """Scene-tool envelope for a layout result, with the moves, the plain explanation and the preview table."""
        payload = _authoring_payload(router, result)
        inner = result.payload if isinstance(result.payload, dict) else {}
        for key in ("explanation", "moves"):
            if key in inner:
                payload[key] = inner[key]
        if isinstance(inner.get("preview"), str):
            payload["preview_table"] = inner["preview"]
        return payload

    def _layout_call(name: str, paths: Any, dry_run: Any, call: Callable[[], Any]) -> dict:
        dry_run = dry_run is not False
        try:
            result = call()
        except (ValueError, TypeError) as exc:
            first = paths[0] if isinstance(paths, list) and paths else paths
            return _error(str(exc), target="scene", action=name, dry_run=dry_run is not False, applied=False, path=first)
        return _layout_payload(result)

    @app.tool(name="scene_layout_snap")
    def scene_layout_snap(paths: list[str], surface: str | float | None = None, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan dropping (or lifting) prims straight down the up axis so the bottom of each one
        rests on a surface, and show the preview to the user first. surface: None finds the floor automatically,
        a prim path uses that prim's top, a number is a height. Only each prim's translation changes. dry_run=False
        changes the user's open scene (not saved; scene_undo_last can undo it)."""
        dry_run = dry_run is not False
        return _layout_call("layout_snap", paths, dry_run,
                            lambda: router._scene.layout_snap(paths, surface=surface, dry_run=dry_run))

    @app.tool(name="scene_layout_stack")
    def scene_layout_stack(
        paths: list[str],
        rows: list[int] | None = None,
        anchor: str | list[float] | None = None,
        gap: float = 0.0,
        facing: str | None = None,
        dry_run: Any = True,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan stacking prims in rows like a pyramid or wall (rows bottom first; default
        [4, 3, 2, 1] for 10 boxes; the counts must add up to the number of paths), each row resting on the one below,
        and show the preview to the user first. anchor: an [x, y, z] point (bottom center), a prim path to stand on,
        or None (where the prims are now); gap: space between neighbors; facing: e.g. '-Y'. dry_run=False changes
        the user's open scene (not saved; scene_undo_last can undo it)."""
        dry_run = dry_run is not False
        return _layout_call(
            "layout_stack", paths, dry_run,
            lambda: router._scene.layout_stack(
                paths, rows=rows, anchor=_target_arg(anchor, "anchor"), gap=gap, facing=facing, dry_run=dry_run
            ),
        )

    @app.tool(name="scene_layout_distribute")
    def scene_layout_distribute(paths: list[str], surface_path: str, margin: float | None = None, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan spreading prims evenly along the long side of a shelf or table top (surface_path),
        all resting on it and centered across its depth, with `margin` free at both ends (default 5 cm), and show the
        preview to the user first. dry_run=False changes the user's open scene (not saved; scene_undo_last can undo
        it)."""
        dry_run = dry_run is not False
        return _layout_call(
            "layout_distribute", paths, dry_run,
            lambda: router._scene.layout_distribute(paths, surface_path, margin=margin, dry_run=dry_run),
        )

    @app.tool(name="scene_layout_space")
    def scene_layout_space(paths: list[str], axis: str = "x", spacing: float | None = None, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan making the gaps between prims equal along one world axis ('x', 'y' or 'z'), and
        show the preview to the user first. spacing=None keeps the first and last prim in place; a number sets the
        edge-to-edge gap in stage units. dry_run=False changes the user's open scene (not saved; scene_undo_last can
        undo it)."""
        dry_run = dry_run is not False
        return _layout_call(
            "layout_space", paths, dry_run,
            lambda: router._scene.layout_space(paths, axis=axis, spacing=spacing, dry_run=dry_run),
        )

    @app.tool(name="scene_layout_align")
    def scene_layout_align(paths: list[str], axis: str = "x", mode: str = "min", dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan lining prims up along one world axis by their bounding-box min sides ('min'),
        centers ('center') or max sides ('max'), and show the preview to the user first. dry_run=False changes the
        user's open scene (not saved; scene_undo_last can undo it)."""
        dry_run = dry_run is not False
        return _layout_call(
            "layout_align", paths, dry_run,
            lambda: router._scene.layout_align(paths, axis=axis, mode=mode, dry_run=dry_run),
        )

    @app.tool(name="robot_inspect")
    def robot_inspect(root_path: str | None = None, scene_file: str | None = None, max_prims: int = 5000) -> dict:
        """Read-only: explain a robot for a beginner: every articulation (robot) with its links (rigid parts, mass,
        colliders), its joints in plain words (hinge, slider, fixed) with axis, limits and motor (drive) settings,
        the chain from the base, degrees of freedom, whether the base is fixed, and setup problems with how to fix
        them. Without scene_file it reads the LIVE open stage (root_path limits it to one robot); scene_file reads a
        saved scene or a robot asset (e.g. under isaacsim-assets-6.1.0) read-only. Nothing is changed."""
        root = root_path.strip() if isinstance(root_path, str) and root_path.strip() else None
        try:
            limit = _whole_arg(max_prims, "max_prims", 1, _ROBOT_MAX_PRIMS)
        except ValueError as exc:
            return _error(str(exc), action="robot_inspect")

        def inspect_file(stage: Any) -> dict:
            budget = usd_insight.TimeBudget(10_000)  # file mode: stop a huge asset after 10 s with partial results
            return robot_inspector.inspect_robot(stage, root, deadline=budget, max_prims=limit)

        if isinstance(scene_file, str) and scene_file.strip():
            return {**_file_mode(router, scene_file, inspect_file), "action": "robot_inspect"}
        live = router._scene.robot_inspect(root, max_prims=limit)
        return {**_homework_live_or_file(router, "robot_inspect", live, inspect_file, hint=_ROBOT_FILE_HINT),
                "action": "robot_inspect"}

    @app.tool(name="docs_docker_status")
    async def docs_docker_status(service: str = "usd", check_endpoint: bool = False) -> dict:
        """Read-only: is Docker installed and running, are the NVIDIA docs MCP images built and is the container up
        (service 'usd', 'kit', 'omniui' or 'isaac'). Runs only read-only Docker queries (version, image ls, ps);
        never pulls, builds, starts or stops anything. check_endpoint=True also tries a TCP connect to the local
        docs port. API keys are only reported as set or not set, never shown."""
        try:
            return await asyncio.to_thread(docker_docs.docker_status, service, check_endpoint is True)
        except ValueError as exc:
            return _error(str(exc), service=service)

    @app.tool(name="docs_docker_setup_plan")
    def docs_docker_setup_plan(service: str = "usd") -> dict:
        """Read-only, runs nothing: the ordered steps and exact commands YOU would run to set up the Docker docs MCP
        for service ('usd', 'kit', 'omniui' or 'isaac'), each marked when it needs your approval (downloads, keys)."""
        try:
            return docker_docs.setup_plan(service)
        except ValueError as exc:
            return _error(str(exc), service=service)

    @app.tool(name="docs_docker_explain")
    def docs_docker_explain() -> dict:
        """Read-only: is Docker worth it? Plain-English pros and cons of the Docker docs MCP versus the router's
        offline docs (which already answer USD, Kit and Isaac Sim questions without Docker)."""
        return docker_docs.explain_choice(None)

    # --- round 8: project starter, layer manager, checkpoints, Kit logs, asset health, journal script, report ------

    @app.tool(name="project_templates")
    def project_templates() -> dict:
        """Read-only: the project templates you can start from (blank, warehouse, digital_twin, configurator,
        robot_cell, synthetic_data), each with its app, units, up axis, default prim and layer stack (strongest
        first). Nothing is written; project_plan previews a project and project_create makes it."""
        return project_starter.list_templates()

    @app.tool(name="project_plan")
    def project_plan(
        name: str, template: str = "blank", goal: str = "", units: str | float | None = None, up_axis: str | None = None
    ) -> dict:
        """Read-only, writes NOTHING: the folder a new project would get inside the workspace (root.usda with one
        sublayer per concern, assets/ layers/ scenes/ renders/ scripts/ docs/, PROJECT_CONTEXT.md, README.md and a
        .gitignore), its units, up axis, layer stack and starter prims. name: letters, digits, '_' and '-' only.
        units: 'm', 'cm' or 'mm' (default: the template's); up_axis: 'Y' or 'Z'."""
        return project_starter.plan_project(name, template, goal=goal or "", units=units, up_axis=up_axis)

    @app.tool(name="project_create")
    def project_create(
        name: str,
        template: str = "blank",
        goal: str = "",
        units: str | float | None = None,
        up_axis: str | None = None,
        dry_run: Any = True,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan a NEW project folder <workspace>/<name> from a template and show the preview to
        the user first. dry_run=False writes ONLY that new folder (files only; it never touches the open scene in
        Isaac Sim or Composer), refuses a folder that already exists and removes everything it wrote if a step
        fails. Then open root.usda in the app and fill in PROJECT_CONTEXT.md."""
        dry_run = dry_run is not False
        result = project_starter.create_project(
            name, template, goal=goal or "", units=units, up_axis=up_axis, dry_run=dry_run is not False
        )
        return {**result, "action": "create_project"}

    @app.tool(name="project_list")
    def project_list() -> dict:
        """Read-only: the project folders directly inside the workspace (those with a PROJECT_CONTEXT.md), each with
        its goal, scene entry file and last change."""
        return project_starter.list_projects()

    def _layer_read(method: str, scene_file: Any, file_action: Callable[[Any], dict], live: Callable[[], dict]) -> dict:
        """A read-only layer answer: scene_file read-only, else the live stage, else the live scene's saved file."""
        if isinstance(scene_file, str) and scene_file.strip():
            return {**_file_mode(router, scene_file, file_action), "action": method}
        try:
            result = live()
        except ValueError as exc:
            return _error(str(exc), action=method)
        return {**_homework_live_or_file(router, method, result, file_action, hint=_LAYER_FILE_HINT), "action": method}

    @app.tool(name="scene_layers")
    def scene_layers(scene_file: str | None = None) -> dict:
        """Read-only: the layer stack, strongest first (session layer, root layer, then its sublayers depth-first),
        with each layer's strength rank, the edit target, muted and missing layers, unsaved edits and the layers
        that references and payloads bring in, explained for a beginner (the 'L' in LIVRPS). Without scene_file it
        reads the LIVE open stage, else the live scene's SAVED file when the bridge cannot; scene_file reads a saved
        file read-only. Nothing is changed."""
        return _layer_read("layer_stack", scene_file, layer_manager.layer_stack, lambda: router._scene.layer_stack())

    @app.tool(name="scene_where_is")
    def scene_where_is(prim_path: str, attribute: str | None = None, scene_file: str | None = None) -> dict:
        """Read-only: every layer that holds an opinion on a prim (or one attribute: attribute='size', or
        prim_path='/World/Cube.size'), strongest first, and which opinion wins and why. Without scene_file it reads
        the LIVE open stage, else the live scene's SAVED file when the bridge cannot; scene_file reads a saved file
        read-only. Nothing is changed."""
        name = attribute.strip() if isinstance(attribute, str) and attribute.strip() else None
        return _layer_read(
            "layer_where_is", scene_file,
            lambda stage: layer_manager.where_is(stage, prim_path, name),
            lambda: router._scene.layer_where_is(prim_path, name),
        )

    @app.tool(name="scene_flatten_preview")
    def scene_flatten_preview(prim_path: str, max_chars: int = 4000, scene_file: str | None = None) -> dict:
        """Read-only: what ONE prim looks like with every layer and composition arc baked into a single layer, as
        .usda text (max_chars 200-20000). Nothing is flattened, saved or changed. Without scene_file it reads the
        LIVE open stage, else the live scene's SAVED file when the bridge cannot; scene_file reads a saved file
        read-only."""
        try:
            limit = _whole_arg(max_chars, "max_chars", _FLATTEN_MIN_CHARS, _FLATTEN_MAX_CHARS)
        except ValueError as exc:
            return _error(str(exc), action="layer_flatten_preview")
        if not isinstance(prim_path, str) or not prim_path.strip().startswith("/") or prim_path.strip() == "/":
            return _error("prim_path must name one prim, such as '/World/Cube'.", action="layer_flatten_preview")
        path = prim_path.strip()
        return _layer_read(
            "layer_flatten_preview", scene_file,
            lambda stage: layer_manager.flatten_preview(stage, path, max_chars=limit),
            lambda: router._scene.layer_flatten_preview(path, max_chars=limit),
        )

    def _layer_write(method: str, dry_run: Any, call: Callable[[], dict]) -> dict:
        """Scene envelope for a writing layer call: preview / applied / refused, with how to put it back."""
        dry_run = dry_run is not False
        try:
            result = call()
        except (ValueError, TypeError) as exc:
            return _error(str(exc), target="scene", action=method, dry_run=dry_run is not False, applied=False)
        payload: dict[str, Any] = {"target": "scene", **result}
        payload.setdefault("action", method)
        if payload.get("needs_bridge_update"):
            payload["how_to_enable"] = list(LIVE_ENABLE_STEPS)
        restore = payload.get("restore_with") or payload.get("undo")
        if payload.get("ok") is not True:
            detail = payload.get("error") or payload.get("message") or "The layer change did not go through."
            payload["applied"] = False
            payload["message"] = f"{detail} Nothing was changed."
        elif payload.get("applied"):
            payload["message"] = (
                "Applied to the open stage (not saved: save the changed layer to keep it). "
                + (f"Put it back with {restore}." if isinstance(restore, dict) else "")
            ).strip()
        elif payload.get("dry_run") is not False:
            payload["preview"] = True
            payload["message"] = (
                "Preview only: nothing was changed. Show this to the user, then call again with dry_run=False to "
                "apply it."
            )
        else:
            payload["message"] = payload.get("explanation") or "Nothing to change."
        return payload

    @app.tool(name="scene_add_sublayer")
    def scene_add_sublayer(
        layer_path: str,
        position: str | int = "strongest",
        create_if_missing: bool = False,
        relative: bool = True,
        allow_outside: bool = False,
        offset: float = 0.0,
        scale: float = 1.0,
        dry_run: Any = True,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan inserting a sublayer (a .usd/.usda/.usdc file, relative to the root layer's
        folder) into the root layer's sublayer list and show the preview to the user first. position 'strongest'
        (first, wins over the others), 'weakest' or an index; create_if_missing writes a new empty layer file;
        offset/scale only shift time. Refuses duplicates, cycles and files outside the root layer's folder (unless
        allow_outside). dry_run=False changes the user's open scene (not saved); the result's restore_with is the
        call that takes it out again."""
        dry_run = dry_run is not False
        return _layer_write("layer_add_sublayer", dry_run, lambda: router._scene.layer_add_sublayer(
            layer_path, position=position, create_if_missing=create_if_missing, relative=relative,
            allow_outside=allow_outside, offset=offset, scale=scale, dry_run=dry_run,
        ))

    @app.tool(name="scene_remove_sublayer")
    def scene_remove_sublayer(layer: str, discard_unsaved_edits: bool = False, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan taking a sublayer (by file name, path or identifier) out of its parent's sublayer
        list and show the preview to the user first; the file on disk is never deleted. Refuses the root and session
        layers, the edit target, and a layer with unsaved edits unless discard_unsaved_edits=True. dry_run=False
        changes the user's open scene (not saved); restore_with puts it back."""
        dry_run = dry_run is not False
        return _layer_write("layer_remove_sublayer", dry_run, lambda: router._scene.layer_remove_sublayer(
            layer, discard_unsaved_edits=discard_unsaved_edits, dry_run=dry_run,
        ))

    @app.tool(name="scene_set_edit_target")
    def scene_set_edit_target(layer: str, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan making one layer of the layer stack the edit target (where new edits are
        written) and show the preview to the user first. dry_run=False changes the user's open scene (stage state;
        no layer is edited); restore_with sets the old target back."""
        dry_run = dry_run is not False
        return _layer_write("layer_set_edit_target", dry_run,
                            lambda: router._scene.layer_set_edit_target(layer, dry_run=dry_run))

    @app.tool(name="scene_mute_layer")
    def scene_mute_layer(layer: str, discard_unsaved_edits: bool = False, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan muting a layer so the stage ignores its opinions (and its sublayers') and show the
        preview to the user first. Refuses the root and session layers, the edit target, and a layer with unsaved
        edits unless discard_unsaved_edits=True. dry_run=False changes the user's open scene (stage state, nothing is
        saved); scene_unmute_layer brings it back."""
        dry_run = dry_run is not False
        return _layer_write("layer_mute", dry_run, lambda: router._scene.layer_mute(
            layer, discard_unsaved_edits=discard_unsaved_edits, dry_run=dry_run,
        ))

    @app.tool(name="scene_unmute_layer")
    def scene_unmute_layer(layer: str, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan unmuting a muted layer so its opinions count again and show the preview to the
        user first. dry_run=False changes the user's open scene (stage state, nothing is saved)."""
        dry_run = dry_run is not False
        return _layer_write("layer_unmute", dry_run, lambda: router._scene.layer_unmute(layer, dry_run=dry_run))

    @app.tool(name="scene_move_opinions")
    def scene_move_opinions(
        prim_path: str,
        src_layer: str,
        dst_layer: str,
        include_children: bool = True,
        properties: list[str] | None = None,
        allow_changes: bool = False,
        dry_run: Any = True,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan moving a prim's opinions (its whole spec, or only properties=[...]) from one
        layer of the layer stack to another and show the preview to the user first. The move is simulated on copies
        and every composed value it would change is listed; dry_run=False refuses such a move unless
        allow_changes=True. dry_run=False changes the user's open scene (not saved; scene_undo_last can undo it)."""
        dry_run = dry_run is not False
        return _layer_write("layer_move_opinions", dry_run, lambda: router._scene.layer_move_opinions(
            prim_path, src_layer, dst_layer, include_children=include_children, properties=properties,
            allow_changes=allow_changes, dry_run=dry_run,
        ))

    def _checkpoint_call(action: str, scene_file: Any, run: Callable[[str], dict], mutating: Any = None) -> dict:
        """Run one checkpoints.py call on scene_file, or on the live scene's saved file; errors become ok False."""
        extra = {} if mutating is None else {"dry_run": mutating is not False, "applied": False}
        try:
            scene = scene_file.strip() if isinstance(scene_file, str) and scene_file.strip() else _live_scene_file(router)
            result = run(scene)
        except (checkpoints.CheckpointError, SceneFileError, ValueError, ImportError) as exc:
            return _error(str(exc), action=action, scene_file=scene_file, **extra)
        except OSError as exc:
            return _error(f"{type(exc).__name__}: {exc}", action=action, scene_file=scene_file, **extra)
        return {**result, "action": action}

    @app.tool(name="scene_checkpoint")
    def scene_checkpoint(scene_file: str | None = None, note: str = "", dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan a checkpoint (a restorable copy of the SAVED scene file and the local layers it
        uses, inside its project folder) and show the preview to the user first. scene_file defaults to the live
        scene's saved file. dry_run=False copies the files into <project>/.checkpoints/ (a folder git ignores); the
        open scene is never changed, and unsaved changes in the app are not included: save first."""
        dry_run = dry_run is not False
        return _checkpoint_call(
            "checkpoint", scene_file,
            lambda scene: checkpoints.create_checkpoint(scene, note or "", dry_run=dry_run is not False),
            mutating=dry_run,
        )

    @app.tool(name="scene_checkpoints")
    def scene_checkpoints(scene_file: str | None = None) -> dict:
        """Read-only: the checkpoints of a scene (default: the live scene's saved file), oldest first, with their
        number, time, note and size."""
        return _checkpoint_call("checkpoints", scene_file, checkpoints.list_checkpoints)

    @app.tool(name="scene_diff")
    def scene_diff(scene_file: str | None = None, a: str | int | None = None, b: str | int = "current") -> dict:
        """Read-only: what changed between two versions of a scene's saved files, in plain English (prims added,
        removed, values changed) and as per-file diffs. a defaults to the latest checkpoint and b to 'current' (the
        files on disk now); each side is 'current', a checkpoint number or id, 'latest' or 'previous'."""
        return _checkpoint_call("diff", scene_file, lambda scene: checkpoints.diff(scene, a, b))

    @app.tool(name="scene_restore_checkpoint")
    def scene_restore_checkpoint(checkpoint: str | int, scene_file: str | None = None, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan putting a checkpoint's files back over the saved scene files and show the preview
        to the user first. dry_run=False first takes a safety checkpoint of the current files, then rewrites only the
        files the checkpoint lists, inside the project folder. IMPORTANT: if the scene is open in Isaac Sim or
        Composer, reload it there afterwards (File > Reload); unsaved changes in the app are lost and saving from the
        app would overwrite the restored files."""
        dry_run = dry_run is not False
        result = _checkpoint_call(
            "restore_checkpoint", scene_file,
            lambda scene: checkpoints.restore_checkpoint(scene, checkpoint, dry_run=dry_run is not False),
            mutating=dry_run,
        )
        warning = checkpoints.APP_RELOAD_WARNING
        warnings = [item for item in (result.get("warnings") or []) if item != warning]
        message = result.get("message") or result.get("error") or ""
        return {**result, "important": warning, "app_reload_warning": warning, "warnings": [warning, *warnings],
                "message": f"IMPORTANT: {warning} {message}".strip()}

    @app.tool(name="scene_prune_checkpoints")
    def scene_prune_checkpoints(scene_file: str | None = None, keep: int = 20, dry_run: Any = True) -> dict:
        """DRY RUN BY DEFAULT: plan deleting all but the newest `keep` checkpoints of a scene (keep >= 1, so the
        newest is never deleted) and show the preview to the user first. dry_run=False deletes only those checkpoint
        folders; scene files are never touched."""
        dry_run = dry_run is not False
        return _checkpoint_call(
            "prune_checkpoints", scene_file,
            lambda scene: checkpoints.prune(scene, keep, dry_run=dry_run is not False),
            mutating=dry_run,
        )

    def _log_app(app_name: Any) -> tuple[str, list[str]]:
        """'isaac' / 'composer' for a kit_* tool's app argument; 'current' follows the router's scene target."""
        wanted = app_name.strip().lower() if isinstance(app_name, str) and app_name.strip() else "current"
        if wanted in ("current", "auto", "active"):
            target = target_from_backend(router._scene.backend_info)
            if target in ("isaac", "composer"):
                return target, []
            return "isaac", [f"The router's scene target is {target or 'unknown'} (not Isaac Sim or Composer), so this "
                             "reads the Isaac Sim log; pass app='composer' for USD Composer."]
        if wanted in ("isaac", "isaacsim", "isaac sim", "isaac-sim", "isaac_sim"):
            return "isaac", []
        if wanted in ("composer", "usd composer", "usd_composer", "usd-composer"):
            return "composer", []
        raise kit_log_reader.KitLogError("app must be 'current', 'isaac' or 'composer'.")

    def _log_error(exc: Exception, app_name: Any, **extra: Any) -> dict:
        return _error(str(exc), app=app_name, read_only=True, log_dirs=kit_log_reader.log_dirs(), **extra)

    @app.tool(name="kit_logs")
    def kit_logs(app: str = "current", lines: int = 100, level: str = "warning") -> dict:
        """Read-only: the newest entries of the Isaac Sim or USD Composer log file (app 'current' follows the scene
        target, or 'isaac' / 'composer'), keeping `level` and worse ('verbose', 'info', 'warning', 'error',
        'fatal'); lines 1-2000. A traceback is one entry. Only Kit .log files are read; tokens, keys and your home
        folder are hidden. Nothing is changed."""
        try:
            name, notes = _log_app(app)
            count = _whole_arg(lines, "lines", 1, 2000)
            wanted = level.strip().lower() if isinstance(level, str) and level.strip() else None
            if wanted is not None and wanted not in _LOG_LEVELS:
                raise ValueError(f"level must be one of {list(_LOG_LEVELS)}")
            entries = kit_log_reader.tail(name, count, level=wanted)
            newest = (kit_log_reader.find_logs(name) or [None])[0]
        except (kit_log_reader.KitLogError, ValueError, OSError) as exc:
            return _log_error(exc, app)
        return {"ok": True, "app": name, "level": wanted, "count": len(entries), "entries": entries, "log": newest,
                "notes": notes, "read_only": True}

    @app.tool(name="kit_log_errors")
    def kit_log_errors(app: str = "current", cursor: str | None = None) -> dict:
        """Read-only: errors written to the app's log since `cursor`; call again with the returned cursor to keep
        watching (the first call, without a cursor, only starts watching from now). app 'current', 'isaac' or
        'composer'."""
        try:
            name, notes = _log_app(app)
            result = kit_log_reader.new_errors(name, cursor=cursor)
        except (kit_log_reader.KitLogError, ValueError, OSError) as exc:
            return _log_error(exc, app)
        return {**result, "notes": [*notes, *(result.get("notes") or [])], "read_only": True}

    def _explain_recent(app_name: Any, limit: Any) -> dict:
        try:
            name, notes = _log_app(app_name)
            count = _whole_arg(limit, "limit", 1, 20)
            result = kit_log_reader.explain_recent(name, limit=count, knowledge=_error_knowledge())
        except (kit_log_reader.KitLogError, ValueError, OSError) as exc:
            return _log_error(exc, app_name)
        return {**result, "notes": notes, "read_only": True}

    @app.tool(name="kit_explain_recent_errors")
    async def kit_explain_recent_errors(app: str = "current", limit: int = 5) -> dict:
        """Read-only: the latest distinct errors of the app's current log (limit 1-20), repeats grouped with a count,
        each explained in plain English (what happened, likely causes, fix steps, related offline docs) by the same
        explainer as explain_error; known harmless Kit messages are listed as noise. Nothing is run or changed."""
        return await asyncio.to_thread(_explain_recent, app, limit)

    @app.tool(name="kit_log_summary")
    def kit_log_summary(app: str = "current") -> dict:
        """Read-only: counts for the app's current log session: entries by level, by extension and by source, plus
        known noise, so you can see at a glance whether anything is wrong. app 'current', 'isaac' or 'composer'."""
        try:
            name, notes = _log_app(app)
            result = kit_log_reader.summary(name)
        except (kit_log_reader.KitLogError, ValueError, OSError) as exc:
            return _log_error(exc, app)
        return {**result, "notes": notes, "read_only": True}

    @app.tool(name="usd_asset_health")
    def usd_asset_health(scene_file: str | None = None, root_path: str | None = None, budget_ms: float = 8000) -> dict:
        """Read-only: is this asset OK to use? A 0-100 health score and grade with plain-language checks (missing or
        absolute file paths, units and up axis, heavy meshes, textures, materials, instancing, names, physics), each
        with why it matters and beginner fix steps, plus its file dependencies and stats. Without scene_file it
        checks the LIVE open stage (root_path limits it to one prim and below), else the live scene's SAVED file when
        the bridge cannot; scene_file checks a saved scene or asset read-only. budget_ms (100-10000) stops a huge
        asset with partial results. Nothing is fixed or saved."""
        if isinstance(budget_ms, bool) or not isinstance(budget_ms, (int, float)) or not math.isfinite(float(budget_ms)) \
                or not 100 <= float(budget_ms) <= _HEALTH_MAX_BUDGET_MS:
            return _error(f"budget_ms must be a number from 100 to {_HEALTH_MAX_BUDGET_MS}", action="asset_health")
        budget = float(budget_ms)
        root = root_path.strip() if isinstance(root_path, str) and root_path.strip() else None

        def check(stage: Any) -> dict:
            return asset_health_module.asset_health(stage, root_path=root, budget_ms=budget)

        if isinstance(scene_file, str) and scene_file.strip():
            return {**_file_mode(router, scene_file, check), "action": "asset_health"}
        live = router._scene.asset_health(root, budget_ms=budget)
        return {**_homework_live_or_file(router, "asset_health", live, check, hint=_HEALTH_FILE_HINT),
                "action": "asset_health"}

    @app.tool(name="usd_health_checks")
    def usd_health_checks() -> dict:
        """Read-only: every check usd_asset_health runs, with its title, the matching NVIDIA Asset Validator rule in
        Kit and how many points a warning or failure costs."""
        checks = asset_health_module.health_checks()
        return {"ok": True, "count": len(checks), "checks": checks}

    @app.tool(name="scene_journal_to_script")
    def scene_journal_to_script(
        context: str = "script_editor", since: str | None = None, limit: int = 50, title: str | None = None
    ) -> dict:
        """Read-only: turn what you did into ONE commented OpenUSD Python script that replays the edits the change
        journal recorded (oldest first; previews, failed edits, saves and undone edits are left out, each with the
        reason). context 'script_editor' (Isaac Sim / Composer Script Editor) or 'standalone' (usd-core); since
        'today', 'yesterday' or a date; limit 1-500 journal entries. The script is only shown: nothing runs, and
        running it later edits the scene again (try it on a copy)."""
        try:
            count = _whole_arg(limit, "limit", 1, 500)
            entries = change_journal.entries(since=_journal_since(since), limit=count)
        except ValueError as exc:
            return _error(str(exc), code=None, read_only=True)
        result = journal_script.script_from_journal(entries, context=context, title=title)
        return {**result, "journal_entries_read": len(entries), "read_only": True}

    def _project_folder(project_dir: Any) -> Path | None:
        """The project folder for a report: project_dir inside the workspace, else the live scene's project."""
        root = _workspace_root().resolve()
        if isinstance(project_dir, str) and project_dir.strip():
            candidate = Path(project_dir.strip())
            folder = (candidate if candidate.is_absolute() else root / candidate).resolve()
            if not folder.is_dir():
                raise ValueError(f"project_dir is not an existing folder: {folder}")
            if folder != root and root not in folder.parents:
                raise ValueError(f"project_dir must be inside the workspace {root}: {folder}")
            return folder
        try:
            scene = Path(_live_scene_file(router)).resolve()
        except (SceneFileError, ValueError):
            return None
        for folder in scene.parents:
            if folder == root or root not in folder.parents:
                break
            if (folder / project_report_module.CONTEXT_FILE).is_file():
                return folder
        return None

    @app.tool(name="project_report")
    def project_report(
        project_dir: str | None = None,
        title: str | None = None,
        include_tour: bool = True,
        include_health: bool = True,
        include_assignment: bool = False,
        write: bool = False,
        overwrite: bool = False,
    ) -> dict:
        """A portfolio report (Markdown and self-contained HTML) of a project: goal and next steps from its
        PROJECT_CONTEXT.md, the scene tour, the asset health score, today's change journal and optionally the
        homework check, gathered read-only. project_dir: a folder inside the workspace (default: the live scene's
        project). PREVIEW BY DEFAULT: nothing is written unless write=True, which saves report.md and report.html
        into <project_dir>/docs only (existing files are replaced only with overwrite=True)."""
        try:
            folder = _project_folder(project_dir)
        except ValueError as exc:
            return _error(str(exc), action="project_report", written=[])
        entry = folder / project_starter.ROOT_LAYER if folder is not None else None
        fallback = str(entry) if entry is not None and entry.is_file() else None
        gathered: dict[str, Any] = {}

        def gather(key: str, enabled: Any, run: Callable[[str | None], dict]) -> Any:
            if enabled is not True:
                gathered[key] = "skipped"
                return None
            result = run(None)
            if not result.get("ok") and fallback:
                result = run(fallback)
            gathered[key] = "ok" if result.get("ok") else f"not available: {result.get('message') or result.get('error')}"
            return result if result.get("ok") else None

        tour = gather("tour", include_tour, lambda file: scene_explain(scene_file=file))
        health = gather("health", include_health, lambda file: usd_asset_health(scene_file=file))
        assignment = gather("assignment", include_assignment, lambda file: assignment_check(scene_file=file))
        try:
            journal_summary = change_journal.summary(None)
            gathered["journal"] = "ok"
        except ValueError as exc:
            journal_summary = None
            gathered["journal"] = f"not available: {exc}"
        report = project_report_module.build_report(
            project_dir=folder, tour=tour, health=health, assignment=assignment, journal_summary=journal_summary,
            title=title,
        )
        payload: dict[str, Any] = {**report, "action": "project_report", "project_dir": str(folder) if folder else None,
                                   "gathered": gathered, "written": []}
        if not report.get("ok"):
            return payload
        if write is not True:
            payload["dry_run"] = True
            payload["message"] = ("Preview only: nothing was written. Call project_report(..., write=True) to save "
                                  "report.md and report.html into the project's docs folder.")
            return payload
        if folder is None:
            payload.update(ok=False, error="write=True needs project_dir (a project folder inside the workspace).")
            return payload
        docs = folder / "docs"
        if docs.is_symlink() or (docs.exists() and not docs.is_dir()):
            payload.update(ok=False, error=f"{docs} is not a plain folder, so nothing was written.")
            return payload
        if not docs.exists():
            docs.mkdir()
        written = project_report_module.write_report(docs, report, dry_run=False, overwrite=overwrite is True)
        payload.update(dry_run=False, write_result=written, written=written.get("written", []),
                       message=written.get("message") or written.get("error"))
        if not written.get("ok"):
            payload.update(ok=False, error=written.get("error"))
        return payload

    # The router's learning-helper sentences ("show me the python for ...", "check my homework", "explain my scene",
    # "what did the teacher say about ...") run these same tools.
    router.attach_learning_helpers(
        {
            "python_show": python_show,
            "python_topics": python_topics,
            "python_exercise": lambda level=None: python_practice_get(None, False, level),
            "python_practice_list": python_practice_list,
            "explain_error": lambda text="": error_explainer.explain_error(text, knowledge=_error_knowledge()),
            "assignment_check": lambda scene_file=None: assignment_check(scene_file=scene_file),
            "assignment_list": assignment_list,
            "scene_explain": lambda focus_path=None: scene_explain(focus_path=focus_path),
            "class_ask": lambda question, session=None: _class_notes().ask(question, session=session),
            "class_sessions": lambda: {"ok": True, "sessions": _class_notes().sessions()},
            # Round 7 sentences (nl_tier6.py): drills, today's plan, the reminder text, the journal, robots, Docker.
            "drill_start": lambda focus="weak", objective=None, domain=None, count=10: learning_drill_start(
                focus=focus, objective=objective, domain=domain, count=count
            ),
            "drill_focuses": learning_drill_focuses,
            "today": lambda minutes=60: learning_today(minutes=minutes),
            "reminder_setup": lambda at="19:00": learning_reminder_setup(at=at),
            "journal": lambda prim=None, since=None: scene_journal(prim=prim, since=since),
            "journal_summary": lambda day="today": scene_journal_summary(day=day),
            "robot_inspect": lambda root_path=None, scene_file=None: robot_inspect(root_path=root_path, scene_file=scene_file),
            "docker_status": lambda service="usd": docker_docs.docker_status(service, False),
            "docker_setup_plan": lambda service="usd": docs_docker_setup_plan(service=service),
            "docker_explain": docs_docker_explain,
            # Round 8 sentences (nl_tier7.py), keyed by tool name; write-ish ones always arrive as previews.
            "project_templates": project_templates,
            "project_list": project_list,
            "project_create": lambda name, template="blank", dry_run=True: project_create(
                name=name, template=template, dry_run=True
            ),
            "scene_layers": scene_layers,
            "scene_where_is": lambda prim_path, attribute=None: scene_where_is(prim_path=prim_path, attribute=attribute),
            "scene_add_sublayer": lambda layer_path, position="strongest", create_if_missing=False, dry_run=True:
                scene_add_sublayer(layer_path=layer_path, position=position, create_if_missing=create_if_missing,
                                   dry_run=True),
            "scene_mute_layer": lambda layer, dry_run=True: scene_mute_layer(layer=layer, dry_run=True),
            "scene_unmute_layer": lambda layer, dry_run=True: scene_unmute_layer(layer=layer, dry_run=True),
            "scene_checkpoint": lambda note="", dry_run=True: scene_checkpoint(note=note, dry_run=True),
            "scene_checkpoints": scene_checkpoints,
            "scene_diff": lambda a="latest", b="current": scene_diff(a=a, b=b),
            "scene_restore_checkpoint": lambda checkpoint="latest", dry_run=True: scene_restore_checkpoint(
                checkpoint=checkpoint, dry_run=True
            ),
            "kit_logs": lambda app="current", level="warning": kit_logs(app=app, level=level, lines=50),
            "kit_explain_recent_errors": lambda app="current": _explain_recent(app, 5),
            "usd_asset_health": lambda scene_file=None, root_path=None: usd_asset_health(
                scene_file=scene_file, root_path=root_path
            ),
            "scene_journal_to_script": scene_journal_to_script,
            "project_report": lambda project_dir=None, write=False: project_report(project_dir=project_dir, write=False),
        }
    )

    from . import features, profiles, workflows
    feature_tools = features.register(app, router, _file_mode)
    router.attach_learning_helpers(feature_tools)
    workflows.register_prompts(app)

    # Always-on meta tools keep a small profile useful without advertising every schema.
    stash = None

    @app.tool(name="router_list_tools")
    def router_list_tools(pattern: str = "*", include_schema: bool = False) -> dict:
        """List tool names and short descriptions, including tools hidden by the current profile."""
        import fnmatch
        rows = [{"name": t.name, "about": (t.description or "").split('\n')[0]}
                for t in app._tool_manager.list_tools() if fnmatch.fnmatchcase(t.name, pattern)]
        if stash is not None:
            rows += stash.describe(pattern)
        if include_schema:
            registry = dict(stash.hidden) if stash is not None else {}
            registry.update(app._tool_manager._tools)
            for row in rows:
                row['inputSchema'] = registry[row['name']].parameters
        return {"ok": True, "tools": sorted(rows, key=lambda r: r['name'])}

    @app.tool(name="router_enable_tools")
    async def router_enable_tools(names: str, ctx: Context) -> dict:
        """Expose more tools by profile, exact tool name, or pattern; no scene edits or configuration writes."""
        enabled = stash.enable(names) if stash is not None else []
        try:
            await ctx.session.send_tool_list_changed()
        except (AttributeError, RuntimeError):
            pass
        return {"ok": True, "enabled": enabled}

    @app.tool(name="router_call_tool")
    async def router_call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict:
        """Call a hidden tool with normal validation. Edits preview unless arguments explicitly set dry_run=False."""
        if name in {"router_call_tool", "router_enable_tools", "router_list_tools"}:
            return _error('Use meta tools directly; recursive meta calls are refused.')
        try:
            return await stash.call(name, arguments or {})
        except (KeyError, TypeError, ValueError, ToolError) as exc:
            return _error(str(exc))

    @app.tool(name="scene_safe_edit")
    async def scene_safe_edit(tool: str, arguments: dict[str, Any] | None = None, confirm: str | None = None,
                              checkpoint: bool = True) -> dict:
        """The safe way to change the scene, in two calls. 1) scene_safe_edit(tool='scene_move', arguments={...}):
        checks the arguments, snapshots the prims, previews the edit (nothing changes) and returns a confirm code.
        Show the preview to the user. 2) Only if they agree: the same call plus confirm='<code>': refuses if those
        prims changed since the preview, takes a checkpoint when the saved file matches the scene, applies, and
        reports before -> after with the exact undo call. Works with any tool that has a dry_run preview; never
        saves. Leave dry_run out of arguments."""
        from . import safe_edit

        def schema_of(name: str) -> dict | None:
            registry = dict(stash.hidden) if stash is not None else {}
            registry.update(app._tool_manager._tools)
            found = registry.get(name)
            return found.parameters if found is not None else None

        async def call(name: str, args: dict[str, Any]) -> Any:
            return await stash.call(name, args)

        return await safe_edit.safe_edit(call, schema_of, tool, arguments, confirm=confirm, checkpoint=checkpoint)

    @app.tool(name="router_doctor")
    def router_doctor() -> dict:
        """Read-only connection doctor: is the router healthy and running the latest code, do the Isaac Sim (9911) and
        Composer (9910) bridges answer with the current version or need a reload, does the router point at an open
        app, and recent Kit log errors. Ends with plain next steps for you; never starts, reloads or retargets
        anything. Also runs without the router: python -m omniverse_scene_mcp doctor."""
        from . import doctor, kit_logs

        report = check_router_health(
            router._scene.backend_info, router.backend_status()["backends"], workspace_root=_workspace_root())
        return doctor.diagnose(workspace=_workspace_root(), router_health_report=report, inside_router=True,
                               logs=lambda key: kit_logs.summary(key))

    @app.tool(name="router_workflow")
    def router_workflow(name: str | None = None, params: dict[str, Any] | None = None) -> dict:
        """Guided step-by-step workflows (the same recipes as this server's MCP prompts) as a normal tool, for
        clients that do not show MCP prompts, such as Codex. No name lists them; with a name, returns the steps
        to follow, one tool call at a time. Example: router_workflow(name='why_value',
        params={'prim_path': '/World/Box', 'attribute': 'size'})."""
        if name is None:
            return {"ok": True, "workflows": [
                {"name": key, "title": spec["title"], "about": spec["description"],
                 "params": {p[0]: p[2] for p in spec["params"]}} for key, spec in workflows.PROMPTS.items()],
                "how": "Call router_workflow(name=..., params={...}) and follow the steps it returns."}
        if params is not None and not isinstance(params, dict):
            return _error("params must be an object such as {'session': 3}")
        try:
            steps = workflows.render_prompt(name, **(params or {}))
        except (KeyError, TypeError) as exc:
            return _error(str(exc).strip("'\""), workflows=workflows.prompt_names())
        return {"ok": True, "name": name, "steps": steps}

    # Read-only tools say so (MCP readOnlyHint), so Codex and other clients can run them without an approval prompt
    # for every look-up; editing tools keep asking.
    from mcp.types import ToolAnnotations

    from .help import read_only_tools

    read_only = read_only_tools()
    for tool in app._tool_manager.list_tools():
        if tool.name in read_only:
            tool.annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

    profiles.install_compact_outputs(app)
    profiles.advertise_tool_list_changed(app)
    stash, profile_report = profiles.apply_profile_to_app(app)

    # Tool descriptions come from raw docstrings; drop the continuation-line indentation clients would see.
    for tool in app._tool_manager.list_tools():
        tool.description = inspect.cleandoc(tool.description or "")
    return app


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniverse-router-mcp")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "9905")))
    parser.add_argument(
        "--transport",
        default=os.environ.get("OMNIVERSE_MCP_TRANSPORT", "streamable-http"),
        choices=("stdio", "sse", "streamable-http"),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    app = build_server(host=args.host, port=args.port)
    app.run(transport=args.transport)
    return 0
