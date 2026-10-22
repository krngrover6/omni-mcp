"""Safe attribute edits and UsdPreviewSurface materials: dry-run by default, every refusal checked before a write.

set_attribute coerces a value to the attribute's declared Sdf value type and predicts whether a stronger opinion
(a stronger layer, or time samples) will hide the edit. create_material defines a UsdShade.Material with one
UsdPreviewSurface shader wired to outputs:surface. bind_material applies MaterialBindingAPI, binds with an optional
strength and purpose, and predicts the material the prim resolves to afterwards.

Each function first plans the whole edit by reading the stage; only dry_run=False writes it, into the stage's
current edit target, with plain USD calls. Imports only pxr, the standard library and this package's pure-pxr
helpers, so the same functions run on temp stages in tests and on the live stage inside Kit / Isaac Sim (where
the edits do not go on Kit's undo stack).
"""

from __future__ import annotations

import difflib
import json
import math
import numbers
import re
from typing import Any

from .authoring import (
    MAX_WARNINGS,
    _check_edit_target,
    _check_stage,
    _creation_target,
    _dry,
    _edit_target_info,
    _prim_path,
    _Refusal,
    _require_pxr,
    _text,
)
from .usd_insight import to_jsonable

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = UsdShade = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

MAX_ARRAY_INPUT = 200_000
MAX_STRING_INPUT = 100_000
MAX_NAME_CHARS = 256
MAX_SUGGESTIONS = 5
MAX_DESCENDANTS = 20_000
MAX_EXAMPLES = 10
PREVIEW_SURFACE_ID = "UsdPreviewSurface"
SHADER_NAME = "PreviewSurface"
PURPOSES = ("allPurpose", "preview", "full")
STRENGTHS = ("weakerThanDescendants", "strongerThanDescendants")

# UsdPreviewSurface inputs (spec name -> Sdf type, spec default), used to plan materials and to suggest create_type.
PREVIEW_SURFACE_INPUTS = {
    "diffuseColor": ("color3f", (0.18, 0.18, 0.18)),
    "emissiveColor": ("color3f", (0.0, 0.0, 0.0)),
    "useSpecularWorkflow": ("int", 0),
    "specularColor": ("color3f", (0.0, 0.0, 0.0)),
    "metallic": ("float", 0.0),
    "roughness": ("float", 0.5),
    "clearcoat": ("float", 0.0),
    "clearcoatRoughness": ("float", 0.01),
    "opacity": ("float", 1.0),
    "opacityThreshold": ("float", 0.0),
    "ior": ("float", 1.5),
    "normal": ("normal3f", (0.0, 0.0, 1.0)),
    "displacement": ("float", 0.0),
    "occlusion": ("float", 1.0),
}
_INT_RANGES = {
    "uchar": (0, 255),
    "int": (-(2**31), 2**31 - 1),
    "uint": (0, 2**32 - 1),
    "int64": (-(2**63), 2**63 - 1),
    "uint64": (0, 2**64 - 1),
}
_FLOAT_LIMITS = {"half": 65504.0, "float": 3.4028234663852886e38, "double": None}
_COMPONENT_LIMITS = {"h": 65504.0, "f": 3.4028234663852886e38, "d": None}
_MATRIX_SIZES = {"matrix2d": 2, "matrix3d": 3, "matrix4d": 4, "frame4d": 4}
_QUATS = ("quath", "quatf", "quatd")
_TEXT_KINDS = ("string", "token", "asset", "pathExpression")
_UNSETTABLE = ("opaque", "group")
_TRUE_WORDS = ("true", "yes", "on", "1")
_FALSE_WORDS = ("false", "no", "off", "0")
_UNEXPECTED = "Unexpected error while planning (nothing was written): {}"
_MISSING = object()
_UNKNOWN = object()
_BLOCK = object()


# --- set_attribute ----------------------------------------------------------------------------------------


def set_attribute(
    stage: Any,
    prim_path: str,
    name: str,
    value: Any,
    *,
    time: Any = None,
    create_type: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author one attribute value, coerced to the attribute's Sdf type.

    time=None writes the default value; a number writes a time sample at that time. A missing attribute is
    refused unless create_type (an Sdf type name such as "float", "color3f" or "token[]") is given, which creates
    a custom attribute of that type. Relationships and connected attributes are refused. The result reports the
    value before (and which layer it comes from), the coerced value after, and warnings when a stronger opinion
    would hide the edit.
    """
    action = "set_attribute"
    dry = _dry(dry_run)
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {
        "path": _text(prim_path) if isinstance(prim_path, str) else None,
        "attribute": _text(name) if isinstance(name, str) else None,
    }
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        prim = _existing_prim(stage, prim_path, "prim_path")
        attr_name = _attribute_name(name)
        fields.update(path=str(prim.GetPath()), attribute=attr_name)
        time_code, numeric = _time(time)
        attr, type_name, creating = _target_attribute(prim, attr_name, create_type, notes)
        fields["type_name"] = str(type_name)
        if numeric and attr is not None and attr.GetVariability() == Sdf.VariabilityUniform:
            raise _Refusal(
                f"{attr_name} is a uniform attribute, so it cannot hold time samples; leave time out to set its value."
            )
        if value is None:
            raise _Refusal("value is required (None cannot be authored as an attribute value).")
        context = _Context(attr_name, _allowed_tokens(attr), warnings, notes)
        usd_value, plain = _coerce(type_name, value, context)
        precision = _precision(type_name)
        tolerance = _tolerance(precision)
        before, edit_plain = _before(stage, attr, time_code, numeric, precision)
        would_change = edit_plain is _MISSING or edit_plain is _UNKNOWN or not _same(edit_plain, plain, tolerance)
        if not would_change:
            notes.append(f"The edit target already holds this value for {attr_name}, so writing it changes nothing.")
        after = to_jsonable(plain)
        effective = _predict_effective(stage, attr, attr_name, after, before, time_code, numeric, warnings, notes)
        _semantic_warnings(prim, attr_name, plain, creating, warnings)
        if creating:
            notes.append(f"Creates a new custom attribute {attr_name} ({type_name}) in the edit target layer.")
        fields.update(
            time=_time_label(time_code),
            creates_attribute=creating,
            before=before,
            after=after,
            effective_after=effective,
        )
    except _Refusal as refusal:
        return _result(action, dry, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _result(action, dry, stage, warnings, notes, fields, error=_UNEXPECTED.format(exc))

    if dry:
        return _result(action, True, stage, warnings, notes, fields, would_change=would_change)

    try:
        if creating:
            attr = prim.CreateAttribute(attr_name, type_name, custom=True)
            if not attr:
                raise ValueError(f"Could not create {attr_name}")
        if not attr.Set(usd_value, time_code):
            raise ValueError("UsdAttribute.Set returned False")
    except Exception as exc:
        rolled_back = False
        if creating:
            try:
                rolled_back = bool(prim.RemoveProperty(attr_name))
            except Exception:
                rolled_back = False
        written = creating and not rolled_back
        note = "the new attribute spec was removed again" if creating and rolled_back else "nothing was written"
        if written:
            note = "the edit target may hold an empty attribute spec"
        return _result(action, False, stage, warnings, notes, fields, error=f"Authoring failed ({note}): {exc}", written=written)

    composed = attr.Get(time_code)
    visible = _same(_plain(composed), plain, tolerance)
    readback = {"value": _tidy(to_jsonable(composed), precision), "shows_new_value": visible}
    if not visible and effective.get("visible") is not False:
        warnings.append(
            f"After the write {attr_name} still resolves to {_text(readback['value'])}: an opinion that could not be "
            "predicted (for example value clips or a stronger composition arc) wins over the edit target."
        )
    return _result(action, False, stage, warnings, notes, fields, written=True, changed=would_change, readback=readback)


# --- create_material --------------------------------------------------------------------------------------


def create_material(
    stage: Any,
    path: str,
    *,
    diffuse_color: Any = None,
    roughness: Any = None,
    metallic: Any = None,
    opacity: Any = None,
    emissive_color: Any = None,
    ior: Any = None,
    use_specular_workflow: Any = None,
    create_parent: Any = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a UsdShade.Material with a UsdPreviewSurface shader.

    The shader is <path>/PreviewSurface (info:id UsdPreviewSurface) and its outputs:surface feeds the material's
    outputs:surface. Only the given inputs are authored; the rest keep the UsdPreviewSurface defaults. Colors are
    0-1 (whole-number 0-255 values and "#RRGGBB" are converted); roughness, metallic and opacity are 0-1.
    displacement is not wired, because none of these inputs need it. create_parent=True also plans a missing
    direct parent (such as /World/Looks) as a Scope when its own parent exists; deeper gaps are still refused.
    """
    action = "create_material"
    dry = _dry(dry_run)
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {"path": _text(path) if isinstance(path, str) else None}
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        sdf_path, parent_path, new_parent = _material_target(stage, path, create_parent is True, warnings)
        if new_parent is not None:
            notes.append(f"Also creates the missing parent {new_parent} as a Scope.")
        shader_path = sdf_path.AppendChild(SHADER_NAME)
        fields.update(path=str(sdf_path), shader_path=str(shader_path))

        inputs: dict[str, Any] = {}
        if diffuse_color is not None:
            inputs["diffuseColor"] = _material_color(diffuse_color, "inputs:diffuseColor", warnings, notes, allow_hdr=False)
        if emissive_color is not None:
            inputs["emissiveColor"] = _material_color(emissive_color, "inputs:emissiveColor", warnings, notes, allow_hdr=True)
        for key, input_name, given in (
            ("roughness", "roughness", roughness),
            ("metallic", "metallic", metallic),
            ("opacity", "opacity", opacity),
        ):
            if given is not None:
                inputs[input_name] = _unit_float(given, key)
        if ior is not None:
            value = _float(ior, "double", "ior")
            if value <= 0.0:
                raise _Refusal(f"ior must be greater than 0, got {value:g}")
            if value < 1.0:
                warnings.append(f"ior {value:g} is below 1 (vacuum); real dielectrics are about 1.3-2.5 (water 1.33, glass 1.5).")
            inputs["ior"] = value
        if use_specular_workflow is not None:
            inputs["useSpecularWorkflow"] = 1 if _bool(use_specular_workflow, "use_specular_workflow") else 0

        if inputs.get("useSpecularWorkflow") == 1:
            if "metallic" in inputs:
                warnings.append("metallic is ignored when useSpecularWorkflow is 1 (the specular workflow uses specularColor).")
            notes.append(
                "Specular workflow: inputs:specularColor keeps its default (0, 0, 0); set it with set_attribute "
                "(create_type='color3f') to get highlights."
            )
        if "opacity" in inputs and inputs["opacity"] < 1.0:
            notes.append(
                "opacity below 1 makes the surface translucent; for cut-outs instead, also set inputs:opacityThreshold "
                "(create_type='float') above 0."
            )

        plan = {
            "material": str(sdf_path),
            "parent": str(parent_path),
            "type": "Material",
            "shader": {"path": str(shader_path), "type": "Shader", "id": PREVIEW_SURFACE_ID},
            "inputs": {
                name: {"attribute": f"inputs:{name}", "type": PREVIEW_SURFACE_INPUTS[name][0], "value": to_jsonable(value)}
                for name, value in inputs.items()
            },
            "defaults_kept": {
                name: to_jsonable(PREVIEW_SURFACE_INPUTS[name][1])
                for name in ("diffuseColor", "emissiveColor", "useSpecularWorkflow", "metallic", "roughness", "opacity", "ior")
                if name not in inputs
            },
            "connections": {
                f"{sdf_path}.outputs:surface": f"{shader_path}.outputs:surface",
            },
            "displacement": "not connected: no displacement input was requested",
        }
        if new_parent is not None:
            plan["creates_parent"] = {"path": str(new_parent), "type": "Scope"}
    except _Refusal as refusal:
        return _result(action, dry, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _result(action, dry, stage, warnings, notes, fields, error=_UNEXPECTED.format(exc))

    if dry:
        return _result(action, True, stage, warnings, notes, fields, would_create=plan)

    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    spec_path = edit_target.MapToSpecPath(sdf_path)
    first_new = next((prefix for prefix in spec_path.GetPrefixes() if not layer.GetPrimAtPath(prefix)), None)
    try:
        if new_parent is not None and not UsdGeom.Scope.Define(stage, new_parent):
            raise ValueError(f"Could not create the parent Scope {new_parent}")
        material = UsdShade.Material.Define(stage, sdf_path)
        shader = UsdShade.Shader.Define(stage, shader_path)
        if not shader.CreateIdAttr(PREVIEW_SURFACE_ID):
            raise ValueError("Could not author info:id")
        for name, value in inputs.items():
            type_name = Sdf.ValueTypeNames.Find(PREVIEW_SURFACE_INPUTS[name][0])
            usd_value = Gf.Vec3f(*value) if isinstance(value, list) else value
            if not shader.CreateInput(name, type_name).Set(usd_value):
                raise ValueError(f"Could not set inputs:{name}")
        surface = shader.CreateOutput("surface", Sdf.ValueTypeNames.Token)
        if not material.CreateSurfaceOutput().ConnectToSource(surface):
            raise ValueError("Could not connect outputs:surface")
    except Exception as exc:
        rolled_back = _remove_new_spec(layer, first_new)
        note = "the new prim specs were removed again" if rolled_back else "the edit target may hold a partial material"
        return _result(action, False, stage, warnings, notes, fields, would_create=plan,
                       error=f"Authoring failed ({note}): {exc}", written=not rolled_back)

    source = _surface_source(material)
    created = {
        "path": str(material.GetPath()),
        "type": material.GetPrim().GetTypeName(),
        "shader": str(shader.GetPath()),
        "shader_id": _text(shader.GetIdAttr().Get()),
        "inputs": {
            name: _tidy(to_jsonable(shader.GetInput(name).Get()), 7) for name in inputs
        },
        "surface_source": source,
    }
    if new_parent is not None:
        created["created_parent"] = str(new_parent)
    if source != f"{shader_path}.outputs:surface":
        warnings.append(f"outputs:surface resolves to {source}, not the new shader; a stronger opinion overrides the connection.")
    return _result(action, False, stage, warnings, notes, fields, would_create=plan, created=created, written=True)


# --- bind_material ----------------------------------------------------------------------------------------


def bind_material(
    stage: Any,
    prim_path: str,
    material_path: str,
    *,
    strength: Any = None,
    purpose: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a direct material binding with MaterialBindingAPI.

    strength is weakerThanDescendants (the default: descendants' own bindings win) or strongerThanDescendants
    (this binding overrides bindings below it). purpose is allPurpose (the default), preview or full. The result
    reports the material the prim resolved to before (ComputeBoundMaterial) and predicts the one it resolves to
    after, following UsdShade's resolution rules, with a warning when another binding would still win.
    """
    action = "bind_material"
    dry = _dry(dry_run)
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {
        "path": _text(prim_path) if isinstance(prim_path, str) else None,
        "material": _text(material_path) if isinstance(material_path, str) else None,
    }
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        prim = _existing_prim(stage, prim_path, "prim_path")
        if prim.IsA(UsdShade.Material) or prim.IsA(UsdShade.Shader) or prim.IsA(UsdShade.NodeGraph):
            raise _Refusal(
                f"{prim.GetPath()} is a {prim.GetTypeName()} prim; bind materials to geometry (or a parent Xform/Scope "
                "whose descendants should inherit the material), not to shading prims."
            )
        material_sdf = _prim_path(material_path, "material_path")
        material_prim = stage.GetPrimAtPath(material_sdf)
        if not material_prim or not material_prim.IsValid():
            raise _Refusal(f"Material does not exist: {material_sdf}. Create it first (create_material).")
        if not material_prim.IsA(UsdShade.Material):
            raise _Refusal(
                f"{material_sdf} is a {material_prim.GetTypeName() or 'typeless'} prim, not a Material; a binding "
                "must target a UsdShade.Material."
            )
        strength_token, strength_label = _strength(strength)
        purpose_token, purpose_label = _purpose(purpose)
        if not UsdShade.MaterialBindingAPI.CanApply(prim):
            raise _Refusal(f"MaterialBindingAPI cannot be applied to {prim.GetPath()} ({prim.GetTypeName() or 'typeless'}).")

        rel_name = _binding_rel_name(purpose_token)
        rel_path = prim.GetPath().AppendProperty(rel_name)
        fields.update(
            path=str(prim.GetPath()),
            material=str(material_sdf),
            purpose=purpose_label,
            strength=strength_label or "weakerThanDescendants",
            strength_authored=strength_label is not None,
            relationship=rel_name,
        )
        api = UsdShade.MaterialBindingAPI(prim)
        has_api = bool(prim.HasAPI(UsdShade.MaterialBindingAPI))
        direct = api.GetDirectBinding(purpose_token)
        direct_path = direct.GetMaterialPath()
        direct_strength = (
            str(UsdShade.MaterialBindingAPI.GetMaterialBindingStrength(direct.GetBindingRel()))
            if not direct_path.isEmpty
            else None
        )
        previous = _bound_record(*api.ComputeBoundMaterial(purpose_token))
        before = {
            "bound_material": previous["material"],
            "via": previous["via"],
            "direct_binding": {"material": str(direct_path), "strength": direct_strength} if not direct_path.isEmpty else None,
            "has_material_binding_api": has_api,
        }

        new_strength = strength_token if strength_label is not None else UsdShade.Tokens.weakerThanDescendants
        override = {"prim": prim.GetPath(), "purpose": purpose_token, "material": material_sdf, "strength": new_strength,
                    "via": str(rel_path)}
        reliable = True
        by_purpose: dict[str, Any] = {}
        for label in PURPOSES:
            token = _purpose_tokens()[label]
            actual = _bound_record(*api.ComputeBoundMaterial(token))
            walked = _walk_binding(stage, prim, token, None)
            if (walked["material"] if walked else None) != actual["material"]:
                reliable = False
            predicted_for = _walk_binding(stage, prim, token, override)
            by_purpose[label] = {
                "before": actual["material"],
                "after_predicted": predicted_for["material"] if predicted_for else None,
            }
        if not reliable:
            warnings.append(
                "The binding prediction could not be checked against ComputeBoundMaterial on the current stage (this "
                "USD build may resolve bindings differently, for example a strict MaterialBindingAPI check); trust "
                "the readback after the write."
            )
        predicted = _walk_binding(stage, prim, purpose_token, override)
        wins = bool(predicted) and predicted["via"] == str(rel_path)
        prediction = {
            "material": predicted["material"] if predicted else None,
            "via": predicted["via"] if predicted else None,
            "binding_wins": wins,
            "hidden_by": None,
            "method": "UsdShade binding resolution walk, checked against ComputeBoundMaterial",
            "reliable": reliable,
        }
        stronger_rel = _stronger_binding_opinion(stage, prim, rel_name)
        if stronger_rel is _UNKNOWN:
            prediction["reliable"] = False
            warnings.append(
                "The edit target maps through a composition arc (for example a variant), so whether a stronger "
                f"opinion hides {rel_name} could not be predicted; check the readback after the write."
            )
            stronger_rel = None
        # Bind writes an explicit [material] in the edit target; the stronger layers' list ops (explicit, prepend,
        # append, delete) then compose on top of it, weakest first, exactly as USD composes relationship targets.
        composed = [material_sdf]
        for op in reversed(stronger_rel["ops"] if stronger_rel else []):
            composed = list(op.ApplyEditsToList(composed))
        if composed != [material_sdf]:
            # UsdShade ignores a binding relationship without exactly one target, so the prim then inherits.
            changed = {**override, "material": composed[0] if len(composed) == 1 else None}
            hidden = _walk_binding(stage, prim, purpose_token, changed)
            reason = (
                f"{stronger_rel['layer']} holds a stronger opinion for {rel_name} on {prim.GetPath()} (within a layer "
                f"stack the stronger layer wins): the targets compose to {[str(path) for path in composed]}, so this "
                "binding in the edit target would be hidden by it. Edit that layer instead to see the change."
            )
            prediction.update(material=hidden["material"] if hidden else None, via=hidden["via"] if hidden else None,
                              binding_wins=False, hidden_by={"layer": stronger_rel["layer"], "reason": reason})
            for label, entry in by_purpose.items():
                walked = _walk_binding(stage, prim, _purpose_tokens()[label], changed)
                entry["after_predicted"] = walked["material"] if walked else None
            warnings.append(reason)
        else:
            if stronger_rel and stronger_rel["strength_layer"]:
                warnings.append(
                    f"{stronger_rel['strength_layer']} holds a stronger bindMaterialAs for {rel_name} on "
                    f"{prim.GetPath()}, so the strength written in the edit target is hidden by it."
                )
            if not wins and predicted:
                reason = _hidden_reason(prim, predicted)
                prediction["hidden_by"] = {**_public_binding(predicted), "reason": reason}
                warnings.append(f"The new binding on {prim.GetPath()} would not take effect for {purpose_label}: {reason}")
            if purpose_label == "allPurpose" and wins:
                for label in ("preview", "full"):
                    after_label = by_purpose[label]["after_predicted"]
                    if after_label != str(material_sdf):
                        warnings.append(
                            f"For the {label} purpose {prim.GetPath()} still resolves to {after_label or 'no material'}: "
                            f"a {label}-specific binding wins over allPurpose bindings, so renderers that ask for {label} "
                            "materials keep showing it."
                        )
        _surface_warnings(UsdShade.Material(material_prim), warnings)
        descendants = _descendant_bindings(prim, purpose_token)
        if descendants["count"]:
            if new_strength == UsdShade.Tokens.strongerThanDescendants:
                notes.append(
                    f"strongerThanDescendants overrides the {descendants['count']} descendant binding(s) below "
                    f"{prim.GetPath()} (for example {descendants['examples'][0]})."
                )
            else:
                notes.append(
                    f"{descendants['count']} descendant prim(s) have their own binding and keep their own material "
                    f"(for example {descendants['examples'][0]}); pass strength='strongerThanDescendants' to override them."
                )
        if not has_api:
            notes.append("Applies MaterialBindingAPI to the prim (recorded in its apiSchemas), as UsdShade requires for bindings.")
        would_change = (
            not has_api
            or direct_path != material_sdf
            or (direct_strength or "weakerThanDescendants") != str(new_strength)
        )
        if not would_change:
            notes.append(f"{prim.GetPath()} already binds {material_sdf} this way, so writing it changes nothing.")
        fields.update(
            binding_relationship=str(rel_path),
            applies_api=not has_api,
            before=before,
            predicted=prediction,
            effective_by_purpose=by_purpose,
            descendant_bindings=descendants,
        )
    except _Refusal as refusal:
        return _result(action, dry, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _result(action, dry, stage, warnings, notes, fields, error=_UNEXPECTED.format(exc))

    if dry:
        return _result(action, True, stage, warnings, notes, fields, would_change=would_change)

    try:
        api = UsdShade.MaterialBindingAPI.Apply(prim)
        if not api:
            raise ValueError("MaterialBindingAPI.Apply failed")
        bind_strength = strength_token if strength_label is not None else UsdShade.Tokens.fallbackStrength
        if not api.Bind(UsdShade.Material(material_prim), bindingStrength=bind_strength, materialPurpose=purpose_token):
            raise ValueError("MaterialBindingAPI.Bind returned False")
    except Exception as exc:
        return _result(action, False, stage, warnings, notes, fields,
                       error=f"Authoring failed part-way (MaterialBindingAPI may already be applied): {exc}", written=True)

    api = UsdShade.MaterialBindingAPI(prim)
    bound = _bound_record(*api.ComputeBoundMaterial(purpose_token))
    after = {
        "bound_material": bound["material"],
        "via": bound["via"],
        "by_purpose": {
            label: _bound_record(*api.ComputeBoundMaterial(_purpose_tokens()[label]))["material"] for label in PURPOSES
        },
        "has_material_binding_api": bool(prim.HasAPI(UsdShade.MaterialBindingAPI)),
    }
    if bound["material"] != prediction["material"]:
        warnings.append(
            f"After the write {prim.GetPath()} resolves to {bound['material'] or 'no material'}, not the predicted "
            f"{prediction['material'] or 'no material'}; another opinion or binding rule wins."
        )
    return _result(action, False, stage, warnings, notes, fields, written=True, changed=would_change, after=after)


# --- set_attribute planning helpers ---------------------------------------------------------------------------


class _Context:
    """Per-call coercion state: the attribute name, its allowed tokens and where notes and warnings go."""

    def __init__(self, name: str, allowed: list[str], warnings: list[str], notes: list[str]) -> None:
        self.name = name
        self.allowed = allowed
        self.warnings = warnings
        self.notes = notes
        self.emissive = "emissive" in name.lower()
        self._said: set[str] = set()

    def note_once(self, key: str, text: str) -> None:
        if key not in self._said:
            self._said.add(key)
            self.notes.append(text)

    def warn_once(self, key: str, text: str) -> None:
        if key not in self._said:
            self._said.add(key)
            self.warnings.append(text)


def _material_target(stage: Any, path: Any, create_parent: bool, warnings: list[str]) -> tuple[Any, Any, Any]:
    """(material path, parent path, parent to create or None) for a new material.

    With create_parent, a missing direct parent whose own parent exists is planned as a new Scope; it passes the same
    checks as any new prim (its parent must be active, loaded and not an instance). Otherwise the parent must exist.
    """
    sdf_path = _prim_path(path, "path")
    parent_path = sdf_path.GetParentPath()
    if create_parent and parent_path != Sdf.Path.absoluteRootPath:
        parent = stage.GetPrimAtPath(parent_path)
        if not parent or not parent.IsValid():
            _creation_target(stage, str(parent_path), warnings)
            return sdf_path, parent_path, parent_path
    sdf_path, parent = _creation_target(stage, path, warnings)
    return sdf_path, parent.GetPath(), None


def _existing_prim(stage: Any, path: Any, label: str) -> Any:
    if isinstance(path, str) and Sdf.Path.IsValidPathString(path.strip()) and Sdf.Path(path.strip()).IsPropertyPath():
        raise _Refusal(f"{label} {path.strip()} is a property path; pass the prim path and the attribute name separately.")
    sdf_path = _prim_path(path, label)
    prim = stage.GetPrimAtPath(sdf_path)
    if not prim or not prim.IsValid():
        raise _Refusal(f"Prim does not exist: {sdf_path}")
    if prim.IsInstanceProxy():
        raise _Refusal(
            f"{sdf_path} is inside an instance (an instance proxy): its properties come from the shared prototype and "
            "cannot be edited here. Edit the prototype's source asset, or make the instance prim not instanceable."
        )
    if prim.IsInPrototype():
        raise _Refusal(f"{sdf_path} is inside an instancing prototype, which cannot be edited directly.")
    return prim


def _attribute_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise _Refusal("name must be an attribute name such as size, visibility or inputs:diffuseColor")
    text = name.strip()
    if len(text) > MAX_NAME_CHARS:
        raise _Refusal(f"name is longer than {MAX_NAME_CHARS} characters")
    if not Sdf.Path.IsValidNamespacedIdentifier(text):
        raise _Refusal(f"{text!r} is not a valid USD property name (letters, digits and _, with ':' between namespaces).")
    return text


def _time(time: Any) -> tuple[Any, bool]:
    """(Usd.TimeCode, numeric): None or 'default' is the default value; a finite number is a time sample."""
    if time is None:
        return Usd.TimeCode.Default(), False
    if isinstance(time, str):
        word = time.strip().lower()
        if word in ("", "default"):
            return Usd.TimeCode.Default(), False
        try:
            time = float(word)
        except ValueError:
            raise _Refusal(f"time must be a number (a time code) or None for the default value, got {time!r}") from None
    if isinstance(time, bool) or not isinstance(time, numbers.Real):
        raise _Refusal(f"time must be a number (a time code) or None for the default value, got {time!r}")
    number = float(time)
    if not math.isfinite(number):
        raise _Refusal(f"time must be finite, got {time!r}")
    return Usd.TimeCode(number), True


def _time_label(time_code: Any) -> Any:
    return "default" if time_code.IsDefault() else float(time_code.GetValue())


def _target_attribute(prim: Any, name: str, create_type: Any, notes: list[str]) -> tuple[Any, Any, bool]:
    """(existing attribute or None, its Sdf.ValueTypeName, creating) after the relationship and connection checks."""
    if prim.HasRelationship(name):
        hint = " Use bind_material for material bindings." if name.startswith("material:binding") else ""
        raise _Refusal(f"{name} on {prim.GetPath()} is a relationship (it targets other prims), not an attribute.{hint}")
    attr = prim.GetAttribute(name)
    if attr and attr.IsValid() and prim.HasAttribute(name):
        type_name = attr.GetTypeName()
        if not type_name:
            raise _Refusal(f"{name} on {prim.GetPath()} has no declared value type, so a value cannot be coerced for it.")
        if create_type is not None:
            wanted = _type_name(create_type)
            if str(wanted) != str(type_name):
                raise _Refusal(
                    f"{name} already exists as {type_name}, not {wanted}; drop create_type (the existing type is used)."
                )
            notes.append(f"create_type was not needed: {name} already exists as {type_name}.")
        if attr.HasAuthoredConnections():
            sources = ", ".join(str(path) for path in attr.GetConnections()[:MAX_SUGGESTIONS]) or "a source"
            raise _Refusal(
                f"{name} on {prim.GetPath()} is connected to {sources}, so its own value is ignored while the "
                "connection exists. Edit the connection source instead, or disconnect it first."
            )
        return attr, type_name, False
    if create_type is None:
        raise _Refusal(_missing_attribute_message(prim, name))
    type_name = _type_name(create_type)
    if name.startswith("xformOp:"):
        notes.append(f"{name} is a new transform op; it only affects the prim once it is listed in xformOpOrder.")
    return None, type_name, True


def _missing_attribute_message(prim: Any, name: str) -> str:
    message = f"{prim.GetPath()} has no attribute {name}."
    bare = name.split(":", 1)[1] if name.startswith("inputs:") else name
    if _is_preview_surface(prim) and bare in PREVIEW_SURFACE_INPUTS:
        type_text = PREVIEW_SURFACE_INPUTS[bare][0]
        return (
            f"{message} UsdPreviewSurface input inputs:{bare} is not authored yet (shader inputs exist only once "
            f"authored); pass create_type='{type_text}' to create it."
        )
    names = [str(item) for item in prim.GetPropertyNames()]
    close = [f"inputs:{name}"] if f"inputs:{name}" in names else []
    close += [item for item in difflib.get_close_matches(name, names, n=MAX_SUGGESTIONS, cutoff=0.6) if item not in close]
    if close:
        message += f" Did you mean {', '.join(close[:MAX_SUGGESTIONS])}?"
    return message + " To create a custom attribute instead, pass create_type (for example 'float' or 'color3f')."


def _is_preview_surface(prim: Any) -> bool:
    try:
        return bool(prim.IsA(UsdShade.Shader)) and str(UsdShade.Shader(prim).GetIdAttr().Get() or "") == PREVIEW_SURFACE_ID
    except Exception:
        return False


def _type_name(create_type: Any) -> Any:
    """Sdf.ValueTypeName for 'float', 'color3f', 'token[]' (also 'Color3f' or 'Float3Array' spellings)."""
    if Sdf is not None and isinstance(create_type, Sdf.ValueTypeName):
        found = create_type
    elif isinstance(create_type, str) and create_type.strip():
        text = create_type.strip()
        found = Sdf.ValueTypeNames.Find(text)
        if not found:
            alternative = text[:-5] + "[]" if text.endswith("Array") else text
            found = Sdf.ValueTypeNames.Find(alternative[:1].lower() + alternative[1:])
    else:
        found = None
    if not found:
        raise _Refusal(
            f"create_type {create_type!r} is not an Sdf value type; use a name such as float, double, int, bool, token, "
            "string, asset, color3f, float3, point3f[], quatf or matrix4d."
        )
    if str(found.scalarType) in _UNSETTABLE:
        raise _Refusal(f"create_type {found} cannot hold a value.")
    return found


def _allowed_tokens(attr: Any) -> list[str]:
    if attr is None:
        return []
    try:
        allowed = attr.GetMetadata("allowedTokens")
    except Exception:
        return []
    return [str(token) for token in allowed] if allowed else []


# --- value coercion ---------------------------------------------------------------------------------------


def _coerce(type_name: Any, value: Any, ctx: _Context) -> tuple[Any, Any]:
    """(USD value of exactly the declared type, JSON-friendly plain value) or a refusal explaining the mismatch."""
    scalar = type_name.scalarType
    kind = _kind(scalar)
    label = f"{ctx.name} ({type_name})"
    if type_name.isArray:
        items = _sequence_input(value, label)
        if len(items) > MAX_ARRAY_INPUT:
            raise _Refusal(f"{label} has {len(items)} elements; at most {MAX_ARRAY_INPUT} can be set in one call.")
        array_class = type_name.type.pythonClass
        if array_class is None:
            raise _Refusal(f"{type_name} arrays cannot be built from Python in this USD build.")
        plain_items = _coerce_elements(scalar, kind, str(type_name.role or ""), items, ctx, label)
        try:
            usd_value = array_class([_build(scalar, kind, item) for item in plain_items])
        except Exception as exc:
            raise _Refusal(f"Could not build a {type_name} array: {exc}") from None
        return usd_value, plain_items
    plain = _coerce_elements(scalar, kind, str(type_name.role or ""), [value], ctx, label)[0]
    try:
        return _build(scalar, kind, plain), plain
    except Exception as exc:
        raise _Refusal(f"Could not build a {type_name} value: {exc}") from None


def _kind(scalar: Any) -> str:
    name = str(scalar)
    if name in _UNSETTABLE:
        raise _Refusal(f"{name} attributes hold no settable value.")
    if name == "bool":
        return "bool"
    if name in _INT_RANGES:
        return "int"
    if name in _FLOAT_LIMITS:
        return "float"
    if name in _TEXT_KINDS or name == "timecode":
        return name
    if name in _QUATS:
        return "quat"
    if name in _MATRIX_SIZES:
        return "matrix"
    default = scalar.defaultValue
    if type(default).__module__.endswith("Gf") and hasattr(default, "__len__"):
        return "vector"
    raise _Refusal(f"Setting {name} values is not supported.")


def _coerce_elements(scalar: Any, kind: str, role: str, items: list[Any], ctx: _Context, label: str) -> list[Any]:
    name = str(scalar)
    if kind == "bool":
        return [_bool(item, label) for item in items]
    if kind == "int":
        return [_int(item, name, label, ctx) for item in items]
    if kind == "float":
        return [_float(item, name, label) for item in items]
    if kind == "timecode":
        return [_float(item, "double", label) for item in items]
    if kind in _TEXT_KINDS:
        return [_text_value(item, kind, label, ctx) for item in items]
    if kind == "quat":
        return [_quat(item, label, ctx) for item in items]
    if kind == "matrix":
        size = _MATRIX_SIZES[name]
        rows = [_matrix(item, size, label) for item in items]
        if size >= 3:
            cls = type(scalar.defaultValue)
            if any(abs(cls(*[v for row in matrix for v in row]).GetDeterminant()) < 1e-12 for matrix in rows):
                ctx.warn_once("singular", f"{label} is singular (zero scale), which collapses anything it transforms.")
        return rows
    dimension = len(scalar.defaultValue)
    component = type(scalar.defaultValue).__name__[-1]
    vectors = [_vector(item, dimension, component, role, label, ctx) for item in items]
    if role == "Color":
        vectors = _normalize_colors(vectors, ctx, label, allow_hdr=True)
    return vectors


def _build(scalar: Any, kind: str, plain: Any) -> Any:
    if kind in ("bool", "int", "float", "string", "token"):
        return plain
    if kind == "asset":
        return Sdf.AssetPath(plain)
    cls = type(scalar.defaultValue)
    if kind in ("timecode", "pathExpression"):
        return cls(plain)
    if kind == "matrix":
        return cls(*[value for row in plain for value in row])
    return cls(*plain)  # vectors (Gf.Vec*) and quaternions (real, i, j, k)


def _bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if _is_number(value) and float(value) in (0.0, 1.0):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    raise _Refusal(f"{label} must be true or false, got {value!r}")


def _int(value: Any, name: str, label: str, ctx: _Context) -> int:
    if isinstance(value, bool):
        ctx.note_once("bool-int", f"{label}: {value} was stored as {int(value)}.")
        value = int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            value = int(text)
        except ValueError:
            try:
                value = float(text)
            except ValueError:
                raise _Refusal(f"{label} must be a whole number, got {value!r}") from None
    if isinstance(value, numbers.Integral):
        number = int(value)
    elif _is_number(value) and math.isfinite(float(value)) and float(value).is_integer():
        number = int(float(value))
    else:
        raise _Refusal(f"{label} must be a whole number, got {value!r}")
    low, high = _INT_RANGES[name]
    if not low <= number <= high:
        raise _Refusal(f"{label} must be between {low} and {high} ({name}), got {number}")
    return number


def _float(value: Any, name: str, label: str) -> float:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise _Refusal(f"{label} must be a number, got {value!r}") from None
    if not _is_number(value):
        raise _Refusal(f"{label} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise _Refusal(f"{label} must be finite, got {value!r}")
    limit = _FLOAT_LIMITS.get(name)
    if limit is not None and abs(number) > limit:
        raise _Refusal(f"{label}: {number:g} does not fit in {name} precision (largest magnitude {limit:g}).")
    return number


def _component(value: Any, component: str, label: str) -> float | int:
    if component == "i":
        if isinstance(value, bool) or not _is_number(value) or not math.isfinite(float(value)) or not float(value).is_integer():
            raise _Refusal(f"{label} components must be whole numbers, got {value!r}")
        number = int(float(value)) if not isinstance(value, numbers.Integral) else int(value)
        low, high = _INT_RANGES["int"]
        if not low <= number <= high:
            raise _Refusal(f"{label} components must fit in int, got {number}")
        return number
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise _Refusal(f"{label} components must be numbers, got {value!r}") from None
    if not _is_number(value):
        raise _Refusal(f"{label} components must be numbers, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise _Refusal(f"{label} components must be finite, got {value!r}")
    limit = _COMPONENT_LIMITS[component]
    if limit is not None and abs(number) > limit:
        precision = "half" if component == "h" else "float"
        raise _Refusal(f"{label}: {number:g} does not fit in {precision} precision (largest magnitude {limit:g}).")
    return number


def _vector(value: Any, dimension: int, component: str, role: str, label: str, ctx: _Context) -> list[Any]:
    if isinstance(value, str):
        text = value.strip()
        if role == "Color" and text.startswith("#"):
            return _hex_color(text, dimension, label, ctx)
        value = _parse_text(text, label)
    if _is_number(value):
        ctx.note_once("broadcast", f"{label}: the single number {value} was used for all {dimension} components.")
        value = [value] * dimension
    items = _items(value, label)
    if len(items) != dimension:
        raise _Refusal(f"{label} needs {dimension} numbers, got {len(items)}")
    return [_component(item, component, label) for item in items]


def _hex_color(text: str, dimension: int, label: str, ctx: _Context) -> list[float]:
    digits = text[1:]
    if len(digits) in (3, 4):
        digits = "".join(char * 2 for char in digits)
    if not re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", digits):
        raise _Refusal(f"{label}: {text!r} is not a hex color such as #ff8800")
    channels = [int(digits[index:index + 2], 16) / 255.0 for index in range(0, len(digits), 2)]
    if dimension == 4 and len(channels) == 3:
        channels.append(1.0)
    if len(channels) != dimension:
        raise _Refusal(f"{label} needs {dimension} channels, but {text} has {len(channels)}")
    rounded = [round(channel, 6) for channel in channels]
    ctx.note_once("hex", f"{label}: hex color {text} was converted to 0-1 values {rounded} (no sRGB-to-linear conversion).")
    return channels


def _normalize_colors(vectors: list[list[float]], ctx: _Context, label: str, *, allow_hdr: bool) -> list[list[float]]:
    """0-1 colors pass; whole-number 0-255 colors are divided by 255 (except emissive colors, which may be HDR)."""
    flat = [component for vector in vectors for component in vector]
    if not flat:
        return vectors
    if min(flat) < 0.0:
        raise _Refusal(f"{label} color components must not be negative, got {min(flat):g}")
    top = max(flat)
    if top <= 1.0:
        return vectors
    if ctx.emissive:
        ctx.note_once(
            "hdr-emissive",
            f"{label}: values above 1 are kept as HDR emission strength. If you meant 0-255 color values, divide them "
            "by 255 or pass a hex string such as #ff8800.",
        )
        return vectors
    if top <= 255.0 and all(float(component).is_integer() for component in flat):
        normalized = [[component / 255.0 for component in vector] for vector in vectors]
        example = [round(component, 6) for component in normalized[0]]
        ctx.note_once(
            "0-255",
            f"{label}: whole-number values up to {top:g} looked like 0-255 colors, so they were divided by 255 "
            f"(first color {example}; no sRGB-to-linear conversion). USD colors are 0-1.",
        )
        return normalized
    if not allow_hdr:
        raise _Refusal(
            f"{label} components must be 0-1 (or whole-number 0-255 values, or a hex string), got a maximum of {top:g}."
        )
    ctx.warn_once("hdr", f"{label}: components above 1 (max {top:g}) are over-bright HDR values; colors are usually 0-1.")
    return vectors


def _quat(value: Any, label: str, ctx: _Context) -> list[float]:
    if isinstance(value, dict):
        real = value.get("real", value.get("w"))
        imaginary = value.get("imaginary")
        if imaginary is None:
            imaginary = [value.get("x"), value.get("y"), value.get("z")]
        items = [real, *_items(imaginary, label)]
    elif hasattr(value, "GetReal") and hasattr(value, "GetImaginary"):
        items = [value.GetReal(), *list(value.GetImaginary())]
    else:
        if isinstance(value, str):
            value = _parse_text(value.strip(), label)
        items = _items(value, label)
    if len(items) != 4:
        raise _Refusal(f"{label} needs 4 numbers (w, x, y, z) = (real, i, j, k), got {len(items)}")
    numbers_ = [float(_component(item, "d", label)) for item in items]
    length = math.sqrt(sum(item * item for item in numbers_))
    if length < 1e-12:
        raise _Refusal(f"{label} is a zero quaternion, which is not a rotation.")
    if abs(length - 1.0) > 1e-6:
        numbers_ = [item / length for item in numbers_]
        ctx.note_once("quat", f"{label}: the quaternion had length {length:g}, so it was normalized to a unit rotation.")
    return numbers_


def _matrix(value: Any, size: int, label: str) -> list[list[float]]:
    if type(value).__name__.startswith("Matrix") and not type(value).__name__.endswith("Array"):
        return [[float(value[row][col]) for col in range(size)] for row in range(size)]
    if isinstance(value, str):
        value = _parse_text(value.strip(), label)
    items = _items(value, label)
    if len(items) == size * size and all(_is_number(item) for item in items):
        flat = items
    elif len(items) == size:
        rows = [_items(row, label) for row in items]
        if any(len(row) != size for row in rows):
            raise _Refusal(f"{label} needs {size} rows of {size} numbers")
        flat = [item for row in rows for item in row]
    else:
        raise _Refusal(f"{label} needs {size}x{size} numbers (nested rows or {size * size} in row order), got {len(items)} items")
    numbers_ = [float(_component(item, "d", label)) for item in flat]
    return [numbers_[row * size:(row + 1) * size] for row in range(size)]


def _text_value(value: Any, kind: str, label: str, ctx: _Context) -> str:
    if kind == "asset":
        if Sdf is not None and isinstance(value, Sdf.AssetPath):
            value = value.path
        elif isinstance(value, dict) and isinstance(value.get("authored"), str):
            value = value["authored"]  # the shape to_jsonable reports for asset paths
    if not isinstance(value, str):
        raise _Refusal(f"{label} must be text, got {value!r}")
    if len(value) > MAX_STRING_INPUT:
        raise _Refusal(f"{label} is longer than {MAX_STRING_INPUT} characters")
    if kind == "asset" and "\\" in value:
        ctx.warn_once("backslash", f"{label}: {value!r} uses backslashes; USD asset paths are portable with forward slashes.")
    if kind == "token" and ctx.allowed:
        if value not in ctx.allowed:
            matches = [token for token in ctx.allowed if token.lower() == value.strip().lower()]
            if not matches:
                raise _Refusal(f"{value!r} is not an allowed token for {ctx.name}; allowed tokens: {', '.join(ctx.allowed)}.")
            ctx.note_once(f"token-{value}", f"{label}: {value!r} was matched to the allowed token {matches[0]!r}.")
            value = matches[0]
    if kind == "pathExpression":
        try:
            Sdf.PathExpression(value)
        except Exception as exc:
            raise _Refusal(f"{label}: {value!r} is not a path expression: {exc}") from None
    return value


def _sequence_input(value: Any, label: str) -> list[Any]:
    if isinstance(value, str):
        value = _parse_text(value.strip(), label)
    return _items(value, label)


def _items(value: Any, label: str) -> list[Any]:
    if isinstance(value, (str, bytes, dict)) or not hasattr(value, "__len__") or not hasattr(value, "__getitem__"):
        raise _Refusal(f"{label} must be a list of values, got {value!r}"[:300])
    try:
        return [value[index] for index in range(len(value))]
    except Exception:
        raise _Refusal(f"{label} must be a list of values, got {value!r}"[:300]) from None


def _parse_text(text: str, label: str) -> Any:
    """'[1, 2, 3]', '(1, 2, 3)', '1, 2, 3' or USD-style '[(1, 2, 3), (4, 5, 6)]' to Python lists and numbers."""
    if text[:1] in "[(":
        try:
            return json.loads(text.replace("(", "[").replace(")", "]"))
        except ValueError:
            raise _Refusal(f"{label}: could not read {text[:120]!r} as a list") from None
    parts = [part for part in re.split(r"[\s,]+", text) if part]
    try:
        values = [float(part) for part in parts]
    except ValueError:
        raise _Refusal(f"{label}: could not read {text[:120]!r} as numbers") from None
    return values[0] if len(values) == 1 else values


def _is_number(value: Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


# --- opinions, prediction and comparison ------------------------------------------------------------------


def _before(stage: Any, attr: Any, time_code: Any, numeric: bool, precision: int | None) -> tuple[dict[str, Any], Any]:
    """What the attribute resolves to now, which spec wins, and the edit target's own opinion (plain, for compare)."""
    if attr is None:
        return {"exists": False, "value": None, "source": "none", "winning_layer": None, "winning_spec": None,
                "winning_opinion": None, "time_sample_count": 0, "edit_target_value": None}, _MISSING
    info = attr.GetResolveInfo(time_code)
    winner = None
    for spec in attr.GetPropertyStack(time_code):
        opinion = _spec_opinion(spec, numeric)
        if opinion:
            winner = (spec, opinion)
            break
    edit_plain, edit_json = _edit_target_opinion(stage, attr, time_code, numeric)
    before = {
        "exists": True,
        "value": _tidy(to_jsonable(attr.Get(time_code)), precision),
        "source": _source_word(info.GetSource()),
        "blocked": bool(info.ValueIsBlocked()),
        "winning_layer": winner[0].layer.identifier if winner else None,
        "winning_spec": str(winner[0].path) if winner else None,
        "winning_opinion": winner[1] if winner else None,
        "time_sample_count": int(attr.GetNumTimeSamples()),
        "edit_target_value": _tidy(edit_json, precision),
    }
    if edit_plain is _UNKNOWN:
        before["edit_target_value"] = "unknown (the edit target has a time offset)"
    return before, edit_plain


def _edit_target_opinion(stage: Any, attr: Any, time_code: Any, numeric: bool) -> tuple[Any, Any]:
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    spec_path = edit_target.MapToSpecPath(attr.GetPath())
    spec = layer.GetAttributeAtPath(spec_path) if spec_path and not spec_path.isEmpty else None
    if spec is None:
        return _MISSING, None
    if numeric:
        offset = edit_target.GetMapFunction().timeOffset
        if not offset.IsIdentity():
            return _UNKNOWN, None
        if not layer.GetNumTimeSamplesForPath(spec_path):
            return _MISSING, None
        value = layer.QueryTimeSample(spec_path, time_code.GetValue())
        if value is None:
            return _MISSING, None
    else:
        if not spec.HasDefaultValue():
            return _MISSING, None
        value = spec.default
    return _plain(value), to_jsonable(value)


def _predict_effective(
    stage: Any,
    attr: Any,
    name: str,
    after: Any,
    before: dict[str, Any],
    time_code: Any,
    numeric: bool,
    warnings: list[str],
    notes: list[str],
) -> dict[str, Any]:
    """Will the composed value show the edit? Checks stronger specs (local layer stack) and same-layer samples."""
    label = _time_label(time_code)
    effective: dict[str, Any] = {"time": label, "predicted_value": after, "visible": True, "hidden_by": None,
                                 "predicted": True}
    if attr is None:
        if not numeric:
            effective["visible_on_timeline"] = True
        return effective
    edit_layer = stage.GetEditTarget().GetLayer().identifier
    stronger = _stronger_specs(stage, attr)
    if stronger is None:
        effective.update(visible=None, predicted=False, predicted_value=None)
        warnings.append(
            "The edit target maps through a composition arc (or is outside the stage's layer stack), so whether a "
            "stronger opinion hides this edit could not be predicted; check the readback after the write."
        )
        return effective
    hidden_default = hidden_timeline = None
    for spec in stronger:
        opinion_default = _spec_opinion(spec, False)
        opinion_timeline = _spec_opinion(spec, True)
        if hidden_default is None and opinion_default:
            hidden_default = _spec_record(spec, opinion_default)
        if hidden_timeline is None and opinion_timeline:
            hidden_timeline = _spec_record(spec, opinion_timeline)
    if numeric:
        if hidden_timeline:
            effective.update(visible=False, hidden_by=hidden_timeline, predicted_value=before["value"])
            warnings.append(
                f"{_text(hidden_timeline['layer'])} holds a stronger {_opinion_words(hidden_timeline['opinion'])} for "
                f"{name}, so this time sample in {_text(edit_layer)} would be hidden (the strongest layer with a value "
                "wins). Edit that layer instead (set it as the edit target) to see the change."
            )
        if before["time_sample_count"] == 0:
            notes.append(
                f"{name} had no time samples: with a single sample it holds this value at every time, and its default "
                "is ignored whenever a time is given."
            )
        return effective
    if hidden_default:
        effective.update(visible=False, hidden_by=hidden_default, predicted_value=before["value"])
        warnings.append(
            f"{_text(hidden_default['layer'])} holds a stronger {_opinion_words(hidden_default['opinion'])} for {name}, "
            f"so this edit in {_text(edit_layer)} would be hidden at every time (within a layer stack the stronger "
            "layer wins). Edit that layer instead (set it as the edit target) to see the change."
        )
    timeline_hidden = hidden_timeline
    if timeline_hidden is None:
        own = _own_samples(stage, attr)
        if own:
            timeline_hidden = {"layer": edit_layer, "spec_path": own, "opinion": "time_samples"}
            warnings.append(
                f"{_text(edit_layer)} also has time samples for {name}; time samples win over the default at every "
                "timeline time, so the viewport keeps showing the animation. Pass time=<frame> to change a sample instead."
            )
    elif not hidden_default:
        warnings.append(
            f"{_text(timeline_hidden['layer'])} has stronger time samples for {name}: at timeline times (playback and "
            "the viewport) they win, so the new default only shows when the stage is read at the default time."
        )
    effective["visible_on_timeline"] = timeline_hidden is None
    effective["timeline_hidden_by"] = timeline_hidden
    return effective


def _own_samples(stage: Any, attr: Any) -> str | None:
    edit_target = stage.GetEditTarget()
    spec_path = edit_target.MapToSpecPath(attr.GetPath())
    layer = edit_target.GetLayer()
    if spec_path and not spec_path.isEmpty and layer.GetNumTimeSamplesForPath(spec_path):
        return str(spec_path)
    return None


def _stronger_specs(stage: Any, prop: Any) -> list[Any] | None:
    """Property specs stronger than where the edit target writes, or None when that cannot be told."""
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    try:
        specs = list(prop.GetPropertyStack(Usd.TimeCode.Default()))
    except Exception:
        return None
    # A local sublayer with only a time offset still maps paths to themselves, so layer-stack order decides; time
    # samples under an offset are handled by _edit_target_opinion.
    if edit_target.GetMapFunction().isIdentityPathMapping:
        order = [item.identifier for item in stage.GetLayerStack(True)]
        if layer.identifier not in order:
            return None
        limit = order.index(layer.identifier)
        return [
            spec for spec in specs
            if spec.path == prop.GetPath() and spec.layer.identifier in order and order.index(spec.layer.identifier) < limit
        ]
    spec_path = edit_target.MapToSpecPath(prop.GetPath())
    for index, spec in enumerate(specs):
        if spec.layer.identifier == layer.identifier and spec.path == spec_path:
            return specs[:index]
    return None


def _spec_opinion(spec: Any, numeric: bool) -> str | None:
    """The opinion a spec contributes at the default time (numeric=False) or at a timeline time."""
    try:
        has_default = bool(spec.HasDefaultValue())
        samples = int(spec.layer.GetNumTimeSamplesForPath(spec.path))
    except Exception:
        return None
    if numeric:
        if samples:
            return "time_samples"
        has_spline = getattr(spec, "HasSpline", None)
        try:
            if has_spline is not None and has_spline():
                return "spline"
        except Exception:
            pass
    if has_default:
        return "block" if isinstance(spec.default, Sdf.ValueBlock) else "default"
    return None


def _spec_record(spec: Any, opinion: str) -> dict[str, Any]:
    return {"layer": _text(spec.layer.identifier), "spec_path": str(spec.path), "opinion": opinion}


def _opinion_words(opinion: str) -> str:
    return {"default": "default value", "block": "value block", "time_samples": "set of time samples", "spline": "spline"}.get(
        opinion, opinion
    )


def _source_word(source: Any) -> str:
    text = str(source).rsplit(".", 1)[-1].replace("ResolveInfoSource", "")
    return re.sub(r"(?<!^)([A-Z])", r"_\1", text).lower() or "none"


def _semantic_warnings(prim: Any, name: str, plain: Any, creating: bool, warnings: list[str]) -> None:
    if not prim.IsActive():
        warnings.append(f"{prim.GetPath()} is inactive, so the new value has no visible effect until it is activated.")
    xformable = UsdGeom.Xformable(prim)
    if not xformable:
        return
    order = [str(item) for item in (xformable.GetXformOpOrderAttr().Get() or [])]
    if name.startswith("xformOp:") and not creating and name not in order:
        warnings.append(f"{name} is not listed in xformOpOrder {order}, so changing it does not move the prim.")
    if name == "xformOpOrder" and isinstance(plain, list):
        missing = [item for item in plain if not item.startswith("!resetXformStack!") and not prim.HasAttribute(
            item[len("!invert!"):] if item.startswith("!invert!") else item)]
        if missing:
            warnings.append(f"xformOpOrder lists ops that do not exist on {prim.GetPath()}: {missing[:MAX_SUGGESTIONS]}.")


def _precision(type_name: Any) -> int | None:
    """Significant digits worth reporting for the type's components (half 4, float 7, double all).

    Keyed on the component type: half/half2/half3/half4 and float/float2/float3/float4 end in a digit, the role
    types (color3h, point3f, quatf, texCoord2h) end in their component letter.
    """
    name = str(type_name.scalarType)
    if name.startswith("half") or name.endswith("h"):
        return 4
    if name.startswith("float") or name.endswith("f"):
        return 7
    return None


def _tolerance(precision: int | None) -> float:
    return {4: 1e-3, 7: 1e-6}.get(precision or 0, 1e-12)


def _tidy(value: Any, digits: int | None) -> Any:
    if digits is None:
        return value
    if isinstance(value, float):
        return float(f"{value:.{digits}g}")
    if isinstance(value, list):
        return [_tidy(item, digits) for item in value]
    if isinstance(value, dict):
        return {key: _tidy(item, digits) for key, item in value.items()}
    return value


def _plain(value: Any) -> Any:
    """Uncapped nested Python data for comparing USD values with coerced input."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Sdf.ValueBlock):
        return _BLOCK
    if isinstance(value, Sdf.AssetPath):
        return value.path
    if hasattr(value, "GetReal") and hasattr(value, "GetImaginary"):
        return [float(value.GetReal()), *[float(item) for item in value.GetImaginary()]]
    type_name = type(value).__name__
    if type_name.startswith("Matrix") and not type_name.endswith("Array"):
        size = len(value)
        return [[float(value[row][col]) for col in range(size)] for row in range(size)]
    if type_name == "TimeCode":
        return float(value)
    if hasattr(value, "GetText") and type_name == "PathExpression":
        return value.GetText()
    if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
        return [_plain(value[index]) for index in range(len(value))]
    return str(value)


def _same(first: Any, second: Any, tolerance: float) -> bool:
    if first is _BLOCK or second is _BLOCK or first is _MISSING or second is _MISSING:
        return first is second
    if isinstance(first, bool) or isinstance(second, bool):
        return first == second
    if _is_number(first) and _is_number(second):
        return math.isclose(float(first), float(second), rel_tol=tolerance, abs_tol=tolerance)
    if isinstance(first, list) and isinstance(second, list):
        return len(first) == len(second) and all(_same(a, b, tolerance) for a, b in zip(first, second))
    return first == second


# --- material helpers -------------------------------------------------------------------------------------


def _material_color(value: Any, name: str, warnings: list[str], notes: list[str], *, allow_hdr: bool) -> list[float]:
    ctx = _Context(name, [], warnings, notes)
    label = f"{name} (color3f)"
    vector = _vector(value, 3, "f", "Color", label, ctx)
    return _normalize_colors([vector], ctx, label, allow_hdr=allow_hdr)[0]


def _unit_float(value: Any, name: str) -> float:
    number = _float(value, "float", name)
    if not 0.0 <= number <= 1.0:
        raise _Refusal(f"{name} must be between 0 and 1, got {number:g}")
    return number


def _surface_source(material: Any) -> str | None:
    try:
        shader, output_name, _source_type = material.ComputeSurfaceSource()
    except Exception:
        return None
    if not shader:
        return None
    return f"{shader.GetPath()}.outputs:{output_name}"


def _surface_warnings(material: Any, warnings: list[str]) -> None:
    try:
        outputs = list(material.GetSurfaceOutputs())
    except Exception:
        return
    if not any(output.HasConnectedSource() for output in outputs):
        warnings.append(
            f"{material.GetPath()} has no connected surface output, so renderers show a fallback look for it; "
            "connect a shader (create_material does this)."
        )


def _remove_new_spec(layer: Any, first_new: Any) -> bool:
    if first_new is None or not layer.GetPrimAtPath(first_new):
        return first_new is not None
    edit = Sdf.BatchNamespaceEdit()
    edit.Add(Sdf.NamespaceEdit.Remove(first_new))
    return bool(layer.Apply(edit))


# --- binding helpers --------------------------------------------------------------------------------------


def _purpose_tokens() -> dict[str, Any]:
    """Purpose label -> UsdShade token (built lazily because pxr may be missing at import time)."""
    return {"allPurpose": UsdShade.Tokens.allPurpose, "preview": UsdShade.Tokens.preview, "full": UsdShade.Tokens.full}


def _purpose(value: Any) -> tuple[Any, str]:
    if value is None:
        return UsdShade.Tokens.allPurpose, "allPurpose"
    text = str(value).strip()
    key = text.lower().replace("_", "").replace(" ", "")
    if key in ("", "all", "allpurpose"):
        return UsdShade.Tokens.allPurpose, "allPurpose"
    if key in ("preview", "full"):
        return _purpose_tokens()[key], key
    raise _Refusal(f"purpose must be allPurpose, preview or full, got {value!r}")


def _strength(value: Any) -> tuple[Any, str | None]:
    if value is None:
        return UsdShade.Tokens.fallbackStrength, None
    key = str(value).strip().lower().replace("_", "").replace(" ", "")
    if key in ("weaker", "weakerthandescendants"):
        return UsdShade.Tokens.weakerThanDescendants, "weakerThanDescendants"
    if key in ("stronger", "strongerthandescendants"):
        return UsdShade.Tokens.strongerThanDescendants, "strongerThanDescendants"
    raise _Refusal(f"strength must be weakerThanDescendants or strongerThanDescendants, got {value!r}")


def _binding_rel_name(purpose_token: Any) -> str:
    base = str(UsdShade.Tokens.materialBinding)
    return base if purpose_token == UsdShade.Tokens.allPurpose else f"{base}:{purpose_token}"


def _purpose_label(token: Any) -> str:
    return "allPurpose" if token == UsdShade.Tokens.allPurpose else str(token)


def _bound_record(material: Any, rel: Any) -> dict[str, Any]:
    return {
        "material": str(material.GetPath()) if material and material.GetPrim().IsValid() else None,
        "via": str(rel.GetPath()) if rel else None,
    }


def _walk_binding(stage: Any, prim: Any, purpose_token: Any, override: dict[str, Any] | None) -> dict[str, Any] | None:
    """The binding that wins for prim, following UsdShade's ComputeBoundMaterial rules.

    Purpose-specific bindings are searched first (on the prim and all its ancestors), then allPurpose ones. Walking
    up from the prim, the closest binding wins unless an ancestor's binding is strongerThanDescendants. At each prim
    the direct binding is considered first, then the first matching collection binding, which also beats the
    direct binding on the same prim. override replaces one direct binding to predict an edit without writing it.
    """
    stronger = UsdShade.Tokens.strongerThanDescendants
    passes = [purpose_token] if purpose_token == UsdShade.Tokens.allPurpose else [purpose_token, UsdShade.Tokens.allPurpose]
    target = prim.GetPath()
    queries: dict[str, Any] = {}
    for pass_purpose in passes:
        winner: dict[str, Any] | None = None
        current = prim
        while current and current.IsValid() and not current.IsPseudoRoot():
            api = UsdShade.MaterialBindingAPI(current)
            if override and current.GetPath() == override["prim"] and pass_purpose == override["purpose"]:
                # material None: the relationship composes to other than one target, which UsdShade ignores.
                direct = None if override["material"] is None else {
                    "material": override["material"], "strength": override["strength"], "via": override["via"]}
            else:
                binding = api.GetDirectBinding(pass_purpose)
                path = binding.GetMaterialPath()
                direct = None
                if not path.isEmpty:
                    rel = binding.GetBindingRel()
                    direct = {
                        "material": path,
                        "strength": UsdShade.MaterialBindingAPI.GetMaterialBindingStrength(rel),
                        "via": str(rel.GetPath()),
                    }
            if direct and (winner is None or direct["strength"] == stronger):
                winner = {**direct, "prim": current.GetPath(), "kind": "direct"}
            for binding in api.GetCollectionBindings(pass_purpose):
                if not binding.IsValid() or not _collection_includes(binding, target, queries):
                    continue
                rel = binding.GetBindingRel()
                strength = UsdShade.MaterialBindingAPI.GetMaterialBindingStrength(rel)
                if winner is None or strength == stronger or winner["prim"] == current.GetPath():
                    winner = {
                        "material": binding.GetMaterialPath(),
                        "strength": strength,
                        "via": str(rel.GetPath()),
                        "prim": current.GetPath(),
                        "kind": "collection",
                        "collection": str(binding.GetCollectionPath()),
                    }
                    break
            current = current.GetParent()
        if winner:
            material_prim = stage.GetPrimAtPath(winner["material"])
            valid = bool(material_prim) and material_prim.IsValid() and material_prim.IsA(UsdShade.Material)
            winner.update(
                material=str(winner["material"]) if valid else None,
                target=str(winner["material"]),
                strength=str(winner["strength"]),
                prim=str(winner["prim"]),
                purpose_pass=_purpose_label(pass_purpose),
            )
            return winner
    return None


def _collection_includes(binding: Any, path: Any, queries: dict[str, Any]) -> bool:
    key = str(binding.GetCollectionPath())
    try:
        if key not in queries:
            queries[key] = binding.GetCollection().ComputeMembershipQuery()
        return bool(queries[key].IsPathIncluded(path))
    except Exception:
        return False


def _public_binding(winner: dict[str, Any]) -> dict[str, Any]:
    keys = ("material", "target", "via", "prim", "kind", "strength", "collection", "purpose_pass")
    return {key: winner[key] for key in keys if key in winner}


def _hidden_reason(prim: Any, winner: dict[str, Any]) -> str:
    if winner["prim"] == str(prim.GetPath()) and winner["kind"] == "collection":
        return (
            f"the collection binding {winner['via']} on the same prim wins over its direct binding (it binds "
            f"{winner['target']})."
        )
    if winner["strength"] == str(UsdShade.Tokens.strongerThanDescendants):
        return (
            f"{winner['prim']} binds {winner['target']} with strongerThanDescendants ({winner['via']}), which overrides "
            "every binding below it."
        )
    return f"{winner['via']} binds {winner['target']} and wins under UsdShade's binding rules."


def _stronger_binding_opinion(stage: Any, prim: Any, rel_name: str) -> Any:
    """Opinions above the edit target for the binding relationship: None when there are none, _UNKNOWN when that
    cannot be told (the edit target maps through an arc), else {"layer", "ops", "targets", "strength_layer"}.

    ops are the stronger specs' target list ops, strongest first; layer is the strongest layer with targets (or
    with bindMaterialAs when none has targets); strength_layer is the strongest layer with bindMaterialAs.
    """
    rel = prim.GetRelationship(rel_name)
    if not rel:
        return None
    specs = _stronger_specs(stage, rel)
    if specs is None:
        return _UNKNOWN
    ops: list[Any] = []
    target_layer = strength_layer = None
    for spec in specs:
        targets = getattr(spec, "targetPathList", None)
        has_targets = targets is not None and (
            targets.isExplicit
            or any(list(getattr(targets, name, [])) for name in ("prependedItems", "appendedItems", "addedItems", "deletedItems"))
        )
        if has_targets:
            ops.append(targets)
            target_layer = target_layer or _text(spec.layer.identifier)
        if strength_layer is None and spec.HasInfo("bindMaterialAs"):
            strength_layer = _text(spec.layer.identifier)
    if not ops and not strength_layer:
        return None
    return {"layer": target_layer or strength_layer, "ops": ops, "targets": bool(ops), "strength_layer": strength_layer}


def _descendant_bindings(prim: Any, purpose_token: Any) -> dict[str, Any]:
    """Descendants with their own direct binding (for this purpose or allPurpose), capped."""
    tokens = {purpose_token, UsdShade.Tokens.allPurpose}
    count = 0
    visited = 0
    examples: list[str] = []
    truncated = False
    iterator = iter(Usd.PrimRange(prim))
    next(iterator, None)  # the prim itself
    for descendant in iterator:
        visited += 1
        if visited > MAX_DESCENDANTS:
            truncated = True
            break
        api = UsdShade.MaterialBindingAPI(descendant)
        if any(not api.GetDirectBinding(token).GetMaterialPath().isEmpty for token in tokens):
            count += 1
            if len(examples) < MAX_EXAMPLES:
                examples.append(str(descendant.GetPath()))
    return {"count": count, "examples": examples, "truncated": truncated}


# --- result helpers ---------------------------------------------------------------------------------------


def _result(
    action: str,
    dry: bool,
    stage: Any,
    warnings: list[str],
    notes: list[str],
    fields: dict[str, Any],
    *,
    error: str | None = None,
    written: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    info = _edit_target_info(stage) if stage is not None and _PXR_IMPORT_ERROR is None else None
    result: dict[str, Any] = {"ok": error is None, "dry_run": dry, "action": action, "written": written}
    if error is not None:
        result["error"] = _text(error)
    result.update(fields)
    result.update(extra)
    result["edit_target_layer"] = info["identifier"] if info else None
    result["edit_target"] = info
    for key, items in (("warnings", warnings), ("notes", notes)):
        unique: list[str] = []
        for item in items:
            text = _text(item)
            if text not in unique:
                unique.append(text)
        result[key] = unique[:MAX_WARNINGS]
        if len(unique) > MAX_WARNINGS:
            result[f"{key}_truncated"] = len(unique) - MAX_WARNINGS
    if error is None and dry:
        result["next_step"] = "Dry run: nothing was written. Call again with dry_run=False to author this."
    return result
