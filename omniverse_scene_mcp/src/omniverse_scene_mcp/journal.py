"""Change journal: a plain-language, append-only log of what the MCP actually changed in a scene.

Only applied edits (dry_run=False) and undo/redo events are recorded; previews never are. Each entry is one JSON
line in ``change_journal.jsonl``: local time, scene target (isaac / composer / file / fake) and stage identifier,
tool, a sanitized argument summary (long values clipped, anything key-like such as a token, password or secret
dropped), the affected prim paths, before/after values when the tool reported them, the undo id when there is one,
and ok/error. explain() turns entries into beginner sentences; summary() tells the story of one day.

Where the file lives (the same rules as the exam-prep stores in runtime.py):

* ``$OMNIVERSE_SCENE_MCP_JOURNAL`` when set: a ``.jsonl`` file path, or a folder that gets ``change_journal.jsonl``;
* otherwise memory-only under the test switch ``$OMNIVERSE_SCENE_MCP_TEST`` (see build_change_journal);
* otherwise ``<workspace>/scene-journal/change_journal.jsonl`` (workspace = ``$OMNIVERSE_ROOT`` or the folder
  that holds ``omniverse_scene_mcp/``). Nothing is created until the first write.

The file is rotated to ``<name>.1`` once it would pass max_bytes (5 MB); reads span both files. Lines that are not
valid JSON are skipped. Writers in one process share a lock per file. Journal errors never break a tool: the
journaled_tool / install_journal hook swallows them.
"""

from __future__ import annotations

import functools
import inspect
import json
import math
import os
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ENV_PATH = "OMNIVERSE_SCENE_MCP_JOURNAL"
TEST_ENV = "OMNIVERSE_SCENE_MCP_TEST"
FILE_NAME = "change_journal.jsonl"
FOLDER_NAME = "scene-journal"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LIMIT = 50
MAX_LIMIT = 5000
MAX_STRING = 300
MAX_ERROR = 400
MAX_LIST = 20
MAX_KEYS = 30
MAX_DEPTH = 4
MAX_PRIMS = 20
REMOVED = "[removed]"
_OFF_VALUES = {"", "0", "false", "no", "off"}

# Every tool that can change a scene (or undo a change). record_result still ignores their dry runs.
JOURNALED_TOOLS = frozenset(
    {
        "router_dispatch",
        "scene_create",
        "scene_delete",
        "scene_duplicate",
        "scene_reparent",
        "scene_move",
        "scene_material",
        "scene_rename",
        "scene_variant_set",
        "scene_save",
        "scene_create_light",
        "scene_create_camera",
        "scene_aim",
        "scene_set_attribute",
        "scene_create_material",
        "scene_bind_material",
        "scene_undo_last",
        "scene_redo",
        "scene_redo_last",
        "scene_add_asset",
        "scene_add_physics_scene",
        "scene_make_rigid_body",
        "scene_make_collider",
        "scene_remove_physics",
        "scene_layout_snap",
        "scene_layout_stack",
        "scene_layout_distribute",
        "scene_layout_space",
        "scene_layout_align",
        # Round 8: layer-stack changes, file checkpoints (restore rewrites scene files) and new project folders.
        "scene_add_sublayer",
        "scene_remove_sublayer",
        "scene_set_edit_target",
        "scene_mute_layer",
        "scene_unmute_layer",
        "scene_move_opinions",
        "scene_checkpoint",
        "scene_restore_checkpoint",
        "scene_prune_checkpoints",
        "project_create",
    }
)

_TOOL_ACTIONS = {
    "scene_create": "create",
    "scene_delete": "delete",
    "scene_duplicate": "duplicate",
    "scene_reparent": "reparent",
    "scene_move": "move",
    "scene_material": "bind_material",
    "scene_rename": "rename",
    "scene_variant_set": "variant",
    "scene_save": "save",
    "scene_create_light": "create_light",
    "scene_create_camera": "create_camera",
    "scene_aim": "aim",
    "scene_set_attribute": "set_attribute",
    "scene_create_material": "create_material",
    "scene_bind_material": "bind_material",
    "scene_undo_last": "undo",
    "scene_redo": "redo",
    "scene_redo_last": "redo",
    "scene_add_asset": "add_asset",
    "scene_add_physics_scene": "add_physics_scene",
    "scene_make_rigid_body": "make_rigid_body",
    "scene_make_collider": "make_collider",
    "scene_remove_physics": "remove_physics",
    "scene_layout_snap": "snap",
    "scene_layout_stack": "stack",
    "scene_layout_distribute": "distribute",
    "scene_layout_space": "space",
    "scene_layout_align": "align",
    "scene_add_sublayer": "add_sublayer",
    "scene_remove_sublayer": "remove_sublayer",
    "scene_set_edit_target": "set_edit_target",
    "scene_mute_layer": "mute_layer",
    "scene_unmute_layer": "unmute_layer",
    "scene_move_opinions": "move_opinions",
    "scene_checkpoint": "checkpoint",
    "scene_restore_checkpoint": "restore_checkpoint",
    "scene_prune_checkpoints": "prune_checkpoints",
    "project_create": "create_project",
}
_LAYOUT_ACTIONS = ("snap", "stack", "distribute", "space", "align")
# Round 8 actions that change the layer stack (stage state, not prim specs): undone with the result's restore_with.
_LAYER_STACK_ACTIONS = ("add_sublayer", "remove_sublayer", "set_edit_target", "mute_layer", "unmute_layer")
# Round 8 actions that write files (checkpoint folders, restored scene files, a new project), not the open scene.
_FILE_ACTIONS = ("checkpoint", "restore_checkpoint", "prune_checkpoints", "create_project")
# ChangeRecord.action / bridge "action" words -> the journal's action names.
_ACTION_ALIASES = {
    "transform": "move",
    "move_prim": "move",
    "material": "bind_material",
    "set_material": "bind_material",
    "rename_prim": "rename",
    "create_prim": "create",
    "delete_prim": "delete",
    "duplicate_prim": "duplicate",
    "reparent_prim": "reparent",
    "set_variant_selection": "variant",
    "undo_last": "undo",
    "redo_last": "redo",
    "save_scene": "save",
}
_TARGET_ALIASES = {
    "isaac": "isaac",
    "isaac sim": "isaac",
    "isaacsim": "isaac",
    "isaac_sim": "isaac",
    "isaac-sim": "isaac",
    "composer": "composer",
    "usd composer": "composer",
    "usd_composer": "composer",
    "usd-composer": "composer",
    "file": "file",
    "saved file": "file",
    "fake": "fake",
    "test": "fake",
    "fake_fallback": "fake",
    "memory": "fake",
    "in-memory": "fake",
}
_TARGET_WORDS = {
    "isaac": "Isaac Sim",
    "composer": "Composer",
    "file": "the saved file",
    "fake": "the practice (in-memory) scene",
    "kit": "the live Kit app",
}
_TARGET_PORTS = {"9911": "isaac", "9910": "composer"}

# A key whose name says it holds a credential: token, secret, password, auth, cookie, api key, private/access key...
_SECRET_KEY = re.compile(
    r"(token|secret|passw|pwd|credential|auth(?!or)|cookie|session[_-]?id|api[_-]?key|private[_-]?key|access[_-]?key"
    r"|signature|^key$|^key[_-]|[_-]key$|[_-]key[_-])",
    re.IGNORECASE,
)
# Secrets inside text: "Bearer abc", "token=abc", "password: abc", "sk-..." style keys.
_SECRET_TEXT = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{4,}"), r"\1 " + REMOVED),
    (
        re.compile(r"(?i)\b(access_token|api[_-]?key|apikey|token|key|password|passwd|pwd|secret|auth)(\s*[=:]\s*)[^\s&,;\"']+"),
        r"\1\2" + REMOVED,
    ),
    (re.compile(r"\b(sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}"), REMOVED),
)

_PATH_KEYS = ("path", "prim_path", "source_path", "target_path", "new_path", "material_path", "material")

_LOCKS: dict[str, _PathState] = {}
_LOCKS_GUARD = threading.Lock()


class _PathState:
    """One lock and id counter per journal file, shared by every ChangeJournal on that file in this process."""

    __slots__ = ("lock", "next_id")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.next_id: int | None = None


def _state_for(key: str) -> _PathState:
    with _LOCKS_GUARD:
        state = _LOCKS.get(key)
        if state is None:
            state = _LOCKS[key] = _PathState()
        return state


# --- where the file lives -------------------------------------------------------------------------------------


def workspace_root() -> Path:
    """``$OMNIVERSE_ROOT`` when set (as server._workspace_root), else the folder that holds omniverse_scene_mcp/."""
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def _path_from_setting(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_dir() or value.endswith(("/", "\\")) or not path.suffix:
        return path / FILE_NAME
    return path


def default_journal_path(environ: Mapping[str, str] | None = None) -> Path:
    """``$OMNIVERSE_SCENE_MCP_JOURNAL`` (a file, or a folder that gets change_journal.jsonl), else
    ``<workspace>/scene-journal/change_journal.jsonl``. Creates nothing."""
    env = os.environ if environ is None else environ
    configured = env.get(ENV_PATH, "").strip()
    if configured:
        return _path_from_setting(configured)
    return workspace_root() / FOLDER_NAME / FILE_NAME


def _ignore_in_git(folder: Path) -> None:
    """Keep the default scene-journal/ folder out of git: it gets a '.gitignore' of '*' (the folder ignores itself).

    Only the default folder name is touched, so a journal pointed elsewhere ($OMNIVERSE_SCENE_MCP_JOURNAL) never gets
    files it did not ask for. An existing .gitignore is left alone. Never raises.
    """
    if folder.name != FOLDER_NAME:
        return
    marker = folder / ".gitignore"
    try:
        if not marker.exists():
            with open(marker, "x", encoding="utf-8", newline="\n") as handle:
                handle.write("# Created by omniverse_scene_mcp.journal: the change journal is local, never committed.\n*\n")
    except OSError:  # FileExistsError from a parallel writer, or a read-only folder: the journal still works
        pass


def build_change_journal(environ: Mapping[str, str] | None = None) -> ChangeJournal:
    """The journal one server shares: ``$OMNIVERSE_SCENE_MCP_JOURNAL`` when set; memory-only under the test switch
    ``$OMNIVERSE_SCENE_MCP_TEST`` (nothing touches the disk); otherwise the workspace file."""
    env = os.environ if environ is None else environ
    if env.get(ENV_PATH, "").strip():
        return ChangeJournal(default_journal_path(env))
    if env.get(TEST_ENV, "").strip().lower() not in _OFF_VALUES:
        return ChangeJournal(memory_only=True)
    return ChangeJournal(default_journal_path(env))


# --- sanitizing -----------------------------------------------------------------------------------------------


def clip(text: Any, limit: int = MAX_STRING) -> str:
    """One line of at most limit characters, with secrets in the text removed."""
    value = " ".join(str(text).split())
    for pattern, replacement in _SECRET_TEXT:
        value = pattern.sub(replacement, value)
    return value if len(value) <= limit else value[: max(0, limit - 3)] + "..."


def is_secret_key(name: Any) -> bool:
    return isinstance(name, str) and bool(_SECRET_KEY.search(name))


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """A JSON-safe, size-capped copy: strings clipped, lists and dicts capped, key-like entries dropped."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return clip(value)
    if depth >= MAX_DEPTH:
        return clip(json.dumps(value, default=str) if isinstance(value, (dict, list, tuple)) else value, 120)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_KEYS:
                out["..."] = f"{len(value) - MAX_KEYS} more"
                break
            name = clip(key, 80)
            if is_secret_key(name):
                continue
            out[name] = sanitize(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        out_list = [sanitize(item, depth=depth + 1) for item in items[:MAX_LIST]]
        if len(items) > MAX_LIST:
            out_list.append(f"... {len(items) - MAX_LIST} more")
        return out_list
    try:  # pxr / numpy values: plain numbers or sequences when possible
        return sanitize(list(value), depth=depth + 1) if hasattr(value, "__len__") else clip(value)
    except Exception:  # noqa: BLE001
        return clip(repr(value))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _since_moment(since: Any) -> datetime | None:
    if since is None or since == "":
        return None
    if isinstance(since, datetime):
        return since if since.tzinfo else since.astimezone()
    if isinstance(since, date):
        return datetime(since.year, since.month, since.day).astimezone()
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        return datetime.fromtimestamp(float(since)).astimezone()
    parsed = _parse_time(str(since).strip())
    if parsed is None:
        raise ValueError(f"since must be an ISO date or time such as 2026-10-22 or 2026-10-22T14:00, not {clip(since, 60)!r}")
    return parsed


def _day(value: Any) -> date:
    if value is None or value == "":
        return datetime.now().astimezone().date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().lower()
    if text == "today":
        return datetime.now().astimezone().date()
    if text == "yesterday":
        return datetime.now().astimezone().date() - timedelta(days=1)
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"day must be YYYY-MM-DD, 'today' or 'yesterday', not {clip(value, 60)!r}") from exc


def normalize_target(target: Any) -> str | None:
    if target is None:
        return None
    text = " ".join(str(target).strip().lower().split())
    if not text:
        return None
    return _TARGET_ALIASES.get(text, clip(text, 40))


def target_from_backend(info: Any) -> str | None:
    """isaac / composer / fake / ... from a SceneMCPService.backend_info dict (target, else endpoint port, else mode)."""
    if not isinstance(info, Mapping):
        return None
    if info.get("target"):
        return normalize_target(info["target"])
    endpoint = str(info.get("endpoint") or "")
    port = endpoint.rstrip("/").rsplit(":", 1)[-1] if ":" in endpoint else ""
    if port in _TARGET_PORTS:
        return _TARGET_PORTS[port]
    mode = str(info.get("mode") or "").lower()
    if mode in ("test", "fake", "fake_fallback"):
        return "fake"
    if mode == "live_kit":
        return "kit"
    return normalize_target(mode) if mode else None


# --- reading a tool result --------------------------------------------------------------------------------------


def _layers(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The result, then its nested payload dicts (router_dispatch -> wrapped result -> bridge payload)."""
    layers: list[Mapping[str, Any]] = []
    current: Any = result
    for _ in range(4):
        if not isinstance(current, Mapping):
            break
        layers.append(current)
        current = current.get("payload") if isinstance(current.get("payload"), Mapping) else current.get("result")
    return layers


def _first(layers: Iterable[Mapping[str, Any]], *keys: str) -> Any:
    for layer in layers:
        for key in keys:
            if key in layer and layer[key] is not None:
                return layer[key]
    return None


def _dry_run(layers: list[Mapping[str, Any]], tool: str) -> bool:
    """True unless the result says it was applied (dry_run False); unknown counts as a preview, and so does a
    read-only answer (a sentence such as 'show the layer stack' whose result also reports dry_run False)."""
    for layer in layers:
        if layer.get("preview") is True or layer.get("read_only") is True:
            return True
        if "dry_run" in layer:
            return layer["dry_run"] is not False
    if tool == "scene_save" or _first(layers, "saved_path"):
        return False  # SceneMCPService.save_scene returns only {"saved_path": ...} when it saves
    return True


def _changes(layers: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    for layer in layers:
        changes = layer.get("changes")
        if isinstance(changes, list):
            return [change for change in changes if isinstance(change, Mapping)]
    return []


def _is_path(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("/") and len(value) < 1000


def _arg(args: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if args.get(name) is not None:
            return args[name]
    return None


def _value_of(value: Any) -> Any:
    """The plain value inside an edits.py before/after record ({"value": ...})."""
    if isinstance(value, Mapping) and "value" in value:
        return value["value"]
    return value


def _derive(tool: str, args: Mapping[str, Any], layers: list[Mapping[str, Any]]) -> dict[str, Any]:
    changes = _changes(layers)
    change = changes[0] if changes else {}
    change_before = change.get("before") if isinstance(change.get("before"), Mapping) else {}
    change_after = change.get("after") if isinstance(change.get("after"), Mapping) else {}
    raw_action = _TOOL_ACTIONS.get(tool) or _first(layers[1:] or layers, "action") or change.get("action") or tool
    if _first(layers, "undone", "would_undo", "redone") is not None and tool not in _TOOL_ACTIONS:
        raw_action = "redo" if _first(layers, "redone") is not None else "undo"
    action = _ACTION_ALIASES.get(str(raw_action), str(raw_action))
    if action in ("undo_last",):
        action = "undo"

    details: dict[str, Any] = {}
    before: Any = None
    after: Any = None
    prims: list[str] = []
    payload_prim = _first(layers, "prim")
    payload_prim_path = payload_prim.get("path") if isinstance(payload_prim, Mapping) else None
    main = _arg(args, "path", "prim_path", "source_path") or change.get("path") or _first(layers, "path", "prim_path")

    if action == "move":
        moved = {key: _arg(args, key) for key in ("translation", "rotation", "scale")}
        moved = {key: value for key, value in moved.items() if value is not None}
        if not moved:
            moved = {key: value for key, value in change_after.items() if value is not None}
        after = moved or None
        old = {key: change_before[key] for key in moved if key in change_before}
        before = old or None
    elif action == "rename":
        new_name = _arg(args, "new_name") or change_after.get("new_name")
        new_path = change_after.get("path") or _first(layers, "new_path") or payload_prim_path
        if not new_path and _is_path(main) and new_name:
            new_path = main.rsplit("/", 1)[0] + "/" + str(new_name) if main.count("/") > 1 else "/" + str(new_name)
        before, after = {"path": main}, {"path": new_path, "name": new_name}
        prims.append(new_path)
    elif action == "reparent":
        details["new_parent"] = _arg(args, "new_parent") or change_after.get("parent")
        before = {"parent": change_before.get("parent")} if change_before.get("parent") else None
        after = {"parent": details["new_parent"]}
        if payload_prim_path:
            prims.append(payload_prim_path)
    elif action == "duplicate":
        details["source"] = _arg(args, "source_path") or change_before.get("path")
        details["copy"] = _arg(args, "target_path") or change.get("path") or payload_prim_path
        main = details["source"]
        prims.append(details["copy"])
    elif action == "create":
        details["type_name"] = _arg(args, "type_name") or _first(layers, "type_name")
    elif action == "bind_material":
        details["material"] = _arg(args, "material_path") or change_after.get("material") or _first(layers, "material")
        old = change_before.get("material")
        previous = _first(layers, "before")
        if old is None and isinstance(previous, Mapping):
            old = previous.get("bound_material")
        before, after = old, details["material"]
    elif action == "set_attribute":
        details["attribute"] = _arg(args, "name") or _first(layers, "attribute")
        time_value = _arg(args, "time")
        if time_value is not None:
            details["time"] = time_value
        reported_before = _first(layers[1:] or layers, "before")
        before = _value_of(reported_before)
        reported_after = _first(layers[1:] or layers, "after")
        after = _value_of(reported_after) if reported_after is not None else _arg(args, "value")
    elif action == "create_light":
        plan = _first(layers, "plan", "created", "would_create")
        details["light_type"] = _arg(args, "light_type") or (plan.get("light_type") if isinstance(plan, Mapping) else None)
        for key in ("intensity", "color"):
            if _arg(args, key) is not None:
                details[key] = args[key]
    elif action == "aim":
        details["aimed_at"] = _arg(args, "target")
    elif action == "create_material":
        for key in ("diffuse_color", "roughness", "metallic", "opacity"):
            if _arg(args, key) is not None:
                details[key] = args[key]
    elif action == "variant":
        details["variant_set"] = _arg(args, "variant_set") or change_after.get("variant_set")
        details["selection"] = _arg(args, "selection") or change_after.get("selection")
        selections = change_before.get("selections")
        before = selections.get(details["variant_set"]) if isinstance(selections, Mapping) else None
        after = details["selection"]
    elif action == "add_asset":
        details["asset"] = _arg(args, "asset_path") or _first(layers, "asset_path", "asset")
    elif action == "save":
        details["saved_to"] = _first(layers, "saved_path") or _arg(args, "path")
        main = None
    elif action in ("undo", "redo"):
        event = _first(layers, "redone" if action == "redo" else "undone")
        if not isinstance(event, Mapping):
            event = _first(layers, "edit", "would_undo")
        if isinstance(event, Mapping):
            details["label"] = event.get("label")
            paths = event.get("paths")
            if isinstance(paths, list):
                prims.extend(p for p in paths if _is_path(p))
        main = None
    elif action in ("add_physics_scene", "make_rigid_body", "make_collider", "remove_physics"):
        for key in ("approximation", "include_descendants", "mass"):
            if _arg(args, key) is not None:
                details[key] = args[key]
    elif action in _LAYER_STACK_ACTIONS:
        details["layer"] = _arg(args, "layer", "layer_path") or _first(layers, "display_name", "layer")
        if _arg(args, "position") is not None:
            details["position"] = args["position"]
        main = None
    elif action == "move_opinions":
        details["src_layer"] = _arg(args, "src_layer")
        details["dst_layer"] = _arg(args, "dst_layer")
    elif action in _FILE_ACTIONS:
        details["scene"] = _first(layers, "scene_relative") or _arg(args, "scene_file")
        if action == "checkpoint":
            details["checkpoint"] = _first(layers, "id")
            details["note"] = _arg(args, "note") or None
        elif action == "restore_checkpoint":
            details["checkpoint"] = _arg(args, "checkpoint") or _first(layers, "checkpoint")
        elif action == "prune_checkpoints":
            details["keep"] = _arg(args, "keep")
        else:
            details["project"] = _arg(args, "name")
            details["template"] = _arg(args, "template")
        main = None
    elif action in _LAYOUT_ACTIONS:
        listed = _arg(args, "paths")
        listed = [listed] if isinstance(listed, str) else listed if isinstance(listed, (list, tuple)) else []
        prims.extend(path for path in listed if _is_path(path))
        details["count"] = len(listed)
        for key in ("surface", "surface_path", "rows", "axis", "mode", "spacing", "gap", "margin", "anchor"):
            if _arg(args, key) is not None:
                details[key] = args[key]
        main = None

    if _is_path(main):
        prims.insert(0, main)
    if action not in ("undo", "redo", "save"):
        for item in changes:
            if _is_path(item.get("path")):
                prims.append(item["path"])
        if not prims:
            for key in _PATH_KEYS[:5]:
                found = _first(layers, key)
                if _is_path(found):
                    prims.append(found)
    unique = list(dict.fromkeys(p for p in prims if _is_path(p)))
    summary = change.get("summary") if isinstance(change.get("summary"), str) else None
    return {
        "action": action,
        "prims": unique[:MAX_PRIMS],
        "before": before,
        "after": after,
        "details": {key: value for key, value in details.items() if value is not None},
        "label": summary or details.get("label"),
    }


def _undo_info(layers: list[Mapping[str, Any]]) -> tuple[Any, bool | None]:
    undo_id = _first(layers, "undo_id", "edit_id", "entry_id")
    if undo_id is None:
        for key in ("undo", "undone", "redone"):
            nested = _first(layers, key)
            if isinstance(nested, Mapping) and nested.get("id") is not None:
                undo_id = nested["id"]
                break
    if _first(layers, "undoable") is True or _first(layers, "fake_scene") is True:
        return undo_id, True
    if _first(layers, "undoable") is False:
        return undo_id, False
    message = _first(layers, "message")
    if isinstance(message, str) and "did not record it for undo" in message:
        return undo_id, False
    return undo_id, None


def _stage_of(layers: list[Mapping[str, Any]]) -> Any:
    stage = _first(layers, "edit_target_layer", "stage_identifier", "stage", "layer")
    if stage is None:
        target = _first(layers, "edit_target")
        if isinstance(target, Mapping):
            stage = target.get("identifier")
    if stage is None:
        for key in ("undone", "redone", "edit"):
            nested = _first(layers, key)
            if isinstance(nested, Mapping) and nested.get("layer"):
                return nested["layer"]
    return stage if isinstance(stage, str) else None


# --- plain-language sentences -----------------------------------------------------------------------------------


def _number(value: float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        return str(value)
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def fmt(value: Any) -> str:
    """A value the way a beginner reads it: (1, 2, 3), 0.5, /World/Cube, nothing."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return clip(value, 120)
    if isinstance(value, Mapping):
        if "value" in value:
            return fmt(value["value"])
        return clip(json.dumps(value, default=str, separators=(", ", ": ")), 120)
    if isinstance(value, (list, tuple)):
        if len(value) > 16:
            return "(" + ", ".join(fmt(item) for item in value[:16]) + ", ...)"
        return "(" + ", ".join(fmt(item) for item in value) + ")"
    return clip(value, 120)


def _clock(entry: Mapping[str, Any]) -> str:
    parsed = _parse_time(entry.get("time"))
    return parsed.astimezone().strftime("%H:%M") if parsed else "--:--"


def _where(entry: Mapping[str, Any]) -> str:
    target = entry.get("target")
    if not target:
        return ""
    return " in " + _TARGET_WORDS.get(str(target), str(target))


def _move_phrase(prim: str, before: Any, after: Any) -> str:
    before = before if isinstance(before, Mapping) else {}
    after = after if isinstance(after, Mapping) else {}
    verbs = (("translation", "moved"), ("rotation", "rotated"), ("scale", "scaled"))
    parts: list[str] = []
    for key, verb in verbs:
        if key not in after:
            continue
        subject = prim if not parts else "it"
        if key in before and before[key] is not None:
            parts.append(f"{verb} {subject} from {fmt(before[key])} to {fmt(after[key])}")
        else:
            parts.append(f"{verb} {subject} to {fmt(after[key])}")
    if not parts:
        return f"changed the transform of {prim}"
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _layout_phrase(action: str, prims: list[Any], details: Mapping[str, Any]) -> str:
    """'stacked 10 prims (/World/Box_01, /World/Box_02, /World/Box_03, ...) in rows of 4, 3, 2, 1'."""
    count = details.get("count") if isinstance(details.get("count"), int) else len(prims)
    shown = ", ".join(str(p) for p in prims[:3]) + (", ..." if len(prims) > 3 else "")
    group = f"{count} prim{'s' if count != 1 else ''}" + (f" ({shown})" if shown else "")
    if action == "snap":
        surface = details.get("surface")
        return f"snapped {group} onto {fmt(surface) if surface is not None else 'the floor'}"
    if action == "stack":
        rows = details.get("rows")
        shape = ", ".join(fmt(row) for row in rows) if isinstance(rows, (list, tuple)) else "4, 3, 2, 1"
        return f"stacked {group} in rows of {shape}"
    if action == "distribute":
        return f"spread {group} evenly along {fmt(details.get('surface_path'))}"
    if action == "space":
        return f"spaced {group} evenly along {str(details.get('axis') or 'x').upper()}"
    return f"lined up {group} along {str(details.get('axis') or 'x').upper()} ({details.get('mode') or 'min'})"


def _round8_phrase(action: str, prim: str, details: Mapping[str, Any]) -> str:
    """Layer-stack, checkpoint and project sentences: 'muted the layer lighting.usda'."""
    layer = fmt(details.get("layer")) if details.get("layer") else "a layer"
    scene = fmt(details.get("scene")) if details.get("scene") else "the scene"
    if action == "add_sublayer":
        return f"added the sublayer {layer} to the root layer"
    if action == "remove_sublayer":
        return f"took the sublayer {layer} out of the layer stack (the file itself was kept)"
    if action == "set_edit_target":
        return f"made {layer} the edit target, so new edits go into it"
    if action == "mute_layer":
        return f"muted the layer {layer}, so the stage ignores its opinions"
    if action == "unmute_layer":
        return f"unmuted the layer {layer}, so its opinions count again"
    if action == "move_opinions":
        return (f"moved the opinions on {prim} from {fmt(details.get('src_layer'))} to "
                f"{fmt(details.get('dst_layer'))}")
    if action == "checkpoint":
        named = f" {fmt(details['checkpoint'])}" if details.get("checkpoint") else ""
        return f"saved the checkpoint{named} of {scene}"
    if action == "restore_checkpoint":
        named = f" {fmt(details['checkpoint'])}" if details.get("checkpoint") is not None else ""
        return f"restored the checkpoint{named} of {scene} (the files on disk were put back)"
    if action == "prune_checkpoints":
        keep = f", keeping the newest {fmt(details['keep'])}" if details.get("keep") is not None else ""
        return f"deleted old checkpoints of {scene}{keep}"
    template = f" from the {fmt(details['template'])} template" if details.get("template") else ""
    return f"created the new project {fmt(details.get('project') or 'folder')}{template}"


def _what(entry: Mapping[str, Any]) -> str:
    """The middle of the sentence: 'moved /World/Cube from (0, 0, 0) to (1, 2, 3)'."""
    action = entry.get("action") or entry.get("tool") or "change"
    prims = entry.get("prims") or []
    prim = prims[0] if prims else "the scene"
    details = entry.get("details") if isinstance(entry.get("details"), Mapping) else {}
    before, after = entry.get("before"), entry.get("after")
    if action == "move":
        return _move_phrase(prim, before, after)
    if action == "rename":
        new = after if isinstance(after, Mapping) else {}
        name, path = new.get("name"), new.get("path")
        if name and path:
            return f"renamed {prim} to {name} (it is now {path})"
        return f"renamed {prim} to {name or path or 'a new name'}"
    if action == "delete":
        return f"deleted {prim} (and everything under it)"
    if action == "create":
        kind = details.get("type_name") or "prim"
        return f"created a new {kind} at {prim}"
    if action == "duplicate":
        return f"copied {details.get('source') or prim} to {details.get('copy') or 'a new path'}"
    if action == "reparent":
        old = before.get("parent") if isinstance(before, Mapping) else None
        tail = f" (it was under {old})" if old else ""
        return f"moved {prim} under {details.get('new_parent') or 'a new parent'}{tail}"
    if action == "create_light":
        kind = details.get("light_type")
        extra = f" with intensity {fmt(details['intensity'])}" if "intensity" in details else ""
        return f"added a {kind + ' ' if kind else ''}light at {prim}{extra}"
    if action == "create_camera":
        return f"added a camera at {prim}"
    if action == "aim":
        return f"turned {prim} to face {fmt(details.get('aimed_at'))}"
    if action == "create_material":
        color = f" with color {fmt(details['diffuse_color'])}" if "diffuse_color" in details else ""
        return f"created the material {prim}{color}"
    if action == "bind_material":
        material = details.get("material") or after
        tail = f" (it had {fmt(before)} before)" if before not in (None, "", after) else ""
        return f"gave {prim} the material {fmt(material)}{tail}"
    if action == "set_attribute":
        name = details.get("attribute") or "an attribute"
        when = f" at time {fmt(details['time'])}" if "time" in details else ""
        if before is not None:
            return f"changed {name} on {prim} from {fmt(before)} to {fmt(after)}{when}"
        return f"set {name} on {prim} to {fmt(after)}{when}"
    if action == "variant":
        tail = f" (it was {fmt(before)})" if before not in (None, "") else ""
        return f"switched the {details.get('variant_set') or 'variant'} variant of {prim} to {fmt(after)}{tail}"
    if action == "add_asset":
        asset = details.get("asset")
        return f"added the asset {fmt(asset)} at {prim}" if asset else f"added an asset at {prim}"
    if action == "add_physics_scene":
        return f"added a physics scene at {prim} (gravity and simulation settings)"
    if action == "make_rigid_body":
        return f"made {prim} a rigid body (it can fall and be pushed when the simulation plays)"
    if action == "make_collider":
        return f"made {prim} a collider (other objects bump into it)"
    if action == "remove_physics":
        return f"removed physics from {prim}"
    if action in _LAYOUT_ACTIONS:
        return _layout_phrase(action, prims, details)
    if action in _LAYER_STACK_ACTIONS or action in _FILE_ACTIONS or action == "move_opinions":
        return _round8_phrase(action, prim, details)
    if action == "save":
        return f"saved the scene to {fmt(details.get('saved_to'))}"
    if action in ("undo", "redo"):
        label = details.get("label")
        verb = "undid" if action == "undo" else "redid"
        if label:
            return f"{verb} the change '{clip(label, 120)}'"
        return f"{verb} the last change" + (f" on {prim}" if prims else "")
    label = entry.get("label")
    if label:
        return f"ran {entry.get('tool')}: {clip(label, 120)}"
    return f"ran {entry.get('tool') or action} on {prim}"


def explain_entry(entry: Mapping[str, Any]) -> str:
    """'14:02 — You moved /World/Cube from (0, 0, 0) to (1, 2, 3) in Composer. Undo with scene_undo_last (id 7).'"""
    clock = _clock(entry)
    what = _what(entry)
    where = _where(entry)
    action = entry.get("action")
    if entry.get("ok") is False:
        error = entry.get("error") or "no reason was given"
        verb = {"undo": "An undo", "redo": "A redo"}.get(str(action))
        if verb:
            return f"{clock} — {verb}{where} did not go through: {clip(error, 200)} Nothing changed."
        return f"{clock} — You tried to change {', '.join((entry.get('prims') or ['the scene'])[:3])}{where} " \
               f"with {entry.get('tool')}, but it did not go through: {clip(error, 200)}"
    sentence = f"{clock} — You {what}{where}."
    if action == "undo":
        return sentence + " Save again if you want the saved file to match."
    if action == "redo":
        return sentence
    if action == "save":
        return sentence + " Saving cannot be undone, but scene_undo_last still reverts the open scene."
    if action in _LAYER_STACK_ACTIONS:
        return sentence + " Put it back with the restore_with call the tool returned."
    if action == "restore_checkpoint":
        return sentence + " Reload the scene in the app; the safety checkpoint taken first can bring the old files back."
    if action in _FILE_ACTIONS:
        return sentence + " This changed files on disk, not the open scene."
    undo_id = entry.get("undo_id")
    if entry.get("undoable") is False:
        return sentence + " This one was not recorded for undo, so change it back with another edit."
    if undo_id is not None:
        return sentence + f" Undo with scene_undo_last (id {undo_id})."
    return sentence + " Undo with scene_undo_last."


# --- the journal ------------------------------------------------------------------------------------------------


class ChangeJournal:
    """Append-only JSON Lines log of applied scene changes; see the module docstring."""

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        memory_only: bool = False,
    ) -> None:
        self.memory_only = bool(memory_only)
        self.max_bytes = max(1024, int(max_bytes))
        self.path: Path | None = None if self.memory_only else (
            _path_from_setting(str(path)) if path is not None else default_journal_path()
        )
        key = f"memory:{id(self)}" if self.path is None else os.path.normcase(str(self.path.absolute()))
        self._state = _state_for(key) if self.path is not None else _PathState()
        self._memory: list[str] = []
        self._memory_rotated: list[str] = []
        self._memory_backups: list[list[str]] = []
        self.corrupt_lines = 0

    # --- paths ---------------------------------------------------------------------------------------------

    @property
    def rotated_path(self) -> Path | None:
        return None if self.path is None else self.path.with_name(self.path.name + ".1")

    # --- writing -------------------------------------------------------------------------------------------

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Append one event (sanitized; time, id and defaults filled in) and return the stored entry."""
        if not isinstance(event, Mapping):
            raise TypeError("event must be a dict")
        clean = sanitize(dict(event))
        prims = clean.get("prims")
        if isinstance(prims, str):
            prims = [prims]
        entry: dict[str, Any] = {
            "id": None,
            "time": clean.get("time") if _parse_time(clean.get("time")) else _now_iso(),
            "kind": clean.get("kind") or ("undo" if clean.get("action") in ("undo", "redo") else "edit"),
            "target": normalize_target(clean.get("target")),
            "stage": clean.get("stage") if isinstance(clean.get("stage"), str) else None,
            "tool": clip(clean.get("tool") or "unknown", 80),
            "action": clip(clean.get("action") or clean.get("tool") or "change", 80),
            "args": clean.get("args") if isinstance(clean.get("args"), dict) else {},
            "prims": [p for p in (prims or []) if isinstance(p, str)][:MAX_PRIMS],
            "before": clean.get("before"),
            "after": clean.get("after"),
            "details": clean.get("details") if isinstance(clean.get("details"), dict) else {},
            "label": clean.get("label") if isinstance(clean.get("label"), str) else None,
            "undo_id": clean.get("undo_id"),
            "undoable": clean.get("undoable") if isinstance(clean.get("undoable"), bool) else None,
            "ok": clean.get("ok") is not False,
            "error": clip(clean["error"], MAX_ERROR) if clean.get("error") else None,
        }
        if entry["ok"]:
            entry["error"] = None
        with self._state.lock:
            entry["id"] = self._next_id()
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            self._append(line)
        return entry

    def record_result(
        self,
        tool: str,
        args: Mapping[str, Any] | None,
        result: Any,
        *,
        target: Any = None,
        stage: Any = None,
    ) -> dict[str, Any] | None:
        """Record an applied tool result; dry runs, previews and non-dict results return None."""
        if not isinstance(result, Mapping):
            return None
        tool = str(tool or "unknown")
        args = dict(args) if isinstance(args, Mapping) else {}
        layers = _layers(result)
        if _dry_run(layers, tool):
            return None
        derived = _derive(tool, args, layers)
        ok_values = [layer.get("ok") for layer in layers if "ok" in layer]
        ok = all(value is not False for value in ok_values)
        error = None
        if not ok:
            error = _first(layers, "error", "message", "refused") or "the tool reported ok=false"
        undo_id, undoable = _undo_info(layers)
        kind = "undo" if derived["action"] in ("undo", "redo") else "edit"
        safe_args = {key: value for key, value in args.items() if key not in ("show_python",)}
        return self.record(
            {
                "kind": kind,
                "target": target if target is not None else _first(layers, "scene_target"),
                "stage": stage if stage is not None else _stage_of(layers),
                "tool": tool,
                "action": derived["action"],
                "args": safe_args,
                "prims": derived["prims"],
                "before": derived["before"],
                "after": derived["after"],
                "details": derived["details"],
                "label": derived["label"],
                "undo_id": undo_id,
                "undoable": undoable,
                "ok": ok,
                "error": error if isinstance(error, str) else (clip(error) if error is not None else None),
            }
        )

    def _next_id(self) -> int:
        if self._state.next_id is None:
            highest = 0
            for line in self._raw_lines():
                try:
                    value = json.loads(line).get("id")
                except (ValueError, AttributeError):
                    continue
                if isinstance(value, int) and not isinstance(value, bool):
                    highest = max(highest, value)
            self._state.next_id = highest + 1
        value = self._state.next_id
        self._state.next_id = value + 1
        return value

    def _append(self, line: str) -> None:
        data = line + "\n"
        if self.path is None:
            if sum(len(item) + 1 for item in self._memory) + len(data) > self.max_bytes and self._memory:
                self._memory_rotated = self._memory
                self._memory = []
            self._memory.append(line)
            return
        encoded = data.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _ignore_in_git(self.path.parent)
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size and size + len(encoded) > self.max_bytes:
            os.replace(self.path, self.rotated_path)
            size = 0
        prefix = b""
        if size:
            with open(self.path, "rb") as handle:  # a torn last line (crash mid-write) must not swallow this one
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    prefix = b"\n"
        with open(self.path, "ab") as handle:
            handle.write(prefix + encoded)
            handle.flush()

    # --- reading -------------------------------------------------------------------------------------------

    def _raw_lines(self) -> list[str]:
        """Oldest first, rotated file before the current one."""
        if self.path is None:
            return list(self._memory_rotated) + list(self._memory)
        lines: list[str] = []
        for path in (self.rotated_path, self.path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    lines.extend(handle.read().splitlines())
            except FileNotFoundError:
                continue
        return lines

    def _all(self) -> list[dict[str, Any]]:
        """Every valid entry, oldest first; counts the lines that could not be read in corrupt_lines."""
        with self._state.lock:
            lines = self._raw_lines()
        entries: list[dict[str, Any]] = []
        corrupt = 0
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                corrupt += 1
                continue
            if not isinstance(entry, dict) or _parse_time(entry.get("time")) is None:
                corrupt += 1
                continue
            entries.append(entry)
        self.corrupt_lines = corrupt
        return entries

    def entries(
        self,
        since: Any = None,
        limit: int | None = DEFAULT_LIMIT,
        prim: str | None = None,
        tool: str | None = None,
        target: str | None = None,
    ) -> list[dict[str, Any]]:
        """Newest first. since: ISO date/time (local), date or datetime; prim matches that prim and its
        descendants; tool matches 'scene_move', 'move' or an action name; target: isaac / composer / file / fake."""
        moment = _since_moment(since)
        try:
            count = DEFAULT_LIMIT if limit is None else max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError):
            count = DEFAULT_LIMIT
        wanted_tool = str(tool).strip().lower() if tool else None
        wanted_target = normalize_target(target)
        wanted_prim = str(prim).strip().rstrip("/") if prim else None
        found: list[dict[str, Any]] = []
        for entry in reversed(self._all()):
            if moment is not None and _parse_time(entry.get("time")) < moment:
                continue
            if wanted_tool:
                names = {str(entry.get("tool", "")).lower(), str(entry.get("action", "")).lower()}
                if wanted_tool not in names and f"scene_{wanted_tool}" not in names:
                    continue
            if wanted_target and entry.get("target") != wanted_target:
                continue
            if wanted_prim:
                prims = [p for p in entry.get("prims") or [] if isinstance(p, str)]
                if not any(p == wanted_prim or p.startswith(wanted_prim + "/") for p in prims):
                    continue
            found.append(entry)
            if len(found) >= count:
                break
        return found

    def explain(
        self,
        entries: Iterable[Mapping[str, Any]] | None = None,
        *,
        since: Any = None,
        limit: int | None = DEFAULT_LIMIT,
        prim: str | None = None,
        tool: str | None = None,
        target: str | None = None,
    ) -> list[str]:
        """Plain sentences for the given entries, or for entries(since, limit, prim, tool, target)."""
        if entries is None:
            entries = self.entries(since=since, limit=limit, prim=prim, tool=tool, target=target)
        return [explain_entry(entry) for entry in entries if isinstance(entry, Mapping)]

    def summary(self, day: Any = None) -> dict[str, Any]:
        """Counts by tool and prim for one local day (default today) and a 3-line story of that day."""
        wanted = _day(day)
        todays = [entry for entry in self._all() if _parse_time(entry.get("time")).astimezone().date() == wanted]
        edits = [entry for entry in todays if entry.get("kind") != "undo"]
        undos = [entry for entry in todays if entry.get("kind") == "undo" and entry.get("ok") is not False]
        failed = [entry for entry in todays if entry.get("ok") is False]
        by_tool = Counter(str(entry.get("tool")) for entry in todays)
        by_prim = Counter(p for entry in todays if entry.get("ok") is not False for p in (entry.get("prims") or [])[:1])
        day_text = wanted.isoformat()
        if not todays:
            story = [
                f"No scene changes were recorded on {day_text}.",
                "Previews (dry runs) are never recorded: only edits applied with dry_run=False, saves and undos.",
                "Apply an edit, then ask for the journal again to see it here.",
            ]
        else:
            applied = len(edits) - sum(1 for entry in edits if entry.get("ok") is False)
            places = sorted({_TARGET_WORDS.get(str(e.get("target")), str(e.get("target"))) for e in todays if e.get("target")})
            where = f" in {' and '.join(places)}" if places else ""
            first = f"On {day_text} you made {applied} change{'s' if applied != 1 else ''}{where}"
            extras = []
            if undos:
                extras.append(f"undid {len(undos)}")
            if failed:
                extras.append(f"{len(failed)} attempt{'s' if len(failed) != 1 else ''} did not go through")
            first += (" (" + "; ".join(extras) + ")." if extras else ".")
            if by_prim:
                prim_name, prim_count = by_prim.most_common(1)[0]
                second = f"You worked most on {prim_name} ({prim_count} time{'s' if prim_count != 1 else ''})"
            else:
                second = "No single prim stood out"
            tool_name, tool_count = by_tool.most_common(1)[0]
            second += f"; the tool you used most was {tool_name} ({tool_count})."
            third = "Last: " + explain_entry(todays[-1])
            story = [first, second, third]
        return {
            "day": day_text,
            "total": len(todays),
            "edits": len(edits),
            "undos": len(undos),
            "failed": len(failed),
            "by_tool": dict(by_tool.most_common()),
            "by_prim": dict(by_prim.most_common(10)),
            "story": story,
        }

    def clear(self, confirm: bool = False) -> dict[str, Any]:
        """Empty the journal (confirm=True required); the old entries are kept in a timestamped backup file."""
        if confirm is not True:
            return {
                "ok": False,
                "cleared": 0,
                "backup": None,
                "message": "Nothing was cleared. Call clear(confirm=True) to empty the journal; a backup is kept.",
            }
        with self._state.lock:
            lines = self._raw_lines()
            count = sum(1 for line in lines if line.strip())
            if self.path is None:
                if lines:
                    self._memory_backups.append(lines)
                self._memory, self._memory_rotated = [], []
                return {"ok": True, "cleared": count, "backup": "memory" if lines else None,
                        "message": f"Cleared {count} entr{'ies' if count != 1 else 'y'} (memory-only journal)."}
            if not lines:
                return {"ok": True, "cleared": 0, "backup": None, "message": "The journal was already empty."}
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.cleared-{stamp}.bak")
            index = 1
            while backup.exists():
                backup = self.path.with_name(f"{self.path.name}.cleared-{stamp}-{index}.bak")
                index += 1
            with open(backup, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines) + "\n")
            for path in (self.rotated_path, self.path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        return {"ok": True, "cleared": count, "backup": str(backup),
                "message": f"Cleared {count} entr{'ies' if count != 1 else 'y'}; the old journal is in {backup.name}."}

    def status(self) -> dict[str, Any]:
        """Where the journal is, how big it is and how many lines could not be read."""
        entries = self._all()
        size = 0
        if self.path is not None:
            for path in (self.path, self.rotated_path):
                try:
                    size += path.stat().st_size
                except FileNotFoundError:
                    pass
        return {
            "path": None if self.path is None else str(self.path),
            "memory_only": self.memory_only,
            "entries": len(entries),
            "bytes": size,
            "max_bytes": self.max_bytes,
            "corrupt_lines": self.corrupt_lines,
        }


# --- the hook for server.py -------------------------------------------------------------------------------------


def _resolve(value: Any) -> Any:
    if callable(value):
        try:
            return value()
        except Exception:  # noqa: BLE001 - a broken target getter must not break the tool
            return None
    return value


def journaled_tool(
    journal: ChangeJournal,
    tool_name: str,
    fn: Callable[..., Any],
    *,
    target: Any = None,
    stage: Any = None,
) -> Callable[..., Any]:
    """fn wrapped so each call's result goes to journal.record_result (sync or async; signature kept for FastMCP).

    target and stage may be values or zero-argument callables (read after the call). Journal errors are swallowed.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        signature = None

    def arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if signature is None:
            return dict(kwargs)
        try:
            bound = signature.bind_partial(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            return dict(kwargs)

    def log(args: tuple[Any, ...], kwargs: dict[str, Any], result: Any) -> None:
        try:
            journal.record_result(tool_name, arguments(args, kwargs), result, target=_resolve(target), stage=_resolve(stage))
        except Exception:  # noqa: BLE001 - the journal is a side record; the tool's answer always wins
            pass

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            result = await fn(*args, **kwargs)
            log(args, kwargs, result)
            return result

        return async_wrapper

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = fn(*args, **kwargs)
        log(args, kwargs, result)
        return result

    return wrapper


def install_journal(
    app: Any,
    journal: ChangeJournal,
    *,
    target: Any = None,
    stage: Any = None,
    tools: Iterable[str] = JOURNALED_TOOLS,
) -> Any:
    """Make app.tool (FastMCP's decorator) register a journaled copy of every tool named in tools.

    Call it right after ``app = FastMCP(...)`` and before the first ``@app.tool``. The decorated function itself is
    returned unchanged (as FastMCP does), so direct calls in tests skip the journal. Returns app.
    """
    names = frozenset(tools)
    register = app.tool

    def tool(name: str | None = None, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        decorator = register(name, *args, **kwargs)

        def apply(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            if tool_name in names:
                decorator(journaled_tool(journal, tool_name, fn, target=target, stage=stage))
            else:
                decorator(fn)
            return fn

        return apply

    app.tool = tool
    return app
