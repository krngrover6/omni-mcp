"""Simple animation with USD time samples: timeline, keyframes, turntables, conveyor rides, door swings.

Read-only: timeline_info, sample, conveyor_plan (a plan and a Python snippet; it never authors physics).
Writers (dry run by default; only dry_run=False writes, into the stage's edit target): set_timeline,
add_keyframes, turntable, move_along, swing, clear_animation.

Time in USD: a time code is a frame number on the stage timeline. seconds = time code / timeCodesPerSecond;
framesPerSecond is only the playback rate hint. A time sample is a value at one time code. Value resolution:
the strongest layer with an opinion wins; inside that layer, time samples beat the default value at every
numeric time (the default is used only at the Default time or when there are no samples). Between two samples
USD interpolates (linear for numbers and vectors, shortest-arc slerp for quaternions; or held when the stage
interpolation type is Held). Before the first and after the last sample the nearest sample is held.

Transform ops are animated in place: the existing translate, rotation (rotateXYZ-style, single-axis rotate or
Isaac's orient quaternion) and scale ops keep their names, types and precision, and xformOpOrder is never
reordered. A missing op is added in the standard place (translate first, rotateXYZ before scale, scale last)
together with a default value that keeps today's pose. Single matrix ops, several rotation ops, inverted ops,
instance proxies and attributes with a stronger opinion in another layer are refused.

Imports only pxr, the standard library and this package's pure-pxr helpers (USD 25.11 and usd-core 26.x APIs),
so the same functions run on a file stage or the live stage inside Kit / Isaac Sim. A failed write puts the edit
target's specs for the prim (or the layer's time metadata) back from a snapshot.
"""

from __future__ import annotations

import math
import numbers
from typing import Any

from . import look_at
from .authoring import (
    _check_edit_target,
    _check_stage,
    _clean,
    _dry,
    _edit_target_info,
    _prim_path,
    _Refusal,
    _stronger_opinion,
    _text,
    _warn_animated_ancestors,
)
from .layout import _dirty_layers, _fmt, _precision_name, _short, _translate_slot, _units_sentence

try:
    from pxr import Gf, Sdf, Usd, UsdGeom
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

PUBLIC_FUNCTIONS = (
    "timeline_info",
    "set_timeline",
    "add_keyframes",
    "turntable",
    "move_along",
    "swing",
    "clear_animation",
    "sample",
    "conveyor_plan",
)
READ_ONLY_FUNCTIONS = ("timeline_info", "sample", "conveyor_plan")
MAX_STEP_DEGREES = 30.0  # turntable / swing keys are at most this far apart, far below the 180 slerp limit
MAX_SAMPLES = 20_000
MAX_POINTS = 10_000
MAX_TIMES = 1_000
MAX_SECONDS = 1e6
MAX_TURNS = 1_000
MAX_SCAN_PRIMS = 50_000
MAX_LISTED = 25
MAX_LISTED_SAMPLES = 60
MAX_PREVIEW_ROWS = 25
MAX_WARNINGS = 40
TRANSLATE = "xformOp:translate"
ROTATE_XYZ = "xformOp:rotateXYZ"
SCALE = "xformOp:scale"
RESET_TOKEN = "!resetXformStack!"
TIME_KEYS = ("startTimeCode", "endTimeCode", "framesPerSecond", "timeCodesPerSecond")
_AXES = "xyz"
_SINGULAR = 1e-12

# Verified read-only in the installed Isaac Sim 6.1 (omni.usd.schema.physx 110.3.2) - see conveyor_plan.
PHYSX_SCHEMA_SOURCE = (
    "C:/Users/krngr/Omniverse/isaacsim-6.1.0/extscache/omni.usd.schema.physx-110.3.2+110.3.0.wx64.r.cp312.u7f4/"
    "plugins/PhysxSchema/resources/generatedSchema.usda"
)
PHYSX_SCHEMA_DOC = (
    "C:/Users/krngr/Omniverse/isaacsim-6.1.0/extscache/omni.usd.schema.physx-110.3.2+110.3.0.wx64.r.cp312.u7f4/"
    "plugins/PhysxSchema/resources/schema.usda"
)
PHYSX_DEMO_SOURCE = (
    "C:/Users/krngr/Omniverse/isaacsim-6.1.0/extscache/omni.physx.demos-110.3.2+110.3.0.cp312.u7f4/"
    "omni/physxdemos/scenes/SurfaceVelocityDemo.py"
)
PHYSX_PYTHON_STUB = (
    "C:/Users/krngr/Omniverse/isaacsim-6.1.0/extscache/omni.usd.schema.physx-110.3.2+110.3.0.wx64.r.cp312.u7f4/"
    "pxr/PhysxSchema/__init__.pyi"
)
ISAAC_CONVEYOR_SOURCE = (
    "C:/Users/krngr/Omniverse/isaacsim-6.1.0/exts/isaacsim.asset.gen.conveyor/isaacsim/asset/gen/conveyor/impl/conveyor.py"
)
SURFACE_VELOCITY_API = "PhysxSurfaceVelocityAPI"
SURFACE_VELOCITY_ATTRIBUTES = {
    "physxSurfaceVelocity:surfaceVelocity": {"type": "vector3f", "fallback": [0.0, 0.0, 0.0], "meaning": "linear belt speed (stage units per second)"},
    "physxSurfaceVelocity:surfaceAngularVelocity": {"type": "vector3f", "fallback": [0.0, 0.0, 0.0], "meaning": "angular surface speed (degrees per second), for round turntables"},
    "physxSurfaceVelocity:surfaceVelocityEnabled": {"type": "bool", "fallback": True, "meaning": "turns the effect on or off"},
    "physxSurfaceVelocity:surfaceVelocityLocalSpace": {"type": "bool", "fallback": True, "meaning": "True: the vector is in the belt's own (local) axes; False: world axes"},
}

TIME_EXPLANATION = (
    "A time code is a frame number on the stage timeline; seconds = time code / timeCodesPerSecond. "
    "framesPerSecond is only how fast players step through it. Keep them equal so one time code is one frame."
)
RESOLUTION_EXPLANATION = (
    "Value resolution: the strongest layer with an opinion wins, and inside it time samples beat the default value "
    "at every frame (the default is only used at the Default time or when there are no samples). Between samples USD "
    "interpolates linearly (quaternions take the shortest arc); before the first and after the last sample it holds the "
    "nearest one."
)


# --- read-only: timeline_info -----------------------------------------------------------------------------


def timeline_info(stage: Any) -> dict[str, Any]:
    """Read-only: start/end time codes, timeCodesPerSecond, framesPerSecond, duration and animated attributes."""
    action = "timeline_info"
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        tcps = float(stage.GetTimeCodesPerSecond())
        fps = float(stage.GetFramesPerSecond())
        start = float(stage.GetStartTimeCode())
        end = float(stage.GetEndTimeCode())
        authored = bool(stage.HasAuthoredTimeCodeRange())
        animated: list[dict[str, Any]] = []
        count = 0
        prims_with_samples = set()
        first = last = None
        scanned = 0
        for prim in stage.Traverse():
            scanned += 1
            if scanned > MAX_SCAN_PRIMS:
                warnings.append(f"Stopped counting after {MAX_SCAN_PRIMS} prims; the counts below are a lower bound.")
                break
            for attr in prim.GetAttributes():
                samples = attr.GetNumTimeSamples()
                if samples <= 0:
                    continue
                times = attr.GetTimeSamples()
                count += 1
                prims_with_samples.add(str(prim.GetPath()))
                lo, hi = float(times[0]), float(times[-1])
                first = lo if first is None else min(first, lo)
                last = hi if last is None else max(last, hi)
                if len(animated) < MAX_LISTED:
                    animated.append({"attribute": str(attr.GetPath()), "samples": int(samples), "first": _clean(lo), "last": _clean(hi)})
        if not authored:
            warnings.append(
                "The root layer has no startTimeCode/endTimeCode, so players show an empty (0 to 0) timeline; "
                "use set_timeline(start=..., end=...) to give it a range."
            )
        if first is not None and authored and (first < start or last > end):
            warnings.append(
                f"Some samples ({_fmt(first)} to {_fmt(last)}) lie outside the timeline ({_fmt(start)} to {_fmt(end)}), "
                "so playback does not reach them."
            )
        if abs(tcps - fps) > 1e-9:
            warnings.append(
                f"timeCodesPerSecond ({_fmt(tcps)}) differs from framesPerSecond ({_fmt(fps)}): one time code is not one "
                "played frame, which confuses beginners. Usually they are kept equal."
            )
        duration_codes = max(0.0, end - start)
        seconds = duration_codes / tcps if tcps > 0 else None
        result = {
            "ok": True,
            "action": action,
            "read_only": True,
            "start_time_code": _clean(start),
            "end_time_code": _clean(end),
            "has_authored_time_range": authored,
            "time_codes_per_second": _clean(tcps),
            "frames_per_second": _clean(fps),
            "duration_time_codes": _clean(duration_codes),
            "duration_seconds": _clean(seconds) if seconds is not None else None,
            "frame_count": int(round(seconds * fps)) + 1 if seconds is not None and authored else None,
            "interpolation": _interpolation_name(stage),
            "animated_attributes_count": count,
            "animated_prims_count": len(prims_with_samples),
            "animated_attributes": animated,
            "animated_attributes_truncated": max(0, count - len(animated)),
            "sample_time_range": [_clean(first), _clean(last)] if first is not None else None,
            "time_metadata_layer": _text(stage.GetRootLayer().identifier),
            "warnings": _unique(warnings),
        }
        sec_text = f"{_fmt(seconds)} s" if seconds is not None else "unknown"
        result["explanation"] = (
            f"The timeline runs from time code {_fmt(start)} to {_fmt(end)} ({_fmt(duration_codes)} time codes = {sec_text} "
            f"at {_fmt(tcps)} time codes per second; played at {_fmt(fps)} frames per second). {count} attribute(s) on "
            f"{len(prims_with_samples)} prim(s) have time samples. {TIME_EXPLANATION} {RESOLUTION_EXPLANATION}"
        )
        result["preview"] = _table(
            [("attribute", "samples", "first", "last")]
            + [(_short(row["attribute"], 3), str(row["samples"]), _fmt(row["first"]), _fmt(row["last"])) for row in animated]
        )
        return result
    except _Refusal as refusal:
        return _read_error(action, str(refusal), warnings)
    except Exception as exc:  # noqa: BLE001 - read only
        return _read_error(action, f"Unexpected error while reading the timeline: {exc}", warnings)


# --- writer: set_timeline ---------------------------------------------------------------------------------


def set_timeline(
    stage: Any,
    *,
    start: Any = None,
    end: Any = None,
    fps: Any = None,
    time_codes_per_second: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (default) or author the stage time range and rates as layer metadata.

    The stage reads startTimeCode, endTimeCode, timeCodesPerSecond and framesPerSecond only from the root layer
    (or the session layer, which is stronger), so they go to the session layer when it is the edit target and to
    the root layer otherwise. fps sets framesPerSecond; timeCodesPerSecond follows it when the two were equal
    (keeping one time code = one frame) unless time_codes_per_second is given.
    """
    action = "set_timeline"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        dirty_before = _dirty_layers(stage)
        edit = _edit_target_info(stage)
        if edit is None:
            raise _Refusal("The stage has no edit target layer.")
        root = stage.GetRootLayer()
        session = stage.GetSessionLayer()
        if edit["session_layer"] and session is not None:
            layer, layer_role = session, "session layer"
            warnings.append("The edit target is the session layer, so the timeline change is not saved with the scene.")
        else:
            layer, layer_role = root, "root layer"
            if not edit["root_layer"]:
                warnings.append(
                    f"The edit target is {edit['display_name']}, but a stage only reads time metadata from its root layer, "
                    "so the change goes to the root layer (time metadata in sublayers only rescales their own samples)."
                )
        if not layer.permissionToEdit:
            raise _Refusal(f"The {layer_role} {layer.identifier} is read-only (permissionToEdit is False).")
        if start is None and end is None and fps is None and time_codes_per_second is None:
            raise _Refusal("Nothing to change: pass start, end, fps and/or time_codes_per_second.")
        new: dict[str, float] = {}
        if start is not None:
            new["startTimeCode"] = _finite(start, "start")
        if end is not None:
            new["endTimeCode"] = _finite(end, "end")
        cur_tcps = float(stage.GetTimeCodesPerSecond())
        cur_fps = float(stage.GetFramesPerSecond())
        if fps is not None:
            new["framesPerSecond"] = _positive(fps, "fps", maximum=1000.0)
        if time_codes_per_second is not None:
            new["timeCodesPerSecond"] = _positive(time_codes_per_second, "time_codes_per_second", maximum=100000.0)
        elif fps is not None and abs(cur_tcps - cur_fps) <= 1e-9:
            new["timeCodesPerSecond"] = new["framesPerSecond"]
        elif fps is not None:
            warnings.append(
                f"timeCodesPerSecond stays {_fmt(cur_tcps)} because it already differed from framesPerSecond; pass "
                "time_codes_per_second to change it too."
            )
        final_start = new.get("startTimeCode", float(stage.GetStartTimeCode()))
        final_end = new.get("endTimeCode", float(stage.GetEndTimeCode()))
        if final_end < final_start:
            raise _Refusal(f"end ({_fmt(final_end)}) must not be before start ({_fmt(final_start)}).")
        if layer is root and session is not None:
            hidden = [key for key in new if session.pseudoRoot.HasInfo(key)]
            if hidden:
                raise _Refusal(
                    f"The session layer already holds {hidden}, which is stronger than the root layer, so the change "
                    "would be hidden. Set the edit target to the session layer (or clear it there) first."
                )
        if "timeCodesPerSecond" in new and abs(new["timeCodesPerSecond"] - cur_tcps) > 1e-9:
            animated = _count_animated(stage)
            ratio = new["timeCodesPerSecond"] / cur_tcps
            if animated:
                warnings.append(
                    f"{animated} attribute(s) already have time samples. They keep their time codes, so they now play "
                    f"{_fmt(ratio if ratio > 1 else 1 / ratio)}x {'faster' if ratio > 1 else 'slower'} in seconds (a key at time code {_fmt(cur_tcps)} "
                    f"was 1 s and becomes {_fmt(1 / ratio)} s). Sublayers and references with their own "
                    "timeCodesPerSecond are rescaled automatically."
                )
        changes = []
        for key in TIME_KEYS:
            if key not in new:
                continue
            old = _layer_time_value(layer, key)
            changes.append({"field": key, "old": _clean(old) if old is not None else None, "new": _clean(new[key])})
        tcps_after = new.get("timeCodesPerSecond", cur_tcps)
        fps_after = new.get("framesPerSecond", cur_fps)
        summary = {
            "start_time_code": _clean(final_start),
            "end_time_code": _clean(final_end),
            "time_codes_per_second": _clean(tcps_after),
            "frames_per_second": _clean(fps_after),
            "duration_seconds": _clean((final_end - final_start) / tcps_after),
        }
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001 - planning only reads
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    rows = [("field", "old", "new")] + [(c["field"], "-" if c["old"] is None else _fmt(c["old"]), _fmt(c["new"])) for c in changes]
    body = (
        f"Time metadata goes to the {layer_role} ({_text(layer.identifier)}): timeline {_fmt(final_start)} to "
        f"{_fmt(final_end)} = {_fmt(summary['duration_seconds'])} s at {_fmt(tcps_after)} time codes per second, played at "
        f"{_fmt(fps_after)} fps. {TIME_EXPLANATION}"
    )
    extra = {"layer": _text(layer.identifier), "layer_role": layer_role, "changes": changes, "timeline": summary}
    if dry:
        newly = sorted(_dirty_layers(stage) - dirty_before)
        if newly:
            warnings.append(f"Read-only violation: these layers became dirty during the dry run: {newly[:10]}")
        return _finish(action, True, stage, warnings, extra=extra, body=body, rows=rows)

    from .undo import SpecSnapshot  # pure pxr

    snapshot = SpecSnapshot.capture(layer, layer_keys=TIME_KEYS)
    try:
        for key in TIME_KEYS:
            if key in new:
                _write_time_key(layer, key, new[key])
    except Exception as exc:  # noqa: BLE001
        try:
            snapshot.restore(layer)
            return _finish(action, False, stage, warnings, extra=extra, body=body, rows=rows,
                           error=f"Writing the timeline failed ({exc}); the old values were put back.")
        except Exception as restore_exc:  # noqa: BLE001
            return _finish(action, False, stage, warnings, extra=extra, body=body, rows=rows, written=True,
                           error=f"Writing the timeline failed ({exc}) and could not be rolled back ({restore_exc}).")
    readback = {
        "start_time_code": _clean(float(stage.GetStartTimeCode())),
        "end_time_code": _clean(float(stage.GetEndTimeCode())),
        "time_codes_per_second": _clean(float(stage.GetTimeCodesPerSecond())),
        "frames_per_second": _clean(float(stage.GetFramesPerSecond())),
    }
    for key in ("start_time_code", "end_time_code", "time_codes_per_second", "frames_per_second"):
        if readback[key] != summary[key]:
            warnings.append(f"After writing, the stage reports {key} = {readback[key]} instead of {summary[key]}; a stronger opinion wins.")
    extra["readback"] = readback
    return _finish(action, False, stage, warnings, extra=extra, body=body, rows=rows, applied=True, written=True)


# --- writers: keyframes, turntable, move_along, swing -----------------------------------------------------


def add_keyframes(
    stage: Any,
    prim_path: str,
    *,
    translate: Any = None,
    rotate: Any = None,
    scale: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (default) or author time samples on the prim's own translate / rotation / scale ops.

    Each argument is {time_code: value}. translate values are local (parent-space) positions, like the
    xformOp:translate value. rotate values are (rx, ry, rz) degrees, written as-is on a rotateXYZ-style op and
    converted (rotateXYZ order) to a quaternion on an orient op, or one number: degrees turned about the stage up
    axis from the prim's rest rotation. scale values are (sx, sy, sz) or one uniform number. Existing samples at
    other times are kept; a key at the same time is replaced. USD time samples have no per-key ease curves: the
    value is interpolated linearly between keys (add more keys for ease-in/out).
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> _Plan:
        if translate is None and rotate is None and scale is None:
            raise _Refusal("Pass at least one of translate, rotate or scale as {time_code: value}.")
        p = _Plan(ctx, _anim_prim(ctx, prim_path, warnings))
        stack = _stack(p.prim)
        if translate is not None:
            keys = _keys(translate, "translate")
            slot = p.slot("translate", stack)
            values = [(t, _vec3(v, f"translate at time {_fmt(t)}")) for t, v in keys]
            p.add_write(slot, values, replace=False)
        if rotate is not None:
            keys = _keys(rotate, "rotate")
            slot = p.slot("rotate", stack)
            numbers_only = all(_is_number(v) for _, v in keys)
            if numbers_only:
                rest = _rest_rotation(slot)
                times = [t for t, _ in keys]
                angles = [_finite(v, f"rotate at time {_fmt(t)}") for t, v in keys]
                if slot["kind"] == "orient":
                    times, angles, added = _densify(times, angles)
                    if added:
                        p.notes.append(
                            f"{added} in-between key(s) were added: neighboring keys 180 degrees or more apart would make the "
                            "quaternion turn the short way round."
                        )
                values = _about_axis(slot, rest, ctx.up, angles, warnings)
                p.add_write(slot, list(zip(times, values)), replace=False)
                p.notes.append(f"rotate numbers are degrees about the up axis ({ctx.up_letter}) from the rest rotation.")
            elif any(_is_number(v) for _, v in keys):
                raise _Refusal("rotate keys must be all (rx, ry, rz) triples or all single numbers (degrees about the up axis).")
            else:
                eulers = [(t, _vec3(v, f"rotate at time {_fmt(t)}")) for t, v in keys]
                p.add_write(slot, _absolute_euler(slot, eulers, warnings), replace=False)
        if scale is not None:
            keys = _keys(scale, "scale")
            slot = p.slot("scale", stack)
            values = []
            for t, v in keys:
                value = (_finite(v, "scale"),) * 3 if _is_number(v) else _vec3(v, f"scale at time {_fmt(t)}")
                if any(abs(c) < 1e-12 for c in value):
                    warnings.append(f"scale at time {_fmt(t)} has a zero component, so the prim is flattened to nothing there.")
                values.append((t, value))
            p.add_write(slot, values, replace=False)
        p.summary = f"{sum(len(w['values']) for w in p.writes)} key(s) on {p.path}"
        return p

    return _run("add_keyframes", stage, dry_run, plan)


def turntable(
    stage: Any,
    prim_path: str,
    *,
    seconds: Any = 10,
    turns: Any = 1,
    axis: Any = None,
    start: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (default) or author a steady spin: `turns` full turns (negative = the other way) in `seconds`.

    axis is 'x', 'y', 'z' or 'up' in the parent's space (None = the stage up axis); start is the first time
    code (None = the stage start). The spin starts from the prim's rest rotation. Keys are at most 30 degrees
    apart: USD interpolates linearly between them, and for quaternion (orient) ops a step of 180 degrees or more
    would turn the short way round (360 degrees is the same quaternion as 0, so first+last keys alone never turn).
    Existing samples on the rotation op in the edit target are replaced.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> _Plan:
        p = _Plan(ctx, _anim_prim(ctx, prim_path, warnings))
        duration = _positive(seconds, "seconds", maximum=MAX_SECONDS)
        count = _finite(turns, "turns")
        if count == 0 or abs(count) > MAX_TURNS:
            raise _Refusal(f"turns must be non-zero and at most {MAX_TURNS} in size, got {turns!r}")
        k = _axis(axis, ctx.up)
        t0 = ctx.start if start is None else _finite(start, "start")
        total = 360.0 * count
        times = _sample_times(t0, duration * ctx.tcps, abs(total))
        angles = [total * (t - t0) / (times[-1] - t0) for t in times]
        stack = _stack(p.prim)
        slot = p.slot("rotate", stack)
        rest = _rest_rotation(slot)
        values = _about_axis(slot, rest, k, angles, warnings)
        p.add_write(slot, list(zip(times, values)), replace=True)
        step = (times[1] - times[0]) if len(times) > 1 else 0.0
        p.extra.update(
            axis=_AXES[k].upper(),
            turns=_clean(count),
            seconds=_clean(duration),
            degrees_per_second=_clean(total / duration),
            key_every_time_codes=_clean(step),
            key_every_degrees=_clean(abs(total) / (len(times) - 1)),
            first_time_code=_clean(t0),
            last_time_code=_clean(times[-1]),
        )
        p.notes.append(
            f"{len(times)} keys, one every {_fmt(step)} time codes ({_fmt(abs(total) / (len(times) - 1))} degrees apart); "
            "USD interpolates linearly between them, so the speed is constant. Keys stay well under 180 degrees apart "
            "because a quaternion for 360 degrees equals the one for 0 degrees, so a lone 0 to 360 pair would not turn."
        )
        p.notes.append(
            f"For a seamless looping render, render time codes {_fmt(t0)} to {_fmt(times[-1] - 1)}: the last key "
            "looks exactly like the first."
        )
        _timeline_warnings(ctx, t0, times[-1], warnings)
        _warn_animated_ancestors(p.prim.GetParent(), "Ancestor", warnings)
        p.summary = f"{p.path} turns {_fmt(total)} degrees about {_AXES[k].upper()} in {_fmt(duration)} s"
        return p

    return _run("turntable", stage, dry_run, plan)


def move_along(
    stage: Any,
    prim_path: str,
    points: Any,
    *,
    seconds: Any,
    loop: Any = False,
    start: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (default) or author a ride through world-space points at constant speed (a box on a conveyor).

    points are world (x, y, z) positions, in stage units, for the prim's origin. Each point gets one key on the
    translate op, timed by distance so the speed is constant; linear interpolation makes straight segments.
    loop=True closes the path back to the first point. World points are converted to the translate op's local
    value through the parent transform and the ops before translate (rotated / scaled parents, Isaac stacks).
    Existing samples on the translate op in the edit target are replaced.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> _Plan:
        p = _Plan(ctx, _anim_prim(ctx, prim_path, warnings))
        duration = _positive(seconds, "seconds", maximum=MAX_SECONDS)
        if isinstance(points, (str, bytes)) or not isinstance(points, (list, tuple)):
            raise _Refusal("points must be a list of world (x, y, z) positions such as [[0, 0, 0], [5, 0, 0]].")
        path = [_vec3(point, f"points[{index}]") for index, point in enumerate(points)]
        closed = _bool(loop, "loop")
        if closed and len(path) >= 2:
            path.append(path[0])
        if len(path) < 2:
            raise _Refusal("points needs at least two positions.")
        if len(path) > MAX_POINTS:
            raise _Refusal(f"At most {MAX_POINTS} points are supported; got {len(path)}.")
        lengths = [_distance(a, b) for a, b in zip(path, path[1:])]
        scale = max(1.0, *(abs(c) for point in path for c in point))
        for index, length in enumerate(lengths):
            if length <= 1e-12 * scale:
                raise _Refusal(
                    f"points[{index}] and the next point are the same, so that segment would take no time; remove the "
                    "duplicate (a pause needs two keys at the same place with a time gap: use add_keyframes)."
                )
        total = sum(lengths)
        t0 = ctx.start if start is None else _finite(start, "start")
        span = duration * ctx.tcps
        times = [t0]
        run = 0.0
        for length in lengths:
            run += length
            times.append(t0 + span * run / total)
        times[-1] = t0 + span
        _check_increasing(times)
        stack = _stack(p.prim)
        slot = p.slot("translate", stack)
        values = []
        for t, point in zip(times, path):
            values.append((t, _world_to_translate(p.prim, slot, point, t)))
        p.add_write(slot, values, replace=True)
        p.world_targets = list(zip(times, path))
        _warn_animated_ancestors(p.prim.GetParent(), "Ancestor", warnings)
        if _rotation_or_parent_varies(p.prim):
            warnings.append(
                "The prim's rotation or a parent transform is animated too; the positions are exact at each key but "
                "may drift between keys."
            )
        speed = total / duration
        p.extra.update(
            seconds=_clean(duration),
            loop=closed,
            path_length=_clean(total),
            speed_units_per_second=_clean(speed),
            speed_meters_per_second=_clean(speed * ctx.mpu),
            first_time_code=_clean(t0),
            last_time_code=_clean(times[-1]),
            world_points=[[_clean(c) for c in point] for point in path],
        )
        p.notes.append(
            f"{len(path)} keys on the translate op, timed by distance so the speed is a constant "
            f"{_fmt(speed)} units/s ({_fmt(speed * ctx.mpu)} m/s). {_units_sentence(ctx.mpu)}"
        )
        if closed:
            p.notes.append("loop=True closes the path; Kit's timeline loop button repeats the playback.")
        _timeline_warnings(ctx, t0, times[-1], warnings)
        p.summary = f"{p.path} rides {len(path)} points ({_fmt(total)} units) in {_fmt(duration)} s"
        return p

    return _run("move_along", stage, dry_run, plan)


def swing(
    stage: Any,
    prim_path: str,
    *,
    axis: Any,
    angle_deg: Any,
    seconds: Any,
    start: Any = None,
    round_trip: Any = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (default) or author a door swing: turn angle_deg about axis (parent space) in `seconds`.

    The prim turns about its own origin (pivot), starting from its rest rotation. round_trip=True swings back
    to closed over another `seconds`. Existing samples on the rotation op in the edit target are replaced.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> _Plan:
        p = _Plan(ctx, _anim_prim(ctx, prim_path, warnings))
        k = _axis(axis, ctx.up, required=True)
        angle = _finite(angle_deg, "angle_deg")
        if angle == 0 or abs(angle) > 360.0:
            raise _Refusal(f"angle_deg must be non-zero and at most 360 in size, got {angle_deg!r}")
        duration = _positive(seconds, "seconds", maximum=MAX_SECONDS)
        back = _bool(round_trip, "round_trip")
        t0 = ctx.start if start is None else _finite(start, "start")
        span = duration * ctx.tcps
        times = _sample_times(t0, span, abs(angle))
        angles = [angle * (t - t0) / span for t in times]
        if back:
            back_times = _sample_times(t0 + span, span, abs(angle))[1:]
            times += back_times
            angles += [angle * (1.0 - (t - t0 - span) / span) for t in back_times]
            angles[-1] = 0.0
        stack = _stack(p.prim)
        slot = p.slot("rotate", stack)
        rest = _rest_rotation(slot)
        values = _about_axis(slot, rest, k, angles, warnings)
        p.add_write(slot, list(zip(times, values)), replace=True)
        world = UsdGeom.XformCache(Usd.TimeCode(t0)).GetLocalToWorldTransform(p.prim)
        parent = _parent_world(p.prim, t0)
        hinge_axis = [0.0, 0.0, 0.0]
        hinge_axis[k] = 1.0
        direction = parent.TransformDir(Gf.Vec3d(*hinge_axis))
        if direction.GetLength() > 0:
            direction = direction.GetNormalized()
        p.extra.update(
            axis=_AXES[k].upper(),
            angle_deg=_clean(angle),
            seconds=_clean(duration),
            round_trip=back,
            hinge_world_point=[_clean(c) for c in world.ExtractTranslation()],
            hinge_world_direction=[_clean(c) for c in direction],
            first_time_code=_clean(t0),
            last_time_code=_clean(times[-1]),
        )
        p.notes.append(
            f"The door turns about its own origin at world {_point(world.ExtractTranslation())}. If the hinge is "
            "somewhere else, put the door under an Xform placed on the hinge and swing that Xform instead."
        )
        _timeline_warnings(ctx, t0, times[-1], warnings)
        p.summary = f"{p.path} swings {_fmt(angle)} degrees about {_AXES[k].upper()} in {_fmt(duration)} s" + (
            " and back" if back else ""
        )
        return p

    return _run("swing", stage, dry_run, plan)


# --- writer: clear_animation ------------------------------------------------------------------------------


def clear_animation(
    stage: Any, prim_path: str, *, attributes: Any = None, keep_value_at: Any = None, dry_run: bool = True
) -> dict[str, Any]:
    """Plan (default) or remove the time samples the edit target layer holds for a prim's attributes.

    attributes is a list of attribute names (None = every attribute with samples in the edit target).
    keep_value_at is a time code: the value the prim shows there becomes the attribute's default value, so
    the prim stays in that pose. Samples in other layers cannot be cleared from here and are reported.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> _Plan:
        p = _Plan(ctx, _anim_prim(ctx, prim_path, warnings))
        layer = ctx.stage.GetEditTarget().GetLayer()
        keep = None if keep_value_at is None else _finite(keep_value_at, "keep_value_at")
        if attributes is None:
            names = [str(attr.GetName()) for attr in p.prim.GetAttributes()]
            explicit = False
        else:
            raw = [attributes] if isinstance(attributes, str) else attributes
            if not isinstance(raw, (list, tuple)) or not raw or not all(isinstance(n, str) and n.strip() for n in raw):
                raise _Refusal("attributes must be a list of attribute names such as ['xformOp:translate'].")
            names = [n.strip() for n in raw]
            explicit = True
        for name in names:
            attr = p.prim.GetAttribute(name)
            if not attr or not attr.IsValid():
                raise _Refusal(f"{p.path} has no attribute {name!r}.")
            spec_path = ctx.stage.GetEditTarget().MapToSpecPath(attr.GetPath())
            here = [float(t) for t in layer.ListTimeSamplesForPath(spec_path)]
            composed = attr.GetNumTimeSamples()
            spline = getattr(attr, "HasSpline", None)
            if callable(spline):
                try:
                    if spline():
                        warnings.append(f"{name} is animated with a spline, which clear_animation does not remove.")
                except Exception:  # noqa: BLE001
                    pass
            if not here:
                if composed:
                    warnings.append(
                        f"{name} has {composed} time sample(s), but in another layer, not the edit target "
                        f"({layer.GetDisplayName()}); set the edit target to that layer to clear them."
                    )
                elif explicit:
                    warnings.append(f"{name} has no time samples, so there is nothing to clear.")
                continue
            stronger = _stronger_opinion(ctx.stage, p.prim, name)
            if stronger:
                warnings.append(
                    f"{name}: {_text(stronger)} has a stronger opinion, so clearing the edit target does not change what you see."
                )
            default_value = None
            if keep is not None:
                default_value = attr.Get(Usd.TimeCode(keep))
                if default_value is None:
                    raise _Refusal(f"{name} has no value at time {_fmt(keep)} to keep.")
            elif not attr.HasAuthoredValue() or (attr.Get(Usd.TimeCode.Default()) is None and _only_samples_everywhere(ctx.stage, attr, spec_path)):
                if name.startswith("xformOp:"):
                    warnings.append(
                        f"{name} will have no value left after clearing (it only had samples), so the transform op is "
                        "ignored or errors; pass keep_value_at to keep a pose."
                    )
            p.clears.append({"attribute": name, "attr": attr, "spec_path": spec_path, "times": here, "default": default_value})
        p.summary = f"clear {sum(len(c['times']) for c in p.clears)} sample(s) on {len(p.clears)} attribute(s) of {p.path}"
        if keep is not None:
            p.notes.append(f"Each cleared attribute keeps its value at time code {_fmt(keep)} as the default value.")
        p.notes.append(
            "Without samples the default value (or a weaker layer's opinion) shows at every time again: that is value "
            "resolution."
        )
        return p

    return _run("clear_animation", stage, dry_run, plan)


# --- read-only: sample ------------------------------------------------------------------------------------


def sample(stage: Any, prim_path: str, times: Any) -> dict[str, Any]:
    """Read-only: the prim's world position and rotation (and local op values) at each time code."""
    action = "sample"
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        sdf_path = _prim_path(prim_path, "prim_path")
        prim = stage.GetPrimAtPath(sdf_path)
        if not prim or not prim.IsValid():
            raise _Refusal(f"Prim does not exist: {sdf_path}")
        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            raise _Refusal(f"{sdf_path} is not transformable (type {prim.GetTypeName() or 'typeless'}).")
        raw = [times] if _is_number(times) else times
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)) or not raw:
            raise _Refusal("times must be a time code or a list of time codes such as [0, 12, 24].")
        if len(raw) > MAX_TIMES:
            raise _Refusal(f"At most {MAX_TIMES} times can be sampled at once; got {len(raw)}.")
        values = [_finite(t, "times") for t in raw]
        up = str(UsdGeom.GetStageUpAxis(stage)).upper()
        tcps = float(stage.GetTimeCodesPerSecond())
        ops = list(xformable.GetOrderedXformOps())
        rows = []
        for t in values:
            code = Usd.TimeCode(t)
            world = UsdGeom.XformCache(code).GetLocalToWorldTransform(prim)
            rotation = Gf.Matrix4d(world).RemoveScaleShear()
            rows3 = tuple(tuple(rotation[r][c] for c in range(3)) for r in range(3))
            quat = look_at.matrix_to_quaternion(rows3)
            entry = {
                "time": _clean(t),
                "seconds": _clean(t / tcps) if tcps > 0 else None,
                "world_position": [_clean(c) for c in world.ExtractTranslation()],
                "world_rotation_quat": [_clean(c) for c in quat],
                "world_rotation_xyz": [_clean(c) for c in look_at.matrix_to_euler(rows3, "XYZ")],
                "heading_about_up_deg": _heading(rows3, up),
                "ops": {str(op.GetOpName()): _plain(op.Get(code)) for op in ops},
            }
            rows.append(entry)
        animated = [str(op.GetOpName()) for op in ops if op.GetNumTimeSamples() > 0]
        if not animated and not xformable.TransformMightBeTimeVarying():
            warnings.append(f"{sdf_path} has no animated transform ops, so every time shows the same pose (unless a parent moves).")
        result = {
            "ok": True,
            "action": action,
            "read_only": True,
            "prim": str(sdf_path),
            "up_axis": up,
            "time_codes_per_second": _clean(tcps),
            "animated_ops": animated,
            "samples": rows,
            "warnings": _unique(warnings),
            "explanation": (
                f"World pose of {sdf_path} at {len(rows)} time code(s), computed by USD (between keys it interpolates). "
                "heading_about_up_deg is the turn about the up axis (-180..180]. "
                + TIME_EXPLANATION
            ),
        }
        result["preview"] = _table(
            [("time", "seconds", "world position", "heading")]
            + [
                (_fmt(r["time"]), "-" if r["seconds"] is None else _fmt(r["seconds"]), _point(r["world_position"]),
                 "-" if r["heading_about_up_deg"] is None else _fmt(r["heading_about_up_deg"]))
                for r in rows[:MAX_PREVIEW_ROWS]
            ]
        )
        return result
    except _Refusal as refusal:
        return _read_error(action, str(refusal), warnings)
    except Exception as exc:  # noqa: BLE001 - read only
        return _read_error(action, f"Unexpected error while sampling: {exc}", warnings)


# --- plan only: conveyor_plan -----------------------------------------------------------------------------


def conveyor_plan(
    stage: Any = None, prim_path: Any = None, *, speed: Any = 0.5, direction: Any = "+x", boxes: Any = None
) -> dict[str, Any]:
    """PLAN ONLY (never writes): how to move boxes on a conveyor with PhysX surface velocity vs keyframes.

    speed is in meters per second (converted to stage units with metersPerUnit); direction is the belt's local
    axis ('+x', '-y', ...) or a local (x, y, z) vector. With a stage and prim_path, the belt prim is inspected
    read-only (rigid body, collider, PhysxSurfaceVelocityAPI, a PhysicsScene). Returns steps and a Python snippet
    for the Script Editor in Isaac Sim / Composer with PhysX.
    """
    action = "conveyor_plan"
    warnings: list[str] = []
    try:
        mps = _positive(speed, "speed", maximum=1000.0)
        vector = _direction(direction)
        mpu = 1.0
        belt_path = "/World/Conveyor"
        status = None
        if stage is not None:
            _require_pxr()
            _check_stage(stage)
            mpu = float(UsdGeom.GetStageMetersPerUnit(stage)) or 0.01
            if prim_path is not None:
                sdf_path = _prim_path(prim_path, "prim_path")
                prim = stage.GetPrimAtPath(sdf_path)
                if not prim or not prim.IsValid():
                    raise _Refusal(f"Prim does not exist: {sdf_path}")
                belt_path = str(sdf_path)
                status = _conveyor_status(stage, prim, warnings)
        elif prim_path is not None:
            belt_path = str(_prim_path(prim_path, "prim_path")) if _PXR_IMPORT_ERROR is None else str(prim_path)
        units_speed = mps / mpu
        velocity = [_clean(units_speed * c) for c in vector]
        box_paths = []
        if boxes is not None:
            raw = [boxes] if isinstance(boxes, str) else boxes
            if not isinstance(raw, (list, tuple)) or not all(isinstance(b, str) for b in raw):
                raise _Refusal("boxes must be a list of prim paths.")
            box_paths = [str(_prim_path(b, "boxes")) if _PXR_IMPORT_ERROR is None else b for b in raw[:MAX_LISTED]]
        registered = None
        if _PXR_IMPORT_ERROR is None:
            try:
                registered = Usd.SchemaRegistry().FindAppliedAPIPrimDefinition(SURFACE_VELOCITY_API) is not None
            except Exception:  # noqa: BLE001
                registered = None
        if registered is False:
            warnings.append(
                "PhysxSurfaceVelocityAPI is not registered in this Python's USD (plain usd-core has no PhysxSchema); "
                "run the snippet inside Isaac Sim or Composer with PhysX."
            )
    except _Refusal as refusal:
        return _read_error(action, str(refusal), warnings)
    except Exception as exc:  # noqa: BLE001 - read only
        return _read_error(action, f"Unexpected error while planning: {exc}", warnings)

    snippet = _conveyor_snippet(belt_path, velocity, box_paths or ["/World/Box_01"])
    steps = [
        "Make sure the stage has a PhysicsScene (physics.add_physics_scene).",
        f"Belt {belt_path}: apply UsdPhysics.RigidBodyAPI with physics:kinematicEnabled = True (the belt itself never moves) "
        "and UsdPhysics.CollisionAPI (a Mesh belt also needs a convexHull or boundingCube approximation).",
        f"Apply PhysxSchema.PhysxSurfaceVelocityAPI to the belt and set physxSurfaceVelocity:surfaceVelocity = {velocity} "
        f"(stage units per second = {_fmt(mps)} m/s along local {_direction_text(vector)}); keep surfaceVelocityLocalSpace = True "
        "so the belt pushes along its own axis even if it is turned.",
        "Each box: apply UsdPhysics.RigidBodyAPI (dynamic, not kinematic) and UsdPhysics.CollisionAPI, then put it on the belt.",
        "Press Play: PhysX adds the belt speed to every contact, so boxes ride, collide and pile up. Nothing moves while "
        "stopped, and the motion is not saved as time samples.",
    ]
    comparison = [
        {"way": "physics (surface velocity)", "good_for": "Isaac Sim digital twins: real contacts, many boxes, boxes falling off the end",
         "limits": "needs Play and PhysX; results vary slightly run to run; not visible in plain USD viewers or offline renders"},
        {"way": "keyframes (move_along)", "good_for": "product renders and fixed demos: exact, repeatable, plays in any USD viewer",
         "limits": "no collisions; every box needs its own keys; the belt texture does not move by itself"},
    ]
    explanation = (
        "Plan only, nothing was written. The physics way gives the belt a surface velocity: PhysX's contact modify "
        "callback pushes anything touching it, while the belt (a kinematic rigid body) stays put. The keyframe way "
        f"(move_along) stores positions as time samples. {_units_sentence(mpu)} The schema facts below were read from "
        "the installed Isaac Sim 6.1 PhysxSchema (see schema_source)."
    )
    result: dict[str, Any] = {
        "ok": True,
        "action": action,
        "read_only": True,
        "plan_only": True,
        "written": False,
        "belt": belt_path,
        "speed_meters_per_second": _clean(mps),
        "surface_velocity": velocity,
        "meters_per_unit": _clean(mpu),
        "schema": {
            "api": SURFACE_VELOCITY_API,
            "python": "PhysxSchema.PhysxSurfaceVelocityAPI.Apply(prim)",
            "attributes": SURFACE_VELOCITY_ATTRIBUTES,
            "requires": "UsdPhysics.RigidBodyAPI on the same prim (schema doc: 'It must be applied to a prim with UsdPhysicsRigidBodyAPI')",
            "schema_source": PHYSX_SCHEMA_SOURCE,
            "schema_source_line": 661,
            "schema_doc_source": PHYSX_SCHEMA_DOC,
            "example_source": PHYSX_DEMO_SOURCE,
            "python_api_stub": PHYSX_PYTHON_STUB,
            "isaac_conveyor_tool": ISAAC_CONVEYOR_SOURCE,
            "registered_in_this_python": registered,
        },
        "belt_status": status,
        "steps": steps,
        "comparison": comparison,
        "python_snippet": snippet,
        "keyframe_alternative": (
            f"animation.move_along(stage, '/World/Box_01', [start_point, end_point], seconds=belt_length_m / {_fmt(mps)})"
        ),
        "warnings": _unique(warnings),
        "explanation": explanation,
    }
    result["preview"] = _table([("way", "good for")] + [(row["way"], row["good_for"]) for row in comparison])
    return result


# --- planning machinery -----------------------------------------------------------------------------------


class _Ctx:
    """Stage facts used by one call."""

    def __init__(self, stage: Any) -> None:
        self.stage = stage
        up = str(UsdGeom.GetStageUpAxis(stage)).upper()
        if up not in ("Y", "Z"):
            raise _Refusal(f"The stage up axis {up!r} is not Y or Z.")
        self.up_letter = up
        self.up = _AXES.index(up.lower())
        self.mpu = float(UsdGeom.GetStageMetersPerUnit(stage)) or 0.01
        self.tcps = float(stage.GetTimeCodesPerSecond())
        if not math.isfinite(self.tcps) or self.tcps <= 0:
            raise _Refusal(f"timeCodesPerSecond is {self.tcps}; it must be positive.")
        self.fps = float(stage.GetFramesPerSecond())
        self.start = float(stage.GetStartTimeCode())
        self.end = float(stage.GetEndTimeCode())
        self.authored_range = bool(stage.HasAuthoredTimeCodeRange())


class _Plan:
    """What one writer call will author on one prim."""

    def __init__(self, ctx: _Ctx, prim: Any) -> None:
        self.ctx = ctx
        self.prim = prim
        self.path = str(prim.GetPath())
        self.writes: list[dict[str, Any]] = []
        self.clears: list[dict[str, Any]] = []
        self.new_ops: list[dict[str, Any]] = []  # slots whose op is added to xformOpOrder
        self.order: list[tuple[str, bool]] | None = None  # (op name, inverse) after adding ops
        self.notes: list[str] = []
        self.extra: dict[str, Any] = {}
        self.world_targets: list[tuple[float, tuple[float, float, float]]] = []
        self.summary = ""

    def slot(self, part: str, stack: dict[str, Any]) -> dict[str, Any]:
        slot = _slot(self.prim, stack, part)
        if slot["new"]:
            self.new_ops.append(slot)
            self.order = _order_with_new_ops(stack, self.new_ops)
        return slot

    def add_write(self, slot: dict[str, Any], values: list[tuple[float, Any]], *, replace: bool) -> None:
        if len(values) > MAX_SAMPLES:
            raise _Refusal(f"At most {MAX_SAMPLES} keys can be written at once; got {len(values)}.")
        typed = [(float(t), _typed(slot, v)) for t, v in values]
        attr = self.prim.GetAttribute(slot["attribute"])
        layer = self.ctx.stage.GetEditTarget().GetLayer()
        existing = 0
        if attr and attr.IsValid():
            existing = len(layer.ListTimeSamplesForPath(self.ctx.stage.GetEditTarget().MapToSpecPath(attr.GetPath())))
        times = {t for t, _ in typed}
        if replace and existing:
            self.notes.append(f"The {existing} existing sample(s) on {slot['attribute']} in the edit target are replaced.")
        elif existing:
            overlap = len(times & {float(t) for t in layer.ListTimeSamplesForPath(self.ctx.stage.GetEditTarget().MapToSpecPath(attr.GetPath()))})
            kept = existing - overlap
            if kept:
                self.notes.append(f"{kept} existing sample(s) on {slot['attribute']} at other times are kept.")
        default = _typed(slot, _identity_value(slot)) if slot["new"] else None
        self.writes.append({"slot": slot, "attribute": slot["attribute"], "values": typed, "replace": replace,
                            "existing": existing, "default": default})


def _anim_prim(ctx: _Ctx, path: Any, warnings: list[str]) -> Any:
    sdf_path = _prim_path(path, "prim_path")
    prim = ctx.stage.GetPrimAtPath(sdf_path)
    if not prim or not prim.IsValid():
        raise _Refusal(f"Prim does not exist: {sdf_path}")
    if prim.IsInstanceProxy() or prim.IsInPrototype():
        raise _Refusal(
            f"{sdf_path} is inside an instance (an instance proxy), so its attributes cannot be edited here; animate the "
            "instance prim itself (an ancestor) instead."
        )
    if not UsdGeom.Xformable(prim):
        raise _Refusal(f"{sdf_path} is not transformable (type {prim.GetTypeName() or 'typeless'}), so it cannot be animated.")
    if not prim.IsActive():
        warnings.append(f"{sdf_path} is inactive, so the animation has no visible effect until it is activated.")
    return prim


_ROT3 = None  # filled lazily: {XformOp type: order}
_SINGLE = None


def _rotation_types() -> tuple[dict[Any, str], dict[Any, int]]:
    global _ROT3, _SINGLE
    if _ROT3 is None:
        _ROT3 = {getattr(UsdGeom.XformOp, "TypeRotate" + order): order for order in look_at.ROTATION_ORDERS}
        _SINGLE = {UsdGeom.XformOp.TypeRotateX: 0, UsdGeom.XformOp.TypeRotateY: 1, UsdGeom.XformOp.TypeRotateZ: 2}
    return _ROT3, _SINGLE  # type: ignore[return-value]


def _stack(prim: Any) -> dict[str, Any]:
    xformable = UsdGeom.Xformable(prim)
    ops = list(xformable.GetOrderedXformOps())
    if any(op.GetOpType() == UsdGeom.XformOp.TypeTransform for op in ops):
        raise _Refusal(
            f"{prim.GetPath()} uses a matrix op (xformOp:transform); its translate, rotation and scale cannot be keyed "
            "separately. Animate a parent Xform, or rebuild the prim with translate/rotate/scale ops."
        )
    order_attr = xformable.GetXformOpOrderAttr()
    raw = [str(token) for token in (order_attr.Get() or [])] if order_attr else []
    if RESET_TOKEN in raw[1:]:
        raise _Refusal(f"{prim.GetPath()} has !resetXformStack! in the middle of xformOpOrder; edit it by hand.")
    return {"xformable": xformable, "ops": ops, "reset": xformable.GetResetXformStack(), "raw_order": raw}


def _slot(prim: Any, stack: dict[str, Any], part: str) -> dict[str, Any]:
    """Which op holds this part: an existing op (kept as it is) or a new standard op."""
    ops = stack["ops"]
    rot3, single = _rotation_types()
    if part == "translate":
        slot = _translate_slot(prim, stack["xformable"])  # layout helper: plain translate op or a new first one
        if slot["kind"] == "transform_matrix":  # pragma: no cover - refused in _stack already
            raise _Refusal(f"{prim.GetPath()} uses a matrix op.")
        op = slot["op"]
        index = ops.index(op) if op is not None else None
        return {"part": "translate", "op": op, "index": index, "attribute": TRANSLATE, "kind": "vec3",
                "precision": slot["precision"], "new": op is None, "type_name": _vec3_type(slot["precision"])}
    if part == "rotate":
        rotations = [op for op in ops if op.GetOpType() in rot3 or op.GetOpType() in single or op.GetOpType() == UsdGeom.XformOp.TypeOrient]
        plain = [op for op in rotations if not op.IsInverseOp()]
        if len(plain) > 1:
            raise _Refusal(
                f"{prim.GetPath()} has more than one rotation op ({[str(op.GetOpName()) for op in plain]}), so it is not clear "
                "which one to key. Animate a parent Xform instead."
            )
        if any(op.IsInverseOp() for op in rotations):
            raise _Refusal(f"{prim.GetPath()} uses a rotation op inverted (!invert!); edit it by hand.")
        if plain:
            op = plain[0]
            kind_type = op.GetOpType()
            precision = _precision_name(op.GetPrecision())
            base = {"part": "rotate", "op": op, "index": ops.index(op), "attribute": str(op.GetName()),
                    "precision": precision, "new": False}
            if kind_type == UsdGeom.XformOp.TypeOrient:
                return {**base, "kind": "orient", "type_name": _quat_type(precision)}
            if kind_type in single:
                return {**base, "kind": "single", "axis": single[kind_type], "type_name": _scalar_type(precision)}
            return {**base, "kind": "euler", "order": rot3[kind_type], "type_name": _vec3_type(precision)}
        precision = _reuse_precision(prim, ROTATE_XYZ, UsdGeom.XformOp.TypeRotateXYZ, "float")
        return {"part": "rotate", "op": None, "index": None, "attribute": ROTATE_XYZ, "kind": "euler", "order": "XYZ",
                "precision": precision, "new": True, "type_name": _vec3_type(precision)}
    scales = [op for op in ops if op.GetOpType() == UsdGeom.XformOp.TypeScale]
    if any(op.IsInverseOp() for op in scales):
        raise _Refusal(f"{prim.GetPath()} uses a scale op inverted (!invert!); edit it by hand.")
    if len(scales) > 1:
        raise _Refusal(f"{prim.GetPath()} has more than one scale op ({[str(op.GetOpName()) for op in scales]}); edit it by hand.")
    if scales:
        op = scales[0]
        precision = _precision_name(op.GetPrecision())
        return {"part": "scale", "op": op, "index": ops.index(op), "attribute": str(op.GetName()), "kind": "vec3",
                "precision": precision, "new": False, "type_name": _vec3_type(precision)}
    precision = _reuse_precision(prim, SCALE, UsdGeom.XformOp.TypeScale, "float")
    return {"part": "scale", "op": None, "index": None, "attribute": SCALE, "kind": "vec3", "precision": precision,
            "new": True, "type_name": _vec3_type(precision)}


def _reuse_precision(prim: Any, name: str, op_type: Any, fallback: str) -> str:
    """An attribute with the new op's name may exist outside xformOpOrder; reuse it with its own type."""
    attr = prim.GetAttribute(name)
    if not attr or not attr.IsValid():
        return fallback
    stale = UsdGeom.XformOp(attr)
    if not stale or stale.GetOpType() != op_type:
        raise _Refusal(f"{prim.GetPath()} has a {name} attribute of an unexpected type, so it cannot be reused as an op.")
    return _precision_name(stale.GetPrecision())


def _order_with_new_ops(stack: dict[str, Any], new_slots: list[dict[str, Any]]) -> list[tuple[str, bool]]:
    """Existing ops keep their relative order; translate goes first, rotateXYZ before the first scale/inverse op, scale last."""
    order = [(str(op.GetName()), op.IsInverseOp(), op.GetOpType()) for op in stack["ops"]]
    parts = {slot["part"] for slot in new_slots}
    if "translate" in parts:
        order.insert(0, (TRANSLATE, False, UsdGeom.XformOp.TypeTranslate))
    if "rotate" in parts:
        position = next(
            (i for i, (_, inverse, kind) in enumerate(order) if inverse or kind == UsdGeom.XformOp.TypeScale), len(order)
        )
        order.insert(position, (ROTATE_XYZ, False, UsdGeom.XformOp.TypeRotateXYZ))
    if "scale" in parts:
        order.append((SCALE, False, UsdGeom.XformOp.TypeScale))
    return [(name, inverse) for name, inverse, _ in order]


def _rest_rotation(slot: dict[str, Any]) -> Any:
    """The rotation the animation starts from: the op's default value, else its earliest sample, else none."""
    op = slot["op"]
    value = None
    if op is not None:
        attr = op.GetAttr()
        value = attr.Get(Usd.TimeCode.Default())
        if value is None and attr.GetNumTimeSamples() > 0:
            value = attr.Get(Usd.TimeCode(attr.GetTimeSamples()[0]))
    if value is None:
        return _identity_value(slot)
    if slot["kind"] == "orient":
        return (float(value.GetReal()), *(float(c) for c in value.GetImaginary()))
    if slot["kind"] == "single":
        return float(value)
    return tuple(float(c) for c in value)


def _identity_value(slot: dict[str, Any]) -> Any:
    if slot["part"] == "scale":
        return (1.0, 1.0, 1.0)
    if slot["kind"] == "orient":
        return (1.0, 0.0, 0.0, 0.0)
    if slot["kind"] == "single":
        return 0.0
    return (0.0, 0.0, 0.0)


def _about_axis(slot: dict[str, Any], rest: Any, axis: int, angles: list[float], warnings: list[str]) -> list[Any]:
    """Rotation op values that turn the rest rotation by each angle about the parent-space axis (applied last)."""
    kind = slot["kind"]
    letter = _AXES[axis].upper()
    if kind == "single":
        if slot["axis"] != axis:
            raise _Refusal(
                f"The only rotation op is {slot['attribute']}, which turns about {_AXES[slot['axis']].upper()}, not {letter}. "
                "Animate a parent Xform about that axis instead."
            )
        return [rest + a for a in angles]
    if kind == "euler":
        order = slot["order"]
        later = order[order.index(letter) + 1:]
        blocking = [name for name in later if abs(rest["XYZ".index(name)]) > 1e-9]
        if blocking:
            raise _Refusal(
                f"{slot['attribute']} = {[_clean(c) for c in rest]} turns about {', '.join(blocking)} after {letter}, so a "
                f"turn about the parent {letter} axis is not one number on this op. Put the prim under a new Xform and "
                "animate that Xform."
            )
        values = []
        for a in angles:
            value = list(rest)
            value[axis] += a
            values.append(tuple(value))
        return values
    rest_rows = look_at.quaternion_to_matrix(rest)
    values = []
    previous = None
    for a in angles:
        turn = [0.0, 0.0, 0.0]
        turn[axis] = a
        rows = _matmul(rest_rows, look_at.euler_to_matrix(turn, "XYZ"))
        quat = look_at.matrix_to_quaternion(rows)
        if previous is not None and sum(p * q for p, q in zip(previous, quat)) < 0:
            quat = tuple(-c for c in quat)  # same rotation; keeps neighboring keys on the same side for slerp
        values.append(quat)
        previous = quat
    _check_slerp_steps([t for t in values], warnings)
    return values


def _absolute_euler(slot: dict[str, Any], keys: list[tuple[float, tuple[float, float, float]]], warnings: list[str]) -> list[tuple[float, Any]]:
    kind = slot["kind"]
    if kind == "euler":
        if slot["order"] != "XYZ":
            warnings.append(
                f"{slot['attribute']} turns in {slot['order']} order: the (rx, ry, rz) values are written as they are and "
                f"applied {', then '.join(slot['order'])}."
            )
        return keys
    if kind == "single":
        axis = slot["axis"]
        for t, value in keys:
            if any(abs(value[i]) > 1e-12 for i in range(3) if i != axis):
                raise _Refusal(
                    f"The only rotation op is {slot['attribute']} (about {_AXES[axis].upper()}), but the key at time {_fmt(t)} "
                    f"also turns about other axes: {list(value)}."
                )
        return [(t, value[axis]) for t, value in keys]
    for (_, first), (_, second) in zip(keys, keys[1:]):
        if max(abs(a - b) for a, b in zip(first, second)) >= 180.0 - 1e-9:
            warnings.append(
                "Two neighboring rotate keys differ by 180 degrees or more on one axis; an orient (quaternion) op turns the "
                "shortest way round, so the turn may go the other way. Add keys in between, or pass numbers (degrees about "
                "the up axis), which get in-between keys automatically."
            )
            break
    out = []
    previous = None
    for t, value in keys:
        quat = look_at.matrix_to_quaternion(look_at.euler_to_matrix(value, "XYZ"))
        if previous is not None and sum(p * q for p, q in zip(previous, quat)) < 0:
            quat = tuple(-c for c in quat)
        out.append((t, quat))
        previous = quat
    _check_slerp_steps([q for _, q in out], warnings)
    warnings.append(f"{slot['attribute']} is a quaternion op: the (rx, ry, rz) keys were converted with rotateXYZ order.")
    return out


def _check_slerp_steps(quats: list[Any], warnings: list[str]) -> None:
    for first, second in zip(quats, quats[1:]):
        dot = min(1.0, abs(sum(a * b for a, b in zip(first, second))))
        if math.degrees(2.0 * math.acos(dot)) >= 179.0:
            warnings.append(
                "Two neighboring orient keys are about 180 degrees or more apart; quaternions interpolate the shortest way "
                "round, so the turn may go the other way. Add keys in between."
            )
            return


def _densify(times: list[float], angles: list[float]) -> tuple[list[float], list[float], int]:
    """Split segments turning 180 degrees or more into steps of at most MAX_STEP_DEGREES (same motion, linear in angle)."""
    out_times, out_angles = [times[0]], [angles[0]]
    added = 0
    for (t0, a0), (t1, a1) in zip(zip(times, angles), zip(times[1:], angles[1:])):
        pieces = 1
        if abs(a1 - a0) >= 180.0 - 1e-9:
            pieces = math.ceil(abs(a1 - a0) / MAX_STEP_DEGREES - 1e-9)
        for i in range(1, pieces):
            out_times.append(t0 + (t1 - t0) * i / pieces)
            out_angles.append(a0 + (a1 - a0) * i / pieces)
            added += 1
        out_times.append(t1)
        out_angles.append(a1)
    return out_times, out_angles, added


def _keys(value: Any, name: str) -> list[tuple[float, Any]]:
    """{time: value} (keys may be numeric strings, as in JSON) or [[time, value], ...], sorted by time."""
    if isinstance(value, dict):
        items = list(value.items())
    elif isinstance(value, (list, tuple)) and value and all(isinstance(i, (list, tuple)) and len(i) == 2 for i in value):
        items = [(i[0], i[1]) for i in value]
    else:
        raise _Refusal(f"{name} must be {{time_code: value}}, such as {{0: [0, 0, 0], 24: [1, 0, 0]}}.")
    if not items:
        raise _Refusal(f"{name} has no keys.")
    if len(items) > MAX_SAMPLES:
        raise _Refusal(f"At most {MAX_SAMPLES} keys are supported; got {len(items)}.")
    keys = []
    for time_value, item in items:
        if isinstance(time_value, str):
            try:
                time_value = float(time_value.strip())
            except ValueError:
                raise _Refusal(f"{name} key {time_value!r} is not a time code number.") from None
        t = _finite(time_value, f"{name} time code")
        keys.append((t, item))
    keys.sort(key=lambda pair: pair[0])
    for (a, _), (b, _) in zip(keys, keys[1:]):
        if a == b:
            raise _Refusal(f"{name} has two keys at time {_fmt(a)}.")
    return keys


def _sample_times(t0: float, span: float, degrees: float) -> list[float]:
    """Evenly spaced key times, at most MAX_STEP_DEGREES apart, on whole time codes when the span allows."""
    if span <= 0:
        raise _Refusal("The animation must last more than zero time codes.")
    segments = max(1, math.ceil(degrees / MAX_STEP_DEGREES - 1e-9))
    whole = abs(span - round(span)) < 1e-9 and round(span) <= 1_000_000
    if whole:
        total = int(round(span))
        for candidate in range(segments, min(total, segments * 4) + 1):
            if total % candidate == 0:
                segments = candidate
                break
    if segments + 1 > MAX_SAMPLES:
        raise _Refusal(f"That needs {segments + 1} keys; at most {MAX_SAMPLES} are supported. Use fewer turns.")
    times = [t0 + span * i / segments for i in range(segments + 1)]
    times[-1] = t0 + span
    return times


def _world_to_translate(prim: Any, slot: dict[str, Any], point: Any, t: float) -> tuple[float, float, float]:
    """Local translate value that puts the prim's origin at a world point at time t (layout's delta method)."""
    code = Usd.TimeCode(t)
    xformable = UsdGeom.Xformable(prim)
    parent = Gf.Matrix4d(1.0) if xformable.GetResetXformStack() else _parent_world(prim, t)
    after = Gf.Matrix4d(1.0)
    op = slot["op"]
    if op is not None:
        ops = list(xformable.GetOrderedXformOps())
        for outer in ops[: slot["index"]]:  # ops listed before translate are applied after it
            after = outer.GetOpTransform(code) * after
        current = op.Get(code)
        current = Gf.Vec3d(*(current if current is not None else (0.0, 0.0, 0.0)))
    else:
        current = Gf.Vec3d(0.0, 0.0, 0.0)
    to_world = after * parent
    determinant = to_world.GetDeterminant()
    if not math.isfinite(determinant) or abs(determinant) < _SINGULAR:
        raise _Refusal(f"{prim.GetPath()} sits under a zero scale, so world points cannot be converted to its translate.")
    origin = UsdGeom.XformCache(code).GetLocalToWorldTransform(prim).ExtractTranslation()
    local = current + to_world.GetInverse().TransformDir(Gf.Vec3d(*point) - origin)
    return (float(local[0]), float(local[1]), float(local[2]))


def _parent_world(prim: Any, t: float) -> Any:
    parent = prim.GetParent()
    if not parent or parent.IsPseudoRoot():
        return Gf.Matrix4d(1.0)
    return UsdGeom.XformCache(Usd.TimeCode(t)).GetLocalToWorldTransform(parent)


def _rotation_or_parent_varies(prim: Any) -> bool:
    xformable = UsdGeom.Xformable(prim)
    for op in xformable.GetOrderedXformOps():
        if op.GetOpType() != UsdGeom.XformOp.TypeTranslate and op.GetNumTimeSamples() > 0:
            return True
    current = prim.GetParent()
    while current and current.IsValid() and not current.IsPseudoRoot():
        parent_xf = UsdGeom.Xformable(current)
        if parent_xf and parent_xf.TransformMightBeTimeVarying():
            return True
        current = current.GetParent()
    return False


def _timeline_warnings(ctx: _Ctx, first: float, last: float, warnings: list[str]) -> None:
    if not ctx.authored_range:
        warnings.append(
            f"The stage has no authored start/end time codes, so a player shows no range; call set_timeline(start="
            f"{_fmt(first)}, end={_fmt(last)}) to see the animation play."
        )
    elif first < ctx.start or last > ctx.end:
        warnings.append(
            f"The animation runs from {_fmt(first)} to {_fmt(last)}, outside the timeline {_fmt(ctx.start)} to "
            f"{_fmt(ctx.end)}; call set_timeline(start={_fmt(min(first, ctx.start))}, end={_fmt(max(last, ctx.end))})."
        )


def _check_stronger(plan: _Plan) -> None:
    names = [w["attribute"] for w in plan.writes] + (["xformOpOrder"] if plan.order is not None else [])
    for name in names:
        stronger = _stronger_opinion(plan.ctx.stage, plan.prim, name)
        if stronger:
            raise _Refusal(
                f"{plan.path}.{name} has a stronger opinion in {_text(stronger)} than the edit target, so these keys would "
                "be hidden by it (the strongest layer wins, even over time samples in a weaker layer). Set the edit target "
                "to that layer, or clear the opinion there."
            )


# --- the shared run / apply pipeline ----------------------------------------------------------------------


def _run(action: str, stage: Any, dry_run: Any, planner: Any) -> dict[str, Any]:
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        dirty_before = _dirty_layers(stage)
        _check_edit_target(stage, warnings)
        ctx = _Ctx(stage)
        plan = planner(ctx, warnings)
        _check_stronger(plan)
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001 - planning only reads the stage
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    extra, body, rows = _describe(plan)
    if dry:
        newly = sorted(_dirty_layers(stage) - dirty_before)
        if newly:  # never expected: planning uses only read APIs
            warnings.append(f"Read-only violation: these layers became dirty during the dry run: {newly[:10]}")
        return _finish(action, True, stage, warnings, extra=extra, body=body, rows=rows)
    if not plan.writes and not plan.clears:
        return _finish(action, False, stage, warnings, extra=extra, body=body, rows=rows, nothing=True)
    error = _apply(stage, plan)
    if error is not None:
        message, written = error
        return _finish(action, False, stage, warnings, extra=extra, body=body, rows=rows, error=message, written=written)
    extra["readback"] = _readback(plan, warnings)
    return _finish(action, False, stage, warnings, extra=extra, body=body, rows=rows, applied=True, written=True)


def _apply(stage: Any, plan: _Plan) -> tuple[str, bool] | None:
    """Author the plan; on failure put the edit target's spec for the prim back. (message, written) on failure."""
    from .undo import SpecSnapshot  # pure pxr

    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    try:
        snapshot = SpecSnapshot.capture(layer, shallow=[edit_target.MapToSpecPath(plan.prim.GetPath())])
    except Exception:  # noqa: BLE001 - e.g. a variant edit target; the writes still run
        snapshot = None
    step = "starting"
    try:
        for clear in plan.clears:
            step = clear["attribute"]
            _write_clear(layer, clear)
        for write in plan.writes:
            step = write["attribute"]
            _write_one(plan.prim, write, layer, edit_target)
        if plan.order is not None:
            step = "xformOpOrder"
            _write_order(plan.prim, plan.order)
    except Exception as exc:  # noqa: BLE001
        if snapshot is not None and snapshot.matches(layer):
            return f"Writing failed before anything changed ({step}): {exc}", False
        if snapshot is not None:
            try:
                snapshot.restore(layer)
                return f"Writing failed at {step} ({exc}); everything already written was put back.", False
            except Exception as restore_exc:  # noqa: BLE001
                return f"Writing failed at {step} ({exc}) and could not be rolled back ({restore_exc}).", True
        return f"Writing failed at {step} ({exc}); the edit target may hold part of the animation.", True
    return None


def _write_one(prim: Any, write: dict[str, Any], layer: Any, edit_target: Any) -> None:
    slot = write["slot"]
    attr = prim.GetAttribute(write["attribute"])
    if not attr or not attr.IsValid():
        attr = prim.CreateAttribute(write["attribute"], slot["type_name"], False)
        if not attr:
            raise ValueError(f"could not create {write['attribute']}")
    if write["replace"]:
        spec_path = edit_target.MapToSpecPath(attr.GetPath())
        for t in list(layer.ListTimeSamplesForPath(spec_path)):
            layer.EraseTimeSample(spec_path, t)
    if write["default"] is not None and attr.Get(Usd.TimeCode.Default()) is None:
        if not attr.Set(write["default"]):
            raise ValueError(f"could not set the default value of {write['attribute']}")
    for t, value in write["values"]:
        if not attr.Set(value, Usd.TimeCode(t)):
            raise ValueError(f"could not set {write['attribute']} at time {t}")


def _write_order(prim: Any, order: list[tuple[str, bool]]) -> None:
    xformable = UsdGeom.Xformable(prim)
    ops = []
    for name, inverse in order:
        op = UsdGeom.XformOp(prim.GetAttribute(name), inverse)
        if not op:
            raise ValueError(f"{name} is not a valid xform op")
        ops.append(op)
    if not xformable.SetXformOpOrder(ops, xformable.GetResetXformStack()):
        raise ValueError("could not write xformOpOrder")


def _write_clear(layer: Any, clear: dict[str, Any]) -> None:
    spec_path = clear["spec_path"]
    for t in list(layer.ListTimeSamplesForPath(spec_path)):
        layer.EraseTimeSample(spec_path, t)
    if clear["default"] is not None:
        if not clear["attr"].Set(clear["default"]):
            raise ValueError(f"could not keep the value of {clear['attribute']} as its default")


def _readback(plan: _Plan, warnings: list[str]) -> dict[str, Any]:
    """Compare what the stage now resolves with what was written (a stronger opinion would differ)."""
    worst = 0.0
    for write in plan.writes:
        attr = plan.prim.GetAttribute(write["attribute"])
        for t, value in write["values"]:
            got = attr.Get(Usd.TimeCode(t))
            error = _value_error(got, value)
            worst = max(worst, error)
            if error > _tolerance(write["slot"]["precision"], value):
                warnings.append(
                    f"{write['attribute']} at time {_fmt(t)} resolves to {_plain(got)} instead of {_plain(value)}: a stronger "
                    "opinion or another layer wins."
                )
                break
    world_error = None
    if plan.world_targets:
        world_error = 0.0
        for t, point in plan.world_targets:
            got = UsdGeom.XformCache(Usd.TimeCode(t)).GetLocalToWorldTransform(plan.prim).ExtractTranslation()
            world_error = max(world_error, max(abs(float(got[i]) - point[i]) for i in range(3)))
    for clear in plan.clears:
        if plan.ctx.stage.GetEditTarget().GetLayer().ListTimeSamplesForPath(clear["spec_path"]):
            warnings.append(f"{clear['attribute']} still has samples in the edit target after clearing.")
    return {"max_value_error": _clean(worst), "max_world_position_error": _clean(world_error) if world_error is not None else None}


# --- describing results -----------------------------------------------------------------------------------


def _describe(plan: _Plan) -> tuple[dict[str, Any], str, list[tuple[str, ...]]]:
    ctx = plan.ctx
    writes = []
    rows: list[tuple[str, ...]] = [("time", "seconds", "attribute", "value")]
    for write in plan.writes:
        slot = write["slot"]
        samples = [
            {"time": _clean(t), "seconds": _clean(t / ctx.tcps), "value": _plain(value)}
            for t, value in write["values"][:MAX_LISTED_SAMPLES]
        ]
        writes.append({
            "attribute": write["attribute"],
            "type": str(slot["type_name"]),
            "op_kind": slot["kind"],
            "precision": slot["precision"],
            "op_added": slot["new"],
            "replaces_existing_samples": write["existing"] if write["replace"] else 0,
            "existing_samples_in_edit_target": write["existing"],
            "sample_count": len(write["values"]),
            "samples": samples,
            "samples_truncated": max(0, len(write["values"]) - len(samples)),
            "default_added": _plain(write["default"]) if write["default"] is not None else None,
        })
        for t, value in write["values"]:
            rows.append((_fmt(t), _fmt(t / ctx.tcps), write["attribute"], _value_text(value)))
    clears = [
        {"attribute": c["attribute"], "samples_removed": len(c["times"]),
         "times": [_clean(t) for t in c["times"][:MAX_LISTED_SAMPLES]],
         "default_kept": _plain(c["default"]) if c["default"] is not None else None}
        for c in plan.clears
    ]
    for c in plan.clears:
        rows.append(("-", "-", c["attribute"], f"remove {len(c['times'])} sample(s)" + (
            f", default = {_value_text(c['default'])}" if c["default"] is not None else "")))
    if len(rows) > MAX_PREVIEW_ROWS + 1:
        hidden = len(rows) - MAX_PREVIEW_ROWS - 1
        rows = rows[: MAX_PREVIEW_ROWS + 1] + [(f"... and {hidden} more",)]
    extra: dict[str, Any] = {
        "prim": plan.path,
        "up_axis": ctx.up_letter,
        "meters_per_unit": _clean(ctx.mpu),
        "time_codes_per_second": _clean(ctx.tcps),
        "frames_per_second": _clean(ctx.fps),
        "interpolation": _interpolation_name(ctx.stage),
        "writes": writes,
        "clears": clears,
        "xform_op_order_before": [str(op.GetOpName()) for op in UsdGeom.Xformable(plan.prim).GetOrderedXformOps()],
        "xform_op_order": [("!invert!" if inverse else "") + name for name, inverse in plan.order] if plan.order is not None
        else [str(op.GetOpName()) for op in UsdGeom.Xformable(plan.prim).GetOrderedXformOps()],
        **plan.extra,
    }
    added = [w["attribute"] for w in plan.writes if w["slot"]["new"]]
    parts = [f"Plan: {plan.summary}."]
    if added:
        parts.append(
            f"New op(s) {added} are added in the standard place (other ops keep their order), with a default value that "
            "keeps today's pose."
        )
    parts.extend(plan.notes)
    parts.append(f"{TIME_EXPLANATION} {RESOLUTION_EXPLANATION}")
    return extra, " ".join(parts), rows


def _finish(
    action: str,
    dry: bool,
    stage: Any,
    warnings: list[str],
    *,
    extra: dict[str, Any] | None = None,
    body: str = "",
    rows: list[tuple[str, ...]] | None = None,
    error: str | None = None,
    applied: bool = False,
    written: bool = False,
    nothing: bool = False,
) -> dict[str, Any]:
    info = _edit_target_info(stage) if stage is not None and _PXR_IMPORT_ERROR is None else None
    if error is not None:
        explanation = f"Nothing was written: {error}" if not written else f"The change was only partly written: {error}"
    elif dry:
        explanation = f"Dry run: nothing was written. {body} Call again with dry_run=False to author it."
    elif applied:
        layer = info["display_name"] if info else "the edit target"
        explanation = f"Written to {layer if action != 'set_timeline' else (extra or {}).get('layer_role', 'the root layer')}. {body}"
    elif nothing:
        explanation = f"There was nothing to write. {body}"
    else:
        explanation = body
    result: dict[str, Any] = {"ok": error is None, "action": action, "dry_run": dry, "applied": applied, "written": written}
    if error is not None:
        result["error"] = _text(error)
    unique = _unique(warnings)
    result["warnings"] = unique[:MAX_WARNINGS]
    if len(unique) > MAX_WARNINGS:
        result["warnings_truncated"] = len(unique) - MAX_WARNINGS
    result["explanation"] = explanation
    result["preview"] = _table(rows) if rows and len(rows) > 1 else ""
    result["edit_target_layer"] = info["identifier"] if info else None
    if extra:
        result.update(extra)
    if error is None and dry:
        result["next_step"] = "Dry run: nothing was written. Call again with dry_run=False to author this."
    return result


def _read_error(action: str, error: str, warnings: list[str]) -> dict[str, Any]:
    return {"ok": False, "action": action, "read_only": True, "error": _text(error), "warnings": _unique(warnings),
            "explanation": f"Nothing was read: {error}"}


# --- small helpers ----------------------------------------------------------------------------------------


def _require_pxr() -> None:
    if _PXR_IMPORT_ERROR is not None:
        raise _Refusal(f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}")


def _is_number(value: Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _finite(value: Any, name: str) -> float:
    if not _is_number(value) or not math.isfinite(float(value)):
        raise _Refusal(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _positive(value: Any, name: str, *, maximum: float) -> float:
    number = _finite(value, name)
    if number <= 0 or number > maximum:
        raise _Refusal(f"{name} must be greater than 0 and at most {maximum:g}, got {number:g}")
    return number


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str) and value.strip().lower() in ("true", "1", "yes", "on", "false", "0", "no", "off", ""):
        return value.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(value, numbers.Integral) and value in (0, 1):
        return bool(value)
    raise _Refusal(f"{name} must be true or false, got {value!r}")


def _vec3(value: Any, name: str) -> tuple[float, float, float]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__len__") or len(value) != 3:
        raise _Refusal(f"{name} must be 3 numbers (x, y, z), got {value!r}")
    x, y, z = (_finite(value[i], name) for i in range(3))
    return x, y, z


def _axis(value: Any, up: int, *, required: bool = False) -> int:
    if value is None:
        if required:
            raise _Refusal("axis is required: 'x', 'y', 'z' or 'up' (the parent-space axis to turn about).")
        return up
    text = value.strip().lower().lstrip("+") if isinstance(value, str) else ""
    if text == "up":
        return up
    if text not in ("x", "y", "z"):
        raise _Refusal(f"axis must be 'x', 'y', 'z' or 'up', got {value!r} (use a negative angle or turns to reverse).")
    return _AXES.index(text)


def _direction(value: Any) -> tuple[float, float, float]:
    if isinstance(value, str):
        text = value.strip().lower().replace(" ", "")
        sign = -1.0 if text.startswith("-") else 1.0
        letter = text.lstrip("+-")
        if letter not in ("x", "y", "z") or len(text) - len(letter) > 1:
            raise _Refusal(f"direction must be a local axis such as '+x' or '-y', or (x, y, z), got {value!r}")
        vector = [0.0, 0.0, 0.0]
        vector[_AXES.index(letter)] = sign
        return vector[0], vector[1], vector[2]
    vector3 = _vec3(value, "direction")
    length = math.sqrt(sum(c * c for c in vector3))
    if length < 1e-12:
        raise _Refusal("direction must not be the zero vector.")
    return vector3[0] / length, vector3[1] / length, vector3[2] / length


def _direction_text(vector: Any) -> str:
    for index, letter in enumerate("XYZ"):
        if abs(abs(vector[index]) - 1.0) < 1e-12:
            return ("+" if vector[index] > 0 else "-") + letter
    return _point(vector)


def _distance(a: Any, b: Any) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def _check_increasing(times: list[float]) -> None:
    for a, b in zip(times, times[1:]):
        if not b > a:
            raise _Refusal("Two keys landed on the same time code; use more seconds or fewer points.")


def _matmul(a: Any, b: Any) -> tuple[tuple[float, float, float], ...]:
    return tuple(tuple(sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)) for r in range(3))


def _heading(rows: Any, up: str) -> float | None:
    if up == "Z":
        x, y = rows[0][0], rows[0][1]
    else:
        x, y = rows[0][0], -rows[0][2]
    if math.hypot(x, y) < 1e-6:
        return None
    angle = math.degrees(math.atan2(y, x))
    return _clean(180.0 if angle == -180.0 else angle)


def _vec3_type(precision: str) -> Any:
    return {"double": Sdf.ValueTypeNames.Double3, "float": Sdf.ValueTypeNames.Float3, "half": Sdf.ValueTypeNames.Half3}[precision]


def _quat_type(precision: str) -> Any:
    return {"double": Sdf.ValueTypeNames.Quatd, "float": Sdf.ValueTypeNames.Quatf, "half": Sdf.ValueTypeNames.Quath}[precision]


def _scalar_type(precision: str) -> Any:
    return {"double": Sdf.ValueTypeNames.Double, "float": Sdf.ValueTypeNames.Float, "half": Sdf.ValueTypeNames.Half}[precision]


def _typed(slot: dict[str, Any], value: Any) -> Any:
    precision = slot["precision"]
    if slot["kind"] == "orient":
        w, x, y, z = (float(c) for c in value)
        if precision == "double":
            return Gf.Quatd(w, Gf.Vec3d(x, y, z))
        if precision == "half":
            return Gf.Quath(w, Gf.Vec3h(x, y, z))
        return Gf.Quatf(w, Gf.Vec3f(x, y, z))
    if slot["kind"] == "single":
        return float(value)
    components = [float(c) for c in value]
    if precision == "double":
        return Gf.Vec3d(*components)
    if precision == "half":
        return Gf.Vec3h(*components)
    return Gf.Vec3f(*components)


def _components(value: Any) -> list[float] | None:
    if value is None:
        return None
    if hasattr(value, "GetReal") and hasattr(value, "GetImaginary"):
        return [float(value.GetReal()), *(float(c) for c in value.GetImaginary())]
    if _is_number(value):
        return [float(value)]
    try:
        return [float(c) for c in value]
    except Exception:  # noqa: BLE001
        return None


def _value_error(got: Any, wanted: Any) -> float:
    a, b = _components(got), _components(wanted)
    if a is None or b is None or len(a) != len(b):
        return math.inf
    return max(abs(x - y) for x, y in zip(a, b))


def _tolerance(precision: str, value: Any) -> float:
    size = max([1.0] + [abs(c) for c in (_components(value) or [])])
    return {"double": 1e-9, "float": 1e-5, "half": 1e-2}.get(precision, 1e-5) * size


def _plain(value: Any) -> Any:
    components = _components(value)
    if components is None:
        return None if value is None else _text(value)
    if _is_number(value):
        return _clean(float(value))
    return [_clean(c) for c in components]


def _value_text(value: Any) -> str:
    components = _components(value)
    if components is None:
        return str(value)
    if len(components) == 1:
        return _fmt(components[0])
    return _point(components)


def _point(values: Any) -> str:
    return "(" + ", ".join(_fmt(float(v)) for v in values) + ")"


def _table(rows: list[tuple[str, ...]]) -> str:
    if not rows or len(rows) < 2:
        return ""
    columns = max(len(row) for row in rows)
    widths = [max(len(row[i]) for row in rows if len(row) > i) for i in range(columns)]
    lines = []
    for row in rows:
        if len(row) == 1 and columns > 1:
            lines.append(row[0])
            continue
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def _unique(warnings: list[str]) -> list[str]:
    out: list[str] = []
    for warning in warnings:
        text = _text(warning)
        if text not in out:
            out.append(text)
    return out


def _interpolation_name(stage: Any) -> str:
    try:
        return "held" if stage.GetInterpolationType() == Usd.InterpolationTypeHeld else "linear"
    except Exception:  # noqa: BLE001
        return "linear"


def _count_animated(stage: Any) -> int:
    count = 0
    for index, prim in enumerate(stage.Traverse()):
        if index >= MAX_SCAN_PRIMS:
            break
        count += sum(1 for attr in prim.GetAttributes() if attr.GetNumTimeSamples() > 0)
    return count


def _layer_time_value(layer: Any, key: str) -> float | None:
    has = {"startTimeCode": layer.HasStartTimeCode, "endTimeCode": layer.HasEndTimeCode,
           "framesPerSecond": layer.HasFramesPerSecond, "timeCodesPerSecond": layer.HasTimeCodesPerSecond}[key]
    return float(getattr(layer, key)) if has() else None


def _write_time_key(layer: Any, key: str, value: float) -> None:
    setattr(layer, key, float(value))


def _only_samples_everywhere(stage: Any, attr: Any, spec_path: Any) -> bool:
    """True when no layer holds a default value for the attribute (so clearing samples leaves it empty)."""
    try:
        for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
            if spec.HasDefaultValue():
                return False
    except Exception:  # noqa: BLE001
        return False
    return True


def _conveyor_status(stage: Any, prim: Any, warnings: list[str]) -> dict[str, Any]:
    names = set(str(name) for name in prim.GetAppliedSchemas())
    try:
        list_op = prim.GetMetadata("apiSchemas")
        if list_op is not None:
            names.update(str(item) for item in list_op.ApplyOperations([]))
    except Exception:  # noqa: BLE001
        pass
    kinematic = prim.GetAttribute("physics:kinematicEnabled")
    velocity = prim.GetAttribute("physxSurfaceVelocity:surfaceVelocity")
    scenes = [str(p.GetPath()) for i, p in enumerate(stage.Traverse()) if i < MAX_SCAN_PRIMS and p.GetTypeName() == "PhysicsScene"]
    status = {
        "path": str(prim.GetPath()),
        "type": prim.GetTypeName() or "",
        "rigid_body": "PhysicsRigidBodyAPI" in names,
        "kinematic": bool(kinematic.Get()) if kinematic and kinematic.HasAuthoredValue() else False,
        "collider": "PhysicsCollisionAPI" in names,
        "surface_velocity_api": SURFACE_VELOCITY_API in names,
        "surface_velocity": _plain(velocity.Get()) if velocity and velocity.HasAuthoredValue() else None,
        "physics_scenes": scenes[:MAX_LISTED],
    }
    if not scenes:
        warnings.append("The stage has no PhysicsScene, so nothing is simulated yet.")
    if status["rigid_body"] and not status["kinematic"]:
        warnings.append(f"{prim.GetPath()} is a dynamic rigid body; make it kinematic so the belt does not fall or get pushed.")
    if UsdGeom.Xformable(prim) and UsdGeom.Xformable(prim).TransformMightBeTimeVarying():
        warnings.append(f"{prim.GetPath()} also has keyframed motion; a surface-velocity belt should stand still.")
    return status


def _conveyor_snippet(belt: str, velocity: list[Any], boxes: list[str]) -> str:
    vx, vy, vz = (float(v or 0.0) for v in velocity)
    lines = [
        "# Run in Isaac Sim's Script Editor (Window > Script Editor) with the stage open. Then press Play.",
        "import omni.usd",
        "from pxr import Gf, PhysxSchema, UsdPhysics",
        "",
        "stage = omni.usd.get_context().get_stage()",
        f"belt = stage.GetPrimAtPath({belt!r})",
        "body = UsdPhysics.RigidBodyAPI.Apply(belt)        # the belt is a rigid body ...",
        "body.CreateKinematicEnabledAttr().Set(True)        # ... that never moves by itself",
        "UsdPhysics.CollisionAPI.Apply(belt)                # boxes need something to touch",
        "surface = PhysxSchema.PhysxSurfaceVelocityAPI.Apply(belt)",
        "surface.CreateSurfaceVelocityEnabledAttr().Set(True)",
        f"surface.CreateSurfaceVelocityAttr().Set(Gf.Vec3f({vx!r}, {vy!r}, {vz!r}))  # stage units per second",
        "surface.CreateSurfaceVelocityLocalSpaceAttr().Set(True)  # along the belt's own axes",
        "",
        f"for path in {boxes!r}:",
        "    box = stage.GetPrimAtPath(path)",
        "    UsdPhysics.RigidBodyAPI.Apply(box)             # dynamic: it can ride, fall and collide",
        "    UsdPhysics.CollisionAPI.Apply(box)",
    ]
    return "\n".join(lines)
