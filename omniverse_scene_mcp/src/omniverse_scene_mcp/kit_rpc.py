"""Extend the live scene JSON-RPC server with capabilities, insight, authoring, safe edits and MCP undo.

make_extended_server_class(base_cls) subclasses the LiveSceneControlServer that Isaac Sim (or Composer) loads from
live_scene_control.py, without editing that file. New methods come from a fixed handler table; every other method
goes to the base class's _dispatch unchanged. Runs inside Kit, but imports neither omni nor pxr itself (usd_insight,
which holds the insight time budget, is imported lazily). Every mutating method is a dry run unless the caller sends
dry_run=false; for the tier3-1 edit and undo methods and the tier4-1 add_reference and physics methods a missing or
malformed dry_run is also a dry run. The tier4-1 viewport capture and simulation methods have no dry_run: capture only
reads the viewport (it writes a new PNG), and simulation play/pause/stop/step change simulation state, not authored
scene data.

The extended server's HTTP handler is wrapped by guard_handler_class (localhost CSRF / DNS-rebinding guard): browser
requests (an Origin or Sec-Fetch-* header), POSTs whose Content-Type is not application/json, requests whose Host is
not 127.0.0.1:<port> or localhost:<port> and, when OMNIVERSE_BRIDGE_TOKEN is set in Kit, POSTs without the matching
X-Omniverse-Bridge-Token header get a 403 JSON answer and are never dispatched.
"""

from __future__ import annotations

import hmac
import json
import math
import os
import time
from collections.abc import Callable
from typing import Any

from .bridge import (
    ASSET_RPC_METHODS,
    BASIC_RPC_METHODS,
    CAPTURE_RPC_METHODS,
    EDIT_RPC_METHODS,
    INSIGHT_QUERIES,
    LIVE_RPC_METHODS,
    PHYSICS_RPC_METHODS,
    RPC_VERSION,
    SIMULATION_RPC_METHODS,
    UNDO_RPC_METHODS,
    unsupported_result,
)

ALL_RPC_METHODS: tuple[str, ...] = BASIC_RPC_METHODS + LIVE_RPC_METHODS
_MAX_PATH_CHARS = 4096
_MAX_WORD_CHARS = 64
_MAX_NAME_CHARS = 256
_MAX_TEXT_VALUE_CHARS = 100_000
_MAX_VALUE_ITEMS = 200_000
_MAX_HISTORY = 50
# live_scene_control._submit stops waiting after 15 s but leaves the request queued, so Kit would still run it on a
# later update while the caller was told it failed. Writes older than this are dropped instead (must stay below 15 s).
_WRITE_METHODS = (
    "create_light",
    "create_camera",
    "aim",
    *EDIT_RPC_METHODS,
    "undo_last",
    "add_reference",
    "add_physics_scene",
    "make_rigid_body",
    "make_collider",
    "remove_physics",
    "capture_viewport",
    "simulation_play",
    "simulation_pause",
    "simulation_stop",
    "simulation_step",
)
_MAX_CAPTURE_SIDE = 16384
_STALE_AFTER_S = 10.0
# Live insight runs on Kit's main thread: its traversals stop at a cooperative time budget (usd_insight.TimeBudget)
# and return partial results. The budget is also cut to what is left of the caller's 15 s wait after queueing,
# minus a margin for the layer checks and JSON trimming that follow the query.
INSIGHT_DEFAULT_BUDGET_MS = 3000.0
INSIGHT_MAX_BUDGET_MS = 10000.0
_CALLER_WAIT_S = 15.0
_RESULT_MARGIN_S = 2.0
_MIN_BUDGET_MS = 1.0

Handler = Callable[[Any, dict[str, Any]], dict[str, Any]]

# Localhost CSRF / DNS-rebinding guard for the bridge's HTTP handler (see guard_handler_class).
BRIDGE_TOKEN_ENV = "OMNIVERSE_BRIDGE_TOKEN"
BRIDGE_TOKEN_HEADER = "X-Omniverse-Bridge-Token"
_GUARD_DRAIN_LIMIT = 1 << 20  # a refused POST body up to 1 MiB is read (never parsed) so the 403 is not lost to a reset


def make_extended_server_class(base_cls: type) -> type:
    """Return a subclass of base_cls (a LiveSceneControlServer) that also answers LIVE_RPC_METHODS."""
    if not isinstance(base_cls, type) or not callable(getattr(base_cls, "_dispatch", None)):
        raise TypeError("make_extended_server_class needs a server class that defines _dispatch(payload)")

    class ExtendedLiveSceneControlServer(base_cls):  # type: ignore[misc, valid-type]
        """LiveSceneControlServer plus capabilities, insight, create_light, create_camera and aim."""

        rpc_version = RPC_VERSION
        rpc_methods = ALL_RPC_METHODS

        def _build_handler(self):  # called by the base _build_server; the base request handling runs unchanged
            return guard_handler_class(super()._build_handler())

        def _submit(self, payload: Any) -> dict[str, Any]:
            # Stamp the queue time so _dispatch (on Kit's main thread) can drop writes the caller stopped waiting for.
            if isinstance(payload, dict):
                payload = {**payload, "_queued_at": time.monotonic()}
            return super()._submit(payload)

        def _dispatch(self, payload: Any) -> dict[str, Any]:
            if not isinstance(payload, dict):
                raise ValueError("The RPC payload must be a JSON object with 'method' and 'params'")
            method = str(payload.get("method", "")).strip().lower()
            handler = _HANDLERS.get(method)
            if handler is None:
                return super()._dispatch(payload)
            queued = payload.get("_queued_at")
            if method in _WRITE_METHODS and isinstance(queued, float) and time.monotonic() - queued > _STALE_AFTER_S:
                return {
                    "ok": True,
                    "result": {
                        "ok": False,
                        "written": False,
                        "error": "Request expired while Kit was busy (queued more than 10 s); nothing was written. Retry.",
                    },
                }
            params = payload.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise ValueError(f"params for '{method}' must be a JSON object")
            if method == "insight":  # the time budget also accounts for how long the request waited in the queue
                return {"ok": True, "result": _insight(self._bridge, params, queued_at=queued)}
            return {"ok": True, "result": handler(self._bridge, params)}

    ExtendedLiveSceneControlServer.__name__ = f"Extended{base_cls.__name__}"
    ExtendedLiveSceneControlServer.__qualname__ = ExtendedLiveSceneControlServer.__name__
    return ExtendedLiveSceneControlServer


def guard_handler_class(handler_cls: type) -> type:
    """Subclass a BaseHTTPRequestHandler so refused requests get a 403 JSON answer before the base do_POST/do_GET."""
    if getattr(handler_cls, "_omniverse_bridge_guarded", False):
        return handler_cls

    class GuardedHandler(handler_cls):  # type: ignore[misc, valid-type]
        _omniverse_bridge_guarded = True

        def do_POST(self) -> None:  # noqa: N802
            if not self._omniverse_bridge_refused():
                super().do_POST()

        def do_GET(self) -> None:  # noqa: N802
            if not self._omniverse_bridge_refused():
                super().do_GET()

        def _omniverse_bridge_refused(self) -> bool:
            try:
                port = int(self.server.server_address[1])
            except Exception:  # pragma: no cover - defensive: without a port no Host can match, so all is refused
                port = None
            token = os.environ.get(BRIDGE_TOKEN_ENV, "").strip() or None
            refusal = bridge_request_refusal(self.command, self.headers, port, token)
            if refusal is None:
                return False
            reason, message = refusal
            if self.command == "POST":
                _drain_body(self)
            body = json.dumps(
                {"ok": False, "forbidden": True, "error": "forbidden", "reason": reason, "message": message}
            ).encode("utf-8")
            self.close_connection = True
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return True

    GuardedHandler.__name__ = GuardedHandler.__qualname__ = f"Guarded{handler_cls.__name__}"
    return GuardedHandler


def bridge_request_refusal(command: str, headers: Any, port: int | None, token: str | None) -> tuple[str, str] | None:
    """(reason, message) when a bridge HTTP request must be refused, else None. headers: an email.message.Message.

    * origin / browser: an Origin or any Sec-Fetch-* header means a browser page sent it (the router's urllib client
      sends neither), so a web page cannot drive the open scene (localhost CSRF);
    * host: exactly one Host header, equal to 127.0.0.1:<port> or localhost:<port> (blocks DNS rebinding);
    * content_type: a POST must say Content-Type: application/json (a no-cors fetch cannot send that);
    * token: when OMNIVERSE_BRIDGE_TOKEN is set in Kit, a POST must carry the same X-Omniverse-Bridge-Token value
      (constant-time compare). GET /health stays token-free so the router's /health probes keep working.
    """
    if _header_values(headers, "Origin"):
        return "origin", "Refused: requests with an Origin header (sent by web browsers) may not use the scene bridge."
    browser = sorted({key.lower() for key in _header_keys(headers) if key.lower().startswith("sec-fetch-")})
    if browser:
        return "browser", f"Refused: browser request headers ({', '.join(browser)}) may not use the scene bridge."
    hosts = [value.strip().lower() for value in _header_values(headers, "Host")]
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}"} if port is not None else set()
    if len(hosts) != 1 or hosts[0] not in allowed:
        return "host", f"Refused: the Host header must be 127.0.0.1:{port} or localhost:{port}."
    if str(command).upper() == "POST":
        types = [value.split(";", 1)[0].strip().lower() for value in _header_values(headers, "Content-Type")]
        if types != ["application/json"]:
            return "content_type", "Refused: POST /rpc needs Content-Type: application/json."
        if token:
            given = _header_values(headers, BRIDGE_TOKEN_HEADER)
            expected = token.encode("utf-8")
            if len(given) != 1 or not hmac.compare_digest(given[0].strip().encode("utf-8", "replace"), expected):
                return "token", (
                    f"Refused: this bridge requires a matching {BRIDGE_TOKEN_HEADER} header; set {BRIDGE_TOKEN_ENV} "
                    "to the same value in the router's environment as in Kit's."
                )
    return None


def _header_values(headers: Any, name: str) -> list[str]:
    getter = getattr(headers, "get_all", None)
    if callable(getter):
        return [str(value) for value in (getter(name) or [])]
    return [str(value) for key, value in dict(headers or {}).items() if str(key).lower() == name.lower()]


def _header_keys(headers: Any) -> list[str]:
    keys = getattr(headers, "keys", None)
    return [str(key) for key in keys()] if callable(keys) else []


def _drain_body(handler: Any) -> None:
    """Read and discard a refused POST body (never parsed) so closing the socket does not reset the 403 away."""
    try:
        length = int(handler.headers.get("Content-Length", "0") or 0)
        if 0 < length <= _GUARD_DRAIN_LIMIT:
            handler.rfile.read(length)
    except Exception:  # pragma: no cover - defensive: the 403 is sent either way
        pass


def _capabilities(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("capabilities", params, ())
    getter = getattr(bridge, "capabilities", None)
    info = dict(getter()) if callable(getter) else {"bridge": type(bridge).__name__}
    info.update(
        {
            "ok": True,
            "version": RPC_VERSION,
            "methods": list(ALL_RPC_METHODS),
            "insight_queries": list(INSIGHT_QUERIES),
            "insight_budget_ms": {"default": INSIGHT_DEFAULT_BUDGET_MS, "max": INSIGHT_MAX_BUDGET_MS},
            "edit_methods": list(EDIT_RPC_METHODS),
            "undo_methods": list(UNDO_RPC_METHODS),
            "asset_methods": list(ASSET_RPC_METHODS),
            "physics_methods": list(PHYSICS_RPC_METHODS),
            "capture_methods": list(CAPTURE_RPC_METHODS),
            "simulation_methods": list(SIMULATION_RPC_METHODS),
            "dry_run_default": True,
        }
    )
    return info


def _insight(bridge: Any, params: dict[str, Any], queued_at: Any = None) -> dict[str, Any]:
    """Run one read-only insight query under a time budget (budget_ms in params or args; default 3000, max 10000)."""
    _check_params("insight", params, ("query", "args", "budget_ms"))
    query = params.get("query")
    name = query.strip().lower() if isinstance(query, str) else ""
    if name not in INSIGHT_QUERIES:
        raise ValueError(f"Unknown insight query {_clip(repr(query))}; use one of {list(INSIGHT_QUERIES)}")
    args = params.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError(f"insight args must be a JSON object, not {type(args).__name__}")
    args = dict(args)
    inner = args.pop("budget_ms", None)  # the bridge's own argument allowlist does not know budget_ms
    outer = params.get("budget_ms")
    if inner is not None and outer is not None:
        raise ValueError("'budget_ms' was given twice (in params and in args)")
    requested = _budget_ms(outer if outer is not None else inner)
    runner = getattr(bridge, "insight", None)
    if not callable(runner):
        return _bridge_lacks("insight", bridge)
    waited_s = max(0.0, time.monotonic() - queued_at) if isinstance(queued_at, float) else 0.0
    left_ms = (_CALLER_WAIT_S - _RESULT_MARGIN_S - waited_s) * 1000.0
    budget_ms = max(_MIN_BUDGET_MS, min(requested, INSIGHT_MAX_BUDGET_MS, left_ms))
    budget = _time_budget(budget_ms)
    started = time.perf_counter()
    if budget is None:
        result = runner(name, args)
    else:
        with budget:  # the ambient budget usd_insight/usd_validate pick up inside the bridge's query
            result = runner(name, args)
    timing = {
        "budget_ms": round(budget_ms, 1),
        "requested_budget_ms": round(requested, 1),
        "elapsed_ms": round((time.perf_counter() - started) * 1000.0, 1),
        "limited_by_queue_wait": left_ms < min(requested, INSIGHT_MAX_BUDGET_MS),
        "queue_wait_ms": round(waited_s * 1000.0, 1),
        "enforced": budget is not None,
    }
    return _with_time_budget(result, timing, budget is not None and budget.truncated)


def _with_time_budget(result: Any, timing: dict[str, Any], truncated: bool) -> Any:
    """Merge kit_rpc's timing into the query's own time_budget record and warn when the results are partial."""
    if not isinstance(result, dict):
        return result
    record = result.get("time_budget")
    truncated = truncated or bool(result.get("truncated_by_time"))
    if not isinstance(record, dict) and "truncated_by_time" not in result and not truncated:
        # Every usd_insight/usd_validate query reports under the ambient budget; a bridge that reports no timing
        # (an older or test bridge) gets its result back unchanged.
        return result
    info: dict[str, Any] = dict(record) if isinstance(record, dict) else {}
    info.update(timing, truncated_by_time=truncated)
    out = dict(result)
    out.update(time_budget=info, truncated_by_time=truncated, budget_ms=timing["budget_ms"])
    out.setdefault("elapsed_ms", int(timing["elapsed_ms"]))
    if truncated:
        existing = out.get("warnings")
        warnings = list(existing) if isinstance(existing, list) else []
        warnings.append(
            f"Stopped at the {timing['budget_ms']:g} ms time budget so Kit stays responsive; the results are partial "
            f"(see time_budget). Narrow the query, or pass budget_ms up to {INSIGHT_MAX_BUDGET_MS:g}."
        )
        out["warnings"] = warnings
    return out


def _budget_ms(value: Any) -> float:
    if value is None:
        return INSIGHT_DEFAULT_BUDGET_MS
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
        raise ValueError(
            f"'budget_ms' must be a positive number of milliseconds (values above {INSIGHT_MAX_BUDGET_MS:g} are capped)"
        )
    return float(value)


def _time_budget(budget_ms: float) -> Any:
    """A usd_insight.TimeBudget, or None when it cannot be built (for example without pxr)."""
    try:
        from .usd_insight import TimeBudget

        return TimeBudget(budget_ms)
    except ImportError:
        return None


_LIGHT_PARAMS = ("path", "light_type", "intensity", "exposure", "color", "size", "translation", "aim_at", "up_axis", "dry_run")
_CAMERA_PARAMS = (
    "path",
    "focal_length",
    "horizontal_aperture",
    "clipping_range",
    "translation",
    "aim_at",
    "copy_optics_from",
    "up_axis",
    "dry_run",
)
_AIM_PARAMS = ("path", "target", "up_axis", "dry_run")


def _create_light(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("create_light", params, _LIGHT_PARAMS)
    author = getattr(bridge, "create_light", None)
    if not callable(author):
        return _bridge_lacks("create_light", bridge)
    return author(
        _path(params, "path"),
        _word(params, "light_type", required=True),
        intensity=_number(params, "intensity"),
        exposure=_number(params, "exposure"),
        color=_vector(params, "color", 3),
        size=_size(params, "size"),
        translation=_vector(params, "translation", 3),
        aim_at=_target(params, "aim_at"),
        up_axis=_word(params, "up_axis"),
        dry_run=_flag(params, "dry_run", default=True),
    )


def _create_camera(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("create_camera", params, _CAMERA_PARAMS)
    author = getattr(bridge, "create_camera", None)
    if not callable(author):
        return _bridge_lacks("create_camera", bridge)
    return author(
        _path(params, "path"),
        focal_length=_number(params, "focal_length"),
        horizontal_aperture=_number(params, "horizontal_aperture"),
        clipping_range=_vector(params, "clipping_range", 2),
        translation=_vector(params, "translation", 3),
        aim_at=_target(params, "aim_at"),
        copy_optics_from=_path(params, "copy_optics_from", required=False),
        up_axis=_word(params, "up_axis"),
        dry_run=_flag(params, "dry_run", default=True),
    )


def _aim(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("aim", params, _AIM_PARAMS)
    author = getattr(bridge, "aim", None)
    if not callable(author):
        return _bridge_lacks("aim", bridge)
    target = _target(params, "target")
    if target is None:
        raise ValueError("aim needs a target: a prim path or an [x, y, z] point")
    return author(
        _path(params, "path"),
        target,
        up_axis=_word(params, "up_axis"),
        dry_run=_flag(params, "dry_run", default=True),
    )


_SET_ATTRIBUTE_PARAMS = ("path", "name", "value", "time", "create_type", "dry_run")
_MATERIAL_PARAMS = (
    "path",
    "diffuse_color",
    "roughness",
    "metallic",
    "opacity",
    "emissive_color",
    "ior",
    "create_parent",
    "dry_run",
)
_BIND_PARAMS = ("prim_path", "material_path", "strength", "purpose", "dry_run")


def _set_attribute(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("set_attribute", params, _SET_ATTRIBUTE_PARAMS)
    author = getattr(bridge, "set_attribute", None)
    if not callable(author):
        return _bridge_lacks("set_attribute", bridge)
    dry_run, warning = _dry_run_flag(params)
    result = author(
        _path(params, "path"),
        _name(params, "name"),
        _value(params, "value"),
        time=_time_code(params, "time"),
        create_type=_word(params, "create_type"),
        dry_run=dry_run,
    )
    return _with_warning(result, warning)


def _create_material(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("create_material", params, _MATERIAL_PARAMS)
    author = getattr(bridge, "create_material", None)
    if not callable(author):
        return _bridge_lacks("create_material", bridge)
    dry_run, warning = _dry_run_flag(params)
    result = author(
        _path(params, "path"),
        diffuse_color=_color(params, "diffuse_color"),
        roughness=_number(params, "roughness"),
        metallic=_number(params, "metallic"),
        opacity=_number(params, "opacity"),
        emissive_color=_color(params, "emissive_color"),
        ior=_number(params, "ior"),
        create_parent=_flag(params, "create_parent", default=False),
        dry_run=dry_run,
    )
    return _with_warning(result, warning)


def _bind_material(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("bind_material", params, _BIND_PARAMS)
    author = getattr(bridge, "bind_material", None)
    if not callable(author):
        return _bridge_lacks("bind_material", bridge)
    dry_run, warning = _dry_run_flag(params)
    result = author(
        _path(params, "prim_path"),
        _path(params, "material_path"),
        strength=_word(params, "strength"),
        purpose=_word(params, "purpose"),
        dry_run=dry_run,
    )
    return _with_warning(result, warning)


def _undo_last(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("undo_last", params, ("dry_run",))
    undo = getattr(bridge, "undo_last", None)
    if not callable(undo):
        return _bridge_lacks("undo_last", bridge)
    dry_run, warning = _dry_run_flag(params)
    return _with_warning(undo(dry_run=dry_run), warning)


def _undo_history(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("undo_history", params, ("limit",))
    history = getattr(bridge, "undo_history", None)
    if not callable(history):
        return _bridge_lacks("undo_history", bridge)
    limit = params.get("limit")
    if limit is None:
        limit = 20
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError(f"'limit' must be a whole number from 1 to {_MAX_HISTORY}")
    return history(max(1, min(int(limit), _MAX_HISTORY)))


# --- RPC tier4-1: add_reference, physics, viewport capture and simulation ---------------------------------------

_ADD_REFERENCE_PARAMS = (
    "prim_path",
    "asset_path",
    "translation",
    "rotation",
    "scale",
    "instanceable",
    "asset_prim_path",
    "correct_units",
    "dry_run",
)
_RIGID_BODY_PARAMS = ("prim_path", "mass", "density", "kinematic", "add_collider", "approximation", "dry_run")


def _add_reference(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("add_reference", params, _ADD_REFERENCE_PARAMS)
    author = getattr(bridge, "add_reference", None)
    if not callable(author):
        return _bridge_lacks("add_reference", bridge)
    dry_run, warning = _dry_run_flag(params)
    instanceable = None if params.get("instanceable") is None else _flag(params, "instanceable", default=False)
    result = author(
        _path(params, "prim_path"),
        _file_path(params, "asset_path"),
        translation=_vector(params, "translation", 3),
        rotation=_vector(params, "rotation", 3),
        scale=_scale(params, "scale"),
        instanceable=instanceable,
        asset_prim_path=_path(params, "asset_prim_path", required=False),
        correct_units=_flag(params, "correct_units", default=False),
        dry_run=dry_run,
    )
    return _with_warning(result, warning)


def _physics_report(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("physics_report", params, ("prim_path",))
    report = getattr(bridge, "physics_report", None)
    if not callable(report):
        return _bridge_lacks("physics_report", bridge)
    return report(_path(params, "prim_path", required=False))


def _add_physics_scene(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("add_physics_scene", params, ("path", "gravity", "dry_run"))
    author = getattr(bridge, "add_physics_scene", None)
    if not callable(author):
        return _bridge_lacks("add_physics_scene", bridge)
    dry_run, warning = _dry_run_flag(params)
    path = _path(params, "path", required=False) or "/physicsScene"
    return _with_warning(author(path, gravity=_scale(params, "gravity"), dry_run=dry_run), warning)


def _make_rigid_body(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("make_rigid_body", params, _RIGID_BODY_PARAMS)
    author = getattr(bridge, "make_rigid_body", None)
    if not callable(author):
        return _bridge_lacks("make_rigid_body", bridge)
    dry_run, warning = _dry_run_flag(params)
    result = author(
        _path(params, "prim_path"),
        mass=_number(params, "mass"),
        density=_number(params, "density"),
        kinematic=_flag(params, "kinematic", default=False),
        add_collider=_flag(params, "add_collider", default=True),
        approximation=_word(params, "approximation"),
        dry_run=dry_run,
    )
    return _with_warning(result, warning)


def _make_collider(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("make_collider", params, ("prim_path", "approximation", "dry_run"))
    author = getattr(bridge, "make_collider", None)
    if not callable(author):
        return _bridge_lacks("make_collider", bridge)
    dry_run, warning = _dry_run_flag(params)
    result = author(_path(params, "prim_path"), approximation=_word(params, "approximation"), dry_run=dry_run)
    return _with_warning(result, warning)


def _remove_physics(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("remove_physics", params, ("prim_path", "include_descendants", "dry_run"))
    author = getattr(bridge, "remove_physics", None)
    if not callable(author):
        return _bridge_lacks("remove_physics", bridge)
    dry_run, warning = _dry_run_flag(params)
    result = author(
        _path(params, "prim_path"),
        include_descendants=_flag(params, "include_descendants", default=False),
        dry_run=dry_run,
    )
    return _with_warning(result, warning)


def _capture_viewport(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("capture_viewport", params, ("output_dir", "width", "height"))
    capture = getattr(bridge, "capture_viewport", None)
    if not callable(capture):
        return _bridge_lacks("capture_viewport", bridge)
    return capture(
        _file_path(params, "output_dir", required=False),
        width=_whole(params, "width", 1, _MAX_CAPTURE_SIDE),
        height=_whole(params, "height", 1, _MAX_CAPTURE_SIDE),
    )


def _capture_status(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("capture_status", params, ("path",))
    status = getattr(bridge, "capture_status", None)
    if not callable(status):
        return _bridge_lacks("capture_status", bridge)
    return status(_file_path(params, "path"))


def _simulation(method: str) -> Handler:
    """A handler for one parameterless simulation method (status, play, pause or stop)."""

    def handler(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
        _check_params(method, params, ())
        call = getattr(bridge, method, None)  # method is one of the fixed table names below, never caller input
        if not callable(call):
            return _bridge_lacks(method, bridge)
        return call()

    handler.__name__ = handler.__qualname__ = f"_{method}"
    return handler


def _simulation_step(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("simulation_step", params, ("frames",))
    step = getattr(bridge, "simulation_step", None)
    if not callable(step):
        return _bridge_lacks("simulation_step", bridge)
    frames = _whole(params, "frames", 1, None)  # the bridge caps it at 600 frames and says so
    return step(1 if frames is None else frames)


# Fixed method table: the only names the extended server adds. Lookups never touch attributes named by callers.
_HANDLERS: dict[str, Handler] = {
    "capabilities": _capabilities,
    "insight": _insight,
    "create_light": _create_light,
    "create_camera": _create_camera,
    "aim": _aim,
    "set_attribute": _set_attribute,
    "create_material": _create_material,
    "bind_material": _bind_material,
    "undo_last": _undo_last,
    "undo_history": _undo_history,
    "add_reference": _add_reference,
    "physics_report": _physics_report,
    "add_physics_scene": _add_physics_scene,
    "make_rigid_body": _make_rigid_body,
    "make_collider": _make_collider,
    "remove_physics": _remove_physics,
    "capture_viewport": _capture_viewport,
    "capture_status": _capture_status,
    "simulation_status": _simulation("simulation_status"),
    "simulation_play": _simulation("simulation_play"),
    "simulation_pause": _simulation("simulation_pause"),
    "simulation_stop": _simulation("simulation_stop"),
    "simulation_step": _simulation_step,
}


def _check_params(method: str, params: dict[str, Any], allowed: tuple[str, ...]) -> None:
    unknown = sorted(_clip(repr(key), 40) for key in params if key not in allowed)
    if unknown:
        raise ValueError(f"Unknown parameter(s) for '{method}': {unknown}; allowed: {list(allowed) or 'none'}")


def _path(params: dict[str, Any], key: str, required: bool = True) -> str | None:
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty prim path string")
    if len(value) > _MAX_PATH_CHARS:
        raise ValueError(f"'{key}' is longer than {_MAX_PATH_CHARS} characters")
    return value.strip()


def _file_path(params: dict[str, Any], key: str, required: bool = True) -> str | None:
    """A non-empty file or folder path string (at most _MAX_PATH_CHARS, no NUL); the bridge checks that it exists."""
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"'{key}' must be a non-empty file path string")
    if len(value) > _MAX_PATH_CHARS:
        raise ValueError(f"'{key}' is longer than {_MAX_PATH_CHARS} characters")
    return value.strip()


def _scale(params: dict[str, Any], key: str) -> float | tuple[float, ...] | None:
    """One number (a uniform scale, or a gravity magnitude) or exactly three numbers."""
    if isinstance(params.get(key), (list, tuple)):
        return _vector(params, key, 3)
    return _number(params, key)


def _whole(params: dict[str, Any], key: str, low: int, high: int | None) -> int | None:
    """A whole number from low to high (no upper bound when high is None); None when absent."""
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        limit = f"from {low} to {high}" if high is not None else f"of at least {low}"
        raise ValueError(f"'{key}' must be a whole number {limit}")
    return value


def _word(params: dict[str, Any], key: str, required: bool = False) -> str | None:
    value = params.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_WORD_CHARS:
        raise ValueError(f"'{key}' must be a non-empty string of at most {_MAX_WORD_CHARS} characters")
    return value.strip()


def _number(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"'{key}' must be a finite number")
    return float(value)


def _vector(params: dict[str, Any], key: str, length: int) -> tuple[float, ...] | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"'{key}' must contain exactly {length} numbers")
    numbers = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)):
            raise ValueError(f"'{key}' must contain exactly {length} finite numbers")
        numbers.append(float(item))
    return tuple(numbers)


def _size(params: dict[str, Any], key: str) -> float | tuple[float, ...] | None:
    """One number, or a pair: authoring takes (width, height) for rect and (length, radius) for cylinder lights."""
    if isinstance(params.get(key), (list, tuple)):
        return _vector(params, key, 2)
    return _number(params, key)


def _target(params: dict[str, Any], key: str) -> str | tuple[float, ...] | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return _path(params, key)
    return _vector(params, key, 3)


def _flag(params: dict[str, Any], key: str, default: bool) -> bool:
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ValueError(f"'{key}' must be true or false")


def _dry_run_flag(params: dict[str, Any]) -> tuple[bool, str | None]:
    """(dry_run, warning) for the edit and undo methods: only false or "false" writes; anything else is a dry run.

    A missing dry_run is a dry run without a warning; a malformed one (0, "no", "maybe", []) is a dry run with a
    warning, never a write and never an error, so a confused caller can only ever get a preview.
    """
    value = params.get("dry_run")
    if value is None or value is True:
        return True, None
    if value is False or (isinstance(value, str) and value.strip().lower() == "false"):
        return False, None
    if isinstance(value, str) and value.strip().lower() == "true":
        return True, None
    shown = _clip(repr(value), 40)
    return True, f"dry_run={shown} is not true or false, so this ran as a dry run and nothing was written."


def _with_warning(result: Any, warning: str | None) -> Any:
    if warning is None or not isinstance(result, dict):
        return result
    out = dict(result)
    existing = out.get("warnings")
    out["warnings"] = [*(existing if isinstance(existing, list) else []), warning]
    return out


def _name(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_NAME_CHARS:
        raise ValueError(f"'{key}' must be a non-empty attribute name of at most {_MAX_NAME_CHARS} characters")
    return value.strip()


def _value(params: dict[str, Any], key: str) -> Any:
    """Any JSON value except null, within size caps; edits.set_attribute coerces it to the attribute's type."""
    value = params.get(key)
    if value is None:
        raise ValueError(f"'{key}' is required (null cannot be authored as an attribute value)")
    _check_value(value, key, 0)
    return value


def _check_value(value: Any, key: str, depth: int) -> None:
    if depth > 3:
        raise ValueError(f"'{key}' is nested too deeply (at most 3 levels of lists)")
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"'{key}' must only contain finite numbers")
        return
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_VALUE_CHARS:
            raise ValueError(f"'{key}' is longer than {_MAX_TEXT_VALUE_CHARS} characters")
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_VALUE_ITEMS:
            raise ValueError(f"'{key}' has more than {_MAX_VALUE_ITEMS} items")
        for item in value:
            _check_value(item, key, depth + 1)
        return
    if isinstance(value, dict):  # the {real, imaginary} / {path} shapes usd_insight reports
        if len(value) > 8:
            raise ValueError(f"'{key}' objects may have at most 8 keys")
        for item in value.values():
            _check_value(item, key, depth + 1)
        return
    raise ValueError(f"'{key}' must be a JSON number, string, boolean, list or object")


def _time_code(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() == "default":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"'{key}' must be a finite time code number, or omitted for the default value")
    return float(value)


def _color(params: dict[str, Any], key: str) -> tuple[float, ...] | str | None:
    """[r, g, b] numbers, or a short color string such as "#ff8800" (edits.py converts and checks it)."""
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        if not value.strip() or len(value) > 32:
            raise ValueError(f"'{key}' must be [r, g, b] or a short color string such as '#ff8800'")
        return value.strip()
    return _vector(params, key, 3)


def _bridge_lacks(method: str, bridge: Any) -> dict[str, Any]:
    return unsupported_result(
        method,
        f"The {type(bridge).__name__} behind this server has no {method}(); update omniverse_scene_mcp and reload the bridge.",
        needs_bridge_update=True,
    )


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
