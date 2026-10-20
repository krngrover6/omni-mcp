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
