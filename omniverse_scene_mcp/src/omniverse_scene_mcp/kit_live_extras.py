"""Kit-only live extras: active-viewport screenshots and simulation (timeline/PhysX) controls.

Everything here runs INSIDE Kit (Isaac Sim / USD Composer) on its main thread, next to kit_bridge.py. Every omni
module is imported lazily on each call, so this module imports fine without Kit: then every function returns
{"ok": False, "message": "Kit not available"}.

Viewport capture (capture_viewport, capture_status):
  - Captures the ACTIVE viewport through omni.kit.viewport.utility.capture_viewport_to_file (Kit 106-110), falling
    back to ViewportAPI.schedule_capture(omni.kit.widget.viewport.capture.FileCapture(path)).
  - Only READS the viewport: it never switches the active camera and never changes the resolution, renderer,
    render mode or any other render setting. The PNG therefore has the viewport's current resolution; a requested
    width/height is applied afterwards by capture_status as a resized copy (when Pillow is importable).
  - Kit writes the file a frame or two later, so capture_viewport returns {"status": "pending", "path": ...}
    immediately and capture_status(path) reports {"ready", "size"} without waiting (ready: non-empty and
    unchanged for a quarter of a second).

Simulation controls (simulation_status/play/pause/stop/step):
  - Use omni.timeline's default timeline and, for stepping, omni.physx when importable.
  - They change SIMULATION state, not authored scene data. While physics runs PhysX moves simulated prims on the
    live stage; stop resets the timeline to its start time and PhysX restores the poses it had before play.
  - simulation_step(frames) advances at most MAX_STEP_FRAMES frames while paused (a stopped timeline is paused
    first). With PhysX it steps physics per frame like PhysX's own debug "Step" button; without PhysX it only moves
    the timeline forward, and says that physics may not advance.
"""

from __future__ import annotations

import importlib
import math
import os
import tempfile
import time
from typing import Any

__all__ = [
    "CAPTURE_DIR_NAME",
    "MAX_STEP_FRAMES",
    "capture_status",
    "capture_viewport",
    "simulation_pause",
    "simulation_play",
    "simulation_status",
    "simulation_step",
    "simulation_stop",
]

CAPTURE_DIR_NAME = "omniverse_mcp_captures"
MAX_STEP_FRAMES = 600
MAX_CAPTURE_SIDE = 16384
_DEFAULT_FPS = 60.0
_SETTLE_SECONDS = 0.25  # a capture file unchanged for this long (and non-empty) counts as written
_MAX_PENDING = 64
_NOT_KIT = "Kit not available"
_STATE_NOTE = (
    "This changes simulation state (timeline/physics), not authored scene data; nothing was saved. "
    "While physics runs, PhysX moves simulated prims on the live stage until stop restores them."
)
# path (normcase'd) -> {"capture": Kit capture delegate or None, "requested": (w, h) or None, "resized": path}
_PENDING: dict[str, dict[str, Any]] = {}


# ------------------------------------------------------------------------------------------------ helpers


def _kit_module(name: str) -> Any:
    """The named omni module, or None when it cannot be imported (not inside Kit, extension disabled)."""
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


def _call(obj: Any, name: str, *args: Any) -> Any:
    fn = getattr(obj, name, None)
    return fn(*args) if callable(fn) else None


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _path_text(value: Any) -> str | None:
    if value is None:
        return None
    text = getattr(value, "pathString", None)
    return str(text if text is not None else value) or None


# ------------------------------------------------------------------------------------------- viewport capture


def _requested_size(width: Any, height: Any) -> tuple[int | None, int | None]:
    sizes = []
    for name, value in (("width", width), ("height", height)):
        if value is None:
            sizes.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_CAPTURE_SIDE:
            raise ValueError(f"{name} must be a whole number of pixels from 1 to {MAX_CAPTURE_SIDE}")
        sizes.append(value)
    return sizes[0], sizes[1]


def _capture_file(output_dir: Any) -> str:
    folder = os.path.join(tempfile.gettempdir(), CAPTURE_DIR_NAME) if output_dir in (None, "") else os.fspath(output_dir)
    folder = os.path.abspath(os.path.expanduser(str(folder)))
    os.makedirs(folder, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + f"_{int((now % 1) * 1_000_000):06d}"
    path = os.path.join(folder, f"viewport_{stamp}.png")
    counter = 1
    while os.path.exists(path):  # never overwrite an earlier capture
        path = os.path.join(folder, f"viewport_{stamp}_{counter}.png")
        counter += 1
    return path


def _resolution(viewport: Any) -> list[int] | None:
    try:
        value = getattr(viewport, "resolution", None)
        if value is None:
            return None
        return [int(value[0]), int(value[1])]
    except Exception:  # noqa: BLE001
        return None


def _read_attr(viewport: Any, name: str) -> Any:
    try:
        value = getattr(viewport, name, None)
    except Exception:  # noqa: BLE001
        return None
    return None if value is None else str(value)


def _schedule(utility: Any, viewport: Any, path: str) -> tuple[Any, str]:
    """Start the capture with the newest API available; returns (capture delegate, api name)."""
    to_file = getattr(utility, "capture_viewport_to_file", None)
    if callable(to_file):
        return to_file(viewport, file_path=path), "omni.kit.viewport.utility.capture_viewport_to_file"
    capture_module = _kit_module("omni.kit.widget.viewport.capture")
    file_capture = getattr(capture_module, "FileCapture", None)
    schedule = getattr(viewport, "schedule_capture", None)
    if callable(file_capture) and callable(schedule):
        return schedule(file_capture(path)), "ViewportAPI.schedule_capture(FileCapture)"
    raise LookupError("this Kit has neither capture_viewport_to_file nor ViewportAPI.schedule_capture")


def _remember(path: str, capture: Any, requested: tuple[int, int] | None) -> None:
    while len(_PENDING) >= _MAX_PENDING:
        _PENDING.pop(next(iter(_PENDING)))
    _PENDING[os.path.normcase(path)] = {"capture": capture, "requested": requested, "resized": None}


def capture_viewport(output_dir: Any = None, *, width: int | None = None, height: int | None = None) -> dict[str, Any]:
    """Capture the ACTIVE viewport to a new PNG; returns at once with status 'pending' (see capture_status).

    Never switches the camera or changes render settings: the PNG has the viewport's current resolution. A
    requested width and/or height (aspect kept when only one is given) is produced later by capture_status as a
    resized copy. output_dir defaults to <temp>/omniverse_mcp_captures and is created when missing.
    """
    try:
        req_w, req_h = _requested_size(width, height)
    except ValueError as exc:
        return _error(str(exc))
    utility = _kit_module("omni.kit.viewport.utility")
    if utility is None:
        return _not_kit("omni.kit.viewport.utility is not importable")
    getter = getattr(utility, "get_active_viewport", None)
    if not callable(getter):
        return _error("omni.kit.viewport.utility has no get_active_viewport in this Kit version")
    try:
        viewport = getter()
    except Exception as exc:  # noqa: BLE001
        return _error(f"Could not get the active viewport: {type(exc).__name__}: {exc}")
    if viewport is None:
        return _error("There is no active viewport to capture (is a Viewport window open?)")

    camera = _path_text(getattr(viewport, "camera_path", None))
    resolution = _resolution(viewport)
    renderer = _read_attr(viewport, "hydra_engine")
    render_mode = _read_attr(viewport, "render_mode")
    try:
        path = _capture_file(output_dir)
    except OSError as exc:
        return _error(f"Could not create the capture folder: {exc}")
    try:
        capture, api = _schedule(utility, viewport, path)
    except Exception as exc:  # noqa: BLE001
        return _error(f"Viewport capture failed to start: {type(exc).__name__}: {exc}", path=path)

    requested = None
    if req_w or req_h:
        requested = (req_w or 0, req_h or 0)
    _remember(path, capture, requested)
    note = (
        "Capture scheduled: Kit writes the PNG within a frame or two. Call capture_status(path) until ready. "
        "The active camera and render settings were not changed."
    )
    if requested:
        note += " The PNG uses the viewport's own resolution; capture_status writes a resized copy when it is ready."
    return {
        "ok": True,
        "status": "pending",
        "path": path,
        "camera": camera,
        "resolution": resolution,
        "requested_resolution": [req_w, req_h] if requested else None,
        "renderer": renderer,
        "render_mode": render_mode,
        "api": api,
        "note": note,
    }


def _capture_done(capture: Any) -> bool | None:
    """Whether Kit's capture delegate says it finished; None when unknown."""
    future = getattr(capture, "_Capture__future", None)  # omni.kit.widget.viewport.capture.Capture's future
    done = getattr(future, "done", None)
    if not callable(done):
        return None
    try:
        return bool(done())
    except Exception:  # noqa: BLE001
        return None


def _resize(path: str, requested: tuple[int, int]) -> tuple[str | None, str | None]:
    """Write <stem>_<w>x<h>.png next to path; returns (resized path, problem)."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None, "Pillow is not importable here, so the requested size was not applied"
    try:
        with Image.open(path) as image:
            src_w, src_h = image.size
            want_w, want_h = requested
            if not want_w:
                want_w = max(1, round(src_w * want_h / src_h))
            if not want_h:
                want_h = max(1, round(src_h * want_w / src_w))
            if (want_w, want_h) == (src_w, src_h):
                return path, None
            stem, ext = os.path.splitext(path)
            target = f"{stem}_{want_w}x{want_h}{ext}"
            image.resize((want_w, want_h)).save(target)
        return target, None
    except Exception as exc:  # noqa: BLE001
        return None, f"Resizing failed: {type(exc).__name__}: {exc}"


def capture_status(path: Any) -> dict[str, Any]:
    """Whether a capture_viewport PNG has been written; never waits. Returns {ok, path, ready, size, ...}."""
    if not path:
        return _error("path is required")
    text = os.path.abspath(os.fspath(path))
    if os.path.splitext(text)[1].lower() != ".png":
        return _error("capture_status only checks .png capture files", path=text)
    record = _PENDING.get(os.path.normcase(text))
    try:
        stat = os.stat(text)
    except FileNotFoundError:
        size, age = 0, 0.0
    except OSError as exc:
        return _error(f"Could not read the capture file: {exc}", path=text)
    else:
        size, age = int(stat.st_size), time.time() - stat.st_mtime
    done = _capture_done(record["capture"]) if record else None
    # Kit may still be writing for a moment after its delegate completes, so the file must also have settled.
    ready = size > 0 and age >= _SETTLE_SECONDS
    result: dict[str, Any] = {"ok": True, "path": text, "ready": bool(ready), "size": size}
    if done is not None:
        result["capture_done"] = done
    if record is None:
        result["note"] = "Not a capture started by this bridge session; readiness is judged from the file alone."
    elif not ready:
        result["note"] = "Not written yet; Kit saves it after the next rendered frame or two. Check again shortly."
    elif record["requested"]:
        if record["resized"] is None:
            record["resized"], problem = _resize(text, record["requested"])
            if problem:
                record["resized"] = ""
                result["resize_note"] = problem
        if record["resized"]:
            result["resized_path"] = record["resized"]
    return result


# ------------------------------------------------------------------------------------------ simulation controls


def _timeline() -> Any:
    module = _kit_module("omni.timeline")
    getter = getattr(module, "get_timeline_interface", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


def _physx() -> Any:
    module = _kit_module("omni.physx")
    getter = getattr(module, "get_physx_interface", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:  # noqa: BLE001
        return None


def _commit(timeline: Any) -> None:
    """Apply pending timeline changes now (main thread), so the returned status is the new state."""
    try:
        _call(timeline, "commit")
    except Exception:  # noqa: BLE001 - the change still applies on Kit's next update
        pass


def _tcps(timeline: Any) -> float:
    rate = _float(_call(timeline, "get_time_codes_per_seconds"))
    return rate if rate and rate > 0 else _DEFAULT_FPS


def _status(timeline: Any, physx: Any = None) -> dict[str, Any]:
    playing = bool(_call(timeline, "is_playing"))
    stopped_raw = _call(timeline, "is_stopped")
    stopped = bool(stopped_raw) and not playing
    rate = _tcps(timeline)
    current = _float(_call(timeline, "get_current_time"))
    status: dict[str, Any] = {
        "ok": True,
        "playing": playing,
        "paused": (not playing and stopped_raw is not None and not stopped),
        "stopped": stopped,
        "current_time": current,
        "current_time_code": None if current is None else current * rate,
        "start_time": _float(_call(timeline, "get_start_time")),
        "end_time": _float(_call(timeline, "get_end_time")),
        "time_codes_per_second": rate,
        "physics_available": (physx if physx is not None else _physx()) is not None,
    }
    return status


def simulation_status() -> dict[str, Any]:
    """{ok, playing, paused, stopped, current_time (s), current_time_code, start/end_time, time_codes_per_second,
    physics_available}. Read-only."""
    timeline = _timeline()
    if timeline is None:
        return _not_kit("omni.timeline is not importable")
    try:
        return _status(timeline)
    except Exception as exc:  # noqa: BLE001
        return _error(f"Could not read the timeline: {type(exc).__name__}: {exc}")


def _control(action: str) -> dict[str, Any]:
    timeline = _timeline()
    if timeline is None:
        return _not_kit("omni.timeline is not importable")
    try:
        before = _status(timeline)
        start_time = before["start_time"]
        if action == "play":
            if before["playing"]:
                message = "The simulation was already playing."
            else:
                timeline.play()
                message = "Playing: the timeline advances every frame and physics (if any) simulates."
        elif action == "pause":
            if before["paused"]:
                message = "The simulation was already paused."
            else:
                timeline.pause()
                message = "Paused at the current time; simulation_step advances it frame by frame."
        else:
            timeline.stop()
            where = "its start time" if start_time is None else f"its start time ({start_time:g} s)"
            message = (
                f"Stopped: the timeline was reset to {where}, and PhysX restores simulated prims to the poses "
                "they had before play."
            )
        _commit(timeline)
        result = _status(timeline)
    except Exception as exc:  # noqa: BLE001
        return _error(f"simulation_{action} failed: {type(exc).__name__}: {exc}")
    result.update(action=action, message=message, note=_STATE_NOTE)
    return result


def simulation_play() -> dict[str, Any]:
    """Play the timeline (physics simulates while playing); returns the new status."""
    return _control("play")


def simulation_pause() -> dict[str, Any]:
    """Pause the timeline at the current time; returns the new status."""
    return _control("pause")


def simulation_stop() -> dict[str, Any]:
    """Stop the timeline: it RESETS to the start time and PhysX restores pre-play poses; returns the new status."""
    return _control("stop")


def simulation_step(frames: Any = 1) -> dict[str, Any]:
    """Advance a paused simulation by frames (1..600; larger requests are capped) and return the new status.

    A stopped timeline is paused first; a playing one is refused (pause it first). Uses PhysX
    update_simulation per frame when omni.physx is available, else only moves the timeline forward.
    """
    if isinstance(frames, bool) or not isinstance(frames, int):
        return _error("frames must be a whole number from 1 to 600")
    if frames < 1:
        return _error("frames must be at least 1")
    requested = frames
    frames = min(frames, MAX_STEP_FRAMES)
    timeline = _timeline()
    if timeline is None:
        return _not_kit("omni.timeline is not importable")
    try:
        before = _status(timeline)
        if before["playing"]:
            return _error("The simulation is playing; call simulation_pause first, then step.", **_strip(before))
        if before["stopped"]:
            timeline.pause()  # enter the paused state at the current (start) time, as the Pause button does
            _commit(timeline)
        rate = _tcps(timeline)
        dt = 1.0 / rate
        start = _float(_call(timeline, "get_current_time")) or 0.0
        physx = _physx()
        via, notes = "timeline", []
        if physx is not None and bool(_call(physx, "is_asyncsimrender_enabled")):
            notes.append("PhysX async simulate/render is enabled, so physics was not stepped manually.")
            physx = None
        current = start
        if physx is not None:
            if _call(physx, "is_running") is False:
                physx.start_simulation()  # stores the initial poses that stop restores, like PhysX's Step button
            for index in range(1, frames + 1):
                current = start + index * dt
                timeline.set_current_time(current)
                physx.update_simulation(dt, current)
            _call(physx, "update_transformations", False, True, True)
            via = "physx"
        else:
            current = start + frames * dt
            timeline.set_current_time(current)
            notes.append("omni.physx was not used: only the timeline moved forward, so physics may not advance.")
        _commit(timeline)
        result = _status(timeline)
    except Exception as exc:  # noqa: BLE001
        return _error(f"simulation_step failed: {type(exc).__name__}: {exc}")
    end = result.get("end_time")
    if end is not None and current > end + 1e-9:
        notes.append(f"The time is now past the timeline end time ({end:g} s).")
    result.update(
        action="step",
        frames=frames,
        requested_frames=requested,
        capped=requested > frames,
        seconds_per_frame=dt,
        stepped_seconds=frames * dt,
        via=via,
        message=f"Stepped {frames} frame(s) ({frames * dt:g} s) via {via}.",
        note=_STATE_NOTE,
    )
    if requested > frames:
        notes.insert(0, f"frames is capped at {MAX_STEP_FRAMES}; {requested} were requested.")
    if notes:
        result["notes"] = notes
    return result


def _strip(status: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in status.items() if key not in ("ok", "message")}
