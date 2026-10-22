"""Variant manager: inspect, compare, check and safely edit USD variant sets (pure pxr, dry run by default).

Read-only:  list_variants, variant_contents, compare_variants, check_variants.
Writing:    duplicate_variant, rename_variant, remove_variant, create_variant_set, set_selection.
Every writing function plans first and only changes a layer when called with dry_run=False. Every write snapshots
the layers it touches (undo.SpecSnapshot) and rolls them back if authoring or the validation that follows fails;
pass undo_stack (an undo.UndoStack) to record the edit for undo_last. Results are JSON-safe dicts
{ok, action, dry_run, applied, written, warnings, explanation, ...}.

Variant reminder (the NCP-OUSD "V" in LIVRPS): a variant set is a named switch on a prim; each variant holds
opinions (attribute values, relationships such as material bindings, references, child prims) that only count
while that variant is selected. Variant opinions are weaker than the prim's local opinions (L), so a direct value
authored outside the variant in any local layer hides what every variant says about that attribute.

compare_variants and check_variants never switch the selection on the stage itself: they open a population-masked
copy of the stage on the same layers (with its own anonymous session layer, which sublayers the stage's session
layer) and switch the selection there. The open stage, its layers and their dirty flags stay exactly as they were,
so no viewport, OmniGraph or Kit listener ever sees a temporary switch.

Imports only pxr, the standard library and this package's pure-pxr helpers, so the same functions run on temp
stages in tests and on the live stage inside Kit / Isaac Sim. Inside Kit these are raw Sdf/Usd calls: they bypass
Kit's own commands and undo stack.
"""

from __future__ import annotations

import math
from typing import Any

from .authoring import _check_stage, _dry, _Refusal, _require_pxr, _text
from .usd_insight import resolve_budget, to_jsonable

try:
    from pxr import Sdf, Usd, UsdShade
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Sdf = Usd = UsdShade = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

MAX_PRIMS_SCANNED = 20_000
MAX_LISTED_PRIMS = 200
MAX_LISTED = 100
MAX_OPINIONS = 500
MAX_SUMMARY_LINES = 60
MAX_COMPARE_PRIMS = 2000
MAX_COMPARE_VALUES = 20_000
MAX_DIFFS = 200
MAX_PROBES = 200
MAX_ISSUES = 200
MAX_OVERRIDES = 100
MAX_NEW_VARIANTS = 100
MAX_NAME_CHARS = 128
MAX_PATH_CHARS = 4096
MAX_NESTING = 8
MAX_WARNINGS = 40
PROBE_TAG = "variant-manager-probe"
_LIST_OP_FIELDS = ("explicitItems", "prependedItems", "appendedItems", "addedItems")
_PRIM_ARC_LISTS = (
    ("referenceList", "references", "adds a reference"),
    ("payloadList", "payload", "adds a payload"),
    ("inheritPathList", "inherits", "inherits from"),
    ("specializesList", "specializes", "specializes"),
)
_PRIM_FIELDS = ("active", "kind", "instanceable", "apiSchemas")


# --- read-only ----------------------------------------------------------------------------------------------


def list_variants(stage: Any, root_path: str | None = None, *, deadline: Any = None) -> dict[str, Any]:
    """Every prim (under root_path, default the whole stage) with variant sets: the sets, their variants, the current
    selection and which layer authors it, where each variant's content lives (layer + spec path) and nested sets.

    deadline is a usd_insight.TimeBudget (or the ambient one); the walk stops with partial results when it runs out.
    """
    action = "list_variants"
    warnings: list[str] = []
    try:
        _ready(stage)
        budget = resolve_budget(deadline)
        if root_path in (None, "", "/"):
            start = stage.GetPseudoRoot()
        else:
            start = _prim(stage, root_path, "root_path")
        records: list[dict[str, Any]] = []
        scanned = 0
        stopped_by_time = False
        stopped_by_count = False
        for prim in Usd.PrimRange(start, Usd.PrimAllPrimsPredicate):
            if budget is not None and budget.tick():
                stopped_by_time = True
                break
            scanned += 1
            if scanned > MAX_PRIMS_SCANNED:
                stopped_by_count = True
                break
            if prim.IsPseudoRoot() or not prim.HasVariantSets():
                continue
            if len(records) >= MAX_LISTED_PRIMS:
                stopped_by_count = True
                break
            records.append(_prim_record(stage, prim, warnings))
        if stopped_by_time:
            warnings.append("Stopped at the time budget; only part of the stage was searched.")
        if stopped_by_count:
            warnings.append(f"Stopped after {MAX_PRIMS_SCANNED} prims or {MAX_LISTED_PRIMS} prims with variants.")
        set_count = sum(len(record["variant_sets"]) for record in records)
        explanation = (
            f"{len(records)} prim(s) carry {set_count} variant set(s). A variant set is a switch: only the selected "
            "variant's opinions count, and they are weaker than the prim's local opinions (V comes after L in LIVRPS)."
            if records else
            "No prim here has a variant set. Create one with create_variant_set."
        )
    except _Refusal as refusal:
        return _result(action, warnings=warnings, error=str(refusal), read_only=True)
    except Exception as exc:  # noqa: BLE001 - read-only, so nothing can be half-done
        return _result(action, warnings=warnings, error=f"Could not list the variants: {exc}", read_only=True)
    return _result(
        action,
        warnings=warnings,
        explanation=explanation,
        read_only=True,
        prims=records,
        prim_count=len(records),
        prims_scanned=min(scanned, MAX_PRIMS_SCANNED),
        truncated=stopped_by_time or stopped_by_count,
        truncated_by_time=stopped_by_time,
    )


def variant_contents(stage: Any, prim_path: str, variant_set: str, variant: str) -> dict[str, Any]:
    """What one variant changes: every opinion inside its variant spec(s) -- attribute values, relationships and
    material bindings, references/payloads, child prims, nested selections -- per layer, in plain English."""
    action = "variant_contents"
    warnings: list[str] = []
    try:
        _ready(stage)
        prim = _prim(stage, prim_path)
        set_name = _existing_set(stage, prim, variant_set)
        name = _existing_variant(stage, prim, set_name, variant)
        content = _content(stage, prim, set_name, name)
        if not content["sites"]:
            warnings.append(f"{set_name}={name} is listed but no layer holds a variant spec for it, so it changes nothing.")
        selection = _selection(prim, set_name)
        explanation = _contents_explanation(prim, set_name, name, content, selection)
    except _Refusal as refusal:
        return _result(action, warnings=warnings, error=str(refusal), read_only=True)
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Could not read the variant: {exc}", read_only=True)
    return _result(
        action,
        warnings=warnings,
        explanation=explanation,
        read_only=True,
        prim_path=str(prim.GetPath()),
        variant_set=set_name,
        variant=name,
        selected=selection == name,
        current_selection=selection,
        sites=content["public_sites"],
        opinions=content["opinions"][:MAX_OPINIONS],
        opinion_count=len(content["opinions"]),
        summary=[item["text"] for item in content["opinions"][:MAX_SUMMARY_LINES]],
        nested_variant_sets=content["nested"],
        changes_nothing=not content["opinions"] and not content["nested"],
    )


def compare_variants(stage: Any, prim_path: str, variant_set: str, a: str, b: str) -> dict[str, Any]:
    """Differences between two variants: in their authored opinions and in the composed result under the prim.

    The composed comparison switches the selection on a masked copy of the stage (see the module docstring), never
    on the stage itself; stage_unchanged confirms that no layer of the open stage was touched.
    """
    action = "compare_variants"
    warnings: list[str] = []
    try:
        _ready(stage)
        guard = _StageGuard(stage)
        prim = _prim(stage, prim_path)
        set_name = _existing_set(stage, prim, variant_set)
        first = _existing_variant(stage, prim, set_name, a)
        second = _existing_variant(stage, prim, set_name, b)
        if first == second:
            raise _Refusal("a and b are the same variant; pick two different variants to compare.")
        content_a = _content(stage, prim, set_name, first)
        content_b = _content(stage, prim, set_name, second)
        authored = _authored_diff(content_a["opinions"], content_b["opinions"], first, second)
        probe = _Probe(stage, prim.GetPath())
        try:
            state_a, cut_a = probe.state_for(prim.GetPath(), _probe_selection(prim, set_name, first, content_a))
            state_b, cut_b = probe.state_for(prim.GetPath(), _probe_selection(prim, set_name, second, content_b))
        finally:
            probe.close()
        if cut_a or cut_b:
            warnings.append(f"The composed comparison stopped after {MAX_COMPARE_PRIMS} prims / {MAX_COMPARE_VALUES} values.")
        composed = _state_diff(state_a, state_b, first, second)
        unchanged = guard.check(warnings)
        if not authored and not composed:
            explanation = f"{first} and {second} give the same result on {prim.GetPath()}: switching between them changes nothing."
        else:
            explanation = (
                f"{first} and {second} differ in {len(authored)} authored opinion(s) and {len(composed)} composed value(s) "
                f"under {prim.GetPath()}. "
                + ("Some authored differences do not show in the composed result: a stronger opinion outside the "
                   "variant hides them (local opinions beat variants)."
                   if len(composed) < len(_value_diffs(authored)) else "")
            ).strip()
    except _Refusal as refusal:
        return _result(action, warnings=warnings, error=str(refusal), read_only=True)
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Could not compare the variants: {exc}", read_only=True)
    return _result(
        action,
        warnings=warnings,
        explanation=explanation,
        read_only=True,
        prim_path=str(prim.GetPath()),
        variant_set=set_name,
        a=first,
        b=second,
        authored_differences=authored[:MAX_DIFFS],
        authored_difference_count=len(authored),
        composed_differences=composed[:MAX_DIFFS],
        composed_difference_count=len(composed),
        composed_values_compared=max(len(state_a), len(state_b)),
        summary=[item["text"] for item in (authored + composed)[:MAX_SUMMARY_LINES]],
        identical=not authored and not composed,
        stage_unchanged=unchanged,
    )


def check_variants(stage: Any, prim_path: str | None = None, *, deadline: Any = None) -> dict[str, Any]:
    """Switch every variant (on a masked copy of the stage) and report problems.

    Reports composition errors, material bindings whose target is missing or not a Material, texture (asset) paths
    that do not resolve, variants that author nothing or compose exactly like another variant, variant opinions
    hidden by a stronger opinion outside the variant, invalid or missing selections, a selection that a stronger
    local layer overrides, and variant set specs that are not listed in variantSets (USD ignores those).
    prim_path limits the check to one prim with variant sets (default: every prim on the stage, capped).
    """
    action = "check_variants"
    warnings: list[str] = []
    issues: list[dict[str, Any]] = []
    per_prim: list[dict[str, Any]] = []
    probes_run = 0
    stopped = False
    try:
        _ready(stage)
        guard = _StageGuard(stage)
        budget = resolve_budget(deadline)
        if prim_path in (None, "", "/"):
            prims = []
            for scanned, prim in enumerate(Usd.PrimRange(stage.GetPseudoRoot(), Usd.PrimAllPrimsPredicate)):
                if scanned >= MAX_PRIMS_SCANNED or len(prims) >= MAX_LISTED_PRIMS:
                    warnings.append("Only the first prims with variant sets were checked.")
                    break
                if not prim.IsPseudoRoot() and prim.HasVariantSets():
                    prims.append(prim)
        else:
            prim = _prim(stage, prim_path)
            if not prim.HasVariantSets() and not _all_set_names(stage, prim):
                raise _Refusal(f"{prim.GetPath()} has no variant sets to check.")
            prims = [prim]
        for prim in prims:
            if stopped:
                break
            record, used, stopped = _check_prim(stage, prim, issues, budget, MAX_PROBES - probes_run)
            probes_run += used
            per_prim.append(record)
        if stopped:
            warnings.append("Stopped at the probe or time limit; the results are partial.")
        unchanged = guard.check(warnings)
    except _Refusal as refusal:
        return _result(action, warnings=warnings, error=str(refusal), read_only=True)
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Could not check the variants: {exc}", read_only=True)
    errors = [issue for issue in issues if issue["severity"] == "error"]
    problems = [issue for issue in issues if issue["severity"] == "warning"]
    explanation = (
        f"Checked {probes_run} variant(s) on {len(per_prim)} prim(s): {len(errors)} error(s), {len(problems)} warning(s)."
        + (" Every variant composes cleanly." if not errors and not problems else "")
    )
    return _result(
        action,
        warnings=warnings,
        explanation=explanation,
        read_only=True,
        ok_variants=not errors,
        issues=issues[:MAX_ISSUES],
        issue_count=len(issues),
        error_count=len(errors),
        warning_count=len(problems),
        prims=per_prim,
        variants_checked=probes_run,
        truncated=stopped,
        summary=[issue["text"] for issue in issues[:MAX_SUMMARY_LINES]],
        stage_unchanged=unchanged,
    )


# --- writing ------------------------------------------------------------------------------------------------


def duplicate_variant(
    stage: Any,
    prim_path: str,
    variant_set: str,
    source: str,
    new_name: str,
    *,
    layer: str | None = None,
    overrides: dict[str, Any] | None = None,
    undo_stack: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or copy a variant to a new variant, then apply overrides inside the copy.

    The source variant spec is copied with Sdf.CopySpec (the source stays untouched). By default (layer=None) it is
    copied inside every local layer that holds content for the source. layer names one local layer instead: if it
    holds source content only that part is copied there; otherwise the source content (which must then live in a
    single local layer) is copied into it, e.g. layer="configurator.usda" puts Pink in your own stronger layer and
    leaves the course asset file untouched. Each override goes into the copy that already authors that attribute
    (else the strongest copy). overrides maps attribute paths (absolute, or relative to the prim, e.g.
    "Mat/Shader.inputs:diffuse_color_constant") to new values, typed from the existing attribute's type
    ([0.9, 0.4, 0.6] becomes a Gf.Vec3f for a color3f); a relationship takes a target path or a list of paths.
    After authoring, the new variant is validated (listed in the set, differs from the source only by the overrides,
    source unchanged); any failure rolls the layers back.
    """
    action = "duplicate_variant"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        guard = _StageGuard(stage)
        prim = _prim(stage, prim_path)
        path = prim.GetPath()
        set_name = _existing_set(stage, prim, variant_set)
        src_name = _existing_variant(stage, prim, set_name, source)
        target_name = _new_variant_name(stage, prim, set_name, new_name)
        content = _content(stage, prim, set_name, src_name)
        sites = content["sites"]
        local_sites = [site for site in sites if site.local]
        remote = [site for site in sites if not site.local]
        if not local_sites:
            where = ", ".join(f"{_short(site.layer)} ({site.path})" for site in remote) or "no layer"
            raise _Refusal(
                f"{set_name}={src_name} has no content in this stage's own layers (it comes from {where}, brought in by "
                "a reference or payload). Open that asset file and duplicate the variant there."
            )
        jobs = [(site, site.layer) for site in local_sites]
        if layer is not None:
            chosen = _find_local_layer(stage, layer)
            holding = [site for site in local_sites if site.layer.identifier == chosen.identifier]
            if holding:
                jobs = [(site, chosen) for site in holding]
                others = [site for site in local_sites if site.layer.identifier != chosen.identifier]
                if others:
                    warnings.append(
                        f"{src_name} also has content in {', '.join(_short(site.layer) for site in others)}; only "
                        f"{_short(chosen)} is copied, so {target_name} will not get that content."
                    )
            elif len(local_sites) > 1:
                raise _Refusal(
                    f"{src_name} has content in several layers ({', '.join(_short(site.layer) for site in local_sites)}), so "
                    f"it cannot be merged into {_short(chosen)}. Pass one of those layers, or leave layer empty."
                )
            else:
                jobs = [(local_sites[0], chosen)]
                warnings.append(
                    f"{target_name} is written into {_short(chosen)} while {src_name} stays in {_short(local_sites[0].layer)}; "
                    f"keep both layers in the stage, or {target_name} disappears."
                )
        elif local_sites[0].layer is not stage.GetEditTarget().GetLayer():
            warnings.append(
                f"{target_name} is written into {_short(local_sites[0].layer)} (the layer that holds {src_name}). To leave "
                f"that file untouched, pass layer= with your own layer (e.g. the edit target {_short(stage.GetEditTarget().GetLayer())})."
            )
        if remote:
            warnings.append(
                f"{src_name} also gets content from referenced layer(s) {', '.join(_short(site.layer) for site in remote)}; "
                f"{target_name} will not get that part, so it may differ from {src_name} by more than the overrides."
            )
        for _site, dst_layer in jobs:
            _require_editable(dst_layer)
            if dst_layer.identifier not in _rank_map(stage):
                raise _Refusal(f"{_short(dst_layer)} is not in the local layer stack.")
        override_plan = _plan_overrides(stage, prim, jobs, sites, overrides, warnings)
        copies = [
            {"layer": _text(dst_layer.identifier), "layer_short": _short(dst_layer),
             "source_layer_short": _short(site.layer), "from": str(site.path),
             "to": str(_sibling_variant(site.path, set_name, target_name))}
            for site, dst_layer in jobs
        ]
        predicted = _predicted_lines(content["opinions"], [site for site, _dst in jobs], override_plan, src_name, target_name)
        plan = {
            "prim_path": str(path),
            "variant_set": set_name,
            "source": src_name,
            "new_variant": target_name,
            "copies": copies,
            "overrides": [_public_override(item) for item in override_plan],
            "predicted_contents": predicted[:MAX_SUMMARY_LINES],
        }
        explanation = (
            f"{target_name} would start as an exact copy of {src_name} in {', '.join(_short(dst) for _s, dst in jobs)}"
            + (f", then {len(override_plan)} value(s) would change inside {target_name} only" if override_plan else "")
            + f". {src_name} stays as it is. Select it with set_selection to see it."
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001 - planning only reads
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    if dry:
        guard.check(warnings)
        return _result(action, dry=True, warnings=warnings, explanation=explanation, plan=plan)

    before_sources = [(site, _spec_state(site.layer, site.path)) for site in local_sites]
    before_opinions = [op for op in content["opinions"]
                       if not op["local"] or any(op["layer"] == _text(site.layer.identifier) for site, _dst in jobs)]

    def mutate() -> None:
        for site, dst_layer in jobs:
            dst = _sibling_variant(site.path, set_name, target_name)
            if dst_layer is not site.layer:
                owner_path = site.path.GetParentPath()
                owner = dst_layer.GetPrimAtPath(owner_path) or Sdf.CreatePrimInLayer(dst_layer, owner_path)
                if set_name not in [str(key) for key in owner.variantSets.keys()]:
                    Sdf.VariantSetSpec(owner, set_name)
            if not Sdf.CopySpec(site.layer, site.path, dst_layer, dst):
                raise RuntimeError(f"Sdf.CopySpec could not copy {site.path} to {dst} in {_short(dst_layer)}")
        for item in override_plan:
            _author_override(item["layer"], item, path, _sibling_variant(item["site"].path, set_name, target_name))

    def verify() -> tuple[str | None, dict[str, Any]]:
        names = list(prim.GetVariantSet(set_name).GetVariantNames())
        if target_name not in names:
            return f"{target_name} does not appear in {set_name} after the copy (variants: {names}).", {}
        for site, state in before_sources:
            if not _same(state, _spec_state(site.layer, site.path)):
                return f"The source variant {site.path} in {_short(site.layer)} changed.", {}
        new_content = _content(stage, prim, set_name, target_name)
        diffs = _authored_diff(before_opinions, new_content["opinions"], src_name, target_name)
        expected = {(item["path"], item["field"]) for item in override_plan}
        remote_keys = {(op["path"], op["field"]) for op in before_opinions if not op["local"]}
        unexpected = [d for d in diffs if (d["path"], d["field"]) not in expected and (d["path"], d["field"]) not in remote_keys]
        if unexpected:
            return f"{target_name} differs from {src_name} in more than the overrides: {unexpected[0]['text']}", {}
        missing = [item for item in override_plan
                   if not any(op["path"] == item["path"] and op["field"] == item["field"] and _same(op["raw"], item["compare"])
                              for op in new_content["opinions"])]
        if missing:
            return f"The override on {missing[0]['path']} is not inside {target_name}.", {}
        hidden = _hidden_overrides(stage, prim, set_name, target_name, new_content, override_plan)
        if hidden:
            warnings.append(f"{len(hidden)} override(s) are hidden by a stronger opinion outside the variant (see "
                            "hidden_overrides), so selecting the new variant will not show them.")
        return None, {
            "new_variant_contents": [op["text"] for op in new_content["opinions"][:MAX_SUMMARY_LINES]],
            "differences_from_source": [d["text"] for d in diffs[:MAX_SUMMARY_LINES]],
            "hidden_overrides": hidden,
        }

    layers = _unique_layers([dst_layer for _site, dst_layer in jobs])
    return _apply(action, stage, [(item, [path], []) for item in layers], mutate, verify, undo_stack,
                  f"duplicate variant {set_name}={src_name} as {target_name} on {path}", warnings,
                  explanation.replace("would start", "started").replace("would change", "changed"), {"plan": plan})


def rename_variant(
    stage: Any,
    prim_path: str,
    variant_set: str,
    old: str,
    new: str,
    *,
    switch_to: str | None = None,
    undo_stack: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or rename a variant in every local layer that holds it.

    Refuses when the variant is the current selection unless switch_to is given (the new name keeps it selected, or
    name another variant). Selection opinions in local layers that name the old variant are rewritten to switch_to
    (or to the new name). Refuses when a referenced layer also defines the variant (rename it in that asset).
    """
    return _rename_or_remove("rename_variant", stage, prim_path, variant_set, old, new, switch_to, undo_stack, dry_run)


def remove_variant(
    stage: Any,
    prim_path: str,
    variant_set: str,
    variant: str,
    *,
    switch_to: str | None = None,
    undo_stack: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or delete a variant (and all its opinions) from every local layer.

    Refuses when it is the current selection unless switch_to names another variant to select instead. Selection
    opinions in local layers that name it are rewritten to switch_to. Refuses when a referenced layer also
    defines it.
    """
    return _rename_or_remove("remove_variant", stage, prim_path, variant_set, variant, None, switch_to, undo_stack, dry_run)


def create_variant_set(
    stage: Any,
    prim_path: str,
    name: str,
    variants: Any = (),
    *,
    selection: str | None = None,
    layer: str | None = None,
    undo_stack: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or add a new variant set with empty variants to a prim.

    layer defaults to the stage's edit target (it must be a local layer). selection optionally selects one of the
    new variants in the same layer. The variants start empty: fill one with duplicate_variant or by authoring inside
    a variant edit context.
    """
    action = "create_variant_set"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        guard = _StageGuard(stage)
        prim = _prim(stage, prim_path)
        path = prim.GetPath()
        set_name = _identifier(name, "name")
        if set_name in _all_set_names(stage, prim):
            raise _Refusal(f"{path} already has a variant set named {set_name}. Use duplicate_variant to add variants to it.")
        if isinstance(variants, str) or not isinstance(variants, (list, tuple)):
            raise _Refusal("variants must be a list of names, e.g. ['Black', 'White'].")
        if len(variants) > MAX_NEW_VARIANTS:
            raise _Refusal(f"At most {MAX_NEW_VARIANTS} variants can be created at once.")
        names: list[str] = []
        for item in variants:
            text = _identifier(item, "variant name")
            if text in names:
                raise _Refusal(f"The variant name {text} is listed twice.")
            names.append(text)
        if selection is not None and selection not in names:
            raise _Refusal(f"selection must be one of the new variants {names}, got {selection!r}.")
        target = _write_layer(stage, layer)
        if not names:
            warnings.append("The variant set has no variants yet; add some with duplicate_variant after creating them.")
        else:
            warnings.append("The new variants are empty: each changes nothing until you author opinions inside it.")
        if selection is None and names:
            warnings.append("No selection is authored, so no variant is active until you call set_selection.")
        if target is stage.GetSessionLayer():
            warnings.append("The session layer is never saved with the scene.")
        plan = {"prim_path": str(path), "variant_set": set_name, "variants": names, "selection": selection,
                "layer": _text(target.identifier), "layer_short": _short(target)}
        explanation = (
            f"{path} would get a variant set {set_name} with {len(names)} variant(s) ({', '.join(names) or 'none'}) "
            f"in {_short(target)}" + (f", with {selection} selected." if selection else ".")
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    if dry:
        guard.check(warnings)
        return _result(action, dry=True, warnings=warnings, explanation=explanation, plan=plan)

    def mutate() -> None:
        spec = Sdf.CreatePrimInLayer(target, path)
        vset = Sdf.VariantSetSpec(spec, set_name)
        for item in names:
            Sdf.VariantSpec(vset, item)
        spec.variantSetNameList.Prepend(set_name)
        if selection is not None:
            spec.variantSelections[set_name] = selection

    def verify() -> tuple[str | None, dict[str, Any]]:
        if set_name not in prim.GetVariantSets().GetNames():
            return f"{set_name} is not listed on {path} after authoring.", {}
        made = sorted(prim.GetVariantSet(set_name).GetVariantNames())
        if made != sorted(names):
            return f"{set_name} lists {made}, expected {sorted(names)}.", {}
        now = prim.GetVariantSet(set_name).GetVariantSelection() or None
        if selection is not None and now != selection:
            return f"{set_name} selects {now!r} instead of {selection!r}.", {}
        return None, {"variants": made, "selection": now}

    return _apply(action, stage, [(target, [path], [])], mutate, verify, undo_stack,
                  f"create variant set {set_name} on {path}", warnings, explanation.replace("would get", "got"),
                  {"plan": plan})


def set_selection(
    stage: Any,
    prim_path: str,
    variant_set: str,
    variant: str,
    *,
    layer: str | None = None,
    undo_stack: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a variant selection in one local layer (default: the edit target).

    Says whether that selection will win: a stronger local layer that selects something else keeps winning, and
    the result names it. Use this (rather than the existing scene_variant_set) when the layer matters.
    """
    action = "set_selection"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        guard = _StageGuard(stage)
        prim = _prim(stage, prim_path)
        path = prim.GetPath()
        set_name = _existing_set(stage, prim, variant_set)
        if set_name not in prim.GetVariantSets().GetNames():
            raise _Refusal(f"{set_name} is not composed on {path} right now (it is nested inside a variant that is not "
                           "selected); select its parent variant first.")
        name = _existing_variant(stage, prim, set_name, variant)
        target = _write_layer(stage, layer)
        ranks = _rank_map(stage)
        rank = ranks.get(target.identifier)
        current = _selection(prim, set_name)
        opinions = _selection_opinions(stage, prim, set_name)
        stronger = [op for op in opinions if op["local"] and op["rank"] is not None and rank is not None
                    and op["rank"] < rank and op["value"] != name]
        existing = target.GetPrimAtPath(path)
        already = bool(existing) and dict(existing.variantSelections).get(set_name) == name
        if stronger:
            warnings.append(
                f"{stronger[0]['layer_short']} is stronger and selects {stronger[0]['value']}, so this selection in "
                f"{_short(target)} will not win. Author it there instead (layer={stronger[0]['layer_short']!r})."
            )
        if target is stage.GetSessionLayer():
            warnings.append("The session layer is never saved with the scene.")
        will_win = not stronger
        plan = {"prim_path": str(path), "variant_set": set_name, "variant": name, "layer": _text(target.identifier),
                "layer_short": _short(target), "current_selection": current, "will_win": will_win,
                "already_authored": already}
        explanation = (
            f"{_short(target)} would select {set_name}={name} on {path}"
            + (" (it already does, so nothing changes)." if already else ".")
            + ("" if will_win else f" A stronger layer still wins with {stronger[0]['value']}.")
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    if dry:
        guard.check(warnings)
        return _result(action, dry=True, warnings=warnings, explanation=explanation, plan=plan)
    if already:
        return _result(action, warnings=warnings, explanation=explanation, plan=plan, selection=current)

    def mutate() -> None:
        spec = Sdf.CreatePrimInLayer(target, path)
        spec.variantSelections[set_name] = name

    def verify() -> tuple[str | None, dict[str, Any]]:
        spec = target.GetPrimAtPath(path)
        if not spec or dict(spec.variantSelections).get(set_name) != name:
            return f"The selection was not authored in {_short(target)}.", {}
        now = _selection(prim, set_name)
        if will_win and now != name:
            return f"{set_name} still resolves to {now!r} instead of {name!r}.", {}
        return None, {"selection": now}

    return _apply(action, stage, [(target, [], [path])], mutate, verify, undo_stack,
                  f"select {set_name}={name} on {path}", warnings, explanation.replace("would select", "selects"),
                  {"plan": plan})


# --- rename / remove ----------------------------------------------------------------------------------------


def _rename_or_remove(
    action: str, stage: Any, prim_path: str, variant_set: str, old: str, new: str | None, switch_to: str | None,
    undo_stack: Any, dry_run: Any,
) -> dict[str, Any]:
    dry = _dry(dry_run)
    warnings: list[str] = []
    renaming = action == "rename_variant"
    try:
        _ready(stage)
        guard = _StageGuard(stage)
        prim = _prim(stage, prim_path)
        path = prim.GetPath()
        set_name = _existing_set(stage, prim, variant_set)
        name = _existing_variant(stage, prim, set_name, old)
        target_name = _new_variant_name(stage, prim, set_name, new) if renaming else None
        all_names = _variant_names(stage, prim, set_name)
        switch = None
        if switch_to is not None:
            switch = str(switch_to).strip()
            allowed = set(all_names) - {name} | ({target_name} if renaming else set())
            if switch not in allowed:
                raise _Refusal(f"switch_to must be one of {sorted(allowed)}, got {switch_to!r}.")
        content = _content(stage, prim, set_name, name)
        sites = content["sites"]
        remote = [site for site in sites if not site.local]
        if remote:
            raise _Refusal(
                f"{set_name}={name} is also defined in {', '.join(_short(site.layer) for site in remote)}, which this stage "
                "brings in through a reference or payload. Changing only the local copy would leave it there; edit that "
                "asset instead."
            )
        if not sites:
            raise _Refusal(f"No layer holds a variant spec for {set_name}={name}.")
        for site in sites:
            _require_editable(site.layer)
        current = _selection(prim, set_name)
        if current == name and switch is None:
            hint = f"switch_to={target_name!r} to keep it selected under its new name" if renaming else "switch_to='<another variant>'"
            raise _Refusal(f"{name} is the current selection of {set_name} on {path}. Pass {hint}.")
        replacement = switch or target_name
        opinions = _selection_opinions(stage, prim, set_name)
        rewrites = []
        for op in opinions:
            if op["value"] != name:
                continue
            if not op["local"]:
                warnings.append(f"{op['layer_short']} (not a local layer) selects {name}; it cannot be rewritten here.")
                continue
            if replacement is None:
                warnings.append(f"{op['layer_short']} selects {name} (a weaker opinion); it will point to a missing variant. "
                                "Pass switch_to to rewrite it.")
                continue
            if not op["layer"].permissionToEdit:
                raise _Refusal(f"{op['layer_short']} selects {name} but is read-only, so the selection cannot be rewritten.")
            rewrites.append(op)
        if current == name and replacement is not None and not any(op["local"] and op["is_winner"] for op in rewrites):
            # The current selection comes from a fallback or a non-local layer: author the switch in the first site.
            rewrites.append({"layer": sites[0].layer, "layer_short": _short(sites[0].layer),
                             "spec_path": sites[0].owner, "value": name, "local": True, "is_winner": True, "new": True})
        if not renaming and len(all_names) == 1:
            warnings.append(f"{name} is the only variant of {set_name}; the set will be left with no variants.")
        plan = {
            "prim_path": str(path),
            "variant_set": set_name,
            "variant": name,
            "new_name": target_name,
            "layers": [{"layer": _text(site.layer.identifier), "layer_short": _short(site.layer), "spec_path": str(site.path)}
                       for site in sites],
            "selection_rewrites": [{"layer_short": op["layer_short"], "spec_path": str(op["spec_path"]),
                                    "from": name, "to": replacement} for op in rewrites],
            "current_selection": current,
        }
        verb = f"renamed to {target_name}" if renaming else "removed"
        explanation = (
            f"{set_name}={name} on {path} would be {verb} in {', '.join(_short(site.layer) for site in sites)}"
            + (f"; {len(rewrites)} selection(s) would change to {replacement}" if rewrites else "") + "."
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    if dry:
        guard.check(warnings)
        return _result(action, dry=True, warnings=warnings, explanation=explanation, plan=plan)

    before_opinions = content["opinions"]

    def mutate() -> None:
        for site in sites:
            owner = site.layer.GetObjectAtPath(site.owner)
            if renaming:
                dst = _sibling_variant(site.path, set_name, target_name)
                if not Sdf.CopySpec(site.layer, site.path, site.layer, dst):
                    raise RuntimeError(f"Sdf.CopySpec could not copy {site.path} to {dst}")
            vset = owner.variantSets[set_name]
            vset.RemoveVariant(vset.variants[name])
        for op in rewrites:
            spec = op["layer"].GetObjectAtPath(op["spec_path"]) or Sdf.CreatePrimInLayer(op["layer"], op["spec_path"])
            spec.variantSelections[set_name] = replacement

    def verify() -> tuple[str | None, dict[str, Any]]:
        names = list(prim.GetVariantSet(set_name).GetVariantNames()) if set_name in prim.GetVariantSets().GetNames() else []
        if name in names:
            return f"{name} is still listed in {set_name}.", {}
        info: dict[str, Any] = {"variants": names, "selection": _selection(prim, set_name)}
        if renaming:
            if target_name not in names:
                return f"{target_name} is not listed in {set_name}.", {}
            moved = _content(stage, prim, set_name, target_name)["opinions"]
            if _authored_diff(before_opinions, moved, name, target_name):
                return f"{target_name} does not hold exactly what {name} held.", {}
        return None, info

    plan_layers = _unique_layers([site.layer for site in sites] + [op["layer"] for op in rewrites])
    entries = [(item, [path], []) for item in plan_layers]
    label = f"rename variant {set_name}={name} to {target_name} on {path}" if renaming else f"remove variant {set_name}={name} on {path}"
    return _apply(action, stage, entries, mutate, verify, undo_stack, label, warnings,
                  explanation.replace("would be", "was").replace("would change", "changed"), {"plan": plan})


# --- the masked probe stage ---------------------------------------------------------------------------------


class _Probe:
    """A population-masked copy of the stage (same layers, own anonymous session layer) to switch variants in."""

    def __init__(self, stage: Any, prim_path: Any) -> None:
        self.source = stage
        self.layer = Sdf.Layer.CreateAnonymous(PROBE_TAG)
        session = stage.GetSessionLayer()
        if session is not None:
            self.layer.subLayerPaths.append(session.identifier)
        mask = Usd.StagePopulationMask([prim_path])
        self.stage = Usd.Stage.OpenMasked(stage.GetRootLayer(), self.layer, stage.GetPathResolverContext(), mask,
                                          Usd.Stage.LoadNone)
        try:
            self.stage.SetLoadRules(stage.GetLoadRules())
        except Exception:  # noqa: BLE001 - fall back to the stage's current load set
            loads = [item for item in stage.GetLoadSet() if item.HasPrefix(prim_path) or prim_path.HasPrefix(item)]
            if loads:
                self.stage.LoadAndUnload(set(loads), set(), Usd.LoadWithoutDescendants)
        muted = list(stage.GetMutedLayers())
        if muted:
            self.stage.MuteAndUnmuteLayers(muted, [])

    def select(self, prim_path: Any, selections: dict[str, str]) -> Any:
        with Sdf.ChangeBlock():
            spec = self.layer.GetPrimAtPath(prim_path) or Sdf.CreatePrimInLayer(self.layer, prim_path)
            for key in [str(name) for name in spec.variantSelections.keys()]:
                del spec.variantSelections[key]
            for key, value in selections.items():
                spec.variantSelections[key] = value
        return self.stage.GetPrimAtPath(prim_path)

    def state_for(self, prim_path: Any, selections: dict[str, str]) -> tuple[dict[Any, Any], bool]:
        prim = self.select(prim_path, selections)
        return _composed_state(prim)

    def close(self) -> None:
        self.stage = None
        self.layer = None


def _probe_selection(prim: Any, set_name: str, variant: str, content: dict[str, Any]) -> dict[str, str]:
    """The selections to author on the probe so set_name=variant composes (enclosing variants included)."""
    selections: dict[str, str] = {}
    current = dict(prim.GetVariantSets().GetAllVariantSelections())
    sites = content["sites"]
    if sites and not any(all(current.get(key) == value for key, value in site.within) for site in sites):
        selections.update(dict(sites[0].within))
    selections[set_name] = variant
    return selections


def _composed_state(prim: Any) -> tuple[dict[Any, Any], bool]:
    state: dict[Any, Any] = {}
    cut = False
    if not prim or not prim.IsValid():
        return state, cut
    for count, item in enumerate(Usd.PrimRange(prim, Usd.PrimAllPrimsPredicate)):
        if count >= MAX_COMPARE_PRIMS or len(state) >= MAX_COMPARE_VALUES:
            cut = True
            break
        key = str(item.GetPath())
        state[(key, "prim")] = (str(item.GetTypeName()), bool(item.IsActive()))
        for attr in item.GetAttributes():
            name = str(attr.GetPath())
            if attr.HasAuthoredValue():
                state[(name, "default")] = attr.Get()
                samples = attr.GetNumTimeSamples()
                if samples:
                    state[(name, "timeSamples")] = samples
            if attr.HasAuthoredConnections():
                state[(name, "connections")] = [str(target) for target in attr.GetConnections()]
        for rel in item.GetRelationships():
            if rel.HasAuthoredTargets():
                state[(str(rel.GetPath()), "targets")] = [str(target) for target in rel.GetTargets()]
    return state, cut


# --- check_variants internals -------------------------------------------------------------------------------


def _check_prim(stage: Any, prim: Any, issues: list[dict[str, Any]], budget: Any, probes_left: int
                ) -> tuple[dict[str, Any], int, bool]:
    path = prim.GetPath()
    record: dict[str, Any] = {"path": str(path), "variant_sets": []}
    used = 0
    stopped = False

    def add(severity: str, kind: str, text: str, **details: Any) -> None:
        issues.append({"severity": severity, "kind": kind, "prim_path": str(path), "text": _text(text), **details})

    composed_names = list(prim.GetVariantSets().GetNames())
    for item in _unlisted_sets(stage, prim, composed_names):
        add("warning", "unlisted_variant_set",
            f"{item['layer_short']} has a variant set spec '{item['name']}' on {path} that is not listed in variantSets, "
            "so USD ignores it. Delete it or list it (prepend variantSets).", variant_set=item["name"], layer=item["layer"])
    probe = _Probe(stage, path)
    try:
        baseline = _scan(stage, probe, path, probe.select(path, {}))
        for problem in baseline.values():
            add(problem["severity"], problem["kind"], f"With the current selections, {problem['text']}",
                **problem["details"])
        for set_name in _all_set_names(stage, prim):
            selection = _selection(prim, set_name)
            names = _variant_names(stage, prim, set_name)
            set_record: dict[str, Any] = {"name": set_name, "selection": selection, "variants": []}
            record["variant_sets"].append(set_record)
            if set_name in composed_names:
                if selection is None:
                    add("info", "no_selection", f"{set_name} on {path} has no selection, so none of its variants is active.",
                        variant_set=set_name)
                elif selection not in names:
                    add("error", "invalid_selection",
                        f"{set_name} on {path} selects '{selection}', which is not one of its variants {names}.",
                        variant_set=set_name)
                _check_selection_layers(stage, prim, set_name, add)
            states: dict[str, Any] = {}
            for name in names:
                if used >= probes_left or (budget is not None and budget.expired()):
                    stopped = True
                    break
                used += 1
                variant_record = _check_variant(stage, prim, probe, set_name, name, baseline, add)
                states[name] = variant_record.pop("_state")
                set_record["variants"].append(variant_record)
            seen: list[str] = []
            for name, state in states.items():
                twin = next((other for other in seen if _same_state(states[other], state)), None)
                if twin is not None:
                    add("warning", "same_as_other_variant",
                        f"{set_name}={name} composes exactly like {twin}: switching between them changes nothing.",
                        variant_set=set_name, variant=name, same_as=twin)
                    for entry in set_record["variants"]:
                        if entry["name"] == name:
                            entry["same_as"] = twin
                seen.append(name)
            if stopped:
                break
    finally:
        probe.close()
    return record, used, stopped


def _check_variant(stage: Any, prim: Any, probe: _Probe, set_name: str, name: str, baseline: dict[Any, Any], add: Any
                   ) -> dict[str, Any]:
    """Problems that appear with set_name=name selected and not with the current selections (those are reported once)."""
    path = prim.GetPath()
    content = _content(stage, prim, set_name, name)
    entry: dict[str, Any] = {"name": name, "opinion_count": len(content["opinions"]), "problems": 0}

    def flag(severity: str, kind: str, text: str, **details: Any) -> None:
        if severity != "info":
            entry["problems"] += 1
        add(severity, kind, text, variant_set=set_name, variant=name, **details)

    if not content["opinions"] and not content["nested"]:
        flag("warning", "changes_nothing", f"{set_name}={name} on {path} authors nothing, so selecting it changes nothing.")
    probed = probe.select(path, _probe_selection(prim, set_name, name, content))
    state, _cut = _composed_state(probed)
    entry["_state"] = state
    if not probed or not probed.IsValid():
        flag("error", "prim_missing", f"With {set_name}={name}, {path} does not compose at all.")
        return entry
    for key, problem in _scan(stage, probe, path, probed).items():
        if key not in baseline:
            flag(problem["severity"], problem["kind"], f"With {set_name}={name}, {problem['text']}", **problem["details"])
    for hidden in _hidden_opinions(probe, set_name, name, content):
        flag("warning", "hidden_by_stronger_opinion", hidden["text"], attribute=hidden["path"], hidden_by=hidden["hidden_by"])
    return entry


def _scan(stage: Any, probe: _Probe, path: Any, probed: Any) -> dict[Any, dict[str, Any]]:
    """Composition errors, broken material bindings and unresolvable asset paths on the probe, keyed for diffing."""
    found: dict[Any, dict[str, Any]] = {}

    def note(key: Any, severity: str, kind: str, text: str, **details: Any) -> None:
        if len(found) < MAX_ISSUES:
            found.setdefault(key, {"severity": severity, "kind": kind, "text": text, "details": details})

    for error in probe.stage.GetCompositionErrors():
        text = _text(str(error))
        note(("composition", text), "error", "composition_error", f"composition reports: {text}")
    if not probed or not probed.IsValid():
        return found
    for item in Usd.PrimRange(probed, Usd.PrimAllPrimsPredicate):
        for rel in item.GetRelationships():
            rel_name = str(rel.GetName())
            if not rel_name.startswith("material:binding") or rel_name.startswith("material:binding:collection"):
                continue
            for target in rel.GetTargets():
                found_prim = probe.stage.GetPrimAtPath(target) if target.HasPrefix(path) else stage.GetPrimAtPath(target)
                key = ("binding", str(rel.GetPath()), str(target))
                if not found_prim or not found_prim.IsValid():
                    note(key, "error", "missing_material", f"{item.GetPath()} binds {target}, which does not exist.",
                         binding=str(rel.GetPath()), target=str(target))
                elif not found_prim.IsA(UsdShade.Material):
                    note(key, "warning", "binding_not_material",
                         f"{item.GetPath()} binds {target}, which is a {found_prim.GetTypeName() or 'typeless prim'}, "
                         "not a Material.", binding=str(rel.GetPath()), target=str(target))
        for attr in item.GetAttributes():
            if attr.GetTypeName() != Sdf.ValueTypeNames.Asset or not attr.HasAuthoredValue():
                continue
            value = attr.Get()
            authored = str(getattr(value, "path", "") or "") if value is not None else ""
            if not authored or "<UDIM>" in authored or getattr(value, "resolvedPath", ""):
                continue
            key = ("asset", str(attr.GetPath()), authored)
            if authored.lower().endswith(".mdl") and "/" not in authored and "\\" not in authored:
                continue  # a core MDL library name (OmniPBR.mdl): the renderer's MDL search path finds it, not USD
            if "://" in authored and not authored.lower().startswith("file:"):
                note(key, "info", "unverified_asset", f"{attr.GetPath()} points to @{authored}@, a URL this offline check "
                     "cannot open (Kit may still load it).", attribute=str(attr.GetPath()), asset=authored)
                continue
            note(key, "error", "missing_texture", f"{attr.GetPath()} points to @{authored}@, which cannot be found.",
                 attribute=str(attr.GetPath()), asset=authored)
    return found


def _check_selection_layers(stage: Any, prim: Any, set_name: str, add: Any) -> None:
    opinions = _selection_opinions(stage, prim, set_name)
    if len({op["value"] for op in opinions}) > 1:
        winner = opinions[0]
        losers = ", ".join(f"{op['layer_short']} ({op['value']})" for op in opinions[1:4] if op["value"] != winner["value"])
        add("info", "selection_overridden",
            f"{winner['layer_short']} selects {set_name}={winner['value']} and wins over {losers}: a selection authored "
            "in a weaker layer does not show.", variant_set=set_name)


def _hidden_opinions(probe: _Probe, set_name: str, name: str, content: dict[str, Any]) -> list[dict[str, Any]]:
    """Variant attribute values that lose to a stronger opinion outside the variant (with the variant selected)."""
    hidden = []
    for op in content["opinions"]:
        if op["field"] == "targets":
            found = _hidden_targets(probe, set_name, name, op)
            if found is not None:
                hidden.append(found)
            continue
        if op["field"] != "default" or op["kind"] != "attribute":
            continue
        attr = probe.stage.GetAttributeAtPath(op["path"])
        if not attr:
            continue
        for spec in attr.GetPropertyStack(Usd.TimeCode.Default()):
            if not spec.HasDefaultValue() and not spec.layer.GetNumTimeSamplesForPath(spec.path):
                continue
            if _inside_variant(spec.path, set_name, name):
                break
            hidden.append({
                "path": op["path"],
                "hidden_by": {"layer": _text(spec.layer.identifier), "layer_short": _short(spec.layer), "spec_path": str(spec.path)},
                "text": (f"{set_name}={name} sets {op['path']} to {_fmt(op['raw'])}, but {_short(spec.layer)} authors it "
                         f"directly at {spec.path} (outside the variant). Local opinions beat variants, so this "
                         "variant's value never shows; move that opinion into a variant or delete it."),
            })
            break
    return hidden


def _hidden_targets(probe: _Probe, set_name: str, name: str, op: dict[str, Any]) -> dict[str, Any] | None:
    """A relationship (e.g. material:binding) whose variant targets lose to an explicit list outside the variant.

    Prepended/appended targets outside the variant combine with the variant's list, so only an explicit list
    (`rel x = </a>`) in a stronger spec replaces it."""
    rel = probe.stage.GetRelationshipAtPath(op["path"])
    if not rel:
        return None
    for spec in rel.GetPropertyStack(Usd.TimeCode.Default()):
        if _inside_variant(spec.path, set_name, name):
            return None
        targets = spec.targetPathList
        if not targets.isExplicit:
            continue
        what = "binding" if op["kind"] == "material_binding" else "targets"
        return {
            "path": op["path"],
            "hidden_by": {"layer": _text(spec.layer.identifier), "layer_short": _short(spec.layer), "spec_path": str(spec.path)},
            "text": (f"{set_name}={name} sets {op['path']} to {_fmt(op['raw'])}, but {_short(spec.layer)} authors an "
                     f"explicit {what} at {spec.path} (outside the variant). Local opinions beat variants, so this "
                     "variant's targets never show; move that opinion into a variant or delete it."),
        }
    return None


def _hidden_overrides(stage: Any, prim: Any, set_name: str, name: str, content: dict[str, Any],
                      overrides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    probe = _Probe(stage, prim.GetPath())
    try:
        probe.select(prim.GetPath(), _probe_selection(prim, set_name, name, content))
        keys = {item["path"] for item in overrides}
        return [item for item in _hidden_opinions(probe, set_name, name, content) if item["path"] in keys]
    finally:
        probe.close()


def _unlisted_sets(stage: Any, prim: Any, composed_names: list[str]) -> list[dict[str, Any]]:
    found = []
    seen = set()
    for spec in prim.GetPrimStack():
        for name in [str(key) for key in spec.variantSets.keys()]:
            if name in composed_names or (spec.layer.identifier, name) in seen:
                continue
            seen.add((spec.layer.identifier, name))
            found.append({"name": name, "layer": _text(spec.layer.identifier), "layer_short": _short(spec.layer),
                          "spec_path": str(spec.path)})
    return found


# --- sites and opinions -------------------------------------------------------------------------------------


class _Site:
    """One variant spec: layer + its path there, the prim spec that owns the set, and the enclosing selections."""

    __slots__ = ("layer", "path", "owner", "within", "local", "rank", "stripped")

    def __init__(self, layer: Any, path: Any, owner: Any, local: bool, rank: int | None) -> None:
        self.layer = layer
        self.path = path
        self.owner = owner
        self.within = _within(owner)
        self.local = local
        self.rank = rank
        self.stripped = path.StripAllVariantSelections()

    def public(self) -> dict[str, Any]:
        return {
            "layer": _text(self.layer.identifier),
            "layer_short": _short(self.layer),
            "spec_path": str(self.path),
            "local": self.local,
            "strength_rank": self.rank,
            "inside": dict(self.within),
        }


def _within(owner: Any) -> tuple[tuple[str, str], ...]:
    """Variant selections that enclose a set on the same prim, e.g. /P{A=x} -> (('A', 'x'),)."""
    chain: list[tuple[str, str]] = []
    for prefix in owner.GetPrefixes():
        if prefix.IsPrimVariantSelectionPath():
            chain.append(tuple(str(part) for part in prefix.GetVariantSelection()))  # type: ignore[arg-type]
        else:
            chain = []
    return tuple(chain)


def _set_sites(stage: Any, prim: Any, set_name: str | None = None) -> dict[str, dict[str, list[_Site]]]:
    """{set: {variant: [sites strongest first]}} from the composed prim stack plus nested sets in local layers."""
    ranks = _rank_map(stage)
    path = prim.GetPath()
    found: dict[str, dict[str, list[_Site]]] = {}
    seen: set[tuple[str, str]] = set()

    def add(spec: Any, local: bool) -> None:
        for name in [str(key) for key in spec.variantSets.keys()]:
            if set_name is not None and name != set_name:
                continue
            bucket = found.setdefault(name, {})
            for variant_name, variant in spec.variantSets[name].variants.items():
                vpath = variant.primSpec.path
                key = (spec.layer.identifier, str(vpath))
                if key in seen:
                    continue
                seen.add(key)
                bucket.setdefault(str(variant_name), []).append(
                    _Site(spec.layer, vpath, spec.path, local, ranks.get(spec.layer.identifier) if local else None))

    for spec in prim.GetPrimStack():
        local = spec.layer.identifier in ranks and spec.path.StripAllVariantSelections() == path
        add(spec, local)

    def walk(spec: Any, depth: int) -> None:
        if depth > MAX_NESTING:
            return
        add(spec, True)
        for name in [str(key) for key in spec.variantSets.keys()]:
            for variant in spec.variantSets[name].variants.values():
                walk(variant.primSpec, depth + 1)

    for layer in _local_layers(stage):
        spec = layer.GetPrimAtPath(path)
        if spec:
            walk(spec, 0)
    for bucket in found.values():
        for sites in bucket.values():
            sites.sort(key=lambda site: (not site.local, site.rank if site.rank is not None else 10**6))
    return found


def _content(stage: Any, prim: Any, set_name: str, variant: str) -> dict[str, Any]:
    sites = _set_sites(stage, prim, set_name).get(set_name, {}).get(variant, [])
    opinions: list[dict[str, Any]] = []
    nested: list[dict[str, Any]] = []
    public_sites = []
    for site in sites:
        spec = site.layer.GetPrimAtPath(site.path)
        if not spec:
            continue
        mine: list[dict[str, Any]] = []
        _collect(spec, site, prim.GetPath(), variant, mine, True)
        for name in [str(key) for key in spec.variantSets.keys()]:
            nested.append({"name": name, "variants": [str(key) for key in spec.variantSets[name].variants.keys()],
                           "layer_short": _short(site.layer), "spec_path": str(site.path)})
        record = site.public()
        record["opinion_count"] = len(mine)
        public_sites.append(record)
        opinions.extend(mine)
    return {"sites": sites, "public_sites": public_sites, "opinions": opinions, "nested": nested}


def _collect(spec: Any, site: _Site, root: Any, label: str, out: list[dict[str, Any]], is_root: bool) -> None:
    if len(out) >= MAX_OPINIONS:
        return
    prim_path = _map(spec.path, site, root)

    def add(kind: str, path: Any, field: str, raw: Any, text: str, spec_path: Any) -> None:
        out.append({
            "kind": kind, "path": str(path), "field": field, "raw": raw, "value": _json(raw), "text": _text(text),
            "layer": _text(site.layer.identifier), "layer_short": _short(site.layer), "spec_path": str(spec_path),
            "local": site.local,
        })

    if not is_root and spec.specifier != Sdf.SpecifierOver:
        kind_word = "class" if spec.specifier == Sdf.SpecifierClass else "defines"
        type_name = str(spec.typeName or "")
        add("prim", prim_path, "specifier", f"{kind_word} {type_name}".strip(),
            f"{label} {kind_word} {prim_path}" + (f" (a {type_name})" if type_name else ""), spec.path)
    elif is_root and spec.HasInfo("typeName") and spec.typeName:
        add("prim", prim_path, "typeName", str(spec.typeName), f"{label} makes {prim_path} a {spec.typeName}", spec.path)
    for field in _PRIM_FIELDS:
        if spec.HasInfo(field):
            value = spec.GetInfo(field)
            if field == "active":
                text = f"{label} makes {prim_path} " + ("active" if value else "inactive (it disappears from the stage)")
            elif field == "apiSchemas":
                text = f"{label} applies API schemas {', '.join(_list_items(value)) or '(none)'} on {prim_path}"
            else:
                text = f"{label} sets {field} of {prim_path} to {_fmt(value)}"
            add("prim", prim_path, field, value, text, spec.path)
    for attr_name, field, verb in _PRIM_ARC_LISTS:
        items = _list_items(getattr(spec, attr_name))
        if items:
            add("arc", prim_path, field, items, f"{label} {verb} on {prim_path}: {', '.join(items[:4])}", spec.path)
    selections = dict(spec.variantSelections)
    for key, value in selections.items():
        add("selection", prim_path, f"variantSelection:{key}", value, f"{label} selects {key}={value} on {prim_path}",
            spec.path)
    for prop in spec.properties.values():
        prop_path = _map(prop.path, site, root)
        if isinstance(prop, Sdf.AttributeSpec):
            if prop.HasDefaultValue():
                value = prop.default
                add("attribute", prop_path, "default", value, f"{label} sets {prop_path} to {_fmt(value)}", prop.path)
            samples = site.layer.GetNumTimeSamplesForPath(prop.path)
            if samples:
                add("attribute", prop_path, "timeSamples", samples,
                    f"{label} animates {prop_path} with {samples} time sample(s)", prop.path)
            connections = [str(_map_target(item, site, root)) for item in _list_items(prop.connectionPathList, raw=True)]
            if connections:
                add("connection", prop_path, "connections", connections,
                    f"{label} connects {prop_path} to {', '.join(connections[:4])}", prop.path)
            if not prop.HasDefaultValue() and not samples and not connections:
                add("attribute", prop_path, "declared", str(prop.typeName),
                    f"{label} declares {prop_path} ({prop.typeName}) without a value", prop.path)
        else:
            targets = [str(_map_target(item, site, root)) for item in _list_items(prop.targetPathList, raw=True)]
            binding = str(prop.name).startswith("material:binding")
            if binding:
                text = f"{label} binds {prim_path} to material {', '.join(targets) or '(nothing)'}"
                if str(prop.name) != "material:binding":
                    text += f" ({prop.name})"
            else:
                text = f"{label} points {prop_path} at {', '.join(targets[:4]) or '(nothing)'}"
            add("material_binding" if binding else "relationship", prop_path, "targets", targets, text, prop.path)
    for child in spec.nameChildren.values():
        _collect(child, site, root, label, out, False)


def _map(path: Any, site: _Site, root: Any) -> Any:
    return path.StripAllVariantSelections().ReplacePrefix(site.stripped, root)


def _map_target(path: Any, site: _Site, root: Any) -> Any:
    stripped = path.StripAllVariantSelections()
    return stripped.ReplacePrefix(site.stripped, root) if stripped.HasPrefix(site.stripped) else stripped


def _list_items(list_op: Any, *, raw: bool = False) -> list[Any]:
    items: list[Any] = []
    if list_op is None:
        return items
    if isinstance(list_op, (list, tuple)):
        values = list(list_op)
    else:
        values = []
        for field in _LIST_OP_FIELDS:
            try:
                values.extend(list(getattr(list_op, field)))
            except Exception:  # noqa: BLE001 - a list op without that field
                continue
    for value in values:
        if raw:
            items.append(value)
        elif hasattr(value, "assetPath"):
            prim = f"<{value.primPath}>" if not value.primPath.isEmpty else ""
            items.append(f"@{value.assetPath}@{prim}" if value.assetPath else (prim or "(internal)"))
        else:
            items.append(str(value))
    return items


def _authored_diff(first: list[dict[str, Any]], second: list[dict[str, Any]], a: str, b: str) -> list[dict[str, Any]]:
    """Opinions that differ between two variants, keyed by (stage path, field); the strongest site wins per key."""
    def keyed(items: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            out.setdefault((item["path"], item["field"]), item)
        return out

    left, right = keyed(first), keyed(second)
    diffs = []
    for key in sorted(set(left) | set(right)):
        x, y = left.get(key), right.get(key)
        if x is not None and y is not None and _same(x["raw"], y["raw"]):
            continue
        path, field = key
        if x is None:
            text = f"Only {b}: {y['text'].split(' ', 1)[1] if ' ' in y['text'] else y['text']}"
        elif y is None:
            text = f"Only {a}: {x['text'].split(' ', 1)[1] if ' ' in x['text'] else x['text']}"
        else:
            text = f"{path} ({field}): {a} {_fmt(x['raw'])}, {b} {_fmt(y['raw'])}"
        diffs.append({"path": path, "field": field, "a": None if x is None else x["value"],
                      "b": None if y is None else y["value"], "text": _text(text)})
    return diffs


def _value_diffs(diffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in diffs if item["field"] in ("default", "targets", "connections")]


def _state_diff(first: dict[Any, Any], second: dict[Any, Any], a: str, b: str) -> list[dict[str, Any]]:
    diffs = []
    for key in sorted(set(first) | set(second)):
        x = first.get(key, _MISSING)
        y = second.get(key, _MISSING)
        if x is not _MISSING and y is not _MISSING and _same(x, y):
            continue
        path, field = key
        if field == "prim":
            if x is _MISSING:
                text = f"{path} exists only with {b}"
            elif y is _MISSING:
                text = f"{path} exists only with {a}"
            else:
                text = f"{path}: type/active {a} {x}, {b} {y}"
        else:
            shown_x = "(not set)" if x is _MISSING else _fmt(x)
            shown_y = "(not set)" if y is _MISSING else _fmt(y)
            text = f"{path}: {a} {shown_x}, {b} {shown_y}"
        diffs.append({"path": path, "field": field, "a": None if x is _MISSING else _json(x),
                      "b": None if y is _MISSING else _json(y), "text": _text(text)})
    return diffs


class _Missing:
    __slots__ = ()


_MISSING = _Missing()


def _same_state(first: dict[Any, Any], second: dict[Any, Any]) -> bool:
    return first.keys() == second.keys() and all(_same(first[key], second[key]) for key in first)


def _inside_variant(path: Any, set_name: str, name: str) -> bool:
    for prefix in path.GetPrefixes():
        if prefix.IsPrimVariantSelectionPath() and tuple(prefix.GetVariantSelection()) == (set_name, name):
            return True
    return False


# --- prim records for list_variants ------------------------------------------------------------------------


def _prim_record(stage: Any, prim: Any, warnings: list[str]) -> dict[str, Any]:
    composed = list(prim.GetVariantSets().GetNames())
    site_map = _set_sites(stage, prim)
    sets = []
    for set_name in _ordered_names(composed, site_map):
        variants = site_map.get(set_name, {})
        names = list(prim.GetVariantSet(set_name).GetVariantNames()) if set_name in composed else sorted(variants)
        selection = _selection(prim, set_name) if set_name in composed else None
        opinions = _selection_opinions(stage, prim, set_name) if set_name in composed else []
        winner = opinions[0] if opinions else None
        nested_in = next((dict(site.within) for sites in variants.values() for site in sites if site.within), None)
        details = []
        for name in names:
            content = _content(stage, prim, set_name, name)
            details.append({
                "name": name,
                "selected": name == selection,
                "content": content["public_sites"],
                "opinion_count": len(content["opinions"]),
                "changes_nothing": not content["opinions"] and not content["nested"],
                "nested_variant_sets": sorted({item["name"] for item in content["nested"]}),
            })
        source = "authored" if winner else ("fallback" if selection else "none")
        record = {
            "name": set_name,
            "composed": set_name in composed,
            "variants": names,
            "selection": selection,
            "selection_source": source,
            "selection_is_valid": (selection in names) if selection else None,
            "selection_authored_in": _public_selection(winner),
            "other_selection_opinions": [_public_selection(op) for op in opinions[1:5]],
            "nested_inside": nested_in,
            "variant_details": details,
        }
        if selection and selection not in names:
            warnings.append(f"{prim.GetPath()}: {set_name} selects '{selection}', which is not one of {names}.")
        sets.append(record)
    unlisted = _unlisted_sets(stage, prim, composed)
    for item in unlisted:
        warnings.append(f"{prim.GetPath()}: {item['layer_short']} has a variant set spec '{item['name']}' that is not listed "
                        "in variantSets, so USD ignores it.")
    sets = [item for item in sets if item["composed"] or item["nested_inside"]]
    return {"path": str(prim.GetPath()), "type": str(prim.GetTypeName()), "variant_sets": sets,
            "unlisted_variant_set_specs": unlisted}


def _ordered_names(composed: list[str], site_map: dict[str, Any]) -> list[str]:
    return composed + sorted(name for name in site_map if name not in composed)


def _public_selection(op: dict[str, Any] | None) -> dict[str, Any] | None:
    if op is None:
        return None
    return {"layer": _text(op["layer"].identifier), "layer_short": op["layer_short"], "spec_path": str(op["spec_path"]),
            "value": op["value"], "local": op["local"], "strength_rank": op["rank"]}


def _selection_opinions(stage: Any, prim: Any, set_name: str) -> list[dict[str, Any]]:
    """Every authored selection for set_name on the prim, strongest first (the first one wins)."""
    ranks = _rank_map(stage)
    path = prim.GetPath()
    found = []
    for spec in prim.GetPrimStack():
        value = dict(spec.variantSelections).get(set_name)
        if value is None:
            continue
        local = spec.layer.identifier in ranks and spec.path.StripAllVariantSelections() == path
        found.append({"layer": spec.layer, "layer_short": _short(spec.layer), "spec_path": spec.path, "value": str(value),
                      "local": local, "rank": ranks.get(spec.layer.identifier) if local else None,
                      "is_winner": not found})
    return found


def _selection(prim: Any, set_name: str) -> str | None:
    if set_name not in prim.GetVariantSets().GetNames():
        return None
    return prim.GetVariantSet(set_name).GetVariantSelection() or None


def _all_set_names(stage: Any, prim: Any) -> list[str]:
    composed = list(prim.GetVariantSets().GetNames())
    nested = []
    for name, variants in _set_sites(stage, prim).items():
        if name not in composed and any(site.within for sites in variants.values() for site in sites):
            nested.append(name)
    return composed + sorted(nested)


def _variant_names(stage: Any, prim: Any, set_name: str) -> list[str]:
    if set_name in prim.GetVariantSets().GetNames():
        return list(prim.GetVariantSet(set_name).GetVariantNames())
    return sorted(_set_sites(stage, prim, set_name).get(set_name, {}))


def _contents_explanation(prim: Any, set_name: str, name: str, content: dict[str, Any], selection: str | None) -> str:
    count = len(content["opinions"])
    where = ", ".join(sorted({site["layer_short"] for site in content["public_sites"]})) or "no layer"
    head = f"{set_name}={name} on {prim.GetPath()} holds {count} opinion(s) in {where}."
    if not count:
        head = f"{set_name}={name} on {prim.GetPath()} holds no opinions: selecting it changes nothing."
    state = (" It is the current selection, so these opinions are active now (unless a local opinion outside the "
             "variant overrides them)." if selection == name else
             f" It is not selected (current: {selection or 'none'}), so these opinions are inactive until you select it.")
    return head + state


# --- writing internals --------------------------------------------------------------------------------------


def _apply(
    action: str, stage: Any, entries: list[tuple[Any, list[Any], list[Any]]], mutate: Any, verify: Any,
    undo_stack: Any, label: str, warnings: list[str], explanation: str, common: dict[str, Any],
) -> dict[str, Any]:
    """Snapshot each (layer, full paths, shallow paths), mutate, verify; roll every layer back on any failure."""
    from . import undo  # pure pxr

    try:
        snapshots = [undo.SpecSnapshot.capture(layer, full, shallow=shallow) for layer, full, shallow in entries]
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Could not snapshot the layers, so nothing was changed: {exc}",
                       **common)
    try:
        with Sdf.ChangeBlock():
            mutate()
        problem, info = verify()
        if problem:
            raise _Refusal(f"validation failed: {problem}")
    except Exception as exc:  # noqa: BLE001 - every failure rolls back
        problems = []
        for snapshot in snapshots:
            try:
                if not snapshot.matches():
                    snapshot.restore()
            except Exception as restore_exc:  # noqa: BLE001
                problems.append(str(restore_exc))
        rolled_back = not problems and all(snapshot.matches() for snapshot in snapshots)
        note = "every layer was rolled back" if rolled_back else f"the rollback was incomplete ({'; '.join(problems)})"
        return _result(action, warnings=warnings, error=f"Authoring failed ({note}): {exc}", applied=not rolled_back,
                       rolled_back=rolled_back, **common)
    undo_info: dict[str, Any] = {"recorded": False}
    if undo_stack is not None:
        try:
            after = [snapshot.recapture() for snapshot in snapshots]
            paths = sorted({str(path) for _layer, full, shallow in entries for path in list(full) + list(shallow)})
            entry = undo.UndoEntry(label, paths, snapshots, after, stage_ref=undo.LayerRef(stage.GetRootLayer()))
            undo_stack.push(entry)
            undo_info = {"recorded": True, "label": entry.label}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"The edit worked but could not be recorded for undo: {exc}")
    for layer, _full, _shallow in entries:
        if layer is not stage.GetSessionLayer():
            warnings.append(f"{_short(layer)} now has unsaved changes; save it to keep this edit.")
    return _result(action, applied=True, warnings=warnings, explanation=explanation, undo=undo_info, **info, **common)


def _plan_overrides(stage: Any, prim: Any, jobs: list[tuple[_Site, Any]], sites: list[_Site], overrides: Any,
                    warnings: list[str]) -> list[dict[str, Any]]:
    """Type each override; it goes into the copy whose source already authors it (else the strongest copy)."""
    if overrides is None:
        return []
    if not isinstance(overrides, dict):
        raise _Refusal("overrides must be a dict of attribute path -> value, e.g. "
                       "{'Mat/Shader.inputs:diffuse_color_constant': [0.9, 0.4, 0.6]}.")
    if len(overrides) > MAX_OVERRIDES:
        raise _Refusal(f"At most {MAX_OVERRIDES} overrides at once.")
    root = prim.GetPath()
    plan = []
    for key, value in overrides.items():
        path = _override_path(root, key)
        site, dst_layer = next(((item, dst) for item, dst in jobs
                                if item.layer.GetPropertyAtPath(path.ReplacePrefix(root, item.path))), jobs[0])
        spec = site.layer.GetPropertyAtPath(path.ReplacePrefix(root, site.path))
        old = None
        if spec:
            is_rel = not isinstance(spec, Sdf.AttributeSpec)
            type_name = None if is_rel else str(spec.typeName)
            if not is_rel and spec.HasDefaultValue():
                old = spec.default
            elif is_rel:
                old = [str(item) for item in _list_items(spec.targetPathList, raw=True)]
        else:
            type_name, is_rel, old = _type_from_elsewhere(stage, root, path, sites)
            warnings.append(f"{path} is not authored inside the source variant in {_short(site.layer)}; the new variant "
                            "adds a new opinion for it.")
        if is_rel:
            typed = _targets(value, str(path))
            field = "targets"
        else:
            typed = _coerce(type_name, value, str(path))
            field = "default"
        compare = [str(target) for target in typed] if is_rel else typed
        if old is not None and _same(old, compare):
            warnings.append(f"The override for {path} equals the source value, so the new variant does not differ there.")
        plan.append({"path": str(path), "field": field, "relationship": is_rel, "type": type_name, "typed": typed,
                     "compare": compare, "site": site, "layer": dst_layer,
                     "old": old, "prim_path": path.GetPrimPath(), "name": str(path.name)})
    return plan


def _type_from_elsewhere(stage: Any, root: Any, path: Any, sites: list[_Site]) -> tuple[str | None, bool, Any]:
    for site in sites:
        spec = site.layer.GetPropertyAtPath(path.ReplacePrefix(root, site.stripped).ReplacePrefix(site.stripped, site.path))
        if spec:
            if isinstance(spec, Sdf.AttributeSpec):
                return str(spec.typeName), False, spec.default if spec.HasDefaultValue() else None
            return None, True, None
    prop = stage.GetPropertyAtPath(path)
    if prop and prop.IsValid():
        if isinstance(prop, Usd.Relationship):
            return None, True, [str(item) for item in prop.GetTargets()]
        return str(prop.GetTypeName()), False, prop.Get()
    raise _Refusal(f"{path} does not exist on the stage or in the source variant, so its type is unknown. Override "
                   "existing attributes only (check the path with variant_contents).")


def _override_path(root: Any, key: Any) -> Any:
    text = str(key).strip() if key is not None else ""
    if not text or len(text) > MAX_PATH_CHARS or not Sdf.Path.IsValidPathString(text):
        raise _Refusal(f"Override key {key!r} is not a USD property path such as Mat/Shader.inputs:diffuse_color_constant.")
    path = Sdf.Path(text)
    if not path.IsAbsolutePath():
        path = path.MakeAbsolutePath(root)
    if path.ContainsPrimVariantSelection():
        raise _Refusal(f"Override key {text} must be a plain stage path without a variant selection.")
    if not path.IsPropertyPath() or not path.GetPrimPath().HasPrefix(root):
        raise _Refusal(f"Override key {text} must name an attribute on {root} or below it (a variant can only change "
                       "its own prim's subtree).")
    return path


def _coerce(type_name: str | None, value: Any, label: str) -> Any:
    value_type = Sdf.ValueTypeNames.Find(str(type_name or ""))
    if not value_type:
        raise _Refusal(f"{label} has an unknown value type {type_name!r}.")
    _check_plain(value, label, value_type == Sdf.ValueTypeNames.Bool)
    scratch = Sdf.Layer.CreateAnonymous("variant-manager-coerce")
    spec = Sdf.AttributeSpec(Sdf.CreatePrimInLayer(scratch, "/Coerce"), "value", value_type)
    tupled = _tupled(value)
    cls = value_type.type.pythonClass
    candidates: list[Any] = [lambda: tupled]
    if value_type == Sdf.ValueTypeNames.Asset and isinstance(value, str):
        candidates.insert(0, lambda: Sdf.AssetPath(value))
    if cls is not None:
        candidates.append(lambda: cls(tupled))
        if isinstance(tupled, tuple):
            candidates.append(lambda: cls(*_flat(tupled)))
    for make in candidates:
        try:
            spec.default = make()
            return spec.default
        except Exception:  # noqa: BLE001 - try the next conversion
            continue
    raise _Refusal(f"{label}: {value!r} cannot be used as a {type_name} value (for a color3f give three numbers, "
                   "e.g. [0.9, 0.4, 0.6]).")


def _check_plain(value: Any, label: str, allow_bool: bool) -> None:
    if isinstance(value, bool):
        if not allow_bool:
            raise _Refusal(f"{label}: {value!r} is a true/false value, but this attribute is not a bool.")
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise _Refusal(f"{label}: values must be finite numbers, got {value!r}.")
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_plain(item, label, allow_bool)
    elif value is not None and not isinstance(value, (int, float, str)):
        return  # already a pxr value (Gf.Vec3f ...); Sdf checks it


def _tupled(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return tuple(_tupled(item) for item in value)
    return value


def _flat(value: Any) -> list[Any]:
    out: list[Any] = []
    for item in value:
        if isinstance(item, tuple):
            out.extend(_flat(item))
        else:
            out.append(item)
    return out


def _targets(value: Any, label: str) -> list[Any]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)) or not items:
        raise _Refusal(f"{label} is a relationship: give a target path or a list of paths, e.g. '/World/Looks/Pink_Mat'.")
    paths = []
    for item in items:
        text = str(item).strip()
        if not text.startswith("/") or not Sdf.Path.IsValidPathString(text):
            raise _Refusal(f"{label}: target {item!r} must be an absolute path such as /World/Looks/Pink_Mat.")
        paths.append(Sdf.Path(text))
    return paths


def _author_override(layer: Any, item: dict[str, Any], root: Any, dst_root: Any) -> None:
    prim_spec = Sdf.CreatePrimInLayer(layer, item["prim_path"].ReplacePrefix(root, dst_root))
    if item["relationship"]:
        spec = layer.GetRelationshipAtPath(prim_spec.path.AppendProperty(item["name"]))
        if not spec:
            spec = Sdf.RelationshipSpec(prim_spec, item["name"], False)
        spec.targetPathList.ClearEditsAndMakeExplicit()
        for target in item["typed"]:
            spec.targetPathList.explicitItems.append(target)
        return
    spec = layer.GetAttributeAtPath(prim_spec.path.AppendProperty(item["name"]))
    if not spec:
        spec = Sdf.AttributeSpec(prim_spec, item["name"], Sdf.ValueTypeNames.Find(item["type"]))
    spec.default = item["typed"]


def _public_override(item: dict[str, Any]) -> dict[str, Any]:
    return {"path": item["path"], "type": item["type"] or "relationship", "old": _json(item["old"]),
            "layer": _text(item["layer"].identifier), "layer_short": _short(item["layer"]),
            "new": _json(item["typed"]) if not item["relationship"] else [str(t) for t in item["typed"]]}


def _predicted_lines(opinions: list[dict[str, Any]], sites: list[_Site], overrides: list[dict[str, Any]], src: str,
                     new: str) -> list[str]:
    copied = {site.layer.identifier for site in sites}
    by_key = {(item["path"], item["field"]): item for item in overrides}
    lines = []
    for op in opinions:
        if op["layer"] not in {_text(identifier) for identifier in copied}:
            continue
        item = by_key.pop((op["path"], op["field"]), None)
        rest = op["text"].split(" ", 1)[1] if " " in op["text"] else op["text"]
        if item is None:
            lines.append(f"{new} {rest}")
        elif item["relationship"]:
            lines.append(f"{new} points {op['path']} at {', '.join(str(t) for t in item['typed'])} (was {_fmt(op['raw'])} in {src})")
        else:
            lines.append(f"{new} sets {op['path']} to {_fmt(item['typed'])} (was {_fmt(op['raw'])} in {src})")
    for item in by_key.values():
        lines.append(f"{new} sets {item['path']} to {_fmt(item['typed'])} (new opinion)")
    return lines


def _sibling_variant(path: Any, set_name: str, name: str) -> Any:
    return path.GetParentPath().AppendVariantSelection(set_name, name)


def _spec_state(layer: Any, path: Any) -> Any:
    from .undo import _subtree_state  # pure pxr

    return _subtree_state(layer, path)


def _new_variant_name(stage: Any, prim: Any, set_name: str, value: Any) -> str:
    name = _identifier(value, "new_name")
    existing = set(_variant_names(stage, prim, set_name))
    for variants in _set_sites(stage, prim, set_name).values():
        existing.update(variants)
    if name in existing:
        raise _Refusal(f"{set_name} on {prim.GetPath()} already has a variant named {name}.")
    return name


def _write_layer(stage: Any, layer: Any) -> Any:
    if layer is None:
        target = stage.GetEditTarget()
        found = target.GetLayer()
        if found is None:
            raise _Refusal("The stage has no edit target layer.")
        try:
            mapped = not target.GetMapFunction().isIdentity
        except Exception:  # noqa: BLE001 - treat an unreadable map as mapped
            mapped = True
        if mapped:
            raise _Refusal("The edit target maps paths (for example a variant edit context); pass layer= explicitly.")
        if found.identifier not in _rank_map(stage):
            raise _Refusal(f"The edit target {_short(found)} is not in the local layer stack; pass layer=.")
    else:
        found = _find_local_layer(stage, layer)
    _require_editable(found)
    return found


# --- validation and layers ----------------------------------------------------------------------------------


def _ready(stage: Any) -> None:
    _require_pxr()
    if _PXR_IMPORT_ERROR is not None:  # pragma: no cover
        raise _Refusal(f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}")
    _check_stage(stage)
    if not hasattr(stage, "GetRootLayer") or stage.GetRootLayer() is None:
        raise _Refusal("No USD stage is open.")


def _prim(stage: Any, value: Any, label: str = "prim_path") -> Any:
    text = str(value).strip() if value is not None else ""
    if not text.startswith("/") or len(text) > MAX_PATH_CHARS or not Sdf.Path.IsValidPathString(text):
        raise _Refusal(f"{label} must be an absolute USD prim path such as /World/Bag/Looks, got {value!r}")
    path = Sdf.Path(text)
    if path.ContainsPrimVariantSelection():
        raise _Refusal(f"{label} must not contain a variant selection ({{set=variant}}); use the plain prim path.")
    if not path.IsPrimPath():
        raise _Refusal(f"{label} must be a prim path such as /World/Bag/Looks, got {text}")
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        raise _Refusal(f"No prim at {path}. Use list_variants to see which prims have variant sets.")
    return prim


def _identifier(value: Any, label: str) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    if not text or len(text) > MAX_NAME_CHARS or not Sdf.Path.IsValidIdentifier(text):
        raise _Refusal(f"{label} must be a simple name (letters, digits, underscores, not starting with a digit), "
                       f"got {value!r}.")
    return text


def _existing_set(stage: Any, prim: Any, value: Any) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    names = _all_set_names(stage, prim)
    if text not in names:
        raise _Refusal(f"{prim.GetPath()} has no variant set {value!r}; its variant sets are {names or 'none'}.")
    return text


def _existing_variant(stage: Any, prim: Any, set_name: str, value: Any) -> str:
    text = str(value).strip() if isinstance(value, str) else ""
    names = _variant_names(stage, prim, set_name)
    if text not in names:
        raise _Refusal(f"{set_name} on {prim.GetPath()} has no variant {value!r}; its variants are {names or 'none'}.")
    return text


def _local_layers(stage: Any) -> list[Any]:
    return list(stage.GetLayerStack(True))


def _rank_map(stage: Any) -> dict[str, int]:
    return {layer.identifier: index for index, layer in enumerate(_local_layers(stage))}


def _find_local_layer(stage: Any, query: Any) -> Any:
    layers = _local_layers(stage)
    text = str(query).strip() if query is not None else ""
    if not text:
        raise _Refusal("layer must name a layer of the local layer stack.")
    lowered = text.lower()
    if lowered == "root":
        return stage.GetRootLayer()
    if lowered == "session" and stage.GetSessionLayer() is not None:
        return stage.GetSessionLayer()
    norm = _norm(text)
    for layer in layers:
        if layer.identifier == text or _norm(layer.identifier) == norm or (layer.realPath and _norm(layer.realPath) == norm):
            return layer
    by_name = [layer for layer in layers if _short(layer).lower() == lowered]
    if len(by_name) == 1:
        return by_name[0]
    names = ", ".join(_short(layer) for layer in layers)
    if len(by_name) > 1:
        raise _Refusal(f"Several local layers are called {text}; give the full identifier. Local layers: {names}")
    raise _Refusal(f"{text} is not in this stage's local layer stack ({names}). Variants can only be edited in local layers.")


def _require_editable(layer: Any) -> None:
    if not layer.permissionToEdit:
        raise _Refusal(f"{_short(layer)} is read-only (permissionToEdit is False).")


def _unique_layers(layers: list[Any]) -> list[Any]:
    out: list[Any] = []
    for layer in layers:
        if all(layer.identifier != other.identifier for other in out):
            out.append(layer)
    return out


def _norm(text: str) -> str:
    return str(text).replace("\\", "/").rstrip("/").lower()


def _short(layer: Any) -> str:
    identifier = str(getattr(layer, "identifier", layer))
    if identifier.startswith("anon:"):
        tag = identifier.rsplit(":", 1)[-1]
        return f"anonymous layer ({tag})" if tag else "anonymous layer"
    return identifier.replace("\\", "/").rsplit("/", 1)[-1]


# --- values and results -------------------------------------------------------------------------------------


def _same(first: Any, second: Any) -> bool:
    from .undo import _equal  # pure pxr

    return _equal(first, second)


def _json(value: Any) -> Any:
    if isinstance(value, Sdf.Path):
        return str(value)
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, Sdf.Path) for item in value):
        return [str(item) for item in value]
    return to_jsonable(value)


def _num(value: Any) -> str:
    number = float(value)
    if not math.isfinite(number):
        return str(number)
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return f"{round(number, 4):g}"


def _fmt(value: Any) -> str:
    if value is None:
        return "(nothing)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _num(value)
    if isinstance(value, str):
        return repr(value)
    if hasattr(value, "resolvedPath") and hasattr(value, "path"):
        return f"@{value.path}@"
    if isinstance(value, Sdf.Path):
        return str(value)
    type_name = type(value).__name__
    try:
        items = list(value)
    except TypeError:
        return _text(str(value))[:120]
    if "Array" in type_name or len(items) > 4:
        if len(items) > 4:
            return f"[{len(items)} values]"
    if all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in items):
        return "(" + ", ".join(_num(item) for item in items) + ")"
    if all(isinstance(item, (str, Sdf.Path)) for item in items):
        return ", ".join(str(item) for item in items) or "(none)"
    return _text(str(value))[:120]


class _StageGuard:
    """Remembers the open stage's layers (dirty flags), session sublayers and edit target to prove nothing changed."""

    def __init__(self, stage: Any) -> None:
        self.stage = stage
        self.layers = list(stage.GetUsedLayers())
        self.dirty = {layer.identifier: bool(layer.dirty) for layer in self.layers}
        session = stage.GetSessionLayer()
        self.session_sublayers = list(session.subLayerPaths) if session is not None else []
        self.edit_target = stage.GetEditTarget().GetLayer()

    def check(self, warnings: list[str]) -> bool:
        changed = [
            _short(layer) for layer in self.layers
            if not layer.expired and bool(layer.dirty) != self.dirty.get(layer.identifier, False)
        ]
        session = self.stage.GetSessionLayer()
        if session is not None and list(session.subLayerPaths) != self.session_sublayers:
            changed.append("session layer sublayers")
        if self.stage.GetEditTarget().GetLayer() is not self.edit_target:
            changed.append("edit target")
        if changed:
            warnings.append(f"Unexpected: the stage changed during a read-only step ({', '.join(changed[:5])}).")
        return not changed


def _result(
    action: str,
    *,
    dry: bool = False,
    applied: bool = False,
    warnings: list[str] | tuple = (),
    explanation: str = "",
    error: str | None = None,
    read_only: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    unique: list[str] = []
    for warning in warnings:
        text = _text(warning)
        if text not in unique:
            unique.append(text)
    result: dict[str, Any] = {
        "ok": error is None,
        "action": action,
        "dry_run": bool(dry),
        "applied": bool(applied),
        "written": bool(applied),
    }
    if read_only:
        result["read_only"] = True
    if error is not None:
        result["error"] = _text(error)
    result["explanation"] = explanation or (_text(error) if error else "")
    result["warnings"] = unique[:MAX_WARNINGS]
    if len(unique) > MAX_WARNINGS:
        result["warnings_truncated"] = len(unique) - MAX_WARNINGS
    result.update(_strip_raw(extra))
    if error is None and dry and not read_only:
        result["next_step"] = "Dry run: nothing was changed. Call again with dry_run=False to apply it."
    return result


def _strip_raw(value: Any) -> Any:
    """Drop the pxr 'raw' values and objects that internal records carry, so results stay JSON-safe."""
    if isinstance(value, dict):
        return {key: _strip_raw(item) for key, item in value.items() if key not in ("raw", "typed", "compare", "site")}
    if isinstance(value, (list, tuple)):
        return [_strip_raw(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return _json(value)
