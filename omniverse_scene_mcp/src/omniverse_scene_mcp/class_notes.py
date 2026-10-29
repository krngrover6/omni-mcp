"""Class companion: search the mentorship teacher's sessions offline.

Sources (all read-only):

* ``<classes root>/Session N/*.srt`` - subtitles of the compressed recording (``source="subtitles"``).
* ``<classes root>/Session N/*Gemini*.txt`` - Gemini meeting notes and transcript (``source="gemini_notes"``).
* ``<mentorship root>/**/REVISION_NOTES.md`` and ``PROJECT_CONTEXT.md`` (``source="revision_notes"``).

The compressed SRT and the Gemini transcript come from different recordings, so their clocks differ by
several minutes. Every hit carries its own ``timeline``; timestamps are never converted between the two.
The SRT is auto-generated and has known mistranscriptions, which is why both sources are indexed and labelled.
PDF copies of the Gemini notes are skipped: the standard library has no PDF text extractor, and the .txt
export holds the same notes.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, NamedTuple

from .offline_knowledge import _BM25Index, _flat, _markdown_sections, _tokenize, _trim, workspace_root

__all__ = [
    "PASSAGE_CHARS",
    "SOURCES",
    "ClassNotes",
    "Cue",
    "SearchHits",
    "default_roots",
    "format_timestamp",
    "merge_cues",
    "parse_gemini",
    "parse_srt",
]

PASSAGE_CHARS = 700
MAX_CUE_GAP = 20.0  # seconds of silence that end a subtitle passage (students practising, breaks)
SOURCES = ("subtitles", "gemini_notes", "revision_notes")
TIMELINES = {"subtitles": "compressed_srt", "gemini_notes": "gemini_transcript", "revision_notes": None}
SOURCE_LABELS = {
    "subtitles": "subtitles of the compressed recording (auto-generated; may mistranscribe)",
    "gemini_notes": "Gemini meeting notes and transcript (separate timeline)",
    "revision_notes": "your revision notes",
}
ENV_ROOT = "OMNIVERSE_CLASS_NOTES_ROOT"
DEFAULT_CLASSES_DIR = ("Downloads", "Issac Sim Classes")
FLAT_ROOT_NAME = "Downloads"  # only its top-level, session-named files are indexed (never all of Downloads)
_COPY_SUFFIX = re.compile(r" \(\d+\)$")  # "name (1).srt": a browser's duplicate download
REVISION_FILES = ("REVISION_NOTES.md", "PROJECT_CONTEXT.md")

TIMELINE_NOTE = (
    "Subtitle (compressed SRT) and Gemini transcript timestamps come from different recordings and do not line up "
    "(the Gemini clock runs several minutes later). Use each timestamp only with its own source; never convert or mix them."
)
SRT_CAVEAT = (
    "Subtitles are auto-generated and contain known mistranscriptions; where a subtitle passage and the Gemini notes "
    "disagree, trust the passage that makes technical sense and check the recording."
)
PDF_NOTE = "PDF files are not indexed (no standard-library text extraction); the Gemini .txt export is indexed instead."

# (session, source, window start, window end or None, any of these terms, caveat). From PROJECT_CONTEXT.md reviews.
KNOWN_SRT_ISSUES: tuple[tuple[int, str, float, float | None, tuple[str, ...], str], ...] = (
    (2, "subtitles", 0.0, None, ("usda", "usdc"),
     "The Session 2 subtitles swap USDA and USDC: USDA is readable text, USDC is the binary crate, USDZ a packaged archive."),
    (2, "subtitles", 4320.0, 4380.0, ("simulation", "play"),
     "Near 01:12:29 the subtitles reverse the instruction: the Gemini transcript says not to play the simulation (static scene)."),
)

_MAX_FILE_BYTES = 25 * 1024 * 1024
_MAX_DEPTH = 6
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
_SESSION = re.compile(r"session[\s_\-]*0*(\d{1,3})(?!\d)", re.I)
_CUE_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})")
_MARKER = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*$")
_INLINE_TS = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\b")
_ENDED = re.compile(r"transcription ended after\s+(\d{1,2}):(\d{2}):(\d{2})", re.I)
_FILE_DATE = re.compile(r"(20\d\d)[_\-](\d\d)[_\-](\d\d)")
_TEXT_DATE = re.compile(r"^\s*([A-Z][a-z]{2,8}\.? \d{1,2}, 20\d\d)\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_XFORM = re.compile(r"\b(?:x|ex)[\s\-]?form(s?)\b", re.I)
_GROUND = re.compile(r"\bground[\s\-]?plane(s?)\b", re.I)
_SEPARATOR = re.compile(r"^\s*_{4,}\s*$")
_BOILERPLATE = re.compile(
    r"^\s*(?:please rate the new|you should review gemini|how is the quality of these|this editable transcript was computer"
    r"|attachments\b|meeting records\b|transcription ended after)",
    re.I,
)
_GEMINI_PARTS = {"summary": "summary", "details": "details", "suggested next steps": "next_steps"}


class Cue(NamedTuple):
    start: float
    end: float
    text: str


class SearchHits(list):
    """A list of hit dicts that also carries ``timeline_note`` (set when subtitle and Gemini hits are both present)."""

    def __init__(self, hits: Iterable[dict[str, Any]] = (), timeline_note: str | None = None) -> None:
        super().__init__(hits)
        self.timeline_note = timeline_note

    def as_dict(self) -> dict[str, Any]:
        return {"results": list(self), "timeline_note": self.timeline_note}


# --- parsing ------------------------------------------------------------------------------------------------------------


def _seconds(h: str, m: str, s: str, ms: str = "0") -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def format_timestamp(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def parse_srt(text: str) -> list[Cue]:
    """Parse SubRip text into cues; tolerant of BOMs, CRLF, missing index lines and blank cues."""
    cues: list[Cue] = []
    for block in re.split(r"\n[ \t]*\n", text.lstrip("﻿").replace("\r\n", "\n")):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            match = _CUE_TIME.search(line)
            if match:
                g = match.groups()
                body = " ".join(part.strip() for part in lines[i + 1:] if part.strip())
                body = re.sub(r"<[^>]+>", "", body).strip()
                if body:
                    cues.append(Cue(_seconds(*g[:4]), _seconds(*g[4:]), body))
                break
    return cues


def _greedy(units: list[str], start: int, can_join, limit: int) -> tuple[int, int]:
    """Indices [start, end] of the longest run from ``start`` whose joined text fits ``limit``."""
    size = len(units[start])
    end = start
    while end + 1 < len(units) and can_join(end, end + 1) and size + 1 + len(units[end + 1]) <= limit:
        end += 1
        size += 1 + len(units[end])
    return start, end


def _windows(units: list[str], can_join=None, limit: int = PASSAGE_CHARS, overlap: bool = True) -> list[tuple[int, int]]:
    """Non-overlapping greedy passages, plus (with ``overlap``) passages starting mid-way through each one."""
    if not units:
        return []
    can_join = can_join or (lambda _a, _b: True)
    base: list[tuple[int, int]] = []
    i = 0
    while i < len(units):
        lo, hi = _greedy(units, i, can_join, limit)
        base.append((lo, hi))
        i = hi + 1
    out = list(base)
    if overlap:
        seen = set(base)
        for lo, hi in base:
            if hi > lo:
                extra = _greedy(units, lo + (hi - lo + 1) // 2, can_join, limit)
                if extra not in seen and extra[1] > hi:
                    seen.add(extra)
                    out.append(extra)
    return out


def merge_cues(cues: list[Cue], limit: int = PASSAGE_CHARS, max_gap: float = MAX_CUE_GAP) -> list[dict[str, Any]]:
    """Merge neighbouring cues into readable, non-overlapping passages of at most ``limit`` characters.

    A passage stops at the size limit or where the silence between two cues exceeds ``max_gap`` seconds.
    """
    texts = [cue.text for cue in cues]
    joinable = lambda a, b: cues[b].start - cues[a].end <= max_gap  # noqa: E731
    return [_cue_passage(cues, lo, hi, limit) for lo, hi in _windows(texts, joinable, limit, overlap=False)]


def _cue_passage(cues: list[Cue], lo: int, hi: int, limit: int) -> dict[str, Any]:
    return {
        "first": lo,
        "last": hi,
        "start": cues[lo].start,
        "end": cues[hi].end,
        "timestamp": format_timestamp(cues[lo].start),
        "end_timestamp": format_timestamp(cues[hi].end),
        "text": _trim(" ".join(cue.text for cue in cues[lo:hi + 1]), limit),
    }


def _split_long(text: str, limit: int = PASSAGE_CHARS) -> list[str]:
    """Split text longer than ``limit`` at sentence ends (hard cuts only for run-on sentences)."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    pieces: list[str] = []
    for sentence in _SENTENCE.split(text):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            pieces.append(sentence)
    return [" ".join(pieces[lo:hi + 1]) for lo, hi in _windows(pieces, limit=limit, overlap=False)]


def parse_gemini(text: str) -> dict[str, Any]:
    """Split a Gemini notes export into note blocks and timestamped transcript blocks.

    Returns ``{"date", "duration", "notes": [{"part", "text", "timestamp"}], "transcript": [{"start", "timestamp", "lines"}]}``.
    ``timestamp`` on a note block is the first inline Gemini reference such as ``(00:12:34)``, if any.
    """
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    date = duration = None
    for line in lines:
        ended = _ENDED.search(line)
        if ended:
            duration = _seconds(*ended.groups())
        if date is None and _TEXT_DATE.match(line):
            raw = _TEXT_DATE.match(line).group(1).replace(".", "")
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    date = datetime.strptime(raw, fmt).date().isoformat()
                    break
                except ValueError:
                    continue
    first_marker = next((i for i, line in enumerate(lines) if _MARKER.match(line)), len(lines))

    def keep(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and not (
            _BOILERPLATE.match(stripped) or _SEPARATOR.match(stripped) or _TEXT_DATE.match(stripped)
            or stripped.endswith(" - Transcript")
        )

    # Notes: blocks separated by blank lines; "Summary"/"Details"/"Suggested next steps" switch the part.
    notes: list[dict[str, Any]] = []
    part = "quick_notes"
    block: list[str] = []

    def flush_block() -> None:
        body = re.sub(r"\s+", " ", " ".join(line.strip() for line in block)).strip()
        block.clear()
        if len(body) < 12:
            return
        for piece in _split_long(body):
            inline = _INLINE_TS.search(piece)
            notes.append({"part": part, "text": piece, "timestamp": format_timestamp(_seconds(*inline.groups())) if inline else None})

    for line in lines[:first_marker]:
        stripped = line.strip()
        if stripped.lower() in _GEMINI_PARTS:
            flush_block()
            part = _GEMINI_PARTS[stripped.lower()]
            continue
        if not stripped or _SEPARATOR.match(stripped):
            flush_block()
        elif keep(line):
            block.append(line)
    flush_block()
    # The meeting title repeated at the top is not a note.
    notes = [note for note in notes if " " in note["text"] or len(note["text"]) > 40]

    transcript: list[dict[str, Any]] = []
    for line in lines[first_marker:]:
        marker = _MARKER.match(line)
        if marker:
            start = _seconds(*marker.groups())
            transcript.append({"start": start, "timestamp": format_timestamp(start), "lines": []})
        elif transcript and keep(line):
            transcript[-1]["lines"].append(re.sub(r"\s+", " ", line.strip()))
    transcript = [entry for entry in transcript if entry["lines"]]
    if duration is None and transcript:
        duration = transcript[-1]["start"]
    return {"date": date, "duration": duration, "notes": notes, "transcript": transcript}


# --- discovery ----------------------------------------------------------------------------------------------------------


def default_roots() -> list[Path]:
    """Class recordings folder (``OMNIVERSE_CLASS_NOTES_ROOT`` overrides; ``os.pathsep`` separates several) + mentorship notes."""
    configured = os.environ.get(ENV_ROOT, "").strip()
    if configured:
        classes = [Path(part) for part in configured.split(os.pathsep) if part.strip()]
    else:
        # Downloads itself is read flat (session-named files only): Sessions 3-6 were saved straight there.
        classes = [Path.home().joinpath(*DEFAULT_CLASSES_DIR), Path.home() / FLAT_ROOT_NAME]
    return [*classes, workspace_root() / "isaacsim-mentorship"]


def _session_number(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    match = _SESSION.search(text)
    return int(match.group(1)) if match else None


def _session_of(path: Path, root: Path) -> int | None:
    for parent in path.parents:
        if parent == root.parent:
            break
        match = _SESSION.match(parent.name)
        if match:
            return int(match.group(1))
    match = _SESSION.search(path.name)
    return int(match.group(1)) if match else None


def _canon(text: str) -> str:
    """Spellings the transcripts use for the same thing ('x form', 'exforms', 'groundplane')."""
    return _GROUND.sub(r"ground plane\1", _XFORM.sub(r"Xform\1", text))


def _is_gemini(path: Path) -> bool:
    if "gemini" in path.name.lower():
        return True
    try:
        with path.open("rb") as handle:
            return b"gemini" in handle.read(4096).lower()
    except OSError:
        return False


# --- index --------------------------------------------------------------------------------------------------------------


class ClassNotes:
    """Lazy BM25 index over the teacher's session subtitles, Gemini notes and the user's revision notes."""

    def __init__(self, roots: Iterable[str | Path] | None = None) -> None:
        self._explicit_roots = None if roots is None else [Path(root) for root in roots]
        self._lock = threading.Lock()
        self._index: _BM25Index | None = None
        self._sessions: dict[int, dict[str, Any]] = {}
        self._files: list[dict[str, Any]] = []
        self._skipped: list[dict[str, str]] = []
        self._index_ms = 0.0

    @property
    def roots(self) -> list[Path]:
        return list(self._explicit_roots) if self._explicit_roots is not None else default_roots()

    def refresh(self) -> dict[str, Any]:
        with self._lock:
            self._index = None
        return self.stats()

    # --- building -------------------------------------------------------------------------------------------------

    def _ensure(self) -> _BM25Index:
        if self._index is not None:
            return self._index
        with self._lock:
            if self._index is None:
                self._build()
        assert self._index is not None
        return self._index

    def _build(self) -> None:
        began = time.perf_counter()
        index = _BM25Index()
        self._sessions, self._files, self._skipped = {}, [], []
        cache: dict[str, tuple[str, ...]] = {}
        seen: set[Path] = set()
        for root in self.roots:
            if not root.is_dir():
                continue
            for path, kind in self._discover(root):
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                session = _session_of(path, root)
                try:
                    if path.stat().st_size > _MAX_FILE_BYTES:
                        self._skip(path, "larger than 25 MB")
                        continue
                    if kind == "subtitles":
                        docs = self._srt_docs(path, session)
                    elif kind == "gemini_notes":
                        docs = self._gemini_docs(path, session)
                    else:
                        docs = self._revision_docs(path, session)
                except (OSError, UnicodeError) as exc:
                    self._skip(path, f"unreadable: {type(exc).__name__}")
                    continue
                for doc in docs:
                    index.add(doc, _tokenize(_canon(f"{doc.get('section') or ''} {doc['text']}"), cache))
                self._files.append({"source": kind, "file": path.name, "session": docs[0]["session"] if docs else session,
                                    "passages": len(docs)})
        index.finish()
        self._index = index
        self._index_ms = round((time.perf_counter() - began) * 1000, 1)

    def _skip(self, path: Path, reason: str) -> None:
        self._skipped.append({"file": path.name, "reason": reason})
        session = _session_number(path.parent.name) or _session_number(path.name)
        if session is not None:
            self._session_entry(session)["skipped"].append(path.name)

    def _session_entry(self, session: int) -> dict[str, Any]:
        return self._sessions.setdefault(
            session, {"session": session, "label": f"Session {session}", "sources": [], "files": [], "duration": {},
                      "date": None, "skipped": []},
        )

    def _register(self, session: int | None, source: str, path: Path, passages: int) -> None:
        if session is None:
            return
        entry = self._session_entry(session)
        if source not in entry["sources"]:
            entry["sources"].append(source)
            entry["sources"].sort(key=SOURCES.index)
        entry["files"].append({"source": source, "file": path.name, "passages": passages})

    def _discover(self, root: Path) -> Iterable[tuple[Path, str]]:
        if root.name == FLAT_ROOT_NAME:
            yield from self._discover_flat(root)
            return
        root_depth = len(root.parts)
        for current, dirs, files in os.walk(root):
            here = Path(current)
            depth = len(here.parts) - root_depth
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")) if depth < _MAX_DEPTH else []
            if here != root and _SESSION.match(here.name):
                self._session_entry(int(_SESSION.match(here.name).group(1)))
            for name in sorted(files):
                path = here / name
                suffix = path.suffix.lower()
                if name in REVISION_FILES:
                    yield path, "revision_notes"
                elif suffix == ".srt":
                    yield path, "subtitles"
                elif suffix == ".txt" and _session_of(path, root) is not None:
                    if _is_gemini(path):
                        yield path, "gemini_notes"
                    else:
                        self._skip(path, "not a Gemini notes export")
                elif suffix == ".pdf" and _session_of(path, root) is not None:
                    self._skip(path, "PDF not indexed (no stdlib text extraction); the .txt export is used")

    def _discover_flat(self, root: Path) -> Iterable[tuple[Path, str]]:
        """Top-level files of a busy folder (Downloads): only session-named .srt and Gemini .txt, no duplicates."""
        try:
            names = sorted(entry.name for entry in os.scandir(root) if entry.is_file())
        except OSError:
            return
        for name in names:
            path = root / name
            if _SESSION.search(name) is None or _COPY_SUFFIX.search(path.stem):
                continue
            suffix = path.suffix.lower()
            if suffix == ".srt":
                yield path, "subtitles"
            elif suffix == ".txt" and _is_gemini(path):
                yield path, "gemini_notes"

    def _doc(self, session, source, path, part, lo, hi, text, timestamp=None, end=None, section=None) -> dict[str, Any]:
        return {
            "session": session,
            "source": source,
            "timeline": TIMELINES[source],
            "timestamp": timestamp,
            "end_timestamp": end,
            "text": text,
            "file": path.name,
            "section": section,
            "_span": (str(path), part, lo, hi),
            "_phrase": _canon(text).lower(),
        }

    def _srt_docs(self, path: Path, session: int | None) -> list[dict[str, Any]]:
        cues = parse_srt(_read(path))
        texts = [cue.text for cue in cues]
        joinable = lambda a, b: cues[b].start - cues[a].end <= MAX_CUE_GAP  # noqa: E731
        docs = []
        for lo, hi in _windows(texts, joinable):
            passage = _cue_passage(cues, lo, hi, PASSAGE_CHARS)
            docs.append(self._doc(session, "subtitles", path, "cues", lo, hi, passage["text"], passage["timestamp"],
                                  passage["end_timestamp"]))
        self._register(session, "subtitles", path, len(docs))
        if session is not None and cues:
            self._session_entry(session)["duration"]["subtitles"] = format_timestamp(cues[-1].end)
        return docs

    def _gemini_docs(self, path: Path, session: int | None) -> list[dict[str, Any]]:
        parsed = parse_gemini(_read(path))
        docs = []
        for i, note in enumerate(parsed["notes"]):
            docs.append(self._doc(session, "gemini_notes", path, "notes", i, i, note["text"], note["timestamp"],
                                  section=note["part"]))
        offset = 0
        for block in parsed["transcript"]:
            for lo, hi in _windows([_trim(line, PASSAGE_CHARS) for line in block["lines"]]):
                text = _trim(" ".join(block["lines"][lo:hi + 1]), PASSAGE_CHARS)
                docs.append(self._doc(session, "gemini_notes", path, "transcript", offset + lo, offset + hi, text,
                                      block["timestamp"], section="transcript"))
            offset += len(block["lines"])
        self._register(session, "gemini_notes", path, len(docs))
        if session is not None:
            entry = self._session_entry(session)
            if parsed["duration"] is not None:
                entry["duration"]["gemini_notes"] = format_timestamp(parsed["duration"])
            file_date = _FILE_DATE.search(path.name)
            date = "-".join(file_date.groups()) if file_date else parsed["date"]
            if date and not entry["date"]:
                entry["date"] = date
        return docs

    def _revision_docs(self, path: Path, session: int | None) -> list[dict[str, Any]]:
        docs = []
        chunk = 0
        for title, _line, raw in _markdown_sections(path, chunk_chars=600):
            doc_session = session if session is not None else _session_number(title)
            for piece in _split_long(_flat(raw)):
                docs.append(self._doc(doc_session, "revision_notes", path, "md", chunk, chunk, piece, section=title))
                chunk += 1
        for doc_session in sorted({doc["session"] for doc in docs if doc["session"] is not None}):
            self._register(doc_session, "revision_notes", path, sum(1 for doc in docs if doc["session"] == doc_session))
        return docs

    # --- public API -----------------------------------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        index = self._ensure()
        counts = Counter(entry["source"] for entry in self._files)
        return {
            "roots": [str(root) for root in self.roots],
            "files": {source: counts.get(source, 0) for source in SOURCES},
            "passages": len(index.docs),
            "sessions": len(self._sessions),
            "skipped": list(self._skipped),
            "index_ms": self._index_ms,
        }

    def sessions(self) -> list[dict[str, Any]]:
        self._ensure()
        out = []
        for number in sorted(self._sessions):
            entry = self._sessions[number]
            out.append({**entry, "sources": list(entry["sources"]), "files": [dict(f) for f in entry["files"]],
                        "duration": dict(entry["duration"]), "skipped": list(entry["skipped"])})
        return out

    def search(self, query: str, top_k: int = 5, session: Any = None) -> SearchHits:
        index = self._ensure()
        query = (query or "").strip()
        terms = _tokenize(_canon(query))
        if not terms:
            return SearchHits()
        top_k = max(1, min(int(top_k or 5), 50))
        wanted = _session_number(session) if session is not None else None
        words = re.findall(r"[a-z0-9]+", _canon(query).lower())
        phrase = " ".join(words) if len(words) > 1 else None
        scored = []
        for doc_id, score in index.scores(terms).items():
            doc = index.docs[doc_id]
            if session is not None and doc["session"] != wanted:
                continue
            if phrase and phrase in re.sub(r"[^a-z0-9]+", " ", doc["_phrase"]):
                score *= 1.3
            scored.append((score, doc_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen: list[tuple[float, dict[str, Any]]] = []
        for score, doc_id in scored:
            doc = index.docs[doc_id]
            path, part, lo, hi = doc["_span"]
            if any(o["_span"][0] == path and o["_span"][1] == part and lo <= o["_span"][3] and o["_span"][2] <= hi
                   for _s, o in chosen):
                continue
            chosen.append((score, doc))
            if len(chosen) >= top_k:
                break
        hits = [self._public(doc, score) for score, doc in chosen]
        present = {hit["source"] for hit in hits}
        note = TIMELINE_NOTE if {"subtitles", "gemini_notes"} <= present else None
        return SearchHits(hits, note)

    @staticmethod
    def _public(doc: dict[str, Any], score: float) -> dict[str, Any]:
        hit = {key: value for key, value in doc.items() if not key.startswith("_")}
        hit["score"] = round(score, 3)
        caveats = [text for sess, source, lo, hi, words, text in KNOWN_SRT_ISSUES
                   if doc["session"] == sess and doc["source"] == source and doc["timestamp"]
                   and _in_window(doc["timestamp"], doc["end_timestamp"], lo, hi) and any(w in doc["_phrase"] for w in words)]
        if caveats:
            hit["caveat"] = " ".join(caveats)
        return hit

    def ask(self, question: str, top_k: int = 6, session: Any = None) -> dict[str, Any]:
        question = (question or "").strip()
        if not question:
            return {"ok": False, "error": "Ask a question, e.g. 'how do I add a ground plane'.", "answer_passages": [],
                    "summary_hint": None, "note": None}
        hits = self.search(question, top_k=top_k, session=session)
        if not self._files:
            return {"ok": False, "error": "No class sources found under: " + "; ".join(str(r) for r in self.roots),
                    "answer_passages": [], "summary_hint": None, "note": None, "index_ms": self._index_ms}
        groups: dict[Any, dict[str, Any]] = {}
        for hit in hits:
            key = hit["session"]
            group = groups.setdefault(key, {"session": key, "label": f"Session {key}" if key is not None else "Course-wide notes",
                                            "passages": []})
            group["passages"].append(hit)
        notes = [hits.timeline_note] if hits.timeline_note else []
        if any(hit["source"] == "subtitles" for hit in hits):
            notes.append(SRT_CAVEAT)
        if any(entry["reason"].startswith("PDF") for entry in self._skipped):
            notes.append(PDF_NOTE)
        return {
            "ok": True,
            "question": question,
            "answer_passages": list(groups.values()),
            "summary_hint": _hint(hits[0]) if hits else "Nothing in the indexed class sources matched; try other words.",
            "note": " ".join(notes) or None,
            "index_ms": self._index_ms,
        }


def _in_window(start: str, end: str | None, lo: float, hi: float | None) -> bool:
    """True when the passage [start, end] overlaps the window [lo, hi] (``hi=None``: open-ended)."""
    first = _seconds(*start.split(":"))
    last = _seconds(*end.split(":")) if end else first
    return last >= lo and (hi is None or first <= hi)


def _hint(hit: dict[str, Any]) -> str:
    where = f"Session {hit['session']}" if hit["session"] is not None else "the course-wide notes"
    if hit["source"] == "subtitles":
        return f"Start with {where}'s subtitles at {hit['timestamp']} (compressed-recording timeline)."
    if hit["source"] == "gemini_notes":
        part = "transcript" if hit["section"] == "transcript" else f"notes ({hit['section'].replace('_', ' ')})"
        at = f" at {hit['timestamp']} (Gemini timeline)" if hit["timestamp"] else ""
        return f"Start with {where}'s Gemini {part}{at}."
    return f"Start with your revision notes for {where}, section '{hit['section']}'."
