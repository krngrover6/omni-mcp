"""usda_check: .usda text is checked against the real USD schemas; app data and Kit schemas are not flagged."""
from __future__ import annotations

try:
    from pxr import Usd  # noqa: F401
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import tempfile
from pathlib import Path

from omniverse_scene_mcp import features
from omniverse_scene_mcp.usda_check import check_usda

BAD = '''#usda 1.0
(
    defaultPrim = "Wrld"
)
def Xform "World"
{
    def Mesh "MeshBox"
    {
        double size = 50
    }
    def Cube "Box"
    {
        float size = 50
        double sise = 2
    }
    def Cub "Typo"
    {
    }
    def Cylinder "Wheel"
    {
        token axis = "Z"
    }
    def Sphere "Ball" (
        prepend apiSchemas = ["PhysicsRigidBodyAPII"]
    )
    {
        rel material:binding = </World/Looks/M>
        custom double myData = 3
    }
}
'''


def test_schema_mistakes_are_found_with_lines_and_fixes():
    result = check_usda(text=BAD)
    assert result["ok"] and result["verdict"] == "problems"
    found = {(p["line"], p["code"]) for p in result["problems"]}
    assert found == {(9, "not_in_schema"), (13, "wrong_type"), (14, "not_in_schema"), (16, "unknown_type"),
                     (21, "wrong_variability"), (24, "unknown_api"), (27, "binding_without_api"),
                     (None, "bad_default_prim")}
    by_line = {p["line"]: p for p in result["problems"]}
    assert by_line[14]["did_you_mean"] == ["size"] and by_line[16]["did_you_mean"][0] == "Cube"
    assert by_line[24]["did_you_mean"] == ["PhysicsRigidBodyAPI"]
    assert any("upAxis" in n for n in result["notes"])


def test_clean_text_app_data_and_kit_schemas_pass():
    text = '''#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Z"
)
def Xform "World"
{
    def Cube "Box" (
        prepend apiSchemas = ["MaterialBindingAPI", "NewtonSceneAPI"]
    )
    {
        double size = 2
        rel material:binding = </World/M>
        double3 xformOp:translate = (0, 0, 1)
        uniform token[] xformOpOrder = ["xformOp:translate"]
        float3[] primvars:displayColor = [(1, 0, 0)]
    }
    def Xform "Environment"
    {
        double ground:size = 1400
    }
}
'''
    result = check_usda(text=text)
    assert result["verdict"] == "ok", result["problems"]
    assert result["stage_metadata"]["upAxis"] == "Z" and any("NewtonSceneAPI" in n for n in result["notes"])


def test_parse_error_line_and_file_mode():
    result = check_usda(text='def Xform "A" {\n  double3 xformOp:translate = (0,0\n}\n')
    assert result["verdict"] == "parse_error" and result["problems"][0]["line"] == 3
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "s.usda"
        path.write_text(BAD)
        assert check_usda(path=str(path))["verdict"] == "problems"
        assert not check_usda(path=str(Path(tmp) / "notes.txt"))["ok"]
    assert not check_usda()["ok"] and not check_usda(text="x", path="y")["ok"]
    assert features.FEATURES["usda_check"].mutates is False


def test_line_numbers_match_the_text_given_without_a_header():
    # Seen live 2026-11-26: pasted text had no #usda line and every line came back one too high.
    result = check_usda(text='def Mesh "Box"\n{\n    double size = 50\n}\n')
    assert [p["line"] for p in result["problems"]] == [3]
