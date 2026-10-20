"""Run in Isaac Sim's Script Editor: reload the 9911 scene bridge with the current omniverse_scene_mcp source.

The new code is first self-tested on temporary stages (tests/test_kit_bridge_usd.py) using Isaac's own USD
build; the open stage is never edited, selected or saved. If the self-test or the new server fails, the old
bridge keeps running (or is restarted). The outcome is written to isaacsim-study/bridge-reload-result.json.
"""
import asyncio
import builtins
import importlib
import importlib.util
import json
import os
import runpy
import sys
import time
import traceback
from pathlib import Path

workspace = Path(os.environ.get("OMNIVERSE_ROOT", "C:/Users/krngr/Omniverse"))
scene_source = workspace / "omniverse_scene_mcp" / "src"
self_test_path = workspace / "omniverse_scene_mcp" / "tests" / "test_kit_bridge_usd.py"
start_script = workspace / "isaacsim-study" / "start_mcp_bridge.py"
result_path = workspace / "isaacsim-study" / "bridge-reload-result.json"
BRIDGE_MODULE = "isaac_shared_scene_control"


def _package_modules():
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "omniverse_scene_mcp" or name.startswith("omniverse_scene_mcp.")
    }


def _swap_package_modules(modules):
    for name in list(_package_modules()):
        del sys.modules[name]
    sys.modules.update(modules)


def _run_self_tests():
    spec = importlib.util.spec_from_file_location("isaac_bridge_self_test", self_test_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    passed, failed = [], []
    for name in sorted(dir(module)):
        if not name.startswith("test_"):
            continue
        try:
            getattr(module, name)()
        except Exception:
            failed.append({"test": name, "error": traceback.format_exc(limit=4)})
        else:
            passed.append(name)
    return passed, failed


def _write(report):
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    result_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[Isaac MCP] Bridge reload: {report['status']} (details: {result_path})")


async def reload_bridge():
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "failed"}
    old_modules = _package_modules()
    old_bridge_module = sys.modules.get(BRIDGE_MODULE)
    old_server = getattr(builtins, "_isaac_scene_control_server", None)
    if str(scene_source) not in sys.path:
        sys.path.insert(0, str(scene_source))

    # 1. Import the new code beside the running bridge and test it on temporary stages only.
    try:
        _swap_package_modules({})
        kit_bridge = importlib.import_module("omniverse_scene_mcp.kit_bridge")
        if kit_bridge._PXR_IMPORT_ERROR is not None or kit_bridge.UsdUtils is None:
            raise ImportError(f"New kit_bridge could not import pxr: {kit_bridge._PXR_IMPORT_ERROR}")
        passed, failed = _run_self_tests()
        report["self_tests"] = {"passed": passed, "failed": failed}
        if failed or not passed:
            raise RuntimeError(f"{len(failed)} self-test(s) failed on Isaac's USD build")
    except Exception as error:
        _swap_package_modules(old_modules)
        report["status"] = "kept_old_bridge"
        report["error"] = f"{type(error).__name__}: {error}"
        _write(report)
        return

    # 2. Replace the running server with one built from the new modules.
    try:
        if old_server is not None:
            await old_server.stop()
        builtins._isaac_scene_control_server = None
        builtins._isaac_scene_control_start_task = None
        sys.modules.pop(BRIDGE_MODULE, None)
        runpy.run_path(str(start_script), run_name="__main__")
        await builtins._isaac_scene_control_start_task
        if getattr(builtins, "_isaac_scene_control_server", None) is None:
            raise RuntimeError("New bridge did not register itself")
        report["status"] = "reloaded"
    except Exception:
        report["error"] = traceback.format_exc(limit=4)
        _swap_package_modules(old_modules)
        if old_bridge_module is not None:
            sys.modules[BRIDGE_MODULE] = old_bridge_module
        if old_server is not None:
            try:
                await old_server.start()
                builtins._isaac_scene_control_server = old_server
                report["status"] = "restored_old_bridge"
            except Exception:
                report["restore_error"] = traceback.format_exc(limit=4)
    _write(report)


previous_reload = getattr(builtins, "_isaac_bridge_reload_task", None)
if previous_reload is None or previous_reload.done():
    builtins._isaac_bridge_reload_task = asyncio.ensure_future(reload_bridge())
else:
    print("[Isaac MCP] A bridge reload is already running.")
