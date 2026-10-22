"""Run in Isaac Sim's Script Editor: reload the 9911 scene bridge with the current omniverse_scene_mcp source.

The new code is first self-tested on temporary stages (tests/test_kit_bridge_usd.py, tests/test_kit_rpc.py,
tests/test_edits.py, tests/test_undo.py, tests/test_kit_live_extras.py and tests/test_physics.py, all fixture-free; Kit's command and undo modules are faked, never the
real ones) using Isaac's own USD build; the open stage is never edited, selected or saved. The new server
is live_scene_control.LiveSceneControlServer extended by omniverse_scene_mcp.kit_rpc (see start_mcp_bridge.py).
If a self-test or the new server fails, the old bridge keeps running (or is restarted). The outcome is written
to isaacsim-study/bridge-reload-result.json.
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
self_test_paths = [
    workspace / "omniverse_scene_mcp" / "tests" / "test_kit_bridge_usd.py",
    workspace / "omniverse_scene_mcp" / "tests" / "test_kit_rpc.py",
    # Safe attribute/material edits and MCP undo (RPC tier3-1) on Isaac's USD 25.11.
    workspace / "omniverse_scene_mcp" / "tests" / "test_edits.py",
    workspace / "omniverse_scene_mcp" / "tests" / "test_undo.py",
    # RPC tier4-1: viewport capture / simulation controls (fake omni modules; every test returns at once inside Kit)
    # and UsdPhysics setup on temporary stages.
    workspace / "omniverse_scene_mcp" / "tests" / "test_kit_live_extras.py",
    workspace / "omniverse_scene_mcp" / "tests" / "test_physics.py",
]
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
    """Run every fixture-free test_* function of each self-test file; return passed, failed and per-file counts."""
    passed, failed, files = [], [], {}
    for path in self_test_paths:
        counts = files.setdefault(path.name, {"passed": 0, "failed": 0})
        try:
            spec = importlib.util.spec_from_file_location(f"isaac_bridge_self_test_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception:
            failed.append({"test": f"{path.name}::<import>", "error": traceback.format_exc(limit=4)})
            counts["failed"] += 1
            continue
        for name in sorted(dir(module)):
            test = getattr(module, name) if name.startswith("test_") else None
            if not callable(test):
                continue
            try:
                test()
            except Exception:
                failed.append({"test": f"{path.name}::{name}", "error": traceback.format_exc(limit=4)})
                counts["failed"] += 1
            else:
                passed.append(f"{path.name}::{name}")
                counts["passed"] += 1
    return passed, failed, files


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
        # start_mcp_bridge.py waits for an open stage, so without one step 2 would stop the old bridge and then
        # hang with no bridge and no result file. Refuse up front instead; the old bridge keeps running.
        from omni import usd

        if usd.get_context().get_stage() is None:
            raise RuntimeError("No stage is open; open a stage and run the reload again")
        _swap_package_modules({})
        # Python caches each package folder's file list; modules added after Isaac started (kit_rpc.py,
        # authoring.py) stay invisible until the cache is refreshed.
        importlib.invalidate_caches()
        kit_bridge = importlib.import_module("omniverse_scene_mcp.kit_bridge")
        if kit_bridge._PXR_IMPORT_ERROR is not None or kit_bridge.UsdUtils is None:
            raise ImportError(f"New kit_bridge could not import pxr: {kit_bridge._PXR_IMPORT_ERROR}")
        kit_rpc = importlib.import_module("omniverse_scene_mcp.kit_rpc")
        if not callable(getattr(kit_rpc, "make_extended_server_class", None)):
            raise ImportError("New kit_rpc has no make_extended_server_class")
        expected_version = kit_rpc.RPC_VERSION
        passed, failed, files = _run_self_tests()
        report["self_tests"] = {"passed": passed, "failed": failed, "files": files}
        empty = [name for name, counts in files.items() if not counts["passed"]]
        if failed or empty:
            raise RuntimeError(f"{len(failed)} self-test(s) failed on Isaac's USD build; files with no passing test: {empty}")
    except Exception as error:
        package = sys.modules.get("omniverse_scene_mcp")
        report["diagnostics"] = {
            "package_path": [str(path) for path in getattr(package, "__path__", [])],
            "scene_source_on_sys_path": str(scene_source) in sys.path,
            "sys_path_head": [str(path) for path in sys.path[:8]],
        }
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
        new_server = getattr(builtins, "_isaac_scene_control_server", None)
        if new_server is None:
            raise RuntimeError("New bridge did not register itself")
        report["rpc_version"] = getattr(new_server, "rpc_version", None)
        if report["rpc_version"] == expected_version:
            report["status"] = "reloaded"
        else:
            # Basic methods still work; capabilities/insight/authoring calls will report needs_bridge_update.
            report["status"] = "reloaded_without_extended_rpc"
            report["warning"] = f"Expected RPC {expected_version}; see the Script Editor output of start_mcp_bridge.py"
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
