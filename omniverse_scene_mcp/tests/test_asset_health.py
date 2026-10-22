"""asset_health on synthetic temp assets (healthy and broken, one per check) plus read-only smokes on real local assets."""

import json
import os
import struct
import tempfile
import time
import zlib
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import asset_health as health
from omniverse_scene_mcp.asset_health import asset_health, asset_health_file
from omniverse_scene_mcp.usd_insight import TimeBudget

ISAAC_PACK = Path("C:/Users/krngr/Omniverse/isaacsim-assets-6.1.0")
FRANKA_CANDIDATES = (
    "Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd",
    "Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaEmika/panda_instanceable.usd",
)
BAG_ROOT = Path("C:/Users/krngr/Omniverse/Bag_Assets")
BAG_CANDIDATES = ("Bag_A_Authored_1k/Assets/Backdrops/Backdrop_MarbleWall.usd",)
STATUSES = {"pass", "warn", "fail", "info"}
# Inside Kit (the bridge reload self-test) the real-asset smoke tests return at once: they open big files.
_IN_KIT = "carb" in __import__("sys").modules or "omni.kit.app" in __import__("sys").modules

HEADER = '#usda 1.0\n(\n    defaultPrim = "Bolt"\n    metersPerUnit = {mpu}\n    upAxis = "Z"\n)\n'
BOLT = """
def Xform "Bolt" (
    kind = "component"
)
{
    def Mesh "Body" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        float3[] extent = [(0, 0, 0), (1, 1, 0)]
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
        normal3f[] normals = [(0, 0, 1), (0, 0, 1), (0, 0, 1), (0, 0, 1)] (
            interpolation = "vertex"
        )
        uniform token subdivisionScheme = "none"
        rel material:binding = </Bolt/Looks/Steel>
    }

    def Scope "Looks"
    {
        def Material "Steel"
        {
            token outputs:surface.connect = </Bolt/Looks/Steel/Surface.outputs:surface>

            def Shader "Surface"
            {
                uniform token info:id = "UsdPreviewSurface"
                color3f inputs:diffuseColor.connect = </Bolt/Looks/Steel/Albedo.outputs:rgb>
                token outputs:surface
            }

            def Shader "Albedo"
            {
                uniform token info:id = "UsdUVTexture"
                asset inputs:file = @TEXTURE@
                token inputs:sourceColorSpace = "sRGB"
                float3 outputs:rgb
            }
        }
    }
}
"""


def _tempdir():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _png(width=4, height=2):
    raw = b"".join(b"\x00" + b"\x80\x80\x80" * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def _bolt_text(texture="./textures/steel_albedo.png", mpu=1, header=True, body=BOLT):
    text = body.replace("TEXTURE", texture)
    return (HEADER.format(mpu=mpu) if header else "#usda 1.0\n") + text


def _write(folder, files):
    for name, content in files.items():
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")


def _healthy_folder(folder, **bolt):
    _write(folder, {"bolt.usda": _bolt_text(**bolt), "textures/steel_albedo.png": _png()})
    return Path(folder) / "bolt.usda"


def _open(path):
    stage = Usd.Stage.Open(str(path))
    assert stage is not None
    return stage


def _check(result, check_id):
    return next(check for check in result["checks"] if check["id"] == check_id)


def _json(result):
    return json.loads(json.dumps(result))


def _snapshot(folder):
    return {str(path): path.read_bytes() for path in Path(folder).rglob("*") if path.is_file()}


def _memory_stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    world = UsdGeom.Xform.Define(stage, "/World").GetPrim()
    Usd.ModelAPI(world).SetKind("assembly")
    stage.SetDefaultPrim(world)
    return stage


def _material(stage, path="/World/Looks/Grey"):
    material = UsdShade.Material.Define(stage, path)
    shader = UsdShade.Shader.Define(stage, path + "/Surface")
    shader.CreateIdAttr("UsdPreviewSurface")
    material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    return material


def _quad(stage, path, scale=1.0, faces=1):
    mesh = UsdGeom.Mesh.Define(stage, path)
    points = [(0, 0, 0), (scale, 0, 0), (scale, scale, 0), (0, scale, 0)]
    mesh.CreatePointsAttr(points)
    mesh.CreateFaceVertexCountsAttr([4] * faces)
    mesh.CreateFaceVertexIndicesAttr([0, 1, 2, 3] * faces)
    mesh.CreateSubdivisionSchemeAttr("none")
    mesh.CreateNormalsAttr([(0, 0, 1)] * 4)
    mesh.SetNormalsInterpolation("vertex")
    mesh.CreateExtentAttr([(0, 0, 0), (scale, scale, 0)])
    return mesh


def _bind(prim, material):
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(material)


# --- structure and the healthy asset ---------------------------------------------------------------------------


def test_healthy_asset_scores_100_and_has_full_structure():
    with _tempdir() as folder:
        stage = _open(_healthy_folder(folder))
        result = _json(asset_health(stage))
        assert result["ok"] and result["healthy"], result
        assert result["score"] == 100 and result["grade"] == "A", [(c["id"], c["status"], c["examples"]) for c in result["checks"] if c["status"] != "pass"]
        for check in result["checks"]:
            assert check["status"] in STATUSES
            assert set(check) >= {"id", "title", "status", "count", "examples", "why", "how_to_fix", "kit_validator_rule"}
            assert len(check["examples"]) <= 10
        assert len({check["id"] for check in result["checks"]}) == len(result["checks"]) >= 25
        deps = result["dependencies"]
        assert deps["counts"]["resolved"] == 1 and deps["counts"]["unresolved"] == 0
        assert deps["resolved"][0]["usage"] == "texture"
        stats = result["stats"]
        assert stats["meshes"] == 1 and stats["total_faces"] == 1 and stats["total_points"] == 4
        assert stats["materials"] == 1 and stats["layers"] >= 1
        assert stats["textures"]["count"] == 1 and stats["textures"]["missing"] == 0
        assert stats["textures"]["formats"] == {"png": 1}
        assert stats["textures"]["largest"][0]["width"] == 4 and stats["textures"]["largest"][0]["height"] == 2
        assert stats["largest_meshes"][0]["path"] == "/Bolt/Body"
        assert stats["up_axis"] == "Z" and stats["meters_per_unit"] == 1 and stats["default_prim"] == "Bolt"
        assert _check(result, "shading_models")["status"] == "info"
        assert "UsdPreviewSurface: 1" in _check(result, "shading_models")["examples"][0]
        assert 3 <= result["summary"].count(". ") + 1 <= 6 and "100/100" in result["summary"]
        assert result["truncated"] is False and not stage.GetRootLayer().dirty


def test_health_checks_lists_kit_validator_equivalents():
    rules = {item["id"]: item["kit_validator_rule"] for item in health.health_checks()}
    assert rules["missing_files"] == "MissingReferenceChecker"
    assert rules["absolute_paths"] == "AnchoredAssetPathsChecker"
    assert rules["extents"] == "ExtentsChecker"
    assert rules["default_prim"] == "OmniDefaultPrimChecker"
    assert rules["instancing_opportunities"] is None
    json.dumps(health.health_checks())


# --- broken assets, one per check ---------------------------------------------------------------------------------


def test_missing_texture_fails_and_names_layer_and_site():
    with _tempdir() as folder:
        _write(folder, {"bolt.usda": _bolt_text()})  # no texture written
        result = _json(asset_health(_open(Path(folder) / "bolt.usda")))
        check = _check(result, "missing_files")
        assert check["status"] == "fail" and check["count"] == 1 and check["how_to_fix"]
        assert check["kit_validator_rule"] == "MissingReferenceChecker"
        unresolved = result["dependencies"]["unresolved"][0]
        assert unresolved["asset"] == "@./textures/steel_albedo.png@" and unresolved["layer"] == "bolt.usda"
        assert unresolved["site"] == "/Bolt/Looks/Steel/Albedo.inputs:file"
        assert result["stats"]["textures"]["missing"] == 1
        assert result["healthy"] is False and result["score"] < 100


def test_absolute_and_outside_project_paths():
    with _tempdir() as folder:
        project = Path(folder) / "project"
        _write(folder, {"shared/steel.png": _png(), "project/textures/steel_albedo.png": _png()})
        absolute = (project / "textures/steel_albedo.png").as_posix()
        _write(project, {"bolt.usda": _bolt_text(texture=absolute)})
        result = _json(asset_health(_open(project / "bolt.usda")))
        assert _check(result, "absolute_paths")["status"] == "warn"
        assert _check(result, "absolute_paths")["kit_validator_rule"] == "AnchoredAssetPathsChecker"
        assert _check(result, "outside_project")["status"] == "pass"
        assert result["dependencies"]["counts"]["absolute_paths"] == 1

        _write(project, {"bolt.usda": _bolt_text(texture="../shared/steel.png")})
        result = _json(asset_health(_open(project / "bolt.usda")))
        assert _check(result, "outside_project")["status"] == "warn"
        assert _check(result, "absolute_paths")["status"] == "pass"
        assert result["dependencies"]["outside_project"][0]["asset"] == "@../shared/steel.png@"
        # A wider project root makes the same file "inside".
        wider = _json(asset_health(_open(project / "bolt.usda"), project_root=folder))
        assert _check(wider, "outside_project")["status"] == "pass"


def test_unit_mismatch_mm_asset_in_meter_stage_reports_factor():
    with _tempdir() as folder:
        _healthy_folder(folder, mpu=0.001)
        _write(folder, {"scene.usda": '#usda 1.0\n(\n    defaultPrim = "World"\n    metersPerUnit = 1\n    upAxis = "Z"\n)\n'
                        'def Xform "World" (\n    kind = "assembly"\n)\n{\n'
                        '    def "Bolt" (\n        prepend references = @./bolt.usda@\n    )\n    {\n    }\n}\n'})
        result = _json(asset_health(_open(Path(folder) / "scene.usda")))
        check = _check(result, "units_mismatch")
        assert check["status"] == "warn", check
        detail = check["details"][0]
        assert abs(detail["apparent_scale"] - 1000) < 1e-6 and abs(detail["suggested_scale"] - 0.001) < 1e-9
        assert "1000x too large" in check["examples"][0]


def test_referenced_file_without_default_prim():
    with _tempdir() as folder:
        _healthy_folder(folder)
        _write(folder, {"bolt.usda": _bolt_text(header=False)})  # no defaultPrim (and no units)
        scene = ('#usda 1.0\n(\n    defaultPrim = "World"\n    metersPerUnit = 1\n    upAxis = "Z"\n)\n'
                 'def Xform "World" (\n    kind = "assembly"\n)\n{\n'
                 '    def "Bolt" (\n        prepend references = @./bolt.usda@ARC\n    )\n    {\n    }\n}\n')
        _write(folder, {"scene.usda": scene.replace("ARC", "")})
        broken = _json(asset_health(_open(Path(folder) / "scene.usda")))
        assert _check(broken, "default_prim")["status"] == "fail"
        assert _check(broken, "composition_errors")["status"] == "fail"
        _write(folder, {"scene.usda": scene.replace("ARC", "</Bolt>")})
        named = _json(asset_health(_open(Path(folder) / "scene.usda")))
        assert _check(named, "default_prim")["status"] == "warn"
        assert any("has no defaultPrim" in example for example in _check(named, "default_prim")["examples"])
        assert _check(named, "composition_errors")["status"] == "pass"
        assert named["score"] > broken["score"]


def test_missing_root_default_prim_and_units():
    with _tempdir() as folder:
        _write(folder, {"bolt.usda": _bolt_text(header=False), "textures/steel_albedo.png": _png()})
        result = _json(asset_health(_open(Path(folder) / "bolt.usda")))
        assert _check(result, "default_prim")["status"] == "warn"
        assert _check(result, "stage_units")["status"] == "warn"
        assert _check(result, "stage_units")["kit_validator_rule"] == "StageMetadataChecker"


def test_stale_extent():
    with _tempdir() as folder:
        stale = BOLT.replace("point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]",
                             "point3f[] points = [(0, 0, 0), (5, 0, 0), (5, 5, 0), (0, 5, 0)]")
        _healthy_folder(folder, body=stale)
        result = _json(asset_health(_open(Path(folder) / "bolt.usda")))
        check = _check(result, "extents")
        assert check["status"] == "warn" and "/Bolt/Body" in check["examples"]
        assert check["details"][0]["computed"] == [[0, 0, 0], [5, 5, 0]]


def test_fifty_identical_meshes_not_instanced():
    stage = _memory_stage()
    material = _material(stage)
    _bind(stage.GetPrimAtPath("/World"), material)
    for index in range(50):
        _quad(stage, f"/World/Bolts/Bolt_{index:02d}")
        UsdGeom.Xformable(stage.GetPrimAtPath(f"/World/Bolts/Bolt_{index:02d}")).AddTranslateOp().Set((index, 0, 0))
    result = _json(asset_health(stage))
    check = _check(result, "instancing_opportunities")
    assert check["status"] == "warn" and check["count"] == 50, check
    assert check["details"][0]["kind"] == "identical_meshes" and check["details"][0]["copies"] == 50
    assert check["kit_validator_rule"] is None and check["mcp_phrase"]
    assert result["stats"]["meshes"] == 50


def test_same_reference_copies_not_instanceable_and_instanced_copies_pass():
    with _tempdir() as folder:
        _healthy_folder(folder)
        children = "".join(f'    def "Bolt_{i}" (\n        prepend references = @./bolt.usda@\n        INST\n    )\n    {{\n    }}\n'
                           for i in range(6))
        scene = ('#usda 1.0\n(\n    defaultPrim = "World"\n    metersPerUnit = 1\n    upAxis = "Z"\n)\n'
                 'def Xform "World" (\n    kind = "assembly"\n)\n{\n' + children + '}\n')
        _write(folder, {"scene.usda": scene.replace("INST", "")})
        plain = _json(asset_health(_open(Path(folder) / "scene.usda")))
        check = _check(plain, "instancing_opportunities")
        assert check["status"] == "warn" and check["details"][0]["kind"] == "same_reference"
        _write(folder, {"scene.usda": scene.replace("INST", "instanceable = true")})
        instanced = _json(asset_health(_open(Path(folder) / "scene.usda")))
        assert _check(instanced, "instancing_opportunities")["status"] == "pass"
        assert instanced["stats"]["instances"] == 6 and instanced["stats"]["prototypes"] == 1
        assert instanced["stats"]["total_faces_with_instances"] == 6


def test_unbound_mesh_and_material_without_surface():
    stage = _memory_stage()
    _quad(stage, "/World/Part")
    UsdShade.Material.Define(stage, "/World/Looks/Empty")
    result = _json(asset_health(stage))
    bindings = _check(result, "material_bindings")
    assert bindings["status"] == "warn" and bindings["examples"] == ["/World/Part"]
    assert _check(result, "materials_surface")["status"] == "warn"
    # A guide-purpose (collision) mesh needs no material.
    stage = _memory_stage()
    UsdGeom.Imageable(_quad(stage, "/World/Collision").GetPrim()).CreatePurposeAttr("guide")
    assert _check(asset_health(stage), "material_bindings")["status"] == "pass"


def test_huge_mesh_is_flagged():
    stage = _memory_stage()
    _bind(stage.GetPrimAtPath("/World"), _material(stage))
    faces = health.HUGE_MESH_FACES + 1
    mesh = UsdGeom.Mesh.Define(stage, "/World/Scan")
    mesh.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 0), Gf.Vec3f(1, 0, 0), Gf.Vec3f(0, 1, 0)]))
    mesh.CreateFaceVertexCountsAttr(Vt.IntArray(faces, 3))
    mesh.CreateFaceVertexIndicesAttr(Vt.IntArray(faces * 3, 0))
    mesh.CreateExtentAttr([(0, 0, 0), (1, 1, 0)])
    result = _json(asset_health(stage))
    check = _check(result, "heavy_meshes")
    assert check["status"] == "warn" and "/World/Scan" in check["examples"][0]
    assert result["stats"]["largest_meshes"][0] == {"path": "/World/Scan", "faces": faces, "points": 3}


def test_physics_names_hierarchy_time_samples_textures_and_normals():
    stage = _memory_stage()
    _bind(stage.GetPrimAtPath("/World"), _material(stage))
    body = UsdGeom.Xform.Define(stage, "/World/Body").GetPrim()
    UsdPhysics.RigidBodyAPI.Apply(body)
    collider = _quad(stage, "/World/Body/Shell", faces=health.COLLIDER_MAX_FACES + 1).GetPrim()
    UsdPhysics.CollisionAPI.Apply(collider)
    UsdGeom.Xform.Define(stage, "/World/Empty")
    UsdGeom.Xform.Define(stage, "/World/Teil_\u00e4")
    path = "/World/Deep"
    for level in range(health.DEEP_HIERARCHY + 2):
        path += f"/L{level}"
    UsdGeom.Xform.Define(stage, path)
    animated = _quad(stage, "/World/Static")
    UsdGeom.Xformable(animated).AddTranslateOp().Set(Gf.Vec3d(1, 2, 3), 0.0)
    subdiv = _quad(stage, "/World/Subdiv")
    subdiv.GetSubdivisionSchemeAttr().Set("catmullClark")
    shader = UsdShade.Shader.Define(stage, "/World/Looks/Grey/NormalTex")
    shader.CreateIdAttr("UsdUVTexture")
    shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set("./bolt_Normal.png")
    result = _json(asset_health(stage))
    statuses = {check["id"]: check["status"] for check in result["checks"]}
    assert statuses["physics_colliders"] == "warn", _check(result, "physics_colliders")
    assert statuses["physics_mass"] == "info"
    assert statuses["empty_xforms"] == "info"
    assert "/World/Empty" in _check(result, "empty_xforms")["examples"]
    assert statuses["prim_names"] == "warn"
    assert statuses["hierarchy_depth"] == "warn" and result["stats"]["max_depth"] > health.DEEP_HIERARCHY
    assert statuses["time_samples"] == "warn"
    assert any("xformOp:translate" in example for example in _check(result, "time_samples")["examples"])
    assert statuses["normals"] == "warn"
    assert statuses["textures"] == "warn"  # a normal map read as sRGB (auto)
    assert result["stats"]["rigid_bodies"] == 1 and result["stats"]["colliders"] == 1
    json.dumps(result)


def test_non_portable_texture_format():
    with _tempdir() as folder:
        _write(folder, {"bolt.usda": _bolt_text(texture="./textures/steel.tga"), "textures/steel.tga": b"TGA"})
        result = _json(asset_health(_open(Path(folder) / "bolt.usda")))
        assert _check(result, "textures")["status"] == "warn"
        assert result["stats"]["textures"]["formats"] == {"tga": 1}


def test_mdl_library_modules_are_not_missing_but_relative_mdl_files_are():
    stage = _memory_stage()
    for name, module in (("Lib", "OmniPBR.mdl"), ("Local", "./materials/Custom.mdl")):
        material = UsdShade.Material.Define(stage, f"/World/Looks/{name}")
        shader = UsdShade.Shader.Define(stage, f"/World/Looks/{name}/Shader")
        shader.SetSourceAsset(Sdf.AssetPath(module), "mdl")
        material.CreateSurfaceOutput("mdl").ConnectToSource(shader.ConnectableAPI(), "out")
        _bind(stage.GetPrimAtPath("/World"), material)
    result = _json(asset_health(stage))
    assert result["dependencies"]["counts"]["mdl_search_path"] == 1
    missing = _check(result, "missing_files")
    assert missing["status"] == "fail" and missing["count"] == 1 and "Custom.mdl" in missing["examples"][0]
    assert _check(result, "materials_surface")["status"] == "pass"
    assert "mdl: 2" in _check(result, "shading_models")["examples"][0]
    assert any("MDL library" in note for note in result["notes"])


def test_udim_textures_resolve_when_a_tile_exists():
    with _tempdir() as folder:
        _write(folder, {"bolt.usda": _bolt_text(texture="./textures/steel.<UDIM>.png"),
                        "textures/steel.1001.png": _png()})
        result = _json(asset_health(_open(Path(folder) / "bolt.usda")))
        assert _check(result, "missing_files")["status"] == "pass"
        _write(folder, {"bolt.usda": _bolt_text(texture="./textures/rust.<UDIM>.png")})
        assert _check(asset_health(_open(Path(folder) / "bolt.usda")), "missing_files")["status"] == "fail"


# --- score, truncation, scope, errors -------------------------------------------------------------------------------


def test_score_ordering_healthy_above_warned_above_failed_above_many_problems():
    with _tempdir() as folder:
        healthy = asset_health(_open(_healthy_folder(Path(folder) / "a")))
        unbound = asset_health(_open(_healthy_folder(Path(folder) / "b", body=BOLT.replace(
            "        rel material:binding = </Bolt/Looks/Steel>\n", ""))))
        _write(Path(folder) / "c", {"bolt.usda": _bolt_text()})
        missing = asset_health(_open(Path(folder) / "c" / "bolt.usda"))
        many_body = BOLT.replace("        rel material:binding = </Bolt/Looks/Steel>\n", "").replace(
            "(1, 1, 0), (0, 1, 0)]", "(7, 7, 0), (0, 7, 0)]")
        _write(Path(folder) / "d", {"bolt.usda": _bolt_text(texture="C:/nowhere/steel.tga", header=False, body=many_body)})
        many = asset_health(_open(Path(folder) / "d" / "bolt.usda"))
    scores = [healthy["score"], unbound["score"], missing["score"], many["score"]]
    assert scores == sorted(scores, reverse=True) and len(set(scores)) == 4, scores
    assert many["grade"] in ("D", "F") and healthy["grade"] == "A"


def test_max_prims_truncates():
    stage = _memory_stage()
    for index in range(50):
        _quad(stage, f"/World/Part_{index}")
    result = _json(asset_health(stage, max_prims=5))
    assert result["truncated"] is True and result["prims_checked"] == 5
    assert result["stats"]["meshes"] <= 5
    assert any("max_prims" in note for note in result["notes"])
    assert "only part" in result["summary"]


def test_deadline_truncates_and_reports_time_budget():
    stage = _memory_stage()
    for index in range(20):
        _quad(stage, f"/World/Part_{index}")
    expired = _json(asset_health(stage, deadline=time.monotonic() - 1))
    assert expired["ok"] and expired["truncated_by_time"] is True and expired["truncated"] is True
    assert expired["time_budget"]["truncated_by_time"] is True and expired["score_is_partial"] is True
    unchecked = [check for check in expired["checks"] if not check["checked"]]
    assert unchecked and all(check["status"] == "info" for check in unchecked)
    assert "score is partial" in expired["summary"]
    roomy = _json(asset_health(stage, deadline=TimeBudget(60_000)))
    assert roomy["truncated_by_time"] is False and roomy["prims_checked"] >= 21
    assert roomy["score_is_partial"] is False and all(check["checked"] for check in roomy["checks"])
    with TimeBudget(0):
        ambient = asset_health(stage)
    assert ambient["truncated_by_time"] is True
    assert asset_health(stage, deadline="soon")["ok"] is False


def test_root_path_scopes_prim_checks():
    stage = _memory_stage()
    _bind(_quad(stage, "/World/Good/Part").GetPrim(), _material(stage))
    _quad(stage, "/World/Bad/Part")
    scoped = _json(asset_health(stage, root_path="/World/Good"))
    assert scoped["root_path"] == "/World/Good"
    assert _check(scoped, "material_bindings")["status"] == "pass"
    assert _check(asset_health(stage), "material_bindings")["status"] == "warn"
    missing = asset_health(stage, root_path="/World/Nope")
    assert missing["ok"] is False and "No prim" in missing["error"]
    assert asset_health(None)["ok"] is False


# --- read-only guarantees -------------------------------------------------------------------------------------------


def test_file_mode_is_read_only_byte_identical_and_json_safe():
    with _tempdir() as folder:
        _healthy_folder(folder)
        _write(folder, {"scene.usda": '#usda 1.0\n(\n    defaultPrim = "World"\n    metersPerUnit = 1\n    upAxis = "Z"\n)\n'
                        'def Xform "World" (\n    kind = "assembly"\n)\n{\n'
                        '    def "Bolt" (\n        prepend references = @./bolt.usda@\n        instanceable = true\n    )\n    {\n    }\n}\n'})
        before = _snapshot(folder)
        stamps = {path: os.stat(path).st_mtime_ns for path in before}
        result = asset_health_file(Path(folder) / "scene.usda", allow_outside_workspace=True)
        text = json.dumps(result)
        assert result["ok"], result
        assert result["file"].endswith("scene.usda") and result["score"] == 100, text
        assert _snapshot(folder) == before
        assert {path: os.stat(path).st_mtime_ns for path in before} == stamps
        # Outside the workspace is refused unless allowed; a missing file is a clean error.
        refused = asset_health_file(Path(folder) / "scene.usda")
        assert refused["ok"] is False and "outside the workspace" in refused["error"]
        gone = asset_health_file(Path(folder) / "nope.usda", allow_outside_workspace=True)
        assert gone["ok"] is False and "does not exist" in gone["error"]


def test_in_memory_stage_is_not_dirtied():
    with _tempdir() as folder:
        stage = _open(_healthy_folder(folder))
        layers = list(stage.GetUsedLayers())
        before = [layer.ExportToString() for layer in layers]
        result = asset_health(stage)
        assert result["ok"]
        assert not any(layer.dirty for layer in layers)
        assert [layer.ExportToString() for layer in layers] == before


# --- real local assets (read-only smoke) -----------------------------------------------------------------------------


def _real_smoke(root, candidates, label):
    if _IN_KIT:
        return None
    paths = [root / relative for relative in candidates if (root / relative).is_file()]
    if not paths:
        print(f"SKIP-ish: no {label} asset under {root}")
        return None
    path = paths[0]
    before = {path: (path.stat().st_mtime_ns, path.stat().st_size)}
    started = time.perf_counter()
    result = asset_health_file(path, deadline=time.monotonic() + 60)
    elapsed = time.perf_counter() - started
    assert result["ok"], result.get("error")
    json.dumps(result)
    assert 0 <= result["score"] <= 100 and result["grade"] in "ABCDF"
    assert all(check["status"] in STATUSES for check in result["checks"])
    assert result["stats"]["prims"] >= 1 and isinstance(result["dependencies"]["counts"], dict)
    assert {path: (path.stat().st_mtime_ns, path.stat().st_size)} == before
    flagged = [f"{c['id']}={c['status']}" for c in result["checks"] if c["status"] in ("warn", "fail")]
    print(f"{label} {path.name}: score {result['score']} ({result['grade']}), {result['stats']['prims']} prims, "
          f"{result['stats']['meshes']} meshes, {elapsed * 1000:.0f} ms; flagged: {', '.join(flagged) or 'none'}")
    return result


def test_real_franka_from_the_isaac_asset_pack_read_only():
    _real_smoke(ISAAC_PACK, FRANKA_CANDIDATES, "Franka")


def test_real_bag_asset_read_only():
    _real_smoke(BAG_ROOT, BAG_CANDIDATES, "Bag")
