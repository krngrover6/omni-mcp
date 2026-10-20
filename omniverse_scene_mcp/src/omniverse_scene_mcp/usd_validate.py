"""Read-only USD validation lab: OpenUSD's UsdValidation plus exam-focused lint checks.

Nothing here authors, fixes or saves. Every public function returns JSON-serializable data.
Objective ids follow the NCP-OUSD study guide numbering (1.x Composition ... 8.x Visualization).
"""

from __future__ import annotations

import contextlib
import math
import re
from collections.abc import Iterator
from typing import Any

try:
    from pxr import Ar, Kind, Pcp, Sdf, Usd, UsdGeom, UsdShade, UsdUtils
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Ar = Kind = Pcp = Sdf = Usd = UsdGeom = UsdShade = UsdUtils = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

PROFILES = ("exam", "usd", "all")
SEVERITIES = ("error", "warning", "info")
MAX_ISSUES_LIMIT = 1000
DEFAULT_MAX_PRIMS = 200_000
_MAX_SPECS = 500_000
_MAX_COMPOSITION_ERRORS = 5000
_MAX_TEXT = 400
_MAX_EXAMPLES = 5

_ABSOLUTE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")
_URI_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]+):")
_TILE_TOKENS = ("<UDIM>", "<UVTILE>")

# code -> (severity, objective ids, fix hint)
_RULES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "missing_default_prim": ("warning", ("1.9", "4.6", "6.3"),
        "Set defaultPrim on the root layer (stage.SetDefaultPrim(stage.GetPrimAtPath('/World'))) so a reference "
        "or payload without a prim path knows which prim to bring in."),
    "invalid_default_prim": ("error", ("1.9", "4.6", "6.3"),
        "Point defaultPrim at an existing root prim with stage.SetDefaultPrim(...), or remove it with stage.ClearDefaultPrim()."),
    "missing_up_axis": ("warning", ("4.1", "4.6", "6.4"),
        "Author it explicitly: UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y) (or z). Without it every consumer "
        "falls back to its own default."),
    "missing_meters_per_unit": ("warning", ("4.1", "4.6", "6.4"),
        "Author it explicitly: UsdGeom.SetStageMetersPerUnit(stage, UsdGeom.LinearUnits.meters). The fallback is 0.01 (centimeters)."),
    "layer_units_mismatch": ("warning", ("4.1", "4.6", "6.4"),
        "USD does not rescale referenced data. Add a corrective scale on the referencing prim (for example 100 for "
        "meters into centimeters) or re-export the asset in the stage's units."),
    "layer_up_axis_mismatch": ("warning", ("4.1", "4.6", "6.4"),
        "USD does not reorient referenced data. Add a corrective rotateX (+90 or -90) on the referencing prim or "
        "re-export the asset with the stage's upAxis."),
    "missing_extent": ("warning", ("4.6", "5.6", "6.4"),
        "Compute and author extent: boundable.GetExtentAttr().Set(UsdGeom.Boundable.ComputeExtentFromPlugins(boundable, time)). "
        "Renderers and bounding-box caches rely on it for culling and framing."),
    "stale_extent": ("warning", ("5.6", "6.4"),
        "Recompute extent whenever points (or size/radius) change with UsdGeom.Boundable.ComputeExtentFromPlugins, "
        "then Set() it at the same time code."),
    "unresolved_asset_path": ("error", ("6.3", "7.7", "1.9"),
        "Check spelling and anchoring: a relative path resolves against the layer that authored it, not the stage. "
        "Fix the path or add the missing file."),
    "asset_failed_to_open": ("error", ("6.3", "4.6"),
        "The path resolves but USD could not read it. Check that the file is valid USD (try usdcat on it) and that "
        "its file format plugin is available."),
    "asset_uri_scheme_unavailable": ("warning", ("3.5", "7.5", "6.3"),
        "A URI asset path needs a resolver registered for its scheme (for example the Omniverse resolver for "
        "omniverse://). Validate where that resolver is installed, or use relative paths."),
    "absolute_asset_path": ("warning", ("1.9", "7.7", "6.3"),
        "Use a path relative to the authoring layer (./textures/wood.png) or a resolver identifier, so the asset "
        "still resolves after it is moved or delivered to another site."),
    "asset_path_backslashes": ("warning", ("7.7",),
        "Use forward slashes in asset paths on every platform; backslashes only work on Windows."),
    "reference_target_missing": ("error", ("6.3", "1.3", "1.9"),
        "Name a prim that exists in the target layer stack (@asset.usda@</Asset>), or set defaultPrim in the target "
        "layer when the arc has no prim path."),
    "composition_error": ("warning", ("6.2", "6.3"),
        "Read the Pcp message, then inspect the arc with Usd.PrimCompositionQuery(prim) or usdview's composition view."),
    "muted_layer": ("info", ("6.2",),
        "This layer is muted on the stage (stage.MuteLayer). Unmute it with stage.UnmuteLayer(identifier) if its "
        "opinions should apply."),
    "unregistered_kind": ("warning", ("3.3", "7.2"),
        "Use a built-in kind (component, group, assembly, subcomponent) or register the custom kind in a plugInfo.json "
        "Kinds section so Kind.Registry knows its base kind."),
    "component_under_component": ("warning", ("7.2", "3.3"),
        "Components are leaf models. Make the inner prim a subcomponent, or make the outer prim a group or assembly."),
    "model_under_component": ("warning", ("7.2", "3.3"),
        "A component cannot contain group or assembly models. Keep groups and assemblies above components, never below."),
    "model_hierarchy_broken": ("warning", ("7.2", "3.3"),
        "Give every ancestor of a model a group-derived kind (group or assembly) up to the root; a model kind under a "
        "prim without one has no effect."),
    "missing_kind_top_level": ("info", ("7.2", "3.3"),
        "Give the asset's root prim a kind (component for a leaf asset, assembly for an aggregate) so model "
        "traversal and pipeline tools recognize it."),
    "gprim_under_assembly": ("warning", ("7.2", "3.3"),
        "Wrap the geometry in a component model (an Xform with kind=component) instead of placing gprims directly in an assembly."),
    "gprim_under_group": ("warning", ("7.2", "3.3"),
        "Wrap the geometry in a component model instead of placing gprims directly in a group."),
    "material_binding_missing_target": ("error", ("8.3", "6.4", "4.6"),
        "Retarget the binding at an existing material, for example UsdShade.MaterialBindingAPI(prim).Bind(material)."),
    "material_binding_not_material": ("error", ("8.3", "6.4"),
        "material:binding must target a Material prim, not a Shader, Scope or Xform. Bind the Material that holds the shader."),
    "material_binding_target_dropped": ("error", ("8.3", "1.9", "6.2"),
        "Targets outside a referenced prim's namespace are dropped. Move the Material under the asset's defaultPrim "
        "(for example /Asset/Looks) and rebind."),
    "material_binding_api_missing": ("warning", ("8.3", "6.4"),
        "Apply the schema before binding: UsdShade.MaterialBindingAPI.Apply(prim).Bind(material) (Bind() alone does not "
        "apply it). UsdShade still resolves bindings on prims without the API, but only as a legacy fallback that warns "
        "('MaterialBindingAPI is not applied'); the usdShadeValidators:MaterialBindingApiAppliedValidator flags it, so "
        "treat the API as required."),
    "instanceable_without_prototype": ("warning", ("1.2", "2.4"),
        "instanceable only takes effect on a prim with a composition arc (reference, payload, inherit, specialize or "
        "variant). Reference the asset, or remove instanceable."),
    "ignored_overrides_under_instance": ("warning", ("1.10", "2.5", "1.8", "6.2"),
        "Opinions below an instance in the local layer stack are ignored. Override on the instance prim itself "
        "(inherited primvars or attributes), use an inherit/class arc, or set instanceable = false on that prim."),
    "time_samples_outside_range": ("info", ("1.4", "6.4"),
        "Extend the stage's startTimeCode/endTimeCode, retime the layer with a layer offset, or drop samples that never play."),
    "missing_time_code_range": ("info", ("1.4",),
        "Author stage.SetStartTimeCode() and stage.SetEndTimeCode() so players and renderers know the animation range."),
    "primvar_invalid_interpolation": ("error", ("5.1", "8.1", "5.5"),
        "Use constant, uniform, varying, vertex or faceVarying, for example primvar.SetInterpolation(UsdGeom.Tokens.vertex)."),
    "primvar_index_out_of_range": ("error", ("5.1", "8.1", "6.4"),
        "Every index must point at an existing element of the value array: fix the indices or add the missing values."),
    "primvar_size_mismatch": ("warning", ("5.1", "8.1", "5.5"),
        "Match the element count to the interpolation: vertex/varying = number of points, uniform = number of faces, "
        "faceVarying = number of face-vertex indices."),
    "mesh_topology_mismatch": ("error", ("5.5", "6.4", "4.6"),
        "sum(faceVertexCounts) must equal len(faceVertexIndices). Rebuild both arrays together when topology changes."),
    "mesh_index_out_of_range": ("error", ("5.5", "6.4", "4.6"),
        "Every faceVertexIndices entry must be a valid index into points. Fix the indices or add the missing points."),
    "invisible_hides_descendants": ("info", ("6.4", "5.5"),
        "visibility=invisible prunes the whole subtree and a child cannot switch itself back on. Keep the parent "
        "inherited and hide only the prims that should disappear."),
}

_USD_ERROR_HINTS = {
    "MissingDefaultPrim": _RULES["missing_default_prim"][2],
    "MissingUpAxisMetadata": _RULES["missing_up_axis"][2],
    "MissingMetersPerUnitMetadata": _RULES["missing_meters_per_unit"][2],
    "UnresolvableDependency": _RULES["unresolved_asset_path"][2],
    "CompositionError": _RULES["composition_error"][2],
}

_USD_OBJECTIVES = (
    ("usdValidation:CompositionErrorTest", ("6.2", "6.3", "4.6")),
    ("usdValidation:StageMetadataChecker", ("1.9", "4.6")),
    ("usdValidation:AttributeTypeMismatch", ("5.2", "6.2", "4.6")),
    ("usdGeomValidators:StageMetadataChecker", ("4.1", "4.6")),
    ("usdGeomValidators:Subset", ("8.3", "4.6")),
    ("usdGeomValidators:EncapsulationChecker", ("7.2", "4.6")),
    ("usdShadeValidators:", ("8.2", "8.3", "4.6")),
    ("usdUtilsValidators:MissingReferenceValidator", ("6.3", "7.7", "4.6")),
    ("usdUtilsValidators:", ("1.9", "4.6")),
)


def exam_rules() -> list[dict[str, Any]]:
    """Every exam-profile check code with its severity, objective ids and fix hint."""
    return [
        {"code": code, "severity": severity, "objective_ids": list(objectives), "fix_hint": hint}
        for code, (severity, objectives, hint) in sorted(_RULES.items())
    ]


def validate(stage: Any, profile: str = "exam", max_issues: int = 200, *, max_prims: int = DEFAULT_MAX_PRIMS) -> dict[str, Any]:
    """Validate an opened Usd.Stage without changing it.

    profile: 'exam' (lint checks tied to exam objectives), 'usd' (OpenUSD's UsdValidation, physics excluded)
    or 'all' (both, physics included). ok is False only when an error-severity issue was found.
    """
    if _PXR_IMPORT_ERROR is not None:
        raise ImportError("usd_validate needs the USD Python bindings (pxr)") from _PXR_IMPORT_ERROR
    profile = _normalize_profile(profile)
    if stage is None or not hasattr(stage, "GetRootLayer"):
        raise ValueError("validate() needs an opened Usd.Stage")
    report = _Report(_clamp(max_issues, 0, MAX_ISSUES_LIMIT))
    with _captured_diagnostics(report.notes, "while validating"):
        if profile in ("exam", "all"):
            _run_exam_checks(stage, report, _clamp(max_prims, 1, 10_000_000))
        if profile in ("usd", "all"):
            _run_usd_validation(stage, report, include_physics=profile == "all")
    if profile == "all":
        report.notes.append("Profile 'all' can report one problem twice, once per source ('exam' lint and 'usd' validators).")
    return report.result(stage, profile)


def validate_file(
    path: str,
    profile: str = "exam",
    max_issues: int = 200,
    *,
    load_payloads: bool = True,
    max_prims: int = DEFAULT_MAX_PRIMS,
) -> dict[str, Any]:
    """Open a saved USD file, validate it and never save it."""
    profile = _normalize_profile(profile)
    if _PXR_IMPORT_ERROR is not None:
        return {"ok": False, "file": path, "error": f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}"}
    open_notes: list[str] = []
    load = Usd.Stage.LoadAll if load_payloads else Usd.Stage.LoadNone
    try:
        with _captured_diagnostics(open_notes, "while opening the stage"):
            stage = Usd.Stage.Open(str(path), load)
    except Exception as exc:  # Tf.ErrorException for unreadable or missing files
        return {"ok": False, "file": path, "error": _clip(f"Could not open the stage: {exc}")}
    if stage is None:
        return {"ok": False, "file": path, "error": "Could not open the stage."}
    result = validate(stage, profile, max_issues, max_prims=max_prims)
    if not load_payloads:
        open_notes.append("Payloads were not loaded, so prims inside payloads were not checked.")
    result["notes"] = open_notes + result["notes"]
    result["file"] = path
    return result


class _Report:
    def __init__(self, max_issues: int) -> None:
        self.max_issues = max_issues
        self.counts = {severity: 0 for severity in SEVERITIES}
        self.kept: dict[str, list[dict[str, Any]]] = {severity: [] for severity in SEVERITIES}
        self.validators_run: list[str] = []
        self.notes: list[str] = []

    def add(
        self,
        code: str,
        path: str,
        message: str,
        *,
        severity: str | None = None,
        source: str = "exam",
        fix_hint: str | None = None,
        objective_ids: tuple[str, ...] | list[str] | None = None,
        **details: Any,
    ) -> None:
        rule = _RULES.get(code)
        severity = severity or (rule[0] if rule else "warning")
        self.counts[severity] += 1
        bucket = self.kept[severity]
        if len(bucket) >= self.max_issues:
            return
        bucket.append({
            "severity": severity,
            "code": code,
            "path": path,
            "message": _clip(message),
            "fix_hint": fix_hint or (rule[2] if rule else ""),
            "objective_ids": list(objective_ids if objective_ids is not None else (rule[1] if rule else ())),
            "source": source,
            "details": _jsonable(details),
        })

    def result(self, stage: Any, profile: str) -> dict[str, Any]:
        issues = [issue for severity in SEVERITIES for issue in self.kept[severity]][: self.max_issues]
        total = sum(self.counts.values())
        notes = list(self.notes)
        if total > len(issues):
            notes.append(f"Showing {len(issues)} of {total} issues (errors first); raise max_issues to see more.")
        notes.append("Read-only: nothing was changed, fixed or saved.")
        return {
            "ok": self.counts["error"] == 0,
            "profile": profile,
            "stage": _layer_name(stage.GetRootLayer(), full=True),
            "usd_version": ".".join(str(part) for part in Usd.GetVersion()),
            "counts": dict(self.counts),
            "total_issues": total,
            "returned": len(issues),
            "truncated": total > len(issues),
            "issues": issues,
            "validators_run": list(self.validators_run),
            "notes": notes,
        }


def _run_exam_checks(stage: Any, report: _Report, max_prims: int) -> None:
    resolver = Ar.GetResolver()
    with Ar.ResolverContextBinder(stage.GetPathResolverContext()):
        _check_stage_metadata(stage, report)
        _check_asset_paths(stage, report, resolver)
        _check_composition_errors(stage, report, resolver)
        _PrimChecks(stage, report).run(max_prims)


def _check_stage_metadata(stage: Any, report: _Report) -> None:
    report.validators_run.append("exam:stage_metadata")
    root = stage.GetRootLayer()
    root_prims = [str(prim.GetPath()) for prim in stage.GetPseudoRoot().GetChildren()][:_MAX_EXAMPLES]
    name = _layer_name(root)
    if not root.HasDefaultPrim():
        report.add("missing_default_prim", "/", f"Root layer {name} has no defaultPrim.", root_prims=root_prims)
    elif not stage.GetDefaultPrim():
        report.add(
            "invalid_default_prim", "/",
            f"defaultPrim '{root.defaultPrim}' in {name} does not name a prim on the stage.",
            default_prim=str(root.defaultPrim), root_prims=root_prims,
        )
    if not stage.HasAuthoredMetadata("upAxis"):
        report.add("missing_up_axis", "/", f"No upAxis is authored; consumers will assume '{UsdGeom.GetFallbackUpAxis()}'.")
    if not stage.HasAuthoredMetadata("metersPerUnit"):
        report.add("missing_meters_per_unit", "/", "No metersPerUnit is authored; consumers will assume 0.01 (centimeters).")


def _check_asset_paths(stage: Any, report: _Report, resolver: Any) -> None:
    report.validators_run.append("exam:asset_paths")
    schemes = {scheme.lower() for scheme in (Ar.GetRegisteredURISchemes() if hasattr(Ar, "GetRegisteredURISchemes") else [])}
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    cache: dict[str, str] = {}
    budget = [_MAX_SPECS]
    tiles = 0
    # Variable-expression asset paths (@`"rock_${VARIANT}.usda"`@) are evaluated before they are checked: the stage's
    # root and session layer expressionVariables override the authoring layer's own.
    expression_type = getattr(Sdf, "VariableExpression", None)
    variables = dict(stage.GetRootLayer().expressionVariables or {})
    session_layer = stage.GetSessionLayer()
    if session_layer is not None:
        variables.update(dict(session_layer.expressionVariables or {}))
    unchecked_expressions = 0

    def note(code: str, layer: Any, usage: str, site: str, authored: str, **extra: Any) -> None:
        entry = found.get((code, layer.identifier, authored))
        if entry is None:
            found[(code, layer.identifier, authored)] = {
                "layer": layer, "usage": usage, "authored": authored, "sites": [site], "occurrences": 1, **extra,
            }
            return
        entry["occurrences"] += 1
        if len(entry["sites"]) < _MAX_EXAMPLES:
            entry["sites"].append(site)

    for layer in _ordered_layers(stage):
        for usage, site, authored in _layer_asset_paths(layer, budget):
            if any(token in authored for token in _TILE_TOKENS):
                tiles += 1
                continue
            target = authored
            if expression_type is not None and expression_type.IsExpression(authored):
                evaluated = None
                result = expression_type(authored).Evaluate({**dict(layer.expressionVariables or {}), **variables})
                if not result.errors and isinstance(result.value, str) and result.value:
                    evaluated = result.value
                if evaluated is None:
                    unchecked_expressions += 1
                    continue
                target = evaluated
            scheme = _URI_SCHEME.match(target)
            if scheme and scheme.group(1).lower() != "file" and scheme.group(1).lower() not in schemes:
                note("asset_uri_scheme_unavailable", layer, usage, site, authored, scheme=scheme.group(1))
                continue
            anchored = _anchor(layer, target)
            if anchored not in cache:
                try:
                    cache[anchored] = str(resolver.Resolve(anchored))
                except Exception:
                    cache[anchored] = ""
            resolved = cache[anchored]
            if not resolved:
                note("unresolved_asset_path", layer, usage, site, authored, anchored=anchored, resolved="")
            if _ABSOLUTE_PATH.match(target) or target.lower().startswith("file:"):
                note("absolute_asset_path", layer, usage, site, authored, resolved=resolved)
            if "\\" in target:
                note("asset_path_backslashes", layer, usage, site, authored, resolved=resolved)

    for (code, _identifier, authored), entry in found.items():
        layer = entry.pop("layer")
        name = _layer_name(layer)
        usage = entry["usage"].capitalize()
        if code == "unresolved_asset_path":
            message = f"{usage} @{authored}@ in {name} does not resolve (anchored as {entry['anchored']})."
        elif code == "asset_uri_scheme_unavailable":
            message = (f"{usage} @{authored}@ in {name} uses the '{entry['scheme']}:' scheme, which has no resolver "
                       "in this USD build, so it was not checked.")
        elif code == "absolute_asset_path":
            message = f"{usage} @{authored}@ in {name} is an absolute path, which breaks when the asset moves."
        else:
            message = f"{usage} @{authored}@ in {name} uses backslashes."
        if entry["occurrences"] > 1:
            message += f" Used {entry['occurrences']} times."
        report.add(code, entry["sites"][0], message, layer=_layer_name(layer, full=True), **entry)
    if tiles:
        report.notes.append(f"{tiles} UDIM/UVTILE texture path(s) were not resolved (tile patterns need per-tile checks).")
    if unchecked_expressions:
        report.notes.append(
            f"{unchecked_expressions} variable-expression asset path(s) could not be evaluated with the stage's "
            "expressionVariables and were not checked."
        )
    if budget[0] <= 0:
        report.notes.append(f"Asset path scan stopped after {_MAX_SPECS} prim specs.")


def _layer_asset_paths(layer: Any, budget: list[int]) -> Iterator[tuple[str, str, str]]:
    for sublayer in layer.subLayerPaths:
        yield "sublayer", "/", str(sublayer)
    stack = list(reversed(layer.pseudoRoot.nameChildren))
    while stack and budget[0] > 0:
        spec = stack.pop()
        budget[0] -= 1
        site = str(spec.path)
        for usage, proxy in (("reference", spec.referenceList), ("payload", spec.payloadList)):
            for item in _list_op_items(proxy):
                if item.assetPath:
                    yield usage, site, item.assetPath
        for attr in spec.attributes:
            if attr.typeName not in (Sdf.ValueTypeNames.Asset, Sdf.ValueTypeNames.AssetArray):
                continue
            values = [attr.default] if attr.HasDefaultValue() else []
            values += [layer.QueryTimeSample(attr.path, time) for time in layer.ListTimeSamplesForPath(attr.path)]
            for value in values:
                for asset in _asset_strings(value):
                    yield "asset attribute", str(attr.path), asset
        if spec.HasInfo("clips"):
            for clip_set in dict(spec.GetInfo("clips") or {}).values():
                clip_set = dict(clip_set)
                for asset in _asset_strings(clip_set.get("assetPaths")) + _asset_strings(clip_set.get("manifestAssetPath")):
                    yield "value clip", site, asset
        for variant_set in spec.variantSets.values():
            for variant in variant_set.variants.values():
                if variant.primSpec:
                    stack.append(variant.primSpec)
        stack.extend(reversed(spec.nameChildren))


def _check_composition_errors(stage: Any, report: _Report, resolver: Any) -> None:
    report.validators_run.append("exam:composition_errors")
    if not hasattr(stage, "GetCompositionErrors"):
        report.notes.append("This USD build has no Usd.Stage.GetCompositionErrors; reference target checks were skipped.")
        return
    seen: set[tuple[str, str]] = set()
    for error in list(stage.GetCompositionErrors())[:_MAX_COMPOSITION_ERRORS]:
        kind = type(error).__name__
        message = str(error).strip()
        site = getattr(error, "rootSite", None)
        path = str(site.path) if site is not None and hasattr(site, "path") else "/"
        hint = None
        if kind == "ErrorUnresolvedPrimPath":
            code = "reference_target_missing"
            if "<defaultPrim>" in message:
                hint = ("The target layer has no defaultPrim. Set one in that layer, or name the prim in the arc "
                        "(@asset.usda@</Asset>).")
        elif kind in ("ErrorInvalidAssetPath", "ErrorInvalidSublayerPath"):
            asset = re.search(r"@([^@]+)@", message)
            if asset is None or not str(resolver.Resolve(asset.group(1))):
                continue  # unresolvable paths are reported, with authored vs resolved, by the asset path scan
            code = "asset_failed_to_open"
        elif kind == "ErrorMutedAssetPath":
            code = "muted_layer"
        else:
            code = "composition_error"
        if (code, message) in seen:
            continue
        seen.add((code, message))
        report.add(code, path, message, fix_hint=hint, pcp_error=kind)


class _PrimChecks:
    """One pass over the composed prims (and each instance prototype once)."""

    def __init__(self, stage: Any, report: _Report) -> None:
        self.stage = stage
        self.report = report
        self.has_range = stage.HasAuthoredTimeCodeRange()
        self.start = stage.GetStartTimeCode()
        self.end = stage.GetEndTimeCode()
        self.stage_units = (UsdGeom.GetStageMetersPerUnit(stage), str(UsdGeom.GetStageUpAxis(stage)))
        self.local_layers = list(stage.GetLayerStack(True))
        self.local_ids = {layer.identifier for layer in self.local_layers}
        self.time = Usd.TimeCode.EarliestTime()
        self.kind_context: dict[Any, tuple[Any, str] | None] = {}
        self.hidden: dict[Any, Any] = {}
        self.hidden_roots: dict[Any, dict[str, Any]] = {}
        self.broken: dict[Any, list[str]] = {}
        self.gprims_in_groups: dict[Any, dict[str, Any]] = {}
        self.roots_with_gprims: dict[str, None] = {}
        self.animated = 0

    def run(self, max_prims: int) -> None:
        self.report.validators_run.extend([
            "exam:model_hierarchy", "exam:extent", "exam:mesh_topology", "exam:primvars", "exam:material_bindings",
            "exam:instancing", "exam:layer_units", "exam:time_samples", "exam:visibility",
        ])
        sources: list[tuple[Any, Any]] = [(None, self.stage.Traverse())]
        for prototype in self.stage.GetPrototypes():
            instances = prototype.GetInstances()
            example = instances[0].GetPath() if instances else prototype.GetPath()
            sources.append(((prototype.GetPath(), example, len(instances)), Usd.PrimRange(prototype)))
        visited = 0
        for prototype, prims in sources:
            for prim in prims:
                if visited >= max_prims:
                    self.report.notes.append(f"Prim checks stopped after {max_prims} prims.")
                    self._finish()
                    return
                visited += 1
                self._visit(prim, prototype)
        self._finish()

    def _add(self, code: str, path: str, message: str, prototype: Any = None, **details: Any) -> None:
        if prototype is not None:
            details.update(prototype=str(prototype[0]), instances=prototype[2])
            message += f" (inside an instance prototype shared by {prototype[2]} instance(s))"
        self.report.add(code, path, message, **details)

    def _display(self, path: Any, prototype: Any) -> str:
        return str(path if prototype is None else path.ReplacePrefix(prototype[0], prototype[1]))

    def _visit(self, prim: Any, prototype: Any) -> None:
        path = prim.GetPath()
        display = self._display(path, prototype)
        parent_context = self.kind_context.get(path.GetParentPath())
        kind = str(Usd.ModelAPI(prim).GetKind() or "")
        self._check_kind(prim, kind, parent_context, display, prototype)
        self.kind_context[path] = (path, kind) if kind else parent_context

        mesh_sizes = None
        if prim.IsA(UsdGeom.Gprim):
            self._check_gprim_placement(path, display, parent_context, prototype)
            self._check_extent(prim, display, prototype)
            if prim.IsA(UsdGeom.Mesh):
                mesh_sizes = self._check_mesh(prim, display, prototype)

        visibility = None
        for attr in prim.GetAuthoredAttributes():
            name = attr.GetName()
            if name == "visibility":
                visibility = str(attr.Get(self.time) or "")
            elif name.startswith("primvars:") and not name.endswith(":indices") and UsdGeom.Primvar.IsPrimvar(attr):
                self._check_primvar(UsdGeom.Primvar(attr), display, mesh_sizes, prototype)
            if attr.GetNumTimeSamples():
                self._check_time_samples(attr, f"{display}.{name}", prototype)
        self._check_visibility(path, display, visibility, prototype)
        self._check_bindings(prim, display, prototype)

        if prim.IsInstanceable() and not prim.IsInstance():
            self._add("instanceable_without_prototype", display,
                      f"{display} is marked instanceable but has no composition arc, so USD does not instance it.", prototype)
        if prototype is None:
            if prim.IsInstance():
                self._check_instance_overrides(prim, display)
            if prim.HasAuthoredReferences() or prim.HasAuthoredPayloads():
                self._check_arc_units(prim, display)

    def _check_kind(self, prim: Any, kind: str, parent_context: Any, display: str, prototype: Any) -> None:
        if not kind:
            return
        if not Kind.Registry.HasKind(kind):
            self._add("unregistered_kind", display, f"{display} has kind '{kind}', which Kind.Registry does not know.", prototype, kind=kind)
            return
        if not Kind.Registry.IsA(kind, Kind.Tokens.model):
            return
        ancestor_path, ancestor_kind = parent_context or (None, "")
        if ancestor_kind and Kind.Registry.HasKind(ancestor_kind) and Kind.Registry.IsA(ancestor_kind, Kind.Tokens.component):
            code = "component_under_component" if Kind.Registry.IsA(kind, Kind.Tokens.component) else "model_under_component"
            ancestor = self._display(ancestor_path, prototype)
            self._add(code, display,
                      f"{display} has kind '{kind}' but its ancestor {ancestor} is a '{ancestor_kind}'; components are leaf models.",
                      prototype, kind=kind, ancestor=ancestor, ancestor_kind=ancestor_kind)
            return
        if prototype is None and not prim.IsModel() and not prim.GetParent().IsPseudoRoot():
            ancestor = prim.GetParent()
            while ancestor and not ancestor.IsPseudoRoot():
                ancestor_kind = str(Usd.ModelAPI(ancestor).GetKind() or "")
                if not (ancestor_kind and Kind.Registry.HasKind(ancestor_kind) and Kind.Registry.IsA(ancestor_kind, Kind.Tokens.group)):
                    self.broken.setdefault(ancestor.GetPath(), []).append(f"{display} (kind={kind})")
                    return
                ancestor = ancestor.GetParent()

    def _check_gprim_placement(self, path: Any, display: str, parent_context: Any, prototype: Any) -> None:
        if prototype is None:
            self.roots_with_gprims.setdefault("/" + str(path).split("/")[1], None)
        if not parent_context:
            return
        ancestor_path, ancestor_kind = parent_context
        if Kind.Registry.HasKind(ancestor_kind) and Kind.Registry.IsA(ancestor_kind, Kind.Tokens.group):
            entry = self.gprims_in_groups.setdefault(ancestor_path, {
                "kind": ancestor_kind, "count": 0, "examples": [], "prototype": prototype,
            })
            entry["count"] += 1
            if len(entry["examples"]) < _MAX_EXAMPLES:
                entry["examples"].append(display)

    def _check_extent(self, prim: Any, display: str, prototype: Any) -> None:
        boundable = UsdGeom.Boundable(prim)
        attr = boundable.GetExtentAttr()
        try:
            computed = UsdGeom.Boundable.ComputeExtentFromPlugins(boundable, self.time)
        except Exception:
            computed = None
        type_name = str(prim.GetTypeName())
        if not attr.HasAuthoredValue():
            fallback = attr.Get(self.time)
            if prim.IsA(UsdGeom.PointBased):
                self._add("missing_extent", display, f"{type_name} {display} has no authored extent.", prototype,
                          computed=_vec_list(computed))
            elif fallback is None or not _extents_close(fallback, computed):
                self._add("missing_extent", display,
                          f"{type_name} {display} has no authored extent and the schema fallback does not match its size.",
                          prototype, fallback=_vec_list(fallback), computed=_vec_list(computed))
            return
        authored = attr.Get(self.time)
        if not _extents_close(authored, computed):
            self._add("stale_extent", display,
                      f"Extent of {type_name} {display} is {_vec_list(authored)} but its geometry bounds are {_vec_list(computed)}.",
                      prototype, authored=_vec_list(authored), computed=_vec_list(computed))

    def _check_mesh(self, prim: Any, display: str, prototype: Any) -> dict[str, int]:
        mesh = UsdGeom.Mesh(prim)
        counts = mesh.GetFaceVertexCountsAttr().Get(self.time)
        indices = mesh.GetFaceVertexIndicesAttr().Get(self.time)
        points = mesh.GetPointsAttr().Get(self.time)
        counts_len = len(counts) if counts is not None else 0
        indices_len = len(indices) if indices is not None else 0
        points_len = len(points) if points is not None else 0
        corners = sum(counts) if counts is not None else 0
        if counts is not None and indices is not None and corners != indices_len:
            self._add("mesh_topology_mismatch", display,
                      f"Mesh {display}: sum(faceVertexCounts) is {corners} but faceVertexIndices has {indices_len} entries.",
                      prototype, face_vertex_counts_sum=corners, face_vertex_indices=indices_len)
        if indices_len and points is not None:
            low, high = min(indices), max(indices)
            if low < 0 or high >= points_len:
                self._add("mesh_index_out_of_range", display,
                          f"Mesh {display}: faceVertexIndices range {low}..{high} but there are only {points_len} points.",
                          prototype, min_index=low, max_index=high, points=points_len)
        return {"vertex": points_len, "varying": points_len, "uniform": counts_len, "faceVarying": indices_len}

    def _check_primvar(self, primvar: Any, display: str, mesh_sizes: dict[str, int] | None, prototype: Any) -> None:
        name = str(primvar.GetPrimvarName())
        site = f"{display}.primvars:{name}"
        interpolation = str(primvar.GetInterpolation())
        if primvar.HasAuthoredInterpolation() and not UsdGeom.Primvar.IsValidInterpolation(interpolation):
            self._add("primvar_invalid_interpolation", site,
                      f"Primvar '{name}' on {display} has interpolation '{interpolation}', which is not a valid interpolation.",
                      prototype, interpolation=interpolation)
            return
        if not primvar.GetTypeName().isArray:
            return
        values = primvar.Get(self.time)
        if values is None:
            return
        element_size = max(1, primvar.GetElementSize())
        elements = len(values) // element_size
        indices = primvar.GetIndices(self.time) if primvar.IsIndexed() else None
        if indices is not None and len(indices):
            low, high = min(indices), max(indices)
            if low < 0 or high >= elements:
                self._add("primvar_index_out_of_range", site,
                          f"Primvar '{name}' on {display} has indices {low}..{high} but only {elements} value(s).",
                          prototype, min_index=low, max_index=high, values=elements)
                return
        if mesh_sizes is None or interpolation not in mesh_sizes:
            return
        expected = mesh_sizes[interpolation]
        actual = len(indices) if indices is not None and len(indices) else elements
        if actual != expected:
            self._add("primvar_size_mismatch", site,
                      f"Primvar '{name}' on {display} is {interpolation} so it needs {expected} element(s), but has {actual}.",
                      prototype, interpolation=interpolation, expected=expected, actual=actual, indexed=indices is not None)

    def _check_time_samples(self, attr: Any, site: str, prototype: Any) -> None:
        self.animated += 1
        if not self.has_range:
            return
        samples = attr.GetTimeSamples()
        if samples and (samples[0] < self.start or samples[-1] > self.end):
            self._add("time_samples_outside_range", site,
                      f"{site} has time samples {samples[0]:g}..{samples[-1]:g} outside the stage range {self.start:g}..{self.end:g}.",
                      prototype, first_sample=samples[0], last_sample=samples[-1], start=self.start, end=self.end)

    def _check_visibility(self, path: Any, display: str, visibility: str | None, prototype: Any) -> None:
        hidden_root = self.hidden.get(path.GetParentPath())
        if hidden_root is not None:
            self.hidden[path] = hidden_root
            entry = self.hidden_roots[hidden_root]
            entry["descendants"] += 1
            if visibility in (UsdGeom.Tokens.inherited, UsdGeom.Tokens.visible) and len(entry["overridden"]) < _MAX_EXAMPLES:
                entry["overridden"].append(display)
        elif visibility == UsdGeom.Tokens.invisible:
            self.hidden[path] = path
            self.hidden_roots[path] = {"display": display, "descendants": 0, "overridden": [], "prototype": prototype}

    def _check_bindings(self, prim: Any, display: str, prototype: Any) -> None:
        relationships = [rel for rel in prim.GetAuthoredRelationships() if rel.GetName().startswith("material:binding")]
        if not relationships:
            return
        if not prim.HasAPI(UsdShade.MaterialBindingAPI):
            self._add("material_binding_api_missing", display,
                      f"{display} authors {relationships[0].GetName()} without applying MaterialBindingAPI.", prototype)
        for rel in relationships:
            name = rel.GetName()
            site = f"{display}.{name}"
            targets = rel.GetTargets()
            if not targets:
                authored = _authored_targets(rel)
                if authored:
                    self._add("material_binding_target_dropped", site,
                              f"{site} authors {authored[:_MAX_EXAMPLES]} but composition drops every target, usually "
                              "because the material sits outside the referenced prim's namespace.",
                              prototype, authored=authored[:_MAX_EXAMPLES])
                continue
            for target in targets[1:] if ":collection:" in name else targets:
                target_prim = None if target.IsPropertyPath() else self.stage.GetPrimAtPath(target)
                shown = self._display(target, prototype) if prototype is not None and target.HasPrefix(prototype[0]) else str(target)
                if target.IsPropertyPath() or (target_prim and not target_prim.IsA(UsdShade.Material)):
                    type_name = "a property" if target_prim is None else f"a {target_prim.GetTypeName() or 'typeless'} prim"
                    self._add("material_binding_not_material", site, f"{site} targets {shown}, which is {type_name}, not a Material.",
                              prototype, target=shown)
                elif not target_prim:
                    self._add("material_binding_missing_target", site, f"{site} targets {shown}, which does not exist.",
                              prototype, target=shown)

    def _check_instance_overrides(self, prim: Any, display: str) -> None:
        ignored: list[str] = []
        total = 0
        for layer in self.local_layers:
            spec = layer.GetPrimAtPath(prim.GetPath())
            if not spec:
                continue
            stack = list(spec.nameChildren)
            while stack:
                child = stack.pop()
                stack.extend(child.nameChildren)
                keys = set(child.ListInfoKeys()) - {"specifier"}
                if child.properties or keys or child.specifier != Sdf.SpecifierOver:
                    total += 1
                    if len(ignored) < _MAX_EXAMPLES:
                        ignored.append(f"{child.path} in {_layer_name(layer)}")
        if total:
            self._add("ignored_overrides_under_instance", display,
                      f"{display} is an instance, so {total} local opinion spec(s) below it are ignored: {', '.join(ignored)}.",
                      ignored_specs=ignored, count=total)

    def _check_arc_units(self, prim: Any, display: str) -> None:
        index = prim.GetPrimIndex()
        root_stack = index.rootNode.layerStack
        stack = list(index.rootNode.children)
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.arcType not in (Pcp.ArcTypeReference, Pcp.ArcTypePayload) or node.IsDueToAncestor():
                continue
            target = node.layerStack.identifier.rootLayer
            if target.identifier in self.local_ids or target.identifier in seen:
                continue
            seen.add(target.identifier)
            if node.parent.layerStack == root_stack:
                parent_mpu, parent_up = self.stage_units
                parent_name = "the stage"
            else:
                parent_layer = node.parent.layerStack.identifier.rootLayer
                if parent_layer.identifier == target.identifier:
                    continue
                parent_mpu = _layer_info(parent_layer, "metersPerUnit")
                parent_up = _layer_info(parent_layer, "upAxis")
                parent_name = _layer_name(parent_layer)
            target_mpu = _layer_info(target, "metersPerUnit")
            target_up = _layer_info(target, "upAxis")
            target_name = _layer_name(target)
            if target_mpu and parent_mpu and not math.isclose(float(target_mpu), float(parent_mpu), rel_tol=1e-6):
                ratio = float(parent_mpu) / float(target_mpu)
                scale = f"{ratio:g}x too large" if ratio > 1 else f"{1 / ratio:g}x too small"
                self._add("layer_units_mismatch", display,
                          f"{display} brings in {target_name} (metersPerUnit={float(target_mpu):g}) under {parent_name} "
                          f"(metersPerUnit={float(parent_mpu):g}); it appears {scale} unless corrected.",
                          layer=target.identifier, layer_meters_per_unit=target_mpu, parent_meters_per_unit=parent_mpu,
                          apparent_scale=ratio)
            if target_up is not None and parent_up is not None and str(target_up) != str(parent_up):
                self._add("layer_up_axis_mismatch", display,
                          f"{display} brings in {target_name} (upAxis={target_up}) under {parent_name} (upAxis={parent_up}).",
                          layer=target.identifier, layer_up_axis=str(target_up), parent_up_axis=str(parent_up))

    def _finish(self) -> None:
        for break_path, examples in self.broken.items():
            kind = str(Usd.ModelAPI(self.stage.GetPrimAtPath(break_path)).GetKind() or "")
            state = f"has kind '{kind}'" if kind else "has no kind"
            self.report.add(
                "model_hierarchy_broken", str(break_path),
                f"{len(examples)} prim(s) below {break_path} author a model kind (e.g. {examples[0]}) but {break_path} "
                f"{state}; it needs group or assembly, otherwise IsModel() is False for them.",
                count=len(examples), examples=examples[:_MAX_EXAMPLES],
            )
        broken_roots = {"/" + str(path).split("/")[1] for path in self.broken}
        for root in self.roots_with_gprims:
            prim = self.stage.GetPrimAtPath(root)
            if prim and not Usd.ModelAPI(prim).GetKind() and root not in broken_roots:
                self.report.add("missing_kind_top_level", root, f"Root prim {root} contains geometry but has no kind.")
        for ancestor_path, entry in self.gprims_in_groups.items():
            code = "gprim_under_assembly" if Kind.Registry.IsA(entry["kind"], Kind.Tokens.assembly) else "gprim_under_group"
            ancestor = self._display(ancestor_path, entry["prototype"])
            self._add(code, ancestor,
                      f"{entry['count']} gprim(s) sit directly in the {entry['kind']} model {ancestor} instead of inside a "
                      f"component (e.g. {entry['examples'][0]}).",
                      entry["prototype"], count=entry["count"], examples=entry["examples"])
        for entry in self.hidden_roots.values():
            if not entry["descendants"]:
                continue
            message = f"{entry['display']} is invisible, which also hides its {entry['descendants']} descendant prim(s)."
            if entry["overridden"]:
                message += f" Descendants that author visible/inherited stay hidden: {', '.join(entry['overridden'])}."
            self._add("invisible_hides_descendants", entry["display"], message, entry["prototype"],
                      descendants=entry["descendants"], descendants_authoring_visibility=entry["overridden"])
        if self.animated and not self.has_range:
            self.report.add("missing_time_code_range", "/",
                            f"{self.animated} attribute(s) have time samples but the stage has no startTimeCode/endTimeCode.",
                            animated_attributes=self.animated)


def _validation_api() -> Any:
    """The module holding ValidationRegistry and ValidationContext, or None on builds without them."""
    try:
        from pxr import UsdValidation
    except ImportError:
        UsdValidation = None
    for namespace in (UsdValidation, Usd):
        if namespace is not None and hasattr(namespace, "ValidationRegistry") and hasattr(namespace, "ValidationContext"):
            return namespace
    return None


def _run_usd_validation(stage: Any, report: _Report, include_physics: bool) -> None:
    api = _validation_api()
    version = ".".join(str(part) for part in Usd.GetVersion())
    if api is None:
        report.notes.append(f"OpenUSD's UsdValidation framework is not available in USD {version}; only exam checks can run.")
        return
    try:
        registry = api.ValidationRegistry()
        names = sorted(
            str(meta.name) for meta in registry.GetAllValidatorMetadata()
            if not meta.isSuite and (include_physics or not _is_physics(meta))
        )
        validators = registry.GetOrLoadValidatorsByName(names)
        errors = api.ValidationContext(validators).Validate(stage)
    except Exception as exc:
        report.notes.append(_clip(f"UsdValidation could not run in USD {version}: {exc}"))
        return
    report.validators_run.extend(names)
    if not include_physics:
        report.notes.append("Physics validators were skipped; use profile='all' to include them.")
    severities = {"Error": "error", "Warn": "warning", "Info": "info"}
    for error in errors:
        try:
            if error.HasNoError():
                continue
            severity = severities.get(str(error.GetType().name))
            if severity is None:
                continue
            validator = error.GetValidator()
            metadata = validator.GetMetadata() if validator else None
            identifier = str(error.GetIdentifier())
            validator_name = str(metadata.name) if metadata else identifier.rsplit(".", 1)[0]
            name = str(error.GetName())
            path, layer = _error_site(error)
            hint = _USD_ERROR_HINTS.get(name) or _first_sentence(str(metadata.doc) if metadata else "")
            details = {"validator": validator_name, "identifier": identifier}
            if layer:
                details["layer"] = layer
            report.add(
                name or "UsdValidationError", path, str(error.GetMessage()), severity=severity, source="usd",
                fix_hint=hint or "See the validator documentation for this error.",
                objective_ids=_usd_objectives(validator_name), **details,
            )
        except Exception as exc:  # one malformed error must not hide the rest
            report.notes.append(_clip(f"Skipped one UsdValidation result: {exc}"))


def _is_physics(meta: Any) -> bool:
    keywords = [str(keyword) for keyword in meta.GetKeywords()]
    return "physics" in str(meta.name).lower() or any("physics" in keyword.lower() for keyword in keywords)


def _usd_objectives(validator_name: str) -> tuple[str, ...]:
    for prefix, objectives in _USD_OBJECTIVES:
        if validator_name.startswith(prefix):
            return objectives
    return ("4.6",)


def _error_site(error: Any) -> tuple[str, str | None]:
    for site in error.GetSites():
        if site.IsProperty():
            return str(site.GetProperty().GetPath()), None
        if site.IsPrim():
            return str(site.GetPrim().GetPath()), None
        layer = site.GetLayer()
        if layer:
            return "/", _layer_name(layer, full=True)
    return "/", None


@contextlib.contextmanager
def _captured_diagnostics(notes: list[str], when: str) -> Iterator[None]:
    """Collect USD warnings into notes instead of letting them spill onto stderr."""
    delegate = None
    if hasattr(UsdUtils, "CoalescingDiagnosticDelegate"):
        try:
            delegate = UsdUtils.CoalescingDiagnosticDelegate()
        except Exception:
            delegate = None
    try:
        yield
    finally:
        if delegate is not None:
            try:
                diagnostics = delegate.TakeUncoalescedDiagnostics()
            except Exception:
                diagnostics = []
            del delegate
            if diagnostics:
                unique = list(dict.fromkeys(_clip(str(item.commentary), 200) for item in diagnostics))
                notes.append(f"USD reported {len(diagnostics)} diagnostic(s) {when}, e.g.: " + " | ".join(unique[:3]))


def _normalize_profile(profile: str) -> str:
    value = (profile or "exam").strip().lower()
    if value not in PROFILES:
        raise ValueError(f"Unknown validation profile '{profile}'. Use one of: {', '.join(PROFILES)}.")
    return value


def _ordered_layers(stage: Any) -> list[Any]:
    layers: list[Any] = []
    seen: set[str] = set()
    for layer in list(stage.GetLayerStack(True)) + sorted(stage.GetUsedLayers(), key=lambda item: item.identifier):
        if layer and layer.identifier not in seen:
            seen.add(layer.identifier)
            layers.append(layer)
    return layers


def _list_op_items(proxy: Any) -> list[Any]:
    items: list[Any] = []
    for name in ("explicitItems", "addedItems", "prependedItems", "appendedItems"):
        items.extend(getattr(proxy, name, None) or [])
    return items


def _authored_targets(rel: Any) -> list[str]:
    """Targets authored across the relationship's property stack, strongest first, honoring deletes."""
    items: list[str] = []
    deleted: set[str] = set()
    for spec in rel.GetPropertyStack(Usd.TimeCode.Default()):
        targets = spec.targetPathList
        if targets.isExplicit:
            items.extend(str(path) for path in targets.explicitItems if str(path) not in deleted)
            break
        deleted.update(str(path) for path in targets.deletedItems)
        for name in ("prependedItems", "appendedItems", "addedItems"):
            items.extend(str(path) for path in getattr(targets, name, []) if str(path) not in deleted)
    return list(dict.fromkeys(items))


def _asset_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    path = getattr(value, "path", None)
    if isinstance(path, str):
        return [path] if path else []
    try:
        return [item.path for item in value if getattr(item, "path", "")]
    except TypeError:
        return []


def _anchor(layer: Any, authored: str) -> str:
    try:
        return str(layer.ComputeAbsolutePath(authored)) or authored
    except Exception:
        return authored


def _layer_info(layer: Any, key: str) -> Any:
    root = layer.pseudoRoot
    return root.GetInfo(key) if root.HasInfo(key) else None


def _layer_name(layer: Any, full: bool = False) -> str:
    identifier = str(layer.identifier)
    if full or layer.anonymous:
        return identifier
    return identifier.replace("\\", "/").rsplit("/", 1)[-1]


def _extents_close(first: Any, second: Any) -> bool:
    if first is None or second is None or len(first) != 2 or len(second) != 2:
        return second is None or len(second) != 2
    for corner_a, corner_b in zip(first, second):
        for a, b in zip(corner_a, corner_b):
            if abs(a - b) > 1e-4 * max(1.0, abs(a), abs(b)):
                return False
    return True


def _vec_list(value: Any) -> list[list[float]] | None:
    if value is None:
        return None
    return [[round(float(component), 6) for component in vector] for vector in value]


def _first_sentence(text: str) -> str:
    text = " ".join(text.split())
    return _clip(text.split(". ")[0].rstrip(".") + ".") if text else ""


def _clip(text: str, limit: int = _MAX_TEXT) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(high, number))


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in list(value)[:_MAX_EXAMPLES * 4]]
    return _clip(str(value), 200)
