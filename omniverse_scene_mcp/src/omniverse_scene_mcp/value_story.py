"""Why does this value appear? A beginner's explanation of USD value resolution, plus a safe "what if".

why_value() builds on usd_insight.explain_value (USD's own resolution) and tells the story in plain words:
what you see, which opinion wins and from where (layer, arc, LIVRPS letter), which weaker opinions it beats
and why, the fallback when nothing is authored, and values that are *computed* rather than authored
(inherited visibility and purpose, world transforms, materials bound on an ancestor).

what_if() answers "if I set this value in that layer, would it show?" by authoring the opinion on in-memory
copies of the local layer stack, never on the stage or its files.
"""
from __future__ import annotations

import re
from typing import Any

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - the router runs without pxr in some test setups
    Sdf = Usd = UsdGeom = UsdShade = None

from . import usd_insight
from .usd_insight import to_jsonable

STRENGTH = "LIVERPS"  # Local, Inherits, VariantSets, rElocates, References, Payloads, Specializes
ARC_WORDS = {
    "L": "your own layers (local)",
    "I": "an inherited class",
    "V": "a variant",
    "E": "a relocate",
    "R": "a reference",
    "P": "a payload",
    "S": "a specialized class",
}
SHORT = {"L": "local opinions", "I": "inherits", "V": "variants", "E": "relocates", "R": "references",
         "P": "payloads", "S": "specializes"}
EXAM = [
    {"id": "1.6", "topic": "LIVRPS strength order"},
    {"id": "1.8", "topic": "Why a value authored in some layer is ignored"},
    {"id": "5.4", "topic": "Authored versus fallback values"},
]
LEARN_MORE = {
    "L": "Learn OpenUSD: Layers and layer stacks",
    "I": "Learn OpenUSD: Inherits",
    "V": "Learn OpenUSD: Variant sets",
    "R": "Learn OpenUSD: References",
    "P": "Learn OpenUSD: Payloads",
    "S": "Learn OpenUSD: Specializes",
    "fallback": "Learn OpenUSD: Attributes (fallback values)",
    "computed": "Learn OpenUSD: Xforms and Imageable (computed values)",
}
_VARIANT = re.compile(r"\{([^=}]+)=([^}]*)\}")
_MATERIAL_NAMES = {"material", "material:binding", "materials"}


class _Refusal(Exception):
    pass


# ------------------------------------------------------------------------------------------------ helpers
def _ready(stage: Any) -> None:
    if Usd is None:
        raise _Refusal("pxr (USD) is not available in this Python")
    if stage is None:
        raise _Refusal("No stage is open")


def _split(prim_path: Any, attribute: Any) -> tuple[str, str | None]:
    if not isinstance(prim_path, str) or not prim_path.strip():
        raise _Refusal("prim_path must be a prim path such as /World/Box (or /World/Box.size)")
    text = prim_path.strip()
    if attribute is None and "." in text.rsplit("/", 1)[-1]:
        text, attribute = text.split(".", 1) if text.count(".") == 1 else (text[: text.rfind(".")], text[text.rfind(".") + 1:])
    if attribute is not None and (not isinstance(attribute, str) or not attribute.strip()):
        raise _Refusal("attribute must be a name such as size or primvars:displayColor")
    return text, attribute.strip() if isinstance(attribute, str) else None


def _records(stage: Any) -> list[dict[str, Any]]:
    from .layer_manager import _walk

    return [r for r in _walk(stage) if r.get("layer") is not None]


def _norm(identifier: str | None) -> str:
    return (identifier or "").replace("\\", "/").lower()


def _local_roles(stage: Any) -> dict[str, str]:
    """identifier -> plain description of where that layer sits in the local layer stack."""
    roles: dict[str, str] = {}
    sublayers = 0
    for record in _records(stage):
        layer = record["layer"]
        name = usd_insight._short_layer(layer.identifier)
        if layer == stage.GetSessionLayer():
            roles[_norm(layer.identifier)] = "the session layer (strongest, not saved with the file)"
        elif layer == stage.GetRootLayer():
            roles[_norm(layer.identifier)] = f"your root layer {name}"
        else:
            sublayers += 1
            roles[_norm(layer.identifier)] = f"sublayer {name} (number {sublayers} in the stack, counted from the strongest)"
    return roles


def _letter(entry: dict[str, Any]) -> str:
    return entry.get("liverps") or "L"


def _variant_of(spec_path: str) -> tuple[str, str] | None:
    match = _VARIANT.search(spec_path or "")
    return (match.group(1), match.group(2)) if match else None


def _where(entry: dict[str, Any], roles: dict[str, str]) -> str:
    """Plain sentence: where this opinion is written."""
    layer = usd_insight._short_layer(entry["layer"])
    letter = _letter(entry)
    if letter == "L":
        return roles.get(_norm(entry["layer"]), f"layer {layer}")
    if letter == "V":
        variant = _variant_of(entry.get("spec_path", ""))
        if variant:
            return f"variant '{variant[1]}' of the variant set '{variant[0]}' (in {layer})"
        return f"a variant (in {layer})"
    if letter in ("R", "P"):
        word = "reference" if letter == "R" else "payload"
        by = usd_insight._short_layer(entry.get("introducing_layer")) if entry.get("introducing_layer") else None
        return f"the {word}d file {layer}" + (f" (the {word} is authored in {by})" if by else "")
    if letter in ("I", "S"):
        source = entry.get("spec_path", "").rsplit(".", 1)[0]
        kind = "inherited" if letter == "I" else "specialized"
        return f"the {kind} class {source} (in {layer})"
    return f"{ARC_WORDS.get(letter, 'an arc')} in {layer}"


def _value_of(entry: dict[str, Any]) -> Any:
    if entry.get("has_default"):
        return entry.get("default")
    if entry.get("time_sample_count"):
        return f"{entry['time_sample_count']} time samples"
    return None


def _loses_because(winner: dict[str, Any], loser: dict[str, Any], roles: dict[str, str]) -> str:
    wl, ll = _letter(winner), _letter(loser)
    if wl != ll:
        return f"{SHORT.get(wl, wl)} ({wl}) beat {SHORT.get(ll, ll)} ({ll}) in LIVRPS order"
    if wl == "L":
        return ("both are in your own layer stack, and the winner's layer is higher in the stack: the session layer "
                "beats the root layer, the root layer beats its sublayers, and earlier sublayers beat later ones")
    return (f"both come through {ARC_WORDS.get(wl, wl)}; inside it the winner's layer is the stronger one")


def _computed(stage: Any, prim: Any, attr_name: str, time_code: Any) -> list[dict[str, str]]:
    """Values USD computes from ancestors rather than reading from one opinion."""
    notes: list[dict[str, str]] = []
    imageable = UsdGeom.Imageable(prim)
    if attr_name == "visibility" and imageable:
        effective = imageable.ComputeVisibility(time_code)
        authored = imageable.GetVisibilityAttr().Get(time_code)
        if str(effective) != str(authored):
            parent = prim.GetParent()
            culprit = None
            while parent and parent.IsValid() and not parent.IsPseudoRoot():
                attr = UsdGeom.Imageable(parent).GetVisibilityAttr() if UsdGeom.Imageable(parent) else None
                if attr and attr.Get(time_code) == UsdGeom.Tokens.invisible:
                    culprit = parent
                parent = parent.GetParent()
            notes.append({
                "what": "computed visibility",
                "value": str(effective),
                "sentence": (f"The attribute says '{authored}', but the prim is drawn as '{effective}' because "
                             + (f"its ancestor {culprit.GetPath()} is invisible: invisibility is inherited down the tree."
                                if culprit else "an ancestor is invisible: invisibility is inherited down the tree.")),
            })
    if attr_name == "purpose" and imageable:
        effective = imageable.ComputePurpose()
        authored = imageable.GetPurposeAttr().Get()
        if str(effective) != str(authored):
            notes.append({"what": "computed purpose", "value": str(effective),
                          "sentence": f"Authored purpose is '{authored}', but it inherits '{effective}' from an ancestor."})
    if (attr_name.startswith("xformOp") or attr_name == "xformOpOrder") and UsdGeom.Xformable(prim):
        cache = UsdGeom.XformCache(time_code)
        world = cache.GetLocalToWorldTransform(prim).ExtractTranslation()
        local = UsdGeom.Xformable(prim).GetLocalTransformation(time_code)
        local_t = local[0].ExtractTranslation() if isinstance(local, tuple) else local.ExtractTranslation()
        movers = []
        parent = prim.GetParent()
        while parent and parent.IsValid() and not parent.IsPseudoRoot():
            xf = UsdGeom.Xformable(parent)
            if xf and xf.GetOrderedXformOps():
                movers.append(str(parent.GetPath()))
            parent = parent.GetParent()
        if movers or list(world) != list(local_t):
            notes.append({
                "what": "computed world position",
                "value": to_jsonable(world),
                "sentence": (f"This attribute is the prim's own (local) transform. Where it ends up in the world, "
                             f"{to_jsonable(world)}, is computed by also applying its parents' transforms"
                             + (f" ({', '.join(movers[:4])})" if movers else "") + "."),
            })
    return notes


def why_material(stage: Any, prim_path: str) -> dict[str, Any]:
    """Which material a prim uses and where that binding was authored (it may be on an ancestor)."""
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        raise _Refusal(f"No prim at {prim_path}")
    material, relationship = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    if not material:
        return {"ok": True, "prim_path": prim_path, "attribute": "material", "value": None,
                "headline": f"{prim_path} has no material bound, on itself or on any ancestor.",
                "computed": [], "exam": [{"id": "8.3", "topic": "Material binding"}],
                "learn_more": ["Learn OpenUSD: Materials and binding"]}
    owner = relationship.GetPrim().GetPath() if relationship else None
    inherited = owner is not None and owner != prim.GetPath()
    sentence = (f"{prim_path} uses material {material.GetPath()}. The binding is authored on "
                + (f"its ancestor {owner}, and children inherit a binding unless they bind their own."
                   if inherited else "the prim itself."))
    return {
        "ok": True, "prim_path": prim_path, "attribute": "material", "value": str(material.GetPath()),
        "headline": sentence,
        "computed": [{"what": "material binding", "value": str(material.GetPath()), "sentence": sentence}] if inherited else [],
        "binding_authored_on": str(owner) if owner else None,
        "exam": [{"id": "8.3", "topic": "Material binding (strength and purpose)"}],
        "learn_more": ["Learn OpenUSD: Materials and binding"],
    }


def _predictions(prim_path: str, attr_name: str, value: Any, winner: dict[str, Any] | None,
                 stage: Any) -> list[dict[str, Any]]:
    new = _different(value)
    if new is None:
        return []
    out = []
    targets = ["root"]
    records = _records(stage)
    weakest = [r for r in records if r["layer"] not in (stage.GetRootLayer(), stage.GetSessionLayer())]
    if weakest:
        targets.append(usd_insight._short_layer(weakest[-1]["layer"].identifier))
    for layer in targets:
        out.append({
            "question": f"Predict: if you set {attr_name} = {new!r} in {'the root layer' if layer == 'root' else layer}, "
                        "will that value show?",
            "check_with": {"tool": "scene_what_if",
                           "arguments": {"prim_path": prim_path, "attribute": attr_name, "value": new, "layer": layer}},
        })
    return out


def _different(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value * 2 + 1
    if isinstance(value, list) and value and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        return [round(v * 0.5 + 0.25, 4) for v in value]
    if isinstance(value, str) and value:
        return value + "_changed"
    return None


# ------------------------------------------------------------------------------------------------ public
def why_value(stage: Any, prim_path: str, attribute: str | None = None, time: Any = None) -> dict[str, Any]:
    """Read-only: why a prim's attribute has the value you see, in plain words: the winning opinion and where it is
    written (layer, arc, LIVRPS letter), the weaker opinions it beats and why, the fallback when nothing is authored,
    and values computed from ancestors. prim_path may include the attribute ("/World/Box.size"); attribute="material"
    explains material binding. Ends with predictions you can check with scene_what_if."""
    try:
        _ready(stage)
        path, name = _split(prim_path, attribute)
        if name is None:
            raise _Refusal("Name an attribute, e.g. prim_path='/World/Box.size' or attribute='size' (or 'material')")
        if name.lower() in _MATERIAL_NAMES:
            return why_material(stage, path)
        raw = usd_insight.explain_value(stage, path, name, time)
    except (_Refusal, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "error": str(exc)}

    roles = _local_roles(stage)
    winner = raw.get("winning_opinion")
    value = raw.get("value")
    stack = raw.get("property_stack", [])
    label = f"{raw['prim_path']}.{raw['attribute']}"
    where_from: dict[str, Any] | None = None
    beats: list[dict[str, Any]] = []
    not_authored = None

    if raw.get("blocked"):
        headline = (f"{label} has no value because the strongest opinion is a value block, written in "
                    f"{_where(winner, roles) if winner else 'a layer'}. A block hides every weaker opinion.")
    elif winner is None:
        fallback = raw.get("fallback")
        not_authored = {
            "fallback": fallback,
            "sentence": ("No layer authors this attribute, so USD uses the fallback value built into the "
                         f"{raw.get('prim_type_name') or 'prim'} schema" + (f": {fallback!r}." if fallback is not None else ".")
                         if fallback is not None else "No layer authors this attribute and the schema has no fallback, so it has no value."),
        }
        headline = f"{label} = {value!r}. {not_authored['sentence']}"
    else:
        kind = {"time_samples": "a time sample", "spline": "a spline", "default": "the default value"}.get(
            winner.get("opinion_at_time") or "default", "an opinion")
        where_from = {
            "written_in": _where(winner, roles),
            "layer_file": usd_insight._short_layer(winner["layer"]),
            "arc": ARC_WORDS.get(_letter(winner), "an arc"),
            "liverps_letter": _letter(winner),
            "opinion_kind": kind,
        }
        headline = f"{label} = {value!r}, from {kind} written in {where_from['written_in']}."
        for entry in stack[winner["index"] + 1:]:
            loser_value = _value_of(entry)
            if loser_value is None:
                continue
            beats.append({
                "written_in": _where(entry, roles),
                "value": loser_value,
                "liverps_letter": _letter(entry),
                "why_it_loses": _loses_because(winner, entry, roles),
            })
        if beats:
            headline += f" It beats {len(beats)} weaker opinion{'s' if len(beats) != 1 else ''}."

    prim = stage.GetPrimAtPath(raw["prim_path"])
    try:
        computed = _computed(stage, prim, raw["attribute"], usd_insight._time_code(time))
    except Exception:  # noqa: BLE001 - computed facts are extra; the story stands without them
        computed = []
    letters = sorted({_letter(e) for e in stack if _value_of(e) is not None}, key=STRENGTH.index)
    learn = [LEARN_MORE[k] for k in letters if k in LEARN_MORE]
    if not_authored:
        learn.append(LEARN_MORE["fallback"])
    if computed:
        learn.append(LEARN_MORE["computed"])
    return {
        "ok": True,
        "prim_path": raw["prim_path"],
        "attribute": raw["attribute"],
        "value": value,
        "type": raw.get("type_name"),
        "time": raw.get("time"),
        "headline": headline,
        "comes_from": where_from,
        "beats": beats,
        "not_authored": not_authored,
        "computed": computed,
        "authored_vs_computed": ("The value above is the authored (resolved) one. 'computed' lists what USD works out "
                                 "from ancestors, which can differ from it." if computed else None),
        "strength_order": "L > I > V > R > P > S  (Local, Inherits, Variants, References, Payloads, Specializes)",
        "predict": _predictions(raw["prim_path"], raw["attribute"], value, winner, stage),
        "exam": EXAM,
        "learn_more": learn or [LEARN_MORE["L"]],
        "technical_why": raw.get("why"),
    }


def what_if(stage: Any, prim_path: str, attribute: str | None = None, value: Any = None, layer: str = "root",
            time: Any = None) -> dict[str, Any]:
    """Read-only experiment: would `value` show if you authored it for this attribute in `layer` ('root', 'session',
    or the name of a sublayer)? Runs on in-memory copies of your layers; the stage and its files are never changed.
    Says whether your value wins, and if not, which opinion still beats it and why."""
    try:
        _ready(stage)
        path, name = _split(prim_path, attribute)
        if name is None:
            raise _Refusal("Name an attribute, e.g. prim_path='/World/Box.size'")
        if value is None:
            raise _Refusal("Give the value you want to try, e.g. value=5")
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            raise _Refusal(f"No prim at {path}")
        attr = prim.GetAttribute(name)
        if not attr:
            raise _Refusal(f"{path} has no attribute '{name}'")
        records = _records(stage)
        target = _pick_layer(stage, records, layer)
        before = usd_insight.explain_value(stage, path, name, time)
        dirty_before = {r["layer"].identifier: r["layer"].dirty for r in records}
        sim, copies, originals = _simulate(stage, records, path)
        from .edits import _Context, _Refusal as _EditRefusal, _coerce

        try:
            usd_value, plain = _coerce(attr.GetTypeName(), value, _Context(name, [], [], []))
        except _EditRefusal as exc:
            raise _Refusal(str(exc)) from None
        copy = copies[_norm(target.identifier)]
        spec = Sdf.CreatePrimInLayer(copy, Sdf.Path(path))
        attr_spec = spec.attributes.get(name) or Sdf.AttributeSpec(spec, name, attr.GetTypeName())
        time_code = usd_insight._time_code(time)
        if time_code.IsDefault():
            attr_spec.default = usd_value
        else:
            copy.SetTimeSample(attr_spec.path, time_code.GetValue(), usd_value)
        after = usd_insight.explain_value(sim, path, name, time)
        dirty_after = {r["layer"].identifier: r["layer"].dirty for r in records}
    except (_Refusal, ValueError, KeyError, TypeError) as exc:
        return {"ok": False, "error": str(exc), "changed_anything": False}

    winner = after.get("winning_opinion") or {}
    won = _norm(winner.get("layer")) == _norm(copy.identifier)
    target_name = ("the session layer" if target == stage.GetSessionLayer() else usd_insight._short_layer(target.identifier))
    if won:
        explanation = (f"Yes: {name} would become {after.get('value')!r}. Nothing stronger than {target_name} sets "
                       f"{name}, so your opinion would win.")
        blocker = None
    else:
        original_layer = originals.get(_norm(winner.get("layer")), winner.get("layer"))
        mapped = dict(winner, layer=original_layer)
        roles = _local_roles(stage)
        blocker = {"written_in": _where(mapped, roles), "value": after.get("value"), "liverps_letter": _letter(mapped)}
        explanation = (f"No: {name} would still be {after.get('value')!r}. Your value in {target_name} loses to "
                       f"{blocker['written_in']} because " + _loses_against(mapped, target, stage) + ".")
    return {
        "ok": True,
        "prim_path": path,
        "attribute": name,
        "tried": {"value": plain, "layer": target_name, "time": to_jsonable(time)},
        "value_now": before.get("value"),
        "value_after": after.get("value"),
        "your_value_would_win": won,
        "still_wins": blocker,
        "explanation": explanation,
        "changed_anything": any(dirty_before[k] != dirty_after.get(k) for k in dirty_before),
        "note": "This ran on in-memory copies of your layers. Your scene and files were not changed.",
        "to_really_do_it": f"scene_set_attribute(prim_path='{path}', attribute='{name}', value=..., dry_run=True) "
                           "previews the real edit (in the current edit target layer).",
    }


def _loses_against(winner: dict[str, Any], target: Any, stage: Any) -> str:
    letter = _letter(winner)
    if letter != "L":
        return (f"that opinion arrives through {ARC_WORDS.get(letter, letter)} ({letter}) that is authored in a "
                "layer stronger than the one you chose, so its whole result outranks your layer")
    if winner.get("layer") and _norm(winner["layer"]) == _norm(stage.GetSessionLayer().identifier):
        return "the session layer is stronger than every layer saved in your file"
    return "that layer is higher in your layer stack than the one you chose"


def _pick_layer(stage: Any, records: list[dict[str, Any]], layer: Any) -> Any:
    if not isinstance(layer, str) or not layer.strip():
        raise _Refusal("layer must be 'root', 'session' or the name of one of your sublayers")
    wanted = layer.strip().lower()
    if wanted == "root":
        return stage.GetRootLayer()
    if wanted == "session":
        return stage.GetSessionLayer()
    for record in records:
        ident = record["layer"].identifier
        if wanted in (_norm(ident), usd_insight._short_layer(ident).lower()):
            return record["layer"]
    names = ", ".join(usd_insight._short_layer(r["layer"].identifier) for r in records)
    raise _Refusal(f"'{layer}' is not in this stage's local layer stack ({names}); use root, session or one of those")


def _simulate(stage: Any, records: list[dict[str, Any]], path: str) -> tuple[Any, dict[str, Any], dict[str, str]]:
    """Copy the local layer stack (session, root, sublayers, strongest first) into anonymous layers."""
    from pxr import UsdUtils

    from .layer_manager import _anchor

    root = stage.GetRootLayer()
    sim_root = Sdf.Layer.CreateAnonymous("why-sim-root")
    for key in ("timeCodesPerSecond", "framesPerSecond", "startTimeCode", "endTimeCode"):
        try:
            if root.pseudoRoot.HasInfo(key):
                sim_root.pseudoRoot.SetInfo(key, root.pseudoRoot.GetInfo(key))
        except Exception:  # noqa: BLE001
            continue
    copies: dict[str, Any] = {}
    originals: dict[str, str] = {}
    for record in records:
        layer = record["layer"]
        if _norm(layer.identifier) in copies:
            continue
        copy = Sdf.Layer.CreateAnonymous("why-sim")
        copy.TransferContent(layer)
        UsdUtils.ModifyAssetPaths(copy, lambda asset, source=layer: _anchor(source, asset))
        copy.subLayerPaths = []
        copies[_norm(layer.identifier)] = copy
        originals[_norm(copy.identifier)] = layer.identifier
        sim_root.subLayerPaths.append(copy.identifier)
        offset = record.get("offset_to_root")
        if offset is not None and hasattr(offset, "offset") and (offset.offset != 0.0 or offset.scale != 1.0):
            sim_root.subLayerOffsets[len(sim_root.subLayerPaths) - 1] = offset
    sim = Usd.Stage.OpenMasked(sim_root, Usd.StagePopulationMask([Sdf.Path(path)]), Usd.Stage.LoadNone)
    sim.SetLoadRules(stage.GetLoadRules())
    # Anonymous layers live only while referenced: copies (returned) and sim_root (held by the caller) keep them.
    copies["__root__"] = sim_root
    return sim, copies, originals
