"""Phase 4 (show) and phase 5 (deliver) tools.

Show:
- scene_camera_frame: a camera placed and aimed so a prim fills the frame, from the real lens maths (focal length and
  aperture -> field of view; bounding sphere -> distance). Built through scene_build_from_spec (preview, one undo,
  checked after).
- scene_lighting_rig: studio_3point / showroom / outdoor / hdri lights placed around a prim's bounds and aimed at it,
  through the spec builder (light attributes checked against the UsdLux schemas). Intensities are starting points.
Deliver:
- project_package: a scene and every file it depends on copied into one folder, absolute paths rewritten relative,
  then checked: every dependency must resolve INSIDE the package. Optional .usdz (one-file package).
- asset_paths_report / asset_paths_fix: absolute, cloud and missing asset paths in every layer a scene uses, and a fix
  that rewrites absolute local paths relative (a .bak copy of each changed file first).
- project_git_files: .gitattributes (Git LFS for binary USD, textures, models) and .gitignore for a project folder.
- asset_validator_run: NVIDIA's Asset Validator (the engine behind Window > Utilities > Asset Validator, from the
  installed omni.asset_validator.core) run on a saved file outside Kit. All rules on a big machine take minutes:
  pass rules=[...] for a quick check.
"""
from __future__ import annotations

import difflib
import math
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdUtils
except ImportError:  # pragma: no cover
    Gf = Sdf = Usd = UsdGeom = UsdUtils = None

from .kit_index import default_root


# ------------------------------------------------------------------------------------------------ helpers
def _look_rotation(eye: Any, target: Any, up: Any) -> tuple[float, float, float]:
    """XYZ Euler degrees that turn a camera / light (looks down its -Z, up = +Y) from eye towards target."""
    from .spec_builder import _euler

    camera = Gf.Matrix4d().SetLookAt(eye, target, up).GetInverse()
    return _euler(camera.ExtractRotation())


def _world_box(stage: Any, prim: Any) -> Any:
    return UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_], useExtentsHint=True) \
        .ComputeWorldBound(prim).ComputeAlignedRange()


def _parent_is_identity(stage: Any, path: str) -> bool:
    parent = stage.GetPrimAtPath(Sdf.Path(path).GetParentPath())
    if not parent or parent.IsPseudoRoot():
        return True
    matrix = UsdGeom.Xformable(parent).ComputeLocalToWorldTransform(Usd.TimeCode.Default()) \
        if parent.IsA(UsdGeom.Xformable) else Gf.Matrix4d(1)
    return Gf.IsClose(matrix, Gf.Matrix4d(1), 1e-9)


def _axes(stage: Any) -> tuple[Any, Any, Any]:
    """(up, right, forward) world vectors for the stage's up axis."""
    if UsdGeom.GetStageUpAxis(stage) == "Z":
        return Gf.Vec3d(0, 0, 1), Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, -1, 0)
    return Gf.Vec3d(0, 1, 0), Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, 0, 1)


# ------------------------------------------------------------------------------------------------ camera framing
def scene_camera_frame(stage: Any, target: str, camera: str = "/World/Cameras/HeroCam", direction: Any = None,
                       focal_length: Any = 35.0, margin: Any = 1.15, *, undo_stack: Any = None,
                       dry_run: Any = True) -> dict[str, Any]:
    """Create or move a camera so `target` fills the frame: distance from the lens maths (focal length, aperture ->
    field of view) and the target's bounding sphere, times margin (1.15 = 15 % space around). direction: where the
    camera sits seen from the target, as [x, y, z] in world axes (default: front-right, a bit above). The camera's
    parent must not be moved/scaled. Preview first; one undo; checked after."""
    from .spec_builder import build_from_spec

    prim = stage.GetPrimAtPath(target) if isinstance(target, str) else None
    if not prim:
        return {"ok": False, "error": f"No prim at {target!r}"}
    box = _world_box(stage, prim)
    if box.IsEmpty():
        return {"ok": False, "error": f"{target} has no geometry bounds to frame"}
    if not _parent_is_identity(stage, camera):
        return {"ok": False, "error": f"the parent of {camera} is moved or scaled: put the camera under an untransformed "
                                      "group such as /World/Cameras"}
    try:
        focal = float(focal_length)
        pad = float(margin)
        if focal <= 0 or pad < 1:
            raise ValueError
    except (TypeError, ValueError):
        return {"ok": False, "error": "focal_length must be > 0 and margin >= 1"}
    up, right, forward = _axes(stage)
    if direction is None:
        way = right + forward + up * 0.6
    else:
        try:
            way = Gf.Vec3d(*[float(v) for v in direction])
        except (TypeError, ValueError):
            return {"ok": False, "error": "direction must be [x, y, z]"}
    if way.GetLength() < 1e-9:
        return {"ok": False, "error": "direction must not be [0, 0, 0]"}
    way = way.GetNormalized()
    if abs(Gf.Dot(way, up)) > 0.999:
        return {"ok": False, "error": "a camera straight above or below needs another up direction: tilt direction a bit"}
    fallback = Usd.SchemaRegistry().FindConcretePrimDefinition("Camera")
    h_ap = float(fallback.GetAttributeFallbackValue("horizontalAperture"))
    v_ap = float(fallback.GetAttributeFallbackValue("verticalAperture"))
    fov = min(2 * math.atan(h_ap / (2 * focal)), 2 * math.atan(v_ap / (2 * focal)))
    centre = box.GetMidpoint()
    radius = box.GetSize().GetLength() / 2
    distance = radius * pad / math.sin(fov / 2)
    eye = centre + way * distance
    rotate = _look_rotation(eye, centre, up)
    spec = {"prims": [{"path": camera, "type": "Camera", "translate": list(eye), "rotate": list(rotate),
                       "attributes": {"focalLength": focal,
                                      "clippingRange": [max(distance * 0.001, 1e-4), distance + radius * 4]}}]}
    result = build_from_spec(stage, spec, undo_stack=undo_stack, dry_run=dry_run)
    result.update({"camera": camera, "target": target, "distance": round(distance, 4),
                   "field_of_view_deg": round(math.degrees(fov), 2), "focal_length": focal,
                   "tip": "In Composer: viewport camera menu (top left) > Cameras > pick it to look through it."})
    return result


# ------------------------------------------------------------------------------------------------ lighting rigs
RIGS = ("studio_3point", "showroom", "outdoor", "hdri")


def scene_lighting_rig(stage: Any, target: str, rig: str = "studio_3point", root: str = "/World/Lights",
                       hdri: Any = None, *, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Add a lighting rig around a prim and aim it there: studio_3point (key, fill, rim rect lights + soft dome),
    showroom (dome + two overhead rect lights), outdoor (sun as a distant light + sky dome), hdri (a dome lit by an
    image file: hdri=<.hdr/.exr path>). Lights go under `root`. Intensities are starting points to tune in Composer.
    Through scene_build_from_spec: preview, one undo, attributes checked against UsdLux."""
    from .spec_builder import build_from_spec

    if rig not in RIGS:
        return {"ok": False, "error": f"rig must be one of {', '.join(RIGS)}"}
    prim = stage.GetPrimAtPath(target) if isinstance(target, str) else None
    if not prim:
        return {"ok": False, "error": f"No prim at {target!r}"}
    box = _world_box(stage, prim)
    if box.IsEmpty():
        return {"ok": False, "error": f"{target} has no geometry bounds to light"}
    if not _parent_is_identity(stage, f"{root}/x"):
        return {"ok": False, "error": f"{root} is moved or scaled: use an untransformed group for the lights"}
    up, right, forward = _axes(stage)
    centre = box.GetMidpoint()
    radius = max(box.GetSize().GetLength() / 2, 1e-6)
    lights: list[dict[str, Any]] = []

    def aimed(name: str, kind: str, way: Any, distance: float, attributes: dict[str, Any]) -> None:
        eye = centre + way.GetNormalized() * distance
        lights.append({"path": f"{root}/{name}", "type": kind, "translate": list(eye),
                       "rotate": list(_look_rotation(eye, centre, up)), "attributes": attributes})

    size = radius * 0.8
    if rig == "studio_3point":
        aimed("Key", "RectLight", right * -1 + forward + up * 0.8, radius * 3,
              {"inputs:intensity": 15000.0, "inputs:width": size, "inputs:height": size})
        aimed("Fill", "RectLight", right + forward + up * 0.3, radius * 3,
              {"inputs:intensity": 5000.0, "inputs:width": size, "inputs:height": size})
        aimed("Rim", "RectLight", forward * -1 + up * 1.2, radius * 3,
              {"inputs:intensity": 10000.0, "inputs:width": size, "inputs:height": size})
        lights.append({"path": f"{root}/Ambient", "type": "DomeLight", "attributes": {"inputs:intensity": 300.0}})
    elif rig == "showroom":
        lights.append({"path": f"{root}/Dome", "type": "DomeLight", "attributes": {"inputs:intensity": 1000.0}})
        aimed("TopFront", "RectLight", forward * 0.4 + up, radius * 2.5,
              {"inputs:intensity": 12000.0, "inputs:width": size * 2, "inputs:height": size})
        aimed("TopBack", "RectLight", forward * -0.4 + up, radius * 2.5,
              {"inputs:intensity": 8000.0, "inputs:width": size * 2, "inputs:height": size})
    elif rig == "outdoor":
        aimed("Sun", "DistantLight", right * -0.5 + forward * 0.5 + up, radius * 2,
              {"inputs:intensity": 3000.0, "inputs:angle": 0.53})
        lights.append({"path": f"{root}/Sky", "type": "DomeLight", "attributes": {"inputs:intensity": 1000.0}})
    else:
        if not isinstance(hdri, str) or not hdri:
            return {"ok": False, "error": "rig hdri needs hdri=<path to an .hdr or .exr image>"}
        lights.append({"path": f"{root}/HDRI", "type": "DomeLight",
                       "attributes": {"inputs:intensity": 1000.0, "inputs:texture:file": {"type": "asset", "value": hdri},
                                      "inputs:texture:format": "latlong"}})
        layer_dir = Path(stage.GetRootLayer().realPath).parent if stage.GetRootLayer().realPath else None
        image = Path(hdri) if Path(hdri).is_absolute() or layer_dir is None else layer_dir / hdri
        if "://" not in hdri and not image.is_file():
            return {"ok": False, "error": f"HDRI image not found: {image}"}
    result = build_from_spec(stage, {"prims": lights}, undo_stack=undo_stack, dry_run=dry_run)
    result.update({"rig": rig, "target": target, "lights": [l["path"].rsplit("/", 1)[-1] for l in lights],
                   "note": "Intensities are starting points; RTX exposure differs per scene. Tune in the Property panel."})
    return result


# ------------------------------------------------------------------------------------------------ asset paths
def _all_asset_paths(layer: Any) -> list[str]:
    found: list[str] = []

    def record(path: str) -> str:
        found.append(path)
        return path

    UsdUtils.ModifyAssetPaths(layer, record)  # identity: reads every asset path (refs, payloads, sublayers, attrs)
    return found


def _kind_of(asset: str, base: Path) -> tuple[str, Path | None]:
    if not asset:
        return "empty", None
    if "://" in asset or asset.startswith("omniverse:"):
        return "remote", None
    path = Path(asset)
    if path.is_absolute() or (len(asset) > 2 and asset[1] == ":"):
        return "absolute", path
    if asset.startswith("."):
        return "relative", (base / asset)
    return "search", None  # e.g. OmniPBR.mdl, found on the app's search path


def asset_paths_report(scene: str) -> dict[str, Any]:
    """Every asset path in every layer a saved scene uses (sublayers, references, payloads, textures, MDL): which are
    relative (good), absolute (break when the project moves), remote/cloud, or missing on disk. Read-only."""
    root = Path(str(scene))
    if not root.is_file():
        return {"ok": False, "error": f"scene must be a saved USD file: {scene}"}
    layers, _, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(root)))
    counts: Counter[str] = Counter()
    rows = []
    for layer in layers:
        base = Path(layer.realPath).parent if layer.realPath else root.parent
        for asset in sorted(set(_all_asset_paths(layer))):
            kind, path = _kind_of(asset, base)
            missing = path is not None and not path.exists()
            counts["missing" if missing else kind] += 1
            if kind in ("absolute", "remote") or missing:
                rows.append({"layer": layer.GetDisplayName(), "path": asset,
                             "problem": "missing on disk" if missing else f"{kind} path"})
    return {"ok": True, "read_only": True, "layers": len(layers), "counts": dict(counts),
            "problems": rows[:100], "unresolved": [str(u) for u in unresolved][:20],
            "next_step": ("asset_paths_fix rewrites absolute local paths as relative ones." if counts["absolute"] else
                          "Paths are portable." if not rows else "Fix missing files first (asset_repoint / move them).")}


def asset_paths_fix(scene: str, dry_run: Any = True) -> dict[str, Any]:
    """Rewrite absolute local asset paths (that exist on disk) as relative ones in every writable layer of a saved
    scene, so the project survives being moved or zipped. A .bak copy of each changed file is made first. Remote
    and missing paths are left alone and listed."""
    from .asset_edit import _rebase

    root = Path(str(scene))
    if not root.is_file():
        return {"ok": False, "error": f"scene must be a saved USD file: {scene}"}
    layers, _, _ = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(root)))
    plan = []
    for layer in layers:
        if not layer.realPath or not layer.permissionToSave:
            continue
        base = Path(layer.realPath).parent
        changes = []
        for asset in sorted(set(_all_asset_paths(layer))):
            kind, path = _kind_of(asset, base)
            if kind == "absolute" and path is not None and path.exists():
                new = _rebase(asset, base, base)
                if new != asset:
                    changes.append((asset, new))
        if changes:
            plan.append((layer, changes))
    shown = [{"layer": l.GetDisplayName(), "changes": [{"from": a, "to": b} for a, b in c[:10]]} for l, c in plan]
    if not plan:
        return {"ok": True, "dry_run": dry_run is not False, "written": False, "changes": [],
                "next_step": "No absolute local paths to fix."}
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, "changes": shown,
                "next_step": f"Preview only: {sum(len(c) for _, c in plan)} path(s) in {len(plan)} file(s). Apply with "
                             "dry_run=False (each file gets a .bak copy first)."}
    written = []
    for layer, changes in plan:
        mapping = dict(changes)
        backup = Path(layer.realPath + ".bak")
        if not backup.exists():
            shutil.copy2(layer.realPath, backup)
        UsdUtils.ModifyAssetPaths(layer, lambda asset, m=mapping: m.get(asset, asset))
        layer.Save()
        written.append(layer.realPath)
    return {"ok": True, "dry_run": False, "written": written, "changes": shown,
            "next_step": "Re-run asset_paths_report: the absolute count should be 0. The .bak files can go once happy."}


# ------------------------------------------------------------------------------------------------ package
def project_package(scene: str, dest: str, usdz: Any = False, dry_run: Any = True) -> dict[str, Any]:
    """Copy a saved scene and every file it depends on into `dest` (layout kept relative to the scene's folder),
    rewrite absolute paths in the copies as relative ones, then CHECK that every dependency resolves inside the
    package. usdz=True also writes <scene>.usdz (one file you can send). Never overwrites."""
    from .asset_edit import _rebase, asset_collect

    source = Path(str(scene))
    target = Path(str(dest))
    if not source.is_file():
        return {"ok": False, "error": f"scene must be a saved USD file: {scene}"}
    # Keep the PROJECT's layout (seen 2026-11-26: Assemblies/CL6_Line.usda references ../Assets/...; copying relative
    # to the scene's own folder broke every machine path). Root = common folder of the scene and its local files.
    layers, assets, source_missing = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(source)))
    local = [Path(p).resolve() for p in [l.realPath for l in layers] + list(assets)
             if p and "://" not in p and Path(p).exists()]
    try:
        project_root = Path(os.path.commonpath([str(source.resolve().parent)] + [str(p.parent) for p in local]))
    except ValueError:  # files on different drives
        project_root = source.resolve().parent
    if len(project_root.parts) <= 1:  # only a drive root in common: too wide to mirror
        project_root = source.resolve().parent
    collect = asset_collect(str(source), str(target), dry_run=dry_run, base_folder=str(project_root))
    collect["project_root"] = str(project_root)
    if not collect.get("ok") or dry_run is not False:
        collect["next_step"] = collect.get("next_step", "") + " Packaging also rewrites absolute paths and verifies."
        return collect
    rebased = 0
    for copied in [Path(c["to"]) for c in collect["copies"]] + list(target.rglob("*.usd*")):
        if copied.suffix.lower() not in (".usd", ".usda", ".usdc") or not copied.is_file():
            continue
        layer = Sdf.Layer.FindOrOpen(str(copied))
        if layer is None:
            continue
        before = layer.ExportToString() if copied.suffix.lower() == ".usda" else None
        changed = []

        def fix(asset: str, base=copied.parent) -> str:
            kind, path = _kind_of(asset, base)
            if kind != "absolute" or path is None:
                return asset
            try:
                inside = path.resolve().relative_to(project_root)
            except ValueError:
                inside = Path("_external") / path.name
            new = _rebase(str(target / inside), base, base)
            if new != asset:
                changed.append(asset)
            return new

        UsdUtils.ModifyAssetPaths(layer, fix)
        if changed:
            layer.Save()
            rebased += len(changed)
        del before
    entry = target / source.resolve().relative_to(project_root)
    layers, assets, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(entry)))
    outside = sorted({p for p in [l.realPath for l in layers] + list(assets)
                      if p and not Path(p).resolve().is_relative_to(target.resolve())})

    def app_provided(name: str) -> bool:  # MDL modules on Kit's search path (OmniPBR.mdl, nvidia/core_definitions.mdl)
        return name.endswith(".mdl") and not name.startswith(".") and not Path(name).is_absolute() and ":" not in name

    already = set()
    for item in source_missing:  # gaps the SOURCE already had, mapped into the package (seen live: course textures)
        text = str(item)
        try:
            already.add(str((target / Path(text).resolve().relative_to(project_root)).resolve()).lower())
        except (ValueError, OSError):
            already.add(text.lower())
    new_missing, old_missing = [], []
    for item in unresolved:
        text = str(item)
        if app_provided(text):
            continue
        key = str(Path(text).resolve()).lower() if (":" in text or Path(text).is_absolute()) else text.lower()
        (old_missing if key in already or text.lower() in already else new_missing).append(text)
    result = {"ok": not outside and not new_missing, "dry_run": False, "written": str(target), "entry_file": str(entry),
              "files": collect["files"], "size_mb": collect["size_mb"], "paths_rebased": rebased,
              "project_root": str(project_root), "outside_package": outside[:10],
              "missing_new_in_package": new_missing[:10],
              "already_missing_in_source": [Path(m).name for m in old_missing][:10]}
    if usdz is True and result["ok"]:
        archive = target / f"{source.stem}.usdz"
        if archive.exists():
            result["usdz"] = f"{archive} already exists (not overwritten)"
        else:
            made = UsdUtils.CreateNewUsdzPackage(Sdf.AssetPath(str(entry)), str(archive))
            result["usdz"] = str(archive) if made else "usdz packaging failed"
    result["next_step"] = ("Self-contained: open the entry file from the package folder to check it, then zip or send "
                           "the folder." if result["ok"] else "Some files are still outside the package or missing: "
                                                              "see outside_package / unresolved.")
    return result


# ------------------------------------------------------------------------------------------------ git
GITATTRIBUTES = [
    "# Git LFS: big binary files (install once: git lfs install)",
    "*.usd filter=lfs diff=lfs merge=lfs -text", "*.usdc filter=lfs diff=lfs merge=lfs -text",
    "*.usdz filter=lfs diff=lfs merge=lfs -text", "*.png filter=lfs diff=lfs merge=lfs -text",
    "*.jpg filter=lfs diff=lfs merge=lfs -text", "*.jpeg filter=lfs diff=lfs merge=lfs -text",
    "*.exr filter=lfs diff=lfs merge=lfs -text", "*.hdr filter=lfs diff=lfs merge=lfs -text",
    "*.tga filter=lfs diff=lfs merge=lfs -text", "*.dds filter=lfs diff=lfs merge=lfs -text",
    "*.fbx filter=lfs diff=lfs merge=lfs -text", "*.obj filter=lfs diff=lfs merge=lfs -text",
    "*.glb filter=lfs diff=lfs merge=lfs -text", "*.jt filter=lfs diff=lfs merge=lfs -text",
    "# .usda and .mdl are text: kept as normal git files so diffs stay readable", "*.usda text",
]
GITIGNORE = ["__pycache__/", "*.pyc", "*.bak", "*.log", ".thumbs/", "_build/", "_compiler/", ".cache/", "*.tmp"]


def project_git_files(folder: str, dry_run: Any = True) -> dict[str, Any]:
    """.gitattributes (Git LFS for binary .usd/.usdc/.usdz, textures and models; .usda stays text) and .gitignore
    (caches, logs, .bak) for a project folder. Existing files are kept: only missing lines are added. Does not run git."""
    base = Path(str(folder))
    if not base.is_dir():
        return {"ok": False, "error": f"Not a folder: {folder}"}
    plan = {}
    for name, lines in ((".gitattributes", GITATTRIBUTES), (".gitignore", GITIGNORE)):
        path = base / name
        have = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
        missing = [line for line in lines if line not in have]
        if missing:
            plan[name] = missing
    if dry_run is not False or not plan:
        return {"ok": True, "dry_run": dry_run is not False, "written": False, "add": plan,
                "next_step": "Nothing to add." if not plan else "Preview only. Apply with dry_run=False. Then, in that "
                                                                 "folder: git init, git lfs install, git add ."}
    for name, missing in plan.items():
        path = base / name
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if text and not text.endswith("\n"):
            text += "\n"
        path.write_text(text + "\n".join(missing) + "\n", encoding="utf-8")
    return {"ok": True, "dry_run": False, "written": [str(base / n) for n in plan], "add": plan,
            "next_step": "In that folder: git init, git lfs install, git add ., git commit."}


# ------------------------------------------------------------------------------------------------ NVIDIA Asset Validator
def _validator_module() -> Any:
    found = sorted(default_root().glob("extscache/omni.asset_validator.core-*/pip_prebundle"), reverse=True)
    if not found:
        return None
    folder = str(found[0])
    if folder not in sys.path:
        sys.path.append(folder)
    try:
        import usd_validation_nvidia as module  # noqa: PLC0415
    except ImportError:
        return None
    return module


def asset_validator_run(file: str, rules: Any = None, max_issues: Any = 60) -> dict[str, Any]:
    """Run NVIDIA's Asset Validator (the engine of Window > Utilities > Asset Validator, read from the installed
    omni.asset_validator.core) on a saved USD file, outside Kit. rules=None runs all ~40 rules (minutes on a big
    machine: the course's N_05 took 5.5 min); rules=["KindChecker", ...] is quick. Results grouped by rule and
    severity, with the first max_issues messages."""
    module = _validator_module()
    if module is None:
        return {"ok": False, "error": "omni.asset_validator.core is not in the local Isaac Sim install (extscache)"}
    path = Path(str(file))
    if path.suffix.lower() not in (".usd", ".usda", ".usdc", ".usdz") or not path.is_file():
        return {"ok": False, "error": f"file must be a saved USD file: {file}"}
    available = sorted(n for n in dir(module) if n.endswith("Checker") and isinstance(getattr(module, n), type)
                       and n != "BaseRuleChecker")
    if rules is None:
        engine = module.ValidationEngine()
    else:
        if not isinstance(rules, list) or not rules:
            return {"ok": False, "error": "rules must be a list of rule names, e.g. [\"KindChecker\"]"}
        unknown = [r for r in rules if r not in available]
        if unknown:
            hints = {r: difflib.get_close_matches(r, available, n=1) for r in unknown}
            return {"ok": False, "error": f"unknown rules {unknown}" + "".join(
                f" ({r} -> {h[0]}?)" for r, h in hints.items() if h), "available": available}
        engine = module.ValidationEngine(init_rules=False)
        for name in rules:
            engine.enable_rule(getattr(module, name))
    started = time.time()
    results = engine.validate(str(path))
    issues = list(results.issues())
    try:
        limit = max(1, min(500, int(max_issues)))
    except (TypeError, ValueError):
        limit = 60
    groups = Counter((i.rule.__name__ if i.rule else "-", str(i.severity).split(".")[-1]) for i in issues)
    shown = []
    for issue in issues[:limit]:
        at = getattr(issue, "at", None)
        where = str(getattr(at, "path", "") or "") if at is not None else ""
        shown.append({"rule": issue.rule.__name__ if issue.rule else None,
                      "severity": str(issue.severity).split(".")[-1], "message": (issue.message or "")[:300],
                      "at": where})
    return {"ok": True, "read_only": True, "file": str(path), "validator": getattr(module, "__version__", "?"),
            "rules_run": len(rules) if rules else "all", "seconds": round(time.time() - started, 1),
            "issues": len(issues), "by_rule": [{"rule": r, "severity": s, "count": c} for (r, s), c in groups.most_common()],
            "first_issues": shown, "available_rules": available if rules is None else None,
            "next_step": "Fix FAILURE/ERROR first (they break loading or composition); warnings can wait. kind_check "
                         "gives a ready fix spec for KindChecker."}
