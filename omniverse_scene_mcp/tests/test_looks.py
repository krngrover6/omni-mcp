"""Phase 2 looks: mdl_info parsing, MDL materials and presets in specs, texture_check, material_bind_pattern."""
from __future__ import annotations

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import tempfile
from pathlib import Path
from unittest import mock

from omniverse_scene_mcp import asset_audit, asset_edit, features, mdl_info, spec_builder as sb

FAKE_PBR = '''mdl 1.6;
export material OmniPBR(
    // Albedo
    color diffuse_color_constant = color(0.2) [[
        anno::display_name("Albedo Color"), anno::in_group("Albedo")
    ]],
    uniform texture_2d diffuse_texture = texture_2d() [[ anno::display_name("Albedo Map") ]],
    float reflection_roughness_constant = float(0.5) [[ anno::hard_range(float(0.0), float(1.0)) ]],
    float metallic_constant = float(0.0),
    uniform texture_2d normalmap_texture = texture_2d(),
    uniform bool enable_emission = false,
    base::mono_mode opacity_mode = base::mono_average [[ anno::hidden() ]]
)
 = material();
'''


def _fake_install(tmp):
    root = Path(tmp)
    (root / "kit" / "mdl" / "core" / "Base").mkdir(parents=True)
    (root / "kit" / "mdl" / "core" / "Base" / "OmniPBR.mdl").write_text(FAKE_PBR)
    mdl_info._parse.cache_clear()
    return root


def test_mdl_info_reads_parameters_types_defaults_and_annotations():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(mdl_info, "default_root", return_value=_fake_install(tmp)):
            info = mdl_info.mdl_info("OmniPBR", include_hidden=True)
            params = {p["name"]: p for p in info["inputs"]}
            assert info["parameters"] == 7
            assert params["diffuse_color_constant"]["usd_type"] == "color3f"
            assert params["diffuse_color_constant"]["display_name"] == "Albedo Color"
            assert params["diffuse_texture"]["usd_type"] == "asset" and params["diffuse_texture"]["uniform"]
            assert params["reflection_roughness_constant"]["hard_range"] == ["float(0.0)", "float(1.0)"]
            assert "usd_type" not in params["opacity_mode"] and params["opacity_mode"]["hidden"]
            assert len(mdl_info.mdl_info("OmniPBR")["inputs"]) == 6  # hidden ones only on request
            assert not mdl_info.mdl_info("NoSuchMaterial")["ok"] and not mdl_info.mdl_info("../x")["ok"]
    assert mdl_info.texture_color_space("diffuse_texture") == "sRGB"
    assert mdl_info.texture_color_space("normalmap_texture") == "raw"
    assert mdl_info.texture_color_space("reflectionroughness_texture") == "raw"


def test_mdl_materials_are_written_like_composer_and_validated():
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(mdl_info, "default_root", return_value=_fake_install(tmp)):
            (Path(tmp) / "tex").mkdir()
            (Path(tmp) / "tex" / "albedo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (Path(tmp) / "tex" / "normal.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            stage = Usd.Stage.CreateNew(str(Path(tmp) / "scene.usda"))
            spec = {"materials": [{"path": "/World/Looks/m_Steel", "mdl": "OmniPBR",
                                   "inputs": {"diffuse_color_constant": [0.6, 0.6, 0.62], "metallic_constant": 1,
                                              "diffuse_texture": "./tex/albedo.png",
                                              "normalmap_texture": "./tex/normal.png"}},
                                  {"path": "/World/Looks/m_Chrome", "preset": "chrome"}]}
            assert sb.build_from_spec(stage, spec, dry_run=False)["written"]
            shader = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/m_Steel/Shader"))
            assert shader.GetSourceAsset("mdl").path == "OmniPBR.mdl"
            assert shader.GetSourceAssetSubIdentifier("mdl") == "OmniPBR"
            assert shader.GetInput("diffuse_texture").GetAttr().GetColorSpace() == "sRGB"
            assert shader.GetInput("normalmap_texture").GetAttr().GetColorSpace() == "raw"
            material = UsdShade.Material(stage.GetPrimAtPath("/World/Looks/m_Steel"))
            assert material.ComputeSurfaceSource("mdl")[0].GetPath() == shader.GetPath()
            assert sb.build_from_spec(stage, spec)["counts"] == {"create": 0, "update": 0, "ok": 2}  # re-run: nothing
            chrome = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/m_Chrome/Shader"))
            assert chrome.GetInput("metallic_constant").Get() == 1.0
            bad = sb.build_from_spec(stage, {"materials": [
                {"path": "/World/Looks/m_Bad", "mdl": "OmniPBR",
                 "inputs": {"diffuse_colour": [1, 0, 0], "opacity_mode": 1, "normalmap_texture": "./tex/missing.png"}},
                {"path": "/World/Looks/m_Mix", "mdl": "OmniPBR", "color": [1, 0, 0]},
                {"path": "/World/Looks/m_P", "preset": "gold_leaf"}]})
            text = " | ".join(bad["errors"])
            for expected in ("did you mean 'diffuse_color_constant'", "option list", "missing.png", "inputs",
                             "unknown preset"):
                assert expected in text, (expected, text)
    assert "chrome" in sb.look_presets()["presets"]


def test_texture_check_finds_missing_files_wrong_color_space_and_gpu_size():
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "normal.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (2048).to_bytes(4, "big")
                        + (1024).to_bytes(4, "big") + b"\x08\x06\x00\x00\x00")
        stage = Usd.Stage.CreateNew(str(Path(tmp) / "scene.usda"))
        shader = UsdShade.Shader.Define(stage, "/World/Looks/M/Shader")
        normal = shader.CreateInput("normalmap_texture", Sdf.ValueTypeNames.Asset).GetAttr()
        normal.Set(Sdf.AssetPath("./normal.png"))
        normal.SetColorSpace("sRGB")
        shader.CreateInput("diffuse_texture", Sdf.ValueTypeNames.Asset).Set(Sdf.AssetPath("./gone.png"))
        result = asset_audit.texture_check(stage)
        assert [m["file"] for m in result["missing"]] == ["./gone.png"]
        assert result["wrong_color_space"][0]["should_be"] == "raw"
        assert result["biggest"][0]["size"] == "2048x1024" and result["gpu_estimate_mb"] == 11


def test_material_bind_pattern_binds_matching_geometry_with_one_undo():
    stage = Usd.Stage.CreateInMemory()
    rubber = UsdShade.Material.Define(stage, "/World/Looks/m_Rubber")
    for name in ("Wheel_FL", "Wheel_FR", "Body"):
        UsdGeom.Cylinder.Define(stage, f"/World/Car/{name}")
    UsdGeom.Xform.Define(stage, "/World/Car/WheelGroup")
    preview = asset_edit.material_bind_pattern(stage, "wheel*", "/World/Looks/m_Rubber")
    assert preview["dry_run"] and preview["matched"] == 2  # the WheelGroup Xform is not geometry
    done = asset_edit.material_bind_pattern(stage, "WHEEL*", "/World/Looks/m_Rubber", dry_run=False)
    assert done["written"]
    for name in ("Wheel_FL", "Wheel_FR"):
        bound = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(f"/World/Car/{name}")).ComputeBoundMaterial()[0]
        assert bound.GetPath() == rubber.GetPath()
    assert not UsdShade.MaterialBindingAPI(stage.GetPrimAtPath("/World/Car/Body")).ComputeBoundMaterial()[0]
    assert not asset_edit.material_bind_pattern(stage, "door*", "/World/Looks/m_Rubber")["ok"]
    assert asset_edit.material_bind_pattern(stage, "wheel*", "/World/Looks/m_Rubber",
                                            geometry_only=False)["matched"] == 3
    for name in ("mdl_info", "look_presets", "texture_check"):
        assert not features.FEATURES[name].mutates
    assert features.FEATURES["material_bind_pattern"].mutates
