"""Run in Isaac Sim Script Editor or via Kit --exec; reuse the scene bridge."""
import asyncio
import builtins
import importlib.util
import os
from pathlib import Path
import sys

workspace = Path(os.environ.get("OMNIVERSE_ROOT", "C:/Users/krngr/Omniverse"))
scene_source = workspace / "omniverse_scene_mcp" / "src"
if str(scene_source) not in sys.path:
    sys.path.insert(0, str(scene_source))
bridge_source = workspace / "kit-app-template/source/extensions/my_company.my_usd_composer_setup_extension/my_company/my_usd_composer_setup_extension/live_scene_control.py"


async def start_bridge():
    try:
        import omni.kit.app
        from omni import usd

        while usd.get_context().get_stage() is None:
            await omni.kit.app.get_app().next_update_async()
        if getattr(builtins, "_isaac_scene_control_server", None) is not None:
            print("[Isaac MCP] Bridge already active on http://127.0.0.1:9911/rpc")
            return
        spec = importlib.util.spec_from_file_location("isaac_shared_scene_control", bridge_source)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        server = module.LiveSceneControlServer(host="127.0.0.1", port=9911)
        await server.start()
        builtins._isaac_scene_control_server = server
        print("[Isaac MCP] Live Isaac Sim scene bridge ready: http://127.0.0.1:9911/rpc")
    except Exception as error:
        print(f"[Isaac MCP] Bridge failed to start: {error}")
        raise


previous_task = getattr(builtins, "_isaac_scene_control_start_task", None)
if previous_task is None or previous_task.done():
    builtins._isaac_scene_control_start_task = asyncio.ensure_future(start_bridge())
