"""Record and replay robot joint motion (live Isaac Sim), and compare two runs.

Words: a RECORDING is a list of SAMPLES; each sample is the time and every joint's position (degrees for hinges,
metres for sliders). REPLAY sends those positions back to the robot as drive targets, frame by frame, so it moves
the same way again. COMPARE lines two recordings up in time and says how far each joint differed: handy to check
that a change to your controller did not change the motion, or how much two runs drift.

record_start subscribes to Kit's per-frame update and reads the joints from PhysX while the simulation plays;
record_stop saves the samples as a NEW JSON file in isaac_study/recordings/ (never overwriting one). Nothing is
authored to USD: Stop resets the robot as usual.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any, Callable

from . import joint_control as jc

MAX_SECONDS = 120.0
MAX_SAMPLES = 20000
_ACTIVE: dict[str, Any] = {}  # "record" / "replay" -> state


def _folder() -> Path:
    from .exam.progress import workspace_root

    return workspace_root() / "isaac_study" / "recordings"


def _timeline_time() -> float:
    timeline = jc._kit_module("omni.timeline")
    return float(timeline.get_timeline_interface().get_current_time()) if timeline else 0.0


def _subscribe(callback: Callable[[Any], None]) -> Any:
    """Call callback every app update (Kit main thread). Returns a handle; set it to None (or unsubscribe) to stop."""
    app = jc._kit_module("omni.kit.app")
    if app is None:
        raise RuntimeError("Kit is not available: run this in Isaac Sim.")
    return app.get_app().get_update_event_stream().create_subscription_to_pop(callback, name="mcp_motion")


def _stop(kind: str) -> dict[str, Any] | None:
    state = _ACTIVE.pop(kind, None)
    if state is not None:
        handle = state.get("handle")
        if handle is not None and hasattr(handle, "unsubscribe"):
            handle.unsubscribe()
        state["handle"] = None
    return state


def record_start(robot_root: str, seconds: float = 10.0, *, dry_run: bool = True) -> dict[str, Any]:
    """KIT-ONLY. Start recording a playing robot's joint positions every frame for up to `seconds` of simulation
    time (max 120). dry_run=True only checks the robot. record_stop saves the file."""
    base = {"action": "record_start", "robot_root": robot_root, "dry_run": jc._dry(dry_run)}
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 0 < seconds <= MAX_SECONDS:
        return {**base, "ok": False, "error": f"seconds must be more than 0 and at most {MAX_SECONDS:g}."}
    if "record" in _ACTIVE:
        return {**base, "ok": False, "error": "A recording is already running: record_stop first."}
    live, refusal, state, mpu = jc._open_live(robot_root)
    if refusal is not None:
        return {**refusal, **base}
    names, kinds = live.names(), live.kinds()
    if base["dry_run"]:
        return {**base, "ok": True, "joints": names, "timeline": state,
                "next_step": "Dry run: nothing is recording. Call again with dry_run=False, then move the robot "
                             "(or let your graph drive it) and call record_stop."}
    recording = {"robot_root": robot_root, "joints": names, "kinds": kinds, "units":
                 [jc.UNIT.get(k, "native") for k in kinds], "samples": [], "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    start = _timeline_time()
    entry: dict[str, Any] = {"recording": recording, "start": start, "seconds": float(seconds), "errors": []}

    def on_update(_event: Any) -> None:
        now = _timeline_time() - start
        if now < 0 or len(recording["samples"]) >= MAX_SAMPLES:
            return
        if now > entry["seconds"]:
            entry["finished"] = True
            return
        try:
            positions = live.state()["position"] or []
        except Exception as exc:  # noqa: BLE001 - the robot vanished or Stop was pressed
            entry["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
            return
        sample = [jc._to_user(kinds[i], positions[i], mpu) if i < len(positions) else None for i in range(len(names))]
        if not recording["samples"] or recording["samples"][-1]["t"] != round(now, 4):
            recording["samples"].append({"t": round(now, 4), "q": sample})

    try:
        entry["handle"] = _subscribe(on_update)
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "error": str(exc)}
    _ACTIVE["record"] = entry
    return {**base, "ok": True, "recording": True, "joints": names, "timeline": state,
            "note": "Recording while the simulation plays (pauses are skipped). Call record_stop to save it."}


def record_stop(name: str | None = None) -> dict[str, Any]:
    """KIT-ONLY. Stop the recording and save it as a new isaac_study/recordings/<name>.json."""
    entry = _stop("record")
    if entry is None:
        return {"ok": False, "action": "record_stop", "error": "Nothing is recording (record_start first)."}
    recording = entry["recording"]
    if not recording["samples"]:
        return {"ok": False, "action": "record_stop", "error": "No samples were recorded: was the simulation "
                "playing?", "errors": entry["errors"][:5]}
    stem = name or "recording_" + time.strftime("%Y%m%d_%H%M%S")
    if not re.match(r"^[A-Za-z0-9_\-]{1,60}$", stem):
        return {"ok": False, "action": "record_stop", "error": "name may use letters, digits, _ and - only."}
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{stem}.json"
    index = 1
    while path.exists():  # never overwrite
        path = folder / f"{stem}_{index}.json"
        index += 1
    path.write_text(json.dumps(recording, indent=1), encoding="utf-8")
    samples = recording["samples"]
    return {"ok": True, "action": "record_stop", "file": str(path), "samples": len(samples),
            "seconds": samples[-1]["t"], "joints": recording["joints"], "errors": entry["errors"][:5],
            "next_step": f"replay_motion(file='{path.name}') plays it back; compare_motion(a, b) compares two runs."}


def _load(file: str) -> tuple[dict[str, Any] | None, str | None]:
    path = Path(file)
    if not path.is_absolute():
        path = _folder() / file
    if path.suffix != ".json":
        path = path.with_suffix(".json")
    if not path.is_file():
        return None, f"No recording {path.name} in {path.parent}."
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"{path.name} is not a readable recording: {exc}"
    if not isinstance(data, dict) or not data.get("samples") or not data.get("joints"):
        return None, f"{path.name} has no samples."
    return data, None


def _at(samples: list[dict[str, Any]], t: float) -> list[float | None]:
    """Joint positions at time t, interpolated between the two nearest samples."""
    if t <= samples[0]["t"]:
        return samples[0]["q"]
    if t >= samples[-1]["t"]:
        return samples[-1]["q"]
    low, high = 0, len(samples) - 1
    while high - low > 1:
        mid = (low + high) // 2
        low, high = (mid, high) if samples[mid]["t"] <= t else (low, mid)
    a, b = samples[low], samples[high]
    span = b["t"] - a["t"] or 1.0
    w = (t - a["t"]) / span
    return [None if x is None or y is None else x + (y - x) * w for x, y in zip(a["q"], b["q"])]


def replay_motion(file: str, speed: float = 1.0, *, dry_run: bool = True) -> dict[str, Any]:
    """KIT-ONLY, SIMULATION STATE. Play a recording back on its robot as drive position targets (speed 0.25-4)."""
    base = {"action": "replay_motion", "file": file, "dry_run": jc._dry(dry_run)}
    data, problem = _load(file)
    if problem:
        return {**base, "ok": False, "error": problem}
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not 0.25 <= speed <= 4:
        return {**base, "ok": False, "error": "speed must be from 0.25 to 4."}
    if "replay" in _ACTIVE:
        return {**base, "ok": False, "error": "A replay is already running; wait or call replay_stop."}
    live, refusal, state, mpu = jc._open_live(data["robot_root"])
    if refusal is not None:
        return {**refusal, **base}
    names, kinds = live.names(), live.kinds()
    missing = [j for j in data["joints"] if j not in names]
    if missing:
        return {**base, "ok": False, "error": f"The robot has no joints {missing}: is it the same robot?"}
    indices = [names.index(j) for j in data["joints"]]
    duration = data["samples"][-1]["t"] / speed
    if base["dry_run"]:
        return {**base, "ok": True, "robot_root": data["robot_root"], "samples": len(data["samples"]),
                "plays_for_seconds": round(duration, 2), "timeline": state,
                "next_step": "Dry run: nothing moved. Press Play, then call again with dry_run=False."}
    start = _timeline_time()
    entry: dict[str, Any] = {"sent": 0, "errors": []}

    def on_update(_event: Any) -> None:
        t = (_timeline_time() - start) * speed
        if t > data["samples"][-1]["t"]:
            _stop("replay")
            entry["finished"] = True
            return
        values, joints = [], []
        for slot, value in enumerate(_at(data["samples"], max(0.0, t))):
            if value is None:
                continue
            kind = kinds[indices[slot]]
            values.append(math.radians(value) if kind == "revolute" else value / mpu)
            joints.append(indices[slot])
        try:
            live.set_targets(values, joints)
            entry["sent"] += 1
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
            _stop("replay")

    try:
        entry["handle"] = _subscribe(on_update)
    except Exception as exc:  # noqa: BLE001
        return {**base, "ok": False, "error": str(exc)}
    _ACTIVE["replay"] = entry
    return {**base, "ok": True, "replaying": True, "robot_root": data["robot_root"],
            "plays_for_seconds": round(duration, 2),
            "note": "The robot follows the recording as drive targets (not USD). It stops by itself at the end."}


def replay_stop() -> dict[str, Any]:
    """KIT-ONLY. Stop a running replay early."""
    entry = _stop("replay")
    return {"ok": True, "action": "replay_stop", "was_running": entry is not None,
            "targets_sent": entry["sent"] if entry else 0}


def compare_motion(a: str, b: str) -> dict[str, Any]:
    """Read-only: how far two recordings of the same robot differ, joint by joint (max and average), lined up in
    time. Tells you whether a change kept the motion the same."""
    first, problem = _load(a)
    second, problem_b = (None, None) if problem else _load(b)
    if problem or problem_b:
        return {"ok": False, "error": problem or problem_b}
    shared = [j for j in first["joints"] if j in second["joints"]]
    if not shared:
        return {"ok": False, "error": "The recordings share no joints: they are of different robots."}
    end = min(first["samples"][-1]["t"], second["samples"][-1]["t"])
    steps = max(2, min(500, int(end * 60)))
    rows = []
    for joint in shared:
        ia, ib = first["joints"].index(joint), second["joints"].index(joint)
        diffs = []
        for k in range(steps + 1):
            t = end * k / steps
            x, y = _at(first["samples"], t)[ia], _at(second["samples"], t)[ib]
            if x is not None and y is not None:
                diffs.append((abs(x - y), t))
        if not diffs:
            continue
        worst, when = max(diffs)
        unit = first["units"][ia] if ia < len(first.get("units", [])) else ""
        rows.append({"joint": joint, "unit": unit, "max_difference": round(worst, 4), "at_second": round(when, 2),
                     "average_difference": round(sum(d for d, _ in diffs) / len(diffs), 4)})
    rows.sort(key=lambda r: -r["max_difference"])
    same = all(r["max_difference"] < (1.0 if r["unit"] == "degrees" else 0.01) for r in rows)
    return {"ok": True, "read_only": True, "compared_seconds": round(end, 2), "joints": rows,
            "verdict": "The same motion (every joint within 1 degree / 1 cm)." if same else
                       f"Different: {rows[0]['joint']} differs most ({rows[0]['max_difference']} {rows[0]['unit']} "
                       f"at {rows[0]['at_second']} s).",
            "durations": [first["samples"][-1]["t"], second["samples"][-1]["t"]]}


def list_recordings() -> dict[str, Any]:
    """Read-only: the saved recordings in isaac_study/recordings/."""
    folder = _folder()
    rows = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data, problem = _load(str(path))
        rows.append({"file": path.name, "robot_root": data["robot_root"] if data else None,
                     "seconds": data["samples"][-1]["t"] if data else None, "problem": problem})
    return {"ok": True, "read_only": True, "folder": str(folder), "recordings": rows}
