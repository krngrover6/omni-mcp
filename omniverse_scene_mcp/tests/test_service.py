import json
import os
import tempfile

from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.service import SceneMCPService


def test_scene_summary_reports_top_level_prims():
    service = SceneMCPService(FakeSceneBridge())
    summary = service.scene_summary()
    assert summary["stage_name"] == "FakeStage"
    assert "/World/Props" in summary["top_level_prims"]


def test_dry_run_create_returns_preview():
    service = SceneMCPService(FakeSceneBridge())
    result = service.create_prim("/World/NewPrim", dry_run=True)
    preview = service.preview_changes(result)
    assert preview["dry_run"] is True
    assert preview["changes"][0]["action"] == "create"
    assert preview["payload"]["type_name"] == "Xform"


def test_move_prim_updates_fake_scene():
    service = SceneMCPService(FakeSceneBridge())
    result = service.move_prim("/World/Props/Cube", translation=(1.0, 2.0, 3.0))
    prim = result.payload["prim"]
    assert prim["translation"] == (1.0, 2.0, 3.0)


def test_variant_selection_updates_fake_scene():
    bridge = FakeSceneBridge()
    service = SceneMCPService(bridge)

    result = service.set_variant_selection("/World/Props", "color", "blue")

    assert result.payload["selections"] == {"color": "blue"}
    assert service.get_variant_selections("/World/Props")["selections"] == {"color": "blue"}


def test_variant_selection_dry_run_does_not_change_scene():
    bridge = FakeSceneBridge()
    service = SceneMCPService(bridge)

    result = service.set_variant_selection("/World/Props", "trim", "sport", dry_run=True)

    assert result.dry_run is True
    assert service.get_variant_selections("/World/Props")["selections"] == {}


def test_timeline_pose_and_clip_controls_fake_scene():
    service = SceneMCPService(FakeSceneBridge())

    pose = service.set_timeline_time(480)
    assert pose["current_time_code"] == 480
    assert pose["playing"] is False

    playing = service.play_timeline(960, 1200)
    assert playing["start_time_code"] == 960
    assert playing["end_time_code"] == 1200
    assert playing["playing"] is True

    stopped = service.stop_timeline()
    assert stopped["playing"] is False


class _NoSaveBridge(FakeSceneBridge):
    """Fake bridge whose stage looks like a real file and whose save must never run."""

    def __init__(self, stage_name: str) -> None:
        super().__init__()
        self._stage_name = stage_name
        self.saves = []

    def save_scene(self, path: str) -> str:
        self.saves.append(path)
        raise AssertionError("dry run must not call the bridge's save")


def test_save_dry_run_in_place_when_path_matches_stage():
    bridge = _NoSaveBridge("C:/Users/Me/Scenes/Shot.usda")
    service = SceneMCPService(bridge)

    preview = service.save_scene(r"c:\users\me\scenes\SHOT.usda", dry_run=True)

    assert bridge.saves == []
    assert preview["dry_run"] is True
    assert preview["in_place"] is True
    assert preview["would"] == "save the root layer in place"
    assert preview["path"] == r"c:\users\me\scenes\SHOT.usda"
    assert any("dirty sublayer" in warning for warning in preview["warnings"])


def test_save_dry_run_strips_file_prefix_from_stage_identifier():
    for identifier in ("file:/C:/Scenes/Shot.usda", "file:///C:/Scenes/Shot.usda", "FILE:C:/Scenes/Shot.usda"):
        service = SceneMCPService(_NoSaveBridge(identifier))
        assert service.save_scene(r"C:\Scenes\Shot.usda", dry_run=True)["in_place"] is True, identifier


def test_save_dry_run_save_as_warns_about_root_layer_only():
    bridge = _NoSaveBridge("C:/Scenes/Shot.usda")
    service = SceneMCPService(bridge)

    preview = service.save_scene("C:/Scenes/Shot_copy.usda", dry_run=True)

    assert bridge.saves == []
    assert preview["in_place"] is False
    assert preview["would"] == "Save As: export a copy of the root layer only"
    text = " ".join(preview["warnings"])
    assert "Only the root layer is copied" in text
    assert "dirty sublayers stay unsaved in memory" in text
    assert "The open stage stays on C:/Scenes/Shot.usda" in text
    json.dumps(preview)


def test_save_dry_run_flags_existing_target_and_anonymous_stage():
    with tempfile.TemporaryDirectory() as folder:
        target = os.path.join(folder, "exists.usda")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("#usda 1.0\n")
        service = SceneMCPService(_NoSaveBridge("anon:0x1234:root.usda"))
        preview = service.save_scene(target, dry_run=True)
    assert preview["in_place"] is False
    assert any("already exists" in warning for warning in preview["warnings"])
    assert any("anonymous" in warning for warning in preview["warnings"])


def test_save_dry_run_on_fake_bridge_does_not_rename_stage():
    service = SceneMCPService(FakeSceneBridge())
    preview = service.save_scene("FakeStage", dry_run=True)
    assert preview["in_place"] is None
    assert any("fake bridge" in warning for warning in preview["warnings"])
    assert any("Relative path" in warning for warning in preview["warnings"])
    assert service.scene_summary()["stage_name"] == "FakeStage"


def test_save_dry_run_relative_path_does_not_claim_copy_only():
    bridge = _NoSaveBridge("C:/Work/proj/scene.usda")
    service = SceneMCPService(bridge)
    preview = service.save_scene("proj/scene.usda", dry_run=True)
    assert bridge.saves == []
    assert preview["in_place"] is None
    assert "working directory" in preview["would"] and "overwrites" in preview["would"]
    assert not preview["would"].startswith("Save As")
    assert any("dirty sublayer" in warning for warning in preview["warnings"])
    uri = service.save_scene("file:/C:/Work/proj/scene.usda", dry_run=True)
    assert uri["in_place"] is True
    anonymous = SceneMCPService(_NoSaveBridge("anon:0x1:root.usda")).save_scene("copy.usda", dry_run=True)
    assert anonymous["in_place"] is False


def test_save_dry_run_requires_a_path():
    service = SceneMCPService(_NoSaveBridge("C:/Scenes/Shot.usda"))
    try:
        service.save_scene("  ", dry_run=True)
    except ValueError as exc:
        assert "required" in str(exc)
    else:
        raise AssertionError("empty path should be rejected")


def test_save_without_dry_run_still_calls_bridge():
    service = SceneMCPService(FakeSceneBridge())
    assert service.save_scene("C:/Scenes/Out.usda") == {"saved_path": "C:/Scenes/Out.usda"}


def test_backend_info_defaults_for_direct_construction():
    info = SceneMCPService(FakeSceneBridge()).backend_info
    assert info["mode"] == "direct"
    assert info["bridge"] == "FakeSceneBridge"
    assert info["silent_fallback"] is False
    custom = SceneMCPService(FakeSceneBridge(), backend_info={"mode": "fake", "bridge": "FakeSceneBridge", "warnings": ["w"]})
    assert custom.backend_info["mode"] == "fake" and custom.backend_info["warnings"] == ["w"]
