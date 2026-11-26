"""usda_explain: plain-word reading of .usda text with real schema facts; dictionaries never break prim paths."""
from __future__ import annotations

try:
    from pxr import Usd  # noqa: F401
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features
from omniverse_scene_mcp.usda_explain import explain_usda

TEXT = '''#usda 1.0
(
    customLayerData = {
        dictionary cameraSettings = {
            double radius = 500
        }
    }
    defaultPrim = "World"
    upAxis = "Y"
)

def Xform "World"
{
    def Sphere "Ball" (
        customData = {
            int note = 1
        }
    )
    {
        double radius.timeSamples = {
            0: 30,
            24: 60,
        }
        custom double myData = 3
    }
    over "Box"
    {
        double size = 50
    }
    def PhysicsRevoluteJoint "Hinge" (
        prepend apiSchemas = ["PhysicsDriveAPI:angular"]
    )
    {
        float drive:angular:physics:damping = 50
        uniform token physics:axis = "Z"
    }
}
'''


def _means(result, line):
    return next(item["means"] for item in result["explanation"] if item["line"] == line)


def test_lines_are_explained_with_paths_and_schema_facts():
    result = explain_usda(text=TEXT)
    assert result["ok"] and result["prims"] == ["/World", "/World/Ball", "/World/Box", "/World/Hinge"]
    lines = {item["line"] for item in result["explanation"]}
    assert not lines & {4, 5, 16, 21, 22}  # inside dictionaries / time samples: app data, not attributes
    assert "never assume" in _means(result, 9)
    assert "`/World/Ball`" in _means(result, 14)
    assert "animation keys" in _means(result, 20) and "fallback is 1.0" in _means(result, 20)
    assert "your own data" in _means(result, 24)
    assert "never creates" in _means(result, 26) and "`/World/Box`" in _means(result, 26)
    assert "PhysicsDriveAPI (angular)" in _means(result, 34)  # multiple-apply API found through its instance
    assert "cannot be animated" in _means(result, 35)


def test_prim_filter_file_mode_and_errors():
    only = explain_usda(text=TEXT, prim="/World/Hinge")
    assert {item["line"] for item in only["explanation"]} == {30, 31, 34, 35}
    assert not explain_usda()["ok"] and not explain_usda(text="x", path="y")["ok"]
    assert not explain_usda(text=TEXT, prim="World")["ok"]
    assert not explain_usda(path="notes.txt")["ok"]
    assert features.FEATURES["usda_explain"].mutates is False


def test_prim_filter_does_not_match_a_sibling_with_a_longer_name():
    text = 'def Xform "World"\n{\n    def Cube "Hinge"\n    {\n    }\n    def Cube "Hinge2"\n    {\n    }\n}\n'
    assert explain_usda(text=text, prim="/World/Hinge")["prims"] == ["/World/Hinge"]
