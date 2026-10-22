"""Project starter: every template plans and creates in a temp root, composes, and lands each prim in its layer.

Every test writes only inside its own tempfile.TemporaryDirectory (root= or an $OMNIVERSE_ROOT override), never in
the workspace. Tests that need USD (pxr) return early without it; the planning and refusal tests still run.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import runpy
import tempfile
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - planning/refusal tests still run; pxr tests return early
    Gf = Sdf = Usd = UsdGeom = UsdShade = None

from omniverse_scene_mcp import project_starter

TEMPLATES = ("blank", "warehouse", "digital_twin", "configurator", "robot_cell", "synthetic_data")
CONTEXT_HEADINGS = (
    "## Goal", "## Independence", "## Entry points", "## Assets and licensing", "## Stage map", "## Layer stack",
    "## Axes, units, and pivots", "## Materials and bindings", "## Animation", "## UI and MCP behavior",
    "## Validation checklist", "## Decisions and lessons", "## Current status and next steps",
)


@contextlib.contextmanager
def _temp_root():
    with tempfile.TemporaryDirectory(prefix="project_starter_", ignore_cleanup_errors=True) as tmp:
        yield Path(tmp).resolve()


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    """Every file and folder under root with (mtime_ns, size), to prove nothing changed."""
    state = {}
    for path in root.rglob("*"):
        info = path.stat()
        state[path.relative_to(root).as_posix()] = (info.st_mtime_ns, info.st_size if path.is_file() else -1)
    return state


def _create(root: Path, name: str, template: str, **kwargs):
    result = project_starter.create_project(name, template, root=root, dry_run=False, **kwargs)
    assert result["ok"], result
    return result


def _defining_layers(prim) -> list[str]:
    return [Path(spec.layer.realPath).name for spec in prim.GetPrimStack() if spec.specifier == Sdf.SpecifierDef]


# --------------------------------------------------------------------------------------------------- listing/planning


def test_list_templates_has_the_six_templates_with_units_and_layers():
    result = project_starter.list_templates()
    assert result["ok"] and result["count"] == len(TEMPLATES)
    by_name = {row["name"]: row for row in result["templates"]}
    assert set(by_name) == set(TEMPLATES)
    for row in by_name.values():
        assert row["up_axis"] in ("Y", "Z")
        assert row["units"] in row["unit_choices"]
        assert row["layers"][0] == "root.usda" and all(layer.startswith("layers/") for layer in row["layers"][1:])
        assert row["default_prim"] == "World"
    assert by_name["robot_cell"]["kilograms_per_unit"] == 1.0
    assert by_name["robot_cell"]["layers"][-1] == "layers/physics.usda"
    assert (by_name["configurator"]["up_axis"], by_name["configurator"]["units"]) == ("Y", "cm")
    assert set(by_name["digital_twin"]["unit_choices"]) == {"m", "mm"}
    json.dumps(result)


def test_plan_describes_every_template_and_writes_nothing():
    with _temp_root() as root:
        for template in TEMPLATES:
            plan = project_starter.plan_project(f"plan_{template}", template, root=root, goal="Learn layers")
            assert plan["ok"] and plan["dry_run"] is True, plan
            json.dumps(plan)
            assert plan["project_dir"] == str(root / f"plan_{template}")
            paths = {entry["path"]: entry["kind"] for entry in plan["files"]}
            assert paths["root.usda"] == "usd_root"
            for folder in ("assets/", "layers/", "scenes/", "renders/", "scripts/", "docs/"):
                assert paths[folder] == "folder"
            for name in ("PROJECT_CONTEXT.md", "README.md", ".gitignore", "scripts/inspect_scene.py"):
                assert name in paths
            stack = plan["layer_stack"]
            assert stack["order"][0] == "root.usda"
            assert [paths[layer] for layer in stack["order"][1:]] == ["usd_layer"] * (len(stack["order"]) - 1)
            assert [layer["strength"] for layer in stack["layers"]] == list(range(len(stack["order"])))
            assert "strongest" in stack["explanation"].lower() or "stronger" in stack["explanation"].lower()
            assert plan["starter_prims"] and plan["starter_prims"][0]["path"] == "/World"
            assert plan["goal"] == "Learn layers"
        assert list(root.iterdir()) == []


def test_create_previews_unless_dry_run_is_exactly_false():
    with _temp_root() as root:
        for flag in (True, None, 0, "false", "no"):
            result = project_starter.create_project("preview_only", "warehouse", root=root, dry_run=flag)
            assert result["ok"] and result["dry_run"] is True, (flag, result)
            assert "nothing was written" in result["message"]
        default = project_starter.create_project("preview_only", "warehouse", root=root)
        assert default["dry_run"] is True
        assert list(root.iterdir()) == []


def test_units_and_up_axis_choices_and_warnings():
    with _temp_root() as root:
        plan = project_starter.plan_project("twin_mm", "digital_twin", root=root, units="millimeters")
        assert plan["settings"]["units"] == "mm" and plan["settings"]["meters_per_unit"] == 0.001
        assert not any("usually" in warning for warning in plan["warnings"])  # mm is one of the twin's choices
        plan = project_starter.plan_project("cell_cm", "robot_cell", root=root, units="cm", up_axis="y")
        assert plan["settings"]["up_axis"] == "Y"
        assert any("meters" in warning for warning in plan["warnings"])
        assert any("Y-up" in warning for warning in plan["warnings"])
        assert project_starter.plan_project("x", "blank", root=root, units=0.01)["settings"]["units"] == "cm"
        for bad in ({"units": "inch"}, {"units": 0.3048}, {"units": True}, {"up_axis": "X"}):
            result = project_starter.plan_project("x", "blank", root=root, **bad)
            assert not result["ok"] and result["error"], bad
        result = project_starter.plan_project("x", "castle", root=root)
        assert not result["ok"] and "Unknown template" in result["error"]
        assert list(root.iterdir()) == []


# --------------------------------------------------------------------------------------------------- refusals


def test_refuses_bad_names_and_path_traversal():
    bad_names = [
        "", "   ", " padded", "..", ".", "../evil", "..\\evil", "a/b", "a\\b", "C:/evil", "C:evil", "/abs", "\\\\server\\x",
        "con", "NUL", "com1", "Lpt9", "aux", "name.with.dot", "has space", "-starts-with-dash", "_underscore_first",
        "x" * 65, "tab\tname", "new\nline", None, 123,
    ]
    with _temp_root() as base:
        root = base / "root"
        root.mkdir()
        for name in bad_names:
            for call in (project_starter.plan_project, project_starter.create_project):
                kwargs = {"dry_run": False} if call is project_starter.create_project else {}
                result = call(name, "blank", root=root, **kwargs)
                assert result["ok"] is False and result["error"], (name, result)
        assert list(root.iterdir()) == []
        assert sorted(path.name for path in base.iterdir()) == ["root"]
        assert project_starter.plan_project("x" * 64, "blank", root=root)["ok"]


def test_refuses_an_existing_folder_or_file_and_leaves_it_alone():
    with _temp_root() as root:
        existing = root / "my_project"
        existing.mkdir()
        keep = existing / "notes.txt"
        keep.write_text("mine", encoding="utf-8")
        (root / "a_file").write_text("also mine", encoding="utf-8")
        before = _tree(root)
        for name in ("my_project", "MY_PROJECT" if os.name == "nt" else "my_project", "a_file"):
            plan = project_starter.plan_project(name, "warehouse", root=root)
            assert not plan["ok"] and "already exists" in plan["error"], plan
            created = project_starter.create_project(name, "warehouse", root=root, dry_run=False)
            assert not created["ok"] and "already exists" in created["error"], created
        assert _tree(root) == before
        assert keep.read_text(encoding="utf-8") == "mine"


def test_refuses_bad_roots():
    with _temp_root() as root:
        a_file = root / "file.txt"
        a_file.write_text("x", encoding="utf-8")
        for bad_root in ("relative/folder", str(root / "missing"), str(a_file), "\\\\server\\share", Path(root.anchor)):
            result = project_starter.create_project("ok_name", "blank", root=bad_root, dry_run=False)
            assert result["ok"] is False and result["error"], (bad_root, result)
        assert sorted(path.name for path in root.iterdir()) == ["file.txt"]
        listed = project_starter.list_projects(str(root / "missing"))
        assert listed["ok"] is False and listed["projects"] == []


def test_env_override_sets_the_default_root():
    with _temp_root() as root:
        previous = os.environ.get("OMNIVERSE_ROOT")
        os.environ["OMNIVERSE_ROOT"] = str(root)
        try:
            plan = project_starter.plan_project("env_project", "blank")
            assert plan["ok"] and plan["project_dir"] == str(root / "env_project")
            assert plan["warnings"] == []
            if Usd is None:
                return
            created = project_starter.create_project("env_project", "blank", dry_run=False)
            assert created["ok"], created
            assert (root / "env_project" / "root.usda").is_file()
            assert project_starter.list_projects()["count"] == 1
        finally:
            if previous is None:
                os.environ.pop("OMNIVERSE_ROOT", None)
            else:
                os.environ["OMNIVERSE_ROOT"] = previous


def test_outside_workspace_root_is_allowed_with_a_warning():
    with _temp_root() as root:
        plan = project_starter.plan_project("outside", "blank", root=root)
        assert plan["ok"]
        assert any("outside the Omniverse workspace" in warning for warning in plan["warnings"])


# --------------------------------------------------------------------------------------------------- creating (pxr)


def test_every_template_creates_a_composing_project_with_prims_in_their_layers():
    if Usd is None:
        return
    templates = {row["name"]: row for row in project_starter.list_templates()["templates"]}
    spec_by_name = project_starter._templates()
    with _temp_root() as root:
        for name in TEMPLATES:
            result = _create(root, f"proj_{name}", name)
            project = root / f"proj_{name}"
            assert result["dry_run"] is False
            assert result["scene_entry"] == str(project / "root.usda")
            assert result["open_in"] == f"Composer: File > Open {project / 'root.usda'}"
            assert result["validation"]["ok"], result["validation"]
            assert result["validation"]["lint"]["counts"]["error"] == 0
            assert len(result["next_steps"]) >= 4 and "File > Open" in result["next_steps"][0]
            json.dumps(result)

            on_disk = {path.relative_to(project).as_posix() + ("/" if path.is_dir() else "") for path in project.rglob("*")}
            assert on_disk == set(result["created"]), on_disk ^ set(result["created"])

            stage = Usd.Stage.Open(str(project / "root.usda"))
            row = templates[name]
            assert UsdGeom.GetStageUpAxis(stage) == row["up_axis"]
            assert Gf.IsClose(UsdGeom.GetStageMetersPerUnit(stage), row["meters_per_unit"], 1e-12)
            assert str(stage.GetDefaultPrim().GetPath()) == "/World"
            if row["kilograms_per_unit"] is not None:
                assert stage.GetRootLayer().pseudoRoot.GetInfo("kilogramsPerUnit") == row["kilograms_per_unit"]
            assert not stage.GetCompositionErrors()

            root_layer = stage.GetRootLayer()
            assert list(root_layer.rootPrims) == [], "the root layer must hold no prims"
            assert list(root_layer.subLayerPaths) == [f"./{layer}" for layer in row["layers"][1:]]
            assert [Path(layer.realPath).name for layer in stage.GetLayerStack(False)] == [
                Path(layer).name for layer in row["layers"]
            ]
            for layer_path in row["layers"][1:]:
                layer = Sdf.Layer.FindOrOpen(str(project / layer_path))
                assert layer.pseudoRoot.GetInfo("upAxis") == row["up_axis"]

            for entry in spec_by_name[name]["prims"]:
                prim = stage.GetPrimAtPath(entry["path"])
                assert prim.IsDefined(), (name, entry["path"])
                intended = f"{entry['layer']}.usda"
                layer = Sdf.Layer.FindOrOpen(str(project / "layers" / intended))
                spec = layer.GetPrimAtPath(entry["path"])
                assert spec is not None, (name, entry["path"], intended)
                if entry.get("type"):
                    assert _defining_layers(prim) == [intended], (name, entry["path"], _defining_layers(prim))
                    assert prim.GetTypeName() == entry["type"]
                else:
                    assert spec.specifier == Sdf.SpecifierOver, (name, entry["path"])
                if entry.get("kind"):
                    assert Usd.ModelAPI(prim).GetKind() == entry["kind"]
                if entry.get("bind"):
                    material = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()[0]
                    assert str(material.GetPath()) == entry["bind"]
                    assert "MaterialBindingAPI" in prim.GetAppliedSchemas()


def test_project_context_readme_gitignore_and_goal():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "ctx_demo", "robot_cell", goal="Pick a cube with a Franka arm.")
        project = root / "ctx_demo"
        context = (project / "PROJECT_CONTEXT.md").read_text(encoding="utf-8")
        for heading in CONTEXT_HEADINGS:
            assert f"\n{heading}\n" in context, heading
        assert "Pick a cube with a Franka arm." in context
        assert "- Main scene: `root.usda`" in context and "- Template: `robot_cell`" in context
        assert "`/World/PhysicsScene`" in context and "physics.usda" in context
        assert "- Kilograms per unit: 1" in context
        assert "D:\\Omniverse" not in context
        readme = (project / "README.md").read_text(encoding="utf-8")
        assert "Pick a cube with a Franka arm." in readme and "Set Authoring Layer" in readme
        ignore = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "renders/*" in ignore and "!renders/.gitkeep" in ignore and "__pycache__/" in ignore
        assert (project / "renders" / ".gitkeep").is_file()
        layer_doc = (project / "docs" / "layer_stack.md").read_text(encoding="utf-8")
        assert "layers/physics.usda" in layer_doc and "Why this order" in layer_doc


def test_units_scale_sizes_positions_and_gravity():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "twin_mm", "digital_twin", units="mm")
        stage = Usd.Stage.Open(str(root / "twin_mm" / "root.usda"))
        assert Gf.IsClose(UsdGeom.GetStageMetersPerUnit(stage), 0.001, 1e-12)
        station = stage.GetPrimAtPath("/World/Factory/Line_01/Station_01")
        assert Gf.IsClose(station.GetAttribute("xformOp:translate").Get(), Gf.Vec3d(0, 0, 750), 1e-6)
        assert stage.GetPrimAtPath("/World/Factory/Line_01/Station_01/Geom").GetAttribute("size").Get() == 1500
        camera = UsdGeom.Camera(stage.GetPrimAtPath("/World/Cameras/Overview"))
        assert Gf.IsClose(camera.GetClippingRangeAttr().Get(), Gf.Vec2f(10, 10_000_000), 1e-3)

        _create(root, "config_cm", "configurator")
        stage = Usd.Stage.Open(str(root / "config_cm" / "root.usda"))
        assert UsdGeom.GetStageUpAxis(stage) == "Y"
        body = stage.GetPrimAtPath("/World/Product/Body")
        assert Gf.IsClose(body.GetAttribute("xformOp:translate").Get(), Gf.Vec3d(0, 25, 0), 1e-6)
        assert body.GetAttribute("size").Get() == 50
        low, high = stage.GetPrimAtPath("/World/Environment/Floor").GetAttribute("extent").Get()
        assert low[1] == high[1] == 0 and high[0] == 300 and high[2] == 300  # the floor lies in the XZ plane

        result = _create(root, "cell_cm", "robot_cell", units="cm", up_axis="Y")
        assert result["warnings"]
        stage = Usd.Stage.Open(str(root / "cell_cm" / "root.usda"))
        scene = stage.GetPrimAtPath("/World/PhysicsScene")
        assert Gf.IsClose(scene.GetAttribute("physics:gravityMagnitude").Get(), 981.0, 1e-3)
        assert scene.GetAttribute("physics:gravityDirection").Get() == Gf.Vec3f(0, -1, 0)


def test_cameras_and_sun_point_where_the_template_says():
    if Usd is None:
        return
    with _temp_root() as root:
        for name, up_axis, units in (("cam_z", "Z", "m"), ("cam_y", "Y", "cm")):
            _create(root, name, "warehouse", up_axis=up_axis, units=units)
            stage = Usd.Stage.Open(str(root / name / "root.usda"))
            up = Gf.Vec3d(0, 0, 1) if up_axis == "Z" else Gf.Vec3d(0, 1, 0)
            camera = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Cameras/Overview"))
            matrix = camera.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            eye = matrix.ExtractTranslation()
            forward = matrix.TransformDir(Gf.Vec3d(0, 0, -1)).GetNormalized()
            to_target = (Gf.Vec3d(0, 0, 0) - eye).GetNormalized()
            assert Gf.Dot(forward, to_target) > 0.9999, (name, forward, to_target)
            assert Gf.Dot(matrix.TransformDir(Gf.Vec3d(0, 1, 0)), up) > 0, "camera must not be upside down"
            sun = UsdGeom.Xformable(stage.GetPrimAtPath("/World/Lights/Sun")).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            assert Gf.Dot(sun.TransformDir(Gf.Vec3d(0, 0, -1)), up) < -0.5, "the sun must shine downwards"


def test_configurator_variants_switch_the_binding_and_root_layer_selection_wins():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "shop", "configurator")
        stage = Usd.Stage.Open(str(root / "shop" / "root.usda"))
        product = stage.GetPrimAtPath("/World/Product")
        body = stage.GetPrimAtPath("/World/Product/Body")
        color = product.GetVariantSets().GetVariantSet("color")
        assert sorted(color.GetVariantNames()) == ["blue", "red", "silver"]
        assert color.GetVariantSelection() == "red"
        assert str(UsdShade.MaterialBindingAPI(body).ComputeBoundMaterial()[0].GetPath()) == "/World/Looks/Red"
        stage.SetEditTarget(stage.GetSessionLayer())  # in memory only: nothing is saved
        color.SetVariantSelection("blue")
        assert str(UsdShade.MaterialBindingAPI(body).ComputeBoundMaterial()[0].GetPath()) == "/World/Looks/Blue"
        assert not any(layer.dirty for layer in stage.GetLayerStack(False))


def test_digital_twin_live_data_layer_wins_over_layout():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "twin", "digital_twin")
        stage = Usd.Stage.Open(str(root / "twin" / "root.usda"))
        status = stage.GetPrimAtPath("/World/Factory/Line_01/Station_01").GetAttribute("twin:status")
        assert status.Get() == "idle"
        assert [Path(spec.layer.realPath).name for spec in status.GetPropertyStack(Usd.TimeCode.Default())] == [
            "live_data.usda", "layout.usda"
        ]
        assert status.IsCustom()


def test_robot_cell_physics_layer_adds_physics_on_layout_prims():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "cell", "robot_cell")
        stage = Usd.Stage.Open(str(root / "cell" / "root.usda"))
        scene = stage.GetPrimAtPath("/World/PhysicsScene")
        assert scene.GetTypeName() == "PhysicsScene" and _defining_layers(scene) == ["physics.usda"]
        assert Gf.IsClose(scene.GetAttribute("physics:gravityMagnitude").Get(), 9.81, 1e-5)
        ground = stage.GetPrimAtPath("/World/Environment/GroundPlane")
        assert "PhysicsCollisionAPI" in ground.GetAppliedSchemas() and _defining_layers(ground) == ["layout.usda"]
        workpiece = stage.GetPrimAtPath("/World/Workcell/Workpiece")
        assert {"PhysicsRigidBodyAPI", "PhysicsMassAPI"} <= set(workpiece.GetAppliedSchemas())
        assert workpiece.GetAttribute("physics:mass").Get() == 0.5
        assert "PhysicsCollisionAPI" in stage.GetPrimAtPath("/World/Workcell/Workpiece/Geom").GetAppliedSchemas()


def test_synthetic_data_target_has_a_semantic_label():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "sdg", "synthetic_data")
        stage = Usd.Stage.Open(str(root / "sdg" / "root.usda"))
        target = stage.GetPrimAtPath("/World/Props/Target")
        assert "SemanticsLabelsAPI:class" in target.GetAppliedSchemas()
        assert list(target.GetAttribute("semantics:labels:class").Get()) == ["target"]
        layer = Sdf.Layer.FindOrOpen(str(root / "sdg" / "layers" / "randomization.usda"))
        assert layer.GetPrimAtPath("/World/Props").specifier == Sdf.SpecifierOver


def test_generated_inspect_script_runs_read_only():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "inspect_me", "robot_cell")
        project = root / "inspect_me"
        before = _tree(project)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            runpy.run_path(str(project / "scripts" / "inspect_scene.py"), run_name="__main__")
        text = output.getvalue()
        assert "Up axis: Z" in text and "Default prim: /World" in text
        assert "5. physics.usda" in text
        assert "/World/PhysicsScene  (PhysicsScene)  defined in physics.usda" in text
        assert _tree(project) == before


def test_failed_check_or_write_rolls_back_only_what_it_created():
    if Usd is None:
        return
    with _temp_root() as root:
        (root / "neighbor").mkdir()
        before = _tree(root)
        original_verify = project_starter._verify
        project_starter._verify = lambda plan: {"ok": False, "checks": [{"check": "fake", "ok": False, "detail": "forced"}]}
        try:
            result = project_starter.create_project("rolled", "warehouse", root=root, dry_run=False)
        finally:
            project_starter._verify = original_verify
        assert not result["ok"] and "forced" in result["error"] and result["rolled_back"]
        assert _tree(root) == before

        original_texts = project_starter._build_texts
        project_starter._build_texts = lambda plan: {**original_texts(plan), "no_such_folder/x.md": "x"}
        try:
            result = project_starter.create_project("rolled", "warehouse", root=root, dry_run=False)
        finally:
            project_starter._build_texts = original_texts
        assert not result["ok"] and result["created"] == []
        assert _tree(root) == before


def test_list_projects_reads_goal_scene_entry_and_template_without_writing():
    if Usd is None:
        return
    with _temp_root() as root:
        _create(root, "alpha", "warehouse", goal="Study racks.")
        _create(root, "beta", "blank")
        (root / "not_a_project").mkdir()
        hand = root / "hand_made"
        hand.mkdir()
        (hand / "PROJECT_CONTEXT.md").write_text(
            "# Project Context: hand\n\n## Goal\n\nA hand-written\nproject.\n\nMore text.\n\n## Entry points\n\n"
            "- Main scene: `scenes/main.usda`\n",
            encoding="utf-8",
        )
        before = _tree(root)
        listed = project_starter.list_projects(root)
        assert _tree(root) == before
        assert listed["ok"] and listed["count"] == 3
        json.dumps(listed)
        by_name = {project["name"]: project for project in listed["projects"]}
        assert set(by_name) == {"alpha", "beta", "hand_made"}
        assert by_name["alpha"]["goal"] == "Study racks."
        assert by_name["alpha"]["template"] == "warehouse"
        assert by_name["alpha"]["scene_entry"] == "root.usda" and by_name["alpha"]["scene_entry_exists"]
        assert by_name["alpha"]["last_modified"]
        assert by_name["hand_made"]["goal"] == "A hand-written project."
        assert by_name["hand_made"]["scene_entry"] == "scenes/main.usda"
        assert by_name["hand_made"]["scene_entry_exists"] is False
        assert by_name["hand_made"]["template"] == ""
