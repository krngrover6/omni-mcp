"""Python practice exercises: every solution passes, starters fail, errors are explained, and runs stay isolated.

Each attempt runs in a child Python process on a throwaway scene in a temporary folder; nothing here touches real
scenes, the network or any local port.
"""

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

try:
    from pxr import Usd  # noqa: F401
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp.exam import blueprint, python_practice as pp
from omniverse_scene_mcp.exam.progress import ProgressStore

REPO_SRC = os.path.dirname(os.path.dirname(os.path.abspath(pp.__file__)))  # .../src


def _exercises():
    return pp._load()["exercises"]


def _with_temp_parent(func):
    """Run func(parent_dir) with attempts created under a fresh folder; return (result, leftover entries)."""
    parent = tempfile.mkdtemp(prefix="ousd_practice_test_")
    old = pp.TEMP_PARENT
    pp.TEMP_PARENT = parent
    try:
        result = func(parent)
        leftovers = os.listdir(parent)
    finally:
        pp.TEMP_PARENT = old
        shutil.rmtree(parent, ignore_errors=True)
    return result, leftovers


# ------------------------------------------------------------------------------------------------- data


def test_exercise_data_is_complete_and_well_formed():
    exercises = _exercises()
    assert len(exercises) >= 20
    ids = [e["id"] for e in exercises]
    assert len(ids) == len(set(ids))
    objective_ids = {o["id"] for d in blueprint.load_blueprint()["domains"] for o in d["objectives"]}
    levels = pp._load()["levels"]
    for exercise in exercises:
        for key in ("id", "title", "level", "objective_ids", "goal", "starter_code", "hints", "solution", "checks"):
            assert exercise.get(key), (exercise["id"], key)
        assert exercise["level"] in levels
        assert set(exercise["objective_ids"]) <= objective_ids, exercise["id"]
        assert 2 <= len(exercise["hints"]) <= 3, exercise["id"]
        assert "TODO" in exercise["starter_code"], exercise["id"]
        assert "stage" in exercise["starter_code"], exercise["id"]
        assert "#" in exercise["solution"], f"{exercise['id']} solution should be commented"
        for check in exercise["checks"]:
            assert check["type"] in pp.CHECK_TYPES, (exercise["id"], check["type"])
            assert check.get("hint"), (exercise["id"], check)
            assert pp._describe_check(check) != check["type"]
        for check in exercise["checks"]:
            if check["type"] == "variable":
                assert check["name"] in exercise.get("report_vars", []), exercise["id"]
    # Gentle order: all beginner exercises come before the intermediate ones.
    order = [levels.index(e["level"]) for e in exercises]
    assert order == sorted(order)


def test_every_solution_passes_and_every_starter_fails():
    for exercise in _exercises():
        solved = pp.run_attempt(exercise["id"], exercise["solution"])
        assert solved["ok"] and solved["passed"], (exercise["id"], solved["checks"], solved["error"], solved["stderr"])
        assert all(c["passed"] for c in solved["checks"])
        assert solved["next_hint"] is None
        starter = pp.run_attempt(exercise["id"], exercise["starter_code"])
        assert starter["ok"] and not starter["passed"], exercise["id"]
        assert starter["error"] is None, (exercise["id"], starter["error"])  # starter code is valid Python
        failing = [c for c in starter["checks"] if not c["passed"]]
        assert failing and all(c["hint"] for c in failing), exercise["id"]
        assert starter["next_hint"]


# ------------------------------------------------------------------------------------------------- listing


def test_list_and_get_exercises():
    listed = pp.list_exercises()
    assert listed["ok"] and listed["count"] == len(_exercises())
    assert all("solution" not in row and "starter_code" not in row for row in listed["exercises"])
    beginner = pp.list_exercises("beginner")
    assert beginner["count"] and all(row["level"] == "beginner" for row in beginner["exercises"])
    assert pp.list_exercises("expert")["ok"] is False

    first = listed["exercises"][0]["id"]
    hidden = pp.get_exercise(first)
    assert hidden["ok"] and "solution" not in hidden
    assert hidden["starter_code"] and hidden["hints"] and hidden["checks"]
    assert hidden["starting_scene"].startswith("#usda")
    shown = pp.get_exercise(first, reveal=True)
    assert shown["solution"]
    missing = pp.get_exercise("no-such-exercise")
    assert missing["ok"] is False and "py01-define-xform" in missing["error"]


def test_bad_inputs_are_rejected_without_running():
    assert pp.run_attempt("nope", "pass")["ok"] is False
    assert pp.run_attempt("py01-define-xform", 42)["ok"] is False
    too_long = pp.run_attempt("py01-define-xform", "#" * (pp.MAX_CODE_CHARS + 1))
    assert too_long["ok"] is False and too_long["passed"] is False
    assert pp.run_attempt("py01-define-xform", "pass", timeout_s="soon")["ok"] is False


# ---------------------------------------------------------------------------------------- helpful messages


def test_wrong_sublayer_order_names_the_problem():
    code = (
        "root = stage.GetRootLayer()\n"
        "root.subLayerPaths.append('base.usda')\n"
        "root.subLayerPaths.append('override.usda')\n"
        "winning_size = stage.GetPrimAtPath('/World/Box').GetAttribute('size').Get()\n"
    )
    result = pp.run_attempt("py16-sublayer-strength", code)
    assert result["ok"] and not result["passed"] and result["error"] is None
    first = result["checks"][0]
    assert first["actual"] == ["base.usda", "override.usda"]
    assert first["expected"] == ["override.usda", "base.usda"]
    assert "strongest first" in result["next_hint"]
    size = result["checks"][1]
    assert size["actual"] == 2 and "base.usda is first" in size["hint"]


def test_wrong_value_reports_expected_and_actual():
    code = "from pxr import UsdGeom\nUsdGeom.Cube.Define(stage, '/World/Box').GetSizeAttr().Set(3)\n"
    result = pp.run_attempt("py02-cube-size", code)
    assert not result["passed"]
    size = next(c for c in result["checks"] if "size" in c["check"])
    assert size["expected"] == 2.5 and size["actual"] == 3 and "2.5" in size["hint"]
    assert result["summary"] == "1 of 2 checks passed."


def test_missing_import_is_explained():
    result = pp.run_attempt("py01-define-xform", "UsdGeom.Xform.Define(stage, '/World')\n")
    error = result["error"]
    assert error["type"] == "NameError" and error["line"] == 1
    assert "from pxr import UsdGeom" in error["explanation"]
    assert error["source_line"] == "UsdGeom.Xform.Define(stage, '/World')"
    assert result["next_hint"] == error["explanation"]


def test_misspelled_name_is_explained():
    code = "from pxr import UsdGeom\nbox = UsdGeom.Cube.Define(stage, '/World/Box')\nBox.GetSizeAttr().Set(2.5)\n"
    error = pp.run_attempt("py02-cube-size", code)["error"]
    assert error["type"] == "NameError" and error["line"] == 3
    assert "case-sensitive" in error["explanation"]


def test_vec3_signature_mismatch_is_explained():
    code = (
        "from pxr import UsdGeom\n"
        "api = UsdGeom.XformCommonAPI(stage.GetPrimAtPath('/World/Box'))\n"
        "api.SetRotate((0, 45))\n"
    )
    error = pp.run_attempt("py04-rotate-scale", code)["error"]
    assert error["type"] == "ArgumentError" and error["line"] == 3
    assert "Gf.Vec3f" in error["explanation"] and "Gf.Vec3d" in error["explanation"]


def test_invalid_prim_is_explained():
    code = "box_size = stage.GetPrimAtPath('/World/box').GetAttribute('size').Get()\n"
    result = pp.run_attempt("py05-read-attribute", code)
    assert "does not exist" in result["error"]["explanation"]
    assert any("was not created" in str(c["actual"]) for c in result["checks"])


def test_attribute_errors_are_explained():
    on_prim = pp.run_attempt(
        "py02-cube-size", "box = stage.DefinePrim('/World/Box', 'Cube')\nbox.GetSizeAttr().Set(2.5)\n"
    )["error"]
    assert on_prim["type"] == "AttributeError" and "UsdGeom.Cube(prim)" in on_prim["explanation"]
    on_none = pp.run_attempt("py01-define-xform", "value = None\nvalue.Define()\n")["error"]
    assert on_none["type"] == "AttributeError" and "None" in on_none["explanation"]


def test_syntax_and_indentation_errors_are_explained():
    syntax = pp.run_attempt("py02-cube-size", "from pxr import UsdGeom\nbox = UsdGeom.Cube.Define(stage, '/World/Box'\n")
    assert syntax["ok"] and not syntax["passed"]
    assert syntax["error"]["type"] == "SyntaxError" and syntax["error"]["line"] == 2
    assert "none of it ran" in syntax["error"]["explanation"]
    indent = pp.run_attempt("py01-define-xform", "if True:\nx = 1\n")
    assert indent["error"]["type"] == "IndentationError"
    assert "indent" in indent["error"]["explanation"]


def test_partial_work_is_still_checked_after_an_error():
    code = "from pxr import UsdGeom\nUsdGeom.Cube.Define(stage, '/World/Box')\nraise ValueError('stop here')\n"
    result = pp.run_attempt("py02-cube-size", code)
    assert not result["passed"] and result["error"]["type"] == "ValueError"
    assert result["checks"][0]["passed"] is True  # the cube was saved before the error
    assert "line 3" in result["summary"]


# ------------------------------------------------------------------------------------------------- safety


def test_infinite_loop_is_killed_on_timeout():
    marker_dir = tempfile.mkdtemp(prefix="ousd_practice_marker_")
    marker = os.path.join(marker_dir, "ticks.txt")
    code = (
        "import time\n"
        f"path = {marker!r}\n"
        "while True:\n"
        "    with open(path, 'a') as handle:\n"
        "        handle.write('x')\n"
        "    time.sleep(0.01)\n"
    )
    try:
        started = time.monotonic()
        result = pp.run_attempt("py01-define-xform", code, timeout_s=1.5)
        elapsed = time.monotonic() - started
        assert result["ok"] and not result["passed"] and result["timed_out"]
        assert result["error"]["type"] == "Timeout" and "loop" in result["error"]["explanation"]
        assert result["checks"] == []
        assert elapsed < 15
        assert os.path.exists(marker)  # the loop really ran ...
        size = os.path.getsize(marker)
        time.sleep(0.5)
        assert os.path.getsize(marker) == size  # ... and stopped when the process was killed
    finally:
        shutil.rmtree(marker_dir, ignore_errors=True)


def test_child_cannot_see_parent_env_or_real_paths():
    code = (
        "import json, os, sys\n"
        "print(json.dumps({\n"
        "    'sentinel': os.environ.get('OUSD_PRACTICE_SENTINEL'),\n"
        "    'proxy': os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy'),\n"
        "    'token': os.environ.get('GITHUB_TOKEN'),\n"
        "    'env_keys': sorted(os.environ),\n"
        "    'env_values': list(os.environ.values()),\n"
        "    'cwd': os.getcwd(),\n"
        "    'root_layer': stage.GetRootLayer().realPath,\n"
        "    'sys_path': sys.path,\n"
        "    'flags_isolated': sys.flags.isolated,\n"
        "}))\n"
    )
    saved = {key: os.environ.get(key) for key in ("OUSD_PRACTICE_SENTINEL", "HTTPS_PROXY", "GITHUB_TOKEN")}
    os.environ["OUSD_PRACTICE_SENTINEL"] = "sentinel-value-7731"
    os.environ["HTTPS_PROXY"] = "http://proxy.invalid:9"
    os.environ["GITHUB_TOKEN"] = "not-a-real-token"
    try:
        result = pp.run_attempt("py01-define-xform", code)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert result["ok"] and result["error"] is None, result
    seen = json.loads(result["stdout"])
    assert seen["sentinel"] is None and seen["proxy"] is None and seen["token"] is None
    assert "sentinel-value-7731" not in result["stdout"]
    assert seen["flags_isolated"] == 1
    # The working folder and scene are the throwaway practice folder (shown scrubbed), not a real project path.
    assert seen["cwd"] == "<practice folder>"
    assert seen["root_layer"].replace("\\", "/") == "<practice folder>/scene.usda"
    # The only real path the child gets is the folder holding pxr (needed to import USD; pxr itself also puts
    # its own folder on PATH when it loads its DLLs).
    pxr_folder = os.path.normcase(pp._pxr_path())
    env_values = [
        entry
        for value in seen["env_values"]
        for entry in os.path.normcase(str(value)).split(os.pathsep)
        if not entry.startswith(pxr_folder)
    ]
    for real in (os.getcwd(), REPO_SRC):
        lowered = os.path.normcase(real)
        assert all(lowered not in value for value in env_values), (lowered, env_values)
        assert all(os.path.normcase(str(p)) != lowered for p in seen["sys_path"])
    assert "PATH" in seen["env_keys"]


def test_temp_dirs_are_cleaned_up():
    def attempts(_parent):
        return [
            pp.run_attempt("py01-define-xform", "from pxr import UsdGeom\nUsdGeom.Xform.Define(stage, '/World')\n"),
            pp.run_attempt("py24-flatten-export", "stage.Flatten().Export('delivery.usda')\n"),
            pp.run_attempt("py01-define-xform", "raise RuntimeError('boom')\n"),
            pp.run_attempt("py01-define-xform", "while True:\n    pass\n", timeout_s=1),
        ]

    results, leftovers = _with_temp_parent(attempts)
    assert [r["passed"] for r in results] == [True, True, False, False]
    assert leftovers == []


def test_output_is_capped():
    result = pp.run_attempt(
        "py01-define-xform", "print('x' * 200000)\nfrom pxr import UsdGeom\nUsdGeom.Xform.Define(stage, '/World')\n"
    )
    assert result["passed"]
    assert result["stdout_truncated"] and len(result["stdout"]) <= pp.STDOUT_CAP


def test_asset_paths_outside_the_practice_folder_are_not_composed():
    code = (
        "from pxr import UsdGeom\n"
        "prim = UsdGeom.Xform.Define(stage, '/World/Chair_01').GetPrim()\n"
        "prim.GetReferences().AddReference('../outside/chair.usda')\n"
    )
    result = pp.run_attempt("py18-add-reference", code)
    assert result["ok"] and not result["passed"]
    assert all("outside the practice folder" in str(c["actual"]) for c in result["checks"])


def test_learner_chdir_does_not_break_grading():
    code = (
        "import os\n"
        "from pxr import UsdGeom\n"
        "UsdGeom.Xform.Define(stage, '/World')\n"
        "os.chdir(os.environ['TEMP'])\n"
    )
    result = pp.run_attempt("py01-define-xform", code)
    assert result["passed"], result


def test_results_say_the_scene_was_throwaway():
    result = pp.run_attempt("py01-define-xform", "pass\n")
    assert "throwaway" in result["sandbox_note"]
    assert "throwaway" in pp.get_exercise("py01-define-xform")["how_it_runs"]


# ------------------------------------------------------------------------------------------------- progress


def test_record_progress_uses_python_practice_source():
    store = ProgressStore(memory_only=True)
    record = pp.record_progress(store, "py16-sublayer-strength", True)
    assert record["ok"] and record["is_correct"] is True
    assert record["domain"]["name"] == "Composition"
    assert {row["objective_id"] for row in record["objectives"]} == {"1.1", "1.3", "1.5"}
    pp.record_progress(store, "py09-display-color", False)
    history = store.history()
    rows = history["history"] if isinstance(history, dict) else history
    assert {row["source"] for row in rows} == {"python_practice"}
    try:
        pp.record_progress(store, "missing", True)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown exercise ids must be rejected")
    try:
        pp.record_progress(store, "py01-define-xform", "yes")
    except TypeError:
        pass
    else:
        raise AssertionError("passed must be a bool")


# ------------------------------------------------------------------------------------- review fixes (round 6)


def test_pxr_as_a_namespace_package_still_finds_its_folder():
    # Kit's omni.usd.libs ships pxr without __init__.py, so pxr.__file__ is None: this used to raise TypeError.
    import pxr

    folder = Path(next(iter(pxr.__path__))).resolve()
    saved = {key: pxr.__dict__[key] for key in ("__file__", "__path__") if key in pxr.__dict__}
    try:
        pxr.__file__ = None
        pxr.__path__ = [str(folder)]
        assert pp._pxr_path() == str(folder.parent)
        pxr.__path__ = []
        assert pp._pxr_path() is None
        result = pp.run_attempt("py01-define-xform", "x = 1\n")
        assert result["ok"] is False and result["passed"] is False
        assert "not installed" in result["error"]["message"], result["error"]
    finally:
        pxr.__dict__.update(saved)


def test_a_process_started_by_the_code_cannot_hang_the_attempt():
    # The grandchild inherits the stdout/stderr pipes; closing them used to wait until it exited.
    looping = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "while True:\n"
        "    pass\n"
    )
    started = time.monotonic()
    result, leftovers = _with_temp_parent(lambda _parent: pp.run_attempt("py01-define-xform", looping, timeout_s=2))
    assert result["ok"] and result["timed_out"] and result["error"]["type"] == "Timeout"
    assert time.monotonic() - started < 15, "the timeout must also stop processes the code started"
    assert leftovers == []

    background = (
        "import os, subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'], cwd=os.path.dirname(sys.executable))\n"
        "from pxr import UsdGeom\n"
        "UsdGeom.Xform.Define(stage, '/World')\n"
    )
    started = time.monotonic()
    result, leftovers = _with_temp_parent(lambda _parent: pp.run_attempt("py01-define-xform", background, timeout_s=20))
    assert time.monotonic() - started < 7, "a finished attempt must not wait for a background program"
    assert result["ok"] and result["passed"] is True and not result["timed_out"], result
    assert "background_process_note" in result
    assert leftovers == []
