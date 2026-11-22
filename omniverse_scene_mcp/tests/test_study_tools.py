"""study_check over study files; snippet / lesson memory with dry-run writes; lessons shown by scene_diagnose."""
from __future__ import annotations

try:
    from pxr import Usd, UsdGeom
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import os
import tempfile
from pathlib import Path
from unittest import mock

from omniverse_scene_mcp import features, study_check, study_memory
from omniverse_scene_mcp.debug_trainer import diagnose

DAY = '''# Day 99

Good code:

```python
from pxr import Usd, UsdGeom
stage = Usd.Stage.CreateInMemory()
UsdGeom.Cube.Define(stage, "/World/Box").GetSizeAttr().Set(2)
```

A mistake to catch:

```python
from pxr import Usd
prim = Usd.Stage.CreateInMemory().DefinePrim("/A")
prim.GetAttr("size")
```

A fragment (skipped):

```python
stage = ...
```

```usda
def Mesh "M"
{
    double size = 2
}
```
'''


def test_study_check_finds_mistakes_with_file_line_and_skips_fragments():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "notes").mkdir()
        (Path(tmp) / "notes" / "day99.md").write_text(DAY, encoding="utf-8")
        with mock.patch.object(study_check, "workspace_root", return_value=Path(tmp)):
            result = study_check.check_study("notes")
            assert result["blocks"] == 4 and result["skipped_fragments"] == 1 and result["checked"] == 3
            found = {(p["file"], p["line"], p["code"]) for p in result["problems"]}
            assert found == {("notes/day99.md", 16, "unknown_name"), ("notes/day99.md", 28, "not_in_schema")}
            assert not study_check.check_study("..")["ok"]  # never outside the workspace
            assert not study_check.check_study("missing")["ok"]


def test_snippets_save_only_with_dry_run_false_and_are_found():
    code = "from pxr import UsdPhysics\nprint(UsdPhysics.DriveAPI)\n"
    plan = study_memory.save_snippet("wasd drive", code, tags="isaac, drive", verified="Isaac 6.1, 2026-11-20")
    assert plan["dry_run"] and plan["static_check"] == "ok"
    assert study_memory.find_snippets("drive")["count"] == 0  # nothing written yet
    saved = study_memory.save_snippet("wasd drive", code, tags=["isaac", "drive"], verified="Isaac 6.1",
                                      dry_run=False)
    assert os.path.isfile(saved["written"])
    hit = study_memory.find_snippets("keyboard drive robot")["snippets"][0]
    assert hit["name"] == "wasd drive" and hit["code"] == code.strip()
    assert study_memory.find_snippets(tag="isaac")["count"] == 1 and study_memory.find_snippets(tag="kit")["count"] == 0
    again = study_memory.save_snippet("WASD drive", code, verified="x", dry_run=False)
    assert again["replaces"] and len(study_memory._load("snippets")) == 1
    assert not study_memory.save_snippet("../bad", code, verified="x")["ok"]
    assert not study_memory.save_snippet("ok", code)["ok"]  # where it was verified is required
    bad = study_memory.save_snippet("typo", "from pxr import Usd\nUsd.Stage.Create('a')\n", verified="no")
    assert bad["static_check"] == "problems" and bad["problems"]


def test_lessons_show_under_matching_diagnose_findings():
    study_memory.add_lesson("Orient on new Isaac Xforms", "New Xforms got Orient; switch the default op to Rotate.",
                            seen="Isaac camera tour 2026-11-13", codes=["orient_not_keyable"], dry_run=False)
    assert study_memory.find_lessons("orient curve editor")["count"] == 1
    assert study_memory.find_lessons(code="orient_not_keyable")["lessons"][0]["seen"].startswith("Isaac")
    stage = Usd.Stage.CreateInMemory()
    rig = UsdGeom.Xform.Define(stage, "/Rig")
    UsdGeom.Camera.Define(stage, "/Rig/Cam")
    rig.AddOrientOp()
    finding = next(f for f in diagnose(stage, "animation")["findings"] if f["code"] == "orient_not_keyable")
    assert finding["lessons"][0]["title"] == "Orient on new Isaac Xforms"


def test_memory_tools_are_registered_and_writes_default_to_dry_run():
    for name in ("snippet_find", "lesson_find", "study_check"):
        assert features.FEATURES[name].mutates is False
    for name in ("snippet_save", "lesson_add"):
        assert features.FEATURES[name].mutates is True
        assert features.signature(name).parameters["dry_run"].default is True
