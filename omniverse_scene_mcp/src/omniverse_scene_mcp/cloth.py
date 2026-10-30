"""Cloth: a new fabric sheet that falls, folds and drapes when you press Play (a PhysX "surface deformable").

Authors exactly what Isaac Sim 6.1's own helper writes (omni.physx 110.3 deformableUtils
create_auto_surface_deformable_hierarchy + add_surface_deformable_material, as in the Surface Deformable demo):

    <root> Xform        OmniPhysicsDeformableBodyAPI, PhysxAutoDeformableBodyAPI (cookingSourceMesh -> Mesh),
                        PhysxSurfaceDeformableBodyAPI (self collision, CCD, solver settings), physics material binding
      Mesh              the visible sheet (triangles), OmniPhysicsDeformablePoseAPI:default (bindPose points)
      SimMesh           empty until Play: PhysX "cooks" it from Mesh; OmniPhysicsSurfaceDeformableSimAPI, collision,
                        guide purpose (hidden)
    <PhysicsMaterials>/<name>  OmniPhysics(Surface)DeformableMaterialAPI + PhysxSurfaceDeformableMaterialAPI

Schemas are written by name (like physics_extras), so previews and tests work without Isaac; PhysX reads them at
Play. Writes only with dry_run=False, snapshotted, verified, rolled back on failure and recorded for undo.
"""
from __future__ import annotations

import math
from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt
except ImportError:  # pragma: no cover
    Gf = Sdf = Usd = UsdGeom = UsdPhysics = UsdShade = Vt = None

from .joints import (_Refusal, _add_api, _api_names, _apply, _guard, _has_collider, _number, _prim, _ready,
                     _result)

BODY_API, AUTO_API, PHYSX_BODY_API = "OmniPhysicsDeformableBodyAPI", "PhysxAutoDeformableBodyAPI", "PhysxSurfaceDeformableBodyAPI"
SIM_API, POSE_API = "OmniPhysicsSurfaceDeformableSimAPI", "OmniPhysicsDeformablePoseAPI:default"
MATERIAL_APIS = ("OmniPhysicsDeformableMaterialAPI", "OmniPhysicsSurfaceDeformableMaterialAPI",
                 "PhysxSurfaceDeformableMaterialAPI")
PHYSICS_PURPOSE = "physics"
REST_OFFSET_M, CONTACT_OFFSET_M = 0.01, 0.03  # the demo's collision offsets, in metres
DRAPE_GAP_M = 0.3
MAX_RESOLUTION, HEAVY_RESOLUTION = 120, 80

# Starting points, not measured fabrics. "cotton" is NVIDIA's Surface Deformable demo; the others scale it.
# Stiffness: stretch = resists pulling longer, shear = resists skewing, bend = resists folding.
# damping: like air resistance on every point (physxDeformableBody:linearDamping), so light fabric stops fluttering.
# roughness: the look material's UsdPreviewSurface roughness (0 = mirror shine, 1 = matte).
PRESETS: dict[str, dict[str, Any]] = {
    "cotton": {"static_friction": 0.4, "dynamic_friction": 0.2, "thickness_m": 0.002, "stretch": 1000.0,
               "shear": 0.1, "bend": 0.2, "damping": 0.5, "color": (0.85, 0.85, 0.8), "roughness": 0.85,
               "feel": "everyday fabric: drapes softly"},
    "silk": {"static_friction": 0.2, "dynamic_friction": 0.1, "thickness_m": 0.001, "stretch": 500.0,
             "shear": 0.05, "bend": 0.01, "damping": 0.8, "color": (0.9, 0.55, 0.65), "roughness": 0.35,
             "feel": "thin and slippery: flows into folds"},
    "denim": {"static_friction": 0.6, "dynamic_friction": 0.5, "thickness_m": 0.004, "stretch": 3000.0,
              "shear": 1.0, "bend": 2.0, "damping": 0.4, "color": (0.2, 0.3, 0.55), "roughness": 0.9,
              "feel": "thick and stiff: few, wide folds"},
    "rubber": {"static_friction": 0.9, "dynamic_friction": 0.8, "thickness_m": 0.003, "stretch": 50.0,
               "shear": 0.5, "bend": 0.5, "damping": 0.4, "color": (0.15, 0.15, 0.15), "roughness": 0.6,
               "feel": "stretchy and grippy sheet"},
}
MAX_DAMPING = 20.0


def _meters_per_unit(stage: Any) -> float:
    return float(UsdGeom.GetStageMetersPerUnit(stage) or 1.0)


def _z_up(stage: Any) -> bool:
    return UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.y


def _grid(size: float, resolution: int, z_up: bool) -> tuple[list[Any], list[int], list[int]]:
    """A flat square of resolution x resolution squares, each split into two triangles, centred on the origin."""
    step, half = size / resolution, size / 2
    points = []
    for row in range(resolution + 1):
        for col in range(resolution + 1):
            a, b = -half + col * step, -half + row * step
            points.append(Gf.Vec3f(a, b, 0) if z_up else Gf.Vec3f(a, 0, b))
    indices = []
    width = resolution + 1
    for row in range(resolution):
        for col in range(resolution):
            p0 = row * width + col
            p1, p2, p3 = p0 + 1, p0 + width, p0 + width + 1
            indices += [p0, p1, p3, p0, p3, p2]
    return points, [3] * (len(indices) // 3), indices


def _turn(degrees: float, z_up: bool) -> Any:
    half = math.radians(degrees) / 2
    return Gf.Quatf(math.cos(half), Gf.Vec3f(0, 0, math.sin(half)) if z_up else Gf.Vec3f(0, math.sin(half), 0))


def _top_level(path: Any) -> Any:
    prefixes = path.GetPrefixes()
    return prefixes[0] if len(prefixes) > 1 else Sdf.Path.absoluteRootPath


def _free_child(stage: Any, parent: Any, name: str) -> Any:
    candidate, index = parent.AppendChild(name), 1
    while stage.GetPrimAtPath(candidate):
        candidate = parent.AppendChild(f"{name}_{index:02d}")
        index += 1
    return candidate


def _physics_scene(stage: Any) -> Any:
    for prim in stage.Traverse():
        if prim.IsA(UsdPhysics.Scene):
            return prim
    return None


def _colliders_outside(stage: Any, root: Any, limit: int = 5000) -> bool:
    for count, prim in enumerate(stage.Traverse()):
        if count > limit:
            return True  # a big scene: assume it has ground
        if prim.HasAPI(UsdPhysics.CollisionAPI) and not prim.GetPath().HasPrefix(root):
            return True
    return False


def _drape_target(stage: Any, path: str, mpu: float, z_up: bool) -> tuple[list[float], float, bool]:
    """(position in metres just above the prim, a sheet size that overhangs it, whether it has a collider)."""
    prim = _prim(stage, path, "drape_over")
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    if box.IsEmpty():
        raise _Refusal(f"drape_over: {path} has no size (no geometry under it), so there is nothing to drape over")
    low, high = box.GetMin(), box.GetMax()
    center = [(low[i] + high[i]) / 2 * mpu for i in range(3)]
    up = 2 if z_up else 1
    center[up] = high[up] * mpu + DRAPE_GAP_M
    across = [i for i in range(3) if i != up]
    widest = max((high[i] - low[i]) * mpu for i in across)
    return center, min(max(widest * 1.6, 0.2), 5.0), _has_collider(prim)


@_guard
def make_cloth(stage: Any, path: str = "/World/Cloth", *, size: Any = None, resolution: Any = 40,
               position: Any = None, drape_over: str | None = None, turn: Any = 0, preset: str = "cotton",
               color: Any = None, damping: Any = None, self_collision: Any = True, undo_stack: Any = None,
               dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False create) a cloth sheet that falls, folds and drapes when you press Play, the
    same setup as Isaac Sim's Surface Deformable demo. size is the square's side in metres (default 1, or a bit wider
    than the drape_over prim), resolution how many squares per side (more = smoother but heavier on the GPU),
    position [x, y, z] in metres (default 1 m up) or drape_over a prim path to hang it just above that prim, turn
    degrees around the up axis, preset cotton/silk/denim/rubber, color [r, g, b] 0-1 to override the preset's,
    damping (air resistance, 0-20; more = calmer, less fluttering), self_collision so folds do not pass through each
    other. The sheet gets a real material (UsdPreviewSurface) and smooth shading, so it reacts to any sky or light.
    Adds a physics scene if there is none. Undo removes everything it made."""
    _ready(stage)
    if not isinstance(path, str) or not Sdf.Path.IsValidPathString(path) or not Sdf.Path(path).IsAbsolutePath() \
            or Sdf.Path(path).IsAbsoluteRootPath():
        raise _Refusal("path must be an absolute prim path such as /World/Cloth")
    root = Sdf.Path(path)
    if stage.GetPrimAtPath(root):
        raise _Refusal(f"{path} already exists; choose another path such as {path}_02")
    parent = root.GetParentPath()
    if not parent.IsAbsoluteRootPath() and not stage.GetPrimAtPath(parent):
        raise _Refusal(f"The parent {parent} does not exist yet; create it first (e.g. scene_create path='{parent}' "
                       "type_name='Xform')")
    if preset not in PRESETS:
        raise _Refusal(f"preset must be one of {', '.join(PRESETS)}")
    if isinstance(resolution, bool) or not isinstance(resolution, int) or not 4 <= resolution <= MAX_RESOLUTION:
        raise _Refusal(f"resolution must be a whole number from 4 to {MAX_RESOLUTION}")
    if not isinstance(self_collision, bool):
        raise _Refusal("self_collision must be true or false")
    turn_deg = _number(turn, "turn") or 0.0
    if color is not None and (not isinstance(color, (list, tuple)) or len(color) != 3):
        raise _Refusal("color must be three numbers [r, g, b] from 0 to 1, e.g. [0.8, 0.1, 0.1] for red")
    rgb = tuple(_number(c, "color", low=0, high=1) for c in color) if color is not None else None
    drag = _number(damping, "damping", low=0, high=MAX_DAMPING)
    mpu, z_up = _meters_per_unit(stage), _z_up(stage)
    warnings: list[str] = []
    drape = None
    if drape_over is not None:
        drape_pos, drape_size, drape_collides = _drape_target(stage, drape_over, mpu, z_up)
        drape = {"prim": drape_over, "has_collider": drape_collides}
        if not drape_collides:
            warnings.append(f"{drape_over} has no collider, so the cloth falls straight through it. Make it one first "
                            f"(scene_make_collider prim_path='{drape_over}').")
        if position is None:
            position = drape_pos
        if size is None:
            size = round(drape_size, 3)
    size_m = _number(1.0 if size is None else size, "size", low=0.05, high=20)
    if position is None:
        position = [0.0, 0.0, 1.0] if z_up else [0.0, 1.0, 0.0]
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        raise _Refusal("position must be three numbers [x, y, z] in metres")
    position_m = [_number(v, "position") for v in position]
    if resolution > HEAVY_RESOLUTION:
        warnings.append(f"resolution {resolution} makes {2 * resolution * resolution} triangles: heavy for a 6 GB GPU. "
                        f"Try {HEAVY_RESOLUTION} or less if Play stutters.")
    if not _colliders_outside(stage, root):
        warnings.append("Nothing in the scene has a collider, so the cloth will fall forever. Add a floor first "
                        "(Create > Physics > Ground Plane, or a Cube with scene_make_collider).")
    scene = _physics_scene(stage)
    if scene is not None:
        gpu = scene.GetAttribute("physxScene:enableGPUDynamics")
        if gpu and gpu.HasAuthoredValue() and gpu.Get() is False:
            warnings.append(f"{scene.GetPath()} has GPU dynamics turned off; cloth only simulates on the GPU. "
                            "Turn it on in the physics scene's Property panel (Enable GPU Dynamics).")
    if abs(mpu - 1.0) > 1e-9:
        warnings.append(f"This stage uses {mpu} metres per unit; the fabric presets were set up in metres, so the "
                        "cloth may feel stiffer or softer than its name.")

    top = _top_level(root)
    materials_scope = top.AppendChild("PhysicsMaterials")
    scope_is_new = not stage.GetPrimAtPath(materials_scope)
    material_path = materials_scope.AppendChild(f"{root.name}Fabric") if scope_is_new else \
        _free_child(stage, materials_scope, f"{root.name}Fabric")
    scene_path = scene.GetPath() if scene is not None else top.AppendChild("PhysicsScene")
    fabric = dict(PRESETS[preset])
    if rgb is not None:
        fabric["color"] = rgb
    if drag is not None:
        fabric["damping"] = drag
    look_path = root.AppendChild("Look")

    def units(metres: float) -> float:
        return metres / mpu

    plan = {
        "cloth_root": str(root), "visible_mesh": str(root.AppendChild("Mesh")),
        "sim_mesh": str(root.AppendChild("SimMesh")), "material": str(material_path),
        "physics_scene": {"path": str(scene_path), "new": scene is None},
        "size_m": size_m, "resolution": resolution, "triangles": 2 * resolution * resolution,
        "position_m": position_m, "turn_deg": turn_deg, "preset": preset, "feel": fabric["feel"],
        "fabric": {k: fabric[k] for k in ("static_friction", "dynamic_friction", "thickness_m", "stretch", "shear",
                                         "bend", "damping")},
        "look": {"material": str(look_path), "color": list(fabric["color"]), "roughness": fabric["roughness"],
                 "shading": "smooth"},
        "self_collision": self_collision, "drape_over": drape, "presets": sorted(PRESETS),
    }
    python = "\n".join([
        "# The same cloth with NVIDIA's own helper (Isaac Sim Script Editor).",
        "import omni.usd",
        "from pxr import UsdGeom",
        "from omni.physx.scripts import deformableUtils, physicsUtils",
        "stage = omni.usd.get_context().get_stage()",
        f"root = UsdGeom.Xform.Define(stage, {str(root)!r})  # the cloth's group: move it to move the cloth",
        f"# ...make a flat triangle Mesh at {str(root.AppendChild('Mesh'))!r} (the visible sheet), then:",
        f"deformableUtils.create_auto_surface_deformable_hierarchy(stage, {str(root)!r},",
        f"    {str(root.AppendChild('SimMesh'))!r}, {str(root.AppendChild('Mesh'))!r}, False, True)",
        f"deformableUtils.add_surface_deformable_material(stage, {str(material_path)!r},",
        f"    dynamic_friction={fabric['dynamic_friction']}, surface_thickness={units(fabric['thickness_m'])!r},",
        f"    surface_stretch_stiffness={fabric['stretch']}, surface_shear_stiffness={fabric['shear']},",
        f"    surface_bend_stiffness={fabric['bend']})",
        f"physicsUtils.add_physics_material_to_prim(stage, root.GetPrim(), {str(material_path)!r})",
    ])
    common = {
        "plan": plan, "warnings": warnings, "python": python,
        "explanation": (
            f"A {size_m:g} m {preset} sheet at {root}. A cloth is a 'surface deformable': unlike a rigid body (a "
            "wooden block that keeps its shape) its shape changes, like a bedsheet. When you press Play, PhysX first "
            "'cooks' a hidden simulation mesh (SimMesh) from the visible one, then lets gravity pull every point. "
            "Press Stop to put it back."),
        "how_to_watch": "Press Play (left toolbar of the viewport). Stop resets the cloth to where it started.",
    }
    if dry_run is not False:
        return _result("make_cloth", next_step="Run again with dry_run=False to create it (undoable).", **common)

    points, counts, indices = _grid(units(size_m), resolution, z_up)

    def mutate() -> None:
        if scene is None:
            new_scene = UsdPhysics.Scene.Define(stage, scene_path)
            new_scene.CreateGravityDirectionAttr().Set(Gf.Vec3f(0, 0, -1) if z_up else Gf.Vec3f(0, -1, 0))
            new_scene.CreateGravityMagnitudeAttr().Set(9.81 / mpu)
        if scope_is_new:
            UsdGeom.Scope.Define(stage, materials_scope)
        material = UsdShade.Material.Define(stage, material_path).GetPrim()
        for api in MATERIAL_APIS:
            _add_api(material, api)
        kg_per_unit = UsdPhysics.GetStageKilogramsPerUnit(stage) or 1.0
        for name, value in (("omniphysics:density", -1.0),  # -1: let PhysX use its default, as the helper does
                            ("omniphysics:staticFriction", fabric["static_friction"]),
                            ("omniphysics:dynamicFriction", fabric["dynamic_friction"]),
                            ("omniphysics:youngsModulus", 5.0e5 * mpu / kg_per_unit),
                            ("omniphysics:poissonsRatio", 0.45),
                            ("omniphysics:surfaceThickness", units(fabric["thickness_m"])),
                            ("omniphysics:surfaceStretchStiffness", fabric["stretch"]),
                            ("omniphysics:surfaceShearStiffness", fabric["shear"]),
                            ("omniphysics:surfaceBendStiffness", fabric["bend"]),
                            ("physxDeformableMaterial:elasticityDamping", 0.0),
                            ("physxDeformableMaterial:bendDamping", 0.0)):
            material.CreateAttribute(name, Sdf.ValueTypeNames.Float).Set(float(value))

        body = UsdGeom.Xform.Define(stage, root)
        body.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(*[units(v) for v in position_m]))
        body.AddOrientOp(UsdGeom.XformOp.PrecisionFloat).Set(_turn(turn_deg, z_up))
        body.AddScaleOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Vec3f(1, 1, 1))

        skin = UsdGeom.Mesh.Define(stage, root.AppendChild("Mesh"))
        skin.CreatePointsAttr().Set(Vt.Vec3fArray(points))
        skin.CreateFaceVertexCountsAttr().Set(Vt.IntArray(counts))
        skin.CreateFaceVertexIndicesAttr().Set(Vt.IntArray(indices))
        skin.CreateExtentAttr().Set(UsdGeom.PointBased.ComputeExtent(Vt.Vec3fArray(points)))
        skin.CreateDoubleSidedAttr().Set(True)  # both sides visible while it folds
        # No subdivisionScheme "none": seen live, that renders every triangle as a flat facet (it looked pixelated);
        # the default (catmullClark) gives smooth normals that follow the moving points.
        skin.CreateDisplayColorAttr().Set([Gf.Vec3f(*fabric["color"])])
        _pose(skin.GetPrim(), points)
        _look(stage, look_path, fabric, skin.GetPrim())

        sim = UsdGeom.Mesh.Define(stage, root.AppendChild("SimMesh"))  # filled by PhysX cooking at Play
        sim_prim = sim.GetPrim()
        _add_api(sim_prim, SIM_API)
        UsdPhysics.CollisionAPI.Apply(sim_prim)
        _add_api(sim_prim, "PhysxCollisionAPI")
        sim_prim.CreateAttribute("physxCollision:restOffset", Sdf.ValueTypeNames.Float).Set(units(REST_OFFSET_M))
        sim_prim.CreateAttribute("physxCollision:contactOffset", Sdf.ValueTypeNames.Float).Set(units(CONTACT_OFFSET_M))
        _pose(sim_prim, None)
        sim.CreatePurposeAttr().Set(UsdGeom.Tokens.guide)  # hidden: the visible Mesh is what you see

        prim = body.GetPrim()
        _add_api(prim, AUTO_API)
        prim.CreateRelationship("physxDeformableBody:cookingSourceMesh").SetTargets([skin.GetPath()])
        _add_api(prim, PHYSX_BODY_API)
        for name, value_type, value in (
                ("physxDeformableBody:selfCollision", Sdf.ValueTypeNames.Bool, self_collision),
                ("physxDeformableBody:enableSpeculativeCCD", Sdf.ValueTypeNames.Bool, True),
                ("physxDeformableBody:solverPositionIterationCount", Sdf.ValueTypeNames.UInt, 16),
                ("physxDeformableBody:collisionPairUpdateFrequency", Sdf.ValueTypeNames.UInt, 4),
                ("physxDeformableBody:collisionIterationMultiplier", Sdf.ValueTypeNames.UInt, 4),
                # Seen live: undamped silk fluttered like exhaust smoke when the car under it moved.
                ("physxDeformableBody:linearDamping", Sdf.ValueTypeNames.Float, float(fabric["damping"]))):
            prim.CreateAttribute(name, value_type).Set(value)
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material(material), UsdShade.Tokens.weakerThanDescendants,
                                                     PHYSICS_PURPOSE)
        _add_api(prim, BODY_API)  # last, like NVIDIA's helper: this is what makes PhysX cook and simulate it

    def verify() -> str | None:
        prim = stage.GetPrimAtPath(root)
        if not prim or BODY_API not in _api_names(prim):
            return "the cloth body was not created"
        if not stage.GetPrimAtPath(root.AppendChild("SimMesh")):
            return "the simulation mesh was not created"
        bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(PHYSICS_PURPOSE)
        return None if bound and bound.GetPath() == material_path else "the fabric material was not bound"

    made = [root, materials_scope if scope_is_new else material_path] + ([] if scene is not None else [scene_path])
    return _apply(stage, "make_cloth", made, [], mutate, verify, undo_stack, f"Make cloth {root.name}", common)


# ------------------------------------------------------------------------------------------------ settle
# Captured draped shapes: {cloth path: {"points": [(x, y, z), ...], "source": "fabric" | "usd"}}. Stop puts PhysX's
# simulated points back to the pre-Play shape, so the shape is captured while paused and written after Stop.
_CAPTURED: dict[str, dict[str, Any]] = {}
MIN_MOVE_M = 0.001


def _cloth_mesh(stage: Any, path: str) -> Any:
    root = _prim(stage, path, "path")
    if BODY_API not in _api_names(root):
        raise _Refusal(f"{path} is not a cloth made by physics_make_cloth (no {BODY_API})")
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath(root.GetPath().AppendChild("Mesh")))
    if not mesh:
        raise _Refusal(f"{path} has no Mesh child, so there is no visible sheet to settle")
    return mesh


def _largest_move(rest: Any, points: Any) -> float:
    return max((math.dist(a, b) for a, b in zip(rest, points)), default=0.0)


def _live_points(usd: Any, stage: Any, mesh_path: str) -> tuple[list[tuple[float, float, float]], str]:
    """The sheet's simulated points: Fabric (where PhysX writes while it runs) via usdrt, else the USD points."""
    try:
        import usdrt  # Kit only

        rt_stage = usdrt.Usd.Stage.Attach(usd.get_context().get_stage_id())
        attr = rt_stage.GetPrimAtPath(mesh_path).GetAttribute("points")
        value = attr.Get() if attr else None
        if value is not None and len(value):
            return [tuple(float(c) for c in p) for p in value], "fabric"
    except Exception:  # noqa: BLE001 - no usdrt or no Fabric copy: fall back to USD
        pass
    value = UsdGeom.Mesh(stage.GetPrimAtPath(mesh_path)).GetPointsAttr().Get() or []
    return [tuple(float(c) for c in p) for p in value], "usd"


@_guard
def apply_settle(stage: Any, path: str, points: Any, *, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Make captured draped points the sheet's starting shape (Mesh points, extent and bind pose)."""
    _ready(stage)
    mesh = _cloth_mesh(stage, path)
    rest = mesh.GetPointsAttr().Get() or []
    if len(points) != len(rest):
        raise _Refusal(f"The captured shape has {len(points)} points but {path} has {len(rest)}: the cloth was rebuilt "
                       "since the capture. Play, pause when it has settled, capture again.")
    draped = Vt.Vec3fArray([Gf.Vec3f(*p) for p in points])
    mpu = _meters_per_unit(stage)
    moved = _largest_move(rest, draped) * mpu
    extent = UsdGeom.PointBased.ComputeExtent(draped)
    plan = {"cloth_root": path, "points": len(draped), "largest_move_m": round(moved, 3),
            "new_extent_m": [[round(v * mpu, 3) for v in corner] for corner in extent]}
    common = {"plan": plan, "warnings": [],
              "explanation": (f"{path} will start in its draped shape (points moved up to {moved:.2f} m), so after "
                              "Stop it rests on the car instead of hovering flat. Only the starting shape changes: "
                              "the fabric and its physics stay the same.")}
    if dry_run is not False:
        return _result("settle_cloth", next_step="Run again with dry_run=False to keep this shape (undoable).",
                       **common)

    def mutate() -> None:
        mesh.GetPointsAttr().Set(draped)
        mesh.GetExtentAttr().Set(extent)
        mesh.GetPrim().GetAttribute("deformablePose:default:omniphysics:points").Set(draped)

    def verify() -> str | None:
        return None if len(mesh.GetPointsAttr().Get() or []) == len(draped) else "the new shape was not written"

    return _apply(stage, "settle_cloth", [], [mesh.GetPath()], mutate, verify, undo_stack,
                  f"Settle cloth {Sdf.Path(path).name}", common)


def settle_cloth(path: str = "/World/Cloth", *, undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Kit only, two steps. 1) Play until the cloth has draped, pause, call this: it captures the draped shape (no
    scene change). 2) Press Stop, call again (dry_run=False to write): the captured shape becomes the cloth's
    starting shape, so the scene opens with the cover already lying on the car. Undoable."""
    from . import joint_control  # local: keeps this module importable without Kit

    usd = joint_control._kit_module("omni.usd")
    if usd is None:
        return {"ok": False, "dry_run": True, "error": "Kit not available: run this inside Isaac Sim or Composer."}
    stage = usd.get_context().get_stage()
    state, problem = joint_control._timeline_state()
    if problem:
        return {"ok": False, "dry_run": True, "error": problem}
    try:
        mesh = _cloth_mesh(stage, path)
    except _Refusal as exc:
        return {"ok": False, "dry_run": True, "error": str(exc)}
    if state != "stopped":
        points, source = _live_points(usd, stage, str(mesh.GetPath()))
        moved = _largest_move(mesh.GetPointsAttr().Get() or [], points) * _meters_per_unit(stage)
        if moved < MIN_MOVE_M:
            return {"ok": False, "dry_run": True, "step": "capture", "source": source,
                    "error": "The cloth has not moved from its starting shape yet. Let it play until it has draped "
                             "(a few seconds), pause, then capture again."}
        _CAPTURED[path] = {"points": points, "source": source}
        return {"ok": True, "dry_run": True, "step": "captured", "written": False, "points": len(points),
                "source": source, "largest_move_m": round(moved, 3),
                "next_step": "Press Stop (sim_stop), then call physics_cloth_settle again: it previews, and "
                             "dry_run=False keeps this shape as the starting shape."}
    captured = _CAPTURED.get(path)
    if captured is None:
        return {"ok": False, "dry_run": True, "step": "capture",
                "error": "Nothing captured yet. Press Play, wait until the cloth has draped, pause, and call this "
                         "first; then Stop and call it again."}
    result = apply_settle(stage, path, captured["points"], undo_stack=undo_stack, dry_run=dry_run)
    if result.get("applied"):
        _CAPTURED.pop(path, None)
    return result


def _look(stage: Any, path: Any, fabric: dict[str, Any], mesh: Any) -> None:
    """A real material (UsdPreviewSurface) on the visible sheet, so it takes light and reflections from any sky or
    lamp instead of showing a flat display colour."""
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path.AppendChild("Surface"))
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*fabric["color"]))
    shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(fabric["roughness"]))
    shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    UsdShade.MaterialBindingAPI.Apply(mesh).Bind(material)


def _pose(prim: Any, points: Any) -> None:
    """OmniPhysicsDeformablePoseAPI:default with the bindPose purpose (and, for the visible mesh, its rest points)."""
    _add_api(prim, POSE_API)
    prim.CreateAttribute("deformablePose:default:omniphysics:purposes", Sdf.ValueTypeNames.TokenArray,
                         variability=Sdf.VariabilityUniform).Set(["bindPose"])
    if points is not None:
        prim.CreateAttribute("deformablePose:default:omniphysics:points", Sdf.ValueTypeNames.Point3fArray).Set(
            Vt.Vec3fArray(points))
