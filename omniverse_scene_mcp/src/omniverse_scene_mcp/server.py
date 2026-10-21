from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from mcp.server.fastmcp import FastMCP

from . import usd_insight, usd_validate
from .bridge import LIVE_RPC_METHODS
from .exam import bank as exam_bank
from .exam import blueprint as exam_blueprint
from .exam import daily as exam_daily
from .exam import puzzles as exam_puzzles
from .exam import sandbox as exam_sandbox
from .exam.mock import MockExamError
from .file_stage import ReadOnlyViolation, SceneFileError, readonly_stage, resolve_scene_file
from .health import router_health as check_router_health
from .offline_knowledge import OFFLINE_NOTE, OfflineUSDKnowledge
from .router import MOCK_EXAM_DEFAULTS, OmniverseRouter, RouteTarget, _local_usd_file
from .runtime import build_mock_store, build_progress_store, build_router


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
    "(isaacsim-study/bridge-reload-result.json should say status 'reloaded' and rpc_version 'tier2-1').",
    "Router: restart it (start-isaac-mcp.ps1 targets the Isaac bridge) so it loads this code and points at "
    "http://127.0.0.1:9911; Composer's 9910 extension only serves the basic scene methods.",
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
_AUTHORING_APPLIED = (
    "Applied to the user's open scene in memory: it is not saved (scene_save keeps it) and it is not on Kit's undo "
    "stack, so remove or re-aim it with another edit if it is wrong."
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
    for key in ("unsupported", "needs_bridge_update", "fake_scene", "warnings", "next_step", "error"):
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
        payload["message"] = _AUTHORING_APPLIED
    return payload


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
    # One exam progress store and one mock-exam store per server, shared by the exam tools and the router.
    progress_store = build_progress_store()
    mock_store = build_mock_store()
    router = build_router(offline_knowledge=offline_usd, progress_store=progress_store, mock_store=mock_store)

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

    @app.tool(name="router_dispatch")
    async def router_dispatch(request: str, dry_run: bool = False) -> dict:
        """Route a natural-language or command-style request to the right Omniverse MCP backend."""
        result = await router.handle(request, dry_run=dry_run)
        return {"decision": _decision_dict(result), "payload": result.payload}

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
    def scene_create(path: str, type_name: str = "Xform", dry_run: bool = False) -> dict:
        """Create a prim, optionally in dry-run mode."""
        return router._wrap_operation_result("scene", router._scene.create_prim(path, type_name, dry_run=dry_run))

    @app.tool(name="scene_delete")
    def scene_delete(path: str, dry_run: bool = False) -> dict:
        """Delete a prim, optionally in dry-run mode."""
        return router._wrap_operation_result("scene", router._scene.delete_prim(path, dry_run=dry_run))

    @app.tool(name="scene_duplicate")
    def scene_duplicate(source_path: str, target_path: str, dry_run: bool = False) -> dict:
        """Duplicate one prim to another path."""
        return router._wrap_operation_result(
            "scene",
            router._scene.duplicate_prim(source_path, target_path, dry_run=dry_run),
        )

    @app.tool(name="scene_reparent")
    def scene_reparent(path: str, new_parent: str, dry_run: bool = False) -> dict:
        """Reparent a prim under a new parent."""
        return router._wrap_operation_result("scene", router._scene.reparent_prim(path, new_parent, dry_run=dry_run))

    @app.tool(name="scene_move")
    def scene_move(
        path: str,
        translation: Sequence[float] | None = None,
        rotation: Sequence[float] | None = None,
        scale: Sequence[float] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Apply transform updates to a prim."""
        return router._wrap_operation_result(
            "scene",
            router._scene.move_prim(
                path,
                translation=_coerce_vector(translation),
                rotation=_coerce_vector(rotation),
                scale=_coerce_vector(scale),
                dry_run=dry_run,
            ),
        )

    @app.tool(name="scene_material")
    def scene_material(path: str, material_path: str, dry_run: bool = False) -> dict:
        """Bind a material to a prim."""
        return router._wrap_operation_result(
            "scene",
            router._scene.set_material(path, material_path, dry_run=dry_run),
        )

    @app.tool(name="scene_rename")
    def scene_rename(path: str, new_name: str, dry_run: bool = False) -> dict:
        """Rename a prim and keep its subtree intact."""
        return router._wrap_operation_result("scene", router._scene.rename_prim(path, new_name, dry_run=dry_run))

    @app.tool(name="scene_variants")
    def scene_variants(path: str = "/World/Car") -> dict:
        """List native USD variant selections on a prim."""
        return router._scene.get_variant_selections(path)

    @app.tool(name="scene_variant_set")
    def scene_variant_set(
        variant_set: str,
        selection: str,
        path: str = "/World/Car",
        dry_run: bool = False,
    ) -> dict:
        """Select a native USD variant on a prim."""
        return router._wrap_operation_result(
            "scene",
            router._scene.set_variant_selection(path, variant_set, selection, dry_run=dry_run),
        )

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
    def scene_save(path: str, dry_run: bool = False) -> dict:
        """Save the current live scene to a USD file; dry_run previews in-place save versus Save As."""
        return router._scene.save_scene(path, dry_run=dry_run)

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
        authoring, its RPC version, and whether an Isaac bridge reload is needed for the live features."""
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
        missing = sorted(set(LIVE_RPC_METHODS) - methods) if capabilities.get("ok") else list(LIVE_RPC_METHODS)
        needs_update = bool(capabilities.get("needs_bridge_update")) or (
            bool(capabilities.get("ok")) and not capabilities.get("fake_scene") and bool(missing)
        )
        if insight["available"] and authoring["available"]:
            message = "Live insight and light/camera authoring are available on the open stage."
        elif needs_update:
            message = "The live bridge needs a reload before live insight and authoring work; see how_to_enable."
        else:
            message = f"Live features are unavailable: {insight['reason'] or authoring['reason']}."
        payload: dict[str, Any] = {
            "ok": True,
            "backend": {key: backend.get(key) for key in ("mode", "bridge", "endpoint", "reason", "warnings")},
            "rpc_version": capabilities.get("version"),
            "live_insight": {**insight, "queries": list(capabilities.get("insight_queries") or [])},
            "live_authoring": authoring,
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
        dry_run: bool = True,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan a UsdLux light (rect, sphere, disk, distant, cylinder or dome) at path in the
        LIVE open scene and show the preview to the user first. dry_run=False changes the user's open scene (not
        saved, not on Kit's undo stack). size: one number, or [width, height] for rect / [length, radius] for
        cylinder; aim_at: a prim path or an [x, y, z] point the light's -Z axis should face."""
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
            return _error(str(exc), target="scene", dry_run=bool(dry_run), applied=False, path=path)
        return _authoring_payload(router, result)

    @app.tool(name="scene_create_camera")
    def scene_create_camera(
        path: str,
        focal_length: float | None = None,
        horizontal_aperture: float | None = None,
        clipping_range: list[float] | None = None,
        translation: list[float] | None = None,
        aim_at: str | list[float] | None = None,
        copy_optics_from: str | None = None,
        dry_run: bool = True,
    ) -> dict:
        """DRY RUN BY DEFAULT: plan a camera at path in the LIVE open scene and show the preview to the user first.
        dry_run=False changes the user's open scene (not saved, not on Kit's undo stack). clipping_range is
        [near, far]; aim_at is a prim path or an [x, y, z] point; copy_optics_from copies another camera's lens."""
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
            return _error(str(exc), target="scene", dry_run=bool(dry_run), applied=False, path=path)
        return _authoring_payload(router, result)

    @app.tool(name="scene_aim")
    def scene_aim(path: str, target: str | list[float], dry_run: bool = True) -> dict:
        """DRY RUN BY DEFAULT: plan rotating a light, camera or other prim in the LIVE open scene so its -Z axis faces
        target (a prim path or an [x, y, z] point), and show the preview to the user first. dry_run=False changes
        the user's open scene (not saved, not on Kit's undo stack)."""
        try:
            aimed_at = _target_arg(target, "target")
            if aimed_at is None:
                raise ValueError("target is required: a prim path or an [x, y, z] point")
            result = router._scene.aim(path, aimed_at, dry_run=dry_run)
        except (ValueError, TypeError) as exc:
            return _error(str(exc), target="scene", dry_run=bool(dry_run), applied=False, path=path)
        return _authoring_payload(router, result)

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
