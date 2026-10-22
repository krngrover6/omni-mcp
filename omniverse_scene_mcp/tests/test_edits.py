"""Safe set_attribute, UsdPreviewSurface materials and material binding, run on temporary in-memory and temp-file stages."""

import json
import math
import tempfile
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    # unittest.SkipTest at import is a module skip for pytest and tools/run_tests.py alike, with or without pytest.
    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import edits

T = UsdShade.Tokens


def _stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, "Z")
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Cube")
    UsdGeom.Scope.Define(stage, "/World/Looks")
    return stage


def _snapshot(stage):
    return tuple(layer.ExportToString() for layer in stage.GetLayerStack(True))


def _json(result):
    json.dumps(result, allow_nan=False)
    return result


def _dry(function, stage, *args, **kwargs):
    """Call with the default dry_run and check nothing in any layer of the stack changed."""
    before = _snapshot(stage)
    result = _json(function(stage, *args, **kwargs))
    assert _snapshot(stage) == before, "a dry run changed a layer"
    assert result["dry_run"] is True and result["written"] is False
    return result


def _ok(result):
    assert result["ok"] is True, result.get("error")
    return result


def _refused(function, stage, *args, fragment, **kwargs):
    before = _snapshot(stage) if stage is not None else None
    result = _json(function(stage, *args, **kwargs))
    assert result["ok"] is False, result
    assert result["written"] is False
    assert fragment.lower() in result["error"].lower(), result["error"]
    if stage is not None:
        assert _snapshot(stage) == before, "a refusal changed a layer"
    return result


def _apply(function, stage, *args, **kwargs):
    result = _ok(_json(function(stage, *args, dry_run=False, **kwargs)))
    assert result["dry_run"] is False and result["written"] is True
    return result


def _set(stage, path, name, value, **kwargs):
    """Dry run first (must not write), then the real write; returns (dry, real)."""
    dry = _ok(_dry(edits.set_attribute, stage, path, name, value, **kwargs))
    real = _apply(edits.set_attribute, stage, path, name, value, **kwargs)
    return dry, real


def _close(first, second, tolerance=1e-5):
    if isinstance(first, (list, tuple)):
        return len(first) == len(second) and all(_close(a, b, tolerance) for a, b in zip(first, second))
    return math.isclose(float(first), float(second), rel_tol=tolerance, abs_tol=tolerance)


def _file_stage(tmp):
    """root.usda (stronger) with weak.usda as its only sublayer, both real files."""
    weak = Sdf.Layer.CreateNew(str(Path(tmp) / "weak.usda"))
    root = Sdf.Layer.CreateNew(str(Path(tmp) / "root.usda"))
    root.subLayerPaths.append("./weak.usda")
    stage = Usd.Stage.Open(root)
    stage.SetEditTarget(Usd.EditTarget(weak))
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Cube")
    return stage, root, weak


# --- set_attribute: dry run, basics -------------------------------------------------------------------------


def test_set_attribute_is_a_dry_run_by_default_and_reports_before_and_after():
    stage = _stage()
    result = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 3))
    assert result["type_name"] == "double"
    assert result["attribute"] == "size" and result["path"] == "/World/Cube"
    assert result["before"]["value"] == 2.0 and result["before"]["source"] == "fallback"
    assert result["before"]["winning_layer"] is None and result["before"]["edit_target_value"] is None
    assert result["after"] == 3.0
    assert result["would_change"] is True and "changed" not in result
    assert result["effective_after"]["visible"] is True and result["effective_after"]["predicted_value"] == 3.0
    assert result["edit_target_layer"] == stage.GetRootLayer().identifier
    assert any("anonymous" in warning for warning in result["warnings"])
    assert "dry_run=False" in result["next_step"]
    assert UsdGeom.Cube(stage.GetPrimAtPath("/World/Cube")).GetSizeAttr().Get() == 2.0


def test_set_attribute_writes_only_with_explicit_false_dry_run():
    stage = _stage()
    for flag in (None, True, 0, "yes", "dry", []):
        _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 3, dry_run=flag))
    real = _apply(edits.set_attribute, stage, "/World/Cube", "size", 3)
    assert real["changed"] is True and real["readback"] == {"value": 3.0, "shows_new_value": True}
    assert stage.GetPrimAtPath("/World/Cube").GetAttribute("size").Get() == 3.0
    assert "double size = 3" in stage.GetRootLayer().ExportToString()
    again = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 3.0))
    assert again["would_change"] is False and again["before"]["winning_layer"] == stage.GetRootLayer().identifier
    assert again["before"]["source"] == "default" and again["before"]["edit_target_value"] == 3.0
    string_flag = _ok(_json(edits.set_attribute(stage, "/World/Cube", "size", 4, dry_run="False")))
    assert string_flag["written"] is True and stage.GetPrimAtPath("/World/Cube").GetAttribute("size").Get() == 4.0


# --- value types --------------------------------------------------------------------------------------------


def test_float_half_double_and_integer_ranges():
    stage = _stage()
    cube = "/World/Cube"
    _refused(edits.set_attribute, stage, cube, "h", 70000, create_type="half", fragment="half precision")
    _refused(edits.set_attribute, stage, cube, "f", 1e39, create_type="float", fragment="float precision")
    _refused(edits.set_attribute, stage, cube, "d", float("nan"), create_type="double", fragment="finite")
    _refused(edits.set_attribute, stage, cube, "d", "lots", create_type="double", fragment="must be a number")
    _refused(edits.set_attribute, stage, cube, "d", True, create_type="double", fragment="must be a number")
    _refused(edits.set_attribute, stage, cube, "c", 256, create_type="uchar", fragment="between 0 and 255")
    _refused(edits.set_attribute, stage, cube, "u", -1, create_type="uint", fragment="between 0 and")
    _refused(edits.set_attribute, stage, cube, "i", 3.5, create_type="int", fragment="whole number")
    _refused(edits.set_attribute, stage, cube, "i", 2**31, create_type="int", fragment="between")
    _refused(edits.set_attribute, stage, cube, "big", 2**64, create_type="uint64", fragment="between")

    prim = stage.GetPrimAtPath(cube)
    for name, type_text, value, expected in (
        ("h", "half", 0.5, 0.5),
        ("f", "float", "0.25", 0.25),
        ("d", "double", 1e300, 1e300),
        ("c", "uchar", 255, 255),
        ("u", "uint", 4000000000, 4000000000),
        ("i", "int", 3.0, 3),
        ("s", "int", "7", 7),
        ("i64", "int64", 2**40, 2**40),
        ("u64", "uint64", 2**63, 2**63),
    ):
        dry, real = _set(stage, cube, name, value, create_type=type_text)
        assert dry["type_name"] == type_text and dry["creates_attribute"] is True and dry["after"] == expected
        attr = prim.GetAttribute(name)
        assert str(attr.GetTypeName()) == type_text and attr.IsCustom()
        assert attr.Get() == expected
        assert real["readback"]["shows_new_value"] is True


def test_bool_values_and_uniform_attributes_refuse_time_samples():
    stage = _stage()
    UsdGeom.Mesh.Define(stage, "/World/Mesh")
    for value, expected in (("yes", True), (False, False), (1, True), ("off", False)):
        dry, _real = _set(stage, "/World/Mesh", "doubleSided", value)
        assert dry["type_name"] == "bool" and dry["after"] is expected
        assert stage.GetPrimAtPath("/World/Mesh").GetAttribute("doubleSided").Get() is expected
    _refused(edits.set_attribute, stage, "/World/Mesh", "doubleSided", 2, fragment="true or false")
    _refused(edits.set_attribute, stage, "/World/Mesh", "doubleSided", True, time=4, fragment="uniform")


def test_tokens_are_validated_against_allowed_tokens():
    stage = _stage()
    dry, _real = _set(stage, "/World/Cube", "visibility", "invisible")
    assert dry["type_name"] == "token" and dry["after"] == "invisible"
    assert stage.GetPrimAtPath("/World/Cube").GetAttribute("visibility").Get() == "invisible"
    fixed = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "visibility", "Inherited"))
    assert fixed["after"] == "inherited" and any("allowed token" in note for note in fixed["notes"])
    refused = _refused(edits.set_attribute, stage, "/World/Cube", "visibility", "hidden", fragment="not an allowed token")
    assert "inherited, invisible" in refused["error"]
    _refused(edits.set_attribute, stage, "/World/Cube", "purpose", "render", time=1, fragment="uniform")
    _refused(edits.set_attribute, stage, "/World/Cube", "purpose", "renderish", fragment="allowed tokens: default, render")
    _refused(edits.set_attribute, stage, "/World/Cube", "visibility", 3, fragment="must be text")
    free = _set(stage, "/World/Cube", "kind_of_thing", "anything", create_type="token")[0]
    assert free["after"] == "anything"  # custom tokens have no allowedTokens


def test_strings_assets_timecodes_and_path_expressions():
    stage = _stage()
    prim = stage.GetPrimAtPath("/World/Cube")
    _set(stage, "/World/Cube", "note", "hello world", create_type="string")
    assert prim.GetAttribute("note").Get() == "hello world"
    _refused(edits.set_attribute, stage, "/World/Cube", "note", 5, fragment="must be text")

    dry, _real = _set(stage, "/World/Cube", "texture", "textures/wood.png", create_type="asset")
    assert dry["after"] == "textures/wood.png"
    stored = prim.GetAttribute("texture").Get()
    assert isinstance(stored, Sdf.AssetPath) and stored.path == "textures/wood.png"
    round_trip = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "texture", {"authored": "textures/wood.png", "resolved": None}))
    assert round_trip["would_change"] is False
    windows = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "texture", "C:\\tex\\wood.png"))
    assert any("forward slashes" in warning for warning in windows["warnings"])

    _set(stage, "/World/Cube", "startFrame", 24, create_type="timecode")
    assert float(prim.GetAttribute("startFrame").Get()) == 24.0
    _set(stage, "/World/Cube", "frames", [1, 2.5], create_type="timecode[]")
    assert [float(item) for item in prim.GetAttribute("frames").Get()] == [1.0, 2.5]

    _set(stage, "/World/Cube", "selection", "/World//", create_type="pathExpression")
    assert prim.GetAttribute("selection").Get().GetText() == "/World//"


def test_colors_normalize_0_255_and_hex_and_refuse_negative():
    stage = _stage()
    prim = stage.GetPrimAtPath("/World/Cube")
    dry, _real = _set(stage, "/World/Cube", "primvars:displayColor", [[255, 128, 0]])
    assert dry["type_name"] == "color3f[]"
    assert _close(dry["after"][0], [1.0, 128 / 255.0, 0.0])
    assert any("0-255" in note for note in dry["notes"])
    assert _close(list(prim.GetAttribute("primvars:displayColor").Get()[0]), [1.0, 128 / 255.0, 0.0])

    unit = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "primvars:displayColor", [[0.2, 0.4, 0.6], [1, 1, 1]]))
    assert _close(unit["after"], [[0.2, 0.4, 0.6], [1, 1, 1]]) and not any("0-255" in note for note in unit["notes"])

    hexed = _set(stage, "/World/Cube", "tint", "#FF8000", create_type="color3f")[0]
    assert _close(hexed["after"], [1.0, 128 / 255.0, 0.0]) and any("hex color" in note for note in hexed["notes"])
    short = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "tint", "#0f0"))
    assert _close(short["after"], [0.0, 1.0, 0.0])
    _refused(edits.set_attribute, stage, "/World/Cube", "tint", "#12345", fragment="not a hex color")
    _refused(edits.set_attribute, stage, "/World/Cube", "tint", [-0.1, 0, 0], fragment="negative")

    hdr = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "tint", [2.5, 1, 1]))
    assert hdr["after"] == [2.5, 1.0, 1.0] and any("HDR" in warning for warning in hdr["warnings"])

    glow = _set(stage, "/World/Cube", "inputs:emissiveColor", [10, 10, 10], create_type="color3f")[0]
    assert glow["after"] == [10.0, 10.0, 10.0] and any("HDR emission" in note for note in glow["notes"])

    rgba = _set(stage, "/World/Cube", "overlay", [255, 0, 0, 51], create_type="color4f")[0]
    assert _close(rgba["after"], [1.0, 0.0, 0.0, 0.2])
    assert _close(list(prim.GetAttribute("overlay").Get()), [1.0, 0.0, 0.0, 0.2])


def test_vectors_parse_lists_strings_and_single_numbers():
    stage = _stage()
    UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Cube")).SetTranslate(Gf.Vec3d(0, 0, 0))
    prim = stage.GetPrimAtPath("/World/Cube")
    dry, _real = _set(stage, "/World/Cube", "xformOp:translate", "1, 2, 3")
    assert dry["type_name"] == "double3" and dry["after"] == [1.0, 2.0, 3.0]
    assert prim.GetAttribute("xformOp:translate").Get() == Gf.Vec3d(1, 2, 3)
    _set(stage, "/World/Cube", "xformOp:translate", (4, 5, 6))
    _set(stage, "/World/Cube", "xformOp:translate", Gf.Vec3d(7, 8, 9))
    assert prim.GetAttribute("xformOp:translate").Get() == Gf.Vec3d(7, 8, 9)
    _refused(edits.set_attribute, stage, "/World/Cube", "xformOp:translate", [1, 2], fragment="needs 3 numbers")
    _refused(edits.set_attribute, stage, "/World/Cube", "xformOp:translate", "1, two, 3", fragment="could not read")
    _refused(edits.set_attribute, stage, "/World/Cube", "xformOp:translate", {"x": 1}, fragment="list of values")
    broadcast = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "xformOp:translate", 5))
    assert broadcast["after"] == [5.0, 5.0, 5.0] and any("single number" in note for note in broadcast["notes"])

    _set(stage, "/World/Cube", "cell", [1, 2], create_type="int2")
    assert prim.GetAttribute("cell").Get() == Gf.Vec2i(1, 2)
    _refused(edits.set_attribute, stage, "/World/Cube", "cell", [1.5, 2], fragment="whole numbers")
    _set(stage, "/World/Cube", "small", [0.5, 1, 2], create_type="half3")
    _refused(edits.set_attribute, stage, "/World/Cube", "small", [70000, 0, 0], fragment="half precision")
    _set(stage, "/World/Cube", "uv", [0.25, 0.75], create_type="texCoord2f")
    assert _close(list(prim.GetAttribute("uv").Get()), [0.25, 0.75])
    _set(stage, "/World/Cube", "p4", [1, 2, 3, 4], create_type="double4")
    assert prim.GetAttribute("p4").Get() == Gf.Vec4d(1, 2, 3, 4)


def test_quaternions_are_normalized_and_matrices_accept_rows_or_flat_lists():
    stage = _stage()
    xform = UsdGeom.Xform.Define(stage, "/World/Spin")
    xform.AddOrientOp(UsdGeom.XformOp.PrecisionFloat)
    xform.AddTransformOp()  # a separate op name: xformOp:transform
    dry, _real = _set(stage, "/World/Spin", "xformOp:orient", [2, 0, 0, 0])
    assert dry["type_name"] == "quatf" and dry["after"] == [1.0, 0.0, 0.0, 0.0]
    assert any("normalized" in note for note in dry["notes"])
    assert stage.GetPrimAtPath("/World/Spin").GetAttribute("xformOp:orient").Get() == Gf.Quatf(1, 0, 0, 0)
    half = math.sqrt(0.5)
    _set(stage, "/World/Spin", "xformOp:orient", {"real": half, "imaginary": [half, 0, 0]})
    quat = stage.GetPrimAtPath("/World/Spin").GetAttribute("xformOp:orient").Get()
    assert _close([quat.GetReal(), *quat.GetImaginary()], [half, half, 0, 0])
    _set(stage, "/World/Spin", "xformOp:orient", Gf.Quatf(0, 0, 0, 1))
    _refused(edits.set_attribute, stage, "/World/Spin", "xformOp:orient", [0, 0, 0, 0], fragment="zero quaternion")
    _refused(edits.set_attribute, stage, "/World/Spin", "xformOp:orient", [1, 0, 0], fragment="needs 4 numbers")
    _set(stage, "/World/Cube", "spinq", [1, 0, 0, 0], create_type="quath")
    _set(stage, "/World/Cube", "spind", [1, 0, 0, 0], create_type="quatd")

    rows = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [5, 6, 7, 1]]
    dry, _real = _set(stage, "/World/Spin", "xformOp:transform", rows)
    assert dry["type_name"] == "matrix4d" and dry["after"] == [[float(v) for v in row] for row in rows]
    matrix = stage.GetPrimAtPath("/World/Spin").GetAttribute("xformOp:transform").Get()
    assert matrix.ExtractTranslation() == Gf.Vec3d(5, 6, 7)
    flat = _ok(_dry(edits.set_attribute, stage, "/World/Spin", "xformOp:transform", [v for row in rows for v in row]))
    assert flat["after"] == dry["after"] and flat["would_change"] is False
    _ok(_dry(edits.set_attribute, stage, "/World/Spin", "xformOp:transform", Gf.Matrix4d(2.0)))
    _refused(edits.set_attribute, stage, "/World/Spin", "xformOp:transform", [1, 2, 3], fragment="4x4 numbers")
    singular = _ok(_dry(edits.set_attribute, stage, "/World/Spin", "xformOp:transform", [0] * 16))
    assert any("singular" in warning for warning in singular["warnings"])
    _set(stage, "/World/Cube", "m2", [1, 2, 3, 4], create_type="matrix2d")
    assert stage.GetPrimAtPath("/World/Cube").GetAttribute("m2").Get() == Gf.Matrix2d(1, 2, 3, 4)
    _set(stage, "/World/Cube", "m3", [[1, 0, 0], [0, 1, 0], [0, 0, 1]], create_type="matrix3d")
    _set(stage, "/World/Cube", "frame", rows, create_type="frame4d")


def test_arrays_of_every_kind():
    stage = _stage()
    prim = stage.GetPrimAtPath("/World/Cube")
    dry, _real = _set(stage, "/World/Cube", "extent", [[-1, -1, -1], [1, 1, 1]])
    assert dry["type_name"] == "float3[]"
    assert list(prim.GetAttribute("extent").Get()) == [Gf.Vec3f(-1, -1, -1), Gf.Vec3f(1, 1, 1)]
    cases = (
        ("floats", "float[]", [1, 2.5], [1.0, 2.5]),
        ("floats_text", "float[]", "[1, 2, 3]", [1.0, 2.0, 3.0]),
        ("doubles", "double[]", [0.1, 0.2], [0.1, 0.2]),
        ("halves", "half[]", [0.5], [0.5]),
        ("ints", "int[]", [1, 2.0, "3"], [1, 2, 3]),
        ("bytes", "uchar[]", [0, 255], [0, 255]),
        ("flags", "bool[]", [True, "no"], [True, False]),
        ("names", "token[]", ["a", "b"], ["a", "b"]),
        ("labels", "string[]", ["x", "y z"], ["x", "y z"]),
        ("files", "asset[]", ["a.usd", "b.usd"], ["a.usd", "b.usd"]),
        ("points", "point3f[]", "[(0, 0, 0), (1, 2, 3)]", [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]),
        ("normals", "normal3f[]", [[0, 0, 1]], [[0.0, 0.0, 1.0]]),
        ("uvs", "texCoord2f[]", [[0, 1]], [[0.0, 1.0]]),
        ("rotations", "quatf[]", [[1, 0, 0, 0]], [[1.0, 0.0, 0.0, 0.0]]),
        ("cells", "int3[]", [[1, 2, 3]], [[1, 2, 3]]),
        ("matrices", "matrix4d[]", [[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]], [[[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]]),
        ("empty", "float[]", [], []),
    )
    for name, type_text, value, expected in cases:
        dry, real = _set(stage, "/World/Cube", name, value, create_type=type_text)
        assert dry["type_name"] == type_text, (name, dry["type_name"])
        assert dry["after"] == expected, (name, dry["after"])
        assert real["readback"]["shows_new_value"] is True, name
        assert str(prim.GetAttribute(name).GetTypeName()) == type_text
    assert [item.path for item in prim.GetAttribute("files").Get()] == ["a.usd", "b.usd"]
    assert list(prim.GetAttribute("flags").Get()) == [True, False]
    _refused(edits.set_attribute, stage, "/World/Cube", "floats", 5, fragment="list of values")
    _refused(edits.set_attribute, stage, "/World/Cube", "names", ["a", 1], fragment="must be text")
    _refused(edits.set_attribute, stage, "/World/Cube", "bytes", [0, 256], fragment="between 0 and 255")
    old_cap = edits.MAX_ARRAY_INPUT
    edits.MAX_ARRAY_INPUT = 3
    try:
        _refused(edits.set_attribute, stage, "/World/Cube", "floats", [1, 2, 3, 4], fragment="at most 3")
    finally:
        edits.MAX_ARRAY_INPUT = old_cap
    long_points = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "floats", list(range(100))))
    assert long_points["after"]["truncated"] is True and long_points["after"]["length"] == 100


# --- time samples and opinion strength -----------------------------------------------------------------


def test_time_samples_author_one_frame_and_keep_the_default():
    stage = _stage()
    attr = stage.GetPrimAtPath("/World/Cube").GetAttribute("size")
    dry, real = _set(stage, "/World/Cube", "size", 4, time=5)
    assert dry["time"] == 5.0 and dry["before"]["value"] == 2.0 and dry["before"]["time_sample_count"] == 0
    assert any("no time samples" in note for note in dry["notes"])
    assert real["readback"]["value"] == 4.0
    layer = stage.GetRootLayer()
    assert layer.QueryTimeSample(attr.GetPath(), 5.0) == 4.0 and attr.GetNumTimeSamples() == 1
    assert attr.Get(Usd.TimeCode.Default()) == 2.0  # the default is untouched (fallback)
    _set(stage, "/World/Cube", "size", 8, time="10")
    assert attr.GetTimeSamples() == [5.0, 10.0] and attr.Get(7.5) == 6.0
    same = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 8, time=10))
    assert same["would_change"] is False and same["before"]["edit_target_value"] == 8.0
    assert same["before"]["source"] == "time_samples" and same["before"]["winning_opinion"] == "time_samples"
    default_edit = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 3))
    assert any("time samples win over the default" in warning for warning in default_edit["warnings"])
    assert default_edit["effective_after"]["visible"] is True
    assert default_edit["effective_after"]["visible_on_timeline"] is False
    _refused(edits.set_attribute, stage, "/World/Cube", "size", 1, time="soon", fragment="time must be a number")
    _refused(edits.set_attribute, stage, "/World/Cube", "size", 1, time=float("inf"), fragment="finite")
    _refused(edits.set_attribute, stage, "/World/Cube", "size", 1, time=True, fragment="time must be a number")


def test_stronger_layer_default_hides_the_edit_and_is_named():
    with tempfile.TemporaryDirectory() as tmp:
        stage, root, weak = _file_stage(tmp)
        root_cube = Sdf.CreatePrimInLayer(root, "/World/Cube")
        root_cube.specifier = Sdf.SpecifierOver
        size = Sdf.AttributeSpec(root_cube, "size", Sdf.ValueTypeNames.Double)
        size.default = 5.0
        dry = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 7))
        assert dry["edit_target_layer"] == weak.identifier
        assert not any("anonymous" in warning for warning in dry["warnings"])
        assert dry["before"]["winning_layer"] == root.identifier and dry["before"]["value"] == 5.0
        effective = dry["effective_after"]
        assert effective["visible"] is False and effective["hidden_by"]["layer"] == root.identifier
        assert effective["hidden_by"]["opinion"] == "default" and effective["predicted_value"] == 5.0
        assert any(root.identifier in warning and "hidden" in warning for warning in dry["warnings"])
        real = _apply(edits.set_attribute, stage, "/World/Cube", "size", 7)
        assert real["readback"] == {"value": 5.0, "shows_new_value": False}
        assert weak.GetAttributeAtPath("/World/Cube.size").default == 7.0
        assert not any("could not be predicted" in warning for warning in real["warnings"])

        # Stronger time samples only hide the new default at timeline times.
        size.ClearDefaultValue()
        root.SetTimeSample(size.path, 1.0, 9.0)
        timeline = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 6))
        assert timeline["effective_after"]["visible"] is True
        assert timeline["effective_after"]["visible_on_timeline"] is False
        assert timeline["effective_after"]["timeline_hidden_by"]["layer"] == root.identifier
        assert any("stronger time samples" in warning for warning in timeline["warnings"])
        sample = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 6, time=1))
        assert sample["effective_after"]["visible"] is False
        assert sample["effective_after"]["hidden_by"]["opinion"] == "time_samples"

        # Editing the stronger layer itself has nothing above it.
        stage.SetEditTarget(Usd.EditTarget(root))
        top = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 6, time=1))
        assert top["effective_after"]["visible"] is True and top["edit_target_layer"] == root.identifier


def test_session_layer_opinions_and_session_edit_target_are_reported():
    stage = _stage()
    session = stage.GetSessionLayer()
    stage.SetEditTarget(Usd.EditTarget(session))
    stage.GetPrimAtPath("/World/Cube").GetAttribute("size").Set(9.0)
    stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
    hidden = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 3))
    assert hidden["effective_after"]["hidden_by"]["layer"] == session.identifier
    assert any(session.identifier in warning for warning in hidden["warnings"])

    stage.SetEditTarget(Usd.EditTarget(session))
    on_session = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 3))
    assert on_session["edit_target"]["session_layer"] is True
    assert any("session layer" in warning for warning in on_session["warnings"])
    assert on_session["effective_after"]["visible"] is True


def test_variant_edit_target_is_supported_and_flagged_when_unpredictable():
    stage = _stage()
    prim = stage.GetPrimAtPath("/World/Cube")
    variants = prim.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("big")
    variants.SetVariantSelection("big")
    with variants.GetVariantEditContext():
        first = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 6))
        assert first["effective_after"]["visible"] is None and first["effective_after"]["predicted"] is False
        assert any("could not be predicted" in warning for warning in first["warnings"])
        real = _apply(edits.set_attribute, stage, "/World/Cube", "size", 6)
        assert real["readback"] == {"value": 6.0, "shows_new_value": True}
        second = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 7))
        assert second["effective_after"]["visible"] is True and second["before"]["edit_target_value"] == 6.0
    spec = stage.GetRootLayer().GetAttributeAtPath("/World/Cube{look=big}.size")
    assert spec is not None and spec.default == 6.0
    assert stage.GetRootLayer().GetAttributeAtPath("/World/Cube.size") is None


def test_missing_attributes_need_create_type_and_create_custom_attributes():
    stage = _stage()
    missing = _refused(edits.set_attribute, stage, "/World/Cube", "sise", 3, fragment="has no attribute sise")
    assert "size" in missing["error"] and "create_type" in missing["error"]
    dry, real = _set(stage, "/World/Cube", "myWeight", 2, create_type="float")
    assert dry["creates_attribute"] is True and dry["before"]["exists"] is False and dry["after"] == 2.0
    assert any("custom attribute" in note for note in dry["notes"])
    attr = stage.GetPrimAtPath("/World/Cube").GetAttribute("myWeight")
    assert attr.IsCustom() and str(attr.GetTypeName()) == "float" and attr.Get() == 2.0
    assert real["changed"] is True
    _refused(edits.set_attribute, stage, "/World/Cube", "size", 3, create_type="float", fragment="already exists as double")
    same_type = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "size", 3, create_type="double"))
    assert any("not needed" in note for note in same_type["notes"])
    _refused(edits.set_attribute, stage, "/World/Cube", "x", 1, create_type="floaty", fragment="not an Sdf value type")
    _refused(edits.set_attribute, stage, "/World/Cube", "x", 1, create_type="opaque", fragment="cannot hold a value")
    spelled = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "x", [1, 0, 0], create_type="Color3f"))
    assert spelled["type_name"] == "color3f"
    arrays = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "x", [[1, 0, 0]], create_type="Float3Array"))
    assert arrays["type_name"] == "float3[]"
    _refused(edits.set_attribute, stage, "/World/Cube", "bad name", 1, create_type="float", fragment="not a valid USD property name")
    _refused(edits.set_attribute, stage, "/World/Cube", "", 1, fragment="attribute name")
    op = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "xformOp:scale", [2, 2, 2], create_type="float3"))
    assert any("xformOpOrder" in note for note in op["notes"])


def test_preview_surface_inputs_suggest_their_create_type():
    stage = _stage()
    shader = UsdShade.Shader.Define(stage, "/World/Looks/Mat/PreviewSurface")
    shader.CreateIdAttr("UsdPreviewSurface")
    refused = _refused(edits.set_attribute, stage, "/World/Looks/Mat/PreviewSurface", "inputs:roughness", 0.2, fragment="not authored yet")
    assert "create_type='float'" in refused["error"]
    bare = _refused(edits.set_attribute, stage, "/World/Looks/Mat/PreviewSurface", "diffuseColor", [1, 0, 0], fragment="not authored yet")
    assert "create_type='color3f'" in bare["error"]
    dry, _real = _set(stage, "/World/Looks/Mat/PreviewSurface", "inputs:roughness", 0.2, create_type="float")
    assert shader.GetInput("roughness").Get() == dry["after"] or _close(shader.GetInput("roughness").Get(), 0.2)


def test_set_attribute_refusals_write_nothing():
    stage = _stage()
    _refused(edits.set_attribute, None, "/World/Cube", "size", 1, fragment="No USD stage")
    _refused(edits.set_attribute, stage, "/World/Nope", "size", 1, fragment="Prim does not exist")
    _refused(edits.set_attribute, stage, "/World/Cube.size", "size", 1, fragment="property path")
    _refused(edits.set_attribute, stage, "World/Cube", "size", 1, fragment="absolute prim path")
    _refused(edits.set_attribute, stage, "/World/Cube", "size", None, fragment="value is required")
    _refused(edits.set_attribute, stage, "/World/Cube", "proxyPrim", "/World", fragment="relationship")
    binding = stage.GetPrimAtPath("/World/Cube").CreateRelationship("material:binding")
    binding.SetTargets([Sdf.Path("/World/Looks")])
    rel = _refused(edits.set_attribute, stage, "/World/Cube", "material:binding", "/World/Looks", fragment="relationship")
    assert "bind_material" in rel["error"]

    material = UsdShade.Material.Define(stage, "/World/Looks/Wood")
    surface = UsdShade.Shader.Define(stage, "/World/Looks/Wood/PreviewSurface")
    surface.CreateIdAttr("UsdPreviewSurface")
    texture = UsdShade.Shader.Define(stage, "/World/Looks/Wood/Texture")
    texture.CreateIdAttr("UsdUVTexture")
    surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
        texture.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
    )
    material.CreateSurfaceOutput().ConnectToSource(surface.CreateOutput("surface", Sdf.ValueTypeNames.Token))
    connected = _refused(edits.set_attribute, stage, "/World/Looks/Wood/PreviewSurface", "inputs:diffuseColor", [1, 0, 0], fragment="connected to")
    assert "/World/Looks/Wood/Texture.outputs:rgb" in connected["error"]

    UsdGeom.Xform.Define(stage, "/Proto")
    UsdGeom.Cube.Define(stage, "/Proto/Box")
    instance = UsdGeom.Xform.Define(stage, "/World/Instance").GetPrim()
    instance.GetReferences().AddInternalReference("/Proto")
    instance.SetInstanceable(True)
    assert stage.GetPrimAtPath("/World/Instance/Box").IsInstanceProxy()
    _refused(edits.set_attribute, stage, "/World/Instance/Box", "size", 3, fragment="instance proxy")

    read_only = _stage()
    read_only.GetRootLayer().SetPermissionToEdit(False)
    _refused(edits.set_attribute, read_only, "/World/Cube", "size", 3, fragment="read-only")


def test_xform_op_not_in_order_is_warned():
    stage = _stage()
    prim = stage.GetPrimAtPath("/World/Cube")
    prim.CreateAttribute("xformOp:translate", Sdf.ValueTypeNames.Double3)
    result = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "xformOp:translate", [1, 2, 3]))
    assert any("not listed in xformOpOrder" in warning for warning in result["warnings"])
    order = _ok(_dry(edits.set_attribute, stage, "/World/Cube", "xformOpOrder", ["xformOp:translate", "xformOp:rotateXYZ"]))
    assert any("do not exist" in warning and "xformOp:rotateXYZ" in warning for warning in order["warnings"])


# --- create_material --------------------------------------------------------------------------------------


def test_create_material_plans_every_input_with_types_and_writes_nothing():
    stage = _stage()
    result = _ok(_dry(
        edits.create_material,
        stage,
        "/World/Looks/Red",
        diffuse_color=[255, 0, 0],
        roughness=0.3,
        metallic=0.8,
        opacity=0.9,
        emissive_color=[4, 2, 0],
        ior=1.45,
        use_specular_workflow=True,
    ))
    assert result["shader_path"] == "/World/Looks/Red/PreviewSurface"
    plan = result["would_create"]
    assert plan["shader"] == {"path": "/World/Looks/Red/PreviewSurface", "type": "Shader", "id": "UsdPreviewSurface"}
    inputs = plan["inputs"]
    assert inputs["diffuseColor"] == {"attribute": "inputs:diffuseColor", "type": "color3f", "value": [1.0, 0.0, 0.0]}
    assert inputs["emissiveColor"]["value"] == [4.0, 2.0, 0.0]  # emission may be HDR, so no 0-255 guess
    assert inputs["roughness"]["type"] == "float" and inputs["roughness"]["value"] == 0.3
    assert inputs["metallic"]["value"] == 0.8 and inputs["opacity"]["value"] == 0.9 and inputs["ior"]["value"] == 1.45
    assert inputs["useSpecularWorkflow"] == {"attribute": "inputs:useSpecularWorkflow", "type": "int", "value": 1}
    assert plan["connections"] == {"/World/Looks/Red.outputs:surface": "/World/Looks/Red/PreviewSurface.outputs:surface"}
    assert "not connected" in plan["displacement"] and plan["defaults_kept"] == {}
    assert any("metallic is ignored" in warning for warning in result["warnings"])
    assert any("0-255" in note for note in result["notes"]) and any("HDR emission" in note for note in result["notes"])
    assert any("translucent" in note for note in result["notes"])
    assert not stage.GetPrimAtPath("/World/Looks/Red")


def test_create_material_authors_a_wired_preview_surface():
    stage = _stage()
    result = _apply(edits.create_material, stage, "/World/Looks/Gold", diffuse_color="#FFD700", roughness=0.25, metallic=1)
    material = UsdShade.Material(stage.GetPrimAtPath("/World/Looks/Gold"))
    shader = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/Gold/PreviewSurface"))
    assert material and shader and material.GetPrim().GetTypeName() == "Material"
    assert shader.GetIdAttr().Get() == "UsdPreviewSurface"
    source, output_name, _kind = material.ComputeSurfaceSource()
    assert source.GetPath() == shader.GetPath() and output_name == "surface"
    assert str(shader.GetInput("diffuseColor").GetTypeName()) == "color3f"
    assert _close(list(shader.GetInput("diffuseColor").Get()), [1.0, 215 / 255.0, 0.0])
    assert str(shader.GetInput("roughness").GetTypeName()) == "float" and _close(shader.GetInput("roughness").Get(), 0.25)
    assert _close(shader.GetInput("metallic").Get(), 1.0)
    for unset in ("opacity", "ior", "emissiveColor", "useSpecularWorkflow"):
        assert not shader.GetInput(unset), unset
    assert not material.GetDisplacementOutput().HasConnectedSource()  # schema-defined, but never wired
    assert stage.GetRootLayer().GetAttributeAtPath("/World/Looks/Gold.outputs:displacement") is None
    created = result["created"]
    assert created["surface_source"] == "/World/Looks/Gold/PreviewSurface.outputs:surface"
    assert created["shader_id"] == "UsdPreviewSurface" and set(created["inputs"]) == {"diffuseColor", "roughness", "metallic"}
    assert created["inputs"]["roughness"] == 0.25
    assert "roughness" not in result["would_create"]["defaults_kept"] and result["would_create"]["defaults_kept"]["ior"] == 1.5


def test_create_material_each_input_alone_has_the_spec_type():
    expected_types = {
        "diffuse_color": ("diffuseColor", "color3f", [0.2, 0.4, 0.6]),
        "emissive_color": ("emissiveColor", "color3f", [1.0, 0.5, 0.0]),
        "roughness": ("roughness", "float", 0.7),
        "metallic": ("metallic", "float", 0.0),
        "opacity": ("opacity", "float", 0.5),
        "ior": ("ior", "float", 1.33),
        "use_specular_workflow": ("useSpecularWorkflow", "int", 0),
    }
    for keyword, (input_name, type_text, value) in expected_types.items():
        stage = _stage()
        given = False if keyword == "use_specular_workflow" else value
        _ok(_dry(edits.create_material, stage, "/World/Looks/M", **{keyword: given}))
        _apply(edits.create_material, stage, "/World/Looks/M", **{keyword: given})
        shader = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/M/PreviewSurface"))
        authored = [item.GetBaseName() for item in shader.GetInputs()]
        assert authored == [input_name], (keyword, authored)
        assert str(shader.GetInput(input_name).GetTypeName()) == type_text
        stored = shader.GetInput(input_name).Get()
        assert _close(list(stored) if type_text == "color3f" else stored, value), (keyword, stored)


def test_create_material_without_inputs_keeps_spec_defaults():
    stage = _stage()
    result = _apply(edits.create_material, stage, "/World/Looks/Plain")
    assert result["would_create"]["inputs"] == {}
    assert result["would_create"]["defaults_kept"]["roughness"] == 0.5
    assert result["would_create"]["defaults_kept"]["diffuseColor"] == [0.18, 0.18, 0.18]
    shader = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/Plain/PreviewSurface"))
    assert shader.GetInputs() == [] and shader.GetIdAttr().Get() == "UsdPreviewSurface"
    assert result["created"]["surface_source"].endswith("PreviewSurface.outputs:surface")


def test_create_material_refusals_and_ranges():
    stage = _stage()
    _refused(edits.create_material, stage, "/World/Looks/M", roughness=1.5, fragment="roughness must be between 0 and 1")
    _refused(edits.create_material, stage, "/World/Looks/M", metallic=-0.1, fragment="metallic must be between 0 and 1")
    _refused(edits.create_material, stage, "/World/Looks/M", opacity="x", fragment="opacity must be a number")
    _refused(edits.create_material, stage, "/World/Looks/M", ior=0, fragment="ior must be greater than 0")
    _refused(edits.create_material, stage, "/World/Looks/M", diffuse_color=[2.5, 0, 0], fragment="must be 0-1")
    _refused(edits.create_material, stage, "/World/Looks/M", diffuse_color=[1, 0], fragment="needs 3 numbers")
    _refused(edits.create_material, stage, "/World/Looks/M", emissive_color=[-1, 0, 0], fragment="negative")
    _refused(edits.create_material, stage, "/World/Looks/M", use_specular_workflow="maybe", fragment="true or false")
    _refused(edits.create_material, stage, "/World/Cube", fragment="already exists")
    _refused(edits.create_material, stage, "/World/Missing/M", fragment="Parent prim does not exist")
    _refused(edits.create_material, stage, "not a path", fragment="not a valid USD path")
    _refused(edits.create_material, None, "/World/Looks/M", fragment="No USD stage")
    low_ior = _ok(_dry(edits.create_material, stage, "/World/Looks/M", ior=0.8))
    assert any("below 1" in warning for warning in low_ior["warnings"])


# --- bind_material ----------------------------------------------------------------------------------------


def _binding_stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/W")
    UsdGeom.Scope.Define(stage, "/W/Looks")
    for name in "ABCD":
        edits.create_material(stage, f"/W/Looks/{name}", dry_run=False)
    UsdGeom.Xform.Define(stage, "/W/G")
    UsdGeom.Cube.Define(stage, "/W/G/Cube")
    return stage


def _material(stage, name):
    return UsdShade.Material(stage.GetPrimAtPath(f"/W/Looks/{name}"))


def _api(stage, path):
    return UsdShade.MaterialBindingAPI.Apply(stage.GetPrimAtPath(path))


def _collection(stage, owner, name, *targets):
    collection = Usd.CollectionAPI.Apply(stage.GetPrimAtPath(owner), name)
    for target in targets:
        collection.CreateIncludesRel().AddTarget(Sdf.Path(target))
    return collection


def test_bind_material_dry_run_then_apply():
    stage = _binding_stage()
    dry = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A"))
    assert dry["before"]["bound_material"] is None and dry["before"]["direct_binding"] is None
    assert dry["applies_api"] is True and dry["would_change"] is True
    assert dry["relationship"] == "material:binding" and dry["binding_relationship"] == "/W/G/Cube.material:binding"
    assert dry["purpose"] == "allPurpose" and dry["strength"] == "weakerThanDescendants" and dry["strength_authored"] is False
    assert dry["predicted"]["material"] == "/W/Looks/A" and dry["predicted"]["binding_wins"] is True
    assert dry["predicted"]["reliable"] is True
    assert dry["effective_by_purpose"]["preview"] == {"before": None, "after_predicted": "/W/Looks/A"}
    assert not stage.GetPrimAtPath("/W/G/Cube").HasAPI(UsdShade.MaterialBindingAPI)

    real = _apply(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A")
    prim = stage.GetPrimAtPath("/W/G/Cube")
    assert prim.HasAPI(UsdShade.MaterialBindingAPI)
    assert UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0].GetPath() == Sdf.Path("/W/Looks/A")
    assert real["after"]["bound_material"] == "/W/Looks/A" and real["after"]["via"] == "/W/G/Cube.material:binding"
    assert real["after"]["by_purpose"] == {"allPurpose": "/W/Looks/A", "preview": "/W/Looks/A", "full": "/W/Looks/A"}
    assert real["changed"] is True
    assert not prim.GetRelationship("material:binding").HasMetadata("bindMaterialAs")
    again = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A"))
    assert again["would_change"] is False and again["applies_api"] is False


def test_bind_material_reports_the_previous_binding():
    stage = _binding_stage()
    _api(stage, "/W/G").Bind(_material(stage, "B"))
    inherited = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A"))
    assert inherited["before"]["bound_material"] == "/W/Looks/B"
    assert inherited["before"]["via"] == "/W/G.material:binding" and inherited["before"]["direct_binding"] is None
    _apply(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A")
    replaced = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/C"))
    assert replaced["before"]["bound_material"] == "/W/Looks/A"
    assert replaced["before"]["direct_binding"] == {"material": "/W/Looks/A", "strength": "weakerThanDescendants"}
    assert replaced["predicted"]["material"] == "/W/Looks/C"


def test_bind_material_strength_and_purpose_are_authored():
    stage = _binding_stage()
    strong = _apply(edits.bind_material, stage, "/W/G", "/W/Looks/B", strength="stronger")
    assert strong["strength"] == "strongerThanDescendants" and strong["strength_authored"] is True
    rel = stage.GetPrimAtPath("/W/G").GetRelationship("material:binding")
    assert rel.GetMetadata("bindMaterialAs") == "strongerThanDescendants"

    preview = _apply(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A", purpose="preview")
    assert preview["relationship"] == "material:binding:preview"
    cube = stage.GetPrimAtPath("/W/G/Cube")
    assert cube.GetRelationship("material:binding:preview").GetTargets() == [Sdf.Path("/W/Looks/A")]
    api = UsdShade.MaterialBindingAPI(cube)
    assert api.ComputeBoundMaterial(T.preview)[0].GetPath() == Sdf.Path("/W/Looks/A")
    assert api.ComputeBoundMaterial(T.full)[0].GetPath() == Sdf.Path("/W/Looks/B")
    assert preview["after"]["by_purpose"] == {"allPurpose": "/W/Looks/B", "preview": "/W/Looks/A", "full": "/W/Looks/B"}
    assert preview["effective_by_purpose"]["full"]["after_predicted"] == "/W/Looks/B"

    reset = _ok(_dry(edits.bind_material, stage, "/W/G", "/W/Looks/B"))
    assert reset["would_change"] is True  # strength None writes weakerThanDescendants back
    _apply(edits.bind_material, stage, "/W/G", "/W/Looks/B")
    assert UsdShade.MaterialBindingAPI.GetMaterialBindingStrength(rel) == T.weakerThanDescendants
    _refused(edits.bind_material, stage, "/W/G", "/W/Looks/B", strength="medium", fragment="strength must be")
    _refused(edits.bind_material, stage, "/W/G", "/W/Looks/B", purpose="render", fragment="purpose must be")


def test_bind_material_predicts_a_stronger_ancestor_and_same_prim_collection():
    stage = _binding_stage()
    _api(stage, "/W/G").Bind(_material(stage, "B"), bindingStrength=T.strongerThanDescendants)
    dry = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A"))
    predicted = dry["predicted"]
    assert predicted["material"] == "/W/Looks/B" and predicted["binding_wins"] is False
    assert predicted["hidden_by"]["prim"] == "/W/G" and "strongerThanDescendants" in predicted["hidden_by"]["reason"]
    assert any("would not take effect" in warning for warning in dry["warnings"])
    real = _apply(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A")
    assert real["after"]["bound_material"] == "/W/Looks/B"
    assert not any("not the predicted" in warning for warning in real["warnings"])

    stage = _binding_stage()
    own = _collection(stage, "/W/G/Cube", "own", "/W/G/Cube")
    _api(stage, "/W/G/Cube").Bind(own, _material(stage, "C"), "own")
    same_prim = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A", strength="strongerThanDescendants"))
    assert same_prim["predicted"]["material"] == "/W/Looks/C"
    assert same_prim["predicted"]["hidden_by"]["kind"] == "collection"
    assert "same prim" in same_prim["predicted"]["hidden_by"]["reason"]


def _scenario_empty(stage):
    pass


def _scenario_strong_parent(stage):
    _api(stage, "/W/G").Bind(_material(stage, "B"), bindingStrength=T.strongerThanDescendants)


def _scenario_weak_parent(stage):
    _api(stage, "/W/G").Bind(_material(stage, "B"))


def _scenario_own_collection(stage):
    _api(stage, "/W/G/Cube").Bind(_collection(stage, "/W/G/Cube", "own", "/W/G/Cube"), _material(stage, "C"), "own")


def _scenario_strong_parent_collection(stage):
    _api(stage, "/W/G").Bind(_collection(stage, "/W/G", "kids", "/W/G/Cube"), _material(stage, "D"), "kids",
                             bindingStrength=T.strongerThanDescendants)


def _scenario_parent_strong_direct_and_weak_collection(stage):
    api = _api(stage, "/W/G")
    api.Bind(_material(stage, "B"), bindingStrength=T.strongerThanDescendants)
    api.Bind(_collection(stage, "/W/G", "kids", "/W/G/Cube"), _material(stage, "D"), "kids")


def _scenario_two_strong_ancestors(stage):
    _api(stage, "/W/G").Bind(_material(stage, "B"), bindingStrength=T.strongerThanDescendants)
    _api(stage, "/W").Bind(_material(stage, "C"), bindingStrength=T.strongerThanDescendants)


def _scenario_preview_on_cube(stage):
    _api(stage, "/W/G/Cube").Bind(_material(stage, "C"), materialPurpose=T.preview)


def _scenario_preview_on_parent(stage):
    _api(stage, "/W/G").Bind(_material(stage, "B"), materialPurpose=T.preview)


def _scenario_strong_own_direct(stage):
    _api(stage, "/W/G/Cube").Bind(_material(stage, "D"), bindingStrength=T.strongerThanDescendants)


def _scenario_parent_collection_not_matching(stage):
    _api(stage, "/W/G").Bind(_collection(stage, "/W/G", "other", "/W/Looks"), _material(stage, "D"), "other",
                             bindingStrength=T.strongerThanDescendants)


def test_bind_material_prediction_matches_compute_bound_material():
    scenarios = (
        (_scenario_empty, "/W/G/Cube", "A", {}, "/W/Looks/A"),
        (_scenario_strong_parent, "/W/G/Cube", "A", {}, "/W/Looks/B"),
        (_scenario_weak_parent, "/W/G/Cube", "A", {}, "/W/Looks/A"),
        (_scenario_own_collection, "/W/G/Cube", "A", {}, "/W/Looks/C"),
        (_scenario_own_collection, "/W/G/Cube", "A", {"strength": "strongerThanDescendants"}, "/W/Looks/C"),
        (_scenario_strong_parent_collection, "/W/G/Cube", "A", {}, "/W/Looks/D"),
        (_scenario_parent_strong_direct_and_weak_collection, "/W/G/Cube", "A", {}, "/W/Looks/D"),
        (_scenario_two_strong_ancestors, "/W/G/Cube", "A", {}, "/W/Looks/C"),
        (_scenario_two_strong_ancestors, "/W/G", "A", {"strength": "strongerThanDescendants"}, "/W/Looks/C"),
        (_scenario_preview_on_cube, "/W/G/Cube", "A", {}, "/W/Looks/A"),
        (_scenario_preview_on_cube, "/W/G/Cube", "A", {"purpose": "preview"}, "/W/Looks/A"),
        (_scenario_preview_on_parent, "/W/G/Cube", "A", {}, "/W/Looks/A"),
        (_scenario_preview_on_parent, "/W/G/Cube", "A", {"purpose": "full"}, "/W/Looks/A"),
        (_scenario_strong_own_direct, "/W/G/Cube", "A", {}, "/W/Looks/A"),
        (_scenario_strong_parent, "/W/G", "C", {}, "/W/Looks/C"),
        (_scenario_parent_collection_not_matching, "/W/G/Cube", "A", {}, "/W/Looks/A"),
    )
    for setup, target, material, options, expected in scenarios:
        stage = _binding_stage()
        setup(stage)
        label = (setup.__name__, target, options)
        dry = _ok(_dry(edits.bind_material, stage, target, f"/W/Looks/{material}", **options))
        assert dry["predicted"]["reliable"] is True, label
        assert dry["predicted"]["material"] == expected, (label, dry["predicted"])
        assert dry["predicted"]["binding_wins"] is (expected == f"/W/Looks/{material}"), label
        real = _apply(edits.bind_material, stage, target, f"/W/Looks/{material}", **options)
        assert real["after"]["bound_material"] == expected, (label, real["after"])
        for purpose_label, entry in dry["effective_by_purpose"].items():
            assert real["after"]["by_purpose"][purpose_label] == entry["after_predicted"], (label, purpose_label)
        assert not any("not the predicted" in warning for warning in real["warnings"]), label


def test_bind_material_warns_when_a_purpose_specific_binding_keeps_another_material():
    stage = _binding_stage()
    _scenario_preview_on_parent(stage)
    dry = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A"))
    assert dry["predicted"]["material"] == "/W/Looks/A"
    assert dry["effective_by_purpose"]["preview"]["after_predicted"] == "/W/Looks/B"
    assert any("preview purpose" in warning and "/W/Looks/B" in warning for warning in dry["warnings"])
    assert not any("full purpose" in warning for warning in dry["warnings"])


def test_bind_material_notes_descendant_bindings():
    stage = _binding_stage()
    _api(stage, "/W/G/Cube").Bind(_material(stage, "D"))
    weak = _ok(_dry(edits.bind_material, stage, "/W", "/W/Looks/A"))
    assert weak["descendant_bindings"] == {"count": 1, "examples": ["/W/G/Cube"], "truncated": False}
    assert any("keep their own material" in note for note in weak["notes"])
    strong = _ok(_dry(edits.bind_material, stage, "/W", "/W/Looks/A", strength="strongerThanDescendants"))
    assert any("overrides the 1 descendant" in note for note in strong["notes"])


def test_bind_material_stronger_layer_relationship_hides_the_binding():
    with tempfile.TemporaryDirectory() as tmp:
        stage, root, weak = _file_stage(tmp)
        stage.SetEditTarget(Usd.EditTarget(root))
        UsdGeom.Scope.Define(stage, "/World/Looks")
        UsdGeom.Sphere.Define(stage, "/World/Ball")
        for name in ("A", "B"):
            edits.create_material(stage, f"/World/Looks/{name}", dry_run=False)
        UsdShade.MaterialBindingAPI.Apply(stage.GetPrimAtPath("/World/Cube")).Bind(
            UsdShade.Material(stage.GetPrimAtPath("/World/Looks/B"))
        )
        ball_rel = stage.GetPrimAtPath("/World/Ball").CreateRelationship("material:binding")
        ball_rel.SetMetadata("bindMaterialAs", "weakerThanDescendants")  # strength only, no targets
        stage.SetEditTarget(Usd.EditTarget(weak))

        dry = _ok(_dry(edits.bind_material, stage, "/World/Cube", "/World/Looks/A"))
        assert any(root.identifier in warning and "stronger opinion for material:binding" in warning for warning in dry["warnings"])
        assert dry["predicted"]["material"] == "/World/Looks/B" and dry["predicted"]["binding_wins"] is False
        assert dry["predicted"]["hidden_by"]["layer"] == root.identifier
        assert dry["effective_by_purpose"]["full"] == {"before": "/World/Looks/B", "after_predicted": "/World/Looks/B"}
        real = _apply(edits.bind_material, stage, "/World/Cube", "/World/Looks/A")
        assert real["after"]["bound_material"] == "/World/Looks/B"
        assert not any("not the predicted" in warning for warning in real["warnings"])
        assert weak.GetRelationshipAtPath("/World/Cube.material:binding") is not None

        strength = _ok(_dry(edits.bind_material, stage, "/World/Ball", "/World/Looks/A", strength="stronger"))
        assert any("stronger bindMaterialAs" in warning for warning in strength["warnings"])
        assert strength["predicted"]["material"] == "/World/Looks/A" and strength["predicted"]["binding_wins"] is True


def _list_op_stage(op, ancestor_binding=False):
    """Root (stronger) holds a list op on /World/Cube.material:binding; the edit target is the weaker sublayer."""
    stage = Usd.Stage.CreateInMemory()
    sub = Sdf.Layer.CreateAnonymous("sub.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    UsdGeom.Xform.Define(stage, "/World")
    cube = UsdGeom.Cube.Define(stage, "/World/Cube").GetPrim()
    for name in "ABC":
        edits.create_material(stage, f"/World/Looks/{name}", create_parent=True, dry_run=False)
    if ancestor_binding:
        UsdShade.MaterialBindingAPI.Apply(stage.GetPrimAtPath("/World")).Bind(
            UsdShade.Material(stage.GetPrimAtPath("/World/Looks/C")))
    UsdShade.MaterialBindingAPI.Apply(cube)
    rel = Sdf.RelationshipSpec(stage.GetRootLayer().GetPrimAtPath("/World/Cube"), "material:binding", False)
    item = Sdf.Path("/World/Looks/C" if op == "delete_other" else "/World/Looks/A")
    setattr(rel.targetPathList, {"append": "appendedItems", "prepend": "prependedItems", "delete": "deletedItems",
                                 "delete_other": "deletedItems", "explicit": "explicitItems"}[op], [item])
    stage.SetEditTarget(stage.GetEditTargetForLocalLayer(sub))
    return stage


def test_bind_material_composes_stronger_list_ops_instead_of_assuming_they_hide_the_binding():
    for ancestor_binding in (False, True):
        for op in ("append", "prepend", "delete", "delete_other", "explicit"):
            label = (op, ancestor_binding)
            stage = _list_op_stage(op, ancestor_binding)
            dry = _ok(_dry(edits.bind_material, stage, "/World/Cube", "/World/Looks/B"))
            real = _apply(edits.bind_material, stage, "/World/Cube", "/World/Looks/B")
            actual = real["after"]["bound_material"]
            assert dry["predicted"]["material"] == actual, (label, dry["predicted"], actual)
            assert not any("not the predicted" in warning for warning in real["warnings"]), (label, real["warnings"])
            hidden = any("would be hidden" in warning for warning in dry["warnings"])
            assert hidden is (actual != "/World/Looks/B"), (label, dry["warnings"])
            assert dry["predicted"]["binding_wins"] is (actual == "/World/Looks/B"), label
            assert dry["effective_by_purpose"]["allPurpose"]["after_predicted"] == actual, label
    # 'delete A' leaves the new target alone: the binding takes effect. Two targets bind nothing (or inherit).
    assert _apply(edits.bind_material, _list_op_stage("delete"), "/World/Cube", "/World/Looks/B")["after"][
        "bound_material"] == "/World/Looks/B"
    assert _apply(edits.bind_material, _list_op_stage("append", True), "/World/Cube", "/World/Looks/B")["after"][
        "bound_material"] == "/World/Looks/C"


def test_bind_material_prediction_through_a_variant_or_time_offset_edit_target_is_honest():
    def base():
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/World")
        prim = UsdGeom.Cube.Define(stage, "/World/Cube").GetPrim()
        for name in "AB":
            edits.create_material(stage, f"/World/Looks/{name}", create_parent=True, dry_run=False)
        return stage, prim

    # A variant edit target: the stronger local binding to A cannot be ordered against it, so say so.
    stage, prim = base()
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material(stage.GetPrimAtPath("/World/Looks/A")))
    variants = prim.GetVariantSets().AddVariantSet("look")
    variants.AddVariant("v1")
    variants.SetVariantSelection("v1")
    stage.SetEditTarget(variants.GetVariantEditTarget())
    dry = _ok(_dry(edits.bind_material, stage, "/World/Cube", "/World/Looks/B"))
    assert dry["predicted"]["reliable"] is False
    assert any("could not be predicted" in warning for warning in dry["warnings"]), dry["warnings"]

    # A local sublayer with only a time offset: layer order still decides, so the root's binding to A wins.
    stage, prim = base()
    sub = Sdf.Layer.CreateAnonymous("sub.usda")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    stage.GetRootLayer().subLayerOffsets[0] = Sdf.LayerOffset(24)
    UsdShade.MaterialBindingAPI.Apply(prim).Bind(UsdShade.Material(stage.GetPrimAtPath("/World/Looks/A")))
    stage.SetEditTarget(stage.GetEditTargetForLocalLayer(sub))
    dry = _ok(_dry(edits.bind_material, stage, "/World/Cube", "/World/Looks/B"))
    assert dry["predicted"]["material"] == "/World/Looks/A" and dry["predicted"]["binding_wins"] is False
    real = _apply(edits.bind_material, stage, "/World/Cube", "/World/Looks/B")
    assert real["after"]["bound_material"] == "/World/Looks/A"
    assert not any("not the predicted" in warning for warning in real["warnings"]), real["warnings"]


def test_set_attribute_float_and_half_tuples_read_back_as_the_new_value():
    stage = _stage()
    camera = UsdGeom.Camera.Define(stage, "/World/Cam")
    _, real = _set(stage, "/World/Cam", "clippingRange", [0.1, 1000])
    assert real["readback"]["shows_new_value"] is True, real["readback"]
    assert not any("still resolves to" in warning for warning in real["warnings"]), real["warnings"]
    again = _ok(_dry(edits.set_attribute, stage, "/World/Cam", "clippingRange", [0.1, 1000]))
    assert again["would_change"] is False
    assert _close(list(camera.GetClippingRangeAttr().Get()), [0.1, 1000.0])
    for name, type_name, value in (
        ("extent", None, [[-0.1, -0.1, -0.1], [0.1, 0.1, 0.1]]),
        ("customFloat3", "float3", [0.1, 0.2, 0.3]),
        ("customHalf3", "half3", [0.1, 0.2, 0.3]),
        ("customFloat4", "float4", [0.1, 0.2, 0.3, 0.4]),
        ("customHalf2Array", "half2[]", [[0.1, 0.2], [0.3, 0.4]]),
    ):
        options = {"create_type": type_name} if type_name else {}
        _, real = _set(stage, "/World/Cube", name, value, **options)
        assert real["readback"]["shows_new_value"] is True, (name, real["readback"])
        assert _ok(_dry(edits.set_attribute, stage, "/World/Cube", name, value))["would_change"] is False, name
    assert edits._precision(Sdf.ValueTypeNames.Float3) == 7 and edits._precision(Sdf.ValueTypeNames.Half4) == 4
    assert edits._precision(Sdf.ValueTypeNames.Color3f) == 7 and edits._precision(Sdf.ValueTypeNames.TexCoord2h) == 4
    assert edits._precision(Sdf.ValueTypeNames.Double3) is None and edits._precision(Sdf.ValueTypeNames.Token) is None


def test_bind_material_refusals_write_nothing():
    stage = _binding_stage()
    _refused(edits.bind_material, None, "/W/G/Cube", "/W/Looks/A", fragment="No USD stage")
    _refused(edits.bind_material, stage, "/W/Nope", "/W/Looks/A", fragment="Prim does not exist")
    _refused(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/Nope", fragment="Material does not exist")
    _refused(edits.bind_material, stage, "/W/G/Cube", "/W/G", fragment="not a Material")
    _refused(edits.bind_material, stage, "/W/Looks/A", "/W/Looks/B", fragment="shading prims")
    _refused(edits.bind_material, stage, "/W/Looks/A/PreviewSurface", "/W/Looks/B", fragment="shading prims")
    _refused(edits.bind_material, stage, "/W/G/Cube", "relative/path", fragment="absolute prim path")
    UsdGeom.Xform.Define(stage, "/Proto")
    UsdGeom.Cube.Define(stage, "/Proto/Box")
    instance = UsdGeom.Xform.Define(stage, "/W/Instance").GetPrim()
    instance.GetReferences().AddInternalReference("/Proto")
    instance.SetInstanceable(True)
    _refused(edits.bind_material, stage, "/W/Instance/Box", "/W/Looks/A", fragment="instance proxy")
    allowed = _ok(_dry(edits.bind_material, stage, "/W/Instance", "/W/Looks/A"))
    assert allowed["predicted"]["material"] == "/W/Looks/A"


def test_bind_material_warns_about_a_material_without_a_surface():
    stage = _binding_stage()
    UsdShade.Material.Define(stage, "/W/Looks/Empty")
    dry = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/Empty"))
    assert any("no connected surface output" in warning for warning in dry["warnings"])
    clean = _ok(_dry(edits.bind_material, stage, "/W/G/Cube", "/W/Looks/A"))
    assert not any("surface output" in warning for warning in clean["warnings"])


# --- module contract ---------------------------------------------------------------------------------------


def test_edits_module_has_no_kit_imports():
    source = Path(edits.__file__).read_text(encoding="utf-8")
    lines = [line.strip() for line in source.splitlines()]
    assert not any(line.startswith(("import omni", "from omni", "import carb", "from carb")) for line in lines)


def test_create_material_can_create_a_missing_looks_scope_and_removes_nothing_else():
    """create_parent=True (what scene_create_material does by default) plans and authors a missing direct parent."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    _refused(edits.create_material, stage, "/World/Looks/Red", fragment="Parent prim does not exist")
    plan = _ok(_dry(edits.create_material, stage, "/World/Looks/Red", diffuse_color=(1, 0, 0), create_parent=True))
    assert plan["would_create"]["creates_parent"] == {"path": "/World/Looks", "type": "Scope"}
    assert plan["would_create"]["parent"] == "/World/Looks"
    assert any("missing parent /World/Looks" in note for note in plan["notes"])
    created = _apply(edits.create_material, stage, "/World/Looks/Red", diffuse_color=(1, 0, 0), create_parent=True)
    assert created["created"]["created_parent"] == "/World/Looks"
    assert stage.GetPrimAtPath("/World/Looks").GetTypeName() == "Scope"
    assert UsdShade.Material(stage.GetPrimAtPath("/World/Looks/Red"))
    # An existing parent is never re-created, and deeper gaps are still refused.
    again = _ok(_dry(edits.create_material, stage, "/World/Looks/Blue", create_parent=True))
    assert "creates_parent" not in again["would_create"]
    _refused(edits.create_material, stage, "/World/A/B/Mat", create_parent=True, fragment="Parent prim does not exist")
    # Only an explicit True creates the parent.
    _refused(edits.create_material, stage, "/World/Other/Mat", create_parent="yes", fragment="Parent prim does not exist")
