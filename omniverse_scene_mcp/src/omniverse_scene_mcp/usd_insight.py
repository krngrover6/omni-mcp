"""Read-only USD insight: stage, composition, value resolution, properties and bounds as JSON-safe dicts.

Imports only pxr and the standard library so the same functions can run on a file stage or inside Kit.

Time budgets: stage_info, properties and bounds accept budget_ms (or deadline=TimeBudget) and stop their traversal
loops cooperatively, returning partial results marked truncated_by_time plus a time_budget record. Inside
``with TimeBudget(ms):`` they (and usd_validate) use that ambient budget when none is passed; kit_rpc wraps live
insight this way so a big scene cannot hold Kit's main thread.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

try:
    from pxr import Gf, Kind, Sdf, Tf, Usd, UsdGeom
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Kind = Sdf = Tf = Usd = UsdGeom = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

MAX_ARRAY_ITEMS = 32
MAX_LIST_ITEMS = 100
MAX_DICT_KEYS = 100
MAX_STRING_CHARS = 4000
MAX_PROPERTIES = 300
MAX_LAYERS = 200
MAX_TRAVERSAL = 200_000
MAX_TYPE_BUCKETS = 50
MAX_DEPTH = 8
# Loops read the clock on every BUDGET_CHECK_EVERY-th TimeBudget.tick(), starting with the first.
BUDGET_CHECK_EVERY = 16
# With a budget, bounds fills the BBoxCache bottom-up and checks the budget between BBoxCache calls. A subtree gets
# its own call once it holds at least the current batch size of unmeasured prims (smaller ones ride along with their
# parent's call). The batch starts at BOUNDS_BATCH_PRIMS and is then sized from the measured cost per prim so one
# call takes about BOUNDS_CALL_SHARE of the remaining budget: few calls (fast) when time is plentiful.
BOUNDS_BATCH_PRIMS = 64
BOUNDS_MAX_BATCH = 1_000_000
BOUNDS_CALL_SHARE = 0.1

LIVERPS = "Local, Inherits, VariantSets, rElocates, References, Payloads, Specializes"
_LIVERPS_LETTERS = "LIVERPS"
_ARC_NAMES = {
    "ArcTypeRoot": ("local", "L"),
    "ArcTypeInherit": ("inherit", "I"),
    "ArcTypeVariant": ("variant", "V"),
    "ArcTypeRelocate": ("relocate", "E"),
    "ArcTypeReference": ("reference", "R"),
    "ArcTypePayload": ("payload", "P"),
    "ArcTypeSpecialize": ("specialize", "S"),
}
_SOURCE_NAMES = {
    "ResolveInfoSourceNone": "none",
    "ResolveInfoSourceFallback": "fallback",
    "ResolveInfoSourceDefault": "default",
    "ResolveInfoSourceTimeSamples": "time_samples",
    "ResolveInfoSourceValueClips": "value_clips",
    "ResolveInfoSourceSpline": "spline",
}
_OPINION_PHRASES = {
    "default": "the default value",
    "time_samples": "the time samples",
    "value_clips": "the value clip samples",
    "spline": "the spline",
}
_UNHANDLED = object()


class TimeBudget:
    """A cooperative time limit for long read-only traversals, timed with pxr's Tf.Stopwatch.

    Loops call tick() and stop with partial results once it returns True. ``with TimeBudget(3000):`` also makes it
    the ambient budget that budget-aware functions here and in usd_validate use when no explicit budget is given.
    The ambient stack is process-wide (not per thread): enter it only on the thread that runs the queries.
    """

    def __init__(self, budget_ms: float) -> None:
        _require_pxr()
        self.budget_ms = _budget_ms_value(budget_ms)
        self.exhausted = self.budget_ms <= 0
        self.truncated = False
        self._ticks = 0
        self._watch = Tf.Stopwatch()
        self._watch.Start()

    @classmethod
    def until(cls, deadline: float, now: float) -> TimeBudget:
        """A budget ending at deadline; deadline and now must come from one clock, e.g. time.monotonic()."""
        for name, value in (("deadline", deadline), ("now", now)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{name} must be a finite clock value in seconds, e.g. from time.monotonic()")
        return cls(max(0.0, (float(deadline) - float(now)) * 1000.0))

    def elapsed_ms(self) -> float:
        """Milliseconds since the budget started."""
        self._watch.Stop()
        seconds = self._watch.seconds
        self._watch.Start()
        return seconds * 1000.0

    def remaining_ms(self) -> float:
        return max(0.0, self.budget_ms - self.elapsed_ms())

    def expired(self) -> bool:
        """Read the clock now; once the budget is used up this stays True."""
        if not self.exhausted and self.elapsed_ms() >= self.budget_ms:
            self.exhausted = True
        return self.exhausted

    def tick(self) -> bool:
        """Cheap per-iteration check that reads the clock on every BUDGET_CHECK_EVERY-th call."""
        self._ticks += 1
        if self.exhausted:
            return True
        if (self._ticks - 1) % BUDGET_CHECK_EVERY:
            return False
        return self.expired()

    def report(self, processed: int, truncated: bool, note: str | None = None, **details: Any) -> dict[str, Any]:
        """The time_budget record for a result; a truncated report also marks the budget truncated."""
        if truncated:
            self.truncated = True
        default = "Stopped at the time budget; the results are partial." if truncated else "Finished within the time budget."
        record: dict[str, Any] = {
            "truncated_by_time": bool(truncated),
            "elapsed_ms": round(self.elapsed_ms(), 1),
            "budget_ms": round(self.budget_ms, 1),
            "processed": int(processed),
            "note": note or default,
        }
        record.update(_jsonable(details, MAX_ARRAY_ITEMS, 0))
        return record

    def __enter__(self) -> TimeBudget:
        _ACTIVE_BUDGETS.append(self)
        return self

    def __exit__(self, *_exc: Any) -> None:
        for index in range(len(_ACTIVE_BUDGETS) - 1, -1, -1):
            if _ACTIVE_BUDGETS[index] is self:
                del _ACTIVE_BUDGETS[index]
                break


_ACTIVE_BUDGETS: list[TimeBudget] = []


def active_budget() -> TimeBudget | None:
    """The innermost ``with TimeBudget(...)`` budget, or None."""
    return _ACTIVE_BUDGETS[-1] if _ACTIVE_BUDGETS else None


def resolve_budget(deadline: Any = None, budget_ms: float | None = None, *, now: float | None = None) -> TimeBudget | None:
    """The tightest of deadline, budget_ms and the ambient budget, or None when there is no limit at all.

    deadline is a TimeBudget (to share one limit across calls), or a clock value such as time.monotonic() + 2 when
    now (read from the same clock) is also given; usd_insight never reads the system clock itself.
    """
    candidates: list[TimeBudget] = []
    if isinstance(deadline, TimeBudget):
        candidates.append(deadline)
    elif deadline is not None:
        if now is None:
            raise TypeError(
                "deadline must be a TimeBudget here, e.g. TimeBudget.until(deadline, time.monotonic()); "
                "or pass budget_ms instead"
            )
        candidates.append(TimeBudget.until(deadline, now))
    if budget_ms is not None:
        candidates.append(TimeBudget(budget_ms))
    ambient = active_budget()
    if ambient is not None:
        candidates.append(ambient)
    if not candidates:
        return None
    return min(candidates, key=lambda budget: budget.remaining_ms())


def to_jsonable(value: Any, max_items: int = MAX_ARRAY_ITEMS) -> Any:
    """Convert Gf/Vt/Sdf/Usd values to JSON-safe data.

    Sequences longer than max_items become {"length", "items", "truncated": True}.
    """
    return _jsonable(value, max_items, 0)


def stage_info(stage: Any, *, deadline: TimeBudget | None = None, budget_ms: float | None = None) -> dict[str, Any]:
    """Summarize layers, stage metadata, prim census and model hierarchy.

    With budget_ms (or deadline, or an ambient TimeBudget) the layer walk and prim census stop at the limit and
    the result carries truncated_by_time and time_budget; without one nothing changes.
    """
    _require_pxr()
    budget = resolve_budget(deadline, budget_ms)
    root = stage.GetRootLayer()
    session = stage.GetSessionLayer()
    stack_ids = {layer.identifier for layer in stage.GetLayerStack(True)}
    layers: list[dict[str, Any]] = []
    visited: set[str] = set()
    layers_complete = True
    if session is not None:
        layers_complete = _walk_layers(stage, session, "session", None, None, 0, None, layers, frozenset(), visited, budget)
    if layers_complete:
        layers_complete = _walk_layers(stage, root, "root", None, None, 0, None, layers, frozenset(), visited, budget)
    for entry in layers:
        entry["in_layer_stack"] = entry["identifier"] in stack_ids

    used = [layer.identifier for layer in stage.GetUsedLayers(True)]
    target = stage.GetEditTarget().GetLayer()
    default_prim = stage.GetDefaultPrim()
    census, models, census_stopped = _census(stage, budget)
    errors_skipped = budget is not None and budget.expired()
    result = {
        "root_layer": _layer_record(stage, root),
        "session_layer": {
            "present": session is not None,
            "identifier": session.identifier if session is not None else None,
            "empty": bool(session.empty) if session is not None else None,
            "dirty": bool(session.dirty) if session is not None else None,
        },
        "edit_target": {
            "layer": target.identifier if target else None,
            "is_root_layer": bool(target) and target.identifier == root.identifier,
            "is_session_layer": bool(target) and session is not None and target.identifier == session.identifier,
        },
        "layer_stack": layers[:MAX_LAYERS],
        "layer_stack_truncated": len(layers) > MAX_LAYERS,
        "muted_layers": [str(layer) for layer in stage.GetMutedLayers()][:MAX_LIST_ITEMS],
        "used_layers": {"count": len(used), "identifiers": used[:MAX_LIST_ITEMS], "truncated": len(used) > MAX_LIST_ITEMS},
        "default_prim": str(default_prim.GetPath()) if default_prim and default_prim.IsValid() else None,
        "default_prim_authored": root.defaultPrim or None,
        "up_axis": str(UsdGeom.GetStageUpAxis(stage)),
        "up_axis_authored": stage.HasAuthoredMetadata("upAxis"),
        "meters_per_unit": _number(UsdGeom.GetStageMetersPerUnit(stage)),
        "meters_per_unit_authored": UsdGeom.StageHasAuthoredMetersPerUnit(stage),
        "time_codes_per_second": _number(stage.GetTimeCodesPerSecond()),
        "frames_per_second": _number(stage.GetFramesPerSecond()),
        "start_time_code": _number(stage.GetStartTimeCode()),
        "end_time_code": _number(stage.GetEndTimeCode()),
        "has_authored_time_code_range": stage.HasAuthoredTimeCodeRange(),
        "prims": census,
        "model_hierarchy": models,
        "composition_errors": [] if errors_skipped else _composition_errors(stage),
    }
    if budget is not None:
        parts = []
        if not layers_complete:
            parts.append(f"layer_stack lists only the first {len(layers)} layer(s)")
        if census_stopped:
            parts.append(f"the prim census and model hierarchy cover only the first {census['total']} prim(s)")
        if errors_skipped:
            parts.append("composition errors were not collected")
        truncated = bool(parts)
        note = _budget_note(budget, parts) if truncated else None
        result["truncated_by_time"] = truncated
        result["time_budget"] = budget.report(census["total"], truncated, note, layers_walked=len(layers))
    return result


def composition(
    stage: Any, prim_path: str, *, deadline: TimeBudget | None = None, budget_ms: float | None = None
) -> dict[str, Any]:
    """Explain how a prim is composed: arcs, variant sets, instancing and the prim stack.

    One prim, capped at MAX_LIST_ITEMS arcs and specs, so it never stops early; under a budget it only reports it.
    """
    prim = _require_prim(stage, prim_path)
    budget = resolve_budget(deadline, budget_ms)
    arcs, arcs_truncated = _arc_records(prim)
    pairs = _prim_stack(prim)
    prim_stack = []
    for spec, offset in pairs[:MAX_LIST_ITEMS]:
        arc = _arc_for_spec(arcs, spec.layer.identifier, str(spec.path))
        prim_stack.append(
            {
                "layer": spec.layer.identifier,
                "spec_path": str(spec.path),
                "specifier": _enum_word(spec.specifier, "Specifier"),
                "type_name": spec.typeName or None,
                "layer_offset": _offset(offset),
                "arc_type": arc["arc_type"] if arc else None,
                "liverps": arc["liverps"] if arc else None,
                "arc_index": arc["index"] if arc else None,
                "fields": sorted(str(key) for key in spec.ListInfoKeys())[:MAX_LIST_ITEMS],
            }
        )
    result = {
        "prim_path": str(prim.GetPath()),
        "type_name": prim.GetTypeName() or None,
        "specifier": _enum_word(prim.GetSpecifier(), "Specifier"),
        "active": prim.IsActive(),
        "defined": prim.IsDefined(),
        "abstract": prim.IsAbstract(),
        "loaded": prim.IsLoaded(),
        "has_authored_payloads": prim.HasAuthoredPayloads(),
        "instancing": _instancing(prim),
        "arcs": arcs,
        "arc_count": len(arcs),
        "arcs_truncated": arcs_truncated,
        "variant_sets": _variant_sets(prim, [spec for spec, _ in pairs]),
        "prim_stack": prim_stack,
        "prim_stack_count": len(pairs),
        "strength_order": LIVERPS,
    }
    if budget is not None:
        result["truncated_by_time"] = False
        result["time_budget"] = budget.report(len(arcs) + len(prim_stack), False)
    return result


def explain_value(
    stage: Any,
    prim_path: str,
    attribute: str | None = None,
    time: Any = None,
    *,
    deadline: TimeBudget | None = None,
    budget_ms: float | None = None,
) -> dict[str, Any]:
    """Resolve an attribute and explain which opinion wins and why (LIVERPS, layer order, time).

    One attribute, capped at MAX_LIST_ITEMS specs, so it never stops early (a partial property stack would give a
    wrong 'why'); under a budget it only reports it.
    """
    prim, attr = _require_attribute(stage, prim_path, attribute)
    budget = resolve_budget(deadline, budget_ms)
    time_code = _time_code(time)
    numeric = not time_code.IsDefault()
    info = attr.GetResolveInfo(time_code)
    source = _source_name(info.GetSource())
    blocked = bool(info.ValueIsBlocked())
    arcs, _ = _arc_records(prim)

    stack: list[dict[str, Any]] = []
    winner: dict[str, Any] | None = None
    pairs = _property_stack(attr, time_code)
    for index, (spec, offset) in enumerate(pairs[:MAX_LIST_ITEMS]):
        samples = int(spec.layer.GetNumTimeSamplesForPath(spec.path))
        has_default = bool(spec.HasDefaultValue())
        default = spec.default if has_default else None
        has_spline = bool(getattr(spec, "HasSpline", lambda: False)())
        # Within one spec at a numeric time: time samples beat a spline, and a spline beats the default.
        opinion = (
            "time_samples" if numeric and samples
            else "spline" if numeric and has_spline
            else "default" if has_default
            else None
        )
        arc = _arc_for_spec(arcs, spec.layer.identifier, str(spec.path.GetParentPath()))
        entry = {
            "index": index,
            "layer": spec.layer.identifier,
            "spec_path": str(spec.path),
            "layer_offset": _offset(offset),
            "has_default": has_default,
            "default": to_jsonable(default),
            "blocked": _is_block(default),
            "time_sample_count": samples,
            "spline": has_spline,
            "opinion_at_time": opinion,
            "arc_type": arc["arc_type"] if arc else None,
            "liverps": arc["liverps"] if arc else None,
            "arc_chain": arc["arc_chain"] if arc else [],
            "ancestral": arc["ancestral"] if arc else None,
            "introducing_layer": arc["introducing_layer"] if arc else None,
            "introducing_prim_path": arc["introducing_prim_path"] if arc else None,
            "wins": False,
        }
        if numeric and samples:
            entry["layer_time"] = _number(_layer_time(offset, time_code.GetValue()))
        if winner is None and opinion is not None and (blocked or source not in ("none", "fallback")):
            entry["wins"] = True
            winner = entry
        stack.append(entry)

    fallback = _fallback_value(prim, attr.GetName())
    result = {
        "prim_path": str(prim.GetPath()),
        "prim_type_name": prim.GetTypeName() or None,
        "attribute": attr.GetName(),
        "type_name": str(attr.GetTypeName()),
        "variability": _enum_word(attr.GetVariability(), "Variability"),
        "time": {"requested": to_jsonable(time), "used": _time_label(time_code)},
        "value": to_jsonable(attr.Get(time_code)),
        "source": source,
        "blocked": blocked,
        "has_value": attr.HasValue(),
        "has_authored_value": attr.HasAuthoredValue(),
        "might_be_time_varying": bool(attr.ValueMightBeTimeVarying()),
        "fallback": to_jsonable(fallback),
        "winning_opinion": winner,
        "property_stack": stack,
        "property_stack_count": len(pairs),
        "time_samples": _time_sample_summary(attr, time_code),
        "strength_order": LIVERPS,
    }
    result["why"] = _why(stage, result, numeric, time_code)
    if budget is not None:
        result["truncated_by_time"] = False
        result["time_budget"] = budget.report(len(stack), False)
    return result


def properties(
    stage: Any,
    prim_path: str,
    include_values: bool = True,
    time: Any = None,
    authored_only: bool = False,
    *,
    deadline: TimeBudget | None = None,
    budget_ms: float | None = None,
) -> dict[str, Any]:
    """List attributes, relationships and prim metadata with capped values (time-budgeted like stage_info)."""
    prim = _require_prim(stage, prim_path)
    budget = resolve_budget(deadline, budget_ms)
    time_code = _time_code(time)
    examined = 0
    stopped = False
    attributes = []
    attribute_count = 0
    for attr in prim.GetAttributes():
        if budget is not None and budget.tick():
            stopped = True
            break
        examined += 1
        if authored_only and not attr.IsAuthored():
            continue
        attribute_count += 1
        if len(attributes) < MAX_PROPERTIES:
            attributes.append(_attribute_record(attr, time_code, include_values))
    relationships = []
    relationship_count = 0
    for rel in [] if stopped else prim.GetRelationships():
        if budget is not None and budget.tick():
            stopped = True
            break
        examined += 1
        if authored_only and not rel.IsAuthored():
            continue
        relationship_count += 1
        if len(relationships) >= MAX_PROPERTIES:
            continue
        targets = [str(path) for path in rel.GetTargets()]
        relationships.append(
            {
                "name": rel.GetName(),
                "custom": rel.IsCustom(),
                "authored": rel.IsAuthored(),
                "targets": targets[:MAX_LIST_ITEMS],
                "target_count": len(targets),
            }
        )
    result = {
        "prim_path": str(prim.GetPath()),
        "type_name": prim.GetTypeName() or None,
        "time": _time_label(time_code),
        "attributes": attributes,
        "attribute_count": attribute_count,
        "attributes_truncated": attribute_count > MAX_PROPERTIES,
        "relationships": relationships,
        "relationship_count": relationship_count,
        "relationships_truncated": relationship_count > MAX_PROPERTIES,
        "metadata": _prim_metadata(prim),
    }
    if budget is not None:
        note = None
        if stopped:
            note = _budget_note(budget, [
                f"only the first {examined} properties were examined, so the attribute and relationship lists and "
                "counts are incomplete"
            ])
        result["truncated_by_time"] = stopped
        result["time_budget"] = budget.report(examined, stopped, note)
    return result


def bounds(
    stage: Any,
    prim_path: str,
    purposes: tuple[str, ...] | list[str] = ("default", "render"),
    time: Any = None,
    use_extents_hint: bool = False,
    *,
    deadline: TimeBudget | None = None,
    budget_ms: float | None = None,
) -> dict[str, Any]:
    """World-space bounds via UsdGeom.BBoxCache, authored versus computed extent, visibility and purpose.

    With a time budget the cache is filled subtree by subtree; if time runs out the bounds cover only the finished
    subtrees (truncated_by_time, time_budget.processed = prims measured) instead of blocking until the end.
    """
    prim = _require_prim(stage, prim_path)
    budget = resolve_budget(deadline, budget_ms)
    time_code = _time_code(time)
    tokens = {
        "default": UsdGeom.Tokens.default_,
        "render": UsdGeom.Tokens.render,
        "proxy": UsdGeom.Tokens.proxy,
        "guide": UsdGeom.Tokens.guide,
    }
    names = [str(name).strip().lower() for name in purposes]
    unknown = [name for name in names if name not in tokens]
    if unknown or not names:
        raise ValueError(f"Unknown purposes {unknown}; use any of default, render, proxy, guide.")
    cache = UsdGeom.BBoxCache(time_code, [tokens[name] for name in names], useExtentsHint=use_extents_hint)
    complete, measured, partial = (True, 0, None) if budget is None else _fill_bbox_cache(cache, prim, time_code, budget)
    result: dict[str, Any] = {
        "prim_path": str(prim.GetPath()),
        "time": _time_label(time_code),
        "purposes": names,
    }
    notes: list[str] = []
    budget_parts: list[str] = []
    if complete:
        result["world_bound"] = _range_record(cache.ComputeWorldBound(prim).ComputeAlignedRange())
        result["local_bound"] = _range_record(cache.ComputeLocalBound(prim).ComputeAlignedRange())
        result["untransformed_bound"] = _range_record(cache.ComputeUntransformedBound(prim).ComputeAlignedRange())
    else:
        # Every finished subtree's bound is in this prim's space (untransformed); add its own and its world transform.
        xform_cache = UsdGeom.XformCache(time_code)
        local_matrix = xform_cache.GetLocalTransformation(prim)[0]
        world_matrix = xform_cache.GetLocalToWorldTransform(prim)
        for key, matrix in (("world_bound", world_matrix), ("local_bound", local_matrix), ("untransformed_bound", None)):
            box = partial if matrix is None else Gf.BBox3d(partial.GetRange(), partial.GetMatrix() * matrix)
            result[key] = dict(_range_record(box.ComputeAlignedRange()), partial=True)
        budget_parts.append(
            f"world_bound, local_bound and untransformed_bound include only the {measured} prim(s) measured so far, "
            "so the real bounds may be larger"
        )
    result["imageable"] = prim.IsA(UsdGeom.Imageable)
    result["boundable"] = prim.IsA(UsdGeom.Boundable)
    extent: dict[str, Any] = {"authored": None, "computed": None, "matches": None}
    if result["boundable"]:
        boundable = UsdGeom.Boundable(prim)
        extent_attr = boundable.GetExtentAttr()
        authored = extent_attr.Get(time_code) if extent_attr.HasAuthoredValue() else None
        computed = None
        if budget is not None and budget.expired():
            budget_parts.append("the extent was not recomputed from the geometry")
        else:
            try:
                computed = UsdGeom.Boundable.ComputeExtentFromPlugins(boundable, time_code)
            except Exception:
                computed = None
        extent["authored"] = to_jsonable(authored)
        extent["computed"] = to_jsonable(computed)
        if authored is not None and computed is not None:
            extent["matches"] = _extents_close(authored, computed)
            if not extent["matches"]:
                notes.append(
                    "The authored extent does not match the extent computed from the geometry. BBoxCache trusts the "
                    "authored extent, so these bounds may be wrong until the extent is re-authored."
                )
        elif authored is None and computed is not None:
            notes.append("No extent is authored, so BBoxCache had to compute it from the geometry; authoring extents is faster.")
    result["extent"] = extent
    hint_attr = prim.GetAttribute("extentsHint")
    result["extents_hint"] = to_jsonable(hint_attr.Get(time_code)) if hint_attr and hint_attr.HasAuthoredValue() else None

    if result["imageable"]:
        imageable = UsdGeom.Imageable(prim)
        computed_visibility = str(imageable.ComputeVisibility(time_code))
        computed_purpose = str(imageable.ComputePurpose())
        result["visibility"] = {
            "authored": to_jsonable(imageable.GetVisibilityAttr().Get(time_code))
            if imageable.GetVisibilityAttr().HasAuthoredValue()
            else None,
            "computed": computed_visibility,
        }
        result["purpose"] = {
            "authored": to_jsonable(imageable.GetPurposeAttr().Get())
            if imageable.GetPurposeAttr().HasAuthoredValue()
            else None,
            "computed": computed_purpose,
        }
        if computed_visibility == str(UsdGeom.Tokens.invisible):
            notes.append(
                "This prim is invisible (itself or through an ancestor). Asking for its own bound still works, but "
                "BBoxCache leaves invisible prims out when it rolls up an ancestor's bound."
            )
        if computed_purpose not in names:
            notes.append(
                f"Its computed purpose is '{computed_purpose}', which is not among the included purposes {names}, "
                "so it contributes an empty bound."
            )
    else:
        result["visibility"] = None
        result["purpose"] = None
    if budget is not None and budget_parts:
        notes.insert(0, _budget_note(budget, budget_parts))
    if result["world_bound"]["empty"] and not notes:
        notes.append("The bound is empty: nothing below this prim is boundable geometry with the included purposes.")
    result["notes"] = notes
    if budget is not None:
        truncated = bool(budget_parts)
        result["truncated_by_time"] = truncated
        result["time_budget"] = budget.report(measured, truncated, notes[0] if truncated else None)
    return result


def _fill_bbox_cache(cache: Any, prim: Any, time_code: Any, budget: TimeBudget) -> tuple[bool, int, Any]:
    """Fill cache bottom-up in batches of subtrees, checking budget between BBoxCache calls.

    Returns (complete, prims measured, partial bound): the partial bound combines the finished subtrees in prim's
    untransformed space and is None when complete. Each open level is [unmeasured prims below it, all prims below
    it, finished (prim, size, bound) subtrees below it whose ancestors are not measured yet].
    """
    root_path = prim.GetPath()
    predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate) if prim.IsInstanceProxy() else Usd.PrimDefaultPredicate
    levels: list[list[Any]] = [[0, 0, []]]
    batch = BOUNDS_BATCH_PRIMS
    spent_ms = 0.0
    computed = 0
    iterator = iter(Usd.PrimRange.PreAndPostVisit(prim, predicate))
    for current in iterator:
        if not iterator.IsPostVisit():
            if budget.tick():
                return _partial_bbox(levels, root_path, time_code)
            levels.append([0, 0, []])
            continue
        level = levels.pop()
        parent = levels[-1]
        unmeasured, total = 1 + level[0], 1 + level[1]
        is_root = current.GetPath() == root_path
        # A crowded parent (many unmeasured prims already) measures its bigger children one by one instead.
        crowded = parent[0] >= batch and unmeasured >= BOUNDS_BATCH_PRIMS
        if not (is_root or unmeasured >= batch or crowded):
            parent[0] += unmeasured
            parent[1] += total
            parent[2].extend(level[2])
            continue
        if budget.expired():
            levels.append(level)
            return _partial_bbox(levels, root_path, time_code)
        started = budget.elapsed_ms()
        bound = cache.ComputeUntransformedBound(current) if is_root else cache.ComputeRelativeBound(current, prim)
        spent_ms += budget.elapsed_ms() - started
        computed += unmeasured
        parent[1] += total
        parent[2].append((current, total, bound))
        per_prim_ms = max(spent_ms / computed, 0.0005)
        batch = int(min(BOUNDS_MAX_BATCH, max(BOUNDS_BATCH_PRIMS, budget.remaining_ms() * BOUNDS_CALL_SHARE / per_prim_ms)))
    if levels[0][2]:
        return True, levels[0][2][0][1], None
    if budget.expired():  # the range skipped the prim itself (for example an inactive prim)
        return False, 0, Gf.BBox3d()
    cache.ComputeUntransformedBound(prim)
    return True, 1, None


def _partial_bbox(levels: list[list[Any]], root_path: Any, time_code: Any) -> tuple[bool, int, Any]:
    combined = Gf.BBox3d()
    measured = 0
    for _unmeasured, _total, finished in levels:
        for prim, size, bound in finished:
            measured += size
            if not _hidden_below(prim, root_path, time_code):
                combined = Gf.BBox3d.Combine(combined, bound)
    return False, measured, combined


def _hidden_below(prim: Any, root_path: Any, time_code: Any) -> bool:
    """True when prim or an ancestor below root_path authors visibility = invisible, which BBoxCache leaves out."""
    current = prim
    while current and current.GetPath() != root_path:
        if current.IsA(UsdGeom.Imageable):
            if UsdGeom.Imageable(current).GetVisibilityAttr().Get(time_code) == UsdGeom.Tokens.invisible:
                return True
        current = current.GetParent()
    return False


def _budget_ms_value(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"budget_ms must be a number of milliseconds, not {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("budget_ms must be a finite number of milliseconds, zero or more")
    return number


def _budget_note(budget: TimeBudget, parts: list[str]) -> str:
    return f"Stopped at the {budget.budget_ms:g} ms time budget: {'; '.join(parts)}. Narrow the query or allow more time."


def _require_pxr() -> None:
    if _PXR_IMPORT_ERROR is not None:
        raise ImportError("USD Python bindings (pxr) are required for USD insight.") from _PXR_IMPORT_ERROR


def _require_prim(stage: Any, prim_path: str) -> Any:
    _require_pxr()
    if not prim_path or not Sdf.Path.IsValidPathString(str(prim_path)):
        raise ValueError(f"Invalid prim path: {prim_path!r}")
    prim = stage.GetPrimAtPath(str(prim_path))
    if not prim or not prim.IsValid():
        raise ValueError(f"Prim does not exist: {prim_path}")
    return prim


def _require_attribute(stage: Any, prim_path: str, attribute: str | None) -> tuple[Any, Any]:
    _require_pxr()
    if not attribute and prim_path and Sdf.Path.IsValidPathString(str(prim_path)):
        path = Sdf.Path(str(prim_path))
        if path.IsPropertyPath():
            prim_path, attribute = str(path.GetPrimPath()), path.name
    if not attribute:
        raise ValueError("Provide an attribute name, or a property path like /World/Cube.size.")
    prim = _require_prim(stage, prim_path)
    attr = prim.GetAttribute(str(attribute))
    if not attr or not attr.IsValid():
        raise ValueError(f"Attribute does not exist: {prim.GetPath()}.{attribute}")
    return prim, attr


def _time_code(time: Any) -> Any:
    _require_pxr()
    if time is None:
        return Usd.TimeCode.Default()
    if isinstance(time, Usd.TimeCode):
        return time
    if isinstance(time, str):
        word = time.strip().lower()
        if word in ("", "default"):
            return Usd.TimeCode.Default()
        if word in ("earliest", "earliesttime", "earliest_time"):
            return Usd.TimeCode.EarliestTime()
        try:
            return Usd.TimeCode(float(word))
        except ValueError as exc:
            raise ValueError(f"Time must be a number, 'default' or 'earliest', not {time!r}") from exc
    if isinstance(time, bool):
        raise ValueError("Time must be a number, 'default' or 'earliest'.")
    return Usd.TimeCode(float(time))


def _time_label(time_code: Any) -> Any:
    if time_code.IsDefault():
        return "default"
    if time_code.IsEarliestTime():
        return "earliest"
    return _number(time_code.GetValue())


def _number(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    number = float(value)
    return number if math.isfinite(number) else str(number)


def _jsonable(value: Any, max_items: int, depth: int) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING_CHARS else value[:MAX_STRING_CHARS] + "..."
    if depth > MAX_DEPTH:
        return _short_text(value)
    if _PXR_IMPORT_ERROR is None:
        converted = _pxr_jsonable(value, max_items, depth)
        if converted is not _UNHANDLED:
            return converted
    if isinstance(value, dict):
        items = list(value.items())
        result = {str(key): _jsonable(item, max_items, depth + 1) for key, item in items[:MAX_DICT_KEYS]}
        if len(items) > MAX_DICT_KEYS:
            result["__truncated_keys__"] = len(items) - MAX_DICT_KEYS
        return result
    if isinstance(value, (set, frozenset)):
        return _sequence(sorted(value, key=str), max_items, depth)
    if isinstance(value, (list, tuple)):
        return _sequence(value, max_items, depth)
    return _short_text(value)


def _pxr_jsonable(value: Any, max_items: int, depth: int) -> Any:
    if isinstance(value, Sdf.ValueBlock):
        return {"value_block": True}
    if isinstance(value, Sdf.AssetPath):
        return {"authored": value.path, "resolved": value.resolvedPath or None}
    if isinstance(value, Sdf.Path):
        return str(value)
    if isinstance(value, Sdf.LayerOffset):
        return _offset(value)
    if isinstance(value, (Sdf.Reference, Sdf.Payload)):
        return {
            "asset_path": value.assetPath or None,
            "prim_path": str(value.primPath) or None,
            "layer_offset": _offset(value.layerOffset),
        }
    if isinstance(value, Sdf.ValueTypeName):
        return str(value)
    if isinstance(value, Sdf.Layer):
        return value.identifier
    if isinstance(value, Usd.TimeCode):
        return _time_label(value)
    # By class name, because these types moved between modules: Sdf.TimeCode(Array) in USD 25.11 became
    # Gf.TimeCode / Vt.TimeCodeArray in 26.x, and Sdf.AssetPathArray lives in pxr.Sdf, not pxr.Vt.
    type_name = type(value).__name__
    if type_name == "TimeCode":
        return _number(float(value))
    if type_name.endswith("Array") and hasattr(value, "__len__") and (type(value).__module__ or "").startswith("pxr"):
        return _sequence(value, max_items, depth)
    if isinstance(value, Usd.Object):
        return str(value.GetPath())
    if isinstance(value, Tf.Enum):
        return getattr(value, "name", None) or str(value)
    if hasattr(value, "explicitItems") and hasattr(value, "prependedItems"):
        if value.isExplicit:
            return {"explicit": _jsonable(list(value.explicitItems), max_items, depth + 1)}
        return {
            "prepended": _jsonable(list(value.prependedItems), max_items, depth + 1),
            "appended": _jsonable(list(value.appendedItems), max_items, depth + 1),
            "deleted": _jsonable(list(value.deletedItems), max_items, depth + 1),
        }
    module = type(value).__module__ or ""
    if module.endswith(".Vt") and hasattr(value, "__len__"):
        return _sequence(value, max_items, depth)
    if module.endswith(".Gf"):
        return _gf_jsonable(value, max_items, depth)
    return _UNHANDLED


def _gf_jsonable(value: Any, max_items: int, depth: int) -> Any:
    if hasattr(value, "GetReal") and hasattr(value, "GetImaginary"):
        return {"real": _number(value.GetReal()), "imaginary": [_number(item) for item in value.GetImaginary()]}
    if hasattr(value, "GetMin") and hasattr(value, "GetMax") and hasattr(value, "IsEmpty"):
        if value.IsEmpty():
            return {"empty": True, "min": None, "max": None}
        return {"min": _jsonable(value.GetMin(), max_items, depth + 1), "max": _jsonable(value.GetMax(), max_items, depth + 1)}
    dimension = getattr(value, "dimension", None)
    if isinstance(dimension, tuple) and len(dimension) == 2:
        rows, cols = dimension
        return [[_number(value[row][col]) for col in range(cols)] for row in range(rows)]
    if isinstance(dimension, int):
        return [_number(value[index]) for index in range(dimension)]
    if hasattr(value, "GetAxis") and hasattr(value, "GetAngle"):
        return {"axis": _jsonable(value.GetAxis(), max_items, depth + 1), "angle": _number(value.GetAngle())}
    if type(value).__name__ == "TimeCode":
        return _number(float(value))
    return _short_text(value)


def _sequence(values: Any, max_items: int, depth: int) -> Any:
    length = len(values)
    items = [_jsonable(values[index], max_items, depth + 1) for index in range(min(length, max_items))]
    if length <= max_items:
        return items
    return {"length": length, "items": items, "truncated": True}


def _short_text(value: Any) -> str:
    text = str(value)
    return text if len(text) <= MAX_STRING_CHARS else text[:MAX_STRING_CHARS] + "..."


def _enum_word(value: Any, prefix: str) -> str:
    name = getattr(value, "name", None) or str(value).rsplit(".", 1)[-1]
    return name[len(prefix):].lower() if name.startswith(prefix) else name


def _source_name(source: Any) -> str:
    name = getattr(source, "name", None) or str(source).rsplit(".", 1)[-1]
    return _SOURCE_NAMES.get(name, name)


def _offset(offset: Any) -> dict[str, Any]:
    if offset is None:
        return {"offset": 0.0, "scale": 1.0}
    return {"offset": _number(offset.offset), "scale": _number(offset.scale)}


def _is_identity(offset: Any) -> bool:
    return offset is None or (offset.offset == 0.0 and offset.scale == 1.0)


def _layer_time(offset: Any, stage_time: float) -> float:
    return stage_time if _is_identity(offset) else offset.GetInverse() * stage_time


def _is_block(value: Any) -> bool:
    return value is not None and isinstance(value, Sdf.ValueBlock)


def _short_layer(identifier: str | None) -> str:
    if not identifier:
        return "an unknown layer"
    if identifier.startswith("anon:") and identifier.endswith("session.usda"):
        return "the session layer"
    return identifier.replace("\\", "/").rsplit("/", 1)[-1]


def _layer_record(stage: Any, layer: Any) -> dict[str, Any]:
    return {
        "identifier": layer.identifier,
        "display_name": layer.GetDisplayName(),
        "real_path": layer.realPath or None,
        "anonymous": bool(layer.anonymous),
        "dirty": bool(layer.dirty),
        "muted": bool(stage.IsLayerMuted(layer.identifier)),
    }


def _walk_layers(
    stage: Any,
    layer: Any,
    role: str,
    authored_path: str | None,
    offset: Any,
    depth: int,
    parent: str | None,
    out: list[dict[str, Any]],
    ancestors: frozenset[str],
    visited: set[str],
    budget: TimeBudget | None = None,
) -> bool:
    """Walk sublayers depth-first. Only a layer that is its own ancestor is a cycle; a layer reached again through
    another parent (a diamond, which Pcp composes fine) is marked 'repeated' and walked again, as Pcp lists it.
    Returns False when the time budget ran out before the walk finished."""
    if len(out) > MAX_LAYERS:
        return True
    if budget is not None and budget.tick():
        return False
    record = _layer_record(stage, layer)
    record.update(role=role, authored_path=authored_path, depth=depth, parent=parent, offset=_offset(offset), missing=False)
    out.append(record)
    if layer.identifier in ancestors:
        record["cycle"] = True
        return True
    if layer.identifier in visited:
        record["repeated"] = True
    visited.add(layer.identifier)
    inner = ancestors | {layer.identifier}
    for sub_path, sub_offset in zip(layer.subLayerPaths, layer.subLayerOffsets):
        sub_layer = Sdf.Layer.FindRelativeToLayer(layer, sub_path)
        if sub_layer is not None:
            args = (stage, sub_layer, "sublayer", sub_path, sub_offset, depth + 1, layer.identifier, out, inner, visited)
            if not _walk_layers(*args, budget):
                return False
            continue
        identifier = Sdf.ComputeAssetPathRelativeToLayer(layer, sub_path) or sub_path
        muted = bool(stage.IsLayerMuted(identifier) or stage.IsLayerMuted(sub_path))
        out.append(
            {
                "identifier": identifier,
                "display_name": _short_layer(identifier),
                "real_path": None,
                "anonymous": False,
                "dirty": False,
                "muted": muted,
                "role": "sublayer",
                "authored_path": sub_path,
                "depth": depth + 1,
                "parent": layer.identifier,
                "offset": _offset(sub_offset),
                "missing": not muted,
            }
        )
    return True


def _census(stage: Any, budget: TimeBudget | None = None) -> tuple[dict[str, Any], dict[str, Any], bool]:
    by_type: Counter[str] = Counter()
    by_specifier: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    counts = Counter()
    models: list[dict[str, str]] = []
    broken: list[dict[str, str]] = []
    truncated = False
    stopped = False
    for prim in Usd.PrimRange.Stage(stage, Usd.PrimAllPrimsPredicate):
        if counts["total"] >= MAX_TRAVERSAL:
            truncated = True
            break
        if budget is not None and budget.tick():
            stopped = True
            break
        counts["total"] += 1
        by_type[prim.GetTypeName() or "(typeless)"] += 1
        by_specifier[_enum_word(prim.GetSpecifier(), "Specifier")] += 1
        active, loaded, defined, abstract = prim.IsActive(), prim.IsLoaded(), prim.IsDefined(), prim.IsAbstract()
        counts["inactive"] += not active
        counts["unloaded"] += not loaded
        counts["abstract"] += abstract
        counts["instances"] += prim.IsInstance()
        counts["traversed_by_default"] += active and loaded and defined and not abstract
        kind = Usd.ModelAPI(prim).GetKind()
        if not kind:
            continue
        by_kind[kind] += 1
        entry = {"path": str(prim.GetPath()), "kind": kind}
        if prim.IsModel():
            if len(models) < MAX_LIST_ITEMS:
                models.append(entry)
        elif Kind.Registry.IsA(kind, Kind.Tokens.model) and len(broken) < MAX_LIST_ITEMS:
            broken.append(entry)
    common = by_type.most_common(MAX_TYPE_BUCKETS)
    census = {
        "total": counts["total"],
        "traversed_by_default": counts["traversed_by_default"],
        "inactive": counts["inactive"],
        "unloaded": counts["unloaded"],
        "abstract": counts["abstract"],
        "instances": counts["instances"],
        "prototypes": len(stage.GetPrototypes()),
        "by_type": dict(common),
        "other_types": sum(by_type.values()) - sum(count for _, count in common),
        "by_specifier": dict(by_specifier),
        "truncated": truncated,
    }
    hierarchy = {
        "by_kind": dict(by_kind),
        "models": models,
        "models_truncated": len(models) >= MAX_LIST_ITEMS,
        "broken_model_kinds": broken,
        "note": "broken_model_kinds lists prims with a model kind whose ancestors are not all group/assembly, so IsModel() is False.",
    }
    if budget is not None:
        census["truncated_by_time"] = stopped
        hierarchy["truncated_by_time"] = stopped
    return census, hierarchy, stopped


def _composition_errors(stage: Any) -> list[str]:
    getter = getattr(stage, "GetCompositionErrors", None)
    if getter is None:
        return []
    try:
        errors = list(getter())
    except Exception:
        return []
    return [_short_text(getattr(error, "description", None) or error) for error in errors[:MAX_LIST_ITEMS]]


def _arc_records(prim: Any) -> tuple[list[dict[str, Any]], bool]:
    try:
        arcs = list(Usd.PrimCompositionQuery(prim).GetCompositionArcs())
    except Exception:
        return [], False
    records = []
    for index, arc in enumerate(arcs[:MAX_LIST_ITEMS]):
        node = arc.GetTargetNode()
        arc_type, letter = _arc_name(arc.GetArcType())
        chain = []
        walker = node
        while walker and not walker.IsRootNode():
            chain.append(_arc_name(walker.arcType)[0])
            walker = walker.parent
        chain.reverse()
        introducing = arc.GetIntroducingLayer()
        introducing_path = str(arc.GetIntroducingPrimPath())
        target_layer = arc.GetTargetLayer() if hasattr(arc, "GetTargetLayer") else node.layerStack.identifier.rootLayer
        target_path = arc.GetTargetPrimPath() if hasattr(arc, "GetTargetPrimPath") else node.path
        try:
            authored_value = to_jsonable(arc.GetIntroducingListEditor()[1])
        except Exception:
            authored_value = None
        layer_ids = [layer.identifier for layer in node.layerStack.layers]
        records.append(
            {
                "index": index,
                "arc_type": arc_type,
                "liverps": letter,
                "arc_chain": chain,
                "introducing_layer": introducing.identifier if introducing else None,
                "introducing_prim_path": introducing_path or None,
                "target_layer": target_layer.identifier if target_layer else None,
                "target_prim_path": str(target_path),
                "target_layer_stack": layer_ids[:MAX_LIST_ITEMS],
                "implicit": arc.IsImplicit(),
                "ancestral": arc.IsAncestral(),
                "has_specs": arc.HasSpecs(),
                "introduced_in_root_layer_stack": arc.IsIntroducedInRootLayerStack(),
                "layer_offset": _offset(node.mapToParent.Evaluate().timeOffset),
                "layer_offset_to_root": _offset(node.mapToRoot.Evaluate().timeOffset),
                "authored_value": authored_value,
            }
        )
    return records, len(arcs) > MAX_LIST_ITEMS


def _arc_name(arc_type: Any) -> tuple[str, str]:
    name = getattr(arc_type, "name", None) or str(arc_type).rsplit(".", 1)[-1]
    return _ARC_NAMES.get(name, (name, "?"))


def _arc_for_spec(arcs: list[dict[str, Any]], layer_id: str, node_path: str) -> dict[str, Any] | None:
    for arc in arcs:
        if arc["target_prim_path"] == node_path and layer_id in arc["target_layer_stack"]:
            return arc
    return None


def _prim_stack(prim: Any) -> list[tuple[Any, Any]]:
    getter = getattr(prim, "GetPrimStackWithLayerOffsets", None)
    if getter is not None:
        return [(spec, offset) for spec, offset in getter()]
    return [(spec, None) for spec in prim.GetPrimStack()]


def _property_stack(attr: Any, time_code: Any) -> list[tuple[Any, Any]]:
    getter = getattr(attr, "GetPropertyStackWithLayerOffsets", None)
    if getter is not None:
        return [(spec, offset) for spec, offset in getter(time_code)]
    return [(spec, None) for spec in attr.GetPropertyStack(time_code)]


def _instancing(prim: Any) -> dict[str, Any]:
    return {
        "instanceable": prim.IsInstanceable(),
        "is_instance": prim.IsInstance(),
        "is_instance_proxy": prim.IsInstanceProxy(),
        "is_prototype": prim.IsPrototype(),
        "in_prototype": prim.IsInPrototype(),
        "prototype": str(prim.GetPrototype().GetPath()) if prim.IsInstance() else None,
        "prim_in_prototype": str(prim.GetPrimInPrototype().GetPath()) if prim.IsInstanceProxy() else None,
    }


def _variant_sets(prim: Any, specs: list[Any]) -> list[dict[str, Any]]:
    variant_sets = prim.GetVariantSets()
    records = []
    for name in variant_sets.GetNames()[:MAX_LIST_ITEMS]:
        variant_set = variant_sets.GetVariantSet(name)
        selection = variant_set.GetVariantSelection() or None
        variants = list(variant_set.GetVariantNames())
        authored_in = None
        for spec in specs:
            chosen = dict(spec.variantSelections).get(name)
            if chosen is not None:
                authored_in = {"layer": spec.layer.identifier, "spec_path": str(spec.path), "selection": chosen}
                break
        records.append(
            {
                "name": name,
                "selection": selection,
                "variants": variants[:MAX_LIST_ITEMS],
                "has_authored_selection": variant_set.HasAuthoredVariantSelection(),
                "selection_authored_in": authored_in,
                "selection_is_valid": (selection in variants) if selection else None,
            }
        )
    return records


def _fallback_value(prim: Any, name: str) -> Any:
    try:
        return prim.GetPrimDefinition().GetAttributeFallbackValue(name)
    except Exception:
        return None


def _time_sample_summary(attr: Any, time_code: Any) -> dict[str, Any]:
    count = attr.GetNumTimeSamples()
    summary: dict[str, Any] = {"count": count}
    if not count:
        return summary
    times = attr.GetTimeSamples()
    summary["first_times"] = [_number(value) for value in times[:MAX_ARRAY_ITEMS]]
    summary["range"] = [_number(times[0]), _number(times[-1])]
    if not time_code.IsDefault():
        bracket = attr.GetBracketingTimeSamples(time_code.GetValue())
        values = [value for value in (bracket or ()) if not isinstance(value, bool)]
        if len(values) >= 2:
            summary["bracketing"] = [_number(value) for value in values[:2]]
    return summary


def _attribute_record(attr: Any, time_code: Any, include_values: bool) -> dict[str, Any]:
    samples = attr.GetNumTimeSamples()
    record: dict[str, Any] = {
        "name": attr.GetName(),
        "type_name": str(attr.GetTypeName()),
        "variability": _enum_word(attr.GetVariability(), "Variability"),
        "custom": attr.IsCustom(),
        "authored": attr.IsAuthored(),
        "has_authored_value": attr.HasAuthoredValue(),
        "time_sample_count": samples,
    }
    if include_values:
        record["value"] = to_jsonable(attr.Get(time_code))
    if samples:
        times = attr.GetTimeSamples()
        record["time_sample_range"] = [_number(times[0]), _number(times[-1])]
    if attr.HasAuthoredConnections():
        record["connections"] = [str(path) for path in attr.GetConnections()][:MAX_LIST_ITEMS]
    if UsdGeom.Primvar.IsPrimvar(attr):
        primvar = UsdGeom.Primvar(attr)
        info: dict[str, Any] = {
            "name": str(primvar.GetPrimvarName()),
            "interpolation": str(primvar.GetInterpolation()),
            "interpolation_authored": primvar.HasAuthoredInterpolation(),
            "element_size": primvar.GetElementSize(),
            "indexed": primvar.IsIndexed(),
        }
        if info["indexed"] and include_values:
            info["indices"] = to_jsonable(primvar.GetIndices(time_code))
        record["primvar"] = info
    elif attr.GetName().startswith("primvars:") and attr.GetName().endswith(":indices"):
        record["indices_for"] = attr.GetName()[: -len(":indices")]
    return record


def _prim_metadata(prim: Any) -> dict[str, Any]:
    authored = prim.GetAllAuthoredMetadata()
    return {
        "kind": Usd.ModelAPI(prim).GetKind() or None,
        "active": prim.IsActive(),
        "instanceable": prim.IsInstanceable(),
        "hidden": prim.IsHidden(),
        "documentation": authored.get("documentation"),
        "custom_data": to_jsonable(authored.get("customData", {})),
        "asset_info": to_jsonable(authored.get("assetInfo", {})),
        "api_schemas": [str(name) for name in prim.GetAppliedSchemas()],
        "authored_keys": sorted(str(key) for key in authored.keys()),
    }


def _range_record(box: Any) -> dict[str, Any]:
    if box.IsEmpty():
        return {"empty": True, "min": None, "max": None, "size": None, "center": None}
    return {
        "empty": False,
        "min": to_jsonable(box.GetMin()),
        "max": to_jsonable(box.GetMax()),
        "size": to_jsonable(box.GetSize()),
        "center": to_jsonable(box.GetMidpoint()),
    }


def _extents_close(first: Any, second: Any, tolerance: float = 1e-5) -> bool:
    if len(first) != len(second):
        return False
    return all(abs(a[axis] - b[axis]) <= tolerance for a, b in zip(first, second) for axis in range(3))


def _why(stage: Any, result: dict[str, Any], numeric: bool, time_code: Any) -> str:
    name = result["attribute"]
    stack = result["property_stack"]
    winner = result["winning_opinion"]
    source = result["source"]
    samples_anywhere = any(entry["time_sample_count"] for entry in stack)
    sentences: list[str] = []

    if result["blocked"]:
        where = f" in {_short_layer(winner['layer'])} at {winner['spec_path']}" if winner else ""
        sentences.append(f"The strongest opinion for '{name}' is a value block (None){where}.")
        outcome = "Get() returns None" if result["value"] is None else "Get() falls back to the schema fallback value"
        sentences.append(f"A block hides every weaker opinion, so the attribute acts as if nothing weaker were authored and {outcome}.")
    elif source in ("none", "fallback"):
        if not numeric and samples_anywhere:
            first = result["time_samples"].get("range", [None])[0]
            sentences.append(
                "You asked at the Default time. A Default-time query reads only authored default values and ignores "
                f"time samples; this attribute's opinions are time samples, so ask at a numeric time such as {first}."
            )
        elif not numeric and any(entry.get("spline") for entry in stack):
            sentences.append(
                "You asked at the Default time. A Default-time query reads only authored default values and ignores "
                "splines; this attribute's opinion is a spline, so ask at a numeric time."
            )
        elif stack:
            sentences.append(f"'{name}' is declared in {len(stack)} spec(s), but none of them authors a value.")
        else:
            sentences.append(f"No layer reached by composition authors an opinion for '{name}'.")
        if source == "fallback":
            type_name = result["prim_type_name"]
            schema = f"the {type_name} schema or an applied API schema" if type_name else "an applied API schema"
            sentences.append(f"USD therefore returns the fallback value defined by {schema}.")
        else:
            sentences.append("The schema defines no fallback, so the attribute has no value.")
    elif winner is not None:
        phrase = _OPINION_PHRASES.get(source, source)
        sentences.append(
            "USD walks the property stack from the strongest opinion to the weakest and uses the first one that has a "
            f"value: the winner is {phrase} in {_short_layer(winner['layer'])} at {winner['spec_path']}."
        )
        # At the Default time, stronger specs that hold only time samples (or a spline) are skipped, not absent.
        skipped = [] if numeric else [
            entry for entry in stack[: winner["index"]]
            if (entry["time_sample_count"] or entry.get("spline")) and not entry["has_default"]
        ]
        sentences.append(_arc_sentence(stage, winner, has_stronger_spec=bool(skipped)))
        if skipped:
            listed = ", ".join(f"{_short_layer(entry['layer'])} {entry['spec_path']}" for entry in skipped[:3])
            kind = "time samples or a spline" if any(entry.get("spline") for entry in skipped) else "time samples"
            sentences.append(
                f"Stronger spec(s) ({listed}) hold only {kind}, which a Default-time query ignores; at a numeric "
                "time the stronger spec would win."
            )
        if source == "spline":
            sentences.append(
                f"You asked at time {_time_label(time_code)}, so the spline is evaluated; within one spec, time samples "
                "beat a spline and a spline beats a default."
            )
        elif source in ("time_samples", "value_clips"):
            requested = _time_label(time_code)
            text = f"You asked at time {requested}, so time samples are used; within one spec, time samples beat a default."
            if "layer_time" in winner and not _offset_is_identity(winner["layer_offset"]):
                offset = winner["layer_offset"]
                text += (
                    f" The layer offset (offset {offset['offset']}, scale {offset['scale']}) maps stage time {requested} "
                    f"to time {winner['layer_time']} inside that layer."
                )
            sentences.append(text)
        elif numeric and any(entry["time_sample_count"] for entry in stack[winner["index"] + 1 :]):
            sentences.append(
                "A weaker spec has time samples, but a stronger default still wins: strength is decided first, and the "
                "time-samples-versus-default choice only happens inside the winning spec."
            )
        elif not numeric and winner["time_sample_count"]:
            sentences.append("You asked at the Default time, so the time samples in the same spec are ignored.")
    else:
        sentences.append(f"USD reports the value source as '{source}', but no spec in the property stack matched it.")

    if winner is not None:
        weaker = [
            entry for entry in stack[winner["index"] + 1 :]
            if entry["has_default"] or entry["time_sample_count"] or entry.get("spline")
        ]
        if weaker:
            listed = "; ".join(
                f"{_short_layer(entry['layer'])} {entry['spec_path']} ({entry['liverps'] or '?'})" for entry in weaker[:5]
            )
            more = f" and {len(weaker) - 5} more" if len(weaker) > 5 else ""
            sentences.append(f"{len(weaker)} weaker opinion(s) are overridden: {listed}{more}.")
    sentences.append(
        f"Strength order across arcs is LIVERPS ({LIVERPS}); inside one layer stack the session layer is strongest, "
        "then the root layer, then sublayers in the order they are listed."
    )
    return " ".join(sentences)


def _arc_sentence(stage: Any, winner: dict[str, Any], has_stronger_spec: bool = False) -> str:
    letter = winner["liverps"]
    layer = winner["layer"]
    if letter == "L":
        text = "This is a local opinion (L in LIVERPS) from the stage's own layer stack, which beats every composition arc."
        session = stage.GetSessionLayer()
        if session is not None and layer == session.identifier:
            return text + " It lives in the session layer, the strongest layer of all."
        if layer == stage.GetRootLayer().identifier:
            return text + " It is authored in the root layer itself."
        return (
            text + f" It comes from sublayer {_short_layer(layer)}; a sublayer is weaker than the root layer and than "
            "sublayers listed before it, but still stronger than any arc."
        )
    if letter is None:
        return "This opinion comes from a layer that is not part of a composition arc (for example a value clip)."
    chain = winner["arc_chain"]
    article = "an" if winner["arc_type"][:1] in "aeiou" else "a"
    text = f"It arrives through {article} {winner['arc_type']} arc ({letter} in LIVERPS)"
    if winner["introducing_layer"]:
        text += f" introduced in {_short_layer(winner['introducing_layer'])} at {winner['introducing_prim_path']}"
    if winner["ancestral"]:
        text += "; the arc is ancestral, authored on an ancestor prim"
    if len(chain) > 1:
        text += f"; it is nested ({' -> '.join(chain)})"
    stronger = _LIVERPS_LETTERS[: _LIVERPS_LETTERS.find(letter)] if letter in _LIVERPS_LETTERS else ""
    if stronger and len(chain) <= 1 and not has_stronger_spec:
        text += f". No stronger opinion ({', '.join(stronger)}) exists for this attribute"
    return text + "."


def _offset_is_identity(offset: dict[str, Any]) -> bool:
    return offset.get("offset") == 0.0 and offset.get("scale") == 1.0
