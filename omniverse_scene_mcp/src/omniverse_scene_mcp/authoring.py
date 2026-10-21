"""Safe light and camera authoring: dry-run by default, every refusal checked before anything is written.

create_light, create_camera and aim first plan the whole edit (resolved attribute values, xform ops, the aim
target point, the up axis and the edit target layer). Only dry_run=False authors it, in the stage's current
edit target, using plain USD calls. Imports only pxr and the standard library, so the same functions run on a
file stage or on the live stage inside Kit / Isaac Sim (where the edits do not go on Kit's undo stack).

Aiming points the prim's local -Z axis at the target with local +Y kept toward the stage up axis: cameras and
UsdLux rect, disk and distant lights look or emit along -Z.
"""

from __future__ import annotations

import math
import numbers
from typing import Any

from . import look_at
from .usd_insight import to_jsonable

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = UsdLux = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

LIGHT_TYPES = {
    "rect": "RectLight",
    "sphere": "SphereLight",
    "disk": "DiskLight",
    "distant": "DistantLight",
    "cylinder": "CylinderLight",
    "dome": "DomeLight",
}
# Light input -> (UsdLux.Tokens member, schema Create method). Same names in USD 25.11 and 26.x (inputs:*).
_LIGHT_INPUTS = {
    "intensity": ("inputsIntensity", "CreateIntensityAttr"),
    "exposure": ("inputsExposure", "CreateExposureAttr"),
    "color": ("inputsColor", "CreateColorAttr"),
    "width": ("inputsWidth", "CreateWidthAttr"),
    "height": ("inputsHeight", "CreateHeightAttr"),
    "radius": ("inputsRadius", "CreateRadiusAttr"),
    "length": ("inputsLength", "CreateLengthAttr"),
    "angle": ("inputsAngle", "CreateAngleAttr"),
}
# Lens and aperture attributes copied by create_camera(copy_optics_from=...); present in USD 25.11 and 26.x.
CAMERA_OPTICS = (
    "projection",
    "focalLength",
    "horizontalAperture",
    "verticalAperture",
    "horizontalApertureOffset",
    "verticalApertureOffset",
    "clippingRange",
    "clippingPlanes",
    "fStop",
    "focusDistance",
)
TARGET_PURPOSES = ("default", "render")
AIM_TOLERANCE_DEGREES = 1e-3
MAX_PATH_CHARS = 1024
MAX_TEXT_CHARS = 600
MAX_WARNINGS = 40
_SINGULAR_DETERMINANT = 1e-12


class _Refusal(Exception):
    """Raised while planning; becomes an ok=False result and nothing is written."""


def create_light(
    stage: Any,
    path: str,
    light_type: str,
    *,
    intensity: float | None = None,
    exposure: float | None = None,
    color: Any = None,
    size: Any = None,
    translation: Any = None,
    aim_at: Any = None,
    up_axis: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a UsdLux light with translate + rotateXYZ xform ops.

    light_type is rect, sphere, disk, distant, cylinder or dome. size is (width, height) or one number for rect,
    the radius for sphere and disk, (length, radius) for cylinder and the angular diameter in degrees for
    distant; dome ignores size and aim_at. translation is in the parent's space (like xformOp:translate);
    aim_at is a prim path (its world bounding-box center) or a world-space (x, y, z) point.
    """
    action = "create_light"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        kind = _light_type(light_type)
        type_name = LIGHT_TYPES[kind]
        sdf_path, parent = _creation_target(stage, path, warnings)

        inputs: dict[str, Any] = {}
        if intensity is not None:
            inputs["intensity"] = _number(intensity, "intensity", minimum=0.0)
        if exposure is not None:
            inputs["exposure"] = _number(exposure, "exposure")
        if color is not None:
            inputs["color"] = _color(color, warnings)
        inputs.update(_light_size(kind, size, warnings))
        attributes = _light_attribute_names(type_name, inputs)

        if kind == "dome":
            if aim_at is not None:
                warnings.append("aim_at was ignored: a dome light surrounds the whole scene and has no direction to aim.")
                aim_at = None
            if translation is not None:
                warnings.append("translation has no lighting effect on a dome light; only its rotation orients the texture.")
        elif kind == "distant" and translation is not None:
            warnings.append(
                "Distant lights are infinitely far away: only their rotation changes the lighting; "
                "translation just places the gizmo."
            )
        if aim_at is not None and kind == "sphere":
            warnings.append(
                "Sphere lights emit in every direction; aiming only matters once a ShapingAPI cone is applied "
                "(the cone opens along -Z)."
            )
        if aim_at is not None and kind == "cylinder":
            warnings.append(
                "Cylinder lights are tubes along their local X axis that emit outward; aiming points -Z at the "
                "target, so the tube lies across that direction."
            )

        xform = _plan_new_xform(stage, sdf_path, parent, translation, aim_at, up_axis, warnings)
        plan = {
            "path": str(sdf_path),
            "parent": str(parent.GetPath()),
            "type": type_name,
            "light_type": kind,
            "attributes": {name: _json(value) for name, (_, value) in attributes.items()},
            **xform["public"],
        }
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    if dry:
        return _finish(action, True, stage, warnings, key="would_create", plan=plan)

    def author() -> Any:
        schema = getattr(UsdLux, type_name).Define(stage, sdf_path)
        for name, (create_method, value) in attributes.items():
            getattr(schema, create_method)(Gf.Vec3f(*value) if name.endswith("color") else float(value))
        _author_new_xform(schema.GetPrim(), xform["translation"], xform["rotation"])
        return schema.GetPrim()

    return _create(action, stage, sdf_path, plan, xform, list(attributes), author, warnings)


def create_camera(
    stage: Any,
    path: str,
    *,
    focal_length: float | None = None,
    horizontal_aperture: float | None = None,
    clipping_range: Any = None,
    translation: Any = None,
    aim_at: Any = None,
    copy_optics_from: str | None = None,
    up_axis: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a UsdGeom.Camera with translate + rotateXYZ xform ops.

    copy_optics_from names an existing camera whose authored lens values (CAMERA_OPTICS) are copied first;
    explicit focal_length, horizontal_aperture and clipping_range (near, far) then override them.
    """
    action = "create_camera"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        sdf_path, parent = _creation_target(stage, path, warnings)

        values: dict[str, Any] = {}
        copied: list[str] = []
        source_path = None
        if copy_optics_from is not None:
            source_path = _prim_path(copy_optics_from, "copy_optics_from")
            source = stage.GetPrimAtPath(source_path)
            if not source or not source.IsValid():
                raise _Refusal(f"copy_optics_from camera does not exist: {source_path}")
            if not source.IsA(UsdGeom.Camera):
                raise _Refusal(f"copy_optics_from must be a Camera prim, but {source_path} is {source.GetTypeName() or 'typeless'}")
            for name in CAMERA_OPTICS:
                attr = source.GetAttribute(name)
                if not attr or not attr.HasAuthoredValue():
                    continue
                if attr.GetNumTimeSamples() > 0 or attr.ValueMightBeTimeVarying():  # samples, splines or value clips
                    value = attr.Get(Usd.TimeCode.EarliestTime())
                    warnings.append(f"{source_path}.{name} is animated; copied its value at the earliest time.")
                else:
                    value = attr.Get()
                if value is not None:
                    values[name] = value
                    copied.append(name)
            if not copied:
                warnings.append(f"{source_path} has no authored lens values, so the new camera keeps the schema defaults.")

        explicit: dict[str, Any] = {}
        if focal_length is not None:
            explicit["focalLength"] = _number(focal_length, "focal_length", minimum=0.0, exclusive=True)
        if horizontal_aperture is not None:
            explicit["horizontalAperture"] = _number(horizontal_aperture, "horizontal_aperture", minimum=0.0, exclusive=True)
        if clipping_range is not None:
            explicit["clippingRange"] = Gf.Vec2f(*_clipping_range(clipping_range))
        overridden = [name for name in explicit if name in values]
        if overridden:
            warnings.append(f"Explicit values override the copied {', '.join(overridden)}.")
        values.update(explicit)
        _check_schema_names("Camera", values)

        if aim_at is None and _resolve_up_axis(stage, up_axis, []) == "Z":
            warnings.append(
                "On a Z-up stage an unrotated camera looks straight down (-Z); pass aim_at, or rotate it 90 "
                "degrees about X, to look horizontally."
            )
        xform = _plan_new_xform(stage, sdf_path, parent, translation, aim_at, up_axis, warnings)
        plan = {
            "path": str(sdf_path),
            "parent": str(parent.GetPath()),
            "type": "Camera",
            "attributes": {name: _json(value) for name, value in values.items()},
            "copied_optics_from": str(source_path) if source_path is not None else None,
            "copied_attributes": copied,
            **xform["public"],
        }
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    if dry:
        return _finish(action, True, stage, warnings, key="would_create", plan=plan)

    def author() -> Any:
        camera = UsdGeom.Camera.Define(stage, sdf_path)
        prim = camera.GetPrim()
        for name, value in values.items():
            if not prim.GetAttribute(name).Set(value):
                raise ValueError(f"Could not set {name}")
        _author_new_xform(prim, xform["translation"], xform["rotation"])
        return prim

    return _create(action, stage, sdf_path, plan, xform, list(values), author, warnings)


def aim(stage: Any, path: str, target: Any, *, up_axis: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a rotation that points an existing prim's -Z at target.

    Only the rotation changes. It is written in place into the prim's own op stack: XformCommonAPI-compatible
    stacks (rotate op in its own order, no pivot), translate/orient/scale stacks or a single transform matrix.
    Animated ops and other stacks are refused. target is a prim path or a world-space (x, y, z) point.
    """
    action = "aim"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        sdf_path = _prim_path(path, "path")
        prim = stage.GetPrimAtPath(sdf_path)
        if not prim or not prim.IsValid():
            raise _Refusal(f"Prim to aim does not exist: {sdf_path}")
        if prim.IsInstanceProxy() or prim.IsInPrototype():
            raise _Refusal(f"{sdf_path} is inside an instance; edit the instance prim or its prototype source instead.")
        if prim.IsA(UsdLux.DomeLight):
            raise _Refusal(f"{sdf_path} is a dome light: it surrounds the scene and has no direction to aim.")
        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            raise _Refusal(f"{sdf_path} is not transformable (type {prim.GetTypeName() or 'typeless'}), so it cannot be aimed.")
        if not prim.IsActive():
            warnings.append(f"{sdf_path} is inactive, so the new rotation has no visible effect until it is activated.")
        if not (prim.IsA(UsdGeom.Camera) or _is_light(prim)):
            warnings.append(f"{sdf_path} is a {prim.GetTypeName() or 'typeless'} prim; aim turns its local -Z axis toward the target.")

        axis = _resolve_up_axis(stage, up_axis, warnings)
        target_world, target_info = _resolve_target(stage, target, sdf_path, warnings)
        stack = _classify_stack(prim, xformable)
        _warn_animated_ancestors(prim.GetParent(), "Ancestor", warnings)

        cache = UsdGeom.XformCache(Usd.TimeCode.Default())
        world = cache.GetLocalToWorldTransform(prim)
        parent_world = Gf.Matrix4d(1.0) if xformable.GetResetXformStack() else cache.GetParentToWorldTransform(prim)
        local = world * _inverse(parent_world, f"The parent transform of {sdf_path}")
        eye_local = tuple(local.ExtractTranslation())
        eye_world = world.ExtractTranslation()
        look = _look_in_parent_space(eye_local, target_world, parent_world, axis)
        new_local, op_value, rotation_info = _new_local_for_stack(stack, look, local)
        check = _verify_aim(new_local, parent_world, target_world)
        if check["error_degrees"] > AIM_TOLERANCE_DEGREES:
            raise _Refusal(
                f"Aiming {sdf_path} would leave its -Z axis {check['error_degrees']:.4f} degrees off the target: its "
                "own scale or shear (for example a negative scale) flips or skews -Z. Fix the scale first."
            )
        if (check["eye_world"] - eye_world).GetLength() > 1e-6 * max(1.0, eye_world.GetLength()):
            raise _Refusal(f"Aiming {sdf_path} would move it, so the rotation was not applied.")
        # The refusals above judge the exact rotation; report the aim of the value actually written (a half op rounds).
        authored = _verify_aim(_authored_local(stack, op_value, new_local), parent_world, target_world)
        if check["error_degrees"] <= AIM_TOLERANCE_DEGREES < authored["error_degrees"]:
            warnings.append(
                f"{stack['rotation_op']} is half precision, so the written value is rounded and the aim ends up "
                f"{authored['error_degrees']:.4f} degrees off the target; use a float or double op for an exact aim."
            )

        written_names = [stack["rotation_op"]] + (["xformOpOrder"] if stack["adds_op"] else [])
        for name in written_names:
            stronger = _stronger_opinion(stage, prim, name)
            if stronger:
                warnings.append(
                    f"{name} has a stronger opinion in {_text(stronger)} than the edit target, so this edit would be "
                    "hidden by it (within a layer stack the stronger layer wins). Edit that layer instead to see the change."
                )

        old_op = prim.GetAttribute(stack["rotation_op"])
        old_value = old_op.Get() if old_op and old_op.HasAuthoredValue() else None
        order_before = [str(op.GetOpName()) for op in stack["ops"]]
        order_after = order_before if not stack["adds_op"] else _order_with_rotate(stack, order_before)
        plan = {
            "path": str(sdf_path),
            "type": prim.GetTypeName() or "",
            "xform_stack": stack["kind"],
            "xform_op_order_before": order_before,
            "xform_op_order": order_after,
            "changes": [
                {
                    "attribute": stack["rotation_op"],
                    "old": _json(old_value),
                    "new": _json(op_value),
                    "adds_op": stack["adds_op"],
                }
            ],
            "translation": _vec_list(eye_local),
            **rotation_info,
            "aim": {
                **target_info,
                "eye_world": _vec_list(eye_world),
                "distance": _clean((target_world - eye_world).GetLength()),
                "forward_world": authored["forward_world"],
                "error_degrees": _clean(authored["error_degrees"]),
                "fallback_up": look["fallback_up"],
            },
            "up_axis": axis,
        }
        warnings.extend(look["warnings"])
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    if dry:
        return _finish(action, True, stage, warnings, key="would_change", plan=plan)

    try:
        _author_rotation(prim, stack, op_value)
    except Exception as exc:
        return _finish(
            action,
            False,
            stage,
            warnings,
            key="would_change",
            plan=plan,
            error=f"Authoring failed part-way (the rotation op may be partly written): {exc}",
            written=True,
        )
    changed_op = prim.GetAttribute(stack["rotation_op"])
    readback = _readback_aim(prim, target_world)
    changed = {
        "path": str(sdf_path),
        "attribute": stack["rotation_op"],
        "value": _json(changed_op.Get() if changed_op else None),
        "xform_op_order": [str(name) for name in (UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get() or [])],
        **readback,
    }
    planned_error = plan["aim"]["error_degrees"] or 0.0
    if readback["aim_error_degrees"] is None or readback["aim_error_degrees"] > planned_error + AIM_TOLERANCE_DEGREES:
        warnings.append(
            "The composed result does not point at the target; a stronger opinion or another layer is overriding the edit."
        )
    return _finish(action, False, stage, warnings, key="would_change", plan=plan, extra={"changed": changed}, written=True)


# --- planning helpers -------------------------------------------------------------------------------------


def _dry(value: Any) -> bool:
    """Only an explicit false value authors; None, 0, empty containers and unrecognized strings stay a dry run."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off")
    return value is not False


def _is_light(prim: Any) -> bool:
    try:
        if prim.IsA(UsdLux.BoundableLightBase) or prim.IsA(UsdLux.NonboundableLightBase):
            return True
        return bool(prim.HasAPI(UsdLux.LightAPI))
    except Exception:
        return False


def _require_pxr() -> None:
    if _PXR_IMPORT_ERROR is not None:
        raise _Refusal(f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}")


def _check_stage(stage: Any) -> None:
    if stage is None or not hasattr(stage, "GetPrimAtPath") or not hasattr(stage, "GetEditTarget"):
        raise _Refusal("No USD stage is open.")


def _check_edit_target(stage: Any, warnings: list[str]) -> None:
    info = _edit_target_info(stage)
    if info is None or not info["identifier"]:
        raise _Refusal("The stage has no edit target layer.")
    if not info["editable"]:
        raise _Refusal(f"The edit target layer {info['identifier']} is read-only (permissionToEdit is False).")
    if info["session_layer"]:
        warnings.append(
            "The edit target is the session layer, so this edit is not saved with the scene; set the edit target "
            "to the root layer or a sublayer to keep it."
        )
    elif info["anonymous"]:
        warnings.append(
            f"The edit target is an anonymous in-memory layer ({info['identifier']}), so this edit is lost unless "
            "that layer is exported or saved to a file."
        )


def _edit_target_info(stage: Any) -> dict[str, Any] | None:
    try:
        layer = stage.GetEditTarget().GetLayer()
    except Exception:
        return None
    if layer is None:
        return None
    try:
        local = {item.identifier for item in stage.GetLayerStack(False)}
        session = {item.identifier for item in stage.GetLayerStack(True)} - local
        return {
            "identifier": _text(layer.identifier),
            "display_name": _text(layer.GetDisplayName()),
            "anonymous": bool(layer.anonymous),
            "session_layer": layer.identifier in session,
            "root_layer": layer.identifier == stage.GetRootLayer().identifier,
            "editable": bool(layer.permissionToEdit),
        }
    except Exception:
        return None


def _prim_path(value: Any, name: str) -> Any:
    if not isinstance(value, str) or not value.strip():
        raise _Refusal(f"{name} must be an absolute prim path such as /World/Key")
    text = value.strip()
    if len(text) > MAX_PATH_CHARS:
        raise _Refusal(f"{name} is longer than {MAX_PATH_CHARS} characters")
    valid = Sdf.Path.IsValidPathString(text)
    if isinstance(valid, tuple):  # (False, reason) for ill-formed paths
        valid = valid[0]
    if not valid:
        raise _Refusal(f"{name} is not a valid USD path: {text}")
    sdf_path = Sdf.Path(text)
    if (
        not sdf_path.IsAbsolutePath()
        or not sdf_path.IsPrimPath()
        or sdf_path == Sdf.Path.absoluteRootPath
        or sdf_path.ContainsPrimVariantSelection()
    ):
        raise _Refusal(f"{name} must be an absolute prim path such as /World/Key, got {text}")
    return sdf_path


def _creation_target(stage: Any, path: Any, warnings: list[str]) -> tuple[Any, Any]:
    """Validate a new prim path: it must not exist and its parent must be an active, authorable prim."""
    sdf_path = _prim_path(path, "path")
    existing = stage.GetPrimAtPath(sdf_path)
    if existing and existing.IsValid():
        raise _Refusal(f"A prim already exists at {sdf_path} ({existing.GetTypeName() or 'typeless'}); choose a new path.")
    parent_path = sdf_path.GetParentPath()
    parent = stage.GetPseudoRoot() if parent_path == Sdf.Path.absoluteRootPath else stage.GetPrimAtPath(parent_path)
    if not parent or not parent.IsValid():
        raise _Refusal(f"Parent prim does not exist: {parent_path}. Create it first.")
    if not parent.IsActive():
        raise _Refusal(f"Parent prim {parent_path} is inactive, so a child under it would not appear.")
    if not parent.IsLoaded():
        # The payload's own children are not composed while it is unloaded, so the 'already exists' check above
        # cannot see them; a new def here would retype a prim the payload already defines.
        raise _Refusal(
            f"Parent {parent_path} is an unloaded payload (or inside one); load it first so an existing prim at "
            f"{sdf_path} is not overwritten."
        )
    if parent.IsInstance() or parent.IsInstanceProxy() or parent.IsInPrototype():
        raise _Refusal(f"Parent {parent_path} is an instance (or inside one); children authored there are ignored.")
    if parent.IsA(UsdGeom.Gprim):
        warnings.append(f"Parent {parent_path} is a {parent.GetTypeName()} gprim; UsdGeom recommends not nesting prims under gprims.")
    return sdf_path, parent


def _resolve_up_axis(stage: Any, up_axis: Any, warnings: list[str]) -> str:
    stage_axis = str(UsdGeom.GetStageUpAxis(stage))
    if up_axis is None:
        try:
            return look_at.normalize_up_axis(stage_axis)
        except ValueError:
            raise _Refusal(f"The stage up axis {stage_axis!r} is not Y or Z.") from None
    try:
        axis = look_at.normalize_up_axis(up_axis)
    except ValueError as exc:
        raise _Refusal(str(exc)) from None
    if axis != stage_axis:
        warnings.append(f"up_axis {axis} overrides the stage up axis {stage_axis}.")
    return axis


def _resolve_target(stage: Any, target: Any, aimed_path: Any, warnings: list[str]) -> tuple[Any, dict[str, Any]]:
    """World-space aim point: a prim's bounding-box center (else its world position) or an (x, y, z) point."""
    if isinstance(target, str):
        target_path = _prim_path(target, "target")
        if target_path == aimed_path:
            raise _Refusal("A prim cannot be aimed at itself.")
        if target_path.HasPrefix(aimed_path):
            raise _Refusal(f"Target {target_path} is a descendant of {aimed_path}, so it would turn with the aim.")
        target_prim = stage.GetPrimAtPath(target_path)
        if not target_prim or not target_prim.IsValid():
            raise _Refusal(f"Target prim does not exist: {target_path}")
        tokens = {"default": UsdGeom.Tokens.default_, "render": UsdGeom.Tokens.render}
        cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [tokens[name] for name in TARGET_PURPOSES], useExtentsHint=False)
        box = cache.ComputeWorldBound(target_prim).ComputeAlignedRange()
        info: dict[str, Any] = {"target": str(target_path)}
        if not box.IsEmpty() and all(math.isfinite(value) for value in (*box.GetMin(), *box.GetMax())):
            point = Gf.Vec3d(box.GetMidpoint())
            info.update(
                target_source="bbox_center",
                target_bbox={"min": _vec_list(box.GetMin()), "max": _vec_list(box.GetMax())},
                bbox_purposes=list(TARGET_PURPOSES),
            )
        else:
            point = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(target_prim).ExtractTranslation()
            info["target_source"] = "world_translation"
            warnings.append(
                f"Target {target_path} has no bounds (for example a camera, light or empty Xform), so its world position is used."
            )
        _warn_animated_ancestors(target_prim, "Target", warnings)
    else:
        point = Gf.Vec3d(*_vec3(target, "target"))
        info = {"target": _vec_list(point), "target_source": "point"}
    info["target_point"] = _vec_list(point)
    return point, info


def _plan_new_xform(
    stage: Any, sdf_path: Any, parent: Any, translation: Any, aim_at: Any, up_axis: Any, warnings: list[str]
) -> dict[str, Any]:
    """translate + rotateXYZ values for a new prim, aimed in its parent's space when aim_at is given."""
    axis = _resolve_up_axis(stage, up_axis, warnings)
    eye_local = _vec3(translation, "translation") if translation is not None else (0.0, 0.0, 0.0)
    rotation = (0.0, 0.0, 0.0)
    quaternion = [1.0, 0.0, 0.0, 0.0]
    aim_info = None
    target_world = None
    if aim_at is not None:
        parent_world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(parent)
        _warn_animated_ancestors(parent, "Parent", warnings)
        target_world, target_info = _resolve_target(stage, aim_at, sdf_path, warnings)
        look = _look_in_parent_space(eye_local, target_world, parent_world, axis)
        rotation = tuple(look["rotate_xyz"])
        quaternion = look["quaternion"]
        local = _local_matrix(look_at.euler_to_matrix(rotation, "XYZ"), (1.0, 1.0, 1.0), eye_local)
        check = _verify_aim(local, parent_world, target_world)
        if check["error_degrees"] > AIM_TOLERANCE_DEGREES:
            raise _Refusal(f"Could not aim at the target (error {check['error_degrees']:.4f} degrees); the parent transform may be sheared.")
        eye_world = check["eye_world"]
        aim_info = {
            **target_info,
            "eye_world": _vec_list(eye_world),
            "distance": _clean((target_world - eye_world).GetLength()),
            "forward_world": check["forward_world"],
            "error_degrees": _clean(check["error_degrees"]),
            "fallback_up": look["fallback_up"],
        }
        warnings.extend(look["warnings"])
    public = {
        "xform_ops": [
            {"name": "xformOp:translate", "type": "double3", "value": _vec_list(eye_local)},
            {"name": "xformOp:rotateXYZ", "type": "float3", "value": _vec_list(rotation)},
        ],
        "xform_op_order": ["xformOp:translate", "xformOp:rotateXYZ"],
        "translation": _vec_list(eye_local),
        "rotation": _vec_list(rotation),
        "quaternion": [_clean(value) for value in quaternion],
        "aim": aim_info,
        "up_axis": axis,
    }
    return {"public": public, "translation": eye_local, "rotation": rotation, "target_world": target_world}


def _look_in_parent_space(eye_local: Any, target_world: Any, parent_world: Any, axis: str) -> dict[str, Any]:
    """look_at in the parent's space, so the authored local rotation points -Z at the world-space target."""
    to_parent = _inverse(parent_world, "The parent transform")
    target_local = to_parent.Transform(Gf.Vec3d(target_world))
    up_world = Gf.Vec3d(*look_at.UP_VECTORS[axis])
    view_world = Gf.Vec3d(target_world) - parent_world.Transform(Gf.Vec3d(*eye_local))
    vertical = view_world.GetLength() > 0.0 and (
        Gf.Cross(view_world.GetNormalized(), up_world).GetLength() < look_at.PARALLEL_TOLERANCE
    )
    # Up/horizon act as plane normals: map them with the transpose so local +X stays level under a non-uniformly
    # scaled parent. A straight up/down view keeps the inverse mapping so look_at's stable-roll fallback still applies.
    reference = to_parent if vertical else parent_world.GetTranspose()
    up_local = reference.TransformDir(up_world)
    horizon_local = reference.TransformDir(Gf.Vec3d(*look_at.HORIZON_FORWARD[axis]))
    look = look_at.look_at_rotation(tuple(eye_local), tuple(target_local), tuple(up_local), horizon_forward=tuple(horizon_local))
    if look["degenerate"]:
        raise _Refusal("The aim target is at the prim's own position, so there is no direction to aim along.")
    return look


def _classify_stack(prim: Any, xformable: Any) -> dict[str, Any]:
    """Which op stack the prim uses and where the rotation goes; refuses animated or unsupported stacks."""
    ops = list(xformable.GetOrderedXformOps())
    names = [str(op.GetOpName()) for op in ops]
    animated = [str(op.GetOpName()) for op in ops if op.GetNumTimeSamples() > 0 or op.MightBeTimeVarying()]
    if animated:
        raise _Refusal(f"Cannot aim {prim.GetPath()}: transform ops {animated} are animated (time samples or clips).")
    op_types = [op.GetOpType() for op in ops]
    three_axis = {getattr(UsdGeom.XformOp, "TypeRotate" + order): order for order in look_at.ROTATION_ORDERS}

    common = UsdGeom.XformCommonAPI(prim)
    if common:
        translate, _rotate, scale, pivot, order_enum = common.GetXformVectors(Usd.TimeCode.Default())
        if any(abs(float(value)) > 1e-12 for value in pivot):
            raise _Refusal(
                f"Cannot aim {prim.GetPath()}: it has a non-zero pivot {_vec_list(pivot)}, so rotating it would also "
                "move it. Reset the pivot first."
            )
        rotate_ops = [op for op in ops if op.GetOpType() in three_axis]
        order = three_axis[rotate_ops[0].GetOpType()] if rotate_ops else _order_name(order_enum)
        return {
            "kind": "xform_common",
            "ops": ops,
            "rotation_op": str(rotate_ops[0].GetOpName()) if rotate_ops else f"xformOp:rotate{order}",
            "op": rotate_ops[0] if rotate_ops else None,
            "adds_op": not rotate_ops,
            "order": order,
            "translate": tuple(float(value) for value in translate),
            "scale": tuple(float(value) for value in scale),
        }
    if len(ops) == 1 and op_types[0] == UsdGeom.XformOp.TypeTransform and not ops[0].IsInverseOp():
        return {"kind": "transform_matrix", "ops": ops, "rotation_op": names[0], "op": ops[0], "adds_op": False}
    canonical = [UsdGeom.XformOp.TypeTranslate, UsdGeom.XformOp.TypeOrient, UsdGeom.XformOp.TypeScale]
    if (
        UsdGeom.XformOp.TypeOrient in op_types
        and not any(op.IsInverseOp() for op in ops)
        and op_types == [kind for kind in canonical if kind in op_types]
    ):
        by_type = dict(zip(op_types, ops))
        translate_op = by_type.get(UsdGeom.XformOp.TypeTranslate)
        scale_op = by_type.get(UsdGeom.XformOp.TypeScale)
        orient_op = by_type[UsdGeom.XformOp.TypeOrient]
        return {
            "kind": "translate_orient_scale",
            "ops": ops,
            "rotation_op": str(orient_op.GetOpName()),
            "op": orient_op,
            "adds_op": False,
            "translate": tuple(float(v) for v in (translate_op.Get() if translate_op else (0.0, 0.0, 0.0)) or (0.0, 0.0, 0.0)),
            "scale": tuple(float(v) for v in (scale_op.Get() if scale_op else (1.0, 1.0, 1.0)) or (1.0, 1.0, 1.0)),
        }
    raise _Refusal(
        f"Cannot aim {prim.GetPath()}: unsupported xformOpOrder {names}. Supported stacks are XformCommonAPI-"
        "compatible ops, translate/orient/scale, or a single transform matrix."
    )


def _new_local_for_stack(stack: dict[str, Any], look: dict[str, Any], local: Any) -> tuple[Any, Any, dict[str, Any]]:
    """New local matrix, the value for the rotation op, and a JSON summary of the rotation."""
    rows = look["matrix"]
    info: dict[str, Any] = {"rotation": look["rotate_xyz"], "quaternion": look["quaternion"]}
    kind = stack["kind"]
    if kind == "xform_common":
        order = stack["order"]
        angles = look_at.matrix_to_euler(rows, order)
        new_local = _local_matrix(look_at.euler_to_matrix(angles, order), stack["scale"], stack["translate"])
        op = stack["op"]
        value = _vec3_for_op(op, angles) if op is not None else Gf.Vec3f(*angles)
        info["rotation_order"] = order
        return new_local, value, info
    if kind == "translate_orient_scale":
        quat = look["quaternion"]
        new_local = _local_matrix(look_at.quaternion_to_matrix(quat), stack["scale"], stack["translate"])
        info["rotation_order"] = None
        return new_local, _quat_for_op(stack["op"], quat), info
    transform = Gf.Transform(stack["op"].Get() or local)
    w, x, y, z = look["quaternion"]
    transform.SetRotation(Gf.Rotation(Gf.Quatd(w, Gf.Vec3d(x, y, z))))
    info["rotation_order"] = None
    new_matrix = transform.GetMatrix()
    return new_matrix, new_matrix, info


def _authored_local(stack: dict[str, Any], value: Any, new_local: Any) -> Any:
    """The local matrix USD composes from the value actually written (rounded to the op's precision)."""
    if stack["kind"] == "xform_common":
        rows = look_at.euler_to_matrix(tuple(float(v) for v in value), stack["order"])
    elif stack["kind"] == "translate_orient_scale":
        matrix = Gf.Matrix4d(1.0).SetRotate(Gf.Rotation(Gf.Quatd(value)))  # how UsdGeom composes orient (no renormalize)
        rows = tuple(tuple(matrix[r][c] for c in range(3)) for r in range(3))
    else:
        return new_local
    return _local_matrix(rows, stack["scale"], stack["translate"])


def _verify_aim(local: Any, parent_world: Any, target_world: Any) -> dict[str, Any]:
    """Transform local -Z to world space and measure how far it is from the eye-to-target direction."""
    world = local * parent_world
    eye = world.ExtractTranslation()
    forward = world.TransformDir(Gf.Vec3d(0.0, 0.0, -1.0))
    wanted = Gf.Vec3d(target_world) - eye
    if forward.GetLength() < 1e-12 or wanted.GetLength() < 1e-12:
        error = math.inf
        forward_list = _vec_list(forward)
    else:
        error = look_at.angle_between(tuple(forward), tuple(wanted))
        forward_list = _vec_list(forward.GetNormalized())
    return {"eye_world": eye, "forward_world": forward_list, "error_degrees": error}


def _stronger_opinion(stage: Any, prim: Any, attr_name: str) -> str | None:
    """Layer holding a stronger opinion than the edit target for attr_name, if any (local layer stack only)."""
    attr = prim.GetAttribute(attr_name)
    if not attr or not attr.IsValid():
        return None
    try:
        specs = attr.GetPropertyStack(Usd.TimeCode.Default())
    except Exception:
        return None
    if not specs:
        return None
    strongest = specs[0].layer
    edit_layer = stage.GetEditTarget().GetLayer()
    if strongest.identifier == edit_layer.identifier:
        return None
    order = [layer.identifier for layer in stage.GetLayerStack(True)]
    if strongest.identifier in order and (
        edit_layer.identifier not in order or order.index(strongest.identifier) < order.index(edit_layer.identifier)
    ):
        return strongest.identifier
    return None


def _warn_animated_ancestors(prim: Any, label: str, warnings: list[str]) -> None:
    current = prim
    while current and current.IsValid() and not current.IsPseudoRoot():
        xformable = UsdGeom.Xformable(current)
        if xformable and xformable.TransformMightBeTimeVarying():
            warnings.append(
                f"{label} {current.GetPath()} has an animated transform; the aim uses the default-time pose and "
                "drifts as it moves."
            )
            return
        current = current.GetParent()


def _light_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("_", "").replace(" ", "")
    if text.endswith("light") and text != "light":
        text = text[: -len("light")]
    if text not in LIGHT_TYPES:
        raise _Refusal(f"light_type must be one of {', '.join(LIGHT_TYPES)}, got {value!r}")
    return text


def _light_size(kind: str, size: Any, warnings: list[str]) -> dict[str, float]:
    if size is None:
        return {}
    if kind == "dome":
        warnings.append("size was ignored: dome lights have no size.")
        return {}
    if kind == "rect":
        if _is_number(size):
            width = height = _number(size, "size", minimum=0.0, exclusive=True)
        else:
            width, height = _pair(size, "size (width, height)", minimum=0.0, exclusive=True)
        return {"width": width, "height": height}
    if kind in ("sphere", "disk"):
        if not _is_number(size):
            raise _Refusal(f"size for a {kind} light is one radius")
        return {"radius": _number(size, "size (radius)", minimum=0.0, exclusive=True)}
    if kind == "cylinder":
        if _is_number(size):
            raise _Refusal("size for a cylinder light is (length, radius)")
        length, radius = _pair(size, "size (length, radius)", minimum=0.0, exclusive=True)
        return {"length": length, "radius": radius}
    if not _is_number(size):  # distant
        raise _Refusal("size for a distant light is its angular diameter in degrees")
    return {"angle": _number(size, "size (angle in degrees)", minimum=0.0, maximum=180.0)}


def _light_attribute_names(type_name: str, inputs: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    attributes: dict[str, tuple[str, Any]] = {}
    for key, value in inputs.items():
        token_name, create_method = _LIGHT_INPUTS[key]
        attributes[str(getattr(UsdLux.Tokens, token_name))] = (create_method, value)
    _check_schema_names(type_name, attributes)
    return attributes


def _check_schema_names(type_name: str, names: Any) -> None:
    definition = Usd.SchemaRegistry().FindConcretePrimDefinition(type_name)
    if definition is None:
        raise _Refusal(f"This USD build has no {type_name} schema.")
    known = set(definition.GetPropertyNames())
    missing = [name for name in names if name not in known]
    if missing:
        raise _Refusal(f"{type_name} in this USD version has no {missing} attributes.")


def _color(value: Any, warnings: list[str]) -> tuple[float, float, float]:
    rgb = _vec3(value, "color")
    if any(component < 0.0 for component in rgb):
        raise _Refusal(f"color components must not be negative, got {list(rgb)}")
    if max(rgb) <= 1.0:
        return rgb
    if max(rgb) <= 255.0 and all(component.is_integer() for component in rgb):
        normalized = tuple(component / 255.0 for component in rgb)
        warnings.append(
            f"color {list(rgb)} looked like 0-255 values, so it was divided by 255 to {_vec_list(normalized)} "
            "(no sRGB-to-linear conversion); UsdLux colors are 0-1."
        )
        return normalized  # type: ignore[return-value]
    raise _Refusal(
        f"color components must be 0-1 (or whole-number 0-255 values), got {list(rgb)}; use intensity or exposure "
        "to make a light brighter."
    )


def _clipping_range(value: Any) -> tuple[float, float]:
    near, far = _pair(value, "clipping_range (near, far)", minimum=0.0, exclusive=True)
    if far <= near:
        raise _Refusal(f"clipping_range far ({far}) must be greater than near ({near})")
    return near, far


def _is_number(value: Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _number(
    value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None, exclusive: bool = False
) -> float:
    if not _is_number(value):
        raise _Refusal(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise _Refusal(f"{name} must be finite, got {value!r}")
    if minimum is not None and (number <= minimum if exclusive else number < minimum):
        raise _Refusal(f"{name} must be {'greater than' if exclusive else 'at least'} {minimum:g}, got {number:g}")
    if maximum is not None and number > maximum:
        raise _Refusal(f"{name} must be at most {maximum:g}, got {number:g}")
    return number


def _pair(value: Any, name: str, **limits: Any) -> tuple[float, float]:
    items = _items(value, 2, name)
    return _number(items[0], name, **limits), _number(items[1], name, **limits)


def _vec3(value: Any, name: str) -> tuple[float, float, float]:
    items = _items(value, 3, name)
    x, y, z = (_number(item, name) for item in items)
    return x, y, z


def _items(value: Any, count: int, name: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__len__"):
        raise _Refusal(f"{name} must be {count} numbers, got {value!r}")
    try:
        items = [value[index] for index in range(len(value))]
    except Exception:
        raise _Refusal(f"{name} must be {count} numbers, got {value!r}") from None
    if len(items) != count:
        raise _Refusal(f"{name} must be {count} numbers, got {len(items)}")
    return items


def _order_name(order_enum: Any) -> str:
    for name in look_at.ROTATION_ORDERS:
        if getattr(UsdGeom.XformCommonAPI, "RotationOrder" + name) == order_enum:
            return name
    raise _Refusal(f"Unknown rotation order {order_enum}")


def _order_with_rotate(stack: dict[str, Any], order_before: list[str]) -> list[str]:
    """XformCommonAPI puts a new rotate op after the translate and pivot ops and before scale."""
    after = []
    inserted = False
    for name in order_before:
        if not inserted and (name.startswith("xformOp:scale") or name.startswith("!invert!")):
            after.append(stack["rotation_op"])
            inserted = True
        after.append(name)
    if not inserted:
        after.append(stack["rotation_op"])
    return after


def _inverse(matrix: Any, label: str) -> Any:
    determinant = matrix.GetDeterminant()
    if not math.isfinite(determinant) or abs(determinant) < _SINGULAR_DETERMINANT:
        raise _Refusal(f"{label} is singular (zero scale), so no rotation can aim through it.")
    return matrix.GetInverse()


def _local_matrix(rows: Any, scale: Any, translation: Any) -> Any:
    """Row-vector local transform scale * rotation * translate, as xformOpOrder [translate, rotate, scale] composes."""
    sx, sy, sz = (float(value) for value in scale)
    r0, r1, r2 = rows
    tx, ty, tz = (float(value) for value in translation)
    return Gf.Matrix4d(
        sx * r0[0], sx * r0[1], sx * r0[2], 0.0,
        sy * r1[0], sy * r1[1], sy * r1[2], 0.0,
        sz * r2[0], sz * r2[1], sz * r2[2], 0.0,
        tx, ty, tz, 1.0,
    )


def _vec3_for_op(op: Any, values: Any) -> Any:
    precision = op.GetPrecision()
    if precision == UsdGeom.XformOp.PrecisionFloat:
        return Gf.Vec3f(*values)
    if precision == UsdGeom.XformOp.PrecisionHalf:
        return Gf.Vec3h(*values)
    return Gf.Vec3d(*values)


def _quat_for_op(op: Any, quat: Any) -> Any:
    w, x, y, z = (float(value) for value in quat)
    precision = op.GetPrecision()
    if precision == UsdGeom.XformOp.PrecisionFloat:
        return Gf.Quatf(w, Gf.Vec3f(x, y, z))
    if precision == UsdGeom.XformOp.PrecisionHalf:
        return Gf.Quath(w, Gf.Vec3h(x, y, z))
    return Gf.Quatd(w, Gf.Vec3d(x, y, z))


# --- authoring helpers ------------------------------------------------------------------------------------


def _author_new_xform(prim: Any, translation: Any, rotation: Any) -> None:
    xformable = UsdGeom.Xformable(prim)
    xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(*translation))
    xformable.AddRotateXYZOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Vec3f(*rotation))


def _author_rotation(prim: Any, stack: dict[str, Any], value: Any) -> None:
    if stack["op"] is None:  # no rotate op yet: XformCommonAPI inserts one in its canonical place
        order = getattr(UsdGeom.XformCommonAPI, "RotationOrder" + stack["order"])
        if not UsdGeom.XformCommonAPI(prim).SetRotate(Gf.Vec3f(value), order):
            raise ValueError("XformCommonAPI.SetRotate failed")
        return
    if not stack["op"].Set(value):
        raise ValueError(f"Could not set {stack['rotation_op']}")


def _create(
    action: str,
    stage: Any,
    sdf_path: Any,
    plan: dict[str, Any],
    xform: dict[str, Any],
    attribute_names: list[str],
    author: Any,
    warnings: list[str],
) -> dict[str, Any]:
    """Run an authoring callback for a new prim; on failure remove the specs it added to the edit target.

    Define also writes 'over' specs for ancestors the edit target layer lacks, so the shallowest missing
    ancestor (or the prim itself) is what gets removed.
    """
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    spec_path = edit_target.MapToSpecPath(sdf_path)
    first_new = next((prefix for prefix in spec_path.GetPrefixes() if not layer.GetPrimAtPath(prefix)), None)
    try:
        prim = author()
    except Exception as exc:
        rolled_back = False
        if first_new is not None and layer.GetPrimAtPath(first_new):
            edit = Sdf.BatchNamespaceEdit()
            edit.Add(Sdf.NamespaceEdit.Remove(first_new))
            rolled_back = bool(layer.Apply(edit))
        note = "the new prim spec was removed again" if rolled_back else "the edit target may hold a partial prim spec"
        return _finish(action, False, stage, warnings, key="would_create", plan=plan, error=f"Authoring failed ({note}): {exc}",
                       written=not rolled_back)
    created: dict[str, Any] = {
        "path": str(prim.GetPath()),
        "type": prim.GetTypeName(),
        "attributes": {name: _json(prim.GetAttribute(name).Get()) for name in attribute_names},
        "xform_op_order": [str(name) for name in (UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get() or [])],
    }
    if xform["target_world"] is not None:
        created.update(_readback_aim(prim, xform["target_world"]))
    return _finish(action, False, stage, warnings, key="would_create", plan=plan, extra={"created": created}, written=True)


def _readback_aim(prim: Any, target_world: Any) -> dict[str, Any]:
    """Measure the composed result on the stage: where -Z points now and how far that is from the target."""
    world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)
    forward = world.TransformDir(Gf.Vec3d(0.0, 0.0, -1.0))
    wanted = Gf.Vec3d(target_world) - world.ExtractTranslation()
    if forward.GetLength() < 1e-12 or wanted.GetLength() < 1e-12:
        return {"world_translation": _vec_list(world.ExtractTranslation()), "forward_world": None, "aim_error_degrees": None}
    return {
        "world_translation": _vec_list(world.ExtractTranslation()),
        "forward_world": _vec_list(forward.GetNormalized()),
        "aim_error_degrees": _clean(look_at.angle_between(tuple(forward), tuple(wanted))),
    }


# --- result helpers ---------------------------------------------------------------------------------------


def _finish(
    action: str,
    dry: bool,
    stage: Any,
    warnings: list[str],
    *,
    key: str | None = None,
    plan: dict[str, Any] | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
    written: bool = False,
) -> dict[str, Any]:
    info = _edit_target_info(stage) if stage is not None and _PXR_IMPORT_ERROR is None else None
    unique: list[str] = []
    for warning in warnings:
        text = _text(warning)
        if text not in unique:
            unique.append(text)
    result: dict[str, Any] = {"ok": error is None, "dry_run": dry, "action": action, "written": written}
    if error is not None:
        result["error"] = _text(error)
    if key is not None and plan is not None:
        result[key] = plan
    result["edit_target_layer"] = info["identifier"] if info else None
    result["edit_target"] = info
    result["warnings"] = unique[:MAX_WARNINGS]
    if len(unique) > MAX_WARNINGS:
        result["warnings_truncated"] = len(unique) - MAX_WARNINGS
    if extra:
        result.update(extra)
    if error is None and dry:
        result["next_step"] = "Dry run: nothing was written. Call again with dry_run=False to author this."
    return result


def _json(value: Any) -> Any:
    if isinstance(value, tuple):
        value = list(value)
    if isinstance(value, float):
        return _clean(value)
    if isinstance(value, list) and all(isinstance(item, float) for item in value):
        return [_clean(item) for item in value]
    return to_jsonable(value)


def _vec_list(values: Any) -> list[float]:
    return [_clean(float(value)) for value in values]


def _clean(value: float) -> float | None:
    if not math.isfinite(value):
        return None
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0.0 else rounded


def _text(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "..."
