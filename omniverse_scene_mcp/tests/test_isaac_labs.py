"""Isaac labs are data: every step must name a real tool with arguments that tool really takes."""
import json
from types import SimpleNamespace

from omniverse_scene_mcp.exam import isaac_labs
from omniverse_scene_mcp.profiles import unknown_arguments
from test_twelve_gaps_integration import _server


def test_every_lab_step_names_a_real_tool_with_real_arguments_and_edits_have_previews():
    labs = isaac_labs._labs()
    with _server() as call:
        tools = {tool.name: tool.parameters for tool in call.app._tool_manager.list_tools()}
    for lab_id, lab in labs.items():
        assert lab["concept"] and lab["steps"] and lab["try_yourself"] and lab["words"], lab_id
        assert lab["objectives"] or lab_id in isaac_labs.FOUNDATIONS, lab_id
        for step in lab["steps"]:
            if step["kind"] == "ui":
                assert step["tool"] is None and step["say"] and step["look"], (lab_id, step)
                continue
            schema = tools.get(step["tool"])
            assert schema is not None, (lab_id, step["tool"])
            problem = unknown_arguments(SimpleNamespace(name=step["tool"], parameters=schema), step["arguments"])
            assert problem is None, (lab_id, problem)
            required = set(schema.get("required", []))
            assert required <= set(step["arguments"]), (lab_id, step["tool"], required - set(step["arguments"]))
            if step["kind"] == "edit":
                assert "dry_run" in schema["properties"], (lab_id, step["tool"], "edits must have a preview")
            assert step["look"], (lab_id, step["tool"])


def test_lab_lookup_by_objective_lab_id_and_code_only_objectives():
    found = isaac_labs.lab(objective_id="1.8")
    assert found["ok"] and found["lab_id"] == "value_ignored" and found["setup"] and found["how_to_run"]
    assert "isaac_study/study.usda" in found["setup"][0]
    assert isaac_labs.lab(lab_id="variants")["objectives"] == ["1.7", "1.6"]
    code_only = isaac_labs.lab(objective_id="3.5")
    assert code_only["ok"] and code_only["lab"] is None and "resolver" in code_only["message"]
    assert not isaac_labs.lab(lab_id="nope")["ok"]
    assert isaac_labs.lab()["start_here"] == ["basics_isaac", "basics_usd"]
    assert all(name in isaac_labs._labs() for name in isaac_labs.FOUNDATIONS)
    listed = isaac_labs.lab()
    assert listed["ok"] and len(listed["labs"]) == len(isaac_labs._labs())
    json.dumps([found, code_only, listed], allow_nan=False)


def test_lab_asset_files_exist_in_this_workspace():
    from pathlib import Path
    for lab in isaac_labs._labs().values():
        for step in lab["steps"]:
            asset = step["arguments"].get("asset_path")
            if asset and Path(isaac_labs._assets()).exists():
                assert Path(asset).is_file(), asset
