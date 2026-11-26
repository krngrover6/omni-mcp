"""mdl_info: the real parameters of an MDL material (OmniPBR, OmniGlass, OmniSurface...), read from the installed file.

Composer materials are MDL shaders. Their inputs in USD are `inputs:<parameter>` on the Shader, named exactly as in the
.mdl source, e.g. OmniPBR's `diffuse_color_constant` or `reflection_roughness_constant`. Guessing those names is how
materials silently do nothing, so this reads `export material <Name>( ... )` from the .mdl files of the local Isaac Sim
/ Kit install (kit/mdl/core/...) and returns each parameter's name, MDL type, default, display name, group, range and
whether it is hidden, plus the USD value type it needs (color -> color3f, texture_2d -> asset, ...).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .kit_index import default_root

_SEARCH = ("kit/mdl/core/Base", "kit/mdl/core/mdl", "kit/mdl/core/Volume", "kit/mdl/core")
USD_TYPES = {"color": "color3f", "float": "float", "int": "int", "bool": "bool", "float2": "float2",
             "float3": "float3", "float4": "float4", "int2": "int2", "int3": "int3", "texture_2d": "asset",
             "string": "string"}
# Textures that hold colours are read as sRGB; data maps (normal, roughness, metallic, ORM, opacity...) as raw.
COLOR_TEXTURE_WORDS = ("diffuse", "albedo", "emissive", "color", "base_color")


def texture_color_space(parameter: str) -> str:
    name = parameter.lower()
    return "sRGB" if any(w in name for w in COLOR_TEXTURE_WORDS) and "normal" not in name else "raw"


def _find(material: str, root: Path) -> Path | None:
    for folder in _SEARCH:
        base = root / folder
        if not base.is_dir():
            continue
        direct = base / f"{material}.mdl"
        if direct.is_file():
            return direct
        for hit in base.rglob(f"{material}.mdl"):
            return hit
    return None


def _split_top(text: str) -> list[str]:
    """Split a parameter list on commas that are not inside (), [], {} or strings."""
    parts, depth, current, quote = [], 0, [], None
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current))
    return parts


def _body(source: str, material: str) -> str | None:
    match = re.search(r"export\s+material\s+" + re.escape(material) + r"\s*\(", source)
    if not match:
        return None
    depth, start = 1, match.end()
    for index in range(start, len(source)):
        if source[index] == "(":
            depth += 1
        elif source[index] == ")":
            depth -= 1
            if depth == 0:
                return source[start:index]
    return None


def _annotation(text: str, name: str) -> str | None:
    found = re.search(r"anno::" + name + r"\(\s*\"((?:[^\"\\]|\\.)*)\"", text)
    return found.group(1) if found else None


@lru_cache(maxsize=32)
def _parse(path: str, material: str) -> list[dict[str, Any]]:
    source = Path(path).read_text(encoding="utf-8", errors="replace")
    source = re.sub(r"//[^\n]*", "", source)
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    body = _body(source, material)
    if body is None:
        return []
    params = []
    for raw in _split_top(body):
        annotations = " ".join(re.findall(r"\[\[(.*?)\]\]", raw, flags=re.S))
        decl = re.sub(r"\[\[.*?\]\]", "", raw, flags=re.S).strip()
        if not decl:
            continue
        head, _, default = decl.partition("=")
        words = head.split()
        if len(words) < 2:
            continue
        name, mdl_type = words[-1], words[-2]
        uniform = "uniform" in words[:-2]
        hard = re.search(r"anno::hard_range\(([^)]*\([^)]*\))\s*,\s*([^)]*\([^)]*\))\)", annotations) or \
            re.search(r"anno::hard_range\(([^,]+),([^)]+)\)", annotations)
        params.append({
            "name": name, "mdl_type": mdl_type, "usd_type": USD_TYPES.get(mdl_type), "uniform": uniform,
            "default": " ".join(default.split()) or None,
            "display_name": _annotation(annotations, "display_name"),
            "group": _annotation(annotations, "in_group"),
            "description": _annotation(annotations, "description"),
            "hidden": "anno::hidden" in annotations,
            "hard_range": [hard.group(1).strip(), hard.group(2).strip()] if hard else None,
        })
    return params


def mdl_parameters(material: str) -> tuple[Path | None, list[dict[str, Any]]]:
    root = default_root()
    path = _find(material, root)
    return (path, _parse(str(path), material)) if path else (None, [])


def mdl_info(material: str = "OmniPBR", group: Any = None, include_hidden: Any = False) -> dict[str, Any]:
    """The real inputs of an installed MDL material (OmniPBR, OmniGlass, OmniSurfaceBase...): each parameter's name
    (use it as inputs:<name> on the Shader), MDL type, the USD value type it needs, default, display name, group and
    range, read from the .mdl file of the local Kit / Isaac Sim install. group filters, e.g. "Albedo"."""
    if not isinstance(material, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", material):
        return {"ok": False, "error": "material must be an MDL material name like OmniPBR"}
    path, params = mdl_parameters(material)
    if path is None:
        return {"ok": False, "error": f"No {material}.mdl in the local install ({default_root()})"}
    shown = [p for p in params if (include_hidden is True or not p["hidden"]) and
             (not isinstance(group, str) or (p["group"] or "").lower() == group.lower())]
    groups: dict[str, int] = {}
    for p in params:
        groups[p["group"] or "-"] = groups.get(p["group"] or "-", 0) + 1
    return {"ok": True, "material": material, "file": str(path), "parameters": len(params), "groups": groups,
            "inputs": [{k: v for k, v in p.items() if v not in (None, False)} for p in shown][:120],
            "how_to_use": f'In a spec material: {{"path": ..., "mdl": "{material}", "inputs": {{"<name>": value}}}}. '
                          "Textures take a file path; colour maps are read as sRGB, data maps (normal, roughness) "
                          "as raw."}
