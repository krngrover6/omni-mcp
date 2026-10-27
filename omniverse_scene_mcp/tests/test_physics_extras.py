"""physics_extras: payloads, physics materials and rigid-body settings, previewed first and undoable."""
from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

try:
    from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features, nl_tier8, physics_extras as px
from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter
from omniverse_scene_mcp.service import SceneMCPService
from omniverse_scene_mcp.undo import UndoStack

ASSET = '#usda 1.0\n(\n    defaultPrim = "Crate"\n    metersPerUnit = 1\n    upAxis = "Z"\n)\ndef Cube "Crate"\n{\n}\n'


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.Xform.Define(stage, "/World")
    ball = UsdGeom.Sphere.Define(stage, "/World/Ball")
    UsdPhysics.RigidBodyAPI.Apply(ball.GetPrim())
    UsdPhysics.CollisionAPI.Apply(ball.GetPrim())
    UsdGeom.Cube.Define(stage, "/World/Decor")
    return stage


def _text(stage):
    return stage.GetRootLayer().ExportToString()


def test_payload_add_list_unload_load_and_undo():
    with tempfile.TemporaryDirectory() as tmp:
        asset = os.path.join(tmp, "crate.usda")
        Path(asset).write_text(ASSET, encoding="utf-8")
        stage = _stage()
        before = _text(stage)
        preview = px.add_payload(stage, "/World/Crate_01", asset)
        assert preview["ok"] and preview["dry_run"] and preview["plan"]["arc"] == "payload" and _text(stage) == before
        stack = UndoStack()
        made = px.add_payload(stage, "/World/Crate_01", asset, translation=[1, 0, 0], undo_stack=stack, dry_run=False)
        assert made["applied"] and made["undo"]["recorded"]
        prim = stage.GetPrimAtPath("/World/Crate_01")
        assert prim.HasAuthoredPayloads() and not prim.HasAuthoredReferences()
        listed = px.list_payloads(stage)
        assert listed["count"] == 1 and listed["payloads"][0]["loaded"] is True
        assert listed["payloads"][0]["assets"][0].endswith("crate.usda")
        assert px.unload_payloads(stage, ["/World/Crate_01"])["dry_run"] and prim.IsLoaded()
        off = px.unload_payloads(stage, ["/World/Crate_01"], dry_run=False)
        assert off["applied"] and not prim.IsLoaded() and px.list_payloads(stage)["unloaded"] == 1
        assert "scene_load_payloads" in off["undo_with"]
        assert stage.GetPrimAtPath("/World/Crate_01")  # unloading never deletes the prim
        on = px.load_payloads(stage, "/World/Crate_01", dry_run=False)
        assert on["applied"] and prim.IsLoaded()
        assert px.load_payloads(stage, "/World/Crate_01", dry_run=False)["plan"]["already"] == ["/World/Crate_01"]
        assert stack.undo_last(stage=stage, dry_run=False)["ok"] and _text(stage) == before


def test_payload_refusals():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _stage()
        assert not px.add_payload(stage, "/World/X", os.path.join(tmp, "missing.usda"))["ok"]
        assert "Not payload prims" in px.unload_payloads(stage, ["/World/Ball"])["error"]
        assert not px.load_payloads(stage, [])["ok"]
        assert not px.load_payloads(stage, ["/World/Nope"])["ok"]


def test_physics_material_create_and_bind_with_physics_purpose():
    stage = _stage()
    before = _text(stage)
    preview = px.create_physics_material(stage, "Rubber", static_friction=0.9, dynamic_friction=0.7, restitution=0.8)
    assert preview["dry_run"] and _text(stage) == before
    compile(preview["python"], "<snippet>", "exec")
    stack = UndoStack()
    made = px.create_physics_material(stage, "Rubber", static_friction=0.9, dynamic_friction=0.7, restitution=0.8,
                                      friction_combine="max", restitution_combine="min", undo_stack=stack, dry_run=False)
    material = stage.GetPrimAtPath("/World/PhysicsMaterials/Rubber")
    api = UsdPhysics.MaterialAPI(material)
    assert made["applied"] and abs(api.GetRestitutionAttr().Get() - 0.8) < 1e-6 and abs(api.GetStaticFrictionAttr().Get() - 0.9) < 1e-6
    assert px._api_names(material) == ["PhysicsMaterialAPI", "PhysxMaterialAPI"]
    assert material.GetAttribute("physxMaterial:frictionCombineMode").Get() == "max"
    bound = px.bind_physics_material(stage, "/World/Ball", "/World/PhysicsMaterials/Rubber", undo_stack=stack, dry_run=False)
    assert bound["applied"]
    rel = stage.GetPrimAtPath("/World/Ball").GetRelationship("material:binding:physics")
    assert [str(t) for t in rel.GetTargets()] == ["/World/PhysicsMaterials/Rubber"]
    look, _ = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/World/Ball")).ComputeBoundMaterial()
    assert not look  # the visual (all-purpose) binding is untouched
    decor = px.bind_physics_material(stage, "/World/Decor", "/World/PhysicsMaterials/Rubber")
    assert any("no collider" in w for w in decor["warnings"])
    assert stack.undo_last(stage=stage, dry_run=False)["ok"]
    assert stack.undo_last(stage=stage, dry_run=False)["ok"] and _text(stage) == before


def test_physics_material_validation():
    stage = _stage()
    assert "at most 1" in px.create_physics_material(stage, "X", restitution=1.5)["error"]
    assert not px.create_physics_material(stage, "X", static_friction=-1)["ok"]
    assert not px.create_physics_material(stage, "X")["ok"]
    assert not px.create_physics_material(stage, "bad name", restitution=0.1)["ok"]
    assert not px.create_physics_material(stage, "X", restitution=0.1, friction_combine="sum")["ok"]
    hint = px.create_physics_material(stage, "Ice", static_friction=0.1, dynamic_friction=0.5)
    assert any("usually lower" in w for w in hint["warnings"])
    px.create_physics_material(stage, "Dup", restitution=0.1, dry_run=False)
    assert "already exists" in px.create_physics_material(stage, "Dup", restitution=0.2)["error"]
    assert "not a physics material" in px.bind_physics_material(stage, "/World/Ball", "/World/Decor")["error"]


def test_rigid_body_settings_launch_a_ball():
    stage = _stage()
    before = _text(stage)
    stack = UndoStack()
    result = px.set_rigid_body_properties(stage, "/World/Ball", velocity=[0, 5, 0], angular_velocity=[0, 0, 360],
                                          disable_gravity=True, linear_damping=0.1, angular_damping=0.05,
                                          enable_ccd=True, starts_asleep=False, undo_stack=stack, dry_run=False)
    ball = stage.GetPrimAtPath("/World/Ball")
    assert result["applied"] and ball.GetAttribute("physics:velocity").Get() == Gf.Vec3f(0, 5, 0)
    assert ball.GetAttribute("physxRigidBody:disableGravity").Get() is True
    assert abs(ball.GetAttribute("physxRigidBody:linearDamping").Get() - 0.1) < 1e-6  # stored as a 32-bit float
    assert px._api_names(ball).count("PhysxRigidBodyAPI") == 1
    compile(result["python"], "<snippet>", "exec")
    assert stack.undo_last(stage=stage, dry_run=False)["ok"] and _text(stage) == before
    assert "no rigid body" in px.set_rigid_body_properties(stage, "/World/Decor", velocity=[1, 0, 0])["error"]
    assert not px.set_rigid_body_properties(stage, "/World/Ball")["ok"]
    assert not px.set_rigid_body_properties(stage, "/World/Ball", velocity=[1, 2])["ok"]
    assert not px.set_rigid_body_properties(stage, "/World/Ball", enable_ccd="yes")["ok"]
    assert not px.set_rigid_body_properties(stage, "/World/Ball", linear_damping=-1)["ok"]
    UsdPhysics.RigidBodyAPI(ball).CreateKinematicEnabledAttr().Set(True)
    warned = px.set_rigid_body_properties(stage, "/World/Ball", velocity=[1, 0, 0])
    assert any("kinematic" in w for w in warned["warnings"])


def test_registered_with_preview_defaults():
    assert features.FEATURES["scene_list_payloads"].mutates is False
    for name in ("scene_add_payload", "scene_load_payloads", "scene_unload_payloads", "scene_create_physics_material",
                 "scene_bind_physics_material", "scene_set_rigid_body_properties"):
        assert features.FEATURES[name].mutates and features.signature(name).parameters["dry_run"].default is True


def test_phrases_and_existing_routes():
    assert nl_tier8.parse("list payloads")["tool"] == "scene_list_payloads"
    unload = nl_tier8.parse("unload payload /World/Factory")
    assert unload == {"tool": "scene_unload_payloads", "arguments": {"paths": ["/World/Factory"], "dry_run": True},
                      "scene": True}
    assert nl_tier8.parse("load the payload at /World/Factory")["tool"] == "scene_load_payloads"
    spec = importlib.util.spec_from_file_location("round8_corpus", Path(__file__).with_name("test_round8_integration.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    phrases = module._corpus()
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    after = {phrase: router.route(phrase) for phrase in phrases}
    saved_read, saved_re = dict(nl_tier8._READ), nl_tier8._PAYLOAD
    for key in ("list payloads", "show payloads", "which payloads are loaded"):
        nl_tier8._READ.pop(key, None)
    import re as _re

    nl_tier8._PAYLOAD = _re.compile(r"(?!x)x")
    try:
        before = {phrase: router.route(phrase) for phrase in phrases}
    finally:
        nl_tier8._READ.clear()
        nl_tier8._READ.update(saved_read)
        nl_tier8._PAYLOAD = saved_re
    assert not [p for p in phrases if before[p] != after[p]]
