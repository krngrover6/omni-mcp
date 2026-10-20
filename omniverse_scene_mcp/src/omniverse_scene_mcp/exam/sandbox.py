"""Composition sandbox: compose USDA layers in a throwaway directory and explain LIVERPS results."""

from __future__ import annotations

import math
import os
import re
import shutil
import tempfile
from typing import Any

try:
    from pxr import Sdf, Usd
except ImportError:  # pragma: no cover - depends on the local environment
    Sdf = Usd = None

TEMP_PARENT: str | None = None  # where sandbox temp dirs are created; None means the system temp dir
MAX_LAYERS = 32
MAX_LAYER_CHARS = 1_000_000
MAX_QUERIES = 50
MAX_SAMPLES = 100
MAX_ITEMS = 64
MAX_STACK = 50
MAX_ERRORS = 50
MAX_PRIMS = 5000

STRENGTH = ("local", "inherit", "variant", "relocate", "reference", "payload", "specialize")
LETTERS = {
    "local": "L",
    "inherit": "I",
    "variant": "V",
    "relocate": "E",
    "reference": "R",
    "payload": "P",
    "specialize": "S",
}
_MEANINGS = {
    "local": "opinions written directly on the prim in the root layer stack (root layer, its sublayers "
    "with earlier ones stronger, and the session layer)",
    "inherit": "opinions from a class the prim inherits; editing the class changes every inheritor, "
    "even ones inside referenced assets",
    "variant": "opinions inside the currently selected variant of a variant set",
    "relocate": "opinions moved from another namespace location by relocates",
    "reference": "opinions from another layer or prim pulled in by a reference",
    "payload": "like a reference, but it can be unloaded to save memory, and it is weaker than references",
    "specialize": "a base the prim refines; weakest of all, so it only shows through where nothing else has an opinion",
}
_ALIASES = {
    **dict.fromkeys(("local", "l", "root", "sublayer", "sublayers", "session", "layer", "layers"), "local"),
    **dict.fromkeys(("inherit", "inherits", "i", "class"), "inherit"),
    **dict.fromkeys(("variant", "variants", "variantset", "variantsets", "variant set", "v"), "variant"),
    **dict.fromkeys(("relocate", "relocates", "e"), "relocate"),
    **dict.fromkeys(("reference", "references", "ref", "r"), "reference"),
    **dict.fromkeys(("payload", "payloads", "p"), "payload"),
    **dict.fromkeys(("specialize", "specializes", "s"), "specialize"),
}
_ARC_NAMES = {
    "ArcTypeRoot": "local",
    "ArcTypeInherit": "inherit",
    "ArcTypeVariant": "variant",
    "ArcTypeRelocate": "relocate",
    "ArcTypeReference": "reference",
    "ArcTypePayload": "payload",
    "ArcTypeSpecialize": "specialize",
}
_PHRASES = {
    "reference": "the referenced asset",
    "payload": "the payload",
    "inherit": "the inherited class",
    "specialize": "the specialized base",
    "variant": "the selected variant",
    "relocate": "the relocated source",
}
_SOURCES = {
    "ResolveInfoSourceNone": "none",
    "ResolveInfoSourceFallback": "fallback",
    "ResolveInfoSourceDefault": "default",
    "ResolveInfoSourceTimeSamples": "time_samples",
    "ResolveInfoSourceValueClips": "value_clips",
    "ResolveInfoSourceSpline": "spline",
}
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def compose(
    layers: dict[str, str],
    root: str,
    queries: list[str] | None = None,
    max_prims: int = 500,
) -> dict[str, Any]:
    """Compose USDA layer strings in a fresh temp dir and report layer stack, prims, arcs and query results.

    Queries: '/Prim' (prim info), '/Prim.attr' (value, property stack, winner, why), '/Prim.attr@24'
    (the same at time code 24), '/Prim.rel' (relationship targets). The temp dir is always deleted.
    An arc's target_layer is the root layer of its target layer stack; the 'sublayer' entries after it
    name the other layers of that stack holding specs. Stack entries carry the root-level arc that
    LIVERPS compares ('arc'), the node's own arc ('node_arc') and the full path through arcs ('via').
    """
    problems = _validate(layers, root, queries)
    if problems:
        return _failed(problems)
    if Usd is None:
        return _failed([{"kind": "environment", "message": "USD Python bindings (pxr) are not installed"}])
    try:
        limit = max(0, min(int(max_prims), MAX_PRIMS))
    except (TypeError, ValueError):
        return _failed([{"kind": "input", "message": "max_prims must be an integer"}])
    tmp = tempfile.mkdtemp(prefix="ousd_sandbox_", dir=TEMP_PARENT)
    try:
        return _compose_in(tmp, layers, root, list(queries or []), limit)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def explain_strength(arc_list: list[Any]) -> dict[str, Any]:
    """Rank arc types strongest-first by LIVERPS and describe each one in plain language."""
    known: list[str] = []
    unknown: list[str] = []
    for item in arc_list or []:
        raw = item.get("arc") if isinstance(item, dict) else item
        arc = _ALIASES.get(str(raw).strip().lower())
        if arc:
            known.append(arc)
        else:
            unknown.append(str(raw))
    order = sorted(set(known), key=STRENGTH.index)
    ranked = [
        {"arc": arc, "letter": LETTERS[arc], "rank": STRENGTH.index(arc) + 1, "meaning": _MEANINGS[arc]}
        for arc in order
    ]
    reminder = (
        "The same order applies again inside every referenced or payloaded asset, and within one layer "
        "stack earlier sublayers beat later ones."
    )
    if not order:
        summary = (
            "LIVERPS lists opinion sources strongest first: Local, Inherits, VariantSets, rElocates, "
            f"References, Payloads, Specializes. {reminder}"
        )
    elif len(order) == 1:
        summary = f"Only {order[0]} opinions are involved ({LETTERS[order[0]]} in LIVERPS). {reminder}"
    else:
        chain = " > ".join(f"{arc} ({LETTERS[arc]})" for arc in order)
        summary = (
            f"Strongest to weakest: {chain}. When several of these hold an opinion for the same property, "
            f"the {order[0]} opinion wins. {reminder}"
        )
    return {"acronym": "LIVERPS", "order": order, "ranked": ranked, "summary": summary, "unknown": unknown}


def format_value(value: Any) -> str:
    """Short display string for a resolved value: 5.0 -> '5', None -> 'None', vectors -> '(1, 2, 3)'."""
    return _fmt(_jsonable(value))


def _validate(layers: Any, root: Any, queries: Any) -> list[dict[str, Any]]:
    if not isinstance(layers, dict) or not layers:
        return [{"kind": "input", "message": "layers must be a non-empty dict of {file name: USDA text}"}]
    if len(layers) > MAX_LAYERS:
        return [{"kind": "input", "message": f"at most {MAX_LAYERS} layers are allowed"}]
    problems = []
    seen: set[str] = set()
    for name, text in layers.items():
        reason = _bad_name(name)
        if reason:
            problems.append({"kind": "input", "layer": str(name), "message": reason})
            continue
        if name.lower() in seen:
            problems.append({"kind": "input", "layer": name, "message": "duplicate layer name (names are case-insensitive)"})
        seen.add(name.lower())
        if not isinstance(text, str):
            problems.append({"kind": "input", "layer": name, "message": "layer content must be USDA text"})
        elif len(text) > MAX_LAYER_CHARS:
            problems.append({"kind": "input", "layer": name, "message": f"layer is larger than {MAX_LAYER_CHARS} characters"})
        elif not text.lstrip().startswith("#usda"):
            problems.append({"kind": "input", "layer": name, "message": "layer text must start with a '#usda 1.0' header"})
    if not isinstance(root, str) or root not in layers:
        problems.append({"kind": "input", "message": f"root {root!r} must be one of the layer names"})
    if queries is not None:
        if not isinstance(queries, (list, tuple)) or not all(isinstance(q, str) for q in queries):
            problems.append({"kind": "input", "message": "queries must be a list of strings such as '/World/Box.size'"})
        elif len(queries) > MAX_QUERIES:
            problems.append({"kind": "input", "message": f"at most {MAX_QUERIES} queries are allowed"})
    return problems


def _bad_name(name: Any) -> str:
    if not isinstance(name, str) or not name:
        return "layer names must be non-empty strings"
    if "/" in name or "\\" in name or ":" in name or ".." in name:
        return f"layer name {name!r} must be a simple file name such as 'shot.usda' (no separators or '..')"
    if not _NAME_RE.match(name):
        return f"layer name {name!r} may only use letters, digits, '_', '-' and '.'"
    stem, _, suffix = name.rpartition(".")
    if suffix.lower() not in ("usda", "usd") or not stem:
        return f"layer name {name!r} must end in .usda or .usd"
    if stem.split(".")[0].upper() in _RESERVED:
        return f"layer name {name!r} is reserved on Windows"
    return ""


def _failed(errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "ok": False,
        "stage_opened": False,
        "errors": errors,
        "root": "",
        "layer_stack": [],
        "prims": [],
        "prim_count": 0,
        "prims_truncated": False,
        "queries": [],
    }


class _Names:
    """Maps sandbox layer identifiers back to the caller's simple file names."""

    def __init__(self, tmp: str) -> None:
        self._dir = os.path.normcase(os.path.realpath(tmp))
        patterns = []
        for directory in dict.fromkeys((tmp, os.path.realpath(tmp))):
            pieces = [p for p in re.split(r"[\\/]+", directory) if p]
            patterns.append(r"[\\/]".join(re.escape(p) for p in pieces) + r"[\\/]")
        self._pattern = re.compile("|".join(patterns), re.IGNORECASE)
        self.session_id = ""
        self.root_id = ""

    def layer(self, layer: Any) -> str:
        if layer is None:
            return ""
        identifier = layer.identifier
        if identifier == self.session_id:
            return "<session>"
        if layer.anonymous:
            return "<anonymous>"
        real = layer.realPath or identifier
        if os.path.normcase(os.path.dirname(os.path.realpath(real))) == self._dir:
            return os.path.basename(real)
        return self.clean(identifier)

    def clean(self, text: str) -> str:
        parts = []
        for chunk in re.split(r"\s*Error in '", str(text)):
            match = re.search(r" : '(.*)'\s*$", chunk, re.S)
            chunk = (match.group(1) if match else chunk).strip()
            if chunk:
                parts.append(self._pattern.sub("", chunk))
        return "; ".join(parts)


def _compose_in(tmp: str, layers: dict[str, str], root: str, queries: list[str], max_prims: int) -> dict[str, Any]:
    names = _Names(tmp)
    errors: list[dict[str, Any]] = []
    opened: dict[str, Any] = {}
    for name, text in layers.items():
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        try:
            layer = Sdf.Layer.FindOrOpen(path)
        except Exception as exc:  # Tf.ErrorException carries the parser message
            errors.append({"kind": "parse", "layer": name, "message": names.clean(str(exc))})
            continue
        if layer is None:
            errors.append({"kind": "parse", "layer": name, "message": "layer could not be opened"})
        else:
            opened[name] = layer
    for name, layer in opened.items():
        for asset in _asset_paths(layer):
            if asset and _strip_dot(asset) not in layers:
                errors.append({
                    "kind": "asset",
                    "layer": name,
                    "message": f"asset path @{asset}@ must name one of the sandbox layers ({', '.join(sorted(layers))})",
                })
    result = _failed(errors)
    result["root"] = root
    if root not in opened or any(e["kind"] == "asset" for e in errors):
        return result

    try:
        stage = Usd.Stage.Open(opened[root], Usd.Stage.LoadAll)
    except Exception as exc:
        errors.append({"kind": "stage", "layer": root, "message": names.clean(str(exc))})
        return result
    names.session_id = stage.GetSessionLayer().identifier if stage.GetSessionLayer() else ""
    names.root_id = stage.GetRootLayer().identifier
    result["stage_opened"] = True
    result["layer_stack"] = [names.layer(layer) for layer in stage.GetLayerStack(False)]

    if hasattr(stage, "GetCompositionErrors"):
        for error in list(stage.GetCompositionErrors())[:MAX_ERRORS]:
            errors.append(_composition_error(error, names))
    prims = []
    for prim in stage.TraverseAll():
        if len(prims) >= max_prims:
            result["prims_truncated"] = True
            break
        prims.append(_prim_info(prim, names))
        if not hasattr(stage, "GetCompositionErrors"):
            errors.extend(_composition_error(e, names) for e in prim.GetPrimIndex().localErrors)
    result["prims"] = prims
    result["prim_count"] = len(prims)
    result["queries"] = [_query(stage, text, names) for text in queries]
    result["errors"] = errors[:MAX_ERRORS]
    result["ok"] = not errors
    return result


def _strip_dot(asset: str) -> str:
    asset = asset.strip()
    return asset[2:] if asset.startswith("./") else asset


def _asset_paths(layer: Any) -> list[str]:
    """Every asset path a layer could make USD open: sublayers, references, payloads and value clips."""
    found = [str(p) for p in layer.subLayerPaths]

    def visit(path: Any) -> None:
        if not (path.IsPrimPath() or path.IsPrimVariantSelectionPath()):
            return
        spec = layer.GetPrimAtPath(path)
        if spec is None:
            return
        for proxy in (spec.referenceList, spec.payloadList):
            for field in ("explicitItems", "addedItems", "prependedItems", "appendedItems", "orderedItems"):
                found.extend(item.assetPath for item in getattr(proxy, field, ()) or ())
        if spec.HasInfo("clips"):
            found.extend(_clip_assets(spec.GetInfo("clips")))

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return found


def _clip_assets(value: Any, key: str = "") -> list[str]:
    if isinstance(value, Sdf.AssetPath):
        return [value.path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _clip_assets(v, str(k))]
    if isinstance(value, str):
        return [f"template:{value}"] if key == "templateAssetPath" else []
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        return [p for item in list(value) for p in _clip_assets(item, key)]
    return []


def _composition_error(error: Any, names: _Names) -> dict[str, Any]:
    kind = _enum_name(getattr(error, "errorType", "")).replace("ErrorType_", "")
    return {"kind": "composition", "type": kind, "message": names.clean(str(error))}


def _enum_name(value: Any) -> str:
    return getattr(value, "name", "") or str(value).rsplit(".", 1)[-1]


def _arc_name(arc_type: Any) -> str:
    return _ARC_NAMES.get(_enum_name(arc_type), _enum_name(arc_type).replace("ArcType", "").lower())


def _prim_info(prim: Any, names: _Names, detail: bool = False) -> dict[str, Any]:
    variant_sets = prim.GetVariantSets()
    info: dict[str, Any] = {
        "path": str(prim.GetPath()),
        "type": str(prim.GetTypeName()),
        "specifier": _enum_name(prim.GetSpecifier()).replace("Specifier", "").lower(),
        "active": prim.IsActive(),
        "kind": str(Usd.ModelAPI(prim).GetKind() or ""),
        "variant_selections": {n: variant_sets.GetVariantSelection(n) for n in variant_sets.GetNames()},
        "arcs": _arcs(prim, names),
    }
    if detail:
        info["variant_sets"] = {n: list(prim.GetVariantSet(n).GetVariantNames()) for n in variant_sets.GetNames()}
        info["prim_stack"] = [
            {"layer": names.layer(spec.layer), "path": str(spec.path)} for spec in prim.GetPrimStack()[:MAX_STACK]
        ]
        info["children"] = [child.GetName() for child in prim.GetAllChildren()][:MAX_ITEMS]
        info["properties"] = [str(name) for name in prim.GetPropertyNames()][:MAX_ITEMS]
        info["is_abstract"] = prim.IsAbstract()
        info["is_instance"] = prim.IsInstance()
        info["has_payload"] = prim.HasAuthoredPayloads()
        info["loaded"] = prim.IsLoaded()
        info["strength"] = explain_strength([arc["arc"] for arc in info["arcs"]])["summary"]
    return info


def _arcs(prim: Any, names: _Names) -> list[dict[str, Any]]:
    query_type = getattr(Usd, "PrimCompositionQuery", None)
    if query_type is None:
        return _arcs_from_index(prim, names)
    arcs = []
    for arc in query_type(prim).GetCompositionArcs():
        node = arc.GetTargetNode()
        target_layer = arc.GetTargetLayer()
        entry = {
            "arc": _arc_name(arc.GetArcType()),
            "target_layer": names.layer(target_layer),
            "target_path": str(arc.GetTargetPrimPath()),
            "introduced_in": names.layer(arc.GetIntroducingLayer()),
            "introduced_at": str(arc.GetIntroducingPrimPath() or ""),
            "has_specs": bool(arc.HasSpecs()),
            "ancestral": bool(arc.IsAncestral()),
            "implicit": bool(arc.IsImplicit()),
        }
        offset = _offset(node.mapToRoot.timeOffset)
        if offset:
            entry["time_offset"] = offset
        arcs.append(entry)
        arcs.extend(_sublayer_arcs(node, target_layer, names))
    return arcs[:MAX_ITEMS]


def _arcs_from_index(prim: Any, names: _Names) -> list[dict[str, Any]]:
    arcs = []
    for info in _strength_nodes(prim, names):
        node = info["node"]
        root_layer = node.layerStack.identifier.rootLayer
        entry = {
            "arc": info["arc"],
            "target_layer": names.layer(root_layer),
            "target_path": str(node.path),
            "has_specs": bool(node.hasSpecs),
            "ancestral": bool(node.IsDueToAncestor()),
        }
        arcs.append(entry)
        arcs.extend(_sublayer_arcs(node, root_layer, names))
    return arcs[:MAX_ITEMS]


def _sublayer_arcs(node: Any, target_layer: Any, names: _Names) -> list[dict[str, Any]]:
    target_id = target_layer.identifier if target_layer is not None else ""
    arcs = []
    for layer in node.layerStack.layers:
        if layer.anonymous or layer.identifier == target_id or not layer.GetPrimAtPath(node.path):
            continue
        arcs.append({
            "arc": "sublayer",
            "target_layer": names.layer(layer),
            "target_path": str(node.path),
            "within": names.layer(target_layer),
            "has_specs": True,
        })
    return arcs


def _offset(layer_offset: Any) -> dict[str, float] | None:
    if layer_offset is None or layer_offset.IsIdentity():
        return None
    return {"offset": float(layer_offset.offset), "scale": float(layer_offset.scale)}


def _strength_nodes(prim: Any, names: _Names) -> list[dict[str, Any]]:
    """Prim index nodes in strength order (depth-first, strongest first) with display info."""
    nodes: list[dict[str, Any]] = []

    def walk(node: Any, chain: tuple[int, ...]) -> None:
        chain = chain + (len(nodes),)
        arc = _arc_name(node.arcType)
        info = {
            "node": node,
            "chain": chain,
            "arc": arc,
            "path": str(node.path),
            "layer_ids": [layer.identifier for layer in node.layerStack.layers],
            "root_layer": names.layer(node.layerStack.identifier.rootLayer),
            "contributes": bool(node.CanContributeSpecs()),
        }
        info["desc"] = _describe(info)
        nodes.append(info)
        for child in node.children:
            walk(child, chain)

    walk(prim.GetPrimIndex().rootNode, ())
    return nodes


def _describe(info: dict[str, Any]) -> str:
    arc, node = info["arc"], info["node"]
    if arc == "local":
        return "local"
    if arc == "variant":
        # An ancestral variant node's path is e.g. /World{layout=wide}Box: the selection sits on a prefix.
        variant_set = selection = ""
        for prefix in reversed(node.path.GetPrefixes()):
            if prefix.IsPrimVariantSelectionPath():
                variant_set, selection = prefix.GetVariantSelection()
                break
        return f"variant {variant_set}={selection}" + (" (ancestral)" if node.IsDueToAncestor() else "")
    if arc in ("reference", "payload"):
        return f"{arc} {info['root_layer']}<{node.path.StripAllVariantSelections()}>"
    # An implied class arc is a copy of an inherit/specialize authored inside an asset, looked up in another (the
    # referencing) layer stack. A specialize Pcp merely propagates to the root keeps its own layer stack: not implied.
    implied = (
        arc in ("inherit", "specialize")
        and node.origin != node.parent
        and node.origin.layerStack != node.layerStack
    )
    return f"{arc} {info['root_layer']}<{info['path']}>" + (" (implied)" if implied else "")


def _phrase(info: dict[str, Any], entry: dict[str, Any]) -> str:
    arc = info["arc"]
    if arc in ("reference", "payload"):
        text = f"{_PHRASES[arc]} {info['desc'].split(' ', 1)[1]}"
        return text if entry["layer"] == info["root_layer"] else f"{text} (layer {entry['layer']})"
    if arc == "variant":
        text = f"the selected variant {info['desc'].split(' ', 1)[1]}"
    elif arc in ("inherit", "specialize") and info["desc"].endswith("(implied)"):
        return (
            f"the {'inherited' if arc == 'inherit' else 'specialized'} class <{info['path']}> in {entry['layer']} "
            f"(implied: the asset's {arc} also reads this class from the root layer stack)"
        )
    else:
        text = f"{_PHRASES.get(arc, arc)} <{info['path']}>"
    return f"{text} in {entry['layer']}"


def _match_node(nodes: list[dict[str, Any]], spec: Any, cursor: int, used: set[tuple[int, str]]) -> int | None:
    layer_id = spec.layer.identifier
    prim_path = str(spec.path.GetParentPath())
    for start, stop in ((cursor, len(nodes)), (0, cursor)):
        for index in range(start, stop):
            info = nodes[index]
            if (
                info["contributes"]
                and info["path"] == prim_path
                and layer_id in info["layer_ids"]
                and (index, layer_id) not in used
            ):
                return index
    return None


def _spec_entries(
    prop: Any, nodes: list[dict[str, Any]], names: _Names, attribute: bool, time: float | None = None
) -> list[dict[str, Any]]:
    # The stack is time-dependent: value clip specs only appear when it is asked for at a numeric time.
    code = Usd.TimeCode.Default() if time is None else Usd.TimeCode(time)
    getter = getattr(prop, "GetPropertyStackWithLayerOffsets", None)
    pairs = list(getter(code)) if getter else [(spec, None) for spec in prop.GetPropertyStack(code)]
    node_layer_ids = {layer_id for info in nodes for layer_id in info["layer_ids"]}
    entries = []
    used: set[tuple[int, str]] = set()
    cursor = 0
    for spec, layer_offset in pairs[:MAX_STACK]:
        index = _match_node(nodes, spec, cursor, used)
        layer_name = names.layer(spec.layer)
        entry: dict[str, Any] = {"layer": layer_name, "path": str(spec.path)}
        if attribute and index is None and spec.layer.identifier not in node_layer_ids:
            # Clip layers belong to no prim index node; their timing comes from the clip 'times' metadata.
            entry.update(
                arc="value clip",
                node_arc="value clip",
                via=f"value clips anchored on <{prop.GetPrim().GetPath()}>",
                clip=True,
            )
        elif index is None:
            entry.update(arc="unknown", node_arc="unknown", via="unknown")
        else:
            cursor = index
            used.add((index, spec.layer.identifier))
            info = nodes[index]
            if index == 0:
                arc = "local" if spec.layer.identifier == names.root_id else (
                    "session" if spec.layer.identifier == names.session_id else "sublayer"
                )
                node_arc = arc
                via = arc if arc != "sublayer" else f"sublayer {layer_name} of the root layer stack"
            else:
                arc, node_arc = nodes[info["chain"][1]]["arc"], info["arc"]
                via = " > ".join(nodes[i]["desc"] for i in info["chain"][1:])
                if layer_name != info["root_layer"]:
                    via += f" (sublayer {layer_name})"
            entry.update(arc=arc, node_arc=node_arc, via=via)
        entry["_node"] = index
        offset = _offset(layer_offset)
        if offset:
            entry["offset"] = offset
        if attribute:
            _attribute_opinion(entry, spec, layer_offset)
        else:
            _relationship_opinion(entry, spec)
        entries.append(entry)
    return entries


def _attribute_opinion(entry: dict[str, Any], spec: Any, layer_offset: Any) -> None:
    has_default = bool(spec.HasDefaultValue())
    default = spec.default if has_default else None
    entry["has_default"] = has_default
    entry["blocked"] = has_default and isinstance(default, Sdf.ValueBlock)
    entry["value"] = None if entry["blocked"] else _jsonable(default)
    entry["spline"] = bool(getattr(spec, "HasSpline", lambda: False)())
    clip = bool(entry.get("clip"))
    offset = layer_offset.offset if layer_offset is not None else 0.0
    scale = layer_offset.scale if layer_offset is not None else 1.0
    samples = []
    for time in list(spec.layer.ListTimeSamplesForPath(spec.path))[:MAX_SAMPLES]:
        samples.append({
            "layer_time": float(time),
            # Clip sample times map to the stage through the clip 'times' metadata, not a layer offset.
            "stage_time": None if clip else float(offset + scale * time),
            "value": _jsonable(spec.layer.QueryTimeSample(spec.path, time)),
        })
    entry["time_samples"] = samples


def _relationship_opinion(entry: dict[str, Any], spec: Any) -> None:
    targets = spec.targetPathList
    entry["explicit"] = bool(getattr(targets, "isExplicit", False))
    for field, key in (
        ("explicitItems", "explicit_targets"),
        ("prependedItems", "prepended"),
        ("appendedItems", "appended"),
        ("deletedItems", "deleted"),
    ):
        entry[key] = [str(path) for path in getattr(targets, field, ()) or ()][:MAX_ITEMS]


def _query(stage: Any, text: str, names: _Names) -> dict[str, Any]:
    raw, _, time_text = text.strip().partition("@")
    raw = raw.strip()
    time = None
    if time_text:
        try:
            time = float(time_text)
        except ValueError:
            time = math.nan
        if not math.isfinite(time):
            return {"query": text, "ok": False, "error": f"time {time_text!r} after '@' must be a number"}
    valid = Sdf.Path.IsValidPathString(raw) if raw.startswith("/") else False
    if not (valid if isinstance(valid, bool) else valid[0]):
        return {"query": text, "ok": False, "error": "queries look like '/World/Box', '/World/Box.size' or '/World/Box.size@24'"}
    path = Sdf.Path(raw)
    if path.IsPrimPath():
        prim = stage.GetPrimAtPath(path)
        if not prim:
            return {"query": text, "ok": False, "error": f"no prim at {raw}"}
        return {"query": text, "ok": True, "query_type": "prim", **_prim_info(prim, names, detail=True)}
    if not path.IsPropertyPath():
        return {"query": text, "ok": False, "error": "query a prim path or a property path"}
    prim = stage.GetPrimAtPath(path.GetPrimPath())
    if not prim:
        return {"query": text, "ok": False, "error": f"no prim at {path.GetPrimPath()}"}
    attribute = stage.GetAttributeAtPath(path)
    if attribute:
        return _attribute_query(text, attribute, prim, time, names)
    relationship = stage.GetRelationshipAtPath(path)
    if relationship:
        return _relationship_query(text, relationship, prim, names)
    return {"query": text, "ok": False, "error": f"{prim.GetPath()} has no property named {path.name!r}"}


def _attribute_query(text: str, attr: Any, prim: Any, time: float | None, names: _Names) -> dict[str, Any]:
    nodes = _strength_nodes(prim, names)
    entries = _spec_entries(attr, nodes, names, attribute=True, time=time)
    code = Usd.TimeCode.Default() if time is None else Usd.TimeCode(time)
    resolve = attr.GetResolveInfo(code)
    opinions = [e for e in entries if e["has_default"] or (time is not None and (e["time_samples"] or e["spline"]))]
    if _enum_name(resolve.GetSource()) == "ResolveInfoSourceValueClips":
        clip = next((e for e in opinions if e.get("clip")), None)
        if clip is not None and opinions[0] is not clip:
            opinions.remove(clip)
            opinions.insert(0, clip)
    winner = opinions[0] if opinions else None
    if winner is not None:
        winner["wins"] = True
    animated = bool(winner and time is not None and (winner["time_samples"] or winner["spline"]))
    blocked = bool(resolve.ValueIsBlocked()) or bool(winner and winner["blocked"] and not animated)
    if str(attr.GetTypeName()).startswith("asset"):
        # Authored paths only: Get() would resolve them and probe files outside the sandbox.
        value, samples = _authored_asset(winner, entries, time)
    else:
        value = attr.Get(code)
        times = list(attr.GetTimeSamples())[:MAX_SAMPLES]
        samples = [{"time": float(t), "value": _jsonable(attr.Get(t))} for t in times]
    result = {
        "query": text,
        "ok": True,
        "query_type": "attribute",
        "path": str(attr.GetPath()),
        "type_name": str(attr.GetTypeName()),
        "time": "default" if time is None else time,
        "value": None if blocked else _jsonable(value),
        "source": _SOURCES.get(_enum_name(resolve.GetSource()), _enum_name(resolve.GetSource())),
        "blocked": blocked,
        "time_samples": samples,
        "winning_layer": winner["layer"] if winner else None,
        "winning_arc": winner["arc"] if winner else None,
        "winning_path": winner["path"] if winner else None,
        "why": _why(opinions, entries, nodes, time, None if blocked else value),
    }
    result["property_stack"] = [_public(e) for e in entries]
    return result


def _authored_asset(
    winner: dict[str, Any] | None, entries: list[dict[str, Any]], time: float | None
) -> tuple[Any, list[dict[str, Any]]]:
    source = next((e for e in entries if not e.get("clip") and (e["has_default"] or e["time_samples"])), None)
    samples = [{"time": s["stage_time"], "value": s["value"]} for s in (source or {}).get("time_samples", [])]
    if winner is None:
        return None, samples
    if winner.get("clip"):
        # Clip sample times need the clip 'times' mapping; do not guess (Get() would resolve files outside the sandbox).
        return None, samples
    if time is not None and winner["time_samples"]:
        held = [s for s in winner["time_samples"] if s["stage_time"] <= time] or winner["time_samples"][:1]
        return held[-1]["value"], samples
    return winner["value"], samples


def _relationship_query(text: str, rel: Any, prim: Any, names: _Names) -> dict[str, Any]:
    nodes = _strength_nodes(prim, names)
    entries = _spec_entries(rel, nodes, names, attribute=False)
    # The strongest explicit list resets everything weaker; only the specs above it still edit on top of it.
    k = next((i for i, e in enumerate(entries) if e["explicit"]), None)
    if not entries:
        why = "no authored target opinions"
    elif k == 0:
        why = f"the explicit target list in {entries[0]['layer']} is the strongest opinion and replaces all weaker edits"
    elif k is None:
        why = (
            f"targets are list-edited: edits from {len(entries)} spec(s) combine, weakest first, "
            "with stronger prepends, appends and deletes applied on top"
        )
    else:
        why = (
            f"the explicit target list in {entries[k]['layer']} replaces every weaker edit; the {k} stronger spec(s) "
            "then apply their prepends, appends and deletes on top of it"
        ) + (f"; {len(entries) - k - 1} weaker spec(s) are ignored" if len(entries) > k + 1 else "")
    forwarded = rel.GetForwardedTargets() if hasattr(rel, "GetForwardedTargets") else []
    return {
        "query": text,
        "ok": True,
        "query_type": "relationship",
        "path": str(rel.GetPath()),
        "targets": [str(p) for p in rel.GetTargets()][:MAX_ITEMS],
        "forwarded_targets": [str(p) for p in forwarded][:MAX_ITEMS],
        "why": why,
        "property_stack": [_public(e) for e in entries],
    }


def _public(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _why(
    opinions: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    time: float | None,
    value: Any,
) -> str:
    if not opinions:
        if time is None and any(e["time_samples"] or e.get("spline") for e in entries):
            tail = "the schema fallback is used" if value is not None else "the attribute has no value"
            ignored = "time samples and splines are" if any(e.get("spline") for e in entries) else "time samples are"
            return f"no default opinion: at the default time only default values count, so {ignored} ignored and {tail}"
        if value is not None:
            return "no authored opinion: the value is the schema fallback"
        return "no authored opinion and no fallback, so the attribute has no value"
    win = opinions[0]
    uses_samples = time is not None and bool(win["time_samples"] or win.get("spline"))
    if win["blocked"] and not uses_samples:
        rest = f" such as {_shown(opinions[1], time)} from {opinions[1]['layer']}" if len(opinions) > 1 else ""
        return f"blocked: {win['layer']} authors a value block (None) on {win['path']}, hiding every weaker opinion{rest}"
    # At the default time, stronger specs that hold only time samples (or a spline) are skipped, not absent.
    position = next(i for i, e in enumerate(entries) if e is win)
    skipped = [] if time is not None else [e for e in entries[:position] if e["time_samples"] or e.get("spline")]
    tail = ""
    if skipped:
        kind = "time samples" if all(e["time_samples"] for e in skipped) else "time samples or a spline"
        tail = (
            f"; the stronger spec(s) in {', '.join(e['layer'] for e in skipped)} hold only {kind}, "
            "which a default-time query ignores"
        )
    if len(opinions) == 1:
        if time is not None and not win["time_samples"] and win.get("spline"):
            return f"only opinion: {format_value(value)} from the spline authored in {win['layer']} ({win['via']})"
        return f"only opinion: {format_value(value)} authored in {win['layer']} ({win['via']}){tail}"
    lose = opinions[1]
    note = ""
    if time is not None and not win["time_samples"] and not win.get("spline") and lose["time_samples"]:
        note = "; a stronger default value beats weaker time samples"
    return f"stronger: {_strength_reason(win, lose, nodes)}{note}{tail}"


def _shown(entry: dict[str, Any], time: float | None) -> str:
    if time is not None and entry["time_samples"]:
        return "time samples"
    if time is not None and entry.get("spline"):
        return "a spline"
    return "None (value block)" if entry["blocked"] else format_value(entry["value"])


def _strength_reason(win: dict[str, Any], lose: dict[str, Any], nodes: list[dict[str, Any]]) -> str:
    if win.get("clip"):
        return (
            f"{win['via']} supply {win['layer']}'s samples at this time; clips are weaker than direct opinions in the "
            f"layer that anchors them but stronger than weaker layers and arcs such as {lose['via']}"
        )
    if lose.get("clip"):
        return (
            f"the direct opinion in {win['layer']} ({win['via']}) beats {lose['via']} from {lose['layer']}: value clips "
            "are weaker than direct opinions in the layer that anchors them"
        )
    wi, li = win["_node"], lose["_node"]
    if wi is None or li is None:
        return f"{win['layer']} comes earlier in the composed property stack than {lose['layer']}"
    if wi == li:
        where = "the root layer stack" if wi == 0 else f"the layer stack of {nodes[wi]['desc']}"
        return (
            f"{win['layer']} is stronger than {lose['layer']} in {where} "
            "(a layer beats its sublayers, and earlier sublayers beat later ones)"
        )
    chain_w, chain_l = nodes[wi]["chain"], nodes[li]["chain"]
    depth = 0
    while depth < min(len(chain_w), len(chain_l)) and chain_w[depth] == chain_l[depth]:
        depth += 1
    if depth == len(chain_l):
        return f"{win['layer']} comes earlier in the composed property stack than {lose['layer']}"
    if depth == len(chain_w):
        arc = nodes[chain_l[depth]]["arc"]
        if wi == 0:
            subject, where = f"{win['arc']} opinion in {win['layer']}", ""
        else:
            subject = f"the opinion authored directly on <{nodes[wi]['path']}> in {win['layer']}"
            where = f"inside {nodes[wi]['desc']}, "
        return (
            f"{where}{subject} beats {_phrase(nodes[chain_l[depth]], lose)} "
            f"(L before {LETTERS.get(arc, '?')}: a prim's own opinions beat the arcs it introduces)"
        )
    top_w, top_l = nodes[chain_w[depth]], nodes[chain_l[depth]]
    where = "" if depth == 1 else f"inside {nodes[chain_w[depth - 1]]['desc']}, "
    if top_w["arc"] != top_l["arc"]:
        return (
            f"{where}{_phrase(top_w, win)} beats {_phrase(top_l, lose)} "
            f"({LETTERS.get(top_w['arc'], '?')} before {LETTERS.get(top_l['arc'], '?')} in LIVERPS)"
        )
    if (
        top_w["arc"] in ("inherit", "specialize")
        and top_w["desc"].endswith("(implied)")
        and not top_l["desc"].endswith("(implied)")
    ):
        article = "an" if top_w["arc"] == "inherit" else "a"
        return (
            f"{where}the copy of class <{top_w['path']}> in the root layer stack ({win['layer']}) beats the asset's own "
            f"class in {lose['layer']}: {article} {top_w['arc']} authored inside an asset is also looked up in every "
            "stronger layer stack that references it, and those implied copies come first"
        )
    return (
        f"{where}both opinions arrive through {top_w['arc']} arcs and {top_w['desc']} is listed "
        f"before {top_l['desc']}, so it is stronger"
    )


def _jsonable(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if Sdf is not None:
        if isinstance(value, Sdf.ValueBlock):
            return None
        if isinstance(value, Sdf.AssetPath):
            return value.path
        if isinstance(value, Sdf.Path):
            return str(value)
    if type(value).__name__ == "TimeCode" and type(value).__module__ in ("pxr.Sdf", "pxr.Gf"):
        return float(value)  # Sdf.TimeCode in USD 25.11, Gf.TimeCode in 26.x
    if depth > 4:
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]}
    if hasattr(value, "GetReal") and hasattr(value, "GetImaginary"):
        return [float(value.GetReal()), *(float(x) for x in value.GetImaginary())]
    if isinstance(value, (list, tuple)) or (hasattr(value, "__len__") and hasattr(value, "__getitem__")):
        count = len(value)
        items = [_jsonable(value[i], depth + 1) for i in range(min(count, MAX_ITEMS))]
        if count > MAX_ITEMS:
            items.append(f"... {count - MAX_ITEMS} more")
        return items
    return str(value)


def _fmt(data: Any) -> str:
    if data is None:
        return "None"
    if isinstance(data, bool):
        return "true" if data else "false"
    if isinstance(data, float):
        if data.is_integer() and abs(data) < 1e15:
            return str(int(data))
        return f"{data:.6g}"
    if isinstance(data, list):
        return "(" + ", ".join(_fmt(item) for item in data) + ")"
    return str(data)
