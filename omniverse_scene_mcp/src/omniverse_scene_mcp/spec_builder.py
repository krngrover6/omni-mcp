"""Build a whole scene from one description (a spec), as ONE previewed, undoable edit; then prove it is right.

Building a project prim by prim costs two tool calls per change. scene_build_from_spec takes the whole description at
once (prims with type, transform, attributes, references/payloads, materials, kind, instancing and variant sets) and:
1. plans: validates everything first (prim types against the USD schema registry, attribute names and value types
   against the prim's schema, asset files on disk, units / up axis against the stage, paths) and lists exactly what
   would be created, changed or is already right; any error stops the whole build before anything is written;
2. applies (dry_run=False): all changes in one edit on the edit target (Composer's authoring layer, or spec "layer"),
   through kit_commands.run_undoable, so one Ctrl+Z in Composer (or scene_undo_last) undoes the entire build, and a
   failure halfway rolls everything back;
3. verifies: the spec is checked again after applying; if anything is still not as described, the edit is rolled back.
Re-running the same spec is safe: what already matches is skipped, so an interrupted build resumes where it stopped.
scene_check_spec is the same comparison read-only (acceptance check: "is everything the spec asks for really
there?", and what is left to build). scene_spec_from_scene writes the spec of an existing subtree.

Spec shape (JSON):
{"meters_per_unit": 0.01, "up_axis": "Y", "layer": "layout.usda",
 "materials": [{"path": "/World/Looks/Red", "color": [0.8, 0.05, 0.05], "roughness": 0.3, "metallic": 0.9}],
 "prims": [{"path": "/World/Car", "type": "Xform", "kind": "component", "translate": [0, 0, 0],
            "rotate": [0, 90, 0], "scale": [1, 1, 1], "reference": "./assets/car.usda", "instanceable": false,
            "material": "/World/Looks/Red", "attributes": {"size": 50, "myTag": {"type": "string", "value": "x"}},
            "variants": {"paint": {"select": "red", "options": {"red": {"material": "/World/Looks/Red"},
                                                                "blue": {"material": "/World/Looks/Blue"}}}}}]}
"""
from __future__ import annotations

import difflib
import math
from pathlib import Path
from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover
    Gf = Sdf = Usd = UsdGeom = UsdShade = None

MAX_PRIMS = 500
TOLERANCE = 1e-4
_PRIM_KEYS = {"path", "type", "kind", "translate", "rotate", "scale", "attributes", "reference", "payload",
              "material", "instanceable", "active", "variants", "doc"}
_MATERIAL_KEYS = {"path", "color", "roughness", "metallic", "opacity", "emissive"}
_KINDS = {"component", "group", "assembly", "subcomponent", "model"}


class _SpecError(ValueError):
    pass


# ------------------------------------------------------------------------------------------------ small helpers
def _typed_names() -> list[str]:
    names = []
    for tf_type in __import__("pxr").Tf.Type.FindByName("UsdTyped").GetAllDerivedTypes():
        name = Usd.SchemaRegistry.GetSchemaTypeName(tf_type)
        if name:
            names.append(str(name))
    return sorted(set(names))


def _flat(value: Any) -> list[float] | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return [float(value)]
        items = list(value)
    except TypeError:
        return None
    out: list[float] = []
    for item in items:
        inner = _flat(item)
        if inner is None:
            return None
        out.extend(inner)
    return out


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, (str, bool)) or isinstance(b, (str, bool)):
        return str(a) == str(b)
    fa, fb = _flat(a), _flat(b)
    if fa is not None and fb is not None:
        return len(fa) == len(fb) and all(math.isclose(x, y, rel_tol=TOLERANCE, abs_tol=TOLERANCE)
                                          for x, y in zip(fa, fb))
    return a == b


def _vec3(value: Any, field: str) -> tuple[float, float, float]:
    flat = _flat(value)
    if flat is None or len(flat) != 3 or not all(math.isfinite(v) for v in flat):
        raise _SpecError(f"{field} must be three numbers [x, y, z]")
    return flat[0], flat[1], flat[2]


def _convert(type_name: Any, value: Any) -> Any:
    """A JSON value as the Python/Gf/Vt object USD expects for this value type."""
    cls = type_name.type.pythonClass
    if cls is None:
        return value
    if type_name.isArray:
        return cls(value)
    if isinstance(value, (list, tuple)):
        return cls(*value)
    if cls is float and isinstance(value, bool):
        raise _SpecError("a number was expected, not true/false")
    return cls(value)


def _asset_parts(value: Any) -> tuple[str, str | None]:
    if isinstance(value, str):
        return value, None
    if isinstance(value, dict) and isinstance(value.get("asset"), str):
        prim = value.get("prim")
        return value["asset"], prim if isinstance(prim, str) else None
    raise _SpecError('reference/payload must be a file path or {"asset": "...", "prim": "/Name"}')


def _resolve_local(layer: Any, asset: str) -> Path | None:
    """The file on disk an asset path points at (relative to the layer), or None for remote/unknown schemes."""
    if "://" in asset or asset.startswith("omniverse:"):
        return None
    path = Path(asset)
    if not path.is_absolute():
        base = Path(layer.realPath).parent if layer is not None and layer.realPath else None
        if base is None:
            return None
        path = base / path
    return path


def _arc_assets(prim: Any, kind: str) -> list[str]:
    out = []
    for spec in prim.GetPrimStack():
        items = spec.referenceList if kind == "reference" else spec.payloadList
        for item in list(items.GetAddedOrExplicitItems()):
            out.append(item.assetPath)
    return out


def _layer_for(stage: Any, name: Any) -> Any:
    if name in (None, ""):
        return stage.GetEditTarget().GetLayer()
    if not isinstance(name, str):
        raise _SpecError("layer must be a layer file name such as layout.usda")
    for layer in stage.GetLayerStack(includeSessionLayers=False):
        if name in (layer.identifier, layer.GetDisplayName()) or layer.identifier.replace("\\", "/").endswith(
                "/" + name.lstrip("./")):
            return layer
    names = [l.GetDisplayName() for l in stage.GetLayerStack(includeSessionLayers=False)]
    raise _SpecError(f"layer {name!r} is not in this stage's layer stack ({', '.join(names)})")


# ------------------------------------------------------------------------------------------------ planning
def _check_spec_shape(spec: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(spec, dict):
        raise _SpecError("spec must be an object with 'prims' (and optionally 'materials')")
    prims = spec.get("prims") or []
    materials = spec.get("materials") or []
    if not isinstance(prims, list) or not isinstance(materials, list):
        raise _SpecError("'prims' and 'materials' must be lists")
    if len(prims) + len(materials) > MAX_PRIMS:
        raise _SpecError(f"at most {MAX_PRIMS} prims and materials per spec; split the build into parts")
    if not prims and not materials:
        raise _SpecError("the spec has no prims and no materials")
    return prims, materials


def _plan(stage: Any, spec: Any) -> dict[str, Any]:
    """Validate and diff. Returns {"errors", "warnings", "steps", "parents", "layer", ...}; never writes."""
    errors: list[str] = []
    warnings: list[str] = []
    steps: list[dict[str, Any]] = []
    prims, materials = _check_spec_shape(spec)
    layer = _layer_for(stage, spec.get("layer"))
    if layer is None or not layer.permissionToEdit:
        errors.append("The target layer is read-only.")
    mpu = spec.get("meters_per_unit")
    if mpu is not None:
        stage_mpu = UsdGeom.GetStageMetersPerUnit(stage)
        if not isinstance(mpu, (int, float)) or not math.isclose(float(mpu), stage_mpu, rel_tol=1e-6):
            errors.append(f"The spec is written for metersPerUnit {mpu}, but the stage uses {stage_mpu:g}: every "
                          "size and position would be off. Fix the spec's numbers (or its meters_per_unit).")
    up = spec.get("up_axis")
    if up is not None and str(up).upper() != str(UsdGeom.GetStageUpAxis(stage)):
        errors.append(f"The spec is written for up axis {up}, but the stage is {UsdGeom.GetStageUpAxis(stage)} up.")
    typed = _typed_names()
    seen: set[str] = set()
    spec_paths = {p.get("path") for p in prims if isinstance(p, dict)} | \
        {m.get("path") for m in materials if isinstance(m, dict)}

    def path_of(item: Any, what: str) -> Any:
        raw = item.get("path") if isinstance(item, dict) else None
        if not isinstance(raw, str) or not Sdf.Path.IsValidPathString(raw) or not raw.startswith("/") \
                or not Sdf.Path(raw).IsPrimPath():
            errors.append(f"{what}: path {raw!r} is not an absolute prim path like /World/Box")
            return None
        if raw in seen:
            errors.append(f"{raw}: appears twice in the spec")
            return None
        seen.add(raw)
        return Sdf.Path(raw)

    for item in materials:
        path = path_of(item, "material")
        if path is None:
            continue
        unknown = set(item) - _MATERIAL_KEYS
        if unknown:
            errors.append(f"{path}: unknown material keys {sorted(unknown)} (use {sorted(_MATERIAL_KEYS)})")
        existing = stage.GetPrimAtPath(path)
        changes = []
        shader = UsdShade.Shader(stage.GetPrimAtPath(path.AppendChild("PreviewSurface"))) if existing else None
        for key, input_name in (("color", "diffuseColor"), ("roughness", "roughness"), ("metallic", "metallic"),
                                ("opacity", "opacity"), ("emissive", "emissiveColor")):
            if key not in item:
                continue
            try:
                value = _vec3(item[key], key) if key in ("color", "emissive") else float(item[key])
            except (_SpecError, TypeError, ValueError) as exc:
                errors.append(f"{path}: {key}: {exc}")
                continue
            current = shader.GetInput(input_name).Get() if shader and shader.GetInput(input_name) else None
            if not _same(current, value):
                changes.append(key)
        if existing and not existing.IsA(UsdShade.Material):
            errors.append(f"{path}: exists as {existing.GetTypeName() or 'a typeless prim'}, not a Material")
            continue
        steps.append({"path": str(path), "what": "material", "action": "create" if not existing else
                      ("update" if changes else "ok"), "changes": changes if existing else ["new material"]})

    for item in sorted((p for p in prims if isinstance(p, dict)), key=lambda p: str(p.get("path", "")).count("/")):
        path = path_of(item, "prim")
        if path is None:
            continue
        unknown = set(item) - _PRIM_KEYS
        if unknown:
            close = {k: difflib.get_close_matches(k, sorted(_PRIM_KEYS), n=1) for k in unknown}
            errors.append(f"{path}: unknown keys {sorted(unknown)}" + "".join(
                f" ({k} -> did you mean {v[0]}?)" for k, v in close.items() if v))
        type_name = item.get("type")
        existing = stage.GetPrimAtPath(path)
        changes: list[str] = []
        if type_name is not None:
            if not isinstance(type_name, str) or type_name not in typed:
                errors.append(f"{path}: there is no prim type {type_name!r}" + (
                    f"; did you mean {difflib.get_close_matches(str(type_name), typed, n=1)[0]}?"
                    if difflib.get_close_matches(str(type_name), typed, n=1) else ""))
                continue
            if existing and existing.GetTypeName() and existing.GetTypeName() != type_name:
                errors.append(f"{path}: exists as {existing.GetTypeName()}; the spec says {type_name}. The builder "
                              "never changes a prim's type: delete it first or use another path.")
                continue
        definition = Usd.SchemaRegistry().FindConcretePrimDefinition(type_name or str(
            existing.GetTypeName() if existing else "")) if (type_name or existing) else None
        if not existing:
            changes.append(f"new {type_name or 'typeless prim'}")
        # transform
        xform_values = {}
        for key in ("translate", "rotate", "scale"):
            if key in item:
                try:
                    xform_values[key] = _vec3(item[key], key)
                except _SpecError as exc:
                    errors.append(f"{path}: {exc}")
        if xform_values:
            if existing and existing.IsA(UsdGeom.Xformable):
                api = UsdGeom.XformCommonAPI(existing)
                if not api:
                    if "rotate" in xform_values:
                        errors.append(f"{path}: its transform uses ops the builder cannot edit safely (e.g. an Orient "
                                      "quaternion); change its rotation by hand or delete and rebuild the prim.")
                else:
                    t, r, s, _, _ = api.GetXformVectors(Usd.TimeCode.Default())
                    current = {"translate": t, "rotate": r, "scale": s}
                    changes += [k for k, v in xform_values.items() if not _same(current[k], v)]
            elif existing and not existing.IsA(UsdGeom.Xformable):
                errors.append(f"{path}: a {existing.GetTypeName()} has no transform")
            else:
                changes += [k for k, v in xform_values.items()]
        # attributes
        attributes = item.get("attributes") or {}
        if not isinstance(attributes, dict):
            errors.append(f"{path}: attributes must be an object name -> value")
            attributes = {}
        for name, raw in attributes.items():
            custom_type = None
            value = raw
            if isinstance(raw, dict) and "value" in raw:
                custom_type, value = raw.get("type"), raw["value"]
            attr_def = definition.GetAttributeDefinition(name) if definition and hasattr(
                definition, "GetAttributeDefinition") else None
            if attr_def:
                type_obj = attr_def.GetTypeName()
            elif existing and existing.GetAttribute(name):
                type_obj = existing.GetAttribute(name).GetTypeName()
            elif custom_type:
                type_obj = Sdf.ValueTypeNames.Find(str(custom_type))
                if not type_obj:
                    errors.append(f"{path}: {name}: unknown value type {custom_type!r} (e.g. float, double3, "
                                  "string, token, color3f)")
                    continue
            else:
                names = sorted(definition.GetPropertyNames()) if definition else []
                guess = difflib.get_close_matches(name, names, n=1)
                errors.append(f"{path}: {name!r} is not an attribute of {type_name or 'this prim'}" +
                              (f"; did you mean {guess[0]!r}?" if guess else "") +
                              f'. For your own data write "{name}": {{"type": "float", "value": ...}}.')
                continue
            try:
                converted = _convert(type_obj, value)
            except Exception as exc:  # noqa: BLE001 - any conversion error is a spec error
                errors.append(f"{path}: {name}: {value!r} is not a {type_obj} ({exc})")
                continue
            current = existing.GetAttribute(name).Get() if existing and existing.GetAttribute(name) and \
                existing.GetAttribute(name).HasAuthoredValue() else None
            if not _same(current, converted):
                changes.append(name)
        # arcs
        for kind in ("reference", "payload"):
            if kind not in item:
                continue
            try:
                asset, _ = _asset_parts(item[kind])
            except _SpecError as exc:
                errors.append(f"{path}: {exc}")
                continue
            local = _resolve_local(layer, asset)
            if local is not None and not local.is_file():
                errors.append(f"{path}: {kind} file {asset!r} does not exist (looked at {local}).")
                continue
            if not existing or asset not in _arc_assets(existing, kind):
                changes.append(kind)
        # material binding
        material = item.get("material")
        if material is not None:
            if not isinstance(material, str) or (material not in spec_paths and not stage.GetPrimAtPath(material)):
                errors.append(f"{path}: material {material!r} is neither in the spec's materials nor in the stage")
            else:
                bound = UsdShade.MaterialBindingAPI(existing).ComputeBoundMaterial()[0] if existing else None
                if not bound or str(bound.GetPath()) != material:
                    changes.append("material")
        # metadata
        kind_value = item.get("kind")
        if kind_value is not None:
            if kind_value not in _KINDS:
                errors.append(f"{path}: kind must be one of {sorted(_KINDS)}")
            elif not existing or Usd.ModelAPI(existing).GetKind() != kind_value:
                changes.append("kind")
        for key in ("instanceable", "active"):
            if key in item:
                if not isinstance(item[key], bool):
                    errors.append(f"{path}: {key} must be true or false")
                elif not existing or (existing.IsInstanceable() if key == "instanceable" else existing.IsActive()) \
                        != item[key]:
                    changes.append(key)
        # variants
        variants = item.get("variants") or {}
        if not isinstance(variants, dict):
            errors.append(f"{path}: variants must be an object set name -> {{options, select}}")
            variants = {}
        for set_name, body in variants.items():
            options = body.get("options") if isinstance(body, dict) else None
            if not isinstance(options, dict) or not options:
                errors.append(f"{path}: variant set {set_name!r} needs options: {{name: {{material/attributes}}}}")
                continue
            select = body.get("select", next(iter(options)))
            if select not in options:
                errors.append(f"{path}: variant set {set_name!r} selects {select!r}, which is not one of its options")
            for option, content in options.items():
                mat = (content or {}).get("material") if isinstance(content, dict) else None
                if mat is not None and mat not in spec_paths and not stage.GetPrimAtPath(mat):
                    errors.append(f"{path}: variant {set_name}={option} uses material {mat!r}, which does not exist")
            vsets = existing.GetVariantSets() if existing else None
            if not existing or not vsets.HasVariantSet(set_name) or \
                    not set(options) <= set(vsets.GetVariantSet(set_name).GetVariantNames()) or \
                    vsets.GetVariantSet(set_name).GetVariantSelection() != select:
                changes.append(f"variants:{set_name}")
        action = "create" if not existing else ("update" if changes else "ok")
        steps.append({"path": str(path), "what": type_name or (str(existing.GetTypeName()) if existing else "prim"),
                      "action": action, "changes": changes})
    # parents that are neither in the stage nor in the spec get a plain Xform
    parents = sorted({str(p) for s in steps for p in Sdf.Path(s["path"]).GetPrefixes()[:-1]
                      if not stage.GetPrimAtPath(p) and str(p) not in spec_paths})
    if parents:
        warnings.append("These parents do not exist and will be made as Xforms: " + ", ".join(parents[:10]))
    return {"errors": errors, "warnings": warnings, "steps": steps, "parents": parents, "layer": layer}


# ------------------------------------------------------------------------------------------------ applying
def _apply(stage: Any, spec: dict[str, Any], plan: dict[str, Any]) -> None:
    prims = {p["path"]: p for p in spec.get("prims") or []}
    for parent in plan["parents"]:
        UsdGeom.Xform.Define(stage, parent)
    for item in spec.get("materials") or []:
        path = Sdf.Path(item["path"])
        material = UsdShade.Material.Define(stage, path)
        shader = UsdShade.Shader.Define(stage, path.AppendChild("PreviewSurface"))
        shader.CreateIdAttr("UsdPreviewSurface")
        for key, input_name, type_name in (("color", "diffuseColor", Sdf.ValueTypeNames.Color3f),
                                           ("roughness", "roughness", Sdf.ValueTypeNames.Float),
                                           ("metallic", "metallic", Sdf.ValueTypeNames.Float),
                                           ("opacity", "opacity", Sdf.ValueTypeNames.Float),
                                           ("emissive", "emissiveColor", Sdf.ValueTypeNames.Color3f)):
            if key in item:
                value = Gf.Vec3f(*_vec3(item[key], key)) if key in ("color", "emissive") else float(item[key])
                shader.CreateInput(input_name, type_name).Set(value)
        material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    for step in plan["steps"]:
        if step["what"] == "material" or step["action"] == "ok":
            continue
        item = prims[step["path"]]
        path = Sdf.Path(item["path"])
        prim = stage.DefinePrim(path, item["type"]) if item.get("type") else (
            stage.GetPrimAtPath(path) or stage.DefinePrim(path))
        for kind in ("reference", "payload"):
            if kind in item:
                asset, target = _asset_parts(item[kind])
                if asset not in _arc_assets(prim, kind):
                    arcs = prim.GetReferences() if kind == "reference" else prim.GetPayloads()
                    if target:
                        arcs.AddReference(asset, target) if kind == "reference" else arcs.AddPayload(asset, target)
                    else:
                        arcs.AddReference(asset) if kind == "reference" else arcs.AddPayload(asset)
        if any(k in item for k in ("translate", "rotate", "scale")):
            api = UsdGeom.XformCommonAPI(prim)
            if "translate" in item:
                api.SetTranslate(Gf.Vec3d(*_vec3(item["translate"], "translate")))
            if "rotate" in item:
                api.SetRotate(Gf.Vec3f(*_vec3(item["rotate"], "rotate")), UsdGeom.XformCommonAPI.RotationOrderXYZ)
            if "scale" in item:
                api.SetScale(Gf.Vec3f(*_vec3(item["scale"], "scale")))
        definition = prim.GetPrimDefinition()
        for name, raw in (item.get("attributes") or {}).items():
            custom_type, value = (raw.get("type"), raw["value"]) if isinstance(raw, dict) and "value" in raw \
                else (None, raw)
            attr_def = definition.GetAttributeDefinition(name) if hasattr(definition, "GetAttributeDefinition") \
                else None
            if attr_def:
                type_obj, custom = attr_def.GetTypeName(), False
            elif prim.GetAttribute(name):
                type_obj, custom = prim.GetAttribute(name).GetTypeName(), prim.GetAttribute(name).IsCustom()
            else:
                type_obj, custom = Sdf.ValueTypeNames.Find(str(custom_type)), True
            prim.CreateAttribute(name, type_obj, custom).Set(_convert(type_obj, value))
        if item.get("kind"):
            Usd.ModelAPI(prim).SetKind(item["kind"])
        if "instanceable" in item:
            prim.SetInstanceable(item["instanceable"])
        if "active" in item:
            prim.SetActive(item["active"])
        if item.get("material"):
            UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material(stage.GetPrimAtPath(item["material"])))
        for set_name, body in (item.get("variants") or {}).items():
            vset = prim.GetVariantSets().AddVariantSet(set_name)
            for option, content in body["options"].items():
                vset.AddVariant(option)
                vset.SetVariantSelection(option)
                with vset.GetVariantEditContext():
                    content = content or {}
                    if content.get("material"):
                        UsdShade.MaterialBindingAPI.Apply(prim).Bind(
                            UsdShade.Material(stage.GetPrimAtPath(content["material"])))
                    for name, value in (content.get("attributes") or {}).items():
                        attr = prim.GetAttribute(name)
                        if attr:
                            attr.Set(_convert(attr.GetTypeName(), value))
            vset.SetVariantSelection(body.get("select", next(iter(body["options"]))))


def _summary(steps: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"create": 0, "update": 0, "ok": 0}
    for step in steps:
        counts[step["action"]] += 1
    return counts


def build_from_spec(stage: Any, spec: Any, *, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Build a whole scene from ONE description (spec) as one undoable edit. dry_run=True (default) validates
    everything (types, attribute names and value types, asset files, units/up axis, paths) and lists what would be
    created / changed / is already right. dry_run=False applies it all at once on the authoring layer (or spec
    'layer'): one Ctrl+Z or scene_undo_last undoes the whole build; any failure rolls everything back. Re-running a
    spec skips what already matches. See the module doc for the spec shape (prims, materials, variants...)."""
    if Usd is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available"}
    try:
        plan = _plan(stage, spec)
    except _SpecError as exc:
        return {"ok": False, "error": str(exc), "written": False}
    layer = plan.pop("layer")
    counts = _summary(plan["steps"])
    base = {"layer": layer.GetDisplayName() if layer else None, "counts": counts,
            "steps": [s for s in plan["steps"] if s["action"] != "ok"][:200], "warnings": plan["warnings"]}
    if plan["errors"]:
        return {"ok": False, "dry_run": dry_run is not False, "written": False, "errors": plan["errors"], **base,
                "next_step": "Fix these spec errors first; nothing was written."}
    if counts["create"] + counts["update"] == 0:
        return {"ok": True, "dry_run": dry_run is not False, "written": False, **base,
                "next_step": "Everything in the spec is already in the scene: nothing to do."}
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, **base,
                "next_step": f"Preview only. Apply with dry_run=False: {counts['create']} new, {counts['update']} "
                             "changed, as one undoable edit."}
    from .kit_commands import run_undoable

    roots = sorted({s["path"] for s in plan["steps"] if s["action"] != "ok"} | set(plan["parents"]),
                   key=lambda p: p.count("/"))
    top = [p for p in roots if not any(p != q and p.startswith(q + "/") for q in roots)]
    old_target = stage.GetEditTarget()

    def build() -> list[str]:
        _apply(stage, spec, plan)
        after = _plan(stage, spec)
        left = [f"{s['path']}: {', '.join(s['changes'])}" for s in after["steps"] if s["action"] != "ok"]
        if after["errors"] or left:
            raise RuntimeError("after building, the scene still differs from the spec: " + "; ".join(
                (after["errors"] + left)[:5]))
        return left

    try:
        stage.SetEditTarget(Usd.EditTarget(layer))
        kwargs = {"stack": undo_stack} if undo_stack is not None else {}
        run_undoable(f"Build {counts['create']} new / {counts['update']} changed from spec", stage, top, build,
                     **kwargs)
    except Exception as exc:  # noqa: BLE001 - run_undoable already rolled back
        return {"ok": False, "dry_run": False, "written": False, "error": f"Build failed and was rolled back: {exc}",
                **base}
    finally:
        stage.SetEditTarget(old_target)
    return {"ok": True, "dry_run": False, "written": True, "applied": True, **base,
            "verified": "The scene was checked against the spec after building: everything matches.",
            "next_step": "Look in the viewport. Ctrl+Z in Composer (or scene_undo_last) undoes the whole build; save "
                         "with Ctrl+S when happy."}


def check_spec(stage: Any, spec: Any) -> dict[str, Any]:
    """Acceptance check, read-only: is everything the spec describes really in the scene? Lists what is missing or
    different (what is left to build) per prim, plus spec errors. passed=True only when everything matches."""
    if Usd is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available"}
    try:
        plan = _plan(stage, spec)
    except _SpecError as exc:
        return {"ok": False, "error": str(exc)}
    plan.pop("layer")
    left = [s for s in plan["steps"] if s["action"] != "ok"]
    counts = _summary(plan["steps"])
    return {"ok": True, "read_only": True, "passed": not left and not plan["errors"],
            "matching": counts["ok"], "missing": counts["create"], "different": counts["update"],
            "left_to_build": left[:200], "errors": plan["errors"],
            "summary": (f"All {counts['ok']} items match the spec." if not left and not plan["errors"] else
                        f"{counts['ok']} match, {counts['create']} missing, {counts['update']} different.")}


def spec_from_scene(stage: Any, root_path: str = "/World", max_prims: Any = 200) -> dict[str, Any]:
    """Read-only: the spec of an existing subtree (types, transforms, kind, references/payloads, bound materials,
    variant selections). Edit it and give it back to scene_build_from_spec to change many prims in one go."""
    root = stage.GetPrimAtPath(root_path) if isinstance(root_path, str) else None
    if not root:
        return {"ok": False, "error": f"No prim at {root_path!r}"}
    limit = max(1, min(MAX_PRIMS, int(max_prims) if isinstance(max_prims, int) else 200))
    prims = []
    walk = iter(Usd.PrimRange(root))
    for prim in walk:
        if len(prims) >= limit:
            break
        if _arc_assets(prim, "reference") or _arc_assets(prim, "payload") or prim.IsInstance():
            walk.PruneChildren()  # an asset's insides come from its file, not from this spec
        entry: dict[str, Any] = {"path": str(prim.GetPath())}
        if prim.GetTypeName():
            entry["type"] = str(prim.GetTypeName())
        if prim.IsA(UsdGeom.Xformable):
            api = UsdGeom.XformCommonAPI(prim)
            if api:
                t, r, s, _, _ = api.GetXformVectors(Usd.TimeCode.Default())
                if not _same(t, (0, 0, 0)):
                    entry["translate"] = [round(v, 6) for v in t]
                if not _same(r, (0, 0, 0)):
                    entry["rotate"] = [round(v, 6) for v in r]
                if not _same(s, (1, 1, 1)):
                    entry["scale"] = [round(v, 6) for v in s]
        kind = Usd.ModelAPI(prim).GetKind()
        if kind:
            entry["kind"] = str(kind)
        for kind_name in ("reference", "payload"):
            assets = _arc_assets(prim, kind_name)
            if assets:
                entry[kind_name] = assets[0]
        if prim.HasAPI(UsdShade.MaterialBindingAPI):
            bound = UsdShade.MaterialBindingAPI(prim).GetDirectBinding().GetMaterial()
            if bound:
                entry["material"] = str(bound.GetPath())
        if prim.IsInstanceable():
            entry["instanceable"] = True
        prims.append(entry)
    return {"ok": True, "read_only": True, "spec": {"meters_per_unit": UsdGeom.GetStageMetersPerUnit(stage),
                                                     "up_axis": str(UsdGeom.GetStageUpAxis(stage)), "prims": prims},
            "count": len(prims), "truncated": len(prims) >= limit,
            "note": "Materials' own settings and variant contents are not exported; references keep their contents."}
