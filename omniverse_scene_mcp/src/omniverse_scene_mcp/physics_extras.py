"""Session 3-4 helpers: payloads (add, list, load/unload), physics materials, and rigid-body settings.

Edits preview by default and write only with dry_run=False, through the same snapshot / verify / rollback / undo
path as joints.py. Names follow UsdPhysics and Isaac Sim 6.1's PhysxSchema (omni.usd.schema.physx 110.3):
PhysicsMaterialAPI (staticFriction, dynamicFriction, restitution, density), PhysxMaterialAPI combine modes,
RigidBodyAPI (velocity, angularVelocity, startsAsleep) and PhysxRigidBodyAPI (disableGravity, linearDamping,
angularDamping, enableCCD). PhysX schemas are written by name so they also work without PhysxSchema installed.
"""
from __future__ import annotations

from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdPhysics, UsdShade
except ImportError:  # pragma: no cover
    Gf = Sdf = Usd = UsdPhysics = UsdShade = None

from .joints import (_Refusal, _add_api, _api_names, _apply, _guard, _has_collider, _number, _prim, _ready, _result,
                     _vec)

COMBINE_MODES = ("average", "min", "multiply", "max")
MATERIAL_API, RIGID_API = "PhysxMaterialAPI", "PhysxRigidBodyAPI"
MAX_LOAD_PATHS = 200
PHYSICS_PURPOSE = "physics"  # material binding purpose that UsdPhysics reads (there is no UsdShade token for it)


# ------------------------------------------------------------------------------------------------ payloads
@_guard
def add_payload(stage: Any, prim_path: str, asset_path: str, *, translation: Any = None, rotation: Any = None,
                scale: Any = None, asset_prim_path: str | None = None, correct_units: Any = False,
                undo_stack: Any = None, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False add) an asset file as a payload at prim_path: like a reference, and you can
    still override it, but it can be unloaded to save memory. Same checks as scene_add_asset (file found, units,
    defaultPrim)."""
    _ready(stage)
    from . import asset_library

    options = dict(translation=translation, rotation=rotation, scale=scale, asset_prim_path=asset_prim_path,
                   correct_units=correct_units is True, as_payload=True)
    preview = asset_library.add_reference(stage, prim_path, asset_path, dry_run=True, **options)
    plan = preview.get("would_create")
    if plan is not None:
        preview["plan"] = plan
    if not preview.get("ok") or dry_run is not False:
        preview.setdefault("explanation", "A payload brings the asset in like a reference, but it can be unloaded "
                                          "(Stage window right-click > Unload, or scene_unload_payloads).")
        return preview
    path = Sdf.Path(plan["path"])
    outcome: dict[str, Any] = {}

    def mutate() -> None:
        result = asset_library.add_reference(stage, prim_path, asset_path, dry_run=False, **options)
        outcome.update(result)
        if not result.get("ok"):
            raise _Refusal(result.get("error", "the payload was not added"))

    def verify() -> str | None:
        prim = stage.GetPrimAtPath(path)
        return None if prim and prim.HasAuthoredPayloads() else "the payload was not added"

    common = {"plan": plan, "warnings": list(preview.get("warnings") or []),
              "explanation": f"{path} now loads {plan['reference']['asset_path']} as a payload."}
    return _apply(stage, "add_payload", [path], [], mutate, verify, undo_stack, f"Add payload {path.name}", common)


def _payload_assets(prim: Any) -> list[str]:
    out = []
    for spec in prim.GetPrimStack():
        for item in spec.payloadList.GetAddedOrExplicitItems():
            if item.assetPath:
                out.append(item.assetPath)
    return out


@_guard
def list_payloads(stage: Any) -> dict[str, Any]:
    """Read-only: every prim with a payload, which file it brings in, and whether it is loaded right now."""
    _ready(stage)
    rows = []
    for path in sorted(stage.FindLoadable(), key=str)[:MAX_LOAD_PATHS]:
        prim = stage.GetPrimAtPath(path)
        if not prim or prim.IsPseudoRoot():
            continue
        assets = _payload_assets(prim)
        if not assets and not prim.HasAuthoredPayloads():
            continue
        rows.append({"path": str(path), "loaded": prim.IsLoaded(), "assets": assets})
    loaded = sum(1 for r in rows if r["loaded"])
    return {"ok": True, "count": len(rows), "loaded": loaded, "unloaded": len(rows) - loaded, "payloads": rows,
            "note": "Load state is a viewing choice: it is not saved in your layers."}


def _load_change(stage: Any, paths: Any, load: bool, dry_run: Any) -> dict[str, Any]:
    _ready(stage)
    action = "load_payloads" if load else "unload_payloads"
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths or len(paths) > MAX_LOAD_PATHS:
        raise _Refusal(f"paths must be a list of 1 to {MAX_LOAD_PATHS} prim paths")
    loadable = {str(p) for p in stage.FindLoadable()}
    prims = [_prim(stage, path, "path") for path in paths]
    bad = [str(p.GetPath()) for p in prims if str(p.GetPath()) not in loadable]
    if bad:
        raise _Refusal(f"Not payload prims (nothing to {'load' if load else 'unload'}): {', '.join(bad[:5])}")
    changes = [str(p.GetPath()) for p in prims if p.IsLoaded() != load]
    plan = {"paths": [str(p.GetPath()) for p in prims], "will_change": changes,
            "already": [str(p.GetPath()) for p in prims if str(p.GetPath()) not in changes]}
    inverse = "scene_unload_payloads" if load else "scene_load_payloads"
    common = {"plan": plan, "warnings": [], "undo_with": f"{inverse}(paths={changes!r}, dry_run=False)",
              "explanation": ("Loading brings the payload's contents back into the scene." if load else
                              "Unloading hides the payload's contents and frees their memory; the prim stays in "
                              "the tree, and nothing is deleted.")}
    if dry_run is not False:
        return _result(action, next_step="Run again with dry_run=False to do it.", **common)
    for path in changes:
        if load:
            stage.Load(path)
        else:
            stage.Unload(path)
    wrong = [p for p in changes if stage.GetPrimAtPath(p).IsLoaded() != load]
    if wrong:
        return _result(action, error=f"Could not change the load state of {', '.join(wrong)}", dry_run=False, **common)
    return _result(action, applied=True, dry_run=False, **common)


@_guard
def load_payloads(stage: Any, paths: Any, *, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False do) loading payload prims, bringing their contents back."""
    return _load_change(stage, paths, True, dry_run)


@_guard
def unload_payloads(stage: Any, paths: Any, *, dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False do) unloading payload prims: their contents leave memory, the prims stay."""
    return _load_change(stage, paths, False, dry_run)


# ------------------------------------------------------------------------------------------------ physics materials
@_guard
def create_physics_material(stage: Any, name: str = "PhysicsMaterial", *, parent: str = "/World/PhysicsMaterials",
                            static_friction: Any = None, dynamic_friction: Any = None, restitution: Any = None,
                            density: Any = None, friction_combine: str | None = None,
                            restitution_combine: str | None = None, undo_stack: Any = None,
                            dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False create) a physics material: how a surface feels, not how it looks.
    static_friction (hard to start sliding), dynamic_friction (slows sliding), restitution (bounce, 0-1), density,
    and how two touching materials mix values (average, min, multiply, max). Bind it with scene_bind_physics_material."""
    _ready(stage)
    if not isinstance(name, str) or not Sdf.Path.IsValidIdentifier(name):
        raise _Refusal("name must be a valid prim name such as Rubber")
    if not isinstance(parent, str) or not Sdf.Path.IsValidPathString(parent) or not Sdf.Path(parent).IsAbsolutePath():
        raise _Refusal("parent must be an absolute prim path such as /World/PhysicsMaterials")
    values = {
        "staticFriction": _number(static_friction, "static_friction", low=0),
        "dynamicFriction": _number(dynamic_friction, "dynamic_friction", low=0),
        "restitution": _number(restitution, "restitution", low=0, high=1),
        "density": _number(density, "density", low=0),
    }
    modes = {"frictionCombineMode": friction_combine, "restitutionCombineMode": restitution_combine}
    for key, mode in modes.items():
        if mode is not None and mode not in COMBINE_MODES:
            raise _Refusal(f"{key} must be one of {', '.join(COMBINE_MODES)}")
    if all(v is None for v in values.values()) and all(m is None for m in modes.values()):
        raise _Refusal("Give at least one value, e.g. static_friction=0.8, dynamic_friction=0.6, restitution=0.2")
    warnings = []
    if values["dynamicFriction"] is not None and values["staticFriction"] is not None and \
            values["dynamicFriction"] > values["staticFriction"]:
        warnings.append("Dynamic friction is usually lower than static friction (sliding is easier than starting).")
    path = Sdf.Path(parent).AppendChild(name)
    if stage.GetPrimAtPath(path):
        raise _Refusal(f"{path} already exists; choose another name")
    plan = {"material_path": str(path), "values": {k: v for k, v in values.items() if v is not None},
            "combine_modes": {k: v for k, v in modes.items() if v is not None}}
    python = "\n".join([
        "# The same physics material in Python (Script Editor).",
        "import omni.usd",
        "from pxr import UsdPhysics, UsdShade",
        "stage = omni.usd.get_context().get_stage()",
        f"material = UsdShade.Material.Define(stage, {str(path)!r})",
        "physics = UsdPhysics.MaterialAPI.Apply(material.GetPrim())  # makes it a physics material",
    ] + [f"physics.Create{k[0].upper() + k[1:]}Attr().Set({v!r})" for k, v in plan["values"].items()])
    common = {"plan": plan, "warnings": warnings, "python": python,
              "explanation": f"A physics material at {path}. Bind it to colliders with scene_bind_physics_material."}
    if dry_run is not False:
        return _result("create_physics_material", next_step="Run again with dry_run=False to create it (undoable).",
                       **common)

    def mutate() -> None:
        material = UsdShade.Material.Define(stage, path)
        api = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
        for key, value in values.items():
            if value is not None:
                getattr(api, f"Create{key[0].upper() + key[1:]}Attr")().Set(value)
        if any(m is not None for m in modes.values()):
            prim = material.GetPrim()
            _add_api(prim, MATERIAL_API)
            for key, mode in modes.items():
                if mode is not None:
                    prim.CreateAttribute(f"physxMaterial:{key}", Sdf.ValueTypeNames.Token, variability=Sdf.VariabilityUniform).Set(mode)

    def verify() -> str | None:
        prim = stage.GetPrimAtPath(path)
        return None if prim and prim.HasAPI(UsdPhysics.MaterialAPI) else "the physics material was not created"

    return _apply(stage, "create_physics_material", [path], [], mutate, verify, undo_stack,
                  f"Create physics material {name}", common)


@_guard
def bind_physics_material(stage: Any, prim_path: str, material_path: str, *, undo_stack: Any = None,
                          dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False bind) a physics material to a prim with the 'physics' purpose, so it changes
    friction and bounce but not the look. It acts on the colliders at or below prim_path."""
    _ready(stage)
    prim = _prim(stage, prim_path, "prim_path")
    material_prim = _prim(stage, material_path, "material_path")
    if not material_prim.IsA(UsdShade.Material) or not material_prim.HasAPI(UsdPhysics.MaterialAPI):
        raise _Refusal(f"{material_path} is not a physics material; create one with scene_create_physics_material")
    warnings = [] if _has_collider(prim) else [f"{prim_path} has no collider, so the physics material has nothing to act on."]
    plan = {"prim_path": prim_path, "material_path": material_path, "purpose": "physics"}
    python = "\n".join([
        "# The same binding in Python (Script Editor).",
        "import omni.usd",
        "from pxr import UsdShade",
        "stage = omni.usd.get_context().get_stage()",
        f"prim = stage.GetPrimAtPath({prim_path!r})",
        f"material = UsdShade.Material(stage.GetPrimAtPath({material_path!r}))",
        "UsdShade.MaterialBindingAPI.Apply(prim).Bind(material, UsdShade.Tokens.weakerThanDescendants, "
        "'physics')  # physics purpose: feel, not look",
    ])
    common = {"plan": plan, "warnings": warnings, "python": python,
              "explanation": f"{prim_path} now uses {material_path} for friction and bounce; its look is unchanged."}
    if dry_run is not False:
        return _result("bind_physics_material", next_step="Run again with dry_run=False to bind it (undoable).", **common)

    def mutate() -> None:
        UsdShade.MaterialBindingAPI.Apply(prim).Bind(
            UsdShade.Material(material_prim), UsdShade.Tokens.weakerThanDescendants, PHYSICS_PURPOSE)

    def verify() -> str | None:
        bound, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial(PHYSICS_PURPOSE)
        return None if bound and bound.GetPath() == material_prim.GetPath() else "the physics binding was not made"

    return _apply(stage, "bind_physics_material", [], [prim.GetPath()], mutate, verify, undo_stack,
                  f"Bind physics material to {prim.GetName()}", common)


# ------------------------------------------------------------------------------------------------ rigid bodies
@_guard
def set_rigid_body_properties(stage: Any, prim_path: str, *, velocity: Any = None, angular_velocity: Any = None,
                              disable_gravity: Any = None, linear_damping: Any = None, angular_damping: Any = None,
                              enable_ccd: Any = None, starts_asleep: Any = None, undo_stack: Any = None,
                              dry_run: Any = True) -> dict[str, Any]:
    """Preview (or with dry_run=False set) a rigid body's settings: velocity [x, y, z] (stage units per second, e.g.
    launch a bowling ball) and angular_velocity (degrees per second), disable_gravity, linear/angular damping (like
    air resistance), enable_ccd (stops fast objects passing through thin ones) and starts_asleep."""
    _ready(stage)
    prim = _prim(stage, prim_path, "prim_path")
    if not prim.HasAPI(UsdPhysics.RigidBodyAPI):
        raise _Refusal(f"{prim_path} has no rigid body; add one first (scene_make_rigid_body)")
    changes: list[tuple[str, Any, Any, bool]] = []
    for attr, value in (("physics:velocity", velocity), ("physics:angularVelocity", angular_velocity)):
        if value is not None:
            changes.append((attr, Sdf.ValueTypeNames.Vector3f, Gf.Vec3f(_vec(value, attr.split(":")[1])), False))
    for attr, value in (("physxRigidBody:linearDamping", linear_damping), ("physxRigidBody:angularDamping", angular_damping)):
        number = _number(value, attr.split(":")[1], low=0)
        if number is not None:
            changes.append((attr, Sdf.ValueTypeNames.Float, number, True))
    for attr, value, physx in (("physxRigidBody:disableGravity", disable_gravity, True),
                               ("physxRigidBody:enableCCD", enable_ccd, True),
                               ("physics:startsAsleep", starts_asleep, False)):
        if value is None:
            continue
        if not isinstance(value, bool):
            raise _Refusal(f"{attr.split(':')[1]} must be true or false")
        changes.append((attr, Sdf.ValueTypeNames.Bool, value, physx))
    if not changes:
        raise _Refusal("Give at least one setting, e.g. velocity=[0, 5, 0] to launch it")
    plan = {"prim_path": prim_path,
            "set": {attr: (list(value) if hasattr(value, "__len__") else value) for attr, _, value, _ in changes},
            "units": {"velocity": "stage units per second", "angular_velocity": "degrees per second"}}
    warnings = []
    if velocity is not None and prim.GetAttribute("physics:kinematicEnabled") and prim.GetAttribute("physics:kinematicEnabled").Get():
        warnings.append("This body is kinematic, so physics ignores its velocity; turn kinematic off first.")
    python_lines = [
        "# The same settings in Python (Script Editor).",
        "import omni.usd",
        "from pxr import Gf, Sdf",
        "stage = omni.usd.get_context().get_stage()",
        f"prim = stage.GetPrimAtPath({prim_path!r})",
    ]
    if any(physx for *_, physx in changes):
        python_lines.append(f"prim.AddAppliedSchema({RIGID_API!r})  # PhysX rigid body settings")
    for attr, value_type, value, _ in changes:
        literal = f"Gf.Vec3f(*{list(value)!r})" if hasattr(value, "__len__") else repr(value)
        python_lines.append(f"prim.CreateAttribute({attr!r}, Sdf.ValueTypeNames.{_type_name(value_type)}).Set({literal})")
    common = {"plan": plan, "warnings": warnings, "python": "\n".join(python_lines),
              "explanation": "Rigid body settings for " + prim_path + ". They act when you press Play."}
    if dry_run is not False:
        return _result("set_rigid_body_properties", next_step="Run again with dry_run=False to set them (undoable).",
                       **common)

    def mutate() -> None:
        for attr, value_type, value, physx in changes:
            if physx:
                _add_api(prim, RIGID_API)
            prim.CreateAttribute(attr, value_type).Set(value)

    def verify() -> str | None:
        return None

    return _apply(stage, "set_rigid_body_properties", [], [prim.GetPath()], mutate, verify, undo_stack,
                  f"Set rigid body settings of {prim.GetName()}", common)


def _type_name(value_type: Any) -> str:
    return {"vector3f": "Vector3f", "float": "Float", "bool": "Bool"}.get(str(value_type), "Float")
