"""Original NCP-OUSD practice question bank: loading, quizzes, grading and USD-backed answer checks."""

from __future__ import annotations

import copy
import json
import math
import random
import re
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).with_name("data") / "questions.json"

# Exam weights (percent) and the number of objectives per domain in the study guide.
DOMAINS: dict[str, dict[str, Any]] = {
    "Composition": {"number": 1, "weight": 23, "objectives": 11, "prefix": "comp"},
    "Content Aggregation": {"number": 2, "weight": 10, "objectives": 5, "prefix": "ca"},
    "Customizing USD": {"number": 3, "weight": 6, "objectives": 8, "prefix": "cust"},
    "Data Exchange": {"number": 4, "weight": 15, "objectives": 8, "prefix": "dx"},
    "Data Modeling": {"number": 5, "weight": 13, "objectives": 6, "prefix": "dm"},
    "Debugging and Troubleshooting": {"number": 6, "weight": 11, "objectives": 5, "prefix": "dbg"},
    "Pipeline Development": {"number": 7, "weight": 14, "objectives": 8, "prefix": "pipe"},
    "Visualization": {"number": 8, "weight": 8, "objectives": 4, "prefix": "vis"},
}
OBJECTIVE_IDS = frozenset(
    f"{info['number']}.{index}" for info in DOMAINS.values() for index in range(1, info["objectives"] + 1)
)
TYPES = ("single", "multi")
DIFFICULTIES = ("easy", "medium", "hard")
CITATION_PREFIXES = ("https://openusd.org/", "https://docs.nvidia.com/learn-openusd/", "LearnOpenUSD-main/docs/")
SELECT_WORDS = {2: "TWO", 3: "THREE", 4: "FOUR"}
LETTERS = "ABCDEF"
MAX_QUIZ = 50
MAX_ARRAY = 200
MAX_DIAGNOSTICS = 20

_PRIM_KEYS = frozenset({
    "exists", "type", "kind", "active", "instance", "is_model", "specifier", "defined", "loaded",
    "variants", "children", "api_schemas", "computed_visibility", "computed_purpose", "world_translation",
    "computed_extent", "bound_material", "material_purpose", "custom_data", "asset_info", "metadata",
    "inherited_primvar", "shares_prototype_with", "authored_attributes", "has_attributes",
})
_PROPERTY_KEYS = frozenset({
    "exists", "value", "targets", "connections", "type_name", "namespace", "base_name", "custom",
    "interpolation", "length", "resolve_source", "authored", "variability",
})
_STAGE_KEYS = frozenset({
    "default_prim", "up_axis", "meters_per_unit", "time_codes_per_second", "start_time_code",
    "end_time_code", "prototype_count", "traverse", "traverse_all", "traverse_instance_proxies",
    "composition_errors",
})
_CHECK_KEYS = frozenset({"path", "expect", "time"})
_SCENARIO_KEYS = frozenset({"checks", "usda", "root", "load", "mute", "flatten", "note"})
_VERIFY_KEYS = _SCENARIO_KEYS | {"scenarios"}


# ---------------------------------------------------------------- loading and validation


def validate_item(item: Any) -> list[str]:
    """Return schema problems for one question item (empty when valid)."""
    if not isinstance(item, dict):
        return ["item is not an object"]
    qid = item.get("id", "?")
    errors: list[str] = []

    def bad(message: str) -> None:
        errors.append(f"{qid}: {message}")

    for key, kind in (("id", str), ("domain", str), ("type", str), ("stem", str), ("difficulty", str),
                      ("objective_ids", list), ("options", list), ("answer", list),
                      ("explanations", list), ("citations", list)):
        if not isinstance(item.get(key), kind):
            bad(f"'{key}' must be a {kind.__name__}")
    if errors:
        return errors
    info = DOMAINS.get(item["domain"])
    if info is None:
        bad(f"unknown domain {item['domain']!r}")
        return errors
    if not re.fullmatch(rf"{info['prefix']}-\d{{3}}", qid):
        bad(f"id must look like '{info['prefix']}-001'")
    objectives = item["objective_ids"]
    if not objectives or any(o not in OBJECTIVE_IDS for o in objectives):
        bad(f"objective_ids must be known study-guide numbers, got {objectives}")
    elif not any(o.split(".")[0] == str(info["number"]) for o in objectives):
        bad(f"objective_ids need at least one '{info['number']}.x' objective")
    if item["type"] not in TYPES:
        bad(f"type must be one of {TYPES}")
    if item["difficulty"] not in DIFFICULTIES:
        bad(f"difficulty must be one of {DIFFICULTIES}")
    if not item["stem"].strip():
        bad("empty stem")
    options = item["options"]
    if not 4 <= len(options) <= 6 or not all(isinstance(o, str) and o.strip() for o in options):
        bad("need 4-6 non-empty string options")
    elif len(set(options)) != len(options):
        bad("duplicate options")
    answer = item["answer"]
    if not answer or not all(isinstance(a, int) and not isinstance(a, bool) for a in answer):
        bad("answer must be a non-empty list of option indices")
    elif len(set(answer)) != len(answer) or any(a < 0 or a >= len(options) for a in answer):
        bad(f"answer indices out of range or repeated: {answer}")
    elif item["type"] == "single" and len(answer) != 1:
        bad("single questions need exactly one answer")
    elif item["type"] == "multi":
        if not 2 <= len(answer) < len(options):
            bad("multi questions need at least two answers and at least one wrong option")
        elif f"Select {SELECT_WORDS.get(len(answer), len(answer))}" not in item["stem"]:
            bad(f"multi stem must say 'Select {SELECT_WORDS.get(len(answer), len(answer))}'")
    explanations = item["explanations"]
    if len(explanations) != len(options) or not all(isinstance(e, str) and e.strip() for e in explanations):
        bad("need one non-empty explanation per option")
    citations = item["citations"]
    if not citations or not all(isinstance(c, str) and c.startswith(CITATION_PREFIXES) for c in citations):
        bad("citations must be openusd.org / learn-openusd URLs or LearnOpenUSD-main/docs paths")
    usda = item.get("usda")
    if usda is not None:
        errors.extend(f"{qid}: {e}" for e in _layer_problems(usda))
        root = item.get("root")
        if root is not None and isinstance(usda, dict) and root not in usda:
            bad(f"root {root!r} is not one of the layers")
        if root is None and isinstance(usda, dict) and len(usda) > 1:
            bad("multi-layer usda needs a 'root'")
        if "verify" not in item:
            bad("questions with USDA must carry a verify block")
    verify = item.get("verify")
    if verify is not None:
        layers = usda if isinstance(usda, dict) else {}
        errors.extend(f"{qid}: {e}" for e in _validate_verify(verify, layers, item.get("root")))
    return errors


def _layer_problems(layers: Any) -> list[str]:
    if not isinstance(layers, dict) or not layers:
        return ["usda must be a non-empty {layer name: text} object"]
    errors = []
    for name, text in layers.items():
        if not _safe_layer_name(name):
            errors.append(f"bad layer name {name!r}")
        if not isinstance(text, str) or not text.startswith("#usda 1.0"):
            errors.append(f"layer {name!r} must start with '#usda 1.0'")
    return errors


def _validate_verify(verify: Any, layers: dict, root: Any) -> list[str]:
    if not isinstance(verify, dict):
        return ["verify must be an object"]
    errors = _validate_block(verify, layers, root, _VERIFY_KEYS)
    scenarios = verify.get("scenarios", [])
    if not isinstance(scenarios, list):
        return errors + ["verify.scenarios must be a list"]
    for number, scenario in enumerate(scenarios, 1):
        if not isinstance(scenario, dict):
            errors.append(f"scenario {number} must be an object")
            continue
        errors.extend(f"scenario {number}: {e}" for e in _validate_block(scenario, layers, root, _SCENARIO_KEYS))
    return errors


def _validate_block(verify: dict, layers: dict, root: Any, allowed_keys: frozenset) -> list[str]:
    errors = [f"unknown verify key {k!r}" for k in verify if k not in allowed_keys]
    extra = verify.get("usda")
    if extra is not None:
        errors.extend(f"verify {e}" for e in _layer_problems(extra))
    merged = {**layers, **(extra if isinstance(extra, dict) else {})}
    root = verify.get("root") or root
    if not merged:
        errors.append("verify needs usda layers")
    elif root is None and len(merged) > 1:
        errors.append("verify needs a root layer")
    elif root is not None and root not in merged:
        errors.append(f"verify root {root!r} is not one of the layers")
    if verify.get("load", "all") not in ("all", "none"):
        errors.append("verify.load must be 'all' or 'none'")
    mute = verify.get("mute", [])
    if not isinstance(mute, list) or any(name not in merged for name in mute):
        errors.append("verify.mute must list layers of this question")
    checks = verify.get("checks")
    if not isinstance(checks, list) or not checks:
        return errors + ["verify.checks must be a non-empty list"]
    for check in checks:
        path = check.get("path") if isinstance(check, dict) else None
        if not isinstance(path, str) or not path.startswith("/") or "expect" not in check:
            errors.append(f"bad check {check!r}")
            continue
        errors.extend(f"check {path}: unknown check key {k!r}" for k in check if k not in _CHECK_KEYS)
        if check.get("time") is not None and not isinstance(check["time"], (int, float)):
            errors.append(f"check {path}: time must be a number")
        expect = check["expect"]
        if isinstance(expect, dict):
            allowed = _STAGE_KEYS if path == "/" else _PROPERTY_KEYS if _is_property_path(path) else _PRIM_KEYS
            errors.extend(f"check {path}: unknown key {k!r}" for k in expect if k not in allowed)
        elif path == "/" or (not _is_property_path(path) and not isinstance(expect, bool)):
            errors.append(f"check {path}: prim and stage checks need a dict (or bool for prim existence)")
    return errors


def _safe_layer_name(name: Any) -> bool:
    if not isinstance(name, str) or not name.endswith(".usda"):
        return False
    parts = Path(name).parts
    return bool(parts) and not Path(name).is_absolute() and ".." not in parts and ":" not in name


def _is_property_path(path: str) -> bool:
    return "." in path.rsplit("/", 1)[-1]


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict, ...]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    items = data["questions"] if isinstance(data, dict) else data
    errors = [e for item in items for e in validate_item(item)]
    ids = [item.get("id") for item in items]
    errors.extend(f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1}))
    if errors:
        raise ValueError("invalid question bank:\n" + "\n".join(errors[:40]))
    return tuple(items)


def load_bank(path: str | Path | None = None) -> list[dict]:
    """Return a validated copy of every question (cached per file)."""
    return copy.deepcopy(list(_load(str(Path(path or DATA_PATH).resolve()))))


@lru_cache(maxsize=1)
def _index() -> dict[str, dict]:
    return {item["id"]: item for item in _load(str(DATA_PATH.resolve()))}


# ---------------------------------------------------------------- quiz, grading, stats


def _match_domain(value: str) -> str:
    wanted = value.strip().lower()
    for name, info in DOMAINS.items():
        if wanted in (name.lower(), info["prefix"], str(info["number"])):
            return name
    for name in DOMAINS:
        if name.lower().startswith(wanted):
            return name
    raise ValueError(f"unknown domain {value!r}; choose one of {sorted(DOMAINS)}")


def public_view(item: dict) -> dict:
    """A question as a candidate sees it: no answers, explanations, citations or verify data."""
    view = {
        "id": item["id"],
        "domain": item["domain"],
        "objective_ids": list(item["objective_ids"]),
        "type": item["type"],
        "difficulty": item["difficulty"],
        "stem": item["stem"],
        "options": [f"{LETTERS[i]}. {text}" for i, text in enumerate(item["options"])],
    }
    if item["type"] == "multi":
        view["select"] = len(item["answer"])
    if item.get("usda"):
        view["usda"] = dict(item["usda"])
        view["root"] = item.get("root") or next(iter(item["usda"]))
    return view


def quiz(
    domain: str | None = None,
    objective: str | None = None,
    count: int = 5,
    seed: int | None = None,
    exclude_ids: list[str] | None = None,
    prefer_ids: list[str] | None = None,
) -> dict:
    """Pick questions (answers withheld), optionally filtered by domain or objective."""
    domain_name = _match_domain(domain) if domain else None
    if objective is not None and str(objective) not in OBJECTIVE_IDS:
        raise ValueError(f"unknown objective {objective!r}; use study-guide numbers such as '1.6'")
    count = max(1, min(int(count), MAX_QUIZ))
    excluded = set(exclude_ids or [])
    pool = [
        item for item in _index().values()
        if item["id"] not in excluded
        and (domain_name is None or item["domain"] == domain_name)
        and (objective is None or str(objective) in item["objective_ids"])
    ]
    by_id = {item["id"]: item for item in pool}
    chosen = [by_id[i] for i in dict.fromkeys(prefer_ids or []) if i in by_id][:count]
    rest = [item for item in pool if item not in chosen]
    random.Random(seed).shuffle(rest)
    chosen.extend(rest[: count - len(chosen)])
    return {
        "filters": {"domain": domain_name, "objective": objective, "excluded": len(excluded)},
        "available": len(pool),
        "count": len(chosen),
        "questions": [public_view(item) for item in chosen],
    }


def _coerce_choice(choice: Any, option_count: int) -> int:
    if isinstance(choice, str) and len(choice.strip()) == 1 and choice.strip().upper() in LETTERS:
        index = LETTERS.index(choice.strip().upper())
    elif isinstance(choice, int) and not isinstance(choice, bool):
        index = choice
    elif isinstance(choice, str) and choice.strip().isdigit():
        index = int(choice.strip())
    else:
        raise ValueError(f"choice {choice!r} must be an option index (0-based) or letter")
    if not 0 <= index < option_count:
        raise ValueError(f"choice {choice!r} is out of range for {option_count} options")
    return index


def grade(question_id: str, chosen: list[int]) -> dict:
    """Grade one answer; multi-select questions need the exact set of correct options."""
    item = _index().get(question_id)
    if item is None:
        raise ValueError(f"unknown question id {question_id!r}")
    if isinstance(chosen, (int, str)):
        chosen = [chosen]
    if not isinstance(chosen, (list, tuple, set)):
        raise ValueError("chosen must be a list of option indices or letters")
    picked = sorted({_coerce_choice(c, len(item["options"])) for c in chosen})
    answer = sorted(item["answer"])
    return {
        "question_id": item["id"],
        "domain": item["domain"],
        "objective_ids": list(item["objective_ids"]),
        "type": item["type"],
        "correct": picked == answer,
        "chosen": picked,
        "correct_answer": answer,
        "correct_letters": [LETTERS[i] for i in answer],
        "explanations": [
            {
                "index": i,
                "letter": LETTERS[i],
                "option": text,
                "is_correct": i in answer,
                "chosen": i in picked,
                "why": item["explanations"][i],
            }
            for i, text in enumerate(item["options"])
        ],
        "citations": list(item["citations"]),
    }


def stats() -> dict:
    """Counts per domain, type, difficulty and objective, with exam weights for comparison."""
    items = list(_index().values())
    by_domain = {name: 0 for name in DOMAINS}
    by_type = {t: 0 for t in TYPES}
    by_difficulty = {d: 0 for d in DIFFICULTIES}
    by_objective: dict[str, int] = {}
    for item in items:
        by_domain[item["domain"]] += 1
        by_type[item["type"]] += 1
        by_difficulty[item["difficulty"]] += 1
        for objective in item["objective_ids"]:
            by_objective[objective] = by_objective.get(objective, 0) + 1
    return {
        "total": len(items),
        "by_domain": by_domain,
        "weights": {name: info["weight"] for name, info in DOMAINS.items()},
        "by_type": by_type,
        "by_difficulty": by_difficulty,
        "with_usda": sum(1 for item in items if item.get("usda")),
        "with_verify": sum(1 for item in items if item.get("verify")),
        "by_objective": dict(sorted(by_objective.items(), key=lambda kv: _objective_key(kv[0]))),
        "uncovered_objectives": sorted(OBJECTIVE_IDS - set(by_objective), key=_objective_key),
    }


def _objective_key(objective: str) -> list[int]:
    return [int(part) for part in objective.split(".")]


# ---------------------------------------------------------------- verification with pxr


def verify_question(item: dict | str) -> dict:
    """Compose the item's USDA in a temp dir and run its verify checks; never touches user files."""
    if isinstance(item, str):
        found = _index().get(item)
        if found is None:
            raise ValueError(f"unknown question id {item!r}")
        item = found
    qid = item.get("id")
    block = item.get("verify")
    if not block:
        return {"id": qid, "passed": None, "skipped": "no verify block"}
    problems = _validate_verify(block, item.get("usda") or {}, item.get("root"))
    if problems:
        return {"id": qid, "passed": False, "errors": problems}
    try:
        from pxr import Usd  # noqa: F401
    except ImportError:
        return {"id": qid, "passed": None, "skipped": "pxr (usd-core) is not importable"}

    base_layers = item.get("usda") or {}
    runs = [_run_block(block, base_layers, item.get("root"), "main")]
    runs.extend(
        _run_block(scenario, base_layers, item.get("root"), scenario.get("note") or f"scenario {number}")
        for number, scenario in enumerate(block.get("scenarios", []), 1)
    )
    checks = [check for run in runs for check in run["checks"]]
    errors = [f"{run['label']}: {e}" for run in runs for e in run["errors"]]
    diagnostics = [f"{run['label']}: {d}" for run in runs for d in run["diagnostics"]][:MAX_DIAGNOSTICS]
    return {
        "id": qid,
        "passed": not errors and all(c["passed"] for c in checks),
        "checks": checks,
        "errors": errors,
        "diagnostics": diagnostics,
    }


def _run_block(block: dict, base_layers: dict, item_root: str | None, label: str) -> dict:
    from pxr import Usd, UsdUtils

    layers = {**base_layers, **(block.get("usda") or {})}
    root = block.get("root") or item_root or next(iter(layers))
    results: list[dict] = []
    errors: list[str] = []
    with tempfile.TemporaryDirectory(prefix="ousd_bank_", ignore_cleanup_errors=True) as tmp:
        base = Path(tmp)
        for name, text in layers.items():
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        stage = None
        delegate = UsdUtils.CoalescingDiagnosticDelegate()  # keep composition warnings off stderr
        try:
            load = Usd.Stage.LoadNone if block.get("load") == "none" else Usd.Stage.LoadAll
            stage = Usd.Stage.Open(str(base / root), load)
            if stage is None:
                raise RuntimeError(f"could not open {root}")
            for name in block.get("mute", []):
                wanted = (base / name).resolve()
                identifiers = [
                    layer.identifier for layer in stage.GetUsedLayers()
                    if layer.realPath and Path(layer.realPath).resolve() == wanted
                ]
                if not identifiers:
                    raise RuntimeError(f"layer {name} to mute is not used by the stage")
                stage.MuteLayer(identifiers[0])
            if block.get("flatten"):
                stage = Usd.Stage.Open(stage.Flatten())
            for check in block["checks"]:
                results.append({"scenario": label, **_run_check(stage, check)})
        except Exception as exc:  # noqa: BLE001 - report composition failures as data
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            del stage
            captured = delegate.TakeUncoalescedDiagnostics()
            del delegate
        diagnostics = [_redact(d.commentary, base) for d in captured[:MAX_DIAGNOSTICS]]
    return {"label": label, "checks": results, "errors": [_redact(e, base) for e in errors], "diagnostics": diagnostics}


def _redact(text: str, base: Path) -> str:
    for form in {str(base), base.as_posix(), str(base.resolve()), base.resolve().as_posix()}:
        text = text.replace(form, "<tmp>")
    return text


def _run_check(stage: Any, check: dict) -> dict:
    from pxr import Usd

    path = check["path"]
    expect = check["expect"]
    time = Usd.TimeCode(check["time"]) if check.get("time") is not None else Usd.TimeCode.Default()
    try:
        if path == "/":
            actual = {key: _stage_fact(stage, key) for key in expect}
        elif _is_property_path(path):
            actual = _property_facts(stage, path, expect, time)
        else:
            spec = {"exists": expect} if isinstance(expect, bool) else expect
            actual = _prim_facts(stage, path, spec, time)
            if isinstance(expect, bool):
                actual = actual["exists"]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        return {"path": path, "expect": expect, "actual": None, "passed": False, "error": error}
    return {"path": path, "expect": expect, "actual": actual, "passed": _same(actual, expect)}


def _stage_fact(stage: Any, key: str) -> Any:
    from pxr import Usd, UsdGeom

    if key == "default_prim":
        prim = stage.GetDefaultPrim()
        return prim.GetName() if prim else None
    if key == "up_axis":
        return str(UsdGeom.GetStageUpAxis(stage))
    if key == "meters_per_unit":
        return UsdGeom.GetStageMetersPerUnit(stage)
    if key == "time_codes_per_second":
        return stage.GetTimeCodesPerSecond()
    if key == "start_time_code":
        return stage.GetStartTimeCode()
    if key == "end_time_code":
        return stage.GetEndTimeCode()
    if key == "prototype_count":
        return len(stage.GetPrototypes())
    if key == "traverse":
        return [str(p.GetPath()) for p in stage.Traverse()][:MAX_ARRAY]
    if key == "traverse_all":
        return [str(p.GetPath()) for p in stage.TraverseAll()][:MAX_ARRAY]
    if key == "traverse_instance_proxies":
        return [str(p.GetPath()) for p in stage.Traverse(Usd.TraverseInstanceProxies())][:MAX_ARRAY]
    if key == "composition_errors":
        return bool(stage.GetCompositionErrors())
    raise KeyError(key)


def _property_facts(stage: Any, path: str, expect: Any, time: Any) -> Any:
    from pxr import Sdf, Usd, UsdGeom

    sdf_path = Sdf.Path(path)
    prim = stage.GetPrimAtPath(sdf_path.GetPrimPath())
    prop = prim.GetProperty(sdf_path.name) if prim else None
    valid = bool(prop) and prop.IsValid()
    is_attr = valid and isinstance(prop, Usd.Attribute)

    def value() -> Any:
        if not valid:
            return None
        return _plain(prop.Get(time)) if is_attr else [str(t) for t in prop.GetTargets()]

    if not isinstance(expect, dict):
        return value()
    facts: dict[str, Any] = {}
    for key in expect:
        if key == "exists":
            facts[key] = valid
        elif not valid:
            facts[key] = None
        elif key == "value":
            facts[key] = value()
        elif key == "targets":
            facts[key] = [str(t) for t in prop.GetTargets()] if not is_attr else None
        elif key == "connections":
            facts[key] = [str(t) for t in prop.GetConnections()] if is_attr else None
        elif key == "type_name":
            facts[key] = str(prop.GetTypeName()) if is_attr else "rel"
        elif key == "namespace":
            facts[key] = prop.GetNamespace()
        elif key == "base_name":
            facts[key] = prop.GetBaseName()
        elif key == "custom":
            facts[key] = prop.IsCustom()
        elif key == "interpolation":
            facts[key] = str(UsdGeom.Primvar(prop).GetInterpolation()) if is_attr else None
        elif key == "length":
            raw = prop.Get(time) if is_attr else prop.GetTargets()
            facts[key] = len(raw) if raw is not None else None
        elif key == "resolve_source":
            facts[key] = _source_name(prop.GetResolveInfo(time).GetSource()) if is_attr else None
        elif key == "authored":
            facts[key] = prop.HasAuthoredValue() if is_attr else prop.HasAuthoredTargets()
        elif key == "variability":
            facts[key] = "uniform" if prop.GetVariability() == Sdf.VariabilityUniform else "varying"
    return facts


def _source_name(source: Any) -> str:
    name = str(source).rsplit(".", 1)[-1].replace("ResolveInfoSource", "")
    return {"TimeSamples": "time_samples", "ValueClips": "value_clips"}.get(name, name.lower())


def _prim_facts(stage: Any, path: str, expect: dict, time: Any) -> dict:
    from pxr import Sdf, Usd, UsdGeom, UsdShade

    prim = stage.GetPrimAtPath(path)
    valid = bool(prim) and prim.IsValid()
    facts: dict[str, Any] = {}
    for key in expect:
        if key == "exists":
            facts[key] = valid
        elif key == "material_purpose":
            facts[key] = expect[key]
        elif not valid:
            facts[key] = None
        elif key == "type":
            facts[key] = str(prim.GetTypeName())
        elif key == "kind":
            facts[key] = str(Usd.ModelAPI(prim).GetKind())
        elif key == "active":
            facts[key] = prim.IsActive()
        elif key == "instance":
            facts[key] = prim.IsInstance()
        elif key == "is_model":
            facts[key] = prim.IsModel()
        elif key == "specifier":
            names = {Sdf.SpecifierDef: "def", Sdf.SpecifierOver: "over", Sdf.SpecifierClass: "class"}
            facts[key] = names[prim.GetSpecifier()]
        elif key == "defined":
            facts[key] = prim.IsDefined()
        elif key == "loaded":
            facts[key] = prim.IsLoaded()
        elif key == "variants":
            facts[key] = {name: prim.GetVariantSet(name).GetVariantSelection() for name in expect[key]}
        elif key == "children":
            facts[key] = [str(name) for name in prim.GetChildrenNames()][:MAX_ARRAY]
        elif key == "api_schemas":
            applied = set(prim.GetAppliedSchemas())
            facts[key] = [name for name in expect[key] if name in applied]
        elif key == "computed_visibility":
            facts[key] = str(UsdGeom.Imageable(prim).ComputeVisibility(time))
        elif key == "computed_purpose":
            facts[key] = str(UsdGeom.Imageable(prim).ComputePurpose())
        elif key == "world_translation":
            matrix = UsdGeom.XformCache(time).GetLocalToWorldTransform(prim)
            facts[key] = _plain(matrix.ExtractTranslation())
        elif key == "computed_extent":
            facts[key] = _plain(UsdGeom.Boundable.ComputeExtentFromPlugins(UsdGeom.Boundable(prim), time))
        elif key == "bound_material":
            purpose = expect.get("material_purpose") or UsdShade.Tokens.allPurpose
            material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(purpose)[0]
            facts[key] = str(material.GetPath()) if material else None
        elif key == "custom_data":
            facts[key] = {name: _plain(prim.GetCustomDataByKey(name)) for name in expect[key]}
        elif key == "asset_info":
            facts[key] = {name: _plain(prim.GetAssetInfoByKey(name)) for name in expect[key]}
        elif key == "metadata":
            facts[key] = {name: _plain(prim.GetMetadata(name)) for name in expect[key]}
        elif key == "inherited_primvar":
            api = UsdGeom.PrimvarsAPI(prim)
            facts[key] = {}
            for name in expect[key]:
                primvar = api.FindPrimvarWithInheritance(name)
                facts[key][name] = _plain(primvar.Get(time)) if primvar else None
        elif key == "authored_attributes":
            facts[key] = sorted(a.GetName() for a in prim.GetAuthoredAttributes())[:MAX_ARRAY]
        elif key == "has_attributes":
            names = set(prim.GetPropertyNames())
            facts[key] = [name for name in expect[key] if name in names]
        elif key == "shares_prototype_with":
            facts[key] = {}
            for other_path in expect[key]:
                other = stage.GetPrimAtPath(other_path)
                facts[key][other_path] = bool(other) and prim.IsInstance() and other.IsInstance() and (
                    prim.GetPrototype() == other.GetPrototype()
                )
    return facts


def _plain(value: Any, cap: int = MAX_ARRAY) -> Any:
    """Convert Gf/Vt/Sdf values to JSON-friendly Python values (arrays capped)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v, cap) for k, v in value.items()}
    if hasattr(value, "pathString"):
        return str(value)
    if hasattr(value, "resolvedPath") and hasattr(value, "path"):
        return value.path
    if hasattr(value, "GetReal") and hasattr(value, "GetImaginary"):
        return [_plain(value.GetReal()), *_plain(value.GetImaginary())]
    if hasattr(value, "GetMin") and hasattr(value, "GetMax"):
        return [_plain(value.GetMin()), _plain(value.GetMax())]
    try:
        items = list(value)
    except TypeError:
        return str(value)
    return [_plain(v, cap) for v in items[:cap]]


def _same(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return actual is expected if isinstance(expected, bool) else False
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return math.isclose(actual, expected, rel_tol=1e-5, abs_tol=1e-6)
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            _same(a, e) for a, e in zip(actual, expected)
        )
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(_same(actual.get(k), v) for k, v in expected.items())
    return actual == expected
