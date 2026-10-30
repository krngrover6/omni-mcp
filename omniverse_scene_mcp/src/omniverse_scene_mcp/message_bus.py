"""Message bus event tester: send/listen to Kit app message-bus events and check which graph nodes listen.

Lets a learner test an Action Graph that reacts to a web-client event (for example the handbag configurator's
'setColor' event with a 'colors' token) WITHOUT the browser.

Verified against the installed Kit 110.1.2 (kit-app-template/_build ... /kit -> kit-kernel 110.1.2) and Isaac Sim
6.1.0 extscache (omni.graph.action_nodes 2.11.0, omni.graph.action_nodes_core 2.11.0, omni.kit.livestream.messaging
1.3.3):

- omni.kit.app.queue_event(name, payload) dispatches "<name>:immediate" at once and queues "<name>" for the next
  app update on the global message bus (kit/kernel/py/omni/kit/app/_app.pyi). IApp.get_message_bus_event_stream()
  still exists but is wrapped with deprecated("Use Events 2.0") (kit/kernel/py/omni/kit/app/_impl/__init__.py).
- The OmniGraph 'On MessageBus Event' node (omni.graph.action.OnMessageBusEvent, C++) listens for EXACTLY the
  inputs:eventName token (case-sensitive, no prefix) and still uses Events 1.0, so a sender links the 1.0 type to
  the 2.0 name first: omni.kit.app.register_event_alias(carb.events.type_from_string(name), name)
  (omni.graph.action_nodes .../tests/test_action_graph_nodes_02.py, test_messagebusevent_nodes). Each payload key
  is copied into a matching DYNAMIC output outputs:<key>; keys without such an output are ignored
  (omni.graph.action_nodes_core .../ogn/docs/OgnOnMessageBusEvent.rst).
- The 'On Custom Event' node (omni.graph.action.OnCustomEvent, Python) observes "omni.graph.action." + eventName
  (OgnOnCustomEvent.py real_event_name); it carries no payload keys except an internal path.
- The web client (bag_configurator_course/reference_client/react-sample/src/Window.jsx
  _generateCustomMessageFromState) sends JSON.stringify({event_type, payload: {[attributeName]: value}}).
  omni.kit.livestream.messaging receives it as {"message": <that JSON>} on "omni.kit.livestream.receive_message",
  then calls omni.kit.app.queue_event(event_type, {**payload, "id", "sender_id"}) (messaging/scripts/extension.py,
  messaging/large/_extension.py). That extension is in Isaac Sim's extscache but NOT in the Composer _build.

send_event / listen_once / start_listener need Kit; their omni/carb imports are lazy, so this module imports fine
outside Kit and they then return {"ok": False, "message": "Kit not available"}. event_name_variants and plan_event
are pure Python; check_graph_listener needs only pxr and never edits or saves the stage.
"""

from __future__ import annotations

import asyncio
import difflib
import importlib
import itertools
import json
import math
import os
import re
import time
from collections.abc import Mapping
from typing import Any

__all__ = [
    "CUSTOM_EVENT_PREFIX",
    "LIVESTREAM_RECEIVE_EVENT",
    "MAX_PAYLOAD_BYTES",
    "TARGETS",
    "check_graph_listener",
    "event_name_variants",
    "listen_once",
    "listener_status",
    "plan_event",
    "send_event",
    "start_listener",
    "stop_listener",
]

CUSTOM_EVENT_PREFIX = "omni.graph.action."
LIVESTREAM_RECEIVE_EVENT = "omni.kit.livestream.receive_message"
_RECEIVE_SETTING = "exts/omni.kit.livestream.messaging/receive_message_event"
TARGETS = ("kit", "custom", "livestream")
# omni.kit.livestream.messaging sends a payload up to ~16 KiB as ONE message part (MESSAGE_PART_SIZE_BYTES).
MAX_PAYLOAD_BYTES = 16 * 1024
MAX_KEYS = 64
MAX_LIST_ITEMS = 4096
MAX_EVENT_NAME = 200
MAX_TIMEOUT_S = 120.0
MAX_LISTENERS = 16
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_TRANSPORT_KEYS = ("id", "sender_id", "part", "numParts")
_INT64 = 2**63

MESSAGE_BUS_LISTENER = "omni.graph.action.OnMessageBusEvent"
MESSAGE_BUS_SENDER = "omni.graph.action.SendMessageBusEvent"
CUSTOM_LISTENER = "omni.graph.action.OnCustomEvent"
CUSTOM_SENDER = "omni.graph.action.SendCustomEvent"
SET_VARIANT = "omni.graph.nodes.SetVariantSelection"
_STATIC_OUTPUTS = {"outputs:execOut", "outputs:path", "outputs:bundle"}
_NOT_KIT = "Kit not available"


# ------------------------------------------------------------------------------------------------ helpers


def _kit_module(name: str) -> Any:
    """The named omni/carb module, or None when it cannot be imported (not inside Kit, extension disabled)."""
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - ImportError, or a Kit extension that fails while loading
        return None


def _not_kit(detail: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "message": _NOT_KIT}
    if detail:
        result["detail"] = detail
    return result


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "message": message, **extra}


def _strip_prefix(name: str) -> str:
    return name[len(CUSTOM_EVENT_PREFIX):] if name.startswith(CUSTOM_EVENT_PREFIX) else name


def _check_event_name(event_name: Any) -> str | None:
    """Problem with an event name, or None when it is usable."""
    if not isinstance(event_name, str) or not event_name:
        return "event_name must be a non-empty string such as 'setColor'"
    if event_name != event_name.strip():
        return f"event_name {event_name!r} has leading/trailing spaces; graph event names never match those"
    if len(event_name) > MAX_EVENT_NAME:
        return f"event_name is longer than {MAX_EVENT_NAME} characters"
    if any(ch.isspace() or not ch.isprintable() for ch in event_name):
        return f"event_name {event_name!r} contains spaces or control characters"
    return None


def _wire_name(event_name: str, target: str) -> str:
    """The event name that is actually dispatched on the bus for this target."""
    if target == "custom":
        return CUSTOM_EVENT_PREFIX + _strip_prefix(event_name)
    return event_name


# ------------------------------------------------------------------------------------------- name variants


def event_name_variants(name: str) -> dict[str, Any]:
    """Which names a graph listens to versus what you must send, for one event name (all case-sensitive)."""
    problem = _check_event_name(name)
    if problem:
        return _error(problem)
    base = _strip_prefix(name)
    prefixed = name.startswith(CUSTOM_EVENT_PREFIX)
    look_alikes = []
    for candidate in (
        name.lower(),
        name.upper(),
        name[:1].swapcase() + name[1:],
        base if prefixed else CUSTOM_EVENT_PREFIX + name,
        name + ":immediate",
    ):
        if candidate != name and candidate not in look_alikes:
            look_alikes.append(candidate)
    explanation = [
        "Event names are compared exactly: 'setColor', 'setcolor' and 'SetColor' are three different events.",
        f"On MessageBus Event: set inputs:eventName to {name!r} and send {name!r} (no prefix).",
        f"On Custom Event: set inputs:eventName to {base!r}; the node really listens for "
        f"{CUSTOM_EVENT_PREFIX + base!r}, so a sender must use that prefixed name.",
        "omni.kit.app.queue_event(name) also fires name + ':immediate' at once; graph nodes react to the "
        "plain name on the next app update (next frame).",
        "A web client sends {\"event_type\": name, \"payload\": {...}}; omni.kit.livestream.messaging turns that "
        "into a bus event with exactly that name.",
    ]
    if prefixed:
        explanation.append(
            f"{name!r} already has the custom-event prefix: an On MessageBus Event node would need that full "
            f"name, while an On Custom Event node's eventName should be {base!r}."
        )
    return {
        "ok": True,
        "name": name,
        "case_sensitive": True,
        "has_custom_prefix": prefixed,
        "message_bus": {
            "node_type": MESSAGE_BUS_LISTENER,
            "graph_event_name": name,
            "send_event_name": name,
            "immediate_event_name": name + ":immediate",
            "events_1_0_type": f"carb.events.type_from_string({name!r})",
            "payload_keys_become": "outputs:<key> dynamic outputs on the node (added by you in the graph)",
        },
        "custom_event": {
            "node_type": CUSTOM_LISTENER,
            "graph_event_name": base,
            "send_event_name": CUSTOM_EVENT_PREFIX + base,
            "payload_keys_become": "nothing (only an internal path; use On MessageBus Event to carry data)",
        },
        "livestream": {
            "web_event_type": name,
            "kit_receive_event": LIVESTREAM_RECEIVE_EVENT,
            "becomes_bus_event": name,
        },
        "will_not_match": look_alikes,
        "explanation": explanation,
    }


# --------------------------------------------------------------------------------------- payload validation


def _scalar_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


def _check_scalar(value: Any, where: str, errors: list[str]) -> Any:
    if isinstance(value, bool) or isinstance(value, str):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                errors.append(f"{where}: text is not valid UTF-8")
        return value
    if isinstance(value, int):
        if not -_INT64 <= value < _INT64:
            errors.append(f"{where}: {value} does not fit a 64-bit integer attribute")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            errors.append(f"{where}: {value} is not a finite number (JSON cannot carry NaN/inf)")
        return value
    return None


def _describe(value: Any) -> str:
    if value is None:
        return "None/null (an attribute cannot be set to nothing)"
    if isinstance(value, Mapping):
        return "a nested object (an event payload is one flat level: key -> value)"
    return f"a {type(value).__name__} (not a JSON string/number/bool/list)"


def _check_value(value: Any, where: str, errors: list[str]) -> Any:
    kind = _scalar_kind(value)
    if kind is not None:
        return _check_scalar(value, where, errors)
    if isinstance(value, (list, tuple)):
        if not value:
            errors.append(f"{where}: empty list; carb dictionaries cannot carry empty values from Python")
            return []
        if len(value) > MAX_LIST_ITEMS:
            errors.append(f"{where}: {len(value)} items is more than {MAX_LIST_ITEMS}")
            return []
        if all(isinstance(item, (list, tuple)) for item in value):  # array of tuples, e.g. float3[]
            width = {len(item) for item in value}
            if len(width) != 1 or 0 in width:
                errors.append(f"{where}: inner lists must all have the same non-zero length (like float3[])")
                return []
            out = []
            for i, item in enumerate(value):
                if not all(_scalar_kind(x) == "number" for x in item):
                    errors.append(f"{where}[{i}]: inner lists may hold only numbers")
                    return []
                out.append([_check_scalar(x, f"{where}[{i}]", errors) for x in item])
            return out
        kinds = set()
        out = []
        for i, item in enumerate(value):
            item_kind = _scalar_kind(item)
            if item_kind is None:
                errors.append(f"{where}[{i}] is {_describe(item)}")
                return []
            kinds.add(item_kind)
            out.append(_check_scalar(item, f"{where}[{i}]", errors))
        if len(kinds) > 1:
            errors.append(f"{where}: mixes {sorted(kinds)}; an array attribute holds one type")
        return out
    errors.append(f"{where} is {_describe(value)}")
    return None


def _validate_payload(payload: Any, target: str) -> tuple[dict[str, Any], list[str], list[str], int]:
    """(normalized payload, errors, warnings, JSON size in bytes)."""
    errors: list[str] = []
    warnings: list[str] = []
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        return {}, [f"payload must be a dict like {{'colors': 'Black'}}, not {type(payload).__name__}"], [], 0
    if len(payload) > MAX_KEYS:
        return {}, [f"payload has {len(payload)} keys; the limit is {MAX_KEYS}"], [], 0
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not _KEY_RE.match(key):
            errors.append(
                f"payload key {key!r} must be an identifier (letters, digits, _; not starting with a digit; "
                "max 64): it becomes the attribute name outputs:<key>"
            )
            continue
        if key in _TRANSPORT_KEYS:
            warnings.append(f"payload key {key!r} is also used by the livestream transport and may be overwritten")
        clean[key] = _check_value(value, f"payload[{key!r}]", errors)
    size = 0
    if not errors:
        try:
            size = len(json.dumps(clean, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            errors.append(f"payload is not JSON-serializable: {exc}")
        else:
            if size > MAX_PAYLOAD_BYTES:
                errors.append(f"payload is {size} bytes of JSON; the limit is {MAX_PAYLOAD_BYTES} (one message part)")
    if target == "custom" and clean:
        warnings.append(
            "On Custom Event nodes do not copy payload keys to outputs; use an On MessageBus Event node to carry data"
        )
    return clean, errors, warnings, size


# -------------------------------------------------------------------------------------------- plan / snippet


def _snippet(event_name: str, payload: dict[str, Any], target: str, receive_event: str) -> str:
    payload_text = repr(payload)
    if target == "livestream":
        message = {"event_type": event_name, "payload": payload}
        return "\n".join([
            "# Paste into Window > Script Editor and press Run (Ctrl+Enter).",
            "# Pretends to be the web client: needs the omni.kit.livestream.messaging extension enabled.",
            "import json",
            "import omni.kit.app",
            "",
            f"MESSAGE = {message!r}  # exactly what the React client sends",
            "",
            "# The messaging extension unpacks this and re-sends MESSAGE['event_type'] with MESSAGE['payload'].",
            f"omni.kit.app.queue_event({receive_event!r}, payload={{\"message\": json.dumps(MESSAGE)}})",
            "print('sent web-style message', MESSAGE)",
        ])
    wire = _wire_name(event_name, target)
    lines = [
        "# Paste into Window > Script Editor and press Run (Ctrl+Enter).",
        "import carb.events",
        "import omni.kit.app",
        "",
    ]
    if target == "custom":
        lines.append(f"EVENT = {wire!r}  # On Custom Event listens for 'omni.graph.action.' + its eventName")
    else:
        lines.append(f"EVENT = {wire!r}  # must equal the node's inputs:eventName EXACTLY (case-sensitive)")
    lines += [
        f"PAYLOAD = {payload_text}  # each key fills the dynamic output outputs:<key> if the node has it",
        "",
        "# OmniGraph's event nodes still listen with old (Events 1.0) numeric types,",
        "# so link that type to the event name once. Doing it again is harmless.",
        "omni.kit.app.register_event_alias(carb.events.type_from_string(EVENT), EVENT)",
        "",
        "# Queue the event: the graph reacts on the next app update (the next frame).",
        "omni.kit.app.queue_event(EVENT, payload=PAYLOAD)",
        "print('sent', EVENT, PAYLOAD)",
    ]
    return "\n".join(lines)


def _receive_event_name() -> str:
    settings_module = _kit_module("carb.settings")
    getter = getattr(settings_module, "get_settings", None)
    if callable(getter):
        try:
            value = getter().get_as_string(_RECEIVE_SETTING)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return str(value)
    return LIVESTREAM_RECEIVE_EVENT


def plan_event(event_name: str, payload: Any = None, *, target: str = "kit") -> dict[str, Any]:
    """Validate an event and show how to send it, WITHOUT sending anything.

    target: 'kit' (straight onto the message bus, what an On MessageBus Event node hears), 'custom' (the
    'omni.graph.action.<name>' event an On Custom Event node hears) or 'livestream' (wrapped exactly like the
    web client's message; needs omni.kit.livestream.messaging in Kit).
    """
    problem = _check_event_name(event_name)
    if problem:
        return _error(problem, dry_run=True)
    if target not in TARGETS:
        return _error(f"target must be one of {list(TARGETS)}", dry_run=True)
    clean, errors, warnings, size = _validate_payload(payload, target)
    if errors:
        return _error("The payload cannot be sent as a message bus event.", dry_run=True, errors=errors,
                      warnings=warnings)
    if target != "custom" and event_name.startswith(CUSTOM_EVENT_PREFIX):
        warnings.append(
            f"{event_name!r} has the custom-event prefix; only an On MessageBus Event node with that FULL name "
            "would hear it. For On Custom Event nodes use target='custom' with the plain name."
        )
    if event_name.endswith(":immediate"):
        warnings.append("Names ending in ':immediate' are Kit's immediate twins; graph nodes listen to the plain name")
    receive_event = _receive_event_name() if target == "livestream" else LIVESTREAM_RECEIVE_EVENT
    wire = _wire_name(event_name, "kit" if target == "livestream" else target)
    web_event_type = wire
    web_message = {"event_type": web_event_type, "payload": clean}
    plan: dict[str, Any] = {
        "ok": True,
        "dry_run": True,
        "target": target,
        "event_type": wire,
        "graph_event_name": _strip_prefix(event_name) if target == "custom" else event_name,
        "payload": clean,
        "payload_bytes": size,
        "expected_outputs": [] if target == "custom" else [f"outputs:{key}" for key in clean],
        "how": _snippet(event_name, clean, target, receive_event),
        "equivalent_web_message": web_message,
        "equivalent_web_message_json": json.dumps(web_message, separators=(",", ":")),
        "notes": [
            "Nothing was sent (dry run).",
            "The graph reacts on the next app update. If the node's 'Only Simulate On Play' (inputs:onlyPlayback) "
            "is on, it reacts only while the timeline is playing.",
            "In the React sample (src/Window.jsx _onSelectColor) replace 'EVENT_NAME'/'ATTRIBUTE_NAME' with the "
            "same event name and payload key.",
        ],
    }
    if target == "livestream":
        plan["kit_receive_event"] = receive_event
        plan["notes"].append(
            "omni.kit.livestream.messaging adds 'id' and 'sender_id' to the payload it re-sends."
        )
    if target == "custom":
        plan["notes"].append("Through a web client this would be event_type 'omni.graph.action.<name>' (untested).")
    if warnings:
        plan["warnings"] = warnings
    return plan


# ------------------------------------------------------------------------------------------------- sending


def _register_alias(app_module: Any, events_module: Any, name: str) -> bool:
    """Link the Events 1.0 type to the 2.0 name, as the OmniGraph nodes need. True when it was attempted."""
    register = getattr(app_module, "register_event_alias", None)
    type_from_string = getattr(events_module, "type_from_string", None)
    if not callable(register) or not callable(type_from_string):
        return False
    try:
        register(type_from_string(name), name)  # False when already registered: that is fine
    except Exception:  # noqa: BLE001
        return False
    return True


def send_event(event_name: str, payload: Any = None, *, dry_run: bool = True, target: str = "kit") -> dict[str, Any]:
    """Send one message bus event inside Kit (dry_run=True by default: only plans it).

    Uses omni.kit.app.queue_event after register_event_alias (the path OmniGraph's own tests use); falls back to
    the deprecated get_message_bus_event_stream().push on a Kit without queue_event. Must run on Kit's main thread.
    """
    plan = plan_event(event_name, payload, target=target)
    if not plan.get("ok"):
        return plan
    if dry_run:
        plan["sent"] = False
        return plan
    app_module = _kit_module("omni.kit.app")
    if app_module is None:
        return _not_kit("omni.kit.app is not importable")
    events_module = _kit_module("carb.events")
    clean = plan["payload"]
    warnings = list(plan.get("warnings", []))
    try:
        if target == "livestream":
            if _kit_module("omni.kit.livestream.messaging") is None:
                return _error(
                    "omni.kit.livestream.messaging is not enabled here, so a web-style message would be ignored. "
                    "Enable that extension or use target='kit' (the same event the graph hears).",
                    dry_run=False, sent=False,
                )
            queue = getattr(app_module, "queue_event", None)
            if not callable(queue):
                return _error("This Kit has no omni.kit.app.queue_event", dry_run=False, sent=False)
            bus_name = plan["kit_receive_event"]
            bus_payload = {"message": plan["equivalent_web_message_json"]}
            queue(bus_name, payload=bus_payload)
            api = "omni.kit.app.queue_event (livestream receive event)"
        else:
            bus_name, bus_payload = plan["event_type"], clean
            aliased = _register_alias(app_module, events_module, bus_name)
            if not aliased:
                warnings.append("Could not register the Events 1.0 alias; OmniGraph event nodes may not hear this")
            queue = getattr(app_module, "queue_event", None)
            if callable(queue):
                queue(bus_name, payload=bus_payload)
                api = "omni.kit.app.queue_event"
            else:
                type_from_string = getattr(events_module, "type_from_string", None)
                if not callable(type_from_string):
                    return _not_kit("neither omni.kit.app.queue_event nor carb.events.type_from_string is available")
                stream = app_module.get_app().get_message_bus_event_stream()
                stream.push(type_from_string(bus_name), payload=bus_payload)
                api = "IApp.get_message_bus_event_stream().push (deprecated Events 1.0)"
    except Exception as exc:  # noqa: BLE001
        return _error(f"Sending failed: {type(exc).__name__}: {exc}", dry_run=False, sent=False)
    result = {
        "ok": True,
        "dry_run": False,
        "sent": True,
        "target": target,
        "event_type": plan["event_type"],
        "bus_event": bus_name,
        "bus_payload": bus_payload,
        "payload": clean,
        "api": api,
        "delivered": "on the next app update (graph nodes react then)",
        "message": f"Sent {plan['event_type']!r} with {clean!r}.",
    }
    if warnings:
        result["warnings"] = warnings
    return result


# ------------------------------------------------------------------------------------------------ listening


def _plain(value: Any, depth: int = 0) -> Any:
    """A JSON-friendly copy of an event payload value (carb dictionaries, tuples, numpy arrays...)."""
    if depth > 8:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return _plain(tolist(), depth + 1)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, (list, tuple, set)):
        return [_plain(v, depth + 1) for v in value]
    return str(value)


def _event_payload(event: Any) -> dict[str, Any]:
    raw = getattr(event, "payload", None)
    if raw is None:
        return {}
    for convert in (dict, lambda p: p.get_dict()):
        try:
            return _plain(convert(raw))
        except Exception:  # noqa: BLE001
            continue
    return {"value": str(raw)}


def _subscribe(event_name: str, on_event: Any, observer_name: str) -> tuple[Any, str | None]:
    """(observer guard, problem). Registers the Events 1.0 alias so old-style pushes are seen too."""
    dispatcher_module = _kit_module("carb.eventdispatcher")
    getter = getattr(dispatcher_module, "get_eventdispatcher", None)
    if not callable(getter):
        return None, "carb.eventdispatcher is not importable"
    _register_alias(_kit_module("omni.kit.app"), _kit_module("carb.events"), event_name)
    try:
        guard = getter().observe_event(event_name=event_name, on_event=on_event, observer_name=observer_name)
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not subscribe: {type(exc).__name__}: {exc}"
    return guard, None


def _unsubscribe(guard: Any) -> bool:
    reset = getattr(guard, "reset", None)
    if not callable(reset):
        return False
    try:
        reset()
    except Exception:  # noqa: BLE001
        return False
    return True


def _check_timeout(timeout_s: Any) -> str | None:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s):
        return f"timeout_s must be a number of seconds from 0 to {MAX_TIMEOUT_S:g}"
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        return f"timeout_s must be more than 0 and at most {MAX_TIMEOUT_S:g} seconds"
    return None


def _next_update_waiter() -> Any:
    app_module = _kit_module("omni.kit.app")
    get_app = getattr(app_module, "get_app", None)
    if callable(get_app):
        try:
            waiter = getattr(get_app(), "next_update_async", None)
        except Exception:  # noqa: BLE001
            waiter = None
        if callable(waiter):
            return waiter
    return lambda: asyncio.sleep(0.01)


async def listen_once(event_name: str, *, timeout_s: float = 5, target: str = "kit", wait: Any = None) -> dict[str, Any]:
    """Await ONE event named event_name (Kit only) and return its payload; always unsubscribes.

    Use inside Kit's event loop, e.g. in the Script Editor:
        import asyncio; asyncio.ensure_future(listen_once('setColor'))
    It yields one app update at a time (never blocks Kit). target='custom' listens for 'omni.graph.action.<name>'.
    wait: optional zero-argument coroutine factory used between checks (defaults to Kit's next_update_async).
    """
    problem = _check_event_name(event_name) or _check_timeout(timeout_s)
    if problem:
        return _error(problem)
    if target not in ("kit", "custom"):
        return _error("target must be 'kit' or 'custom' for listening")
    wire = _wire_name(event_name, target)
    received: list[Any] = []

    def on_event(event: Any) -> None:
        if not received:
            received.append(event)

    guard, problem = _subscribe(wire, on_event, f"omniverse_scene_mcp.listen_once.{wire}")
    if guard is None:
        return _not_kit(problem)
    waiter = wait or _next_update_waiter()
    started = time.monotonic()
    try:
        while not received and time.monotonic() - started < timeout_s:
            await waiter()
    finally:
        unsubscribed = _unsubscribe(guard)
    waited = round(time.monotonic() - started, 3)
    if not received:
        return {
            "ok": True, "received": False, "timed_out": True, "event_name": wire, "payload": None,
            "waited_s": waited, "unsubscribed": unsubscribed,
            "message": f"No {wire!r} event arrived within {timeout_s:g} s (names are case-sensitive).",
        }
    event = received[0]
    return {
        "ok": True, "received": True, "timed_out": False,
        "event_name": str(getattr(event, "event_name", wire) or wire),
        "payload": _event_payload(event), "waited_s": waited, "unsubscribed": unsubscribed,
    }


# Non-blocking variant for synchronous callers (an RPC handler on Kit's main thread cannot await).
_LISTENERS: dict[str, dict[str, Any]] = {}
_IDS = itertools.count(1)


def _close(record: dict[str, Any]) -> None:
    if record.get("guard") is not None:
        record["unsubscribed"] = _unsubscribe(record["guard"])
        record["guard"] = None


def _sweep() -> None:
    now = time.monotonic()
    for key, record in list(_LISTENERS.items()):
        if record["events"] or now >= record["deadline"]:
            _close(record)
        if record["guard"] is None and now - record["deadline"] > MAX_TIMEOUT_S:
            _LISTENERS.pop(key, None)
    closed = [key for key, record in _LISTENERS.items() if record["guard"] is None]
    for key in closed[: max(0, len(_LISTENERS) - 4 * MAX_LISTENERS)]:  # keep finished results bounded
        _LISTENERS.pop(key, None)


def start_listener(event_name: str, *, timeout_s: float = 30, target: str = "kit") -> dict[str, Any]:
    """Subscribe to the next event_name (Kit only) without waiting; read it later with listener_status."""
    problem = _check_event_name(event_name) or _check_timeout(timeout_s)
    if problem:
        return _error(problem)
    if target not in ("kit", "custom"):
        return _error("target must be 'kit' or 'custom' for listening")
    _sweep()
    if sum(1 for r in _LISTENERS.values() if r["guard"] is not None) >= MAX_LISTENERS:
        return _error(f"{MAX_LISTENERS} listeners are already active; stop one first")
    wire = _wire_name(event_name, target)
    listener_id = f"listener-{os.getpid()}-{next(_IDS)}"
    record: dict[str, Any] = {"event_name": wire, "events": [], "guard": None, "unsubscribed": False,
                              "started": time.monotonic(), "deadline": time.monotonic() + float(timeout_s)}

    def on_event(event: Any) -> None:
        if not record["events"]:
            record["events"].append({"event_name": str(getattr(event, "event_name", wire) or wire),
                                     "payload": _event_payload(event)})

    guard, problem = _subscribe(wire, on_event, f"omniverse_scene_mcp.{listener_id}")
    if guard is None:
        return _not_kit(problem)
    record["guard"] = guard
    _LISTENERS[listener_id] = record
    return {"ok": True, "listener_id": listener_id, "event_name": wire, "timeout_s": float(timeout_s),
            "message": f"Listening for {wire!r}; call event_status({listener_id!r})."}


def listener_status(listener_id: str) -> dict[str, Any]:
    """{received, payload, timed_out, active}; unsubscribes once the event arrived or the time ran out."""
    record = _LISTENERS.get(listener_id)
    if record is None:
        return _error(f"Unknown listener {listener_id!r}")
    now = time.monotonic()
    received = bool(record["events"])
    timed_out = not received and now >= record["deadline"]
    if received or timed_out:
        _close(record)
    first = record["events"][0] if received else None
    return {
        "ok": True, "listener_id": listener_id, "event_name": record["event_name"], "received": received,
        "payload": first["payload"] if first else None, "timed_out": timed_out,
        "active": record["guard"] is not None, "unsubscribed": record["unsubscribed"] or record["guard"] is None,
        "waited_s": round(now - record["started"], 3),
    }


def stop_listener(listener_id: str) -> dict[str, Any]:
    """Unsubscribe and forget a listener (safe to call twice)."""
    record = _LISTENERS.pop(listener_id, None)
    if record is None:
        return {"ok": True, "listener_id": listener_id, "stopped": False, "message": "No such listener."}
    _close(record)
    return {"ok": True, "listener_id": listener_id, "stopped": True, "received": bool(record["events"])}


# ------------------------------------------------------------------------------------- graph listener check


def _token(attr: Any) -> str | None:
    if not attr:
        return None
    try:
        value = attr.Get()
    except Exception:  # noqa: BLE001
        return None
    return None if value is None else str(value)


def _bool(attr: Any, default: bool | None) -> bool | None:
    if not attr:
        return default
    try:
        value = attr.Get()
    except Exception:  # noqa: BLE001
        return default
    return default if value is None else bool(value)


def _listener_kind(node_type: str) -> str | None:
    if node_type == MESSAGE_BUS_LISTENER or (node_type.endswith("OnMessageBusEvent")):
        return "message_bus"
    if node_type == CUSTOM_LISTENER or node_type.endswith("OnCustomEvent"):
        return "custom"
    return None


def _sender_kind(node_type: str) -> str | None:
    if node_type == MESSAGE_BUS_SENDER or node_type.endswith("SendMessageBusEvent"):
        return "message_bus"
    if node_type == CUSTOM_SENDER or node_type.endswith("SendCustomEvent"):
        return "custom"
    return None


def _normal(name: str) -> str:
    return _strip_prefix(name.strip()).strip().casefold()


def _name_match(wanted: str, authored: str, kind: str) -> tuple[str | None, list[str]]:
    """('exact' | 'near_miss' | None, reasons) for a wanted event name against a node's authored eventName."""
    if kind == "custom":
        exact = authored == wanted or CUSTOM_EVENT_PREFIX + authored == wanted or authored == _strip_prefix(wanted)
    else:
        exact = authored == wanted
    if exact:
        return "exact", []
    reasons = []
    if authored != authored.strip():
        reasons.append("the graph's eventName has leading/trailing spaces")
    a_plain, w_plain = _strip_prefix(authored.strip()), _strip_prefix(wanted)
    if kind == "message_bus" and (authored.strip().startswith(CUSTOM_EVENT_PREFIX) != wanted.startswith(CUSTOM_EVENT_PREFIX)):
        reasons.append(f"prefix: one name has '{CUSTOM_EVENT_PREFIX}' and the other does not")
    if a_plain != w_plain and a_plain.casefold() == w_plain.casefold():
        reasons.append(f"capitalization: graph has {authored.strip()!r}, you have {wanted!r}")
    if reasons and _normal(authored) == _normal(wanted):
        return "near_miss", reasons
    ratio = difflib.SequenceMatcher(None, _normal(authored), _normal(wanted)).ratio()
    if ratio >= 0.8:
        return "near_miss", [f"similar spelling ({ratio:.0%} alike): graph has {authored!r}, you have {wanted!r}"]
    return None, []


def _key_match(key: str, outputs: list[str]) -> str | None:
    """The output a payload key nearly matches (different capitalization), or None."""
    for name in outputs:
        if name != key and name.casefold() == key.casefold():
            return name
    return None


def _open_stage(stage: Any) -> tuple[Any, str | None]:
    if hasattr(stage, "Traverse"):
        return stage, None
    if isinstance(stage, (str, os.PathLike)):
        try:
            from pxr import Usd
        except ImportError:
            return None, "pxr (OpenUSD) is not importable here"
        path = os.fspath(stage)
        if not os.path.isfile(path):
            return None, f"No such USD file: {path}"
        try:
            opened = Usd.Stage.Open(path, Usd.Stage.LoadNone)  # read-only use: nothing is ever saved
        except Exception as exc:  # noqa: BLE001
            return None, f"Could not open {path}: {type(exc).__name__}: {exc}"
        return (opened, None) if opened else (None, f"Could not open {path}")
    return None, "stage must be a Usd.Stage or a path to a USD file"


def _variant_info(stage: Any, node: Any) -> dict[str, Any]:
    info: dict[str, Any] = {"variant_set": _token(node.GetAttribute("inputs:variantSetName"))}
    rel = node.GetRelationship("inputs:prim")
    targets = [str(t) for t in rel.GetTargets()] if rel else []
    info["prims"] = targets
    variants: dict[str, list[str]] = {}
    for target in targets:
        prim = stage.GetPrimAtPath(target)
        if prim and info["variant_set"]:
            vsets = prim.GetVariantSets()
            if vsets.HasVariantSet(info["variant_set"]):
                variants[target] = list(vsets.GetVariantSet(info["variant_set"]).GetVariantNames())
    info["variants"] = variants
    return info


def _value_check(value: Any, variants: dict[str, list[str]]) -> dict[str, Any] | None:
    if not isinstance(value, str) or not variants:
        return None
    names = sorted({name for group in variants.values() for name in group})
    if value in names:
        return {"value": value, "match": "exact"}
    near = [name for name in names if name.casefold() == value.casefold()]
    return {"value": value, "match": "near_miss" if near else "missing", "closest": near or names,
            "message": f"No variant named {value!r}" + (f"; did you mean {near[0]!r}? (case-sensitive)" if near else "")}


def check_graph_listener(stage: Any, event_name: str, *, payload: Any = None) -> dict[str, Any]:
    """Which OmniGraph nodes in the stage listen for event_name: exact matches and near misses (pure pxr).

    stage: a Usd.Stage, or a USD file path (opened read-only, never saved). Finds On MessageBus Event and On Custom
    Event nodes by node:type and compares their inputs:eventName. For each exact listener it lists the payload keys
    it accepts (its dynamic outputs:<key>), what those outputs feed (e.g. Set Variant Selection and the variant
    names on its target prim), and, when payload is given, flags keys/values that would be silently ignored.
    """
    problem = _check_event_name(event_name)
    if problem:
        return _error(problem)
    clean: dict[str, Any] = {}
    if payload is not None:
        clean, errors, _warnings, _size = _validate_payload(payload, "kit")
        if errors:
            return _error("The payload is not valid.", errors=errors)
    usd_stage, problem = _open_stage(stage)
    if usd_stage is None:
        return _error(problem or "No stage")

    nodes = []
    for prim in usd_stage.Traverse():
        attr = prim.GetAttribute("node:type")
        if attr:
            nodes.append((prim, _token(attr) or ""))
    consumers: dict[str, list[dict[str, Any]]] = {}
    for prim, node_type in nodes:
        for attr in prim.GetAttributes():
            try:
                sources = attr.GetConnections()
            except Exception:  # noqa: BLE001
                continue
            for source in sources:
                consumers.setdefault(str(source), []).append(
                    {"node": str(prim.GetPath()), "node_type": node_type, "input": attr.GetName(), "prim": prim})

    exact, near, others, senders = [], [], [], []
    for prim, node_type in nodes:
        sender = _sender_kind(node_type)
        if sender:
            name = _token(prim.GetAttribute("inputs:eventName")) or ""
            match, _ = _name_match(event_name, name, sender)
            if match:
                senders.append({"path": str(prim.GetPath()), "node_type": node_type, "event_name": name,
                                "match": match})
            continue
        kind = _listener_kind(node_type)
        if not kind:
            continue
        authored = _token(prim.GetAttribute("inputs:eventName")) or ""
        outputs = sorted(
            a.GetName()[len("outputs:"):] for a in prim.GetAttributes()
            if a.GetName().startswith("outputs:") and a.GetName() not in _STATIC_OUTPUTS
        )
        entry: dict[str, Any] = {
            "path": str(prim.GetPath()),
            "node_type": node_type,
            "kind": kind,
            "event_name": authored,
            "listens_to": CUSTOM_EVENT_PREFIX + authored if kind == "custom" else authored,
            "only_playback": _bool(prim.GetAttribute("inputs:onlyPlayback"), True),
            "payload_outputs": outputs if kind == "message_bus" else [],
        }
        match, reasons = _name_match(event_name, authored, kind)
        if match == "exact":
            entry["feeds"] = []
            for key in outputs:
                for use in consumers.get(f"{prim.GetPath()}.outputs:{key}", []):
                    feed = {"output": f"outputs:{key}", "node": use["node"], "node_type": use["node_type"],
                            "input": use["input"]}
                    if use["node_type"] == SET_VARIANT and use["input"] == "inputs:variantName":
                        feed.update(_variant_info(usd_stage, use["prim"]))
                        check = _value_check(clean.get(key), feed["variants"]) if key in clean else None
                        if check:
                            feed["value_check"] = check
                    entry["feeds"].append(feed)
            if payload is not None and kind == "message_bus":
                entry["payload_check"] = {
                    "used": [k for k in clean if k in outputs],
                    "ignored": [k for k in clean if k not in outputs],
                    "near_miss_keys": {k: hit for k in clean if (hit := _key_match(k, outputs))},
                    "outputs_not_in_payload": [k for k in outputs if k not in clean],
                }
            exact.append(entry)
        elif match == "near_miss":
            entry["reasons"] = reasons
            near.append(entry)
        else:
            others.append({"path": entry["path"], "kind": kind, "event_name": authored})

    warnings: list[str] = []
    for entry in exact:
        if entry["only_playback"]:
            warnings.append(f"{entry['path']} has 'Only Simulate On Play' on: it fires only while the timeline plays")
        check = entry.get("payload_check")
        if check:
            for key, hit in check["near_miss_keys"].items():
                warnings.append(f"payload key {key!r} does not match output {hit!r} (case-sensitive): it is ignored")
            for key in check["ignored"]:
                if key not in check["near_miss_keys"]:
                    warnings.append(f"payload key {key!r} has no outputs:{key} on {entry['path']}: it is ignored")
        for feed in entry.get("feeds", []):
            check = feed.get("value_check")
            if check and check["match"] != "exact":
                warnings.append(f"{feed['node']}: {check['message']}")
        if entry["kind"] == "custom":
            warnings.append(f"{entry['path']} is an On Custom Event node: send {entry['listens_to']!r} "
                            "(target='custom'); it does not receive payload keys")

    if exact:
        verdict = f"{len(exact)} node(s) listen for {event_name!r} exactly: " + ", ".join(e["path"] for e in exact)
    elif near:
        verdict = (f"No node listens for {event_name!r} exactly. Near misses: "
                   + "; ".join(f"{e['path']} has {e['event_name']!r}" for e in near))
    else:
        verdict = f"No node in this stage listens for {event_name!r}."
    result: dict[str, Any] = {
        "ok": True,
        "event_name": event_name,
        "listening": bool(exact),
        "verdict": verdict,
        "exact": exact,
        "near_misses": near,
        "other_listeners": others,
        "senders": senders,
        "listener_count": len(exact) + len(near) + len(others),
        "read_only": True,
    }
    if payload is not None:
        result["payload"] = clean
    if warnings:
        result["warnings"] = warnings
    return result
