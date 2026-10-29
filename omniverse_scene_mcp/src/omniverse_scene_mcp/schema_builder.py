"""Custom schema builder (NCP-OUSD: schemas): write and check a schema.usda, every line explained. Read-only.

Words first:
- A SCHEMA is a named recipe of properties USD knows about, e.g. UsdGeom.Sphere has radius.
- A TYPED schema is a prim TYPE (def StudioRobot "Bot"): a prim has exactly one type.
- An API schema is a set of extra properties you APPLY to any prim (like MaterialBindingAPI); a prim can have many.
  Single-apply: once per prim. Multiple-apply: several named copies (like CollectionAPI:<name>).
- CODELESS means no C++/Python classes are generated: only the schema's description (generatedSchema.usda and
  plugInfo.json) is made, which is all USD needs to know the schema. skipCodeGeneration = true turns it on.

schema_plan writes the schema.usda text for a new schema; schema_check reads one you wrote and points out the
usual mistakes. Turning schema.usda into the registered files is done by OpenUSD's usdGenSchema tool, which
needs the jinja2 Python package (not installed in this workspace; ask before installing).
"""

from __future__ import annotations

import keyword
import re
from typing import Any

try:
    from pxr import Sdf
except ImportError:  # pragma: no cover
    Sdf = None

KINDS = {
    "applied_api": ("singleApply", "APISchemaBase"),
    "multiple_apply_api": ("multipleApply", "APISchemaBase"),
    "typed": (None, "Typed"),
    "typed_xformable": (None, "Xformable"),
}
_IDENT = re.compile(r"^[A-Z][A-Za-z0-9]*$")
_PROP = re.compile(r"^[a-z][A-Za-z0-9]*(:[a-z][A-Za-z0-9]*)*$")
_DEFAULT_SAMPLE = {"string": '""', "token": '""', "bool": "false", "int": "0", "float": "0", "double": "0",
                   "float3": "(0, 0, 0)", "double3": "(0, 0, 0)", "color3f": "(1, 1, 1)", "asset": "@@"}


def _usda_value(type_name: str, value: Any) -> str:
    if value is None:
        return _DEFAULT_SAMPLE.get(type_name, "")
    if type_name in ("string", "token"):
        return '"' + str(value).replace('"', '\\"') + '"'
    if type_name == "bool":
        return "true" if value else "false"
    if type_name == "asset":
        return f"@{value}@"
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(str(v) for v in value) + ")"
    return str(value)


def schema_plan(name: str, kind: str = "applied_api", attributes: list[dict[str, Any]] | None = None,
                library: str = "studio", doc: str = "") -> dict[str, Any]:
    """Read-only: the schema.usda text for a new codeless schema, each part explained. kind: applied_api,
    multiple_apply_api, typed (a new prim type) or typed_xformable (a prim type you can move). attributes:
    [{name, type, default, doc}], names in the library's namespace (e.g. studio:assetId)."""
    if Sdf is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    if kind not in KINDS:
        return {"ok": False, "error": f"kind must be one of {', '.join(KINDS)}."}
    if not isinstance(library, str) or not re.match(r"^[a-z][a-z0-9]*$", library):
        return {"ok": False, "error": "library must be a short lower-case word, e.g. 'studio'."}
    if not isinstance(name, str) or not _IDENT.match(name) or keyword.iskeyword(name):
        return {"ok": False, "error": "name must start with a capital letter and use letters/digits, e.g. StudioAsset."}
    apply_type, base = KINDS[kind]
    if apply_type and not name.endswith("API"):
        return {"ok": False, "error": f"API schema names end in 'API' by convention: try {name}API."}
    if not apply_type and name.endswith("API"):
        return {"ok": False, "error": "A typed schema is a prim type; its name should not end in 'API'."}
    lines_attrs, explained = [], []
    for item in attributes or []:
        if not isinstance(item, dict) or "name" not in item or "type" not in item:
            return {"ok": False, "error": "each attribute needs a name and a type, e.g. {'name': 'studio:assetId', "
                                          "'type': 'string'}."}
        prop, type_name = str(item["name"]), str(item["type"])
        if not _PROP.match(prop):
            return {"ok": False, "error": f"{prop} is not a property name (lower-case start, parts joined by ':')."}
        if apply_type == "singleApply" and not prop.startswith(library + ":"):
            return {"ok": False, "error": f"Put API schema properties in your namespace: {library}:{prop}. Without a "
                                          "namespace they can clash with other schemas' properties."}
        if not Sdf.ValueTypeNames.Find(type_name):
            return {"ok": False, "error": f"{type_name} is not a USD value type (e.g. string, token, float, "
                                          "double3, color3f, asset)."}
        if apply_type == "multipleApply":
            prop = prop.split(":", 1)[-1]  # multiple-apply properties get the instance name added by USD
        value = _usda_value(type_name, item.get("default"))
        doc_text = str(item.get("doc") or f"{prop} of this {name}").replace('"', "'")
        lines_attrs.append(f'    {type_name} {prop} = {value} (\n        doc = "{doc_text}"\n    )')
        explained.append(f"{type_name} {prop} = {value}: a property every prim with this schema gets, with that "
                         "fallback value until someone authors their own.")
    meta = [f"    inherits = </{base}>", f'    doc = "{(doc or name + " schema").replace(chr(34), chr(39))}"']
    if apply_type:
        meta.append('    customData = {\n        token apiSchemaType = "' + apply_type + '"' +
                    (f'\n        token propertyNamespacePrefix = "{library}"' if apply_type == "multipleApply" else "") +
                    "\n    }")
    header = f'class "{name}"' if apply_type else f'class {name} "{name}"'
    text = "\n".join([
        "#usda 1.0",
        "(",
        "    subLayers = [",
        "        @usd/schema.usda@",
        "    ]",
        ")",
        "",
        'over "GLOBAL" (',
        "    customData = {",
        f'        string libraryName = "{library}"',
        '        string libraryPath = "."',
        "        bool skipCodeGeneration = true",
        "    }",
        ")",
        "{",
        "}",
        "",
        f"{header} (",
        *meta,
        ")",
        "{",
        *lines_attrs,
        "}",
        "",
    ])
    check = schema_check(text)
    usage = (f'prim.ApplyAPI("{name}")  # or in USDA: prepend apiSchemas = ["{name}"]' if apply_type == "singleApply"
             else f'prim.ApplyAPI("{name}", "<instance>")  # several named copies' if apply_type else
             f'stage.DefinePrim("/World/Thing", "{name}")  # or in USDA: def {name} "Thing"')
    return {
        "ok": check["ok"], "read_only": True, "name": name, "kind": kind, "schema_usda": text, "check": check,
        "line_by_line": [
            "subLayers @usd/schema.usda@: brings in USD's own base schemas (Typed, APISchemaBase) to inherit from.",
            f'over "GLOBAL": settings for the whole library: its name ({library}) and skipCodeGeneration = true, '
            "which makes it CODELESS (no C++/Python classes, only the description).",
            f"{header}: " + ("a class with no type name is an API schema (applied, never def'd)." if apply_type else
                             f"'class {name}' gives the schema a type name, so prims can be def {name}."),
            f"inherits = </{base}>: " + {"APISchemaBase": "every API schema derives from APISchemaBase.",
                                          "Typed": "a plain prim type (no transform).",
                                          "Xformable": "a prim type that can be moved, rotated and scaled."}[base],
        ] + ([f"apiSchemaType = {apply_type}: " + ("applied once per prim." if apply_type == "singleApply" else
                                                     "applied several times with an instance name.")]
             if apply_type else []) + explained,
        "use_it": usage,
        "next_steps": [
            "Save this as schema.usda in a folder of its own (e.g. <project>/schemas/" + library + "/).",
            "Run usdGenSchema schema.usda . in that folder: it writes generatedSchema.usda and plugInfo.json. It needs "
            "the jinja2 package, which is not installed here (ask before installing).",
            "Tell USD where the plugin is: set PXR_PLUGINPATH_NAME to that folder before starting the app.",
            "Check: Usd.SchemaRegistry().FindAppliedAPIPrimDefinition('" + name + "') (API) or "
            "FindConcretePrimDefinition('" + name + "') (typed) is not None.",
        ],
    }


def schema_check(text: str) -> dict[str, Any]:
    """Read-only: parse a schema.usda and list beginner mistakes (missing GLOBAL/libraryName, wrong base class, API
    name without 'API', missing apiSchemaType, typed class without a type name, properties outside a namespace)."""
    if Sdf is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "Paste the schema.usda text."}
    layer = Sdf.Layer.CreateAnonymous("schema.usda")
    try:
        if not layer.ImportFromString(text):
            raise ValueError("not valid USDA")
    except Exception as exc:  # noqa: BLE001 - Tf errors carry the parser message
        return {"ok": True, "read_only": True, "parses": False, "problems": [
            {"severity": "error", "where": "file", "message": f"USD cannot read it: {str(exc).strip()[:300]}",
             "fix": "Check brackets and quotes near the line the message names."}], "schemas": []}
    problems: list[dict[str, str]] = []
    if not any("usd/schema.usda" in path for path in layer.subLayerPaths):
        problems.append({"severity": "error", "where": "header", "message": "No subLayer @usd/schema.usda@.",
                         "fix": "Add subLayers = [@usd/schema.usda@] so the base schemas exist to inherit from."})
    glob = layer.GetPrimAtPath("/GLOBAL")
    data = dict(glob.customData) if glob else {}
    if not glob or not data.get("libraryName"):
        problems.append({"severity": "error", "where": "GLOBAL", "message": "No over \"GLOBAL\" with libraryName.",
                         "fix": 'Add over "GLOBAL" (customData = {string libraryName = "studio"}) {}.'})
    elif not data.get("skipCodeGeneration"):
        problems.append({"severity": "info", "where": "GLOBAL", "message": "skipCodeGeneration is not true, so "
                         "usdGenSchema will also generate C++ code (needs a build).",
                         "fix": "For a codeless schema add bool skipCodeGeneration = true."})
    schemas = []
    library = str(data.get("libraryName") or "")
    for spec in layer.rootPrims:
        if spec.name == "GLOBAL":
            continue
        where = spec.name
        inherits = [str(p) for p in spec.inheritPathList.GetAddedOrExplicitItems()]
        api_type = dict(spec.customData).get("apiSchemaType")
        is_api = spec.name.endswith("API") or inherits == ["/APISchemaBase"]
        schemas.append({"name": spec.name, "type_name": spec.typeName, "inherits": inherits,
                        "api": bool(is_api), "apiSchemaType": api_type,
                        "properties": [p.name for p in spec.properties]})
        if spec.specifier != Sdf.SpecifierClass:
            problems.append({"severity": "error", "where": where, "message": "Schemas are written as class prims.",
                             "fix": f'Write class "{spec.name}" (...) instead of def/over.'})
        if not inherits:
            problems.append({"severity": "error", "where": where, "message": "It inherits from nothing.",
                             "fix": "Add inherits = </APISchemaBase> (API schema) or </Typed> / </Xformable> (type)."})
        if is_api:
            if not spec.name.endswith("API"):
                problems.append({"severity": "error", "where": where, "message": "An API schema's name must end in "
                                 "'API'.", "fix": f"Rename it {spec.name}API."})
            if spec.typeName:
                problems.append({"severity": "error", "where": where, "message": "API schemas have no type name.",
                                 "fix": f'Write class "{spec.name}", without a type before the name.'})
            if api_type not in ("singleApply", "multipleApply", "nonApplied"):
                problems.append({"severity": "warning", "where": where, "message": "No apiSchemaType: it would be "
                                 "a single-apply API by default, but say it explicitly.",
                                 "fix": 'Add customData = {token apiSchemaType = "singleApply"}.'})
            if api_type == "singleApply" and library:
                loose = [p.name for p in spec.properties if ":" not in p.name]
                if loose:
                    problems.append({"severity": "warning", "where": where, "message": f"Properties {loose} have no "
                                     "namespace and could clash with other schemas.",
                                     "fix": f"Name them {library}:<name>, e.g. {library}:{loose[0]}."})
        elif not spec.typeName:
            problems.append({"severity": "info", "where": where, "message": "A typed class without a type name is "
                             "ABSTRACT: you cannot def a prim of it.",
                             "fix": f'For a concrete type write class {spec.name} "{spec.name}".'})
    if not schemas:
        problems.append({"severity": "error", "where": "file", "message": "No schema classes found.",
                         "fix": 'Add class "MyStudioAPI" (inherits = </APISchemaBase>) { ... }.'})
    errors = [p for p in problems if p["severity"] == "error"]
    return {"ok": not errors, "read_only": True, "parses": True, "library": library or None, "schemas": schemas,
            "problems": problems,
            "summary": f"{len(errors)} error(s)." if errors else "Looks like a valid codeless schema description."}
