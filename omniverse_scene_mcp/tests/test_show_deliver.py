"""Phase 4-5: camera framing, lighting rigs, asset paths, packaging, git files, NVIDIA Asset Validator."""
from __future__ import annotations

try:
    from pxr import Gf, Usd, UsdGeom
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import tempfile
from pathlib import Path
from unittest import mock

from omniverse_scene_mcp import features, show_deliver as s


def _box_stage(up="Z"):
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, up)
    cube = UsdGeom.Cube.Define(stage, "/World/Machine")
    cube.GetSizeAttr().Set(200.0)
    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(500, 300, 100))
    return stage


def _corners(stage, path):
    box = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_]).ComputeWorldBound(stage.GetPrimAtPath(path)) \
        .ComputeAlignedRange()
    lo, hi = box.GetMin(), box.GetMax()
    return box, [Gf.Vec3d(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]


def test_camera_frame_puts_the_whole_target_in_view_for_both_up_axes():
    for up in ("Z", "Y"):
        stage = _box_stage(up)
        preview = s.scene_camera_frame(stage, "/World/Machine")
        assert preview["ok"] and preview["dry_run"] and not stage.GetPrimAtPath("/World/Cameras/HeroCam")
        done = s.scene_camera_frame(stage, "/World/Machine", focal_length=50, dry_run=False)
        assert done["written"], done
        frustum = UsdGeom.Camera(stage.GetPrimAtPath("/World/Cameras/HeroCam")).GetCamera(0).frustum
        box, corners = _corners(stage, "/World/Machine")
        assert all(frustum.Intersects(c) for c in corners), up
        camera = stage.GetPrimAtPath("/World/Cameras/HeroCam")
        matrix = UsdGeom.Xformable(camera).ComputeLocalToWorldTransform(0)
        looking = -matrix.TransformDir(Gf.Vec3d(0, 0, 1)).GetNormalized()
        to_centre = (box.GetMidpoint() - matrix.ExtractTranslation()).GetNormalized()
        assert Gf.Dot(looking, to_centre) > 0.9999  # aimed at the centre
    stage = _box_stage()
    assert "units_warning" not in s.scene_camera_frame(stage, "/World/Machine")  # 200 cm: believable
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)  # same numbers read as metres: a 200 m box
    assert "units_warning" not in s.scene_camera_frame(stage, "/World/Machine")
    UsdGeom.Cube(stage.GetPrimAtPath("/World/Machine")).GetSizeAttr().Set(2000.0)  # 2 km: a units mix-up
    assert "units mix-up" in s.scene_camera_frame(stage, "/World/Machine")["units_warning"]
    assert "units_warning" in s.scene_lighting_rig(stage, "/World/Machine")
    stage = _box_stage()
    assert not s.scene_camera_frame(stage, "/World/Nope")["ok"]
    assert not s.scene_camera_frame(stage, "/World/Machine", direction=[0, 0, 1])["ok"]  # straight down: no up vector
    UsdGeom.XformCommonAPI(UsdGeom.Xform.Define(stage, "/World/Moved")).SetTranslate(Gf.Vec3d(1, 0, 0))
    assert not s.scene_camera_frame(stage, "/World/Machine", camera="/World/Moved/Cam")["ok"]


def test_lighting_rigs_are_valid_and_aimed():
    stage = _box_stage()
    for rig, count in (("studio_3point", 4), ("showroom", 3), ("outdoor", 2)):
        preview = s.scene_lighting_rig(stage, "/World/Machine", rig=rig, root=f"/World/L_{rig}")
        assert preview["ok"] and preview["counts"]["create"] == count, (rig, preview)
    done = s.scene_lighting_rig(stage, "/World/Machine", dry_run=False)
    assert done["written"]
    key = stage.GetPrimAtPath("/World/Lights/Key")
    matrix = UsdGeom.Xformable(key).ComputeLocalToWorldTransform(0)
    box, _ = _corners(stage, "/World/Machine")
    looking = -matrix.TransformDir(Gf.Vec3d(0, 0, 1)).GetNormalized()
    assert Gf.Dot(looking, (box.GetMidpoint() - matrix.ExtractTranslation()).GetNormalized()) > 0.9999
    assert key.GetAttribute("inputs:intensity").Get() == 15000
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "sky.hdr").write_bytes(b"#?RADIANCE")
        file_stage = Usd.Stage.CreateNew(str(Path(tmp) / "scene.usda"))
        UsdGeom.Cube.Define(file_stage, "/World/Box")
        assert not s.scene_lighting_rig(file_stage, "/World/Box", rig="hdri")["ok"]  # needs an image
        assert not s.scene_lighting_rig(file_stage, "/World/Box", rig="hdri", hdri="./nope.hdr")["ok"]
        hdri = s.scene_lighting_rig(file_stage, "/World/Box", rig="hdri", hdri="./sky.hdr", dry_run=False)
        assert hdri["written"], hdri
        dome = file_stage.GetPrimAtPath("/World/Lights/HDRI")
        assert dome.GetAttribute("inputs:texture:file").Get().path == "./sky.hdr"
        assert s.scene_lighting_rig(file_stage, "/World/Box", rig="hdri", hdri="./sky.hdr")["counts"]["ok"] == 1
    assert not s.scene_lighting_rig(stage, "/World/Machine", rig="disco")["ok"]


def _project(tmp):
    root = Path(tmp) / "Project"
    (root / "Assets" / "Props").mkdir(parents=True)
    (root / "Assemblies").mkdir()
    (root / "Assets" / "Props" / "wood.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "Assets" / "Props" / "crate.usda").write_text(
        '#usda 1.0\n(\n    defaultPrim = "Crate"\n)\ndef Xform "Crate"\n{\n    asset inputs:tex = @./wood.png@\n}\n')
    absolute = (root / "Assets" / "Props" / "crate.usda").as_posix()
    (root / "Assemblies" / "line.usda").write_text(
        '#usda 1.0\n(\n    defaultPrim = "World"\n)\ndef Xform "World"\n{\n'
        '    def "A" (\n        prepend references = @../Assets/Props/crate.usda@\n    )\n    {\n    }\n'
        f'    def "B" (\n        prepend references = @{absolute}@\n    )\n    {{\n    }}\n}}\n')
    return root


def test_paths_report_fix_and_package_keep_the_project_layout():
    with tempfile.TemporaryDirectory() as tmp:
        root = _project(tmp)
        scene = root / "Assemblies" / "line.usda"
        report = s.asset_paths_report(str(scene))
        assert report["counts"]["absolute"] == 1 and report["counts"]["relative"] >= 2
        package = s.project_package(str(scene), str(Path(tmp) / "Delivery"), dry_run=False)
        assert package["ok"], package
        assert package["entry_file"].replace("\\", "/").endswith("Delivery/Assemblies/line.usda")
        assert package["paths_rebased"] == 1 and package["missing_new_in_package"] == []
        assert "Delivery" in Path(package["entry_file"]).parts
        preview = s.asset_paths_fix(str(scene))
        assert preview["dry_run"] and preview["changes"][0]["changes"][0]["to"] == "../Assets/Props/crate.usda"
        fixed = s.asset_paths_fix(str(scene), dry_run=False)
        assert fixed["written"] and (root / "Assemblies" / "line.usda.bak").exists()
        assert s.asset_paths_report(str(scene))["counts"].get("absolute", 0) == 0
        assert not s.project_package(str(scene), str(Path(tmp) / "Delivery"), dry_run=False)["ok"]  # never overwrites


def test_git_files_add_only_missing_lines():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / ".gitignore").write_text("*.log\nmy_own_rule\n")
        preview = s.project_git_files(tmp)
        assert preview["dry_run"] and "*.log" not in preview["add"][".gitignore"]
        s.project_git_files(tmp, dry_run=False)
        ignore = (Path(tmp) / ".gitignore").read_text().splitlines()
        assert ignore.count("*.log") == 1 and "my_own_rule" in ignore and "__pycache__/" in ignore
        assert "*.usdc filter=lfs diff=lfs merge=lfs -text" in (Path(tmp) / ".gitattributes").read_text()
        assert s.project_git_files(tmp)["add"] == {}


class _FakeIssue:
    def __init__(self, message):
        self.message, self.severity, self.rule, self.at = message, "IssueSeverity.FAILURE", _FakeKindChecker, None


class _FakeKindChecker:
    pass


class _FakeResults:
    def issues(self):
        return [_FakeIssue("Invalid Kind \"group\"")]


class _FakeEngine:
    def __init__(self, init_rules=True):
        self.rules = []

    def enable_rule(self, rule):
        self.rules.append(rule)

    def validate(self, path):
        return _FakeResults()


def test_asset_validator_run_uses_nvidias_engine_and_checks_rule_names():
    fake = type("M", (), {"ValidationEngine": _FakeEngine, "KindCheckerChecker": None, "__version__": "test"})
    fake.KindChecker = type("KindChecker", (), {})
    fake.WeldChecker = type("WeldChecker", (), {})
    with tempfile.TemporaryDirectory() as tmp:
        file = Path(tmp) / "a.usda"
        file.write_text("#usda 1.0\n")
        with mock.patch.object(s, "_validator_module", return_value=fake):
            result = s.asset_validator_run(str(file), rules=["KindChecker"])
            assert result["ok"] and result["issues"] == 1 and result["by_rule"][0]["severity"] == "FAILURE"
            bad = s.asset_validator_run(str(file), rules=["KindCheker"])
            assert not bad["ok"] and "KindChecker" in bad["error"]
        with mock.patch.object(s, "_validator_module", return_value=None):
            assert not s.asset_validator_run(str(file))["ok"]
    for name in ("scene_camera_frame", "scene_lighting_rig", "project_package", "asset_paths_fix", "project_git_files"):
        assert features.FEATURES[name].mutates, name
    assert not features.FEATURES["asset_validator_run"].mutates and not features.FEATURES["asset_paths_report"].mutates
