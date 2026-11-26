"""Non-destructive asset edits for digital-twin assembly (NVIDIA "Assembling Digital Twins" course).

- material_replace: the course's "Replace Asset Materials" in one step. Every prim whose material binding points at
  one material (by path, or by name: all copies, e.g. each machine's own Aluminum_Brushed) is rebound to another, as
  overs in an override layer (e.g. MaterialOverride.usd made with scene_add_sublayer), so the asset files stay
  untouched and muting or removing that layer reverts it. Prims inside instances cannot be changed one by one and
  are reported. Previews by default; applied edits are one undo (Ctrl+Z in Composer).
- asset_repoint: change reference/payload asset paths (e.g. a cloud https://... prop placed from the NVIDIA Assets
  browser) to a local copy, written relative to the layer that holds the arc. The course: "Collect" copies files but
  does not update the placed reference; this does that part.
- asset_collect: copy a local USD file and every file it depends on (sublayers, references, payloads, textures, MDL)
  into a project folder, keeping their relative layout, so the project stops depending on outside folders.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

try:
    from pxr import Sdf, Usd, UsdShade, UsdUtils
except ImportError:  # pragma: no cover
    Sdf = Usd = UsdShade = UsdUtils = None

MAX_PRIMS = 200_000
MAX_EDITS = 5000


def _layer_for(stage: Any, name: Any) -> Any:
    if name in (None, ""):
        return stage.GetEditTarget().GetLayer()
    for layer in stage.GetLayerStack(includeSessionLayers=False):
        if name in (layer.identifier, layer.GetDisplayName()) or layer.identifier.replace("\\", "/").endswith(
                "/" + str(name).lstrip("./")):
            return layer
    names = ", ".join(l.GetDisplayName() for l in stage.GetLayerStack(includeSessionLayers=False))
    raise ValueError(f"layer {name!r} is not in the layer stack ({names}). Make it first with "
                     f"scene_add_sublayer(layer_path='{name}', create_if_missing=True).")


def _run(stage: Any, layer: Any, label: str, paths: list[str], fn: Any, undo_stack: Any) -> None:
    from .kit_commands import run_undoable

    old = stage.GetEditTarget()
    try:
        stage.SetEditTarget(Usd.EditTarget(layer))
        kwargs = {"stack": undo_stack} if undo_stack is not None else {}
        run_undoable(label, stage, [], fn, shallow=paths, **kwargs)
    finally:
        stage.SetEditTarget(old)


# ------------------------------------------------------------------------------------------------ material_replace
def material_replace(stage: Any, from_material: str, to_material: str, root_path: Any = None, layer: Any = None,
                     *, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Rebind every prim bound to from_material (a path, or a material NAME to catch every copy) to to_material (a
    path), as overrides in `layer` (default: the authoring layer; the course uses a MaterialOverride.usd sublayer), so
    the asset files are not touched. root_path limits it to one subtree. Preview first; dry_run=False applies it as
    one undoable edit and checks every affected prim afterwards."""
    if Usd is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available"}
    if not isinstance(from_material, str) or not from_material or not isinstance(to_material, str):
        return {"ok": False, "error": "from_material (path or name) and to_material (path) are required"}
    target = stage.GetPrimAtPath(to_material) if to_material.startswith("/") else None
    if not target or not target.IsA(UsdShade.Material):
        return {"ok": False, "error": f"to_material {to_material!r} is not a Material prim path in this stage"}
    try:
        layer_obj = _layer_for(stage, layer)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not layer_obj.permissionToEdit:
        return {"ok": False, "error": f"{layer_obj.GetDisplayName()} is read-only"}
    start = stage.GetPrimAtPath(root_path) if isinstance(root_path, str) else stage.GetPseudoRoot()
    if not start:
        return {"ok": False, "error": f"No prim at {root_path!r}"}
    by_name = not from_material.startswith("/")

    def matches(path: Any) -> bool:
        return (path.name == from_material) if by_name else (str(path) == from_material)

    sources = sorted({str(p.GetPath()) for p in Usd.PrimRange(start, Usd.TraverseInstanceProxies())
                      if p.IsA(UsdShade.Material) and matches(p.GetPath())})
    if not sources:
        return {"ok": False, "error": f"No material matches {from_material!r}" + (" (by name)" if by_name else "")}
    edits, locked = [], []
    for prim in Usd.PrimRange(start, Usd.TraverseInstanceProxies()):
        if len(edits) >= MAX_EDITS:
            break
        rel = UsdShade.MaterialBindingAPI(prim).GetDirectBindingRel() if prim.HasAPI(UsdShade.MaterialBindingAPI) \
            else None
        targets = rel.GetTargets() if rel else []
        if targets and matches(targets[0]):
            (locked if prim.IsInstanceProxy() else edits).append(str(prim.GetPath()))
    # Seen live 2026-11-26 (course N_02_PCB_Router): the binding was written in the ROOT layer, and the override layer
    # was its sublayer, which is weaker, so every rebind lost. Check layer strength BEFORE asking for a yes.
    order = [l.identifier for l in stage.GetLayerStack(includeSessionLayers=True)]
    target_rank = order.index(layer_obj.identifier) if layer_obj.identifier in order else len(order)
    stronger: dict[str, int] = {}
    for path in edits:
        rel = stage.GetPrimAtPath(path).GetRelationship("material:binding")
        for spec in rel.GetPropertyStack(Usd.TimeCode.Default()) if rel else []:
            rank = order.index(spec.layer.identifier) if spec.layer.identifier in order else len(order)
            if rank < target_rank:  # a local layer above the target holds this binding: the target cannot win
                stronger[spec.layer.GetDisplayName()] = stronger.get(spec.layer.GetDisplayName(), 0) + 1
                break
    base = {"from": sources[:10], "to": to_material, "layer": layer_obj.GetDisplayName(), "prims": len(edits),
            "examples": [p.rsplit("/", 2)[-2] + "/" + p.rsplit("/", 1)[-1] for p in edits[:5]],
            "inside_instances": len(locked), "locked_examples": locked[:3]}
    if stronger:
        names = ", ".join(f"{name} ({count})" for name, count in stronger.items())
        return {"ok": False, "dry_run": dry_run is not False, "written": False, "blocked_by_stronger_layer": stronger,
                **base,
                "error": f"{sum(stronger.values())} of these bindings are written in {names}, which is STRONGER than "
                         f"{layer_obj.GetDisplayName()} in the layer stack (a root layer always beats its sublayers), "
                         "so an override there cannot win.",
                "fix": "Do the swap where this asset is REFERENCED instead (a wrapper file or the production-line "
                       "assembly): local opinions beat referenced ones (L before R in LIVRPS), and the asset file stays "
                       "untouched. Or choose a layer at least as strong as the one named above."}
    if locked:
        base["warning"] = (f"{len(locked)} binding(s) sit inside instances and cannot be changed per copy: switch "
                           "off Instanceable on their instance root, or change the material inside the asset itself.")
    if not edits:
        return {"ok": True, "dry_run": dry_run is not False, "written": False, **base,
                "next_step": "Nothing to rebind (no direct bindings to that material outside instances)."}
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, **base,
                "next_step": f"Preview only: {len(edits)} prim(s) would be rebound in {layer_obj.GetDisplayName()}. "
                             "Apply with dry_run=False (one undo)."}
    material = UsdShade.Material(target)

    def apply() -> None:
        for path in edits:
            prim = stage.GetPrimAtPath(path)
            UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)
        wrong = [p for p in edits if str(UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(p)).GetDirectBinding()
                                         .GetMaterialPath()) != to_material]
        if wrong:
            raise RuntimeError(f"{len(wrong)} prim(s) still point elsewhere (a stronger layer wins?): {wrong[:3]}")

    try:
        _run(stage, layer_obj, f"Rebind {len(edits)} prims to {target.GetName()}", edits, apply, undo_stack)
    except Exception as exc:  # noqa: BLE001 - rolled back by run_undoable
        return {"ok": False, "dry_run": False, "written": False, "error": f"Rolled back: {exc}", **base}
    return {"ok": True, "dry_run": False, "written": True, **base,
            "next_step": f"Done in {layer_obj.GetDisplayName()}: the asset files are untouched. Mute or remove that "
                         "layer to see the original materials again; Ctrl+Z undoes it; save to keep it."}


# ------------------------------------------------------------------------------------------------ asset_repoint
def _relative_to_layer(layer: Any, new_path: str) -> str:
    target = Path(new_path)
    if not target.is_absolute() or not layer.realPath:
        return new_path.replace("\\", "/")
    rel = os.path.relpath(target, Path(layer.realPath).parent).replace("\\", "/")
    return rel if rel.startswith(".") else f"./{rel}"


def asset_repoint(stage: Any, from_asset: str, to_asset: str, *, undo_stack: Any = None,
                  dry_run: Any = True) -> dict[str, Any]:
    """Change every reference/payload in the local layer stack whose asset path is from_asset (exact, or a prefix
    ending in '/' to move a whole folder) to to_asset (a local file; written relative to each layer). For props placed
    from the cloud NVIDIA Assets browser after Collect copied them locally. The new file must exist. Preview first;
    dry_run=False applies it as one undoable edit."""
    if Usd is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available"}
    if not isinstance(from_asset, str) or not from_asset or not isinstance(to_asset, str) or not to_asset:
        return {"ok": False, "error": "from_asset and to_asset are required"}
    folder = from_asset.endswith("/")
    plan = []
    for layer in stage.GetLayerStack(includeSessionLayers=False):
        if not layer.permissionToEdit:
            continue

        def visit(path: Any, layer=layer) -> None:
            if not path.IsPrimPath():
                return
            spec = layer.GetPrimAtPath(path)
            if spec is None:
                return
            for kind, items in (("reference", spec.referenceList), ("payload", spec.payloadList)):
                for item in list(items.GetAddedOrExplicitItems()):
                    asset = item.assetPath
                    if not asset or not (asset.startswith(from_asset) if folder else asset == from_asset):
                        continue
                    new_abs = to_asset + asset[len(from_asset):] if folder else to_asset
                    plan.append({"layer": layer, "prim": str(path), "kind": kind, "old": asset, "item": item,
                                 "new_abs": new_abs, "new": _relative_to_layer(layer, new_abs)})

        layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    shown = [{k: v for k, v in p.items() if k not in ("layer", "item", "new_abs")} | {"layer": p["layer"].GetDisplayName()}
             for p in plan[:50]]
    if not plan:
        return {"ok": False, "error": f"No reference or payload uses {from_asset!r} in this stage's local layers"}
    missing = sorted({p["new_abs"] for p in plan if not Path(p["new_abs"]).is_file()})
    if missing and not any("://" in m for m in missing):
        return {"ok": False, "error": "The new file does not exist yet: " + ", ".join(missing[:3]) +
                ". Collect it first (asset_collect, or right-click > Collect in the NVIDIA Assets browser).",
                "arcs": shown}
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, "arcs": len(plan), "changes": shown,
                "next_step": f"Preview only: {len(plan)} arc(s) would point at the local file. Apply with dry_run=False."}
    by_layer: dict[Any, list[dict[str, Any]]] = {}
    for item in plan:
        by_layer.setdefault(item["layer"], []).append(item)
    try:
        for layer, items in by_layer.items():
            def apply(items=items, layer=layer) -> None:
                for p in items:
                    spec = layer.GetPrimAtPath(p["prim"])
                    lst = spec.referenceList if p["kind"] == "reference" else spec.payloadList
                    old = p["item"]
                    new = (Sdf.Reference(p["new"], old.primPath, old.layerOffset, old.customData)
                           if p["kind"] == "reference" else Sdf.Payload(p["new"], old.primPath, old.layerOffset))
                    lst.ReplaceItemEdits(old, new)

            _run(stage, layer, f"Repoint {len(items)} arc(s) to local files", [p["prim"] for p in items], apply,
                 undo_stack)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dry_run": False, "written": False, "error": f"Rolled back: {exc}", "changes": shown}
    unresolved = [p["prim"] for p in plan if not stage.GetPrimAtPath(p["prim"]) or
                  not stage.GetPrimAtPath(p["prim"]).GetChildren()]
    return {"ok": True, "dry_run": False, "written": True, "arcs": len(plan), "changes": shown,
            "empty_after": unresolved[:10],
            "next_step": "Check the props still look right in the viewport, then save." +
                         (" Some repointed prims have no children now: check those paths." if unresolved else "")}


# ------------------------------------------------------------------------------------------------ asset_collect
def asset_collect(source: str, dest_folder: str, dry_run: Any = True) -> dict[str, Any]:
    """Copy a local USD file and everything it depends on (layers, references, payloads, textures, MDL files) into
    dest_folder, keeping their layout relative to the source file, so the project no longer depends on outside
    folders. Files outside the source's folder go into dest_folder/_external/. Never overwrites. Remote (http, cloud)
    dependencies are listed, not downloaded: use Collect in the NVIDIA Assets browser for those."""
    if UsdUtils is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available"}
    src = Path(str(source))
    dest = Path(str(dest_folder))
    if src.suffix.lower() not in (".usd", ".usda", ".usdc") or not src.is_file():
        return {"ok": False, "error": f"source must be an existing USD file: {source}"}
    layers, assets, unresolved = UsdUtils.ComputeAllDependencies(Sdf.AssetPath(str(src)))
    files = sorted({Path(l.realPath) for l in layers if l.realPath} | {Path(a) for a in assets if a})
    base = src.parent
    plan, remote = [], []
    for path in files:
        if "://" in str(path):
            remote.append(str(path))
            continue
        try:
            rel = path.resolve().relative_to(base.resolve())
        except ValueError:
            rel = Path("_external") / path.name
        plan.append((path, dest / rel))
    clashes = [str(t) for _, t in plan if t.exists()]
    result = {"files": len(plan), "copies": [{"from": str(s), "to": str(t)} for s, t in plan[:40]],
              "unresolved": [str(u) for u in unresolved][:20], "remote": remote[:20],
              "size_mb": round(sum(s.stat().st_size for s, _ in plan if s.is_file()) / 1e6, 2)}
    if clashes:
        return {"ok": False, "error": f"{len(clashes)} file(s) already exist in the destination (nothing is "
                                      f"overwritten): {clashes[:3]}", **result}
    external = [t for _, t in plan if "_external" in t.parts]
    if external:
        result["warning"] = (f"{len(external)} file(s) live outside the source folder and go to _external/: their "
                             "relative paths in the copied files would no longer match. Fix those with asset_repoint.")
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, **result,
                "next_step": f"Preview only: copy {len(plan)} file(s) ({result['size_mb']} MB). Apply with dry_run=False."}
    for source_file, target in plan:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target)
    entry = dest / src.name
    return {"ok": True, "dry_run": False, "written": True, **result, "entry_file": str(entry),
            "next_step": f"Collected. Point the placed prop at it: asset_repoint(from_asset=<old path>, "
                         f"to_asset='{entry}')."}
