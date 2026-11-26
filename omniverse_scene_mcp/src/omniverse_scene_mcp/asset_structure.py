"""Phase 3 asset tools: asset_scaffold, scene_set_pivot, mesh_check, scene_scatter.

- asset_scaffold: a component asset laid out the recommended way (NVIDIA / ASWF asset structure guidelines): an entry
  file `<Name>.usda` (defaultPrim, kind = component, units, assetInfo) that PAYLOADS `<Name>_payload.usda`, which stacks
  `<Name>_look.usda` over `<Name>_geo.usda`. Optionally the geo layer references an existing model file. Opening the
  entry file shows the asset; unloading the payload keeps a light placeholder.
- scene_set_pivot: put a prim's pivot at its bottom-centre / centre / a point, with USD pivot ops (translate:pivot and
  its inverse), so rotating and scaling turn around that point while nothing moves.
- mesh_check: geometry problems that make assets render or simulate badly: faces with < 3 corners, out-of-range
  indices, zero-area faces, normals / UV (primvars:st) counts that do not match their interpolation, missing or wrong
  extents.
- scene_scatter: many copies of one prototype (a prop asset or a prim) as ONE UsdGeom.PointInstancer: grid rows or
  random positions inside an area with a minimum gap. Far lighter than hundreds of references.
"""
from __future__ import annotations

import math
import os
import random
from pathlib import Path
from typing import Any

try:
    from pxr import Gf, Kind, Sdf, Usd, UsdGeom, Vt
except ImportError:  # pragma: no cover
    Gf = Kind = Sdf = Usd = UsdGeom = Vt = None

MAX_FACES_PER_MESH = 200_000
MAX_MESHES = 5000
MAX_INSTANCES = 5000


# ------------------------------------------------------------------------------------------------ asset_scaffold
def asset_scaffold(folder: str, name: str, geometry: Any = None, up_axis: Any = "Z", meters_per_unit: Any = 0.001,
                   dry_run: Any = True) -> dict[str, Any]:
    """Make a component asset folder the recommended way: <Name>.usda (entry: defaultPrim, kind component, units,
    assetInfo) payloads <Name>_payload.usda, which stacks <Name>_look.usda (materials and bindings go here) over
    <Name>_geo.usda (geometry; references `geometry` if given, e.g. a converted CAD file). Never overwrites."""
    if Sdf is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available"}
    if not isinstance(name, str) or not Sdf.Path.IsValidIdentifier(name):
        return {"ok": False, "error": "name must be a valid prim name (letters, digits, _), e.g. N_08_Conveyor"}
    if up_axis not in ("Y", "Z"):
        return {"ok": False, "error": "up_axis must be Y or Z"}
    try:
        mpu = float(meters_per_unit)
    except (TypeError, ValueError):
        return {"ok": False, "error": "meters_per_unit must be a number (0.001 = mm, 0.01 = cm, 1 = m)"}
    base = Path(str(folder)) / name
    files = {"entry": base / f"{name}.usda", "payload": base / f"{name}_payload.usda",
             "look": base / f"{name}_look.usda", "geo": base / f"{name}_geo.usda"}
    geo_ref = None
    if geometry is not None:
        source = Path(str(geometry))
        if not source.is_file():
            return {"ok": False, "error": f"geometry file not found: {geometry}"}
        geo_ref = Path(os.path.relpath(source, base)).as_posix() if _same_drive(source, base) else source.as_posix()
        if not geo_ref.startswith((".", "/")) and ":" not in geo_ref:
            geo_ref = f"./{geo_ref}"
    clashes = [str(p) for p in files.values() if p.exists()]
    plan = {"folder": str(base), "files": {k: p.name for k, p in files.items()}, "geometry_reference": geo_ref,
            "layout": f"{name}.usda --payload--> {name}_payload.usda --sublayers--> [{name}_look.usda, {name}_geo.usda]"}
    if clashes:
        return {"ok": False, "error": f"already exists (nothing is overwritten): {clashes[:3]}", **plan}
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, **plan,
                "next_step": "Preview only. Apply with dry_run=False."}
    base.mkdir(parents=True, exist_ok=True)
    root = Sdf.Path("/" + name)

    def new_layer(path: Path) -> Any:
        layer = Sdf.Layer.CreateNew(str(path))
        layer.defaultPrim = name
        layer.pseudoRoot.SetInfo("upAxis", up_axis)
        layer.pseudoRoot.SetInfo("metersPerUnit", mpu)
        return layer

    geo = new_layer(files["geo"])
    spec = Sdf.CreatePrimInLayer(geo, root)
    spec.specifier, spec.typeName = Sdf.SpecifierDef, "Xform"
    geo_spec = Sdf.CreatePrimInLayer(geo, root.AppendChild("Geometry"))
    geo_spec.specifier, geo_spec.typeName = Sdf.SpecifierDef, "Xform"
    if geo_ref:
        geo_spec.referenceList.Prepend(Sdf.Reference(geo_ref))
    geo.Save()
    look = new_layer(files["look"])
    over = Sdf.CreatePrimInLayer(look, root)
    over.specifier = Sdf.SpecifierOver
    looks = Sdf.CreatePrimInLayer(look, root.AppendChild("Looks"))
    looks.specifier, looks.typeName = Sdf.SpecifierDef, "Scope"
    look.Save()
    payload = new_layer(files["payload"])
    payload.subLayerPaths.append(f"./{files['look'].name}")  # look first = stronger than geo
    payload.subLayerPaths.append(f"./{files['geo'].name}")
    payload.Save()
    entry = new_layer(files["entry"])
    prim = Sdf.CreatePrimInLayer(entry, root)
    prim.specifier, prim.typeName = Sdf.SpecifierDef, "Xform"
    prim.SetInfo("kind", "component")
    prim.assetInfo = {"name": name, "identifier": Sdf.AssetPath(f"./{files['entry'].name}")}
    prim.payloadList.Prepend(Sdf.Payload(f"./{files['payload'].name}"))
    entry.Save()
    stage = Usd.Stage.Open(str(files["entry"]))
    ok_prim = stage.GetPrimAtPath(root)
    checks = {"opens": bool(ok_prim), "kind": str(Usd.ModelAPI(ok_prim).GetKind()) if ok_prim else None,
              "is_model": bool(ok_prim and ok_prim.IsModel()), "has_looks": bool(stage.GetPrimAtPath(root.AppendChild("Looks"))),
              "geometry_children": len(stage.GetPrimAtPath(root.AppendChild("Geometry")).GetChildren())
              if stage.GetPrimAtPath(root.AppendChild("Geometry")) else 0}
    return {"ok": checks["opens"] and checks["is_model"], "dry_run": False, "written": [str(p) for p in files.values()],
            **plan, "checks": checks,
            "next_step": f"Reference {files['entry'].name} into scenes (it brings the payload). Put materials and their "
                         f"bindings in {files['look'].name}, geometry edits in {files['geo'].name}."}


def _same_drive(a: Path, b: Path) -> bool:
    return a.resolve().drive.lower() == b.resolve().drive.lower()



# ------------------------------------------------------------------------------------------------ scene_set_pivot
def scene_set_pivot(stage: Any, prim_path: str, where: Any = "bottom_center", *, undo_stack: Any = None,
                    dry_run: Any = True) -> dict[str, Any]:
    """Put a prim's pivot (the point it rotates and scales around) at its bounding-box bottom_center, center, or a
    point [x, y, z] in its own space, with USD pivot ops (xformOp:translate:pivot + its inverse): nothing moves.
    Composer's Move/Rotate gizmo then turns it around that point. Preview first; one undo."""
    prim = stage.GetPrimAtPath(prim_path) if isinstance(prim_path, str) else None
    if not prim or not prim.IsA(UsdGeom.Xformable):
        return {"ok": False, "error": f"{prim_path!r} is not a movable prim (Xform, mesh, shape)"}
    bound = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_], useExtentsHint=True)
    box = bound.ComputeUntransformedBound(prim).ComputeAlignedRange()
    up = 2 if UsdGeom.GetStageUpAxis(stage) == "Z" else 1
    if isinstance(where, list) and len(where) == 3:
        pivot = Gf.Vec3d(*[float(v) for v in where])
    elif where in ("bottom_center", "center"):
        if box.IsEmpty():
            return {"ok": False, "error": "the prim has no geometry bounds to measure"}
        pivot = Gf.Vec3d(box.GetMidpoint())
        if where == "bottom_center":
            pivot[up] = box.GetMin()[up]
    else:
        return {"ok": False, "error": "where must be bottom_center, center or [x, y, z]"}
    xformable = UsdGeom.Xformable(prim)
    ops = xformable.GetOrderedXformOps()
    existing = [op for op in ops if op.GetOpName() == "xformOp:translate:pivot"]
    before = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    local_before = xformable.GetLocalTransformation()
    plan = {"prim": prim_path, "pivot": [round(v, 4) for v in pivot], "where": where,
            "replaces_existing_pivot": bool(existing)}
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, **plan,
                "next_step": "Preview only. Apply with dry_run=False (one undo). Nothing moves; only the turning point."}
    from .kit_commands import run_undoable

    def apply() -> None:
        if existing:
            existing[0].Set(pivot)
        else:
            api = UsdGeom.XformCommonAPI(prim)
            if api:
                api.SetPivot(Gf.Vec3f(pivot))
            else:
                # Composer's translate/orient/scale stack: pivot op after translate, inverse at the end.
                op = xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble, "pivot")
                op.Set(pivot)
                current = xformable.GetOrderedXformOps()[:-1]
                first_after_translate = 1 if current and current[0].GetOpType() == UsdGeom.XformOp.TypeTranslate else 0
                order = current[:first_after_translate] + [op] + current[first_after_translate:]
                inverse = xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble, "pivot", isInverseOp=True)
                xformable.SetXformOpOrder(order + [inverse])
        # A pivot wraps rotate/scale in (move to pivot ... move back), which shifts a prim that is already rotated or
        # scaled (seen in tests 2026-11-26). Like a DCC "move pivot", compensate with the translate op: the 3x3 part
        # is unchanged, so the fix is exactly the difference in translation.
        shift = local_before.ExtractTranslation() - xformable.GetLocalTransformation().ExtractTranslation()
        if shift.GetLength() > 1e-9:
            translate = next((op for op in xformable.GetOrderedXformOps()
                              if op.GetOpName() == "xformOp:translate"), None)
            if translate is None:
                translate = xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble)
                xformable.SetXformOpOrder([translate] + list(xformable.GetOrderedXformOps()[:-1]))
            value = translate.Get() or Gf.Vec3d(0, 0, 0)
            translate.Set(type(value)(*(Gf.Vec3d(value) + shift)))
        after = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        if not Gf.IsClose(before, after, 1e-6):
            raise RuntimeError("the prim moved (it must not): rolled back")

    kwargs = {"stack": undo_stack} if undo_stack is not None else {}
    try:
        run_undoable(f"Pivot of {prim.GetName()} to {where}", stage, [], apply, shallow=[prim_path], **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dry_run": False, "written": False, "error": f"Rolled back: {exc}", **plan}
    return {"ok": True, "dry_run": False, "written": True, **plan,
            "next_step": "Rotate it in Composer: it now turns around the new pivot. Ctrl+Z undoes it."}


# ------------------------------------------------------------------------------------------------ mesh_check
try:  # Kit's Python has numpy (mesh_check runs inside Kit when the app is open); the router's may not
    import numpy as _np
except ImportError:  # pragma: no cover - depends on the interpreter
    _np = None
SAMPLE_FACES = 300_000  # pure-Python budget (seen 2026-11-26: 5.5 M faces of N_02 took 86 s without numpy)


def _zero_area_faces(points: Any, indices: Any, counts: Any, budget: dict[str, Any]) -> int:
    """Faces whose first three corners span no area (collapsed)."""
    if _np is not None:
        pts = _np.asarray(points, dtype=float)
        idx = _np.asarray(indices, dtype=_np.int64)
        cnt = _np.asarray(counts, dtype=_np.int64)
        starts = _np.cumsum(cnt) - cnt
        valid = starts[cnt >= 3]
        a, b, c = pts[idx[valid]], pts[idx[valid + 1]], pts[idx[valid + 2]]
        return int(_np.count_nonzero(_np.linalg.norm(_np.cross(b - a, c - a), axis=1) <= 1e-12))
    zero, offset = 0, 0
    for count in counts:
        if budget["faces"] <= 0:
            budget["sampled"] = True
            break
        budget["faces"] -= 1
        if count >= 3:
            a, b, c = (points[indices[offset + k]] for k in range(3))
            if Gf.Cross(Gf.Vec3d(b) - Gf.Vec3d(a), Gf.Vec3d(c) - Gf.Vec3d(a)).GetLength() <= 1e-12:
                zero += 1
        offset += count
    return zero


def _interp_count(mesh: Any, interp: str, faces: int, points: int, corners: int) -> int | None:
    return {"constant": 1, "uniform": faces, "vertex": points, "varying": points, "faceVarying": corners}.get(interp)


def mesh_check(stage: Any, root_path: Any = None) -> dict[str, Any]:
    """Geometry problems per mesh: faces with fewer than 3 corners, indices out of range, zero-area faces, normals or
    UVs (primvars:st) whose count does not match their interpolation, and missing / too small extents. Read-only;
    looks inside instances; very large meshes are sampled (first 200,000 faces)."""
    start = stage.GetPrimAtPath(root_path) if isinstance(root_path, str) else stage.GetPseudoRoot()
    if not start:
        return {"ok": False, "error": f"No prim at {root_path!r}"}
    problems: list[dict[str, Any]] = []
    meshes = faces_total = clean = 0
    no_uv = 0
    budget = {"faces": SAMPLE_FACES, "sampled": False}  # only used without numpy
    for prim in Usd.PrimRange(start, Usd.TraverseInstanceProxies()):
        if not prim.IsA(UsdGeom.Mesh) or meshes >= MAX_MESHES:
            continue
        meshes += 1
        mesh = UsdGeom.Mesh(prim)
        counts = mesh.GetFaceVertexCountsAttr().Get() or []
        indices = mesh.GetFaceVertexIndicesAttr().Get() or []
        points = mesh.GetPointsAttr().Get() or []
        issues = []
        n_faces, n_points, n_corners = len(counts), len(points), len(indices)
        faces_total += n_faces
        if sum(counts) != n_corners:
            issues.append(f"faceVertexCounts add up to {sum(counts)}, but there are {n_corners} indices")
        small = sum(1 for c in counts if c < 3)
        if small:
            issues.append(f"{small} face(s) with fewer than 3 corners")
        if indices and n_points and max(indices) >= n_points:
            issues.append(f"an index ({max(indices)}) points past the last point ({n_points - 1})")
        elif indices and points and sum(counts) == n_corners:
            zero = _zero_area_faces(points, indices, counts, budget)
            if zero:
                issues.append(f"{zero} zero-area face(s) (collapsed triangles: shading and collision trouble)")
        normals = mesh.GetNormalsAttr()
        if normals and normals.HasAuthoredValue():
            want = _interp_count(mesh, str(mesh.GetNormalsInterpolation()), n_faces, n_points, n_corners)
            have = len(normals.Get() or [])
            if want is not None and have != want:
                issues.append(f"{have} normals for interpolation {mesh.GetNormalsInterpolation()} (needs {want})")
        st = UsdGeom.PrimvarsAPI(prim).GetPrimvar("st")
        if st and st.HasAuthoredValue():
            values = st.Get() or []
            want = _interp_count(mesh, str(st.GetInterpolation()), n_faces, n_points, n_corners)
            have = len(st.GetIndices() or []) if st.IsIndexed() else len(values)
            if want is not None and have != want:
                issues.append(f"{have} UV entries for interpolation {st.GetInterpolation()} (needs {want})")
        else:
            no_uv += 1
        extent = mesh.GetExtentAttr().Get()
        if points:
            computed = UsdGeom.PointBased.ComputeExtent(points)
            if extent is None:
                issues.append("no extent authored (renderers and culling guess the size)")
            elif not all(extent[0][i] <= computed[0][i] + 1e-4 and extent[1][i] >= computed[1][i] - 1e-4
                         for i in range(3)):
                issues.append("authored extent is smaller than the points (parts get culled or clipped)")
        if issues:
            if len(problems) < 100:
                problems.append({"mesh": str(prim.GetPath()), "issues": issues})
        else:
            clean += 1
    return {"ok": True, "read_only": True, "meshes": meshes, "faces": faces_total, "clean": clean,
            "with_problems": meshes - clean, "without_uvs": no_uv, "problems": problems,
            "zero_area_check": "every face (numpy)" if _np is not None else
            (f"sampled: first {SAMPLE_FACES:,} faces (no numpy here; inside Kit every face is checked)"
             if budget["sampled"] else "every face"),
            "next_step": ("Fix the listed meshes in the source app or re-export; Composer's Scene Optimizer "
                          "(Merge Vertices, remove zero-area faces) repairs many." if problems else
                          "No geometry problems found.") + (f" {no_uv} mesh(es) have no UVs: textures cannot map on them."
                                                           if no_uv else "")}


# ------------------------------------------------------------------------------------------------ scene_scatter
def scene_scatter(stage: Any, path: str, prototype: str, count: Any = 10, area: Any = None, layout: Any = "grid",
                  spacing: Any = None, seed: Any = 1, random_yaw: Any = False, *, undo_stack: Any = None,
                  dry_run: Any = True) -> dict[str, Any]:
    """Place `count` copies of one prototype as ONE PointInstancer at `path`. prototype: a USD file (referenced) or a
    prim path (referenced inside). area: [[x0, y0], [x1, y1]] on the floor (stage units, floor = the plane under the
    up axis). layout "grid" fills rows; "random" keeps at least `spacing` between copies (default: prototype size).
    random_yaw turns each copy randomly around the up axis. Preview first; one undo."""
    if not isinstance(path, str) or not Sdf.Path.IsValidPathString(path) or not path.startswith("/"):
        return {"ok": False, "error": "path must be a new prim path like /World/Props/Pallets"}
    if stage.GetPrimAtPath(path):
        return {"ok": False, "error": f"{path} already exists: choose a new path"}
    try:
        n = int(count)
        if not 1 <= n <= MAX_INSTANCES:
            raise ValueError
    except (TypeError, ValueError):
        return {"ok": False, "error": f"count must be 1..{MAX_INSTANCES}"}
    if not (isinstance(area, list) and len(area) == 2 and all(isinstance(c, list) and len(c) == 2 for c in area)):
        return {"ok": False, "error": "area must be [[x0, y0], [x1, y1]] on the floor"}
    is_file = isinstance(prototype, str) and Path(prototype).suffix.lower() in (".usd", ".usda", ".usdc")
    if is_file:
        layer_dir = Path(stage.GetRootLayer().realPath).parent if stage.GetRootLayer().realPath else None
        local = (layer_dir / prototype) if layer_dir and not Path(prototype).is_absolute() else Path(prototype)
        if not local.is_file():
            return {"ok": False, "error": f"prototype file not found: {prototype}"}
    elif not (isinstance(prototype, str) and stage.GetPrimAtPath(prototype)):
        return {"ok": False, "error": "prototype must be a .usd file or an existing prim path"}
    up = UsdGeom.GetStageUpAxis(stage)
    x0, x1 = sorted([float(area[0][0]), float(area[1][0])])  # not "(x0, y0), (x1, y1) = sorted(xs), sorted(ys)":
    y0, y1 = sorted([float(area[0][1]), float(area[1][1])])  # that put the x range into (x0, y0) (tests 2026-11-26)
    rng = random.Random(seed)
    # Units like the Metrics Assembler (seen 2026-11-26: a metre pallet in the cm factory measured 1.2 "cm" and
    # would have come in 100x too small): the prototype gets scale = its metersPerUnit / the stage's, and a +-90 deg
    # turn about X when its up axis differs.
    factor, turn = 1.0, None
    if is_file:
        proto_layer = Sdf.Layer.FindOrOpen(str(local))  # keep the layer: a temporary one expires (seen 2026-11-26)
        info = proto_layer.pseudoRoot
        proto_mpu = float(info.GetInfo("metersPerUnit")) if info.HasInfo("metersPerUnit") else 0.01
        proto_up = str(info.GetInfo("upAxis")) if info.HasInfo("upAxis") else "Y"
        factor = proto_mpu / UsdGeom.GetStageMetersPerUnit(stage)
        if proto_up != up:
            turn = (90.0, 0.0, 0.0) if (proto_up, up) == ("Y", "Z") else (-90.0, 0.0, 0.0)
    # measure the converted prototype on a scratch stage to know its footprint in stage units
    # Two levels (seen in tests 2026-11-26): the unit fix sits on an Xform, the reference on a TYPELESS child. A
    # reference on the Xform itself loses the asset's own type (a Cube became an empty Xform) and the unit scale
    # would overwrite the asset's own scale op.
    probe = Usd.Stage.CreateInMemory()
    holder = UsdGeom.Xform.Define(probe, "/P").GetPrim()
    if is_file:
        probe.DefinePrim("/P/Asset").GetReferences().AddReference(str(local))
        api = UsdGeom.XformCommonAPI(holder)
        api.SetScale(Gf.Vec3f(factor, factor, factor))
        if turn:
            api.SetRotate(Gf.Vec3f(*turn))
    size = None
    box = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_]).ComputeWorldBound(
        holder if is_file else stage.GetPrimAtPath(prototype)).ComputeAlignedRange()
    if not box.IsEmpty():
        size = box.GetSize()
    floor = (0, 1) if up == "Z" else (0, 2)
    gap = float(spacing) if spacing is not None else (max(size[floor[0]], size[floor[1]]) if size else 1.0)
    positions: list[tuple[float, float]] = []
    if layout == "grid":
        cols = max(1, int((x1 - x0) // gap) + 1)
        for i in range(n):
            px, py = x0 + (i % cols) * gap, y0 + (i // cols) * gap
            if py > y1:
                break
            positions.append((px, py))
    elif layout == "random":
        tries = 0
        while len(positions) < n and tries < n * 200:
            tries += 1
            px, py = rng.uniform(x0, x1), rng.uniform(y0, y1)
            if all((px - qx) ** 2 + (py - qy) ** 2 >= gap * gap for qx, qy in positions):
                positions.append((px, py))
    else:
        return {"ok": False, "error": "layout must be grid or random"}
    plan = {"path": path, "prototype": prototype, "placed": len(positions), "requested": n,
            "spacing": round(gap, 4), "prototype_size": [round(v, 3) for v in size] if size else None,
            "units_fix": {"scale": factor, "rotate": turn} if (factor != 1.0 or turn) else None}
    if len(positions) < n:
        plan["warning"] = f"only {len(positions)} of {n} fit in the area with spacing {round(gap, 3)}"
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, **plan, "first_positions": positions[:5],
                "next_step": "Preview only. Apply with dry_run=False (one undo)."}
    from .kit_commands import run_undoable

    def apply() -> None:
        instancer = UsdGeom.PointInstancer.Define(stage, path)
        proto = stage.DefinePrim(f"{path}/Prototypes/Proto0", "Xform")
        if is_file:
            stage.DefinePrim(f"{path}/Prototypes/Proto0/Asset").GetReferences().AddReference(prototype)
            if factor != 1.0 or turn:
                api = UsdGeom.XformCommonAPI(proto)
                api.SetScale(Gf.Vec3f(factor, factor, factor))
                if turn:
                    api.SetRotate(Gf.Vec3f(*turn))
        else:
            proto.GetReferences().AddInternalReference(prototype)
        instancer.CreatePrototypesRel().SetTargets([proto.GetPath()])
        pts = [Gf.Vec3f(px, py, 0) if up == "Z" else Gf.Vec3f(px, 0, py) for px, py in positions]
        instancer.CreatePositionsAttr(Vt.Vec3fArray(pts))
        instancer.CreateProtoIndicesAttr(Vt.IntArray([0] * len(pts)))
        if random_yaw is True:
            axis = Gf.Vec3d(0, 0, 1) if up == "Z" else Gf.Vec3d(0, 1, 0)
            quats = [Gf.Quath(Gf.Rotation(axis, rng.uniform(0, 360)).GetQuat()) for _ in pts]
            instancer.CreateOrientationsAttr(Vt.QuathArray(quats))

    kwargs = {"stack": undo_stack} if undo_stack is not None else {}
    try:
        run_undoable(f"Scatter {len(positions)} copies at {path}", stage, [path], apply, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dry_run": False, "written": False, "error": f"Rolled back: {exc}", **plan}
    return {"ok": True, "dry_run": False, "written": True, **plan,
            "next_step": "One PointInstancer holds every copy: select it to move them all; edit positions to move one."}
