"""USD bug hunt (NCP-OUSD composition practice): small sets of .usda files with ONE hidden composition bug.

Each puzzle states a goal ("the lamp should be red") that the files do not reach yet. Open root.usda in Composer
or Isaac Sim (File > Open), or read the files in a text editor, find out why, fix the file, then:
- usd_bug_answer(id, answer) grades your explanation (hints first, the answer only when you ask);
- usd_bug_verify(id) opens your fixed files with USD and says whether the goal now holds.
Puzzles are written as NEW folders under isaac_study/usd_bugs/ and never overwrite one; the topics are the
exam's: sublayer order, references and defaultPrim, variants, LIVERPS strength, inherits, instancing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    from pxr import Usd, UsdGeom
except ImportError:  # pragma: no cover
    Usd = UsdGeom = None

HEADER = "#usda 1.0\n"


def _colour(stage: Any, path: str) -> tuple[float, ...] | None:
    prim = stage.GetPrimAtPath(path)
    value = UsdGeom.Gprim(prim).GetDisplayColorAttr().Get() if prim and prim.IsA(UsdGeom.Gprim) else None
    return tuple(round(v, 2) for v in value[0]) if value else None


PUZZLES: dict[str, dict[str, Any]] = {
    "sublayer_order": {
        "level": 1, "topic": "Composition: sublayer strength",
        "goal": "/World/Lamp should be RED: colors.usda was added to make it red.",
        "files": {
            "root.usda": HEADER + '(\n    subLayers = [\n        @./base.usda@,\n        @./colors.usda@\n    ]\n)\n',
            "base.usda": HEADER + 'def Xform "World"\n{\n    def Sphere "Lamp"\n    {\n'
                                  '        color3f[] primvars:displayColor = [(0, 0, 1)]\n    }\n}\n',
            "colors.usda": HEADER + 'over "World"\n{\n    over "Lamp"\n    {\n'
                                    '        color3f[] primvars:displayColor = [(1, 0, 0)]\n    }\n}\n',
        },
        "check": lambda stage: _colour(stage, "/World/Lamp") == (1.0, 0.0, 0.0),
        "answer": ["order", "first", "stronger", "strongest", "earlier", "top"],
        "hints": ["Both files say what colour the lamp is. Which one does USD listen to?",
                  "In a subLayers list, is the first entry the strongest or the weakest?"],
        "cause": "Sublayers are strongest FIRST: base.usda (blue) is listed before colors.usda (red), so blue wins.",
        "fix": "Put @./colors.usda@ first in the subLayers list."},
    "missing_default_prim": {
        "level": 1, "topic": "References: defaultPrim",
        "goal": "/World/Chair should show the chair from chair.usda (a Cube named Seat under it).",
        "files": {
            "root.usda": HEADER + 'def Xform "World"\n{\n    def Xform "Chair" (\n        prepend references = '
                                  '@./chair.usda@\n    )\n    {\n    }\n}\n',
            "chair.usda": HEADER + 'def Xform "ChairAsset"\n{\n    def Cube "Seat"\n    {\n    }\n}\n',
        },
        "check": lambda stage: bool(stage.GetPrimAtPath("/World/Chair/Seat")),
        "answer": ["defaultprim", "default prim"],
        "hints": ["The reference names a FILE but no prim inside it. How does USD choose which prim to bring in?",
                  "Look at the very top of chair.usda: is something missing from its header?"],
        "cause": "chair.usda has no defaultPrim, and the reference names no prim, so nothing is brought in.",
        "fix": 'Add defaultPrim = "ChairAsset" to chair.usda\'s header, or reference @./chair.usda@</ChairAsset>.'},
    "wrong_reference_path": {
        "level": 1, "topic": "References: asset paths",
        "goal": "/World/Box should show the box from assets/box.usda (a Cube named Body under it).",
        "files": {
            "root.usda": HEADER + 'def Xform "World"\n{\n    def Xform "Box" (\n        prepend references = '
                                  '@./asset/box.usda@\n    )\n    {\n    }\n}\n',
            "assets/box.usda": HEADER + '(\n    defaultPrim = "Box"\n)\n\ndef Xform "Box"\n{\n    def Cube "Body"\n'
                                        '    {\n    }\n}\n',
        },
        "check": lambda stage: bool(stage.GetPrimAtPath("/World/Box/Body")),
        "answer": ["path", "folder", "assets", "typo", "spelling", "wrong file", "cannot find", "can't find"],
        "hints": ["Compare the path inside the reference with the folders next to root.usda.",
                  "usd_composition / scene_explain shows 'could not open asset' for this prim."],
        "cause": "The reference points to ./asset/box.usda, but the folder is called assets.",
        "fix": "Change the reference to @./assets/box.usda@."},
    "variant_not_selected": {
        "level": 2, "topic": "Variants: selection",
        "goal": "/World/Ball should be BIG (radius 2): the 'big' option of its size variant set.",
        "files": {
            "root.usda": HEADER + 'def Xform "World"\n{\n    def Sphere "Ball" (\n        prepend variantSets = '
                                  '"size"\n    )\n    {\n        variantSet "size" = {\n            "big" {\n'
                                  '                double radius = 2\n            }\n            "small" {\n'
                                  '                double radius = 0.5\n            }\n        }\n    }\n}\n',
        },
        "check": lambda stage: stage.GetPrimAtPath("/World/Ball").GetAttribute("radius").Get() == 2,
        "answer": ["select", "selection", "selected", "variants ="],
        "hints": ["The 'big' option exists. Is it switched on?",
                  "A variant set needs a selection in the prim's metadata: variants = { string size = ... }."],
        "cause": "The variant set has no selection, so neither option applies and the radius stays at its "
                 "fallback (1).",
        "fix": 'Add variants = { string size = "big" } to the Ball\'s metadata (next to variantSets).'},
    "local_beats_variant": {
        "level": 3, "topic": "LIVERPS: local opinions beat variants",
        "goal": "/World/Light should have intensity 5, the value in the asset's 'bright' variant (selected).",
        "files": {
            "root.usda": HEADER + 'def Xform "World"\n{\n    def Xform "Light" (\n        prepend references = '
                                  '@./light.usda@\n    )\n    {\n    }\n}\n',
            "light.usda": HEADER + '(\n    defaultPrim = "Light"\n)\n\ndef Xform "Light" (\n    variants = {\n'
                                   '        string mode = "bright"\n    }\n    prepend variantSets = "mode"\n)\n{\n'
                                   '    double intensity = 1\n    variantSet "mode" = {\n        "bright" {\n'
                                   '            double intensity = 5\n        }\n    }\n}\n',
        },
        "check": lambda stage: stage.GetPrimAtPath("/World/Light").GetAttribute("intensity").Get() == 5,
        "answer": ["local", "liverps", "stronger", "beats", "outside the variant", "intensity = 1"],
        "hints": ["The 'bright' variant IS selected. So something stronger than a variant must set intensity.",
                  "LIVERPS: L comes before V. Look for intensity written OUTSIDE the variant in light.usda."],
        "cause": "light.usda writes intensity = 1 directly on the prim (a local opinion), which beats the "
                 "variant's 5 (L before V in LIVERPS).",
        "fix": "Remove 'double intensity = 1' from the Light prim body in light.usda (keep it only in variants)."},
    "inherit_typo": {
        "level": 2, "topic": "Inherits: class path",
        "goal": "/World/Pin should be WHITE, from the class /_class_Pin it inherits.",
        "files": {
            "root.usda": HEADER + 'class "_class_Pin"\n{\n    color3f[] primvars:displayColor = [(1, 1, 1)]\n}\n\n'
                                  'def Xform "World"\n{\n    def Cylinder "Pin" (\n        prepend inherits = '
                                  '</_class_Pn>\n    )\n    {\n    }\n}\n',
        },
        "check": lambda stage: _colour(stage, "/World/Pin") == (1.0, 1.0, 1.0),
        "answer": ["typo", "spelling", "path", "_class_pn", "wrong name", "misspel"],
        "hints": ["The class exists and has the colour. Is the Pin really pointing at it?",
                  "Compare the inherits path letter by letter with the class name."],
        "cause": "The Pin inherits </_class_Pn> (missing 'i'), a class that does not exist, so nothing is "
                 "inherited.",
        "fix": "Change the inherits path to </_class_Pin>."},
    "edit_inside_instance": {
        "level": 3, "topic": "Instancing: instance proxies cannot be edited",
        "goal": "Only /World/ChairB's Seat should be RED (ChairA stays grey).",
        "files": {
            "root.usda": HEADER + 'def Xform "World"\n{\n    def Xform "ChairA" (\n        instanceable = true\n'
                                  '        prepend references = @./chair.usda@\n    )\n    {\n    }\n\n'
                                  '    def Xform "ChairB" (\n        instanceable = true\n'
                                  '        prepend references = @./chair.usda@\n    )\n    {\n'
                                  '        over "Seat"\n        {\n'
                                  '            color3f[] primvars:displayColor = [(1, 0, 0)]\n        }\n    }\n}\n',
            "chair.usda": HEADER + '(\n    defaultPrim = "Chair"\n)\n\ndef Xform "Chair"\n{\n    def Cube "Seat"\n'
                                   '    {\n        color3f[] primvars:displayColor = [(0.5, 0.5, 0.5)]\n    }\n}\n',
        },
        "check": lambda stage: _colour(stage, "/World/ChairB/Seat") == (1.0, 0.0, 0.0) and
        _colour(stage, "/World/ChairA/Seat") == (0.5, 0.5, 0.5),
        "answer": ["instanc", "instance proxy", "prototype"],
        "hints": ["The override is written under ChairB. Why would USD ignore opinions there?",
                  "Look at the metadata both chairs share. What does it mean for their children?"],
        "cause": "ChairB is instanceable: its children come from a shared prototype, so opinions authored under "
                 "it (over \"Seat\") are ignored.",
        "fix": "Set instanceable = false on ChairB (ChairA can stay an instance)."},
}


def _folder(base: str | None) -> Path:
    if base:
        return Path(base)
    from .exam.progress import workspace_root

    return workspace_root() / "isaac_study" / "usd_bugs"


def list_puzzles() -> dict[str, Any]:
    """Read-only: the bug-hunt puzzles, easiest first, with their exam topic and goal (never the answer)."""
    rows = [{"id": k, "level": v["level"], "topic": v["topic"], "goal": v["goal"]}
            for k, v in sorted(PUZZLES.items(), key=lambda item: (item[1]["level"], item[0]))]
    return {"ok": True, "read_only": True, "puzzles": rows,
            "how": "usd_bug_make(id) writes the files; open root.usda, find the bug, fix the file, then "
                   "usd_bug_verify(id). usd_bug_answer(id, answer) grades your explanation."}


def make_puzzle(puzzle_id: str, folder: str | None = None, *, dry_run: bool = True) -> dict[str, Any]:
    """Write a puzzle's .usda files into a NEW folder isaac_study/usd_bugs/<id>/ (never overwrites)."""
    spec = PUZZLES.get(puzzle_id)
    if spec is None:
        return {"ok": False, "error": f"Unknown puzzle {puzzle_id!r}; usd_bug_list shows them."}
    target = _folder(folder) / puzzle_id
    plan = {"ok": True, "dry_run": dry_run is not False, "puzzle": puzzle_id, "folder": str(target),
            "files": sorted(spec["files"]), "goal": spec["goal"], "topic": spec["topic"],
            "open": str(target / "root.usda")}
    if target.exists():
        return {**plan, "ok": False, "error": f"{target} already exists: fix it, verify it, or delete the folder "
                                              "to start over."}
    if plan["dry_run"]:
        plan["next_step"] = "Dry run: nothing written. Call again with dry_run=False."
        return plan
    for name, text in spec["files"].items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    plan["next_step"] = ("Open root.usda (File > Open) or read the files, find why the goal is not met, fix the "
                         f"file, then usd_bug_verify('{puzzle_id}').")
    return plan


def verify_puzzle(puzzle_id: str, folder: str | None = None) -> dict[str, Any]:
    """Read-only: open the puzzle's root.usda as it is on disk now and say whether the goal holds."""
    spec = PUZZLES.get(puzzle_id)
    if spec is None:
        return {"ok": False, "error": f"Unknown puzzle {puzzle_id!r}."}
    if Usd is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    root = _folder(folder) / puzzle_id / "root.usda"
    if not root.is_file():
        return {"ok": False, "error": f"No {root}: usd_bug_make('{puzzle_id}', dry_run=False) first."}
    try:
        stage = Usd.Stage.Open(str(root), Usd.Stage.LoadAll)
    except Exception as exc:  # noqa: BLE001 - a file the learner broke while editing
        return {"ok": True, "read_only": True, "solved": False,
                "message": f"USD cannot open the files now: {str(exc).strip()[:300]}. Check the brackets and quotes "
                           "you edited."}
    try:
        solved = bool(spec["check"](stage))
    except Exception:  # noqa: BLE001 - e.g. the prim was deleted while fixing
        solved = False
    return {"ok": True, "read_only": True, "puzzle": puzzle_id, "goal": spec["goal"], "solved": solved,
            "message": "Solved! The goal holds now. Explain why with usd_bug_answer to lock it in." if solved else
                       "Not yet: the goal does not hold. usd_bug_answer(id, hint=True) gives a hint."}


def check_answer(puzzle_id: str, answer: str = "", *, hint: bool = False, reveal: bool = False) -> dict[str, Any]:
    """Grade an explanation of a puzzle's bug: right / not yet; hints with hint=True; the answer with reveal=True."""
    spec = PUZZLES.get(puzzle_id)
    if spec is None:
        return {"ok": False, "error": f"Unknown puzzle {puzzle_id!r}."}
    if reveal:
        return {"ok": True, "puzzle": puzzle_id, "cause": spec["cause"], "fix": spec["fix"]}
    if hint and not answer:
        return {"ok": True, "puzzle": puzzle_id, "hints": spec["hints"]}
    if not isinstance(answer, str) or not answer.strip():
        return {"ok": False, "error": "Explain what you think is wrong (or pass hint=True)."}
    text = answer.lower()
    if any(word in text for word in spec["answer"]):
        return {"ok": True, "puzzle": puzzle_id, "correct": True, "cause": spec["cause"], "fix": spec["fix"],
                "topic": spec["topic"], "next": "Fix the file and run usd_bug_verify to see the goal hold."}
    return {"ok": True, "puzzle": puzzle_id, "correct": False, "feedback": "Not yet.", "hint": spec["hints"][0],
            "next": "Try again, or usd_bug_answer(id, hint=True) for both hints."}
