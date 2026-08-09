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
