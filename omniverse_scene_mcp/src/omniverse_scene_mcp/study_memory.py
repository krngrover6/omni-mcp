"""Tested-code library and lesson log: what worked live, kept so it is reused instead of rewritten.

Two small JSON files in the workspace (exam-prep/my-notes/mcp-memory/), readable by the user and by any MCP client:
- snippets.json: code that ran live (name, tags, where and when it was seen working, a note). snippet_find searches
  them before new code is written; snippet_save statically checks the code with usd_code_verify first.
- lessons.json: things learned by testing in Composer / Isaac ("seen live"), optionally tied to scene_diagnose finding
  codes; scene_diagnose then shows the matching lesson under that finding.
Writes follow the MCP's rule: dry_run=True (the default) only shows what would be saved.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .exam.progress import workspace_root

MAX_CODE_CHARS = 20000
MAX_TEXT_CHARS = 2000
MAX_ENTRIES = 500
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,79}$")


def memory_dir() -> Path:
    configured = os.environ.get("OMNIVERSE_MCP_MEMORY_DIR", "").strip()
    return Path(configured) if configured else workspace_root() / "exam-prep" / "my-notes" / "mcp-memory"


def _load(kind: str) -> list[dict[str, Any]]:
    path = memory_dir() / f"{kind}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(kind: str, items: list[dict[str, Any]]) -> str:
    folder = memory_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{kind}.json"
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(items, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)  # never a half-written file
    return str(path)


def _tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,\s]+", tags)
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise ValueError("tags must be a list of words or a comma-separated string")
    return sorted({t.strip().lower() for t in tags if t.strip()})[:12]


def _text(value: Any, field: str, limit: int = MAX_TEXT_CHARS, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} is longer than {limit} characters")
    return value


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 1}


def _score(query: set[str], *fields: str) -> int:
    score = 0
    for weight, field in zip((5, 3, 2, 1), fields):
        score += weight * len(query & _words(field))
    return score


# ------------------------------------------------------------------------------------------------ snippets
def save_snippet(name: str, code: str, tags: Any = None, note: Any = "", verified: Any = "",
                 dry_run: Any = True) -> dict[str, Any]:
    """Keep code that WORKED LIVE in the tested-code library (replaces an entry with the same name). verified says where
    and when it was seen working, e.g. "Isaac 6.1, 2026-11-20, robot_wasd.usda". The code is first checked with
    usd_code_verify (static); problems are shown and saving still needs dry_run=False."""
    from .code_verify import verify_code

    if not isinstance(name, str) or not _NAME.match(name.strip()):
        return {"ok": False, "error": "name: 1-80 letters, digits, spaces, _ . -"}
    try:
        code = _text(code, "code", MAX_CODE_CHARS, required=True)
        entry = {"name": name.strip(), "tags": _tags(tags), "note": _text(note, "note"),
                 "verified": _text(verified, "verified", 300, required=True)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    check = verify_code(code, run=False)
    entry.update({"code": code, "saved": _dt.date.today().isoformat(),
                  "static_check": check.get("verdict"), "problems": [p["message"] for p in check.get("problems", [])][:5]})
    items = [s for s in _load("snippets") if s.get("name", "").lower() != entry["name"].lower()]
    replaced = len(items) < len(_load("snippets"))
    if len(items) >= MAX_ENTRIES:
        return {"ok": False, "error": f"the library is full ({MAX_ENTRIES} snippets); delete some by hand first"}
    plan = {"ok": True, "dry_run": dry_run is not False, "name": entry["name"], "replaces": replaced,
            "static_check": entry["static_check"], "problems": entry["problems"],
            "file": str(memory_dir() / "snippets.json")}
    if dry_run is not False:
        plan["next_step"] = "Call again with dry_run=False to save." + (
            " The static check found problems: fix them first unless they are known false alarms." if entry["problems"]
            else "")
        return plan
    plan["written"] = _save("snippets", items + [entry])
    return plan


def find_snippets(query: Any = "", tag: Any = None, limit: Any = 5) -> dict[str, Any]:
    """Search the tested-code library (code that worked live) by words and/or a tag. Look here BEFORE writing new code;
    the best matches come back with their full code, where they were verified, and notes."""
    if not isinstance(query, str):
        return {"ok": False, "error": "query must be text"}
    words = _words(query)
    wanted = tag.strip().lower() if isinstance(tag, str) and tag.strip() else None
    scored = []
    for item in _load("snippets"):
        if wanted and wanted not in item.get("tags", []):
            continue
        score = _score(words, item.get("name", ""), " ".join(item.get("tags", [])), item.get("note", ""),
                       item.get("code", "")[:3000]) if words else 1
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    count = max(1, min(20, int(limit) if isinstance(limit, int) and not isinstance(limit, bool) else 5))
    hits = [item for _, item in scored[:count]]
    return {"ok": True, "count": len(scored), "snippets": hits,
            "all_names": [s.get("name") for s in _load("snippets")][:60],
            "note": "Each snippet worked where 'verified' says; re-check paths with usd_code_verify(scene_file=...) "
                    "before using it on another scene." if hits else "No match: write the code, test it live, then "
                    "save it with snippet_save."}


# ------------------------------------------------------------------------------------------------ lessons
def add_lesson(title: str, lesson: str, seen: Any = "", tags: Any = None, codes: Any = None,
               dry_run: Any = True) -> dict[str, Any]:
    """Record something learned by testing live (title, the lesson in plain words, where/when it was seen). codes ties it
    to scene_diagnose finding codes (e.g. ["orient_not_keyable"]) so that finding shows the lesson."""
    try:
        entry = {"title": _text(title, "title", 120, required=True), "lesson": _text(lesson, "lesson", required=True),
                 "seen": _text(seen, "seen", 300), "tags": _tags(tags), "codes": _tags(codes),
                 "added": _dt.date.today().isoformat()}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    items = [x for x in _load("lessons") if x.get("title", "").lower() != entry["title"].lower()]
    replaced = len(items) < len(_load("lessons"))
    if len(items) >= MAX_ENTRIES:
        return {"ok": False, "error": f"the lesson log is full ({MAX_ENTRIES} lessons)"}
    plan = {"ok": True, "dry_run": dry_run is not False, "title": entry["title"], "replaces": replaced,
            "codes": entry["codes"], "file": str(memory_dir() / "lessons.json")}
    if dry_run is not False:
        plan["next_step"] = "Call again with dry_run=False to save."
        return plan
    plan["written"] = _save("lessons", items + [entry])
    return plan


def find_lessons(query: Any = "", code: Any = None, limit: Any = 8) -> dict[str, Any]:
    """Search the lesson log (things seen live in Composer / Isaac) by words, or by a scene_diagnose finding code."""
    if not isinstance(query, str):
        return {"ok": False, "error": "query must be text"}
    words = _words(query)
    wanted = code.strip().lower() if isinstance(code, str) and code.strip() else None
    scored = []
    for item in _load("lessons"):
        if wanted and wanted not in item.get("codes", []):
            continue
        score = _score(words, item.get("title", ""), " ".join(item.get("tags", [])), item.get("lesson", ""),
                       item.get("seen", "")) if words else 1
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: -pair[0])
    count = max(1, min(30, int(limit) if isinstance(limit, int) and not isinstance(limit, bool) else 8))
    return {"ok": True, "count": len(scored), "lessons": [item for _, item in scored[:count]]}


def lessons_for_codes(codes: set[str]) -> dict[str, list[dict[str, str]]]:
    """{finding code: [{title, lesson, seen}]} for scene_diagnose. Never raises."""
    out: dict[str, list[dict[str, str]]] = {}
    try:
        for item in _load("lessons"):
            for code in item.get("codes", []):
                if code in codes:
                    out.setdefault(code, []).append({k: item.get(k, "") for k in ("title", "lesson", "seen")})
    except Exception:  # noqa: BLE001 - lessons are extra; a broken file must not break a diagnosis
        return {}
    return out
