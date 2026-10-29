"""Project report: turn the results of the learning tools into one portfolio page (Markdown and HTML).

build_report(...) takes the result dicts other tools already return and lays them out as a report a beginner can
put in a portfolio or hand in with homework:

* project_dir: reads ``PROJECT_CONTEXT.md`` there when present (the project name from its first heading, the
  Goal section, the Next steps / Remaining work / Status section, and Decisions when none are given);
* scene_summary: any dict (or a sentence) describing the scene, shown as "Scene at a glance";
* tour: scene_tour.scene_tour(...) (overview, counts, a few items per section, questions to explore);
* health: an asset-health result; any dict with ``score`` / ``checks`` / ``summary`` keys works (read defensively);
* assignment: assignments.check_assignment(...) (score, passed, each rule's status, the next step);
* journal_summary: journal.ChangeJournal.summary(...) (the story of the day and the edit counts);
* screenshots: relative image paths (or {"path", "caption"} dicts), shown as relative <img> links;
* decisions: strings or {"date", "decision", "why"} dicts.

The HTML is one self-contained file: inline CSS that follows the reader's light/dark setting, no scripts, no
external fonts or links, and a Content-Security-Policy that blocks anything but inline styles and local images.
Every piece of text is escaped (HTML-escaped in the HTML; HTML- and Markdown-escaped in the Markdown), so a prim
name such as ``<script>`` shows up as text and never runs.

write_report(folder, report, dry_run=True, overwrite=False) writes report.md and report.html into a folder that
must already exist. It is a preview unless dry_run=False, and it refuses to replace existing files unless
overwrite=True. Nothing else in this module writes to disk; build_report only reads PROJECT_CONTEXT.md.
"""

from __future__ import annotations

import html
import math
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import quote

CONTEXT_FILE = "PROJECT_CONTEXT.md"
REPORT_FILES = ("report.md", "report.html")
MAX_CONTEXT_BYTES = 256 * 1024
MAX_TEXT = 600
MAX_ROWS = 40
MAX_TOUR_ITEMS = 6
MAX_TOUR_SECTIONS = 10
MAX_SCREENSHOTS = 30
MAX_DECISIONS = 30
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
STATUS_CLASSES = ("pass", "partial", "fail", "info", "unknown")
DEFAULT_TITLE = "My OpenUSD project"

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*)$")
_MD_SPECIAL = re.compile(r"([\\`*_\[\]|{}])")
_MD_LEADING = re.compile(r"^([#>+\-=~]|\d+[.)])")
_ANY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2066-\u2069]")


# ------------------------------------------------------------------------------------------------ text helpers


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    """One clean line of plain text (no control or bidi characters), clipped."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, float):
        text = _number(value)
    else:
        text = str(value)
    text = _CONTROL.sub(" ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _number(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def _md(value: Any, limit: int = MAX_TEXT) -> str:
    """Markdown-safe text: HTML characters as entities, Markdown syntax characters backslash-escaped."""
    text = html.escape(_text(value, limit), quote=False)
    text = _MD_SPECIAL.sub(r"\\\1", text)
    return _MD_LEADING.sub(r"\\\1", text)


def _h(value: Any, limit: int = MAX_TEXT) -> str:
    return html.escape(_text(value, limit), quote=True)


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _status(value: Any) -> str:
    """pass / partial / fail / unknown from a status word or a boolean."""
    if isinstance(value, bool):
        return "pass" if value else "fail"
    word = _text(value, 40).lower()
    if word in ("pass", "passed", "ok", "good", "yes", "true", "success", "green"):
        return "pass"
    if word in ("info", "note", "skipped", "not checked"):
        return "info"
    if word in ("partial", "warn", "warning", "partly", "yellow", "minor"):
        return "partial"
    if word in ("fail", "failed", "error", "bad", "no", "false", "red", "missing", "broken"):
        return "fail"
    return "unknown"


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


# --------------------------------------------------------------------------------------------- the document


class _Section:
    """A heading and blocks: ("p", text), ("list", [text]), ("kv", [(key, value)]), ("table", headers, rows),
    ("status", word, text), ("images", [(src, caption)]), ("h3", text)."""

    def __init__(self, key: str, heading: str) -> None:
        self.key = key
        self.heading = heading
        self.blocks: list[tuple] = []

    def add(self, *block: Any) -> None:
        self.blocks.append(block)


# ------------------------------------------------------------------------------------------- PROJECT_CONTEXT.md


def _read_context(project_dir: Path, warnings: list[str]) -> dict[str, Any] | None:
    path = project_dir / CONTEXT_FILE
    if not path.is_file():
        return None
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONTEXT_BYTES + 1)
    except OSError as exc:
        warnings.append(f"Could not read {CONTEXT_FILE}: {_text(exc, 200)}")
        return None
    if len(raw) > MAX_CONTEXT_BYTES:
        warnings.append(f"{CONTEXT_FILE} is larger than {MAX_CONTEXT_BYTES // 1024} KB; only the start was read.")
        raw = raw[:MAX_CONTEXT_BYTES]
    return _parse_context(raw.decode("utf-8", errors="replace"))


def _parse_context(text: str) -> dict[str, Any]:
    name = None
    sections: list[tuple[int, str, list[str]]] = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        match = None if in_code else _HEADING.match(line)
        if match:
            level, heading = len(match.group(1)), match.group(2)
            if level == 1 and name is None:
                name = heading
            sections.append((level, heading, []))
        elif sections and not in_code:
            sections[-1][2].append(line)

    def body(*patterns: str) -> list[str] | None:
        for pattern in patterns:
            for index, (level, heading, lines) in enumerate(sections):
                if not re.search(pattern, heading, re.IGNORECASE):
                    continue
                collected = list(lines)
                for deeper_level, deeper_heading, deeper_lines in sections[index + 1:]:
                    if deeper_level <= level:
                        break
                    collected += [deeper_heading] + deeper_lines
                return collected
        return None

    if name:
        name = re.sub(r"^\s*project\s+context\s*[:\-\u2014\u2013]\s*", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s*[\-\u2014\u2013:]\s*project\s+context\s*$", "", name, flags=re.IGNORECASE)
    return {
        "name": _text(name, 120) or None,
        "goal": body(r"\bgoal"),
        "next_steps": body(r"next\s+steps?", r"remaining\s+work", r"\bto\s*-?\s*do\b", r"\bstatus\b"),
        "decisions": body(r"decision"),
    }


def _blocks_from_lines(lines: list[str] | None, section: _Section, max_items: int = 20) -> bool:
    """Paragraphs and bullet lists from Markdown lines (formatting marks are kept as escaped text)."""
    if not lines:
        return False
    paragraph: list[str] = []
    items: list[str] = []
    added = 0

    def flush() -> None:
        nonlocal paragraph, items, added
        if paragraph:
            section.add("p", " ".join(paragraph))
            added += 1
        if items:
            section.add("list", items[:max_items] + ([f"...and {len(items) - max_items} more"]
                                                     if len(items) > max_items else []))
            added += 1
        paragraph, items = [], []

    for raw in lines:
        if not raw.strip() or raw.strip().startswith("|"):
            flush()
            continue
        bullet = _BULLET.match(raw)
        if bullet:
            if paragraph:
                section.add("p", " ".join(paragraph))
                added += 1
                paragraph = []
            if bullet.group(1).strip():
                items.append(bullet.group(1).strip())
        elif items and raw.startswith((" ", "\t")):
            items[-1] += " " + raw.strip()
        else:
            if items:
                flush()
            paragraph.append(raw.strip())
    flush()
    return added > 0


# --------------------------------------------------------------------------------------------- the sections


def _scene_section(scene_summary: Any) -> _Section | None:
    if scene_summary is None:
        return None
    section = _Section("scene", "Scene at a glance")
    if isinstance(scene_summary, str):
        section.add("p", scene_summary)
        return section
    if not isinstance(scene_summary, Mapping):
        section.add("p", f"(The scene summary was a {type(scene_summary).__name__}, not a dict, so it is not shown.)")
        return section
    sentence = _first(scene_summary, "summary", "overview", "message")
    if isinstance(sentence, str):
        section.add("p", sentence)
    rows: list[tuple[str, str]] = []
    for key, value in scene_summary.items():
        if key in ("summary", "overview", "message", "ok") or len(rows) >= MAX_ROWS:
            continue
        if _scalar(value):
            rows.append((_text(key, 80), _text(value, 200)))
        elif isinstance(value, Mapping):
            for inner_key, inner_value in list(value.items())[:12]:
                if _scalar(inner_value):
                    rows.append((f"{_text(key, 60)}: {_text(inner_key, 60)}", _text(inner_value, 200)))
        elif isinstance(value, (list, tuple)):
            shown = [_text(item, 80) for item in value[:6] if _scalar(item)]
            summary = ", ".join(shown) if shown else f"{len(value)} item(s)"
            if len(value) > 6 and shown:
                summary += f", ... ({len(value)} in all)"
            rows.append((_text(key, 80), summary))
    if rows:
        section.add("kv", rows[:MAX_ROWS])
    return section if section.blocks else None


def _tour_section(tour: Any) -> _Section | None:
    if tour is None:
        return None
    section = _Section("tour", "Scene tour")
    if not isinstance(tour, Mapping):
        section.add("p", "(The scene tour result was not a dict, so it is not shown.)")
        return section
    if tour.get("ok") is False:
        section.add("p", f"The scene tour could not run: {_text(tour.get('error') or 'no reason given', 300)}")
        return section
    if tour.get("overview"):
        section.add("p", tour["overview"])
    counts = tour.get("counts")
    if isinstance(counts, Mapping):
        rows = [(_text(key, 60).replace("_", " "), _text(value, 40)) for key, value in counts.items() if _scalar(value)]
        if rows:
            section.add("kv", rows[:MAX_ROWS])
    sections = tour.get("sections")
    if isinstance(sections, Mapping):
        sections = [{"title": key, **value} if isinstance(value, Mapping) else {"title": key, "items": value}
                    for key, value in sections.items()]
    if isinstance(sections, (list, tuple)):
        for part in list(sections)[:MAX_TOUR_SECTIONS]:
            if not isinstance(part, Mapping):
                continue
            items = part.get("items")
            if not isinstance(items, (list, tuple)) or not items:
                continue
            lines = []
            for item in items[:MAX_TOUR_ITEMS]:
                if isinstance(item, Mapping):
                    path, plain = _text(item.get("path"), 160), _text(item.get("plain"), 400)
                    lines.append(f"{path}: {plain}" if path and plain else (plain or path))
                else:
                    lines.append(_text(item, 400))
            if len(items) > MAX_TOUR_ITEMS:
                lines.append(f"...and {len(items) - MAX_TOUR_ITEMS} more")
            section.add("h3", part.get("title") or "Section")
            section.add("list", [line for line in lines if line])
    questions = tour.get("questions_to_explore")
    if isinstance(questions, (list, tuple)) and questions:
        section.add("h3", "Questions to explore next")
        section.add("list", [_text(q.get("question") if isinstance(q, Mapping) else q, 300) for q in questions[:8]])
    return section if section.blocks else None


def _health_section(health: Any) -> _Section | None:
    if health is None:
        return None
    section = _Section("health", "Asset health")
    if not isinstance(health, Mapping):
        section.add("p", "(The asset health result was not a dict, so it is not shown.)")
        return section
    if health.get("ok") is False and not health.get("checks"):
        section.add("p", f"The health check could not run: {_text(health.get('error') or 'no reason given', 300)}")
        return section
    score = health.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(float(score)):
        out_of = health.get("max_score") or health.get("out_of") or (100 if health.get("grade") else None)
        shown = _number(float(score)) + (f" / {_text(out_of, 20)}" if _scalar(out_of) and out_of is not None else "")
        if _scalar(health.get("grade")) and health.get("grade"):
            shown += f" (grade {_text(health['grade'], 10)})"
        if health.get("score_is_partial") is True:
            shown += ", partial: the time budget ran out"
        section.add("kv", [("Score", shown)])
    elif score is not None and _scalar(score):
        section.add("kv", [("Score", _text(score, 40))])
    if isinstance(health.get("healthy"), bool):
        section.add("status", "pass" if health["healthy"] else "fail",
                    "No check fails" if health["healthy"] else "At least one check fails")
    summary = health.get("summary")
    if isinstance(summary, str):
        section.add("p", summary)
    elif isinstance(summary, Mapping):
        rows = [(_text(k, 60), _text(v, 120)) for k, v in summary.items() if _scalar(v)]
        if rows:
            section.add("kv", rows[:MAX_ROWS])
    checks = health.get("checks")
    rows: list[list[str]] = []
    if isinstance(checks, Mapping):
        checks = [{"name": key, **value} if isinstance(value, Mapping) else {"name": key, "status": value}
                  for key, value in checks.items()]
    if isinstance(checks, (list, tuple)):
        for check in list(checks)[:MAX_ROWS]:
            if isinstance(check, Mapping):
                name = _first(check, "title", "name", "check", "id", "rule") or "check"
                raw_status = _first(check, "status", "result", "level", "severity", "passed", "ok")
                detail = _check_detail(check, _status(raw_status))
                rows.append([_text(name, 120), _status(raw_status), _text(detail, 400)])
            elif _scalar(check):
                rows.append([_text(check, 120), "unknown", ""])
        if len(checks) > MAX_ROWS:
            rows.append([f"...and {len(checks) - MAX_ROWS} more", "unknown", ""])
    if rows:
        section.add("table", ["Check", "Status", "Details"], rows)
    return section if section.blocks else None


def _check_detail(check: Mapping[str, Any], status: str) -> str:
    """The best one-line explanation a check offers: its own text, else what was found and the first fix."""
    for key in ("detail", "message", "plain", "summary"):
        if isinstance(check.get(key), str) and check[key].strip():
            return _text(check[key], 400)
    parts = []
    count = check.get("count")
    examples = [_text(item, 80) for item in check.get("examples") or [] if _scalar(item) and item is not None][:3]         if isinstance(check.get("examples"), (list, tuple)) else []
    if isinstance(count, int) and not isinstance(count, bool) and count:
        parts.append(f"{count} found" + (f", e.g. {', '.join(examples)}" if examples else ""))
    elif examples:
        parts.append(", ".join(examples))
    if status != "pass":
        fixes = check.get("how_to_fix") or check.get("fix")
        first_fix = fixes[0] if isinstance(fixes, (list, tuple)) and fixes else fixes
        if isinstance(first_fix, str) and first_fix:
            parts.append(f"Fix: {first_fix}")
        elif isinstance(check.get("why"), str):
            parts.append(check["why"])
    return _text(". ".join(parts), 400)


def _assignment_section(assignment: Any) -> _Section | None:
    if assignment is None:
        return None
    section = _Section("assignment", "Assignment check")
    if not isinstance(assignment, Mapping):
        section.add("p", "(The assignment result was not a dict, so it is not shown.)")
        return section
    if assignment.get("ok") is False:
        section.add("p", f"The assignment check could not run: "
                         f"{_text(assignment.get('error') or assignment.get('summary') or 'no reason given', 300)}")
        return section
    name = _first(assignment, "title", "assignment")
    if name:
        section.add("h3", name)
    passed = assignment.get("passed")
    score = assignment.get("score_pct")
    if isinstance(passed, bool):
        words = "Every check passes" if passed else "Not finished yet"
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            words += f" (score {_number(float(score))}%)"
        section.add("status", "pass" if passed else "partial", words)
    if isinstance(assignment.get("summary"), str):
        section.add("p", assignment["summary"])
    rows = []
    results = assignment.get("results")
    if isinstance(results, (list, tuple)):
        for item in list(results)[:MAX_ROWS]:
            if isinstance(item, Mapping):
                rows.append([_text(_first(item, "title", "rule") or "rule", 120), _status(item.get("status")),
                             _text(item.get("detail"), 400)])
    if rows:
        section.add("table", ["Rule", "Status", "Details"], rows)
    if assignment.get("next_step"):
        section.add("p", f"Next step: {_text(assignment['next_step'], 500)}")
    return section if section.blocks else None


def _journal_section(journal_summary: Any) -> _Section | None:
    if journal_summary is None:
        return None
    section = _Section("journal", "What I changed")
    if not isinstance(journal_summary, Mapping):
        section.add("p", "(The journal summary was not a dict, so it is not shown.)")
        return section
    story = journal_summary.get("story")
    if isinstance(story, (list, tuple)):
        section.add("list", [_text(line, 400) for line in story[:10]])
    elif isinstance(story, str):
        section.add("p", story)
    rows = []
    for key, label in (("day", "Day"), ("edits", "Edits"), ("undos", "Undos"), ("failed", "Did not go through")):
        if _scalar(journal_summary.get(key)) and journal_summary.get(key) is not None:
            rows.append((label, _text(journal_summary[key], 40)))
    by_tool = journal_summary.get("by_tool")
    if isinstance(by_tool, Mapping):
        for tool, count in list(by_tool.items())[:10]:
            rows.append((f"Tool: {_text(tool, 60)}", _text(count, 20)))
    if rows:
        section.add("kv", rows)
    return section if section.blocks else None


def _safe_image(item: Any, project_dir: Path | None, warnings: list[str]) -> tuple[str, str] | None:
    caption = ""
    if isinstance(item, Mapping):
        caption = _text(item.get("caption") or item.get("title") or "", 200)
        item = item.get("path") or item.get("src")
    if isinstance(item, os.PathLike):
        item = os.fspath(item)
    if not isinstance(item, str) or not item.strip() or _ANY_CONTROL.search(item) or _CONTROL.search(item) \
            or len(item) > 500:
        warnings.append("A screenshot entry was not a usable file path, so it was left out.")
        return None
    text = item.strip()
    windows = PureWindowsPath(text)
    if windows.is_absolute() or windows.drive or text.startswith(("/", "\\")):
        relative = None
        if project_dir is not None:
            try:
                relative = Path(text).resolve().relative_to(project_dir.resolve())
            except (ValueError, OSError):
                relative = None
        if relative is None:
            warnings.append(f"Screenshot '{_text(text, 120)}' is an absolute path outside the project folder; use a "
                            "path relative to where the report is saved.")
            return None
        warnings.append(f"Screenshot '{_text(text, 120)}' was made relative to the project folder; save the report "
                        "there so the image link works.")
        text = relative.as_posix()
    if ":" in text or "?" in text or "#" in text:
        warnings.append(f"Screenshot '{_text(text, 120)}' looks like a URL; only local relative image files are linked.")
        return None
    posix = PurePosixPath(text.replace("\\", "/"))
    if any(part == ".." for part in posix.parts):
        warnings.append(f"Screenshot '{_text(text, 120)}' points outside the report folder ('..'), so it was left out.")
        return None
    if posix.suffix.lower() not in IMAGE_EXTS:
        warnings.append(f"Screenshot '{_text(text, 120)}' is not a {'/'.join(e[1:] for e in IMAGE_EXTS)} image.")
        return None
    clean = "/".join(part for part in posix.parts if part not in (".", ""))
    return quote(clean, safe="/-_.~"), caption or posix.stem.replace("_", " ").replace("-", " ")


def _decision_items(decisions: Any) -> list[str]:
    if decisions is None or isinstance(decisions, (str, Mapping)):
        decisions = [decisions] if decisions else []
    items = []
    for item in list(decisions)[:MAX_DECISIONS]:
        if isinstance(item, Mapping):
            what = _text(_first(item, "decision", "what", "text", "title"), 300)
            why = _text(item.get("why") or item.get("reason"), 300)
            when = _text(item.get("date") or item.get("when"), 40)
            if not what:
                continue
            line = (f"{when}: " if when else "") + what + (f" (why: {why})" if why else "")
            items.append(line)
        elif _scalar(item) and item is not None:
            items.append(_text(item, 600))
    return [item for item in items if item]


# ------------------------------------------------------------------------------------------------ rendering


def _render_markdown(title: str, subtitle: str, sections: list[_Section], footer: str) -> str:
    out = [f"# {_md(title, 200)}", "", f"_{_md(subtitle, 300)}_", ""]
    for section in sections:
        out += [f"## {_md(section.heading, 120)}", ""]
        for block in section.blocks:
            kind = block[0]
            if kind == "p":
                out += [_md(block[1], 1500), ""]
            elif kind == "h3":
                out += [f"### {_md(block[1], 160)}", ""]
            elif kind == "list":
                out += [f"- {_md(item, 600)}" for item in block[1]] + [""]
            elif kind == "kv":
                out += ["| Item | Value |", "| --- | --- |"]
                out += [f"| {_md(key, 120)} | {_md(value, 300)} |" for key, value in block[1]] + [""]
            elif kind == "table":
                headers, rows = block[1], block[2]
                out.append("| " + " | ".join(_md(h, 60) for h in headers) + " |")
                out.append("| " + " | ".join("---" for _ in headers) + " |")
                out += ["| " + " | ".join(_md(cell, 400) for cell in row) + " |" for row in rows] + [""]
            elif kind == "status":
                mark = {"pass": "PASS", "partial": "IN PROGRESS", "fail": "FAIL"}.get(block[1], "INFO")
                out += [f"**{mark}**: {_md(block[2], 300)}", ""]
            elif kind == "images":
                for src, caption in block[1]:
                    out += [f"![{_md(caption, 200)}]({src})", "", f"_{_md(caption, 200)}_", ""]
    out += ["---", "", f"_{_md(footer, 300)}_", ""]
    return "\n".join(out)


_CSS = """
:root { color-scheme: light dark; --bg: #ffffff; --fg: #1f2328; --muted: #59636e; --card: #f6f8fa;
  --border: #d1d9e0; --accent: #4f7d00; --pass: #1a7f37; --partial: #9a6700; --fail: #cf222e; --unknown: #59636e; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --card: #151b23; --border: #3d444d; --accent: #8cc63f;
    --pass: #3fb950; --partial: #d29922; --fail: #f85149; --unknown: #9198a1; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); line-height: 1.55;
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
main { max-width: 880px; margin: 0 auto; padding: 32px 16px 48px; }
header { border-bottom: 3px solid var(--accent); margin-bottom: 24px; padding-bottom: 12px; }
h1 { margin: 0 0 4px; font-size: 2rem; line-height: 1.2; overflow-wrap: anywhere; }
h2 { margin: 32px 0 12px; font-size: 1.35rem; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
h3 { margin: 20px 0 8px; font-size: 1.05rem; }
p, li, td, th { overflow-wrap: anywhere; }
.subtitle, figcaption, footer { color: var(--muted); }
.table-wrap { overflow-x: auto; margin: 8px 0 16px; }
table { border-collapse: collapse; width: 100%; font-size: 0.95rem; }
th, td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: var(--card); }
.badge { display: inline-block; min-width: 4.5em; text-align: center; padding: 1px 8px; border-radius: 999px;
  font-size: 0.8rem; font-weight: 600; border: 1px solid currentColor; }
.pass { color: var(--pass); } .partial { color: var(--partial); } .fail { color: var(--fail); }
.unknown, .info { color: var(--unknown); }
.status-line { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; }
.gallery { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }
figure { margin: 0; background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 8px; }
img { display: block; max-width: 100%; height: auto; border-radius: 4px; }
figcaption { font-size: 0.9rem; margin-top: 6px; }
footer { margin-top: 40px; border-top: 1px solid var(--border); padding-top: 12px; font-size: 0.85rem; }
""".strip()

_CSP = "default-src 'none'; img-src 'self' data: file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
_STATUS_WORDS = {"pass": "pass", "partial": "partly", "fail": "fail", "info": "info", "unknown": "unknown"}


def _badge(status: str) -> str:
    status = status if status in STATUS_CLASSES else "unknown"
    return f'<span class="badge {status}">{_STATUS_WORDS[status]}</span>'


def _render_html(title: str, subtitle: str, sections: list[_Section], footer: str) -> str:
    out = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">',
        '<meta name="referrer" content="no-referrer">',
        f"<title>{_h(title, 200)}</title>",
        f"<style>\n{_CSS}\n</style>",
        "</head>",
        "<body>",
        "<main>",
        f'<header><h1>{_h(title, 200)}</h1><p class="subtitle">{_h(subtitle, 300)}</p></header>',
    ]
    for section in sections:
        out.append(f'<section id="{section.key}">')
        out.append(f"<h2>{_h(section.heading, 120)}</h2>")
        for block in section.blocks:
            kind = block[0]
            if kind == "p":
                out.append(f"<p>{_h(block[1], 1500)}</p>")
            elif kind == "h3":
                out.append(f"<h3>{_h(block[1], 160)}</h3>")
            elif kind == "list":
                out.append("<ul>" + "".join(f"<li>{_h(item, 600)}</li>" for item in block[1]) + "</ul>")
            elif kind == "kv":
                rows = "".join(f"<tr><th scope=\"row\">{_h(k, 120)}</th><td>{_h(v, 300)}</td></tr>" for k, v in block[1])
                out.append(f'<div class="table-wrap"><table><tbody>{rows}</tbody></table></div>')
            elif kind == "table":
                headers, rows = block[1], block[2]
                head = "".join(f"<th scope=\"col\">{_h(h, 60)}</th>" for h in headers)
                body = []
                for row in rows:
                    cells = [f"<td>{_badge(cell)}</td>" if index == 1 else f"<td>{_h(cell, 400)}</td>"
                             for index, cell in enumerate(row)]
                    body.append("<tr>" + "".join(cells) + "</tr>")
                out.append(f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead>'
                           f'<tbody>{"".join(body)}</tbody></table></div>')
            elif kind == "status":
                out.append(f'<p class="status-line">{_badge(block[1])} {_h(block[2], 300)}</p>')
            elif kind == "images":
                figures = "".join(
                    f'<figure><img src="{html.escape(src, quote=True)}" alt="{_h(caption, 200)}" loading="lazy">'
                    f"<figcaption>{_h(caption, 200)}</figcaption></figure>"
                    for src, caption in block[1]
                )
                out.append(f'<div class="gallery">{figures}</div>')
        out.append("</section>")
    out += [f"<footer>{_h(footer, 300)}</footer>", "</main>", "</body>", "</html>", ""]
    return "\n".join(out)


# ------------------------------------------------------------------------------------------------ public API


def build_report(
    *,
    project_dir: str | os.PathLike[str] | None = None,
    scene_summary: Any = None,
    tour: Any = None,
    health: Any = None,
    assignment: Any = None,
    journal_summary: Any = None,
    screenshots: Any = (),
    decisions: Any = (),
    title: str | None = None,
    achievements: Any = (),
) -> dict[str, Any]:
    """A portfolio report from other tools' results. Read-only (reads PROJECT_CONTEXT.md at most).

    Returns {ok, title, markdown, html, sections: [keys], warnings, project_context: {path, found}}; ok is False
    (with error) only when project_dir is given but is not a folder.
    """
    warnings: list[str] = []
    folder: Path | None = None
    context: dict[str, Any] | None = None
    if project_dir is not None and project_dir != "":
        if not isinstance(project_dir, (str, os.PathLike)) or _CONTROL.search(os.fspath(project_dir)):
            return {"ok": False, "error": "project_dir must be a folder path", "markdown": None, "html": None}
        folder = Path(project_dir)
        if not folder.is_dir():
            return {"ok": False, "error": f"project_dir is not an existing folder: {_text(folder, 200)}",
                    "markdown": None, "html": None}
        context = _read_context(folder, warnings)
        if context is None and not any(CONTEXT_FILE in w for w in warnings):
            warnings.append(f"No {CONTEXT_FILE} in the project folder, so the goal and next steps are not shown.")

    name = _text(title, 200) if title else ""
    name = name or (context or {}).get("name") or (_text(folder.name, 120) if folder is not None else "") \
        or DEFAULT_TITLE
    now = datetime.now()
    subtitle = f"OpenUSD project report, {now.strftime('%Y-%m-%d')}"

    sections: list[_Section] = []
    goal = _Section("goal", "Goal")
    if context and _blocks_from_lines(context.get("goal"), goal):
        sections.append(goal)
    for built in (_scene_section(scene_summary), _tour_section(tour), _health_section(health),
                  _assignment_section(assignment), _journal_section(journal_summary)):
        if built is not None:
            sections.append(built)

    proved = [_text(item, 300) for item in list(achievements or [])[:20] if _text(item, 300)]
    if proved:
        evidence = _Section("achievements", "What I built and proved")
        evidence.add("list", proved)
        sections.append(evidence)

    if isinstance(screenshots, (str, os.PathLike, Mapping)):
        screenshots = [screenshots]
    images = []
    if screenshots:
        items = list(screenshots) if isinstance(screenshots, Iterable) else []
        if len(items) > MAX_SCREENSHOTS:
            warnings.append(f"Only the first {MAX_SCREENSHOTS} of {len(items)} screenshots are shown.")
        for item in items[:MAX_SCREENSHOTS]:
            safe = _safe_image(item, folder, warnings)
            if safe is not None:
                images.append(safe)
    if images:
        gallery = _Section("screenshots", "Screenshots")
        gallery.add("images", images)
        sections.append(gallery)

    decision_items = _decision_items(decisions)
    decided = _Section("decisions", "Decisions and lessons")
    if decision_items:
        decided.add("list", decision_items)
        sections.append(decided)
    elif context and _blocks_from_lines(context.get("decisions"), decided):
        sections.append(decided)

    upcoming = _Section("next_steps", "Next steps")
    from_context = bool(context) and _blocks_from_lines(context.get("next_steps"), upcoming)
    if isinstance(assignment, Mapping) and assignment.get("ok") is not False and assignment.get("passed") is False \
            and assignment.get("next_step"):
        upcoming.add("p", f"From the assignment check: {_text(assignment['next_step'], 500)}")
    if from_context or upcoming.blocks:
        sections.append(upcoming)

    footer = f"Made on {now.strftime('%Y-%m-%d %H:%M')} with omniverse_scene_mcp project_report."
    if not sections:
        warnings.append("No results were given, so the report only has a title. Pass a tour, an assignment check "
                        "or a journal summary to fill it.")
    return {
        "ok": True,
        "title": name,
        "markdown": _render_markdown(name, subtitle, sections, footer),
        "html": _render_html(name, subtitle, sections, footer),
        "sections": [section.key for section in sections],
        "warnings": warnings,
        "project_context": {"path": str(folder / CONTEXT_FILE) if folder is not None else None,
                            "found": context is not None},
    }


def write_report(
    path_dir: str | os.PathLike[str],
    report: Mapping[str, Any],
    *,
    dry_run: bool = True,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write report.md and report.html into an existing folder. A preview unless dry_run=False; existing files are
    only replaced with overwrite=True. Returns {ok, dry_run, folder, files: [{name, path, bytes, exists}], written,
    message} (ok False with error when refused; then nothing was written)."""

    def refuse(error: str, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"ok": False, "dry_run": dry_run is not False, "folder": None if folder is None else str(folder),
                "files": files or [], "written": [], "error": error}

    folder: Path | None = None
    if not isinstance(path_dir, (str, os.PathLike)) or not os.fspath(path_dir).strip():
        return refuse("Give the folder to write into explicitly (an existing folder path).")
    if _CONTROL.search(os.fspath(path_dir)):
        return refuse("The folder path contains control characters.")
    folder = Path(path_dir)
    if not folder.is_dir():
        return refuse(f"{_text(folder, 200)} is not an existing folder; create it first (nothing was written).")
    if not isinstance(report, Mapping) or report.get("ok") is not True:
        return refuse("report must be the result of build_report with ok True.")
    contents = {"report.md": report.get("markdown"), "report.html": report.get("html")}
    if not all(isinstance(text, str) and text for text in contents.values()):
        return refuse("report has no markdown/html text; build it with build_report first.")

    files = []
    for name in REPORT_FILES:
        target = folder / name
        data = contents[name].encode("utf-8")
        files.append({"name": name, "path": str(target), "bytes": len(data), "exists": target.exists()})
        if target.exists() and not target.is_file():
            return refuse(f"{name} in that folder is not a file, so it will not be replaced.", files)
    existing = [item["name"] for item in files if item["exists"]]
    if existing and overwrite is not True:
        return refuse(f"{' and '.join(existing)} already exist{'s' if len(existing) == 1 else ''} in that folder; "
                      "pass overwrite=True to replace, or pick another folder. Nothing was written.", files)
    if dry_run is not False:
        verb = "replace" if existing else "create"
        return {"ok": True, "dry_run": True, "folder": str(folder), "files": files, "written": [],
                "message": f"Preview only: would {verb} {', '.join(REPORT_FILES)} in {folder}. Call again with "
                           "dry_run=False to write."}

    written: list[str] = []
    created: list[Path] = []
    try:
        for item in files:
            target = Path(item["path"])
            mode = "w" if item["exists"] else "x"
            with open(target, mode, encoding="utf-8", newline="\n") as handle:
                handle.write(contents[item["name"]])
            written.append(str(target))
            if not item["exists"]:
                created.append(target)
    except OSError as exc:
        for path in created:  # do not leave half a report behind
            try:
                path.unlink()
            except OSError:
                pass
        return refuse(f"Could not write the report: {_text(exc, 200)}", files)
    return {"ok": True, "dry_run": False, "folder": str(folder), "files": files, "written": written,
            "message": f"Wrote {', '.join(REPORT_FILES)} in {folder}. Open report.html in a browser to see it."}


def collect_achievements(project_dir: str | os.PathLike[str] | None, workspace: str | os.PathLike[str]) -> list[str]:
    """Evidence for the report, read from disk (read-only): datasets made (frames, randomized or not), trained
    colour models and their tested accuracy, and robot motion recordings."""
    import json as _json

    found: list[str] = []
    folder = Path(project_dir) if project_dir else None
    if folder is not None and folder.is_dir():
        for sub in sorted(p for p in folder.iterdir() if p.is_dir())[:200]:
            frames = len(list(sub.glob("rgb_*.png")))
            if frames:
                manifest = sub / "dataset_manifest.json"
                randomized = False
                try:
                    randomized = bool(_json.loads(manifest.read_text(encoding="utf-8")).get("randomize"))
                except (OSError, ValueError, AttributeError):
                    pass
                found.append(f"Synthetic dataset {sub.name}: {frames} rendered frame(s)"
                             + (", with domain randomization (positions, colours, lighting)" if randomized else ""))
            model = sub / "colour_model.json"
            if model.is_file():
                try:
                    data = _json.loads(model.read_text(encoding="utf-8"))
                    found.append(f"Trained a colour classifier ({', '.join(sorted(data.get('centroids', {})))}) on "
                                 f"{sub.name}: {data.get('test_accuracy_pct')}% on held-out frames")
                except (OSError, ValueError, AttributeError):
                    pass
    recordings = Path(workspace) / "isaac_study" / "recordings"
    files = sorted(recordings.glob("*.json")) if recordings.is_dir() else []
    if files:
        longest, robots = 0.0, set()
        for path in files[:200]:
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
                longest = max(longest, float(data["samples"][-1]["t"]))
                robots.add(str(data.get("robot_root")))
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                continue
        found.append(f"Recorded {len(files)} robot motion run(s) of {', '.join(sorted(robots)) or 'robots'} "
                     f"(longest {longest:.0f} s) for replay and comparison")
    return found
