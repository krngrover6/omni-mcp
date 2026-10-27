"""Create physics joints, drives, limits and articulation roots, previewed first.

The joint frames match Isaac Sim's own Create > Physics > Joint (omni.physx scripts/utils.py createJoint): the joint
prim goes under the second body, its frame sits at that body's origin with the body's orientation (so axis "X" is the
body's own X), localPos0/localRot0 is that frame seen from the first body with scale removed, one body alone is pinned
to the world, and break force and torque start at the largest float. A pivot point (world coordinates) moves the
frame, e.g. to put a door's hinge on its edge.

Writes only with dry_run=False, into the current edit target. Each write is snapshotted, verified, rolled back on
failure, and recorded for undo when an undo stack is given.
"""
from __future__ import annotations

import math
from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    Gf = Sdf = Usd = UsdGeom = UsdPhysics = None

KINDS = {"fixed": "FixedJoint", "revolute": "RevoluteJoint", "prismatic": "PrismaticJoint",
         "spherical": "SphericalJoint", "distance": "DistanceJoint", "d6": "Joint"}
D6_AXES = ("transX", "transY", "transZ", "rotX", "rotY", "rotZ")
# PhysX extras (PhysxSchema in Isaac Sim 6.1, omni.usd.schema.physx 110.3): written by name so they also work
# where PhysxSchema is not installed; Isaac's own schema recognises them.
GEAR_TYPE, RACK_TYPE = "PhysxPhysicsGearJoint", "PhysxPhysicsRackAndPinionJoint"
JOINT_API, SPRING_API = "PhysxJointAPI", "PhysxPhysicsDistanceJointAPI"
_FACE = __import__("re").compile(r"^(?:[+-][xyz]){1,3}$")
AXES = ("X", "Y", "Z")
MAX_FLOAT = 3.4028234663852886e38
MOTION_LEFT = {
    "fixed": "none: the two bodies move as one",
    "revolute": "one turn around the axis (like a wheel or hinge)",
    "prismatic": "one slide along the axis (like a drawer)",
    "spherical": "turning in any direction (like a shoulder), within the cone limits",
    "distance": "anything, as long as the distance stays between min and max",
    "d6": "only the axes you leave free (the others are locked)",
}
_COLLIDER_SCAN = 500


class _Refusal(Exception):
    pass


# ------------------------------------------------------------------------------------------------ helpers
def _ready(stage: Any) -> None:
    if UsdPhysics is None:
        raise _Refusal("pxr (USD) is not available in this Python")
    if stage is None:
        raise _Refusal("No stage is open")


def _prim(stage: Any, path: Any, label: str) -> Any:
    if not isinstance(path, str) or not Sdf.Path.IsValidPathString(path) or not Sdf.Path(path).IsAbsolutePath():
        raise _Refusal(f"{label} must be an absolute prim path such as /World/Box")
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid() or prim.IsPseudoRoot():
        raise _Refusal(f"{label}: no prim at {path}")
    if prim.IsInstanceProxy() or prim.IsInPrototype():
        raise _Refusal(f"{label} {path} is inside an instance; edit its source asset or make it non-instanceable first")
    return prim


def _number(value: Any, label: str, *, low: float | None = None, high: float | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _Refusal(f"{label} must be a finite number")
    if low is not None and value < low:
        raise _Refusal(f"{label} must be at least {low}")
    if high is not None and value > high:
        raise _Refusal(f"{label} must be at most {high}")
    return float(value)


def _rigid(prim: Any) -> bool:
    return bool(prim) and prim.HasAPI(UsdPhysics.RigidBodyAPI)


def _kinematic(prim: Any) -> bool:
    attr = prim.GetAttribute("physics:kinematicEnabled")
    return bool(attr and attr.Get())


def _has_collider(prim: Any) -> bool:
    for count, item in enumerate(Usd.PrimRange(prim)):
        if count >= _COLLIDER_SCAN:
            return True  # big subtree: assume the asset carries its own colliders
        if item.HasAPI(UsdPhysics.CollisionAPI):
            return True
    return False


def _joint_parent(prim: Any) -> Any:
    """Like Isaac: the joint goes under the body, or its first writable ancestor outside instances."""
    while prim and not prim.IsPseudoRoot() and (prim.IsInPrototype() or prim.IsInstanceProxy() or prim.IsInstanceable()):
        prim = prim.GetParent()
    return prim


def _unused(stage: Any, parent: Any, name: str) -> Any:
    base = parent.GetPath() if parent and not parent.IsPseudoRoot() else Sdf.Path.absoluteRootPath
    candidate = base.AppendChild(name)
    index = 1
    while stage.GetPrimAtPath(candidate):
        candidate = base.AppendChild(f"{name}_{index:02d}")
        index += 1
    return candidate


def _vec(value: Any, label: str) -> Any:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _Refusal(f"{label} must be three numbers [x, y, z]")
    return Gf.Vec3d(*[_number(v, label) for v in value])


def _frames(stage: Any, body0: Any, body1: Any, pivot: Any) -> dict[str, Any]:
    """Joint frames the way Isaac's createJoint makes them, optionally moved to a world pivot point."""
    cache = UsdGeom.XformCache()
    to_pose = cache.GetLocalToWorldTransform(body1)
    frame = Gf.Matrix4d(to_pose).RemoveScaleShear()
    if pivot is not None:
        if pivot == "body0" and body0:
            point = cache.GetLocalToWorldTransform(body0).ExtractTranslation()
        elif pivot in ("body1", "center"):
            point = to_pose.ExtractTranslation()
        elif isinstance(pivot, str):
            point = _face_point(body1, pivot)
        else:
            point = _vec(pivot, "pivot")
        frame.SetTranslateOnly(Gf.Vec3d(point))
    if body0 is None:
        rel0 = Gf.Matrix4d(frame)
    else:
        rel0 = (frame * cache.GetLocalToWorldTransform(body0).GetInverse()).RemoveScaleShear()
    rel1 = (frame * to_pose.GetInverse()).RemoveScaleShear()
    return {
        "pos0": Gf.Vec3f(rel0.ExtractTranslation()), "rot0": Gf.Quatf(rel0.ExtractRotationQuat()),
        "pos1": Gf.Vec3f(rel1.ExtractTranslation()), "rot1": Gf.Quatf(rel1.ExtractRotationQuat()),
        "frame": frame,
    }


def _face_point(prim: Any, spec: str) -> Any:
    """A point on the prim's world bounding box: '-y' is the centre of its -Y face, '-x-y' the middle of that edge."""
    text = spec.strip().lower()
    if not _FACE.fullmatch(text):
        raise _Refusal("pivot must be [x, y, z], 'center', 'body0', or a face/edge of body1 such as '-y', '+x' or '-x-y'")
    box = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"]).ComputeWorldBound(prim).ComputeAlignedRange()
    if box.IsEmpty():
        raise _Refusal(f"{prim.GetPath()} has no size to measure; give the pivot as [x, y, z] instead")
    low, high = box.GetMin(), box.GetMax()
    point = [(low[i] + high[i]) / 2 for i in range(3)]
    for sign, letter in zip(text[0::2], text[1::2]):
        index = "xyz".index(letter)
        point[index] = high[index] if sign == "+" else low[index]
    return Gf.Vec3d(*point)


def _round(values: Any, digits: int = 4) -> list[float]:
    return [round(float(v), digits) + 0.0 for v in values]


def _quat(q: Any) -> list[float]:
    return _round([q.GetReal(), *q.GetImaginary()])


def _result(action: str, **fields: Any) -> dict[str, Any]:
    base = {"ok": "error" not in fields, "action": action, "dry_run": True, "applied": False, "written": False,
            "warnings": []}
    base.update(fields)
    base["written"] = base["applied"]
    return base


def _apply(stage: Any, action: str, paths: list[Any], shallow: list[Any], mutate: Any, verify: Any,
           undo_stack: Any, label: str, common: dict[str, Any]) -> dict[str, Any]:
    from . import undo

    layer = stage.GetEditTarget().GetLayer()
    if layer is None or not layer.permissionToEdit:
        return _result(action, error="The current edit target layer is read-only, so nothing was changed.", **common)
    try:
        snapshot = undo.SpecSnapshot.capture(layer, paths, shallow=shallow)
    except Exception as exc:  # noqa: BLE001
        return _result(action, error=f"Could not snapshot the layer, so nothing was changed: {exc}", **common)
    try:
        mutate()  # Usd-level Define/Apply: not inside an Sdf.ChangeBlock, which forbids recomposition
        problem = verify()
        if problem:
            raise _Refusal(problem)
    except Exception as exc:  # noqa: BLE001 - every failure rolls back
        try:
            snapshot.restore()
            rolled = snapshot.matches()
        except Exception:  # noqa: BLE001
            rolled = False
        return _result(action, error=f"Authoring failed ({'rolled back' if rolled else 'rollback incomplete'}): {exc}",
                       applied=not rolled, dry_run=False, **common)
    undo_info: dict[str, Any] = {"recorded": False}
    if undo_stack is not None:
        try:
            entry = undo.UndoEntry(label, [str(p) for p in list(paths) + list(shallow)], [snapshot], [snapshot.recapture()],
                                   stage_ref=undo.LayerRef(stage.GetRootLayer()))
            undo_stack.push(entry)
            undo_info = {"recorded": True, "label": label}
        except Exception as exc:  # noqa: BLE001
            common.setdefault("warnings", []).append(f"The edit worked but could not be recorded for undo: {exc}")
    return _result(action, applied=True, dry_run=False, undo=undo_info, **common)


def _guard(function):
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except _Refusal as exc:
            return _result(function.__name__, error=str(exc))
    wrapper.__name__ = function.__name__
    wrapper.__doc__ = function.__doc__
    wrapper.__wrapped__ = function
    return wrapper


# ------------------------------------------------------------------------------------------------ public
@_guard
def create_joint(stage: Any, kind: str, body0: str | None = None, body1: str | None = None, *, pivot: Any = None,
                 axis: str = "X", lower: Any = None, upper: Any = None, min_distance: Any = None,
                 max_distance: Any = None, cone0: Any = None, cone1: Any = None, free: Any = None,
                 name: str | None = None, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False create) a fixed, revolute, prismatic, spherical, distance or d6 joint between
    body0 and body1, like Isaac Sim's Create > Physics > Joint. Give only one body to pin it to the world. pivot is a
    world point [x, y, z], 'center', or a face/edge of body1 such as '-y' or '-x-y' (a door's hinge edge); axis X/Y/Z
    is in body1's frame. lower/upper are degrees (revolute) or distance (prismatic); cone0/cone1 degrees (spherical);
    min/max_distance (distance). d6: free={'rotZ': [-90, 90], 'transX': None}, others locked."""
    _ready(stage)
    key = str(kind).strip().lower() if isinstance(kind, str) else ""
    if key not in KINDS:
        raise _Refusal(f"kind must be one of {', '.join(KINDS)}")
    if body0 is None and body1 is None:
        raise _Refusal("Give body1 (and body0 to join two bodies, or leave body0 out to pin body1 to the world)")
    if body1 is None:
        body0, body1 = None, body0
    p1 = _prim(stage, body1, "body1")
    p0 = _prim(stage, body0, "body0") if body0 is not None else None
    if p0 is not None and p0.GetPath() == p1.GetPath():
        raise _Refusal("body0 and body1 are the same prim; a joint needs two different bodies")
    warnings: list[str] = []
    bodies = [p for p in (p0, p1) if p is not None]
    if not any(_rigid(p) for p in bodies):
        names = " and ".join(str(p.GetPath()) for p in bodies)
        raise _Refusal(f"Cannot create a joint between static bodies: {names} {'has' if len(bodies) == 1 else 'have'} no "
                       "rigid body. Give the moving part a rigid body first (scene_make_rigid_body, or in Isaac Sim "
                       "Add > Physics > Rigid Body with Colliders Preset).")
    for prim in bodies:
        if _rigid(prim) and not _has_collider(prim):
            warnings.append(f"{prim.GetPath()} has no collider, so it will pass through other objects.")
    if all(_kinematic(p) for p in bodies if _rigid(p)):
        warnings.append("Every rigid body here is kinematic, so physics will not move this joint.")
    axis_key = str(axis).strip().upper() if isinstance(axis, str) else ""
    if key in ("revolute", "prismatic", "spherical") and axis_key not in AXES:
        raise _Refusal("axis must be X, Y or Z")
    unit = "degrees" if key == "revolute" else "stage units (metersPerUnit=" + str(UsdGeom.GetStageMetersPerUnit(stage)) + ")"
    lo, hi = _number(lower, "lower"), _number(upper, "upper")
    if key not in ("revolute", "prismatic") and (lo is not None or hi is not None):
        raise _Refusal("lower/upper limits apply to revolute and prismatic joints")
    if lo is not None and hi is not None and lo >= hi:
        raise _Refusal("lower must be smaller than upper")
    if key == "revolute" and any(v is not None and abs(v) > 360 for v in (lo, hi)):
        raise _Refusal("revolute limits are degrees between -360 and 360")
    c0, c1 = _number(cone0, "cone0", low=0, high=180), _number(cone1, "cone1", low=0, high=180)
    if key != "spherical" and (c0 is not None or c1 is not None):
        raise _Refusal("cone0/cone1 apply to spherical joints")
    dmin, dmax = _number(min_distance, "min_distance", low=0), _number(max_distance, "max_distance", low=0)
    if key != "distance" and (dmin is not None or dmax is not None):
        raise _Refusal("min_distance/max_distance apply to distance joints")
    if dmin is not None and dmax is not None and dmin > dmax:
        raise _Refusal("min_distance must not be larger than max_distance")
    if name is not None and (not isinstance(name, str) or not Sdf.Path.IsValidIdentifier(name)):
        raise _Refusal("name must be a valid prim name such as WheelJoint_01")
    d6: dict[str, Any] = {}
    if key == "d6":
        if free is None:
            free = {}
        if not isinstance(free, dict) or any(axis not in D6_AXES for axis in free):
            raise _Refusal(f"free must map axes ({', '.join(D6_AXES)}) to None (free) or [low, high] (limited)")
        for axis_name in D6_AXES:
            if axis_name not in free:
                d6[axis_name] = "locked"
            elif free[axis_name] is None:
                d6[axis_name] = "free"
            else:
                pair = free[axis_name]
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    raise _Refusal(f"{axis_name} must be None or [low, high]")
                low, high = _number(pair[0], f"{axis_name} low"), _number(pair[1], f"{axis_name} high")
                if low >= high:
                    raise _Refusal(f"{axis_name}: low must be smaller than high")
                if axis_name.startswith("rot") and max(abs(low), abs(high)) > 360:
                    raise _Refusal(f"{axis_name} limits are degrees between -360 and 360")
                d6[axis_name] = [low, high]
    elif free is not None:
        raise _Refusal("free applies to d6 joints")

    frames = _frames(stage, p0, p1, pivot)
    path = _unused(stage, _joint_parent(p1), name or KINDS[key])
    axis_world = None
    if axis_key in AXES and key in ("revolute", "prismatic", "spherical"):
        unit_axis = {"X": Gf.Vec3d(1, 0, 0), "Y": Gf.Vec3d(0, 1, 0), "Z": Gf.Vec3d(0, 0, 1)}[axis_key]
        axis_world = _round(frames["frame"].TransformDir(unit_axis), 3)
    plan = {
        "joint_path": str(path), "type": KINDS[key], "body0": str(p0.GetPath()) if p0 else None,
        "body1": str(p1.GetPath()), "pinned_to_world": p0 is None,
        "pivot_world": _round(frames["frame"].ExtractTranslation()), "axis": axis_key if axis_world else None,
        "axis_world_direction": axis_world, "local_pos0": _round(frames["pos0"]), "local_rot0": _quat(frames["rot0"]),
        "local_pos1": _round(frames["pos1"]), "local_rot1": _quat(frames["rot1"]),
        "limits": {k: v for k, v in (("lower", lo), ("upper", hi), ("cone0", c0), ("cone1", c1),
                                      ("min_distance", dmin), ("max_distance", dmax)) if v is not None},
        "limit_units": unit if key in ("revolute", "prismatic") else None,
        "motion_left": MOTION_LEFT[key],
    }
    if d6:
        plan["d6_axes"] = d6
    explanation = (f"A {key} joint at {plan['pivot_world']} joining {plan['body0'] or 'the world'} and {plan['body1']}. "
                   f"It leaves {MOTION_LEFT[key]}." + (f" Axis {axis_key} points along {axis_world} in the world."
                                                        if axis_world else ""))
    common = {"plan": plan, "explanation": explanation, "warnings": warnings, "python": _joint_python(plan, key)}
    if dry_run is not False:
        return _result("create_joint", next_step="Run again with dry_run=False to create it (undoable).", **common)

    def mutate() -> None:
        schema = getattr(UsdPhysics, KINDS[key]).Define(stage, path)
        if p0 is not None:
            schema.CreateBody0Rel().SetTargets([p0.GetPath()])
        schema.CreateBody1Rel().SetTargets([p1.GetPath()])
        schema.CreateLocalPos0Attr().Set(frames["pos0"])
        schema.CreateLocalRot0Attr().Set(frames["rot0"])
        schema.CreateLocalPos1Attr().Set(frames["pos1"])
        schema.CreateLocalRot1Attr().Set(frames["rot1"])
        schema.CreateBreakForceAttr().Set(MAX_FLOAT)
        schema.CreateBreakTorqueAttr().Set(MAX_FLOAT)
        if axis_world:
            schema.CreateAxisAttr().Set(axis_key)
        if key in ("revolute", "prismatic"):
            if lo is not None:
                schema.CreateLowerLimitAttr().Set(lo)
            if hi is not None:
                schema.CreateUpperLimitAttr().Set(hi)
        if key == "spherical":
            if c0 is not None:
                schema.CreateConeAngle0LimitAttr().Set(c0)
            if c1 is not None:
                schema.CreateConeAngle1LimitAttr().Set(c1)
        if key == "distance":
            if dmin is not None:
                schema.CreateMinDistanceAttr().Set(dmin)
            if dmax is not None:
                schema.CreateMaxDistanceAttr().Set(dmax)
        for axis_name, state in d6.items():  # Isaac's convention: low > high locks the axis
            if state == "free":
                continue
            limit = UsdPhysics.LimitAPI.Apply(schema.GetPrim(), axis_name)
            low, high = (1.0, -1.0) if state == "locked" else state
            limit.CreateLowAttr().Set(low)
            limit.CreateHighAttr().Set(high)

    def verify() -> str | None:
        prim = stage.GetPrimAtPath(path)
        ok = prim and prim.IsA(getattr(UsdPhysics, KINDS[key]))
        return None if ok else "the joint prim was not created"

    return _apply(stage, "create_joint", [path], [], mutate, verify, undo_stack, f"Create {key} joint {path.name}", common)


@_guard
def add_drive(stage: Any, joint_path: str, *, target_position: Any = None, target_velocity: Any = None,
              stiffness: Any = None, damping: Any = None, max_force: Any = None, drive_type: str = "force",
              undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False add) a motor to a revolute (angular drive) or prismatic (linear drive) joint.
    To hold or reach an angle/position give target_position and stiffness; to spin at a speed give target_velocity
    and damping. Angular targets are degrees and degrees per second."""
    _ready(stage)
    joint = _prim(stage, joint_path, "joint_path")
    if joint.IsA(UsdPhysics.RevoluteJoint):
        instance, units = "angular", ("degrees", "degrees per second")
    elif joint.IsA(UsdPhysics.PrismaticJoint):
        instance, units = "linear", ("stage units", "stage units per second")
    else:
        raise _Refusal("Drives work on revolute (angular) and prismatic (linear) joints only")
    pos, vel = _number(target_position, "target_position"), _number(target_velocity, "target_velocity")
    stiff, damp = _number(stiffness, "stiffness", low=0), _number(damping, "damping", low=0)
    force = _number(max_force, "max_force", low=0)
    if pos is None and vel is None:
        raise _Refusal("Give target_position (to hold or reach a position) or target_velocity (to spin or slide at a speed)")
    if pos is not None and not stiff:
        raise _Refusal("A position target needs stiffness > 0 (how hard it pulls toward the target). Start around 1000 "
                       "and raise it if the joint is too weak; add some damping to stop it wobbling.")
    if vel is not None and pos is None and not damp:
        raise _Refusal("A velocity target needs damping > 0 (how hard it pushes toward that speed). Start around 1000.")
    if drive_type not in ("force", "acceleration"):
        raise _Refusal("drive_type must be force or acceleration")
    values = {"type": drive_type, "targetPosition": pos, "targetVelocity": vel, "stiffness": stiff, "damping": damp,
              "maxForce": force}
    plan = {"joint_path": str(joint.GetPath()), "drive": instance,
            "values": {k: v for k, v in values.items() if v is not None},
            "units": {"target_position": units[0], "target_velocity": units[1]}}
    explanation = (f"An {instance} drive on {joint.GetPath()}: "
                   + (f"it pulls toward {pos} {units[0]} with stiffness {stiff}" if pos is not None else
                      f"it pushes toward {vel} {units[1]} with damping {damp}") + ".")
    common = {"plan": plan, "explanation": explanation, "warnings": [], "python": _drive_python(plan)}
    if dry_run is not False:
        return _result("add_drive", next_step="Run again with dry_run=False to add it (undoable).", **common)

    def mutate() -> None:
        drive = UsdPhysics.DriveAPI.Apply(joint, instance)
        drive.CreateTypeAttr().Set(drive_type)
        for name, value in (("TargetPosition", pos), ("TargetVelocity", vel), ("Stiffness", stiff),
                            ("Damping", damp), ("MaxForce", force)):
            if value is not None:
                getattr(drive, f"Create{name}Attr")().Set(value)

    def verify() -> str | None:
        return None if joint.HasAPI(UsdPhysics.DriveAPI, instance) else "the drive was not applied"

    return _apply(stage, "add_drive", [], [joint.GetPath()], mutate, verify, undo_stack,
                  f"Add {instance} drive to {joint.GetName()}", common)


@_guard
def set_joint_limits(stage: Any, joint_path: str, lower: Any = None, upper: Any = None, *, undo_stack: Any = None,
                     dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False set) a revolute (degrees) or prismatic (distance) joint's lower/upper limits."""
    _ready(stage)
    joint = _prim(stage, joint_path, "joint_path")
    if joint.IsA(UsdPhysics.RevoluteJoint):
        schema, unit = UsdPhysics.RevoluteJoint(joint), "degrees"
    elif joint.IsA(UsdPhysics.PrismaticJoint):
        schema, unit = UsdPhysics.PrismaticJoint(joint), "stage units"
    else:
        raise _Refusal("Limits here apply to revolute and prismatic joints")
    lo, hi = _number(lower, "lower"), _number(upper, "upper")
    if lo is None and hi is None:
        raise _Refusal("Give lower, upper or both")
    old_lo, old_hi = schema.GetLowerLimitAttr().Get(), schema.GetUpperLimitAttr().Get()
    new_lo = lo if lo is not None else old_lo
    new_hi = hi if hi is not None else old_hi
    if new_lo is not None and new_hi is not None and math.isfinite(new_lo) and math.isfinite(new_hi) and new_lo >= new_hi:
        raise _Refusal("lower must be smaller than upper")
    plan = {"joint_path": str(joint.GetPath()), "lower": new_lo, "upper": new_hi, "units": unit,
            "before": {"lower": old_lo, "upper": old_hi}}
    common = {"plan": plan, "warnings": [],
              "explanation": f"{joint.GetName()} may move between {new_lo} and {new_hi} {unit}."}
    if dry_run is not False:
        return _result("set_joint_limits", next_step="Run again with dry_run=False to set them (undoable).", **common)

    def mutate() -> None:
        if lo is not None:
            schema.CreateLowerLimitAttr().Set(lo)
        if hi is not None:
            schema.CreateUpperLimitAttr().Set(hi)

    def verify() -> str | None:
        return None

    return _apply(stage, "set_joint_limits", [], [joint.GetPath()], mutate, verify, undo_stack,
                  f"Set limits on {joint.GetName()}", common)


@_guard
def add_articulation_root(stage: Any, prim_path: str, *, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False add) an articulation root: it marks a jointed machine (a robot) so PhysX
    simulates and controls it accurately. Put it on the base body, or on the robot's top prim."""
    _ready(stage)
    prim = _prim(stage, prim_path, "prim_path")
    clash = [str(p.GetPath()) for p in Usd.PrimRange(prim) if p.HasAPI(UsdPhysics.ArticulationRootAPI)]
    parent = prim.GetParent()
    while parent and not parent.IsPseudoRoot():
        if parent.HasAPI(UsdPhysics.ArticulationRootAPI):
            clash.append(str(parent.GetPath()))
        parent = parent.GetParent()
    if clash:
        raise _Refusal(f"There is already an articulation root at {clash[0]}; one machine needs exactly one root")
    joints = [p for p in Usd.PrimRange(prim) if p.IsA(UsdPhysics.Joint)]
    warnings = [] if joints else ["No joints were found under this prim. The root goes on the base of a jointed "
                                  "machine whose joints sit under it."]
    if not _rigid(prim) and not any(_rigid(p) for p in Usd.PrimRange(prim)):
        warnings.append("No rigid bodies were found here; an articulation needs rigid-body links.")
    plan = {"prim_path": str(prim.GetPath()), "joints_found": len(joints)}
    common = {"plan": plan, "warnings": warnings,
              "explanation": f"{prim.GetPath()} becomes the articulation root for {len(joints)} joint(s) below it."}
    if dry_run is not False:
        return _result("add_articulation_root", next_step="Run again with dry_run=False to add it (undoable).", **common)

    def mutate() -> None:
        UsdPhysics.ArticulationRootAPI.Apply(prim)

    def verify() -> str | None:
        return None if prim.HasAPI(UsdPhysics.ArticulationRootAPI) else "the articulation root was not applied"

    return _apply(stage, "add_articulation_root", [], [prim.GetPath()], mutate, verify, undo_stack,
                  f"Add articulation root to {prim.GetName()}", common)


def _api_names(prim: Any) -> list[str]:
    """Applied API schema names straight from metadata: GetAppliedSchemas() hides PhysX schemas where PhysxSchema
    is not installed (plain usd-core), though Isaac Sim sees them."""
    ops = prim.GetMetadata("apiSchemas")
    return [str(name) for name in ops.ApplyOperations([])] if ops else []


def _add_api(prim: Any, name: str) -> None:
    if name not in _api_names(prim):
        prim.AddAppliedSchema(name)


def _moving_body(joint: Any) -> Any:
    """The body a hinge or slider moves: body1, or body0 when body1 is empty (as Isaac's demos connect them)."""
    schema = UsdPhysics.Joint(joint)
    for rel in (schema.GetBody1Rel(), schema.GetBody0Rel()):
        targets = rel.GetTargets() if rel else []
        if targets:
            return joint.GetStage().GetPrimAtPath(targets[0])
    return None


def _coupling(stage: Any, action: str, type_name: str, links: list[tuple[str, Any, Any]], ratio_attr: str, ratio: float,
              name: str | None, undo_stack: Any, dry_run: Any, explanation: str) -> dict[str, Any]:
    """Shared body of gear and rack-and-pinion joints: they couple existing joints with a ratio."""
    bodies = [_moving_body(joint) for _, joint, _ in links]
    if any(body is None for body in bodies):
        raise _Refusal("Each linked joint must connect to a body (body0 or body1)")
    if bodies[0].GetPath() == bodies[1].GetPath():
        raise _Refusal("The two joints move the same body; couple joints that move different parts")
    if name is not None and (not isinstance(name, str) or not Sdf.Path.IsValidIdentifier(name)):
        raise _Refusal("name must be a valid prim name")
    path = _unused(stage, _joint_parent(bodies[0]), name or type_name.replace("PhysxPhysics", ""))
    plan = {"joint_path": str(path), "type": type_name, "body0": str(bodies[0].GetPath()),
            "body1": str(bodies[1].GetPath()), ratio_attr: ratio,
            "links": {rel: str(joint.GetPath()) for rel, joint, _ in links}}
    python = "\n".join([
        "# The same coupling in Python (Script Editor).",
        "import omni.usd",
        "from pxr import PhysxSchema, Sdf",
        "stage = omni.usd.get_context().get_stage()",
        f"joint = PhysxSchema.{type_name}.Define(stage, {str(path)!r})",
        f"joint.CreateBody0Rel().SetTargets([Sdf.Path({plan['body0']!r})])",
        f"joint.CreateBody1Rel().SetTargets([Sdf.Path({plan['body1']!r})])",
        f"joint.Create{ratio_attr[0].upper() + ratio_attr[1:]}Attr({ratio!r})",
    ] + [f"joint.Create{method}Rel().SetTargets([Sdf.Path({str(joint.GetPath())!r})])" for _, joint, method in links])
    common = {"plan": plan, "explanation": explanation, "warnings": [], "python": python}
    if dry_run is not False:
        return _result(action, next_step="Run again with dry_run=False to create it (undoable).", **common)

    def mutate() -> None:
        prim = stage.DefinePrim(path, type_name)
        prim.CreateRelationship("physics:body0").SetTargets([bodies[0].GetPath()])
        prim.CreateRelationship("physics:body1").SetTargets([bodies[1].GetPath()])
        prim.CreateAttribute(f"physics:{ratio_attr}", Sdf.ValueTypeNames.Float).Set(ratio)
        for rel, joint, _ in links:
            prim.CreateRelationship(f"physics:{rel}").SetTargets([joint.GetPath()])

    def verify() -> str | None:
        prim = stage.GetPrimAtPath(path)
        return None if prim and prim.GetTypeName() == type_name else "the coupling joint was not created"

    return _apply(stage, action, [path], [], mutate, verify, undo_stack, f"Create {path.name}", common)


@_guard
def create_gear_joint(stage: Any, hinge0: str, hinge1: str, ratio: Any = 1.0, *, name: str | None = None,
                      undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False create) a gear joint: two existing revolute joints turn together. ratio is how
    fast the second turns per turn of the first; for touching gears use radius0 / radius1 (Isaac's gear demo)."""
    _ready(stage)
    first, second = _prim(stage, hinge0, "hinge0"), _prim(stage, hinge1, "hinge1")
    for prim in (first, second):
        if not prim.IsA(UsdPhysics.RevoluteJoint):
            raise _Refusal(f"{prim.GetPath()} is not a revolute joint; a gear couples two revolute joints")
    if first.GetPath() == second.GetPath():
        raise _Refusal("hinge0 and hinge1 must be different joints")
    value = _number(ratio, "ratio")
    if not value:
        raise _Refusal("ratio must not be 0")
    return _coupling(stage, "create_gear_joint", GEAR_TYPE, [("hinge0", first, "Hinge0"), ("hinge1", second, "Hinge1")],
                     "gearRatio", value, name, undo_stack, dry_run,
                     f"A gear joint: when {first.GetName()} turns, {second.GetName()} turns {value} times as fast.")


@_guard
def create_rack_and_pinion_joint(stage: Any, hinge: str, prismatic: str, ratio: Any, *, name: str | None = None,
                                 undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False create) a rack-and-pinion joint: turning an existing revolute joint slides an
    existing prismatic joint. ratio is degrees of turn per unit of slide."""
    _ready(stage)
    turn, slide = _prim(stage, hinge, "hinge"), _prim(stage, prismatic, "prismatic")
    if not turn.IsA(UsdPhysics.RevoluteJoint):
        raise _Refusal(f"{turn.GetPath()} is not a revolute joint")
    if not slide.IsA(UsdPhysics.PrismaticJoint):
        raise _Refusal(f"{slide.GetPath()} is not a prismatic joint")
    value = _number(ratio, "ratio")
    if not value:
        raise _Refusal("ratio must not be 0")
    return _coupling(stage, "create_rack_and_pinion_joint", RACK_TYPE,
                     [("hinge", turn, "Hinge"), ("prismatic", slide, "Prismatic")], "ratio", value, name, undo_stack,
                     dry_run, f"A rack and pinion: every {value} degrees {turn.GetName()} turns, {slide.GetName()} "
                              "slides one unit.")


@_guard
def set_joint_properties(stage: Any, joint_path: str, *, break_force: Any = None, break_torque: Any = None,
                         joint_friction: Any = None, max_joint_velocity: Any = None, collision_enabled: Any = None,
                         enabled: Any = None, exclude_from_articulation: Any = None, undo_stack: Any = None,
                         dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False set) a joint's settings: break_force/break_torque (it snaps when pulled
    harder), joint_friction and max_joint_velocity (PhysX), collision_enabled (let the two bodies touch),
    enabled, exclude_from_articulation."""
    _ready(stage)
    joint = _prim(stage, joint_path, "joint_path")
    if not joint.IsA(UsdPhysics.Joint) and joint.GetTypeName() not in (GEAR_TYPE, RACK_TYPE):
        raise _Refusal(f"{joint.GetPath()} is not a joint")
    changes: list[tuple[str, Any, Any, str | None]] = []
    for attr, value, kind in (("physics:breakForce", break_force, "force"), ("physics:breakTorque", break_torque, "force"),
                              ("physxJoint:jointFriction", joint_friction, "force"),
                              ("physxJoint:maxJointVelocity", max_joint_velocity, "force")):
        number = _number(value, attr.split(":")[-1], low=0)
        if number is not None:
            changes.append((attr, Sdf.ValueTypeNames.Float, number, JOINT_API if attr.startswith("physx") else None))
    for attr, value in (("physics:collisionEnabled", collision_enabled), ("physics:jointEnabled", enabled),
                        ("physics:excludeFromArticulation", exclude_from_articulation)):
        if value is None:
            continue
        if not isinstance(value, bool):
            raise _Refusal(f"{attr.split(':')[-1]} must be true or false")
        changes.append((attr, Sdf.ValueTypeNames.Bool, value, None))
    if not changes:
        raise _Refusal("Give at least one setting to change")
    plan = {"joint_path": str(joint.GetPath()), "set": {attr: value for attr, _, value, _ in changes},
            "before": {attr: (joint.GetAttribute(attr).Get() if joint.GetAttribute(attr) else None)
                       for attr, _, _, _ in changes}}
    notes = []
    if break_force is not None or break_torque is not None:
        notes.append("Break force/torque are in the stage's force units; the joint snaps (switches off) above them.")
    if joint_friction is not None or max_joint_velocity is not None:
        notes.append("joint_friction and max_joint_velocity are PhysX settings (PhysxJointAPI).")
    common = {"plan": plan, "warnings": [], "explanation": " ".join(notes) or "Joint settings updated."}
    if dry_run is not False:
        return _result("set_joint_properties", next_step="Run again with dry_run=False to set them (undoable).", **common)

    def mutate() -> None:
        for attr, value_type, value, api in changes:
            if api:
                _add_api(joint, api)
            joint.CreateAttribute(attr, value_type).Set(value)

    def verify() -> str | None:
        return None

    return _apply(stage, "set_joint_properties", [], [joint.GetPath()], mutate, verify, undo_stack,
                  f"Set properties of {joint.GetName()}", common)


@_guard
def set_distance_spring(stage: Any, joint_path: str, *, stiffness: Any = None, damping: Any = None,
                        enabled: Any = True, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False set) the spring of a distance joint (PhysX): with it enabled the distance
    stretches like a spring instead of a rope. stiffness pulls back, damping calms the bounce."""
    _ready(stage)
    joint = _prim(stage, joint_path, "joint_path")
    if not joint.IsA(UsdPhysics.DistanceJoint):
        raise _Refusal(f"{joint.GetPath()} is not a distance joint")
    if not isinstance(enabled, bool):
        raise _Refusal("enabled must be true or false")
    stiff, damp = _number(stiffness, "stiffness", low=0), _number(damping, "damping", low=0)
    if enabled and not stiff:
        raise _Refusal("A spring needs stiffness > 0 (how hard it pulls back). Start around 100.")
    values = {"springEnabled": enabled, "springStiffness": stiff, "springDamping": damp}
    plan = {"joint_path": str(joint.GetPath()), "spring": {k: v for k, v in values.items() if v is not None}}
    common = {"plan": plan, "warnings": [],
              "explanation": ("The distance joint now acts like a spring." if enabled else "The spring is switched off.")}
    if dry_run is not False:
        return _result("set_distance_spring", next_step="Run again with dry_run=False to set it (undoable).", **common)

    def mutate() -> None:
        _add_api(joint, SPRING_API)
        joint.CreateAttribute("physxPhysicsDistanceJoint:springEnabled", Sdf.ValueTypeNames.Bool).Set(enabled)
        for key, value in (("springStiffness", stiff), ("springDamping", damp)):
            if value is not None:
                joint.CreateAttribute(f"physxPhysicsDistanceJoint:{key}", Sdf.ValueTypeNames.Float).Set(value)

    def verify() -> str | None:
        return None if SPRING_API in _api_names(joint) else "the spring settings were not applied"

    return _apply(stage, "set_distance_spring", [], [joint.GetPath()], mutate, verify, undo_stack,
                  f"Set spring on {joint.GetName()}", common)


# ------------------------------------------------------------------------------------------------ python snippets
def _joint_python(plan: dict[str, Any], key: str) -> str:
    lines = [
        "# The same joint in Python (Script Editor). Values come from the preview above.",
        "import omni.usd",
        "from pxr import Gf, Sdf, UsdPhysics",
        "stage = omni.usd.get_context().get_stage()",
        f"joint = UsdPhysics.{plan['type']}.Define(stage, {plan['joint_path']!r})  # the joint prim",
    ]
    if plan["body0"]:
        lines.append(f"joint.CreateBody0Rel().SetTargets([Sdf.Path({plan['body0']!r})])  # first body")
    lines.append(f"joint.CreateBody1Rel().SetTargets([Sdf.Path({plan['body1']!r})])  # second body")
    lines.append(f"joint.CreateLocalPos0Attr().Set(Gf.Vec3f(*{plan['local_pos0']!r}))  # joint point, seen from body0")
    lines.append(f"joint.CreateLocalRot0Attr().Set(Gf.Quatf(*{plan['local_rot0']!r}))")
    lines.append(f"joint.CreateLocalPos1Attr().Set(Gf.Vec3f(*{plan['local_pos1']!r}))  # joint point, seen from body1")
    lines.append(f"joint.CreateLocalRot1Attr().Set(Gf.Quatf(*{plan['local_rot1']!r}))")
    if plan["axis"]:
        lines.append(f"joint.CreateAxisAttr().Set({plan['axis']!r})  # direction it turns or slides")
    names = {"lower": "LowerLimit", "upper": "UpperLimit", "cone0": "ConeAngle0Limit", "cone1": "ConeAngle1Limit",
             "min_distance": "MinDistance", "max_distance": "MaxDistance"}
    for limit, value in plan["limits"].items():
        lines.append(f"joint.Create{names[limit]}Attr().Set({value!r})")
    for axis, state in plan.get("d6_axes", {}).items():
        if state == "free":
            continue
        low, high = (1.0, -1.0) if state == "locked" else state
        note = "  # low > high locks this axis" if state == "locked" else "  # limited range"
        lines.append(f"limit = UsdPhysics.LimitAPI.Apply(joint.GetPrim(), {axis!r}){note}")
        lines.append(f"limit.CreateLowAttr().Set({low!r}); limit.CreateHighAttr().Set({high!r})")
    return "\n".join(lines)


def _drive_python(plan: dict[str, Any]) -> str:
    lines = [
        "# The same drive in Python (Script Editor).",
        "import omni.usd",
        "from pxr import UsdPhysics",
        "stage = omni.usd.get_context().get_stage()",
        f"joint = stage.GetPrimAtPath({plan['joint_path']!r})",
        f"drive = UsdPhysics.DriveAPI.Apply(joint, {plan['drive']!r})  # angular for turning, linear for sliding",
    ]
    for name, value in plan["values"].items():
        attr = name[0].upper() + name[1:]
        lines.append(f"drive.Create{attr}Attr().Set({value!r})")
    return "\n".join(lines)
