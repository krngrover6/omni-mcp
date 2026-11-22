"""usd_code_verify: invented USD names are caught with suggestions, real code passes, run and docs are optional."""
from __future__ import annotations

try:
    from pxr import Usd  # noqa: F401
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import features
from omniverse_scene_mcp.code_verify import verify_code

BAD = '''from pxr import Usd, UsdGeom, UsdPhysics
stage = Usd.Stage.CreateInMemory()
cube = UsdGeom.Cube.Define(stage, "/World/Box")
cube.SetSize(50)
prim = cube.GetPrim()
prim.GetAttr("size")
UsdGeom.XformCommonAPI(prim).SetTranslation((0, 25, 0))
drive = UsdPhysics.DriveAPI.Apply(prim, "angular")
drive.GetTargetVelocityAttr().Set(200)
for p in stage.Traverse():
    print(p.GetPath(), p.GetTypeNme())
other = Usd.Stage.Create("x.usda")
'''

GOOD = '''from pxr import Usd, UsdGeom, UsdPhysics
import omni.usd
stage = omni.usd.get_context().get_stage()
cube = UsdGeom.Cube.Define(stage, "/World/C")
cube.GetSizeAttr().Set(2)
joint = stage.GetPrimAtPath("/World/C")
UsdPhysics.DriveAPI(joint, "angular").GetTargetVelocityAttr()
UsdGeom.XformCommonAPI(cube.GetPrim()).SetTranslate((0, 1, 0))
for child in stage.GetPseudoRoot().GetChildren():
    print(child.GetName())


def helper(thing):
    return thing.AnythingAtAll()   # unknown type: skipped, never guessed
'''


def _names(result):
    return {(p["line"], p["name"]) for p in result["problems"]}


def test_invented_names_are_caught_with_line_and_closest_real_name():
    result = verify_code(BAD, run=False)
    assert result["ok"] and result["verdict"] == "problems"
    assert _names(result) == {(4, "UsdGeom.Cube.SetSize"), (6, "Usd.Prim.GetAttr"),
                              (7, "UsdGeom.XformCommonAPI.SetTranslation"), (11, "Usd.Prim.GetTypeNme"),
                              (12, "Usd.Stage.Create")}
    by_line = {p["line"]: p for p in result["problems"]}
    assert "GetSizeAttr" in by_line[4]["did_you_mean"] and "GetSizeAttr().Set" in by_line[4]["tip"]
    assert by_line[11]["did_you_mean"][0] == "GetTypeName"
    assert "SetTranslate" in by_line[7]["did_you_mean"]
    assert result["next_step"].startswith("Line 4:")
    assert "UsdPhysics.DriveAPI.GetTargetVelocityAttr" in result["checked"]


def test_real_code_passes_and_kit_modules_are_only_noted():
    result = verify_code(GOOD, run=False)
    assert result["verdict"] == "ok" and result["problems"] == []
    assert "omni.usd" in result["kit_imports"] and result["unchecked"] >= 1
    assert {"UsdGeom.Cube.GetSizeAttr", "Usd.Attribute.Set", "UsdGeom.XformCommonAPI.SetTranslate",
            "Usd.Prim.GetChildren"} <= set(result["checked"])


def test_syntax_error_and_bad_input():
    result = verify_code("from pxr import Usd\nif True\n    pass\n")
    assert result["verdict"] == "syntax_error" and result["problems"][0]["line"] == 2
    assert not verify_code("")["ok"] and not verify_code(None)["ok"]
    bad_module = verify_code("from pxr import UsdGeomm\n", run=False)
    assert bad_module["problems"][0]["code"] == "unknown_module"
    assert "UsdGeom" in bad_module["problems"][0]["did_you_mean"]


def test_trial_run_reports_what_the_code_did():
    result = verify_code("from pxr import UsdGeom\nimport omni.usd\nstage = omni.usd.get_context().get_stage()\n"
                         "UsdGeom.Sphere.Define(stage, '/World/Ball')\nprint('made')\n", run=True)
    assert result["verdict"] == "ok" and result["run"]["ran"]
    assert "/World/Ball" in result["run"]["created_prims"] and "made" in result["run"]["stdout"]
    failing = verify_code("from pxr import Usd\nUsd.Stage.Open('no_such_file.usda').GetPrimAtPath('/A')\n", run=True)
    assert failing["verdict"] == "problems" and failing["problems"] == []
    assert failing["next_step"].startswith("The names exist, but the trial run failed")


def test_docs_attach_pages_and_survive_a_dead_server():
    asked = []

    def fake(queries):
        asked.extend(queries)
        return [{"ok": True, "content": "Title 'UsdGeom Cube', URL 'https://openusd.org/cube'\n---\nCube size attr.\n"}
                for _ in queries]

    result = verify_code(BAD, run=False, docs=True, runner=fake)
    assert result["docs"]["ok"] and result["docs"]["pages"][0]["sources"][0]["url"] == "https://openusd.org/cube"
    assert asked and asked[0].startswith("UsdGeom.Cube") and len(asked) <= 3

    def dead(queries):
        raise ConnectionError("off")

    off = verify_code(BAD, run=False, docs=True, runner=dead)
    assert not off["docs"]["ok"] and "docker start usd-code-mcp" in off["docs"]["note"]
    assert off["problems"]  # the API check still stands


def test_registered_as_read_only_local_tool_without_internal_params():
    spec = features.FEATURES["usd_code_verify"]
    assert spec.location == "local" and spec.mutates is False
    assert set(features.signature("usd_code_verify").parameters) == {"code", "run", "docs", "timeout_s"}
