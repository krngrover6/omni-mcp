"""Phase 3 assets: asset_scaffold, scene_set_pivot, mesh_check, scene_scatter."""
from __future__ import annotations

try:
    from pxr import Gf, Usd, UsdGeom, Vt
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import tempfile
from pathlib import Path

from omniverse_scene_mcp import asset_structure as a, features
from omniverse_scene_mcp.undo import UndoStack


def test_asset_scaffold_builds_entry_payload_look_geo_and_never_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "cad.usda").write_text('#usda 1.0\n(\n    defaultPrim = "Part"\n)\ndef Xform "Part"\n{\n'
                                            '    def Cube "Body"\n    {\n    }\n}\n')
        preview = a.asset_scaffold(tmp, "N_08_Conveyor", geometry=str(Path(tmp) / "cad.usda"))
        assert preview["dry_run"] and not (Path(tmp) / "N_08_Conveyor").exists()
        made = a.asset_scaffold(tmp, "N_08_Conveyor", geometry=str(Path(tmp) / "cad.usda"), dry_run=False)
        assert made["ok"] and made["checks"]["is_model"] and made["checks"]["kind"] == "component"
        assert made["geometry_reference"] == "../cad.usda"
        stage = Usd.Stage.Open(str(Path(tmp) / "N_08_Conveyor" / "N_08_Conveyor.usda"))
        assert UsdGeom.GetStageUpAxis(stage) == "Z" and UsdGeom.GetStageMetersPerUnit(stage) == 0.001
        assert stage.GetPrimAtPath("/N_08_Conveyor/Geometry/Body") and stage.GetPrimAtPath("/N_08_Conveyor/Looks")
        payload = Usd.Stage.Open(str(Path(tmp) / "N_08_Conveyor" / "N_08_Conveyor_payload.usda"))
        assert [l.GetDisplayName() for l in payload.GetLayerStack(False)][1:] == \
            ["N_08_Conveyor_look.usda", "N_08_Conveyor_geo.usda"]  # look is stronger than geo
        stage.Unload("/N_08_Conveyor")
        assert not stage.GetPrimAtPath("/N_08_Conveyor/Geometry")  # the payload can be left unloaded
        assert not a.asset_scaffold(tmp, "N_08_Conveyor", dry_run=False)["ok"]  # never overwrites
        assert not a.asset_scaffold(tmp, "bad name")["ok"]


def test_set_pivot_moves_nothing_for_both_transform_stacks():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    simple = UsdGeom.Cube.Define(stage, "/World/Simple")
    simple.GetSizeAttr().Set(2.0)
    UsdGeom.XformCommonAPI(simple).SetTranslate(Gf.Vec3d(5, 0, 0))
    composer = UsdGeom.Cube.Define(stage, "/World/Composer")  # translate + orient + scale, like Composer
    composer.AddTranslateOp().Set(Gf.Vec3d(0, 5, 0))
    composer.AddOrientOp().Set(Gf.Quatf(Gf.Rotation(Gf.Vec3d(0, 0, 1), 30).GetQuat()))
    composer.AddScaleOp().Set(Gf.Vec3f(2, 2, 2))
    for path in ("/World/Simple", "/World/Composer"):
        prim = stage.GetPrimAtPath(path)
        before = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)
        preview = a.scene_set_pivot(stage, path)
        assert preview["dry_run"] and preview["pivot"] == [0, 0, -1]  # bottom centre of a size-2 cube
        result = a.scene_set_pivot(stage, path, undo_stack=UndoStack(), dry_run=False)
        assert result["written"], result
        after = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(0)
        assert Gf.IsClose(before, after, 1e-6)
        names = [op.GetOpName() for op in UsdGeom.Xformable(prim).GetOrderedXformOps()]
        assert "xformOp:translate:pivot" in names and names[-1] == "!invert!xformOp:translate:pivot"
    composer_ops = [op.GetOpName() for op in composer.GetOrderedXformOps()]
    assert composer_ops == ["xformOp:translate", "xformOp:translate:pivot", "xformOp:orient", "xformOp:scale",
                            "!invert!xformOp:translate:pivot"]
    assert not a.scene_set_pivot(stage, "/World/Nope")["ok"]


def test_mesh_check_finds_broken_faces_indices_normals_and_extent():
    stage = Usd.Stage.CreateInMemory()
    good = UsdGeom.Mesh.Define(stage, "/World/Good")
    good.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)])
    good.CreateFaceVertexCountsAttr([4])
    good.CreateFaceVertexIndicesAttr([0, 1, 2, 3])
    good.CreateExtentAttr(UsdGeom.PointBased.ComputeExtent(good.GetPointsAttr().Get()))
    UsdGeom.PrimvarsAPI(good).CreatePrimvar("st", Vt.Vec2fArray().GetElementType() if False else
                                            __import__("pxr").Sdf.ValueTypeNames.TexCoord2fArray,
                                            UsdGeom.Tokens.faceVarying).Set([(0, 0), (1, 0), (1, 1), (0, 1)])
    bad = UsdGeom.Mesh.Define(stage, "/World/Bad")
    bad.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 1, 0)])
    bad.CreateFaceVertexCountsAttr([3, 2])  # a collinear (zero-area) triangle and a 2-corner face
    bad.CreateFaceVertexIndicesAttr([0, 1, 2, 0, 3])
    bad.CreateNormalsAttr([(0, 0, 1)] * 2)
    bad.SetNormalsInterpolation(UsdGeom.Tokens.vertex)  # needs 4 (one per point)
    bad.CreateExtentAttr([(0, 0, 0), (0.5, 0.5, 0)])  # smaller than the points
    result = a.mesh_check(stage)
    assert result["meshes"] == 2 and result["clean"] == 1 and result["without_uvs"] == 1
    issues = " | ".join(result["problems"][0]["issues"])
    for expected in ("fewer than 3 corners", "zero-area", "2 normals", "extent is smaller"):
        assert expected in issues, (expected, issues)
    oob = UsdGeom.Mesh.Define(stage, "/World/OutOfRange")
    oob.CreatePointsAttr([(0, 0, 0), (1, 0, 0), (0, 1, 0)])
    oob.CreateFaceVertexCountsAttr([3])
    oob.CreateFaceVertexIndicesAttr([0, 1, 7])
    assert any("points past" in i for p in a.mesh_check(stage, "/World/OutOfRange")["problems"] for i in p["issues"])


def test_scatter_places_copies_as_one_point_instancer_with_unit_fix():
    with tempfile.TemporaryDirectory() as tmp:
        # a 1.2 x 0.8 m pallet in metres, scattered into a cm stage (the course factory's units)
        (Path(tmp) / "pallet.usda").write_text('#usda 1.0\n(\n    defaultPrim = "Pallet"\n    metersPerUnit = 1\n'
                                               '    upAxis = "Z"\n)\ndef Cube "Pallet"\n{\n    double size = 1\n'
                                               '    float3 xformOp:scale = (1.2, 0.8, 0.15)\n'
                                               '    uniform token[] xformOpOrder = ["xformOp:scale"]\n}\n')
        stage = Usd.Stage.CreateNew(str(Path(tmp) / "factory.usda"))
        UsdGeom.SetStageMetersPerUnit(stage, 0.01)
        UsdGeom.SetStageUpAxis(stage, "Z")
        grid = a.scene_scatter(stage, "/World/Pallets", "./pallet.usda", count=6, area=[[0, 0], [400, 400]])
        assert grid["dry_run"] and grid["placed"] == 6 and grid["units_fix"] == {"scale": 100.0, "rotate": None}
        assert abs(grid["prototype_size"][0] - 120) < 1e-3 and abs(grid["spacing"] - 120) < 1e-3
        done = a.scene_scatter(stage, "/World/Pallets", "./pallet.usda", count=6, area=[[0, 0], [400, 400]],
                               undo_stack=UndoStack(), dry_run=False)
        assert done["written"]
        instancer = UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/Pallets"))
        assert len(instancer.GetPositionsAttr().Get()) == 6
        box = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_]).ComputeWorldBound(
            stage.GetPrimAtPath("/World/Pallets/Prototypes/Proto0")).ComputeAlignedRange()
        assert abs(box.GetSize()[0] - 120) < 1e-3  # a real 120 cm pallet in the cm stage
        random_one = a.scene_scatter(stage, "/World/Field", "./pallet.usda", count=8, area=[[0, 0], [1000, 1000]],
                                     layout="random", random_yaw=True, dry_run=False)
        assert random_one["written"], random_one
        # list(): slicing a Vt array returned None in usd-core 26.08 (seen 2026-11-26)
        pts = list(UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/Field")).GetPositionsAttr().Get())
        gaps = [(p - q).GetLength() for i, p in enumerate(pts) for q in pts[i + 1:]]
        assert random_one["placed"] == 8 and min(gaps) >= random_one["spacing"] - 1e-3
        assert not a.scene_scatter(stage, "/World/Field", "./pallet.usda", area=[[0, 0], [1, 1]])["ok"]  # exists
        tight = a.scene_scatter(stage, "/World/Tight", "./pallet.usda", count=50, area=[[0, 0], [200, 200]])
        assert tight["placed"] < 50 and "fit" in tight["warning"]
    assert features.FEATURES["scene_scatter"].mutates and not features.FEATURES["mesh_check"].mutates
