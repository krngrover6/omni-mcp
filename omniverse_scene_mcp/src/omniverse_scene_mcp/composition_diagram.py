"""Composition picture: draw how a prim is composed (read-only).

scene_composition and scene_explain_value describe composition as lists. This draws the same facts as a picture:
the prim's composition tree (OpenUSD calls it the prim index), one box per place an opinion can come from - the
local layer stack, each reference, payload, inherit, specialize and variant - numbered in strength order (1 is the
strongest, LIVERPS). With an attribute, the box whose opinion wins is highlighted. You get Mermaid text (paste it
into any Mermaid viewer, GitHub or a Markdown preview) and the same tree as plain text for the terminal.
"""

from __future__ import annotations

from pathlib import PurePath
from typing import Any

try:
    from pxr import Sdf, Usd
except ImportError:  # pragma: no cover
    Sdf = Usd = None

from .exam.sandbox import _arc_name

MAX_NODES = 60
_LETTER = {"local": "L", "inherit": "I", "variant": "V", "relocate": "E", "reference": "R", "payload": "P",
           "specialize": "S"}
_PLAIN = {"local": "the prim itself, in the open layers", "reference": "a reference (another file or prim pulled in)",
          "payload": "a payload (a reference you can unload)", "inherit": "an inherit (a shared class it follows)",
          "specialize": "a specialize (a weak fallback base)", "variant": "a variant (the chosen option of a set)",
          "relocate": "a relocate (a moved namespace)"}


def _layer_name(layer: Any) -> str:
    if layer is None:
        return "?"
    if layer.anonymous:
        return "(in memory)"
    return PurePath(layer.identifier.replace("\\", "/")).name or layer.identifier


def _winning_spec(prim: Any, attribute: str) -> tuple[str, str] | None:
    attr = prim.GetAttribute(attribute)
    if not attr:
        return None
    for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
        if spec.HasDefaultValue() or spec.layer.GetNumTimeSamplesForPath(spec.path):
            return spec.layer.identifier, str(spec.path.GetPrimPath())
    return None


def _quote(text: str) -> str:
    return text.replace('"', "'").replace("<", "&lt;").replace(">", "&gt;")


def diagram(stage: Any, prim_path: str, attribute: str | None = None) -> dict[str, Any]:
    """Read-only: the prim's composition as a picture (Mermaid + text tree), strongest opinion first; with attribute,
    the box that supplies its value is marked."""
    if Usd is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    if stage is None:
        return {"ok": False, "error": "No stage is open"}
    if not isinstance(prim_path, str) or not Sdf.Path.IsValidPathString(prim_path):
        return {"ok": False, "error": "prim_path must be a prim path like /World/Lamp."}
    prim = stage.GetPrimAtPath(prim_path)
    if not prim:
        return {"ok": False, "error": f"No prim at {prim_path}."}
    winner = _winning_spec(prim, attribute) if attribute else None
    if attribute and not prim.GetAttribute(attribute):
        return {"ok": False, "error": f"{prim_path} has no attribute {attribute}."}
    nodes: list[dict[str, Any]] = []

    def walk(node: Any, parent: int | None, depth: int) -> None:
        if len(nodes) >= MAX_NODES:
            return
        arc = _arc_name(node.arcType)
        layers = list(node.layerStack.layers)
        index = len(nodes)
        has_specs = any(layer.GetPrimAtPath(node.path) for layer in layers)
        holds_winner = bool(winner) and str(node.path) == winner[1] and any(
            layer.identifier == winner[0] for layer in layers)
        nodes.append({
            "strength": index + 1, "parent": parent, "depth": depth, "arc": arc, "letter": _LETTER.get(arc, "?"),
            "path": str(node.path), "layers": [_layer_name(layer) for layer in layers if layer.GetPrimAtPath(node.path)]
            or [_layer_name(node.layerStack.identifier.rootLayer)],
            "has_opinions": has_specs, "wins": holds_winner,
            "meaning": _PLAIN.get(arc, arc),
        })
        for child in node.children:
            walk(child, index, depth + 1)

    walk(prim.GetPrimIndex().rootNode, None, 0)
    lines = ["flowchart TD"]
    tree = []
    for node in nodes:
        # Seen live in Composer: quoting the joined label escaped the <br/> too, so Mermaid showed "&lt;br/&gt;".
        parts = [f"{node['strength']}. {node['letter']} {node['arc']}", node["path"], ", ".join(node["layers"])]
        if node["wins"]:
            parts.append(f"WINS for {attribute}")
        label = "<br/>".join(_quote(part) for part in parts)
        lines.append(f'  n{node["strength"]}["{label}"]')
        if node["parent"] is not None:
            lines.append(f"  n{node['parent'] + 1} -->|{node['arc']}| n{node['strength']}")
        if not node["has_opinions"]:
            lines.append(f"  style n{node['strength']} stroke-dasharray: 4 3")
        if node["wins"]:
            lines.append(f"  style n{node['strength']} stroke-width:4px")
        tree.append("  " * node["depth"] + f"{node['strength']}. [{node['letter']}] {node['arc']:<10} {node['path']}  "
                    f"({', '.join(node['layers'])})" + ("  <- WINS" if node["wins"] else "")
                    + ("" if node["has_opinions"] else "  (no opinions here)"))
    arcs = sorted({n["arc"] for n in nodes if n["arc"] != "local"})
    return {
        "ok": True, "read_only": True, "prim": prim_path, "attribute": attribute,
        "nodes": nodes, "truncated": len(nodes) >= MAX_NODES,
        "mermaid": "\n".join(lines), "text_tree": "\n".join(tree),
        "how_to_read": [
            "Each box is one place opinions can come from; the number is its strength (1 = strongest).",
            "USD reads boxes in that order and the first one with a value wins (LIVERPS: Local, Inherits, "
            "VariantSets, rElocates, References, Payloads, Specializes; inside one box, stronger layers first).",
            "Dashed boxes are arcs that exist but hold no opinions for this prim.",
        ] + ([f"The thick box supplies {attribute}." if winner else
              f"No box authors {attribute}: it uses its fallback (schema default)."] if attribute else []),
        "arcs_used": [f"{a}: {_PLAIN.get(a, a)}" for a in arcs] or ["none: everything is authored locally"],
        "view_it": "Paste 'mermaid' into https://mermaid.live or a Markdown ```mermaid block; 'text_tree' reads in "
                   "any terminal.",
    }
