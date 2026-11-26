"""asset_audit: library inventory, KindChecker rule, material audit, instancing report, inventory export."""
from __future__ import annotations

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import tempfile
from pathlib import Path

from omniverse_scene_mcp import asset_audit as a, features

GOOD = '#usda 1.0\n(\n    defaultPrim = "Feeder"\n    metersPerUnit = 0.001\n    upAxis = "Z"\n)\ndef Xform "Feeder" (\n    kind = "component"\n)\n{\n}\n'
METRES = GOOD.replace("0.001", "1")
NO_DEFAULT = '#usda 1.0\n(\n    metersPerUnit = 0.001\n    upAxis = "Z"\n)\ndef Xform "Line" (\n    prepend references = @./missing.usda@\n)\n{\n}\n'


def test_inventory_flags_units_defaultprim_and_broken_paths():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "Machine_USD" / "N_03_Feeder").mkdir(parents=True)
        (Path(tmp) / "Machine_USD" / "N_03_Feeder" / "N_03_Feeder.usda").write_text(GOOD)
        (Path(tmp) / "metres.usda").write_text(METRES)
        (Path(tmp) / "line.usda").write_text(NO_DEFAULT)
        result = a.asset_library_inventory(tmp)
        rows = {r["file"]: r for r in result["assets"]}
        assert result["files"] == 3 and result["clean"] == 1
        feeder = rows["Machine_USD/N_03_Feeder/N_03_Feeder.usda"]
        assert feeder["kind"] == "component" and feeder["defaultPrim"] == "Feeder" and not feeder["problems"]
        assert "metersPerUnit 1" in rows["metres.usda"]["problems"][0]
        problems = " ".join(rows["line.usda"]["problems"])
        assert "no defaultPrim" in problems and "missing.usda" in problems
        assert a.asset_library_inventory(tmp, up_axis=None, meters_per_unit=None)["clean"] == 2
    assert not a.asset_library_inventory("no/such/folder")["ok"]


def test_kind_check_finds_models_under_non_group_parents():
    stage = Usd.Stage.CreateInMemory()
    world = UsdGeom.Xform.Define(stage, "/World").GetPrim()
    line = UsdGeom.Xform.Define(stage, "/World/Line").GetPrim()
    machine = UsdGeom.Xform.Define(stage, "/World/Line/Machine").GetPrim()
    UsdGeom.Xform.Define(stage, "/World/Line/Machine/Part")
    Usd.ModelAPI(machine).SetKind("component")
    Usd.ModelAPI(stage.GetPrimAtPath("/World/Line/Machine/Part")).SetKind("subcomponent")
    result = a.kind_check(stage)
    assert result["broken_models"] == 1 and result["problems"][0]["prim"] == "/World/Line/Machine"
    assert {f["path"] for f in result["fix_plan"]} == {"/World", "/World/Line"}  # subcomponent is never reported
    Usd.ModelAPI(world).SetKind("group")
    Usd.ModelAPI(line).SetKind("assembly")
    assert a.kind_check(stage)["broken_models"] == 0


def test_material_audit_counts_users_defining_file_and_unbound_geometry():
    stage = Usd.Stage.CreateInMemory()
    material = UsdShade.Material.Define(stage, "/World/Looks/m_PolishedSteel")
    shader = UsdShade.Shader.Define(stage, "/World/Looks/m_PolishedSteel/Shader")
    shader.SetSourceAsset(Sdf.AssetPath("./OmniPBR.mdl"), "mdl")
    shader.SetSourceAssetSubIdentifier("OmniPBR", "mdl")
    material.CreateSurfaceOutput("mdl").ConnectToSource(shader.ConnectableAPI(), "out")
    UsdShade.Material.Define(stage, "/World/Looks/m_Unused")
    for name in ("A", "B"):
        cube = UsdGeom.Cube.Define(stage, f"/World/{name}").GetPrim()
        UsdShade.MaterialBindingAPI.Apply(cube).Bind(material)
    UsdGeom.Cube.Define(stage, "/World/Bare")
    result = a.material_audit(stage)
    top = result["used"][0]
    assert top["name"] == "m_PolishedSteel" and top["used_by"] == 2 and top["shader"] == "MDL OmniPBR.mdl (OmniPBR)"
    assert result["unused_count"] == 1 and result["unused_examples"] == ["m_Unused"]
    assert result["unbound_examples"] == ["World/Bare"]


def test_instancing_report_counts_prototypes_and_suggests_repeated_assets():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "fence.usda").write_text(GOOD.replace("Feeder", "Fence"))
        stage = Usd.Stage.CreateNew(str(Path(tmp) / "factory.usda"))
        for i in range(4):
            prim = stage.DefinePrim(f"/World/Fence_{i}")
            prim.GetReferences().AddReference("./fence.usda")
        first = a.instancing_report(stage)
        assert first["PrototypeCount"] == 0 and first["candidates"][0]["uses"] == 4
        for i in range(4):
            stage.GetPrimAtPath(f"/World/Fence_{i}").SetInstanceable(True)
        after = a.instancing_report(stage)
        assert after["PrototypeCount"] == 1 and after["TotalInstanceCount"] == 4 and not after["candidates"]


def test_inventory_export_sees_tags_inside_instances_and_writes_a_new_csv_only():
    with tempfile.TemporaryDirectory() as tmp:
        asset = Usd.Stage.CreateNew(str(Path(tmp) / "router.usda"))
        router = UsdGeom.Xform.Define(asset, "/Router").GetPrim()
        part = UsdGeom.Xform.Define(asset, "/Router/Spindle").GetPrim()
        part.CreateAttribute("Unique_ID", Sdf.ValueTypeNames.Int).Set(105001)
        asset.SetDefaultPrim(router)
        asset.Save()
        stage = Usd.Stage.CreateNew(str(Path(tmp) / "line.usda"))
        line = UsdGeom.Xform.Define(stage, "/World/CL6_Line_Full").GetPrim()
        line.CreateAttribute("ProductionLine_ID", Sdf.ValueTypeNames.String).Set("PL_001")
        copy = stage.DefinePrim("/World/CL6_Line_Full/N_02_PCB_Router")
        copy.GetReferences().AddReference("./router.usda")
        copy.SetInstanceable(True)
        result = a.asset_inventory_export(stage)
        names = {r["Name"]: r for r in result["preview"]}
        assert result["dry_run"] and set(names) == {"CL6_Line_Full", "Spindle"}
        assert names["Spindle"]["InInstance"] and names["Spindle"]["Unique_ID"] == "105001"
        assert len(a.asset_inventory_export(stage, include_instances=False)["preview"]) == 1  # plain Traverse misses it
        out = Path(tmp) / "factory_inventory.csv"
        written = a.asset_inventory_export(stage, csv_path=str(out), dry_run=False)
        assert written["written"] == str(out) and "Unique_ID" in out.read_text().splitlines()[0]
        assert not a.asset_inventory_export(stage, csv_path=str(out), dry_run=False)["ok"]  # never overwrites
    for name in ("kind_check", "material_audit", "instancing_report", "asset_inventory_export", "asset_library_inventory"):
        assert features.FEATURES[name].mutates is False, name


def _machine(folder, mpu, size):
    """A Z-up asset whose cube is `size` numbers across, labelled metersPerUnit=mpu."""
    path = Path(folder) / f"machine_{size}.usda"
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.SetStageMetersPerUnit(stage, mpu)
    root = UsdGeom.Xform.Define(stage, "/Machine")
    UsdGeom.Cube.Define(stage, "/Machine/Body").GetSizeAttr().Set(float(size))
    stage.SetDefaultPrim(root.GetPrim())
    stage.GetRootLayer().Save()
    return str(path)


def _place(stage, path, asset, scale=None, rotate=None):
    prim = UsdGeom.Xform.Define(stage, path)
    prim.GetPrim().GetReferences().AddReference(asset)
    if rotate is not None:
        prim.AddXformOp(UsdGeom.XformOp.TypeRotateX, UsdGeom.XformOp.PrecisionDouble, "unitsResolve").Set(rotate)
    if scale is not None:
        prim.AddXformOp(UsdGeom.XformOp.TypeScale, UsdGeom.XformOp.PrecisionDouble, "unitsResolve").Set((scale,) * 3)


def test_units_check_finds_the_mm_labelled_metres_trap_and_missing_resolve():
    with tempfile.TemporaryDirectory() as tmp:
        wrong = _machine(tmp, 1.0, 2000)   # mm numbers labelled metres: the course's machines
        right = _machine(tmp, 0.001, 1500)  # honest mm
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(stage, "Y")
        UsdGeom.SetStageMetersPerUnit(stage, 0.01)
        _place(stage, "/World/Wrong", wrong, scale=100.0, rotate=-90.0)  # what Composer writes
        _place(stage, "/World/Good", right, scale=0.1, rotate=-90.0)
        _place(stage, "/World/Unresolved", right)  # referenced without the Metrics Assembler
        result = a.units_check(stage)
        rows = {row["prim"]: row for row in result["assets"]}
        assert result["ok"] and result["assets_checked"] == 3 and result["with_problems"] == 2
        assert rows["/World/Good"]["problems"] == [] and abs(rows["/World/Good"]["real_size_m"] - 1.5) < 1e-6
        wrong_row = rows["/World/Wrong"]
        assert wrong_row["real_size_m"] == 2000 and wrong_row["likely_label"][0]["unit"] == "mm"
        assert wrong_row["likely_label"][0]["fix_scale"] == 0.1 and "should be 0.1" in wrong_row["problems"][0]
        unresolved = " ".join(rows["/World/Unresolved"]["problems"])
        assert "no unitsResolve scale yet" in unresolved and "needs -90" in unresolved
        assert a.units_check(stage, prim="/World/Good")["assets_checked"] == 1
        assert not a.units_check(stage, prim="/World/Nope")["ok"]
    assert not features.FEATURES["units_check"].mutates
