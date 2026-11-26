"""study_check: run usd_code_verify and usda_check over study material, read-only.

Study files (the 25-day plan, guides, notes) were written over many sessions and only partly replayed live, and a wrong
fact there gets memorised for the exam. This tool finds every ```python / ```usda block in Markdown files, every <pre>
code block in HTML guides, and every .py / .usda file in a folder of the workspace, checks each one statically (no code
is run) and lists the real problems with file and line. Fragments that are not meant to stand alone (a snippet with
"...", a half prim) are counted as skipped, never reported as mistakes.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .exam.progress import workspace_root

MAX_FILES = 400
MAX_FILE_BYTES = 2_000_000
MAX_REPORTED = 60
_FENCE = re.compile(r"^([ \t]*)```[ \t]*(python|py|usda)[ \t]*\n(.*?)^\1```", re.M | re.S)
_PRE = re.compile(r"<pre[^>]*>(?:\s*<code[^>]*>)?(.*?)(?:</code>\s*)?</pre>", re.S | re.I)
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", "phone", "print-bw"}


def _looks_usda(text: str) -> bool:
    return bool(re.search(r'^\s*(#usda|def\s+\w*\s*"|over\s+"|class\s+"\w+")', text, re.M))


def _blocks(path: Path, text: str) -> list[tuple[str, int, str]]:
    """(kind, first line, code) for each block in a file."""
    suffix = path.suffix.lower()
    if suffix == ".py":
        return [("python", 1, text)]
    if suffix == ".usda":
        return [("usda", 1, text)]
    out = []
    if suffix == ".md":
        for match in _FENCE.finditer(text):
            indent, lang, body = match.group(1), match.group(2), match.group(3)
            if indent:
                body = "\n".join(line[len(indent):] if line.startswith(indent) else line for line in body.splitlines())
            line = text.count("\n", 0, match.start()) + 2
            out.append(("usda" if lang == "usda" else "python", line, body))
    elif suffix in (".html", ".htm"):
        for match in _PRE.finditer(text):
            body = html.unescape(re.sub(r"<[^>]+>", "", match.group(1)))
            line = text.count("\n", 0, match.start()) + 1
            if _looks_usda(body):
                out.append(("usda", line, body))
            elif re.search(r"^\s*(from|import)\s+\w|^\s*\w+\s*=\s*\w+\.\w+\(", body, re.M):
                out.append(("python", line, body))
    return out


def _fragment(kind: str, code: str) -> bool:
    if "..." in code or "…" in code or "<your" in code.lower():
        return True
    if kind == "usda":
        return code.count("{") != code.count("}") or code.count("(") != code.count(")")
    return False


def check_study(folder: Any = "exam-prep/25-day-plan", include_html: Any = False) -> dict[str, Any]:
    """Check every python / usda code block in a workspace folder of study files (Markdown fences, .py and .usda files;
    HTML <pre> blocks with include_html=True) with usd_code_verify (static, nothing runs) and usda_check. Lists real
    problems with file and line; fragments ("...", half prims) are skipped. Read-only."""
    from .code_verify import verify_code
    from .usda_check import check_usda

    root = workspace_root().resolve()
    target = (root / str(folder)).resolve()
    if root != target and root not in target.parents:
        return {"ok": False, "error": "folder must be inside the workspace"}
    if not target.exists():
        return {"ok": False, "error": f"No such folder or file in the workspace: {folder}"}
    suffixes = {".md", ".py", ".usda"} | ({".html", ".htm"} if include_html is True else set())
    files = [target] if target.is_file() else sorted(
        p for p in target.rglob("*") if p.is_file() and p.suffix.lower() in suffixes
        and not any(part in _SKIP_DIRS for part in p.parts))
    problems: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for path in files[:MAX_FILES]:
        if path.stat().st_size > MAX_FILE_BYTES:
            counts["too_big"] += 1
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = str(path.relative_to(root)).replace("\\", "/")
        for kind, line, code in _blocks(path, text):
            if not code.strip():
                continue
            counts["blocks"] += 1
            if _fragment(kind, code):
                counts["skipped_fragments"] += 1
                continue
            if kind == "python":
                result = verify_code(code, run=False)
                if result.get("verdict") == "syntax_error":
                    counts["skipped_fragments"] += 1  # pseudo-code or a partial line, not a USD mistake
                    continue
                found = result.get("problems", [])
            else:
                # A whole .usda file is opened by path, so its relative sublayers and references resolve.
                result = check_usda(path=str(path)) if path.suffix.lower() == ".usda" else check_usda(text=code)
                if result.get("verdict") == "parse_error":
                    counts["skipped_fragments"] += 1
                    continue
                found = result.get("problems", [])
            counts["checked"] += 1
            for item in found:
                counts["problems"] += 1
                if len(problems) < MAX_REPORTED:
                    at = item.get("line")
                    problems.append({"file": rel, "line": (line + at - 1) if isinstance(at, int) else line,
                                     "kind": kind, "code": item.get("code"),
                                     "message": item.get("message"), **({"fix": item["fix"]} if item.get("fix") else {}),
                                     **({"tip": item["tip"]} if item.get("tip") else {})})
    return {
        "ok": True, "folder": str(target.relative_to(root)).replace("\\", "/") or ".", "files": len(files[:MAX_FILES]),
        "blocks": counts["blocks"], "checked": counts["checked"], "skipped_fragments": counts["skipped_fragments"],
        "problem_count": counts["problems"], "problems": problems, "truncated": counts["problems"] > len(problems),
        "next_step": (f"{problems[0]['file']}:{problems[0]['line']}: {problems[0]['message']}" if problems else
                      "No USD mistake found in the checked blocks (fragments were skipped; live behaviour is not "
                      "tested here)."),
    }
