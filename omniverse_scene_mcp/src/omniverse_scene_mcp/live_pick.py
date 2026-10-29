"""Live pick-and-place: the Franka in the open scene picks an object and puts it down, while you watch.

robot_pick_place_script WRITES a script; this RUNS the same steps live. Isaac Sim's own PickPlaceController (a
10-phase state machine: move above, go down, close the gripper, lift, carry, go down, open, lift ...) plans each
step's joint targets with RMPflow (a motion planner that steers the hand toward a goal while avoiding joint
limits), and a physics callback sends them to the robot every physics step until it is done or times out.

Run it with the simulation playing. Nothing is authored to USD: Stop puts everything back. Uses the classic
(deprecated since 6.0, still shipped in 6.1) Franka example classes, which live in an extension that is off by
default; it is switched on for this app session only (not saved).
"""

from __future__ import annotations

import time
from typing import Any

from . import joint_control as jc

EXAMPLES_EXTENSION = "isaacsim.robot.manipulators.examples"
TIMEOUT_STEPS = 7200   # physics steps (120 s of simulation at 60 Hz), not wall time: slow laptops run slower
OPEN_STEPS = 30        # open the gripper first for half a second of simulation
# PickPlaceController's phase timings (isaacsim.robot.manipulators.examples franka/controllers): each phase lasts
# 1/dt physics steps whether or not the arm has arrived. Seen live: with the object 76 degrees to the arm's side,
# the arm was still swinging when "go down" and "close" ran, and grabbed air. pace < 1 gives every phase longer.
EVENTS_DT = [0.008, 0.005, 1, 0.1, 0.05, 0.05, 0.0025, 1, 0.008, 0.08]
_JOB: dict[str, Any] = {}


def _vector(value: Any, label: str) -> list[float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3 or
            not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise ValueError(f"{label} must be three numbers [x, y, z] in metres")
    return [float(v) for v in value]


def _object_position(object_path: str) -> list[float] | None:
    usd = jc._kit_module("omni.usd")
    pxr = jc._kit_module("pxr.UsdGeom")
    if usd is None or pxr is None:
        return None
    stage = usd.get_context().get_stage()
    prim = stage.GetPrimAtPath(object_path) if stage else None
    if not prim:
        return None
    from pxr import Usd, UsdGeom

    t = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation()
    return [float(t[0]), float(t[1]), float(t[2])]


def _subscribe_physics(callback: Any) -> Any:
    physx = jc._kit_module("omni.physx")
    if physx is None:
        raise RuntimeError("omni.physx is not available: run this in Isaac Sim.")
    return physx.get_physx_interface().subscribe_physics_step_events(callback)


def _make_controller(robot_root: str, pace: float = 1.0) -> tuple[Any, Any, Any]:
    """(franka wrapper, PickPlaceController, articulation controller) for the Franka already in the scene."""
    app = jc._kit_module("omni.kit.app")
    if app is None:
        raise RuntimeError("Kit is not available: run this in Isaac Sim.")
    app.get_app().get_extension_manager().set_extension_enabled_immediate(EXAMPLES_EXTENSION, True)
    from isaacsim.robot.manipulators.examples.franka import Franka
    from isaacsim.robot.manipulators.examples.franka.controllers import PickPlaceController

    franka = Franka(prim_path=robot_root, name=f"mcp_live_pick_{abs(hash(robot_root))}")
    franka.initialize()
    controller = PickPlaceController(name="mcp_live_pick", gripper=franka.gripper, robot_articulation=franka,
                                     events_dt=[dt * pace for dt in EVENTS_DT])
    return franka, controller, franka.get_articulation_controller()


def live_pick(robot_root: str, object_path: str, place_position: Any, pace: float = 0.5, *,
              dry_run: bool = True) -> dict[str, Any]:
    """KIT-ONLY, SIMULATION STATE. The Franka at robot_root picks object_path and places it at place_position
    ([x, y, z] metres, world). pace (0.2-1): how fast the phases move on; lower gives the arm more time to arrive
    (needed when the object is far to the arm's side). dry_run=True checks everything and moves nothing. Needs Play."""
    base = {"action": "live_pick", "robot_root": robot_root, "object": object_path, "dry_run": jc._dry(dry_run)}
    try:
        place = _vector(place_position, "place_position")
    except ValueError as exc:
        return {**base, "ok": False, "error": str(exc)}
    if isinstance(pace, bool) or not isinstance(pace, (int, float)) or not 0.2 <= pace <= 1:
        return {**base, "ok": False, "error": "pace must be from 0.2 (slow, more time per phase) to 1 (Isaac's default)."}
    if _JOB.get("running"):
        return {**base, "ok": False, "error": "A live pick is already running: wait, or call live_pick_stop."}
    state, problem = jc._timeline_state()
    if problem:
        return {**base, "ok": False, "error": problem}
    if state != "playing":
        return {**base, "ok": False, "error": "Press Play first: the arm can only move while the simulation runs.",
                "timeline": state}
    start = _object_position(object_path)
    if start is None:
        return {**base, "ok": False, "error": f"No object at {object_path} (or Kit is not available)."}
    base_pos = _object_position(robot_root) or [0.0, 0.0, 0.0]
    far = [label for label, point in (("object", start), ("place", place))
           if ((point[0] - base_pos[0]) ** 2 + (point[1] - base_pos[1]) ** 2) ** 0.5 > 0.8]
    plan = {**base, "ok": True, "timeline": state, "object_start": [round(v, 3) for v in start],
            "place_position": place,
            "warnings": [f"The {w} is more than 0.8 m from the arm's base: it may not reach." for w in far],
            "phases": ["move above the object", "go down", "close the gripper", "lift", "carry to the place",
                       "go down", "open the gripper", "lift away"]}
    if base["dry_run"]:
        plan["next_step"] = "Dry run: nothing moved. Call again with dry_run=False and watch the viewport."
        return plan
    try:
        franka, controller, articulation = _make_controller(robot_root, float(pace))
    except Exception as exc:  # noqa: BLE001 - missing extension, not a Franka, physics not ready
        return {**base, "ok": False, "error": f"Could not set up the Franka controller: {type(exc).__name__}: {exc}",
                "hint": "Use the Franka from Create > Robots (its root is the prim with the articulation root)."}
    np = jc._kit_module("numpy")
    job = {"running": True, "steps": 0, "phase": "opening the gripper", "started": time.time(), "result": None,
           "errors": []}
    _JOB.clear()
    _JOB.update(job)

    def on_step(_dt: float) -> None:
        if not _JOB.get("running"):
            return
        if _JOB["steps"] > TIMEOUT_STEPS:
            _finish("timed_out")
            return
        try:
            joints = franka.get_joint_positions()
            if joints is None:
                return
            if _JOB["steps"] < OPEN_STEPS:
                # Seen live: without this the fingers started CLOSED (Isaac's examples open them in post_reset),
                # came down on the cube and pushed it away, and the "pick" closed on nothing.
                articulation.apply_action(franka.gripper.forward(action="open"))
                _JOB["steps"] += 1
                return
            where = _object_position(object_path) or start
            actions = controller.forward(
                picking_position=np.array(where), placing_position=np.array(place),
                current_joint_positions=joints, end_effector_offset=np.array([0.0, 0.005, 0.0]))
            articulation.apply_action(actions)
            _JOB["steps"] += 1
            _JOB["phase"] = int(controller.get_current_event()) if hasattr(controller, "get_current_event") else None
            if controller.is_done():
                _finish("done")
        except Exception as exc:  # noqa: BLE001 - Stop pressed, robot removed
            _JOB["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
            _finish("failed")

    try:
        _JOB["handle"] = _subscribe_physics(on_step)
    except Exception as exc:  # noqa: BLE001
        _JOB.clear()
        return {**base, "ok": False, "error": str(exc)}
    _JOB["object"], _JOB["place"] = object_path, place
    return {**plan, "running": True,
            "note": "The arm is moving. live_pick_status shows its progress; it stops by itself when done "
                    f"(or after {TIMEOUT_STEPS // 60} s of simulation time)."}


def _finish(result: str) -> None:
    _JOB["running"] = False
    _JOB["result"] = result
    handle = _JOB.pop("handle", None)
    if handle is not None and hasattr(handle, "unsubscribe"):
        handle.unsubscribe()


def live_pick_status() -> dict[str, Any]:
    """KIT-ONLY, READ-ONLY. Progress of the live pick: running or finished (done / timed_out / failed), the
    controller phase, physics steps, and where the object is now compared with the place position."""
    if not _JOB:
        return {"ok": True, "read_only": True, "running": False, "message": "No live pick has been started."}
    now = _object_position(_JOB.get("object", "")) if _JOB.get("object") else None
    place = _JOB.get("place")
    gap = (sum((a - b) ** 2 for a, b in zip(now[:2], place[:2])) ** 0.5) if now and place else None
    return {"ok": True, "read_only": True, "running": bool(_JOB.get("running")), "result": _JOB.get("result"),
            "phase": _JOB.get("phase"), "steps": _JOB.get("steps"), "seconds": round(time.time() - _JOB["started"], 1),
            "object_now": [round(v, 3) for v in now] if now else None,
            "distance_to_place_m": round(gap, 3) if gap is not None else None,
            "placed": bool(gap is not None and gap < 0.05 and _JOB.get("result") == "done"),
            "errors": _JOB.get("errors", [])[:5]}


def live_pick_stop() -> dict[str, Any]:
    """KIT-ONLY. Stop a running live pick (the arm keeps its last targets until Stop)."""
    was = bool(_JOB.get("running"))
    if was:
        _finish("stopped")
    return {"ok": True, "was_running": was}
