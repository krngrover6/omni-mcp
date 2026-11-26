"""usda_check: read .usda text (or a saved .usd/.usda file) and check it against the real USD schemas, read-only.

What it catches, each with the prim path, the line when it can be found, and a plain fix:
- text that USD cannot parse (with USD's own error and line),
- a prim type that does not exist (`def Cub "Box"`) and API schemas that do not exist,
- an attribute that is not part of its prim's schema or applied APIs (`double size` on a Mesh, `radiuss` on a Sphere),
- a schema attribute written with the wrong value type (`float size` on a Cube: size is double) or the wrong
  variability (`token axis` without `uniform`),
- material:binding on a prim without MaterialBindingAPI (USD warns and newer versions ignore it),
- a defaultPrim that names no root prim, and missing upAxis / metersPerUnit (read them, never assume).
Attributes marked `custom`, primvars:, xformOp:, inputs:/outputs: and userProperties: are free-form and skipped.
Schemas that only Kit ships (PhysX, Omni, Isaac) are unknown to plain USD: those are noted, not flagged.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

try:
    from pxr import Sdf, Tf, Usd
except ImportError:  # pragma: no cover
    Sdf = Tf = Usd = None

MAX_TEXT_CHARS = 400_000
MAX_PROBLEMS = 40
_FREE = ("primvars:", "xformOp:", "inputs:", "outputs:", "userProperties:", "info:", "config:", "ui:")
_KIT_PREFIXES = ("physx", "Physx", "omni", "Omni", "isaac", "Isaac", "nv", "Nv", "mdl", "semantic", "Semantics")


def _schema_names() -> tuple[list[str], list[str]]:
    typed, api = [], []
    for kind, out in (("UsdTyped", typed), ("UsdAPISchemaBase", api)):
        for tf_type in Tf.Type.FindByName(kind).GetAllDerivedTypes():
            name = Usd.SchemaRegistry.GetSchemaTypeName(tf_type)
            if name:
                out.append(str(name))
    return sorted(set(typed)), sorted(set(api))


def _line_of(lines: list[str], prim_name: str, token: str | None = None) -> int | None:
    """Best-effort line number: the prim's def/over line, then the first line after it that has the token."""
    start = None
    pattern = re.compile(r'^\s*(def|over|class)\b[^"]*"' + re.escape(prim_name) + '"')
    for index, line in enumerate(lines):
        if pattern.search(line):
            start = index
            break
    if start is None:
        return None
    if token is None:
        return start + 1
    word = re.compile(r"(?<![\w:])" + re.escape(token) + r"(?![\w:])")
    for index in range(start + 1, min(len(lines), start + 400)):
        if word.search(lines[index]):
            return index + 1
    return start + 1


def _attr_def(definition: Any, name: str) -> tuple[Any, Any] | None:
    """(type name, variability) of a schema attribute, or None."""
    getter = getattr(definition, "GetAttributeDefinition", None)
    if getter is not None:
        attr = getter(name)
        if attr:
            return attr.GetTypeName(), attr.GetVariability()
        return None
    spec = definition.GetSchemaAttributeSpec(name)
    return (spec.typeName, spec.variability) if spec else None


def check_usda(text: Any = None, path: Any = None) -> dict[str, Any]:
    """Check .usda text (or a saved .usd/.usda/.usdc file by path) against the real USD schemas: unknown prim types and
    API schemas, attributes that are not part of the prim's schema (e.g. `double size` on a Mesh), wrong value types
    (`float size` on a Cube) or variability, material:binding without MaterialBindingAPI, and stage metadata. Read-only;
    each problem has the prim path, the line when found, and a fix."""
    if Usd is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available in the router's Python"}
    if (text is None) == (path is None):
        return {"ok": False, "error": "give exactly one of text (the .usda text) or path (a .usd/.usda/.usdc file)"}
    if path is not None:
        file = Path(str(path))
        if file.suffix.lower() not in (".usd", ".usda", ".usdc") or not file.is_file():
            return {"ok": False, "error": f"path must be an existing .usd, .usda or .usdc file: {path}"}
        layer = Sdf.Layer.FindOrOpen(str(file))
        if layer is None:
            return {"ok": False, "error": f"USD could not open {path}"}
        # Lines must match the file the user opens: read a .usda's own text (USD's re-export reformats it).
        text = file.read_text(encoding="utf-8", errors="replace") if file.suffix.lower() == ".usda" \
            else layer.ExportToString()
        source = str(file)
        added = 0
    else:
        if not isinstance(text, str) or not text.strip():
            return {"ok": False, "error": "text must be non-empty .usda text"}
        if len(text) > MAX_TEXT_CHARS:
            return {"ok": False, "error": f"text is longer than {MAX_TEXT_CHARS} characters"}
        added = 0
        if not text.lstrip().startswith("#usda"):
            text, added = "#usda 1.0\n" + text, 1
        layer = Sdf.Layer.CreateAnonymous("check.usda")
        source = "text"
        try:
            parsed = layer.ImportFromString(text)
        except Tf.ErrorException as exc:
            raw = str(exc)
            # USD's message names its own C++ file and line first; the text position is ':LINE:COL: parse error ...'.
            match = re.search(r":(\d+):(\d+): (.*?)(?: at '| in <|$)", raw)
            where = re.search(r"at '([^']*)' in <([^>]*)>", raw)
            line = int(match.group(1)) - added if match else None
            what = match.group(3).replace("pxrInternal_v0_", "").strip() if match else raw.strip().splitlines()[-1]
            what = re.sub(r"struct \S*::(\w+)", r"\1", what)
            near = f" near {where.group(1)!r} in {where.group(2)}" if where else ""
            return {"ok": True, "verdict": "parse_error", "source": source,
                    "problems": [{"code": "parse_error", "line": line,
                                  "message": f"USD cannot read this text at line {line}: {what}{near}."}],
                    "next_step": "Fix the text at that line first (a missing quote, brace or bracket is the usual cause)."}
        if not parsed:
            return {"ok": True, "verdict": "parse_error", "source": source,
                    "problems": [{"code": "parse_error", "line": None, "message": "USD cannot read this text."}],
                    "next_step": "Check quotes, braces and brackets."}
    lines = text.splitlines()
    typed_names, api_names = _schema_names()
    stage = Usd.Stage.Open(layer, Usd.Stage.LoadNone)
    problems: list[dict[str, Any]] = []
    notes: list[str] = []

    def add(code: str, path: Any, message: str, *, token: str | None = None, fix: str = "",
            suggestions: list[str] | None = None) -> None:
        if len(problems) >= MAX_PROBLEMS:
            return
        prim_name = path.name if hasattr(path, "name") else str(path)
        hint = (" Did you mean " + " or ".join(f"`{s}`" for s in suggestions) + "?") if suggestions else ""
        found = _line_of(lines, prim_name, token)
        line = found - added if isinstance(found, int) else None  # lines of the text the user gave, not our header
        problems.append({"code": code, "prim": str(path), "line": line,
                         "message": message + hint, "fix": fix, "did_you_mean": suggestions or []})

    specs: list[Any] = []
    layer.Traverse(Sdf.Path.absoluteRootPath, lambda p: specs.append(layer.GetPrimAtPath(p)) if p.IsPrimPath() else None)
    checked_attrs = 0
    for spec in specs:
        if spec is None:
            continue
        path = spec.path
        type_name = spec.typeName
        prim = stage.GetPrimAtPath(path)
        definition = prim.GetPrimDefinition() if prim else None
        if type_name and type_name not in typed_names:
            close = difflib.get_close_matches(type_name, typed_names, n=3, cutoff=0.7)
            if type_name.startswith(_KIT_PREFIXES) or not close:
                notes.append(f"{path}: type {type_name} is not in plain USD (a Kit/PhysX type?); not checked.")
                continue
            add("unknown_type", path, f"There is no prim type {type_name!r} in USD {_version()}.",
                token=None, fix="Use a real type name (Xform, Scope, Cube, Sphere, Mesh, Camera, DistantLight...).",
                suggestions=close)
            continue
        applied = [str(a) for a in spec.GetInfo("apiSchemas").GetAddedOrExplicitItems()] \
            if spec.HasInfo("apiSchemas") else []
        kit_api = False
        for api in applied:
            base = api.split(":")[0]
            if base in api_names:
                continue
            close = difflib.get_close_matches(base, api_names, n=3, cutoff=0.85)
            if base.startswith(_KIT_PREFIXES) or not close:
                # Not near any USD name: a schema only Kit or Isaac ships (seen: NewtonSceneAPI on Isaac 6.1 scenes).
                kit_api = True
                notes.append(f"{path}: API schema {api} is not in plain USD (a Kit/Isaac schema?); its attributes "
                             "are not checked.")
                continue
            add("unknown_api", path, f"There is no API schema {base!r} in USD {_version()}.", token=base,
                fix="Fix the name in apiSchemas = [...].", suggestions=close)
        if not type_name and not applied:
            continue  # a typeless prim (or an over) has no schema to check against
        names = set(definition.GetPropertyNames()) if definition else set()
        for attr in spec.attributes:
            checked_attrs += 1
            name = attr.name
            if attr.custom or name.startswith(_FREE):
                continue
            found = _attr_def(definition, name) if definition else None
            if found is None:
                if kit_api or name.split(":")[0].startswith(_KIT_PREFIXES) or ":" in name and not type_name:
                    continue
                spaces = {n.split(":")[0] for n in names if ":" in n}
                if ":" in name and name.split(":")[0] not in spaces:
                    continue  # app data in its own namespace (seen: Composer's ground:size, subsetFamily:...)
                schema = type_name or "its applied schemas"
                add("not_in_schema", path, f"{name!r} is not an attribute of {schema} (or its applied APIs).",
                    token=name, fix=f"Use a real {schema} attribute, or write `custom {attr.typeName} {name}` if it is "
                                    "your own data.",
                    suggestions=difflib.get_close_matches(name, sorted(n for n in names if not n.startswith("__")),
                                                          n=3, cutoff=0.6))
                continue
            want_type, want_var = found
            if want_type and attr.typeName != want_type and str(attr.typeName) != str(want_type):
                add("wrong_type", path, f"{name!r} is {want_type} in the {type_name or 'schema'} schema, but this "
                                        f"file writes it as {attr.typeName}.", token=name,
                    fix=f"Write `{want_type} {name}` (USD will not read a {attr.typeName} as the schema value).")
            elif want_var is not None and attr.variability != want_var:
                word = "uniform" if want_var == Sdf.VariabilityUniform else "varying"
                add("wrong_variability", path, f"{name!r} is {word} in the schema, but this file writes it "
                                               f"{'without' if word == 'uniform' else 'with'} `uniform`.", token=name,
                    fix=f"Write `{'uniform ' if word == 'uniform' else ''}{want_type} {name}`.")
        for rel in spec.relationships:
            if rel.name.startswith("material:binding") and "MaterialBindingAPI" not in applied and not rel.custom:
                add("binding_without_api", path, "material:binding is set but the prim does not apply "
                                                 "MaterialBindingAPI.", token="material:binding",
                    fix='Add prepend apiSchemas = ["MaterialBindingAPI"] to the prim (UsdShade.MaterialBindingAPI.Apply '
                        "does it in Python).")
    info: dict[str, Any] = {}
    for key in ("upAxis", "metersPerUnit", "defaultPrim", "timeCodesPerSecond", "startTimeCode", "endTimeCode"):
        if layer.pseudoRoot.HasInfo(key):
            info[key] = layer.pseudoRoot.GetInfo(key)
    roots = [p.name for p in layer.rootPrims]
    # Composed, so a root prim that comes from a sublayer counts (seen: lamp_payload.usda gets Lamp from its sublayers).
    if "defaultPrim" in info and not stage.GetPrimAtPath(Sdf.Path.absoluteRootPath.AppendChild(info["defaultPrim"])):
        add("bad_default_prim", "/", f"defaultPrim is {info['defaultPrim']!r} but no root prim has that name.",
            fix=f"Set defaultPrim to one of {roots[:5]} (a reference to this file uses it).")
    missing = [k for k in ("upAxis", "metersPerUnit") if k not in info]
    if missing and roots:
        notes.append("No " + " or ".join(missing) + " in this layer: USD falls back to Y up / centimetres (0.01), and "
                     "Isaac uses Z up / metres. Read them, never assume.")
    problems.sort(key=lambda p: (p["line"] is None, p["line"] or 0))
    return {
        "ok": True, "verdict": "problems" if problems else "ok", "source": source, "usd_version": _version(),
        "prims": len(specs), "attributes_checked": checked_attrs, "stage_metadata": info, "root_prims": roots[:20],
        "problems": problems, "notes": notes[:15],
        "next_step": (f"{problems[0]['prim']}: {problems[0]['message']} {problems[0]['fix']}".strip() if problems
                      else "Every type, API schema and schema attribute checks out against USD."),
    }


def _version() -> str:
    return ".".join(str(v) for v in Usd.GetVersion()) if Usd is not None else "?"
