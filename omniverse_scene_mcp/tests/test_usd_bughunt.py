"""USD bug hunt: every puzzle is broken as written, and its documented fix makes the goal hold."""
from __future__ import annotations

import tempfile
from pathlib import Path

from omniverse_scene_mcp import features, usd_bughunt as bh

FIXES = {
    "sublayer_order": ("root.usda", "@./base.usda@,\n        @./colors.usda@", "@./colors.usda@,\n        @./base.usda@"),
    "missing_default_prim": ("chair.usda", "#usda 1.0\n", '#usda 1.0\n(\n    defaultPrim = "ChairAsset"\n)\n'),
    "wrong_reference_path": ("root.usda", "./asset/box.usda", "./assets/box.usda"),
    "variant_not_selected": ("root.usda", 'prepend variantSets = "size"',
                             'variants = {\n            string size = "big"\n        }\n        prepend variantSets = "size"'),
    "local_beats_variant": ("light.usda", "    double intensity = 1\n    variantSet", "    variantSet"),
    "inherit_typo": ("root.usda", "</_class_Pn>", "</_class_Pin>"),
    "edit_inside_instance": ("root.usda", 'def Xform "ChairB" (\n        instanceable = true',
                             'def Xform "ChairB" (\n        instanceable = false'),
}


def test_every_puzzle_is_broken_then_fixed_by_its_documented_fix():
    folder = tempfile.mkdtemp()
    assert set(FIXES) == set(bh.PUZZLES)
    for pid, (name, old, new) in FIXES.items():
        assert bh.make_puzzle(pid, folder)["dry_run"] and not (Path(folder) / pid).exists()
        made = bh.make_puzzle(pid, folder, dry_run=False)
        assert made["ok"], made
        assert not bh.make_puzzle(pid, folder, dry_run=False)["ok"]  # never overwrites
        assert bh.verify_puzzle(pid, folder)["solved"] is False, pid  # the bug is real
        path = Path(folder) / pid / name
        text = path.read_text(encoding="utf-8")
        assert old in text, (pid, old)
        path.write_text(text.replace(old, new), encoding="utf-8")
        assert bh.verify_puzzle(pid, folder)["solved"] is True, pid  # the documented fix works


def test_answers_hints_and_listing():
    assert bh.check_answer("local_beats_variant", "the local opinion intensity = 1 beats the variant")["correct"]
    assert not bh.check_answer("sublayer_order", "the lamp is broken")["correct"]
    assert len(bh.check_answer("inherit_typo", hint=True)["hints"]) == 2
    assert "first" in bh.check_answer("sublayer_order", reveal=True)["fix"]
    levels = [p["level"] for p in bh.list_puzzles()["puzzles"]]
    assert levels == sorted(levels) and "answer" not in str(bh.list_puzzles()["puzzles"][0])
    assert not bh.make_puzzle("nope")["ok"] and not bh.verify_puzzle("inherit_typo", tempfile.mkdtemp())["ok"]
    assert features.FEATURES["usd_bug_make"].mutates and not features.FEATURES["usd_bug_verify"].mutates
