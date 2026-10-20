"""Deterministic, self-grading LIVERPS puzzles whose answers come from composing the layers with pxr."""

from __future__ import annotations

import random
import re
from typing import Any, Callable

from . import sandbox

DIFFICULTIES = ("easy", "medium", "hard")
_ID_RE = re.compile(r"^liverps-(easy|medium|hard)-(\d{1,9})$")
_POOL = (3, 4, 5, 6, 7, 8, 9, 10, 12, 15, 20, 25, 30, 40, 50)
_SUBJECTS = (
    ("Cube", "Box", "size", 2.0),
    ("Sphere", "Ball", "radius", 1.0),
    ("Cylinder", "Can", "height", 2.0),
    ("Cone", "Marker", "height", 2.0),
)
_SUBLAYER_NAMES = ("layout.usda", "anim.usda", "fx.usda", "light.usda", "set.usda")
_ASSET_NAMES = ("geo.usda", "look.usda", "rig.usda", "model.usda", "proxy.usda")
_VARIANT_NAMES = ("small", "medium", "large", "huge")
_NONE_WORDS = {"none", "no value", "blocked", "null", "value block", "none (value block)"}


def generate_puzzle(seed: int | None = None, difficulty: str = "easy") -> dict[str, Any]:
    """Build a LIVERPS puzzle (layers, question, options, hint) without revealing the answer."""
    difficulty = str(difficulty).strip().lower()
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"difficulty must be one of {', '.join(DIFFICULTIES)}")
    if seed is None:
        seed = random.SystemRandom().randrange(1_000_000)
    seed = int(seed)
    if not 0 <= seed <= 999_999_999:
        raise ValueError("seed must be between 0 and 999999999")
    spec = _build(difficulty, seed)
    return {
        "puzzle_id": f"liverps-{difficulty}-{seed}",
        "difficulty": difficulty,
        "seed": seed,
        "topic": spec["topic"],
        "question": spec["question"],
        "options": spec["options"],
        "hint": spec["hint"],
        "layers": spec["layers"],
        "root": spec["root"],
        "sandbox_query": spec["query"],
    }


def check_puzzle(puzzle_id: str, answer: Any) -> dict[str, Any]:
    """Grade an answer (option text, number or option letter) by composing the puzzle's layers."""
    match = _ID_RE.match(str(puzzle_id).strip())
    if not match:
        return {"ok": False, "correct": False, "error": "puzzle_id looks like 'liverps-medium-1234'"}
    difficulty, seed = match.group(1), int(match.group(2))
    spec = _build(difficulty, seed)
    composed = sandbox.compose(spec["layers"], spec["root"], [spec["query"]])
    query = composed["queries"][0] if composed.get("queries") else {}
    if not composed["ok"] or not query.get("ok"):
        return {
            "ok": False,
            "correct": False,
            "error": query.get("error") or "the puzzle layers could not be composed",
            "errors": composed.get("errors", []),
        }
    correct_answer = sandbox.format_value(query["value"])
    given = _given(answer, spec["options"])
    return {
        "ok": True,
        "puzzle_id": f"liverps-{difficulty}-{seed}",
        "question": spec["question"],
        "your_answer": given,
        "correct": _normalize(given) == _normalize(correct_answer),
        "correct_answer": correct_answer,
        "explanation": _explain(spec, query, correct_answer),
        "why": query["why"],
        "property_stack": query["property_stack"],
        "layers": spec["layers"],
        "root": spec["root"],
    }


def _build(difficulty: str, seed: int) -> dict[str, Any]:
    rng = random.Random(f"liverps:{difficulty}:{seed}")
    spec = rng.choice(_POOLS[difficulty])(rng)
    spec["options"] = _options(rng, spec)
    return spec


def _given(answer: Any, options: list[str]) -> str:
    text = "None" if answer is None else str(answer).strip()
    if len(text) == 1 and text.upper() in "ABCDE":
        index = "ABCDE".index(text.upper())
        if index < len(options):
            return options[index]
    return text


def _normalize(value: str) -> str:
    text = value.strip().lower()
    if text in _NONE_WORDS:
        return "none"
    try:
        return f"{float(text):.6g}"
    except ValueError:
        return text


def _options(rng: random.Random, spec: dict[str, Any]) -> list[str]:
    required: list[Any] = list(dict.fromkeys(spec["values"]))
    if spec["allow_none"]:
        required.append(None)
    if spec["fallback_possible"] and spec["fallback"] not in required:
        required.append(spec["fallback"])
    target = max(3, min(5, len(required) + 1))
    extras = [spec["fallback"]] if spec["fallback"] not in required else []
    extras += [v for v in rng.sample(_POOL, len(_POOL)) if v not in required]
    chosen = required + extras[: max(0, target - len(required))]
    labels = [sandbox.format_value(None if v is None else float(v)) for v in chosen]
    rng.shuffle(labels)
    return labels


def _explain(spec: dict[str, Any], query: dict[str, Any], correct_answer: str) -> str:
    at = "" if spec["time"] is None else f" at time {sandbox.format_value(float(spec['time']))}"
    stack = query["property_stack"]
    lines = [f"Correct answer: {correct_answer}.", f"Opinions on {query['path']}{at}, strongest first:"]
    for number, entry in enumerate(stack, 1):
        mark = "  <- wins" if entry.get("wins") else ""
        lines.append(f"  {number}. {entry['layer']} {entry['path']}: {_opinion(entry, spec['time'])} [{entry['via']}]{mark}")
    if not stack:
        lines.append("  (no authored opinions)")
    lines.append(f"Why: {query['why']}.")
    summary = sandbox.explain_strength([entry["arc"] for entry in stack])["summary"]
    # The summary ranks root-level arcs only. Arcs nested inside a referenced asset are ranked on their own, never
    # merged into the root-level order (the asset's local opinion can beat its inherit even though I beats R).
    inner = [
        "local" if entry["node_arc"] == entry["arc"] else entry["node_arc"]
        for entry in stack
        if entry["arc"] in ("reference", "payload") and entry["node_arc"] != "unknown"
    ]
    if any(arc != "local" for arc in inner):
        summary = (
            f"At the root level: {summary} Inside the referenced asset LIVERPS applies again: "
            f"{' > '.join(sandbox.explain_strength(inner)['order'])}."
        )
    lines.append(summary)
    return "\n".join(lines)


def _opinion(entry: dict[str, Any], time: float | None) -> str:
    parts = []
    if entry["has_default"]:
        parts.append("None (value block)" if entry["blocked"] else sandbox.format_value(entry["value"]))
    if entry["time_samples"]:
        shown = ", ".join(
            f"t={sandbox.format_value(s['stage_time'])}: {sandbox.format_value(s['value'])}" for s in entry["time_samples"]
        )
        ignored = " (ignored at the default time)" if time is None else ""
        parts.append(f"time samples {shown}{ignored}")
    return "; ".join(parts) or "declared without a value"


def _spec(
    topic: str,
    layers: dict[str, str],
    subject: tuple[str, str, str, float],
    values: list[Any],
    hint: str,
    time: float | None = None,
    fallback_possible: bool = False,
    allow_none: bool = False,
) -> dict[str, Any]:
    _kind, name, attr, fallback = subject
    path = f"/World/{name}.{attr}"
    at = "" if time is None else f" at time code {sandbox.format_value(float(time))}"
    return {
        "topic": topic,
        "layers": layers,
        "root": "shot.usda",
        "query": path if time is None else f"{path}@{time}",
        "time": time,
        "question": f"With shot.usda as the root layer, what is the resolved value of {path}{at}?",
        "hint": hint,
        "values": values,
        "fallback": fallback,
        "fallback_possible": fallback_possible,
        "allow_none": allow_none,
    }


def _indent(text: str) -> str:
    return "\n".join(f"    {line}" if line else line for line in text.splitlines())


def _prim(specifier: str, name: str, type_name: str = "", meta: list[str] = (), body: list[str] = ()) -> str:
    head = f'{specifier} {type_name + " " if type_name else ""}"{name}"'
    if meta:
        head += " (\n" + "\n".join(_indent(m) for m in meta) + "\n)"
    inner = "\n".join(body)
    return head + " {\n" + (_indent(inner) + "\n" if inner else "") + "}"


def _layer(*prims: str, meta: list[str] = ()) -> str:
    text = "#usda 1.0\n"
    if meta:
        text += "(\n" + "\n".join(_indent(m) for m in meta) + "\n)\n"
    return text + "\n" + "\n\n".join(prims) + "\n"


def _attr(name: str, value: Any) -> str:
    return f"double {name} = {'None' if value is None else value}"


def _samples(name: str, samples: dict[int, Any]) -> str:
    rows = "\n".join(f"    {time}: {value}," for time, value in samples.items())
    return f"double {name}.timeSamples = {{\n{rows}\n}}"


def _sublayers(names: list[str], offset: str = "") -> str:
    rows = ",\n".join(f"    @{name}@{offset}" for name in names)
    return f"subLayers = [\n{rows}\n]"


def _selection(variant_set: str, selection: str) -> str:
    return f'variants = {{\n    string {variant_set} = "{selection}"\n}}'


def _variant_set(variant_set: str, bodies: dict[str, list[str]]) -> str:
    blocks = "\n".join(_indent(_prim_block(f'"{name}"', body)) for name, body in bodies.items())
    return f'variantSet "{variant_set}" = {{\n{blocks}\n}}'


def _prim_block(head: str, body: list[str]) -> str:
    inner = "\n".join(body)
    return head + " {\n" + (_indent(inner) + "\n" if inner else "") + "}"


def _asset(subject: tuple[str, str, str, float], body: list[str], meta: list[str] = (), extra: list[str] = ()) -> str:
    asset = _prim("def", "Asset", subject[0], meta=list(meta), body=body)
    return _layer(*extra, asset, meta=['defaultPrim = "Asset"'])


def _world(*children: str) -> str:
    return _prim("def", "World", "Xform", body=list(children))


def _sublayer_order(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    kind, name, attr, _ = subject
    count = rng.choice((2, 3))
    subs = rng.sample(_SUBLAYER_NAMES, count)
    values = rng.sample(_POOL, count)
    define = rng.randrange(count)
    layers = {"shot.usda": _layer(_world(), meta=[_sublayers(subs)])}
    for index, (sub, value) in enumerate(zip(subs, values)):
        box = _prim("def" if index == define else "over", name, kind if index == define else "", body=[_attr(attr, value)])
        layers[sub] = _layer(_prim("over", "World", body=[box]))
    hint = "Inside one layer stack the subLayers list sets the strength: the first entry is the strongest."
    return _spec("sublayer order", layers, subject, values, hint)


def _local_vs_reference(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    a, b = rng.sample(_POOL, 2)
    mode = rng.choice(("root", "root", "sublayer", "none"))
    target = rng.choice(("@asset.usda@", "@asset.usda@</Asset>"))
    box = _prim("def", name, meta=[f"prepend references = {target}"], body=[_attr(attr, b)] if mode == "root" else [])
    layers = {
        "shot.usda": _layer(_world(box), meta=[_sublayers(["overrides.usda"])] if mode == "sublayer" else []),
        "asset.usda": _asset(subject, [_attr(attr, a)]),
    }
    if mode == "sublayer":
        layers["overrides.usda"] = _layer(_prim("over", "World", body=[_prim("over", name, body=[_attr(attr, b)])]))
    hint = "L comes before R: anything in the root layer stack (root layer or its sublayers) beats what a reference brings in."
    return _spec("local vs reference", layers, subject, [a] if mode == "none" else [a, b], hint)


def _reference_vs_payload(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    r, p = rng.sample(_POOL, 2)
    ref_file, payload_file = rng.sample(_ASSET_NAMES, 2)
    meta = [f"prepend references = @{ref_file}@", f"prepend payload = @{payload_file}@"]
    rng.shuffle(meta)
    layers = {
        "shot.usda": _layer(_world(_prim("def", name, meta=meta))),
        ref_file: _asset(subject, [_attr(attr, r)]),
        payload_file: _asset(subject, [_attr(attr, p)]),
    }
    hint = "A payload is a reference that can be unloaded; in LIVERPS the P sits right after the R."
    return _spec("reference vs payload", layers, subject, [r, p], hint)


def _inherits_vs_reference(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    c, a = rng.sample(_POOL, 2)
    class_value = rng.random() < 0.75
    class_prim = _prim("class", f"_class_{name}", body=[_attr(attr, c)] if class_value else [])
    meta = [f"inherits = </_class_{name}>", "prepend references = @asset.usda@"]
    rng.shuffle(meta)
    box = _prim("def", name, meta=meta)
    layers = {"asset.usda": _asset(subject, [_attr(attr, a)])}
    if rng.random() < 0.4:
        layers["shot.usda"] = _layer(_world(box), meta=[_sublayers(["classes.usda"])])
        layers["classes.usda"] = _layer(class_prim)
    else:
        layers["shot.usda"] = _layer(_world(box), class_prim)
    hint = "I comes before R: an inherited class is stronger than a reference on the same prim."
    return _spec("inherits vs reference", layers, subject, [a, c] if class_value else [a], hint)


def _specializes_weakest(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    s, a, c = rng.sample(_POOL, 3)
    mode = rng.choice(("reference", "reference", "empty_reference", "inherit"))
    meta = [f"specializes = </World/{name}Base>", "prepend references = @asset.usda@"]
    if mode == "inherit":
        meta.append(f"inherits = </_class_{name}>")
    rng.shuffle(meta)
    shot_prims = [_world(_prim("def", name, meta=meta), _prim("def", f"{name}Base", body=[_attr(attr, s)]))]
    if mode == "inherit":
        shot_prims.append(_prim("class", f"_class_{name}", body=[_attr(attr, c)]))
    layers = {
        "shot.usda": _layer(*shot_prims),
        "asset.usda": _asset(subject, [] if mode == "empty_reference" else [_attr(attr, a)]),
    }
    values = [s] + ([] if mode == "empty_reference" else [a]) + ([c] if mode == "inherit" else [])
    hint = "Specializes is the S at the very end of LIVERPS; it only shows through where nothing else has an opinion."
    return _spec("specializes is weakest", layers, subject, values, hint)


def _variant_selection(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    kind, name, attr, _ = subject
    variants = rng.sample(_VARIANT_NAMES, 3)
    values = rng.sample(_POOL, 4)
    default = rng.choice(variants)
    override = rng.choice([v for v in variants if v != default]) if rng.random() < 0.5 else None
    with_reference = rng.random() < 0.5
    meta = ['prepend variantSets = "sizes"', _selection("sizes", default)]
    if with_reference:
        meta.append("prepend references = @asset.usda@")
    variant_set = _variant_set("sizes", {v: [_attr(attr, value)] for v, value in zip(variants, values)})
    layers = {"model.usda": _layer(_world(_prim("def", name, kind, meta=meta, body=[variant_set])))}
    over = [_prim("over", "World", body=[_prim("over", name, meta=[_selection("sizes", override)])])] if override else []
    layers["shot.usda"] = _layer(*over, meta=[_sublayers(["model.usda"])])
    if with_reference:
        layers["asset.usda"] = _asset(subject, [_attr(attr, values[3])])
    hint = "Only the selected variant contributes, and the selection is itself an opinion resolved by strength (V beats R)."
    return _spec("variant selection", layers, subject, values[:3] + ([values[3]] if with_reference else []), hint)


def _class_broadcast(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    c1, a, c2 = rng.sample(_POOL, 3)
    asset_class, asset_local, override = rng.random() < 0.7, rng.random() < 0.6, rng.random() < 0.75
    class_name = f"_class_{name}"
    asset_class_prim = _prim("class", class_name, body=[_attr(attr, c1)] if asset_class else [])
    layers = {
        "asset.usda": _asset(
            subject, [_attr(attr, a)] if asset_local else [], meta=[f"inherits = </{class_name}>"], extra=[asset_class_prim]
        )
    }
    shot_prims = [_world(_prim("def", name, meta=["prepend references = @asset.usda@"]))]
    shot_meta: list[str] = []
    over_class = _prim("over", class_name, body=[_attr(attr, c2)])
    if override and rng.random() < 0.5:
        shot_prims.append(over_class)
    elif override:
        shot_meta = [_sublayers(["lookdev.usda"])]
        layers["lookdev.usda"] = _layer(over_class)
    layers["shot.usda"] = _layer(*shot_prims, meta=shot_meta)
    values = [v for v, on in ((c1, asset_class), (a, asset_local), (c2, override)) if on]
    hint = "An inherit authored inside an asset is also looked up in the root layer stack, at Inherit (I) strength."
    return _spec("class override through a reference", layers, subject, values, hint, fallback_possible=not values)


def _variant_in_reference(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    variants = rng.sample(_VARIANT_NAMES, rng.choice((2, 3)))
    values = rng.sample(_POOL, len(variants) + 1)
    asset_local = rng.random() < 0.4
    default = rng.choice(variants)
    shot_selection = rng.choice(variants) if rng.random() < 0.75 else None
    variant_set = _variant_set("sizes", {v: [_attr(attr, value)] for v, value in zip(variants, values)})
    body = ([_attr(attr, values[-1])] if asset_local else []) + [variant_set]
    layers = {
        "asset.usda": _asset(subject, body, meta=['prepend variantSets = "sizes"', _selection("sizes", default)])
    }
    meta = ["prepend references = @asset.usda@"]
    shot_meta: list[str] = []
    if shot_selection and rng.random() < 0.5:
        meta.append(_selection("sizes", shot_selection))
    elif shot_selection:
        shot_meta = [_sublayers(["layout.usda"])]
        over = _prim("over", name, meta=[_selection("sizes", shot_selection)])
        layers["layout.usda"] = _layer(_prim("over", "World", body=[over]))
    layers["shot.usda"] = _layer(_world(_prim("def", name, meta=meta)), meta=shot_meta)
    candidates = values[: len(variants)] + ([values[-1]] if asset_local else [])
    hint = (
        "LIVERPS restarts inside the referenced asset: its own local opinions beat its variants, "
        "while the shot may still choose which variant is selected."
    )
    return _spec("variant inside a reference", layers, subject, candidates, hint)


def _time_offset(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    kind, name, attr, _ = subject
    v0, v1, d = rng.sample(_POOL, 3)
    offset, scale = rng.choice((5, 10, 12, 24)), rng.choice((1, 1, 2))
    local_default = rng.random() < 0.35
    layer_time = rng.choice((0, 10))
    time = offset + scale * layer_time
    shift = f" (offset = {offset}; scale = {scale})" if scale != 1 else f" (offset = {offset})"
    samples = _samples(attr, {0: v0, 10: v1})
    local = [_attr(attr, d)] if local_default else []
    if rng.random() < 0.5:
        box = _prim("def", name, meta=[f"prepend references = @asset.usda@{shift}"], body=local)
        layers = {"shot.usda": _layer(_world(box)), "asset.usda": _asset(subject, [samples])}
    else:
        anim = _prim("over", "World", body=[_prim("over", name, body=[samples])])
        layers = {
            "shot.usda": _layer(_world(_prim("def", name, kind, body=local)), meta=[_sublayers(["anim.usda"], shift)]),
            "anim.usda": _layer(anim),
        }
    hint = (
        "Layer offsets map times: stage time = offset + scale x layer time. Strength is decided before time, "
        "so a stronger default beats weaker time samples."
    )
    return _spec("layer offsets and time samples", layers, subject, [v0, v1] + ([d] if local_default else []), hint, time=time)


def _combination(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    c1, c2, s, va, vb = rng.sample(_POOL, 5)
    variants = rng.sample(_VARIANT_NAMES, 2)
    default = rng.choice(variants)
    shot_selection = rng.choice(variants) if rng.random() < 0.6 else None
    asset_class, override, specialize = rng.random() < 0.5, rng.random() < 0.5, rng.random() < 0.6
    class_name = f"_class_{name}"
    variant_set = _variant_set("sizes", {variants[0]: [_attr(attr, va)], variants[1]: [_attr(attr, vb)]})
    asset_meta = [f"inherits = </{class_name}>", 'prepend variantSets = "sizes"', _selection("sizes", default)]
    class_prim = _prim("class", class_name, body=[_attr(attr, c1)] if asset_class else [])
    layers = {"asset.usda": _asset(subject, [variant_set], meta=asset_meta, extra=[class_prim])}
    meta = ["prepend references = @asset.usda@"]
    if specialize:
        meta.append(f"specializes = </World/{name}Base>")
    if shot_selection:
        meta.append(_selection("sizes", shot_selection))
    rng.shuffle(meta)
    children = [_prim("def", name, meta=meta)]
    if specialize:
        children.append(_prim("def", f"{name}Base", body=[_attr(attr, s)]))
    shot_meta: list[str] = []
    if override:
        shot_meta = [_sublayers(["lookdev.usda"])]
        layers["lookdev.usda"] = _layer(_prim("over", class_name, body=[_attr(attr, c2)]))
    layers["shot.usda"] = _layer(_world(*children), meta=shot_meta)
    values = [va, vb] + [v for v, on in ((c1, asset_class), (c2, override), (s, specialize)) if on]
    hint = (
        "Walk LIVERPS from the top: the root layer stack, then inherits (including classes reached from inside "
        "the asset), then the reference's own contents and variants, and specializes last."
    )
    return _spec("class, variant and specializes combined", layers, subject, values, hint)


def _value_block(rng: random.Random) -> dict[str, Any]:
    subject = rng.choice(_SUBJECTS)
    _kind, name, attr, _ = subject
    a, b = rng.sample(_POOL, 2)
    mode = rng.choice(("root_block", "weak_block", "asset_block", "asset_block_override"))
    asset_value = None if mode.startswith("asset_block") else a
    box_body = {"root_block": [_attr(attr, None)], "weak_block": [_attr(attr, b)]}.get(mode, [])
    box = _prim("def", name, meta=["prepend references = @asset.usda@"], body=box_body)
    layers = {"asset.usda": _asset(subject, [_attr(attr, asset_value)])}
    fix_value = {"weak_block": None, "asset_block_override": b}.get(mode, "skip")
    if fix_value == "skip":
        layers["shot.usda"] = _layer(_world(box))
    else:
        layers["shot.usda"] = _layer(_world(box), meta=[_sublayers(["fix.usda"])])
        layers["fix.usda"] = _layer(_prim("over", "World", body=[_prim("over", name, body=[_attr(attr, fix_value)])]))
    values = ([] if asset_value is None else [a]) + ([b] if mode in ("weak_block", "asset_block_override") else [])
    hint = "A value block (= None) hides every weaker opinion, but a stronger opinion still wins over the block."
    return _spec("value blocks", layers, subject, values, hint, allow_none=True)


_POOLS: dict[str, tuple[Callable[[random.Random], dict[str, Any]], ...]] = {
    "easy": (_sublayer_order, _local_vs_reference, _reference_vs_payload),
    "medium": (_inherits_vs_reference, _specializes_weakest, _variant_selection, _reference_vs_payload),
    "hard": (_class_broadcast, _variant_in_reference, _time_offset, _combination, _value_block),
}
