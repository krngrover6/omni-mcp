"""scene_safe_edit: walk one scene edit through check -> preview -> confirm -> checkpoint -> apply -> verify.

Why a tool and not only a recipe: a cheaper client (Codex, Claude Pro) skips recipe steps, but it cannot skip what a
tool enforces. Two calls, same tool:

1. ``scene_safe_edit(tool, arguments)``: refuses file-level tools and unknown argument names, snapshots the prims the
   edit names, runs the edit as a dry run, says what the safety net is (router undo, a checkpoint) and returns a
   one-time confirm code. Nothing changes.
2. ``scene_safe_edit(tool, arguments, confirm=code)``: same tool and arguments only. Refuses when those prims changed
   since the preview, takes a checkpoint of the saved file when that captures the current scene, applies, snapshots
   again and reports before -> after with the exact undo / restore call.

Everything goes through ``call(name, arguments)``, the router's own tool runner, so normal validation applies.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any, Awaitable, Callable

Call = Callable[[str, dict[str, Any]], Awaitable[Any]]
SchemaOf = Callable[[str], "dict[str, Any] | None"]

CODE_TTL_SECONDS = 15 * 60
MAX_PATHS = 6
_SNAPSHOT_FIELDS = ("type_name", "translation", "rotation", "scale", "material")
_FILE_SUFFIXES = (".usd", ".usda", ".usdc", ".usdz", ".png", ".jpg", ".json", ".py")

# Tools that write files, reset progress or are the safety net themselves: the user runs these knowingly.
REFUSED: dict[str, str] = {
    "scene_save": "saving writes the scene file; ask the user, then call scene_save yourself",
    "scene_restore_checkpoint": "restoring rewrites saved files; it is the safety net, call it directly (preview first)",
    "scene_prune_checkpoints": "it deletes checkpoints; call it directly (preview first)",
    "scene_checkpoint": "a checkpoint is not a scene edit; call it directly",
    "scene_journal_clear": "it empties the change journal; call it directly",
    "scene_undo_last": "undo is the safety net itself; call scene_undo_last directly (preview first)",
    "learning_exam_reset": "it clears exam progress, not the scene",
    "project_create": "it creates project folders; call it directly (preview first)",
    "scene_safe_edit": "it cannot wrap itself",
}

_PENDING: dict[str, dict[str, Any]] = {}


def _fingerprint(tool: str, arguments: dict[str, Any]) -> str:
    text = json.dumps([tool, arguments], sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def prim_paths(arguments: Any) -> list[str]:
    """Prim paths an edit names: string values (and dict keys) that look like /Absolute/Prim/Paths."""
    found: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if (text.startswith("/") and len(text) < 500 and "\n" not in text and "\\" not in text
                    and not text.lower().endswith(_FILE_SUFFIXES) and text not in found):
                found.append(text)
        elif isinstance(value, dict):
            for key, item in value.items():
                visit(key)
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(arguments)
    return found[:MAX_PATHS]


def _failed(result: Any) -> str | None:
    """The error text of a failed tool result, else None."""
    if not isinstance(result, dict):
        return None
    if result.get("ok") is False or ("error" in result and result.get("ok") is not True):
        return str(result.get("error") or result.get("message") or "the tool reported ok=false")
    return None


def _undoable(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    undo = result.get("undo")
    payload = result.get("payload")
    return bool(result.get("undoable") is True or (isinstance(undo, dict) and undo.get("recorded"))
                or (isinstance(payload, dict) and payload.get("undoable") is True))


async def _safe_call(call: Call, name: str, arguments: dict[str, Any]) -> tuple[Any, str | None]:
    try:
        return await call(name, arguments), None
    except Exception as exc:  # noqa: BLE001 - a missing prim or a bridge error is an answer here, not a crash
        return None, str(exc) or type(exc).__name__


async def snapshot(call: Call, paths: list[str]) -> dict[str, Any]:
    """What each path looks like now: exists, type, transform, material and child count."""
    shots: dict[str, Any] = {}
    for path in paths:
        result, error = await _safe_call(call, "scene_inspect", {"path": path})
        if error is not None or not isinstance(result, dict) or _failed(result):
            shots[path] = {"exists": False}
            continue
        shot = {"exists": True, **{key: result.get(key) for key in _SNAPSHOT_FIELDS if key in result}}
        if isinstance(result.get("children"), list):
            shot["children"] = len(result["children"])
        shots[path] = shot
    return shots


def describe_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Plain lines such as '/World/Box: translation [0, 0, 0] -> [1, 2, 0]'."""
    lines: list[str] = []
    for path in after:
        old, new = before.get(path, {"exists": False}), after[path]
        if not old.get("exists") and new.get("exists"):
            lines.append(f"{path}: created ({new.get('type_name') or 'prim'})")
        elif old.get("exists") and not new.get("exists"):
            lines.append(f"{path}: removed")
        elif new.get("exists"):
            for key in sorted(set(old) | set(new)):
                if key != "exists" and old.get(key) != new.get(key):
                    lines.append(f"{path}: {key} {_show(old.get(key))} -> {_show(new.get(key))}")
    return lines


def _show(value: Any) -> str:
    """Short text for a snapshot value: [0, -2, 1.5] rather than (0.0, -2.0, 1.5) or 0.20000000298."""
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_show(item) for item in value) + "]"
    if isinstance(value, float):
        return f"{round(value, 6):g}"
    return str(value)


async def _scene_state(call: Call) -> dict[str, Any]:
    layers, error = await _safe_call(call, "scene_layers", {})
    if error is not None or not isinstance(layers, dict) or _failed(layers):
        return {"known": False}
    return {"known": True, "stage": layers.get("stage_identifier"),
            "anonymous": bool(layers.get("root_layer_anonymous")),
            "unsaved": bool(layers.get("has_unsaved_changes"))}


async def _undo_count(call: Call) -> int | None:
    history, error = await _safe_call(call, "scene_undo_history", {"limit": 1})
    if error is not None or not isinstance(history, dict):
        return None
    count = history.get("count")
    return count if isinstance(count, int) else None


def _checkpoint_plan(state: dict[str, Any]) -> tuple[bool, str]:
    if not state.get("known"):
        return False, "The scene's save state could not be read, so no checkpoint is taken; undo is the safety net."
    if state.get("anonymous"):
        return False, ("The scene was never saved to a file, so a checkpoint has nothing to copy; undo is the "
                       "safety net (save the scene first if you want a restorable copy).")
    if state.get("unsaved"):
        return False, ("The scene has unsaved changes, and a checkpoint copies the SAVED file only, so it would "
                       "miss them; undo is the safety net (save first if you want a checkpoint too).")
    return True, "The saved file matches the open scene, so a checkpoint is taken first and can restore it exactly."


async def safe_edit(
    call: Call,
    schema_of: SchemaOf,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    confirm: str | None = None,
    checkpoint: bool = True,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Step 1 without confirm (check and preview, changes nothing); step 2 with the confirm code (apply safely)."""
    arguments = dict(arguments or {})
    base = {"action": "safe_edit", "tool": tool}
    if not isinstance(tool, str) or not tool:
        return {**base, "ok": False, "error": "Name the editing tool, e.g. tool='scene_move'."}
    if tool in REFUSED:
        return {**base, "ok": False, "error": f"scene_safe_edit does not wrap {tool}: {REFUSED[tool]}."}
    schema = schema_of(tool)
    if schema is None:
        return {**base, "ok": False, "error": f"Unknown tool: {tool}. router_list_tools shows the names."}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or "dry_run" not in properties:
        return {**base, "ok": False, "error": (f"{tool} has no dry_run preview, so it is not a scene edit this "
                                               "routine can check first; call it directly.")}
    if "dry_run" in arguments:
        return {**base, "ok": False, "error": "Leave dry_run out: scene_safe_edit previews and applies for you."}
    from types import SimpleNamespace

    from .profiles import unknown_arguments

    problem = unknown_arguments(SimpleNamespace(name=tool, parameters=schema), arguments)
    if problem:
        return {**base, "ok": False, "error": problem}
    for code in [code for code, entry in _PENDING.items() if now() - entry["created"] > CODE_TTL_SECONDS]:
        _PENDING.pop(code, None)
    paths = prim_paths(arguments)
    if confirm is None:
        return await _preview(call, tool, arguments, paths, base, now)
    return await _apply(call, tool, arguments, paths, base, confirm, checkpoint, now)


async def _preview(call: Call, tool: str, arguments: dict[str, Any], paths: list[str], base: dict[str, Any],
                   now: Callable[[], float]) -> dict[str, Any]:
    state = await _scene_state(call)
    before = await snapshot(call, paths)
    preview, error = await _safe_call(call, tool, {**arguments, "dry_run": True})
    error = error or _failed(preview)
    if error:
        return {**base, "ok": False, "step": "preview", "error": f"The preview failed, so nothing will be applied: "
                f"{error}", "preview": preview}
    take_checkpoint, checkpoint_why = _checkpoint_plan(state)
    code = secrets.token_hex(3)
    _PENDING[code] = {"fingerprint": _fingerprint(tool, arguments), "stage": state.get("stage"),
                      "before": before, "created": now()}
    return {
        **base, "ok": True, "step": "preview", "changed": False,
        "preview": _without_dry_run_hints(preview),
        "prims_now": before,
        "safety_net": {"checkpoint_first": take_checkpoint, "why": checkpoint_why,
                       "undo": "scene_undo_last undoes it afterwards" if _undoable(preview) else
                       ("checked right after applying: most router edits land on the undo history "
                        "(scene_undo_last); the answer says which")},
        "confirm": code,
        "expires_in_minutes": CODE_TTL_SECONDS // 60,
        "next": (f"Show the user this preview. Only if they say yes, call scene_safe_edit(tool='{tool}', "
                 f"arguments=<the same arguments>, confirm='{code}'). Nothing has changed yet."),
    }


_NEXT_KEYS = ("next_step", "next")


def _without_dry_run_hints(value: Any) -> Any:
    """A copy of the wrapped tool's preview without its own "Call again with dry_run=False" next steps.

    Seen live in Composer: the inner next_step contradicted the confirm-code flow; here the only way on is
    scene_safe_edit(..., confirm=code), so those hints are dropped (the tool's own result is not changed).
    """
    if isinstance(value, dict):
        return {key: _without_dry_run_hints(item) for key, item in value.items()
                if not (key in _NEXT_KEYS and isinstance(item, str) and "dry_run=false" in item.lower())}
    if isinstance(value, list):
        return [_without_dry_run_hints(item) for item in value]
    return value


async def _apply(call: Call, tool: str, arguments: dict[str, Any], paths: list[str], base: dict[str, Any],
                 confirm: str, checkpoint: bool, now: Callable[[], float]) -> dict[str, Any]:
    pending = _PENDING.get(str(confirm))
    if pending is None:
        return {**base, "ok": False, "step": "apply", "error": ("That confirm code is unknown or expired (codes "
                "last 15 minutes and work once). Call scene_safe_edit without confirm to preview again.")}
    if pending["fingerprint"] != _fingerprint(tool, arguments):
        return {**base, "ok": False, "step": "apply", "error": ("The tool or arguments differ from the preview "
                "this code belongs to; nothing was applied. Preview the new edit first.")}
    _PENDING.pop(str(confirm), None)
    state = await _scene_state(call)
    if pending.get("stage") and state.get("stage") and state["stage"] != pending["stage"]:
        return {**base, "ok": False, "step": "apply", "error": (f"A different scene is open now ({state['stage']}) "
                "than at the preview; nothing was applied. Preview again.")}
    before = await snapshot(call, paths)
    if before != pending["before"]:
        return {**base, "ok": False, "step": "apply", "changed_since_preview": describe_changes(pending["before"],
                before), "error": ("These prims changed after the preview, so the preview no longer shows what "
                                   "would happen; nothing was applied. Preview again.")}
    safety: dict[str, Any] = {}
    take_checkpoint, why = _checkpoint_plan(state)
    if checkpoint and take_checkpoint:
        made, error = await _safe_call(call, "scene_checkpoint", {"note": f"before {tool} (scene_safe_edit)",
                                                                   "dry_run": False})
        error = error or _failed(made)
        if error:
            return {**base, "ok": False, "step": "checkpoint", "error": (f"The safety checkpoint failed, so the "
                    f"edit was not applied: {error}")}
        safety["checkpoint"] = made.get("id") if isinstance(made, dict) else None
    else:
        safety["checkpoint"] = None
        safety["checkpoint_skipped"] = "checkpoint=False was passed." if not checkpoint else why
    undo_before = await _undo_count(call)
    result, error = await _safe_call(call, tool, {**arguments, "dry_run": False})
    error = error or _failed(result)
    after = await snapshot(call, paths)
    changes = describe_changes(before, after)
    if error:
        return {**base, "ok": False, "step": "apply", "error": f"The edit failed: {error}", "result": result,
                "changes": changes, "left_unchanged": not changes, **_restore_help(safety)}
    undo_after = await _undo_count(call)
    undo = (undo_before is not None and undo_after is not None and undo_after > undo_before) or _undoable(result)
    safety["undo"] = ("scene_undo_last(dry_run=True), then dry_run=False, undoes exactly this edit" if undo else
                      "not on the router's undo history; use the checkpoint or Ctrl+Z in the app")
    return {
        **base, "ok": True, "step": "applied", "changed": True,
        "changes": changes or ["No change shows in type, transform, material or children; the edit touched other "
                               "attributes (see result), or nothing at all."],
        "prims_after": after,
        "result": result,
        "safety_net": safety,
        "saved": False,
        "next": ("Tell the user what changed (changes). If it looks wrong, undo it (safety_net). The scene is not "
                 "saved: they save it when they are happy (Ctrl+S, or scene_save after asking)."),
        **_restore_help(safety),
    }


def _restore_help(safety: dict[str, Any]) -> dict[str, Any]:
    checkpoint = safety.get("checkpoint")
    if not checkpoint:
        return {}
    return {"restore": f"scene_restore_checkpoint(checkpoint='{checkpoint}', dry_run=True) shows how to put the saved "
                       "file back; close or reopen the scene in the app afterwards."}
