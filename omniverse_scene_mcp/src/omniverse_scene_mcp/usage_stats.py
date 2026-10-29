"""Tool-usage report: which tools you really call, which fail, and what the starter list should hold.

Every tool call is counted per day (calls, errors, total time): only names and numbers, never arguments or
results, so nothing private is stored. Counts live in ``<workspace>/scene-journal/tool_usage.json`` (or
``$OMNIVERSE_SCENE_MCP_USAGE``; tests keep them in memory) and are written at most every 20 seconds.
usage_report then suggests trimming starter tools you never use and promoting hidden tools you keep reaching
through router_call_tool, so the list a client loads stays small and useful.
"""

from __future__ import annotations

import atexit
import functools
import inspect
import json
import os
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

ENV_PATH = "OMNIVERSE_SCENE_MCP_USAGE"
TEST_ENV = "OMNIVERSE_SCENE_MCP_TEST"
FILE_NAME = "tool_usage.json"
KEEP_DAYS = 90
FLUSH_SECONDS = 20.0


def default_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    if env.get(TEST_ENV) and not env.get(ENV_PATH):
        return None  # tests: memory only
    configured = (env.get(ENV_PATH) or "").strip()
    if configured:
        path = Path(configured)
        return path / FILE_NAME if path.suffix.lower() != ".json" else path
    from .journal import workspace_root

    return workspace_root() / "scene-journal" / FILE_NAME


class UsageStats:
    def __init__(self, path: Path | None = None, clock: Any = time.monotonic) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._days: dict[str, dict[str, dict[str, float]]] = self._read()
        self._dirty = False
        self._last_flush = clock()

    def _read(self) -> dict[str, dict[str, dict[str, float]]]:
        if self.path is None or not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        days = data.get("days") if isinstance(data, dict) else None
        return days if isinstance(days, dict) else {}

    def record(self, tool: str, ok: bool, seconds: float, today: date | None = None) -> None:
        day = (today or date.today()).isoformat()
        with self._lock:
            row = self._days.setdefault(day, {}).setdefault(tool, {"calls": 0, "errors": 0, "seconds": 0.0})
            row["calls"] += 1
            row["errors"] += 0 if ok else 1
            row["seconds"] = round(row["seconds"] + max(0.0, seconds), 3)
            self._dirty = True
            due = self._clock() - self._last_flush >= FLUSH_SECONDS
        if due:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._dirty or self.path is None:
                return
            cutoff = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
            self._days = {d: rows for d, rows in self._days.items() if d >= cutoff}
            payload = json.dumps({"version": 1, "days": self._days}, indent=1, sort_keys=True)
            self._dirty = False
            self._last_flush = self._clock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, self.path)
        except OSError:
            self._dirty = True  # try again next time

    def totals(self, days: int, today: date | None = None) -> dict[str, dict[str, float]]:
        first = ((today or date.today()) - timedelta(days=days - 1)).isoformat()
        out: dict[str, dict[str, float]] = {}
        with self._lock:
            for day, rows in self._days.items():
                if day < first:
                    continue
                for tool, row in rows.items():
                    total = out.setdefault(tool, {"calls": 0, "errors": 0, "seconds": 0.0})
                    for key in total:
                        total[key] += row.get(key, 0)
        return out


_STATS: UsageStats | None = None


def stats() -> UsageStats:
    global _STATS
    if _STATS is None:
        _STATS = UsageStats(default_path())
        atexit.register(_STATS.flush)
    return _STATS


def _failed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok") is False


def install_usage_counter(app: Any, store: UsageStats | None = None) -> list[str]:
    """Wrap every registered tool so each call is counted (name, ok, time). Never wraps a tool twice."""
    from .profiles import _tool_registry

    registry = _tool_registry(app) or {}
    counter = store or stats()
    wrapped = []
    for name, tool in registry.items():
        fn = getattr(tool, "fn", None)
        if fn is None or getattr(fn, "_omniverse_counted", False):
            continue
        is_async = bool(getattr(tool, "is_async", inspect.iscoroutinefunction(fn)))

        def make(fn: Any = fn, name: str = name) -> Any:
            if is_async:
                @functools.wraps(fn)
                async def async_counted(*args: Any, **kwargs: Any) -> Any:
                    start, ok = time.perf_counter(), False
                    try:
                        result = await fn(*args, **kwargs)
                        ok = not _failed(result)
                        return result
                    finally:
                        counter.record(name, ok, time.perf_counter() - start)
                async_counted._omniverse_counted = True  # type: ignore[attr-defined]
                return async_counted

            @functools.wraps(fn)
            def counted(*args: Any, **kwargs: Any) -> Any:
                start, ok = time.perf_counter(), False
                try:
                    result = fn(*args, **kwargs)
                    ok = not _failed(result)
                    return result
                finally:
                    counter.record(name, ok, time.perf_counter() - start)
            counted._omniverse_counted = True  # type: ignore[attr-defined]
            return counted

        try:
            tool.fn = make()
        except (AttributeError, TypeError):
            continue
        wrapped.append(name)
    return wrapped


def usage_report(days: int = 14, store: UsageStats | None = None, all_tools: Iterable[str] | None = None,
                 starter: Iterable[str] | None = None) -> dict[str, Any]:
    """Read-only: tool calls over the last `days` days: most used, most failing, slowest, starter tools never used,
    and hidden tools you call often (candidates for the starter list)."""
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= KEEP_DAYS:
        return {"ok": False, "error": f"days must be a whole number from 1 to {KEEP_DAYS}."}
    counter = store or stats()
    totals = counter.totals(days)
    calls = sum(row["calls"] for row in totals.values())
    ranked = sorted(totals.items(), key=lambda item: -item[1]["calls"])
    starter_set = set(starter or [])
    meta = {"router_call_tool", "router_list_tools", "router_enable_tools"}
    promote = [name for name, row in ranked if row["calls"] >= 3 and name not in starter_set and name not in meta][:8]
    never = sorted(starter_set - set(totals)) if calls >= 30 else []
    failing = [{"tool": n, "errors": int(r["errors"]), "calls": int(r["calls"])}
               for n, r in sorted(totals.items(), key=lambda i: -i[1]["errors"]) if r["errors"]][:8]
    slow = [{"tool": n, "avg_seconds": round(r["seconds"] / r["calls"], 2)}
            for n, r in sorted(totals.items(), key=lambda i: -(i[1]["seconds"] / max(1, i[1]["calls"])))
            if r["calls"]][:5]
    return {
        "ok": True, "read_only": True, "days": days, "calls": int(calls), "tools_used": len(totals),
        "tools_registered": len(set(all_tools or [])) or None,
        "most_used": [{"tool": n, "calls": int(r["calls"])} for n, r in ranked[:15]],
        "most_failing": failing, "slowest": slow,
        "suggest_add_to_starter": promote,
        "suggest_drop_from_starter": never,
        "message": ("No calls counted yet in this period." if not calls else
                    f"{int(calls)} calls to {len(totals)} tools in {days} day(s)."
                    + ("" if calls >= 30 else " Starter trimming is suggested only after 30 calls.")),
        "stored": str(counter.path) if counter.path else "memory only (tests)",
        "privacy": "Only tool names, counts and times are stored: never arguments, paths or results.",
    }
