"""Record / replay / compare robot joint motion, with a fake articulation and a fake Kit frame loop."""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

from omniverse_scene_mcp import features, joint_control as jc, motion_recorder as mr


class _Robot:
    api = "fake"

    def __init__(self):
        self.q = [0.0, 0.0]
        self.sent = []

    def names(self):
        return ["shoulder", "slider"]

    def kinds(self):
        return ["revolute", "prismatic"]

    def state(self):
        return {"position": list(self.q), "velocity": [0, 0], "effort": [0, 0]}

    def set_targets(self, values, indices):
        self.sent.append((list(values), list(indices)))


def _rig(robot):
    clock = {"t": 0.0}
    callbacks = []
    saved = (jc._open_live, mr._timeline_time, mr._subscribe, os.environ.get("OMNIVERSE_ROOT"))
    folder = tempfile.mkdtemp()
    jc._open_live = lambda root: (robot, None, "playing", 1.0)
    mr._timeline_time = lambda: clock["t"]

    class _Handle:
        def __init__(self, cb):
            self.cb = cb
            callbacks.append(cb)

        def unsubscribe(self):
            callbacks.remove(self.cb)

    mr._subscribe = _Handle
    os.environ["OMNIVERSE_ROOT"] = folder

    def tick(seconds):
        clock["t"] += seconds
        for cb in list(callbacks):
            cb(None)

    def restore():
        jc._open_live, mr._timeline_time, mr._subscribe, root = saved
        if root is None:
            os.environ.pop("OMNIVERSE_ROOT", None)
        else:
            os.environ["OMNIVERSE_ROOT"] = root
        mr._ACTIVE.clear()

    return tick, callbacks, folder, restore


def test_record_save_replay_and_compare():
    robot = _Robot()
    tick, callbacks, folder, restore = _rig(robot)
    try:
        assert mr.record_start("/World/Arm")["dry_run"] and not callbacks  # dry run records nothing
        assert mr.record_start("/World/Arm", seconds=1.0, dry_run=False)["recording"]
        assert not mr.record_start("/World/Arm", dry_run=False)["ok"]  # one at a time
        for step in range(1, 13):
            robot.q = [math.radians(10 * step), 0.01 * step]
            tick(0.1)
        saved = mr.record_stop("wave")
        assert saved["ok"] and saved["samples"] == 10 and saved["seconds"] == 1.0, saved  # stops after 1 s
        assert Path(saved["file"]).parent == Path(folder) / "isaac_study" / "recordings"
        assert mr.record_stop()["ok"] is False and not callbacks
        mr.record_start("/World/Arm", seconds=1.0, dry_run=False)
        tick(0.1)
        assert Path(mr.record_stop("wave")["file"]).name == "wave_1.json"  # never overwrites
        assert [r["file"] for r in mr.list_recordings()["recordings"]] == ["wave.json", "wave_1.json"]

        assert mr.replay_motion("wave")["dry_run"] and not robot.sent
        assert mr.replay_motion("wave", speed=2.0, dry_run=False)["plays_for_seconds"] == 0.5
        robot.sent.clear()
        tick(0.25)  # t = 0.5 s of the recording at double speed
        values, joints = robot.sent[-1]
        assert joints == [0, 1] and math.isclose(math.degrees(values[0]), 50, abs_tol=1e-6), values
        tick(1.0)
        assert not callbacks and mr.replay_stop()["was_running"] is False  # ended by itself

        same = mr.compare_motion("wave", "wave")
        assert same["ok"] and "same motion" in same["verdict"]
        assert not mr.compare_motion("wave", "missing")["ok"]
        assert not mr.replay_motion("wave", speed=9)["ok"]
    finally:
        restore()
    assert features.FEATURES["robot_record_start"].mutates and features.FEATURES["robot_compare_motion"].mutates is False
