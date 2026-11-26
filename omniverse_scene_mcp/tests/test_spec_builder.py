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
