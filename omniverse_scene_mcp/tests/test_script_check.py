"""Script checker (static, nothing runs) and the Isaac-style trial run on a practice scene (class session 7)."""
from omniverse_scene_mcp import script_check as sc
from omniverse_scene_mcp.exam import python_practice as pp

GOOD = """import omni.usd
from pxr import Gf, UsdGeom

stage = omni.usd.get_context().get_stage()
for i in range(3):
    cube = UsdGeom.Cube.Define(stage, f"/World/Boxes/Box_{i}")
    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(i, 0, 0.5))
box = stage.GetPrimAtPath("/World/Box")
if not box.IsValid():
    print("wrong path")
"""


def _codes(result):
    return {item["code"] for item in result["findings"]}


def test_clean_script_editor_code_has_no_errors():
    result = sc.check_script(GOOD)
    assert result["ok"] and result["runs"] and not [f for f in result["findings"] if f["severity"] == "error"], result


def test_beginner_mistakes_are_named_with_line_and_fix():
    cases = {
        "from omni.isaac.core import World\n": "renamed_import",
        "import time\nimport omni.usd\nstage = omni.usd.get_context().get_stage()\ntime.sleep(5)\n": "sleep",
        "while True:\n    x = 1\n": "endless_loop",
        "from pxr import Usd\nstage = Usd.Stage.Open('a.usda')\n": "opens_other_stage",
        "import shutil\nshutil.rmtree('C:/stuff')\n": "deletes_files",
        "prim = stage.GetPrimAtPath('/World/Box')\n": "no_stage",
        "x = omni.usd.get_context()\n": "missing_import",
        "from isaacsim import SimulationApp\napp = SimulationApp({})\n": "simulation_app_in_editor",
        "def f(:\n    pass\n": "syntax",
    }
    for code, expected in cases.items():
        result = sc.check_script(code)
        assert result["ok"] and expected in _codes(result), (expected, result["findings"])
        finding = next(f for f in result["findings"] if f["code"] == expected)
        assert finding["message"] and finding["fix"], finding
    unchecked = sc.check_script("import omni.usd\nstage = omni.usd.get_context().get_stage()\n"
                                "p = stage.GetPrimAtPath('/World/X')\nprint(p.GetName())\n")
    assert "unchecked_prim" in _codes(unchecked)


def test_standalone_scripts_need_simulation_app_first():
    assert "no_simulation_app" in _codes(sc.check_script("import omni.usd\n", where="standalone"))
    late = "import omni.usd\nfrom isaacsim import SimulationApp\napp = SimulationApp({})\n"
    assert "import_before_app" in _codes(sc.check_script(late, where="standalone"))
    right = "from isaacsim import SimulationApp\napp = SimulationApp({'headless': True})\nimport omni.usd\napp.close()\n"
    assert sc.check_script(right, where="standalone")["runs"]
    assert not sc.check_script("x", where="phone")["ok"] and not sc.check_script("")["ok"]


def test_trial_run_reports_created_prims_output_and_errors():
    if pp._pxr_path() is None:
        return
    done = pp.trial_run(GOOD + "import omni.timeline\nomni.timeline.get_timeline_interface().play()\n")
    assert done["ok"] and done["ran"], done
    assert {"/World/Boxes", "/World/Boxes/Box_0", "/World/Boxes/Box_2"} <= set(done["created_prims"]), done
    assert "[timeline] play" in done["stdout"]
    broken = pp.trial_run("import omni.usd\nstage = omni.usd.get_context().get_stage()\nstage.GetPrimAtPath('/World/Box').Nope()\n")
    assert broken["ok"] and not broken["ran"] and broken["error"]["line"] == 3, broken
    assert not pp.trial_run("")["ok"]
