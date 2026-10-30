"""Cloth: preview changes nothing; apply writes NVIDIA's surface-deformable layout; undo removes all of it."""
from __future__ import annotations

try:
    from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import cloth, features
from omniverse_scene_mcp.undo import UndoStack


def _stage(up=UsdGeom.Tokens.z, mpu=1.0):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up)
    UsdGeom.SetStageMetersPerUnit(stage, mpu)
    UsdGeom.Xform.Define(stage, "/World")
    return stage


def _floor(stage, path="/World/Floor", size=4.0):
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr().Set(1.0)
    cube.AddScaleOp().Set(Gf.Vec3f(size, size, 0.1))
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return cube


def _text(stage):
    return stage.GetRootLayer().ExportToString()


def test_preview_plans_a_cloth_and_changes_nothing():
    stage = _stage()
    before = _text(stage)
    preview = cloth.make_cloth(stage, "/World/Cloth", resolution=10)
    assert preview["ok"] and preview["dry_run"] and not preview["written"] and _text(stage) == before
    plan = preview["plan"]
    assert plan["triangles"] == 200 and plan["position_m"] == [0.0, 0.0, 1.0] and plan["size_m"] == 1.0
    assert plan["physics_scene"] == {"path": "/World/PhysicsScene", "new": True}
    assert plan["material"] == "/World/PhysicsMaterials/ClothFabric" and plan["preset"] == "cotton"
    assert any("fall forever" in w for w in preview["warnings"])
    assert "surface deformable" in preview["explanation"] and "dry_run=False" in preview["next_step"]
    compile(preview["python"], "<cloth>", "exec")


def test_apply_writes_nvidias_layout_and_undo_removes_it():
    stage = _stage()
    _floor(stage)
    before = _text(stage)
    stack = UndoStack()
    made = cloth.make_cloth(stage, "/World/Cloth", resolution=6, position=[1, 2, 1.5], turn=90, preset="silk",
                            undo_stack=stack, dry_run=False)
    assert made["applied"] and made["undo"]["recorded"], made
    assert not any("fall forever" in w for w in made["warnings"])
    root = stage.GetPrimAtPath("/World/Cloth")
    assert cloth._api_names(root) == [cloth.AUTO_API, cloth.PHYSX_BODY_API, "MaterialBindingAPI", cloth.BODY_API]
    assert [str(t) for t in root.GetRelationship("physxDeformableBody:cookingSourceMesh").GetTargets()] == [
        "/World/Cloth/Mesh"]
    assert root.GetAttribute("physxDeformableBody:selfCollision").Get() is True
    assert root.GetAttribute("physxDeformableBody:collisionIterationMultiplier").Get() == 4
    xform = UsdGeom.Xformable(root)
    assert xform.ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation() == Gf.Vec3d(1, 2, 1.5)

    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Cloth/Mesh"))
    assert len(mesh.GetPointsAttr().Get()) == 49 and set(mesh.GetFaceVertexCountsAttr().Get()) == {3}
    assert len(mesh.GetFaceVertexIndicesAttr().Get()) == 6 * 6 * 6 and mesh.GetDoubleSidedAttr().Get()
    assert all(abs(p[2]) < 1e-9 for p in mesh.GetPointsAttr().Get())  # flat on the ground plane (Z up)
    assert cloth.POSE_API in cloth._api_names(mesh.GetPrim())
    assert not mesh.GetSubdivisionSchemeAttr().HasAuthoredValue()  # smooth shading, not flat facets
    look, _ = UsdShade.MaterialBindingAPI(mesh.GetPrim()).ComputeBoundMaterial()
    assert look.GetPath() == "/World/Cloth/Look"
    surface = UsdShade.Shader(stage.GetPrimAtPath("/World/Cloth/Look/Surface"))
    assert surface.GetIdAttr().Get() == "UsdPreviewSurface"
    assert abs(surface.GetInput("roughness").Get() - 0.35) < 1e-6  # silk is a little shiny
    assert abs(root.GetAttribute("physxDeformableBody:linearDamping").Get() - 0.8) < 1e-6
    assert mesh.GetPrim().GetAttribute("deformablePose:default:omniphysics:purposes").Get() == ["bindPose"]
    assert len(mesh.GetPrim().GetAttribute("deformablePose:default:omniphysics:points").Get()) == 49

    sim = stage.GetPrimAtPath("/World/Cloth/SimMesh")
    assert cloth.SIM_API in cloth._api_names(sim) and sim.HasAPI(UsdPhysics.CollisionAPI)
    assert UsdGeom.Imageable(sim).GetPurposeAttr().Get() == UsdGeom.Tokens.guide
    assert not UsdGeom.Mesh(sim).GetPointsAttr().HasAuthoredValue()  # PhysX cooks it at Play

    material = stage.GetPrimAtPath("/World/PhysicsMaterials/ClothFabric")
    assert set(cloth.MATERIAL_APIS) <= set(cloth._api_names(material))
    assert abs(material.GetAttribute("omniphysics:surfaceStretchStiffness").Get() - 500) < 1e-6
    assert abs(material.GetAttribute("omniphysics:surfaceThickness").Get() - 0.001) < 1e-9
    bound, _ = UsdShade.MaterialBindingAPI(root).ComputeBoundMaterial("physics")
    assert bound.GetPath() == material.GetPath()
    look, _ = UsdShade.MaterialBindingAPI(root).ComputeBoundMaterial()
    assert not look  # only the physics purpose: the colour stays the mesh's displayColor

    scene = UsdPhysics.Scene(stage.GetPrimAtPath("/World/PhysicsScene"))
    assert scene and scene.GetGravityDirectionAttr().Get() == Gf.Vec3f(0, 0, -1)
    assert stack.undo_last(stage=stage, dry_run=False)["ok"] and _text(stage) == before


def test_drape_over_hangs_above_the_prim_and_warns_without_a_collider():
    stage = _stage()
    table = UsdGeom.Cube.Define(stage, "/World/Table")
    table.CreateSizeAttr().Set(1.0)
    table.AddTranslateOp().Set(Gf.Vec3d(2, 0, 0.5))
    preview = cloth.make_cloth(stage, "/World/Cloth", drape_over="/World/Table")
    plan = preview["plan"]
    assert [round(v, 6) for v in plan["position_m"]] == [2.0, 0.0, 1.3] and plan["size_m"] == 1.6
    assert plan["drape_over"] == {"prim": "/World/Table", "has_collider": False}
    assert any("falls straight through" in w for w in preview["warnings"])
    UsdPhysics.CollisionAPI.Apply(table.GetPrim())
    ready = cloth.make_cloth(stage, "/World/Cloth", drape_over="/World/Table", size=3, position=[0, 0, 2])
    assert ready["plan"]["size_m"] == 3 and ready["plan"]["position_m"] == [0, 0, 2]  # given values win
    assert not ready["warnings"]
    UsdGeom.Xform.Define(stage, "/World/Empty")
    assert "no size" in cloth.make_cloth(stage, drape_over="/World/Empty")["error"]


def test_y_up_and_centimetre_stages():
    stage = _stage(up=UsdGeom.Tokens.y, mpu=0.01)
    _floor(stage)
    made = cloth.make_cloth(stage, "/World/Cloth", size=2, resolution=4, dry_run=False)
    assert made["applied"] and any("metres per unit" in w for w in made["warnings"])
    points = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Cloth/Mesh")).GetPointsAttr().Get()
    assert all(abs(p[1]) < 1e-9 for p in points) and max(p[0] for p in points) == 100  # 1 m = 100 cm, XZ plane
    translate = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cloth")).ComputeLocalToWorldTransform(
        Usd.TimeCode.Default()).ExtractTranslation()
    assert translate == Gf.Vec3d(0, 100, 0)
    scene = UsdPhysics.Scene(stage.GetPrimAtPath("/World/PhysicsScene"))
    assert scene.GetGravityDirectionAttr().Get() == Gf.Vec3f(0, -1, 0)
    assert abs(scene.GetGravityMagnitudeAttr().Get() - 981) < 1e-3
    material = stage.GetPrimAtPath("/World/PhysicsMaterials/ClothFabric")
    assert abs(material.GetAttribute("omniphysics:surfaceThickness").Get() - 0.2) < 1e-6


def test_reuses_the_physics_scene_and_material_folder_and_warns_about_cpu_physics():
    stage = _stage()
    _floor(stage)
    scene = UsdPhysics.Scene.Define(stage, "/World/Physics")
    scene.GetPrim().CreateAttribute("physxScene:enableGPUDynamics", __import__("pxr").Sdf.ValueTypeNames.Bool).Set(False)
    first = cloth.make_cloth(stage, "/World/Flag", dry_run=False)
    assert first["applied"] and first["plan"]["physics_scene"] == {"path": "/World/Physics", "new": False}
    assert any("GPU dynamics" in w for w in first["warnings"])
    assert not stage.GetPrimAtPath("/World/PhysicsScene")
    UsdGeom.Xform.Define(stage, "/World/Other")
    second = cloth.make_cloth(stage, "/World/Other/Flag")
    assert second["plan"]["material"] == "/World/PhysicsMaterials/FlagFabric_01"  # FlagFabric is taken


def test_refusals():
    stage = _stage()
    UsdGeom.Xform.Define(stage, "/World/Cloth")
    assert "already exists" in cloth.make_cloth(stage, "/World/Cloth")["error"]
    assert "does not exist yet" in cloth.make_cloth(stage, "/World/Shelf/Cloth")["error"]
    assert "preset" in cloth.make_cloth(stage, "/World/C", preset="velvet")["error"]
    assert "resolution" in cloth.make_cloth(stage, "/World/C", resolution=200)["error"]
    assert "resolution" in cloth.make_cloth(stage, "/World/C", resolution=True)["error"]
    assert "at least" in cloth.make_cloth(stage, "/World/C", size=0.01)["error"]
    assert "true or false" in cloth.make_cloth(stage, "/World/C", self_collision="yes")["error"]
    assert "three numbers" in cloth.make_cloth(stage, "/World/C", position=[1, 2])["error"]
    assert "absolute prim path" in cloth.make_cloth(stage, "Cloth")["error"]
    assert "drape_over" in cloth.make_cloth(stage, "/World/C", drape_over="/World/Nope")["error"]
    assert "color" in cloth.make_cloth(stage, "/World/C", color=[1, 0])["error"]
    assert "at most 1" in cloth.make_cloth(stage, "/World/C", color=[2, 0, 0])["error"]
    assert "damping" in cloth.make_cloth(stage, "/World/C", damping=-1)["error"]
    custom = cloth.make_cloth(stage, "/World/C", color=[0.8, 0.1, 0.1], damping=2)["plan"]
    assert custom["look"]["color"] == [0.8, 0.1, 0.1] and custom["fabric"]["damping"] == 2
    heavy = cloth.make_cloth(stage, "/World/C", resolution=100)
    assert heavy["ok"] and any("heavy for a 6 GB GPU" in w for w in heavy["warnings"])


def test_settle_captures_while_paused_and_writes_the_draped_shape_after_stop():
    from types import SimpleNamespace

    from omniverse_scene_mcp import joint_control

    stage = _stage()
    _floor(stage)
    cloth.make_cloth(stage, "/World/Cloth", resolution=4, dry_run=False)
    mesh = UsdGeom.Mesh(stage.GetPrimAtPath("/World/Cloth/Mesh"))
    rest = list(mesh.GetPointsAttr().Get())
    draped = [(p[0], p[1], p[2] - 0.5) for p in rest]  # as if it fell 0.5 m
    state = {"now": "paused"}
    usd = SimpleNamespace(get_context=lambda: SimpleNamespace(get_stage=lambda: stage, get_stage_id=lambda: 0))
    saved = (joint_control._kit_module, joint_control._timeline_state, cloth._live_points)
    joint_control._kit_module = lambda name: usd if name == "omni.usd" else None
    joint_control._timeline_state = lambda: (state["now"], None)
    try:
        cloth._live_points = lambda *_: (rest, "fabric")
        assert "not moved" in cloth.settle_cloth("/World/Cloth")["error"]  # still flat: nothing to capture
        cloth._live_points = lambda *_: (draped, "fabric")
        captured = cloth.settle_cloth("/World/Cloth")
        assert captured["step"] == "captured" and captured["largest_move_m"] == 0.5 and not captured["written"]
        assert list(mesh.GetPointsAttr().Get()) == rest  # capturing changes nothing
        state["now"] = "stopped"
        before = _text(stage)
        preview = cloth.settle_cloth("/World/Cloth")
        assert preview["ok"] and preview["dry_run"] and _text(stage) == before
        stack = UndoStack()
        done = cloth.settle_cloth("/World/Cloth", undo_stack=stack, dry_run=False)
        assert done["applied"] and done["plan"]["largest_move_m"] == 0.5
        assert [round(p[2], 6) for p in mesh.GetPointsAttr().Get()] == [round(p[2], 6) for p in draped]
        pose = mesh.GetPrim().GetAttribute("deformablePose:default:omniphysics:points").Get()
        assert round(pose[0][2], 6) == round(draped[0][2], 6)
        assert "Nothing captured" in cloth.settle_cloth("/World/Cloth")["error"]  # used up
        assert stack.undo_last(stage=stage, dry_run=False)["ok"] and _text(stage) == before
        assert "not a cloth" in cloth.settle_cloth("/World/Floor")["error"]
    finally:
        joint_control._kit_module, joint_control._timeline_state, cloth._live_points = saved
        cloth._CAPTURED.clear()
    assert "rebuilt" in cloth.apply_settle(stage, "/World/Cloth", draped[:3])["error"]


def test_registered_as_a_previewing_stage_tool():
    spec = features.FEATURES["physics_make_cloth"]
    assert spec.mutates and spec.location == "stage" and spec.group == "Assets & physics"
    stage = _stage()
    before = _text(stage)
    preview = features.invoke("physics_make_cloth", {"path": "/World/Cloth", "preset": "denim"}, stage=stage)
    assert preview["ok"] and preview["dry_run"] and _text(stage) == before
    made = features.invoke("physics_make_cloth", {"path": "/World/Cloth", "dry_run": "false"}, stage=stage)
    assert made["dry_run"] and _text(stage) == before  # only literal False writes
