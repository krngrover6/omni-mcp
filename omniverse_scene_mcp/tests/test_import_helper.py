"""import_helper: wrapper layers for units/up axis, unit detection, import plans and a FAKE Kit converter.

Fixture-free test_* functions using temporary folders only. Fake omni modules go into sys.modules for one test and
the previous entries are restored; inside a real Kit process the fake-converter tests return at once.
The smoke test only READS one course asset (if present) and checks it was not modified.
"""

import contextlib
import os
import sys
import tempfile
import types
from collections import namedtuple
from pathlib import Path

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import import_helper as ih

_IN_KIT = "carb" in sys.modules or "omni.kit.app" in sys.modules
_FAKE_NAMES = (
    "omni.kit.asset_converter",
    "omni.kit.converter.hoops_core",
    "omni.kit.converter.jt_core",
    "omni.kit.converter.dgn_core",
    "omni.kit.async_engine",
)
_MISSING = object()
_COURSE = Path(__file__).resolve().parents[2] / "digital_twin_physical_ai_course" / "course_assets"


# ------------------------------------------------------------------------------------------------------ helpers


@contextlib.contextmanager
def _tmp():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
        yield os.path.realpath(folder)


def _author_asset(path, *, mpu=0.01, up="Y", size=(50.0, 80.0, 50.0), root="World", default_prim=True,
                  root_translate=None, fmt="usda", authored_metrics=True, extra_root=None):
    """A box-shaped 'chair' standing on the ground of its own up axis, sized in its own units."""
    stage = Usd.Stage.CreateInMemory()
    if authored_metrics:
        UsdGeom.SetStageMetersPerUnit(stage, mpu)
        UsdGeom.SetStageUpAxis(stage, up)
    world = UsdGeom.Xform.Define(stage, f"/{root}")
    if root_translate is not None:
        world.AddTranslateOp().Set(tuple(root_translate))
    cube = UsdGeom.Cube.Define(stage, f"/{root}/Seat")
    cube.CreateSizeAttr(1.0)
    cube.CreateExtentAttr([(-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)])
    sx, sy, sz = size
    lift = (0.0, sy / 2.0, 0.0) if up == "Y" else (0.0, 0.0, sz / 2.0)
    cube.AddTranslateOp().Set(lift)
    cube.AddScaleOp().Set((sx, sy, sz))
    if extra_root:
        UsdGeom.Xform.Define(stage, f"/{extra_root}")
    if default_prim:
        stage.SetDefaultPrim(world.GetPrim())
    stage.GetRootLayer().Export(os.fspath(path), args={"format": fmt} if str(path).endswith(".usd") else {})
    return os.fspath(path)


def _bbox(path):
    stage = Usd.Stage.Open(os.fspath(path))
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render])
    box = cache.ComputeWorldBound(stage.GetDefaultPrim()).ComputeAlignedRange()
    result = ([float(v) for v in box.GetMin()], [float(v) for v in box.GetMax()],
              str(UsdGeom.GetStageUpAxis(stage)), float(UsdGeom.GetStageMetersPerUnit(stage)))
    del stage
    return result


def _close(values, expected, tol=1e-4):
    assert len(values) == len(expected), (values, expected)
    for got, want in zip(values, expected):
        assert abs(got - want) <= tol * max(1.0, abs(want)), (values, expected)


def _make_project(folder, *, mpu=0.001, up="Z", layout=True):
    project = os.path.join(folder, "factory_proj")
    os.makedirs(os.path.join(project, "assets"))
    os.makedirs(os.path.join(project, "layers"))
    root = Sdf.Layer.CreateAnonymous(".usda")
    root.pseudoRoot.SetInfo("metersPerUnit", mpu)
    root.pseudoRoot.SetInfo("upAxis", up)
    root.Export(os.path.join(project, "root.usda"))
    if layout:
        Sdf.Layer.CreateAnonymous(".usda").Export(os.path.join(project, "layers", "layout.usda"))
    return project


def _write(path, data=b"fake source bytes"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


@contextlib.contextmanager
def _fake_modules(**modules):
    """Install fake modules (key: dotted name with '__' for '.'); the other converter names fail to import."""
    saved = {name: sys.modules.get(name, _MISSING) for name in _FAKE_NAMES}
    try:
        for name in _FAKE_NAMES:
            sys.modules[name] = None
        for key, module in modules.items():
            sys.modules[key.replace("__", ".")] = module
        yield
    finally:
        for name, module in saved.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _fake_asset_converter(*, succeed=True, write_output=True, partial=False, error="boom"):
    module = types.ModuleType("omni.kit.asset_converter")
    calls = []

    class AssetConverterContext:
        def __init__(self):
            for key, value in ih.ASSET_CONVERTER_OPTIONS.items():
                setattr(self, key, value)

    class Task:
        def __init__(self, output, progress):
            self.output, self.progress, self.status, self.message = output, progress, 0, ""

        async def wait_until_finished(self):
            if partial:
                _write(self.output, b"partial")
            if not succeed:
                self.status, self.message = 3, error
                return False
            if write_output:
                _author_asset(self.output, mpu=0.01, up="Y")  # like an FBX: centimetres, Y-up
            if self.progress:
                self.progress(1, 1)
            return True

        def get_status(self):
            return self.status

        def get_error_message(self):
            return self.message

    class Instance:
        def create_converter_task(self, import_path, output_path, progress_callback=None,
                                  asset_converter_context=None, material_loader=None,
                                  close_stage_and_reopen_if_opened=False):
            calls.append({"import_path": import_path, "output_path": output_path,
                          "options": {key: getattr(asset_converter_context, key) for key in ih.ASSET_CONVERTER_OPTIONS},
                          "close_stage": close_stage_and_reopen_if_opened})
            return Task(output_path, progress_callback)

    instance = Instance()
    module.AssetConverterContext = AssetConverterContext
    module.get_instance = lambda: instance
    module.calls = calls
    return module


# ------------------------------------------------------------------------------------------------- wrap_units


def test_wrap_units_cm_to_m_scales_composed_bbox():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"), mpu=0.01, up="Y")
        wrapper = os.path.join(folder, "chair_wrapped.usda")
        result = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Y", dry_run=False)
        assert result["ok"] and result["written"], result
        assert result["scale"] == 0.01 and result["rotate_x"] == 0.0
        assert "source is centimetres (0.01), project is metres (1) -> scale 0.01" in result["explanation"]
        low, high, up, mpu = _bbox(wrapper)
        assert up == "Y" and mpu == 1.0
        _close(low, [-0.25, 0.0, -0.25])
        _close(high, [0.25, 0.8, 0.25])
        assert result["verify"]["ok"], result["verify"]


def test_wrap_units_mm_to_m():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "bin.usda"), mpu=0.001, up="Z", size=(600.0, 400.0, 300.0))
        wrapper = os.path.join(folder, "bin_wrapped.usda")
        result = ih.wrap_units(src, wrapper, target_mpu=1.0, target_up="Z", dry_run=False)
        assert result["ok"] and result["scale"] == 0.001 and result["rotate_x"] == 0.0
        assert result["xform_op_order"] == [ih.SCALE_OP]
        low, high, _, _ = _bbox(wrapper)
        _close(low, [-0.3, -0.2, 0.0])
        _close(high, [0.3, 0.2, 0.3])


def test_wrap_units_y_up_to_z_up_rotates_plus_90():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"), mpu=1.0, up="Y", size=(0.5, 0.8, 0.4))
        wrapper = os.path.join(folder, "chair_wrapped.usda")
        result = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Z", dry_run=False)
        assert result["ok"] and result["scale"] == 1.0 and result["rotate_x"] == 90.0
        assert result["xform_op_order"] == [ih.ROTATE_OP]
        low, high, up, _ = _bbox(wrapper)
        assert up == "Z"
        _close(low, [-0.25, -0.2, 0.0])  # the 0.8 m height now runs along Z, standing on the ground
        _close(high, [0.25, 0.2, 0.8])


def test_wrap_units_z_up_to_y_up_rotates_minus_90():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "lamp.usda"), mpu=1.0, up="Z", size=(0.3, 0.3, 1.5))
        wrapper = os.path.join(folder, "lamp_wrapped.usda")
        result = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Y", dry_run=False)
        assert result["ok"] and result["rotate_x"] == -90.0
        low, high, up, _ = _bbox(wrapper)
        assert up == "Y"
        _close(low, [-0.15, 0.0, -0.15])
        _close(high, [0.15, 1.5, 0.15])


def test_wrap_units_cm_y_up_into_course_mm_z_up():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"), mpu=0.01, up="Y")
        wrapper = os.path.join(folder, "chair_wrapped.usda")
        result = ih.wrap_units(src, wrapper, target_mpu="mm", target_up="Z", dry_run=False)
        assert result["ok"] and result["scale"] == 10.0 and result["rotate_x"] == 90.0
        assert "project is millimetres (0.001) -> scale 10" in result["explanation"]
        low, high, up, mpu = _bbox(wrapper)
        assert up == "Z" and mpu == 0.001
        _close(low, [-250.0, -250.0, 0.0])
        _close(high, [250.0, 250.0, 800.0])
        assert result["verify"]["ok"]
        _close(result["verify"]["composed_size_m"], [0.5, 0.5, 0.8])
        layer = Sdf.Layer.FindOrOpen(wrapper)
        prim = layer.GetPrimAtPath("/chair")
        assert layer.defaultPrim == "chair" and prim.GetInfo("kind") == "component"


def test_wrap_units_keeps_source_root_xform_ops():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"), mpu=0.01, up="Y", root_translate=(100.0, 0.0, 0.0))
        wrapper = os.path.join(folder, "chair_wrapped.usda")
        result = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Y", dry_run=False)
        assert result["kept_source_xform_ops"] == ["xformOp:translate"]
        assert result["xform_op_order"] == [ih.SCALE_OP, "xformOp:translate"]
        low, high, _, _ = _bbox(wrapper)
        _close(low, [0.75, 0.0, -0.25])  # the 100 cm root offset survives as 1 m
        _close(high, [1.25, 0.8, 0.25])
        assert result["verify"]["ok"], result["verify"]


def test_wrap_units_reference_is_relative():
    with _tmp() as folder:
        os.makedirs(os.path.join(folder, "converted"))
        os.makedirs(os.path.join(folder, "wrappers"))
        src = _author_asset(os.path.join(folder, "converted", "chair.usda"))
        same = ih.wrap_units(src, os.path.join(folder, "converted", "chair_wrapped.usda"), target_mpu="m",
                             target_up="Y", dry_run=False)
        assert same["reference"] == "./chair.usda" and same["reference_is_relative"]
        other = ih.wrap_units(src, os.path.join(folder, "wrappers", "chair_wrapped.usda"), target_mpu="m",
                              target_up="Y", dry_run=False)
        assert other["reference"] == "../converted/chair.usda"
        layer = Sdf.Layer.FindOrOpen(os.path.join(folder, "wrappers", "chair_wrapped.usda"))
        refs = layer.GetPrimAtPath("/chair").referenceList.prependedItems
        assert [ref.assetPath for ref in refs] == ["../converted/chair.usda"]
        assert other["verify"]["ok"]


def test_wrap_units_refuses_overwrite_unless_asked():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"))
        wrapper = os.path.join(folder, "chair_wrapped.usda")
        _write(wrapper, b"#usda 1.0\n")
        before = Path(wrapper).read_bytes()
        refused = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Z", dry_run=False)
        assert not refused["ok"] and "already exists" in refused["error"]
        assert Path(wrapper).read_bytes() == before
        replaced = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Z", dry_run=False, overwrite=True)
        assert replaced["ok"] and replaced["replaced_existing"]
        assert Path(wrapper).read_bytes() != before


def test_wrap_units_dry_run_writes_nothing_and_previews_usda():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"))
        wrapper = os.path.join(folder, "chair_wrapped.usda")
        result = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Z")
        assert result["ok"] and result["dry_run"] and not os.path.exists(wrapper)
        assert "xformOp:rotateX:unitsResolve = 90" in result["usda"]
        assert 'kind = "component"' in result["usda"]
        assert "metersPerUnit = 1" in result["usda"] and 'upAxis = "Z"' in result["usda"]
        _close(result["expected_bbox"]["size"], [0.5, 0.5, 0.8])


def test_wrap_units_unauthored_units_need_source_mpu():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"), authored_metrics=False)
        wrapper = os.path.join(folder, "chair_wrapped.usda")
        missing = ih.wrap_units(src, wrapper, target_mpu="m", target_up="Y")
        assert not missing["ok"] and "source_mpu" in missing["error"]
        given = ih.wrap_units(src, wrapper, source_mpu="cm", target_mpu="m", target_up="Z", dry_run=False)
        assert given["ok"] and given["scale"] == 0.01 and given["rotate_x"] == 90.0
        assert any("no upAxis" in warning for warning in given["warnings"])


def test_wrap_units_default_prim_handling():
    with _tmp() as folder:
        single = _author_asset(os.path.join(folder, "one.usda"), default_prim=False)
        result = ih.wrap_units(single, os.path.join(folder, "one_wrapped.usda"), target_mpu="m", target_up="Y",
                               dry_run=False)
        assert result["ok"] and result["verify"]["ok"], result
        assert "</World>" in result["usda"]
        multi = _author_asset(os.path.join(folder, "two.usda"), default_prim=False, extra_root="Other")
        refused = ih.wrap_units(multi, os.path.join(folder, "two_wrapped.usda"), target_mpu="m", target_up="Y")
        assert not refused["ok"] and "defaultPrim" in refused["error"]


def test_wrap_units_input_validation():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"))
        assert not ih.wrap_units(src, src, target_mpu="m", target_up="Y")["ok"]
        assert not ih.wrap_units(src, os.path.join(folder, "w.txt"), target_mpu="m", target_up="Y")["ok"]
        assert not ih.wrap_units(src, os.path.join(folder, "nope", "w.usda"), target_mpu="m", target_up="Y")["ok"]
        assert not ih.wrap_units(os.path.join(folder, "missing.usda"), os.path.join(folder, "w.usda"),
                                 target_mpu="m", target_up="Y")["ok"]
        assert "up axes" in ih.wrap_units(src, os.path.join(folder, "w.usda"), target_mpu="m",
                                          target_up="X")["error"]
        assert not ih.wrap_units(src, os.path.join(folder, "w.usda"), target_mpu="furlong", target_up="Y")["ok"]
        assert not ih.wrap_units(src, os.path.join(folder, "w.usda"), target_mpu=-1, target_up="Y")["ok"]
        assert not os.path.exists(os.path.join(folder, "w.usda"))


# ------------------------------------------------------------------------------------------------ detect_units


def test_detect_units_chair_80_units_unauthored_is_centimetres():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "office_chair.usda"), authored_metrics=False)
        result = ih.detect_units(src, workspace_root=folder)
        assert result["ok"], result
        assert not result["meters_per_unit_authored"] and result["heuristic"]["hint"] == "chair"
        assert result["heuristic"]["suggested_units"] == "cm"
        assert result["suggested_meters_per_unit"] == 0.01 and result["confidence"] == "medium"
        assert result["bbox"]["largest"] == 80.0 and result["bbox"]["largest_axis"] == "Y"


def test_detect_units_authored_and_plausible_is_high_confidence():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"), mpu=0.01, up="Y")
        result = ih.detect_units(src, workspace_root=folder)
        assert result["ok"] and result["meters_per_unit_authored"] and result["up_axis_authored"]
        assert result["confidence"] == "high" and result["metadata_matches_size"] is True
        assert result["suggested_meters_per_unit"] == 0.01
        _close(result["size_m"], [0.5, 0.8, 0.5])


def test_detect_units_metadata_contradicting_size_warns():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "chair.usda"), mpu=1.0, up="Y")  # an 80 m chair
        result = ih.detect_units(src, workspace_root=folder)
        assert result["ok"] and result["confidence"] == "medium"
        assert result["metadata_matches_size"] is False
        assert result["heuristic"]["suggested_units"] == "cm"
        assert any("80 m" in warning and "chair" in warning for warning in result["warnings"])


def test_detect_units_without_hint_is_low_confidence_and_name_hint_helps():
    with _tmp() as folder:
        src = _author_asset(os.path.join(folder, "thing.usda"), authored_metrics=False, size=(1500.0, 900.0, 600.0))
        plain = ih.detect_units(src, workspace_root=folder)
        assert plain["ok"] and plain["heuristic"]["hint"] is None and plain["confidence"] == "low"
        hinted = ih.detect_units(src, workspace_root=folder, name_hint="WorkTable")
        assert hinted["heuristic"]["hint"] == "table" and hinted["heuristic"]["suggested_units"] == "mm"
        assert hinted["confidence"] == "medium"


def test_detect_units_errors_are_reported():
    with _tmp() as folder:
        result = ih.detect_units(os.path.join(folder, "missing.usda"), workspace_root=folder)
        assert not result["ok"] and "does not exist" in result["error"]
        outside = ih.detect_units(os.path.join(folder, "x.usda"), workspace_root=os.path.join(folder, "sub"))
        assert not outside["ok"]


# ------------------------------------------------------------------------------------------------- plan_import


def test_plan_import_paths_units_and_placement():
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "downloads", "Office Chair-01.fbx"))
        plan = ih.plan_import(source, project_dir=project)
        assert plan["ok"] and plan["dry_run"], plan
        assert plan["converter"] == "omni.kit.asset_converter"
        assert plan["alternatives"] == ["omni.kit.converter.hoops_core"]
        assert plan["name"] == "Office_Chair_01"
        assert plan["output_path"] == os.path.join(project, "assets", "Office_Chair_01", "Office_Chair_01.usd")
        assert plan["wrapper"]["path"] == os.path.join(project, "assets", "Office_Chair_01",
                                                       "Office_Chair_01_wrapped.usda")
        assert plan["wrapper"]["references"] == "./Office_Chair_01.usd"
        assert plan["target"]["meters_per_unit"] == 0.001 and plan["target"]["up_axis"] == "Z"
        assert "root.usda" in plan["target"]["from"]
        assert plan["wrapper"]["expected_scale"] == 10.0 and plan["wrapper"]["expected_rotate_x"] == 90.0
        place = plan["how_to_place"]
        assert place["prim_path"] == "/World/Office_Chair_01" and place["layer_exists"]
        assert place["asset_path"] == "../assets/Office_Chair_01/Office_Chair_01_wrapped.usda"
        assert "AddReference" in place["python"] and "SetInstanceable" not in place["python"]
        assert any("spaces" in warning for warning in plan["warnings"])
        assert not os.path.exists(os.path.join(project, "assets", "Office_Chair_01"))  # plan writes nothing


def test_plan_import_gltf_factor_explained_and_options():
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "crate.glb"))
        plan = ih.plan_import(source, project_dir=project, name="crate_a", instanceable=True, as_reference=False)
        assert plan["ok"] and plan["expected_source"]["certain"]
        assert plan["wrapper"]["expected_scale"] == 1000.0
        assert "source is metres (1), project is millimetres (0.001) -> scale 1000" in plan["wrapper"]["explanation"]
        assert plan["how_to_place"]["arc"] == "payload" and "AddPayload" in plan["how_to_place"]["python"]
        assert "SetInstanceable(True)" in plan["how_to_place"]["python"]
        explicit = ih.plan_import(source, project_dir=project, target_units="m", up_axis="y")
        assert explicit["target"]["meters_per_unit"] == 1.0 and explicit["target"]["up_axis"] == "Y"
        assert explicit["wrapper"]["expected_scale"] == 1.0 and explicit["wrapper"]["expected_rotate_x"] == 0.0


def test_plan_import_cad_converters():
    with _tmp() as folder:
        project = _make_project(folder)
        cases = {"bracket.STEP": "omni.kit.converter.hoops_core", "line.jt": "omni.kit.converter.jt_core",
                 "plant.dgn": "omni.kit.converter.dgn_core", "frame.prt.3": "omni.kit.converter.hoops_core",
                 "gear.sldprt": "omni.kit.converter.hoops_core", "base.stl": "omni.kit.asset_converter"}
        for filename, converter in cases.items():
            plan = ih.plan_import(_write(os.path.join(folder, "cad", filename)), project_dir=project)
            assert plan["ok"] and plan["converter"] == converter, (filename, plan)
        frame = ih.plan_import(os.path.join(folder, "cad", "frame.prt.3"), project_dir=project)
        assert frame["name"] == "frame" and frame["source_format"] == "prt"
        assert any("16 GB" in warning for warning in frame["warnings"])


def test_plan_import_validation_errors():
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "chair.fbx"))
        for bad in ("../evil", "a/b", "..", "1chair", "has space", "CON", "x" * 65, ""):
            result = ih.plan_import(source, project_dir=project, name=bad)
            assert not result["ok"], bad
        missing = ih.plan_import(os.path.join(folder, "nope.fbx"), project_dir=project)
        assert not missing["ok"] and "does not exist" in missing["error"]
        odd = ih.plan_import(_write(os.path.join(folder, "notes.xyz")), project_dir=project)
        assert not odd["ok"] and "Unsupported file type '.xyz'" in odd["error"]
        robot = ih.plan_import(_write(os.path.join(folder, "arm.urdf")), project_dir=project)
        assert not robot["ok"] and "isaacsim.asset.importer.urdf" in robot["error"]
        assert not ih.plan_import(source, project_dir=os.path.join(folder, "missing_proj"))["ok"]
        assert not ih.plan_import(source, project_dir="relative/proj")["ok"]
        assert not ih.plan_import(source, project_dir=project, target_units="parsec")["ok"]
        assert not ih.plan_import(source, project_dir=project, up_axis="X")["ok"]
        bare = os.path.join(folder, "bare")
        os.makedirs(bare)
        no_root = ih.plan_import(source, project_dir=bare)
        assert not no_root["ok"] and "target_units" in no_root["error"]
        ok = ih.plan_import(source, project_dir=bare, target_units="cm", up_axis="Y")
        assert ok["ok"] and ok["wrapper"]["expected_scale"] == 1.0
        assert any("does not exist yet" in warning for warning in ok["warnings"])
        assert not ok["how_to_place"]["layer_exists"]


def test_plan_import_unicode_existing_outputs_and_usd_source():
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "stühl#1.obj"))
        plan = ih.plan_import(source, project_dir=project, name="stuhl")
        assert plan["ok"]
        assert any("non-ASCII" in warning for warning in plan["warnings"])
        assert any("URL syntax" in warning for warning in plan["warnings"])
        _write(os.path.join(project, "assets", "stuhl", "stuhl.usd"))
        again = ih.plan_import(source, project_dir=project, name="stuhl")
        assert any("already exists" in warning for warning in again["warnings"])
        usd = _author_asset(os.path.join(folder, "chair.usda"))
        as_usd = ih.plan_import(usd, project_dir=project)
        assert as_usd["ok"] and as_usd["converter"] is None and as_usd["output_path"] is None
        assert as_usd["wrapper"]["expected_scale"] == 10.0  # read from the file: cm -> project mm


def test_supported_formats_lists_verified_converters():
    info = ih.supported_formats()
    assert info["ok"] and info["verified_on"]
    assert set(info["converters"]) == {"omni.kit.asset_converter", "omni.kit.converter.hoops_core",
                                       "omni.kit.converter.jt_core", "omni.kit.converter.dgn_core"}
    assert info["converters"]["omni.kit.asset_converter"]["versions"]["isaacsim-6.1.0"] == "6.0.5"
    assert info["formats"]["fbx"]["converter"] == "omni.kit.asset_converter"
    assert info["formats"]["step"]["converter"] == "omni.kit.converter.hoops_core"
    assert info["formats"]["jt"]["converter"] == "omni.kit.converter.jt_core"
    assert "urdf" in info["not_handled_here"] and "usd" in info["not_handled_here"]
    for converter in info["converters"].values():
        assert converter["sources"] and converter["api"]


# ------------------------------------------------------------------------------------------------ import_asset


def test_import_asset_defaults_to_dry_run_and_needs_kit():
    if _IN_KIT:
        return
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "chair.fbx"))
        with _fake_modules():
            dry = ih.import_asset(source, project_dir=project)
            assert dry["ok"] and dry["dry_run"] and not os.path.exists(os.path.join(project, "assets", "chair"))
            live = ih.import_asset(source, project_dir=project, dry_run=False)
            assert not live["ok"] and live["message"] == "Kit not available"
            assert not os.path.exists(os.path.join(project, "assets", "chair"))


def test_import_asset_fake_converter_converts_wraps_and_checks_health():
    if _IN_KIT:
        return
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "office_chair.fbx"))
        fake = _fake_asset_converter()
        with _fake_modules(omni__kit__asset_converter=fake):
            result = ih.import_asset(source, project_dir=project, dry_run=False, options={"ignore_camera": True})
        assert result["ok"] and result["status"] == "succeeded", result
        call = fake.calls[0]
        assert call["import_path"] == source
        assert call["output_path"] == os.path.join(project, "assets", "office_chair", "office_chair.usd")
        assert call["options"]["ignore_camera"] is True
        assert call["options"]["use_meter_as_world_unit"] is False
        assert call["options"]["create_world_as_default_root_prim"] is True
        assert call["close_stage"] is False
        status = ih.import_status(result["job_id"])
        assert status["ok"] and status["status"] == "succeeded" and status["progress"] == [1, 1]
        assert status["wrap"]["scale"] == 10.0 and status["wrap"]["rotate_x"] == 90.0
        assert status["detected_units"]["confidence"] == "high"
        assert status["health"]["ok"] and "score" in status["health"]
        assert "_plan" not in status
        low, high, up, mpu = _bbox(status["wrapper_path"])
        assert up == "Z" and mpu == 0.001
        _close(low, [-250.0, -250.0, 0.0])
        _close(high, [250.0, 250.0, 800.0])
        assert any(job["job_id"] == result["job_id"] for job in ih.import_status()["jobs"])

        # A second import never overwrites the first unless asked.
        with _fake_modules(omni__kit__asset_converter=_fake_asset_converter()):
            again = ih.import_asset(source, project_dir=project, dry_run=False)
            assert not again["ok"] and "already exists" in again["error"]
            replaced = ih.import_asset(source, project_dir=project, dry_run=False, overwrite=True)
        assert replaced["ok"] and replaced["status"] == "succeeded", replaced


def test_import_asset_fake_converter_failure_cleans_up():
    if _IN_KIT:
        return
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "broken.obj"))
        fake = _fake_asset_converter(succeed=False, partial=True, error="Assimp could not parse the file")
        with _fake_modules(omni__kit__asset_converter=fake):
            result = ih.import_asset(source, project_dir=project, dry_run=False)
        assert not result["ok"] and result["status"] == "failed"
        assert "Assimp could not parse" in result["error"]
        assert not os.path.exists(os.path.join(project, "assets", "broken"))  # partial file and new folder removed
        assert os.path.isdir(os.path.join(project, "assets"))  # the existing assets/ folder is kept
        nothing = _fake_asset_converter(write_output=False)
        with _fake_modules(omni__kit__asset_converter=nothing):
            silent = ih.import_asset(source, project_dir=project, dry_run=False)
        assert not silent["ok"] and "no output file" in silent["error"]


def test_import_asset_rejects_unknown_options_and_not_started_extension():
    if _IN_KIT:
        return
    with _tmp() as folder:
        project = _make_project(folder)
        source = _write(os.path.join(folder, "chair.fbx"))
        with _fake_modules(omni__kit__asset_converter=_fake_asset_converter()):
            bad = ih.import_asset(source, project_dir=project, dry_run=False, options={"ignore_cameras": True})
            assert not bad["ok"] and "ignore_cameras" in bad["error"]
            typed = ih.import_asset(source, project_dir=project, dry_run=False, options={"ignore_camera": "yes"})
            assert not typed["ok"] and "True or False" in typed["error"]
        stopped = _fake_asset_converter()
        stopped.get_instance = lambda: None
        with _fake_modules(omni__kit__asset_converter=stopped):
            off = ih.import_asset(source, project_dir=project, dry_run=False)
            assert not off["ok"] and "not started" in off["error"]
        assert not os.path.exists(os.path.join(project, "assets", "chair"))


def test_import_asset_fake_cad_converter():
    if _IN_KIT:
        return
    Status = namedtuple("ConverterStatus", "error_code error_msg")
    calls = []

    class Hoops:
        def __init__(self, ok):
            self.ok = ok

        def create_converter_task(self, input_path, output_path, file_format_args):
            calls.append((input_path, output_path, file_format_args))

            async def run():
                if not self.ok:
                    return "", Status(-1, "A3D_LOAD_CANNOT_ACCESS_CADFILE")
                _author_asset(output_path, mpu=0.001, up="Z", size=(300.0, 200.0, 100.0))
                return output_path, Status(0, "")

            return run()

    with _tmp() as folder:
        project = _make_project(folder, mpu=1.0, up="Z")
        source = _write(os.path.join(folder, "bracket.step"))
        module = types.ModuleType("omni.kit.converter.hoops_core")
        module.get_instance = lambda: Hoops(True)
        with _fake_modules(omni__kit__converter__hoops_core=module):
            result = ih.import_asset(source, project_dir=project, dry_run=False, options={"tessLOD": 3})
        assert result["ok"] and result["status"] == "succeeded", result
        assert calls[0][2] == {"tessLOD": "3"}
        assert result["wrap"]["scale"] == 0.001 and result["wrap"]["rotate_x"] == 0.0
        failing = types.ModuleType("omni.kit.converter.hoops_core")
        failing.get_instance = lambda: Hoops(False)
        with _fake_modules(omni__kit__converter__hoops_core=failing):
            bad = ih.import_asset(_write(os.path.join(folder, "other.step")), project_dir=project, dry_run=False)
        assert not bad["ok"] and "A3D_LOAD_CANNOT_ACCESS_CADFILE" in bad["error"]
        assert not os.path.exists(os.path.join(project, "assets", "other"))


# ------------------------------------------------------------------------------------------- smoke (read-only)


def test_smoke_detect_units_on_course_asset_read_only():
    candidates = [
        _COURSE / "PhysicalAI_SceneAssembly_Start" / "Assets" / "Machine_USD" / "N_01_PCB_On_Board" / "Source"
        / "Component" / "button.usd",
        _COURSE / "PhysicalAI_SceneAssembly_Start" / "Assets" / "Machine_USD" / "N_01_PCB_On_Board" / "Source"
        / "Component" / "three_color_light.usd",
    ]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return  # course assets not extracted on this machine
    before = os.stat(path)
    result = ih.detect_units(path, allow_outside_workspace=True)
    after = os.stat(path)
    assert (before.st_mtime_ns, before.st_size) == (after.st_mtime_ns, after.st_size)
    assert result["ok"], result
    assert result["meters_per_unit"] > 0 and result["up_axis"] in ("Y", "Z")
    assert result["confidence"] in ("high", "medium", "low")
