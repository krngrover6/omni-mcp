"""scene_build_from_spec: validate-first, one undoable edit, verified after, safe to re-run; plus check and export."""
from __future__ import annotations

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import copy
import tempfile
from pathlib import Path
from unittest import mock

from omniverse_scene_mcp import features, spec_builder as sb
from omniverse_scene_mcp.undo import UndoStack

CAR = '#usda 1.0\n(\n    defaultPrim = "Car"\n)\ndef Xform "Car"\n{\n    def Cube "Body"\n    {\n    }\n}\n'
SPEC = {
    "meters_per_unit": 0.01, "up_axis": "Y",
    "materials": [{"path": "/World/Looks/Red", "color": [0.8, 0.05, 0.05], "roughness": 0.3, "metallic": 0.9},
                  {"path": "/World/Looks/Blue", "color": [0.05, 0.1, 0.8]},
                  {"path": "/World/Looks/Floor", "color": [0.2, 0.2, 0.2], "roughness": 0.8}],
    "prims": [
        {"path": "/World/Showroom/Floor", "type": "Cube", "translate": [0, -1, 0], "scale": [500, 1, 500],
         "attributes": {"size": 2}, "material": "/World/Looks/Floor"},
        {"path": "/World/Showroom/Car", "type": "Xform", "kind": "component", "reference": "./car.usda",
         "rotate": [0, 90, 0],
         "variants": {"paint": {"select": "red", "options": {"red": {"material": "/World/Looks/Red"},
                                                              "blue": {"material": "/World/Looks/Blue"}}}}},
        {"path": "/World/Showroom/Key", "type": "DistantLight", "attributes": {"inputs:intensity": 3000,
                                                                               "label": {"type": "string",
                                                                                         "value": "key"}}},
        {"path": "/World/Showroom/Hero", "type": "Camera", "translate": [0, 150, 600],
         "attributes": {"focalLength": 50}},
    ],
}


def _stage(tmp):
    (Path(tmp) / "car.usda").write_text(CAR)
    stage = Usd.Stage.CreateNew(str(Path(tmp) / "showroom.usda"))
    UsdGeom.SetStageUpAxis(stage, "Y")
    UsdGeom.SetStageMetersPerUnit(stage, 0.01)
    return stage


def test_preview_then_build_then_verify_then_rerun_is_a_no_op():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _stage(tmp)
        before = stage.GetRootLayer().ExportToString()
        preview = sb.build_from_spec(stage, SPEC)
        assert preview["ok"] and preview["dry_run"] and not preview["written"]
        assert preview["counts"] == {"create": 7, "update": 0, "ok": 0}
        assert "/World/Showroom" in " ".join(preview["warnings"])  # parents made as Xforms, said up front
        assert stage.GetRootLayer().ExportToString() == before  # a preview writes nothing
        built = sb.build_from_spec(stage, SPEC, dry_run=False)
        assert built["ok"] and built["written"] and "everything matches" in built["verified"]
        floor = stage.GetPrimAtPath("/World/Showroom/Floor")
        assert floor.GetTypeName() == "Cube" and UsdGeom.Cube(floor).GetSizeAttr().Get() == 2.0
        assert str(UsdShade.MaterialBindingAPI(floor).ComputeBoundMaterial()[0].GetPath()) == "/World/Looks/Floor"
        car = stage.GetPrimAtPath("/World/Showroom/Car")
        assert car.GetChild("Body") and Usd.ModelAPI(car).GetKind() == "component"  # the reference came in
        assert car.GetVariantSets().GetVariantSet("paint").GetVariantSelection() == "red"
        assert str(UsdShade.MaterialBindingAPI(car).ComputeBoundMaterial()[0].GetPath()) == "/World/Looks/Red"
        assert stage.GetPrimAtPath("/World/Showroom/Key").GetAttribute("label").Get() == "key"
        assert sb.check_spec(stage, SPEC)["passed"]
        again = sb.build_from_spec(stage, SPEC, dry_run=False)
        assert again["ok"] and not again["written"] and "nothing to do" in again["next_step"]
        changed = copy.deepcopy(SPEC)
        changed["prims"][1]["variants"]["paint"]["select"] = "blue"
        changed["prims"][3]["attributes"]["focalLength"] = 35
        update = sb.build_from_spec(stage, changed)
        assert update["counts"]["update"] == 2 and update["counts"]["create"] == 0  # only what differs
        check = sb.check_spec(stage, changed)
        assert not check["passed"] and check["different"] == 2


def test_one_undo_removes_the_whole_build():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _stage(tmp)
        stack = UndoStack()
        assert sb.build_from_spec(stage, SPEC, undo_stack=stack, dry_run=False)["written"]
        assert stage.GetPrimAtPath("/World/Showroom/Car")
        result = stack.undo_last(stage=stage, dry_run=False)
        assert result.get("ok"), result
        assert not stage.GetPrimAtPath("/World/Showroom") and not stage.GetPrimAtPath("/World/Looks/Red")


def test_every_spec_mistake_is_reported_before_anything_is_written():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _stage(tmp)
        UsdGeom.Mesh.Define(stage, "/World/Existing")
        before = stage.GetRootLayer().ExportToString()
        bad = {"meters_per_unit": 1, "prims": [
            {"path": "/World/A", "type": "Cub"},
            {"path": "/World/B", "type": "Cube", "attributes": {"sise": 2}},
            {"path": "/World/C", "type": "Xform", "reference": "./missing.usda"},
            {"path": "/World/Existing", "type": "Cube"},
            {"path": "/World/D", "type": "Sphere", "material": "/World/Looks/Nope", "translat": [0, 0, 0]},
            {"path": "World/E", "type": "Cube"},
        ]}
        result = sb.build_from_spec(stage, bad, dry_run=False)
        text = " | ".join(result["errors"])
        assert not result["ok"] and not result["written"]
        for expected in ("metersPerUnit 1", "did you mean Cube", "'sise'", "missing.usda", "exists as Mesh",
                         "/World/Looks/Nope", "did you mean translate", "World/E"):
            assert expected in text, (expected, text)
        assert stage.GetRootLayer().ExportToString() == before


def test_a_failure_halfway_rolls_everything_back():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _stage(tmp)
        before = stage.GetRootLayer().ExportToString()
        real = sb._apply

        def half(stage_, spec, plan):
            real(stage_, {"materials": spec["materials"], "prims": []}, {**plan, "steps": []})
            raise RuntimeError("disk full")

        with mock.patch.object(sb, "_apply", side_effect=half):
            result = sb.build_from_spec(stage, SPEC, dry_run=False)
        assert not result["ok"] and "rolled back" in result["error"]
        assert stage.GetRootLayer().ExportToString() == before


def test_spec_from_scene_round_trips_and_tools_are_registered():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _stage(tmp)
        sb.build_from_spec(stage, SPEC, dry_run=False)
        exported = sb.spec_from_scene(stage, "/World/Showroom")
        paths = [p["path"] for p in exported["spec"]["prims"]]
        assert "/World/Showroom/Car" in paths and "/World/Showroom/Car/Body" not in paths  # asset insides skipped
        assert sb.check_spec(stage, exported["spec"])["passed"]
    assert features.FEATURES["scene_build_from_spec"].mutates
    assert not features.FEATURES["scene_check_spec"].mutates and not features.FEATURES["scene_spec_from_scene"].mutates
    assert features.signature("scene_build_from_spec").parameters["dry_run"].default is True


def test_composer_orient_stacks_are_read_exported_and_edited():
    # Seen live 2026-11-26: Composer places prims with translate + orient (quaternion) + scale. NVIDIA's
    # CL6_Line_Full.usd does it for every machine; the exporter used to DROP those positions silently.
    from pxr import Gf

    stage = Usd.Stage.CreateInMemory()
    machine = UsdGeom.Xform.Define(stage, "/World/N_02")
    machine.AddTranslateOp().Set(Gf.Vec3d(492.2, -1957.9, 0))
    machine.AddOrientOp().Set(Gf.Quatf(sb._compose((0, 0, 90)).GetQuat()))
    machine.AddScaleOp().Set(Gf.Vec3f(1, 1, 1))
    exported = sb.spec_from_scene(stage, "/World")
    entry = next(p for p in exported["spec"]["prims"] if p["path"] == "/World/N_02")
    assert entry["translate"] == [492.2, -1957.9, 0] and abs(entry["rotate"][2] - 90) < 1e-4
    assert exported["transforms_not_exported"] == []
    assert sb.check_spec(stage, exported["spec"])["passed"]
    move = {"prims": [{"path": "/World/N_02", "translate": [600, -1957.9, 0], "rotate": [0, 0, 180]}]}
    assert sb.build_from_spec(stage, move)["counts"] == {"create": 0, "update": 1, "ok": 0}
    assert sb.build_from_spec(stage, move, dry_run=False)["written"]
    ops = [op.GetOpName() for op in machine.GetOrderedXformOps()]
    assert ops == ["xformOp:translate", "xformOp:orient", "xformOp:scale"]  # Composer's stack kept, not replaced
    expected = Gf.Matrix4d().SetRotate(sb._compose((0, 0, 180))) * Gf.Matrix4d().SetTranslate(Gf.Vec3d(600, -1957.9, 0))
    got = machine.GetLocalTransformation()
    assert all(abs(got[i][j] - expected[i][j]) < 1e-3 for i in range(4) for j in range(4))
    same_turn = {"prims": [{"path": "/World/N_02", "rotate": [0, 0, -180]}]}  # same turn, other numbers
    assert sb.build_from_spec(stage, same_turn)["counts"]["ok"] == 1


def test_matrix_transforms_are_never_dropped_silently():
    from pxr import Gf

    stage = Usd.Stage.CreateInMemory()
    prim = UsdGeom.Xform.Define(stage, "/World/Cad")
    prim.AddTransformOp().Set(Gf.Matrix4d().SetTranslate(Gf.Vec3d(1, 2, 3)))
    assert sb.spec_from_scene(stage, "/World")["transforms_not_exported"] == ["/World/Cad"]
    result = sb.build_from_spec(stage, {"prims": [{"path": "/World/Cad", "translate": [0, 0, 0]}]})
    assert not result["ok"] and "cannot edit safely" in result["errors"][0]


def test_match_units_scales_and_turns_a_referenced_asset_like_the_metrics_assembler():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "pallet.usda").write_text('#usda 1.0\n(\n    defaultPrim = "Pallet"\n    metersPerUnit = 1\n'
                                               '    upAxis = "Y"\n)\ndef Xform "Pallet"\n{\n}\n')
        stage = Usd.Stage.CreateNew(str(Path(tmp) / "factory.usda"))
        UsdGeom.SetStageMetersPerUnit(stage, 0.01)
        UsdGeom.SetStageUpAxis(stage, "Z")
        spec = {"prims": [{"path": "/World/Pallet", "type": "Xform", "reference": "./pallet.usda",
                           "match_units": True}]}
        preview = sb.build_from_spec(stage, spec)
        assert "scale x100" in " ".join(preview["warnings"]) and "rotate [90.0, 0.0, 0.0]" in " ".join(preview["warnings"])
        assert spec["prims"][0]["match_units"] is True and "scale" not in spec["prims"][0]  # caller's spec untouched
        assert sb.build_from_spec(stage, spec, dry_run=False)["written"]
        xform = UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Pallet"))
        _, rotate, scale, _, _ = xform.GetXformVectors(Usd.TimeCode.Default())
        assert tuple(scale) == (100, 100, 100) and tuple(rotate) == (90, 0, 0)
        assert sb.check_spec(stage, spec)["passed"]  # the same spec checks out after building
        no_arc = sb.build_from_spec(stage, {"prims": [{"path": "/World/X", "type": "Xform", "match_units": True}]})
        assert not no_arc["ok"] and "needs a reference" in " ".join(no_arc["errors"])
