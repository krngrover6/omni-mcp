"""Turn the change journal into one runnable Python script: "show me the code for everything I did today".

script_from_journal(entries, context=..., title=..., include_failed=False) takes journal entries (journal.py: the
dicts ChangeJournal.entries() returns, newest or oldest first) and builds ONE well-commented OpenUSD script that
replays the APPLIED edits in order. Every step is a python_teacher.python_for snippet, chosen through the same
tool -> snippet mapping as show_python (python_links), with the values the journal recorded. The imports and the
"get the stage" lines are written once at the top; each step is a commented section with the journal time and the
plain sentence journal.explain_entry gives for it.

What is left out (listed in ``skipped`` with a reason): previews never reach the journal; failed edits (unless
include_failed=True, which keeps them as commented-out code); saves (save yourself when you are happy); tools that
have no faithful snippet (reparent, aim, remove physics, router sentences...); values the journal clipped or
redacted; and undos. An undo drops the step it reverted when the journal identifies that step (same undo id, or
the same label on the same prim); otherwise the undo is skipped with a note so you can check by hand. A redo of a
dropped step brings it back at the redo's place.

Safety: journal values never become code by themselves. Each one goes through python_teacher's validation (prim
paths, names, finite numbers, fixed choices) and lands in the script only as a repr() literal in a settings line.
Journal text used in comments (sentences, labels, times, the title) is reduced to one line without control
characters. Nothing is evaluated: the finished script is only compiled as a syntax self-check.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from . import python_links
from .journal import REMOVED, explain_entry
from .python_teacher import CONTEXTS, DEFAULT_STAGE_PATH, MODULE_MEANINGS, WHERE_TO_RUN, python_for

MAX_ENTRIES = 500
MAX_COMMENT = 160
_MODULE_ORDER = ("Gf", "Sdf", "Usd", "UsdGeom", "UsdLux", "UsdPhysics", "UsdShade")

# What each tool needs from the journal so the snippet does what the tool did (python_for would otherwise fall back
# to its own example values). A tuple inside the tuple means "at least one of these".
_REQUIRED: dict[str, tuple] = {
    "scene_create": ("path",),
    "scene_delete": ("path",),
    "scene_duplicate": ("source_path", "target_path"),
    "scene_rename": ("path", "new_name"),
    "scene_move": ("path", ("translation", "rotation", "scale")),
    "scene_material": ("path", "material_path"),
    "scene_bind_material": ("prim_path", "material_path"),
    "scene_variant_set": ("path", "variant_set", "selection"),
    "scene_set_attribute": ("path", "name", "value"),
    "scene_create_light": ("path", "light_type"),
    "scene_create_camera": ("path", "translation", "aim_at"),
    "scene_create_material": ("path",),
    "scene_add_asset": ("prim_path", "asset_path"),
    "scene_add_physics_scene": ("path",),
    "scene_make_rigid_body": ("prim_path",),
    "scene_make_collider": ("prim_path",),
}
# Tool arguments the snippet does not reproduce: a note says so when the journal shows one was used.
_NOT_IN_SNIPPET: dict[str, tuple[str, ...]] = {
    "scene_create_light": ("exposure", "size", "aim_at"),
    "scene_create_camera": ("horizontal_aperture", "clipping_range", "copy_optics_from"),
    "scene_create_material": ("opacity", "emissive_color", "ior"),
    "scene_bind_material": ("purpose",),
    "scene_add_asset": ("translation", "rotation", "scale", "instanceable", "correct_units"),
    "scene_make_rigid_body": ("density", "kinematic", "approximation"),
}
_UNSUPPORTED = {
    "scene_reparent": "There is no reparent snippet yet; move the prim under its new parent by hand.",
    "scene_aim": "The aim snippet only shows the math on a new demo camera, so it would not repeat this edit.",
    "scene_remove_physics": "There is no remove-physics snippet yet.",
    "scene_save": "Saves are left out on purpose: run the script, check the result, then save yourself.",
    "router_dispatch": "Sentence commands are not replayed: the journal keeps the sentence, not the exact tool "
                       "arguments. Use the matching scene_* tool if you want it in a script.",
}
# Vector arguments python_links silently drops when they are not 3 numbers (python_for would then use its example).
_VECTORS: dict[str, tuple] = {
    "scene_move": (("translation", python_links._vec), ("rotation", python_links._vec), ("scale", python_links._vec)),
    "scene_create_light": (("color", python_links._color), ("translation", python_links._vec)),
    "scene_create_camera": (("translation", python_links._vec),),
    "scene_create_material": (("diffuse_color", python_links._color),),
}
# Number-valued attributes whose USD type is known, so an int like 5000 is written with the type USD expects.
_NUMBER_TYPES = {
    "size": "double", "radius": "double", "height": "double",
    "focalLength": "float", "horizontalAperture": "float", "verticalAperture": "float",
    "physics:mass": "float", "physics:density": "float",
}


class _Skip(Exception):
    """This entry cannot become a step; the message says why in plain words."""


# --------------------------------------------------------------------------------------------- small helpers


def _one_line(value: Any, limit: int = MAX_COMMENT) -> str:
    """Text safe inside a # comment: one line, no control or line-separator characters, clipped."""
    text = str(value)
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") else ch for ch in text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _entry_id(entry: Mapping[str, Any]) -> Any:
    value = entry.get("id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return _one_line(value, 40) if value is not None else None


def _time_text(entry: Mapping[str, Any]) -> str:
    value = entry.get("time")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return "unknown time"


def _sentence(entry: Mapping[str, Any]) -> str:
    """explain_entry without the clock and without the undo advice (the script is not the MCP)."""
    try:
        text = explain_entry(entry)
    except Exception:  # noqa: BLE001 - a strange entry still gets a step, just without a sentence
        return f"Ran {_one_line(entry.get('tool'), 60)}."
    for marker in (" Undo with scene_undo_last", " This one was not recorded for undo", " Save again if"):
        text = text.split(marker)[0]
    if " \u2014 " in text:
        text = text.split(" \u2014 ", 1)[1]
    return _one_line(text)


def _has_redaction(value: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, str):
        return REMOVED in value
    if isinstance(value, Mapping):
        return any(_has_redaction(item, depth + 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_redaction(item, depth + 1) for item in value)
    return False


def _given(args: Mapping[str, Any], key: str) -> bool:
    value = args.get(key)
    return value is not None and value != ""


def _stamp(entry: Mapping[str, Any]) -> float | None:
    try:
        return datetime.fromisoformat(str(entry.get("time"))).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _ordered(entries: list[Any]) -> list[Any]:
    """Oldest first: by journal id when every entry has one, else by time. entries() lists newest first, so a
    newest-first input is reversed before the (stable) time sort keeps same-second entries in their real order."""
    mappings = [entry for entry in entries if isinstance(entry, Mapping)]
    others = [entry for entry in entries if not isinstance(entry, Mapping)]
    if mappings and all(_is_int(entry.get("id")) for entry in mappings):
        return sorted(mappings, key=lambda entry: entry["id"]) + others
    stamps = [_stamp(entry) for entry in mappings]
    known = [stamp for stamp in stamps if stamp is not None]
    if len(known) > 1 and known[0] > known[-1]:
        mappings.reverse()
    return sorted(mappings, key=lambda entry: (_stamp(entry) is None, _stamp(entry) or 0.0)) + others


# ------------------------------------------------------------------------------------------- one entry -> step


def _attribute_args(args: dict[str, Any], snippet_args: dict[str, Any]) -> dict[str, Any]:
    """Pick the USD value type for a number when the journal did not record create_type."""
    if args.get("create_type") or "value_type" not in snippet_args:
        return snippet_args
    if args.get("time") is not None:
        raise _Skip("This value was written at a time code (animation); the set_attribute snippet writes the "
                    "default (non-animated) value only.")
    name = str(args.get("name") or "")
    value = args.get("value")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        wanted = _NUMBER_TYPES.get(name) or ("float" if name.startswith("inputs:") else None)
        if wanted:
            return {**snippet_args, "value_type": wanted}
    return snippet_args


def _build(entry: Mapping[str, Any], context: str, stage_path: str) -> tuple[dict[str, Any], list[str]]:
    """(python_for result, notes) for one edit entry, or _Skip with the reason."""
    tool = entry.get("tool")
    if not isinstance(tool, str) or not tool:
        raise _Skip("The entry does not name a tool.")
    if tool in _UNSUPPORTED:
        raise _Skip(_UNSUPPORTED[tool])
    if tool not in _REQUIRED:
        raise _Skip(f"No Python snippet replays the tool {_one_line(tool, 60)!r} yet.")
    args = entry.get("args")
    if not isinstance(args, Mapping):
        raise _Skip("The journal did not keep this edit's arguments, so it cannot be repeated exactly.")
    args = dict(args)
    if _has_redaction(args):
        raise _Skip("A value was removed from the journal (it looked like a secret), so the edit cannot be repeated.")
    for need in _REQUIRED[tool]:
        names = need if isinstance(need, tuple) else (need,)
        if not any(_given(args, name) for name in names):
            raise _Skip(f"The journal has no {' or '.join(names)} for this edit, so the snippet would have to guess.")
    clipped = [key for key, value in args.items() if isinstance(value, str) and len(value) >= 297 and value.endswith("...")]
    if clipped:
        raise _Skip(f"The journal shortened {', '.join(clipped)}, so the exact value is not known.")
    for key, convert in _VECTORS.get(tool, ()):
        if _given(args, key) and convert(args[key]) is None:
            raise _Skip(f"{key} is not a list of 3 numbers in the journal, so the snippet would have to guess it.")
    if tool == "scene_create_camera" and python_links._vec(args.get("aim_at")) is None:
        raise _Skip("The camera was aimed at a prim, not a point; the camera snippet needs an [x, y, z] target.")
    if tool == "scene_create_light" and python_links._light_type(args.get("light_type")) is None:
        raise _Skip(f"Unknown light type {_one_line(args.get('light_type'), 40)!r} for the light snippet.")

    action, snippet_args, note = python_links._for_tool(tool, args)
    if action is None:
        raise _Skip(python_links.NOT_AVAILABLE)
    if tool == "scene_set_attribute":
        snippet_args = _attribute_args(args, snippet_args)
    if context == "standalone":
        snippet_args = {**snippet_args, "stage_path": stage_path}
    snippet = python_for(action, snippet_args, context=context)
    if not snippet.get("ok"):
        raise _Skip(f"python_teacher refused a value: {_one_line(snippet.get('error'), 200)}")
    notes = [note] if note else []
    if tool == "scene_add_physics_scene" and args.get("gravity") is not None and \
            not isinstance(args.get("gravity"), (int, float)):
        notes.append("Gravity was given as a vector; the snippet uses 9.81 m/s^2 down the stage up axis.")
    extras = [key for key in _NOT_IN_SNIPPET.get(tool, ()) if args.get(key) not in (None, False, "")]
    if extras:
        notes.append(f"The snippet does not set {', '.join(extras)}; set {'it' if len(extras) == 1 else 'them'} "
                     "by hand if it matters.")
    return snippet, notes


def _split(snippet: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """(import lines, stage lines, step lines) of one python_for code block; the trailing stage.Save() is dropped."""
    blocks = [block.split("\n") for block in snippet["code"].rstrip("\n").split("\n\n")]
    preamble, rest = blocks[0], blocks[1:]
    if rest and rest[-1] == ["stage.Save()"]:
        rest = rest[:-1]
    imports = [line for line in preamble if line.startswith(("import ", "from "))]
    stage_lines = [line for line in preamble if not line.startswith(("import ", "from "))]
    body = [line for block in rest for line in block]
    return imports, stage_lines, body


# ------------------------------------------------------------------------------------------------- the script


def _undo_target(entry: Mapping[str, Any], candidates: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """The most recent candidate this undo/redo reverted, and how it was identified."""
    undo_id = entry.get("undo_id")
    if undo_id is not None:
        for item in reversed(candidates):
            if item["entry"].get("undo_id") == undo_id:
                return item, f"undo id {_one_line(undo_id, 40)}"
    details = entry.get("details") if isinstance(entry.get("details"), Mapping) else {}
    label = details.get("label") or entry.get("label")
    prims = set(p for p in entry.get("prims") or [] if isinstance(p, str))
    if isinstance(label, str) and label:
        for item in reversed(candidates):
            other = item["entry"]
            same_prim = not prims or prims & set(other.get("prims") or [])
            if other.get("label") == label and same_prim:
                return item, f"the label '{_one_line(label, 80)}'"
    return None, ""


def script_from_journal(
    entries: Any,
    *,
    context: str = "script_editor",
    title: str | None = None,
    include_failed: bool = False,
    stage_path: str | None = None,
) -> dict[str, Any]:
    """One runnable, commented Python script that replays the applied journal edits in order.

    entries: journal entry dicts (any order; a {"entries": [...]} tool result works too). context: "script_editor"
    (Isaac Sim / Composer Script Editor) or "standalone" (usd-core; stage_path is the .usda file, default
    my_scene.usda, created when missing; the script ends with stage.Save()).

    Returns {ok, context, title, code, steps: [{entry_id, tool, action, ok, note}], skipped: [{entry_id, reason}],
    where_to_run, safety_note, notes}. ok is False (code None) when nothing can be replayed or the input is wrong.
    """
    where = WHERE_TO_RUN.get(context, "")
    safety = ("Running this script edits the scene again: every step below is applied once more on top of what is "
              "there now. Run it on a copy of the scene first (File > Save As, or copy the .usda), never on the only "
              "copy of work you care about.")
    if context == "standalone":
        safety += " The standalone script also saves the file at stage_path at the end."
    else:
        safety += " Plain pxr edits are not on Ctrl+Z; the scene file changes only when you save."

    def fail(error: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "context": context, "title": None, "code": None, "steps": extra.get("steps", []),
                "skipped": extra.get("skipped", []), "where_to_run": where, "safety_note": safety,
                "notes": extra.get("notes", []), "error": error}

    if context not in CONTEXTS:
        return fail(f"context must be one of {list(CONTEXTS)}")
    if isinstance(entries, Mapping) and isinstance(entries.get("entries"), (list, tuple)):
        entries = entries["entries"]
    if entries is None or isinstance(entries, (str, bytes, Mapping)) or not isinstance(entries, Iterable):
        return fail("entries must be a list of journal entries (the dicts journal entries() returns)")
    entries = list(entries)
    notes: list[str] = []
    skipped: list[dict[str, Any]] = []
    if len(entries) > MAX_ENTRIES:
        notes.append(f"Only the oldest {MAX_ENTRIES} of {len(entries)} entries were used; ask for a shorter range.")
        entries = _ordered(entries)[:MAX_ENTRIES]
    if include_failed is not True and include_failed is not False:
        return fail("include_failed must be true or false")
    path_value = DEFAULT_STAGE_PATH if stage_path is None else stage_path
    if context == "standalone":
        probe = python_for("save", {"stage_path": path_value}, context="standalone")
        if not probe.get("ok"):
            return fail(f"stage_path: {_one_line(probe.get('error'), 200)}")

    steps: list[dict[str, Any]] = []      # included so far, in order
    dropped: list[dict[str, Any]] = []    # removed by an undo (a redo can bring them back)
    seen_edits: list[dict[str, Any]] = []  # every applied edit, included or not, for undo matching
    targets: set[str] = set()

    for entry in _ordered(entries):
        if not isinstance(entry, Mapping):
            skipped.append({"entry_id": None, "reason": "Not a journal entry (expected a dict)."})
            continue
        entry_id = _entry_id(entry)
        action = str(entry.get("action") or "")
        is_undo = action in ("undo", "redo") or entry.get("kind") == "undo"
        if entry.get("target"):
            targets.add(_one_line(entry.get("target"), 40))
        if is_undo:
            verb = "redo" if action == "redo" else "undo"
            if entry.get("ok") is False:
                skipped.append({"entry_id": entry_id, "reason": f"This {verb} did not go through, so nothing changed."})
                continue
            if verb == "undo":
                found, how = _undo_target(entry, [step for step in steps if not step.get("failed")])
                if found is not None:
                    steps.remove(found)
                    dropped.append(found)
                    reason = (f"Undo at {_time_text(entry)}: it reverted entry {found['entry_id']} (matched by {how}), "
                              "so that step was taken out of the script.")
                    skipped.append({"entry_id": found["entry_id"], "reason": reason})
                    skipped.append({"entry_id": entry_id, "reason": f"Undo of entry {found['entry_id']}; handled by "
                                    "leaving that step out."})
                    continue
                other, how = _undo_target(entry, seen_edits)
                if other is not None:
                    skipped.append({"entry_id": entry_id, "reason": f"Undo of entry {_entry_id(other['entry'])} "
                                    f"(matched by {how}), which is not in the script anyway."})
                    continue
                skipped.append({"entry_id": entry_id, "reason": "This undo did not say which edit it reverted, so "
                                "the script still contains that edit. Check the steps before this time and delete "
                                "the one you undid."})
                continue
            found, how = _undo_target(entry, dropped)
            if found is not None:
                dropped.remove(found)
                again = {**found, "redo_time": _time_text(entry)}
                steps.append(again)
                skipped[:] = [item for item in skipped if not (item["entry_id"] == found["entry_id"]
                                                              and item["reason"].startswith("Undo at"))]
                skipped.append({"entry_id": entry_id, "reason": f"Redo of entry {found['entry_id']}; that step is "
                                "back in the script at this point."})
                continue
            skipped.append({"entry_id": entry_id, "reason": "This redo does not match an undone step in the script, "
                            "so it was left out."})
            continue

        failed = entry.get("ok") is False
        if failed and not include_failed:
            skipped.append({"entry_id": entry_id, "reason": "This edit did not go through when you tried it "
                            "(include_failed=True keeps it as commented-out code)."})
            continue
        record = {"entry": entry, "entry_id": entry_id}
        if not failed:
            seen_edits.append(record)
        try:
            snippet, step_notes = _build(entry, context, path_value)
        except _Skip as exc:
            skipped.append({"entry_id": entry_id, "reason": str(exc)})
            continue
        record.update({"snippet": snippet, "notes": step_notes, "failed": failed})
        steps.append(record)

    if len(targets) > 1:
        notes.append(f"These entries come from several scenes ({', '.join(sorted(targets))}); run the script on the "
                     "one you mean, or ask for one target at a time.")
    if not steps:
        return fail("Nothing to replay: no applied edit in these entries has a matching Python snippet.",
                    skipped=skipped, notes=notes)

    code, step_rows = _assemble(steps, context=context, title=title, where=where, safety=safety)
    try:
        compile(code, "<journal script>", "exec")
    except SyntaxError as exc:  # pragma: no cover - guarded by the tests
        return fail(f"internal error: the script does not compile ({exc})", skipped=skipped, notes=notes)
    return {
        "ok": True,
        "context": context,
        "title": _one_line(title, 100) if title else "Replay of my scene changes",
        "code": code,
        "steps": step_rows,
        "skipped": skipped,
        "where_to_run": where,
        "safety_note": safety,
        "notes": notes,
    }


def _assemble(steps: list[dict[str, Any]], *, context: str, title: str | None, where: str,
              safety: str) -> tuple[str, list[dict[str, Any]]]:
    parsed = [(step, *_split(step["snippet"])) for step in steps]
    explanations: dict[str, str] = {}
    said: set[str] = set()
    for step, *_ in parsed:
        for row in step["snippet"].get("lines", []):
            explanations.setdefault(row["line"], row["explanation"])

    modules: set[str] = set()
    use_os = use_omni = False
    for _, imports, _, _ in parsed:
        for line in imports:
            if line == "import os":
                use_os = True
            elif line == "import omni.usd":
                use_omni = True
            elif line.startswith("from pxr import "):
                modules.update(name.strip() for name in line[len("from pxr import "):].split(","))
    stage_lines = parsed[0][2]

    heading = _one_line(title, 100) if title else "Replay of my scene changes"
    applied = sum(1 for step in steps if not step.get("failed"))
    out: list[str] = [
        f"# {heading}",
        f"# Replays {applied} edit{'s' if applied != 1 else ''} from the MCP change journal, oldest first.",
        f"# Made on {datetime.now().strftime('%Y-%m-%d %H:%M')} by omniverse_scene_mcp.journal_script from "
        "python_teacher snippets.",
        f"# Where to run: {where}.",
    ]
    out += ["# " + line for line in _wrap("SAFETY: " + safety, 100)]
    out += ["# Every value comes from the journal, was checked by python_teacher, and is written as a plain literal.", ""]

    out.append("# --- Setup: load the libraries and get the stage (done once for every step) ---")
    if use_omni:
        out += ["# Loads Omniverse's USD helper, which knows the scene open in the app.", "import omni.usd"]
    if use_os:
        out += ["# Loads Python's os module, used here for file paths and checking files exist.", "import os"]
    ordered = [name for name in _MODULE_ORDER if name in modules]
    if ordered:
        meaning = ", ".join(MODULE_MEANINGS.get(name, name) for name in ordered)
        out += ["# " + line for line in _wrap(f"Loads the OpenUSD modules the steps use: {meaning}.", 100)]
        out.append(f"from pxr import {', '.join(ordered)}")
    for line in stage_lines:
        out += _explained(line, explanations, said)
    out.append("")

    rows: list[dict[str, Any]] = []
    total = len(steps)
    for number, (step, _, _, body) in enumerate(parsed, start=1):
        entry = step["entry"]
        snippet = step["snippet"]
        when = _time_text(entry)
        out.append(f"# === Step {number} of {total}: journal entry {step['entry_id']}, {when} ===")
        out.append(f"# {_sentence(entry)}")
        if step.get("redo_time"):
            out.append(f"# (Undone and then redone at {step['redo_time']}, so it runs here.)")
        out.append(f"# Snippet: {snippet['title']} (python_for {snippet['action']!r}).")
        for note in step["notes"]:
            out += ["# Note: " + line if i == 0 else "#   " + line for i, line in enumerate(_wrap(note, 96))]
        if step.get("failed"):
            out.append("# This edit did NOT go through when you tried it, so its code is commented out below.")
            out.append("# Remove the '# ' in front of each line if you want to try it again.")
        for line in body:
            lines = _explained(line, explanations, said)
            if step.get("failed"):
                lines = [text if text.lstrip().startswith("#") else "# " + text for text in lines]
            out += lines
        out.append("")
        note = " ".join(step["notes"]) or None
        if step.get("failed"):
            note = "Commented out: this edit did not go through originally." + (f" {note}" if note else "")
        rows.append({"entry_id": step["entry_id"], "tool": entry.get("tool"), "action": snippet["action"],
                     "ok": not step.get("failed"), "note": note})

    out.append("# --- Done ---")
    if context == "standalone":
        out += ["# Writes the changes into the file on disk; without this the file stays as it was.", "stage.Save()"]
        out.append(f"print('Replayed {applied} step(s) from the change journal and saved', stage_path)")
    else:
        out.append(f"print('Replayed {applied} step(s) from the change journal. Check the scene, then save it "
                   "with File > Save.')")
    return "\n".join(out) + "\n", rows


def _explained(line: str, explanations: dict[str, str], said: set[str]) -> list[str]:
    """The line with its python_teacher explanation as a comment above it (each explanation said once per script)."""
    explanation = explanations.get(line)
    indent = line[: len(line) - len(line.lstrip())]
    if not explanation or explanation in said:
        return [line]
    said.add(explanation)
    return [indent + "# " + _one_line(explanation, 300), line]



def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = _one_line(text, 2000).split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines or [""]
