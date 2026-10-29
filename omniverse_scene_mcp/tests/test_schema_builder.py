"""Custom schema builder: schema.usda written and checked, every part explained."""
from __future__ import annotations

try:
    from pxr import Sdf  # noqa: F401
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features, schema_builder as sb


def test_plans_for_each_kind_parse_and_pass_their_own_check():
    api = sb.schema_plan("StudioAssetAPI", attributes=[{"name": "studio:assetId", "type": "string", "default": "A1"},
                                                       {"name": "studio:price", "type": "float", "default": 2.5}])
    assert api["ok"] and api["check"]["ok"], api["check"]
    assert 'class "StudioAssetAPI"' in api["schema_usda"] and 'apiSchemaType = "singleApply"' in api["schema_usda"]
    assert 'string studio:assetId = "A1"' in api["schema_usda"] and "skipCodeGeneration = true" in api["schema_usda"]
    assert "ApplyAPI" in api["use_it"] and api["line_by_line"]
    typed = sb.schema_plan("StudioRobot", kind="typed_xformable", attributes=[{"name": "speed", "type": "double"}])
    assert typed["ok"] and 'class StudioRobot "StudioRobot"' in typed["schema_usda"], typed["check"]
    assert "inherits = </Xformable>" in typed["schema_usda"] and "DefinePrim" in typed["use_it"]
    multi = sb.schema_plan("StudioTagAPI", kind="multiple_apply_api", attributes=[{"name": "studio:label",
                                                                                   "type": "token"}])
    assert multi["ok"] and "multipleApply" in multi["schema_usda"] and "token label" in multi["schema_usda"]
    assert features.FEATURES["schema_plan"].mutates is False and features.FEATURES["schema_check"].mutates is False


def test_plan_refuses_beginner_mistakes():
    assert "API" in sb.schema_plan("StudioAsset")["error"]
    assert not sb.schema_plan("RobotAPI", kind="typed")["ok"]
    assert "namespace" in sb.schema_plan("StudioAssetAPI", attributes=[{"name": "assetId", "type": "string"}])["error"]
    assert not sb.schema_plan("StudioAssetAPI", attributes=[{"name": "studio:x", "type": "number"}])["ok"]
    assert not sb.schema_plan("lower", kind="typed")["ok"] and not sb.schema_plan("X", kind="weird")["ok"]


def test_check_names_the_usual_mistakes():
    broken = '''#usda 1.0
def "StudioAsset" (
    inherits = </APISchemaBase>
)
{
    string assetId = ""
}
'''
    result = sb.schema_check(broken)
    messages = " ".join(p["message"] for p in result["problems"])
    assert not result["ok"] and result["parses"]
    for words in ("subLayer", "GLOBAL", "class prims", "end in 'API'", "apiSchemaType"):
        assert words in messages, (words, messages)
    unreadable = sb.schema_check('class "X" (')
    assert unreadable["parses"] is False and unreadable["problems"][0]["severity"] == "error"
    assert not sb.schema_check("")["ok"]
