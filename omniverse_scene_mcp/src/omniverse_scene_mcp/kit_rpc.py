"""Extend the live scene JSON-RPC server with capabilities, insight, authoring, safe edits and MCP undo.

make_extended_server_class(base_cls) subclasses the LiveSceneControlServer that Isaac Sim (or Composer) loads from
live_scene_control.py, without editing that file. New methods come from a fixed handler table; every other method
goes to the base class's _dispatch unchanged. Runs inside Kit, but imports neither omni nor pxr itself (usd_insight,
which holds the insight time budget, is imported lazily). Every mutating method is a dry run unless the caller sends
dry_run=false; for the tier3-1 edit and undo methods a missing or malformed dry_run is also a dry run.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from .bridge import (
    BASIC_RPC_METHODS,
    EDIT_RPC_METHODS,
    INSIGHT_QUERIES,
    LIVE_RPC_METHODS,
    RPC_VERSION,
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
_WRITE_METHODS = ("create_light", "create_camera", "aim", *EDIT_RPC_METHODS, "undo_last")
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


def make_extended_server_class(base_cls: type) -> type:
    """Return a subclass of base_cls (a LiveSceneControlServer) that also answers LIVE_RPC_METHODS."""
    if not isinstance(base_cls, type) or not callable(getattr(base_cls, "_dispatch", None)):
        raise TypeError("make_extended_server_class needs a server class that defines _dispatch(payload)")

    class ExtendedLiveSceneControlServer(base_cls):  # type: ignore[misc, valid-type]
        """LiveSceneControlServer plus capabilities, insight, create_light, create_camera and aim."""

        rpc_version = RPC_VERSION
        rpc_methods = ALL_RPC_METHODS

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
