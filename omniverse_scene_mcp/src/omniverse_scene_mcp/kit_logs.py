"""Read-only watcher for the Kit log files Isaac Sim and USD Composer write while they run.

Where the logs are: Kit writes one log per session to
``~/.nvidia-omniverse/logs/Kit/<app name>/<app version>/kit_<YYYYMMDD>_<HHMMSS>.log`` (``Isaac-Sim Full/6.1`` and
``My USD Composer/0.1`` on this laptop; neither app's ``.kit`` file overrides ``/log/file``). ``%LOCALAPPDATA%/ov/logs``
is checked too in case a Kit build uses it. ``OMNIVERSE_KIT_LOG_DIRS`` (a ``os.pathsep`` list) replaces the defaults.

A log line looks like ``2026-10-22T13:55:56Z [2,583ms] [Warning] [omni.rtx] message``. Lines without that prefix
continue the previous entry (multi-line messages, tracebacks). Python output arrives as one ``[py stderr]:`` line per
traceback line; those are glued back into one entry so a traceback reads as one error.

Public API (all read-only; nothing is ever written, executed or sent anywhere):

* ``log_dirs()`` - the folders that are searched.
* ``find_logs(app=None)`` - session logs, newest first.
* ``tail(app, lines=200, *, level=None, since=None)`` - the last parsed entries of the newest log.
* ``new_errors(app, *, cursor=None)`` - entries written since ``cursor``; poll it with the returned cursor.
* ``explain_recent(app, *, limit=5, knowledge=None)`` - recent distinct errors, de-duplicated and explained.
* ``summary(app)`` - counts by level / extension for the current session log.

Safety: only ``.log`` files under the log folders are opened; at most ``MAX_READ_BYTES`` are read from the end of a
file; secrets (tokens, keys, passwords) and the user's home folder (``C:/Users/<name>`` -> ``~``) are redacted from
every returned string. Problems such as "no log found" raise ``KitLogError`` (a ``ValueError``) with a hint.
"""

from __future__ import annotations

import base64
import functools
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable

ENV_VAR = "OMNIVERSE_KIT_LOG_DIRS"
MAX_READ_BYTES = 5 * 1024 * 1024  # never read more than this from one file in one call
ACTIVE_MINUTES = 10  # a log modified this recently belongs to an app that is probably still running
QUIET_SECONDS = 5.0  # a log untouched this long is complete: a half-written traceback at its end is returned
MAX_LINE_CHARS = 4_000
MAX_MESSAGE_CHARS = 12_000
MAX_ENTRY_LINES = 300  # continuation lines kept per entry
MAX_TAIL_LINES = 5_000
MAX_POLL_ENTRIES = 200
MAX_LOG_FILES = 2_000
_FIRST_WINDOW = 64 * 1024
_LINE_COUNT_LIMIT = 8 * 1024 * 1024  # count newlines before a window only when the prefix is this small
_HEAD_BYTES = 256  # file identity for cursors: a hash of the first bytes (Kit's first line has the start time)
_CURSOR_PREFIX = "kitlog1."

LEVELS = ("verbose", "info", "warning", "error", "fatal")
_RANK = {name: i for i, name in enumerate(LEVELS)}
_RANK["debug"] = 0

NOISE_PATTERNS: list[dict[str, str]] = [
    {"id": "missing_extension_toml", "pattern": r"Extensions config 'extension\.toml' doesn't exist",
     "reason": "Kit checks cached extensions for an optional config file; nothing is broken."},
    {"id": "non_nvidia_gpu", "pattern": r"Skipping unsupported non-NVIDIA GPU",
     "reason": "The laptop's built-in Intel GPU is ignored; RTX rendering uses the NVIDIA GPU."},
    {"id": "pcie_link", "pattern": r"PCIe link (?:width|generation) current .* don't match",
     "reason": "Laptop GPUs lower their PCIe link to save power; this does not affect results."},
    {"id": "deprecation", "pattern": r"\bis deprecated\b|\[DEPRECATED\]|DeprecationWarning",
     "reason": "An extension uses an older API that still works; only the extension author needs to act."},
    {"id": "window_layout", "source": r"^omni\.kit\.(?:window|widget|mainwindow|quicklayout)",
     "pattern": r"layout|dock|workspace",
     "reason": "A window-layout note from the UI; windows are still created normally."},
    {"id": "dlss_resolution", "pattern": r"DLSS increasing input dimensions",
     "reason": "The viewport is small, so DLSS renders at its minimum size; visual only."},
    {"id": "usd_diagnostics_muted", "pattern": r"USD Diagnostics are currently muted",
     "reason": "Kit hides low-level USD warnings by default; this line only says so."},
    {"id": "viewport_camera_saved", "pattern": r"Updated /OmniverseKit_(?:Persp|Top|Front|Right)\b",
     "reason": "The built-in viewport cameras were saved into the stage; expected after moving the view."},
    {"id": "snippet_folder", "pattern": r"Snippet folder doesn't exist",
     "reason": "The Script Editor's optional shared-snippets folder has not been created."},
    {"id": "material_config", "pattern": r"No material configuration file",
     "reason": "Replicator falls back to its default material list."},
    {"id": "mdl_cache_warming", "pattern": r"mdl_list_cache is not complete",
     "reason": "The material list is still being built in the background at start-up."},
    {"id": "offline_hub_cache", "pattern": r"Unable to detect Omniverse Cache Server|Hub version manifest",
     "reason": "The optional Omniverse Hub/cache service is not running; local work is unaffected."},
    {"id": "duplicate_pxr_build", "pattern": r"A different OpenUSD \(pxr\) build was already imported",
     "reason": "An asset converter loads its own USD copy; Kit keeps using its own."},
]

_HEADER = re.compile(
    r"^(?P<time>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?Z?)\s+"
    r"(?:\[(?P<elapsed>[\d,.]+\s*m?s)\]\s+)?"
    r"\[(?P<level>Verbose|Debug|Info|Warning|Error|Fatal)\]\s?"
    r"(?:\[(?P<source>[^\]\n]{1,200})\]\s?)?"
    r"(?P<message>.*)$",
    re.I,
)
_EXT_TAG = re.compile(r"^\[ext:\s*(?P<ext>[A-Za-z_][\w.]*?)(?:-\d[^\]\s]*)?\]\s?")
_STREAM = re.compile(r"^\[py (?P<stream>stderr|stdout)\]:?\s?", re.I)
_TRACEBACK = re.compile(r"^Traceback \(most recent call last\)")
_CHAIN = ("During handling of the above exception", "The above exception was the direct cause")
_CARET = re.compile(r"^[\s^~]+$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufeff]")
_PRIVATE_PART = re.compile(r"^(?:_.*|impl|scripts|extension|internal)$")
_DIGITS = re.compile(r"\d+")
_DETAILS = re.compile(r"\[[^\]\n]{0,400}\]|'[^'\n]{0,400}'|\"[^\"\n]{0,400}\"|\([^()\n]{0,400}\)")
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.I)

_SECRET_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\beyJ[\w-]{6,}\.[\w-]{6,}\.[\w-]{6,}"), "***"),  # JWT
    (re.compile(r"\b(?:nvapi|sk|ghp|gho|ghs|github_pat|xox[abpr])[-_][A-Za-z0-9_-]{10,}"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
    (re.compile(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"(?i)(://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)([?&](?:access_token|token|key|api_key|apikey|sig|signature|secret|password|"
                r"x-amz-signature|x-amz-credential|x-amz-security-token)=)[^&\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|auth[_-]?token|"
                r"access[_-]?token|refresh[_-]?token|token|secret|password|passwd|pwd)\b['\"]?\s*[:=]\s*['\"]?)"
                r"(?!\*\*\*)[^\s'\",;&]+"), r"\1***"),
]
_GENERIC_HOME = re.compile(r"(?i)\b[a-z]:[\\/]+users[\\/]+(?!public\b|default\b)[^\\/\s'\"<>|:*?]+")
_POSIX_HOME = re.compile(r"(?<![\w.:])/(?:home|Users)/[^/\s'\"]+")


class KitLogError(ValueError):
    """A log could not be found or opened (the message says why and what to do)."""


# --- redaction ---------------------------------------------------------------------------------------------------------

def _home_pattern() -> re.Pattern[str] | None:
    return _home_pattern_for(os.environ.get("USERPROFILE"), os.environ.get("HOME"))


@functools.lru_cache(maxsize=8)
def _home_pattern_for(_userprofile: str | None, _home: str | None) -> re.Pattern[str] | None:
    home = os.path.expanduser("~")
    if not home or home == "~" or len(home) < 4:
        return None
    parts = [re.escape(p) for p in re.split(r"[\\/]+", home) if p]
    if not parts:
        return None
    prefix = r"[\\/]+" if home[:1] in "\\/" else ""
    return re.compile(prefix + r"[\\/]+".join(parts) + r"(?![\w.-])", re.I)


def redact(text: Any) -> Any:
    """Hide secrets and the user's home folder in ``text`` (non-strings are returned unchanged)."""
    if not isinstance(text, str) or not text:
        return text
    for pattern, replacement in _SECRET_RULES:
        text = pattern.sub(replacement, text)
    home = _home_pattern()
    if home is not None:
        text = home.sub("~", text)
    text = _GENERIC_HOME.sub("~", text)
    return _POSIX_HOME.sub("~", text)


def _display(path: Path | str) -> str:
    return redact(str(path).replace("\\", "/"))


# --- log folders and files -----------------------------------------------------------------------------------------------

def _roots(dirs: Iterable[str | os.PathLike[str]] | None = None) -> list[Path]:
    if dirs is not None:
        raw = [str(d) for d in dirs]
    elif os.environ.get(ENV_VAR, "").strip():
        raw = [p for p in os.environ[ENV_VAR].split(os.pathsep) if p.strip()]
    else:
        raw = [os.path.join("~", ".nvidia-omniverse", "logs", "Kit")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            raw += [os.path.join(local, "ov", "logs", "Kit"), os.path.join(local, "ov", "logs")]
    roots: list[Path] = []
    for item in raw:
        try:
            path = Path(os.path.realpath(os.path.expanduser(item.strip().strip('"'))))
        except (OSError, ValueError):
            continue
        if path not in roots:
            roots.append(path)
    return roots


def log_dirs(*, dirs: Iterable[str | os.PathLike[str]] | None = None) -> list[dict[str, Any]]:
    """The folders searched for Kit logs (home redacted) and whether each exists."""
    return [{"path": _display(root), "exists": root.is_dir(),
             "source": "argument" if dirs is not None else ("env" if os.environ.get(ENV_VAR, "").strip() else "default")}
            for root in _roots(dirs)]


def app_key(name: str) -> str:
    """'isaac' / 'composer' for the two main apps, else the app folder name unchanged."""
    low = name.lower()
    if "isaac" in low and "compat" not in low and "checker" not in low:
        return "isaac"
    if "composer" in low:
        return "composer"
    return name


def _scan_root(root: Path) -> list[tuple[Path, str, str | None]]:
    """(file, app folder, version folder) for every .log at depth 1-3 under ``root``."""
    found: list[tuple[Path, str, str | None]] = []

    def walk(folder: Path, depth: int, app: str | None, version: str | None) -> None:
        if depth > 3 or len(found) >= MAX_LOG_FILES:
            return
        try:
            items = list(os.scandir(folder))
        except OSError:
            return
        for item in items:
            try:
                if item.is_file(follow_symlinks=False) and item.name.lower().endswith(".log"):
                    found.append((Path(item.path), app or root.name, version))
                elif item.is_dir(follow_symlinks=False):
                    if depth == 1:
                        walk(Path(item.path), depth + 1, item.name, None)
                    elif depth == 2:
                        walk(Path(item.path), depth + 1, app, item.name)
            except OSError:
                continue

    walk(root, 1, None, None)
    return found


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _all_logs(dirs: Iterable[str | os.PathLike[str]] | None, active_minutes: float) -> list[dict[str, Any]]:
    now = time.time()
    logs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in _roots(dirs):
        if not root.is_dir():
            continue
        for path, app_name, version in _scan_root(root):
            key = os.path.normcase(str(path))
            if key in seen:
                continue
            seen.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            logs.append({
                "app": app_key(app_name),
                "app_name": app_name,
                "version": version,
                "name": path.name,
                "path": _display(path),
                "size": st.st_size,
                "modified": _iso(st.st_mtime),
                "age_seconds": max(0, int(now - st.st_mtime)),
                "active": (now - st.st_mtime) <= active_minutes * 60,
                "_path": path,
                "_mtime": st.st_mtime,
            })
    logs.sort(key=lambda item: (item["_mtime"], item["name"]), reverse=True)
    return logs


def _matches_app(log: dict[str, Any], app: str) -> bool:
    want = app.strip().lower()
    return want in (log["app"].lower(), log["app_name"].lower()) or app_key(app.strip()).lower() == log["app"].lower()


def find_logs(app: str | None = None, *, dirs: Iterable[str | os.PathLike[str]] | None = None,
              active_minutes: float = ACTIVE_MINUTES) -> list[dict[str, Any]]:
    """Kit session logs, newest first: ``[{app, app_name, version, name, path, size, modified, age_seconds, active}]``.

    ``app`` filters by 'isaac', 'composer' or an app folder name. ``path`` has the home folder shown as ``~``;
    it can be passed back to ``tail``/``new_errors``. ``active`` means modified within ``active_minutes``.
    """
    logs = _all_logs(dirs, active_minutes)
    if app:
        logs = [log for log in logs if _matches_app(log, app)]
    return [{k: v for k, v in log.items() if not k.startswith("_")} for log in logs]


def _looks_like_path(app: str) -> bool:
    return any(ch in app for ch in "/\\") or app.lower().endswith(".log")


def _resolve(app: str | os.PathLike[str] | None, dirs: Iterable[str | os.PathLike[str]] | None) -> tuple[Path, str]:
    """(real path, app label) of the log to read; only files under the log folders are allowed."""
    roots = _roots(dirs)
    target = "isaac" if app is None else str(app).strip()
    if not target:
        target = "isaac"
    if _looks_like_path(target):
        try:
            path = Path(os.path.realpath(os.path.expanduser(target)))
        except (OSError, ValueError) as exc:
            raise KitLogError(f"Not a usable log path: {_display(target)}") from exc
        inside = False
        for root in roots:
            try:
                inside = os.path.commonpath([os.path.normcase(str(root)), os.path.normcase(str(path))]) == \
                    os.path.normcase(str(root))
            except ValueError:
                inside = False
            if inside:
                break
        if not inside:
            raise KitLogError(
                f"{_display(target)} is not inside the Kit log folders ({', '.join(_display(r) for r in roots)}). "
                "Only Kit logs can be read; use kit_logs() to read the newest one.")
        if path.suffix.lower() != ".log" or not path.is_file():
            raise KitLogError(f"No .log file at {_display(path)}. Use kit_logs() to read the newest Kit log.")
        label = "log"
        for root in roots:
            try:
                rel = path.relative_to(root)
                label = app_key(rel.parts[0]) if len(rel.parts) > 1 else root.name
                break
            except ValueError:
                continue
        return path, label
    for log in _all_logs(dirs, ACTIVE_MINUTES):
        if _matches_app(log, target):
            return log["_path"], log["app"]
    pretty = {"isaac": "Isaac Sim", "composer": "USD Composer"}.get(app_key(target), target)
    raise KitLogError(
        f"No {pretty} log found in {', '.join(_display(r) for r in roots) or 'the Kit log folders'}. "
        f"Start {pretty} once so Kit writes a log, or set {ENV_VAR} to the folder that holds kit_*.log files.")


# --- parsing ------------------------------------------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    raw = raw[: MAX_LINE_CHARS * 4]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1252")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", "replace")
    text = _CONTROL.sub("", _ANSI.sub("", text.rstrip("\r\n")))
    return text[:MAX_LINE_CHARS]


def _extension_of(source: str | None) -> str | None:
    if not source:
        return None
    parts = source.split(".")
    keep: list[str] = []
    for part in parts:
        if keep and _PRIVATE_PART.match(part):
            break
        keep.append(part)
    return ".".join(keep) or source


class _Entry:
    __slots__ = ("time", "elapsed", "level", "source", "extension", "stream", "lines", "offset", "end", "line_no",
                 "tb", "dropped")

    def __init__(self, match: re.Match[str], message: str, ext: str | None, stream: str | None,
                 offset: int, end: int, line_no: int) -> None:
        self.time = match.group("time")
        self.elapsed = (match.group("elapsed") or "").replace(" ", "") or None
        level = match.group("level").lower()
        self.level = "verbose" if level == "debug" else level
        self.source = match.group("source")
        self.extension = ext or _extension_of(self.source)
        self.stream = stream
        self.lines = [message]
        self.offset = offset
        self.end = end
        self.line_no = line_no
        self.tb: str | None = None  # traceback state for glued [py stderr] lines: frames / after / chain
        self.dropped = 0

    def add(self, text: str, end: int) -> None:
        self.end = end
        if len(self.lines) < MAX_ENTRY_LINES:
            self.lines.append(text)
        else:
            self.dropped += 1

    @property
    def message(self) -> str:
        lines = list(self.lines)
        while len(lines) > 1 and not lines[-1].strip():
            lines.pop()
        while len(lines) > 1 and not lines[0].strip():
            lines.pop(0)
        text = "\n".join(lines)
        if self.dropped:
            text += f"\n... ({self.dropped} more lines)"
        return text[:MAX_MESSAGE_CHARS]

    @property
    def open(self) -> bool:
        return self.tb in ("frames", "chain")


def _is_exception_line(text: str) -> bool:
    return bool(text.strip()) and not text[:1].isspace() and not _TRACEBACK.match(text) and not _CARET.match(text)


def _lines(data: bytes, base: int) -> Iterable[tuple[int, int, bytes]]:
    pos, size = 0, len(data)
    while pos < size:
        newline = data.find(b"\n", pos)
        end = size if newline < 0 else newline + 1
        yield base + pos, base + end, data[pos:end]
        pos = end


def _parse(data: bytes, base: int, first_line_no: int) -> list[_Entry]:
    entries: list[_Entry] = []
    current: _Entry | None = None
    line_no = first_line_no - 1
    for offset, end, raw in _lines(data, base):
        line_no += 1
        text = _decode(raw)
        match = _HEADER.match(text)
        if match is None:
            if current is not None:  # a continuation line (multi-line message or unprefixed traceback line)
                current.add(text, end)
            continue  # text before the first header belongs to an entry that started before this window
        rest = match.group("message")
        ext = None
        tag = _EXT_TAG.match(rest)
        if tag:
            ext, rest = tag.group("ext"), rest[tag.end():]
        stream_match = _STREAM.match(rest)
        stream = stream_match.group("stream").lower() if stream_match else None
        if stream_match:
            rest = rest[stream_match.end():]
        level = match.group("level").lower()
        if (stream and current is not None and current.tb and current.stream == stream
                and current.source == match.group("source") and current.level == level):
            state = current.tb
            if state == "frames":
                current.add(rest, end)
                if _is_exception_line(rest):
                    current.tb = "after"
                continue
            if state == "chain":
                current.add(rest, end)
                if _TRACEBACK.match(rest):
                    current.tb = "frames"
                continue
            if not rest.strip():  # state 'after': blank lines may lead to a chained traceback
                current.add(rest, end)
                continue
            if rest.startswith(_CHAIN):
                current.add(rest, end)
                current.tb = "chain"
                continue
        if stream and not rest.strip():
            if current is not None:
                current.end = end
            continue
        current = _Entry(match, rest, ext, stream, offset, end, line_no)
        if stream and _TRACEBACK.match(rest):
            current.tb = "frames"
        entries.append(current)
    return entries


def _noise_of(entry: _Entry) -> dict[str, str] | None:
    """The NOISE_PATTERNS rule a warning/error matches (checked on its first non-blank line), else None."""
    first = next((line for line in entry.lines if line.strip()), "")
    for rule in NOISE_PATTERNS:
        source_rule = rule.get("source")
        if source_rule and not re.search(source_rule, entry.source or "", re.I):
            continue
        if re.search(rule["pattern"], first, re.I):
            return rule
    return None


def _entry_dict(entry: _Entry, *, relative_lines: bool = False) -> dict[str, Any]:
    noise = _noise_of(entry)
    out = {
        "time": entry.time,
        "elapsed": entry.elapsed,
        "level": entry.level,
        "source": redact(entry.source),
        "extension": redact(entry.extension),
        "stream": entry.stream,
        "message": redact(entry.message),
        "raw_line_no": entry.line_no,
        "offset": entry.offset,
        "noise": noise["id"] if noise else None,
    }
    if relative_lines:
        out["raw_line_no_relative"] = True
    return out


# --- reading ------------------------------------------------------------------------------------------------------------

def _line_base(handle: Any, start: int) -> tuple[int, bool]:
    """(1-based line number of byte ``start``, exact?)."""
    if start <= 0:
        return 1, True
    if start > _LINE_COUNT_LIMIT:
        return 1, False
    handle.seek(0)
    count, left = 0, start
    while left > 0:
        block = handle.read(min(1024 * 1024, left))
        if not block:
            break
        count += block.count(b"\n")
        left -= len(block)
    return count + 1, True


def _read_tail(path: Path, want: int, cap: int) -> tuple[list[_Entry], int, int, bool]:
    """Parse the last ``want`` bytes (aligned to a line start): (entries, window start, file size, exact lines)."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        want = max(1, min(want, cap))
        start = max(0, size - want)
        handle.seek(start)
        data = handle.read(size - start)
        if start > 0:
            newline = data.find(b"\n")
            if newline >= 0:
                start += newline + 1
                data = data[newline + 1:]
            else:
                data = b""
                start = size
        elif data.startswith(b"\xef\xbb\xbf"):
            data, start = data[3:], 3
        line_no, exact = _line_base(handle, start)
    return _parse(data, start, line_no), start, size, exact


def _level_ok(entry: _Entry, level: str | None) -> bool:
    return level is None or _RANK.get(entry.level, 1) >= _RANK[level]


def _check_level(level: str | None) -> str | None:
    if level is None or level == "":
        return None
    name = str(level).strip().lower()
    if name == "warn":
        name = "warning"
    if name not in _RANK:
        raise ValueError(f"level must be one of {', '.join(LEVELS)} (got {level!r})")
    return name


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text.replace("Z", "+00:00").replace(",", ".").replace(" ", "T", 1))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _since(since: Any) -> datetime | None:
    if since is None or since == "":
        return None
    if isinstance(since, datetime):
        return since if since.tzinfo else since.replace(tzinfo=timezone.utc)
    if isinstance(since, (int, float)) and not isinstance(since, bool):
        return datetime.now(timezone.utc) - timedelta(seconds=float(since))
    value = _parse_time(str(since).strip())
    if value is None:
        raise ValueError("since must be an ISO time like '2026-10-22T13:55:54Z', a datetime, or seconds ago")
    return value


def tail(app: str | os.PathLike[str] = "isaac", lines: int = 200, *, level: str | None = None, since: Any = None,
         dirs: Iterable[str | os.PathLike[str]] | None = None, max_bytes: int = MAX_READ_BYTES) -> list[dict[str, Any]]:
    """The last ``lines`` parsed entries of the newest ``app`` log ('isaac', 'composer', an app name or a log path).

    Each entry is ``{time, elapsed, level, source, extension, stream, message, raw_line_no, offset, noise}``; a
    traceback is one entry whose ``message`` holds all of its lines. ``level`` keeps that level and worse
    ('warning' keeps warnings and errors). ``since`` (ISO time, datetime, or seconds ago) keeps newer entries.
    Only the end of the file is read (at most ``max_bytes``). ``raw_line_no`` is the line in the file; for very
    large files it counts from the start of the window read and the entry has ``raw_line_no_relative``.
    """
    level_name = _check_level(level)
    since_time = _since(since)
    count = max(1, min(int(lines), MAX_TAIL_LINES))
    path, _label = _resolve(app, dirs)
    cap = max(1024, min(int(max_bytes), MAX_READ_BYTES))
    want = min(cap, max(_FIRST_WINDOW, count * 512))
    while True:
        try:
            entries, start, _size, exact = _read_tail(path, want, cap)
        except OSError as exc:
            raise KitLogError(f"Could not read {_display(path)}: {exc.strerror or exc}") from exc
        picked = [e for e in entries if _level_ok(e, level_name)]
        if since_time is not None:
            picked = [e for e in picked if (_parse_time(e.time) or since_time) >= since_time]
            reached = bool(entries) and (_parse_time(entries[0].time) or since_time) < since_time
        else:
            reached = False
        if len(picked) >= count or start == 0 or want >= cap or reached:
            break
        want = min(cap, want * 4)
    return [_entry_dict(e, relative_lines=not exact) for e in picked[-count:]]


# --- polling ------------------------------------------------------------------------------------------------------------

def _identity(path: Path) -> dict[str, Any]:
    st = path.stat()
    with open(path, "rb") as handle:
        head = handle.read(_HEAD_BYTES)
    return {"ino": int(getattr(st, "st_ino", 0) or 0), "hl": len(head),
            "h": hashlib.sha1(head).hexdigest()[:16], "size": st.st_size}


def _same_file(path: Path, cursor: dict[str, Any]) -> bool:
    try:
        st = path.stat()
        if cursor.get("i") and getattr(st, "st_ino", 0) and int(st.st_ino) != int(cursor["i"]):
            return False
        length = int(cursor.get("hl", 0))
        if st.st_size < length:
            return False
        with open(path, "rb") as handle:
            head = handle.read(length)
        return hashlib.sha1(head).hexdigest()[:16] == cursor.get("h")
    except (OSError, ValueError, TypeError):
        return False


def _encode_cursor(path: Path, offset: int, app: str) -> str:
    ident = _identity(path)
    payload = {"v": 1, "p": _display(path), "o": int(offset), "i": ident["ino"], "hl": ident["hl"], "h": ident["h"],
               "a": app}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: Any) -> dict[str, Any] | None:
    if isinstance(cursor, dict):
        data = cursor
    elif isinstance(cursor, str) and cursor.startswith(_CURSOR_PREFIX):
        body = cursor[len(_CURSOR_PREFIX):][:4096]
        try:
            data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
    else:
        return None
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("p"), str):
        return None
    try:
        data["o"] = max(0, int(data.get("o", 0)))
    except (TypeError, ValueError):
        return None
    return data


def _end_offset(path: Path) -> int:
    """Offset just after the last complete line (a half-written last line is read on the next poll)."""
    with open(path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        start = max(0, size - _FIRST_WINDOW)
        handle.seek(start)
        data = handle.read(size - start)
    newline = data.rfind(b"\n")
    if newline < 0:
        return start if start > 0 else 0
    return start + newline + 1


def _read_from(path: Path, offset: int, cap: int, *, final: bool) -> tuple[list[_Entry], int, int, bool]:
    """Entries from ``offset`` to the end: (entries, next offset, skipped bytes, exact line numbers).

    Unless ``final`` (or the file has been quiet for ``QUIET_SECONDS``), a half-written last line and a traceback
    still being written are left for the next poll.
    """
    with open(path, "rb") as handle:
        st = os.fstat(handle.fileno())
        size = st.st_size
        offset = min(offset, size)
        final = final or (time.time() - st.st_mtime) > QUIET_SECONDS
        skipped = 0
        if size - offset > cap:
            skipped = size - cap - offset
            offset = size - cap
            handle.seek(offset)
            data = handle.read(cap)
            newline = data.find(b"\n")
            if 0 <= newline < len(data) - 1:
                skipped += newline + 1
                offset += newline + 1
                data = data[newline + 1:]
        else:
            handle.seek(offset)
            data = handle.read(size - offset)
        if not final:
            newline = data.rfind(b"\n")
            if newline >= 0:
                data = data[: newline + 1]
            elif len(data) < cap:
                data = b""  # one half-written line: wait for its newline
        line_no, exact = _line_base(handle, offset)
    entries = _parse(data, offset, line_no)
    next_offset = offset + len(data)
    if not final and entries and entries[-1].open and (entries[-1].offset > offset or len(data) < cap):
        next_offset = entries[-1].offset
        entries = entries[:-1]
    return entries, next_offset, skipped, exact


def new_errors(app: str | os.PathLike[str] = "isaac", *, cursor: Any = None, level: str = "error",
               dirs: Iterable[str | os.PathLike[str]] | None = None, max_bytes: int = MAX_READ_BYTES,
               max_entries: int = MAX_POLL_ENTRIES) -> dict[str, Any]:
    """Entries at ``level`` or worse written since ``cursor``; call again with the returned ``cursor`` to poll.

    Without a cursor nothing old is returned: the cursor starts at the current end of the log (use ``tail`` or
    ``explain_recent`` for the backlog). When the app starts a new session (a new kit_*.log) or the file is
    truncated or replaced, ``rotated`` is true and reading continues in the new file from its start (the rest of
    the old file is read first). Returns ``{ok, app, path, entries, count, cursor, rotated, started, skipped_bytes,
    more}``; ``more`` means ``max_entries`` were returned and more are waiting.
    """
    level_name = _check_level(level) or "verbose"
    cap = max(1024, min(int(max_bytes), MAX_READ_BYTES))
    limit = max(1, min(int(max_entries), 1000))
    path, label = _resolve(app, dirs)
    state = _decode_cursor(cursor)
    if state is not None and not _looks_like_path(str(app or "")) and state.get("a") not in (None, label):
        state = None  # a cursor from another app's log: start again rather than mixing the two logs
    result: dict[str, Any] = {"ok": True, "app": label, "path": _display(path), "entries": [], "count": 0,
                              "rotated": False, "started": False, "skipped_bytes": 0, "more": False}
    try:
        if state is None:
            result["started"] = True
            if cursor not in (None, ""):
                result["cursor_reset"] = "The cursor was not recognised, so polling starts again from now."
            result["cursor"] = _encode_cursor(path, _end_offset(path), label)
            return result
        batches: list[tuple[Path, list[_Entry], int, bool]] = []
        old_path = Path(os.path.realpath(os.path.expanduser(state["p"])))
        same_path = os.path.normcase(str(old_path)) == os.path.normcase(str(path))
        if same_path and _same_file(path, state) and path.stat().st_size >= state["o"]:
            entries, offset, skipped, exact = _read_from(path, state["o"], cap, final=False)
            batches.append((path, entries, offset, exact))
            result["skipped_bytes"] += skipped
        else:
            result["rotated"] = True
            if not same_path:
                try:
                    _resolve(str(old_path), dirs)  # a cursor may only point inside the log folders
                    if not _same_file(old_path, state):
                        raise KitLogError("old log was replaced")
                    entries, _offset, skipped, exact = _read_from(old_path, state["o"], cap, final=True)
                    batches.append((old_path, entries, _offset, exact))
                    result["skipped_bytes"] += skipped
                except KitLogError:
                    pass
            entries, offset, skipped, exact = _read_from(path, 0, cap, final=False)
            batches.append((path, entries, offset, exact))
            result["skipped_bytes"] += skipped
    except OSError as exc:
        raise KitLogError(f"Could not read {_display(path)}: {exc.strerror or exc}") from exc

    picked: list[dict[str, Any]] = []
    next_offset = batches[-1][2]
    for source_path, entries, end_offset, exact in batches:
        stop = False
        for entry in entries:
            if not _level_ok(entry, level_name):
                continue
            if len(picked) >= limit:
                result["more"] = True
                if source_path == path:
                    next_offset = entry.offset
                else:  # the old file still has entries: resume there next time
                    result["cursor"] = _encode_cursor_for(source_path, state, entry.offset, label)
                stop = True
                break
            item = _entry_dict(entry, relative_lines=not exact)
            if source_path != path:
                item["file"] = _display(source_path)
            picked.append(item)
        if stop:
            break
    result["entries"] = picked
    result["count"] = len(picked)
    result.setdefault("cursor", _encode_cursor(path, next_offset, label))
    return result


def _encode_cursor_for(path: Path, state: dict[str, Any], offset: int, app: str) -> str:
    payload = {"v": 1, "p": _display(path), "o": int(offset), "i": state.get("i", 0), "hl": state.get("hl", 0),
               "h": state.get("h", ""), "a": app}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# --- explaining and summarising -------------------------------------------------------------------------------------------

def _signature(entry: _Entry) -> str:
    lines = [line.strip() for line in entry.lines if line.strip()]
    if entry.lines and _TRACEBACK.match(entry.lines[0].strip()):
        tail_lines = [line for line in entry.lines if _is_exception_line(line)]
        key = tail_lines[-1] if tail_lines else (lines[-1] if lines else "")
    else:
        key = lines[0] if lines else ""
    key = _DETAILS.sub(" ", key)
    key = _HEX.sub("#", key)
    key = _DIGITS.sub("#", key)
    return f"{entry.level}|{entry.source or ''}|{' '.join(key.lower().split())[:300]}"


def _session_entries(path: Path, cap: int) -> tuple[list[_Entry], bool, bool, int]:
    try:
        entries, start, size, exact = _read_tail(path, cap, cap)
    except OSError as exc:
        raise KitLogError(f"Could not read {_display(path)}: {exc.strerror or exc}") from exc
    return entries, start > 0, exact, size


_EXPLANATION_KEYS = ("kind", "category", "severity", "summary", "what_happened", "likely_causes", "how_to_fix",
                     "example_fix", "where", "confidence", "exception", "related_docs")


def _explain(entry: _Entry, knowledge: Any) -> dict[str, Any]:
    from .error_explainer import explain_error

    text = f"[{entry.level.title()}]" + (f" [{entry.source}]" if entry.source else "") + " " + entry.message
    try:
        result = explain_error(redact(text), knowledge=knowledge)
    except Exception as exc:  # noqa: BLE001 - an explainer bug must not hide the log entries
        return {"kind": "unknown", "summary": "This error could not be explained automatically.",
                "how_to_fix": ["Read the message below and search the docs for its key words."],
                "explain_failed": type(exc).__name__}
    return {key: redact(result[key]) if isinstance(result.get(key), str) else result[key]
            for key in _EXPLANATION_KEYS if key in result}


def explain_recent(app: str | os.PathLike[str] = "isaac", *, limit: int = 5, knowledge: Any = None,
                   include_warnings: bool = False, dirs: Iterable[str | os.PathLike[str]] | None = None,
                   max_bytes: int = MAX_READ_BYTES) -> dict[str, Any]:
    """The most recent distinct errors of the newest ``app`` log, de-duplicated and explained in plain English.

    Repeats (same source and message, ignoring numbers) are grouped with a ``count``. Each group carries an
    ``explanation`` from ``error_explainer.explain_error`` (summary, what_happened, how_to_fix, ...; ``knowledge``
    adds related docs). Known harmless Kit messages are not explained but listed under ``noise`` with a reason.
    Returns ``{ok, app, path, errors: [...], noise: [...], error_count, distinct, truncated}``.
    """
    count = max(1, min(int(limit), 20))
    path, label = _resolve(app, dirs)
    cap = max(1024, min(int(max_bytes), MAX_READ_BYTES))
    entries, truncated, exact, _size = _session_entries(path, cap)
    floor = "warning" if include_warnings else "error"
    groups: dict[str, dict[str, Any]] = {}
    noise: dict[str, dict[str, Any]] = {}
    total = 0
    for entry in entries:
        if not _level_ok(entry, floor):
            continue
        total += 1
        rule = _noise_of(entry)
        if rule is not None:
            item = noise.setdefault(rule["id"], {"id": rule["id"], "reason": rule["reason"], "count": 0,
                                                "level": entry.level, "example": redact(entry.lines[0])[:300]})
            item["count"] += 1
            item["last_time"] = entry.time
            continue
        key = _signature(entry)
        group = groups.get(key)
        if group is None:
            group = groups[key] = {"count": 0, "first_time": entry.time, "_entry": entry}
        group["count"] += 1
        group["last_time"] = entry.time
        group["_entry"] = entry
        group["_order"] = entry.offset
    recent = sorted(groups.values(), key=lambda g: g["_order"], reverse=True)[:count]
    errors = []
    for group in recent:
        entry = group["_entry"]
        item = _entry_dict(entry, relative_lines=not exact)
        errors.append({
            "count": group["count"],
            "first_time": group["first_time"],
            "last_time": group["last_time"],
            "level": entry.level,
            "source": item["source"],
            "extension": item["extension"],
            "message": item["message"][:4000],
            "raw_line_no": item["raw_line_no"],
            "explanation": _explain(entry, knowledge),
        })
    return {
        "ok": True,
        "app": label,
        "path": _display(path),
        "errors": errors,
        "noise": sorted(noise.values(), key=lambda n: -n["count"]),
        "error_count": total,
        "distinct": len(groups),
        "truncated": truncated,
        "note": ("No errors in this log." if not errors else
                 "Fix the oldest error first: later errors are often caused by it (see first_time)."),
    }


def summary(app: str | os.PathLike[str] = "isaac", *, dirs: Iterable[str | os.PathLike[str]] | None = None,
            max_bytes: int = MAX_READ_BYTES, top: int = 15) -> dict[str, Any]:
    """Counts for the current session log of ``app``: by level, by extension and by source, plus noise counts.

    Returns ``{ok, app, path, size, modified, active, entries, by_level, by_extension, by_source, noise, errors,
    warnings, first_time, last_time, session_start, truncated}``; ``truncated`` means only the last
    ``max_bytes`` of a big log were counted (``session_start`` is then unknown).
    """
    path, label = _resolve(app, dirs)
    cap = max(1024, min(int(max_bytes), MAX_READ_BYTES))
    entries, truncated, _exact, size = _session_entries(path, cap)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = time.time()
    levels = Counter(e.level for e in entries)
    noisy = Counter()
    by_ext: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    ext_problems: dict[str, Counter[str]] = {}
    for entry in entries:
        ext = entry.extension or "(none)"
        by_ext[ext] += 1
        by_source[entry.source or "(none)"] += 1
        if entry.level in ("warning", "error", "fatal"):
            rule = _noise_of(entry)
            if rule is not None:
                noisy[rule["id"]] += 1
            else:
                ext_problems.setdefault(ext, Counter())[entry.level] += 1
    limit = max(1, min(int(top), 100))
    return {
        "ok": True,
        "app": label,
        "path": _display(path),
        "size": size,
        "modified": _iso(mtime),
        "active": (time.time() - mtime) <= ACTIVE_MINUTES * 60,
        "entries": len(entries),
        "by_level": {name: levels.get(name, 0) for name in LEVELS},
        "errors": levels.get("error", 0) + levels.get("fatal", 0),
        "warnings": levels.get("warning", 0),
        "by_extension": {redact(k): v for k, v in by_ext.most_common(limit)},
        "by_source": {redact(k): v for k, v in by_source.most_common(limit)},
        "problems_by_extension": {redact(ext): dict(c) for ext, c in sorted(
            ext_problems.items(), key=lambda kv: -sum(kv[1].values()))[:limit]},
        "noise": dict(noisy.most_common()),
        "first_time": entries[0].time if entries else None,
        "last_time": entries[-1].time if entries else None,
        "session_start": entries[0].time if entries and not truncated else None,
        "truncated": truncated,
    }


__all__ = [
    "ACTIVE_MINUTES", "ENV_VAR", "KitLogError", "LEVELS", "MAX_READ_BYTES", "NOISE_PATTERNS", "app_key",
    "explain_recent", "find_logs", "log_dirs", "new_errors", "redact", "summary", "tail",
]
