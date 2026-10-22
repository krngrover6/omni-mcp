"""Smart layout: snap prims onto a surface, stack them in rows, spread them along a shelf, space and align them.

Every function plans first and only writes with dry_run=False (the default is a dry run, which never dirties a
layer). Positions come from measured world bounding boxes (UsdGeom.BBoxCache, default + render purposes), so a box
"rests" on a surface when its bounding-box bottom touches the surface's bounding-box top along the stage up axis.

Only the prim's translation changes, and only one attribute is written per prim:
* the plain xformOp:translate op when the stack has one (its own type and precision, double/float/half, are kept);
* the translation part of a single xformOp:transform matrix (rotation and scale inside the matrix are kept);
* otherwise a new double3 xformOp:translate op, put first in xformOpOrder (the other ops keep their order).
The world move is converted to that op's local value through the parent's full transform (rotation, scale and the
ops that come before the translate op), so it is correct under rotated or scaled parents and for Isaac Sim's
translate + orient (quatf/quatd) + scale stacks, where XformCommonAPI is not usable.

Refused (nothing is written): instance proxies and prims inside a prototype, inactive or non-xformable prims, prims
without bounds, the same prim twice, a prim listed together with its own ancestor, animated transforms, and a
translate op that is also used inverted. Distances (gap, spacing, margin, anchor points) are in stage units.

Imports only pxr and the standard library, so the same functions run on a file stage or on the live stage in Kit.
Writes go to the stage's edit target; if one write fails, the edit target's specs for the moved prims are put back.
"""

from __future__ import annotations

import math
import numbers
from typing import Any

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
    _vec3,
    _vec3_for_op,
    _warn_animated_ancestors,
)

try:
    from pxr import Gf, Sdf, Usd, UsdGeom
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

PUBLIC_FUNCTIONS = ("snap_to_surface", "stack_rows", "distribute_on", "space_evenly", "align")
MAX_PRIMS = 500
MAX_ROWS = 200
MAX_FLOOR_SCAN = 50_000
MAX_WARNINGS = 40
MAX_PREVIEW_ROWS = 25
FLAT_RATIO = 0.1  # a "flat" surface is at most 10% as thick (along up) as its smaller horizontal side
DEFAULT_MARGIN_METERS = 0.05
TRANSLATE_NAME = "xformOp:translate"
ALIGN_MODES = ("min", "center", "max")
_AXES = "xyz"
_SINGULAR_DETERMINANT = 1e-12


# --- public API -------------------------------------------------------------------------------------------


def snap_to_surface(stage: Any, paths: Any, *, surface: Any = None, dry_run: bool = True) -> dict[str, Any]:
    """Drop (or lift) each prim along the up axis so its bounding-box bottom touches the surface top.

    surface is a prim path (its world bounding-box top is used) or a height number along the up axis. With None
    the floor is found automatically: the largest flat, visible gprim whose top is not above the
    highest prim center.
    Only the up-axis coordinate changes.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> dict[str, Any]:
        items = _collect(ctx, paths, warnings)
        u = ctx.up
        if surface is None:
            surface_path, top, box = _find_floor(ctx, items, warnings)
            how = f"the floor {surface_path} (found automatically: the largest flat surface below)"
        elif _is_number(surface):
            surface_path, top, box = None, _finite(surface, "surface"), None
            how = f"the height {ctx.axis_name(u)} = {_fmt(top)}"
        else:
            surface_path, box = _reference_prim(ctx, surface, "surface", items)
            top = box[1][u]
            how = f"the top of {surface_path}"
        deltas = []
        for item in items:
            delta = [0.0, 0.0, 0.0]
            delta[u] = top - item.box_min[u]
            deltas.append(delta)
            if box is not None and not _footprints_overlap(item.box_min, item.box_max, box[0], box[1], u):
                warnings.append(f"{item.path} is not above {surface_path}, so it would float beside it, not rest on it.")
        summary = (
            f"each prim moves along the up axis ({ctx.axis_name(u)}) until its bottom rests on {how} "
            f"at {ctx.axis_name(u)} = {_fmt(top)}"
        )
        return {
            "items": items,
            "deltas": deltas,
            "summary": summary,
            "extra": {"surface": surface_path, "surface_top": _clean(top)},
        }

    return _run("snap_to_surface", stage, dry_run, plan)


def stack_rows(
    stage: Any,
    paths: Any,
    *,
    rows: Any = (4, 3, 2, 1),
    anchor: Any = None,
    gap: float = 0.0,
    facing: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Stack prims in rows (bottom row first), one deep, as a wall or pyramid whose front faces `facing`.

    rows gives how many prims go in each row from the bottom up; their sum must equal the number of paths, which
    fill the rows in order, left to right as seen from the front. Each row is centered on the anchor and rests on
    the tallest prim of the row below; gap is the horizontal space between neighbors. anchor is a world (x, y, z)
    point (the bottom center of the stack), a prim path (the stack stands on top of that prim's bounding box,
    centered on it) or None (centered where the prims are now, starting at their lowest bottom). facing is a
    horizontal axis such as '-Y' or '+X'; None means -Y on a Z-up stage and +Z on a Y-up stage.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> dict[str, Any]:
        items = _collect(ctx, paths, warnings)
        counts = _rows(rows, len(items))
        space = _number(gap, "gap", minimum=0.0)
        f, f_sign = _facing(facing, ctx.up)
        u = ctx.up
        r = 3 - f - u
        right_sign = _right_sign(f, f_sign, u, r)
        if anchor is None:
            low = [min(item.box_min[i] for item in items) for i in range(3)]
            high = [max(item.box_max[i] for item in items) for i in range(3)]
            center = [(low[i] + high[i]) / 2.0 for i in range(3)]
            base = low[u]
            anchor_text = "where the prims are now (their combined center, lowest bottom)"
            anchor_info: Any = None
        elif isinstance(anchor, str):
            anchor_path, box = _reference_prim(ctx, anchor, "anchor", items)
            center = [(box[0][i] + box[1][i]) / 2.0 for i in range(3)]
            base = box[1][u]
            anchor_text = f"on top of {anchor_path}"
            anchor_info = anchor_path
        else:
            point = _vec3(anchor, "anchor")
            center = list(point)
            base = point[u]
            anchor_text = f"at the point {_point(point)}"
            anchor_info = _vec(point)

        deltas: list[list[float]] = []
        placements = []
        index = 0
        level = base
        for row_number, count in enumerate(counts):
            row_items = items[index:index + count]
            index += count
            widths = [item.size(r) for item in row_items]
            total = sum(widths) + space * (count - 1)
            offset = -total / 2.0  # measured along "right", as seen from the front
            for column, (item, width) in enumerate(zip(row_items, widths)):
                along = offset + width / 2.0
                offset += width + space
                delta = [0.0, 0.0, 0.0]
                delta[r] = center[r] + right_sign * along - item.center(r)
                delta[f] = center[f] - item.center(f)
                delta[u] = level - item.box_min[u]
                deltas.append(delta)
                placements.append({"path": item.path, "row": row_number + 1, "position_in_row": column + 1})
            level += max(item.size(u) for item in row_items)
        facing_name = f"{'+' if f_sign > 0 else '-'}{_AXES[f].upper()}"
        summary = (
            f"{len(items)} prims are stacked in {len(counts)} row(s) of {', '.join(str(c) for c in counts)} "
            f"(bottom to top), one deep, front facing {facing_name}, centered {anchor_text}; rows run along "
            f"{_AXES[r].upper()}, each row rests on the tallest prim of the row below, gap {_fmt(space)} between neighbors, "
            f"and the stack is {_fmt(level - base)} tall"
        )
        return {
            "items": items,
            "deltas": deltas,
            "summary": summary,
            "extra": {
                "rows": counts,
                "facing": facing_name,
                "row_axis": _AXES[r].upper(),
                "anchor": anchor_info,
                "base": _clean(base),
                "gap": _clean(space),
                "stack_height": _clean(level - base),
                "placements": placements,
            },
        }

    return _run("stack_rows", stage, dry_run, plan)


def distribute_on(
    stage: Any, paths: Any, surface_path: Any, *, margin: float | None = None, dry_run: bool = True
) -> dict[str, Any]:
    """Spread prims evenly along the long side of a shelf (or table) top, all resting on it.

    The first path goes at the low-coordinate end of the shelf's longer horizontal axis and the rest follow in
    order, with equal gaps between neighbors and `margin` kept free at both ends. Each prim is centered across the
    shelf's depth. margin is in stage units; None means 5 cm (0.05 / metersPerUnit).
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> dict[str, Any]:
        items = _collect(ctx, paths, warnings)
        shelf_path, box = _reference_prim(ctx, surface_path, "surface_path", items)
        u = ctx.up
        free = (DEFAULT_MARGIN_METERS / ctx.meters_per_unit) if margin is None else _number(margin, "margin", minimum=0.0)
        h1, h2 = [i for i in range(3) if i != u]
        sizes = {i: box[1][i] - box[0][i] for i in (h1, h2)}
        along, across = (h1, h2) if sizes[h1] >= sizes[h2] else (h2, h1)
        length = sizes[along]
        usable = length - 2.0 * free
        widths = [item.size(along) for item in items]
        needed = sum(widths)
        tolerance = 1e-9 * max(1.0, abs(length))
        if usable < -tolerance or needed > usable + tolerance:
            raise _Refusal(
                f"The prims do not fit on {shelf_path}: together they are {_fmt(needed)} long along "
                f"{_AXES[along].upper()}, but the shelf is {_fmt(length)} long and {_fmt(free)} margin is kept at each "
                "end. Use fewer prims, a smaller margin or a longer shelf."
            )
        _warn_rotated_surface(ctx, shelf_path, warnings)
        top = box[1][u]
        across_center = (box[0][across] + box[1][across]) / 2.0
        depth = sizes[across]
        if len(items) == 1:
            space = 0.0
            cursor = (box[0][along] + box[1][along]) / 2.0 - widths[0] / 2.0
        else:
            space = (usable - needed) / (len(items) - 1)
            cursor = box[0][along] + free
        deltas = []
        for item, width in zip(items, widths):
            delta = [0.0, 0.0, 0.0]
            delta[along] = cursor + width / 2.0 - item.center(along)
            delta[across] = across_center - item.center(across)
            delta[u] = top - item.box_min[u]
            deltas.append(delta)
            cursor += width + space
            if item.size(across) > depth + tolerance:
                warnings.append(
                    f"{item.path} is {_fmt(item.size(across))} deep but {shelf_path} is only {_fmt(depth)} deep, so it overhangs."
                )
        summary = (
            f"{len(items)} prim(s) are spread along the long side ({_AXES[along].upper()}, {_fmt(length)} long) of "
            f"{shelf_path}, resting on its top at {ctx.axis_name(u)} = {_fmt(top)} and centered across its depth, "
            f"with {_fmt(free)} margin at each end and {_fmt(space)} between neighbors"
        )
        return {
            "items": items,
            "deltas": deltas,
            "summary": summary,
            "extra": {
                "surface": shelf_path,
                "surface_top": _clean(top),
                "long_axis": _AXES[along].upper(),
                "margin": _clean(free),
                "spacing": _clean(space),
            },
        }

    return _run("distribute_on", stage, dry_run, plan)


def space_evenly(
    stage: Any, paths: Any, *, axis: str = "x", spacing: float | None = None, dry_run: bool = True
) -> dict[str, Any]:
    """Make the gaps between neighboring bounding boxes along one world axis equal.

    The prims are taken in their current order along the axis. With spacing=None the first and last stay put and
    the gaps in between become equal; with a spacing (stage units, edge to edge) the first stays put and each next
    prim starts that far after the previous one. Only that axis changes.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> dict[str, Any]:
        items = _collect(ctx, paths, warnings)
        a = _axis(axis, ctx.up)
        if len(items) < 2:
            raise _Refusal("space_evenly needs at least two prims.")
        order = sorted(range(len(items)), key=lambda i: (items[i].center(a), i))
        ordered = [items[i] for i in order]
        widths = [item.size(a) for item in ordered]
        if spacing is None:
            span = ordered[-1].box_max[a] - ordered[0].box_min[a]
            space = (span - sum(widths)) / (len(items) - 1)
            if len(items) == 2:
                warnings.append("With two prims and no spacing there is only one gap, so nothing moves; pass spacing to set it.")
            if space < 0.0:
                warnings.append(
                    f"The prims are wider than the distance between the first and the last, so they overlap by {_fmt(-space)} each."
                )
        else:
            space = _number(spacing, "spacing", minimum=0.0)
        deltas_by_index: dict[int, list[float]] = {}
        cursor = ordered[0].box_min[a]
        for index, item, width in zip(order, ordered, widths):
            delta = [0.0, 0.0, 0.0]
            delta[a] = cursor - item.box_min[a]
            deltas_by_index[index] = delta
            cursor += width + space
        mode = "keeping the first and last in place" if spacing is None else "keeping the first in place"
        summary = (
            f"{len(items)} prims are spaced along {_AXES[a].upper()} with {_fmt(space)} between neighboring "
            f"bounding boxes, {mode}, in their current order: {', '.join(_short(i.path) for i in ordered[:8])}"
            + (" ..." if len(ordered) > 8 else "")
        )
        return {
            "items": items,
            "deltas": [deltas_by_index[i] for i in range(len(items))],
            "summary": summary,
            "extra": {"axis": _AXES[a].upper(), "spacing": _clean(space), "order": [i.path for i in ordered]},
        }

    return _run("space_evenly", stage, dry_run, plan)


def align(stage: Any, paths: Any, *, axis: str = "x", mode: str = "min", dry_run: bool = True) -> dict[str, Any]:
    """Line prims up along one world axis: their bounding-box min sides, centers or max sides.

    min aligns to the lowest min side among the prims, max to the highest max side, center to the center of the
    box around all of them. Only that axis changes.
    """

    def plan(ctx: _Ctx, warnings: list[str]) -> dict[str, Any]:
        items = _collect(ctx, paths, warnings)
        a = _axis(axis, ctx.up)
        how = str(mode).strip().lower() if isinstance(mode, str) else ""
        if how not in ALIGN_MODES:
            raise _Refusal(f"mode must be one of {', '.join(ALIGN_MODES)}, got {mode!r}")
        if len(items) < 2:
            raise _Refusal("align needs at least two prims.")
        low = min(item.box_min[a] for item in items)
        high = max(item.box_max[a] for item in items)
        target = {"min": low, "max": high, "center": (low + high) / 2.0}[how]
        deltas = []
        for item in items:
            current = {"min": item.box_min[a], "max": item.box_max[a], "center": item.center(a)}[how]
            delta = [0.0, 0.0, 0.0]
            delta[a] = target - current
            deltas.append(delta)
        side = {"min": "low sides", "max": "high sides", "center": "centers"}[how]
        summary = f"{len(items)} prims are lined up so their bounding-box {side} sit at {_AXES[a].upper()} = {_fmt(target)}"
        return {"items": items, "deltas": deltas, "summary": summary, "extra": {"axis": _AXES[a].upper(), "mode": how, "target": _clean(target)}}

    return _run("align", stage, dry_run, plan)


# --- the shared plan / apply pipeline ---------------------------------------------------------------------


class _Ctx:
    """Per-call caches and stage facts (up axis, units)."""

    def __init__(self, stage: Any) -> None:
        self.stage = stage
        up = str(UsdGeom.GetStageUpAxis(stage)).upper()
        if up not in ("Y", "Z"):
            raise _Refusal(f"The stage up axis {up!r} is not Y or Z.")
        self.up_letter = up
        self.up = _AXES.index(up.lower())
        self.meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage)) or 0.01
        self.time = Usd.TimeCode.Default()
        self.bbox = _bbox_cache(self.time)
        self.xform = UsdGeom.XformCache(self.time)

    def axis_name(self, index: int) -> str:
        return _AXES[index].upper()

    def world_box(self, prim: Any) -> tuple[list[float], list[float]] | None:
        box = self.bbox.ComputeWorldBound(prim).ComputeAlignedRange()
        if box.IsEmpty():
            return None
        low, high = [float(v) for v in box.GetMin()], [float(v) for v in box.GetMax()]
        if not all(math.isfinite(v) for v in (*low, *high)):
            return None
        return low, high


class _Item:
    """One prim to move: its measured box and where its translation lives."""

    def __init__(self, path: str, prim: Any, box: tuple[list[float], list[float]], slot: dict[str, Any], to_world: Any, origin: Any) -> None:
        self.path = path
        self.prim = prim
        self.box_min, self.box_max = box
        self.slot = slot
        self.to_world = to_world  # row-vector matrix mapping a change of the translate value to a world move
        self.origin = origin

    def size(self, axis: int) -> float:
        return self.box_max[axis] - self.box_min[axis]

    def center(self, axis: int) -> float:
        return (self.box_min[axis] + self.box_max[axis]) / 2.0


def _run(action: str, stage: Any, dry_run: Any, planner: Any) -> dict[str, Any]:
    dry = _dry(dry_run)
    warnings: list[str] = []
    ctx: _Ctx | None = None
    try:
        if _PXR_IMPORT_ERROR is not None:
            raise _Refusal(f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}")
        _check_stage(stage)
        dirty_before = _dirty_layers(stage)
        _check_edit_target(stage, warnings)
        ctx = _Ctx(stage)
        plan = planner(ctx, warnings)
        items: list[_Item] = plan["items"]
        moves = [_plan_move(ctx, item, delta, warnings) for item, delta in zip(items, plan["deltas"])]
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # planning only reads the stage, so nothing was written
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    extra = {"up_axis": ctx.up_letter, "meters_per_unit": ctx.meters_per_unit, **plan["extra"]}
    if dry:
        newly_dirty = sorted(_dirty_layers(stage) - dirty_before)
        if newly_dirty:  # never expected: planning uses only read APIs
            warnings.append(f"Read-only violation: these layers became dirty during the dry run: {newly_dirty[:10]}")
        return _finish(action, True, stage, warnings, moves=moves, summary=plan["summary"], extra=extra)

    to_write = [(item, move) for item, move in zip(items, moves) if move["moved"]]
    if not to_write:
        return _finish(action, False, stage, warnings, moves=moves, summary=plan["summary"], extra=extra)
    error = _author_all(stage, to_write)
    if error is not None:
        message, written = error
        return _finish(action, False, stage, warnings, moves=moves, summary=plan["summary"], extra=extra, error=message, written=written)
    _readback(ctx, to_write, warnings)
    return _finish(action, False, stage, warnings, moves=moves, summary=plan["summary"], extra=extra, applied=True, written=True)


def _collect(ctx: _Ctx, paths: Any, warnings: list[str]) -> list[_Item]:
    """Validate the prims to move, measure them and find each one's translate slot."""
    raw = [paths] if isinstance(paths, str) else paths
    if not isinstance(raw, (list, tuple)) or not raw:
        raise _Refusal("paths must be a list of absolute prim paths such as ['/World/Box_01', '/World/Box_02'].")
    if len(raw) > MAX_PRIMS:
        raise _Refusal(f"At most {MAX_PRIMS} prims can be laid out at once; got {len(raw)}.")
    sdf_paths = []
    for value in raw:
        sdf_path = _prim_path(value, "paths")
        if sdf_path in sdf_paths:
            raise _Refusal(f"{sdf_path} is listed twice; list each prim once.")
        sdf_paths.append(sdf_path)
    for first in sdf_paths:
        for second in sdf_paths:
            if first != second and second.HasPrefix(first):
                raise _Refusal(
                    f"{second} is inside {first}, which is also listed: moving {first} already moves it. List only one of them."
                )
    items = []
    for sdf_path in sdf_paths:
        prim = ctx.stage.GetPrimAtPath(sdf_path)
        if not prim or not prim.IsValid():
            raise _Refusal(f"Prim does not exist: {sdf_path}")
        if prim.IsInstanceProxy() or prim.IsInPrototype():
            raise _Refusal(f"{sdf_path} is inside an instance; move the instance prim itself (an ancestor) instead.")
        if not prim.IsActive():
            raise _Refusal(f"{sdf_path} is inactive (deactivated), so it has no bounds to lay out.")
        xformable = UsdGeom.Xformable(prim)
        if not xformable:
            raise _Refusal(f"{sdf_path} is not transformable (type {prim.GetTypeName() or 'typeless'}), so it cannot be moved.")
        if xformable.TransformMightBeTimeVarying() or any(op.GetNumTimeSamples() > 0 for op in xformable.GetOrderedXformOps()):
            raise _Refusal(f"{sdf_path} has an animated transform (time samples), so a still position cannot be written.")
        box = ctx.world_box(prim)
        if box is None:
            raise _Refusal(
                f"{sdf_path} has no bounds (for example an empty Xform, a camera, an invisible prim or one whose purpose "
                "is guide), so there is nothing to measure."
            )
        _warn_animated_ancestors(prim.GetParent(), "Ancestor", warnings)
        slot = _translate_slot(prim, xformable)
        parent = Gf.Matrix4d(1.0) if xformable.GetResetXformStack() else ctx.xform.GetParentToWorldTransform(prim)
        to_world = slot["after"] * parent
        determinant = to_world.GetDeterminant()
        if not math.isfinite(determinant) or abs(determinant) < _SINGULAR_DETERMINANT:
            raise _Refusal(f"{sdf_path} sits under a zero scale (its parent or an op before translate), so it cannot be moved.")
        origin = ctx.xform.GetLocalToWorldTransform(prim).ExtractTranslation()
        items.append(_Item(str(sdf_path), prim, box, slot, to_world, origin))
    return items


def _translate_slot(prim: Any, xformable: Any) -> dict[str, Any]:
    """Where the translation is written: the plain translate op, a lone matrix op, or a new first translate op."""
    ops = list(xformable.GetOrderedXformOps())
    time = Usd.TimeCode.Default()
    if any(op.IsInverseOp() and str(op.GetName()) == TRANSLATE_NAME for op in ops):
        raise _Refusal(
            f"{prim.GetPath()} uses {TRANSLATE_NAME} inverted (!invert!), so changing it would cancel out; edit it by hand."
        )
    for index, op in enumerate(ops):
        if op.GetOpType() == UsdGeom.XformOp.TypeTranslate and str(op.GetName()) == TRANSLATE_NAME:
            after = Gf.Matrix4d(1.0)
            for outer in ops[:index]:  # ops listed before translate are applied after it
                after = outer.GetOpTransform(time) * after
            value = op.Get(time)
            return {
                "kind": "translate_op",
                "op": op,
                "attribute": TRANSLATE_NAME,
                "precision": _precision_name(op.GetPrecision()),
                "current": Gf.Vec3d(*(value if value is not None else (0.0, 0.0, 0.0))),
                "after": after,
                "ops": ops,
            }
    if len(ops) == 1 and ops[0].GetOpType() == UsdGeom.XformOp.TypeTransform and not ops[0].IsInverseOp():
        matrix = ops[0].Get(time) or Gf.Matrix4d(1.0)
        return {
            "kind": "transform_matrix",
            "op": ops[0],
            "attribute": str(ops[0].GetName()),
            "precision": "double",
            "current": Gf.Vec3d(matrix.ExtractTranslation()),
            "matrix": Gf.Matrix4d(matrix),
            "after": Gf.Matrix4d(1.0),
            "ops": ops,
        }
    existing = prim.GetAttribute(TRANSLATE_NAME)
    precision = "double"
    if existing and existing.IsValid():  # authored but not in xformOpOrder: reuse it with its own type
        stale = UsdGeom.XformOp(existing)
        if not stale or stale.GetOpType() != UsdGeom.XformOp.TypeTranslate:
            raise _Refusal(f"{prim.GetPath()} has a {TRANSLATE_NAME} attribute of an unexpected type, so it cannot be reused.")
        precision = _precision_name(stale.GetPrecision())
    return {
        "kind": "new_translate_op",
        "op": None,
        "attribute": TRANSLATE_NAME,
        "precision": precision,
        "current": Gf.Vec3d(0.0, 0.0, 0.0),
        "after": Gf.Matrix4d(1.0),
        "ops": ops,
    }


def _plan_move(ctx: _Ctx, item: _Item, delta: list[float], warnings: list[str]) -> dict[str, Any]:
    """Convert a world move into the new local translate value and describe it."""
    world_delta = Gf.Vec3d(*delta)
    scale = max(1.0, *(abs(v) for v in (*item.box_min, *item.box_max)))
    moved = world_delta.GetLength() > 1e-9 * scale
    slot = item.slot
    current = Gf.Vec3d(slot["current"])
    if moved:
        local_delta = item.to_world.GetInverse().TransformDir(world_delta)
        target = current + local_delta
    else:
        target = current
    written = _as_written(slot, target)
    slot["target"] = written
    names = [slot["attribute"]] + (["xformOpOrder"] if slot["kind"] == "new_translate_op" else [])
    if moved:
        for name in names:
            stronger = _stronger_opinion(ctx.stage, item.prim, name)
            if stronger:
                warnings.append(
                    f"{item.path}: {name} has a stronger opinion in {_text(stronger)} than the edit target, so the move "
                    "would be hidden by it. Set the edit target to that layer to see the change."
                )
        if slot["precision"] == "half":
            warnings.append(f"{item.path}: its translate op is half precision, so the position is rounded to about 3 digits.")
    new_min = [item.box_min[i] + delta[i] for i in range(3)]
    new_max = [item.box_max[i] + delta[i] for i in range(3)]
    return {
        "path": item.path,
        "moved": moved,
        "from_world": _vec(item.origin),
        "to_world": _vec(Gf.Vec3d(item.origin) + world_delta),
        "from_local": _vec(current),
        "to_local": _vec(written),
        "world_delta": _vec(delta),
        "bbox_before": {"min": _vec(item.box_min), "max": _vec(item.box_max)},
        "bbox_after": {"min": _vec(new_min), "max": _vec(new_max)},
        "attribute": slot["attribute"],
        "translate_kind": slot["kind"],
        "precision": slot["precision"],
    }


def _author_all(stage: Any, to_write: list[tuple[_Item, dict[str, Any]]]) -> tuple[str, bool] | None:
    """Write every move; if one fails, put the edit target's specs for these prims back. (message, written) on failure."""
    from .undo import SpecSnapshot  # pure pxr, no omni

    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    snapshot = None
    try:
        snapshot = SpecSnapshot.capture(layer, shallow=[edit_target.MapToSpecPath(item.prim.GetPath()) for item, _ in to_write])
    except Exception:  # noqa: BLE001 - e.g. a variant edit target; the writes below still run
        snapshot = None
    done = 0
    try:
        for item, _move in to_write:
            _author(item)
            done += 1
    except Exception as exc:  # noqa: BLE001
        if done == 0 and snapshot is not None and snapshot.matches(layer):
            return f"Writing failed before anything changed: {exc}", False
        if snapshot is not None:
            try:
                snapshot.restore(layer)
                return f"Writing failed at {item.path} ({exc}); the {done} prim(s) already moved were put back.", False
            except Exception as restore_exc:  # noqa: BLE001
                return f"Writing failed at {item.path} ({exc}) and could not be rolled back ({restore_exc}).", True
        return f"Writing failed at {item.path} ({exc}); {done} prim(s) were already moved.", done > 0
    return None


def _author(item: _Item) -> None:
    slot = item.slot
    value = slot["target"]
    if slot["kind"] == "translate_op":
        op = slot["op"]
        if not op.Set(_vec3_for_op(op, value)):
            raise ValueError(f"could not set {slot['attribute']}")
        return
    if slot["kind"] == "transform_matrix":
        matrix = Gf.Matrix4d(slot["matrix"])
        matrix.SetTranslateOnly(Gf.Vec3d(value))
        if not slot["op"].Set(matrix):
            raise ValueError(f"could not set {slot['attribute']}")
        return
    prim = item.prim
    attr = prim.GetAttribute(TRANSLATE_NAME)
    if not attr or not attr.IsValid():
        attr = prim.CreateAttribute(TRANSLATE_NAME, Sdf.ValueTypeNames.Double3, False)
    op = UsdGeom.XformOp(attr)
    if not op or not op.Set(_vec3_for_op(op, value)):
        raise ValueError(f"could not create {TRANSLATE_NAME}")
    xformable = UsdGeom.Xformable(prim)
    if not xformable.SetXformOpOrder([op, *slot["ops"]], xformable.GetResetXformStack()):
        raise ValueError("could not put the new translate op first in xformOpOrder")


def _readback(ctx: _Ctx, written: list[tuple[_Item, dict[str, Any]]], warnings: list[str]) -> None:
    """Measure the composed result; a mismatch means a stronger opinion (or rounding) changed the outcome."""
    cache = _bbox_cache(ctx.time)
    for item, move in written:
        box = cache.ComputeWorldBound(item.prim).ComputeAlignedRange()
        planned = move["bbox_after"]["min"]
        if box.IsEmpty():
            warnings.append(f"{item.path} has no bounds after the move; check it in the viewport.")
            continue
        error = max(abs(float(box.GetMin()[i]) - planned[i]) for i in range(3))
        scale = max(1.0, *(abs(v) for v in planned))
        allowed = {"double": 1e-6, "float": 1e-5, "half": 5e-3}.get(move["precision"], 1e-5) * scale
        move["readback_error"] = _clean(error)
        if error > allowed:
            warnings.append(
                f"{item.path} ended up {_fmt(error)} away from the planned spot: a stronger opinion in another layer, "
                "or a reference that also moves it, is overriding the edit."
            )


# --- helpers ----------------------------------------------------------------------------------------------


def _bbox_cache(time: Any) -> Any:
    return UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=False)


def _find_floor(ctx: _Ctx, items: list[_Item], warnings: list[str]) -> tuple[str, float, Any]:
    """The largest flat, visible gprim whose top is not above the highest item center (items excluded)."""
    u = ctx.up
    limit = max(item.center(u) for item in items) + 1e-9 * max(1.0, *(abs(item.center(u)) for item in items))
    item_paths = [item.prim.GetPath() for item in items]
    best = None
    scanned = 0
    prims = iter(Usd.PrimRange.Stage(ctx.stage, Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)))
    for prim in prims:
        scanned += 1
        if scanned > MAX_FLOOR_SCAN:
            warnings.append(f"The floor search stopped after {MAX_FLOOR_SCAN} prims; pass surface= to choose it yourself.")
            break
        path = prim.GetPath()
        if any(path.HasPrefix(item_path) for item_path in item_paths):
            prims.PruneChildren()
            continue
        if not prim.IsA(UsdGeom.Gprim) or any(item_path.HasPrefix(path) for item_path in item_paths):
            continue
        if UsdGeom.Imageable(prim).ComputeVisibility(ctx.time) == UsdGeom.Tokens.invisible:
            continue
        box = ctx.world_box(prim)
        if box is None:
            continue
        sides = [box[1][i] - box[0][i] for i in range(3) if i != u]
        thickness = box[1][u] - box[0][u]
        if min(sides) <= 0.0 or thickness > FLAT_RATIO * min(sides):
            continue
        top = box[1][u]
        if top > limit:
            continue
        area = sides[0] * sides[1]
        if best is None or area > best[0]:
            best = (area, str(path), top, box)
    if best is None:
        raise _Refusal(
            "No floor was found: no visible flat surface lies below the prims. Pass surface='/World/...' (a prim) "
            "or a height number."
        )
    return best[1], best[2], best[3]


def _reference_prim(ctx: _Ctx, value: Any, name: str, items: list[_Item]) -> tuple[str, tuple[list[float], list[float]]]:
    """A surface or anchor prim: it must exist, have bounds and not move with the prims being laid out."""
    sdf_path = _prim_path(value, name)
    prim = ctx.stage.GetPrimAtPath(sdf_path)
    if not prim or not prim.IsValid():
        raise _Refusal(f"{name} prim does not exist: {sdf_path}")
    for item in items:
        item_path = item.prim.GetPath()
        if sdf_path.HasPrefix(item_path):
            raise _Refusal(f"{name} {sdf_path} is (inside) {item_path}, one of the prims being moved; it would move too.")
        if item_path.HasPrefix(sdf_path):
            raise _Refusal(
                f"{name} {sdf_path} contains {item_path}, one of the prims being moved, so its bounds include that prim. "
                "Pick the surface prim itself (for example its mesh or cube)."
            )
    box = ctx.world_box(prim)
    if box is None:
        raise _Refusal(f"{name} {sdf_path} has no bounds (empty, invisible or guide-only), so it has no top to use.")
    return str(sdf_path), box


def _warn_rotated_surface(ctx: _Ctx, path: str, warnings: list[str]) -> None:
    matrix = ctx.xform.GetLocalToWorldTransform(ctx.stage.GetPrimAtPath(path))
    for axis in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
        direction = matrix.TransformDir(Gf.Vec3d(*axis))
        length = direction.GetLength()
        if length > 0.0 and max(abs(v) for v in direction) / length < 0.9999:
            warnings.append(
                f"{path} is turned at an angle to the world axes, so its world bounding box is larger than the shelf "
                "itself; prims are spread along that box."
            )
            return


def _footprints_overlap(low: list[float], high: list[float], other_low: list[float], other_high: list[float], up: int) -> bool:
    return all(low[i] <= other_high[i] and other_low[i] <= high[i] for i in range(3) if i != up)


def _rows(value: Any, count: int) -> list[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or not value:
        raise _Refusal("rows must be a list of whole numbers, bottom row first, such as [4, 3, 2, 1].")
    if len(value) > MAX_ROWS:
        raise _Refusal(f"At most {MAX_ROWS} rows are supported; got {len(value)}.")
    rows = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, numbers.Integral) or int(entry) < 1:
            raise _Refusal(f"Every row needs a whole number of prims (1 or more); got {entry!r} in rows {list(value)}.")
        rows.append(int(entry))
    if sum(rows) != count:
        raise _Refusal(f"rows {rows} hold {sum(rows)} prims but {count} path(s) were given; the numbers must add up to {count}.")
    return rows


def _facing(value: Any, up: int) -> tuple[int, float]:
    if value is None:
        return (1, -1.0) if up == 2 else (2, 1.0)
    text = str(value).strip().lower().replace(" ", "") if isinstance(value, str) else ""
    sign = -1.0 if text.startswith("-") else 1.0
    letter = text.lstrip("+-")
    if letter not in ("x", "y", "z") or len(text) - len(letter) > 1:
        raise _Refusal(f"facing must be a horizontal axis such as '-Y', '+X' or 'Z', got {value!r}")
    index = _AXES.index(letter)
    if index == up:
        raise _Refusal(f"facing {value!r} is the up axis; the front of a stack must face a horizontal direction.")
    return index, sign


def _right_sign(f: int, f_sign: float, u: int, r: int) -> float:
    """Sign of the row axis that points to the viewer's right when looking at the front (cross(-facing, up))."""
    view = [0.0, 0.0, 0.0]
    view[f] = -f_sign
    up = [0.0, 0.0, 0.0]
    up[u] = 1.0
    right = (
        view[1] * up[2] - view[2] * up[1],
        view[2] * up[0] - view[0] * up[2],
        view[0] * up[1] - view[1] * up[0],
    )
    return 1.0 if right[r] > 0 else -1.0


def _axis(value: Any, up: int) -> int:
    text = str(value).strip().lower() if isinstance(value, str) else ""
    if text == "up":
        return up
    if text not in ("x", "y", "z"):
        raise _Refusal(f"axis must be 'x', 'y', 'z' or 'up', got {value!r}")
    return _AXES.index(text)


def _number(value: Any, name: str, *, minimum: float | None = None) -> float:
    number = _finite(value, name)
    if minimum is not None and number < minimum:
        raise _Refusal(f"{name} must be at least {minimum:g}, got {number:g}")
    return number


def _finite(value: Any, name: str) -> float:
    if not _is_number(value) or not math.isfinite(float(value)):
        raise _Refusal(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _is_number(value: Any) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _precision_name(precision: Any) -> str:
    if precision == UsdGeom.XformOp.PrecisionFloat:
        return "float"
    if precision == UsdGeom.XformOp.PrecisionHalf:
        return "half"
    return "double"


def _as_written(slot: dict[str, Any], value: Any) -> Any:
    """The value as the op stores it (float/half translate ops round)."""
    components = [float(v) for v in value]
    if slot["precision"] == "float":
        return Gf.Vec3d(*(float(v) for v in Gf.Vec3f(*components)))
    if slot["precision"] == "half":
        return Gf.Vec3d(*(float(v) for v in Gf.Vec3h(*components)))
    return Gf.Vec3d(*components)


def _dirty_layers(stage: Any) -> set[str]:
    try:
        return {layer.identifier for layer in stage.GetUsedLayers() if layer.dirty}
    except Exception:  # noqa: BLE001
        return set()


def _vec(values: Any) -> list[float]:
    return [_clean(float(v)) for v in values]


def _fmt(value: float) -> str:
    text = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def _point(values: Any) -> str:
    return "(" + ", ".join(_fmt(v) for v in values) + ")"


def _short(path: str, keep: int = 2) -> str:
    parts = path.split("/")
    return path if len(parts) <= keep + 1 else ".../" + "/".join(parts[-keep:])


def _preview(moves: list[dict[str, Any]]) -> str:
    if not moves:
        return ""
    rows = [("prim", "from (world)", "to (world)", "change")]
    for move in moves[:MAX_PREVIEW_ROWS]:
        change = ", ".join(
            f"{axis} {'+' if value > 0 else ''}{_fmt(value)}" for axis, value in zip("XYZ", move["world_delta"]) if _fmt(value) != "0"
        )
        rows.append((_short(move["path"]), _point(move["from_world"]), _point(move["to_world"]), change or "stays"))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    lines = ["  ".join(cell.ljust(widths[i]) if i < 3 else cell for i, cell in enumerate(row)).rstrip() for row in rows]
    if len(moves) > MAX_PREVIEW_ROWS:
        lines.append(f"... and {len(moves) - MAX_PREVIEW_ROWS} more")
    return "\n".join(lines)


def _units_sentence(meters_per_unit: float) -> str:
    known = {1.0: "meters", 0.01: "centimeters", 0.001: "millimeters"}
    name = next((label for value, label in known.items() if abs(meters_per_unit - value) < 1e-12), None)
    return f"Distances are in stage units ({name})." if name else f"Distances are in stage units (1 unit = {meters_per_unit:g} m)."


def _finish(
    action: str,
    dry: bool,
    stage: Any,
    warnings: list[str],
    *,
    moves: list[dict[str, Any]] | None = None,
    summary: str = "",
    extra: dict[str, Any] | None = None,
    error: str | None = None,
    applied: bool = False,
    written: bool = False,
) -> dict[str, Any]:
    info = _edit_target_info(stage) if stage is not None and _PXR_IMPORT_ERROR is None else None
    unique: list[str] = []
    for warning in warnings:
        text = _text(warning)
        if text not in unique:
            unique.append(text)
    moves = moves or []
    moving = [move for move in moves if move["moved"]]
    if error is not None:
        explanation = f"Nothing was moved: {error}" if not written else f"The layout was only partly written: {error}"
    else:
        units = _units_sentence(extra["meters_per_unit"]) if extra else ""
        keep = "Only the translation is written (rotation and scale stay as they are)."
        if dry:
            head = f"Dry run: {len(moving)} of {len(moves)} prim(s) would move; nothing was written."
            tail = "Call again with dry_run=False to apply it."
        elif applied:
            head = f"Moved {len(moving)} of {len(moves)} prim(s) in the edit target layer."
            tail = "Undo restores the previous positions."
        else:
            head = "Every prim is already in place, so nothing was written."
            tail = ""
        explanation = " ".join(part for part in (head, f"Plan: {summary}.", keep, units, tail) if part)
    result: dict[str, Any] = {
        "ok": error is None,
        "action": action,
        "dry_run": dry,
        "applied": applied,
        "written": written,
    }
    if error is not None:
        result["error"] = _text(error)
    result["moves"] = moves
    result["warnings"] = unique[:MAX_WARNINGS]
    if len(unique) > MAX_WARNINGS:
        result["warnings_truncated"] = len(unique) - MAX_WARNINGS
    result["explanation"] = explanation
    result["preview"] = _preview(moves)
    result["edit_target_layer"] = info["identifier"] if info else None
    if extra:
        result.update(extra)
    if error is None and dry:
        result["next_step"] = "Dry run: nothing was written. Call again with dry_run=False to apply this layout."
    return result
