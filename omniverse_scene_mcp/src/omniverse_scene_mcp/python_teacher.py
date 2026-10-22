""""Show me the Python": short, runnable OpenUSD snippets that do what a router tool does, explained line by line.

python_for(action, args, context=...) returns a snippet for one task (inspect a prim, move it, add a light, a
reference, a variant set, a rigid body...) plus a plain-English explanation of every non-blank line, the USD
concepts it uses, where to run it, what it changes, and the NCP-OUSD exam objectives it touches.
list_python_topics() lists the supported actions by group.

Two contexts:
  * "script_editor": paste into Isaac Sim / USD Composer (Window > Script Editor). The stage is the open scene,
    from omni.usd.get_context().get_stage(); edits stay in memory until the scene is saved.
  * "standalone": a plain Python file with usd-core. The stage is a .usda file opened with Usd.Stage.Open (or
    created with Usd.Stage.CreateNew); editing snippets end with stage.Save().

Safety: user values never become code. Every value is validated (prim paths with Sdf.Path.IsValidPathString,
names as identifiers, numbers as finite numbers, choices against a fixed list, file paths without control
characters) and is written into the snippet only as repr() of a plain str / float / int / bool / tuple, in a
"settings" assignment at the top. Structural choices (prim type, light type, value type) pick from fixed
templates. Nothing is evaluated here; the finished snippet is only compiled as a syntax self-check.

Imports only the standard library (and pxr when available, for path validation). Snippets use only APIs present
in USD 25.11 (Isaac Sim 6.x / Kit) and usd-core 26.8.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

try:
    from pxr import Sdf
except ImportError:  # pragma: no cover - depends on the local environment
    Sdf = None  # type: ignore[assignment]

CONTEXTS = ("script_editor", "standalone")
WHERE_TO_RUN = {
    "script_editor": "Isaac Sim / Composer: Window > Script Editor, paste, Ctrl+Enter",
    "standalone": "a plain Python file with usd-core",
}
DEFAULT_STAGE_PATH = "my_scene.usda"
USD_FILE_EXTS = (".usd", ".usda", ".usdc")
MAX_TEXT = 2000

Line = tuple[str, "str | None"]

# One-line meanings for every USD term a snippet can use.
CONCEPTS = {
    "stage": "The whole composed scene you are working on: all layers combined into one view.",
    "prim": "One object (node) in the scene tree, such as /World/Cube.",
    "prim path": "The address of a prim, like a folder path: /World/Cube is Cube inside World.",
    "prim type (schema)": "What kind of prim it is (Xform, Cube, Mesh, RectLight...); the type decides its built-in attributes.",
    "API schema": "An extra bundle of attributes you apply to a prim (like a sticker), e.g. PhysicsRigidBodyAPI.",
    "attribute": "A named value on a prim, such as size, radius or xformOp:translate.",
    "relationship": "A named pointer from a prim to other prims, such as a material binding.",
    "value type": "The data type of an attribute (float, double, token, color3f...), chosen with Sdf.ValueTypeNames.",
    "authored vs fallback value": "Authored means someone wrote a value; fallback is the schema default used when nothing is written.",
    "children": "The prims directly under another prim in the scene tree.",
    "traverse": "Walking over every prim in the stage, parents before children.",
    "Define": "Creates a prim with a type (a 'def'), or just sets the type if the prim is already there.",
    "xformOp": "One transform step stored as an attribute (translate, rotateXYZ, orient, scale...).",
    "xformOpOrder": "The list saying which xformOps apply and in which order.",
    "XformCommonAPI": "A helper that edits translate / rotate / scale in the common stack; it refuses other stacks.",
    "orient (quaternion)": "A rotation stored as a quaternion in xformOp:orient, the way Isaac Sim stores rotation.",
    "local vs world transform": "Local is relative to the parent prim; world is the final placement after all parents.",
    "edit target": "The layer that receives your edits right now (normally the root layer).",
    "layer": "One USD file (or in-memory document) holding opinions about prims.",
    "root layer": "The top layer of the stage, the file you opened.",
    "sublayer": "A layer stacked under another; earlier entries in subLayerPaths are stronger.",
    "spec": "The data one layer holds for one prim or property; a prim on the stage can be built from many specs.",
    "namespace edit": "Renaming or moving a prim so every layer in the layer stack is updated consistently.",
    "active / deactivate": "Deactivating hides a prim and its children non-destructively; it can be switched back on.",
    "reference": "A composition arc that brings another file's prim (usually its defaultPrim) into this prim.",
    "payload": "Like a reference, but it can be unloaded to keep big scenes light.",
    "defaultPrim": "The prim a file offers when someone references it without naming a prim.",
    "layer offset": "Shifts (and scales) the time of referenced animation, e.g. start 10 frames later.",
    "variant set": "A named switch on a prim (e.g. color) holding alternative versions (red, green...).",
    "variant selection": "Which variant of a variant set is currently chosen.",
    "LIVERPS": "Strength order of opinions: Local, Inherits, Variants, Relocates, References, Payloads, Specializes.",
    "composition arc": "A way one piece of scene description pulls in another: sublayers, references, payloads, variants...",
    "flatten": "Bake all layers and arcs of a stage into one single layer with no composition left.",
    "usda vs usdc": ".usda is readable text, .usdc is compact fast binary (crate); .usd can be either.",
    "UsdLux light": "Light prims (RectLight, SphereLight, DomeLight, DistantLight...) with inputs:intensity etc.",
    "camera": "A UsdGeom.Camera prim; it looks down its local -Z axis with +Y up.",
    "focal length": "Lens zoom in millimetres: bigger numbers zoom in, smaller numbers see wider.",
    "up axis": "Which world axis points up: Z in Isaac Sim, often Y in Composer and DCC apps.",
    "metersPerUnit": "How many metres one stage unit is (1.0 in Isaac Sim, often 0.01 = centimetres in Composer).",
    "material": "A UsdShade.Material prim that describes how a surface looks.",
    "shader": "A node inside a material; UsdPreviewSurface is the portable standard surface shader.",
    "UsdPreviewSurface": "USD's simple standard surface (diffuseColor, roughness, metallic...) that every renderer understands.",
    "material binding": "The material:binding relationship (via MaterialBindingAPI) that says which material a prim uses.",
    "primvar": "A 'primitive variable' such as primvars:displayColor that renderers can read across a surface.",
    "physics scene": "A UsdPhysics.Scene prim with gravity settings; simulation needs one.",
    "rigid body": "PhysicsRigidBodyAPI: the prim moves as one solid object under physics.",
    "collider": "PhysicsCollisionAPI: the prim takes part in collisions.",
    "collision approximation": "The simpler shape physics uses for a mesh collider (convexHull, convexDecomposition...).",
    "mass": "PhysicsMassAPI's mass in stage mass units (kilograms when kilogramsPerUnit is 1).",
    "save": "Writes changed layers back to their files on disk.",
    "export": "Writes the stage (or one layer) to a new file, leaving the original untouched.",
    "undo": "Kit commands (omni.kit.commands) go on Ctrl+Z; plain pxr edits do not.",
    "time code": "A frame number on the timeline (Usd.TimeCode); Default() means the non-animated value.",
    "time sample": "A value stored at one time code; between samples USD interpolates.",
}

MODULE_MEANINGS = {
    "Usd": "Usd (stages and prims)",
    "UsdGeom": "UsdGeom (geometry, cameras and transforms)",
    "UsdLux": "UsdLux (lights)",
    "UsdShade": "UsdShade (materials and shaders)",
    "UsdPhysics": "UsdPhysics (physics schemas)",
    "Sdf": "Sdf (paths, layers and value types)",
    "Gf": "Gf (math types: vectors, matrices, rotations)",
}
_MODULE_ORDER = ("Gf", "Sdf", "Usd", "UsdGeom", "UsdLux", "UsdPhysics", "UsdShade")
_MODULE_RE = re.compile(r"\b(Gf|Sdf|Usd|UsdGeom|UsdLux|UsdPhysics|UsdShade)\.")

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NS_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(:[A-Za-z_][A-Za-z0-9_]*)*$")
_PRIM_PATH_RE = re.compile(r"^(/[A-Za-z_][A-Za-z0-9_]*)+$")
_VARIANT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*$")
_OS_RE = re.compile(r"\bos\.")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

PRIM_TYPES = ("Xform", "Scope", "Cube", "Sphere", "Cylinder", "Cone", "Capsule", "Plane", "Mesh")
LIGHT_TYPES = {
    "rect": "RectLight",
    "sphere": "SphereLight",
    "disk": "DiskLight",
    "distant": "DistantLight",
    "dome": "DomeLight",
    "cylinder": "CylinderLight",
}
LIGHT_INTENSITY = {"rect": 5000.0, "sphere": 30000.0, "disk": 5000.0, "distant": 3000.0, "dome": 1000.0, "cylinder": 5000.0}
APPROXIMATIONS = ("convexHull", "convexDecomposition", "boundingCube", "boundingSphere", "meshSimplification", "none")
# value_type -> (Sdf.ValueTypeNames member, value kind, default value)
VALUE_TYPES = {
    "bool": ("Bool", "bool", True),
    "int": ("Int", "int", 1),
    "float": ("Float", "number", 1.0),
    "double": ("Double", "number", 2.0),
    "string": ("String", "text", "hello"),
    "token": ("Token", "text", "default"),
    "asset": ("Asset", "file", "./textures/wood.png"),
    "color3f": ("Color3f", "vec3f", (1.0, 0.0, 0.0)),
    "float3": ("Float3", "vec3f", (1.0, 1.0, 1.0)),
    "double3": ("Double3", "vec3d", (1.0, 1.0, 1.0)),
    "vector3f": ("Vector3f", "vec3f", (0.0, 0.0, 1.0)),
    "point3f": ("Point3f", "vec3f", (0.0, 0.0, 0.0)),
}


class _Refuse(ValueError):
    """A value that cannot be put into a snippet safely or sensibly."""


@dataclass(frozen=True)
class _Param:
    name: str
    kind: str
    default: Any = None
    help: str = ""
    choices: tuple = ()
    emit: bool = True  # False: a structural choice that picks a template instead of becoming a variable
    exts: tuple = ()
    minimum: float | None = None


@dataclass(frozen=True)
class _Action:
    name: str
    group: str
    title: str
    summary: str
    params: tuple
    build: Callable[[dict], list]
    changes: str
    concepts: tuple
    exam: tuple = ()
    mode: str = "edit"  # read | edit | export | save | undo
    prepare: Callable[[dict], None] | None = None


# ----------------------------------------------------------------------------------------------- validation


def _text(value: Any, label: str, *, allow_control: bool = False) -> str:
    if not isinstance(value, str):
        raise _Refuse(f"{label} must be a string, got {type(value).__name__}")
    if len(value) > MAX_TEXT:
        raise _Refuse(f"{label} is longer than {MAX_TEXT} characters")
    if not allow_control and _CONTROL_RE.search(value):
        raise _Refuse(f"{label} contains a newline or other control character")
    return value


def _prim_path(value: Any, label: str) -> str:
    text = _text(value, label)
    if Sdf is not None:
        valid = Sdf.Path.IsValidPathString(text)
        path = Sdf.Path(text) if valid else None
        ok = bool(path) and path.IsAbsolutePath() and path.IsPrimPath() and path != Sdf.Path.absoluteRootPath
    else:  # pragma: no cover - pxr missing
        ok = bool(_PRIM_PATH_RE.match(text))
    if not ok:
        raise _Refuse(f"{label} {text!r} is not a valid absolute prim path such as /World/Cube")
    return text


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label)
    ok = Sdf.Path.IsValidIdentifier(text) if Sdf is not None else bool(_IDENT_RE.match(text))
    if not ok:
        raise _Refuse(f"{label} {text!r} must be a USD name: letters, digits and _, not starting with a digit")
    return text


def _attr_name(value: Any, label: str) -> str:
    text = _text(value, label)
    ok = Sdf.Path.IsValidNamespacedIdentifier(text) if Sdf is not None else bool(_NS_IDENT_RE.match(text))
    if not ok:
        raise _Refuse(f"{label} {text!r} must be an attribute name such as size or userProperties:note")
    return text


def _number(value: Any, label: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Refuse(f"{label} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise _Refuse(f"{label} must be a finite number")
    if minimum is not None and number < minimum:
        raise _Refuse(f"{label} must be at least {minimum}")
    return number


def _integer(value: Any, label: str) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Refuse(f"{label} must be a whole number, got {value!r}")
    return value


def _vec3(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _Refuse(f"{label} must be a list of 3 numbers such as [0, 0, 1]")
    return tuple(_number(item, f"{label}[{index}]") for index, item in enumerate(value))  # type: ignore[return-value]


def _file(value: Any, label: str, exts: tuple = ()) -> str:
    text = _text(value, label).strip()
    if not text:
        raise _Refuse(f"{label} must not be empty")
    if exts and not text.lower().endswith(exts):
        raise _Refuse(f"{label} must end with one of {', '.join(exts)}")
    return text


def _choice(value: Any, label: str, choices: tuple) -> str:
    if isinstance(value, str):
        for choice in choices:
            if value.strip().lower() == choice.lower():
                return choice
    raise _Refuse(f"{label} must be one of: {', '.join(choices)}")


def _variant_names(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 20:
        raise _Refuse(f"{label} must be a list of 1 to 20 variant names")
    names = []
    for item in value:
        text = _text(item, label)
        if not _VARIANT_RE.match(text):
            raise _Refuse(f"variant name {text!r} may only use letters, digits, _ and -")
        if text in names:
            raise _Refuse(f"variant name {text!r} is listed twice")
        names.append(text)
    return tuple(names)


def _coerce(param: _Param, value: Any) -> Any:
    kind, label = param.kind, param.name
    if value is None:
        if kind.startswith("optional_") or param.default is None:
            return None
        raise _Refuse(f"{label} cannot be empty")
    base = kind.removeprefix("optional_")
    if base == "prim_path":
        return _prim_path(value, label)
    if base == "identifier":
        return _identifier(value, label)
    if base == "attr_name":
        return _attr_name(value, label)
    if base == "variant":
        text = _text(value, label)
        if not _VARIANT_RE.match(text):
            raise _Refuse(f"{label} {text!r} may only use letters, digits, _ and -")
        return text
    if base == "variants":
        return _variant_names(value, label)
    if base == "number":
        return _number(value, label, param.minimum)
    if base == "int":
        return _integer(value, label)
    if base == "bool":
        if not isinstance(value, bool):
            raise _Refuse(f"{label} must be true or false")
        return value
    if base == "vec3":
        return _vec3(value, label)
    if base == "file":
        return _file(value, label, param.exts)
    if base == "choice":
        return _choice(value, label, param.choices)
    if base == "any":  # set_attribute's value: checked against value_type in _prepare_set_attribute
        return value
    raise AssertionError(f"unknown parameter kind {kind}")  # pragma: no cover


# ------------------------------------------------------------------------------------------- line helpers


def _get_prim(var: str = "prim", path_var: str = "prim_path") -> list[Line]:
    return [
        (f"{var} = stage.GetPrimAtPath({path_var})",
         "Looks up the prim at that path. If nothing is there you get an 'invalid' prim, not an error."),
        (f"if not {var}.IsValid():", "Checks that the prim really exists before using it."),
        (f"    raise RuntimeError(f'No prim at {{{path_var}}}')",
         "Stops with a clear message if the path is wrong (paths are case-sensitive)."),
    ]


def _vec_expr(var: str, gf: str = "Gf.Vec3d") -> str:
    return f"{gf}(*{var})"


# ------------------------------------------------------------------------------------------------ builders


def _b_inspect(v: dict) -> list[Line]:
    return _get_prim() + [
        ("print('Path:', prim.GetPath())", "Prints the prim's full path."),
        ("print('Type:', prim.GetTypeName())", "Prints its schema type (Xform, Cube, Mesh...); empty means no type."),
        ("print('Active:', prim.IsActive(), '| Defined:', prim.IsDefined())",
         "Active means not switched off; Defined means some layer says 'def' (not just 'over')."),
        ("print('Applied API schemas:', prim.GetAppliedSchemas())",
         "Lists API schemas stuck onto the prim, such as PhysicsRigidBodyAPI or MaterialBindingAPI."),
        ("print('Children:', [child.GetName() for child in prim.GetChildren()])", "Lists the names of prims directly below it."),
        ("for attr in prim.GetAttributes():", "Loops over every attribute the prim has (authored or from its schema)."),
        ("    source = 'authored' if attr.HasAuthoredValue() else 'fallback'",
         "Notes whether a layer wrote this value or it is just the schema's default (fallback)."),
        ("    print(' ', attr.GetName(), attr.GetTypeName(), '=', attr.Get(), f'({source})')",
         "Prints the attribute's name, value type and current (default-time) value."),
        ("print('Relationships:', [rel.GetName() for rel in prim.GetRelationships()])",
         "Lists relationships, e.g. material:binding pointing at a material."),
        ("print('Has references:', prim.HasAuthoredReferences(), '| Has variant sets:', prim.HasVariantSets())",
         "Shows whether composition arcs (references, variants) bring content into this prim."),
    ]


def _b_children(v: dict) -> list[Line]:
    return _get_prim() + [
        ("children = prim.GetChildren()", "Gets the prims directly under this one (not grandchildren)."),
        ("for child in children:", "Goes through the children one at a time."),
        ("    print(child.GetPath(), child.GetTypeName())", "Prints each child's path and type."),
        ("print('Number of children:', len(children))", "Prints how many children there are."),
    ]


def _b_traverse(v: dict) -> list[Line]:
    return [
        ("count = 0", "Starts a counter for the prims we visit."),
        ("for prim in stage.Traverse():", "Walks every active, loaded prim in the stage, parents before children."),
        ("    print(prim.GetPath(), prim.GetTypeName())", "Prints the path and type of each prim."),
        ("    count += 1", "Adds one to the counter."),
        ("print('Prims visited:', count)", "Prints the total number of prims."),
    ]


def _b_create_prim(v: dict) -> list[Line]:
    cls = v["type_name"]
    return [
        (f"prim = UsdGeom.{cls}.Define(stage, prim_path).GetPrim()",
         f"Creates a {cls} prim at that path (or just sets its type if it already exists). Missing parents are "
         "created too, but without a type."),
        ("print('Created', prim.GetPath(), 'of type', prim.GetTypeName())", "Confirms what was created."),
    ]


def _xform_common(setter: str, expr: str, what: str) -> list[Line]:
    return _get_prim() + [
        ("xform_api = UsdGeom.XformCommonAPI(prim)",
         "Wraps the prim in XformCommonAPI, a helper for the usual translate / rotate / scale stack."),
        (f"if not xform_api.{setter}({expr}):",
         f"Sets the {what}. The helper returns False when the prim uses a stack it cannot edit."),
        ("    raise RuntimeError('This prim uses xform ops XformCommonAPI cannot edit (e.g. Isaac Sim orient). "
         "Use the set_transform_isaac snippet.')",
         "Explains the usual reason: Isaac Sim prims store rotation in xformOp:orient, which this helper refuses."),
        ("print('xformOpOrder:', UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get())",
         "Prints which transform steps the prim now has, in order."),
    ]


def _b_move(v: dict) -> list[Line]:
    return _xform_common("SetTranslate", "Gf.Vec3d(*translate)", "position (in stage units, relative to the parent)")


def _b_rotate(v: dict) -> list[Line]:
    return _xform_common(
        "SetRotate", "Gf.Vec3f(*rotate), UsdGeom.XformCommonAPI.RotationOrderXYZ",
        "rotation in degrees, applied X first, then Y, then Z",
    )


def _b_scale(v: dict) -> list[Line]:
    return _xform_common("SetScale", "Gf.Vec3f(*scale)", "scale factor per axis (1 = unchanged)")


def _b_isaac(v: dict) -> list[Line]:
    return _get_prim() + [
        ("xformable = UsdGeom.Xformable(prim)", "Wraps the prim so we can read and add its xformOps directly."),
        ("allowed = {'xformOp:translate', 'xformOp:orient', 'xformOp:scale'}",
         "The three ops Isaac Sim uses: translate, orient (quaternion) and scale."),
        ("other_ops = [op.GetOpName() for op in xformable.GetOrderedXformOps() if op.GetOpName() not in allowed]",
         "Collects any other ops (rotateXYZ, transform, pivots such as xformOp:translate:pivot...) that this snippet "
         "should not mix with."),
        ("if other_ops:", "Checks whether such ops exist."),
        ("    raise RuntimeError(f'Only translate/orient/scale stacks are handled here; found {other_ops}')",
         "Stops instead of creating a confusing mix (use the move / rotate / scale snippets for those prims)."),
        ("ops = {op.GetOpType(): op for op in xformable.GetOrderedXformOps()}",
         "Makes a lookup from op kind to the existing op, so we reuse ops that are already there."),
        ("precision = UsdGeom.XformOp.PrecisionDouble", "New ops will store double-precision numbers, like Isaac Sim's default."),
        ("translate_op = ops.get(UsdGeom.XformOp.TypeTranslate) or xformable.AddTranslateOp(precision)",
         "Uses the existing xformOp:translate, or adds one."),
        ("orient_op = ops.get(UsdGeom.XformOp.TypeOrient) or xformable.AddOrientOp(precision)",
         "Uses the existing xformOp:orient (rotation as a quaternion), or adds one."),
        ("scale_op = ops.get(UsdGeom.XformOp.TypeScale) or xformable.AddScaleOp(precision)",
         "Uses the existing xformOp:scale, or adds one."),
        ("rotation = (Gf.Rotation(Gf.Vec3d(1, 0, 0), rotate[0]) * Gf.Rotation(Gf.Vec3d(0, 1, 0), rotate[1])"
         " * Gf.Rotation(Gf.Vec3d(0, 0, 1), rotate[2]))",
         "Builds the rotation from your degrees: about X, then Y, then Z (the same result as rotateXYZ)."),
        ("quat = rotation.GetQuat()", "Turns the rotation into a quaternion (a double-precision Gf.Quatd)."),
        ("if orient_op.GetPrecision() == UsdGeom.XformOp.PrecisionFloat:",
         "Checks whether the existing orient op stores float (quatf) instead of double (quatd)."),
        ("    quat = Gf.Quatf(quat)", "Converts the quaternion to float precision, because USD refuses a type mismatch."),
        ("translate_op.Set(Gf.Vec3d(*translate))", "Writes the position (stage units, relative to the parent)."),
        ("orient_op.Set(quat)", "Writes the rotation quaternion."),
        ("scale_op.Set(Gf.Vec3d(*scale))", "Writes the scale per axis."),
        ("xformable.SetXformOpOrder([translate_op, orient_op, scale_op])",
         "Makes sure the ops apply in Isaac Sim's order: translate, orient, scale."),
        ("print('xformOpOrder:', xformable.GetXformOpOrderAttr().Get())", "Prints the final list of transform steps."),
    ]


def _b_rename(v: dict) -> list[Line]:
    return _get_prim() + [
        ("editor = Usd.NamespaceEditor(stage)",
         "Creates a namespace editor, which renames prims safely across the layers of the stage."),
        ("if not editor.RenamePrim(prim, new_name):",
         "Asks for the rename; it returns False if the name is taken or the prim cannot be renamed here."),
        ("    raise RuntimeError('USD refused this rename (name taken, or the prim comes from a reference)')",
         "Explains the usual reasons a rename is refused."),
        ("if not editor.ApplyEdits():", "Performs the rename on the layers; returns False if it could not."),
        ("    raise RuntimeError('The rename could not be applied')", "Stops with a message if applying failed."),
        ("print('Renamed', prim_path, 'to', Sdf.Path(prim_path).ReplaceName(new_name))", "Prints the new path."),
    ]


def _b_delete(v: dict) -> list[Line]:
    lines = _get_prim()
    if v["mode"] == "deactivate":
        return lines + [
            ("prim.SetActive(False)",
             "Deactivates the prim: it and its children disappear from the scene, but nothing is deleted."),
            ("print('Active now:', stage.GetPrimAtPath(prim_path).IsActive())",
             "Confirms it is inactive; prim.SetActive(True) brings it back."),
        ]
    return lines + [
        ("if not stage.RemovePrim(prim_path):",
         "Removes the prim's spec from the current edit target layer (only that layer)."),
        ("    raise RuntimeError('RemovePrim failed')", "Stops with a message if nothing could be removed."),
        ("print('Still on the stage:', stage.GetPrimAtPath(prim_path).IsValid())",
         "True would mean another layer or a reference still defines it; deactivating is the safer way to hide those."),
    ]


def _b_duplicate(v: dict) -> list[Line]:
    return [
        ("layer = stage.GetEditTarget().GetLayer()", "Gets the layer that currently receives edits (normally the root layer)."),
        ("if not layer.GetPrimAtPath(source_path):",
         "Checks that this layer has data (a spec) for the source prim; only that data can be copied."),
        ("    raise RuntimeError(f'{source_path} has no spec in the edit target layer, so there is nothing to copy here')",
         "Stops if the prim only comes from another layer or a reference."),
        ("if stage.GetPrimAtPath(target_path).IsValid():", "Checks that the new path is still free."),
        ("    raise RuntimeError(f'{target_path} already exists')", "Stops instead of overwriting another prim."),
        ("parent_path = Sdf.Path(target_path).GetParentPath()", "Works out the parent of the new prim."),
        ("if parent_path != Sdf.Path.absoluteRootPath:", "Skips the next step when the copy goes directly under the root."),
        ("    Sdf.CreatePrimInLayer(layer, parent_path)", "Makes sure the parent exists in this layer so the copy has a home."),
        ("if not Sdf.CopySpec(layer, source_path, layer, target_path):",
         "Copies the prim, its attributes and its children inside the layer."),
        ("    raise RuntimeError('Sdf.CopySpec failed')", "Stops with a message if the copy failed."),
        ("print('Copied', source_path, 'to', target_path, '| type:', stage.GetPrimAtPath(target_path).GetTypeName())",
         "Confirms the copy and its type."),
    ]


def _b_set_attribute(v: dict) -> list[Line]:
    member, kind, _ = VALUE_TYPES[v["value_type"]]
    if kind == "vec3f":
        expr, why = "Gf.Vec3f(*value)", "a Gf.Vec3f (three floats)"
    elif kind == "vec3d":
        expr, why = "Gf.Vec3d(*value)", "a Gf.Vec3d (three doubles)"
    elif kind == "file":
        expr, why = "Sdf.AssetPath(value)", "an Sdf.AssetPath (a file reference USD can resolve)"
    else:
        expr, why = "value", f"a plain Python {type(v['value']).__name__}"
    return _get_prim() + [
        ("attr = prim.GetAttribute(attr_name)", "Looks for the attribute; schema attributes like size exist even if never written."),
        ("if not attr:", "If the attribute does not exist yet..."),
        (f"    attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.{member})",
         f"...creates it with the value type {v['value_type']} (Sdf.ValueTypeNames.{member})."),
        (f"elif attr.GetTypeName() != Sdf.ValueTypeNames.{member}:",
         "If it exists with a different value type (e.g. token vs string, float vs double)..."),
        ("    raise RuntimeError(f'{attr_name} already has type {attr.GetTypeName()}')",
         "...stops, because USD will not store a value of the wrong type."),
        (f"if not attr.Set({expr}):", f"Writes the value as {why} at the default (non-animated) time."),
        ("    raise RuntimeError('USD refused the value')", "Stops if USD rejected it."),
        ("print(attr_name, '=', attr.Get())", "Reads the value back to confirm it."),
    ]


def _b_create_light(v: dict) -> list[Line]:
    cls = LIGHT_TYPES[v["light_type"]]
    lines: list[Line] = [
        (f"light = UsdLux.{cls}.Define(stage, prim_path)", f"Creates a {cls} prim (or reuses one at that path)."),
        ("light.CreateIntensityAttr(intensity)", "Sets inputs:intensity, the brightness."),
        ("light.CreateColorAttr(Gf.Vec3f(*color))", "Sets inputs:color as red, green, blue from 0 to 1."),
    ]
    if v.get("texture_file") is not None:
        lines.append(("light.CreateTextureFileAttr(texture_file)",
                      "Uses an HDR/EXR image as the sky around the scene (inputs:texture:file)."))
    if v.get("translate") is not None:
        lines.append(("UsdGeom.XformCommonAPI(light.GetPrim()).SetTranslate(Gf.Vec3d(*translate))",
                      "Moves the light to that position."))
    if v["light_type"] in ("rect", "disk", "distant"):
        lines.append(("print('Note: this light shines along its local -Z axis; rotate it to aim.')",
                      "Reminds you which way the light points before you rotate it."))
    lines.append(("print('Created', light.GetPath(), 'intensity', light.GetIntensityAttr().Get())",
                  "Confirms the light and its brightness."))
    return lines


def _b_create_camera(v: dict) -> list[Line]:
    return [
        ("camera = UsdGeom.Camera.Define(stage, prim_path)", "Creates a camera prim (or reuses one at that path)."),
        ("camera.CreateFocalLengthAttr(focal_length)", "Sets the lens focal length in millimetres (bigger = more zoom)."),
        ("up = Gf.Vec3d(0, 0, 1) if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z else Gf.Vec3d(0, 1, 0)",
         "Picks the world 'up' direction from the stage's up axis (Z in Isaac Sim, Y in many other apps)."),
        ("eye = Gf.Vec3d(*position)", "Where the camera stands, in world coordinates."),
        ("look_at = Gf.Vec3d(*target)", "The point the camera should look at."),
        ("if Gf.Cross(look_at - eye, up).GetLength() < 1e-9:",
         "Checks the camera is not looking exactly along the up axis (then 'up' would be ambiguous)."),
        ("    raise RuntimeError('The camera looks straight up or down; move it a little sideways')",
         "Stops with advice instead of producing a broken transform."),
        ("camera_to_world = Gf.Matrix4d().SetLookAt(eye, look_at, up).GetInverse()",
         "SetLookAt builds a world-to-camera (view) matrix; the inverse places the camera in the world."),
        ("parent_to_world = UsdGeom.XformCache().GetParentToWorldTransform(camera.GetPrim())",
         "Gets the parent's world transform, so a moved parent does not throw the aim off."),
        ("local = camera_to_world * parent_to_world.GetInverse()",
         "Converts the world placement into a transform relative to the parent."),
        ("UsdGeom.Xformable(camera).MakeMatrixXform().Set(local)",
         "Replaces the camera's xformOps with one matrix op (xformOp:transform) holding that placement."),
        ("print('Camera', camera.GetPath(), 'looks from', eye, 'at', look_at)", "Confirms where the camera is aimed."),
    ]


def _material_lines() -> list[Line]:
    return [
        ("material = UsdShade.Material.Define(stage, material_path)", "Creates the Material prim that holds the shader."),
        ("shader = UsdShade.Shader.Define(stage, Sdf.Path(material_path).AppendChild('PreviewSurface'))",
         "Creates a Shader prim inside the material, at <material_path>/PreviewSurface."),
        ("shader.CreateIdAttr('UsdPreviewSurface')", "Says which shader this is: USD's portable UsdPreviewSurface."),
        ("shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color))",
         "Sets the base colour (red, green, blue from 0 to 1)."),
        ("shader.CreateInput('roughness', Sdf.ValueTypeNames.Float).Set(roughness)",
         "Sets roughness: 0 is mirror-shiny, 1 is fully matte."),
        ("shader.CreateInput('metallic', Sdf.ValueTypeNames.Float).Set(metallic)", "Sets metallic: 0 plastic-like, 1 metal."),
        ("material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')",
         "Connects the shader's surface output to the material's surface output, so renderers use it."),
    ]


def _b_create_material(v: dict) -> list[Line]:
    lines: list[Line] = []
    if v.get("bind_to") is not None:
        lines += _get_prim("target", "bind_to")
    lines += _material_lines()
    if v.get("bind_to") is not None:
        lines += [
            ("binding = UsdShade.MaterialBindingAPI.Apply(target)",
             "Applies MaterialBindingAPI to the target prim, the schema that owns material:binding."),
            ("binding.Bind(material)", "Writes the material:binding relationship pointing at the new material."),
            ("print('Bound', binding.ComputeBoundMaterial()[0].GetPath(), 'to', target.GetPath())",
             "Asks USD which material actually wins for the target, and prints it."),
        ]
    else:
        lines.append(("print('Created material', material.GetPath())", "Confirms the material was created."))
    return lines


def _b_bind_material(v: dict) -> list[Line]:
    strength = v["strength"]
    bind = "binding.Bind(material)"
    why = "Writes material:binding on the prim, pointing at the material (weaker than descendants' own bindings)."
    if strength == "strongerThanDescendants":
        bind = "binding.Bind(material, UsdShade.Tokens.strongerThanDescendants)"
        why = "Writes material:binding with strength strongerThanDescendants, so it overrides bindings on children."
    return _get_prim() + [
        ("material = UsdShade.Material(stage.GetPrimAtPath(material_path))", "Wraps the prim at material_path as a Material."),
        ("if not material:", "Checks it really is a Material prim."),
        ("    raise RuntimeError(f'No material at {material_path}')", "Stops if the path is wrong or not a material."),
        ("binding = UsdShade.MaterialBindingAPI.Apply(prim)", "Applies MaterialBindingAPI to the prim."),
        (bind, why),
        ("print('Bound material:', binding.ComputeBoundMaterial()[0].GetPath())",
         "Asks USD which material wins for this prim after all bindings are considered."),
    ]


def _arc_lines(v: dict, kind: str) -> list[Line]:
    getter, adder, cls = ("GetReferences", "AddReference", "Reference") if kind == "reference" else (
        "GetPayloads", "AddPayload", "Payload")
    lines: list[Line] = [
        ("prim = stage.GetPrimAtPath(prim_path)", "Looks for an existing prim to hold the arc."),
        ("if not prim.IsValid():", "If there is none yet..."),
        ("    prim = UsdGeom.Xform.Define(stage, prim_path).GetPrim()", "...creates an Xform there so the asset can be moved."),
    ]
    offset = v.get("time_offset") or 0.0
    target = v.get("ref_prim_path")
    if target is None and not offset:
        lines.append((f"prim.{getter}().{adder}(asset_path)",
                      f"Adds a {kind} to the file's defaultPrim. Relative paths are relative to the layer being edited."))
    else:
        args = ["asset_path"]
        if target is not None:
            args.append("ref_prim_path")
        if offset:
            args.append("layerOffset=Sdf.LayerOffset(time_offset)")
        what = "the named prim in the file" if target is not None else "the file's defaultPrim"
        lines.append((f"arc = Sdf.{cls}({', '.join(args)})",
                      f"Describes the {kind}: which file, {what}"
                      + (", shifted later in time by time_offset frames" if offset else "") + "."))
        lines.append((f"prim.{getter}().{adder}(arc)", f"Adds that {kind} to the prim."))
    lines += [
        ("children = [child.GetName() for child in prim.GetChildren()]", "Lists what came in under the prim."),
        ("print('Children now:', children)", "Prints them so you can see the asset arrived."),
        ("if not children:", "If nothing came in..."),
        ("    print('Nothing came in: check the file path and that the file has a defaultPrim (or give ref_prim_path).')",
         "...prints the two most common causes."),
    ]
    return lines


def _b_add_reference(v: dict) -> list[Line]:
    return _arc_lines(v, "reference")


def _b_add_payload(v: dict) -> list[Line]:
    return _arc_lines(v, "payload")


def _b_variant_set(v: dict) -> list[Line]:
    return _get_prim() + [
        ("variant_set = prim.GetVariantSets().AddVariantSet(set_name)", "Adds a variant set (a named switch) to the prim."),
        ("palette = [(0.8, 0.1, 0.1), (0.1, 0.6, 0.2), (0.1, 0.3, 0.8), (0.9, 0.7, 0.1)]",
         "A few colours (red, green, blue, yellow) to give each variant something different."),
        ("for index, name in enumerate(variants):", "Goes through your variant names, counting 0, 1, 2..."),
        ("    variant_set.AddVariant(name)", "Adds the variant to the set."),
        ("    variant_set.SetVariantSelection(name)", "Selects it, so the next edits go inside this variant."),
        ("    with variant_set.GetVariantEditContext():", "Temporarily sends edits into the selected variant."),
        ("        color = Gf.Vec3f(*palette[index % len(palette)])", "Picks this variant's colour from the palette."),
        ("        prim.CreateAttribute('primvars:displayColor', Sdf.ValueTypeNames.Color3fArray).Set([color])",
         "Stores the colour inside the variant. A colour authored directly on the prim (local) would win over it: LIVERPS."),
        ("variant_set.SetVariantSelection(selection)", "Chooses which variant is shown."),
        ("print(set_name, 'variants:', variant_set.GetVariantNames(), '| selected:', variant_set.GetVariantSelection())",
         "Prints the variants and the current selection."),
    ]


def _b_select_variant(v: dict) -> list[Line]:
    return _get_prim() + [
        ("if not prim.GetVariantSets().HasVariantSet(set_name):", "Checks that the prim has a variant set with that name."),
        ("    raise RuntimeError(f'{prim_path} has no variant set {set_name!r}; it has {prim.GetVariantSets().GetNames()}')",
         "Stops and lists the variant sets it does have."),
        ("variant_set = prim.GetVariantSets().GetVariantSet(set_name)", "Gets the variant set."),
        ("if selection not in variant_set.GetVariantNames():", "Checks that the variant you asked for exists."),
        ("    raise RuntimeError(f'{selection!r} is not one of {variant_set.GetVariantNames()}')",
         "Stops and lists the valid variant names."),
        ("variant_set.SetVariantSelection(selection)",
         "Writes the selection in the edit target layer; the stage recomposes with that variant."),
        ("print(set_name, '=', variant_set.GetVariantSelection())", "Confirms the selection."),
    ]


def _layer_file_lines() -> list[Line]:
    return [
        ("root_layer = stage.GetRootLayer()", "Gets the root layer, the file the stage was opened from."),
        ("if root_layer.anonymous and not os.path.isabs(layer_path):",
         "An untitled scene has no folder yet, so a relative layer path would have nothing to be relative to..."),
        ("    raise RuntimeError('Save the scene first, or give layer_path as a full path')",
         "...so it stops instead of writing the file into some unexpected folder."),
        ("layer_file = os.path.join(os.path.dirname(root_layer.realPath), layer_path)",
         "Builds the full file path next to the root layer (a full layer_path is kept as it is)."),
    ]


def _b_sublayer(v: dict) -> list[Line]:
    insert = "root_layer.subLayerPaths.append(layer_path)"
    why = "Adds it at the end of subLayerPaths: the weakest sublayer (stronger layers come first)."
    if v["position"] == "strongest":
        insert = "root_layer.subLayerPaths.insert(0, layer_path)"
        why = "Adds it at the start of subLayerPaths: the strongest sublayer (beaten only by the root layer itself)."
    return _layer_file_lines() + [
        ("sub_layer = Sdf.Layer.FindOrOpen(layer_file)", "Opens the layer file if it exists (or finds it if already open)."),
        ("if sub_layer is None:", "If the file does not exist yet..."),
        ("    sub_layer = Sdf.Layer.CreateNew(layer_file)", "...creates a new, empty layer file."),
        ("if layer_path in root_layer.subLayerPaths:", "Checks whether it is already a sublayer."),
        ("    print('Already a sublayer:', layer_path)", "Says so instead of adding it twice."),
        ("else:", "Otherwise..."),
        ("    " + insert, why),
        ("print('Sublayers (strongest first):', list(root_layer.subLayerPaths))", "Prints the sublayer list."),
    ]


def _b_edit_target(v: dict) -> list[Line]:
    return _layer_file_lines() + [
        ("layer = Sdf.Layer.FindOrOpen(layer_file)", "Finds the layer (it is already open if the stage uses it)."),
        ("if layer is None or not stage.HasLocalLayer(layer):", "Checks the layer is part of this stage's own layer stack."),
        ("    raise RuntimeError('That layer is not in this stage. Add it as a sublayer first.')",
         "Stops: you can only target layers the stage already uses."),
        ("stage.SetEditTarget(Usd.EditTarget(layer))",
         "From now on, every edit (from this session) is written into that layer instead of the root layer."),
        ("print('Edits now go to', stage.GetEditTarget().GetLayer().identifier)", "Confirms the new edit target."),
    ]


def _b_physics_scene(v: dict) -> list[Line]:
    return [
        ("scene = UsdPhysics.Scene.Define(stage, prim_path)", "Creates the PhysicsScene prim that physics needs."),
        ("down = Gf.Vec3f(0, 0, -1) if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z else Gf.Vec3f(0, -1, 0)",
         "Gravity points opposite to the stage's up axis."),
        ("scene.CreateGravityDirectionAttr(down)", "Stores the gravity direction."),
        ("meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)", "Reads how many metres one stage unit is."),
        ("scene.CreateGravityMagnitudeAttr(gravity / meters_per_unit)",
         "Stores gravity in stage units per second squared (9.81 m/s^2 becomes 981 in a centimetre stage)."),
        ("print('Physics scene', scene.GetPath(), 'gravity', scene.GetGravityMagnitudeAttr().Get(), 'along', down)",
         "Confirms the gravity settings."),
    ]


def _b_rigid_body(v: dict) -> list[Line]:
    lines = _get_prim() + [
        ("UsdPhysics.RigidBodyAPI.Apply(prim)", "Makes the prim a rigid body: it moves as one solid object in the simulation."),
    ]
    if v["add_collider"]:
        lines.append(("UsdPhysics.CollisionAPI.Apply(prim)",
                      "Makes it collide. On an Xform with no geometry this does nothing; put colliders on its meshes."))
    if v.get("mass") is not None:
        lines.append(("UsdPhysics.MassAPI.Apply(prim).CreateMassAttr(mass)",
                      "Sets the mass in stage mass units (kilograms when kilogramsPerUnit is 1)."))
    lines.append(("print('Applied API schemas:', prim.GetAppliedSchemas())", "Lists the physics schemas now on the prim."))
    lines.append(("print('Remember: the stage needs a PhysicsScene prim before you press Play.')",
                  "Reminds you that without a physics scene nothing falls."))
    return lines


def _b_collider(v: dict) -> list[Line]:
    return _get_prim() + [
        ("UsdPhysics.CollisionAPI.Apply(prim)", "Makes the prim a collider (it takes part in collisions but does not move by itself)."),
        ("if prim.IsA(UsdGeom.Mesh):", "Checks whether the prim is a mesh..."),
        ("    UsdPhysics.MeshCollisionAPI.Apply(prim).CreateApproximationAttr(approximation)",
         "...and if so, tells physics which simpler shape to use for its collisions."),
        ("print('Applied API schemas:', prim.GetAppliedSchemas())", "Lists the physics schemas now on the prim."),
    ]


def _b_save(v: dict) -> list[Line]:
    return [
        ("root_layer = stage.GetRootLayer()", "Gets the root layer (the main file of the scene)."),
        ("if root_layer.anonymous:", "Checks whether the scene was never saved to a file (an 'untitled' stage)."),
        ("    raise RuntimeError('This scene has no file yet: use File > Save As first, or the export snippet.')",
         "Stops, because USD cannot guess a file name."),
        ("stage.Save()", "Writes every changed layer of the stage back to its file on disk."),
        ("print('Saved', root_layer.realPath)", "Prints which file was written."),
    ]


def _b_export(v: dict) -> list[Line]:
    # A bare relative name would land in the process's current folder (in Kit: the app's install folder).
    where: list[Line] = [
        ("root_layer = stage.GetRootLayer()", "Gets the root layer, the file the stage was opened from."),
        ("if root_layer.anonymous and not os.path.isabs(export_path):",
         "An untitled scene has no folder yet, so a relative export path would have nothing to be relative to..."),
        ("    raise RuntimeError('Save the scene first, or give export_path as a full path')",
         "...so it stops instead of writing the file into some unexpected folder."),
        ("export_file = os.path.join(os.path.dirname(root_layer.realPath), export_path)",
         "Builds the full file path next to the scene file (a full export_path is kept as it is)."),
    ]
    if v["flatten"]:
        return where + [
            ("stage.Export(export_file)",
             "Flattens the whole composed stage (all layers, references, variants) into ONE layer and writes it."),
            ("print('Exported a flattened copy to', export_file)",
             "Confirms the file. The extension picks the format: .usda text, .usdc binary."),
        ]
    return where + [
        ("root_layer.Export(export_file)",
         "Writes just the root layer as it is (its sublayers and references stay as links, not copied in)."),
        ("print('Exported the root layer to', export_file)", "Confirms the file. The extension picks .usda text or .usdc binary."),
    ]


def _b_undo(v: dict) -> list[Line]:
    # Kit's CreatePrim command re-types a prim that already exists and its undo DELETES that path, so the demo
    # must only ever run on a free path.
    guard: list[Line] = [
        ("if stage.GetPrimAtPath(prim_path).IsValid():",
         "Checks that nothing exists at the demo path yet: the demo creates a prim there and then removes it again."),
        ("    raise RuntimeError(f'{prim_path} already exists; pick a free demo path so none of your prims is touched')",
         "Stops before touching one of your own prims."),
    ]
    if v["_context"] == "script_editor":
        return guard + [
            ("import omni.kit.commands", "Loads Kit's command system; commands run through it can be undone with Ctrl+Z."),
            ("import omni.kit.undo", "Loads Kit's undo helpers."),
            ("omni.kit.commands.execute('CreatePrim', prim_path=prim_path, prim_type='Cube')",
             "Creates a cube through a Kit command, so it lands on the undo stack."),
            ("print('After the command:', stage.GetPrimAtPath(prim_path).IsValid())", "Shows the cube exists now."),
            ("omni.kit.undo.undo()", "Undoes the last command, the same as pressing Ctrl+Z."),
            ("print('After undo:', stage.GetPrimAtPath(prim_path).IsValid())", "Shows the cube is gone again."),
            ("print('Plain pxr edits (the other snippets) are NOT on Ctrl+Z: save first, or use File > Revert.')",
             "Reminds you how the other snippets differ."),
        ]
    return guard + [
        ("layer = stage.GetEditTarget().GetLayer()", "Gets the layer our edit will go into."),
        ("snapshot = layer.ExportToString()", "Takes a text snapshot of the layer: plain USD has no undo stack, so we keep a copy."),
        ("UsdGeom.Cube.Define(stage, prim_path)", "Makes an edit to undo: creates a cube."),
        ("print('After the edit:', stage.GetPrimAtPath(prim_path).IsValid())", "Shows the cube exists."),
        ("layer.ImportFromString(snapshot)", "Puts the layer back exactly as it was in the snapshot: our 'undo'."),
        ("print('After undo:', stage.GetPrimAtPath(prim_path).IsValid())", "Shows the cube is gone again."),
        ("print('Tip: layer.Reload() also throws away every unsaved change by re-reading the file.')",
         "Mentions the other easy way to discard edits."),
    ]


def _b_time_samples(v: dict) -> list[Line]:
    return _get_prim() + [
        ("stage.SetStartTimeCode(start_frame)", "Sets the first frame of the stage's timeline."),
        ("stage.SetEndTimeCode(end_frame)", "Sets the last frame of the timeline."),
        ("xform_api = UsdGeom.XformCommonAPI(prim)", "Wraps the prim in the common translate / rotate / scale helper."),
        ("if not xform_api.SetTranslate(Gf.Vec3d(*start_position), Usd.TimeCode(start_frame)):",
         "Writes a time sample (keyframe) for the position at the first frame."),
        ("    raise RuntimeError('This prim uses an xform stack XformCommonAPI cannot edit (e.g. Isaac orient).')",
         "Stops if the prim's transform stack is not the common one."),
        ("xform_api.SetTranslate(Gf.Vec3d(*end_position), Usd.TimeCode(end_frame))",
         "Writes a second time sample at the last frame; USD interpolates the frames in between."),
        ("translate_attr = prim.GetAttribute('xformOp:translate')", "Gets the attribute that now holds the animation."),
        ("print('Time samples at frames:', translate_attr.GetTimeSamples())", "Lists the frames that have keyframes."),
        ("middle = Usd.TimeCode((start_frame + end_frame) / 2)", "Picks the frame halfway through."),
        ("print('Position halfway:', translate_attr.Get(middle))",
         "Reads the interpolated value there. Time samples win over the plain default value."),
    ]


# ------------------------------------------------------------------------------------------- preparation


def _prepare_light(v: dict) -> None:
    if v.get("prim_path") is None:
        v["prim_path"] = "/World/" + LIGHT_TYPES[v["light_type"]]
    if v.get("intensity") is None:
        v["intensity"] = LIGHT_INTENSITY[v["light_type"]]
    if v.get("texture_file") is not None and v["light_type"] != "dome":
        raise _Refuse("texture_file only applies to a dome light (light_type 'dome')")


def _prepare_set_attribute(v: dict) -> None:
    _, kind, default = VALUE_TYPES[v["value_type"]]
    value = v.get("value")
    if value is None:
        v["value"] = default
        return
    if kind == "bool":
        if not isinstance(value, bool):
            raise _Refuse("value must be true or false for value_type bool")
    elif kind == "int":
        v["value"] = _integer(value, "value")
    elif kind == "number":
        v["value"] = _number(value, "value")
    elif kind == "text":
        v["value"] = _text(value, "value", allow_control=True)  # repr() keeps quotes and newlines harmless
    elif kind == "file":
        v["value"] = _file(value, "value")
    else:
        v["value"] = _vec3(value, "value")


def _prepare_camera(v: dict) -> None:
    if all(abs(a - b) < 1e-9 for a, b in zip(v["position"], v["target"])):
        raise _Refuse("position and target must be different points")


def _prepare_duplicate(v: dict) -> None:
    source, target = v["source_path"], v["target_path"]
    if target == source or target.startswith(source + "/"):
        raise _Refuse("target_path must not be the source prim or inside it")


def _prepare_variants(v: dict) -> None:
    if v.get("selection") is None:
        v["selection"] = v["variants"][0]
    if v["selection"] not in v["variants"]:
        raise _Refuse(f"selection {v['selection']!r} must be one of the variants {list(v['variants'])}")


def _prepare_time(v: dict) -> None:
    if v["end_frame"] <= v["start_frame"]:
        raise _Refuse("end_frame must be after start_frame")


# ----------------------------------------------------------------------------------------------- actions

P = _Param
_PRIM = P("prim_path", "prim_path", "/World/Cube", "the path of the prim to work on")
_LAYER_FILE = P("layer_path", "file", "lighting.usda", "the layer file (relative paths sit next to the root layer)",
                exts=USD_FILE_EXTS)

ACTIONS: dict[str, _Action] = {}


def _add(*actions: _Action) -> None:
    for action in actions:
        ACTIONS[action.name] = action


_add(
    _Action("inspect_prim", "Look around", "Inspect a prim", "Print a prim's type, schemas, attributes and arcs.",
            (P("prim_path", "prim_path", "/World", "the path of the prim to inspect"),), _b_inspect,
            "Nothing: it only reads.",
            ("prim", "prim path", "prim type (schema)", "API schema", "attribute", "authored vs fallback value", "relationship"),
            ("5.4",), mode="read"),
    _Action("list_children", "Look around", "List children", "Print the prims directly under a prim.",
            (P("prim_path", "prim_path", "/World", "the path of the parent prim"),), _b_children,
            "Nothing: it only reads.", ("prim", "children", "prim path"), ("5.4",), mode="read"),
    _Action("traverse", "Look around", "Walk the whole stage", "Print every prim in the stage.", (), _b_traverse,
            "Nothing: it only reads.", ("stage", "traverse", "prim"), ("5.4",), mode="read"),
    _Action("create_prim", "Create and edit prims", "Create a prim",
            "Define a new Xform, Scope, Cube, Sphere, Cylinder, Cone, Capsule, Plane or Mesh.",
            (P("prim_path", "prim_path", "/World/MyCube", "where the new prim goes"),
             P("type_name", "choice", "Cube", "the prim type", PRIM_TYPES, emit=False)), _b_create_prim,
            "Adds one new prim (and any missing parents).", ("Define", "prim type (schema)", "prim path"), ("7.2",)),
    _Action("rename", "Create and edit prims", "Rename a prim", "Rename a prim with Usd.NamespaceEditor.",
            (_PRIM, P("new_name", "identifier", "Box", "the new name (letters, digits, _)")), _b_rename,
            "Renames the prim in the layers of the stage's layer stack.", ("namespace edit", "prim path", "spec"), ("1.1",)),
    _Action("delete", "Create and edit prims", "Delete or deactivate a prim",
            "Remove a prim from the edit target layer, or deactivate it non-destructively.",
            (_PRIM, P("mode", "choice", "remove", "remove the spec, or deactivate", ("remove", "deactivate"), emit=False)),
            _b_delete, "Removes the prim's spec from the edit target layer (or sets active = false).",
            ("spec", "edit target", "active / deactivate", "LIVERPS"), ("1.1", "1.8")),
    _Action("duplicate", "Create and edit prims", "Duplicate a prim", "Copy a prim's spec inside a layer with Sdf.CopySpec.",
            (P("source_path", "prim_path", "/World/Cube", "the prim to copy"),
             P("target_path", "prim_path", "/World/Cube_copy", "the path for the copy")), _b_duplicate,
            "Adds a copy of the prim (only the data in the edit target layer).", ("spec", "layer", "edit target"), ("1.1",),
            prepare=_prepare_duplicate),
    _Action("move", "Transforms", "Move (translate) a prim", "Set a prim's position with XformCommonAPI.",
            (_PRIM, P("translate", "vec3", (0.0, 0.0, 1.0), "the position [x, y, z] in stage units")), _b_move,
            "Sets the prim's xformOp:translate.", ("xformOp", "xformOpOrder", "XformCommonAPI", "local vs world transform"),
            ("5.5",)),
    _Action("rotate", "Transforms", "Rotate a prim", "Set a prim's rotation (degrees, XYZ) with XformCommonAPI.",
            (_PRIM, P("rotate", "vec3", (0.0, 0.0, 45.0), "the rotation [x, y, z] in degrees")), _b_rotate,
            "Sets the prim's xformOp:rotateXYZ.", ("xformOp", "xformOpOrder", "XformCommonAPI"), ("5.5",)),
    _Action("scale", "Transforms", "Scale a prim", "Set a prim's scale with XformCommonAPI.",
            (_PRIM, P("scale", "vec3", (2.0, 2.0, 2.0), "the scale factor [x, y, z]")), _b_scale,
            "Sets the prim's xformOp:scale.", ("xformOp", "xformOpOrder", "XformCommonAPI"), ("5.5",)),
    _Action("set_transform_isaac", "Transforms", "Move / rotate / scale an Isaac Sim prim",
            "Edit translate, orient (quaternion) and scale ops, the stack Isaac Sim uses (XformCommonAPI refuses it).",
            (_PRIM, P("translate", "vec3", (0.0, 0.0, 0.5), "the position [x, y, z] in stage units"),
             P("rotate", "vec3", (0.0, 0.0, 90.0), "the rotation [x, y, z] in degrees, turned into a quaternion"),
             P("scale", "vec3", (1.0, 1.0, 1.0), "the scale factor [x, y, z]")), _b_isaac,
            "Sets xformOp:translate, xformOp:orient and xformOp:scale and their order.",
            ("xformOp", "xformOpOrder", "orient (quaternion)", "XformCommonAPI"), ("5.5",)),
    _Action("set_attribute", "Attributes and animation", "Set a typed attribute",
            "Create or set an attribute with an explicit value type.",
            (_PRIM, P("attr_name", "attr_name", "size", "the attribute name"),
             P("value_type", "choice", "double", "the USD value type", tuple(VALUE_TYPES), emit=False),
             P("value", "any", None, "the value to write")), _b_set_attribute,
            "Writes one attribute value (creating the attribute if needed).",
            ("attribute", "value type", "authored vs fallback value"), ("5.2", "5.4"), prepare=_prepare_set_attribute),
    _Action("time_samples", "Attributes and animation", "Animate with time samples",
            "Keyframe a prim's position at two frames and read the interpolated value.",
            (_PRIM, P("start_frame", "number", 1.0, "the first frame"), P("end_frame", "number", 48.0, "the last frame"),
             P("start_position", "vec3", (0.0, 0.0, 0.0), "the position at the first frame"),
             P("end_position", "vec3", (0.0, 0.0, 5.0), "the position at the last frame")), _b_time_samples,
            "Sets the stage's start/end frames and writes two time samples on xformOp:translate.",
            ("time code", "time sample", "xformOp", "XformCommonAPI"), ("1.4", "6.2"), prepare=_prepare_time),
    _Action("create_light", "Lights and cameras", "Create a light",
            "Create a rect, sphere, disk, distant, dome or cylinder light with intensity and colour.",
            (P("light_type", "choice", "rect", "the kind of light", tuple(LIGHT_TYPES), emit=False),
             P("prim_path", "optional_prim_path", None, "where the light goes (default /World/<LightType>)"),
             P("intensity", "number", None, "the brightness (inputs:intensity)", minimum=0.0),
             P("color", "vec3", (1.0, 1.0, 1.0), "the light colour [r, g, b] from 0 to 1"),
             P("translate", "optional_vec3", None, "the light position [x, y, z] (optional)"),
             P("texture_file", "optional_file", None, "an HDR/EXR sky image, dome lights only")), _b_create_light,
            "Adds one light prim.", ("UsdLux light", "attribute", "prim path"), (), prepare=_prepare_light),
    _Action("create_camera", "Lights and cameras", "Create and aim a camera",
            "Create a camera at a position and aim it at a target point.",
            (P("prim_path", "prim_path", "/World/MyCamera", "where the camera goes"),
             P("position", "vec3", (6.0, -6.0, 4.0), "where the camera stands [x, y, z] (world)"),
             P("target", "vec3", (0.0, 0.0, 0.0), "the point to look at [x, y, z] (world)"),
             P("focal_length", "number", 35.0, "the lens focal length in mm", minimum=0.1)), _b_create_camera,
            "Adds a camera prim with one matrix xformOp.", ("camera", "focal length", "up axis", "local vs world transform"),
            (), prepare=_prepare_camera),
    _Action("create_material", "Materials", "Create a material (and bind it)",
            "Build a UsdPreviewSurface material and optionally bind it to a prim.",
            (P("material_path", "prim_path", "/World/Looks/RedPlastic", "where the material goes"),
             P("color", "vec3", (0.8, 0.1, 0.1), "the diffuse colour [r, g, b] from 0 to 1"),
             P("roughness", "number", 0.4, "0 shiny to 1 matte", minimum=0.0),
             P("metallic", "number", 0.0, "0 plastic to 1 metal", minimum=0.0),
             P("bind_to", "optional_prim_path", "/World/Cube", "the prim to bind it to (null to skip)")),
            _b_create_material, "Adds a Material with a UsdPreviewSurface shader, and binds it if bind_to is set.",
            ("material", "shader", "UsdPreviewSurface", "material binding", "API schema"), ("8.2", "8.3")),
    _Action("bind_material", "Materials", "Bind an existing material", "Bind a material to a prim with MaterialBindingAPI.",
            (_PRIM, P("material_path", "prim_path", "/World/Looks/RedPlastic", "the material to bind"),
             P("strength", "choice", "weakerThanDescendants", "binding strength",
               ("weakerThanDescendants", "strongerThanDescendants"), emit=False)), _b_bind_material,
            "Writes the prim's material:binding relationship.", ("material binding", "API schema", "relationship"), ("8.3",)),
    _Action("add_reference", "Composition", "Add a reference (bring in an asset)",
            "Reference another USD file (its defaultPrim or a named prim), optionally shifted in time.",
            (P("prim_path", "prim_path", "/World/Asset", "the prim that will hold the asset"),
             P("asset_path", "file", "./assets/chair.usd", "the USD file to reference"),
             P("ref_prim_path", "optional_prim_path", None, "a prim inside that file (default: its defaultPrim)"),
             P("time_offset", "number", 0.0, "frames to shift the asset's animation (layer offset)")), _b_add_reference,
            "Adds a reference arc on the prim (creating an Xform there if needed).",
            ("reference", "defaultPrim", "layer offset", "composition arc", "LIVERPS"), ("1.3", "1.4", "1.6", "7.7")),
    _Action("add_payload", "Composition", "Add a payload",
            "Like a reference, but the content can be unloaded to keep big scenes light.",
            (P("prim_path", "prim_path", "/World/Asset", "the prim that will hold the asset"),
             P("asset_path", "file", "./assets/chair.usd", "the USD file to load as a payload"),
             P("ref_prim_path", "optional_prim_path", None, "a prim inside that file (default: its defaultPrim)"),
             P("time_offset", "number", 0.0, "frames to shift the asset's animation (layer offset)")), _b_add_payload,
            "Adds a payload arc on the prim (creating an Xform there if needed).",
            ("payload", "defaultPrim", "composition arc", "LIVERPS"), ("1.3", "6.3")),
    _Action("create_variant_set", "Composition", "Create a variant set",
            "Add a variant set with several variants, each with its own display colour, and select one.",
            (_PRIM, P("set_name", "identifier", "color", "the variant set name"),
             P("variants", "variants", ("red", "green", "blue"), "the variant names"),
             P("selection", "optional_variant", None, "the variant to select (default: the first)")), _b_variant_set,
            "Adds a variant set with variants and a selection to the prim.",
            ("variant set", "variant selection", "LIVERPS", "primvar"), ("1.6", "1.7"), prepare=_prepare_variants),
    _Action("select_variant", "Composition", "Switch a variant", "Change the selection of an existing variant set.",
            (_PRIM, P("set_name", "identifier", "color", "the variant set name"),
             P("selection", "variant", "red", "the variant to select")), _b_select_variant,
            "Writes a variant selection in the edit target layer.", ("variant set", "variant selection", "LIVERPS"),
            ("1.6", "1.7")),
    _Action("add_sublayer", "Composition", "Add a sublayer", "Add a layer file (created if missing) to the root layer's sublayers.",
            (_LAYER_FILE, P("position", "choice", "weakest", "where in the list", ("weakest", "strongest"), emit=False)),
            _b_sublayer, "Adds an entry to the root layer's subLayerPaths; a missing layer file is created on disk "
            "right away (empty), next to the root layer.",
            ("sublayer", "layer", "root layer", "LIVERPS"), ("1.3", "1.5")),
    _Action("set_edit_target", "Composition", "Send edits to another layer",
            "Make a sublayer the edit target so new edits are written there.", (_LAYER_FILE,), _b_edit_target,
            "Changes where later edits in this session go (the edit target itself is not saved in any file).",
            ("edit target", "layer", "sublayer"), ("1.1", "1.5"), mode="read"),
    _Action("add_physics_scene", "Physics", "Add a physics scene", "Create a PhysicsScene with gravity in the right units.",
            (P("prim_path", "prim_path", "/physicsScene", "where the physics scene goes"),
             P("gravity", "number", 9.81, "gravity strength in m/s^2", minimum=0.0)), _b_physics_scene,
            "Adds a PhysicsScene prim with gravity direction and magnitude.", ("physics scene", "up axis", "metersPerUnit")),
    _Action("make_rigid_body", "Physics", "Make a rigid body", "Apply RigidBodyAPI (plus collider and mass).",
            (_PRIM, P("add_collider", "bool", True, "also apply CollisionAPI", emit=False),
             P("mass", "optional_number", None, "the mass in kg (optional)", minimum=0.0)), _b_rigid_body,
            "Applies PhysicsRigidBodyAPI (and CollisionAPI / MassAPI) to the prim.",
            ("rigid body", "collider", "mass", "API schema")),
    _Action("make_collider", "Physics", "Make a static collider", "Apply CollisionAPI, with a mesh approximation for meshes.",
            (_PRIM, P("approximation", "choice", "convexHull", "the collision shape for meshes", APPROXIMATIONS)),
            _b_collider, "Applies PhysicsCollisionAPI (and MeshCollisionAPI on meshes) to the prim.",
            ("collider", "collision approximation", "API schema")),
    _Action("save", "Save, export and undo", "Save the stage", "Write every changed layer back to disk.", (), _b_save,
            "Writes changed layers to their files on disk.", ("save", "layer", "root layer"), ("4.3",), mode="save"),
    _Action("export", "Save, export and undo", "Export (flatten) to a new file",
            "Write a flattened copy of the stage, or just the root layer, to a new file.",
            (P("export_path", "file", "exported_scene.usda",
               "the new file (.usda text or .usdc binary; relative paths sit next to the scene file)", exts=USD_FILE_EXTS),
             P("flatten", "bool", True, "flatten all layers into one", emit=False)), _b_export,
            "Writes a new file; the stage itself is not changed.", ("export", "flatten", "usda vs usdc"),
            ("1.9", "4.3"), mode="export"),
    _Action("undo_hint", "Save, export and undo", "How undo works",
            "Undo with omni.kit.commands in Kit; snapshot and restore a layer in plain USD.",
            (P("prim_path", "prim_path", "/World/UndoDemo", "a temporary prim used for the demo"),), _b_undo,
            "Creates a demo cube and undoes it again, leaving the scene as it was.", ("undo", "layer"), mode="undo"),
)

ALIASES = {
    "inspect": "inspect_prim", "scene_inspect": "inspect_prim", "children": "list_children",
    "scene_children": "list_children", "list_prims": "traverse", "create": "create_prim", "scene_create": "create_prim",
    "translate": "move", "scene_move": "move", "orient": "set_transform_isaac", "isaac_transform": "set_transform_isaac",
    "scene_rename": "rename", "remove": "delete", "scene_delete": "delete", "deactivate": "delete",
    "copy": "duplicate", "scene_duplicate": "duplicate", "set_attr": "set_attribute",
    "scene_set_attribute": "set_attribute", "animate": "time_samples", "keyframes": "time_samples",
    "light": "create_light", "scene_create_light": "create_light", "camera": "create_camera",
    "scene_create_camera": "create_camera", "aim": "create_camera", "scene_aim": "create_camera",
    "material": "create_material", "scene_create_material": "create_material", "scene_bind_material": "bind_material",
    "scene_material": "bind_material", "reference": "add_reference", "add_asset": "add_reference",
    "scene_add_asset": "add_reference", "payload": "add_payload", "variant": "create_variant_set",
    "variant_set": "create_variant_set", "variant_selection": "select_variant", "scene_variant_set": "select_variant",
    "sublayer": "add_sublayer", "edit_target": "set_edit_target", "physics_scene": "add_physics_scene",
    "scene_add_physics_scene": "add_physics_scene", "rigid_body": "make_rigid_body",
    "scene_make_rigid_body": "make_rigid_body", "collider": "make_collider", "scene_make_collider": "make_collider",
    "scene_save": "save", "flatten": "export", "undo": "undo_hint", "scene_undo_last": "undo_hint",
}
_ARG_ALIASES = {"path": "prim_path"}


# ------------------------------------------------------------------------------------------- public API


@lru_cache(maxsize=1)
def _objectives() -> dict[str, str]:
    path = Path(__file__).resolve().parent / "exam" / "data" / "blueprint.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {obj["id"]: obj.get("text") or obj.get("title") or "" for dom in data["domains"] for obj in dom["objectives"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _objective_text(obj_id: str) -> str:
    return _objectives().get(obj_id, "")


def supported_actions() -> list[str]:
    return list(ACTIONS)


def list_python_topics() -> dict:
    """The supported actions grouped by topic, each with a one-line description and its arguments."""
    groups: dict[str, list[dict]] = {}
    for action in ACTIONS.values():
        groups.setdefault(action.group, []).append({
            "action": action.name,
            "title": action.title,
            "description": action.summary,
            "args": {p.name: _jsonable(p.default) for p in action.params},
        })
    return {
        "ok": True,
        "contexts": dict(WHERE_TO_RUN),
        "groups": [{"group": name, "topics": topics} for name, topics in groups.items()],
        "count": len(ACTIONS),
        "usage": "python_for(action, args, context='script_editor' or 'standalone')",
    }


def _jsonable(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _normalize(action: Any) -> str:
    if not isinstance(action, str):
        return ""
    key = re.sub(r"[\s\-]+", "_", action.strip().lower())
    return ALIASES.get(key, key)


def _fail(action: Any, error: str, **extra: Any) -> dict:
    return {"ok": False, "action": action, "error": error, **extra}


def _settings_lines(spec: _Action, v: dict) -> list[Line]:
    lines = []
    for param in spec.params:
        if not param.emit or v.get(param.name) is None:
            continue
        lines.append((f"{param.name} = {v[param.name]!r}",
                      f"Your setting: {param.help}. Kept in the variable {param.name} so the code below can use it."))
    return lines


def _modules_used(lines: list[Line]) -> list[str]:
    found = {match for text, _ in lines for match in _MODULE_RE.findall(text)}
    return [name for name in _MODULE_ORDER if name in found]


def _preamble(spec: _Action, context: str, body: list[Line], v: dict) -> list[Line]:
    modules = _modules_used(body)
    lines: list[Line] = []
    if context == "script_editor":
        lines.append(("import omni.usd", "Loads Omniverse's USD helper, which knows the scene open in the app."))
    if any(_OS_RE.search(text) for text, _ in body) or (
            context == "standalone" and spec.mode not in ("read", "export", "save")):
        lines.append(("import os", "Loads Python's os module, used here for file paths and checking files exist."))
    if context == "standalone" and "Usd" not in modules:
        modules = [name for name in _MODULE_ORDER if name in set(modules) | {"Usd"}]
    if modules:
        lines.append((f"from pxr import {', '.join(modules)}",
                      "Loads the OpenUSD modules used here: " + ", ".join(MODULE_MEANINGS[m] for m in modules) + "."))
    if context == "script_editor":
        lines += [
            ("stage = omni.usd.get_context().get_stage()",
             "Gets the stage (the whole scene) open in Isaac Sim or Composer right now."),
            ("if stage is None:", "Checks that a scene is actually open."),
            ("    raise RuntimeError('No stage is open: open or create a scene first')", "Stops with a clear message if not."),
        ]
        return lines
    lines.append((f"stage_path = {v['stage_path']!r}",
                  "Your setting: the .usda/.usdc file to work on. Kept in the variable stage_path."))
    if spec.mode in ("read", "export", "save"):
        lines.append(("stage = Usd.Stage.Open(stage_path)", "Opens the file as a stage (it must already exist)."))
    else:
        lines.append(("stage = Usd.Stage.Open(stage_path) if os.path.exists(stage_path) else Usd.Stage.CreateNew(stage_path)",
                      "Opens the file as a stage, or creates a new empty one if the file does not exist yet."))
    return lines


def _safety_note(spec: _Action, context: str) -> str:
    if spec.mode == "read":
        tail = ("It only reads and prints; nothing is written."
                if spec.name != "set_edit_target" else "Nothing is written to any file.")
        return f"{spec.changes} {tail}"
    if spec.mode == "export":
        return f"{spec.changes} An existing file at export_path is overwritten, so pick a new name."
    if spec.mode == "save":
        return f"{spec.changes} Saving cannot be undone with Ctrl+Z; keep a copy if the old version matters."
    if context == "script_editor":
        tail = ("It edits the open scene in memory until saved: the scene file changes only when you save "
                "(File > Save). Plain pxr edits are not on Ctrl+Z; see the undo_hint snippet.")
        if spec.mode == "undo":
            tail = "It edits the open scene in memory until saved, and the demo undoes its own edit."
        return f"{spec.changes} {tail}"
    if spec.mode == "undo":
        return f"{spec.changes} It edits the stage in memory only and does not save the file."
    return (f"{spec.changes} It edits the stage in memory until saved; stage.Save() at the end writes the change "
            "into the file at stage_path. Work on a copy if that file matters.")


def python_for(action: str, args: dict | None = None, *, context: str = "script_editor") -> dict:
    """A short runnable snippet for one task, with a beginner explanation of every non-blank line.

    Returns {ok, action, title, code, lines: [{line, explanation}], concepts: [{term, meaning}], where_to_run,
    safety_note, exam_link: [{id, objective}], args}. Unknown actions or unsafe values give ok False.
    """
    name = _normalize(action)
    if name not in ACTIONS:
        return _fail(action, f"Unknown action {action!r}.", supported_actions=supported_actions())
    if context not in CONTEXTS:
        return _fail(name, f"context must be one of {list(CONTEXTS)}", contexts=dict(WHERE_TO_RUN))
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return _fail(name, "args must be an object (dictionary) of argument names to values")
    spec = ACTIONS[name]
    allowed = {p.name for p in spec.params} | {"stage_path"}
    given: dict[str, Any] = {}
    for key, value in args.items():
        key = _ARG_ALIASES.get(key, key) if isinstance(key, str) and _ARG_ALIASES.get(key) in allowed else key
        if key not in allowed:
            return _fail(name, f"Unknown argument {key!r} for {name}.", accepted_args=sorted(allowed))
        given[key] = value
    try:
        v: dict[str, Any] = {}
        for param in spec.params:
            v[param.name] = _coerce(param, given.get(param.name, param.default))
        v["stage_path"] = _file(given.get("stage_path", DEFAULT_STAGE_PATH), "stage_path", USD_FILE_EXTS)
        if spec.prepare is not None:
            spec.prepare(v)
    except _Refuse as exc:
        return _fail(name, str(exc), accepted_args=sorted(allowed))
    v["_context"] = context

    body = spec.build(v)
    settings = _settings_lines(spec, v)
    preamble = _preamble(spec, context, body, v)
    epilogue: list[Line] = []
    if context == "standalone" and spec.mode == "edit":
        epilogue = [("stage.Save()", "Writes the changes into the file on disk; without this the file stays as it was.")]
    blocks = [block for block in (preamble, settings, body, epilogue) if block]
    rows: list[Line] = []
    for index, block in enumerate(blocks):
        if index:
            rows.append(("", None))
        rows.extend(block)
    for text, explanation in rows:
        if text.strip() and not explanation:  # pragma: no cover - guarded by tests
            raise AssertionError(f"missing explanation for {text!r}")
    code = "\n".join(text for text, _ in rows) + "\n"
    try:
        compile(code, f"<python_for {name}>", "exec")
    except SyntaxError as exc:  # pragma: no cover - guarded by tests
        return _fail(name, f"internal error: generated code does not compile ({exc})")

    public_args = {key: _jsonable(value) for key, value in v.items() if not key.startswith("_")}
    if context == "script_editor":
        public_args.pop("stage_path", None)
    return {
        "ok": True,
        "action": name,
        "title": spec.title,
        "context": context,
        "code": code,
        "lines": [{"line": text, "explanation": explanation} for text, explanation in rows if text.strip()],
        "concepts": [{"term": term, "meaning": CONCEPTS[term]} for term in
                     (("stage",) if "stage" not in spec.concepts else ()) + spec.concepts],
        "where_to_run": WHERE_TO_RUN[context],
        "safety_note": _safety_note(spec, context),
        "exam_link": [{"id": obj_id, "objective": _objective_text(obj_id)} for obj_id in spec.exam],
        "args": public_args,
    }
