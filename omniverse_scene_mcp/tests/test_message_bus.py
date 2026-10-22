"""message_bus: event planning, sending and listening against FAKE omni/carb modules, plus pure-pxr graph checks.

Fixture-free test_* functions. Fake Kit modules go into sys.modules only for one test and the previous entries are
put back; inside a real Kit process (carb / omni.kit.app loaded) the Kit tests return at once, so a live Kit's
modules are never replaced. The graph checks build in-memory stages; the optional smoke test opens the real bag
asset read-only and never saves it.
"""

import asyncio
import contextlib
import json
import os
import sys
import time
import types
from pathlib import Path

from omniverse_scene_mcp import message_bus as mb

try:
    from pxr import Sdf, Usd
except ImportError:  # the graph checks need pxr; every other test runs without it
    Sdf = Usd = None

_IN_KIT = "carb" in sys.modules or "omni.kit.app" in sys.modules
_NAMES = (
    "omni",
    "omni.kit",
    "omni.kit.app",
    "omni.kit.livestream",
    "omni.kit.livestream.messaging",
    "carb",
    "carb.events",
    "carb.eventdispatcher",
    "carb.settings",
)
_MISSING = object()
_BAG = Path(__file__).resolve().parents[2] / "bag_configurator_course" / "assets" / "Step4_Web_Savepoint.usd"


# ------------------------------------------------------------------------------------------------ fakes


class FakeGuard:
    def __init__(self, dispatcher, name, callback):
        self.dispatcher, self.name, self.callback, self.resets = dispatcher, name, callback, 0

    def reset(self):
        self.resets += 1
        if self in self.dispatcher.observers:
            self.dispatcher.observers.remove(self)


class FakeEvent:
    def __init__(self, name, payload):
        self.event_name = name
        self.payload = payload


class FakeDispatcher:
    def __init__(self):
        self.observers = []
        self.subscribed = []

    def observe_event(self, event_name=None, on_event=None, observer_name=None, **_kw):
        guard = FakeGuard(self, event_name, on_event)
        self.observers.append(guard)
        self.subscribed.append((event_name, observer_name))
        return guard

    def fire(self, name, payload):
        for guard in list(self.observers):
            if guard.name == name:
                guard.callback(FakeEvent(name, payload))


class FakeStream:
    def __init__(self):
        self.pushed = []

    def push(self, event_type, payload=None):
        self.pushed.append((event_type, payload))


class FakeApp:
    def __init__(self, on_update=None):
        self.stream = FakeStream()
        self.updates = 0
        self.on_update = on_update

    def get_message_bus_event_stream(self):
        return self.stream

    async def next_update_async(self):
        self.updates += 1
        if self.on_update:
            self.on_update(self.updates)
        await asyncio.sleep(0)


def _type_from_string(name):
    return hash(("type", name)) & 0xFFFFFFFF


def _kit_modules(*, queue=True, messaging=False, on_update=None, receive_setting=None):
    app_module = types.ModuleType("omni.kit.app")
    app_module.queued, app_module.aliases = [], []
    app_module.app = FakeApp(on_update)
    app_module.get_app = lambda: app_module.app
    app_module.register_event_alias = lambda event_type, name, push_name=None: app_module.aliases.append(
        (event_type, name)) or True
    if queue:
        app_module.queue_event = lambda name, payload=None, **kw: app_module.queued.append((name, payload))
    events = types.ModuleType("carb.events")
    events.type_from_string = _type_from_string
    dispatcher_module = types.ModuleType("carb.eventdispatcher")
    dispatcher_module.dispatcher = FakeDispatcher()
    dispatcher_module.get_eventdispatcher = lambda: dispatcher_module.dispatcher
    settings_module = types.ModuleType("carb.settings")

    class _Settings:
        def get_as_string(self, path):
            return receive_setting if path == mb._RECEIVE_SETTING else ""

    settings_module.get_settings = lambda: _Settings()
    modules = {"omni.kit.app": app_module, "carb.events": events, "carb.eventdispatcher": dispatcher_module,
               "carb.settings": settings_module}
    if messaging:
        modules["omni.kit.livestream.messaging"] = types.ModuleType("omni.kit.livestream.messaging")
    return modules


@contextlib.contextmanager
def _fake_kit(modules):
    """Install fake omni/carb modules; every other omni/carb name fails to import."""
    saved = {name: sys.modules.get(name, _MISSING) for name in _NAMES}
    try:
        for name in _NAMES:
            sys.modules[name] = None  # makes `import name` raise ImportError
        for package in ("omni", "omni.kit", "omni.kit.livestream", "carb"):
            module = types.ModuleType(package)
            module.__path__ = []  # nothing on disk: submodules not faked here cannot be imported
            sys.modules[package] = module
        sys.modules.update(modules)
        mb._LISTENERS.clear()
        yield modules
    finally:
        mb._LISTENERS.clear()
        for name, module in saved.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@contextlib.contextmanager
def _no_kit():
    with _fake_kit({}) as modules:
        yield modules


# ------------------------------------------------------------------------------------------- name variants


def test_event_name_variants_message_bus_and_custom_names():
    v = mb.event_name_variants("setColor")
    assert v["ok"] and v["case_sensitive"] is True and v["has_custom_prefix"] is False
    assert v["message_bus"]["graph_event_name"] == "setColor" == v["message_bus"]["send_event_name"]
    assert v["message_bus"]["immediate_event_name"] == "setColor:immediate"
    assert v["custom_event"]["graph_event_name"] == "setColor"
    assert v["custom_event"]["send_event_name"] == "omni.graph.action.setColor"
    assert v["livestream"]["kit_receive_event"] == "omni.kit.livestream.receive_message"
    for look_alike in ("setcolor", "SetColor", "SETCOLOR", "omni.graph.action.setColor"):
        assert look_alike in v["will_not_match"]
    assert "setColor" not in v["will_not_match"]


def test_event_name_variants_with_prefix_and_bad_names():
    v = mb.event_name_variants("omni.graph.action.startConfig")
    assert v["has_custom_prefix"] and v["custom_event"]["graph_event_name"] == "startConfig"
    assert v["custom_event"]["send_event_name"] == "omni.graph.action.startConfig"
    assert "startConfig" in v["will_not_match"]
    for bad in ("", " setColor", "set Color", None, 5, "x" * 300):
        assert mb.event_name_variants(bad)["ok"] is False


# ------------------------------------------------------------------------------------------- plan_event


def test_plan_event_kit_snippet_and_web_message():
    plan = mb.plan_event("setColor", {"colors": "Black"})
    assert plan["ok"] and plan["dry_run"] is True and plan["target"] == "kit"
    assert plan["event_type"] == "setColor" and plan["payload"] == {"colors": "Black"}
    assert plan["expected_outputs"] == ["outputs:colors"]
    assert plan["equivalent_web_message"] == {"event_type": "setColor", "payload": {"colors": "Black"}}
    assert json.loads(plan["equivalent_web_message_json"]) == plan["equivalent_web_message"]
    how = plan["how"]
    assert "omni.kit.app.queue_event(EVENT, payload=PAYLOAD)" in how
    assert "register_event_alias(carb.events.type_from_string(EVENT), EVENT)" in how
    assert "EVENT = 'setColor'" in how and "PAYLOAD = {'colors': 'Black'}" in how
    compile(how, "<snippet>", "exec")  # a beginner can paste it: it is valid Python


def test_plan_event_custom_and_livestream_targets():
    custom = mb.plan_event("startConfig", None, target="custom")
    assert custom["ok"] and custom["event_type"] == "omni.graph.action.startConfig"
    assert custom["graph_event_name"] == "startConfig" and custom["expected_outputs"] == []
    compile(custom["how"], "<snippet>", "exec")
    data = mb.plan_event("startConfig", {"x": 1}, target="custom")
    assert any("Custom Event" in w for w in data["warnings"])
    live = mb.plan_event("setColor", {"colors": "Pink"}, target="livestream")
    assert live["ok"] and live["event_type"] == "setColor"
    assert live["kit_receive_event"] == "omni.kit.livestream.receive_message"
    assert "json.dumps(MESSAGE)" in live["how"]
    compile(live["how"], "<snippet>", "exec")
    assert mb.plan_event("setColor", {}, target="browser")["ok"] is False


def test_plan_event_accepts_lists_and_tuple_arrays():
    plan = mb.plan_event("e", {"names": ["a", "b"], "point": (1, 2.5, 3), "pts": [[0, 0, 1], [1, 2, 3]],
                               "flag": True, "n": -4})
    assert plan["ok"], plan
    assert plan["payload"]["point"] == [1, 2.5, 3] and plan["payload"]["pts"] == [[0, 0, 1], [1, 2, 3]]


def test_plan_event_rejects_nested_huge_and_non_json_payloads():
    cases = {
        "nested": {"colors": {"value": "Black"}},
        "none": {"colors": None},
        "bytes": {"colors": b"Black"},
        "set": {"colors": {"Black"}},
        "nan": {"x": float("nan")},
        "big int": {"x": 2**70},
        "mixed": {"x": [1, "a"]},
        "deep": {"x": [[1, [2]], [3, 4]]},
        "ragged": {"x": [[1, 2], [3]]},
        "empty list": {"x": []},
        "bad key": {"outputs:colors": "Black"},
        "digit key": {"1colors": "Black"},
        "space key": {"my colors": "Black"},
        "huge": {"x": "a" * (mb.MAX_PAYLOAD_BYTES + 1)},
        "too many keys": {f"k{i}": i for i in range(mb.MAX_KEYS + 1)},
        "not a dict": ["colors", "Black"],
    }
    for label, payload in cases.items():
        plan = mb.plan_event("setColor", payload)
        assert plan["ok"] is False, label
        assert plan.get("errors") or plan.get("message"), label
    long_list = mb.plan_event("e", {"x": list(range(mb.MAX_LIST_ITEMS + 1))})
    assert long_list["ok"] is False
    warn = mb.plan_event("e", {"id": "abc"})
    assert warn["ok"] and any("transport" in w for w in warn["warnings"])
    assert mb.plan_event("omni.graph.action.setColor", {})["warnings"]


# ------------------------------------------------------------------------------------------- send_event


def test_send_event_dry_run_never_sends():
    if _IN_KIT:
        return
    with _fake_kit(_kit_modules()) as modules:
        result = mb.send_event("setColor", {"colors": "Black"})
        assert result["ok"] and result["dry_run"] is True and result["sent"] is False
        app = modules["omni.kit.app"]
        assert app.queued == [] and app.aliases == [] and app.app.stream.pushed == []


def test_send_event_queues_after_alias():
    if _IN_KIT:
        return
    with _fake_kit(_kit_modules()) as modules:
        result = mb.send_event("setColor", {"colors": "Black"}, dry_run=False)
        app = modules["omni.kit.app"]
        assert result["ok"] and result["sent"] is True and result["api"] == "omni.kit.app.queue_event"
        assert app.aliases == [(_type_from_string("setColor"), "setColor")]
        assert app.queued == [("setColor", {"colors": "Black"})]
        custom = mb.send_event("startConfig", None, dry_run=False, target="custom")
        assert custom["ok"] and app.queued[-1] == ("omni.graph.action.startConfig", {})
        assert app.aliases[-1] == (_type_from_string("omni.graph.action.startConfig"), "omni.graph.action.startConfig")


def test_send_event_falls_back_to_legacy_push():
    if _IN_KIT:
        return
    with _fake_kit(_kit_modules(queue=False)) as modules:
        result = mb.send_event("setColor", {"colors": "White"}, dry_run=False)
        assert result["ok"] and "deprecated" in result["api"]
        assert modules["omni.kit.app"].app.stream.pushed == [(_type_from_string("setColor"), {"colors": "White"})]


def test_send_event_livestream_wraps_like_web_client():
    if _IN_KIT:
        return
    with _fake_kit(_kit_modules(messaging=False)) as modules:
        refused = mb.send_event("setColor", {"colors": "Pink"}, dry_run=False, target="livestream")
        assert refused["ok"] is False and "messaging" in refused["message"]
        assert modules["omni.kit.app"].queued == []
    with _fake_kit(_kit_modules(messaging=True, receive_setting="custom.receive")) as modules:
        result = mb.send_event("setColor", {"colors": "Pink"}, dry_run=False, target="livestream")
        assert result["ok"] and result["bus_event"] == "custom.receive"
        name, payload = modules["omni.kit.app"].queued[0]
        assert name == "custom.receive"
        assert json.loads(payload["message"]) == {"event_type": "setColor", "payload": {"colors": "Pink"}}


def test_send_event_invalid_payload_or_no_kit():
    if _IN_KIT:
        return
    with _fake_kit(_kit_modules()) as modules:
        bad = mb.send_event("setColor", {"colors": {"nested": 1}}, dry_run=False)
        assert bad["ok"] is False and modules["omni.kit.app"].queued == []
    with _no_kit():
        result = mb.send_event("setColor", {"colors": "Black"}, dry_run=False)
        assert result == {"ok": False, "message": "Kit not available", "detail": "omni.kit.app is not importable"}
        assert mb.send_event("setColor", {"colors": "Black"})["ok"] is True  # a dry run needs no Kit


# ------------------------------------------------------------------------------------------- listening


def test_listen_once_returns_payload_and_unsubscribes():
    if _IN_KIT:
        return
    holder = {}

    def on_update(count):
        if count == 3:
            holder["dispatcher"].fire("setColor", {"colors": "Black", "sender_id": 7})

    with _fake_kit(_kit_modules(on_update=on_update)) as modules:
        dispatcher = modules["carb.eventdispatcher"].dispatcher
        holder["dispatcher"] = dispatcher
        result = asyncio.run(mb.listen_once("setColor", timeout_s=5))
        assert result["ok"] and result["received"] is True and result["timed_out"] is False
        assert result["payload"] == {"colors": "Black", "sender_id": 7}
        assert result["unsubscribed"] is True and dispatcher.observers == []
        assert dispatcher.subscribed[0][0] == "setColor"
        assert modules["omni.kit.app"].aliases[-1][1] == "setColor"  # old-style pushes are seen too


def test_listen_once_timeout_unsubscribes():
    if _IN_KIT:
        return
    with _fake_kit(_kit_modules()) as modules:
        dispatcher = modules["carb.eventdispatcher"].dispatcher
        result = asyncio.run(mb.listen_once("setcolor", timeout_s=0.05))
        assert result["ok"] and result["received"] is False and result["timed_out"] is True
        assert result["unsubscribed"] is True and dispatcher.observers == []
        custom = asyncio.run(mb.listen_once("startConfig", timeout_s=0.02, target="custom"))
        assert custom["event_name"] == "omni.graph.action.startConfig" and dispatcher.observers == []


def test_listen_once_unsubscribes_when_cancelled_by_error():
    if _IN_KIT:
        return

    async def boom():
        raise RuntimeError("update failed")

    with _fake_kit(_kit_modules()) as modules:
        dispatcher = modules["carb.eventdispatcher"].dispatcher
        try:
            asyncio.run(mb.listen_once("setColor", timeout_s=1, wait=boom))
        except RuntimeError:
            pass
        else:
            raise AssertionError("the error should propagate")
        assert dispatcher.observers == []


def test_listen_once_argument_checks_and_no_kit():
    assert asyncio.run(mb.listen_once("setColor", timeout_s=0))["ok"] is False
    assert asyncio.run(mb.listen_once("setColor", timeout_s=500))["ok"] is False
    assert asyncio.run(mb.listen_once("setColor", target="livestream"))["ok"] is False
    if _IN_KIT:
        return
    with _no_kit():
        result = asyncio.run(mb.listen_once("setColor", timeout_s=0.01))
        assert result["ok"] is False and result["message"] == "Kit not available"


def test_start_listener_status_and_stop():
    if _IN_KIT:
        return
    with _fake_kit(_kit_modules()) as modules:
        dispatcher = modules["carb.eventdispatcher"].dispatcher
        started = mb.start_listener("setColor", timeout_s=30)
        assert started["ok"] and len(dispatcher.observers) == 1
        lid = started["listener_id"]
        waiting = mb.listener_status(lid)
        assert waiting["received"] is False and waiting["active"] is True
        dispatcher.fire("setColor", {"colors": "White"})
        done = mb.listener_status(lid)
        assert done["received"] and done["payload"] == {"colors": "White"} and done["active"] is False
        assert dispatcher.observers == []
        assert mb.stop_listener(lid)["stopped"] is True and mb.stop_listener(lid)["stopped"] is False
        short = mb.start_listener("setColor", timeout_s=0.001)
        time.sleep(0.05)  # more than Windows' ~16 ms monotonic clock tick
        timed = mb.listener_status(short["listener_id"])
        assert timed["timed_out"] and not timed["active"] and dispatcher.observers == []
        stopped = mb.start_listener("setColor", timeout_s=30)
        mb.stop_listener(stopped["listener_id"])
        assert dispatcher.observers == []
        assert mb.listener_status("nope")["ok"] is False


# ------------------------------------------------------------------------------------- graph listener check


def _graph_stage(event_name="setColor", output="colors", only_playback=False, custom=None):
    stage = Usd.Stage.CreateInMemory()
    looks = stage.DefinePrim("/World/Bag/Looks", "Scope")
    vset = looks.GetVariantSets().AddVariantSet("Leather")
    for name in ("Black", "Pink", "White"):
        vset.AddVariant(name)
    stage.DefinePrim("/World/Graphs", "Scope")
    stage.DefinePrim("/World/Graphs/VC", "OmniGraph")
    listener = stage.DefinePrim("/World/Graphs/VC/on_messagebus_event", "OmniGraphNode")
    listener.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set(mb.MESSAGE_BUS_LISTENER)
    listener.CreateAttribute("inputs:eventName", Sdf.ValueTypeNames.Token).Set(event_name)
    listener.CreateAttribute("inputs:onlyPlayback", Sdf.ValueTypeNames.Bool).Set(only_playback)
    listener.CreateAttribute("outputs:execOut", Sdf.ValueTypeNames.UInt)
    out = listener.CreateAttribute(f"outputs:{output}", Sdf.ValueTypeNames.Token)
    setter = stage.DefinePrim("/World/Graphs/VC/set_variant_selection", "OmniGraphNode")
    setter.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set(mb.SET_VARIANT)
    setter.CreateAttribute("inputs:variantSetName", Sdf.ValueTypeNames.Token).Set("Leather")
    setter.CreateAttribute("inputs:variantName", Sdf.ValueTypeNames.Token).AddConnection(out.GetPath())
    setter.CreateRelationship("inputs:prim").AddTarget("/World/Bag/Looks")
    sender = stage.DefinePrim("/World/Graphs/VC/send_messagebus_event", "OmniGraphNode")
    sender.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set(mb.MESSAGE_BUS_SENDER)
    sender.CreateAttribute("inputs:eventName", Sdf.ValueTypeNames.Token).Set(event_name)
    other = stage.DefinePrim("/World/Graphs/VC/on_backdrop", "OmniGraphNode")
    other.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set(mb.MESSAGE_BUS_LISTENER)
    other.CreateAttribute("inputs:eventName", Sdf.ValueTypeNames.Token).Set("setBackdrop")
    if custom:
        node = stage.DefinePrim("/World/Graphs/VC/on_custom_event", "OmniGraphNode")
        node.CreateAttribute("node:type", Sdf.ValueTypeNames.Token).Set(mb.CUSTOM_LISTENER)
        node.CreateAttribute("inputs:eventName", Sdf.ValueTypeNames.Token).Set(custom)
    return stage


def test_check_graph_listener_exact_match_with_payload_and_variants():
    if Usd is None:
        return
    stage = _graph_stage()
    result = mb.check_graph_listener(stage, "setColor", payload={"colors": "Black"})
    assert result["ok"] and result["listening"] and result["read_only"]
    (entry,) = result["exact"]
    assert entry["path"] == "/World/Graphs/VC/on_messagebus_event" and entry["kind"] == "message_bus"
    assert entry["payload_outputs"] == ["colors"] and entry["only_playback"] is False
    assert entry["payload_check"]["used"] == ["colors"] and entry["payload_check"]["ignored"] == []
    (feed,) = entry["feeds"]
    assert feed["node_type"] == mb.SET_VARIANT and feed["variant_set"] == "Leather"
    assert feed["prims"] == ["/World/Bag/Looks"]
    assert feed["variants"] == {"/World/Bag/Looks": ["Black", "Pink", "White"]}
    assert feed["value_check"]["match"] == "exact"
    assert result["senders"][0]["path"] == "/World/Graphs/VC/send_messagebus_event"
    assert {"path": "/World/Graphs/VC/on_backdrop", "kind": "message_bus", "event_name": "setBackdrop"} in result[
        "other_listeners"]
    assert "warnings" not in result


def test_check_graph_listener_near_miss_payload_key_and_value_case():
    if Usd is None:
        return
    stage = _graph_stage()
    result = mb.check_graph_listener(stage, "setColor", payload={"Colors": "black"})
    entry = result["exact"][0]
    assert entry["payload_check"]["near_miss_keys"] == {"Colors": "colors"}
    assert entry["payload_check"]["ignored"] == ["Colors"]
    assert entry["payload_check"]["outputs_not_in_payload"] == ["colors"]
    assert any("'Colors'" in w and "'colors'" in w for w in result["warnings"])
    value = mb.check_graph_listener(stage, "setColor", payload={"colors": "black"})
    check = value["exact"][0]["feeds"][0]["value_check"]
    assert check["match"] == "near_miss" and check["closest"] == ["Black"]
    missing = mb.check_graph_listener(stage, "setColor", payload={"colors": "Red"})
    assert missing["exact"][0]["feeds"][0]["value_check"]["match"] == "missing"


def test_check_graph_listener_event_name_near_misses():
    if Usd is None:
        return
    stage = _graph_stage()
    lower = mb.check_graph_listener(stage, "setcolor")
    assert lower["listening"] is False and lower["exact"] == []
    (near,) = lower["near_misses"]
    assert near["event_name"] == "setColor" and any("capitalization" in r for r in near["reasons"])
    assert "Near misses" in lower["verdict"]
    prefixed = mb.check_graph_listener(stage, "omni.graph.action.setColor")
    assert prefixed["exact"] == [] and any("prefix" in r for r in prefixed["near_misses"][0]["reasons"])
    spelled = mb.check_graph_listener(stage, "setColour")
    assert spelled["near_misses"] and "similar" in spelled["near_misses"][0]["reasons"][0]
    nothing = mb.check_graph_listener(stage, "openDoor")
    assert nothing["exact"] == [] and nothing["near_misses"] == [] and nothing["listener_count"] == 2
    graph_upper = mb.check_graph_listener(_graph_stage(event_name="SetColor"), "setColor")
    assert graph_upper["near_misses"][0]["event_name"] == "SetColor"


def test_check_graph_listener_custom_event_and_only_playback():
    if Usd is None:
        return
    stage = _graph_stage(only_playback=True, custom="startConfig")
    custom = mb.check_graph_listener(stage, "startConfig")
    (entry,) = custom["exact"]
    assert entry["kind"] == "custom" and entry["listens_to"] == "omni.graph.action.startConfig"
    assert any("target='custom'" in w for w in custom["warnings"])
    assert mb.check_graph_listener(stage, "omni.graph.action.startConfig")["exact"][0]["kind"] == "custom"
    playing = mb.check_graph_listener(stage, "setColor")
    assert any("Only Simulate On Play" in w for w in playing["warnings"])


def test_check_graph_listener_argument_errors():
    assert mb.check_graph_listener(None, "setColor")["ok"] is False
    assert mb.check_graph_listener("Z:/definitely/missing.usda", "setColor")["ok"] is False
    if Usd is None:
        return
    stage = _graph_stage()
    assert mb.check_graph_listener(stage, "")["ok"] is False
    assert mb.check_graph_listener(stage, "setColor", payload={"colors": {"x": 1}})["ok"] is False


def test_check_graph_listener_does_not_dirty_the_stage():
    if Usd is None:
        return
    stage = _graph_stage()
    before = stage.GetRootLayer().ExportToString()
    mb.check_graph_listener(stage, "setColor", payload={"colors": "Pink"})
    assert stage.GetRootLayer().ExportToString() == before


def test_check_graph_listener_real_bag_asset_read_only():
    """Smoke test on the course's Step4 asset when it is present (opened read-only, never saved)."""
    if Usd is None or not _BAG.is_file():
        return
    stamp = (os.stat(_BAG).st_size, os.stat(_BAG).st_mtime_ns)
    result = mb.check_graph_listener(str(_BAG), "setColor", payload={"colors": "Black"})
    assert result["ok"] and result["listening"], result["verdict"]
    entry = result["exact"][0]
    assert entry["path"] == "/World/Graphs/VariantController/on_messagebus_event"
    assert entry["payload_outputs"] == ["colors"] and entry["only_playback"] is False
    feed = entry["feeds"][0]
    assert feed["variant_set"] == "Leather" and feed["value_check"]["match"] == "exact"
    assert mb.check_graph_listener(str(_BAG), "setcolor")["near_misses"]
    assert (os.stat(_BAG).st_size, os.stat(_BAG).st_mtime_ns) == stamp
