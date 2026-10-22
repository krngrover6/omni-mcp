"""Plain-English explanations for errors pasted from the Script Editor, the Kit console or a terminal.

``explain_error(text)`` reads Python tracebacks, Kit ``[Error]``/``[Warning]`` log lines, USD/Tf diagnostics
(``Error in '...' at line N``, ``Coding Error``, ``pxr.Tf.ErrorException``), Boost.Python ``did not match C++
signature`` errors, PhysX messages and ``omni.kit.commands`` failures, and returns a beginner-friendly explanation:
what happened, likely causes, numbered fix steps and a short commented example.

How it decides:

* The **root cause** is the last line of the *first* traceback (later tracebacks and log errors are usually
  consequences). Without a traceback it is the first error line, else the first warning, else the whole text.
* The root's text is matched against ``data/error_patterns.json``; patterns are tried in file order, so the specific
  ones (``stage_is_none``) win over the generic ones (``attribute_error_none``, ``python_exception``).
* ``where`` comes from the last traceback frame that is not library code (site-packages, Kit ``exts`` folders ...),
  plus the user's line of code when the traceback shows it or ``code=`` is given.

The text is only ever read with regular expressions: nothing in it is evaluated, compiled, imported or executed.
Input is capped (``MAX_INPUT_CHARS`` / ``MAX_LINE_CHARS``) so huge or garbage pastes stay fast.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from typing import Any, Iterable

PATTERNS_PATH = Path(__file__).with_name("data") / "error_patterns.json"

MAX_INPUT_CHARS = 200_000
MAX_CODE_CHARS = 100_000
MAX_LINE_CHARS = 2_000
_HEAD_CHARS = 160_000  # a capped paste keeps its head (where the first traceback is) and a short tail
_TRACEBACK_SCAN = 600  # lines one traceback may span
_CONTINUATION_LINES = 25  # lines of a multi-line exception message (Tf errors, Boost signatures)
_MAX_CANDIDATES = 500
_OTHER_ERRORS = 5
_FALLBACK_SCAN = 50
_DOC_HITS = 3
_DOC_SNIPPET = 240
_GROUP_CHARS = 120

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PREFIX = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?Z?\s*)?"
    r"(?:\[\s*[\d,.]+\s*m?s\]\s*)?"
    r"\[(?P<level>Error|Warning|Fatal|Info|Verbose|Debug)\]\s?"
    r"(?:\[(?P<source>[^\]\n]{1,160})\]\s?)?"
    r"(?:\[py std(?:err|out)\]:?\s?)?",
    re.I,
)
_TRACEBACK = re.compile(r"^\s*Traceback \(most recent call last\):?")
_PY_FRAME = re.compile(r'^\s*File "(?P<file>[^"\n]{1,500})", line (?P<line>\d{1,7})(?:, in (?P<func>[^\n]{1,200}))?')
_KIT_FRAME = re.compile(r"^\s*(?P<file>[^\s(][^()\n]{0,500}?)\((?P<line>\d{1,7})\):\s*(?P<func>[^\n]{1,200})$")
_EXCEPTION = re.compile(
    r"^(?P<qualname>(?:[A-Za-z_]\w{0,80}\.){0,6}(?P<type>[A-Za-z_]\w{0,80}?"
    r"(?:Error|Exception|Exit|Interrupt|Warning|Iteration)))(?::|$)"
)
_DOTTED_COLON = re.compile(r"^(?:[A-Za-z_]\w{0,80}\.){1,6}[A-Za-z_]\w{0,80}: ")
_CARET = re.compile(r"^\s*[\^~]+[\s\^~]*$")
_TF_ERROR = re.compile(r"\b(?:Coding|Runtime|Fatal) Error\b|^Error(?::| in ')|\bError in '[^'\n]{1,200}' at line \d")
_WARNING_TEXT = re.compile(r"^\s*Warning:|\b\w+Warning:")
_LIBRARY = re.compile(
    r"site-packages|dist-packages|pip_prebundle|<frozen |[\\/]exts(?:cache|core|Internal|Deprecated)?[\\/]"
    r"|[\\/]kit[\\/]|python3\d*[\\/]lib[\\/]|[\\/]importlib[\\/]|[\\/]pxr[\\/]",
    re.I,
)
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_WORDS = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{2,60}")

_DEFAULTS = {
    "name": "that name",
    "attr": "that attribute",
    "module": "that module",
    "path": "that path",
    "prim": "that prim",
    "key": "that key",
    "type": "this kind of",
    "func": "the function",
    "arg": "that argument",
    "expected": "the expected type",
    "got": "the type you passed",
    "ext": "that extension",
    "call": "the function",
    "signature": "the C++ signature",
    "exception": "an exception",
    "message": "",
    "given": "more",
    "count": "some",
}

_UNKNOWN = {
    "kind": "unknown",
    "category": "unknown",
    "severity": "unknown",
    "generic": True,
    "summary": "This does not match an error I recognise, but here is how to read it.",
    "what_happened": (
        "I could not match this text to a known Script Editor, Kit, USD or PhysX error. Python errors end with a "
        "line like 'SomeError: reason'; Kit log errors start with [Error] [extension.name]; USD errors often say "
        "\"Error in '...' at line N\". The reason is usually in the last line or in the quoted text."
    ),
    "likely_causes": [
        "The pasted text may be cut off before the actual error line.",
        "It may be a message from an extension or tool that is not in the pattern list yet.",
    ],
    "how_to_fix": [
        "Copy the whole output, from 'Traceback (most recent call last):' down to the last line, and try again.",
        "Look for the first line that says Error or [Error] - later errors are often consequences of the first.",
        "Search the docs for the exact error message (without your own prim names and paths).",
    ],
    "example_fix": None,
    "docs_query": "",
}

_EMPTY = {
    "kind": "empty",
    "category": "unknown",
    "severity": "unknown",
    "generic": True,
    "summary": "No error text was given.",
    "what_happened": "There was no text to explain.",
    "likely_causes": ["The paste was empty or only contained spaces."],
    "how_to_fix": [
        "In the Script Editor or the Console window, select the full error output (Ctrl+A in the output pane) and copy it.",
        "Paste it in, including the 'Traceback (most recent call last):' line and the last line of the error.",
    ],
    "example_fix": None,
    "docs_query": "",
}


# --- pattern data ------------------------------------------------------------------------------------------------------

class _Pattern:
    __slots__ = ("data", "kind", "generic", "match", "require", "context")

    def __init__(self, data: dict[str, Any]) -> None:
        flags = re.I | re.M
        self.data = data
        self.kind = str(data["kind"])
        self.generic = bool(data.get("generic"))
        self.match = [re.compile(rx, flags) for rx in data["match"]]
        self.require = [re.compile(rx, flags) for rx in data.get("require") or ()]
        self.context = [re.compile(rx, flags) for rx in data.get("context_match") or ()]

    def test(self, text: str, context: str) -> dict[str, str] | None:
        """Named groups when this pattern fits ``text`` (and ``context`` if it has context rules), else None."""
        if self.require and not all(rx.search(text) for rx in self.require):
            return None
        if self.context and not any(rx.search(context) for rx in self.context):
            return None
        groups: dict[str, str] = {}
        hit = False
        for rx in self.match:
            found = rx.search(text)
            if not found:
                continue
            hit = True
            for key, value in found.groupdict().items():
                cleaned = _clean_group(value)
                if cleaned and key not in groups:
                    groups[key] = cleaned
        return groups if hit else None


_LOCK = threading.Lock()
_CACHE: dict[str, list[_Pattern]] = {}

_REQUIRED_FIELDS = ("kind", "category", "match", "summary", "what_happened", "likely_causes", "how_to_fix")


def load_patterns(path: str | Path | None = None) -> list[dict[str, Any]]:
    """The raw pattern entries from ``error_patterns.json`` (validated; regexes compile), in matching order."""
    return [p.data for p in _patterns(path)]


def _patterns(path: str | Path | None = None) -> list[_Pattern]:
    key = str(Path(path) if path else PATTERNS_PATH)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        raw = json.loads(Path(key).read_text(encoding="utf-8"))
        entries = raw["patterns"] if isinstance(raw, dict) else raw
        patterns: list[_Pattern] = []
        seen: set[str] = set()
        for entry in entries:
            missing = [f for f in _REQUIRED_FIELDS if not entry.get(f)]
            if missing:
                raise ValueError(f"error pattern {entry.get('kind')!r} is missing {', '.join(missing)}")
            if entry["kind"] in seen:
                raise ValueError(f"duplicate error pattern kind {entry['kind']!r}")
            seen.add(entry["kind"])
            patterns.append(_Pattern(entry))
        _CACHE[key] = patterns
        return patterns


def list_patterns() -> list[dict[str, Any]]:
    """One line per known error kind: [{kind, category, severity, summary}] (summary placeholders left as-is)."""
    return [
        {
            "kind": p.kind,
            "category": p.data["category"],
            "severity": p.data.get("severity", "error"),
            "summary": p.data["summary"],
        }
        for p in _patterns()
    ]


# --- text helpers ------------------------------------------------------------------------------------------------------

def _coerce(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value[: MAX_INPUT_CHARS * 4]).decode("utf-8", errors="replace")
    if not isinstance(value, str):
        try:
            value = str(value)
        except Exception:  # noqa: BLE001 - a hostile __str__ must not break the explainer
            return ""
    return value


def _normalize(text: str, limit: int) -> tuple[str, bool]:
    truncated = len(text) > limit
    if truncated:
        head = min(_HEAD_CHARS, limit)
        text = text[:head] + "\n" + text[-(limit - head):] if limit > head else text[:limit]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI.sub("", text)
    text = _CONTROL.sub("", text)
    return text, truncated


def _clean_group(value: str | None) -> str:
    if not value:
        return ""
    value = " ".join(str(value).split())
    if len(value) > _GROUP_CHARS:
        value = value[: _GROUP_CHARS - 3] + "..."
    return value


def _fill(text: Any, groups: dict[str, str]) -> Any:
    if not isinstance(text, str):
        return text
    return _PLACEHOLDER.sub(lambda m: groups.get(m.group(1)) or _DEFAULTS.get(m.group(1), "it"), text)


class _Line:
    __slots__ = ("text", "level", "source", "prefixed")

    def __init__(self, raw: str) -> None:
        if len(raw) > MAX_LINE_CHARS:
            raw = raw[:MAX_LINE_CHARS]
        match = _PREFIX.match(raw)
        if match:
            self.text = raw[match.end():]
            self.level = match.group("level").lower()
            self.source = match.group("source") or ""
            self.prefixed = True
        else:
            self.text = raw
            self.level = ""
            self.source = ""
            self.prefixed = False


def _tag(line: _Line) -> str:
    if not line.prefixed:
        return ""
    return f"[{line.level.title()}]" + (f" [{line.source}]" if line.source else "")


def _frame(match: re.Match[str]) -> dict[str, Any]:
    frame = {"file": _clean_group(match.group("file")) or "?", "line": int(match.group("line"))}
    func = _clean_group(match.group("func"))
    if func:
        frame["function"] = func
    return frame


def _is_frame(text: str) -> bool:
    return bool(_PY_FRAME.match(text))


def _exception_parts(first_line: str) -> tuple[str | None, str]:
    match = _EXCEPTION.match(first_line)
    if not match:
        return None, first_line
    message = first_line[match.end():].strip()
    return match.group("type"), message


# --- parsing -------------------------------------------------------------------------------------------------------------

class _Error:
    """One error found in the text: its block of lines, traceback frames and severity."""

    __slots__ = ("index", "block", "frames", "severity", "exception", "message", "from_traceback", "tag")

    def __init__(self, index: int, block: list[str], frames: list[dict[str, Any]], severity: str,
                 from_traceback: bool, tag: str = "") -> None:
        self.index = index
        self.tag = tag  # '[Error] [omni.physx.plugin]' for Kit log lines, so patterns can see level and extension
        self.block = block
        self.frames = frames
        self.severity = severity
        self.from_traceback = from_traceback
        self.exception, self.message = _exception_parts(block[0] if block else "")

    @property
    def text(self) -> str:
        body = "\n".join(self.block)
        return f"{self.tag}\n{body}" if self.tag else body

    @property
    def first_line(self) -> str:
        return self.block[0] if self.block else ""


def _continuation(lines: list[_Line], index: int) -> tuple[list[str], int]:
    """The exception line plus its message lines (Tf errors and Boost signatures span several lines)."""
    head = lines[index]
    block = [head.text.strip()]
    j = index + 1
    while j < len(lines) and len(block) < _CONTINUATION_LINES:
        line = lines[j]
        stripped = line.text.strip()
        if not stripped or stripped == "At:":
            break
        if line.prefixed and not (head.prefixed and line.level == head.level and line.source == head.source):
            break
        if (_TRACEBACK.match(stripped) or stripped.startswith(("During handling", "The above exception"))
                or _is_frame(line.text) or _EXCEPTION.match(stripped)):
            break
        block.append(stripped)
        j += 1
    return block, j


def _kit_frames_after(lines: list[_Line], start: int) -> list[dict[str, Any]]:
    """Kit prints 'At:' followed by 'file(line): function' frames after an error message."""
    frames: list[dict[str, Any]] = []
    j = start
    end = min(len(lines), start + 6)
    while j < end:
        stripped = lines[j].text.strip()
        if stripped == "At:":
            break
        if stripped and (lines[j].prefixed or _EXCEPTION.match(stripped) or _TRACEBACK.match(stripped)):
            return frames
        j += 1
    else:
        return frames
    j += 1
    while j < len(lines) and len(frames) < 50:
        match = _KIT_FRAME.match(lines[j].text)
        if not match:
            break
        frames.append(_frame(match))
        j += 1
    # Kit lists the innermost frame first; store them like Python does (innermost last).
    frames.reverse()
    return frames


def _frames_before(lines: list[_Line], index: int) -> list[dict[str, Any]]:
    """A SyntaxError pasted without its 'Traceback' header still has 'File ..., line N' just above it."""
    for j in range(index - 1, max(-1, index - 6), -1):
        match = _PY_FRAME.match(lines[j].text)
        if match:
            frame = _frame(match)
            if j + 1 < index:
                code = lines[j + 1].text.strip()
                if code and not _CARET.match(lines[j + 1].text):
                    frame["code"] = code[:300]
            return [frame]
    return []


def _parse_traceback(lines: list[_Line], start: int) -> tuple[_Error | None, int]:
    frames: list[dict[str, Any]] = []
    i = start + 1
    limit = min(len(lines), start + _TRACEBACK_SCAN)
    while i < limit:
        text = lines[i].text
        stripped = text.strip()
        if _TRACEBACK.match(stripped):
            return None, i
        match = _PY_FRAME.match(text)
        if match:
            frames.append(_frame(match))
            i += 1
            if i < limit:
                nxt = lines[i].text
                code = nxt.strip()
                if (code and not _is_frame(nxt) and not _EXCEPTION.match(code) and not _CARET.match(nxt)
                        and not _TRACEBACK.match(code)):
                    frames[-1]["code"] = code[:300]
                    i += 1
            continue
        if not stripped or _CARET.match(text) or stripped.startswith("[Previous line repeated") or stripped == "...":
            i += 1
            continue
        if _EXCEPTION.match(stripped) or (frames and _DOTTED_COLON.match(stripped)):
            block, end = _continuation(lines, i)
            return _Error(i, block, frames, _severity_of(block[0]), True, _tag(lines[i])), end
        i += 1
    return None, i


def _severity_of(first_line: str) -> str:
    exception, _message = _exception_parts(first_line)
    return "warning" if exception and exception.endswith("Warning") else "error"


def _scan(lines: list[_Line]) -> tuple[list[_Error], list[_Error]]:
    """(errors from tracebacks in order, standalone error/warning lines in order)."""
    tracebacks: list[_Error] = []
    standalone: list[_Error] = []
    covered_until = -1
    i = 0
    while i < len(lines) and len(tracebacks) + len(standalone) < _MAX_CANDIDATES:
        line = lines[i]
        stripped = line.text.strip()
        if _TRACEBACK.match(stripped):
            error, end = _parse_traceback(lines, i)
            if error is not None:
                tracebacks.append(error)
            covered_until = max(covered_until, end - 1)
            i = max(end, i + 1)
            continue
        if i <= covered_until or not stripped:
            i += 1
            continue
        severity = None
        if _EXCEPTION.match(stripped):
            severity = _severity_of(stripped)
        elif line.level in ("error", "fatal") or _TF_ERROR.search(stripped):
            severity = "error"
        elif line.level == "warning" or _WARNING_TEXT.search(stripped):
            severity = "warning"
        if severity is None:
            i += 1
            continue
        block, end = _continuation(lines, i)
        frames = _frames_before(lines, i) if _EXCEPTION.match(stripped) else []
        if not frames:
            frames = _kit_frames_after(lines, end)
        standalone.append(_Error(i, block, frames, severity, False, _tag(line)))
        i = max(end, i + 1)
    return tracebacks, standalone


# --- matching, location and docs ---------------------------------------------------------------------------------------

def _context(error: _Error, code: str) -> str:
    parts = []
    for frame in error.frames:
        parts.append(f"{frame.get('file', '')} {frame.get('function', '')} {frame.get('code', '')}")
    if code:
        parts.append(code)
    return "\n".join(parts)


def _best_pattern(text: str, context: str) -> tuple[_Pattern | None, dict[str, str]]:
    for pattern in _patterns():
        groups = pattern.test(text, context)
        if groups is not None:
            return pattern, groups
    return None, {}


def _user_frame(frames: list[dict[str, Any]]) -> dict[str, Any] | None:
    for frame in reversed(frames):
        if not _LIBRARY.search(frame.get("file", "")):
            return frame
    return frames[-1] if frames else None


def _locate_in_code(code_lines: list[str], groups: dict[str, str], names: Iterable[str]) -> dict[str, Any] | None:
    for name in names:
        value = groups.get(name, "").strip("'\" ")
        if not value or len(value) < 2 or value.endswith("..."):
            continue
        token = value.split(".")[-1] if name in ("func", "attr") else value
        word = re.compile(r"(?<![\w])" + re.escape(token) + r"(?![\w])")
        for number, line in enumerate(code_lines, start=1):
            if word.search(line) and not line.lstrip().startswith("#"):
                return {"line": number, "code": line.strip()[:300], "location": f"your code:{number}",
                        "found_by": f"searching your code for '{token}'"}
    return None


def _where(error: _Error, code_lines: list[str], groups: dict[str, str], locate: Iterable[str]) -> dict[str, Any] | None:
    frame = _user_frame(error.frames)
    if frame is not None:
        where: dict[str, Any] = {
            "file": frame["file"],
            "line": frame["line"],
            "location": f"{frame['file']}:{frame['line']}",
        }
        if frame.get("function"):
            where["function"] = frame["function"]
        if frame.get("code"):
            where["code"] = frame["code"]
        elif code_lines and not _LIBRARY.search(frame["file"]) and 1 <= frame["line"] <= len(code_lines):
            where["code"] = code_lines[frame["line"] - 1].strip()[:300]
            where["found_by"] = "line number applied to your code"
        if _LIBRARY.search(frame["file"]):
            where["note"] = "This frame is inside library code; your own call is further up the traceback."
        return where
    if code_lines:
        return _locate_in_code(code_lines, groups, locate)
    return None


def _docs_query(pattern_data: dict[str, Any], error: _Error | None, groups: dict[str, str]) -> str:
    query = _fill(pattern_data.get("docs_query") or "", groups).strip()
    if query:
        return query
    if error is None:
        return ""
    words = _WORDS.findall(f"{error.exception or ''} {error.message or error.first_line}")
    return " ".join(words[:14])


def _normalize_hit(hit: Any) -> dict[str, Any] | None:
    if not isinstance(hit, dict):
        return None
    title = _clean_group(str(hit.get("title") or hit.get("name") or ""))
    where = hit.get("url_or_path") or hit.get("source_path") or hit.get("path") or hit.get("url") or ""
    if not title and not where:
        return None
    snippet = " ".join(str(hit.get("snippet") or hit.get("summary") or "").split())
    if len(snippet) > _DOC_SNIPPET:
        snippet = snippet[: _DOC_SNIPPET - 3] + "..."
    source = hit.get("source") or hit.get("extension") or hit.get("domain") or ""
    return {"title": title, "source": str(source), "location": str(where), "snippet": snippet}


def _related_docs(knowledge: Any, query: str) -> list[dict[str, Any]]:
    if not query:
        return []
    backends = list(knowledge) if isinstance(knowledge, (list, tuple)) else [knowledge]
    per_backend: list[list[dict[str, Any]]] = []
    for backend in backends:
        try:
            if hasattr(backend, "search"):
                hits = backend.search(query, top_k=_DOC_HITS)
            elif callable(backend):
                hits = backend(query)
            else:
                continue
            normalized = [n for n in (_normalize_hit(h) for h in list(hits or [])[: _DOC_HITS * 2]) if n]
        except Exception:  # noqa: BLE001 - docs are a bonus; a broken index must not hide the explanation
            continue
        per_backend.append(normalized)
    picked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for rank in range(_DOC_HITS * 2):
        for hits in per_backend:
            if rank < len(hits):
                key = (hits[rank]["title"], hits[rank]["location"])
                if key not in seen:
                    seen.add(key)
                    picked.append(hits[rank])
            if len(picked) >= _DOC_HITS:
                return picked
    return picked


# --- public API ----------------------------------------------------------------------------------------------------------

def explain_error(text: str, *, code: str | None = None, knowledge: Any = None) -> dict[str, Any]:
    """Explain pasted error output in plain English. Never executes anything from ``text`` or ``code``.

    ``code`` (optional) is the script that produced the error; it is used to show the failing line.
    ``knowledge`` (optional) is anything with ``search(query, top_k=...)`` (OfflineUSDKnowledge,
    OfflineDocsKnowledge), a callable ``query -> [hit, ...]``, or a list of those; the top 3 hits become
    ``related_docs``. Without it ``related_docs`` is left out.

    Returns ``{ok, kind, category, severity, summary, what_happened, likely_causes, how_to_fix, example_fix, where,
    confidence ('high'|'medium'|'low'), exception, message, error_count, other_errors, truncated[, related_docs]}``.
    """
    raw = _coerce(text)
    source, truncated = _normalize(raw, MAX_INPUT_CHARS)
    code_text, _code_truncated = _normalize(_coerce(code), MAX_CODE_CHARS)
    code_lines = code_text.split("\n") if code_text.strip() else []

    if not source.strip():
        return _result(_EMPTY, {}, None, None, "low", [], 0, truncated, False, knowledge)

    lines = [_Line(raw_line) for raw_line in source.split("\n")]
    tracebacks, standalone = _scan(lines)
    errors = [e for e in standalone if e.severity == "error"]
    warnings = [e for e in standalone if e.severity != "error"]

    root: _Error | None
    if tracebacks:
        root = tracebacks[0]
    elif errors:
        root = errors[0]
    elif warnings:
        root = warnings[0]
    else:
        root = None

    whole_text = False
    if root is None:
        # Nothing looks like an error line: match the start of the text itself ("Cannot author ... instance proxy").
        head = [line.text.strip() for line in lines if line.text.strip()][:_CONTINUATION_LINES]
        root = _Error(0, head or [source.strip()[:MAX_LINE_CHARS]], [], "error", False)
        whole_text = True

    pattern, groups = _best_pattern(root.text, _context(root, code_text))
    confidence = "medium" if whole_text else "high"
    if pattern is None:
        # The root is not recognised; a later, recognised error is the next best explanation.
        for candidate in (tracebacks[1:] + errors + warnings)[:_FALLBACK_SCAN]:
            if candidate is root:
                continue
            pattern, groups = _best_pattern(candidate.text, _context(candidate, code_text))
            if pattern is not None and not pattern.generic:
                root, confidence = candidate, "medium"
                break
            pattern, groups = None, {}
    if pattern is not None and pattern.generic:
        confidence = "medium"

    all_errors = tracebacks + errors + (warnings if not tracebacks and not errors else [])
    others = [e for e in sorted(tracebacks + errors, key=lambda e: e.index) if e is not root]
    other_errors = []
    seen_lines = {root.first_line}
    for other in others:
        if other.first_line in seen_lines:
            continue
        seen_lines.add(other.first_line)
        other_pattern, _other_groups = _best_pattern(other.text, _context(other, ""))
        other_errors.append({
            "line": other.index + 1,
            "text": other.first_line[:200],
            "kind": other_pattern.kind if other_pattern else "unknown",
        })
        if len(other_errors) >= _OTHER_ERRORS:
            break

    groups = dict(groups)
    if root.exception:
        groups.setdefault("exception", root.exception)
    if root.message:
        groups.setdefault("message", _clean_group(root.message))
    data = pattern.data if pattern is not None else _UNKNOWN
    if pattern is None:
        confidence = "low"
    locate = list(data.get("locate") or []) + ["attr", "name", "key", "func", "arg"]
    where = _where(root, code_lines, groups, locate)
    return _result(data, groups, root, where, confidence, other_errors, len(all_errors), truncated, True,
                   knowledge)


def _result(data: dict[str, Any], groups: dict[str, str], root: _Error | None, where: dict[str, Any] | None,
            confidence: str, other_errors: list[dict[str, Any]], error_count: int, truncated: bool, ok: bool,
            knowledge: Any) -> dict[str, Any]:
    if root is not None and (root.severity == "warning" or data["kind"] == "unknown"):
        severity = root.severity  # a '[Warning]' log line stays a warning even when the pattern is usually an error
    else:
        severity = data.get("severity", "error")
    result: dict[str, Any] = {
        "ok": ok,
        "kind": data["kind"],
        "category": data.get("category", "unknown"),
        "severity": severity,
        "summary": _fill(data["summary"], groups),
        "what_happened": _fill(data["what_happened"], groups),
        "likely_causes": [_fill(item, groups) for item in data.get("likely_causes") or []],
        "how_to_fix": [_fill(item, groups) for item in data.get("how_to_fix") or []],
        "example_fix": _fill(data.get("example_fix"), groups),
        "where": where,
        "confidence": confidence,
        "exception": root.exception if root is not None else None,
        # Multi-line messages (Tf errors, Boost signatures) read as one line: 'pxr.Tf.ErrorException: Error in ...'.
        "message": " ".join(" ".join(root.block).split())[:500] if root is not None else "",
        "error_count": error_count,
        "other_errors": other_errors,
        "truncated": truncated,
    }
    if other_errors:
        result["note"] = (
            "Fix the first error first: the other errors listed in other_errors are often caused by it."
        )
    if knowledge is not None and ok:
        result["related_docs"] = _related_docs(knowledge, _docs_query(data, root, groups))
    return result


__all__ = ["MAX_INPUT_CHARS", "PATTERNS_PATH", "explain_error", "list_patterns", "load_patterns"]
