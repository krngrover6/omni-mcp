"""Tool-usage report: counts per tool and day, stored without arguments, with starter-list suggestions."""
from __future__ import annotations

import asyncio
import json
import tempfile
import types
from datetime import date
from pathlib import Path

from omniverse_scene_mcp import usage_stats as us


def _app(*tools):
    registry = {name: types.SimpleNamespace(fn=fn, is_async=asyncio.iscoroutinefunction(fn)) for name, fn in tools}
    return types.SimpleNamespace(_tool_manager=types.SimpleNamespace(_tools=registry))


def test_counter_wraps_sync_and_async_tools_once_and_counts_failures():
    store = us.UsageStats(None)

    def good(x=1):
        return {"ok": True, "x": x}

    async def bad():
        return {"ok": False, "error": "nope"}

    app = _app(("scene_summary", good), ("scene_move", bad))
    assert sorted(us.install_usage_counter(app, store)) == ["scene_move", "scene_summary"]
    assert us.install_usage_counter(app, store) == []  # never wrapped twice
    registry = app._tool_manager._tools
    assert registry["scene_summary"].fn(x=5) == {"ok": True, "x": 5}
    registry["scene_summary"].fn()
    asyncio.run(registry["scene_move"].fn())
    totals = store.totals(1)
    assert totals["scene_summary"]["calls"] == 2 and totals["scene_summary"]["errors"] == 0
    assert totals["scene_move"]["calls"] == 1 and totals["scene_move"]["errors"] == 1


def test_report_suggests_starter_changes_and_the_file_holds_no_arguments():
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "tool_usage.json"
        store = us.UsageStats(path, clock=lambda: 0.0)
        for _ in range(28):
            store.record("scene_summary", True, 0.01)
        for _ in range(4):
            store.record("graph_inspect", True, 0.5)  # hidden tool used often
        store.record("graph_inspect", False, 0.1)
        report = us.usage_report(7, store, all_tools=["scene_summary", "graph_inspect", "scene_find"],
                                 starter=["scene_summary", "scene_find"])
        assert report["ok"] and report["calls"] == 33 and report["most_used"][0]["tool"] == "scene_summary"
        assert report["suggest_add_to_starter"] == ["graph_inspect"]
        assert report["suggest_drop_from_starter"] == ["scene_find"]  # never used, after 30+ calls
        assert report["most_failing"] == [{"tool": "graph_inspect", "errors": 1, "calls": 5}]
        store.flush()
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert set(saved["days"][date.today().isoformat()]) == {"scene_summary", "graph_inspect"}
        assert us.UsageStats(path).totals(1)["scene_summary"]["calls"] == 28  # read back after a restart
    assert not us.usage_report(0)["ok"] and not us.usage_report(True)["ok"]
    assert us.default_path({"OMNIVERSE_SCENE_MCP_TEST": "1"}) is None  # tests never write to disk
