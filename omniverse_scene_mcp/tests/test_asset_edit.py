"""asset_edit: material_replace in an override layer, asset_repoint to local files, asset_collect, kind fix spec."""
from __future__ import annotations

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import tempfile
from pathlib import Path

from omniverse_scene_mcp import asset_audit, asset_edit as e, features, spec_builder
from omniverse_scene_mcp.undo import UndoStack


def _machine(tmp):
    """A machine asset whose parts use Paint_Eggshell_White, like the course's N_02_PCB_Router."""
    asset = Usd.Stage.CreateNew(str(Path(tmp) / "router.usda"))
    root = UsdGeom.Xform.Define(asset, "/Router").GetPrim()
    asset.SetDefaultPrim(root)
    white = UsdShade.Material.Define(asset, "/Router/Looks/Paint_Eggshell_White")
    UsdShade.Material.Define(asset, "/Router/Looks/Black_Matte")
    for name in ("SideWallL", "SideWallR", "Frame"):
        part = UsdGeom.Cube.Define(asset, f"/Router/{name}").GetPrim()
        UsdShade.MaterialBindingAPI.Apply(part).Bind(white)
    asset.Save()
    return Path(tmp) / "router.usda"


def _scene(tmp, override=True):
    _machine(tmp)
    stage = Usd.Stage.CreateNew(str(Path(tmp) / "N_02.usda"))
    library = UsdShade.Material.Define(stage, "/World/Looks/m_PolishedSteel")
    stage.DefinePrim("/World/Router").GetReferences().AddReference("./router.usda")
    if override:
        layer = Sdf.Layer.CreateNew(str(Path(tmp) / "MaterialOverride.usda"))
        stage.GetRootLayer().subLayerPaths.insert(0, "./MaterialOverride.usda")
    stage.GetRootLayer().Save()
    return stage, library


def test_material_replace_rebinds_in_the_override_layer_and_leaves_the_asset_alone():
    with tempfile.TemporaryDirectory() as tmp:
        stage, _ = _scene(tmp)
        asset_before = (Path(tmp) / "router.usda").read_text()
        preview = e.material_replace(stage, "Paint_Eggshell_White", "/World/Looks/m_PolishedSteel",
                                     layer="MaterialOverride.usda")
        assert preview["ok"] and preview["dry_run"] and preview["prims"] == 3
        assert preview["layer"] == "MaterialOverride.usda"
        stack = UndoStack()
        done = e.material_replace(stage, "Paint_Eggshell_White", "/World/Looks/m_PolishedSteel",
                                  layer="MaterialOverride.usda", undo_stack=stack, dry_run=False)
        assert done["written"], done
        wall = stage.GetPrimAtPath("/World/Router/SideWallL")
        assert str(UsdShade.MaterialBindingAPI(wall).ComputeBoundMaterial()[0].GetPath()) == "/World/Looks/m_PolishedSteel"
        override = Sdf.Layer.Find(str(Path(tmp) / "MaterialOverride.usda"))
        assert override.GetPrimAtPath("/World/Router/SideWallL") is not None  # the over lives in the override layer
        assert stage.GetRootLayer().GetPrimAtPath("/World/Router/SideWallL") is None
        assert (Path(tmp) / "router.usda").read_text() == asset_before  # the asset file is untouched
        assert stack.undo_last(stage=stage, dry_run=False)["ok"]
        assert str(UsdShade.MaterialBindingAPI(wall).ComputeBoundMaterial()[0].GetPath()).endswith("Paint_Eggshell_White")


def test_material_replace_reports_errors_and_instance_locks():
    with tempfile.TemporaryDirectory() as tmp:
        stage, _ = _scene(tmp)
        assert not e.material_replace(stage, "Nope", "/World/Looks/m_PolishedSteel")["ok"]
        assert not e.material_replace(stage, "Paint_Eggshell_White", "/World/Looks/Missing")["ok"]
        missing_layer = e.material_replace(stage, "Paint_Eggshell_White", "/World/Looks/m_PolishedSteel",
                                           layer="Other.usda")
        assert not missing_layer["ok"] and "scene_add_sublayer" in missing_layer["error"]
        stage.GetPrimAtPath("/World/Router").SetInstanceable(True)
        locked = e.material_replace(stage, "Paint_Eggshell_White", "/World/Looks/m_PolishedSteel")
        assert locked["prims"] == 0 and locked["inside_instances"] == 3 and "Instanceable" in locked["warning"]


def test_asset_repoint_moves_a_cloud_reference_to_the_local_copy():
    with tempfile.TemporaryDirectory() as tmp:
        local = _machine(tmp)
        stage = Usd.Stage.CreateNew(str(Path(tmp) / "FactoryProps.usda"))
        cloud = "https://omniverse-content.example/Props/router.usd"
        stage.DefinePrim("/World/Cart").GetReferences().AddReference(cloud)
        preview = e.asset_repoint(stage, cloud, str(local))
        assert preview["dry_run"] and preview["changes"][0]["new"] == "./router.usda"
        assert not e.asset_repoint(stage, cloud, str(Path(tmp) / "nothere.usda"))["ok"]
        done = e.asset_repoint(stage, cloud, str(local), undo_stack=UndoStack(), dry_run=False)
        assert done["written"] and not done["empty_after"]
        assert stage.GetPrimAtPath("/World/Cart/SideWallL")  # the local file's contents came in
        refs = stage.GetRootLayer().GetPrimAtPath("/World/Cart").referenceList.GetAddedOrExplicitItems()
        assert [r.assetPath for r in refs] == ["./router.usda"]


def test_asset_collect_copies_dependencies_and_never_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src"
        src.mkdir()
        (src / "tex").mkdir()
        (src / "tex" / "wood.png").write_bytes(b"\x89PNG fake")
        (src / "body.usda").write_text('#usda 1.0\ndef Xform "Body"\n{\n}\n')
        (src / "pallet.usda").write_text(
            '#usda 1.0\n(\n    defaultPrim = "Pallet"\n)\ndef Xform "Pallet" (\n    prepend references = @./body.usda@\n)\n'
            '{\n    asset inputs:tex = @./tex/wood.png@\n}\n')
        dest = Path(tmp) / "Assets" / "Props" / "Pallets"
        preview = e.asset_collect(str(src / "pallet.usda"), str(dest))
        assert preview["dry_run"] and preview["files"] == 3
        done = e.asset_collect(str(src / "pallet.usda"), str(dest), dry_run=False)
        assert done["written"] and (dest / "body.usda").is_file() and (dest / "tex" / "wood.png").is_file()
        assert not e.asset_collect(str(src / "pallet.usda"), str(dest), dry_run=False)["ok"]  # no overwrite


def test_kind_check_fix_spec_repairs_the_hierarchy_through_the_spec_builder():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    machine = UsdGeom.Xform.Define(stage, "/World/Machine").GetPrim()
    Usd.ModelAPI(machine).SetKind("assembly")
    report = asset_audit.kind_check(stage)
    assert report["broken_models"] == 1 and report["fix_spec"] == {"prims": [{"path": "/World", "kind": "group"}]}
    assert spec_builder.build_from_spec(stage, report["fix_spec"], dry_run=False)["written"]
    assert asset_audit.kind_check(stage)["broken_models"] == 0
    assert features.FEATURES["material_replace"].mutates and features.FEATURES["asset_collect"].mutates


def test_override_sublayer_weaker_than_the_root_binding_is_refused_in_the_preview():
    # Seen live 2026-11-26 on the course's N_02_PCB_Router.usd: its bindings are written in the ROOT layer, so a
    # MaterialOverride.usd sublayer (weaker) cannot win. The preview must say so instead of rolling back after a yes.
    with tempfile.TemporaryDirectory() as tmp:
        stage = Usd.Stage.CreateNew(str(Path(tmp) / "N_02.usda"))
        white = UsdShade.Material.Define(stage, "/World/Material/Paint_Eggshell_White")
        UsdShade.Material.Define(stage, "/World/Material/Steel_Stainless")
        for name in ("WallL", "WallR"):
            UsdShade.MaterialBindingAPI.Apply(UsdGeom.Cube.Define(stage, f"/World/Machine/{name}").GetPrim()).Bind(white)
        Sdf.Layer.CreateNew(str(Path(tmp) / "MaterialOverride.usda"))
        stage.GetRootLayer().subLayerPaths.insert(0, "./MaterialOverride.usda")
        stage.GetRootLayer().Save()
        blocked = e.material_replace(stage, "/World/Material/Paint_Eggshell_White", "/World/Material/Steel_Stainless",
                                     layer="MaterialOverride.usda")
        assert not blocked["ok"] and blocked["blocked_by_stronger_layer"] == {"N_02.usda": 2}
        assert "STRONGER" in blocked["error"] and "REFERENCED" in blocked["fix"]
        # The fix it suggests: reference the asset into a wrapper; local beats reference, the asset stays untouched.
        before = (Path(tmp) / "N_02.usda").read_text()
        wrapper = Usd.Stage.CreateNew(str(Path(tmp) / "N_02_Steel.usda"))
        wrapper.DefinePrim("/World").GetReferences().AddReference("./N_02.usda", "/World")
        done = e.material_replace(wrapper, "/World/Material/Paint_Eggshell_White", "/World/Material/Steel_Stainless",
                                  dry_run=False)
        assert done["written"] and done["prims"] == 2
        wall = wrapper.GetPrimAtPath("/World/Machine/WallL")
        assert str(UsdShade.MaterialBindingAPI(wall).ComputeBoundMaterial()[0].GetPath()) == "/World/Material/Steel_Stainless"
        assert (Path(tmp) / "N_02.usda").read_text() == before


def test_material_library_copies_materials_with_relative_mdl_paths_and_never_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "Source").mkdir()
        (Path(tmp) / "Source" / "Steel_Stainless.mdl").write_text("mdl 1.0;")
        src = Usd.Stage.CreateNew(str(Path(tmp) / "Material.usda"))
        material = UsdShade.Material.Define(src, "/Material_ver3/Steel_Stainless")
        shader = UsdShade.Shader.Define(src, "/Material_ver3/Steel_Stainless/Shader")
        shader.SetSourceAsset(Sdf.AssetPath("./Source/Steel_Stainless.mdl"), "mdl")
        material.CreateSurfaceOutput("mdl").ConnectToSource(shader.ConnectableAPI(), "out")
        src.Save()
        (Path(tmp) / "Library").mkdir()
        dest = Path(tmp) / "Library" / "MaterialLibrary.usda"
        wanted = [{"source": str(Path(tmp) / "Material.usda"), "prim": "/Material_ver3/Steel_Stainless",
                   "name": "m_PolishedSteel"}]
        assert e.material_library_create(str(dest), wanted)["dry_run"] and not dest.exists()
        made = e.material_library_create(str(dest), wanted, dry_run=False)
        assert made["ok"] and made["materials"][0]["mdl"] == "../Source/Steel_Stainless.mdl"  # relative, rebased
        assert made["materials"][0]["mdl_found"] and made["materials"][0]["shader"]
        library = Usd.Stage.Open(str(dest))
        copied = UsdShade.Material(library.GetPrimAtPath("/World/Looks/m_PolishedSteel"))
        assert str(copied.ComputeSurfaceSource("mdl")[0].GetPath()) == "/World/Looks/m_PolishedSteel/Shader"
        assert library.GetDefaultPrim().GetName() == "World"
        again = e.material_library_create(str(dest), wanted)
        assert not again["ok"] and "nothing is overwritten" in again["errors"][0]
        bad = e.material_library_create(str(dest), [{"source": str(Path(tmp) / "Material.usda"),
                                                     "prim": "/Material_ver3", "name": "m X"}])
        assert not bad["ok"]


def test_material_audit_lists_materials_from_outside_the_subtree():
    stage = Usd.Stage.CreateInMemory()
    steel = UsdShade.Material.Define(stage, "/World/MaterialLibrary/Looks/m_PolishedSteel")
    part = UsdGeom.Cube.Define(stage, "/World/Line/N_02/Wall").GetPrim()
    UsdShade.MaterialBindingAPI.Apply(part).Bind(steel)
    used = asset_audit.material_audit(stage, "/World/Line/N_02")["used"]
    assert [(u["name"], u["used_by"]) for u in used] == [("m_PolishedSteel", 1)]
