"""Read-only USD insight engine and file-stage helpers, exercised on temporary USDA scenes."""

import ast
import contextlib
import json
import os
import tempfile
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, Vt
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    # unittest.SkipTest at import is a module skip for pytest and tools/run_tests.py alike, with or without pytest.
    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import file_stage, usd_insight

ASSET = """#usda 1.0
(
    defaultPrim = "Chair"
)

def Xform "Chair" (
    kind = "component"
    variants = {
        string color = "red"
    }
    prepend variantSets = "color"
)
{
    double price = 10
    double blockme = 3
    double strength.timeSamples = {
        0: 100,
        10: 200,
    }
    double wobble.timeSamples = {
        0: 0,
        10: 10,
    }

    def Mesh "Seat" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {
        float3[] extent = [(0, 0, 0), (1, 1, 0)]
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0)]
        color3f[] primvars:displayColor = [(1, 0, 0), (0, 0, 1)] (
            interpolation = "vertex"
        )
        int[] primvars:displayColor:indices = [0, 1, 0]
        float primvars:roughness = 0.25 (
            interpolation = "constant"
        )
        asset texture = @./asset.usda@
        rel material:binding = </Chair/Looks/Wood>
    }

    def Scope "Looks"
    {
        def Material "Wood"
        {
        }
    }

    variantSet "color" = {
        "blue" {
            custom string finish = "blue"
        }
        "red" {
            custom string finish = "red"
        }
    }
}
"""

PAYLOAD = """#usda 1.0
(
    defaultPrim = "Crate"
)

def Xform "Crate" (
    kind = "component"
)
{
    def Cube "Box"
    {
        double size = 4
        float3[] extent = [(-1, -1, -1), (1, 1, 1)]
    }
}
"""

ANIM = """#usda 1.0

over "World"
{
    over "Chair"
    {
        custom double spin.timeSamples = {
            0: 0,
            10: 100,
        }
    }
}
"""

WEAK = """#usda 1.0

over "World"
{
    over "Chair"
    {
        custom double price = 50
    }
}
"""

ROOT = """#usda 1.0
(
    defaultPrim = "World"
    endTimeCode = 48
    metersPerUnit = 0.01
    startTimeCode = 0
    subLayers = [
        @./anim.usda@ (offset = 5; scale = 2),
        @./weak.usda@
    ]
    timeCodesPerSecond = 24
    upAxis = "Z"
)

def Xform "World" (
    kind = "assembly"
)
{
    def "Chair" (
        assetInfo = {
            string name = "chair"
        }
        customData = {
            string owner = "lesson"
        }
        doc = "A chair"
        prepend inherits = </_Furniture>
        prepend references = @./asset.usda@ (offset = 10)
        variants = {
            string color = "blue"
        }
    )
    {
        double blockme = None
        double strength = 1
        double3 xformOp:translate = (2, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def "Crate" (
        prepend payload = @./payload.usda@
    )
    {
        double3 xformOp:translate = (5, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def "Instanced" (
        instanceable = true
        prepend references = @./asset.usda@
    )
    {
    }

    def Cube "Ghost"
    {
        token visibility = "invisible"
        double3 xformOp:translate = (100, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def Cube "Helper" (
        hidden = true
    )
    {
        uniform token purpose = "guide"
        double3 xformOp:translate = (0, 50, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }

    def Xform "Loose"
    {
        def Xform "Orphan" (
            kind = "component"
        )
        {
        }
    }
}

class "_Furniture"
{
    custom string category = "seating"
    custom double price = 99
}
"""

FILES = {"asset.usda": ASSET, "payload.usda": PAYLOAD, "anim.usda": ANIM, "weak.usda": WEAK, "root.usda": ROOT}


@contextlib.contextmanager
def _scene():
    with tempfile.TemporaryDirectory(prefix="usd_insight_") as folder:
        for name, text in FILES.items():
            Path(folder, name).write_text(text, encoding="utf-8")
        yield Path(folder), str(Path(folder, "root.usda"))


def _json(value):
    return json.loads(json.dumps(value, allow_nan=False))


def _close(actual, expected, tolerance=1e-5):
    return all(abs(a - b) <= tolerance for a, b in zip(actual, expected))


def _raises(error, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except error as exc:
        return exc
    raise AssertionError(f"{func.__name__} did not raise {error.__name__}")


def _snapshot(folder):
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in sorted(Path(folder).iterdir())}


def test_usd_insight_imports_only_pxr_and_stdlib():
    tree = ast.parse(Path(usd_insight.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "usd_insight must not use relative imports"
            roots.add(node.module.split(".")[0])
    assert roots <= {"__future__", "collections", "math", "typing", "pxr"}, roots


def test_to_jsonable_converts_usd_value_types():
    assert usd_insight.to_jsonable(Gf.Vec3f(1, 2, 3)) == [1.0, 2.0, 3.0]
    assert usd_insight.to_jsonable(Gf.Vec2i(4, 5)) == [4, 5]
    matrix = usd_insight.to_jsonable(Gf.Matrix4d(1).SetTranslate(Gf.Vec3d(7, 8, 9)))
    assert matrix[3] == [7.0, 8.0, 9.0, 1.0] and matrix[0] == [1.0, 0.0, 0.0, 0.0]
    assert usd_insight.to_jsonable(Gf.Quatf(1, Gf.Vec3f(0, 0, 0))) == {"real": 1.0, "imaginary": [0.0, 0.0, 0.0]}
    assert usd_insight.to_jsonable(Sdf.Path("/World/Cube")) == "/World/Cube"
    assert usd_insight.to_jsonable(Usd.TimeCode.Default()) == "default"
    assert usd_insight.to_jsonable(Usd.TimeCode(12)) == 12.0
    assert usd_insight.to_jsonable(Vt.TokenArray(["a", "b"])) == ["a", "b"]
    assert usd_insight.to_jsonable(Sdf.ValueBlock()) == {"value_block": True}
    assert usd_insight.to_jsonable(Sdf.AssetPath("./tex.png")) == {"authored": "./tex.png", "resolved": None}
    assert usd_insight.to_jsonable(Sdf.LayerOffset(5, 2)) == {"offset": 5.0, "scale": 2.0}
    assert usd_insight.to_jsonable(float("nan")) == "nan"
    assert usd_insight.to_jsonable(Gf.Range3d()) == {"empty": True, "min": None, "max": None}
    assert usd_insight.to_jsonable(Gf.Range3d(Gf.Vec3d(0, 0, 0), Gf.Vec3d(1, 2, 3))) == {
        "min": [0.0, 0.0, 0.0],
        "max": [1.0, 2.0, 3.0],
    }

    big = usd_insight.to_jsonable(Vt.Vec3fArray([(i, 0, 0) for i in range(100)]), max_items=4)
    assert big["length"] == 100 and big["truncated"] is True
    assert big["items"] == [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]]
    nested = usd_insight.to_jsonable({"pos": Gf.Vec3d(1, 2, 3), "list": [Sdf.Path("/A")], "arr": Vt.IntArray([1, 2])})
    assert nested == {"pos": [1.0, 2.0, 3.0], "list": ["/A"], "arr": [1, 2]}
    _json(nested)


def test_stage_info_reports_layers_metadata_and_model_hierarchy():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        info = _json(usd_insight.stage_info(stage))

        assert info["root_layer"]["identifier"].endswith("root.usda")
        assert info["session_layer"]["present"] is True and info["session_layer"]["empty"] is True
        assert info["edit_target"]["is_root_layer"] is True
        roles = [(Path(entry["identifier"]).name, entry["role"]) for entry in info["layer_stack"]]
        assert roles[0][1] == "session"
        assert roles[1:] == [("root.usda", "root"), ("anim.usda", "sublayer"), ("weak.usda", "sublayer")]
        anim = info["layer_stack"][2]
        assert anim["offset"] == {"offset": 5.0, "scale": 2.0} and anim["depth"] == 1
        assert anim["authored_path"] == "./anim.usda" and anim["in_layer_stack"] is True
        assert all(entry["dirty"] is False and entry["muted"] is False for entry in info["layer_stack"])
        used = {Path(identifier).name for identifier in info["used_layers"]["identifiers"]}
        assert {"asset.usda", "payload.usda", "root.usda", "anim.usda", "weak.usda"} <= used

        assert info["default_prim"] == "/World"
        assert info["up_axis"] == "Z" and info["up_axis_authored"] is True
        assert info["meters_per_unit"] == 0.01 and info["meters_per_unit_authored"] is True
        assert info["time_codes_per_second"] == 24.0
        assert (info["start_time_code"], info["end_time_code"]) == (0.0, 48.0)

        prims = info["prims"]
        assert prims["by_type"]["Cube"] == 3 and prims["by_type"]["Mesh"] == 1
        assert prims["by_specifier"]["class"] == 1 and prims["abstract"] == 1
        assert prims["instances"] == 1 and prims["prototypes"] == 1
        assert prims["truncated"] is False and prims["total"] >= prims["traversed_by_default"]

        hierarchy = info["model_hierarchy"]
        models = {entry["path"]: entry["kind"] for entry in hierarchy["models"]}
        assert models["/World"] == "assembly"
        assert models["/World/Chair"] == "component" and models["/World/Crate"] == "component"
        assert [entry["path"] for entry in hierarchy["broken_model_kinds"]] == ["/World/Loose/Orphan"]
        assert hierarchy["by_kind"]["component"] == 4
        assert info["composition_errors"] == []


def test_stage_info_reports_muted_sublayer_and_missing_layers():
    with _scene() as (folder, root):
        stage = file_stage.open_readonly(root)
        weak = Sdf.Layer.FindRelativeToLayer(stage.GetRootLayer(), "./weak.usda").identifier
        stage.MuteLayer(weak)
        info = _json(usd_insight.stage_info(stage))
        weak_entry = next(entry for entry in info["layer_stack"] if entry["identifier"] == weak)
        assert weak_entry["muted"] is True and weak_entry["in_layer_stack"] is False
        assert weak in info["muted_layers"]

        explained = usd_insight.explain_value(stage, "/World/Chair", "price")
        assert explained["value"] == 99.0 and explained["winning_opinion"]["liverps"] == "I"

        broken = Path(folder, "broken.usda")
        broken.write_text('#usda 1.0\n(\n    subLayers = [@./nope.usda@]\n)\n\ndef "A" (\n    references = @./gone.usda@\n)\n{\n}\n')
        info = _json(usd_insight.stage_info(file_stage.open_readonly(str(broken))))
        missing = [entry for entry in info["layer_stack"] if entry.get("missing")]
        assert len(missing) == 1 and missing[0]["authored_path"] == "./nope.usda"
        assert len(info["composition_errors"]) == 2


def test_composition_lists_arcs_variants_and_prim_stack():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        chair = _json(usd_insight.composition(stage, "/World/Chair"))

        assert chair["specifier"] == "def" and chair["type_name"] == "Xform" and chair["active"] is True
        kinds = [arc["arc_type"] for arc in chair["arcs"]]
        assert kinds == ["local", "inherit", "reference", "variant"]
        assert [arc["liverps"] for arc in chair["arcs"]] == ["L", "I", "R", "V"]
        inherit, reference, variant = chair["arcs"][1:]
        assert inherit["target_prim_path"] == "/_Furniture" and inherit["introducing_layer"].endswith("root.usda")
        assert reference["target_layer"].endswith("asset.usda") and reference["target_prim_path"] == "/Chair"
        assert reference["introducing_prim_path"] == "/World/Chair" and reference["ancestral"] is False
        assert reference["layer_offset"] == {"offset": 10.0, "scale": 1.0}
        assert reference["authored_value"]["asset_path"] == "./asset.usda"
        assert variant["target_prim_path"] == "/Chair{color=blue}"
        assert variant["arc_chain"] == ["reference", "variant"]

        color = chair["variant_sets"][0]
        assert color["name"] == "color" and color["selection"] == "blue"
        assert color["variants"] == ["blue", "red"] and color["selection_is_valid"] is True
        assert color["selection_authored_in"]["layer"].endswith("root.usda")

        stack = chair["prim_stack"]
        assert Path(stack[0]["layer"]).name == "root.usda" and stack[0]["specifier"] == "def"
        assert "references" in stack[0]["fields"]
        anim = next(entry for entry in stack if entry["layer"].endswith("anim.usda"))
        assert anim["specifier"] == "over" and anim["layer_offset"] == {"offset": 5.0, "scale": 2.0}
        assert anim["liverps"] == "L"
        asset_specs = [entry for entry in stack if entry["layer"].endswith("asset.usda")]
        assert [entry["liverps"] for entry in asset_specs] == ["R", "V"]
        assert asset_specs[0]["layer_offset"]["offset"] == 10.0

        seat = usd_insight.composition(stage, "/World/Chair/Seat")
        ancestral = next(arc for arc in seat["arcs"] if arc["arc_type"] == "reference")
        assert ancestral["ancestral"] is True and ancestral["target_prim_path"] == "/Chair/Seat"

        crate = usd_insight.composition(stage, "/World/Crate")
        assert any(arc["arc_type"] == "payload" for arc in crate["arcs"])
        assert crate["loaded"] is True and crate["has_authored_payloads"] is True

        furniture = usd_insight.composition(stage, "/_Furniture")
        assert furniture["specifier"] == "class" and furniture["abstract"] is True


def test_composition_reports_instancing():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        instance = _json(usd_insight.composition(stage, "/World/Instanced"))
        assert instance["instancing"]["instanceable"] is True
        assert instance["instancing"]["is_instance"] is True
        assert instance["instancing"]["prototype"].startswith("/__Prototype")

        proxy = _json(usd_insight.composition(stage, "/World/Instanced/Seat"))
        assert proxy["instancing"]["is_instance_proxy"] is True
        assert proxy["instancing"]["prim_in_prototype"].endswith("/Seat")
        assert any(arc["arc_type"] == "reference" for arc in proxy["arcs"])


def test_explain_value_local_sublayer_beats_inherit_and_reference():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        price = _json(usd_insight.explain_value(stage, "/World/Chair", "price"))
        assert price["value"] == 50.0 and price["source"] == "default" and price["blocked"] is False
        assert price["time"] == {"requested": None, "used": "default"}
        winner = price["winning_opinion"]
        assert Path(winner["layer"]).name == "weak.usda" and winner["liverps"] == "L"
        assert [entry["liverps"] for entry in price["property_stack"]] == ["L", "I", "R"]
        assert [entry["default"] for entry in price["property_stack"]] == [50.0, 99.0, 10.0]
        assert [entry["wins"] for entry in price["property_stack"]] == [True, False, False]
        assert "sublayer weak.usda" in price["why"] and "2 weaker opinion(s)" in price["why"]
        assert "LIVERPS" in price["why"]

        category = usd_insight.explain_value(stage, "/World/Chair.category")
        assert category["value"] == "seating" and category["winning_opinion"]["arc_type"] == "inherit"
        assert "an inherit arc (I in LIVERPS)" in category["why"]

        finish = usd_insight.explain_value(stage, "/World/Chair", "finish")
        assert finish["value"] == "blue"
        assert finish["winning_opinion"]["liverps"] == "V"
        assert finish["winning_opinion"]["arc_chain"] == ["reference", "variant"]
        assert "nested (reference -> variant)" in finish["why"]


def test_explain_value_reports_value_block():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        blocked = _json(usd_insight.explain_value(stage, "/World/Chair", "blockme"))
        assert blocked["blocked"] is True and blocked["value"] is None
        assert blocked["winning_opinion"]["blocked"] is True
        assert blocked["winning_opinion"]["default"] == {"value_block": True}
        assert Path(blocked["winning_opinion"]["layer"]).name == "root.usda"
        assert blocked["property_stack"][1]["default"] == 3.0
        assert "value block" in blocked["why"]


def test_explain_value_time_samples_with_layer_offsets():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        spin = _json(usd_insight.explain_value(stage, "/World/Chair", "spin", time=15))
        assert spin["time"]["used"] == 15.0 and spin["source"] == "time_samples"
        assert abs(spin["value"] - 50.0) < 1e-9
        winner = spin["winning_opinion"]
        assert winner["layer_offset"] == {"offset": 5.0, "scale": 2.0} and winner["layer_time"] == 5.0
        assert spin["time_samples"]["count"] == 2 and spin["time_samples"]["range"] == [5.0, 25.0]
        assert spin["time_samples"]["bracketing"] == [5.0, 25.0]
        assert "maps stage time 15.0 to time 5.0" in spin["why"]

        wobble = usd_insight.explain_value(stage, "/World/Chair", "wobble", time="15")
        assert abs(wobble["value"] - 5.0) < 1e-9 and wobble["winning_opinion"]["liverps"] == "R"
        assert wobble["winning_opinion"]["layer_offset"]["offset"] == 10.0

        at_default = usd_insight.explain_value(stage, "/World/Chair", "wobble")
        assert at_default["value"] is None and at_default["source"] == "none"
        assert at_default["winning_opinion"] is None and "Default time" in at_default["why"]

        strength = usd_insight.explain_value(stage, "/World/Chair", "strength", time=5)
        assert strength["value"] == 1.0 and strength["source"] == "default"
        assert "stronger default still wins" in strength["why"]


def test_explain_value_fallback_and_errors():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        size = _json(usd_insight.explain_value(stage, "/World/Ghost", "size"))
        assert size["source"] == "fallback" and size["value"] == 2.0 and size["fallback"] == 2.0
        assert size["winning_opinion"] is None and "fallback" in size["why"]
        _raises(ValueError, usd_insight.explain_value, stage, "/World/Chair", "nope")
        _raises(ValueError, usd_insight.explain_value, stage, "/World/Nope", "size")
        _raises(ValueError, usd_insight.explain_value, stage, "/World/Chair", "price", time="soon")


def test_properties_lists_attributes_primvars_relationships_and_metadata():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        seat = _json(usd_insight.properties(stage, "/World/Chair/Seat"))
        attributes = {entry["name"]: entry for entry in seat["attributes"]}

        color = attributes["primvars:displayColor"]
        assert color["type_name"] == "color3f[]" and color["variability"] == "varying"
        assert color["authored"] is True and color["custom"] is False
        assert color["value"] == [[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]]
        assert color["primvar"]["interpolation"] == "vertex" and color["primvar"]["indexed"] is True
        assert color["primvar"]["indices"] == [0, 1, 0]
        assert attributes["primvars:displayColor:indices"]["indices_for"] == "primvars:displayColor"
        roughness = attributes["primvars:roughness"]["primvar"]
        assert roughness["interpolation"] == "constant" and roughness["indexed"] is False
        assert attributes["doubleSided"]["variability"] == "uniform" and attributes["doubleSided"]["authored"] is False
        texture = attributes["texture"]["value"]
        assert texture["authored"] == "./asset.usda" and texture["resolved"].replace("\\", "/").endswith("asset.usda")

        relationships = {entry["name"]: entry for entry in seat["relationships"]}
        assert relationships["material:binding"]["targets"] == ["/World/Chair/Looks/Wood"]
        assert seat["metadata"]["api_schemas"] == ["MaterialBindingAPI"]

        trimmed = usd_insight.properties(stage, "/World/Chair/Seat", include_values=False, authored_only=True)
        names = [entry["name"] for entry in trimmed["attributes"]]
        assert "doubleSided" not in names and "points" in names
        assert all("value" not in entry for entry in trimmed["attributes"])

        chair = _json(usd_insight.properties(stage, "/World/Chair", time=15))
        spin = next(entry for entry in chair["attributes"] if entry["name"] == "spin")
        assert spin["custom"] is True and spin["time_sample_count"] == 2 and abs(spin["value"] - 50.0) < 1e-9
        assert spin["time_sample_range"] == [5.0, 25.0]
        metadata = chair["metadata"]
        assert metadata["kind"] == "component" and metadata["documentation"] == "A chair"
        assert metadata["custom_data"] == {"owner": "lesson"} and metadata["asset_info"] == {"name": "chair"}
        assert metadata["hidden"] is False
        assert usd_insight.properties(stage, "/World/Helper")["metadata"]["hidden"] is True


def test_bounds_extent_visibility_and_purpose():
    with _scene() as (_folder, root):
        stage = file_stage.open_readonly(root)
        world = _json(usd_insight.bounds(stage, "/World"))
        assert world["world_bound"]["empty"] is False
        assert _close(world["world_bound"]["min"], [0, -1, -1]) and _close(world["world_bound"]["max"], [6, 1, 1])

        box = _json(usd_insight.bounds(stage, "/World/Crate/Box"))
        assert box["extent"]["authored"] == [[-1.0, -1.0, -1.0], [1.0, 1.0, 1.0]]
        assert box["extent"]["computed"] == [[-2.0, -2.0, -2.0], [2.0, 2.0, 2.0]]
        assert box["extent"]["matches"] is False and any("authored extent" in note for note in box["notes"])
        assert _close(box["world_bound"]["min"], [4, -1, -1]) and _close(box["world_bound"]["center"], [5, 0, 0])
        assert box["visibility"] == {"authored": None, "computed": "inherited"}

        seat = usd_insight.bounds(stage, "/World/Chair/Seat")
        assert seat["extent"]["matches"] is True
        assert _close(seat["world_bound"]["min"], [2, 0, 0]) and _close(seat["world_bound"]["size"], [1, 1, 0])

        ghost = _json(usd_insight.bounds(stage, "/World/Ghost"))
        assert ghost["visibility"] == {"authored": "invisible", "computed": "invisible"}
        assert any("invisible" in note for note in ghost["notes"])

        helper = _json(usd_insight.bounds(stage, "/World/Helper"))
        assert helper["world_bound"]["empty"] is True and helper["purpose"]["computed"] == "guide"
        assert any("guide" in note for note in helper["notes"])
        with_guides = usd_insight.bounds(stage, "/World/Helper", purposes=("default", "guide"))
        assert _close(with_guides["world_bound"]["min"], [-1, 49, -1])
        _raises(ValueError, usd_insight.bounds, stage, "/World", purposes=("sideways",))


def test_resolve_scene_file_accepts_paths_and_file_uris():
    with _scene() as (folder, root):
        expected = str(Path(root).resolve())
        assert file_stage.resolve_scene_file(root, workspace_root=folder) == expected
        assert file_stage.resolve_scene_file("root.usda", workspace_root=folder) == expected
        forward = expected.replace("\\", "/")
        assert file_stage.resolve_scene_file(stage_name=f"file:/{forward}", workspace_root=folder) == expected
        assert file_stage.resolve_scene_file(stage_name=f"file:///{forward}", workspace_root=folder) == expected
        assert file_stage.resolve_scene_file(stage_name=forward, workspace_root=folder) == expected


def test_resolve_scene_file_rejects_outside_workspace_and_non_usd():
    with _scene() as (folder, root), tempfile.TemporaryDirectory(prefix="usd_outside_") as outside:
        stray = Path(outside, "stray.usda")
        stray.write_text("#usda 1.0\n", encoding="utf-8")
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file, str(stray), workspace_root=folder)
        allowed = file_stage.resolve_scene_file(str(stray), workspace_root=folder, allow_outside_workspace=True)
        assert allowed == str(stray.resolve())
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file, os.path.join(folder, "..", Path(outside).name, "stray.usda"), workspace_root=folder)

        notes = Path(folder, "notes.txt")
        notes.write_text("not usd", encoding="utf-8")
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file, str(notes), workspace_root=folder)
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file, str(Path(folder, "missing.usda")), workspace_root=folder)
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file, stage_name="anon:0000:root-session.usda")
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file, stage_name="omniverse://server/scene.usd")
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file)

        default_root = file_stage.default_workspace_root()
        if not os.environ.get("OMNIVERSE_ROOT"):
            assert (default_root / "omniverse_scene_mcp").is_dir()
        _raises(file_stage.SceneFileError, file_stage.resolve_scene_file, root)


def test_resolve_scene_file_rejects_unc_and_outside_paths_before_touching_the_filesystem():
    touched = []
    original_resolve = Path.resolve

    def spying_resolve(self, *args, **kwargs):
        touched.append(str(self))
        if str(self).startswith(("\\\\", "//")):
            raise AssertionError(f"UNC path resolved: {self}")
        return original_resolve(self, *args, **kwargs)

    with _scene() as (folder, _root):
        before = len(touched)
        Path.resolve = spying_resolve
        try:
            for text in (
                "\\\\evil-host\\share\\x.usda",
                "//evil-host/share/x.usda",
                "C:/Windows/System32/does-not-exist.usda",
            ):
                try:
                    file_stage.resolve_scene_file(text, workspace_root=folder)
                except file_stage.SceneFileError as exc:
                    assert "outside the workspace" in str(exc), (text, str(exc))
                else:
                    raise AssertionError(f"{text} should be rejected")
            try:
                file_stage.resolve_scene_file(stage_name="file://evil-host/share/x.usda", workspace_root=folder)
            except file_stage.SceneFileError as exc:
                assert "outside the workspace" in str(exc), str(exc)
            else:
                raise AssertionError("file://evil-host URI should be rejected")
        finally:
            Path.resolve = original_resolve
        # Only the workspace root itself is resolved; none of the rejected candidates are.
        assert all("evil-host" not in name and "System32" not in name for name in touched[before:]), touched[before:]


def test_readonly_stage_never_modifies_files():
    with _scene() as (folder, root):
        before = _snapshot(folder)
        with file_stage.readonly_stage(root) as stage:
            _json(usd_insight.stage_info(stage))
            for path in ("/World", "/World/Chair", "/World/Chair/Seat", "/World/Instanced/Seat", "/_Furniture"):
                _json(usd_insight.composition(stage, path))
                _json(usd_insight.properties(stage, path))
                _json(usd_insight.bounds(stage, path))
            for name, time in (("price", None), ("spin", 15), ("wobble", 15), ("blockme", None), ("finish", None)):
                _json(usd_insight.explain_value(stage, "/World/Chair", name, time))
            assert not any(layer.dirty for layer in stage.GetUsedLayers())
        assert _snapshot(folder) == before

        stage = file_stage.open_readonly(root)
        assert stage.GetPrimAtPath("/World/Crate/Box").IsValid()
        del stage
        assert _snapshot(folder) == before


def test_readonly_stage_detects_dirty_layers_and_changed_files():
    with _scene() as (folder, root):
        before = _snapshot(folder)
        error = None
        try:
            with file_stage.readonly_stage(root) as stage:
                stage.GetPrimAtPath("/World").SetMetadata("comment", "accidental edit")
        except file_stage.ReadOnlyViolation as exc:
            error = str(exc)
        assert error is not None and "root.usda" in error
        assert _snapshot(folder) == before

    with _scene() as (folder, root):
        anim = Path(folder, "anim.usda")
        error = None
        try:
            with file_stage.readonly_stage(root):
                stamp = anim.stat().st_mtime_ns + 5_000_000_000
                os.utime(anim, ns=(stamp, stamp))
        except file_stage.ReadOnlyViolation as exc:
            error = str(exc)
        assert error is not None and "anim.usda" in error


def test_open_readonly_rejects_unreadable_usd():
    with _scene() as (folder, _root):
        bad = Path(folder, "bad.usda")
        bad.write_text("this is not usda", encoding="utf-8")
        _raises(file_stage.SceneFileError, file_stage.open_readonly, str(bad))


@contextlib.contextmanager
def _stage_from(files, root="shot.usda"):
    with tempfile.TemporaryDirectory(prefix="usd_insight_") as folder:
        for name, text in files.items():
            Path(folder, name).write_text(text, encoding="utf-8")
        stage = Usd.Stage.Open(str(Path(folder, root)))
        try:
            yield stage
        finally:
            del stage


def test_explain_value_names_stronger_samples_only_specs_at_the_default_time():
    files = {
        "shot.usda": '#usda 1.0\ndef Xform "World"\n{\n    def Sphere "Ball" (\n        prepend references = @asset.usda@</A>\n'
        '    )\n    {\n        double radius.timeSamples = {\n            0: 1,\n        }\n    }\n}\n',
        "asset.usda": '#usda 1.0\ndef Sphere "A"\n{\n    double radius = 9\n}\n',
    }
    with _stage_from(files) as stage:
        result = usd_insight.explain_value(stage, "/World/Ball", "radius")
        assert result["value"] == 9.0
        assert "No stronger opinion" not in result["why"]
        assert "Stronger spec(s) (shot.usda /World/Ball.radius) hold only time samples" in result["why"]
        at_zero = usd_insight.explain_value(stage, "/World/Ball", "radius", time=0)
        assert at_zero["value"] == 1.0 and "Stronger spec(s)" not in at_zero["why"]


def test_explain_value_reports_a_spline_winner():
    files = {
        "shot.usda": '#usda 1.0\ndef Xform "World"\n{\n    def Cube "Box"\n    {\n        double size.spline = {\n'
        '            0: 1; post linear,\n            10: 11; post linear,\n        }\n    }\n}\n'
    }
    with _stage_from(files) as stage:
        attr = stage.GetAttributeAtPath("/World/Box.size")
        if attr.GetResolveInfo(5).GetSource() != Usd.ResolveInfoSourceSpline:
            print("SKIP spline check: this USD build does not read spline values")
            return
        result = usd_insight.explain_value(stage, "/World/Box", "size", time=5)
        assert result["source"] == "spline" and _close([result["value"]], [6.0])
        assert result["winning_opinion"] is not None and result["winning_opinion"]["spline"] is True
        assert "no spec in the property stack matched" not in result["why"]
        assert "the winner is the spline" in result["why"]
        # At the Default time the spline is ignored; say so instead of claiming nothing is authored.
        default = usd_insight.explain_value(stage, "/World/Box", "size")
        assert default["source"] == "fallback"
        assert "ignores splines" in default["why"], default["why"]
        assert "none of them authors a value" not in default["why"]


def test_to_jsonable_handles_asset_arrays_and_time_codes():
    assets = Sdf.AssetPathArray([Sdf.AssetPath("a.png"), Sdf.AssetPath("b.png")])
    converted = usd_insight.to_jsonable(assets)
    assert isinstance(converted, list) and [item["authored"] for item in converted] == ["a.png", "b.png"]
    time_code = getattr(Gf, "TimeCode", None) or getattr(Sdf, "TimeCode")
    assert usd_insight.to_jsonable(time_code(12)) == 12.0
    array_type = getattr(Vt, "TimeCodeArray", None) or getattr(Sdf, "TimeCodeArray")
    assert usd_insight.to_jsonable(array_type([time_code(1), time_code(2)])) == [1.0, 2.0]
    assert usd_insight.to_jsonable(Usd.TimeCode.Default()) == "default"


def test_stage_info_marks_diamond_sublayers_repeated_not_cycles():
    files = {
        "shot.usda": '#usda 1.0\n(\n    subLayers = [@a.usda@, @b.usda@]\n)\n',
        "a.usda": '#usda 1.0\n(\n    subLayers = [@c.usda@]\n)\n',
        "b.usda": '#usda 1.0\n(\n    subLayers = [@c.usda@]\n)\n',
        "c.usda": '#usda 1.0\n',
    }
    with _stage_from(files) as stage:
        layers = usd_insight.stage_info(stage)["layer_stack"]
        names = [Path(entry["identifier"]).name for entry in layers if entry["role"] != "session"]
        assert names == ["shot.usda", "a.usda", "c.usda", "b.usda", "c.usda"]
        assert not any(entry.get("cycle") for entry in layers)
        assert [entry.get("repeated", False) for entry in layers if Path(entry["identifier"]).name == "c.usda"] == [False, True]
    cyclic = {
        "shot.usda": '#usda 1.0\n(\n    subLayers = [@a.usda@]\n)\n',
        "a.usda": '#usda 1.0\n(\n    subLayers = [@shot.usda@]\n)\n',
    }
    with _stage_from(cyclic) as stage:
        layers = usd_insight.stage_info(stage)["layer_stack"]
        assert any(entry.get("cycle") for entry in layers)


# --- cooperative time budgets ------------------------------------------------------------------------------------

BUDGET_TOTAL = 1 + 200 + 200 * 100  # /World, 200 groups, 100 cubes each


def _translate(spec, offset):
    attr = Sdf.AttributeSpec(spec, "xformOp:translate", Sdf.ValueTypeNames.Double3)
    attr.default = Gf.Vec3d(*offset)
    order = Sdf.AttributeSpec(spec, "xformOpOrder", Sdf.ValueTypeNames.TokenArray, variability=Sdf.VariabilityUniform)
    order.default = Vt.TokenArray(["xformOp:translate"])


def _budget_stage(groups=200, per=100):
    """In-memory /World (moved up 10 in y) > G<i> (x = 3 * i) > C<j> cubes with authored extents: 20,201 prims."""
    layer = Sdf.Layer.CreateAnonymous(".usda")
    with Sdf.ChangeBlock():
        world = Sdf.PrimSpec(layer, "World", Sdf.SpecifierDef, "Xform")
        _translate(world, (0.0, 10.0, 0.0))
        for group_index in range(groups):
            group = Sdf.PrimSpec(world, f"G{group_index}", Sdf.SpecifierDef, "Xform")
            _translate(group, (3.0 * group_index, 0.0, 0.0))
            for cube_index in range(per):
                cube = Sdf.PrimSpec(group, f"C{cube_index}", Sdf.SpecifierDef, "Cube")
                extent = Sdf.AttributeSpec(cube, "extent", Sdf.ValueTypeNames.Float3Array)
                extent.default = Vt.Vec3fArray([Gf.Vec3f(-1, -1, -1), Gf.Vec3f(1, 1, 1)])
    return Usd.Stage.Open(layer)


class _Countdown(usd_insight.TimeBudget):
    """Deterministic budget: the first `checks` clock checks pass, every later one reports the time as used up."""

    def __init__(self, checks):
        super().__init__(60_000)
        self.checks = checks

    def expired(self):
        self.checks -= 1
        if self.checks < 0:
            self.exhausted = True
        return self.exhausted


def _without_time_keys(value):
    if isinstance(value, dict):
        return {key: _without_time_keys(item) for key, item in value.items() if key not in ("truncated_by_time", "time_budget")}
    if isinstance(value, list):
        return [_without_time_keys(item) for item in value]
    return value


def test_time_budget_counts_down_resolves_the_tightest_and_nests_as_ambient():
    roomy = usd_insight.TimeBudget(60_000)
    assert not roomy.expired() and not roomy.tick() and 0 < roomy.remaining_ms() <= 60_000 and roomy.elapsed_ms() >= 0
    spent = usd_insight.TimeBudget(0)
    assert spent.expired() and spent.tick() and spent.remaining_ms() == 0
    assert usd_insight.TimeBudget.until(10.0, 9.0).budget_ms == 1000.0
    assert usd_insight.TimeBudget.until(5.0, 9.0).expired()
    for bad in (-1, float("nan"), float("inf"), True, "5", None):
        _raises(ValueError, usd_insight.TimeBudget, bad)
    _raises(ValueError, usd_insight.TimeBudget.until, "soon", 1.0)

    assert usd_insight.resolve_budget() is None
    assert usd_insight.resolve_budget(budget_ms=5).budget_ms == 5.0
    assert usd_insight.resolve_budget(deadline=roomy) is roomy
    assert usd_insight.resolve_budget(deadline=12.0, now=10.0).budget_ms == 2000.0
    assert "TimeBudget" in str(_raises(TypeError, usd_insight.resolve_budget, 12.0))
    assert usd_insight.resolve_budget(deadline=roomy, budget_ms=1).budget_ms == 1.0  # the tighter limit wins

    assert usd_insight.active_budget() is None
    with usd_insight.TimeBudget(50_000) as outer:
        assert usd_insight.active_budget() is outer
        with usd_insight.TimeBudget(20_000) as inner:
            assert usd_insight.active_budget() is inner
            assert usd_insight.resolve_budget(budget_ms=55_000) is inner
            assert usd_insight.resolve_budget(budget_ms=1).budget_ms == 1.0
        assert usd_insight.active_budget() is outer
    assert usd_insight.active_budget() is None
    try:
        with usd_insight.TimeBudget(5):
            raise RuntimeError("query failed")
    except RuntimeError:
        pass
    assert usd_insight.active_budget() is None

    record = roomy.report(7, False, extra=(1, 2))
    assert record["truncated_by_time"] is False and record["processed"] == 7 and record["budget_ms"] == 60_000.0
    assert record["extra"] == [1, 2] and record["note"] and not roomy.truncated
    assert roomy.report(3, True)["truncated_by_time"] is True and roomy.truncated
    _json(record)


def test_stage_info_time_budget_on_a_20k_prim_stage():
    stage = _budget_stage()
    before = stage.GetRootLayer().ExportToString()
    plain = usd_insight.stage_info(stage)
    assert plain["prims"]["total"] == BUDGET_TOTAL
    assert "truncated_by_time" not in plain and "time_budget" not in plain

    for tiny in (usd_insight.stage_info(stage, budget_ms=0), usd_insight.stage_info(stage, budget_ms=1)):
        assert tiny["truncated_by_time"] is True and tiny["prims"]["truncated_by_time"] is True
        budget = tiny["time_budget"]
        assert budget["truncated_by_time"] is True and budget["processed"] == tiny["prims"]["total"] < BUDGET_TOTAL
        assert "time budget" in budget["note"] and budget["elapsed_ms"] >= 0 and budget["budget_ms"] <= 1
        _json(tiny)

    partial = usd_insight.stage_info(stage, deadline=_Countdown(6))
    assert partial["truncated_by_time"] is True and partial["model_hierarchy"]["truncated_by_time"] is True
    assert 0 < partial["time_budget"]["processed"] == partial["prims"]["total"] < BUDGET_TOTAL
    assert partial["layer_stack"] and "composition errors were not collected" in partial["time_budget"]["note"]

    generous = usd_insight.stage_info(stage, budget_ms=600_000)
    assert generous["truncated_by_time"] is False and generous["prims"]["truncated_by_time"] is False
    assert generous["time_budget"]["processed"] == BUDGET_TOTAL
    assert generous["time_budget"]["note"] == "Finished within the time budget."
    assert _without_time_keys(generous) == plain
    with usd_insight.TimeBudget(0):
        assert usd_insight.stage_info(stage)["truncated_by_time"] is True  # the ambient budget applies
    assert usd_insight.active_budget() is None
    assert stage.GetRootLayer().ExportToString() == before


def test_bounds_time_budget_returns_partial_bounds_or_the_exact_ones():
    stage = _budget_stage()
    plain = usd_insight.bounds(stage, "/World")
    assert plain["world_bound"]["min"] == [-1.0, 9.0, -1.0] and plain["world_bound"]["max"] == [598.0, 11.0, 1.0]
    assert "truncated_by_time" not in plain

    generous = usd_insight.bounds(stage, "/World", budget_ms=600_000)
    assert generous["truncated_by_time"] is False and generous["time_budget"]["processed"] == BUDGET_TOTAL
    for key in ("world_bound", "local_bound", "untransformed_bound", "extent", "visibility", "purpose", "notes"):
        assert generous[key] == plain[key], key

    spent = usd_insight.bounds(stage, "/World", budget_ms=0)
    assert spent["truncated_by_time"] is True and spent["time_budget"]["processed"] == 0
    assert spent["world_bound"] == {"empty": True, "min": None, "max": None, "size": None, "center": None, "partial": True}
    assert "time budget" in spent["notes"][0] and spent["notes"][0] == spent["time_budget"]["note"]

    # Stopped after the first group was measured: only G0's cubes (x -1..1) count; the transforms still apply.
    partial = usd_insight.bounds(stage, "/World", deadline=_Countdown(20))
    measured = partial["time_budget"]["processed"]
    assert partial["truncated_by_time"] is True and 0 < measured < BUDGET_TOTAL
    world, local, untransformed = partial["world_bound"], partial["local_bound"], partial["untransformed_bound"]
    assert world["partial"] is True and not world["empty"]
    assert world["min"] == [-1.0, 9.0, -1.0] and world["max"][0] < plain["world_bound"]["max"][0]
    assert local == world  # /World's parent is the pseudo-root, so local space is world space here
    assert untransformed["min"] == [-1.0, -1.0, -1.0] and untransformed["max"][1] == 1.0
    _json(partial)

    one_group = usd_insight.bounds(stage, "/World/G5", budget_ms=600_000)
    assert one_group["truncated_by_time"] is False and one_group["time_budget"]["processed"] == 101
    assert one_group["world_bound"] == usd_insight.bounds(stage, "/World/G5")["world_bound"]


def test_properties_time_budget_stops_on_a_prim_with_many_attributes():
    layer = Sdf.Layer.CreateAnonymous(".usda")
    with Sdf.ChangeBlock():
        spec = Sdf.PrimSpec(layer, "P", Sdf.SpecifierDef, "Xform")
        for index in range(2000):
            Sdf.AttributeSpec(spec, f"a{index:04d}", Sdf.ValueTypeNames.Double).default = float(index)
    stage = Usd.Stage.Open(layer)
    plain = usd_insight.properties(stage, "/P", authored_only=True)
    assert plain["attribute_count"] == 2000 and len(plain["attributes"]) == usd_insight.MAX_PROPERTIES
    generous = usd_insight.properties(stage, "/P", authored_only=True, budget_ms=600_000)
    assert generous["truncated_by_time"] is False and generous["time_budget"]["processed"] >= 2000
    assert _without_time_keys(generous) == plain

    spent = usd_insight.properties(stage, "/P", budget_ms=0)
    assert spent["truncated_by_time"] is True and spent["attributes"] == [] and spent["time_budget"]["processed"] == 0
    partial = usd_insight.properties(stage, "/P", authored_only=True, deadline=_Countdown(3))
    examined = partial["time_budget"]["processed"]
    assert partial["truncated_by_time"] is True and 0 < examined < 2000
    assert partial["attribute_count"] == examined == len(partial["attributes"])
    assert "incomplete" in partial["time_budget"]["note"]
    _json(partial)
