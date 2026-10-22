"""Read-only asset health check for assets a beginner brings into a project (CAD, FBX exports, Isaac assets, downloads).

asset_health(stage) and asset_health_file(path) return one JSON-safe report: a 0-100 score and grade, a list of
plain-language checks (each with why it matters, beginner fix steps and the equivalent NVIDIA Asset Validator rule
when Kit has one), the asset's file dependencies (resolved, unresolved, absolute, outside the project), and stats
(prims, meshes, faces, largest meshes, materials, textures, instancing, payloads, layers).

Reuse, not duplication: stage metadata, extents, composition errors, kinds, material-binding targets, primvar
indices, mesh topology and arc unit mismatches come from usd_validate.validate (profile 'exam'); the asset-path walk
reuses usd_validate's layer spec walker. This module adds what an asset intake needs on top: dependency lists,
texture facts (formats, local file sizes, PNG/JPEG pixel sizes, color-space hints), heavy meshes, instancing
opportunities, unbound gprims, materials without a surface output, shading models, names, empty Xforms, deep
hierarchies, static time samples and physics colliders / mass.

Nothing is authored, fixed or saved: pxr reads plus os.stat / a 64 KB header read of local texture files.
Time: deadline (a time.monotonic() value or a usd_insight.TimeBudget), budget_ms, or an ambient
``with usd_insight.TimeBudget(ms):``. usd_validate gets half of the time left, the health pass the rest; a cut-short
run reports truncated / truncated_by_time and only covers what it reached. max_prims caps each prim pass.
Uses only APIs present in USD 25.11 (Kit) and usd-core 26.8.
"""

from __future__ import annotations

import contextlib
import itertools
import math
import os
import re
import struct
import time
from collections import Counter
from typing import Any

from . import usd_validate
from .usd_insight import TimeBudget, resolve_budget

try:
    from pxr import Ar, Sdf, Usd, UsdGeom, UsdShade
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Ar = Sdf = Usd = UsdGeom = UsdShade = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

try:
    from pxr import UsdPhysics
except ImportError:  # pragma: no cover - physics schemas are part of usd-core and Kit
    UsdPhysics = None  # type: ignore[assignment]

DEFAULT_MAX_PRIMS = 50_000
MAX_EXAMPLES = 10
MAX_DEPENDENCY_ITEMS = 100
MAX_SPECS = 500_000
MAX_MESH_READS = 20_000  # meshes whose arrays are read; beyond this mesh stats are counted but not measured
MAX_TEXTURE_FILES = 500  # local texture files stat()ed and header-read
HUGE_MESH_FACES = 500_000
HEAVY_TOTAL_FACES = 10_000_000
COLLIDER_MAX_FACES = 20_000
IDENTICAL_MESH_MIN_COPIES = 10
REFERENCE_MIN_COPIES = 5
DEEP_HIERARCHY = 24
LONG_NAME = 100
LARGE_TEXTURE_PIXELS = 8192
LARGE_TEXTURE_BYTES = 64 * 1024 * 1024
_POINT_SAMPLES = 32
_TIME_SAMPLE_FULL_COMPARE = 16

IMAGE_EXTENSIONS = frozenset(
    {"png", "jpg", "jpeg", "exr", "hdr", "tga", "tif", "tiff", "bmp", "dds", "psd", "gif", "webp", "ktx", "ktx2", "tx"}
)
PORTABLE_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "exr"})  # SupportedFileTypesChecker's image list
USD_EXTENSIONS = frozenset({"usd", "usda", "usdc", "usdz"})
_NON_COLOR_TEXTURE = re.compile(
    r"(normal|nrm|rough|metal|orm|occlusion|(^|[_.\-])ao([_.\-]|$)|height|displac|bump|gloss|opacity|mask)", re.I
)
_HEALTH_PRIM_CHECKS = (
    "heavy_meshes", "instancing_opportunities", "material_bindings", "materials_surface", "shading_models",
    "textures", "normals", "prim_names", "empty_xforms", "hierarchy_depth", "time_samples",
    "physics_colliders", "physics_mass",
)
_SEVERITY_RANK = {"pass": 0, "info": 1, "warn": 2, "fail": 3}

# id -> title, fail penalty, warn penalty, Kit Asset Validator rule (omni.asset_validator 1.20), why, fix steps, MCP phrase
_CHECKS: dict[str, dict[str, Any]] = {
    "missing_files": {
        "title": "Missing files (textures, references, payloads, sublayers)",
        "fail": 25, "warn": 6, "kit": "MissingReferenceChecker",
        "why": "A path that does not resolve loads nothing: a missing reference or payload drops geometry, and a "
               "missing texture renders as a flat or pink surface. URL paths (https://, omniverse://) cannot be "
               "checked offline and fail the same way when no resolver for them is installed.",
        "fix": ["Open the layer named in each example and look at the @path@ it authors.",
                "Copy the missing file next to the asset (keep the folder layout), or correct the path.",
                "Prefer paths relative to the layer that authors them, e.g. @./textures/wood.png@.",
                "Run the health check again until missing_files passes."],
        "mcp": "Ask the router to validate the scene file again after copying the files.",
    },
    "composition_errors": {
        "title": "Composition errors (broken references and arcs)",
        "fail": 15, "warn": 5, "kit": None,
        "why": "USD reported an arc it could not compose, so part of the asset is silently missing on the stage.",
        "fix": ["Read the message: it names the prim and the arc that failed.",
                "If it says <defaultPrim>, set defaultPrim in the referenced file or add the prim path to the "
                "reference (@asset.usda@</Root>).",
                "Re-open the stage and check the prim appears."],
        "mcp": "Ask: 'show the composition of <prim path>' to see every arc on that prim.",
    },
    "absolute_paths": {
        "title": "Absolute file paths",
        "fail": 0, "warn": 6, "kit": "AnchoredAssetPathsChecker",
        "why": "A path like C:/Users/me/asset/wood.png only works on the machine that wrote it. Move the folder or "
               "share it and every such file goes missing.",
        "fix": ["Replace each absolute path with one relative to the layer that authors it (./textures/wood.png).",
                "In Composer: right-click the layer, 'Collect asset' / 'Save with relative paths', or re-export "
                "from your DCC with relative paths enabled."],
        "mcp": None,
    },
    "outside_project": {
        "title": "Files outside the asset's folder",
        "fail": 0, "warn": 4, "kit": None,
        "why": "The asset depends on files outside its own folder, so copying only that folder to a project, a "
               "teammate or a server leaves those files behind.",
        "fix": ["Copy the listed files into the asset folder (for example a textures/ subfolder).",
                "Update the paths to point at the copies, relative to the layer.",
                "Shared libraries (an Isaac/NVIDIA material pack) are fine if every machine has them at the same "
                "relative location."],
        "mcp": None,
    },
    "backslash_paths": {
        "title": "Backslashes in asset paths",
        "fail": 0, "warn": 3, "kit": "PortableAssetPathChecker",
        "why": "Backslashes only work on Windows; Linux render farms and containers will not find the files.",
        "fix": ["Replace '\\' with '/' in the listed asset paths (USD accepts forward slashes on Windows too)."],
        "mcp": None,
    },
    "stage_units": {
        "title": "Units and up axis authored",
        "fail": 0, "warn": 6, "kit": "StageMetadataChecker",
        "why": "Without metersPerUnit and upAxis every tool guesses (USD's fallback is centimeters, Y up), so the "
               "asset can arrive 100x too big or lying on its side. Isaac Sim expects meters and Z up "
               "(Isaac validator profiles: MetersPerUnit1Checker, UpAxisZChecker).",
        "fix": ["Author both on the asset's root layer: UsdGeom.SetStageMetersPerUnit(stage, 1.0) and "
                "UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z).",
                "Set them to what the geometry really uses; changing the number does not rescale points."],
        "mcp": None,
    },
    "units_mismatch": {
        "title": "Unit or up-axis mismatch between files",
        "fail": 0, "warn": 10, "kit": None,
        "why": "USD never rescales or rotates referenced data. A millimeter asset referenced into a meter stage "
               "appears 1000x too large; a Y-up asset in a Z-up stage lies on its back.",
        "fix": ["Add a corrective transform on the prim that references the asset: scale = suggested_scale "
                "(e.g. 0.001 for mm into m), rotateX = 90 for Y-up into Z-up.",
                "Or re-export the asset in the stage's units and up axis.",
                "Sublayer metadata is ignored entirely: only the root layer's units count."],
        "mcp": "Ask: 'set the scale of <prim path> to 0.001' (dry run first).",
    },
    "default_prim": {
        "title": "defaultPrim set (here and in referenced files)",
        "fail": 15, "warn": 6, "kit": "OmniDefaultPrimChecker",
        "why": "defaultPrim tells a reference without a prim path which prim to bring in. A file without one "
               "references as empty unless every arc names the prim explicitly.",
        "fix": ["Open the file and set its defaultPrim to the top prim: stage.SetDefaultPrim(stage.GetPrimAtPath('/Root')).",
                "Keep one top-level prim per asset file so the choice is obvious."],
        "mcp": None,
    },
    "model_hierarchy": {
        "title": "Kind / model hierarchy",
        "fail": 0, "warn": 4, "kit": "KindChecker",
        "why": "kind (component, assembly, group) lets tools treat the asset as one selectable model. A broken "
               "chain (a component inside a component, or a model under a prim without group/assembly) means "
               "model traversal and selection modes skip it.",
        "fix": ["Give the asset root kind = component (a leaf asset) or assembly (an asset made of assets).",
                "Every ancestor of a model must be group or assembly; parts inside a component use subcomponent "
                "or no kind."],
        "mcp": None,
    },
    "extents": {
        "title": "Extents present and up to date",
        "fail": 0, "warn": 5, "kit": "ExtentsChecker",
        "why": "extent is a mesh's precomputed bounding box. Missing or stale extents make framing, culling and "
               "bounds queries wrong (objects vanish at some angles or 'frame selected' zooms to the wrong place).",
        "fix": ["Recompute: mesh.GetExtentAttr().Set(UsdGeom.Boundable.ComputeExtentFromPlugins(mesh, Usd.TimeCode.Default())).",
                "Re-export with 'write extents' enabled in your DCC exporter."],
        "mcp": "Ask: 'show the bounds of <prim path>' to compare authored and computed boxes.",
    },
    "mesh_topology": {
        "title": "Mesh topology valid",
        "fail": 10, "warn": 5, "kit": "ValidateTopologyChecker",
        "why": "faceVertexCounts, faceVertexIndices and points disagree, so renderers drop or garble the mesh.",
        "fix": ["Re-export the mesh from the source tool; the arrays must be written together.",
                "Check sum(faceVertexCounts) == len(faceVertexIndices) and every index < len(points)."],
        "mcp": None,
    },
    "primvars": {
        "title": "Primvars (UVs, colors) sized and indexed correctly",
        "fail": 6, "warn": 4, "kit": "IndexedPrimvarChecker",
        "why": "A primvar whose length or indices do not match its interpolation is ignored or reads garbage, so "
               "textures map wrongly.",
        "fix": ["vertex = one value per point, faceVarying = one per face corner, uniform = one per face.",
                "Re-export UVs from the DCC, or fix the indices array so every index is in range."],
        "mcp": None,
    },
    "normals": {
        "title": "Normals sanity",
        "fail": 0, "warn": 3, "kit": "NormalsExistChecker, NormalsValidChecker",
        "why": "Normals decide smooth vs faceted shading. Wrong-length normals are ignored; normals on a "
               "subdivision mesh are ignored too (USD computes its own), which is why CAD/FBX imports often look "
               "blobby or faceted.",
        "fix": ["Polygon meshes from CAD/FBX: set subdivisionScheme = 'none' and keep the exported normals.",
                "Make the normals count match normals interpolation (vertex = points, faceVarying = face corners)."],
        "mcp": None,
    },
    "heavy_meshes": {
        "title": "Very heavy meshes",
        "fail": 0, "warn": 6, "kit": None,
        "why": f"A single mesh above {HUGE_MESH_FACES:,} faces (or more than {HEAVY_TOTAL_FACES:,} faces in total) "
               "slows loading, selection and physics; CAD tessellation is the usual cause.",
        "fix": ["Re-tessellate the CAD at a coarser tolerance, or decimate in Blender (Decimate modifier).",
                "Keep the heavy mesh for rendering only; use a simple collider for physics."],
        "mcp": None,
    },
    "instancing_opportunities": {
        "title": "Repeated geometry not instanced",
        "fail": 0, "warn": 6, "kit": None,
        "why": "Many copies of the same asset or identical meshes each cost memory and load time. Marking "
               "references instanceable lets USD share one prototype across all copies.",
        "fix": ["If the copies reference the same file: set instanceable = true on each referencing prim.",
                "If they are pasted copies of one mesh: move the mesh to its own file, reference it from each "
                "location and mark those prims instanceable.",
                "Edit per-copy things (transform, material on the instance root) only on the instance prim."],
        "mcp": "Ask: 'make <prim path> instanceable' (dry run first).",
    },
    "instancing_setup": {
        "title": "Instancing set up correctly",
        "fail": 0, "warn": 3, "kit": None,
        "why": "instanceable without a composition arc does nothing, and edits below an instance are ignored.",
        "fix": ["Only mark prims instanceable that reference or payload an asset.",
                "Make per-copy changes on the instance prim itself, or set instanceable = false there."],
        "mcp": None,
    },
    "material_bindings": {
        "title": "Every visible mesh has a material",
        "fail": 10, "warn": 6, "kit": "UsdDanglingMaterialBinding, UsdMaterialBindingApi, MaterialOutOfScopeChecker",
        "why": "An unbound mesh renders with the renderer's default grey; a binding to a missing material or one "
               "outside the referenced asset is silently dropped.",
        "fix": ["Bind a material to the mesh or to a parent Xform (bindings inherit down the tree).",
                "Keep materials inside the asset (e.g. /Asset/Looks) so references keep their bindings.",
                "Apply MaterialBindingAPI before binding: UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)."],
        "mcp": "Ask: 'assign a material to <prim path>'.",
    },
    "materials_surface": {
        "title": "Materials have a surface shader",
        "fail": 0, "warn": 6, "kit": None,
        "why": "A Material whose outputs:surface (or outputs:mdl:surface) connects to nothing renders as if unbound.",
        "fix": ["Connect the material's surface output to its shader: material.CreateSurfaceOutput().ConnectToSource("
                "shader.ConnectableAPI(), 'surface').",
                "Or delete the empty material and bind a working one."],
        "mcp": None,
    },
    "shading_models": {
        "title": "Shading models used (UsdPreviewSurface / MDL / MaterialX)",
        "fail": 0, "warn": 0, "kit": "OmniMaterialUsdPreviewSurfaceChecker",
        "why": "MDL looks best in Omniverse RTX but other tools (Blender, usdview) show only UsdPreviewSurface. "
               "Assets meant to travel should carry a UsdPreviewSurface too.",
        "fix": ["Nothing to fix; this is information. For portability add a UsdPreviewSurface network next to the MDL."],
        "mcp": None,
    },
    "textures": {
        "title": "Texture formats, sizes and color spaces",
        "fail": 0, "warn": 4, "kit": "TextureChecker, SupportedFileTypesChecker, NormalMapTextureChecker",
        "why": "Only PNG, JPEG and EXR load everywhere. Normal, roughness, metallic and AO maps hold data, not "
               "color, and must be read as 'raw'; read as sRGB they shade wrongly. Very large textures cost GPU memory.",
        "fix": ["Convert TGA/TIFF/BMP/DDS/PSD textures to PNG (8-bit) or EXR (HDR) and update the paths.",
                "Set inputs:sourceColorSpace = 'raw' on UsdUVTexture nodes reading normal/roughness/metal/AO maps "
                "(and colorSpace = 'raw' on MDL texture inputs).",
                "Downsize 8K textures to 2K/4K unless the asset is seen close up."],
        "mcp": None,
    },
    "prim_names": {
        "title": "Prim names portable",
        "fail": 0, "warn": 3, "kit": "UnicodeNameChecker",
        "why": "Non-ASCII or very long prim names (common from CAD part numbers) break older tools, scripts and "
               "some Isaac/ROS pipelines.",
        "fix": ["Rename the prims to short ASCII names (letters, digits, underscores).",
                "Rename in the source file before referencing; renaming through a reference needs a relocate."],
        "mcp": "Ask: 'rename <prim path> to <new_name>' (dry run first).",
    },
    "empty_xforms": {
        "title": "Empty Xforms",
        "fail": 0, "warn": 0, "kit": None,
        "why": "Xforms with no children are often leftovers from an exporter (null nodes, empty groups). They do "
               "no harm but clutter the tree.",
        "fix": ["Delete them, or deactivate them, if nothing uses them as a locator."],
        "mcp": None,
    },
    "hierarchy_depth": {
        "title": "Hierarchy depth",
        "fail": 0, "warn": 2, "kit": None,
        "why": f"Trees deeper than {DEEP_HIERARCHY} levels (typical of CAD assemblies) make the stage slow to "
               "traverse and hard to navigate.",
        "fix": ["Flatten intermediate groups that carry no transform or meaning, ideally in the source tool."],
        "mcp": None,
    },
    "time_samples": {
        "title": "Time samples on static content",
        "fail": 0, "warn": 2, "kit": None,
        "why": "Values written as time samples that never change (often one sample at frame 0 from an exporter) "
               "make tools treat the asset as animated, which disables caching and can hide default values.",
        "fix": ["Re-export without animation, or convert each single-sample attribute to a default value.",
                "If it is real animation, author startTimeCode/endTimeCode on the root layer."],
        "mcp": None,
    },
    "physics_colliders": {
        "title": "Colliders on heavy render meshes",
        "fail": 0, "warn": 6, "kit": "ColliderChecker",
        "why": f"Using a render mesh with more than {COLLIDER_MAX_FACES:,} faces as a triangle-mesh collider makes "
               "physics slow, and moving rigid bodies cannot simulate triangle meshes at all (PhysX falls back to a "
               "convex hull).",
        "fix": ["Set physics:approximation on the collider: convexHull (simple parts), convexDecomposition "
                "(concave parts), boundingCube/boundingSphere (cheapest), or meshSimplification (static only).",
                "Or add a separate simple collision mesh (purpose = guide) and remove CollisionAPI from the render mesh."],
        "mcp": None,
    },
    "physics_mass": {
        "title": "Rigid bodies with explicit mass",
        "fail": 0, "warn": 0, "kit": "MassChecker, RigidBodyChecker",
        "why": "A rigid body without MassAPI gets its mass from collider volume times a default density "
               "(1000 kg/m3, water), which is often far from the real part and makes robots and grippers behave oddly.",
        "fix": ["Apply UsdPhysics.MassAPI to the rigid body and set physics:mass (kg), or physics:density."],
        "mcp": None,
    },
}

# phase that did not start (time budget) -> the checks it would have filled
_PHASE_CHECKS = {
    "dependencies": ("missing_files", "absolute_paths", "outside_project", "backslash_paths", "textures"),
    "usd_validate": ("composition_errors", "stage_units", "units_mismatch", "default_prim", "model_hierarchy",
                     "extents", "mesh_topology", "primvars", "instancing_setup"),
    "health_prims": _HEALTH_PRIM_CHECKS,
}

# usd_validate exam code -> (health check id, status)
_VALIDATE_MAP = {
    "missing_default_prim": ("default_prim", "warn"),
    "invalid_default_prim": ("default_prim", "fail"),
    "missing_up_axis": ("stage_units", "warn"),
    "missing_meters_per_unit": ("stage_units", "warn"),
    "layer_units_mismatch": ("units_mismatch", "warn"),
    "layer_up_axis_mismatch": ("units_mismatch", "warn"),
    "missing_extent": ("extents", "warn"),
    "stale_extent": ("extents", "warn"),
    "asset_failed_to_open": ("composition_errors", "fail"),
    "reference_target_missing": ("composition_errors", "fail"),
    "composition_error": ("composition_errors", "warn"),
    "unregistered_kind": ("model_hierarchy", "warn"),
    "component_under_component": ("model_hierarchy", "warn"),
    "model_under_component": ("model_hierarchy", "warn"),
    "model_hierarchy_broken": ("model_hierarchy", "warn"),
    "gprim_under_assembly": ("model_hierarchy", "warn"),
    "gprim_under_group": ("model_hierarchy", "warn"),
    "missing_kind_top_level": ("model_hierarchy", "info"),
    "material_binding_missing_target": ("material_bindings", "fail"),
    "material_binding_not_material": ("material_bindings", "fail"),
    "material_binding_target_dropped": ("material_bindings", "fail"),
    "material_binding_api_missing": ("material_bindings", "warn"),
    "instanceable_without_prototype": ("instancing_setup", "warn"),
    "ignored_overrides_under_instance": ("instancing_setup", "warn"),
    "time_samples_outside_range": ("time_samples", "info"),
    "missing_time_code_range": ("time_samples", "info"),
    "primvar_invalid_interpolation": ("primvars", "fail"),
    "primvar_index_out_of_range": ("primvars", "fail"),
    "primvar_size_mismatch": ("primvars", "warn"),
    "mesh_topology_mismatch": ("mesh_topology", "fail"),
    "mesh_index_out_of_range": ("mesh_topology", "fail"),
}
# asset paths are scanned here (with resolved/outside lists); usd_validate's copies of these are skipped
_VALIDATE_SKIP = frozenset({"unresolved_asset_path", "absolute_asset_path", "asset_path_backslashes",
                            "asset_uri_scheme_unavailable"})


def health_checks() -> list[dict[str, Any]]:
    """Every health check id with its title, Kit Asset Validator equivalent and penalties."""
    return [
        {"id": check_id, "title": spec["title"], "kit_validator_rule": spec["kit"],
         "penalty_fail": spec["fail"], "penalty_warn": spec["warn"]}
        for check_id, spec in _CHECKS.items()
    ]


def asset_health(
    stage: Any,
    *,
    root_path: str | None = None,
    deadline: Any = None,
    max_prims: int = DEFAULT_MAX_PRIMS,
    budget_ms: float | None = None,
    project_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Health-check an opened Usd.Stage (or the subtree at root_path) without changing it.

    deadline: a time.monotonic() value or a usd_insight.TimeBudget; budget_ms and an ambient TimeBudget also apply.
    project_root: folder the asset should be self-contained in (default: the root layer's folder).
    ok is False only when the check could not run (error says why); healthy is False when any check failed.
    """
    if _PXR_IMPORT_ERROR is not None:
        return {"ok": False, "error": f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}"}
    if stage is None or not hasattr(stage, "GetRootLayer"):
        return {"ok": False, "error": "asset_health needs an opened Usd.Stage."}
    try:
        budget = resolve_budget(deadline, budget_ms, now=time.monotonic())
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Invalid time budget: {exc}"}
    max_prims = max(1, min(int(max_prims or DEFAULT_MAX_PRIMS), 10_000_000))
    root = stage.GetPseudoRoot()
    if root_path not in (None, "", "/"):
        try:
            root = stage.GetPrimAtPath(Sdf.Path(str(root_path)))
        except Exception:
            root = None
        if not root:
            return {"ok": False, "error": f"No prim at {root_path!r} on this stage."}
    started = time.perf_counter()
    dirty_before = {layer.identifier for layer in stage.GetUsedLayers() if layer.dirty}
    run = _Run(stage, root, budget, max_prims, project_root)
    with usd_validate._captured_diagnostics(run.notes, "while checking asset health"):
        resolver = Ar.GetResolver()
        with Ar.ResolverContextBinder(stage.GetPathResolverContext()):
            run.scan_dependencies(resolver)
            run.check_root_metadata()
            run.run_validate()
            run.prim_pass()
    dirty_after = {layer.identifier for layer in stage.GetUsedLayers() if layer.dirty}
    if dirty_after - dirty_before:  # pragma: no cover - would be a bug here
        run.notes.append(f"Unexpected: layers became dirty during the check: {sorted(dirty_after - dirty_before)}")
    return run.result(time.perf_counter() - started)


def asset_health_file(
    path: str | os.PathLike[str],
    *,
    root_path: str | None = None,
    deadline: Any = None,
    max_prims: int = DEFAULT_MAX_PRIMS,
    budget_ms: float | None = None,
    project_root: str | os.PathLike[str] | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    allow_outside_workspace: bool = False,
) -> dict[str, Any]:
    """Open a USD file strictly read-only (file_stage.readonly_stage), health-check it and never save it."""
    from . import file_stage

    try:
        resolved = file_stage.resolve_scene_file(
            path, workspace_root=workspace_root, allow_outside_workspace=allow_outside_workspace
        )
        if _PXR_IMPORT_ERROR is not None:
            raise ImportError(f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}")
        # Resolve the time budget before opening so it also counts the time to open the file.
        budget = resolve_budget(deadline, budget_ms, now=time.monotonic())
        open_notes: list[str] = []
        with contextlib.ExitStack() as stack:  # its exit raises ReadOnlyViolation if anything changed
            with usd_validate._captured_diagnostics(open_notes, "while opening the file"):
                stage = stack.enter_context(file_stage.readonly_stage(resolved))
            report = asset_health(stage, root_path=root_path, deadline=budget, max_prims=max_prims,
                                  project_root=project_root)
            del stage
    except (file_stage.SceneFileError, file_stage.ReadOnlyViolation, ImportError, TypeError, ValueError) as exc:
        return {"ok": False, "file": os.fspath(path), "error": str(exc)}
    report["file"] = resolved
    if report.get("ok"):
        report["notes"] = open_notes + report["notes"]
    return report


class _Run:
    """State for one health check: findings per check id, dependencies and stats."""

    def __init__(self, stage: Any, root: Any, budget: TimeBudget | None, max_prims: int, project_root: Any) -> None:
        self.stage = stage
        self.root = root
        self.root_path = root.GetPath()
        self.budget = budget
        self.max_prims = max_prims
        self.notes: list[str] = []
        self.findings: dict[str, dict[str, Any]] = {}
        self.not_run: list[str] = []
        self.stopped: list[str] = []
        self.truncated_by_prims = False
        self.prims_checked = 0
        self.validate_summary: dict[str, Any] = {}
        root_layer = stage.GetRootLayer()
        real = "" if root_layer.anonymous else str(root_layer.realPath or "")
        if project_root is not None:
            self.project_root = os.path.abspath(os.fspath(project_root))
        elif real:
            self.project_root = os.path.dirname(os.path.abspath(real.split("[", 1)[0]))
        else:
            self.project_root = None
        self.deps: dict[str, list[dict[str, Any]]] = {
            "resolved": [], "unresolved": [], "unchecked_urls": [], "absolute_paths": [], "outside_project": [],
            "mdl_search_path": [],
        }
        self.dep_counts: Counter[str] = Counter()
        self.usage_counts: Counter[str] = Counter()
        self.textures: dict[str, dict[str, Any]] = {}  # resolved (or anchored) path -> facts
        self.missing_textures: set[str] = set()
        self.referenced_layers: dict[str, tuple[str, str]] = {}  # resolved USD file -> (authored, anchored)
        self.stats: dict[str, Any] = {}

    # ------------------------------------------------------------------------------------------- findings

    def hit(self, check_id: str, status: str, example: Any = None, count: int = 1, detail: Any = None) -> None:
        entry = self.findings.setdefault(check_id, {"status": "pass", "count": 0, "examples": [], "details": []})
        if _SEVERITY_RANK[status] > _SEVERITY_RANK[entry["status"]]:
            entry["status"] = status
        entry["count"] += count
        if example is not None and len(entry["examples"]) < MAX_EXAMPLES:
            text = _clip(str(example))
            if text not in entry["examples"]:
                entry["examples"].append(text)
        if detail is not None and len(entry["details"]) < MAX_EXAMPLES:
            entry["details"].append(_jsonable(detail))

    def stop_now(self) -> bool:
        return self.budget is not None and self.budget.tick()

    # --------------------------------------------------------------------------------------- dependencies

    def scan_dependencies(self, resolver: Any) -> None:
        if self.budget is not None and self.budget.expired():
            self.not_run.append("dependencies")
            return
        schemes = {scheme.lower() for scheme in (Ar.GetRegisteredURISchemes() if hasattr(Ar, "GetRegisteredURISchemes") else [])}
        expression_type = getattr(Sdf, "VariableExpression", None)
        variables = dict(self.stage.GetRootLayer().expressionVariables or {})
        cache: dict[str, str] = {}
        seen: set[tuple[str, str]] = set()
        spec_budget = [MAX_SPECS]
        unchecked_expressions = 0
        stopped = False
        for layer in usd_validate._ordered_layers(self.stage):
            if stopped:
                break
            for usage, site, authored in usd_validate._layer_asset_paths(layer, spec_budget):
                if self.stop_now():
                    stopped = True
                    break
                key = (layer.identifier, authored)
                if key in seen:
                    continue
                seen.add(key)
                target = authored
                if expression_type is not None and expression_type.IsExpression(authored):
                    result = expression_type(authored).Evaluate({**dict(layer.expressionVariables or {}), **variables})
                    if result.errors or not isinstance(result.value, str) or not result.value:
                        unchecked_expressions += 1
                        continue
                    target = result.value
                kind = _usage_kind(usage, target)
                self.usage_counts[kind] += 1
                record = {"asset": f"@{authored}@", "usage": kind, "layer": usd_validate._layer_name(layer), "site": site}
                scheme = usd_validate._URI_SCHEME.match(target)
                if scheme and scheme.group(1).lower() != "file" and scheme.group(1).lower() not in schemes:
                    self._dep("unchecked_urls", record)
                    self.hit("missing_files", "warn", f"{record['asset']} ({kind}) in {record['layer']}: "
                             f"'{scheme.group(1)}:' paths cannot be resolved offline")
                    continue
                if any(token in target for token in usd_validate._TILE_TOKENS):
                    resolved = self._resolve_tiles(layer, target)
                else:
                    anchored = usd_validate._anchor(layer, target)
                    if anchored not in cache:
                        try:
                            cache[anchored] = str(resolver.Resolve(anchored))
                        except Exception:
                            cache[anchored] = ""
                    resolved = cache[anchored]
                if usd_validate._ABSOLUTE_PATH.match(target) or target.lower().startswith("file:"):
                    self._dep("absolute_paths", dict(record, resolved=resolved))
                    self.hit("absolute_paths", "warn", f"{record['asset']} in {record['layer']}")
                if "\\" in target:
                    self.hit("backslash_paths", "warn", f"{record['asset']} in {record['layer']}")
                if not resolved and kind == "mdl" and not target.startswith(("./", "../")) \
                        and not usd_validate._ABSOLUTE_PATH.match(target):
                    # A bare module name (OmniPBR.mdl, nvidia/core_definitions.mdl) is found through Kit's MDL search
                    # paths, not the file system (Kit rule MaterialPathChecker), so it is not missing.
                    self._dep("mdl_search_path", record)
                    continue
                if not resolved:
                    self._dep("unresolved", record)
                    self.hit("missing_files", "fail", f"{record['asset']} ({kind}) in {record['layer']} at {site}")
                    if kind == "texture":
                        self.missing_textures.add(usd_validate._anchor(layer, target))
                    continue
                self._dep("resolved", dict(record, resolved=_short_path(resolved)))
                local = resolved.split("[", 1)[0]
                if self.project_root and os.path.isabs(local) and not _within(local, self.project_root):
                    self._dep("outside_project", dict(record, resolved=_short_path(resolved)))
                    self.hit("outside_project", "warn", f"{record['asset']} -> {_short_path(resolved)}")
                if kind == "texture":
                    self.textures.setdefault(resolved, {"ext": _extension(target), "site": site,
                                                        "in_package": "[" in resolved, "udim": "<" in target})
                elif kind in ("reference", "payload") and _extension(local) in USD_EXTENSIONS:
                    self.referenced_layers.setdefault(resolved, (authored, usd_validate._anchor(layer, target)))
        if stopped:
            self.stopped.append("dependencies")
        if spec_budget[0] <= 0:
            self.notes.append(f"Dependency scan stopped after {MAX_SPECS} prim specs.")
            self.stopped.append("dependencies")
        if self.dep_counts.get("mdl_search_path"):
            example = self.deps["mdl_search_path"][0]["asset"]
            self.notes.append(f"{self.dep_counts['mdl_search_path']} MDL module path(s) such as {example} come from "
                              "Kit's MDL library (search paths): they load in Omniverse apps but not in other tools.")
        if unchecked_expressions:
            self.notes.append(f"{unchecked_expressions} variable-expression asset path(s) could not be evaluated and were not checked.")

    def _dep(self, bucket: str, record: dict[str, Any]) -> None:
        self.dep_counts[bucket] += 1
        if len(self.deps[bucket]) < MAX_DEPENDENCY_ITEMS:
            self.deps[bucket].append(record)

    def _resolve_tiles(self, layer: Any, target: str) -> str:
        """A UDIM/UVTILE path resolves when at least one tile file exists next to the pattern."""
        anchored = usd_validate._anchor(layer, target)
        folder, name = os.path.split(anchored.split("[", 1)[0])
        pattern = re.escape(name)
        for token in usd_validate._TILE_TOKENS:
            pattern = pattern.replace(re.escape(token), r"[0-9_uv]{4,7}")
        regex = re.compile(f"^{pattern}$", re.I)
        try:
            with os.scandir(folder or ".") as entries:
                for index, entry in enumerate(entries):
                    if index > 20_000:
                        break
                    if regex.match(entry.name):
                        return anchored
        except OSError:
            return ""
        return ""

    # -------------------------------------------------------------------------------------- root metadata

    def check_root_metadata(self) -> None:
        """Sublayer unit mismatches and referenced files without defaultPrim (usd_validate covers the root layer)."""
        stage_mpu = UsdGeom.GetStageMetersPerUnit(self.stage)
        stage_up = str(UsdGeom.GetStageUpAxis(self.stage))
        root_id = self.stage.GetRootLayer().identifier
        for layer in self.stage.GetLayerStack(False):
            if layer.identifier == root_id:
                continue
            mpu = usd_validate._layer_info(layer, "metersPerUnit")
            up = usd_validate._layer_info(layer, "upAxis")
            name = usd_validate._layer_name(layer)
            if mpu and not math.isclose(float(mpu), float(stage_mpu), rel_tol=1e-6):
                factor = float(stage_mpu) / float(mpu)
                self.hit("units_mismatch", "warn",
                         f"Sublayer {name} says metersPerUnit={float(mpu):g} but the stage uses {float(stage_mpu):g}; "
                         f"sublayer units are ignored, so its data appears {_factor_text(factor)}.",
                         detail={"layer": name, "layer_meters_per_unit": float(mpu), "stage_meters_per_unit": float(stage_mpu),
                                 "apparent_scale": factor, "suggested_scale": 1.0 / factor})
            if up is not None and str(up) != stage_up:
                self.hit("units_mismatch", "warn",
                         f"Sublayer {name} says upAxis={up} but the stage is {stage_up}-up; sublayer upAxis is ignored.",
                         detail={"layer": name, "layer_up_axis": str(up), "stage_up_axis": stage_up})
        for resolved, (authored, anchored) in self.referenced_layers.items():
            # Only layers the stage already opened (Find never opens a file).
            layer = Sdf.Layer.Find(anchored) or Sdf.Layer.Find(resolved)
            if not layer or layer.HasDefaultPrim():
                continue
            self.hit("default_prim", "warn", f"Referenced file @{authored}@ has no defaultPrim")

    # ------------------------------------------------------------------------------------- usd_validate

    def run_validate(self) -> None:
        if self.budget is not None and self.budget.expired():
            self.not_run.append("usd_validate")
            return
        sub_budget = TimeBudget(self.budget.remaining_ms() * 0.5) if self.budget is not None else None
        try:
            result = usd_validate.validate(self.stage, "exam", max_issues=usd_validate.MAX_ISSUES_LIMIT,
                                           max_prims=self.max_prims, deadline=sub_budget)
        except Exception as exc:  # one broken lint must not hide the health pass
            self.notes.append(_clip(f"usd_validate could not run: {exc}"))
            self.not_run.append("usd_validate")
            return
        if result.get("truncated_by_time"):
            self.stopped.append("usd_validate")
        if any(note.startswith("Prim checks stopped after") for note in result.get("notes", [])):
            self.truncated_by_prims = True
        self.validate_summary = {"counts": result.get("counts", {}), "validators_run": len(result.get("validators_run", []))}
        unmapped: Counter[str] = Counter()
        for issue in result.get("issues", []):
            code = issue.get("code", "")
            if code in _VALIDATE_SKIP:
                continue
            if not self._in_scope(issue.get("path", "/")):
                continue
            mapping = _VALIDATE_MAP.get(code)
            if mapping is None:
                unmapped[code] += 1
                continue
            check_id, status = mapping
            details = issue.get("details") or {}
            detail = None
            if code == "layer_units_mismatch" and details.get("apparent_scale"):
                scale = float(details["apparent_scale"])
                detail = {"prim": issue.get("path"), "layer": details.get("layer"),
                          "layer_meters_per_unit": details.get("layer_meters_per_unit"),
                          "parent_meters_per_unit": details.get("parent_meters_per_unit"),
                          "apparent_scale": scale, "suggested_scale": 1.0 / scale if scale else None}
            elif code == "layer_up_axis_mismatch":
                detail = {"prim": issue.get("path"), "layer": details.get("layer"),
                          "layer_up_axis": details.get("layer_up_axis"), "parent_up_axis": details.get("parent_up_axis")}
            elif code == "stale_extent":
                detail = {"prim": issue.get("path"), "authored": details.get("authored"), "computed": details.get("computed")}
            example = issue.get("message") if check_id in ("units_mismatch", "composition_errors", "stage_units",
                                                            "default_prim", "model_hierarchy", "time_samples") \
                else issue.get("path")
            self.hit(check_id, status, example, detail=detail)
            if code == "reference_target_missing" and "defaultPrim" in str(issue.get("message", "")):
                self.hit("default_prim", "fail", issue.get("message"))
        if unmapped:
            self.notes.append("usd_validate also reported: " + ", ".join(f"{code} ({n})" for code, n in sorted(unmapped.items())))

    def _in_scope(self, path: str) -> bool:
        if self.root_path == Sdf.Path.absoluteRootPath or path in ("/", ""):
            return True
        root = str(self.root_path)
        return path == root or path.startswith(root + "/") or path.startswith(root + ".")

    # ---------------------------------------------------------------------------------------- prim pass

    def prim_pass(self) -> None:
        stats = {
            "prims": 0, "meshes": 0, "meshes_measured": 0, "gprims": 0, "total_points": 0, "total_faces": 0,
            "total_faces_with_instances": 0, "largest_meshes": [], "materials": 0, "shaders": 0,
            "instanceable": 0, "instances": 0, "prototypes": 0, "payloads": 0, "references": 0, "max_depth": 0,
            "rigid_bodies": 0, "colliders": 0, "sampled": False,
        }
        self.stats = stats
        if self.budget is not None and self.budget.expired():
            self.not_run.append("health_prims")
            return
        sources: list[tuple[Any, Any]] = []
        if self.root.IsPseudoRoot():
            sources.append((None, self.stage.Traverse()))
        else:
            sources.append((None, Usd.PrimRange(self.root)))
        for prototype in self.stage.GetPrototypes():
            instances = [inst for inst in prototype.GetInstances() if inst.GetPath().HasPrefix(self.root_path)]
            if instances:
                sources.append(((prototype.GetPath(), instances[0].GetPath(), len(instances)), Usd.PrimRange(prototype)))
        stats["prototypes"] = len(sources) - 1
        largest: list[tuple[int, int, str]] = []
        identical: dict[tuple, list[str]] = {}
        same_ref: dict[tuple, list[str]] = {}
        rigid_cache: dict[Any, bool] = {}
        shading = Counter()
        visited = 0
        stop = None
        for prototype, prims in sources:
            for prim in prims:
                if visited >= self.max_prims:
                    stop = "max_prims"
                    break
                if self.stop_now():
                    stop = "time"
                    break
                visited += 1
                try:
                    self._visit(prim, prototype, stats, largest, identical, same_ref, rigid_cache, shading)
                except Exception as exc:  # one odd prim must not end the pass
                    if len(self.notes) < 50:
                        self.notes.append(_clip(f"Skipped {prim.GetPath()}: {type(exc).__name__}: {exc}"))
            if stop:
                break
        self.prims_checked = visited
        stats["prims"] = visited
        if stop == "max_prims":
            self.truncated_by_prims = True
            self.notes.append(f"Health checks stopped after max_prims={self.max_prims} prims; the rest was not checked.")
        elif stop == "time":
            self.stopped.append("health_prims")
        largest.sort(reverse=True)
        stats["largest_meshes"] = [{"path": path, "faces": faces, "points": points} for faces, points, path in largest[:10]]
        if stats["meshes"] > stats["meshes_measured"]:
            stats["sampled"] = True
        self._finish_prims(stats, identical, same_ref, shading)

    def _visit(self, prim: Any, prototype: Any, stats: dict[str, Any], largest: list, identical: dict, same_ref: dict,
               rigid_cache: dict, shading: Counter) -> None:
        path = prim.GetPath()
        display = str(path if prototype is None else path.ReplacePrefix(prototype[0], prototype[1]))
        weight = 1 if prototype is None else prototype[2]
        depth = display.count("/")
        if depth > stats["max_depth"]:
            stats["max_depth"] = depth
        if depth > DEEP_HIERARCHY:
            self.hit("hierarchy_depth", "warn", display)
        name = prim.GetName()
        if not name.isascii():
            self.hit("prim_names", "warn", display)
        elif len(name) > LONG_NAME:
            self.hit("prim_names", "warn", f"{display} ({len(name)} characters)")
        if prototype is None:
            if prim.IsInstanceable():
                stats["instanceable"] += 1
            if prim.IsInstance():
                stats["instances"] += 1
        if prim.HasAuthoredPayloads():
            stats["payloads"] += 1
        if prim.HasAuthoredReferences():
            stats["references"] += 1
        if prototype is None and (prim.HasAuthoredReferences() or prim.HasAuthoredPayloads()) \
                and not prim.IsInstanceable() and not prim.IsInstance():
            key = _arc_key(prim)
            if key:
                same_ref.setdefault(key, []).append(display)

        type_name = str(prim.GetTypeName())
        if prim.IsA(UsdGeom.Gprim):
            stats["gprims"] += 1
            self._check_binding(prim, display)
        if prim.IsA(UsdGeom.Mesh):
            self._check_mesh(prim, display, prototype, weight, stats, largest, identical, rigid_cache)
        elif prim.IsA(UsdShade.Material):
            stats["materials"] += 1
            self._check_material(prim, display)
        elif prim.IsA(UsdShade.Shader):
            stats["shaders"] += 1
            self._check_shader(prim, display, shading)
        elif type_name in ("Xform", "") and prim.IsDefined() and not prim.IsPseudoRoot() and not prim.IsInstance() \
                and not prim.HasAuthoredReferences() and not prim.HasAuthoredPayloads() \
                and not prim.GetAppliedSchemas() and not prim.GetChildren():
            self.hit("empty_xforms", "info", display)
        if UsdPhysics is not None:
            if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                stats["rigid_bodies"] += 1
                if not _has_mass(prim):
                    self.hit("physics_mass", "info", display)
            if prim.HasAPI(UsdPhysics.CollisionAPI):
                stats["colliders"] += 1
        self._check_time_samples(prim, display)

    def _check_binding(self, prim: Any, display: str) -> None:
        try:
            if str(UsdGeom.Imageable(prim).ComputePurpose()) in ("guide", "proxy"):
                return
        except Exception:
            pass
        material, _rel = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
        if material:
            return
        for subset in UsdGeom.Subset.GetAllGeomSubsets(UsdGeom.Imageable(prim)):
            if UsdShade.MaterialBindingAPI(subset.GetPrim()).ComputeBoundMaterial()[0]:
                return
        self.hit("material_bindings", "warn", display)

    def _check_mesh(self, prim: Any, display: str, prototype: Any, weight: int, stats: dict[str, Any], largest: list,
                    identical: dict, rigid_cache: dict) -> None:
        stats["meshes"] += 1
        if stats["meshes_measured"] >= MAX_MESH_READS:
            return
        stats["meshes_measured"] += 1
        mesh = UsdGeom.Mesh(prim)
        points_attr = mesh.GetPointsAttr()
        counts = mesh.GetFaceVertexCountsAttr().Get() or []
        points = points_attr.Get()
        faces = len(counts)
        n_points = len(points) if points is not None else 0
        stats["total_points"] += n_points
        stats["total_faces"] += faces
        stats["total_faces_with_instances"] += faces * weight
        largest.append((faces, n_points, display))
        if len(largest) > 40:
            largest.sort(reverse=True)
            del largest[10:]
        if faces > HUGE_MESH_FACES:
            self.hit("heavy_meshes", "warn", f"{display} ({faces:,} faces)")
        # normals
        scheme = str(mesh.GetSubdivisionSchemeAttr().Get() or "catmullClark")
        normals_attr = mesh.GetNormalsAttr()
        normals_primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar("normals")
        has_normals = normals_attr.HasAuthoredValue() or (normals_primvar and normals_primvar.HasAuthoredValue())
        if has_normals and scheme != "none":
            self.hit("normals", "warn", f"{display}: normals are ignored because subdivisionScheme is '{scheme}'")
        elif not has_normals and scheme == "none" and faces:
            self.hit("normals", "info", f"{display}: polygon mesh without normals (renders faceted)")
        if normals_attr.HasAuthoredValue() and not normals_attr.GetNumTimeSamples():
            normals = normals_attr.Get()
            interpolation = str(mesh.GetNormalsInterpolation())
            indices_len = len(mesh.GetFaceVertexIndicesAttr().Get() or [])
            expected = {"vertex": n_points, "varying": n_points, "faceVarying": indices_len, "uniform": faces,
                        "constant": 1}.get(interpolation)
            if normals is not None and expected is not None and len(normals) != expected:
                self.hit("normals", "warn", f"{display}: {len(normals)} normals but {interpolation} needs {expected}")
        # identical meshes (instancing opportunity), only for non-instanced, non-animated meshes
        if prototype is None and n_points and not points_attr.GetNumTimeSamples():
            step = max(1, n_points // _POINT_SAMPLES)
            sample = tuple(round(float(c), 4) for i in range(0, n_points, step)[:_POINT_SAMPLES] for c in points[i])
            identical.setdefault((n_points, faces, sample), []).append(display)
        # physics collider on a heavy render mesh
        if UsdPhysics is not None and prim.HasAPI(UsdPhysics.CollisionAPI):
            approximation = "none"
            if prim.HasAPI(UsdPhysics.MeshCollisionAPI):
                approximation = str(UsdPhysics.MeshCollisionAPI(prim).GetApproximationAttr().Get() or "none")
            if approximation == "none":
                dynamic = _under_rigid_body(prim, rigid_cache)
                if faces > COLLIDER_MAX_FACES:
                    self.hit("physics_colliders", "warn",
                             f"{display}: triangle-mesh collider with {faces:,} faces (physics:approximation = none)")
                elif dynamic:
                    self.hit("physics_colliders", "warn",
                             f"{display}: triangle-mesh collider on a moving rigid body (PhysX will use a convex hull)")

    def _check_material(self, prim: Any, display: str) -> None:
        material = UsdShade.Material(prim)
        outputs = list(material.GetSurfaceOutputs())
        if not any(output.HasConnectedSource() for output in outputs):
            self.hit("materials_surface", "warn", display)

    def _check_shader(self, prim: Any, display: str, shading: Counter) -> None:
        shader = UsdShade.Shader(prim)
        source = str(shader.GetImplementationSource())
        shader_id = str(shader.GetShaderId() or "") if source == "id" else ""
        if source == "sourceAsset" and getattr(shader.GetSourceAsset("mdl"), "path", ""):
            shading["mdl"] += 1
        elif shader_id == "UsdPreviewSurface":
            shading["UsdPreviewSurface"] += 1
        elif shader_id.startswith("ND_"):
            shading["MaterialX"] += 1
        elif source == "id" and str(prim.GetAttribute("info:implementationSource").Get() or "") == "mdlMaterial":
            shading["mdl (old schema)"] += 1
        if shader_id == "UsdUVTexture":
            file_input = shader.GetInput("file")
            asset = file_input.Get() if file_input else None
            name = os.path.basename(getattr(asset, "path", "") or "")
            if name and _NON_COLOR_TEXTURE.search(name):
                color_space_input = shader.GetInput("sourceColorSpace")
                color_space = str(color_space_input.Get() or "auto") if color_space_input else "auto"
                if color_space != "raw":
                    self.hit("textures", "warn",
                             f"{display}: '{name}' looks like data (normal/roughness/...) but sourceColorSpace is '{color_space}'")
            return
        if source == "sourceAsset":
            for shader_input in shader.GetInputs():
                attr = shader_input.GetAttr()
                if attr.GetTypeName() != Sdf.ValueTypeNames.Asset or not attr.HasAuthoredValue():
                    continue
                value = attr.Get()
                name = os.path.basename(getattr(value, "path", "") or "")
                if name and _NON_COLOR_TEXTURE.search(name) and str(attr.GetColorSpace() or "") == "sRGB":
                    self.hit("textures", "warn", f"{display}.{attr.GetName()}: '{name}' is a data map read as sRGB")

    def _check_time_samples(self, prim: Any, display: str) -> None:
        for attr in prim.GetAuthoredAttributes():
            count = attr.GetNumTimeSamples()
            if not count:
                continue
            if count == 1:
                self.hit("time_samples", "warn", f"{display}.{attr.GetName()} (1 sample)")
                continue
            times = attr.GetTimeSamples()
            picks = times if count <= _TIME_SAMPLE_FULL_COMPARE else [times[0], times[count // 2], times[-1]]
            first = attr.Get(picks[0])
            try:
                static = all(attr.Get(t) == first for t in picks[1:])
            except Exception:
                static = False
            if static:
                label = "samples" if count <= _TIME_SAMPLE_FULL_COMPARE else "sampled values"
                self.hit("time_samples", "warn", f"{display}.{attr.GetName()} ({count} identical {label})")

    def _finish_prims(self, stats: dict[str, Any], identical: dict, same_ref: dict, shading: Counter) -> None:
        if stats["total_faces"] > HEAVY_TOTAL_FACES:
            self.hit("heavy_meshes", "warn", f"{stats['total_faces']:,} faces in total", count=0)
        for (n_points, faces, _sample), paths in sorted(identical.items(), key=lambda item: -len(item[1])):
            if len(paths) >= IDENTICAL_MESH_MIN_COPIES:
                self.hit("instancing_opportunities", "warn",
                         f"{len(paths)} identical meshes ({n_points} points, {faces} faces), e.g. {paths[0]}",
                         count=len(paths), detail={"kind": "identical_meshes", "copies": len(paths), "examples": paths[:5]})
        for key, paths in sorted(same_ref.items(), key=lambda item: -len(item[1])):
            if len(paths) >= REFERENCE_MIN_COPIES:
                asset = key[0][0]
                self.hit("instancing_opportunities", "warn",
                         f"{len(paths)} prims reference {_short_path(asset)} without instanceable, e.g. {paths[0]}",
                         count=len(paths), detail={"kind": "same_reference", "copies": len(paths), "examples": paths[:5]})
        if stats["materials"] or shading:
            parts = [f"{name}: {count}" for name, count in shading.most_common()]
            self.hit("shading_models", "info", "; ".join(parts) if parts else "materials without recognised shaders",
                     count=stats["materials"])
            if shading.get("mdl") and not shading.get("UsdPreviewSurface") and not shading.get("MaterialX"):
                self.hit("shading_models", "info", "Only MDL shaders: other tools will show default grey", count=0)
            if shading.get("mdl (old schema)"):
                self.hit("shading_models", "info", "Deprecated MDL schema found (Kit rule MaterialOldMdlSchemaChecker)", count=0)
        self._texture_facts()

    # --------------------------------------------------------------------------------------- textures

    def _texture_facts(self) -> None:
        formats: Counter[str] = Counter()
        total_bytes = 0
        largest: list[dict[str, Any]] = []
        inspected = 0
        for resolved, facts in self.textures.items():
            formats[facts["ext"] or "?"] += 1
            ext = facts["ext"]
            if ext and ext not in PORTABLE_IMAGE_EXTENSIONS:
                self.hit("textures", "warn", f"{_short_path(resolved)} (.{ext} is not portable; use png/jpg/exr)")
            if facts["in_package"] or facts["udim"] or inspected >= MAX_TEXTURE_FILES:
                continue
            if self.stop_now():
                self.stopped.append("textures")
                break
            inspected += 1
            try:
                size = os.stat(resolved).st_size
            except OSError:
                continue
            total_bytes += size
            width_height = _image_size(resolved, ext)
            record = {"path": _short_path(resolved), "bytes": size}
            if width_height:
                record["width"], record["height"] = width_height
                if max(width_height) >= LARGE_TEXTURE_PIXELS:
                    self.hit("textures", "info", f"{_short_path(resolved)} is {width_height[0]}x{width_height[1]}", count=0)
            if size >= LARGE_TEXTURE_BYTES:
                self.hit("textures", "info", f"{_short_path(resolved)} is {size / 1048576:.0f} MB", count=0)
            largest.append(record)
        largest.sort(key=lambda item: -item["bytes"])
        self.stats["textures"] = {
            "count": len(self.textures) + len(self.missing_textures),
            "missing": len(self.missing_textures),
            "formats": dict(formats),
            "local_files_inspected": inspected,
            "local_bytes": total_bytes,
            "largest": largest[:10],
        }

    # ----------------------------------------------------------------------------------------- result

    def result(self, elapsed: float) -> dict[str, Any]:
        checks = []
        penalty = 0
        unchecked: set[str] = set()
        for phase in self.not_run:
            unchecked.update(_PHASE_CHECKS.get(phase, ()))
        for check_id, spec in _CHECKS.items():
            entry = self.findings.get(check_id, {"status": "pass", "count": 0, "examples": [], "details": []})
            if check_id in unchecked and entry["status"] == "pass":
                entry = {"status": "info", "count": 0, "details": [],
                         "examples": ["Not checked: the time budget ran out before this check started."]}
            status = entry["status"]
            penalty += spec.get(status, 0) if status in ("fail", "warn") else 0
            record = {
                "id": check_id,
                "title": spec["title"],
                "status": status,
                "count": entry["count"],
                "examples": entry["examples"],
                "why": spec["why"],
                "how_to_fix": list(spec["fix"]) if status != "pass" else [],
                "kit_validator_rule": spec["kit"],
                "checked": check_id not in unchecked,
            }
            if status != "pass" and spec.get("mcp"):
                record["mcp_phrase"] = spec["mcp"]
            if entry["details"]:
                record["details"] = entry["details"]
            checks.append(record)
        score = max(0, min(100, 100 - penalty))
        grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 55 else "F"
        truncated_by_time = bool(self.stopped or self.not_run)
        truncated = truncated_by_time or self.truncated_by_prims
        notes = list(self.notes)
        if truncated_by_time:
            parts = []
            if self.stopped:
                parts.append(f"{', '.join(dict.fromkeys(self.stopped))} stopped early")
            if self.not_run:
                parts.append(f"{', '.join(dict.fromkeys(self.not_run))} did not run")
            notes.insert(0, f"Time budget reached: {'; '.join(parts)}. Results cover only what was checked; allow more time.")
        if self.project_root is None:
            notes.append("The stage has no file on disk, so 'outside project' was not checked (pass project_root).")
        if self.root_path != Sdf.Path.absoluteRootPath:
            notes.append(f"Prim checks cover {self.root_path} only; dependencies and units cover the whole stage.")
        notes.append("Read-only: nothing was changed, fixed or saved.")
        stats = dict(self.stats)
        stats.setdefault("textures", {"count": len(self.textures), "missing": len(self.missing_textures)})
        stats["layers"] = len(self.stage.GetUsedLayers())
        stats["layer_files"] = [usd_validate._layer_name(layer) for layer in self.stage.GetUsedLayers()][:20]
        stats["meters_per_unit"] = float(UsdGeom.GetStageMetersPerUnit(self.stage))
        stats["up_axis"] = str(UsdGeom.GetStageUpAxis(self.stage))
        stats["default_prim"] = str(self.stage.GetRootLayer().defaultPrim or "") or None
        dependencies = {
            "counts": {bucket: self.dep_counts.get(bucket, 0) for bucket in self.deps},
            "by_usage": dict(self.usage_counts),
            **{bucket: list(items) for bucket, items in self.deps.items()},
            "project_root": self.project_root,
        }
        result = {
            "ok": True,
            "healthy": not any(check["status"] == "fail" for check in checks),
            "score": score,
            "grade": grade,
            "score_is_partial": truncated,
            "stage": usd_validate._layer_name(self.stage.GetRootLayer(), full=True),
            "root_path": str(self.root_path),
            "usd_version": ".".join(str(part) for part in Usd.GetVersion()),
            "checks": checks,
            "dependencies": dependencies,
            "stats": stats,
            "summary": "",
            "truncated": truncated,
            "truncated_by_time": truncated_by_time,
            "prims_checked": self.prims_checked,
            "elapsed_ms": round(elapsed * 1000.0, 1),
            "usd_validate": self.validate_summary,
            "notes": notes,
        }
        if self.budget is not None:
            result["time_budget"] = self.budget.report(self.prims_checked, truncated_by_time,
                                                       stopped_early=list(dict.fromkeys(self.stopped)),
                                                       not_run=list(dict.fromkeys(self.not_run)))
        result["summary"] = _summary(result)
        return _jsonable(result)


# ------------------------------------------------------------------------------------------------ helpers


def _summary(result: dict[str, Any]) -> str:
    stats = result["stats"]
    fails = [check for check in result["checks"] if check["status"] == "fail"]
    warns = [check for check in result["checks"] if check["status"] == "warn"]
    sentences = [f"This asset scores {result['score']}/100 (grade {result['grade']})."]
    sentences.append(
        f"It has {stats.get('prims', 0):,} prims, {stats.get('meshes', 0):,} meshes with "
        f"{stats.get('total_faces', 0):,} faces, {stats.get('materials', 0):,} materials and "
        f"{stats.get('textures', {}).get('count', 0):,} textures, in {stats.get('meters_per_unit', 0):g} m units, "
        f"{stats.get('up_axis', '?')}-up."
    )
    if fails:
        sentences.append("Must fix: " + "; ".join(f"{check['title'].lower()} ({check['count']})" for check in fails[:3]) + ".")
    if warns:
        sentences.append("Worth fixing: " + "; ".join(check["title"].lower() for check in warns[:4]) + ".")
    if not fails and not warns:
        sentences.append("No problems were found; it is ready to reference into a project.")
    elif fails or warns:
        first = (fails or warns)[0]
        if first["how_to_fix"]:
            sentences.append(f"Start here: {first['how_to_fix'][0]}")
    if result["truncated"]:
        sentences.append("The check stopped early (time or prim limit), so this covers only part of the asset "
                         "and the score is partial.")
    return " ".join(sentences[:5])


def _usage_kind(usage: str, target: str) -> str:
    if usage != "asset attribute":
        return usage
    ext = _extension(target)
    if ext in IMAGE_EXTENSIONS:
        return "texture"
    if ext == "mdl":
        return "mdl"
    if ext in USD_EXTENSIONS:
        return "usd asset"
    return "file"


def _extension(path: str) -> str:
    base = str(path).split("[", 1)[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _arc_key(prim: Any) -> tuple:
    items = []
    for spec in prim.GetPrimStack():
        for proxy in (spec.referenceList, spec.payloadList):
            for item in usd_validate._list_op_items(proxy):
                asset = usd_validate._anchor(spec.layer, item.assetPath) if item.assetPath else spec.layer.identifier
                items.append((asset, str(item.primPath)))
        if items:
            break
    return tuple(sorted(items))


def _has_mass(prim: Any) -> bool:
    for candidate in itertools.islice(Usd.PrimRange(prim), 200):  # the body and its colliders
        if candidate.HasAPI(UsdPhysics.MassAPI):
            api = UsdPhysics.MassAPI(candidate)
            mass = api.GetMassAttr().Get() if api.GetMassAttr().HasAuthoredValue() else 0.0
            density = api.GetDensityAttr().Get() if api.GetDensityAttr().HasAuthoredValue() else 0.0
            if (mass or 0) > 0 or (density or 0) > 0:
                return True
    return False


def _under_rigid_body(prim: Any, cache: dict[Any, bool]) -> bool:
    chain = []
    current = prim
    result = False
    while current and not current.IsPseudoRoot():
        path = current.GetPath()
        if path in cache:
            result = cache[path]
            break
        chain.append(path)
        if current.HasAPI(UsdPhysics.RigidBodyAPI):
            result = True
            break
        current = current.GetParent()
    for path in chain:
        cache[path] = result
    return result


def _image_size(path: str, ext: str) -> tuple[int, int] | None:
    """Pixel size from a PNG or JPEG header (first 64 KB, read-only); None for other formats."""
    if ext not in ("png", "jpg", "jpeg"):
        return None
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536)
    except OSError:
        return None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR" and len(head) >= 24:
        width, height = struct.unpack(">II", head[16:24])
        return int(width), int(height)
    if head[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(head):
            if head[index] != 0xFF:
                index += 1
                continue
            marker = head[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", head[index + 5:index + 9])
                return int(width), int(height)
            length = struct.unpack(">H", head[index + 2:index + 4])[0]
            index += 2 + length
    return None


def _within(path: str, root: str) -> bool:
    child = os.path.normcase(os.path.abspath(path))
    parent = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _short_path(path: str) -> str:
    text = str(path).replace("\\", "/")
    parts = text.split("/")
    return text if len(parts) <= 4 else ".../" + "/".join(parts[-3:])


def _factor_text(factor: float) -> str:
    return f"{factor:g}x too large" if factor > 1 else f"{1 / factor:g}x too small"


def _clip(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 6) if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return _clip(str(value), 300)
