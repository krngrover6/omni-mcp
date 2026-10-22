"""journal_script: the change journal becomes one runnable, commented replay script built from python_teacher snippets.

Standalone scripts are run with usd-core against a fixture .usda in a fresh temp dir and must reproduce the final
composed values; script-editor scripts run against the same kind of stage through a tiny stand-in for omni.usd.
Hostile journal values must be refused or end up as harmless literals. No network, no ports, no Kit; temp files only.
"""

from __future__ import annotations

import ast
import contextlib
import io
import shutil
import sys
import tempfile
import types
from pathlib import Path

try:
    from pxr import Gf, Usd, UsdGeom, UsdLux, UsdShade
except ImportError:  # pragma: no cover - depends on the local environment
    Usd = None  # the compile / refusal tests still run without pxr

from omniverse_scene_mcp.journal import ChangeJournal
from omniverse_scene_mcp.journal_script import script_from_journal

# One line: the journal stores text on one line (it collapses whitespace), so this is what a replay can write back.
EVIL_TEXT = "hi'); import os; os.system('echo pwned') # \"\"\" __import__('os') \\"


# --------------------------------------------------------------------------------------------- helpers


@contextlib.contextmanager
def _folder():
    folder = Path(tempfile.mkdtemp(prefix="journal_script_"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _fixture(folder: Path) -> Path:
    path = folder / "scene.usda"
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    UsdGeom.Cube.Define(stage, "/World/Cube")
    UsdGeom.Scope.Define(stage, "/World/Looks")
    stage.GetRootLayer().Save()
    return path


def _journal(events: list[dict]) -> list[dict]:
    """Real journal entries (ChangeJournal.record sanitizes and numbers them), newest first like entries()."""
    journal = ChangeJournal(memory_only=True)
    for index, event in enumerate(events):
        journal.record({"time": f"2026-10-22T10:{index:02d}:00", "target": "composer", **event})
    return journal.entries(limit=500)


def _edit(tool: str, action: str, args: dict, prim: str, **extra) -> dict:
    return {"tool": tool, "action": action, "args": {**args, "dry_run": False, "show_python": False},
            "prims": [prim], **extra}


def _undo(**extra) -> dict:
    return {"tool": "scene_undo_last", "action": "undo", "kind": "undo", "args": {"dry_run": False}, **extra}


def _story() -> list[dict]:
    """Move, rename, light, material, bind, attributes, an undone scale, and things the script must leave out."""
    return [
        _edit("scene_move", "move", {"path": "/World/Cube", "translation": [1, 2, 3], "rotation": None, "scale": None},
              "/World/Cube", after={"translation": [1, 2, 3]}, undo_id=1),
        _edit("scene_rename", "rename", {"path": "/World/Cube", "new_name": "Box"}, "/World/Cube", undo_id=2),
        _edit("scene_create_light", "create_light",
              {"path": "/World/Key", "light_type": "sphere", "intensity": 5000, "color": [1, 0.5, 0.25],
               "translation": [0, 0, 4], "exposure": None}, "/World/Key", details={"light_type": "sphere"}, undo_id=3),
        _edit("scene_create_material", "create_material",
              {"path": "/World/Looks/Red", "diffuse_color": [0.8, 0.1, 0.1], "roughness": 0.3}, "/World/Looks/Red",
              undo_id=4),
        _edit("scene_bind_material", "bind_material", {"prim_path": "/World/Box", "material_path": "/World/Looks/Red"},
              "/World/Box", undo_id=5),
        _edit("scene_set_attribute", "set_attribute", {"path": "/World/Key", "name": "inputs:intensity", "value": 8000},
              "/World/Key", undo_id=6),
        _edit("scene_set_attribute", "set_attribute", {"path": "/World/Box", "name": "size", "value": 3},
              "/World/Box", undo_id=7),
        _edit("scene_move", "move", {"path": "/World/Box", "translation": None, "rotation": None, "scale": [2, 2, 2]},
              "/World/Box", undo_id=8),
        _undo(undo_id=8, details={"label": "Scale /World/Box"}),
        _edit("scene_set_attribute", "set_attribute",
              {"path": "/World/Box", "name": "userProperties:note", "value": EVIL_TEXT}, "/World/Box", undo_id=9),
        _edit("scene_delete", "delete", {"path": "/World/Missing"}, "/World/Missing", ok=False, error="No prim"),
        _edit("scene_reparent", "reparent", {"path": "/World/Box", "new_parent": "/World/Looks"}, "/World/Box"),
        {"tool": "scene_save", "action": "save", "args": {}, "details": {"saved_to": "scene.usda"}},
        _undo(ok=False, error="the prim changed"),
    ]


class _FakeContext:
    def __init__(self, stage):
        self._stage = stage

    def get_stage(self):
        return self._stage


@contextlib.contextmanager
def _fake_omni(stage):
    saved = {name: sys.modules.get(name) for name in ("omni", "omni.usd")}
    omni = types.ModuleType("omni")
    omni_usd = types.ModuleType("omni.usd")
    omni_usd.get_context = lambda: _FakeContext(stage)
    omni.usd = omni_usd
    omni.__path__ = []
    sys.modules["omni"], sys.modules["omni.usd"] = omni, omni_usd
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _exec(code: str) -> tuple[dict, str]:
    namespace = {"__name__": "__journal_script__"}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(code, "<journal script>", "exec"), namespace)
    return namespace, out.getvalue()


def _close(a, b, tol=1e-5) -> bool:
    return all(abs(float(x) - float(y)) < tol for x, y in zip(a, b)) and len(a) == len(b)


def _check_final_scene(stage) -> None:
    """The composed values the journal story ends with."""
    assert not stage.GetPrimAtPath("/World/Cube").IsValid()
    box = stage.GetPrimAtPath("/World/Box")
    assert box.IsValid() and box.GetTypeName() == "Cube"
    assert _close(box.GetAttribute("xformOp:translate").Get(), (1, 2, 3))
    assert not box.GetAttribute("xformOp:scale").IsValid(), "the undone scale must not be replayed"
    assert box.GetAttribute("size").Get() == 3.0
    assert box.GetAttribute("userProperties:note").Get() == EVIL_TEXT
    bound = UsdShade.MaterialBindingAPI(box).ComputeBoundMaterial()[0]
    assert str(bound.GetPath()) == "/World/Looks/Red"
    shader = UsdShade.Shader(stage.GetPrimAtPath("/World/Looks/Red/PreviewSurface"))
    assert _close(shader.GetInput("diffuseColor").Get(), (0.8, 0.1, 0.1))
    assert abs(shader.GetInput("roughness").Get() - 0.3) < 1e-6
    light = UsdLux.SphereLight(stage.GetPrimAtPath("/World/Key"))
    assert light and light.GetPrim().GetTypeName() == "SphereLight"
    assert light.GetIntensityAttr().Get() == 8000.0
    assert _close(light.GetColorAttr().Get(), (1, 0.5, 0.25))
    assert _close(light.GetPrim().GetAttribute("xformOp:translate").Get(), (0, 0, 4))
    assert stage.GetPrimAtPath("/World/Box").GetParent().GetPath() == "/World"  # reparent was not replayed


def _imports(code: str) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module)
    return names


# ------------------------------------------------------------------------------------------------- tests


def test_story_builds_one_script_with_steps_and_skips():
    result = script_from_journal(_journal(_story()), title="Warehouse day 1")
    assert result["ok"], result
    compile(result["code"], "<x>", "exec")
    actions = [step["action"] for step in result["steps"]]
    assert actions == ["move", "rename", "create_light", "create_material", "bind_material", "set_attribute",
                       "set_attribute", "set_attribute"], actions
    ids = [step["entry_id"] for step in result["steps"]]
    assert ids == sorted(ids), "steps must run oldest first even though entries() lists newest first"
    reasons = {item["entry_id"]: item["reason"] for item in result["skipped"]}
    assert len(result["skipped"]) == 6
    assert "taken out of the script" in reasons[8] and "Undo of entry 8" in reasons[9]
    assert "did not go through" in reasons[11] and "reparent" in reasons[12].lower()
    assert "Saves are left out" in reasons[13] and "did not go through" in reasons[14]
    code = result["code"]
    assert code.startswith("# Warehouse day 1\n")
    assert code.count("import omni.usd") == 1 and code.count("from pxr import") == 1
    assert code.count("stage = omni.usd.get_context().get_stage()") == 1
    assert code.count("# === Step ") == 8 and "Step 1 of 8: journal entry 1, 2026-10-22 10:00:00" in code
    assert "# You moved /World/Cube to (1, 2, 3) in Composer." in code
    assert "Undo with scene_undo_last" not in code
    assert "scale = (2.0, 2.0, 2.0)" not in code
    assert "Run it on a copy" in result["safety_note"] and "Script Editor" in result["where_to_run"]
    light_note = next(step["note"] for step in result["steps"] if step["action"] == "create_light")
    assert light_note is None  # exposure was None, so nothing is missing from the snippet


def test_standalone_script_reproduces_the_final_scene_on_a_temp_stage():
    if Usd is None:
        return
    with _folder() as folder:
        path = _fixture(folder)
        result = script_from_journal(_journal(_story()), context="standalone", stage_path=str(path))
        assert result["ok"], result
        assert result["code"].count("stage.Save()") == 1 and result["code"].rstrip().endswith("stage_path)")
        assert "import omni" not in result["code"] and "get_context" not in result["code"]
        namespace, out = _exec(result["code"])
        assert "Replayed 8 step(s)" in out
        _check_final_scene(namespace["stage"])
        reopened = Usd.Stage.Open(str(path))
        reopened.Reload()
        _check_final_scene(reopened)  # stage.Save() wrote it to disk
        assert sorted(p.name for p in folder.iterdir()) == ["scene.usda"]


def test_script_editor_script_runs_against_a_stand_in_omni_usd():
    if Usd is None:
        return
    with _folder() as folder:
        path = _fixture(folder)
        result = script_from_journal(_journal(_story()), context="script_editor")
        assert result["ok"], result
        stage = Usd.Stage.Open(str(path))
        with _fake_omni(stage):
            namespace, out = _exec(result["code"])
        assert namespace["stage"] is stage and "File > Save" in out
        _check_final_scene(stage)
        assert "stage.Save()" not in result["code"]


def test_undo_without_an_id_is_skipped_with_a_note_and_redo_brings_a_step_back():
    events = [
        _edit("scene_create", "create", {"path": "/World/Ball", "type_name": "Sphere"}, "/World/Ball", undo_id=1),
        _edit("scene_move", "move", {"path": "/World/Ball", "translation": [0, 0, 2]}, "/World/Ball", undo_id=2),
        _undo(undo_id=2),
        {"tool": "scene_redo", "action": "redo", "kind": "undo", "args": {}, "undo_id": 2},
        _edit("scene_move", "move", {"path": "/World/Ball", "translation": [0, 0, 5]}, "/World/Ball"),
        _undo(),  # says nothing about what it reverted
        _edit("scene_set_attribute", "set_attribute", {"path": "/World/Ball", "name": "radius", "value": 2},
              "/World/Ball", label="Set radius on /World/Ball"),
        _undo(details={"label": "Set radius on /World/Ball"}, prims=["/World/Ball"]),
    ]
    result = script_from_journal(_journal(events), context="standalone", stage_path="ball.usda")
    assert result["ok"], result
    assert [step["entry_id"] for step in result["steps"]] == [1, 2, 5]
    reasons = {item["entry_id"]: item["reason"] for item in result["skipped"]}
    assert "Redo of entry 2" in reasons[4]
    assert "did not say which edit" in reasons[6]
    assert "label" in reasons[7] and "taken out" in reasons[7]
    assert "(Undone and then redone at" in result["code"]
    if Usd is None:
        return
    with _folder() as folder:
        target = folder / "ball.usda"
        result = script_from_journal(_journal(events), context="standalone", stage_path=str(target))
        namespace, _ = _exec(result["code"])
        ball = namespace["stage"].GetPrimAtPath("/World/Ball")
        assert ball.GetTypeName() == "Sphere"
        assert _close(ball.GetAttribute("xformOp:translate").Get(), (0, 0, 5))
        assert not ball.GetAttribute("radius").HasAuthoredValue()  # the undone radius edit is not replayed
        assert target.exists()


def test_include_failed_keeps_failed_edits_as_commented_out_code():
    events = [
        _edit("scene_create", "create", {"path": "/World/A", "type_name": "Cube"}, "/World/A"),
        _edit("scene_delete", "delete", {"path": "/World/Ghost"}, "/World/Ghost", ok=False, error="No prim"),
    ]
    plain = script_from_journal(_journal(events))
    assert len(plain["steps"]) == 1 and "include_failed=True" in plain["skipped"][0]["reason"]
    result = script_from_journal(_journal(events), include_failed=True)
    assert result["ok"] and [step["ok"] for step in result["steps"]] == [True, False]
    assert "Commented out" in result["steps"][1]["note"]
    assert "# prim_path = '/World/Ghost'" in result["code"]
    assert "\nprim_path = '/World/Ghost'" not in result["code"]
    assert "Replays 1 edit from" in result["code"]
    tree = ast.parse(result["code"])
    assert not any("Ghost" in ast.unparse(node) for node in tree.body)
    if Usd is None:
        return
    with _folder() as folder:
        path = _fixture(folder)
        stage = Usd.Stage.Open(str(path))
        with _fake_omni(stage):
            _exec(result["code"])
        assert stage.GetPrimAtPath("/World/A").IsValid()


def test_injection_like_journal_values_are_refused_or_become_literals():
    evil_path = "/World/Cube'); import os; os.system('calc') #"
    events = [
        _edit("scene_move", "move", {"path": evil_path, "translation": [1, 2, 3]}, "/World/Cube"),
        _edit("scene_rename", "rename", {"path": "/World/Cube", "new_name": "Box\nimport os"}, "/World/Cube"),
        _edit("scene_set_attribute", "set_attribute", {"path": "/World/Cube", "name": "size; import os", "value": 1},
              "/World/Cube"),
        _edit("scene_set_attribute", "set_attribute",
              {"path": "/World/Cube", "name": "size", "value": {"__class__": "os.system"}}, "/World/Cube"),
        _edit("scene_create_light", "create_light", {"path": "/World/L", "light_type": "rect\nimport os"}, "/World/L"),
        _edit("scene_move", "move", {"path": "/World/Cube", "translation": ["1", "2", "__import__('os')"]},
              "/World/Cube"),
        _edit("scene_create", "create", {"path": "/World/Ok", "type_name": "Cube'); import os #"}, "/World/Ok"),
        {"tool": "scene_move\nimport os", "action": "move", "args": {"path": "/World/Cube", "translation": [0, 0, 1]}},
        _edit("scene_set_attribute", "set_attribute", {"path": "/World/Cube", "name": "userProperties:note",
                                                       "value": EVIL_TEXT}, "/World/Cube"),
        _edit("scene_set_attribute", "set_attribute", {"path": "/World/Cube", "name": "userProperties:secret_note",
                                                       "value": "password=hunter2"}, "/World/Cube"),
        "not an entry",
    ]
    entries = _journal([e for e in events if isinstance(e, dict)]) + [events[-1]]
    result = script_from_journal(entries, title="Evil\nimport os\nos.system('x') \u2028 more", context="script_editor")
    assert result["ok"], result
    assert [step["entry_id"] for step in result["steps"]] == [9], result["steps"]
    code = result["code"]
    assert _imports(code) == {"omni.usd", "pxr"}
    for node in ast.walk(ast.parse(code)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "system"
        if isinstance(node, ast.Name):
            assert node.id not in ("__import__", "eval", "exec", "os")
    assert code.splitlines()[0] == "# Evil import os os.system('x') more"
    assert f"value = {EVIL_TEXT!r}" in code
    reasons = " | ".join(item["reason"] for item in result["skipped"])
    assert "python_teacher refused" in reasons and "Not a journal entry" in reasons
    assert "removed from the journal" in reasons  # password=... was redacted by the journal, so it is not replayed
    assert len(result["skipped"]) == 10
    # The same hostile text in a standalone script is still a literal and runs harmlessly.
    if Usd is None:
        return
    with _folder() as folder:
        path = _fixture(folder)
        standalone = script_from_journal(entries, context="standalone", stage_path=str(path))
        namespace, _ = _exec(standalone["code"])
        assert namespace["stage"].GetPrimAtPath("/World/Cube").GetAttribute("userProperties:note").Get() == EVIL_TEXT


def test_unsupported_tools_and_missing_values_are_explained():
    events = [
        _edit("scene_aim", "aim", {"path": "/World/Cam", "target": [0, 0, 0]}, "/World/Cam"),
        _edit("scene_create_camera", "create_camera", {"path": "/World/Cam", "translation": [5, 5, 5],
                                                       "aim_at": "/World/Box"}, "/World/Cam"),
        _edit("scene_move", "move", {"path": "/World/Cube"}, "/World/Cube"),
        _edit("scene_set_attribute", "set_attribute", {"path": "/World/Cube", "name": "size", "value": 2, "time": 10},
              "/World/Cube"),
        {"tool": "router_dispatch", "action": "move", "args": {"request": "move the cube up"}},
        {"tool": "scene_create", "action": "create", "args": "path=/World/X"},
        _edit("scene_create_light", "create_light", {"path": "/World/Key", "light_type": "rect", "exposure": 2},
              "/World/Key"),
    ]
    result = script_from_journal(_journal(events))
    assert result["ok"] and len(result["steps"]) == 1
    assert "exposure" in result["steps"][0]["note"]
    reasons = [item["reason"] for item in result["skipped"]]
    assert any("demo camera" in r for r in reasons) and any("aimed at a prim" in r for r in reasons)
    assert any("translation or rotation or scale" in r for r in reasons)
    assert any("time code" in r for r in reasons) and any("Sentence commands" in r for r in reasons)
    assert any("The journal has no path" in r for r in reasons)  # args that were not a dict are stored as {}


def test_bad_input_and_nothing_to_replay():
    assert script_from_journal([], context="script_editor")["ok"] is False
    assert script_from_journal(None)["ok"] is False
    assert script_from_journal("entries")["ok"] is False
    assert "context" in script_from_journal([], context="blender")["error"]
    bad_path = script_from_journal([], context="standalone", stage_path="scene.txt")
    assert bad_path["ok"] is False and "stage_path" in bad_path["error"]
    only_save = script_from_journal({"entries": [{"id": 1, "time": "2026-10-22T10:00:00", "tool": "scene_save",
                                                 "action": "save", "args": {}}]})
    assert only_save["ok"] is False and only_save["code"] is None and len(only_save["skipped"]) == 1
    assert "Nothing to replay" in only_save["error"]


def test_entries_without_ids_are_ordered_by_time_and_mixed_targets_are_noted():
    newest_first = [
        {"time": "2026-10-22T10:05:00", "target": "isaac", "tool": "scene_move", "action": "move",
         "args": {"path": "/World/A", "translation": [0, 0, 2]}, "prims": ["/World/A"]},
        {"time": "2026-10-22T10:00:00", "target": "composer", "tool": "scene_create", "action": "create",
         "args": {"path": "/World/A", "type_name": "Cube"}, "prims": ["/World/A"]},
    ]
    result = script_from_journal(newest_first)
    assert [step["action"] for step in result["steps"]] == ["create_prim", "move"]
    assert any("several scenes" in note for note in result["notes"])
