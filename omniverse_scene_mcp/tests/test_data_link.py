"""Live data link: CSV replayed on the simulation clock, JSON re-read live, values mapped, clamped and written."""
from __future__ import annotations

import json
import tempfile
import types
from pathlib import Path

from omniverse_scene_mcp import data_link as dl, features, joint_control as jc


def _rig(clock):
    callbacks, written = [], []
    stream = types.SimpleNamespace(create_subscription_to_pop=lambda cb, name: callbacks.append(cb) or
                                   types.SimpleNamespace(unsubscribe=lambda: callbacks.remove(cb)))
    modules = {
        "omni.kit.app": types.SimpleNamespace(get_app=lambda: types.SimpleNamespace(
            get_update_event_stream=lambda: stream)),
        "omni.timeline": types.SimpleNamespace(get_timeline_interface=lambda: types.SimpleNamespace(
            is_playing=lambda: True, get_current_time=lambda: clock["t"])),
    }
    saved = (jc._kit_module, dl._write, dl.time.monotonic)
    jc._kit_module = lambda name: modules.get(name)
    dl._write = lambda target, value: written.append((target, round(value, 3))) or "fake"
    tick = {"now": 0.0}
    dl.time.monotonic = lambda: tick["now"]

    def step(seconds):
        clock["t"] += seconds
        tick["now"] += seconds
        for cb in list(callbacks):
            cb(None)

    def restore():
        jc._kit_module, dl._write, dl.time.monotonic = saved
        dl._LINK.clear()

    return step, callbacks, written, restore


def test_csv_follows_the_simulation_clock_with_scale_and_clamp():
    folder = Path(tempfile.mkdtemp())
    (folder / "belt.csv").write_text("time,speed,temp\n0,0.2,20\n10,1.2,25\n", encoding="utf-8")
    preview = dl.preview_source(str(folder / "belt.csv"))
    assert preview["ok"] and preview["columns"] == ["speed", "temp"] and preview["ranges"]["speed"] == [0.2, 1.2]
    target = "/World/FactoryCell/BeltGraph.graph:variable:Velocity"
    mappings = [{"column": "speed", "target": target, "max": 1.0}]
    clock = {"t": 0.0}
    step, callbacks, written, restore = _rig(clock)
    try:
        preview = dl.start_link(str(folder / "belt.csv"), mappings)
        assert preview["dry_run"] and not callbacks
        json.dumps(preview, allow_nan=False)  # strict JSON: no Infinity / NaN
        assert preview["mappings"][0]["min"] is None and preview["mappings"][0]["max"] == 1.0
        assert dl.start_link(str(folder / "belt.csv"), mappings, dry_run=False)["running"]
        step(5.0)   # halfway: 0.2 -> 1.2 gives 0.7
        step(10.0)  # past the end: 1.2, clamped to 1.0
        assert written == [(target, 0.7), (target, 1.0)], written
        assert dl.link_status()["updates"] == 2
        assert dl.stop_link()["was_running"] and not callbacks
    finally:
        restore()


def test_json_feed_is_reread_and_bad_input_is_refused():
    folder = Path(tempfile.mkdtemp())
    feed = folder / "live.json"
    feed.write_text(json.dumps({"lux": 300}), encoding="utf-8")
    clock = {"t": 0.0}
    step, callbacks, written, restore = _rig(clock)
    try:
        dl.start_link(str(feed), [{"column": "lux", "target": "/World/Dome.inputs:intensity", "scale": 2}],
                      period=1.0, dry_run=False)
        step(1.0)
        feed.write_text(json.dumps({"lux": 500}), encoding="utf-8")  # another program updates the feed
        step(0.5)   # before the next period: not re-read yet
        step(0.6)
        assert [v for _, v in written] == [600, 1000], written
        dl.stop_link()
    finally:
        restore()
    assert not dl.start_link(str(feed), [{"column": "nope", "target": "/A.b"}])["ok"]
    assert not dl.start_link(str(feed), [{"column": "lux", "target": "World"}])["ok"]
    assert not dl.preview_source(str(folder / "x.txt"))["ok"]
    assert features.FEATURES["data_link_start"].mutates and not features.FEATURES["data_link_preview"].mutates


def test_stop_puts_graph_variables_back():
    # Seen live: the belt stayed at speed 0 after data_link_stop and even after Stop/Play.
    import sys

    state = {"value": 0.2}
    variable = types.SimpleNamespace(get=lambda ctx: state["value"], set=lambda ctx, v: state.update(value=v))
    graph = types.SimpleNamespace(find_variable=lambda name: variable, get_default_graph_context=lambda: "ctx")
    og = types.SimpleNamespace(get_graph_by_path=lambda path: graph)
    saved = jc._kit_module
    jc._kit_module = lambda name: og if name == "omni.graph.core" else None
    try:
        dl._LINK.clear()
        assert dl._write("/World/G.graph:variable:Velocity", 0.0) == "graph variable" and state["value"] == 0.0
        dl._write("/World/G.graph:variable:Velocity", 0.5)
        stopped = dl.stop_link()
        assert stopped["graph_variables_restored"] == 1 and state["value"] == 0.2, (stopped, state)
    finally:
        jc._kit_module = saved
        dl._LINK.clear()
