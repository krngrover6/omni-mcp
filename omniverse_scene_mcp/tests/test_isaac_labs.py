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
        assert lab["objectives"] or lab_id in isaac_labs.FOUNDATIONS or lab_id in isaac_labs.CLASS_LABS, lab_id
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


def test_class_session_labs_cover_sessions_6_to_15_and_are_returned_by_lab_id():
    labs = isaac_labs._labs()
    assert sorted(isaac_labs.CLASS_LABS.values()) == list(range(6, 16))  # one lab per class session
    listed = isaac_labs.lab()["class_labs"]
    for lab_id, session in isaac_labs.CLASS_LABS.items():
        lab = labs[lab_id]
        assert lab["class_session"] == session and lab["title"].startswith(f"Class session {session}:"), lab_id
        assert lab["objectives"] == [] and listed[lab_id] == f"class session {session}", lab_id
        assert any(step["kind"] == "ui" for step in lab["steps"]), (lab_id, "teach the clicks in Isaac too")
        found = isaac_labs.lab(lab_id=lab_id)
        assert found["ok"] and found["lab_id"] == lab_id and found["setup"] and found["how_to_run"], lab_id
        json.dumps(found, allow_nan=False)
    # Labs work in the study stage, each in its own group, never a course scene.
    for lab_id in isaac_labs.CLASS_LABS:
        for step in labs[lab_id]["steps"]:
            for key in ("path", "prim_path", "graph_path", "root", "parent", "robot_root", "parent_path"):
                value = step["arguments"].get(key)
                assert value is None or value == "/World" or value.startswith("/World/"), (lab_id, value)


def test_session_lab_scripts_and_live_facts():
    from omniverse_scene_mcp import script_check

    labs = isaac_labs._labs()
    assert script_check.check_script(isaac_labs.PY_READ)["ok"]
    for code in (isaac_labs.PY_READ, isaac_labs.PY_MANY_BOXES):
        checked = script_check.check_script(code)
        assert not [f for f in checked.get("findings", []) if f.get("severity") == "error"], checked
    buggy = json.dumps(script_check.check_script(isaac_labs.PY_BUGGY)).lower()
    assert "while true" in buggy or "sleep" in buggy
    # Session 12 shrinks the spawned boxes by changing the Script Node's code (graph_set_value resets the node).
    code = next(s["arguments"]["value"] for s in labs["warehouse_debug"]["steps"]
                if s["arguments"].get("pin") == "Spawner.inputs:script")
    assert "CreateSizeAttr(0.05)" in code and "CreateSizeAttr(0.2)" not in code
    # Proven live: pace 0.3, belt reversed through the conveyor's direction, unturned arm 0.6 m behind the curb.
    for lab_id in ("warehouse_setup", "factory_cell"):
        picks = [s for s in labs[lab_id]["steps"] if s["tool"] == "robot_live_pick"]
        assert picks and all(s["arguments"]["pace"] == 0.3 for s in picks), lab_id
    steps = {s["tool"]: s["arguments"] for s in labs["warehouse_setup"]["steps"] if s["kind"] != "ui"}
    assert steps["graph_apply"]["options"]["direction"] == [-1, 0, 0]
    franka = next(s["arguments"]["position"] for s in labs["warehouse_setup"]["steps"]
                  if s["tool"] == "robot_add_ready")
    curb = next(s["arguments"]["translation"] for s in labs["warehouse_setup"]["steps"]
                if s["tool"] == "scene_move" and s["arguments"]["path"].endswith("/Curb"))
    assert round(curb[0] + 0.1 - franka[0], 3) == 0.6 and curb[1] == franka[1]  # pick spot 0.6 m straight ahead


def test_sorting_labels_and_factory_cell_bin_match_what_the_tools_build():
    try:
        from pxr import Usd, UsdGeom
    except ImportError:  # pragma: no cover - pxr is an optional extra
        return
    from omniverse_scene_mcp import factory_cell, spawner

    def stage():
        made = Usd.Stage.CreateInMemory()
        UsdGeom.SetStageMetersPerUnit(made, 1)
        UsdGeom.SetStageUpAxis(made, UsdGeom.Tokens.z)
        for path in ("/World", "/World/Sort"):
            UsdGeom.Xform.Define(made, path)
        return made

    labs = isaac_labs._labs()
    spawn = next(s["arguments"] for s in labs["package_sorting"]["steps"] if s["tool"] == "scene_spawn_packages")
    preview = spawner.spawn_packages(stage(), **spawn)
    colours = {p["path"]: p["color"] for p in preview["would_create"]["packages"]}
    labels = next(s["arguments"]["mapping"] for s in labs["package_sorting"]["steps"] if s["tool"] == "semantic_labels")
    assert labels == colours, (labels, colours)
    cell = next(s["arguments"] for s in labs["factory_cell"]["steps"] if s["tool"] == "scene_build_factory_cell")
    # The layout alone, not build_cell: its preview also checks the Franka asset files, which exist on the laptop
    # but not on the CI runner (seen in CI run 48: this test failed there and passed locally).
    layout = factory_cell._layout(cell["origin"], cell["colours"])
    pick = next(s["arguments"] for s in labs["factory_cell"]["steps"] if s["tool"] == "robot_live_pick")
    low, high = layout["packages_region"]
    package = spawner.spawn_packages(stage(), f"{cell['root']}/Packages", 1, region_min=low, region_max=high,
                                     size_range=(0.05, 0.06), colors=cell["colours"],
                                     seed=3)["would_create"]["packages"][0]  # build_cell's default seed
    assert pick["object_path"] == package["path"]
    assert pick["place_position"][:2] == layout["bins"][package["color"]][:2], (pick, layout["bins"], package)


def test_lab_asset_files_exist_in_this_workspace():
    from pathlib import Path
    for lab in isaac_labs._labs().values():
        for step in lab["steps"]:
            asset = step["arguments"].get("asset_path")
            if asset and Path(isaac_labs._assets()).exists():
                assert Path(asset).is_file(), asset
