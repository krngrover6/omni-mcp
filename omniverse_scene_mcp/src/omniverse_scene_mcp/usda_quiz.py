"""usda_quiz / usda_quiz_answer: exam-style questions made from the user's OWN scene files, graded by real USD.

Questions come from what USD composes for the file, so every answer key is a fact, not a guess:
- stage settings: up axis and units (exam habit: read upAxis, never assume),
- full paths of prims with a unique name,
- fallback values: a schema attribute with no opinion anywhere (e.g. a Cube's size when nothing is written),
- which layer wins: an attribute with opinions in more than one layer (strongest first, LIVERPS / layer order).
No state is stored: a question id is "<seed>:<number>", and usda_quiz_answer rebuilds the same quiz to grade it.
"""
from __future__ import annotations

import datetime as _dt
import random
import re
from pathlib import Path
from typing import Any

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover
    Sdf = Usd = UsdGeom = None

MAX_PRIMS = 5000
_SKIP_ROOTS = ("/Render", "/OmniverseKit", "/Environment")
# Attributes worth asking about (beginner-level, schema-backed); others are noise like extents or normals.
_FALLBACK_ATTRS = {"Cube": ["size"], "Sphere": ["radius"], "Cylinder": ["radius", "height", "axis"],
                   "Cone": ["radius", "height", "axis"], "Capsule": ["radius", "height", "axis"],
                   "Xform": [], "Mesh": ["subdivisionScheme", "orientation"], "Camera": ["focalLength", "projection"],
                   "DistantLight": ["inputs:intensity", "inputs:angle"], "SphereLight": ["inputs:radius",
                                                                                     "inputs:intensity"]}
_COMMON = ["visibility", "purpose"]


def _open(path: Any) -> tuple[Any, str | None]:
    if Usd is None:
        return None, "USD Python bindings (pxr) are not available"
    file = Path(str(path))
    if file.suffix.lower() not in (".usd", ".usda", ".usdc") or not file.is_file():
        return None, f"path must be an existing .usd, .usda or .usdc file: {path}"
    stage = Usd.Stage.Open(str(file), Usd.Stage.LoadAll)
    return (stage, None) if stage else (None, f"USD could not open {path}")


def _prims(stage: Any) -> list[Any]:
    out = []
    for prim in stage.Traverse():
        if len(out) >= MAX_PRIMS:
            break
        if not str(prim.GetPath()).startswith(_SKIP_ROOTS):
            out.append(prim)
    return out


def _layer_name(layer: Any, stage: Any) -> str:
    if layer == stage.GetRootLayer():
        return f"{layer.GetDisplayName()} (the root layer)"
    if layer == stage.GetSessionLayer():
        return "the session layer"
    return layer.GetDisplayName()


def _show(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _questions(stage: Any, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    prims = _prims(stage)
    pool: list[dict[str, Any]] = []
    mpu = UsdGeom.GetStageMetersPerUnit(stage)
    pool.append({"kind": "stage_settings",
                 "question": "Open the file's settings block (the ( ) at the top). Which axis points up, and how many "
                             "metres is one unit?",
                 "answer": {"upAxis": str(UsdGeom.GetStageUpAxis(stage)), "metersPerUnit": mpu},
                 "explain": f"upAxis = {UsdGeom.GetStageUpAxis(stage)} and metersPerUnit = {mpu:g} "
                            f"({'centimetres' if abs(mpu - 0.01) < 1e-9 else 'metres' if mpu == 1 else 'units'}). "
                            "When a file does not say, USD falls back to Y up and centimetres."})
    names: dict[str, list[Any]] = {}
    for prim in prims:
        names.setdefault(prim.GetName(), []).append(prim)
    unique = [p[0] for n, p in names.items() if len(p) == 1 and p[0].GetPath().pathElementCount >= 2]
    for prim in rng.sample(unique, min(3, len(unique))):
        pool.append({"kind": "full_path", "question": f"What is the full path of the prim named `{prim.GetName()}`?",
                     "answer": str(prim.GetPath()),
                     "explain": f"Read the braces from the top: {' > '.join(str(p) for p in prim.GetPath().GetPrefixes())}."
                                " A path always starts with /."})
    fallbacks = []
    for prim in prims:
        for name in _FALLBACK_ATTRS.get(str(prim.GetTypeName()), []) + _COMMON:
            attr = prim.GetAttribute(name)
            if attr and not attr.HasAuthoredValue() and not attr.GetNumTimeSamples():
                fallbacks.append((prim, attr))
    rng.shuffle(fallbacks)
    picked, seen_names = [], set()
    for prim, attr in fallbacks:  # one question per attribute name: not two "visibility" questions
        if attr.GetName() not in seen_names and len(picked) < 2:
            seen_names.add(attr.GetName())
            picked.append((prim, attr))
    for prim, attr in picked:
        value = attr.Get()
        type_name = str(prim.GetTypeName())
        article = "an" if type_name[:1].upper() in "AEIOUX" else "a"
        pool.append({"kind": "fallback",
                     "question": f"No layer writes `{attr.GetName()}` on `{prim.GetPath()}` ({article} {type_name}). "
                                 "What value does USD give it?",
                     "answer": value,
                     "explain": f"{_show(value)}: the {prim.GetTypeName()} schema's fallback, used when no layer "
                                "has an opinion."})
    contested = []
    for prim in prims:
        for attr in prim.GetAttributes():
            stack = [s for s in attr.GetPropertyStack(Usd.TimeCode.Default()) if s.HasDefaultValue()]
            layers = {s.layer.identifier for s in stack}
            if len(layers) >= 2:
                contested.append((prim, attr, stack))
    for prim, attr, stack in rng.sample(contested, min(2, len(contested))):
        winner = stack[0]
        others = ", ".join(f"{_layer_name(s.layer, stage)} = {_show(s.default)}" for s in stack)
        pool.append({"kind": "which_layer_wins",
                     "question": f"`{attr.GetName()}` on `{prim.GetPath()}` has opinions in {len(stack)} layers: "
                                 + "; ".join(f"{_layer_name(s.layer, stage)} says {_show(s.default)}" for s in
                                             sorted(stack, key=lambda s: s.layer.GetDisplayName()))
                                 + ". Which value wins?",
                     "answer": winner.default,
                     "explain": f"{_show(winner.default)}, from {_layer_name(winner.layer, stage)}: the strongest "
                                f"layer wins, per attribute. Strongest first: {others}."})
    rng.shuffle(pool)
    return pool


def _seed(seed: Any) -> int:
    if isinstance(seed, int) and not isinstance(seed, bool):
        return seed
    return int(_dt.date.today().strftime("%Y%m%d"))


def make_quiz(path: str, count: Any = 4, seed: Any = None) -> dict[str, Any]:
    """Exam-style questions from YOUR scene file (a .usda/.usd you made): up axis and units, full paths, fallback
    values, which layer wins. Answers are withheld; answer with usda_quiz_answer(path, question_id, answer). The same
    seed gives the same quiz (default: today's date, so it changes every day)."""
    stage, error = _open(path)
    if error:
        return {"ok": False, "error": error}
    seed = _seed(seed)
    pool = _questions(stage, seed)
    number = max(1, min(10, int(count) if isinstance(count, int) and not isinstance(count, bool) else 4))
    return {"ok": True, "file": str(path), "seed": seed, "count": min(number, len(pool)),
            "questions": [{"id": f"{seed}:{i}", "kind": q["kind"], "question": q["question"]}
                          for i, q in enumerate(pool[:number])],
            "note": "Answer one at a time with usda_quiz_answer (answers come from USD itself)."}


def _matches(expected: Any, given: str) -> bool:
    text = given.strip().strip("`'\"").lower()
    if isinstance(expected, dict):  # stage settings: both parts must be in the answer
        axis_ok = str(expected["upAxis"]).lower() in re.findall(r"\b[xyz]\b", text)
        numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)]
        mpu = float(expected["metersPerUnit"])
        words = {0.01: ("cm", "centimet"), 1.0: ("metre", "meter", " m")}
        unit_ok = any(abs(n - mpu) < 1e-9 for n in numbers) or any(w in f" {text}" for w in words.get(mpu, ()))
        return axis_ok and unit_ok
    if isinstance(expected, bool):
        return text in (("true", "1", "yes") if expected else ("false", "0", "no"))
    if isinstance(expected, (int, float)):
        numbers = re.findall(r"-?\d+(?:\.\d+)?", text)
        return any(abs(float(n) - float(expected)) < 1e-6 for n in numbers)
    if isinstance(expected, str) and expected.startswith("/"):
        return text.rstrip("/") == expected.lower()
    return text == str(expected).strip().lower()


def answer_quiz(path: str, question_id: str, answer: str) -> dict[str, Any]:
    """Grade one usda_quiz question (question_id like "20261126:2") with USD's own answer, and explain it."""
    if not isinstance(question_id, str) or not re.match(r"^-?\d+:\d+$", question_id):
        return {"ok": False, "error": "question_id looks like 20261126:2 (from usda_quiz)"}
    if not isinstance(answer, str) or not answer.strip():
        return {"ok": False, "error": "answer must be text"}
    stage, error = _open(path)
    if error:
        return {"ok": False, "error": error}
    seed, index = (int(x) for x in question_id.split(":"))
    pool = _questions(stage, seed)
    if index >= len(pool):
        return {"ok": False, "error": "No such question in this quiz (did the file change?)"}
    question = pool[index]
    right = _matches(question["answer"], answer)
    return {"ok": True, "question": question["question"], "your_answer": answer, "correct": right,
            "explain": question["explain"],
            "message": "Right!" if right else "Not quite: read the explanation, then try the next one."}
