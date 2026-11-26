"""Asset-library and scene audits for digital-twin assembly (NVIDIA "Assembling Digital Twins" course), read-only.

- asset_library_inventory: every .usd/.usda/.usdc in a folder with its up axis, units, defaultPrim, kind, payloads and
  references, missing or absolute asset paths, and whether it matches the project standard (the course: Z up,
  metersPerUnit 0.001). Reads layer headers only (no composition), so a big library is fast.
- kind_check: the model hierarchy rule Asset Validator's KindChecker enforces. A prim whose kind is a model kind
  (component, group, assembly) only IS a model when every ancestor is a group-kind (group or assembly); USD's own
  prim.IsModel() says whether that holds. Lists each broken prim and the ancestors that need kind "group".
- material_audit: every material (where it is defined: which file/layer, MDL or UsdPreviewSurface), how many prims use
  it, unused materials, and geometry with no material.
- instancing_report: prototypes and instances (the Statistics panel's PrototypeCount / TotalInstanceCount), and assets
  referenced many times that are not instanceable yet (candidates, not orders: the course's Count / Complexity /
  Stability / Scale checklist decides).
- asset_inventory_export: the course's inventory CSV (Name, Path, Type, Kind, Category + tag attributes), but it also
  looks inside instances (stage.Traverse() skips them) and counts unloaded payloads, which the course warns about.
"""
from __future__ import annotations

import csv
import io
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from pxr import Kind, Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover
    Kind = Sdf = Usd = UsdGeom = UsdShade = None

MAX_FILES = 2000
MAX_PRIMS = 200_000
_SUFFIXES = (".usd", ".usda", ".usdc")
CATEGORIES = (("Manufacturing Equipment", ("machine", "robot", "press", "router", "feeder", "conveyor")),
              ("Safety Equipment", ("cone", "barrier", "safety", "warning", "sign", "fenc")),
              ("Storage/Transport", ("pallet", "container", "box", "rack", "shelf", "cart")),
              ("Lighting", ("light", "lamp", "fixture")))
DEFAULT_TAGS = ("Unique_ID", "ProductionLine_ID", "Capacity_UnitsPerHour")


def _is_remote(asset: str) -> bool:
    return "://" in asset or asset.startswith("omniverse:")


def _arc_paths(layer: Any) -> tuple[list[str], list[str], list[str]]:
    """(references, payloads, sublayers) asset paths authored anywhere in the layer."""
    refs: list[str] = []
    pays: list[str] = []

    def visit(path: Any) -> None:
        if not path.IsPrimPath():
            return
        spec = layer.GetPrimAtPath(path)
        if spec is None:
            return
        for item in spec.referenceList.GetAddedOrExplicitItems():
            if item.assetPath:
                refs.append(item.assetPath)
        for item in spec.payloadList.GetAddedOrExplicitItems():
            if item.assetPath:
                pays.append(item.assetPath)

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return refs, pays, list(layer.subLayerPaths)


def asset_library_inventory(folder: str, up_axis: Any = "Z", meters_per_unit: Any = 0.001) -> dict[str, Any]:
    """Every USD file in a folder (and below) with up axis, units, defaultPrim, kind, references/payloads, and problems:
    units or up axis that differ from the project standard (default Z up, metersPerUnit 0.001 as in NVIDIA's
    digital-twin course; pass None to skip), no defaultPrim, missing or absolute asset paths. Read-only."""
    if Sdf is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available"}
    root = Path(str(folder))
    if not root.is_dir():
        return {"ok": False, "error": f"Not a folder: {folder}"}
    files = sorted(p for p in root.rglob("*") if p.suffix.lower() in _SUFFIXES and p.is_file())[:MAX_FILES]
    rows = []
    counts: Counter[str] = Counter()
    for path in files:
        rel = str(path.relative_to(root)).replace("\\", "/")
        layer = Sdf.Layer.FindOrOpen(str(path))
        if layer is None:
            rows.append({"file": rel, "problems": ["USD cannot open this file"]})
            counts["unreadable"] += 1
            continue
        info = layer.pseudoRoot
        axis = info.GetInfo("upAxis") if info.HasInfo("upAxis") else None
        mpu = info.GetInfo("metersPerUnit") if info.HasInfo("metersPerUnit") else None
        default = layer.defaultPrim or None
        default_spec = layer.GetPrimAtPath(Sdf.Path.absoluteRootPath.AppendChild(default)) if default else None
        kind = default_spec.GetInfo("kind") if default_spec is not None and default_spec.HasInfo("kind") else None
        refs, pays, subs = _arc_paths(layer)
        problems = []
        if up_axis is not None and axis is not None and str(axis) != str(up_axis):
            problems.append(f"up axis {axis}, project uses {up_axis}")
        if up_axis is not None and axis is None:
            problems.append(f"no upAxis written (USD then assumes Y; project uses {up_axis})")
        if meters_per_unit is not None and mpu is not None and abs(float(mpu) - float(meters_per_unit)) > 1e-9:
            problems.append(f"metersPerUnit {mpu:g}, project uses {float(meters_per_unit):g} (Composer's Metrics "
                            "Assembler scales it when referenced: Scale:unitsResolve)")
        if meters_per_unit is not None and mpu is None:
            problems.append("no metersPerUnit written (USD then assumes 0.01 = cm)")
        if not default and layer.rootPrims:
            problems.append("no defaultPrim: referencing this file without a prim path fails")
        missing, absolute = [], []
        for asset in refs + pays + subs:
            if _is_remote(asset):
                absolute.append(asset)
                continue
            if Path(asset).is_absolute() or re.match(r"^[A-Za-z]:[\\/]", asset):
                absolute.append(asset)
            resolved = (path.parent / asset).resolve() if not Path(asset).is_absolute() else Path(asset)
            if not resolved.is_file():
                missing.append(asset)
        if missing:
            problems.append(f"{len(missing)} asset path(s) do not resolve: {', '.join(sorted(set(missing))[:3])}")
        if absolute:
            problems.append(f"{len(absolute)} absolute or cloud path(s): {', '.join(sorted(set(absolute))[:2])} "
                            "(the course: use relative paths and local copies)")
        counts["with_problems" if problems else "clean"] += 1
        rows.append({"file": rel, "size_kb": round(path.stat().st_size / 1024, 1), "upAxis": axis,
                     "metersPerUnit": mpu, "defaultPrim": default, "kind": kind, "references": len(refs),
                     "payloads": len(pays), "sublayers": len(subs), "problems": problems})
    return {"ok": True, "folder": str(root), "files": len(rows), "clean": counts["clean"],
            "with_problems": counts["with_problems"] + counts["unreadable"], "assets": rows,
            "standard": {"upAxis": up_axis, "metersPerUnit": meters_per_unit},
            "next_step": next((f"{r['file']}: {r['problems'][0]}" for r in rows if r.get("problems")),
                              "Every file matches the project standard.")}


# ------------------------------------------------------------------------------------------------ kinds
def _model_kind(kind: str) -> bool:
    return bool(kind) and Kind.Registry.IsA(kind, Kind.Tokens.model)


def _prims(stage: Any) -> list[Any]:
    out = []
    for prim in stage.Traverse():
        if len(out) >= MAX_PRIMS:
            break
        out.append(prim)
    return out


def kind_check(stage: Any) -> dict[str, Any]:
    """Asset Validator's KindChecker rule, read-only: a prim with kind component / group / assembly is only a real model
    when every ancestor is group or assembly (USD's prim.IsModel()). Lists each broken prim, the ancestor kinds that
    break it, and the fix (give those ancestors kind "group", or "assembly" for a whole line/cell)."""
    broken = []
    needs_group: dict[str, str] = {}
    for prim in _prims(stage):
        kind = str(Usd.ModelAPI(prim).GetKind() or "")
        if not _model_kind(kind) or prim.IsModel():
            continue
        chain = []
        parent = prim.GetParent()
        while parent and not parent.IsPseudoRoot():
            parent_kind = str(Usd.ModelAPI(parent).GetKind() or "")
            if not (parent_kind and Kind.Registry.IsA(parent_kind, Kind.Tokens.group)):
                chain.append({"path": str(parent.GetPath()), "kind": parent_kind or None})
                needs_group.setdefault(str(parent.GetPath()), parent_kind or "")
            parent = parent.GetParent()
        broken.append({"prim": str(prim.GetPath()), "kind": kind, "blocking_ancestors": chain[:6],
                       "message": f'Kind "{kind}" here is not a model: model prims can only sit under group or '
                                  "assembly prims (" + ", ".join(f"{c['path']} is {c['kind'] or 'no kind'}"
                                                                 for c in chain[:3]) + ")."})
    fixes = [{"path": path, "current_kind": kind or None, "set_kind": "group"} for path, kind in sorted(needs_group.items())]
    # The same fix as Asset Validator's KindChecker "Fix Selected", ready for scene_build_from_spec (preview, one undo,
    # checked after). Kinds on prims inside a referenced asset become overs in the authoring layer: the asset file
    # stays as it is.
    fix_spec = {"prims": [{"path": f["path"], "kind": "group"} for f in fixes[:500]]} if fixes else None
    return {"ok": True, "read_only": True, "broken_models": len(broken), "problems": broken[:100],
            "fix_plan": fixes[:100], "fix_spec": fix_spec,
            "next_step": ("Give the listed ancestors kind group (assembly for a whole line or cell), e.g. with "
                          "scene_build_from_spec: {\"path\": ..., \"kind\": \"group\"}, or Asset Validator > KindChecker > "
                          "Fix Selected." if broken else "The kind hierarchy is valid: every model kind is a real model."),
            "note": "subcomponent is not a model kind, so it is never reported (as the course explains)."}


# ------------------------------------------------------------------------------------------------ materials
def _defined_in(prim: Any) -> str:
    stack = prim.GetPrimStack()
    for spec in stack:
        if spec.specifier == Sdf.SpecifierDef:
            return spec.layer.GetDisplayName()
    return stack[0].layer.GetDisplayName() if stack else "?"


def _shader_kind(material: Any) -> str:
    # Composer materials are MDL (outputs:mdl:surface), so ask the "mdl" render context first, then the universal one.
    # ComputeSurfaceSource takes ONE render-context token (seen 2026-11-26: a list raises ArgumentError).
    shader = None
    for context in ("mdl", ""):
        found = material.ComputeSurfaceSource(context)[0]
        if found:
            shader = found
            break
    if not shader:
        return "no surface shader"
    source = shader.GetSourceAsset("mdl") if hasattr(shader, "GetSourceAsset") else None
    if source:
        sub = shader.GetSourceAssetSubIdentifier("mdl") if hasattr(shader, "GetSourceAssetSubIdentifier") else ""
        return f"MDL {Path(source.path).name}" + (f" ({sub})" if sub else "")
    shader_id = shader.GetIdAttr().Get() if shader.GetIdAttr() else None
    return str(shader_id or "unknown shader")


def material_audit(stage: Any, root_path: Any = None) -> dict[str, Any]:
    """Every material in the scene (or under root_path): where it is defined (file/layer), its shader (MDL file or
    UsdPreviewSurface), how many prims use it; unused materials; geometry without a material. Read-only."""
    start = stage.GetPrimAtPath(root_path) if isinstance(root_path, str) else stage.GetPseudoRoot()
    if not start:
        return {"ok": False, "error": f"No prim at {root_path!r}"}
    users: dict[str, int] = Counter()
    unbound = []
    materials = []
    gprims = 0
    for prim in Usd.PrimRange(start, Usd.TraverseInstanceProxies()):
        if prim.IsA(UsdShade.Material):
            materials.append(prim)
        elif prim.IsA(UsdGeom.Gprim):
            gprims += 1
            bound = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
            if bound:
                users[str(bound.GetPath())] += 1
            elif len(unbound) < 50:
                unbound.append(str(prim.GetPath()))
            elif len(unbound) == 50:
                unbound.append("...")
    # Materials used under root_path but defined elsewhere (e.g. a referenced MaterialLibrary) count too
    # (seen 2026-11-26: 60 parts used m_PolishedSteel from the library and the audit did not list it).
    inside = {str(p.GetPath()) for p in materials}
    for path in users:
        if path not in inside and stage.GetPrimAtPath(path):
            materials.append(stage.GetPrimAtPath(path))
    rows = []
    for prim in materials:
        material = UsdShade.Material(prim)
        rows.append({"material": str(prim.GetPath()), "name": prim.GetName(), "defined_in": _defined_in(prim),
                     "shader": _shader_kind(material), "used_by": users.get(str(prim.GetPath()), 0)})
    rows.sort(key=lambda r: -r["used_by"])
    files = Counter(r["defined_in"] for r in rows)
    used = [r for r in rows if r["used_by"]]
    unused = [r["material"] for r in rows if not r["used_by"]]
    # Kept short on purpose (seen live 2026-11-26: N_02 listed 44 unused materials and long CAD paths).
    return {"ok": True, "read_only": True, "materials": len(rows), "geometry": gprims,
            "geometry_without_material": len([u for u in unbound if u != "..."]) + (1 if "..." in unbound else 0),
            "defined_in_files": dict(files), "used": used[:40], "unused_count": len(unused),
            "unused_examples": [u.rsplit("/", 1)[-1] for u in unused[:8]],
            "unbound_examples": [u.rsplit("/", 2)[-2] + "/" + u.rsplit("/", 1)[-1] for u in unbound[:5] if u != "..."],
            "next_step": ("Shared materials come from " + ", ".join(f"{k} ({v})" for k, v in files.most_common(3))
                          + ". To swap one everywhere without touching the asset: material_replace(from_material=<name>, "
                          "to_material=<path>, layer='MaterialOverride.usd')." if rows else "No materials in this scene.")}


# ------------------------------------------------------------------------------------------------ textures
def _image_size(path: Path) -> tuple[int, int] | None:
    """(width, height) from a PNG or JPEG header, without loading the image."""
    try:
        with path.open("rb") as handle:
            head = handle.read(26)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
            if head[:2] == b"\xff\xd8":
                handle.seek(2)
                while True:
                    marker = handle.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    length = int.from_bytes(handle.read(2), "big")
                    if marker[1] in (0xC0, 0xC1, 0xC2):
                        data = handle.read(5)
                        return int.from_bytes(data[3:5], "big"), int.from_bytes(data[1:3], "big")
                    handle.seek(length - 2, 1)
    except OSError:
        return None
    return None


def texture_check(stage: Any, gpu_budget_gb: Any = 6.0) -> dict[str, Any]:
    """Every texture the scene's shaders use: missing files, colour-space mistakes (normal / roughness / metallic maps
    must be raw, colour maps sRGB: a normal map read as sRGB shades wrongly), and an estimate of the GPU memory the
    PNG/JPEG textures need (width x height x 4 bytes, +1/3 for mipmaps) against gpu_budget_gb (the laptop: 6). Read-only."""
    from .mdl_info import texture_color_space

    files: dict[str, dict[str, Any]] = {}
    missing, wrong_space = [], []
    for prim in Usd.PrimRange(stage.GetPseudoRoot(), Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdShade.Shader):
            continue
        for shader_input in UsdShade.Shader(prim).GetInputs():
            attr = shader_input.GetAttr()
            if attr.GetTypeName() != Sdf.ValueTypeNames.Asset:
                continue
            value = attr.Get()
            if not value or not value.path:
                continue
            name = shader_input.GetBaseName()
            where = f"{prim.GetPath()}.inputs:{name}"
            if not value.resolvedPath:
                if len(missing) < 50:
                    missing.append({"input": where, "file": value.path})
                continue
            space = attr.GetColorSpace() or "auto"
            expected = texture_color_space(name)
            if space not in ("auto", expected) and len(wrong_space) < 50:
                wrong_space.append({"input": where, "colorSpace": space, "should_be": expected,
                                    "why": "data maps (normal, roughness, metallic) must not be gamma-corrected"
                                    if expected == "raw" else "colour maps are stored gamma-corrected (sRGB)"})
            entry = files.setdefault(value.resolvedPath, {"file": value.resolvedPath, "used_by": 0})
            entry["used_by"] += 1
    total = 0
    for entry in files.values():
        size = _image_size(Path(entry["file"]))
        if size:
            entry["size"] = f"{size[0]}x{size[1]}"
            entry["gpu_mb"] = round(size[0] * size[1] * 4 * 4 / 3 / 1e6, 1)
            total += entry["gpu_mb"]
    biggest = sorted(files.values(), key=lambda e: -e.get("gpu_mb", 0))[:10]
    try:
        budget_mb = float(gpu_budget_gb) * 1000
    except (TypeError, ValueError):
        budget_mb = 6000.0
    return {"ok": True, "read_only": True, "textures": len(files), "missing": missing, "wrong_color_space": wrong_space,
            "gpu_estimate_mb": round(total), "gpu_budget_mb": round(budget_mb), "biggest": biggest,
            "next_step": (f"{len(missing)} texture file(s) are missing: fix their paths first." if missing else
                          f"{len(wrong_space)} texture(s) have the wrong colour space." if wrong_space else
                          "Textures resolve and colour spaces are right.") +
                         (f" Textures alone need ~{round(total)} MB of the {round(budget_mb)} MB GPU: downsize the "
                          "biggest." if total > budget_mb * 0.5 else ""),
            "note": "Only PNG and JPEG sizes are read; EXR/TGA/DDS count as 0 MB. 'auto' lets Kit decide the colour space."}


# ------------------------------------------------------------------------------------------------ instancing
def instancing_report(stage: Any, min_repeats: Any = 3) -> dict[str, Any]:
    """Prototypes and instances (the Statistics panel's PrototypeCount, TotalInstanceCount, TotalPrimCount), and
    assets referenced at least min_repeats times that are not instanceable (candidates to consider). Read-only."""
    prototypes = stage.GetPrototypes()
    instances = []
    total = 0
    by_asset: dict[str, list[str]] = defaultdict(list)
    for prim in Usd.PrimRange(stage.GetPseudoRoot()):
        total += 1
        if prim.IsInstance():
            instances.append(prim)
        for spec in prim.GetPrimStack():
            for item in list(spec.referenceList.GetAddedOrExplicitItems()) + list(spec.payloadList.GetAddedOrExplicitItems()):
                if item.assetPath:
                    by_asset[f"{item.assetPath}{item.primPath or ''}"].append(str(prim.GetPath()))
                    break
    try:
        repeats = max(2, int(min_repeats))
    except (TypeError, ValueError):
        repeats = 3
    candidates = []
    for asset, paths in by_asset.items():
        unique = sorted(set(paths))
        not_instanced = [p for p in unique if not stage.GetPrimAtPath(p).IsInstanceable()]
        if len(unique) >= repeats and not_instanced:
            candidates.append({"asset": asset, "uses": len(unique), "not_instanceable": len(not_instanced),
                               "examples": not_instanced[:4]})
    candidates.sort(key=lambda c: -c["uses"])
    per_prototype = Counter(str(p.GetPrototype().GetPath()) for p in instances)
    return {"ok": True, "read_only": True, "PrototypeCount": len(prototypes), "TotalInstanceCount": len(instances),
            "TotalPrimCount": total, "instances_per_prototype": dict(per_prototype.most_common(20)),
            "candidates": candidates[:30],
            "advice": "Instance repeated, stable props (pallets, barriers, beams); not unique machines or objects you "
                      "edit per copy: instanced insides cannot be changed one by one. Measure before and after "
                      "(Statistics panel / scene_frame_stats): counts show sharing, not speed.",
            "how_to": "scene_make_instanceable(paths=[...]) previews the change."}


# ------------------------------------------------------------------------------------------------ inventory export
def _category(name: str) -> str:
    low = name.lower()
    for label, words in CATEGORIES:
        if any(word in low for word in words):
            return label
    return "Other"


def asset_inventory_export(stage: Any, attributes: Any = None, csv_path: Any = None, include_instances: Any = True,
                           dry_run: Any = True) -> dict[str, Any]:
    """The digital-twin inventory: every prim carrying one of the tag attributes (default Unique_ID, ProductionLine_ID,
    Capacity_UnitsPerHour) with Name, Path, Type, Kind and Category, as rows (dry_run=True, default) or written to
    csv_path (dry_run=False). Unlike a plain stage.Traverse() it also looks inside instances, and it reports how many
    payloads are unloaded (their tags cannot be seen until loaded)."""
    tags = list(attributes) if isinstance(attributes, list) and all(isinstance(a, str) for a in attributes) \
        else list(DEFAULT_TAGS)
    predicate = Usd.TraverseInstanceProxies() if include_instances is not False else Usd.PrimDefaultPredicate
    rows = []
    for prim in Usd.PrimRange(stage.GetPseudoRoot(), predicate):
        if len(rows) >= 50_000:
            break
        if prim.IsPseudoRoot():
            continue  # the invisible root "/" has no attributes (asking warns)
        values = {}
        for name in tags:
            attr = prim.GetAttribute(name)
            if attr and attr.HasAuthoredValue():
                values[name] = str(attr.Get())
        if not values:
            continue
        rows.append({"Name": prim.GetName(), "Path": str(prim.GetPath()), "Type": str(prim.GetTypeName()),
                     "Kind": str(Usd.ModelAPI(prim).GetKind() or ""), "Category": _category(prim.GetName()),
                     "InInstance": prim.IsInstanceProxy(), **values})
    unloaded = [str(p.GetPath()) for p in Usd.PrimRange(stage.GetPseudoRoot(), Usd.PrimAllPrimsPredicate)
                if p.HasAuthoredPayloads() and not p.IsLoaded()]
    columns = ["Name", "Path", "Type", "Kind", "Category", "InInstance"] + [t for t in tags if any(t in r for r in rows)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    result = {"ok": True, "rows": len(rows), "columns": columns, "preview": rows[:25],
              "by_category": dict(Counter(r["Category"] for r in rows)),
              "inside_instances": sum(1 for r in rows if r["InInstance"]),
              "unloaded_payloads": len(unloaded), "unloaded_examples": unloaded[:5],
              "note": "Category is a guess from the prim name (as in the course). A blank cell means the attribute is "
                      "absent, not zero." + (" Unloaded payloads hide their tags: load them first to count everything."
                                             if unloaded else "")}
    if dry_run is not False:
        result.update({"dry_run": True, "written": False,
                       "next_step": "Give csv_path and dry_run=False to write the CSV file."})
        return result
    if not isinstance(csv_path, str) or not csv_path.lower().endswith(".csv"):
        return {"ok": False, "error": "csv_path must be a path ending in .csv"}
    target = Path(csv_path)
    if not target.parent.is_dir():
        return {"ok": False, "error": f"Folder does not exist: {target.parent}"}
    if target.exists():
        return {"ok": False, "error": f"{target} already exists: choose a new file name (nothing is overwritten)"}
    target.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    result.update({"dry_run": False, "written": str(target)})
    return result
