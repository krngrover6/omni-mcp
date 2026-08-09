from omniverse_scene_mcp.server import build_server


def test_server_builds():
    app = build_server()
    assert app is not None
