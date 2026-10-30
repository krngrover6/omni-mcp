"""Physics setup with UsdPhysics: a read-only report plus dry-run-by-default scene, rigid body and collider edits.

physics_report lists the PhysicsScene prims (with their effective gravity), rigid bodies, colliders, mass,
mesh collision approximations, articulation roots and joints, and warns about common setup mistakes.
add_physics_scene, make_rigid_body, make_collider and remove_physics first plan every change by reading the
stage; only dry_run=False writes it, into the stage's current edit target, with plain USD calls.

Units: gravity is given in m/s^2 and mass/density in kg and kg/m^3; they are converted to stage units with the
stage's metersPerUnit and kilogramsPerUnit (UsdPhysics stores distance and mass in stage units).

Imports only pxr, the standard library and this package's pure-pxr helpers, and uses only UsdPhysics calls
present in both USD 25.11 and usd-core 26.x, so the same functions run on temp stages in tests and on the live
stage inside Kit / Isaac Sim (where the edits do not go on Kit's undo stack).
"""

from __future__ import annotations

import math
from typing import Any

from .authoring import (
    _check_edit_target,
    _check_stage,
    _creation_target,
    _dry,
    _is_number,
    _items,
    _number,
    _prim_path,
    _Refusal,
    _require_pxr,
    _stronger_opinion,
    _text,
)
from .edits import _bool, _existing_prim, _result
from .usd_insight import to_jsonable

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = UsdPhysics = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

EARTH_GRAVITY = 9.81  # m/s^2
DEFAULT_GRAVITY = (0.0, 0.0, -EARTH_GRAVITY)  # m/s^2 in a Z-up frame; rotated onto the stage up axis
APPROXIMATIONS = ("convexHull", "convexDecomposition", "boundingCube", "boundingSphere", "meshSimplification", "none")
# Applied API schema name -> UsdPhysics class name. These are the only APIs this module adds or removes.
MODULE_APIS = {
    "PhysicsRigidBodyAPI": "RigidBodyAPI",
    "PhysicsCollisionAPI": "CollisionAPI",
    "PhysicsMeshCollisionAPI": "MeshCollisionAPI",
    "PhysicsMassAPI": "MassAPI",
}
REPORTED_APIS = tuple(MODULE_APIS) + ("PhysicsArticulationRootAPI",)
MAX_SCAN = 20_000  # prims visited by one report or one scene search
MAX_REPORT_PRIMS = 500  # physics prims listed in a report
MAX_REPORT_JOINTS = 200
MAX_EDIT_PRIMS = 1_000  # prims one edit may touch (colliders on descendants, remove_physics with descendants)
HUGE_MESH_POINTS = 50_000  # a triangle-mesh collider (approximation none) above this is expensive
_UNEXPECTED = "Unexpected error while planning (nothing was written): {}"
_REL_TOLERANCE = 1e-6


# --- physics_report ---------------------------------------------------------------------------------------


def physics_report(stage: Any, prim_path: str | None = None) -> dict[str, Any]:
    """Read-only summary of the stage's physics setup (or one subtree when prim_path is given).

    Lists PhysicsScene prims with authored and effective gravity, every prim with a rigid body, collider, mass,
    mesh collision or articulation root API (instance proxies included and flagged), and joints with their
    bodies. Warns about a missing PhysicsScene, rigid bodies without colliders, nested rigid bodies, dynamic
    bodies with triangle-mesh colliders, huge meshes without a collision approximation and broken joints.
    At most MAX_SCAN prims are visited and MAX_REPORT_PRIMS listed; the result says when it was truncated.
    """
    action = "physics_report"
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {"read_only": True, "root": _text(prim_path) if isinstance(prim_path, str) else "/"}
    try:
        _require_pxr()
        _check_stage(stage)
        if prim_path is None or (isinstance(prim_path, str) and prim_path.strip() in ("", "/")):
            root = stage.GetPseudoRoot()
        else:
            root = stage.GetPrimAtPath(_prim_path(prim_path, "prim_path"))
            if not root or not root.IsValid():
                raise _Refusal(f"Prim does not exist: {prim_path.strip()}")
        fields["root"] = str(root.GetPath())
        units = _units(stage, notes)
        fields["stage"] = units

        scenes, scenes_truncated = _find_scenes(stage)
        scene_entries = [_scene_entry(scene, units) for scene in scenes]
        fields["physics_scenes"] = scene_entries
        fields["has_physics_scene"] = bool(scene_entries)
        fields["gravity"] = scene_entries[0]["effective_gravity"] if scene_entries else None
        if scenes_truncated:
            notes.append(f"The PhysicsScene search stopped after {MAX_SCAN} prims; a scene further down may be missed.")
        if len(scene_entries) > 1:
            warnings.append(
                f"{len(scene_entries)} PhysicsScene prims: bodies without simulationOwner go to the first one the "
                "simulator finds; keep one scene unless you assign owners deliberately."
            )

        predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
        prims: list[dict[str, Any]] = []
        joints: list[dict[str, Any]] = []
        rigid_paths: list[Any] = []
        collider_paths: list[Any] = []
        counts = {"rigid_bodies": 0, "colliders": 0, "mass": 0, "mesh_collision": 0, "articulation_roots": 0, "joints": 0}
        articulation_roots: list[str] = []
        scanned = 0
        truncated = False
        listed_truncated = 0
        for prim in Usd.PrimRange(root, predicate):
            if prim.IsPseudoRoot():
                continue
            scanned += 1
            if scanned > MAX_SCAN:
                truncated = True
                scanned -= 1
                break
            if prim.IsA(UsdPhysics.Joint):
                counts["joints"] += 1
                if len(joints) < MAX_REPORT_JOINTS:
                    joints.append(_joint_entry(stage, prim, warnings))
                continue
            applied = [name for name in REPORTED_APIS if _has_api(prim, name)]
            if not applied:
                continue
            entry = _prim_entry(prim, applied)
            if "PhysicsRigidBodyAPI" in applied:
                counts["rigid_bodies"] += 1
                rigid_paths.append(prim)
            if "PhysicsCollisionAPI" in applied:
                counts["colliders"] += 1
                collider_paths.append(prim)
            if "PhysicsMassAPI" in applied:
                counts["mass"] += 1
            if "PhysicsMeshCollisionAPI" in applied:
                counts["mesh_collision"] += 1
            if "PhysicsArticulationRootAPI" in applied:
                counts["articulation_roots"] += 1
                articulation_roots.append(str(prim.GetPath()))
            if len(prims) < MAX_REPORT_PRIMS:
                prims.append(entry)
            else:
                listed_truncated += 1

        _report_warnings(rigid_paths, collider_paths, warnings, notes)
        has_content = any(counts[key] for key in ("rigid_bodies", "colliders", "joints", "articulation_roots"))
        if not scene_entries:
            message = "No PhysicsScene prim on the stage: nothing simulates until one exists (add_physics_scene)."
            (warnings if has_content else notes).append(message)
        if not has_content:
            notes.append("No rigid bodies, colliders, joints or articulation roots were found.")
        fields.update(
            prims=prims,
            joints=joints,
            articulation_roots=articulation_roots[:MAX_REPORT_PRIMS],
            counts=counts,
            scanned=scanned,
            truncated=truncated,
        )
        if truncated:
            warnings.append(f"Stopped after {MAX_SCAN} prims; pass prim_path to report a smaller subtree.")
        if listed_truncated:
            fields["prims_truncated"] = listed_truncated
        if counts["joints"] > len(joints):
            fields["joints_truncated"] = counts["joints"] - len(joints)
    except _Refusal as refusal:
        return _report_result(action, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # the report only reads the stage
        return _report_result(action, stage, warnings, notes, fields, error=f"Unexpected error while reading the stage: {exc}")
    return _report_result(action, stage, warnings, notes, fields)


# --- add_physics_scene ------------------------------------------------------------------------------------


def add_physics_scene(
    stage: Any,
    path: str = "/physicsScene",
    *,
    gravity: Any = DEFAULT_GRAVITY,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a UsdPhysics.Scene with explicit gravity.

    gravity is in m/s^2: either a number (the magnitude, pointing down the stage up axis) or an (x, y, z) vector
    written for a Z-up world, which is rotated onto the stage up axis (on a Y-up stage (0, 0, -9.81) becomes
    (0, -9.81, 0)). The magnitude is divided by the stage metersPerUnit (9.81 m/s^2 is 981 on a centimeter
    stage). An existing PhysicsScene at path gets its gravity updated; any other existing prim is refused.
    """
    action = "add_physics_scene"
    dry = _dry(dry_run)
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {"path": _text(path) if isinstance(path, str) else None}
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        sdf_path = _prim_path(path, "path")
        fields["path"] = str(sdf_path)
        units = _units(stage, notes)
        plan_gravity = _gravity(gravity, units)
        fields["gravity"] = plan_gravity
        fields["stage"] = units
        down = [0.0, -1.0, 0.0] if units["up_axis"] == "Y" else [0.0, 0.0, -1.0]
        along = sum(a * b for a, b in zip(plan_gravity["direction"], down))
        if plan_gravity["magnitude_m_per_s2"] and along < 1.0 - 1e-6:
            warnings.append(
                f"Gravity points along {plan_gravity['direction']}, not straight down the stage's {units['up_axis']} "
                "up axis. A gravity vector is read in a Z-up frame (down is [0, 0, -9.81]) and turned onto the stage "
                "up axis; pass a single number for plain downward gravity."
            )

        ops: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        existing = stage.GetPrimAtPath(sdf_path)
        if existing and existing.IsValid():
            if existing.IsInstanceProxy() or existing.IsInPrototype():
                raise _Refusal(f"{sdf_path} is inside an instance and cannot be edited here.")
            if not existing.IsA(UsdPhysics.Scene):
                raise _Refusal(
                    f"A {existing.GetTypeName() or 'typeless'} prim already exists at {sdf_path}; choose a new path "
                    "for the PhysicsScene."
                )
            prim = existing
            notes.append(f"{sdf_path} is already a PhysicsScene; only its gravity is updated.")
        else:
            _creation_target(stage, str(sdf_path), warnings)
            prim = None
            ops.append({"kind": "prim", "op": "define", "path": str(sdf_path), "type": "PhysicsScene"})

        direction = Gf.Vec3f(*plan_gravity["direction"])
        magnitude = float(plan_gravity["magnitude_stage_units"])
        for name, value, plain in (
            ("physics:gravityDirection", direction, plan_gravity["direction"]),
            ("physics:gravityMagnitude", magnitude, plan_gravity["magnitude_stage_units"]),
        ):
            _plan_attribute(stage, prim, sdf_path, name, value, plain, ops, unchanged, warnings)

        others = [str(scene.GetPath()) for scene in _find_scenes(stage)[0] if scene.GetPath() != sdf_path]
        if others:
            warnings.append(
                f"The stage already has PhysicsScene prim(s) {', '.join(others[:5])}; a second scene splits the "
                "simulation unless bodies set simulationOwner. Usually one scene is enough."
            )
    except _Refusal as refusal:
        return _result(action, dry, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _result(action, dry, stage, warnings, notes, fields, error=_UNEXPECTED.format(exc))
    return _execute(action, dry, stage, ops, unchanged, warnings, notes, fields)


# --- make_rigid_body / make_collider ----------------------------------------------------------------------


def make_rigid_body(
    stage: Any,
    prim_path: str,
    *,
    mass: Any = None,
    density: Any = None,
    kinematic: Any = False,
    add_collider: Any = True,
    approximation: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or apply RigidBodyAPI (plus optional MassAPI and colliders) to a prim.

    mass is in kg and density in kg/m^3 (converted with kilogramsPerUnit and metersPerUnit); when both are given
    mass wins. kinematic=True authors physics:kinematicEnabled (an animated body that pushes others but is not
    pushed). add_collider=True applies CollisionAPI to the prim if it is a gprim, otherwise to every gprim below
    it; approximation (convexHull, convexDecomposition, boundingCube, boundingSphere, meshSimplification or none)
    is authored with MeshCollisionAPI on the meshes among them.
    """
    action = "make_rigid_body"
    dry = _dry(dry_run)
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {"path": _text(prim_path) if isinstance(prim_path, str) else None}
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        prim = _edit_prim(stage, prim_path, warnings)
        fields["path"] = str(prim.GetPath())
        kinematic_flag = _bool(kinematic, "kinematic")
        collider_flag = _bool(add_collider, "add_collider")
        approx = _approximation(approximation)
        mass_kg = None if mass is None else _number(mass, "mass", minimum=0.0, exclusive=True)
        density_si = None if density is None else _number(density, "density", minimum=0.0, exclusive=True)
        units = _units(stage, notes)
        fields["stage"] = units

        ops: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        path = prim.GetPath()
        _plan_api(prim, "PhysicsRigidBodyAPI", ops, unchanged)
        kin_attr = prim.GetAttribute("physics:kinematicEnabled")
        current_kinematic = bool(kin_attr.Get()) if kin_attr and kin_attr.HasAuthoredValue() else False
        if kinematic_flag or current_kinematic:
            _plan_attribute(stage, prim, path, "physics:kinematicEnabled", kinematic_flag, kinematic_flag, ops, unchanged, warnings)
        enabled = prim.GetAttribute("physics:rigidBodyEnabled")
        if enabled and enabled.HasAuthoredValue() and enabled.Get() is False:
            warnings.append(f"{path} has physics:rigidBodyEnabled = false, so the body stays disabled.")

        mass_info: dict[str, Any] = {}
        if mass_kg is not None or density_si is not None:
            _plan_api(prim, "PhysicsMassAPI", ops, unchanged)
            if mass_kg is not None:
                value = mass_kg / units["kilograms_per_unit"]
                mass_info["mass_kg"] = _round(mass_kg)
                mass_info["mass_stage_units"] = _round(value)
                _plan_attribute(stage, prim, path, "physics:mass", value, _round(value), ops, unchanged, warnings)
            if density_si is not None:
                value = density_si * units["meters_per_unit"] ** 3 / units["kilograms_per_unit"]
                mass_info["density_kg_per_m3"] = _round(density_si)
                mass_info["density_stage_units"] = _round(value)
                _plan_attribute(stage, prim, path, "physics:density", value, _round(value), ops, unchanged, warnings)
            if mass_kg is not None and density_si is not None:
                warnings.append("Both mass and density were given: UsdPhysics uses mass and ignores density on the same prim.")
        fields["mass"] = mass_info or None

        colliders: list[str] = []
        if collider_flag:
            colliders = _plan_colliders(stage, prim, approx, ops, unchanged, warnings, notes, require=False)
        elif approx is not None:
            warnings.append("approximation was ignored because add_collider is false.")
        fields["collider_prims"] = colliders

        _nesting_warnings(prim, warnings)
        if not _subtree_has_collider(prim) and not colliders:
            warnings.append(
                f"Rigid body {path} has no collider on itself or below it, so it falls through everything; pass "
                "add_collider=True or call scene_make_collider on its geometry."
            )
        if not kinematic_flag:
            _dynamic_triangle_mesh_warnings(prim, approx if collider_flag else None, warnings, will_collide=collider_flag)
        if not _find_scenes(stage)[0]:
            warnings.append("The stage has no PhysicsScene yet: nothing simulates until one exists (add_physics_scene).")
    except _Refusal as refusal:
        return _result(action, dry, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _result(action, dry, stage, warnings, notes, fields, error=_UNEXPECTED.format(exc))
    return _execute(action, dry, stage, ops, unchanged, warnings, notes, fields)


def make_collider(stage: Any, prim_path: str, *, approximation: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or apply CollisionAPI to a gprim, or to every gprim below a group prim.

    approximation (convexHull, convexDecomposition, boundingCube, boundingSphere, meshSimplification or none)
    applies MeshCollisionAPI and sets physics:approximation on the meshes; implicit shapes (Cube, Sphere,
    Capsule, ...) always collide with their exact shape. A collider without a rigid body above it is static.
    """
    action = "make_collider"
    dry = _dry(dry_run)
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {"path": _text(prim_path) if isinstance(prim_path, str) else None}
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        prim = _edit_prim(stage, prim_path, warnings)
        fields["path"] = str(prim.GetPath())
        approx = _approximation(approximation)
        ops: list[dict[str, Any]] = []
        unchanged: list[dict[str, Any]] = []
        fields["collider_prims"] = _plan_colliders(stage, prim, approx, ops, unchanged, warnings, notes, require=True)
        body = _rigid_body_owner(prim)
        fields["rigid_body"] = str(body.GetPath()) if body is not None else None
        if body is None:
            notes.append("No rigid body above this prim, so the collider is static (it blocks but never moves).")
        elif not _kinematic(body):
            _dynamic_triangle_mesh_warnings(prim, approx, warnings, will_collide=True)
    except _Refusal as refusal:
        return _result(action, dry, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _result(action, dry, stage, warnings, notes, fields, error=_UNEXPECTED.format(exc))
    return _execute(action, dry, stage, ops, unchanged, warnings, notes, fields)


# --- remove_physics ---------------------------------------------------------------------------------------


def remove_physics(stage: Any, prim_path: str, *, include_descendants: Any = False, dry_run: bool = True) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or remove the RigidBody, Collision, MeshCollision and Mass APIs.

    When the edit target layer is the only layer applying an API, its name is taken out of that layer's
    apiSchemas list op and the API's physics:* attribute specs in that layer are removed, so the prim looks as
    if the API was never applied. When another layer (a sublayer or reference) applies it, Usd.Prim.RemoveAPI
    authors a 'delete apiSchemas' entry instead; a stronger layer's opinion still wins over it. ArticulationRootAPI
    and joints are left alone. include_descendants=True also clears every prim below prim_path.
    """
    action = "remove_physics"
    dry = _dry(dry_run)
    warnings: list[str] = []
    notes: list[str] = []
    fields: dict[str, Any] = {"path": _text(prim_path) if isinstance(prim_path, str) else None}
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        prim = _edit_prim(stage, prim_path, warnings)
        fields["path"] = str(prim.GetPath())
        descendants = _bool(include_descendants, "include_descendants")
        targets = [prim]
        if descendants:
            targets = [item for item in Usd.PrimRange(prim) if item.IsA(UsdGeom.Imageable)]
            if len(targets) > MAX_EDIT_PRIMS:
                raise _Refusal(
                    f"{prim.GetPath()} has {len(targets)} imageable prims (more than {MAX_EDIT_PRIMS}); remove physics "
                    "from smaller subtrees."
                )
        ops: list[dict[str, Any]] = []
        for target in targets:
            _plan_removals(stage, target, ops, warnings, notes)
        if _has_api(prim, "PhysicsArticulationRootAPI"):
            notes.append(f"{prim.GetPath()} keeps its PhysicsArticulationRootAPI (this module does not add or remove it).")
        if not descendants:
            below = [str(item.GetPath()) for item in _subtree(prim, proxies=False) if _module_apis(item)]
            if below:
                notes.append(
                    f"{len(below)} prim(s) below also have physics APIs (for example {below[0]}); pass "
                    "include_descendants=True to remove those too."
                )
        proxies = [str(item.GetPath()) for item in _subtree(prim, proxies=True) if item.IsInstanceProxy() and _module_apis(item)]
        if proxies:
            warnings.append(
                f"{len(proxies)} instance proxy prim(s) such as {proxies[0]} have physics APIs from their prototype; "
                "they cannot be edited here. Edit the prototype's source asset."
            )
        if not ops:
            notes.append("Nothing to remove: no RigidBody, Collision, MeshCollision or Mass API is applied here.")
    except _Refusal as refusal:
        return _result(action, dry, stage, warnings, notes, fields, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _result(action, dry, stage, warnings, notes, fields, error=_UNEXPECTED.format(exc))
    return _execute(action, dry, stage, ops, [], warnings, notes, fields)


# --- planning helpers -------------------------------------------------------------------------------------


def _edit_prim(stage: Any, prim_path: Any, warnings: list[str]) -> Any:
    """An existing, editable, imageable prim (instance proxies and prototype prims are refused)."""
    prim = _existing_prim(stage, prim_path, "prim_path")
    if not prim.IsA(UsdGeom.Imageable):
        raise _Refusal(
            f"{prim.GetPath()} is a {prim.GetTypeName() or 'typeless'} prim, not imageable geometry or a transform "
            "group, so physics APIs on it have no effect."
        )
    if not prim.IsActive():
        warnings.append(f"{prim.GetPath()} is inactive, so its physics has no effect until it is activated.")
    if not prim.IsLoaded():
        warnings.append(f"{prim.GetPath()} is an unloaded payload; geometry inside it is not visible to this edit.")
    if prim.IsInstance():
        warnings.append(
            f"{prim.GetPath()} is an instance: its children are instance proxies from a shared prototype, so colliders "
            "for that geometry must be authored in the prototype's source asset."
        )
    return prim


def _approximation(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        wanted = value.strip().lower()
        for name in APPROXIMATIONS:
            if name.lower() == wanted:
                return name
    raise _Refusal(f"approximation must be one of {', '.join(APPROXIMATIONS)} (or None), got {value!r}")


def _units(stage: Any, notes: list[str]) -> dict[str, Any]:
    up_axis = str(UsdGeom.GetStageUpAxis(stage))
    meters = float(UsdGeom.GetStageMetersPerUnit(stage))
    kilograms = float(UsdPhysics.GetStageKilogramsPerUnit(stage))
    if not stage.HasAuthoredMetadata("upAxis"):
        notes.append(f"The stage has no authored upAxis; USD's fallback {up_axis} is used.")
    if not UsdGeom.StageHasAuthoredMetersPerUnit(stage):
        notes.append(f"The stage has no authored metersPerUnit; USD's fallback {meters:g} (centimeters) is used.")
    if up_axis not in ("Y", "Z"):
        raise _Refusal(f"The stage up axis {up_axis!r} is not Y or Z.")
    if not (math.isfinite(meters) and meters > 0.0):
        raise _Refusal(f"The stage metersPerUnit {meters!r} is not a positive number.")
    if not (math.isfinite(kilograms) and kilograms > 0.0):
        raise _Refusal(f"The stage kilogramsPerUnit {kilograms!r} is not a positive number.")
    return {"up_axis": up_axis, "meters_per_unit": meters, "kilograms_per_unit": kilograms}


def _gravity(value: Any, units: dict[str, Any]) -> dict[str, Any]:
    """Gravity in m/s^2 (a number along -up, or a Z-up-frame vector) -> stage direction and stage-unit magnitude."""
    up = units["up_axis"]
    if _is_number(value):
        acceleration = _number(value, "gravity", minimum=0.0)
        vector = (0.0, 0.0, -acceleration)
    else:
        if value is None:
            raise _Refusal("gravity must be a number (m/s^2 along -up) or an (x, y, z) vector in m/s^2")
        items = _items(value, 3, "gravity")
        vector = tuple(_number(item, "gravity") for item in items)
    stage_vector = vector if up == "Z" else (vector[0], vector[2], -vector[1])  # Z-up frame -> Y-up frame
    length = math.sqrt(sum(component * component for component in stage_vector))
    if length > 0.0:
        direction = [component / length for component in stage_vector]
    else:
        direction = [0.0, -1.0, 0.0] if up == "Y" else [0.0, 0.0, -1.0]
    magnitude = length / units["meters_per_unit"]
    return {
        "input_m_per_s2": [_round(component) for component in vector],
        "stage_vector_m_per_s2": [_round(component) for component in stage_vector],
        "direction": [_round(component) for component in direction],
        "magnitude_m_per_s2": _round(length),
        "magnitude_stage_units": _round(magnitude),
        "up_axis": up,
        "meters_per_unit": units["meters_per_unit"],
    }


def _plan_api(prim: Any, name: str, ops: list[dict[str, Any]], unchanged: list[dict[str, Any]]) -> None:
    entry = {"kind": "api", "path": str(prim.GetPath()), "name": name}
    if _has_api(prim, name):
        unchanged.append({**entry, "reason": "already applied"})
    elif not any(op["kind"] == "api" and op["path"] == entry["path"] and op["name"] == name for op in ops):
        ops.append({**entry, "op": "apply"})


def _plan_attribute(
    stage: Any,
    prim: Any,
    path: Any,
    name: str,
    value: Any,
    plain: Any,
    ops: list[dict[str, Any]],
    unchanged: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    attr = prim.GetAttribute(name) if prim is not None else None
    old = attr.Get() if attr and attr.HasAuthoredValue() else None
    entry = {"kind": "attribute", "path": str(path), "name": name}
    if old is not None and _same(old, value):
        unchanged.append({**entry, "value": to_jsonable(plain), "reason": "already set"})
        return
    ops.append({**entry, "op": "set", "old": _plain(old), "new": to_jsonable(plain), "_value": value})
    if attr:
        if attr.GetNumTimeSamples() > 0:
            warnings.append(f"{path}.{name} has time samples, which win over the default value this edit writes.")
        stronger = _stronger_opinion(stage, prim, name)
        if stronger:
            warnings.append(
                f"{path}.{name} has a stronger opinion in {_text(stronger)} than the edit target, so this edit would "
                "be hidden by it."
            )


def _plan_colliders(
    stage: Any,
    prim: Any,
    approximation: str | None,
    ops: list[dict[str, Any]],
    unchanged: list[dict[str, Any]],
    warnings: list[str],
    notes: list[str],
    *,
    require: bool,
) -> list[str]:
    if prim.IsA(UsdGeom.Gprim):
        targets = [prim]
    else:
        targets = [item for item in Usd.PrimRange(prim) if item != prim and item.IsA(UsdGeom.Gprim)]
    instances = [item for item in Usd.PrimRange(prim) if item != prim and item.IsInstance()]
    if prim.IsInstance():
        instances.insert(0, prim)
    if instances:
        warnings.append(
            f"{len(instances)} instance prim(s) such as {instances[0].GetPath()} were skipped: their geometry lives in "
            "a shared prototype, so colliders for it must be authored in the prototype's source asset."
        )
    if not targets:
        message = (
            f"{prim.GetPath()} is a {prim.GetTypeName() or 'typeless'} prim with no geometry (gprims) on or below it, "
            "so there is nothing to collide with."
        )
        if require:
            raise _Refusal(message)
        warnings.append(message + " No collider was planned.")
        return []
    if len(targets) > MAX_EDIT_PRIMS:
        raise _Refusal(
            f"{prim.GetPath()} has {len(targets)} gprims below it (more than {MAX_EDIT_PRIMS}); add colliders to "
            "smaller subtrees."
        )
    if len(targets) > 1 or targets[0] != prim:
        notes.append(f"Colliders go on the {len(targets)} gprim(s) below {prim.GetPath()} (CollisionAPI only works on geometry).")
    implicit: list[str] = []
    for target in targets:
        _plan_api(target, "PhysicsCollisionAPI", ops, unchanged)
        enabled = target.GetAttribute("physics:collisionEnabled")
        if enabled and enabled.HasAuthoredValue() and enabled.Get() is False:
            warnings.append(f"{target.GetPath()} has physics:collisionEnabled = false, so it does not collide.")
        if not target.IsA(UsdGeom.Mesh):
            implicit.append(target.GetTypeName())
            continue
        if approximation is not None:
            _plan_api(target, "PhysicsMeshCollisionAPI", ops, unchanged)
            _plan_attribute(
                stage, target, target.GetPath(), "physics:approximation", approximation, approximation, ops, unchanged, warnings
            )
        _huge_mesh_warning(target, approximation, warnings)
    if approximation is not None and implicit:
        kinds = ", ".join(sorted(set(implicit)))
        notes.append(f"approximation applies only to meshes; the {kinds} prim(s) collide with their exact shape.")
    return [str(target.GetPath()) for target in targets]


def _plan_removals(stage: Any, prim: Any, ops: list[dict[str, Any]], warnings: list[str], notes: list[str]) -> None:
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    spec_path = edit_target.MapToSpecPath(prim.GetPath())
    spec = layer.GetPrimAtPath(spec_path) if spec_path and not spec_path.isEmpty else None
    edit_items = _added_api_names(spec) if spec else set()
    built_in = _built_in_apis(prim)
    order = [item.identifier for item in stage.GetLayerStack(True)]
    for name in _module_apis(prim):
        entry = {"kind": "api", "op": "remove", "path": str(prim.GetPath()), "name": name}
        if name in built_in:
            warnings.append(f"{name} on {prim.GetPath()} is built into the {prim.GetTypeName()} type and cannot be removed.")
            continue
        others = [
            other
            for other in prim.GetPrimStack()
            if not (other.layer.identifier == layer.identifier and other.path == spec_path) and name in _added_api_names(other)
        ]
        if name in edit_items and not others and not _legacy_list_op(spec):
            entry["method"] = "list_op_edit"
            entry["source"] = "edit target layer"
            ops.append(entry)
            for attr_name in _schema_attribute_names(name):
                if layer.GetPropertyAtPath(spec_path.AppendProperty(attr_name)):
                    ops.append(
                        {"kind": "attribute", "op": "remove_spec", "path": str(prim.GetPath()), "name": attr_name}
                    )
            continue
        entry["method"] = "remove_api"
        entry["source"] = ", ".join(sorted({_text(other.layer.GetDisplayName() or other.layer.identifier) for other in others}))
        ops.append(entry)
        notes.append(
            f"{name} on {prim.GetPath()} is applied by another layer ({entry['source']}), so the edit target gets a "
            "'delete apiSchemas' entry; its physics:* attribute values are left in place."
        )
        stronger = [
            other.layer.identifier
            for other in others
            if other.layer.identifier in order
            and layer.identifier in order
            and order.index(other.layer.identifier) < order.index(layer.identifier)
        ]
        if stronger:
            warnings.append(
                f"{name} on {prim.GetPath()} is applied in the stronger layer {_text(stronger[0])}; a delete in the "
                "edit target cannot override it. Edit that layer instead."
            )


def _added_api_names(spec: Any) -> set[str]:
    try:
        list_op = spec.GetInfo("apiSchemas") if spec.HasInfo("apiSchemas") else None
    except Exception:
        return set()
    if list_op is None:
        return set()
    names: set[str] = set()
    for attr in ("explicitItems", "prependedItems", "appendedItems", "addedItems"):
        names.update(str(item) for item in (getattr(list_op, attr, None) or []))
    return names


def _legacy_list_op(spec: Any) -> bool:
    """True when the apiSchemas list op uses the deprecated added/ordered items, which _remove_from_list_op skips."""
    try:
        list_op = spec.GetInfo("apiSchemas")
        return bool(getattr(list_op, "addedItems", None) or getattr(list_op, "orderedItems", None))
    except Exception:
        return True


def _remove_from_list_op(stage: Any, prim: Any, name: str) -> None:
    edit_target = stage.GetEditTarget()
    spec = edit_target.GetLayer().GetPrimAtPath(edit_target.MapToSpecPath(prim.GetPath()))
    list_op = spec.GetInfo("apiSchemas")
    keep = lambda items: [item for item in (items or []) if str(item) != name]  # noqa: E731
    if list_op.isExplicit:
        new_op = Sdf.TokenListOp.CreateExplicit(keep(list_op.explicitItems))
    else:
        new_op = Sdf.TokenListOp.Create(
            prependedItems=keep(list_op.prependedItems),
            appendedItems=keep(list_op.appendedItems),
            deletedItems=list(list_op.deletedItems or []),
        )
    empty = not new_op.isExplicit and not (new_op.prependedItems or new_op.appendedItems or new_op.deletedItems)
    if empty:
        spec.ClearInfo("apiSchemas")
    else:
        spec.SetInfo("apiSchemas", new_op)


def _built_in_apis(prim: Any) -> set[str]:
    try:
        definition = Usd.SchemaRegistry().FindConcretePrimDefinition(prim.GetTypeName())
        return {str(name) for name in definition.GetAppliedAPISchemas()} if definition else set()
    except Exception:
        return set()


def _schema_attribute_names(api_name: str) -> list[str]:
    return [str(name) for name in getattr(UsdPhysics, MODULE_APIS[api_name]).GetSchemaAttributeNames(False)]


def _has_api(prim: Any, name: str) -> bool:
    try:
        return name in prim.GetAppliedSchemas()
    except Exception:
        return False


def _module_apis(prim: Any) -> list[str]:
    applied = set(str(name) for name in prim.GetAppliedSchemas())
    return [name for name in MODULE_APIS if name in applied]


def _subtree(prim: Any, *, proxies: bool) -> list[Any]:
    predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate) if proxies else Usd.PrimDefaultPredicate
    found: list[Any] = []
    for item in Usd.PrimRange(prim, predicate):
        if item == prim:
            continue
        found.append(item)
        if len(found) >= MAX_SCAN:
            break
    return found


def _subtree_has_collider(prim: Any) -> bool:
    if _has_api(prim, "PhysicsCollisionAPI"):
        return True
    return any(_has_api(item, "PhysicsCollisionAPI") for item in _subtree(prim, proxies=True))


def _rigid_body_owner(prim: Any) -> Any:
    current = prim
    while current and current.IsValid() and not current.IsPseudoRoot():
        if _has_api(current, "PhysicsRigidBodyAPI"):
            return current
        current = current.GetParent()
    return None


def _kinematic(prim: Any) -> bool:
    attr = prim.GetAttribute("physics:kinematicEnabled")
    return bool(attr.Get()) if attr and attr.IsValid() else False


def _nesting_warnings(prim: Any, warnings: list[str]) -> None:
    parent = prim.GetParent()
    owner = _rigid_body_owner(parent) if parent and parent.IsValid() else None
    if owner is not None:
        warnings.append(
            f"{prim.GetPath()} is inside the rigid body {owner.GetPath()}: nested rigid bodies simulate separately "
            "only if the inner prim sets resetXformStack; otherwise make the outer one the only body or join them."
        )
    nested = [item for item in _subtree(prim, proxies=True) if _has_api(item, "PhysicsRigidBodyAPI")]
    if nested:
        warnings.append(
            f"{len(nested)} rigid bod(ies) below {prim.GetPath()} (for example {nested[0].GetPath()}) would be nested "
            "inside this one; nested rigid bodies are a common setup error."
        )


def _effective_approximation(mesh: Any, planned: str | None) -> str:
    if planned is not None:
        return planned
    attr = mesh.GetAttribute("physics:approximation")
    if _has_api(mesh, "PhysicsMeshCollisionAPI") and attr and attr.IsValid():
        return str(attr.Get())
    return "none"


def _dynamic_triangle_mesh_warnings(prim: Any, planned: str | None, warnings: list[str], *, will_collide: bool) -> None:
    """Warn about mesh colliders (existing, or planned when will_collide) that stay exact triangle meshes."""
    meshes = [prim] if prim.IsA(UsdGeom.Mesh) else [item for item in _subtree(prim, proxies=True) if item.IsA(UsdGeom.Mesh)]
    exact = []
    for mesh in meshes:
        editable = not mesh.IsInstanceProxy()
        if not (_has_api(mesh, "PhysicsCollisionAPI") or (will_collide and editable)):
            continue
        if _effective_approximation(mesh, planned if editable else None) == "none":
            exact.append(str(mesh.GetPath()))
    if exact:
        warnings.append(
            f"{len(exact)} mesh collider(s) such as {exact[0]} use approximation 'none' (the exact triangle mesh) on a "
            "dynamic rigid body; PhysX only simulates triangle meshes on static or kinematic bodies and falls back "
            "to a convex hull. Pass approximation='convexHull' or 'convexDecomposition'."
        )


def _huge_mesh_warning(mesh: Any, planned: str | None, warnings: list[str]) -> None:
    if _effective_approximation(mesh, planned) != "none":
        return
    count = _point_count(mesh)
    if count > HUGE_MESH_POINTS:
        warnings.append(
            f"{mesh.GetPath()} has {count} points and no collision approximation, so it collides as an exact "
            "triangle mesh, which is slow; use approximation='convexDecomposition' or 'meshSimplification'."
        )


def _point_count(mesh: Any) -> int:
    try:
        points = UsdGeom.Mesh(mesh).GetPointsAttr().Get()
        return len(points) if points is not None else 0
    except Exception:
        return 0


def _find_scenes(stage: Any) -> tuple[list[Any], bool]:
    scenes: list[Any] = []
    for index, prim in enumerate(Usd.PrimRange.Stage(stage)):
        if index >= MAX_SCAN:
            return scenes, True
        if prim.IsA(UsdPhysics.Scene):
            scenes.append(prim)
    return scenes, False


# --- report helpers ---------------------------------------------------------------------------------------


def _scene_entry(prim: Any, units: dict[str, Any]) -> dict[str, Any]:
    scene = UsdPhysics.Scene(prim)
    direction = scene.GetGravityDirectionAttr().Get()
    magnitude = scene.GetGravityMagnitudeAttr().Get()
    direction_list = [float(value) for value in direction] if direction is not None else [0.0, 0.0, 0.0]
    if not any(direction_list):
        effective_direction = [0.0, -1.0, 0.0] if units["up_axis"] == "Y" else [0.0, 0.0, -1.0]
        direction_source = "default (down the stage up axis)"
    else:
        length = math.sqrt(sum(value * value for value in direction_list))
        effective_direction = [value / length for value in direction_list]
        direction_source = "authored"
    if magnitude is None or not math.isfinite(float(magnitude)) or float(magnitude) < 0.0:
        effective_magnitude = EARTH_GRAVITY / units["meters_per_unit"]
        magnitude_source = "default (earth gravity in stage units)"
    else:
        effective_magnitude = float(magnitude)
        magnitude_source = "authored"
    return {
        "path": str(prim.GetPath()),
        "gravity_direction": [_round(value) for value in direction_list],
        "gravity_magnitude": _round(float(magnitude)) if magnitude is not None else None,
        "effective_gravity": {
            "direction": [_round(value) for value in effective_direction],
            "direction_source": direction_source,
            "magnitude_stage_units": _round(effective_magnitude),
            "magnitude_m_per_s2": _round(effective_magnitude * units["meters_per_unit"]),
            "magnitude_source": magnitude_source,
        },
    }


def _prim_entry(prim: Any, applied: list[str]) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "path": str(prim.GetPath()),
        "type": prim.GetTypeName() or "",
        "apis": applied,
        "instance_proxy": bool(prim.IsInstanceProxy()),
    }
    if "PhysicsRigidBodyAPI" in applied:
        entry["rigid_body"] = {
            "enabled": _attr_value(prim, "physics:rigidBodyEnabled", True),
            "kinematic": _attr_value(prim, "physics:kinematicEnabled", False),
        }
    if "PhysicsCollisionAPI" in applied:
        collider: dict[str, Any] = {"enabled": _attr_value(prim, "physics:collisionEnabled", True)}
        if prim.IsA(UsdGeom.Mesh):
            collider["approximation"] = _effective_approximation(prim, None)
            collider["points"] = _point_count(prim)
        entry["collider"] = collider
    if "PhysicsMassAPI" in applied:
        entry["mass"] = {"mass": _authored(prim, "physics:mass"), "density": _authored(prim, "physics:density")}
    if "PhysicsArticulationRootAPI" in applied:
        entry["articulation_root"] = True
    return entry


def _joint_entry(stage: Any, prim: Any, warnings: list[str]) -> dict[str, Any]:
    joint = UsdPhysics.Joint(prim)
    bodies = {}
    for key, rel in (("body0", joint.GetBody0Rel()), ("body1", joint.GetBody1Rel())):
        targets = rel.GetTargets() if rel else []
        bodies[key] = str(targets[0]) if targets else None
        for target in targets:
            body = stage.GetPrimAtPath(target)
            if not body or not body.IsValid():
                warnings.append(f"Joint {prim.GetPath()} {key} targets {target}, which does not exist.")
    if bodies["body0"] is None and bodies["body1"] is None:
        warnings.append(f"Joint {prim.GetPath()} has neither body0 nor body1, so it connects nothing.")
    return {
        "path": str(prim.GetPath()),
        "type": prim.GetTypeName() or "",
        **bodies,
        "enabled": _attr_value(prim, "physics:jointEnabled", True),
        "instance_proxy": bool(prim.IsInstanceProxy()),
    }


def _report_warnings(rigid: list[Any], colliders: list[Any], warnings: list[str], notes: list[str]) -> None:
    owned = set()
    static = 0
    for collider in colliders:
        owner = _rigid_body_owner(collider)
        if owner is None:
            static += 1
            continue
        owned.add(owner.GetPath())
        if not _kinematic(owner) and collider.IsA(UsdGeom.Mesh) and _effective_approximation(collider, None) == "none":
            warnings.append(
                f"{collider.GetPath()} is a triangle-mesh collider (approximation none) on the dynamic body "
                f"{owner.GetPath()}; PhysX falls back to a convex hull. Set approximation to convexHull or "
                "convexDecomposition."
            )
    for collider in colliders:
        if collider.IsA(UsdGeom.Mesh) and _effective_approximation(collider, None) == "none":
            count = _point_count(collider)
            if count > HUGE_MESH_POINTS:
                warnings.append(
                    f"{collider.GetPath()} has {count} points and no collision approximation, so it collides as an "
                    "exact triangle mesh, which is slow; set approximation to convexDecomposition or meshSimplification."
                )
    for body in rigid:
        path = body.GetPath()
        if path not in owned and not _subtree_has_collider(body):
            warnings.append(f"Rigid body {path} has no collider on itself or below it, so it falls through everything.")
        parent = body.GetParent()
        outer = _rigid_body_owner(parent) if parent and parent.IsValid() else None
        if outer is not None:
            warnings.append(f"Rigid body {path} is nested inside the rigid body {outer.GetPath()}.")
    if static:
        notes.append(f"{static} collider(s) have no rigid body above them, so they are static.")


def _attr_value(prim: Any, name: str, fallback: Any) -> Any:
    attr = prim.GetAttribute(name)
    value = attr.Get() if attr and attr.IsValid() else None
    return to_jsonable(value) if value is not None else fallback


def _authored(prim: Any, name: str) -> Any:
    attr = prim.GetAttribute(name)
    if not attr or not attr.HasAuthoredValue():
        return None
    return _plain(attr.Get())


def _report_result(
    action: str, stage: Any, warnings: list[str], notes: list[str], fields: dict[str, Any], *, error: str | None = None
) -> dict[str, Any]:
    result = _result(action, True, stage, warnings, notes, fields, error=error)
    result.pop("next_step", None)
    return result


# --- execution --------------------------------------------------------------------------------------------


def _execute(
    action: str,
    dry: bool,
    stage: Any,
    ops: list[dict[str, Any]],
    unchanged: list[dict[str, Any]],
    warnings: list[str],
    notes: list[str],
    fields: dict[str, Any],
) -> dict[str, Any]:
    public = [_public(op) for op in ops]
    if not ops:
        notes.append("Everything requested is already in place, so nothing needs to be written.")
    if dry:
        return _result(action, True, stage, warnings, notes, fields, would_change=public, unchanged=unchanged)
    done: list[dict[str, Any]] = []
    for op, entry in zip(ops, public):
        try:
            _apply(stage, op)
        except Exception as exc:
            return _result(
                action,
                False,
                stage,
                warnings,
                notes,
                fields,
                error=f"Authoring failed part-way after {len(done)} of {len(ops)} changes ({entry['name']}): {exc}",
                written=bool(done),
                changed=done,
                not_written=public[len(done):],
            )
        done.append(entry)
    for op in ops:
        problem = _readback(stage, op)
        if problem:
            warnings.append(problem)
    return _result(action, False, stage, warnings, notes, fields, written=bool(done), changed=done, unchanged=unchanged)


def _apply(stage: Any, op: dict[str, Any]) -> None:
    path = Sdf.Path(op["path"])
    if op["kind"] == "prim":
        if not UsdPhysics.Scene.Define(stage, path):
            raise ValueError(f"Could not define a PhysicsScene at {path}")
        return
    prim = stage.GetPrimAtPath(path)
    if op["kind"] == "api" and op["op"] == "apply":
        if not getattr(UsdPhysics, MODULE_APIS[op["name"]]).Apply(prim):
            raise ValueError(f"Could not apply {op['name']}")
    elif op["kind"] == "api" and op["op"] == "remove":
        if op.get("method") == "list_op_edit":
            _remove_from_list_op(stage, prim, op["name"])
        elif not prim.RemoveAPI(getattr(UsdPhysics, MODULE_APIS[op["name"]])):
            raise ValueError(f"Could not remove {op['name']}")
    elif op["kind"] == "attribute" and op["op"] == "set":
        attr = prim.GetAttribute(op["name"])
        if not attr or not attr.Set(op["_value"]):
            raise ValueError(f"Could not set {op['name']}")
    elif op["kind"] == "attribute" and op["op"] == "remove_spec":
        if not prim.RemoveProperty(op["name"]):
            raise ValueError(f"Could not remove the {op['name']} spec")
    else:  # pragma: no cover - plans only contain the kinds above
        raise ValueError(f"Unknown change {op['kind']}/{op['op']}")


def _readback(stage: Any, op: dict[str, Any]) -> str | None:
    prim = stage.GetPrimAtPath(op["path"])
    if not prim or not prim.IsValid():
        return f"After the write {op['path']} does not exist."
    if op["kind"] == "api" and op["op"] == "apply" and not _has_api(prim, op["name"]):
        return f"After the write {op['path']} still lacks {op['name']}: a stronger opinion deletes it."
    if op["kind"] == "api" and op["op"] == "remove" and _has_api(prim, op["name"]):
        return f"After the write {op['path']} still has {op['name']}: a stronger layer applies it."
    if op["kind"] == "attribute" and op["op"] == "set":
        attr = prim.GetAttribute(op["name"])
        value = attr.Get() if attr else None
        if value is None or not _same(value, op["_value"]):
            return f"After the write {op['path']}.{op['name']} resolves to {_text(_plain(value))}: a stronger opinion wins."
    return None


def _public(op: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in op.items() if not key.startswith("_")}


def _same(old: Any, new: Any) -> bool:
    if isinstance(new, bool) or isinstance(old, bool):
        return bool(old) == bool(new) and isinstance(old, bool) == isinstance(new, bool)
    if isinstance(new, str):
        return str(old) == new
    if _is_number(new):
        try:
            old_value = float(old)
        except (TypeError, ValueError):
            return False
        return math.isclose(old_value, float(new), rel_tol=_REL_TOLERANCE, abs_tol=1e-12)
    try:
        return len(old) == len(new) and all(_same(float(a), float(b)) for a, b in zip(old, new))
    except TypeError:
        return False


def _plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float):
        return _round(value)
    return to_jsonable(value)


def _round(value: float) -> float | None:
    number = float(value)
    if not math.isfinite(number):
        return None
    rounded = round(number, 9)
    if rounded == 0.0 and number != 0.0:
        return float(f"{number:.9g}")  # keep tiny stage-unit densities (kg/cm^3) readable
    return 0.0 if rounded == 0.0 else rounded
