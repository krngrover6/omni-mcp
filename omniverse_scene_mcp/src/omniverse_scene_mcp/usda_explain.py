"""usda_explain: read a .usda file line by line in plain words, for a beginner (read-only).

Every study day ends with "read the .usda". This tool explains each meaningful line the way a teacher would: what the
keyword means, the full path of each prim (worked out from the braces), whether an attribute belongs to the prim's
schema or is custom, what the value type means (double vs float, token, arrays), and what composition arcs, time
samples, relationships and variants do. Facts about schemas (is this attribute part of Cube? its fallback?) come from
the real USD schema registry, never from guesses. Lines it has nothing to add to (braces, blank lines) are skipped.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from pxr import Sdf, Usd
except ImportError:  # pragma: no cover
    Sdf = Usd = None

MAX_TEXT_CHARS = 400_000
MAX_LINES = 250

TYPES = {
    "double": "a decimal number stored precisely (64-bit). Most transform and size values use it.",
    "float": "a decimal number stored with less precision (32-bit). Colors, radii of lights, many shader inputs.",
    "int": "a whole number (no decimals).",
    "bool": "true or false.",
    "string": "free text in quotes.",
    "token": "a word from a short, fixed list (e.g. \"Y\" or \"inherited\"). USD stores it once and reuses it: fast to "
             "compare.",
    "asset": "a file path written between @ signs, e.g. @./tex.png@.",
    "double3": "three precise numbers (x, y, z).",
    "float3": "three decimal numbers (x, y, z), less precise.",
    "point3f": "a position in space: three floats. Used for mesh points.",
    "vector3f": "a direction (not a position): three floats.",
    "normal3f": "a direction a surface faces: three floats.",
    "color3f": "a color: red, green, blue between 0 and 1.",
    "texCoord2f": "a UV coordinate: where on a texture image this point picks its color.",
    "quatf": "a rotation stored as a quaternion (4 numbers). The Curve Editor cannot key it.",
    "quatd": "a rotation stored as a precise quaternion (4 numbers).",
    "matrix4d": "a 4x4 grid of numbers holding a whole transform (move + rotate + scale) at once.",
    "timecode": "a time on the timeline, in time codes (frames).",
}
METADATA = {
    "defaultPrim": "the prim used when another file references this file without naming a prim.",
    "upAxis": "which axis points up in this file (\"Y\" or \"Z\"). Read it, never assume it.",
    "metersPerUnit": "how many metres one unit is: 0.01 = centimetres, 1 = metres.",
    "timeCodesPerSecond": "how many time codes (frames) make one second when the timeline plays.",
    "startTimeCode": "the first time code of the animation range.",
    "endTimeCode": "the last time code of the animation range.",
    "framesPerSecond": "the playback rate the file asks for.",
    "doc": "a comment stored in the file (any text, for people).",
    "subLayers": "other layers stacked under this one: the first in the list is the strongest.",
    "customLayerData": "free extra information an app (like Composer) keeps in the file.",
    "kind": "the role of this prim in an asset: component (one asset), group, assembly, subcomponent.",
    "instanceable": "true = USD may share one copy of this prim's contents among all its instances.",
    "active": "false = the prim is switched off: USD ignores it and everything under it.",
    "hidden": "a hint for apps to hide the prim from lists; it does NOT hide it in the viewport.",
}
ARCS = {
    "references": "brings in another file's prim (or a prim in this file) as if it were written here. A local edit "
                  "in this file wins over it.",
    "payload": "like a reference, but the app may leave it unloaded to save memory until you ask for it.",
    "payloads": "like a reference, but the app may leave it unloaded to save memory until you ask for it.",
    "inherits": "this prim receives everything the class prim has; editing the class changes all who inherit it.",
    "specializes": "like inherits, but weaker: anything written on this prim or its references wins over it.",
    "variantSets": "names a set of switchable versions (like paint colors); one variant is picked at a time.",
    "apiSchemas": "extra abilities stuck onto this prim (e.g. MaterialBindingAPI, PhysicsRigidBodyAPI).",
}
SPECIFIERS = {
    "def": "creates the prim (defines it).",
    "over": "only changes a prim that something else creates; it never creates one on its own.",
    "class": "a template prim: not drawn itself; others inherit from it.",
}
_PRIM = re.compile(r'^\s*(def|over|class)\s+(?:(\w+)\s+)?"([^"]+)"')
_ATTR = re.compile(r"^\s*(uniform\s+|custom\s+|varying\s+)*([A-Za-z_][\w]*(?:\[\])?)\s+([A-Za-z_][\w:]*)"
                   r"(\.timeSamples|\.connect)?\s*(=.*)?$")
_REL = re.compile(r"^\s*(custom\s+)?rel\s+([A-Za-z_][\w:]*)\s*(=\s*(.*))?$")
_META = re.compile(r"^\s*(prepend\s+|append\s+|delete\s+|add\s+)?([A-Za-z_]\w*)\s*=\s*(.*)$")
_VSET = re.compile(r'^\s*variantSet\s+"([^"]+)"\s*=')
_VARIANT = re.compile(r'^\s*"([^"]+)"\s*(\(|\{)')


def _from_definition(definition: Any, attr: str) -> tuple[bool, Any]:
    getter = getattr(definition, "GetAttributeDefinition", None)
    if getter is not None:
        spec = getter(attr)
        return (True, spec.GetFallbackValue()) if spec else (False, None)
    spec = definition.GetSchemaAttributeSpec(attr)
    return (True, spec.default) if spec else (False, None)


def _attr_info(type_name: str | None, apis: list[str], attr: str) -> tuple[str, Any] | None:
    """(schema it comes from, fallback) when the attribute belongs to the prim's type or one of its applied APIs."""
    if Usd is None:
        return None
    registry = Usd.SchemaRegistry()
    if type_name:
        definition = registry.FindConcretePrimDefinition(type_name)
        if definition is not None:
            found, fallback = _from_definition(definition, attr)
            if found:
                return type_name, fallback
    for api in apis:
        name, _, instance = api.partition(":")
        definition = registry.FindAppliedAPIPrimDefinition(name)
        if definition is None:
            continue
        lookup = attr
        if instance and attr.count(":") >= 2:  # multiple-apply, e.g. drive:angular:physics:damping
            parts = attr.split(":")
            if len(parts) > 2 and parts[1] == instance:
                lookup = ":".join([parts[0], "__INSTANCE_NAME__"] + parts[2:])
        found, fallback = _from_definition(definition, lookup)
        if found:
            return f"{name}" + (f" ({instance})" if instance else ""), fallback
    return None


def _under(path: str | None, prim: str) -> bool:
    """path is prim itself or inside it (/World/Hinge2 is not inside /World/Hinge)."""
    return bool(path) and (path == prim or path.startswith(prim.rstrip("/") + "/"))


def _type_words(type_name: str) -> str:
    base = type_name[:-2] if type_name.endswith("[]") else type_name
    words = TYPES.get(base, f"a {base} value.")
    return ("a LIST of values, each one " + words[0].lower() + words[1:]) if type_name.endswith("[]") else words


def explain_usda(text: Any = None, path: Any = None, prim: Any = None) -> dict[str, Any]:
    """Explain a .usda (text, or a saved .usda/.usd file by path) line by line in plain words for a beginner: keywords,
    each prim's full path, which attributes belong to the prim's schema (with their fallback) or are custom, value
    types, uniform, time samples, relationships, composition arcs and variants. prim="/World/Box" explains only that
    prim's block. Read-only."""
    if Usd is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available in the router's Python"}
    if (text is None) == (path is None):
        return {"ok": False, "error": "give exactly one of text or path"}
    if path is not None:
        file = Path(str(path))
        if file.suffix.lower() not in (".usd", ".usda", ".usdc") or not file.is_file():
            return {"ok": False, "error": f"path must be an existing .usd, .usda or .usdc file: {path}"}
        if file.suffix.lower() == ".usda":
            text = file.read_text(encoding="utf-8", errors="replace")
        else:  # a binary crate: show it as text first (the same thing the file holds)
            layer = Sdf.Layer.FindOrOpen(str(file))
            if layer is None:
                return {"ok": False, "error": f"USD could not open {path}"}
            text = layer.ExportToString()
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text must be non-empty .usda text"}
    if len(text) > MAX_TEXT_CHARS:
        return {"ok": False, "error": f"text is longer than {MAX_TEXT_CHARS} characters"}
    if prim is not None and (not isinstance(prim, str) or not prim.startswith("/")):
        return {"ok": False, "error": "prim must be a path like /World/Box"}

    out: list[dict[str, Any]] = []
    stack: list[list[Any]] = []  # [path, type, applied APIs] of the prims whose braces we are inside
    pending: list[Any] | None = None  # a prim header seen, waiting for its {
    in_header = text.lstrip().startswith("#usda")
    paren = 0  # inside ( ... ): settings (metadata) of the file or of a prim
    dictionary = 0  # inside a metadata dictionary like customLayerData = { ... }: app data, not prims
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        current = stack[-1] if stack else [None, None, []]
        inside = prim is None or _under(current[0], prim) or (pending is not None and _under(pending[0], prim))
        note = None
        if dictionary:
            dictionary += line.count("{") - line.count("}")
            continue  # inside an app's settings dictionary: nothing for a beginner to read
        if line.startswith("#usda"):
            note = "Says this is a USD text file (version 1.0). Every .usda starts with it."
        elif line.startswith("#"):
            note = "A comment: people read it, USD ignores it."
        elif (match := _PRIM.match(line)):
            specifier, type_name, name = match.groups()
            parent = current[0] or ""
            full = f"{parent}/{name}"
            pending = [full, type_name, []]
            paren += line.count("(") - line.count(")")
            kind = f"of type {type_name}" if type_name else "with no type (a plain container)"
            note = (f"`{specifier}` {SPECIFIERS[specifier]} Prim `{name}` {kind}; its full path is `{full}` "
                    f"({'inside ' + parent if parent else 'at the top of the file'}).")
            inside = prim is None or _under(full, prim)
        elif line.startswith("{"):
            if pending:
                stack.append(pending)
                pending = None
            continue
        elif line.startswith("}"):
            if stack:
                stack.pop()
            continue
        elif (match := _VSET.match(line)):
            note = (f"The variant set `{match.group(1)}` starts here: each block below is one version; only the "
                    "selected one is used.")
        elif (match := _REL.match(line)):
            custom, name, _, target = match.groups()
            what = ("points at the material this prim is drawn with (needs MaterialBindingAPI in apiSchemas)."
                    if name.startswith("material:binding") else "points at other prims (a link, not a value).")
            note = f"`rel {name}` is a relationship: it {what}" + (f" Target: {target.strip()}." if target else "")
        elif (match := _META.match(line)) and (match.group(2) in METADATA or match.group(2) in ARCS
                                                 or match.group(2) == "variants"):
            listop, key, value = match.groups()
            if value.rstrip().endswith("{") and value.count("{") > value.count("}"):
                dictionary = value.count("{") - value.count("}")  # its lines are skipped until it closes
            if key == "apiSchemas" and pending is not None:
                pending[2] = re.findall(r'"([^"]+)"', value)
            meaning = METADATA.get(key) or ARCS.get(key) or "which version of each variant set is picked."
            how = ""
            if listop:
                how = {"prepend": " `prepend` puts these first (strongest) in the list from weaker layers.",
                       "append": " `append` adds these at the end (weakest).",
                       "delete": " `delete` removes these from the list.",
                       "add": " `add` adds these to the list."}[listop.strip()]
            note = f"`{key}`: {meaning}{how} Value: {value.strip()[:120]}"
        elif paren == 0 and stack and (match := _ATTR.match(line)) and match.group(2) not in ("rel", "dictionary"):
            modifiers, type_name, name, suffix, value = match.groups()
            modifiers = modifiers or ""
            words = [f"Attribute `{name}`, type `{type_name}`: {_type_words(type_name)}"]
            if "uniform" in modifiers:
                words.append("`uniform` = one value for all time (it cannot be animated).")
            info = _attr_info(current[1], current[2], name)
            if "custom" in modifiers:
                words.append("`custom` = your own data, not part of any schema.")
            elif info:
                fallback = info[1]
                words.append(f"It is part of the {info[0]} schema; if nothing is written its fallback is "
                             f"{fallback!r}." if fallback is not None else f"It is part of the {info[0]} schema.")
            elif name.startswith("primvars:"):
                words.append("A primvar: extra data that travels along the surface (colors, UVs) to the renderer.")
            elif name.startswith("xformOp:"):
                words.append("A transform step (move / rotate / scale); xformOpOrder says which steps run, in order.")
            elif name.startswith(("inputs:", "outputs:")):
                words.append("A shader connection point: an input sets a material setting, an output sends a result.")
            if suffix == ".timeSamples":
                words.append("`.timeSamples` = animation keys: {time: value, ...}. Between keys USD blends; after the "
                             "last key it holds the last value.")
            elif suffix == ".connect":
                words.append("`.connect` = this input takes its value from another attribute (wired like a cable).")
            elif value and value.strip() == "=":
                pass
            if name == "xformOpOrder":
                words = ["`xformOpOrder` lists which transform steps (xformOp:...) are used and in which order; "
                         "a step not in this list is ignored."]
            note = " ".join(words)
        elif line.startswith("(") or line.startswith(")"):
            paren = max(0, paren + line.count("(") - line.count(")"))
            if in_header and number <= 3 and line.startswith("("):
                note = "Starts the file's settings (metadata) block: values that apply to the whole file."
        if note and inside and len(out) < MAX_LINES:
            out.append({"line": number, "text": raw.rstrip()[:160], "means": note})
        # `name = {` opens a dictionary (customData, timeSamples...): skip its inner lines, so its } is not taken
        # for the end of a prim.
        if "=" in line and line.endswith("{") and not _PRIM.match(line) and not dictionary:
            dictionary = line.count("{") - line.count("}")
    prim_paths = []
    for item in out:
        found = re.search(r"full path is `([^`]+)`", item["means"])
        if found:
            prim_paths.append(found.group(1))
    return {"ok": True, "lines_explained": len(out), "truncated": len(out) >= MAX_LINES, "prims": prim_paths[:60],
            "explanation": out,
            "how_to_read": "Read top to bottom. Braces { } hold what is inside a prim; parentheses ( ) hold settings "
                           "(metadata) about the prim or file; = gives a value. To check the file for mistakes, use "
                           "usda_check."}
