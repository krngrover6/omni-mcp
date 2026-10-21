"""Extend the live scene JSON-RPC server with capabilities, live insight and light/camera authoring.

make_extended_server_class(base_cls) subclasses the LiveSceneControlServer that Isaac Sim (or Composer) loads from
live_scene_control.py, without editing that file. New methods come from a fixed handler table; every other method
goes to the base class's _dispatch unchanged. Runs inside Kit, but imports neither omni nor pxr itself.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from .bridge import BASIC_RPC_METHODS, INSIGHT_QUERIES, LIVE_RPC_METHODS, RPC_VERSION, unsupported_result

ALL_RPC_METHODS: tuple[str, ...] = BASIC_RPC_METHODS + LIVE_RPC_METHODS
_MAX_PATH_CHARS = 4096
_MAX_WORD_CHARS = 64
# live_scene_control._submit stops waiting after 15 s but leaves the request queued, so Kit would still run it on a
# later update while the caller was told it failed. Writes older than this are dropped instead (must stay below 15 s).
_WRITE_METHODS = ("create_light", "create_camera", "aim")
_STALE_AFTER_S = 10.0

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
            "dry_run_default": True,
        }
    )
    return info


def _insight(bridge: Any, params: dict[str, Any]) -> dict[str, Any]:
    _check_params("insight", params, ("query", "args"))
    query = params.get("query")
    name = query.strip().lower() if isinstance(query, str) else ""
    if name not in INSIGHT_QUERIES:
        raise ValueError(f"Unknown insight query {_clip(repr(query))}; use one of {list(INSIGHT_QUERIES)}")
    args = params.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError(f"insight args must be a JSON object, not {type(args).__name__}")
    runner = getattr(bridge, "insight", None)
    if not callable(runner):
        return _bridge_lacks("insight", bridge)
    return runner(name, dict(args))


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


# Fixed method table: the only names the extended server adds. Lookups never touch attributes named by callers.
_HANDLERS: dict[str, Handler] = {
    "capabilities": _capabilities,
    "insight": _insight,
    "create_light": _create_light,
    "create_camera": _create_camera,
    "aim": _aim,
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


def _bridge_lacks(method: str, bridge: Any) -> dict[str, Any]:
    return unsupported_result(
        method,
        f"The {type(bridge).__name__} behind this server has no {method}(); update omniverse_scene_mcp and reload the bridge.",
        needs_bridge_update=True,
    )


def _clip(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."
