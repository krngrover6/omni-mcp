"""Live pick-and-place driver, with a fake Franka controller and a fake physics-step loop."""
from __future__ import annotations

import types

from omniverse_scene_mcp import features, joint_control as jc, live_pick as lp


class _Controller:
    def __init__(self, steps_to_finish):
        self.left, self.calls = steps_to_finish, []

    def forward(self, **kwargs):
        self.calls.append(kwargs)
        self.left -= 1
        return "action"

    def is_done(self):
        return self.left <= 0

    def get_current_event(self):
        return 9 - max(0, self.left)


def _rig(timeline="playing", positions=None):
    positions = positions or {"/World/Cube": [0.5, 0.0, 0.03], "/World/Franka": [0.0, 0.0, 0.0]}
    callbacks, applied = [], []
    controller = _Controller(3)
    saved = (jc._timeline_state, lp._object_position, lp._subscribe_physics, lp._make_controller, jc._kit_module)
    jc._timeline_state = lambda: (timeline, None)
    lp._object_position = lambda path: positions.get(path)

    class _Handle:
        def __init__(self, cb):
            callbacks.append(cb)
            self.cb = cb

        def unsubscribe(self):
            callbacks.remove(self.cb)

    lp._subscribe_physics = _Handle
    gripper = types.SimpleNamespace(forward=lambda action: f"gripper {action}")
    franka = types.SimpleNamespace(get_joint_positions=lambda: [0.0] * 9, gripper=gripper)
    lp._make_controller = lambda root, pace=1.0: (franka, controller, types.SimpleNamespace(apply_action=applied.append))
    jc._kit_module = lambda name: types.SimpleNamespace(array=list) if name == "numpy" else saved[4](name)

    def restore():
        jc._timeline_state, lp._object_position, lp._subscribe_physics, lp._make_controller, jc._kit_module = saved
        lp._JOB.clear()

    return callbacks, applied, controller, positions, restore


def test_live_pick_drives_the_controller_each_physics_step_until_done():
    callbacks, applied, controller, positions, restore = _rig()
    try:
        preview = lp.live_pick("/World/Franka", "/World/Cube", [0.3, 0.4, 0.05])
        assert preview["ok"] and preview["dry_run"] and not callbacks and not preview["warnings"], preview
        assert not lp.live_pick("/World/Franka", "/World/Cube", [1, 2])["ok"]
        assert not lp.live_pick("/World/Franka", "/World/Nope", [0.3, 0.4, 0.05])["ok"]
        started = lp.live_pick("/World/Franka", "/World/Cube", [0.3, 0.4, 0.05], dry_run=False)
        assert started["running"] and len(callbacks) == 1
        assert not lp.live_pick("/World/Franka", "/World/Cube", [0.3, 0.4, 0.05], dry_run=False)["ok"]  # one at a time
        for _ in range(lp.OPEN_STEPS + 5):
            for cb in list(callbacks):
                cb(1 / 60)
        # Seen live: the gripper must open BEFORE the controller runs, or closed fingers knock the cube away.
        assert applied == ["gripper open"] * lp.OPEN_STEPS + ["action"] * 3 and not callbacks, applied[-5:]
        assert controller.calls[0]["picking_position"] == [0.5, 0.0, 0.03]
        positions["/World/Cube"] = [0.31, 0.4, 0.03]
        status = lp.live_pick_status()
        assert status["result"] == "done" and status["placed"] and status["steps"] == lp.OPEN_STEPS + 3, status
        assert lp.live_pick_stop()["was_running"] is False
    finally:
        restore()


def test_live_pick_needs_play_and_warns_about_reach():
    _, _, _, _, restore = _rig(timeline="stopped")
    try:
        assert "Play" in lp.live_pick("/World/Franka", "/World/Cube", [0.3, 0.4, 0.05])["error"]
    finally:
        restore()
    _, _, _, _, restore = _rig(positions={"/World/Cube": [2.0, 0, 0.03], "/World/Franka": [0, 0, 0]})
    try:
        assert lp.live_pick("/World/Franka", "/World/Cube", [0.3, 0.4, 0.05])["warnings"]
    finally:
        restore()
    _, _, _, _, restore = _rig(positions={"/World/Cube": [0.6, 0, 0.03], "/World/Franka": [0, 0, 0]})
    try:  # seen live: a 155 degree swing flung the package out; place it behind the arm and get warned
        behind = lp.live_pick("/World/Franka", "/World/Cube", [-0.5, 0.1, 0.05])["warnings"]
        beside = lp.live_pick("/World/Franka", "/World/Cube", [0.3, 0.4, 0.05])["warnings"]
        assert any("swing" in w for w in behind) and not any("swing" in w for w in beside), (behind, beside)
    finally:
        restore()
    assert not lp.live_pick("/World/Franka", "/World/Cube", [0.3, 0.4, 0.05], pace=5)["ok"]
    assert features.FEATURES["robot_live_pick"].mutates and not features.FEATURES["robot_live_pick_status"].mutates


def test_packed_neighbours_are_warned_about():
    # Seen live: four packages piled at a stopper ~3 cm apart; the fingers landed on the neighbours.
    try:
        from pxr import Gf, Usd, UsdGeom, UsdPhysics
    except ImportError:  # pxr is an optional extra: skip without it
        return

    stage = Usd.Stage.CreateInMemory()
    for name, y in (("A", 0.0), ("B", 0.06), ("Far", 0.5)):
        cube = UsdGeom.Cube.Define(stage, f"/World/{name}")
        UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(0.5, y, 0.03))
        UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())
    belt = UsdGeom.Cube.Define(stage, "/World/Belt")
    UsdPhysics.RigidBodyAPI.Apply(belt.GetPrim()).CreateKinematicEnabledAttr(True)
    UsdGeom.XformCommonAPI(belt).SetTranslate(Gf.Vec3d(0.5, 0.0, 0.0))
    saved = jc._kit_module
    jc._kit_module = lambda name: types.SimpleNamespace(
        get_context=lambda: types.SimpleNamespace(get_stage=lambda: stage)) if name == "omni.usd" else None
    try:
        assert lp._neighbours("/World/A", [0.5, 0.0, 0.03]) == ["/World/B"]  # not the far box, not the belt
        assert lp._neighbours("/World/Far", [0.5, 0.5, 0.03]) == []
    finally:
        jc._kit_module = saved
