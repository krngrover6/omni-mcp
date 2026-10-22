"""Plain-English phrases for round 8: project starter, layer manager, scene checkpoints, Kit logs, asset health,
the journal-to-script helper and the project report.

Pure string parsing for router.py, checked after nl_tier6: nothing here reads the scene, a file or a log.
parse(text) takes the request as typed (case kept: prim paths and file names are case-sensitive) and returns a small
spec {"kind": ..., ...} or None.

Kinds and what the router does with them (write-ish kinds are ALWAYS previews from a sentence; the payload names
the tool call that applies them):

* project_templates / project_list (project_templates / project_list): read-only.
* project_create (project_create): PREVIEW only (dry_run=True); the payload names project_create(..., dry_run=False).
* layer_stack / where_is (scene_layers / scene_where_is): read-only.
* add_sublayer / mute_layer / unmute_layer (scene_add_sublayer / scene_mute_layer / scene_unmute_layer): PREVIEW only.
* checkpoint (scene_checkpoint): PREVIEW only; checkpoints (scene_checkpoints) and diff (scene_diff): read-only.
* restore (scene_restore_checkpoint): ALWAYS a preview, whatever dry_run the sentence is sent with.
* kit_logs / explain_errors (kit_logs / kit_explain_recent_errors): read-only.
* asset_health (usd_asset_health): read-only.
* journal_script (scene_journal_to_script): read-only (the script is only shown, never run or saved).
* project_report (project_report): PREVIEW only (write=False); the payload names project_report(..., write=True).

Every pattern is anchored, has no nested unbounded repeats and only runs on text of at most MAX_PHRASE_CHARS
characters, so parse() stays linear on long input (it runs on every router request).
"""

from __future__ import annotations

import re
from typing import Any

from .nl_tier6 import MAX_PHRASE_CHARS, _strip

_FLAGS = re.IGNORECASE

# Kinds the router answers as scene requests (the rest are learning / workspace requests).
SCENE_KINDS = frozenset({
    "layer_stack", "where_is", "add_sublayer", "mute_layer", "unmute_layer", "checkpoint", "checkpoints", "diff",
    "restore", "asset_health", "journal_script",
})
# Kinds that would write something: a sentence only ever previews them.
PREVIEW_KINDS = frozenset({"project_create", "add_sublayer", "mute_layer", "unmute_layer", "checkpoint", "restore",
                           "project_report"})

_USD_FILE = r"[^\s\"'<>|?*]{1,300}\.usd[acz]?"
_PRIM_OR_PROPERTY = re.compile(r"^(?P<prim>/[A-Za-z_][\w]*(?:/[A-Za-z_][\w]*){0,60})(?:\.(?P<attr>[A-Za-z_][\w:]{0,200}))?$")

# --- projects ------------------------------------------------------------------------------------------------------
_TEMPLATE_WORDS = (
    ("digital twin", "digital_twin"), ("digital-twin", "digital_twin"), ("digital_twin", "digital_twin"),
    ("factory", "digital_twin"), ("twin", "digital_twin"),
    ("warehouse", "warehouse"),
    ("product configurator", "configurator"), ("configurator", "configurator"),
    ("robot cell", "robot_cell"), ("robot_cell", "robot_cell"), ("robot", "robot_cell"), ("isaac sim", "robot_cell"),
    ("synthetic data", "synthetic_data"), ("synthetic_data", "synthetic_data"), ("sdg", "synthetic_data"),
    ("replicator", "synthetic_data"),
    ("blank", "blank"), ("empty", "blank"), ("basic", "blank"), ("simple", "blank"), ("usd", "blank"),
    ("openusd", "blank"),
)
_PROJECT_CREATE = re.compile(
    r"^(?:start|create|make|begin|set\s*up|setup|scaffold)\s+(?:me\s+)?(?:a\s+|an\s+)?(?:new\s+)?"
    r"(?P<kind>[A-Za-z][\w\s-]{0,40}?\s+)?project\s+(?:called|named)\s+(?P<name>[A-Za-z0-9][A-Za-z0-9_-]{0,63})"
    r"(?:\s+(?:with|using|from)\s+(?:the\s+)?(?P<kind2>[A-Za-z][\w\s-]{0,40}?)\s+template)?$",
    _FLAGS,
)
_PROJECT_TEMPLATES = re.compile(
    r"^(?:what|which)\s+(?:project\s+)?templates\s+(?:are\s+there|do\s+you\s+have|can\s+i\s+(?:use|pick|choose)|"
    r"exist|are\s+available)$|^(?:list|show)\s+(?:me\s+)?(?:the\s+|all\s+)?(?:project\s+)?templates$|"
    r"^project\s+templates$",
    _FLAGS,
)
_PROJECT_LIST = re.compile(
    r"^(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?my\s+projects$|^what\s+projects\s+do\s+i\s+have$|^my\s+projects$|"
    r"^(?:list|show)\s+(?:me\s+)?(?:the\s+|all\s+)?(?:workspace\s+)?projects$",
    _FLAGS,
)

# --- layers --------------------------------------------------------------------------------------------------------
_LAYER_STACK = re.compile(
    r"^(?:show|list|display|print)\s+(?:me\s+)?(?:the\s+|my\s+)?(?:scene'?s?\s+|stage'?s?\s+)?layer\s*stack$|"
    r"^what(?:'s|\s+is)\s+(?:in\s+)?(?:the\s+|my\s+)?layer\s*stack$|"
    r"^(?:show|list)\s+(?:me\s+)?(?:the\s+|my\s+|all\s+)?(?:scene\s+|stage\s+)?(?:layers|sublayers)$|"
    r"^(?:what|which)\s+layers\s+(?:are\s+there|does\s+(?:my|the|this)\s+(?:scene|stage)\s+have|"
    r"are\s+in\s+(?:my|the|this)\s+(?:scene|stage))$|^layer\s*stack$",
    _FLAGS,
)
_WHERE_IS = (
    re.compile(r"^(?:which|what)\s+layers?\s+(?:sets?|defines?|authors?|holds?|has|have|controls?|wins?\s+(?:for|on))\s+"
               r"(?P<target>/\S{1,400})$", _FLAGS),
    re.compile(r"^where\s+(?:is|does)\s+(?P<target>/\S{1,400})\s+(?:set|defined|authored|come\s+from)$", _FLAGS),
    re.compile(r"^(?:which|what)\s+layer\s+(?:is|does)\s+(?P<target>/\S{1,400})\s+(?:set|defined|authored|come\s+from)"
               r"(?:\s+in)?$", _FLAGS),
)
_ADD_SUBLAYER = (
    re.compile(rf"^(?:add|insert|attach)\s+(?:a\s+|the\s+)?(?P<new>new\s+)?(?:sub\s*layer|sublayer)\s+(?:called\s+|named\s+)?"
               rf"(?P<file>{_USD_FILE})(?:\s+(?:as|at)\s+(?:the\s+)?(?P<pos>strongest|weakest|top|bottom)(?:\s+layer)?)?$",
               _FLAGS),
    re.compile(rf"^(?:add|insert|attach)\s+(?P<file>{_USD_FILE})\s+as\s+a\s+(?P<new>new\s+)?(?:sub\s*layer|sublayer)"
               r"(?:\s+(?:at\s+the\s+)?(?P<pos>strongest|weakest|top|bottom))?$", _FLAGS),
)
_MUTE = re.compile(
    rf"^(?P<verb>mute|unmute|un-mute)\s+(?:the\s+)?(?:(?:sub)?layer\s+(?P<a>\S{{1,300}})|(?P<b>{_USD_FILE})(?:\s+(?:sub)?layer)?|"
    r"(?P<c>[\w.-]{1,120})\s+(?:sub)?layer)$",
    _FLAGS,
)

# --- checkpoints ---------------------------------------------------------------------------------------------------
_CHECKPOINT = re.compile(
    r"^(?:save|take|make|create|add)\s+(?:me\s+)?(?:a\s+|another\s+|one\s+)?(?:new\s+)?(?:scene\s+)?checkpoint"
    r"(?:\s+(?:of|for)\s+(?:my|the|this)\s+scene)?(?:\s+(?:called|named|with\s+(?:the\s+)?note|note|for)\s+"
    r"(?P<note>[^\n]{1,200}))?$|^checkpoint\s+(?:my|the|this)\s+scene$",
    _FLAGS,
)
_CHECKPOINTS = re.compile(
    r"^(?:list|show)\s+(?:me\s+)?(?:my\s+|the\s+|all\s+)?(?:scene\s+)?checkpoints$|^what\s+checkpoints\s+do\s+i\s+have$|"
    r"^my\s+checkpoints$",
    _FLAGS,
)
_DIFF = re.compile(
    r"^what(?:'s|\s+has|\s+have)?\s+changed\s+since\s+(?:the\s+|my\s+)?(?:(?P<w>last|latest|previous)\s+checkpoint|"
    r"checkpoint\s+(?P<n>\d{1,5}))$|"
    r"^(?:diff|compare)\s+(?:the\s+scene\s+)?(?:with|against|to)\s+(?:the\s+)?(?:(?P<w2>last|latest|previous)\s+"
    r"checkpoint|checkpoint\s+(?P<n2>\d{1,5}))$|"
    r"^(?:show\s+(?:me\s+)?)?(?:the\s+)?(?:diff|changes)\s+(?:since|from)\s+(?:the\s+)?(?:(?P<w3>last|latest|previous)\s+"
    r"checkpoint|checkpoint\s+(?P<n3>\d{1,5}))$",
    _FLAGS,
)
_RESTORE = re.compile(
    r"^(?:restore|roll\s*back\s+to|go\s+back\s+to|revert\s+to|return\s+to|load)\s+(?:the\s+|my\s+)?"
    r"(?:checkpoint\s+(?:number\s+|#)?(?P<n>\d{1,5}|latest|last|previous)|(?P<w>last|latest|previous)\s+checkpoint)$",
    _FLAGS,
)

# --- Kit logs ------------------------------------------------------------------------------------------------------
_APP = r"(?P<app>isaac(?:\s*sim)?|isaacsim|composer|usd\s+composer|kit)"
_KIT_LOGS = (
    re.compile(rf"^(?:show|list|display|get|read)\s+(?:me\s+)?(?:the\s+)?(?:latest\s+|recent\s+|last\s+|new\s+)?{_APP}(?:'s)?\s+"
               r"(?P<what>errors|warnings|logs?|log\s+errors|log\s+warnings)$", _FLAGS),
    re.compile(rf"^(?:are\s+there\s+)?any\s+(?:new\s+)?(?P<what>errors|warnings)\s+in\s+(?:the\s+)?{_APP}(?:\s+log)?$", _FLAGS),
    re.compile(rf"^(?:does|did)\s+{_APP}\s+(?:have|show|log)\s+(?:any\s+)?(?P<what>errors|warnings)$", _FLAGS),
    re.compile(rf"^(?:show|open|read)\s+(?:me\s+)?(?:the\s+)?{_APP}\s+(?P<what>log)(?:\s+file)?$", _FLAGS),
)
_EXPLAIN_ERRORS = re.compile(
    rf"^explain\s+(?:the\s+|my\s+)?(?:latest|recent|last|new|newest)\s+(?:{_APP}\s+)?(?:log\s+)?errors"
    r"(?:\s+in\s+(?P<app2>isaac(?:\s*sim)?|composer))?$|"
    r"^what\s+(?:do|does)\s+(?:the|these|my)\s+(?:latest\s+|recent\s+)?(?:kit\s+|log\s+)?errors\s+mean$|"
    r"^why\s+(?:is|does)\s+(?P<app3>isaac(?:\s*sim)?|composer)\s+(?:show(?:ing)?\s+)?errors$",
    _FLAGS,
)

# --- asset health --------------------------------------------------------------------------------------------------
_HEALTH = (
    re.compile(r"^(?:check|run|do|show)\s+(?:the\s+|an?\s+)?(?:asset\s+health|health\s+of\s+(?:this|my|the)\s+asset)"
               r"(?:\s+check)?(?:\s+(?:on|for|of)\s+(?P<target>\S{1,400}))?$", _FLAGS),
    re.compile(r"^is\s+(?:this|my|the)\s+(?:asset|model|file|usd|scene)\s+(?:ok|okay|healthy|fine|good|clean|valid)"
               r"(?:\s+to\s+use)?$", _FLAGS),
    re.compile(r"^is\s+(?P<target>\S{1,400}\.usd[acz]?)\s+(?:ok|okay|healthy|fine|good|clean)(?:\s+to\s+use)?$", _FLAGS),
    re.compile(r"^asset\s+health(?:\s+check)?(?:\s+(?:on|for|of)\s+(?P<target>\S{1,400}))?$", _FLAGS),
)

# --- journal -> script, project report -----------------------------------------------------------------------------
_JOURNAL_SCRIPT = re.compile(
    r"^(?:turn|convert|make|change)\s+(?:what\s+(?:i|we|you)\s+(?:did|have\s+done|changed)|my\s+(?:changes|edits)|"
    r"the\s+(?:change\s+)?journal)(?:\s+today)?\s+in(?:to)?\s+(?:a\s+)?(?:python\s+)?script$|"
    r"^(?:show|give)\s+(?:me\s+)?(?:the\s+|a\s+)?(?:python\s+)?(?:code|script)\s+(?:for|of)\s+(?:everything|all\s+(?:of\s+)?"
    r"(?:what|that))\s+(?:i|we|you)\s+(?:did|changed|have\s+done)(?:\s+today)?$|"
    r"^(?:journal|changes|my\s+changes)\s+(?:to|as)\s+(?:a\s+)?(?:python\s+)?script$|"
    r"^replay\s+(?:my|the)\s+(?:changes|edits)\s+as\s+(?:a\s+)?(?:python\s+)?script$",
    _FLAGS,
)
_REPORT = re.compile(
    r"^(?:make|create|build|generate|write|prepare)\s+(?:me\s+)?(?:a\s+|the\s+|my\s+)?(?:project\s+|portfolio\s+)"
    r"report(?:\s+for\s+(?P<dir>[A-Za-z0-9][\w-]{0,63}))?$|^(?:project|portfolio)\s+report$",
    _FLAGS,
)


# --------------------------------------------------------------------------------------------------------- helpers


def _template(words: str | None) -> str | None:
    """The template a project sentence names ('blank' when none); None for words no template matches."""
    text = " ".join((words or "").lower().replace("_", " ").split())
    text = re.sub(r"\b(?:new|my|a|an|the|usd|openusd|omniverse|study|demo)\b", " ", text)
    text = " ".join(text.split())
    if not text:
        return "blank"
    for key, name in _TEMPLATE_WORDS:
        if text == key.replace("_", " ") or text == key:
            return name
    for key, name in _TEMPLATE_WORDS:
        if re.search(rf"\b{re.escape(key.replace('_', ' '))}\b", text):
            return name
    return None


def _app(text: str | None) -> str:
    if not text:
        return "current"
    lowered = text.lower()
    if "isaac" in lowered:
        return "isaac"
    if "composer" in lowered:
        return "composer"
    return "current"


def _checkpoint_ref(number: str | None, word: str | None) -> str | int:
    if number and number.isdigit():
        return int(number)
    word = (number or word or "latest").lower()
    return "previous" if word == "previous" else "latest"


# --------------------------------------------------------------------------------------------------------- parse


def parse(text: str) -> dict[str, Any] | None:
    """{"kind": ..., ...} for a round-8 request, else None. Never reads anything."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_PHRASE_CHARS:
        return None
    core = _strip(text)
    if not core:
        return None
    core = re.sub(r"\s+", " ", core)

    if _PROJECT_TEMPLATES.match(core):
        return {"kind": "project_templates"}
    if _PROJECT_LIST.match(core):
        return {"kind": "project_list"}
    created = _PROJECT_CREATE.match(core)
    if created:
        template = _template(created.group("kind2") or created.group("kind"))
        if template is not None:
            return {"kind": "project_create", "name": created.group("name"), "template": template}

    if _LAYER_STACK.match(core):
        return {"kind": "layer_stack"}
    for pattern in _WHERE_IS:
        match = pattern.match(core)
        if match:
            target = match.group("target").rstrip(".,;:?")
            parts = _PRIM_OR_PROPERTY.match(target)
            if parts:
                return {"kind": "where_is", "prim_path": parts.group("prim"), "attribute": parts.group("attr")}
            return None
    for pattern in _ADD_SUBLAYER:
        match = pattern.match(core)
        if match:
            position = (match.group("pos") or "strongest").lower()
            position = {"top": "strongest", "bottom": "weakest"}.get(position, position)
            return {"kind": "add_sublayer", "layer_path": match.group("file"), "position": position,
                    "create_if_missing": bool(match.group("new"))}
    muted = _MUTE.match(core)
    if muted:
        layer = (muted.group("a") or muted.group("b") or muted.group("c") or "").strip(".,;:")
        if layer and layer.lower() not in ("it", "this", "that", "them"):
            kind = "mute_layer" if muted.group("verb").lower() == "mute" else "unmute_layer"
            return {"kind": kind, "layer": layer}

    checkpoint = _CHECKPOINT.match(core)
    if checkpoint:
        note = (checkpoint.groupdict().get("note") or "").strip().strip("'\"")
        return {"kind": "checkpoint", "note": note}
    if _CHECKPOINTS.match(core):
        return {"kind": "checkpoints"}
    diff = _DIFF.match(core)
    if diff:
        groups = diff.groupdict()
        number = groups.get("n") or groups.get("n2") or groups.get("n3")
        word = groups.get("w") or groups.get("w2") or groups.get("w3")
        return {"kind": "diff", "a": _checkpoint_ref(number, word), "b": "current"}
    restore = _RESTORE.match(core)
    if restore:
        return {"kind": "restore", "checkpoint": _checkpoint_ref(restore.group("n"), restore.group("w"))}

    for pattern in _KIT_LOGS:
        match = pattern.match(core)
        if match:
            what = match.group("what").lower()
            level = "error" if "error" in what else "warning"
            return {"kind": "kit_logs", "app": _app(match.group("app")), "level": level}
    explained = _EXPLAIN_ERRORS.match(core)
    if explained:
        groups = explained.groupdict()
        return {"kind": "explain_errors", "app": _app(groups.get("app") or groups.get("app2") or groups.get("app3"))}

    for pattern in _HEALTH:
        match = pattern.match(core)
        if match:
            target = (match.groupdict().get("target") or "").rstrip(".,;:?") or None
            spec: dict[str, Any] = {"kind": "asset_health", "scene_file": None, "root_path": None}
            if target and target.startswith("/") and _PRIM_OR_PROPERTY.match(target):
                spec["root_path"] = target
            elif target and re.search(r"\.usd[acz]?$", target, _FLAGS):
                spec["scene_file"] = target
            elif target and target.lower() not in ("this", "it", "my", "scene", "asset", "the"):
                return None
            return spec

    if _JOURNAL_SCRIPT.match(core):
        return {"kind": "journal_script"}
    report = _REPORT.match(core)
    if report:
        return {"kind": "project_report", "project_dir": report.groupdict().get("dir")}
    return None
