"""Run in USD Composer's Script Editor: give a running Composer the extended scene bridge on 9910, no restart.

1. The new omniverse_scene_mcp code is imported fresh and self-tested on temporary stages with Composer's own USD
   build: tests/test_kit_bridge_usd.py and tests/test_kit_rpc.py (required), plus tests/test_edits.py and
   tests/test_undo.py when present (fixture-free test_* functions only). Any failure keeps the running bridge.
2. Whatever serves 127.0.0.1:9910 in this Composer is stopped: the bridge an earlier start/reload registered, or the
   setup extension's stock LiveSceneControlServer (found through its _live_scene_control attribute and a gc scan for
   running server objects bound to 9910). Servers on other ports (the stock one on 9909, Isaac Sim's 9911) are never
   touched, and nothing is stopped while an unknown program holds 9910.
3. start_composer_bridge.py starts the extended server on 9910. On ANY failure the new server is stopped and the
   stopped server(s) are started again. The open stage is never edited, selected or saved.
4. Once the extended (guarded) server runs on 9910, the setup extension's stock server on 9909 (no browser/Host
   guard) is stopped too; the report's stock_server says so. If that fails, the stock server keeps running.

The outcome is written to composer-study/bridge-reload-result.json. The setup extension keeps a reference to its
stopped stock server; Composer's shutdown stops it again harmlessly.
"""
from __future__ import annotations

import asyncio
import builtins
import gc
import importlib
import importlib.util
import inspect
import json
import os
import runpy
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterable

LOG_PREFIX = "[Composer MCP]"
RELOAD_TASK_ATTR = "_composer_bridge_reload_task"
REQUIRED_SELF_TESTS = ("test_kit_bridge_usd.py", "test_kit_rpc.py")
OPTIONAL_SELF_TESTS = ("test_edits.py", "test_undo.py", "test_kit_live_extras.py", "test_physics.py")
SERVER_CLASS_NAME = "LiveSceneControlServer"
EXTENSION_ATTR = "_live_scene_control"
_MAX_LISTED = 60


def workspace_root() -> Path:
    """The Omniverse workspace: $OMNIVERSE_ROOT or the laptop default."""
    return Path(os.environ.get("OMNIVERSE_ROOT", "").strip() or "C:/Users/krngr/Omniverse")


def _package_modules() -> dict[str, Any]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "omniverse_scene_mcp" or name.startswith("omniverse_scene_mcp.")
    }


def _swap_package_modules(modules: dict[str, Any]) -> None:
    for name in list(_package_modules()):
        del sys.modules[name]
    sys.modules.update(modules)


def _is_server(obj: Any) -> bool:
    """A LiveSceneControlServer (stock or extended subclass), judged by class names only (no attribute hooks run)."""
    return any(getattr(klass, "__name__", "") == SERVER_CLASS_NAME for klass in type(obj).__mro__)


def _instance_dict(obj: Any) -> dict | None:
    if not getattr(type(obj), "__dictoffset__", 0):
        return None
    try:
        attrs = object.__getattribute__(obj, "__dict__")
    except Exception:
        return None
    return attrs if isinstance(attrs, dict) else None


def find_scene_servers(
    port: int,
    objects: Callable[[], Iterable[Any]] | None = None,
    is_running: Callable[[Any], bool] | None = None,
    server_port: Callable[[Any], int | None] | None = None,
) -> list[dict[str, Any]]:
    """Running LiveSceneControlServer objects bound to port, each once, with the objects that hold them.

    Extension instances are found by their _live_scene_control attribute; the gc scan (or objects(), for tests) also
    finds server objects held elsewhere. Servers on any other port, and stopped ones, never match.
    """
    running = is_running or (lambda server: bool(getattr(server, "_running", False)))
    bound = server_port or (lambda server: getattr(server, "_port", None))
    found: dict[int, dict[str, Any]] = {}
    for obj in gc.get_objects() if objects is None else objects():
        holder = None
        candidate = obj
        if not _is_server(obj):
            attrs = _instance_dict(obj)
            if attrs is None or EXTENSION_ATTR not in attrs:
                continue
            candidate = attrs[EXTENSION_ATTR]
            holder = type(obj).__name__
            if not _is_server(candidate):
                continue
        if bound(candidate) != port or not running(candidate):
            continue
        entry = found.setdefault(
            id(candidate), {"server": candidate, "class": type(candidate).__name__, "port": port, "held_by": []}
        )
        if holder and holder not in entry["held_by"]:
            entry["held_by"].append(holder)
    return list(found.values())


def run_self_tests(tests_dir: Path | str) -> dict[str, Any]:
    """Run every fixture-free test_* function defined in each self-test file; never raises."""
    report: dict[str, Any] = {"passed": [], "failed": [], "skipped": [], "files": {}, "missing": []}
    for name in REQUIRED_SELF_TESTS + OPTIONAL_SELF_TESTS:
        path = Path(tests_dir) / name
        if not path.is_file():
            report["missing"].append(name)
            continue
        counts = report["files"].setdefault(name, {"passed": 0, "failed": 0, "skipped": 0})
        module_name = f"composer_bridge_self_test_{path.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module  # dataclasses and pickling look their module up by name
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            report["failed"].append({"test": f"{name}::<import>", "error": traceback.format_exc(limit=4)})
            counts["failed"] += 1
            continue
        try:
            for attr, test in sorted(vars(module).items()):
                if not attr.startswith("test_") or not inspect.isfunction(test) or test.__module__ != module_name:
                    continue
                if inspect.signature(test).parameters or inspect.iscoroutinefunction(test):
                    report["skipped"].append(f"{name}::{attr}")  # needs pytest fixtures or an event loop
                    counts["skipped"] += 1
                    continue
                try:
                    test()
                except Exception:
                    report["failed"].append({"test": f"{name}::{attr}", "error": traceback.format_exc(limit=4)})
                    counts["failed"] += 1
                else:
                    report["passed"].append(f"{name}::{attr}")
                    counts["passed"] += 1
        finally:
            sys.modules.pop(module_name, None)
    return report


def self_test_problems(report: dict[str, Any]) -> list[str]:
    """Why the self-tests block the reload: required files missing, failures, or a file with no passing test."""
    problems = []
    missing = [name for name in REQUIRED_SELF_TESTS if name in report["missing"]]
    if missing:
        problems.append(f"required self-test file(s) missing: {missing}")
    if report["failed"]:
        problems.append(f"{len(report['failed'])} self-test(s) failed on Composer's USD build")
    empty = [name for name, counts in report["files"].items() if not counts["passed"]]
    if empty:
        problems.append(f"files with no passing test: {empty}")
    return problems


def _capped(report: dict[str, Any]) -> dict[str, Any]:
    """The self-test report with long lists cut, so the result file stays small."""
    capped = dict(report)
    for key in ("passed", "failed", "skipped"):
        items = list(report[key])
        capped[key] = items[:_MAX_LISTED]
        if len(items) > _MAX_LISTED:
            capped[f"{key}_not_listed"] = len(items) - _MAX_LISTED
    return capped


def _describe(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in ("class", "port", "held_by", "registered") if key in entry}


def _stage_is_open(context_name: str) -> bool:
    from omni import usd

    return usd.get_context(context_name).get_stage() is not None


def _write(result_path: Path, report: dict[str, Any], log: Callable[[str], Any]) -> None:
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    except OSError as error:
        report["write_error"] = f"{type(error).__name__}: {error}"
    log(f"{LOG_PREFIX} Composer bridge reload: {report['status']} (details: {result_path})")


async def reload_bridge(
    workspace: Path | str | None = None,
    *,
    objects: Callable[[], Iterable[Any]] | None = None,
    port_in_use: Callable[[int], bool] | None = None,
    log: Callable[[str], Any] = print,
) -> dict[str, Any]:
    """Self-test the new code, then swap whatever serves 9910 for the extended bridge; return the written report.

    objects() replaces the gc scan and port_in_use(port) the loopback probe (tests). Never raises.
    """
    workspace = Path(workspace) if workspace is not None else workspace_root()
    result_path = workspace / "composer-study" / "bridge-reload-result.json"
    report: dict[str, Any] = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "failed"}
    try:
        await _reload(workspace, report, objects, port_in_use, log)
    except Exception:  # a bug here must still leave a result file behind
        report["status"] = "failed"
        report["error"] = traceback.format_exc(limit=6)
    _write(result_path, report, log)
    return report


async def _reload(
    workspace: Path,
    report: dict[str, Any],
    objects: Callable[[], Iterable[Any]] | None,
    port_in_use: Callable[[int], bool] | None,
    log: Callable[[str], Any],
) -> None:
    old_modules = _package_modules()

    # 0. Read-only checks: the start script, the port, an open stage and who serves the port now.
    try:
        start = runpy.run_path(
            str(workspace / "composer-study" / "start_composer_bridge.py"),
            init_globals={"COMPOSER_BRIDGE_AUTORUN": False},
            run_name="composer_bridge_start",
        )
        port = start["bridge_port"]()
        report["endpoint"] = f"http://{start['HOST']}:{port}/rpc"
        server_attr = start["SERVER_ATTR"]
        registered = getattr(builtins, server_attr, None)
        context_name = os.environ.get("OMNIVERSE_USD_CONTEXT_NAME", "").strip()
        # start_bridge waits for a stage, so without one the swap would stop the old bridge and then hang.
        if not _stage_is_open(context_name):
            raise RuntimeError("No stage is open; open a stage and run the reload again")
        if registered is not None and start["is_running"](registered) and start["server_port"](registered) != port:
            raise RuntimeError(
                f"The registered Composer bridge serves port {start['server_port'](registered)}, not {port}; "
                "restart Composer with start-composer-study.ps1 instead"
            )
        holders = find_scene_servers(port, objects, start["is_running"], start["server_port"])
        for entry in holders:
            entry["registered"] = entry["server"] is registered
        if registered is not None and start["is_running"](registered) and not any(e["registered"] for e in holders):
            entry = {"server": registered, "class": type(registered).__name__, "port": port, "held_by": ["builtins"]}
            holders.insert(0, {**entry, "registered": True})
        report["holders"] = [_describe(entry) for entry in holders]
        probe = start["port_accepts"] if port_in_use is None else port_in_use
        if not holders and probe(port):
            raise RuntimeError(
                f"Port {port} answers but no scene server object in this Composer holds it (another Composer or "
                "app?); nothing was stopped"
            )
    except Exception as error:
        report["status"] = "kept_old_bridge"
        report["error"] = f"{type(error).__name__}: {error}"
        return

    # 1. Import the new code beside the running bridge and test it on temporary stages only.
    try:
        start["ensure_scene_source"](workspace)
        _swap_package_modules({})
        importlib.invalidate_caches()
        kit_bridge = importlib.import_module("omniverse_scene_mcp.kit_bridge")
        if kit_bridge._PXR_IMPORT_ERROR is not None or kit_bridge.UsdUtils is None:
            raise ImportError(f"New kit_bridge could not import pxr: {kit_bridge._PXR_IMPORT_ERROR}")
        kit_rpc = importlib.import_module("omniverse_scene_mcp.kit_rpc")
        if not callable(getattr(kit_rpc, "make_extended_server_class", None)):
            raise ImportError("New kit_rpc has no make_extended_server_class")
        expected_version = kit_rpc.RPC_VERSION
        tests = run_self_tests(workspace / "omniverse_scene_mcp" / "tests")
        report["self_tests"] = _capped(tests)
        problems = self_test_problems(tests)
        if problems:
            raise RuntimeError("; ".join(problems))
    except Exception as error:
        package = sys.modules.get("omniverse_scene_mcp")
        report["diagnostics"] = {
            "package_path": [str(path) for path in getattr(package, "__path__", [])],
            "sys_path_head": [str(path) for path in sys.path[:8]],
        }
        _swap_package_modules(old_modules)
        report["status"] = "kept_old_bridge"
        report["error"] = f"{type(error).__name__}: {error}"
        return

    # 2. Swap the server on the port; on ANY failure put the old one(s) back.
    old_bridge_module = sys.modules.get(start["BRIDGE_MODULE"])
    stopping: list[dict[str, Any]] = []
    new_server = None
    report["stopped"] = []
    try:
        for entry in holders:
            stopping.append(entry)
            await entry["server"].stop()
            report["stopped"].append(_describe(entry))
        setattr(builtins, server_attr, None)
        sys.modules.pop(start["BRIDGE_MODULE"], None)
        new_server = await start["start_bridge"](workspace, port, port_in_use=port_in_use, log=log, stop_stock=False)
        if new_server is None or not start["is_running"](new_server) or start["server_port"](new_server) != port:
            raise RuntimeError(f"The new bridge is not running on port {port}")
        report["rpc_version"] = getattr(new_server, "rpc_version", None)
        report["server_class"] = type(new_server).__name__
        if report["rpc_version"] == expected_version:
            report["status"] = "reloaded"
        else:
            # Basic methods still work; capabilities/insight/authoring calls will report needs_bridge_update.
            report["status"] = "reloaded_without_extended_rpc"
            report["warning"] = f"Expected RPC {expected_version}; see the Script Editor output"
        # The setup extension reads this variable when it (re)starts; keep its stock server off the bridge's port.
        stock_port = os.environ.get("OMNIVERSE_SCENE_CONTROL_PORT", "").strip() or "9910"
        if stock_port == str(port):
            os.environ["OMNIVERSE_SCENE_CONTROL_PORT"] = "9909"
            report["stock_port_env"] = (
                f"OMNIVERSE_SCENE_CONTROL_PORT {stock_port} -> 9909 in this Composer process only, so a restart of "
                f"the setup extension cannot bind {port} again"
            )
    except Exception:
        report["error"] = traceback.format_exc(limit=4)
        report["status"] = "failed"
        candidate = new_server if new_server is not None else getattr(builtins, server_attr, None)
        restore_errors = []
        if candidate is not None and all(candidate is not entry["server"] for entry in stopping):
            try:
                if start["is_running"](candidate):
                    await candidate.stop()
            except Exception:
                restore_errors.append(traceback.format_exc(limit=4))
        _swap_package_modules(old_modules)
        if old_bridge_module is not None:
            sys.modules[start["BRIDGE_MODULE"]] = old_bridge_module
        else:
            sys.modules.pop(start["BRIDGE_MODULE"], None)
        setattr(builtins, server_attr, registered)
        restarted = []
        for entry in reversed(stopping):
            try:
                if not start["is_running"](entry["server"]):
                    await entry["server"].start()
                restarted.append(_describe(entry))
            except Exception:
                restore_errors.append(traceback.format_exc(limit=4))
        report["restarted"] = restarted
        if restore_errors:
            report["restore_error"] = restore_errors
        elif stopping:
            report["status"] = "restored_old_bridge"
        # else: no bridge was serving the port before and the new one did not start, so the status stays "failed".
        return

    # 3. The guarded bridge now serves the port: stop the setup extension's unguarded stock server (9909). This never
    #    undoes the reload; if it fails, the stock server keeps running as before (stop_stock_servers restarts it).
    try:
        report["stock_server"] = await start["stop_stock_servers"](new_server, objects=objects, log=log)
    except Exception as error:  # stop_stock_servers never raises; an older start script without it must not either
        report["stock_server"] = {"status": "kept", "error": f"{type(error).__name__}: {error}"}


def main() -> asyncio.Future | None:
    """Schedule reload_bridge() once on Kit's event loop; a second run while it is running does nothing."""
    previous = getattr(builtins, RELOAD_TASK_ATTR, None)
    if previous is not None and not previous.done():
        print(f"{LOG_PREFIX} A Composer bridge reload is already running.")
        return None
    task = asyncio.ensure_future(reload_bridge())
    setattr(builtins, RELOAD_TASK_ATTR, task)
    return task


# Tests load this file with COMPOSER_BRIDGE_AUTORUN=False to call reload_bridge() with fakes.
if globals().get("COMPOSER_BRIDGE_AUTORUN", True):
    main()
