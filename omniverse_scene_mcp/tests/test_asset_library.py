"""Asset library index/search on a synthetic pack in a temp dir, add_reference planning on temp stages, one real-pack smoke test."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover - the index/search tests still run; pxr tests return early
    Sdf = Usd = UsdGeom = None

from omniverse_scene_mcp import asset_library
from omniverse_scene_mcp.asset_library import AssetLibrary, add_reference

REAL_PACK = Path("C:/Users/krngr/Omniverse/isaacsim-assets-6.1.0")


def _usda(default_prim: str | None = "Root", up_axis: str | None = "Z", mpu: float | None = 1.0, roots=("Root",)) -> str:
    meta = []
    if default_prim:
        meta.append(f'    defaultPrim = "{default_prim}"')
    if mpu is not None:
        meta.append(f"    metersPerUnit = {mpu}")
    if up_axis:
        meta.append(f'    upAxis = "{up_axis}"')
    header = "#usda 1.0\n(\n" + "\n".join(meta) + "\n)\n\n" if meta else "#usda 1.0\n\n"
    body = "".join(f'def Xform "{name}"\n{{\n    def Cube "Body"\n    {{\n    }}\n}}\n\n' for name in roots)
    return header + body


def _pack(tmp: str) -> Path:
    """<tmp>/pack/Assets/Isaac/9.9/{Isaac,NVIDIA}/... with robots, an environment, props, a material and noise files."""
    pack = Path(tmp) / "pack"
    content = pack / "Assets" / "Isaac" / "9.9"
    files = {
        "Isaac/Robots/Acme/Arm/arm.usda": _usda("Arm", roots=("Arm",)),
        "Isaac/Robots/Acme/Arm/configuration/arm_robot_schema.usda": _usda("Arm", roots=("Arm",)),
        "Isaac/Robots/Acme/Arm/Props/arm_link0.usda": _usda("Link", roots=("Link",)),
        "Isaac/Robots/Acme/Arm/.thumbs/256x256/arm.usda.png": "png",
        "Isaac/Robots/Acme/Arm/.thumbs/256x256/hidden.usda": _usda(),
        "Isaac/Robots/Acme/Rover/rover_v1.usda": _usda("Rover", roots=("Rover",)),
        "Isaac/Environments/Warehouse/warehouse_shelves.usda": _usda("World", roots=("World",)),
        "Isaac/Environments/Warehouse/Props/SM_RackShelf_01.usda": _usda("Shelf", roots=("Shelf",)),
        "Isaac/Props/Shapes/cube.usda": _usda("Cube", roots=("Cube",)),
        "Isaac/Props/Shapes/cube_y_cm.usda": _usda("Cube", up_axis="Y", mpu=0.01, roots=("Cube",)),
        "Isaac/Props/Shapes/no_units.usda": _usda("Thing", up_axis=None, mpu=None, roots=("Thing",)),
        "Isaac/Props/Odd/one_root.usda": _usda(None, roots=("Only",)),
        "Isaac/Props/Odd/two_roots.usda": _usda(None, roots=("A", "B")),
        "Isaac/Props/Odd/readme.txt": "not an asset",
        "Isaac/Sensors/Acme/lidar_2d.usda": _usda("Lidar", roots=("Lidar",)),
        "Isaac/Materials/Base/carbon_fiber.mdl": "mdl 1.4;",
        "NVIDIA/Assets/Characters/bob.usda": _usda("Bob", roots=("Bob",)),
    }
    for rel, text in files.items():
        path = content / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return pack


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def _stage(tmp: str, up_axis: str = "Z", mpu: float = 1.0):
    stage = Usd.Stage.CreateNew(str(Path(tmp) / "scene" / "scene.usda"))
    UsdGeom.SetStageUpAxis(stage, up_axis)
    UsdGeom.SetStageMetersPerUnit(stage, mpu)
    UsdGeom.Xform.Define(stage, "/World")
    return stage


def _layers(stage):
    return stage.GetRootLayer().ExportToString(), stage.GetSessionLayer().ExportToString()


# --- index and search (no pxr needed) ---------------------------------------------------------------------------


def test_status_finds_content_root_counts_and_categories():
    with tempfile.TemporaryDirectory() as tmp:
        asset_library.clear_cache()
        lib = AssetLibrary(_pack(tmp))
        status = lib.status()
        assert status["available"] is True
        assert status["content_root"].endswith("Assets/Isaac/9.9")
        assert status["pack_version"] == "9.9"
        assert status["usd_count"] == 13  # the .thumbs copy and readme.txt are not indexed
        assert status["mdl_count"] == 1
        assert status["categories"] == {
            "Characters": 1, "Environments": 2, "Materials": 1, "Props": 5, "Robots": 4, "Sensors": 1,
        }
        assert isinstance(status["index_seconds"], float)
        json.dumps(status)


def test_search_ranks_name_matches_and_lowers_robot_parts():
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        hits = lib.search("arm")["results"]
        assert hits[0]["relative_path"] == "Isaac/Robots/Acme/Arm/arm.usda"
        assert hits[0]["thumbnail"].endswith(".thumbs/256x256/arm.usda.png")
        order = [hit["relative_path"] for hit in hits]
        assert order.index("Isaac/Robots/Acme/Arm/configuration/arm_robot_schema.usda") > 0
        cube = lib.search("cube", top_k=1)
        assert cube["ok"] and cube["results"][0]["name"] == "cube.usda"
        assert lib.search("rover")["results"][0]["name"] == "rover_v1.usda"
        json.dumps(lib.search("cube"))


def test_search_multi_word_and_plural_query():
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        names = [hit["name"] for hit in lib.search("warehouse shelves", top_k=3)["results"]]
        assert names[:2] == ["warehouse_shelves.usda", "SM_RackShelf_01.usda"]
        assert lib.search("zzzz-nothing")["results"] == []


def test_search_category_filter_aliases_and_unknown_category():
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        robots = lib.search("acme", category="robot")
        assert robots["ok"] and robots["matched_categories"] == ["Robots"]
        assert {hit["category"] for hit in robots["results"]} == {"Robots"}
        sensors = lib.search("lidar", category="Sensors")["results"]
        assert [hit["name"] for hit in sensors] == ["lidar_2d.usda"]
        materials = lib.search("carbon fiber", category="materials")["results"]
        assert materials[0]["kind"] == "mdl" and materials[0]["referenceable"] is False
        people = lib.search("", category="people")
        assert [hit["name"] for hit in people["results"]] == ["bob.usda"]
        bad = lib.search("cube", category="spaceships")
        assert bad["ok"] is False and "Props" in bad["categories"]
        assert lib.search("  ")["ok"] is False


def test_index_is_cached_per_process_and_clear_cache_rebuilds():
    with tempfile.TemporaryDirectory() as tmp:
        pack = _pack(tmp)
        asset_library.clear_cache()
        first = AssetLibrary(pack)._index()
        assert AssetLibrary(pack)._index() is first
        (pack / "Assets/Isaac/9.9/Isaac/Props/Shapes/teapot.usda").write_text(_usda(), encoding="utf-8")
        assert AssetLibrary(pack).search("teapot")["results"] == []  # cached walk
        asset_library.clear_cache()
        assert AssetLibrary(pack).search("teapot")["results"][0]["name"] == "teapot.usda"


def test_env_override_caps_and_missing_root():
    with tempfile.TemporaryDirectory() as tmp:
        pack = _pack(tmp)
        old = os.environ.get(asset_library.ENV_ROOT)
        os.environ[asset_library.ENV_ROOT] = str(pack)
        try:
            assert AssetLibrary().root == pack
        finally:
            if old is None:
                os.environ.pop(asset_library.ENV_ROOT, None)
            else:
                os.environ[asset_library.ENV_ROOT] = old
        capped = AssetLibrary(pack, max_assets=3).status()
        assert capped["asset_count"] == 3 and any("max_assets" in note for note in capped["truncated"])
        missing = AssetLibrary(Path(tmp) / "nope")
        assert missing.status()["available"] is False
        assert missing.search("cube")["ok"] is False
        assert missing.categories() == {}


def test_index_search_and_info_never_write_into_the_pack():
    with tempfile.TemporaryDirectory() as tmp:
        pack = _pack(tmp)
        before = _snapshot_tree(pack)
        asset_library.clear_cache()
        lib = AssetLibrary(pack)
        lib.status()
        lib.search("cube")
        lib.info("Isaac/Props/Shapes/cube.usda")
        lib.info("Isaac/Props/Odd/two_roots.usda")
        assert _snapshot_tree(pack) == before


def test_info_missing_file_suggests_similar_assets():
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        info = lib.info("Isaac/Props/Shapes/cubes.usda")
        assert info["ok"] is False and "Isaac/Props/Shapes/cube.usda" in info["suggestions"]


# --- info header and add_reference (pxr) -------------------------------------------------------------------------


def test_info_reads_header_and_category():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        info = lib.info("Isaac/Robots/Acme/Arm/arm.usda")
        assert info["ok"] and info["category"] == "Robots" and info["library"] == "Isaac" and info["group"] == "Acme"
        assert (info["default_prim"], info["up_axis"], info["meters_per_unit"]) == ("Arm", "Z", 1.0)
        assert info["size"] > 0 and info["thumbnail"].endswith("arm.usda.png")
        units = lib.info("Isaac/Props/Shapes/no_units.usda")
        assert units["header_read"] and units["up_axis"] is None and units["meters_per_unit"] is None
        skipped = lib.info("Isaac/Props/Shapes/cube.usda", header_max_bytes=10)
        assert skipped["header_read"] is False and "header_skipped" in skipped
        mdl = lib.info("Isaac/Materials/Base/carbon_fiber.mdl")
        assert mdl["kind"] == "mdl" and "header_read" not in mdl
        json.dumps(info)


def test_add_reference_dry_run_writes_nothing():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        stage = _stage(tmp)
        before = _layers(stage)
        result = add_reference(stage, "/World/Arm", "Isaac/Robots/Acme/Arm/arm.usda", translation=(1, 2, 0),
                               rotation=(0, 0, 90), scale=2, instanceable=True, library=lib)
        assert result["ok"] and result["dry_run"] is True and result["written"] is False
        assert _layers(stage) == before
        assert not stage.GetPrimAtPath("/World/Arm")
        plan = result["would_create"]
        assert plan["reference"]["asset_path"].endswith("Isaac/Robots/Acme/Arm/arm.usda")
        assert "\\" not in plan["reference"]["asset_path"] and plan["reference"]["prim_path"] is None
        assert plan["xform_ops"] == ["xformOp:translate", "xformOp:rotateXYZ", "xformOp:scale"]
        assert plan["scale"] == [2.0, 2.0, 2.0] and plan["instanceable"] is True
        assert plan["relative_path_hint"].startswith("../")
        assert result["edit_target_layer"] == stage.GetRootLayer().identifier
        json.dumps(result)


def test_add_reference_authors_xform_reference_and_ops():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        stage = _stage(tmp)
        result = add_reference(stage, "/World/Arm", "Isaac/Robots/Acme/Arm/arm.usda", translation=(1, 2, 0),
                               rotation=(0, 0, 90), instanceable=False, library=lib, dry_run=False)
        assert result["ok"] and result["written"] is True and result["dry_run"] is False
        created = result["created"]
        assert created["type"] == "Xform" and created["reference_resolved"] is True
        assert created["xform_op_order"] == ["xformOp:translate", "xformOp:rotateXYZ"]
        assert created["references"][0]["asset_path"].endswith("arm.usda")
        prim = stage.GetPrimAtPath("/World/Arm")
        assert prim.GetChild("Body").GetTypeName() == "Cube"
        assert UsdGeom.Xformable(prim).GetLocalTransformation().ExtractTranslation() == (1, 2, 0)
        assert prim.IsInstanceable() is False and prim.HasAuthoredInstanceable()
        assert not result["warnings"] or all("Z-up" not in w for w in result["warnings"])
        json.dumps(result)


def test_add_reference_refusals_leave_stage_unchanged():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        stage = _stage(tmp)
        before = _layers(stage)
        cases = [
            ("/World", "Isaac/Props/Shapes/cube.usda", {}, "already exists"),
            ("/Missing/Cube", "Isaac/Props/Shapes/cube.usda", {}, "Parent prim does not exist"),
            ("/World/Cube", "Isaac/Props/Shapes/nope.usda", {}, "not found"),
            ("/World/Mat", "Isaac/Materials/Base/carbon_fiber.mdl", {}, "MDL"),
            ("/World/Txt", "Isaac/Props/Odd/readme.txt", {}, "not a USD file"),
            ("/World/Two", "Isaac/Props/Odd/two_roots.usda", {}, "several root prims"),
            ("/World/Cube", "Isaac/Props/Shapes/cube.usda", {"scale": (1, 0, 1)}, "must not be 0"),
            ("/World/Cube", "Isaac/Props/Shapes/cube.usda", {"rotation": (1, 2)}, "three numbers"),
            ("/World/Self", stage.GetRootLayer().realPath, {}, "cycle"),
        ]
        for prim_path, asset, extra, message in cases:
            for dry in (True, False):
                result = add_reference(stage, prim_path, asset, library=lib, dry_run=dry, **extra)
                assert result["ok"] is False and message in result["error"], (prim_path, asset, result.get("error"))
                assert result["written"] is False
        assert _layers(stage) == before


def test_add_reference_unit_mismatch_warns_and_correct_units_applies():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        stage = _stage(tmp, up_axis="Y", mpu=0.01)  # a Y-up centimeter scene; Isaac assets are Z-up meters
        plan = add_reference(stage, "/World/Arm", "Isaac/Robots/Acme/Arm/arm.usda", library=lib)
        assert plan["ok"]
        text = " ".join(plan["warnings"])
        assert "Z-up but the stage is Y-up" in text and "metersPerUnit 1 but the stage uses 0.01" in text
        assert plan["would_create"]["unit_correction"] == {"rotation": [-90.0, 0.0, 0.0], "scale": [100.0, 100.0, 100.0]}
        assert plan["would_create"]["xform_ops"] == []
        applied = add_reference(stage, "/World/Arm", "Isaac/Robots/Acme/Arm/arm.usda", library=lib,
                                correct_units=True, dry_run=False)
        assert applied["ok"] and applied["created"]["xform_op_order"] == ["xformOp:rotateXYZ", "xformOp:scale"]
        prim = stage.GetPrimAtPath("/World/Arm")
        assert tuple(prim.GetAttribute("xformOp:scale").Get()) == (100.0, 100.0, 100.0)
        kept = add_reference(stage, "/World/Arm2", "Isaac/Robots/Acme/Arm/arm.usda", library=lib, scale=5,
                             correct_units=True)
        assert kept["would_create"]["scale"] == [5.0, 5.0, 5.0]
        assert any("did not change scale" in w for w in kept["warnings"])
        same = add_reference(stage, "/World/YCube", "Isaac/Props/Shapes/cube_y_cm.usda", library=lib)
        assert same["would_create"]["unit_correction"] is None
        missing = add_reference(stage, "/World/Thing", "Isaac/Props/Shapes/no_units.usda", library=lib)
        assert any("does not author upAxis" in w for w in missing["warnings"])


def test_add_reference_without_default_prim_uses_root_or_explicit_prim():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        lib = AssetLibrary(_pack(tmp))
        stage = _stage(tmp)
        one = add_reference(stage, "/World/One", "Isaac/Props/Odd/one_root.usda", library=lib, dry_run=False)
        assert one["ok"] and one["would_create"]["reference"]["prim_path"] == "/Only"
        assert one["created"]["reference_resolved"] and any("no defaultPrim" in w for w in one["warnings"])
        two = add_reference(stage, "/World/B", "Isaac/Props/Odd/two_roots.usda", asset_prim_path="/B", library=lib,
                            dry_run=False)
        assert two["ok"] and two["created"]["references"][0]["prim_path"] == "/B"
        assert stage.GetPrimAtPath("/World/B/Body")


def test_add_reference_accepts_absolute_path_and_in_memory_stage():
    if Usd is None:
        return
    with tempfile.TemporaryDirectory() as tmp:
        pack = _pack(tmp)
        absolute = str(pack / "Assets/Isaac/9.9/Isaac/Props/Shapes/cube.usda")
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageUpAxis(stage, "Z")
        UsdGeom.SetStageMetersPerUnit(stage, 1.0)
        before = _layers(stage)
        result = add_reference(stage, "/Cube", absolute, library=AssetLibrary(pack))
        assert result["ok"] and _layers(stage) == before
        assert result["would_create"]["relative_path_hint"] is None  # anonymous layer: no folder to be relative to
        assert any("anonymous" in w for w in result["warnings"])
        written = add_reference(stage, "/Cube", absolute, library=AssetLibrary(pack), dry_run="false")
        assert written["ok"] and stage.GetPrimAtPath("/Cube/Body")


# --- real asset pack ----------------------------------------------------------------------------------------------


def test_smoke_real_isaac_asset_pack():
    if not REAL_PACK.is_dir():
        return
    started = time.perf_counter()
    lib = AssetLibrary(REAL_PACK)
    found = lib.search("franka", top_k=5)
    assert found["ok"] and found["results"], found
    assert any("franka" in hit["name"].lower() for hit in found["results"])
    assert lib.status()["usd_count"] > 1000
    assert time.perf_counter() - started < 20
