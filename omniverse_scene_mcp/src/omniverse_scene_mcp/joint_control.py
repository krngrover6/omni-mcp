"""Joint control for robots (articulations): drive targets, named poses, drive gains, and live joint state/moves.

Two layers, kept separate on purpose:

1. Pure pxr (any stage, offline or inside Kit), built on robot_inspector:
   - joint_table(stage, robot_root)                      READ-ONLY: one row per hinge/slider (its degree of freedom),
     with limits, drive (motor) settings and the drive target currently authored in USD.
   - set_drive_targets(stage, robot_root, targets, dry_run=True)   WRITE: authors UsdPhysics DriveAPI
     drive:<angular|linear>:physics:targetPosition. Values are DEGREES for hinges (UsdPhysics stores angular targets in
     degrees, see usdPhysics/schema.usda: "if angular drive: degrees") and METRES for sliders (converted to stage
     units with metersPerUnit). Out-of-limit values are clamped with a warning; unknown joints, joints without a drive,
     instance proxies, animated targets and stronger-layer opinions are refused before anything is written.
   - set_drive_gains(stage, robot_root, joints, stiffness, damping, dry_run=True)   WRITE: drive stiffness/damping.
   - Named poses: FRANKA_POSES (built in), save_pose (WRITE: <project_dir>/robots/poses.json, explicit path only),
     list_poses / pose_targets (READ-ONLY), apply_pose (= set_drive_targets with a pose).
   Authored targets are what the robot starts driving toward when the simulation plays; they are saved with the scene
   like any other edit (only when the user saves).

2. Kit-only (Isaac Sim, lazy imports, never authored to USD):
   - live_joint_state(robot_root)              READ-ONLY: positions/velocities/efforts per DOF from PhysX.
   - live_move(robot_root, targets, dry_run=True)   SIMULATION STATE: sets PhysX drive position targets at runtime.
   Both need a playing (or paused) timeline: when it is stopped there is no physics view and they return
   {"ok": False, "reason": ...}. Like every Kit call they must run on Kit's MAIN thread (the Kit RPC bridge already
   dispatches its handlers there; in the Script Editor you are on the main thread too).

Isaac Sim 6.1 runtime API (verified read-only in C:/Users/krngr/Omniverse/isaacsim-6.1.0):
  - Preferred: isaacsim.core.experimental.prims.Articulation(paths)
    (exts/isaacsim.core.experimental.prims/isaacsim/core/experimental/prims/impl/articulation.py). No initialize():
    it binds to SimulationManager's tensor view when physics is ready; is_physics_tensor_entity_valid() says whether
    it can be used. dof_names, dof_types (omni.physics.tensors.DofType.Rotation / Translation), get_dof_positions(),
    get_dof_velocities(), get_dof_efforts(), get_dof_limits() -> (lower, upper), set_dof_position_targets(positions,
    dof_indices=...). Values are warp arrays shaped (prims, dofs); angles in RADIANS. Without a valid tensor entity,
    set_dof_position_targets falls back to the USD backend and AUTHORS targetPosition; live_move therefore refuses
    first, so it never writes USD.
  - Fallback (deprecated since 6.0, in extsDeprecated/, still loaded through isaacsim.core.api):
    isaacsim.core.prims.SingleArticulation(prim_path, name, reset_xform_properties=False) + initialize(),
    handles_initialized, dof_names, dof_properties["type"] (1 rotation, 2 translation; "lower"/"upper" in radians),
    get_joint_positions(), get_joint_velocities(), get_measured_joint_efforts(), apply_action(ArticulationAction(
    joint_positions=..., joint_indices=...)) with ArticulationAction from isaacsim.core.utils.types.
    (reset_xform_properties defaults to True there and would rewrite the robot's xform ops, so it is passed False.)
  - World is not needed: pressing Play makes isaacsim.core.simulation_manager.SimulationManager._on_play call
    initialize_physics(), which creates the omni.physics.tensors simulation view
    (SimulationManager.get_physics_simulation_view()); Stop calls invalidate_physics(). The view appears on the
    frame after Play, so a call in the same script line as timeline.play() may still see "not ready".
"""

from __future__ import annotations

import importlib
import json
import math
import numbers
import os
import re
import time
from pathlib import Path
from typing import Any

from .authoring import _check_edit_target, _dry, _edit_target_info, _Refusal, _stronger_opinion
from .robot_inspector import inspect_robot

try:
    from pxr import Usd, UsdGeom, UsdPhysics
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Usd = UsdGeom = UsdPhysics = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

__all__ = [
    "FRANKA_POSES",
    "GAINS_GUIDE",
    "POSES_FILE",
    "apply_pose",
    "joint_table",
    "list_poses",
    "live_joint_state",
    "live_move",
    "pose_targets",
    "save_pose",
    "set_drive_gains",
    "set_drive_targets",
]

POSES_FILE = ("robots", "poses.json")  # relative to an explicit project_dir
MAX_TARGETS = 256
MAX_WARNINGS = 60
POSE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$")
_SAME = 1e-9
_DEG_PER_RAD = 180.0 / math.pi
DRIVE_INSTANCE = {"revolute": "angular", "prismatic": "linear"}
UNIT = {"revolute": "degrees", "prismatic": "metres"}
_PROBLEM_CODES = ("no_drive", "drive_without_strength", "no_limits", "inverted_limits")


def _franka_pose(arm_radians: tuple[float, ...], finger_metres: float) -> dict[str, float]:
    pose = {f"panda_joint{index}": round(math.degrees(value), 3) for index, value in enumerate(arm_radians, start=1)}
    pose["panda_finger_joint1"] = finger_metres
    pose["panda_finger_joint2"] = finger_metres
    return pose


# Built-in Franka Panda poses (degrees for the 7 arm hinges, metres for the 2 finger sliders), converted from the
# radian values Isaac Sim 6.1's own Franka setups use.
FRANKA_POSES: dict[str, dict[str, Any]] = {
    "home": {
        "robot": "Franka Panda (panda_joint1..7, panda_finger_joint1/2)",
        "description": "Lula/RMPflow's default configuration (default_q): arm folded up and back, hand in front of "
        "the base; fingers open.",
        "source": "isaacsim-6.1.0/extsDeprecated/isaacsim.robot_motion.motion_generation/motion_policy_configs/"
        "franka/rmpflow/robot_descriptor.yaml default_q = [0.0, -1.3, 0.0, -2.87, 0.0, 2.0, 0.75] rad "
        "(fingers 0.04 m = fully open, the finger upper limit)",
        "targets": _franka_pose((0.0, -1.3, 0.0, -2.87, 0.0, 2.0, 0.75), 0.04),
    },
    "ready": {
        "robot": "Franka Panda (panda_joint1..7, panda_finger_joint1/2)",
        "description": "Isaac's manipulation examples' default pose: hand pointing down over the table, ready to "
        "grasp; fingers open. The Franka asset's own authored drive targets are this pose.",
        "source": "isaacsim-6.1.0/exts/isaacsim.robot_motion.examples/isaacsim/robot_motion/examples/manipulation/"
        "robots.py _FRANKA default_joint_positions = [0.012, -0.568, 0.0, -2.811, 0.0, 3.037, 0.741] rad, "
        "fingers 0.04 m",
        "targets": _franka_pose((0.012, -0.568, 0.0, -2.811, 0.0, 3.037, 0.741), 0.04),
    },
}

GAINS_GUIDE = {
    "stiffness": "Stiffness is the spring in the motor: how hard the drive pulls the joint toward its target "
    "position. Higher = snappier and holds against gravity better; too high = jerky or unstable.",
    "damping": "Damping is the shock absorber: it resists speed (toward the target velocity, usually 0). Some damping "
    "stops the joint overshooting and wobbling around the target; too much makes it slow and sluggish.",
    "position_control": "Position control: stiffness > 0 and some damping (a common start is damping about 10-20% "
    "of stiffness).",
    "velocity_control": "Velocity control (wheels, conveyors): stiffness 0, damping > 0, and a target velocity.",
    "effort_control": "Effort (torque) control: stiffness 0 and damping 0, then apply efforts from code.",
    "units": "USD stores angular gains per DEGREE (stiffness: torque per degree; damping: torque per degree/second) "
    "and linear gains per stage unit. Results also show the SI equivalent (N*m/rad, N*m*s/rad, N/m, N*s/m).",
    "typical": "Start from the robot asset's own values: Isaac Sim 6.1's Franka (Robots/FrankaRobotics/FrankaPanda/"
    "franka.usd) authors stiffness 400 and damping 80 on every drive (angular values per degree, about 22918 N*m/rad "
    "and 4584 N*m*s/rad). Change gains by a factor of 2 at a time and watch the motion; Isaac Sim's Gain Tuner "
    "(isaacsim.robot_setup.gain_tuner) helps tune them.",
}


# ------------------------------------------------------------------------------------------------ joint table


def joint_table(stage: Any, robot_root: str) -> dict[str, Any]:
    """READ-ONLY. One row per hinge (revolute) or slider (prismatic) of the robot at robot_root, in tree order.

    Each row: name, path, kind, type (hinge/slider), unit (degrees/metres), lower/upper limit, has_drive, drive
    ({instance, stiffness, damping, max_force, target_position, target_velocity, gains_si}), mimics (a joint that
    copies another one instead of having a motor). Other joints (fixed, ball, D6) are listed under other_joints.
    """
    base = {"ok": False, "action": "joint_table", "read_only": True, "robot_root": robot_root}
    try:
        robot, info = _robot(stage, robot_root)
    except _Refusal as refusal:
        return {**base, "error": str(refusal)}
    rows, others = _rows(stage, robot)
    driven = [row["name"] for row in rows if row["has_drive"]]
    problems = [item for item in robot.get("problems", []) if item["code"] in _PROBLEM_CODES]
    return {
        **base,
        "ok": True,
        "robot_root": robot["root"],
        "robot_name": robot["name"],
        "articulation_root": robot["articulation_root"],
        "dof_count": len(rows),
        "driven_count": len(driven),
        "joints": [_public(row) for row in rows],
        "other_joints": others,
        "problems": problems,
        "stage": info,
        "notes": [
            "Targets and limits are in degrees for hinges and metres for sliders (USD stores angular drive targets "
            "in degrees; slider values were converted from stage units).",
            "Rows follow the robot's tree from the base (close to, but not guaranteed to be, PhysX's DOF order).",
            "Read-only: nothing was edited or saved.",
        ],
    }


def _robot(stage: Any, robot_root: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if _PXR_IMPORT_ERROR is not None:
        raise _Refusal(f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}")
    if stage is None or not hasattr(stage, "GetPrimAtPath"):
        raise _Refusal("No USD stage is open.")
    if not isinstance(robot_root, str) or not robot_root.startswith("/"):
        raise _Refusal("robot_root must be the robot's prim path, such as '/World/franka'.")
    report = inspect_robot(stage, robot_root)
    if not report.get("ok"):
        raise _Refusal(report.get("error") or "Could not read the robot.")
    robots = report["robots"]
    if not robots:
        raise _Refusal(f"No robot (articulation or jointed rigid bodies) was found at {robot_root}.")
    if len(robots) > 1:
        roots = ", ".join(robot["root"] for robot in robots[:10])
        raise _Refusal(f"{robot_root} contains {len(robots)} robots ({roots}); pass one of those paths.")
    if report.get("truncated"):
        raise _Refusal("The robot scan was cut short (too many prims); pass the robot's own root path.")
    return robots[0], report["stage"]


def _rows(stage: Any, robot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mpu = _mpu(stage)
    kgpu = _kgpu(stage)
    order = {edge["joint"]: index for index, edge in enumerate(robot.get("tree_edges") or [])}
    joints = sorted(robot["joints"], key=lambda entry: order.get(entry["path"], len(order)))
    rows: list[dict[str, Any]] = []
    others: list[dict[str, Any]] = []
    for entry in joints:
        kind = entry["kind"]
        if kind not in DRIVE_INSTANCE:
            why = {
                "fixed": "a fixed joint does not move, so it has no target",
                "spherical": "a ball joint has 3 rotations; set its drives per axis by hand",
                "d6": "a D6 joint has one drive per axis (rotX, transY, ...); set them by hand",
                "distance": "a distance joint has no drive",
            }.get(kind, "not a single-axis joint")
            others.append({"name": entry["name"], "path": entry["path"], "kind": kind, "why_not_listed": why})
            continue
        instance = DRIVE_INSTANCE[kind]
        drive = next((item for item in entry.get("drives") or [] if item["instance"] == instance), None)
        prim = stage.GetPrimAtPath(entry["path"])
        row: dict[str, Any] = {
            "name": entry["name"],
            "path": entry["path"],
            "kind": kind,
            "type": entry["type"],
            "axis": entry.get("axis"),
            "unit": UNIT[kind],
            "lower": (entry.get("limits") or {}).get("lower"),
            "upper": (entry.get("limits") or {}).get("upper"),
            "has_drive": drive is not None,
            "drive": None,
            "mimics": entry.get("mimics"),
            "instance_proxy": bool(entry.get("instance_proxy")),
            "_prim": prim,
            "_mpu": mpu,
        }
        if (entry.get("limits") or {}).get("locked"):
            row["locked"] = True
        if drive is not None:
            row["drive"] = {
                "instance": instance,
                "stiffness": drive["stiffness"],
                "damping": drive["damping"],
                "max_force": drive["max_force"],
                "target_position": drive["target_position"],
                "target_velocity": drive["target_velocity"],
                "gains_si": _gains_si(kind, drive["stiffness"], drive["damping"], mpu, kgpu),
            }
        else:
            other = [item["instance"] for item in entry.get("drives") or []]
            if other:
                row["note"] = f"It has drive(s) {other}, but a {entry['type']} is driven by the '{instance}' drive."
        rows.append(row)
    return rows, others


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def _gains_si(kind: str, stiffness: Any, damping: Any, mpu: float, kgpu: float) -> dict[str, Any]:
    if kind == "revolute":
        scale = kgpu * mpu * mpu * _DEG_PER_RAD
        return {
            "stiffness": _num(None if stiffness is None else stiffness * scale, 3),
            "stiffness_unit": "N*m/rad",
            "damping": _num(None if damping is None else damping * scale, 3),
            "damping_unit": "N*m*s/rad",
        }
    return {
        "stiffness": _num(None if stiffness is None else stiffness * kgpu, 3),
        "stiffness_unit": "N/m",
        "damping": _num(None if damping is None else damping * kgpu, 3),
        "damping_unit": "N*s/m",
    }


# ------------------------------------------------------------------------------------------------ drive targets


def set_drive_targets(
    stage: Any, robot_root: str, targets: Any, *, dry_run: bool = True
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author drive targetPosition values: {joint name or path: value}.

    Values: degrees for hinges, metres for sliders. Clamped to the joint limits with a warning. Refused (nothing
    written): unknown joints, ambiguous names, joints that are not hinges/sliders, joints without a drive, instance
    proxies, animated (time-sampled) targets, a stronger layer overriding the edit target, a read-only edit target.
    Joints that mimic another joint (no motor of their own) are skipped with a warning.
    """
    action = "set_drive_targets"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        robot, _info = _robot(stage, robot_root)
        _check_edit_target(stage, warnings)
        values = _target_values(targets)
        rows, _others = _rows(stage, robot)
        plan = []
        for key, value in values.items():
            row = _find_row(rows, key, robot)
            if row["mimics"] and not row["has_drive"]:
                warnings.append(
                    f"{row['name']} has no motor: it copies {_leaf(row['mimics'].get('reference_joint'))} "
                    f"(gearing {row['mimics'].get('gearing')}), so it was skipped. Set that joint instead."
                )
                continue
            _check_writable(stage, row, "targetPosition")
            plan.append(_target_step(row, value, warnings))
        plan = _dedupe(plan)
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # planning only reads
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")
    return _apply(action, dry, stage, plan, warnings, robot)


def _target_values(targets: Any) -> dict[str, float]:
    if not isinstance(targets, dict) or not targets:
        raise _Refusal("targets must be a non-empty {joint name: value} mapping, e.g. {'panda_joint1': 30}.")
    if len(targets) > MAX_TARGETS:
        raise _Refusal(f"At most {MAX_TARGETS} joints per call.")
    values: dict[str, float] = {}
    for key, value in targets.items():
        if not isinstance(key, str) or not key.strip():
            raise _Refusal(f"Joint names must be non-empty strings, got {key!r}.")
        values[key.strip()] = _finite(value, f"target for {key}")
    return values


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise _Refusal(f"{name} must be a number, got {value!r}.")
    number = float(value)
    if not math.isfinite(number):
        raise _Refusal(f"{name} must be finite, got {value!r}.")
    return number


def _find_row(rows: list[dict[str, Any]], key: str, robot: dict[str, Any]) -> dict[str, Any]:
    if key.startswith("/"):
        matches = [row for row in rows if row["path"] == key]
    else:
        matches = [row for row in rows if row["name"] == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _Refusal(f"'{key}' names {len(matches)} joints ({', '.join(row['path'] for row in matches)}); use the full path.")
    other = next((entry for entry in robot["joints"] if entry["name"] == key or entry["path"] == key), None)
    if other is not None:
        raise _Refusal(f"{other['path']} is a {other['type']} joint, which has no single drive target.")
    names = ", ".join(row["name"] for row in rows[:20]) or "none"
    raise _Refusal(f"Unknown joint '{key}' in {robot['root']}. Its hinges/sliders are: {names}.")


def _check_writable(stage: Any, row: dict[str, Any], what: str) -> None:
    if not row["has_drive"]:
        raise _Refusal(
            f"{row['name']} has no '{DRIVE_INSTANCE[row['kind']]}' drive (motor), so it has no {what} to set. "
            "Add one first (Isaac Sim: select the joint > Add > Physics > "
            f"{'Angular' if row['kind'] == 'revolute' else 'Linear'} Drive)."
        )
    prim = row["_prim"]
    if row["instance_proxy"] or prim.IsInstanceProxy():
        raise _Refusal(
            f"{row['path']} is inside an instanceable prim, so it cannot be edited here; edit the asset's prototype "
            "or turn off instanceable on its ancestor."
        )
    name = f"drive:{DRIVE_INSTANCE[row['kind']]}:physics:{what}"
    attr = prim.GetAttribute(name)
    if attr and attr.IsValid() and attr.GetNumTimeSamples() > 0:
        raise _Refusal(f"{row['path']}.{name} is animated (time samples), so a single value would be hidden.")
    stronger = _stronger_opinion(stage, prim, name)
    if stronger:
        raise _Refusal(
            f"{row['path']}.{name} is set in the stronger layer {stronger}, which would hide this edit; make that "
            "layer the edit target or remove its value first."
        )


def _target_step(row: dict[str, Any], value: float, warnings: list[str]) -> dict[str, Any]:
    target = value
    lower, upper = row["lower"], row["upper"]
    clamped = False
    if row.get("locked"):
        warnings.append(f"{row['name']} has its lower limit above its upper limit, so PhysX keeps it locked.")
    elif lower is not None and target < lower:
        target, clamped = lower, True
    elif upper is not None and target > upper:
        target, clamped = upper, True
    if clamped:
        warnings.append(
            f"{row['name']}: {value:g} {row['unit']} is outside its limits {_range(lower, upper)}, so it was clamped "
            f"to {target:g}."
        )
    scale = 1.0 if row["kind"] == "revolute" else 1.0 / row["_mpu"]
    previous = row["drive"]["target_position"]
    return {
        "joint": row["name"],
        "path": row["path"],
        "kind": row["kind"],
        "unit": row["unit"],
        "attribute": f"drive:{DRIVE_INSTANCE[row['kind']]}:physics:targetPosition",
        "requested": _num(value, 6),
        "target": _num(target, 6),
        "previous": previous,
        "clamped": clamped,
        "authored_value": _num(target * scale, 9),
        "changed": previous is None or abs(previous - target) > 1e-6,
        "_prim": row["_prim"],
        "_instance": DRIVE_INSTANCE[row["kind"]],
        "_writes": {"targetPosition": target * scale},
    }


def _dedupe(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for step in plan:
        if step["path"] in seen:
            raise _Refusal(f"{step['path']} is given twice (by name and by path); give it once.")
        seen[step["path"]] = step
    return plan


def _author_value(prim: Any, instance: str, field: str, value: float) -> None:
    """Author one DriveAPI attribute at the default time in the stage's edit target."""
    drive = UsdPhysics.DriveAPI(prim, instance)
    creator = {
        "targetPosition": drive.CreateTargetPositionAttr,
        "stiffness": drive.CreateStiffnessAttr,
        "damping": drive.CreateDampingAttr,
    }[field]
    creator().Set(float(value))


def _apply(
    action: str, dry: bool, stage: Any, plan: list[dict[str, Any]], warnings: list[str], robot: dict[str, Any]
) -> dict[str, Any]:
    public = [_public(step) for step in plan]
    extra = {"robot_root": robot["root"], "plan": public}
    changes = [step for step in plan if step["changed"]]
    if dry:
        return _finish(action, True, stage, warnings, extra=extra)
    if not changes:
        extra["note"] = "Every joint already has these values; nothing was written."
        return _finish(action, False, stage, warnings, extra=extra)
    from .undo import SpecSnapshot  # pure pxr

    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    try:
        snapshot = SpecSnapshot.capture(layer, shallow=[edit_target.MapToSpecPath(step["_prim"].GetPath()) for step in changes])
    except Exception:  # noqa: BLE001 - e.g. a variant edit target; the writes still run
        snapshot = None
    current = "starting"
    try:
        for step in changes:
            for field, value in step["_writes"].items():
                current = f"{step['path']} {field}"
                _author_value(step["_prim"], step["_instance"], field, value)
    except Exception as exc:  # noqa: BLE001
        written = True
        if snapshot is not None:
            try:
                snapshot.restore(layer)
                written = False
                message = f"Writing failed at {current} ({exc}); everything already written was put back."
            except Exception as restore_exc:  # noqa: BLE001
                message = f"Writing failed at {current} ({exc}) and could not be rolled back ({restore_exc})."
        else:
            message = f"Writing failed at {current} ({exc}); the edit target may hold part of the change."
        return _finish(action, False, stage, warnings, extra=extra, error=message, written=written)
    extra["readback"] = _readback(changes)
    extra["note"] = (
        "Authored in the edit target (not saved). The robot drives toward these targets when the simulation plays."
    )
    return _finish(action, False, stage, warnings, extra=extra, written=True)


def _readback(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for step in changes:
        prim = step["_prim"]
        values = {}
        for field in step["_writes"]:
            attr = prim.GetAttribute(f"drive:{step['_instance']}:physics:{field}")
            values[field] = _num(attr.Get() if attr else None, 6)
        rows.append({"joint": step["joint"], "path": step["path"], **values})
    return rows


def _finish(
    action: str,
    dry: bool,
    stage: Any,
    warnings: list[str],
    *,
    extra: dict[str, Any] | None = None,
    error: str | None = None,
    written: bool = False,
) -> dict[str, Any]:
    unique: list[str] = []
    for warning in warnings:
        if warning not in unique:
            unique.append(warning)
    info = _edit_target_info(stage) if stage is not None and _PXR_IMPORT_ERROR is None and hasattr(stage, "GetEditTarget") else None
    result: dict[str, Any] = {"ok": error is None, "action": action, "dry_run": dry, "written": written}
    if error is not None:
        result["error"] = error
    if extra:
        result.update(extra)
    result["edit_target_layer"] = info["identifier"] if info else None
    result["warnings"] = unique[:MAX_WARNINGS]
    if error is None and dry:
        result["next_step"] = "Dry run: nothing was written. Call again with dry_run=False to author this."
    return result


# ------------------------------------------------------------------------------------------------ drive gains


def set_drive_gains(
    stage: Any,
    robot_root: str,
    joints: Any = None,
    stiffness: Any = None,
    damping: Any = None,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True) or author drive stiffness and/or damping (USD units: per degree for hinges).

    joints: a joint name/path, a list of them, or None/"all" for every driven hinge/slider. Pass stiffness, damping
    or both (numbers >= 0); a None value is left unchanged. See GAINS_GUIDE for what they mean and typical values.
    """
    action = "set_drive_gains"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        robot, _info = _robot(stage, robot_root)
        _check_edit_target(stage, warnings)
        if stiffness is None and damping is None:
            raise _Refusal("Pass stiffness, damping or both.")
        new_k = None if stiffness is None else _finite(stiffness, "stiffness")
        new_d = None if damping is None else _finite(damping, "damping")
        for name, value in (("stiffness", new_k), ("damping", new_d)):
            if value is not None and value < 0:
                raise _Refusal(f"{name} must be 0 or more, got {value:g}.")
        rows, _others = _rows(stage, robot)
        if joints is None or joints == "all":
            chosen = [row for row in rows if row["has_drive"]]
            if not chosen:
                raise _Refusal(f"{robot['root']} has no hinge or slider with a drive.")
        else:
            keys = [joints] if isinstance(joints, str) else joints
            if not isinstance(keys, (list, tuple)) or not keys or not all(isinstance(key, str) for key in keys):
                raise _Refusal("joints must be a joint name, a list of names, or 'all'.")
            chosen = [_find_row(rows, key.strip(), robot) for key in keys]
        mpu, kgpu = _mpu(stage), _kgpu(stage)
        plan = []
        for row in chosen:
            fields = [name for name, value in (("stiffness", new_k), ("damping", new_d)) if value is not None]
            for field in fields:
                _check_writable(stage, row, field)
            old = row["drive"]
            k = old["stiffness"] if new_k is None else new_k
            d = old["damping"] if new_d is None else new_d
            writes = {}
            if new_k is not None:
                writes["stiffness"] = new_k
            if new_d is not None:
                writes["damping"] = new_d
            changed = any(abs((old[field] or 0.0) - value) > _SAME for field, value in writes.items())
            plan.append(
                {
                    "joint": row["name"],
                    "path": row["path"],
                    "kind": row["kind"],
                    "previous": {"stiffness": old["stiffness"], "damping": old["damping"]},
                    "new": {"stiffness": _num(k, 6), "damping": _num(d, 6)},
                    "units": "per degree (angular)" if row["kind"] == "revolute" else "per stage unit (linear)",
                    "gains_si": _gains_si(row["kind"], k, d, mpu, kgpu),
                    "mode": _mode(k, d),
                    "changed": changed,
                    "_prim": row["_prim"],
                    "_instance": DRIVE_INSTANCE[row["kind"]],
                    "_writes": writes,
                }
            )
            _gain_warnings(row["name"], k, d, warnings)
        plan = _dedupe(plan)
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")
    result = _apply(action, dry, stage, plan, warnings, robot)
    result["guide"] = dict(GAINS_GUIDE)
    return result


def _mode(k: float, d: float) -> str:
    if k > 0:
        return "position control (stiffness pulls toward the target position)"
    if d > 0:
        return "velocity control (damping pushes toward the target velocity)"
    return "no strength (the joint goes limp; only efforts from code move it)"


def _gain_warnings(name: str, k: float, d: float, warnings: list[str]) -> None:
    if k <= 0 and d <= 0:
        warnings.append(f"{name}: stiffness 0 and damping 0 make the motor push with zero force; the joint goes limp.")
    elif k > 0 and d <= 0:
        warnings.append(
            f"{name}: stiffness without damping makes the joint overshoot and wobble around its target; add some "
            "damping (about 10-20% of stiffness to start)."
        )
    elif k > 0 and d > k:
        warnings.append(f"{name}: damping is larger than stiffness, so the joint will move slowly toward its target.")


# ------------------------------------------------------------------------------------------------ named poses


def _poses_path(project_dir: Any) -> Path:
    if project_dir in (None, "") or not isinstance(project_dir, (str, os.PathLike)):
        raise _Refusal("project_dir must be given explicitly (the project's folder); poses are never saved elsewhere.")
    folder = Path(project_dir).expanduser()
    if not folder.is_absolute():
        raise _Refusal("project_dir must be an absolute folder path.")
    if not folder.is_dir():
        raise _Refusal(f"project_dir {folder} does not exist; create the project folder first.")
    return folder.joinpath(*POSES_FILE)


def _read_poses(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": 1, "poses": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _Refusal(f"{path} could not be read as JSON ({exc}); fix or move it first (it was not changed).") from None
    if not isinstance(data, dict) or not isinstance(data.get("poses"), dict):
        raise _Refusal(f"{path} is not a poses file (expected {{'version': 1, 'poses': {{...}}}}); it was not changed.")
    return data


def save_pose(
    stage: Any,
    robot_root: str,
    name: str,
    values: Any = None,
    *,
    project_dir: Any,
    overwrite: bool = False,
) -> dict[str, Any]:
    """WRITE (a JSON file only): save a named pose to <project_dir>/robots/poses.json.

    values: {joint: degrees or metres}; None saves the drive targets currently authored on the robot. Joints are
    checked against the robot; out-of-limit values are kept but warned about (they are clamped when applied).
    The stage is only read. Built-in names (home, ready) and existing names (unless overwrite=True) are refused.
    """
    base: dict[str, Any] = {"ok": False, "action": "save_pose", "name": name, "written": False}
    warnings: list[str] = []
    try:
        if not isinstance(name, str) or not POSE_NAME.match(name.strip()):
            raise _Refusal("name must be 1-64 letters, digits, spaces, '_', '-' or '.', starting with a letter or digit.")
        name = name.strip()
        if name.lower() in FRANKA_POSES:
            raise _Refusal(f"'{name}' is a built-in pose name; choose another name.")
        path = _poses_path(project_dir)
        robot, _info = _robot(stage, robot_root)
        rows, _others = _rows(stage, robot)
        if values is None:
            targets = {row["name"]: row["drive"]["target_position"] for row in rows if row["has_drive"]}
            if not targets:
                raise _Refusal(f"{robot['root']} has no driven joints, so there is no current pose to save.")
            source = "the drive targets currently authored on the robot"
        else:
            targets = {}
            for key, value in _target_values(values).items():
                row = _find_row(rows, key, robot)
                if row["name"] in targets:
                    raise _Refusal(f"{row['name']} is given twice.")
                lower, upper = row["lower"], row["upper"]
                if (lower is not None and value < lower) or (upper is not None and value > upper):
                    warnings.append(
                        f"{row['name']}: {value:g} {row['unit']} is outside its limits {_range(lower, upper)}; it will "
                        "be clamped when the pose is applied."
                    )
                targets[row["name"]] = _num(value, 6)
            source = "the values given"
        data = _read_poses(path)
        if name in data["poses"] and not overwrite:
            raise _Refusal(f"A pose named '{name}' is already saved in {path}; pass overwrite=True to replace it.")
        units = {row["name"]: row["unit"] for row in rows if row["name"] in targets}
        data["version"] = 1
        data["poses"][name] = {
            "robot_root": robot["root"],
            "robot_name": robot["name"],
            "targets": targets,
            "units": units,
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        path.parent.mkdir(exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, path)
    except _Refusal as refusal:
        return {**base, "error": str(refusal), "warnings": warnings}
    except OSError as exc:
        return {**base, "error": f"Could not write the poses file: {exc}", "warnings": warnings}
    return {
        **base,
        "ok": True,
        "written": True,
        "file": str(path),
        "robot_root": robot["root"],
        "targets": targets,
        "units": units,
        "source": source,
        "warnings": warnings,
        "next_step": f"apply_pose(stage, '{robot['root']}', '{name}', project_dir=...) authors it; live_move moves the "
        "running simulation.",
    }


def list_poses(project_dir: Any = None) -> dict[str, Any]:
    """READ-ONLY. Built-in poses plus the ones saved in <project_dir>/robots/poses.json (when project_dir is given)."""
    poses = [
        {
            "name": name,
            "source": "built-in",
            "robot": pose["robot"],
            "description": pose["description"],
            "joint_count": len(pose["targets"]),
            "targets": dict(pose["targets"]),
        }
        for name, pose in FRANKA_POSES.items()
    ]
    result: dict[str, Any] = {"ok": True, "action": "list_poses", "read_only": True, "file": None}
    if project_dir not in (None, ""):
        try:
            path = _poses_path(project_dir)
            data = _read_poses(path)
        except _Refusal as refusal:
            return {**result, "ok": False, "error": str(refusal), "poses": poses}
        result["file"] = str(path)
        result["file_exists"] = path.exists()
        for name in sorted(data["poses"]):
            pose = data["poses"][name]
            targets = pose.get("targets") if isinstance(pose, dict) else None
            if not isinstance(targets, dict):
                continue
            poses.append(
                {
                    "name": name,
                    "source": "project",
                    "robot": pose.get("robot_root"),
                    "saved": pose.get("saved"),
                    "joint_count": len(targets),
                    "targets": dict(targets),
                }
            )
    result["poses"] = poses
    result["units"] = "degrees for hinges, metres for sliders"
    return result


def pose_targets(name: str, *, project_dir: Any = None) -> dict[str, Any]:
    """READ-ONLY. The {joint: value} targets of a built-in or saved pose (degrees for hinges, metres for sliders)."""
    base = {"ok": False, "action": "pose_targets", "name": name}
    if not isinstance(name, str) or not name.strip():
        return {**base, "error": "name must be a pose name such as 'home'."}
    key = name.strip()
    builtin = FRANKA_POSES.get(key.lower())
    if builtin is not None:
        return {
            **base,
            "ok": True,
            "source": "built-in",
            "robot": builtin["robot"],
            "description": builtin["description"],
            "reference": builtin["source"],
            "targets": dict(builtin["targets"]),
        }
    if project_dir in (None, ""):
        return {**base, "error": f"No built-in pose '{key}' (built-in: {', '.join(FRANKA_POSES)}); pass project_dir for saved poses."}
    try:
        path = _poses_path(project_dir)
        data = _read_poses(path)
    except _Refusal as refusal:
        return {**base, "error": str(refusal)}
    pose = data["poses"].get(key)
    if not isinstance(pose, dict) or not isinstance(pose.get("targets"), dict):
        known = ", ".join(list(FRANKA_POSES) + sorted(data["poses"])) or "none"
        return {**base, "error": f"No pose named '{key}'. Known poses: {known}."}
    return {
        **base,
        "ok": True,
        "source": "project",
        "file": str(path),
        "robot": pose.get("robot_root"),
        "targets": dict(pose["targets"]),
    }


def apply_pose(
    stage: Any, robot_root: str, name: str, *, project_dir: Any = None, dry_run: bool = True
) -> dict[str, Any]:
    """Plan (dry_run=True) or author a named pose's drive targets: set_drive_targets with pose_targets(name)."""
    pose = pose_targets(name, project_dir=project_dir)
    if not pose["ok"]:
        return {"ok": False, "action": "apply_pose", "dry_run": _dry(dry_run), "written": False, "error": pose["error"]}
    result = set_drive_targets(stage, robot_root, pose["targets"], dry_run=dry_run)
    result["action"] = "apply_pose"
    result["pose"] = {key: pose[key] for key in ("name", "source", "robot") if key in pose}
    return result


# ------------------------------------------------------------------------------------------------ Kit-only live


def _kit_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - ImportError, or a Kit extension that fails while loading
        return None


def _refuse(reason: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "message": reason, **extra}


def _timeline_state() -> tuple[str | None, str | None]:
    """('playing' | 'paused' | 'stopped', None) or (None, reason)."""
    module = _kit_module("omni.timeline")
    getter = getattr(module, "get_timeline_interface", None)
    if not callable(getter):
        return None, "Kit not available (omni.timeline is not importable); run this inside Isaac Sim."
    try:
        timeline = getter()
        if timeline.is_playing():
            return "playing", None
        return ("stopped" if timeline.is_stopped() else "paused"), None
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the timeline: {type(exc).__name__}: {exc}"


def _live_mpu(robot_root: str) -> tuple[float, str | None]:
    """Stage metersPerUnit of Kit's open stage, and a problem text when robot_root is not a prim there."""
    usd = _kit_module("omni.usd")
    try:
        stage = usd.get_context().get_stage() if usd is not None else None
    except Exception:  # noqa: BLE001
        stage = None
    if stage is None or _PXR_IMPORT_ERROR is not None:
        return 1.0, None
    prim = stage.GetPrimAtPath(robot_root)
    if not prim or not prim.IsValid():
        return 1.0, f"No prim at {robot_root} on the open stage."
    try:
        return _positive(UsdGeom.GetStageMetersPerUnit(stage)), None
    except Exception:  # noqa: BLE001
        return 1.0, None


def _row_values(value: Any) -> list[float] | None:
    """First row of a (prims, dofs) array (warp/numpy/torch/list) as floats."""
    if value is None:
        return None
    if hasattr(value, "numpy") and callable(value.numpy):
        value = value.numpy()
    if hasattr(value, "tolist") and callable(value.tolist):
        value = value.tolist()
    items = list(value)
    if items and isinstance(items[0], (list, tuple)):
        items = list(items[0])
    return [float(item) for item in items]


class _Experimental:
    api = "isaacsim.core.experimental.prims.Articulation"

    def __init__(self, module: Any, robot_root: str) -> None:
        self.view = module.Articulation(robot_root)
        self.valid = bool(self.view.is_physics_tensor_entity_valid())

    def names(self) -> list[str]:
        return [str(name) for name in self.view.dof_names]

    def kinds(self) -> list[str]:
        kinds = []
        for item in self.view.dof_types:
            text = str(getattr(item, "name", item))
            kinds.append("revolute" if "Rotation" in text else "prismatic" if "Translation" in text else "unknown")
        return kinds

    def state(self) -> dict[str, list[float] | None]:
        # get_dof_efforts only echoes efforts set by hand (all 0.0 for a Franka holding itself up, seen live in
        # Isaac 6.1); the projected joint forces are what each joint really pushes with (drive plus gravity).
        measured = getattr(self.view, "get_dof_projected_joint_forces", None)
        return {
            "position": _row_values(self.view.get_dof_positions()),
            "velocity": _row_values(self.view.get_dof_velocities()),
            "effort": _row_values(measured() if callable(measured) else self.view.get_dof_efforts()),
        }

    def limits(self) -> list[tuple[float, float]]:
        lower, upper = self.view.get_dof_limits()
        return list(zip(_row_values(lower) or [], _row_values(upper) or []))

    def set_targets(self, values: list[float], indices: list[int]) -> None:
        self.view.set_dof_position_targets([values], dof_indices=indices)


class _Deprecated:
    api = "isaacsim.core.prims.SingleArticulation (deprecated since Isaac Sim 6.0)"

    def __init__(self, module: Any, robot_root: str) -> None:
        self.view = module.SingleArticulation(
            prim_path=robot_root, name=f"mcp_joint_control_{abs(hash(robot_root))}", reset_xform_properties=False
        )
        self.view.initialize()
        self.valid = bool(self.view.handles_initialized)

    def names(self) -> list[str]:
        return [str(name) for name in self.view.dof_names]

    def kinds(self) -> list[str]:
        types = self.view.dof_properties["type"]
        return ["revolute" if int(item) == 1 else "prismatic" if int(item) == 2 else "unknown" for item in types]

    def state(self) -> dict[str, list[float] | None]:
        return {
            "position": _row_values(self.view.get_joint_positions()),
            "velocity": _row_values(self.view.get_joint_velocities()),
            "effort": _row_values(self.view.get_measured_joint_efforts()),
        }

    def limits(self) -> list[tuple[float, float]]:
        props = self.view.dof_properties
        return list(zip(_row_values(props["lower"]) or [], _row_values(props["upper"]) or []))

    def set_targets(self, values: list[float], indices: list[int]) -> None:
        types = _kit_module("isaacsim.core.utils.types")
        numpy = _kit_module("numpy")
        positions = numpy.array(values, dtype=float) if numpy is not None else list(values)
        joints = numpy.array(indices, dtype=int) if numpy is not None else list(indices)
        self.view.apply_action(types.ArticulationAction(joint_positions=positions, joint_indices=joints))


def _open_live(robot_root: Any) -> tuple[Any, dict[str, Any] | None, str | None, float]:
    """(articulation adapter, refusal, timeline state, metersPerUnit)."""
    if not isinstance(robot_root, str) or not robot_root.startswith("/"):
        return None, _refuse("robot_root must be the robot's prim path, such as '/World/franka'."), None, 1.0
    state, problem = _timeline_state()
    if problem:
        return None, _refuse(problem), None, 1.0
    if state == "stopped":
        return None, _refuse(
            "The timeline is stopped, so PhysX has no running robot to read or move. Press Play (or "
            "simulation_play) first; to change the starting pose without playing, use set_drive_targets.",
            timeline="stopped",
        ), state, 1.0
    mpu, missing = _live_mpu(robot_root)
    if missing:
        return None, _refuse(missing, timeline=state), state, mpu
    errors = []
    for module_name, adapter in (("isaacsim.core.experimental.prims", _Experimental), ("isaacsim.core.prims", _Deprecated)):
        module = _kit_module(module_name)
        if module is None:
            errors.append(f"{module_name} is not importable")
            continue
        try:
            live = adapter(module, robot_root)
        except Exception as exc:  # noqa: BLE001 - no articulation root, wrong path, physics not set up
            errors.append(f"{adapter.api}: {type(exc).__name__}: {exc}")
            continue
        if not live.valid:
            return None, _refuse(
                f"The articulation at {robot_root} is not ready in PhysX (no physics view yet). If you just pressed "
                "Play, try again in a moment; otherwise check that the robot has an articulation root "
                "(inspect_robot) and that physics is enabled.",
                timeline=state,
                api=adapter.api,
            ), state, mpu
        return live, None, state, mpu
    if all("not importable" in error for error in errors):
        return None, _refuse("Kit not available (no Isaac Sim articulation API is importable); run this in Isaac Sim.", details=errors), state, mpu
    return None, _refuse(f"Could not open the articulation at {robot_root}.", details=errors, timeline=state), state, mpu


def _to_user(kind: str, value: float | None, mpu: float) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return _num(math.degrees(value) if kind == "revolute" else value * mpu, 6)


def live_joint_state(robot_root: str) -> dict[str, Any]:
    """KIT-ONLY, READ-ONLY. Current joint positions, velocities and efforts of a simulating robot.

    Positions in degrees (hinges) / metres (sliders), velocities per second, efforts as PhysX reports them
    (N*m for hinges, N for sliders when the stage is in metres). Needs a playing or paused timeline.
    """
    live, refusal, state, mpu = _open_live(robot_root)
    if refusal is not None:
        return {**refusal, "action": "live_joint_state", "robot_root": robot_root}
    try:
        names, kinds, values = live.names(), live.kinds(), live.state()
        limits = live.limits()
    except Exception as exc:  # noqa: BLE001
        return {**_refuse(f"Reading the joint state failed: {type(exc).__name__}: {exc}"), "action": "live_joint_state"}
    joints = []
    for index, name in enumerate(names):
        kind = kinds[index] if index < len(kinds) else "unknown"
        unit = UNIT.get(kind, "native")
        pick = {key: (row[index] if row and index < len(row) else None) for key, row in values.items()}
        low, high = limits[index] if index < len(limits) else (None, None)
        joints.append(
            {
                "name": name,
                "kind": kind,
                "unit": unit,
                "position": _to_user(kind, pick["position"], mpu),
                "velocity": _to_user(kind, pick["velocity"], mpu),
                "velocity_unit": unit + "/second",
                "effort": _num(pick["effort"], 6),
                "effort_unit": "N*m" if kind == "revolute" else "N",
                "lower": _to_user(kind, low, mpu),
                "upper": _to_user(kind, high, mpu),
            }
        )
    notes = ["Read from PhysX (the running simulation), not from USD; nothing was changed."]
    if state == "paused":
        notes.append("The simulation is paused, so these values are frozen until Play or a step.")
    return {
        "ok": True,
        "action": "live_joint_state",
        "read_only": True,
        "robot_root": robot_root,
        "api": live.api,
        "timeline": state,
        "dof_count": len(names),
        "joints": joints,
        "notes": notes,
    }


def live_move(robot_root: str, targets: Any, *, dry_run: bool = True) -> dict[str, Any]:
    """KIT-ONLY, SIMULATION STATE. Plan (dry_run=True) or send PhysX drive position targets to a simulating robot.

    targets: {joint name: degrees for hinges / metres for sliders}; clamped to the joint limits with a warning.
    Not authored to USD: Stop resets the robot, and saving does not keep it (use set_drive_targets for that). The
    robot moves over the next physics steps, as fast as its drive gains allow.
    """
    dry = _dry(dry_run)
    base = {"action": "live_move", "robot_root": robot_root, "dry_run": dry, "applied": False}
    try:
        values = _target_values(targets)
    except _Refusal as refusal:
        return {**_refuse(str(refusal)), **base}
    live, refusal, state, mpu = _open_live(robot_root)
    if refusal is not None:
        return {**refusal, **base}
    warnings: list[str] = []
    try:
        names, kinds, current = live.names(), live.kinds(), live.state()["position"] or []
        limits = live.limits()
    except Exception as exc:  # noqa: BLE001
        return {**_refuse(f"Reading the articulation failed: {type(exc).__name__}: {exc}"), **base}
    plan, indices, native = [], [], []
    for key, value in values.items():
        name = key.rsplit("/", 1)[-1]
        if name not in names:
            return {**_refuse(f"Unknown joint '{key}'. This robot's DOFs are: {', '.join(names)}."), **base, "api": live.api}
        index = names.index(name)
        if index in indices:
            return {**_refuse(f"{name} is given twice."), **base}
        kind = kinds[index] if index < len(kinds) else "unknown"
        if kind == "unknown":
            return {**_refuse(f"PhysX does not report whether {name} turns or slides, so no unit conversion is safe."), **base}
        low, high = limits[index] if index < len(limits) else (None, None)
        low_u, high_u = _to_user(kind, low, mpu), _to_user(kind, high, mpu)
        target = value
        if low_u is not None and target < low_u:
            target = low_u
        elif high_u is not None and target > high_u:
            target = high_u
        if target != value:
            warnings.append(f"{name}: {value:g} {UNIT[kind]} is outside its limits {_range(low_u, high_u)}, so it was clamped to {target:g}.")
        raw = math.radians(target) if kind == "revolute" else target / mpu
        indices.append(index)
        native.append(raw)
        plan.append(
            {
                "joint": name,
                "index": index,
                "unit": UNIT[kind],
                "requested": _num(value, 6),
                "target": _num(target, 6),
                "clamped": target != value,
                "current": _to_user(kind, current[index] if index < len(current) else None, mpu),
                "physx_value": _num(raw, 9),
                "physx_unit": "radians" if kind == "revolute" else "stage units",
            }
        )
    result: dict[str, Any] = {**base, "ok": True, "api": live.api, "timeline": state, "plan": plan, "warnings": warnings}
    if dry:
        result["next_step"] = "Dry run: nothing was sent to PhysX. Call again with dry_run=False to move the robot."
        return result
    try:
        live.set_targets(native, indices)
    except Exception as exc:  # noqa: BLE001
        return {**result, **_refuse(f"Sending the targets failed: {type(exc).__name__}: {exc}")}
    result["applied"] = True
    result["note"] = (
        "Sent to PhysX (simulation state, not USD): the joints move toward these targets over the next physics steps. "
        "Stop resets the robot; use set_drive_targets to keep a pose in the scene."
    )
    if state == "paused":
        result["note"] += " The simulation is paused: press Play or step to see it move."
    return result


# ------------------------------------------------------------------------------------------------ helpers


def _mpu(stage: Any) -> float:
    try:
        return _positive(UsdGeom.GetStageMetersPerUnit(stage))
    except Exception:  # noqa: BLE001
        return 1.0


def _kgpu(stage: Any) -> float:
    try:
        return _positive(UsdPhysics.GetStageKilogramsPerUnit(stage))
    except Exception:  # noqa: BLE001
        return 1.0


def _positive(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 1.0
    return number if math.isfinite(number) and number > 0 else 1.0


def _num(value: Any, digits: int) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    rounded = round(number, digits)
    return 0.0 if rounded == 0 else rounded


def _range(lower: Any, upper: Any) -> str:
    def text(value: Any) -> str:
        return "none" if value is None else f"{value:g}"

    return f"[{text(lower)}, {text(upper)}]"


def _leaf(path: Any) -> str:
    return str(path).rstrip("/").rsplit("/", 1)[-1] if path else "another joint"
