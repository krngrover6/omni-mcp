"""project_report: tool results become a portfolio report (Markdown + self-contained HTML); temp folders only."""

from __future__ import annotations

import re
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    from pxr import Usd, UsdGeom, UsdLux
except ImportError:  # pragma: no cover - depends on the local environment
    Usd = None

from omniverse_scene_mcp.journal import ChangeJournal
from omniverse_scene_mcp.project_report import build_report, write_report

SCRIPT = "<script>alert('x')</script>"

CONTEXT = f"""# Project Context: Warehouse {SCRIPT}

## Goal

Build a small warehouse scene and learn references. {SCRIPT}

## Stage map

- /World/Shelf

## Decisions and lessons

- 2026-10-20: kept Z-up because Isaac Sim uses it.

## Current status and next steps

- Add a second light
- [ ] Bind a wood material to the shelves
1. Take a screenshot | then save

```
## not a heading inside code
```
"""


@contextmanager
def _folder():
    folder = Path(tempfile.mkdtemp(prefix="project_report_"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _tour() -> dict:
    return {
        "ok": True,
        "overview": f"This is warehouse.usda with 3 shapes. {SCRIPT}",
        "sections": [
            {"title": "Geometry", "items": [{"path": "/World/Floor", "kind": "geometry", "plain": "A flat floor."},
                                            {"path": "/World/Box", "kind": "geometry", "plain": f"A box {SCRIPT}"}]},
            {"title": "Lights", "items": []},
        ],
        "counts": {"prims": 5, "geometry": 3, "lights": 1},
        "questions_to_explore": ["Why is the floor grey?"],
    }


def _assignment() -> dict:
    return {
        "ok": True, "assignment": "isaac_session02_warehouse", "title": "Session 2 warehouse", "score_pct": 62.5,
        "passed": False, "summary": "5 of 8 checks pass.",
        "results": [{"rule": "floor", "title": "A floor", "status": "pass", "detail": "Found /World/Floor."},
                    {"rule": "light", "title": "Key light", "status": "fail", "detail": f"No light {SCRIPT}"}],
        "next_step": "Key light: add a RectLight aimed at the shelves.",
    }


def _health() -> dict:
    return {"score": 87.5, "summary": "Mostly healthy.", "checks": [
        {"name": "Textures resolve", "status": "pass", "detail": "All 4 found."},
        {"title": "Units", "ok": False, "message": f"metersPerUnit is 0.01 {SCRIPT}"},
        {"id": "default_prim", "severity": "warning", "why": "No defaultPrim."},
        "plain string check",
    ]}


def _journal_summary() -> dict:
    journal = ChangeJournal(memory_only=True)
    journal.record({"time": "2026-10-22T10:00:00", "tool": "scene_move", "action": "move", "target": "composer",
                    "prims": ["/World/Box"], "after": {"translation": [1, 2, 3]}})
    journal.record({"time": "2026-10-22T10:05:00", "tool": "scene_undo_last", "action": "undo", "kind": "undo"})
    return journal.summary("2026-10-22")


def test_full_report_has_every_section_and_escapes_text():
    with _folder() as folder:
        (folder / "PROJECT_CONTEXT.md").write_text(CONTEXT, encoding="utf-8")
        report = build_report(
            project_dir=folder,
            scene_summary={"stage": "warehouse.usda", "up_axis": "Z", "counts": {"lights": 1}, "layers": ["a", "b"],
                           "note": SCRIPT},
            tour=_tour(), health=_health(), assignment=_assignment(), journal_summary=_journal_summary(),
            screenshots=["shots/view 1.png", {"path": "shots/top.jpg", "caption": SCRIPT}],
            decisions=["Chose a Z-up stage", {"date": "2026-10-22", "decision": "Used references", "why": "reuse"}],
        )
    assert report["ok"], report
    assert report["sections"] == ["goal", "scene", "tour", "health", "assignment", "journal", "screenshots",
                                  "decisions", "next_steps"]
    assert report["project_context"]["found"] is True and report["warnings"] == []
    md, page = report["markdown"], report["html"]
    for heading in ("Goal", "Scene at a glance", "Scene tour", "Asset health", "Assignment check", "What I changed",
                    "Screenshots", "Decisions and lessons", "Next steps"):
        assert f"## {heading}" in md and f"<h2>{heading}</h2>" in page, heading
    assert "<script" not in md.lower() and "<script" not in page.lower()
    assert "&lt;script&gt;" in page and "&lt;script&gt;" in md
    assert report["title"].startswith("Warehouse ") and "Project Context" not in report["title"]
    assert "Add a second light" in md and "Bind a wood material to the shelves" in page
    assert "Take a screenshot \\| then save" in md  # a pipe cannot break a Markdown table or list
    assert "not a heading inside code" not in md
    assert "From the assignment check: Key light" in md
    assert "kept Z\\-up" not in md and "Used references (why: reuse)" in md  # given decisions win over the file
    assert 'src="shots/view%201.png"' in page and "](shots/view%201.png)" in md
    assert '<span class="badge fail">fail</span>' in page and '<span class="badge partial">partly</span>' in page
    assert "87.5" in page and "62.5%" in page and "Undos" in page
    assert "/World/Floor: A flat floor." in page and "Why is the floor grey?" in page


def test_html_is_self_contained_and_theme_aware():
    page = build_report(tour=_tour(), title="Demo")["html"]
    assert page.startswith("<!doctype html>") and "<style>" in page
    assert "prefers-color-scheme: dark" in page and "color-scheme: light dark" in page
    assert "://" not in page and "<link" not in page and "@import" not in page and "url(" not in page
    assert "Content-Security-Policy" in page and "default-src 'none'" in page
    assert re.search(r"<title>Demo</title>", page)


def test_screenshot_paths_must_be_local_relative_images():
    with _folder() as folder:
        inside = folder / "shots" / "a.png"
        report = build_report(project_dir=folder, screenshots=[
            str(inside), "C:/Windows/evil.png", "/etc/evil.png", "../up.png", "http://example.com/x.png",
            "shot.exe", "ok.webp", "bad\npath.png", 42, "x.png?y=1",
        ])
    assert report["ok"]
    page = report["html"]
    assert 'src="shots/a.png"' in page and 'src="ok.webp"' in page
    assert page.count("<img ") == 2
    joined = " ".join(report["warnings"])
    assert "made relative to the project folder" in joined and "outside the project folder" in joined
    assert "'..'" in joined and "looks like a URL" in joined and "not a png" in joined
    assert "example.com" not in page and "evil" not in page


def test_health_results_are_read_defensively():
    odd = build_report(health={"score": "B+", "checks": {"Textures": "ok", "Units": {"status": "error"}}},
                       assignment={"ok": False, "error": "No stage"}, tour={"ok": False, "error": "pxr missing"},
                       journal_summary="not a dict")
    assert odd["ok"]
    assert "B+" in odd["html"] and '<span class="badge pass">pass</span>' in odd["html"]
    assert '<span class="badge fail">fail</span>' in odd["html"]
    assert "The assignment check could not run: No stage" in odd["markdown"]
    assert "The scene tour could not run: pxr missing" in odd["markdown"]
    assert "not a dict" in odd["markdown"]
    assert build_report(health={"ok": False, "error": "boom"})["markdown"].count("could not run: boom") == 1
    assert build_report(health=["x"])["sections"] == ["health"]


def test_asset_health_shaped_results_show_score_grade_and_fixes():
    health = {"ok": True, "healthy": False, "score": 72, "grade": "C", "summary": "2 problems.", "checks": [
        {"id": "missing_files", "title": "Missing files", "status": "fail", "count": 2,
         "examples": ["@./tex/wood.png@ on /World/Shelf", "@./a.usd@"], "why": "Nothing loads.",
         "how_to_fix": ["Copy the textures next to the scene."]},
        {"id": "extents", "title": "Extents", "status": "warn", "count": 0, "examples": [], "why": "Stale bounds."},
        {"id": "time", "title": "Time samples", "status": "info", "count": 0,
         "examples": ["Not checked: the time budget ran out."]},
        {"id": "names", "title": "Prim names portable", "status": "pass", "count": 0, "examples": [],
         "how_to_fix": []},
    ]}
    page = build_report(health=health)["html"]
    assert "72 / 100 (grade C)" in page and "At least one check fails" in page
    assert "2 found, e.g. @./tex/wood.png@ on /World/Shelf, @./a.usd@. Fix: Copy the textures next to the scene." in page
    assert "Stale bounds." in page and '<span class="badge info">info</span>' in page
    assert '<span class="badge partial">partly</span>' in page and page.count("<tr>") == 6  # score row, header row, 4 checks
    try:
        from omniverse_scene_mcp.asset_health import asset_health  # built in parallel; skip if not there yet
    except ImportError:
        return
    if Usd is None:
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Cube.Define(stage, "/World/Box")
    real = build_report(health=asset_health(stage))
    assert real["ok"] and real["sections"] == ["health"] and "<table>" in real["html"]


def test_empty_report_and_bad_project_dir():
    empty = build_report()
    assert empty["ok"] and empty["title"] == "My OpenUSD project" and empty["sections"] == []
    assert "only has a title" in empty["warnings"][0]
    with _folder() as folder:
        missing = build_report(project_dir=folder / "nope")
        assert missing["ok"] is False and "not an existing folder" in missing["error"]
        no_context = build_report(project_dir=folder)
        assert no_context["ok"] and no_context["title"] == folder.name
        assert "No PROJECT_CONTEXT.md" in no_context["warnings"][0]
    hostile = build_report(title="# Big\n| a | b |", decisions="- item")
    assert hostile["markdown"].startswith("# \\# Big \\| a \\| b \\|\n")
    assert "- \\- item" in hostile["markdown"]


def test_real_scene_tour_and_assignment_results_render():
    if Usd is None:
        return
    from omniverse_scene_mcp.assignments import check_assignment
    from omniverse_scene_mcp.scene_tour import scene_tour

    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Box")
    UsdLux.RectLight.Define(stage, "/World/Key")
    report = build_report(tour=scene_tour(stage), assignment=check_assignment(stage))
    assert report["ok"] and report["sections"][:2] == ["tour", "assignment"]
    assert "/World/Box" in report["markdown"] and "<table>" in report["html"]


def test_write_report_previews_refuses_overwrite_and_writes_only_two_files():
    report = build_report(tour=_tour(), title="Write test")
    with _folder() as folder:
        preview = write_report(folder, report)
        assert preview["ok"] and preview["dry_run"] and list(folder.iterdir()) == []
        done = write_report(folder, report, dry_run=False)
        assert done["ok"] and sorted(p.name for p in folder.iterdir()) == ["report.html", "report.md"]
        assert (folder / "report.html").read_text(encoding="utf-8") == report["html"]
        (folder / "report.md").write_text("my edits", encoding="utf-8")
        again = write_report(folder, report, dry_run=False)
        assert again["ok"] is False and "overwrite=True" in again["error"] and again["written"] == []
        assert (folder / "report.md").read_text(encoding="utf-8") == "my edits"
        assert write_report(folder, report)["ok"] is False  # the preview refuses too
        replaced = write_report(folder, report, dry_run=False, overwrite=True)
        assert replaced["ok"] and len(replaced["written"]) == 2
        assert (folder / "report.md").read_text(encoding="utf-8") == report["markdown"]
        assert sorted(p.name for p in folder.iterdir()) == ["report.html", "report.md"]

        assert write_report(folder / "missing", report, dry_run=False)["ok"] is False
        assert not (folder / "missing").exists()
        assert write_report("", report, dry_run=False)["ok"] is False
        assert write_report(None, report, dry_run=False)["ok"] is False
        assert write_report(folder, {"ok": False}, dry_run=False)["ok"] is False
        assert write_report(folder, {"ok": True, "markdown": "", "html": "x"}, dry_run=False)["ok"] is False
        sub = folder / "sub"
        sub.mkdir()
        (sub / "report.html").mkdir()
        blocked = write_report(sub, report, dry_run=False, overwrite=True)
        assert blocked["ok"] is False and "not a file" in blocked["error"]
        assert sorted(p.name for p in sub.iterdir()) == ["report.html"]
