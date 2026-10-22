"""composer-study/start_composer_bridge.py and reload_composer_bridge.py outside Kit, plus start-composer-study.ps1.

omni.* is faked, every server class is a stand-in that never binds a port, the gc scan and the port probe are injected
(or socket.create_connection is replaced), and only temp folders are written. Not one of the reload script's
self-test files, so it never runs inside Composer; every test also returns early when a real omni package is loaded.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
import os
import runpy
import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path

from omniverse_scene_mcp.bridge import RPC_VERSION

WORKSPACE = Path(__file__).resolve().parents[2]
STUDY = WORKSPACE / "composer-study"
START_SCRIPT = STUDY / "start_composer_bridge.py"
RELOAD_SCRIPT = STUDY / "reload_composer_bridge.py"
LAUNCHER = WORKSPACE / "start-composer-study.ps1"
PACKAGE_SRC = WORKSPACE / "omniverse_scene_mcp" / "src"
EXTENSION_FOLDER = (
    "kit-app-template/source/extensions/my_company.my_usd_composer_setup_extension/"
    "my_company/my_usd_composer_setup_extension"
)
SERVER_ATTR = "_composer_scene_control_server"
_BUILTINS = (SERVER_ATTR, "_composer_scene_control_start_task", "_composer_bridge_reload_task")
_ENV = (
    "OMNIVERSE_ROOT",
    "OMNIVERSE_SCENE_MCP_SRC",
    "OMNIVERSE_COMPOSER_BRIDGE_PORT",
    "OMNIVERSE_USD_CONTEXT_NAME",
    "OMNIVERSE_SCENE_CONTROL_PORT",
)
_REGISTRY = "composer_scripts_test_registry"
_MISSING = object()
PASSING = "def test_passes():\n    assert True\n"
FAILING = "def test_breaks_on_this_usd_build():\n    raise AssertionError('broken on this USD build')\n"

# Stand-in for the setup extension's live_scene_control.py: same constructor and async start/stop, binds nothing.
FAKE_LIVE_SCENE_CONTROL = '''
import composer_scripts_test_registry as _registry

FAIL_START = @FAIL_START@


class LiveSceneControlServer:
    def __init__(self, host="127.0.0.1", port=9910, usd_context_name=""):
        self._host = host
        self._port = port
        self._usd_context_name = usd_context_name
        self._running = False
        self._bridge = object()
        self.events = []
        _registry.servers.append(self)

    @property
    def endpoint(self):
        return "http://%s:%s" % (self._host, self._port)

    async def start(self):
        self.events.append("start")
        if FAIL_START:
            raise OSError("[WinError 10048] port %s is already in use" % self._port)
        self._running = True

    async def stop(self):
        self.events.append("stop")
        self._running = False

    def _submit(self, payload):
        return self._dispatch(payload)

    def _dispatch(self, payload):
        return {"ok": True, "result": {"status": "ok"}}
'''


class LiveSceneControlServer:
    """Stand-in for a running stock server (same class name, so the reload's scan recognizes it)."""

    def __init__(self, port: int, running: bool = True) -> None:
        self._host = "127.0.0.1"
        self._port = port
        self._running = running
        self.events: list[str] = []

    async def start(self) -> None:
        self.events.append("start")
        self._running = True

    async def stop(self) -> None:
        self.events.append("stop")
        self._running = False


class CreateSetupExtension:
    """Stand-in for the setup extension instance that holds its stock server."""

    def __init__(self, server: object) -> None:
        self._live_scene_control = server


class _UntouchableStage:
    """An open stage that records any attribute read; the scripts must only compare it with None."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str):
        self.touched.append(name)
        raise AttributeError(name)


def _inside_kit() -> bool:
    module = sys.modules.get("omni")
    return module is not None and bool(getattr(module, "__file__", None) or getattr(module, "__path__", None))


def _has_pxr() -> bool:
    return importlib.util.find_spec("pxr") is not None


class _Sandbox:
    """Temp workspace with copies of both scripts, a fake live_scene_control.py and fake self-tests; fake omni.

    Saves and restores builtins, environment, sys.path, the event loop and every omni / omniverse_scene_mcp module.
    """

    def __init__(
        self,
        *,
        stage_open: bool = True,
        stage_after_updates: int = 0,
        fail_start: bool = False,
        self_tests: dict[str, str] | None = None,
    ) -> None:
        self.stage = _UntouchableStage()
        self.stage_open = stage_open
        self.stage_after_updates = stage_after_updates
        self.fail_start = fail_start
        self.self_tests = (
            {"test_kit_bridge_usd.py": PASSING, "test_kit_rpc.py": PASSING} if self_tests is None else self_tests
        )
        self.updates = 0
        self.contexts: list[str] = []
        self.servers: list[object] = []
        self.messages: list[str] = []

    def __enter__(self) -> _Sandbox:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        study = self.root / "composer-study"
        study.mkdir()
        shutil.copy2(START_SCRIPT, study / START_SCRIPT.name)
        shutil.copy2(RELOAD_SCRIPT, study / RELOAD_SCRIPT.name)
        extension = self.root / EXTENSION_FOLDER
        extension.mkdir(parents=True)
        source = FAKE_LIVE_SCENE_CONTROL.replace("@FAIL_START@", repr(self.fail_start))
        (extension / "live_scene_control.py").write_text(source, encoding="utf-8")
        tests = self.root / "omniverse_scene_mcp" / "tests"
        tests.mkdir(parents=True)
        for name, body in self.self_tests.items():
            (tests / name).write_text(body, encoding="utf-8")

        self._saved_builtins = {name: getattr(builtins, name, _MISSING) for name in _BUILTINS}
        self._saved_env = {name: os.environ.get(name) for name in _ENV}
        self._saved_modules = {name: module for name, module in sys.modules.items() if self._tracked(name)}
        self._saved_path = list(sys.path)
        for name in _BUILTINS:
            if hasattr(builtins, name):
                delattr(builtins, name)
        for name in _ENV:
            os.environ.pop(name, None)
        os.environ["OMNIVERSE_ROOT"] = str(self.root)
        os.environ["OMNIVERSE_SCENE_MCP_SRC"] = str(PACKAGE_SRC)
        sys.modules.update(self._fake_omni())
        registry = types.ModuleType(_REGISTRY)
        registry.servers = self.servers
        sys.modules[_REGISTRY] = registry
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            for server in self.servers:
                server._running = False  # nothing keeps "serving" into later tests
            for task_name in ("_composer_scene_control_start_task", "_composer_bridge_reload_task"):
                task = getattr(builtins, task_name, None)
                if task is not None and not task.done():
                    task.cancel()
        finally:
            asyncio.set_event_loop(None)
            self.loop.close()
            for name, value in self._saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            for name, value in self._saved_builtins.items():
                if value is _MISSING:
                    if hasattr(builtins, name):
                        delattr(builtins, name)
                else:
                    setattr(builtins, name, value)
            for name in [name for name in sys.modules if self._tracked(name)]:
                del sys.modules[name]
            sys.modules.update(self._saved_modules)
            sys.path[:] = self._saved_path
            self._tmp.cleanup()

    @staticmethod
    def _tracked(name: str) -> bool:
        return (
            name in ("omni", "omniverse_scene_mcp", _REGISTRY, "composer_shared_scene_control")
            or name.startswith(("omni.", "omniverse_scene_mcp.", "composer_bridge_self_test_"))
        )

    def _fake_omni(self) -> dict[str, types.ModuleType]:
        sandbox = self

        class _App:
            async def next_update_async(self) -> None:
                sandbox.updates += 1
                if sandbox.stage_after_updates and sandbox.updates >= sandbox.stage_after_updates:
                    sandbox.stage_open = True
                await asyncio.sleep(0)

        app = _App()

        def get_context(name: str = "") -> types.SimpleNamespace:
            sandbox.contexts.append(name)
            return types.SimpleNamespace(get_stage=lambda: sandbox.stage if sandbox.stage_open else None)

        omni = types.ModuleType("omni")
        kit = types.ModuleType("omni.kit")
        kit_app = types.ModuleType("omni.kit.app")
        usd = types.ModuleType("omni.usd")
        kit_app.get_app = lambda: app
        usd.get_context = get_context
        omni.kit, omni.usd, kit.app = kit, usd, kit_app
        return {"omni": omni, "omni.kit": kit, "omni.kit.app": kit_app, "omni.usd": usd}

    def script(self, name: str) -> dict:
        """The sandbox copy of a script, loaded without running it (COMPOSER_BRIDGE_AUTORUN=False)."""
        path = self.root / "composer-study" / name
        return runpy.run_path(str(path), init_globals={"COMPOSER_BRIDGE_AUTORUN": False}, run_name=f"under_test_{path.stem}")

    def run(self, awaitable):
        return self.loop.run_until_complete(asyncio.wait_for(awaitable, 60))

    def log(self, message: str) -> None:
        self.messages.append(message)


def _no_probe(port: int) -> bool:
    assert port == 9910, port
    return False


# --- start-composer-study.ps1 ---------------------------------------------------------------------------------


def test_launcher_moves_the_stock_server_to_9909_and_execs_the_bridge():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "$env:OMNIVERSE_SCENE_CONTROL_PORT = '9909'" in text
    assert "OMNIVERSE_SCENE_CONTROL_PORT = '9910'" not in text
    launch = [line for line in text.splitlines() if line.strip().startswith("& $composerLauncher")]
    assert len(launch) == 1, launch
    line = launch[0]
    assert line.count("'--exec'") == 2
    assert line.index("composer-study\\startup.py") < line.index("composer-study\\start_composer_bridge.py")
    assert "'--/app/auto_load_usd='" in line and "'--/app/content/emptyStageOnStart=false'" in line
    # The port variable is still saved before and restored after the launch, like the other variables.
    assert "'OMNIVERSE_SCENE_CONTROL_PORT')" in text and "SetEnvironmentVariable" in text
    assert "$env:OMNIVERSE_EMPTY_STAGE_ON_START = '1'" in text
    assert START_SCRIPT.is_file() and RELOAD_SCRIPT.is_file()


def test_reload_self_test_files_exist_in_the_package():
    if _inside_kit():
        return
    with _Sandbox() as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
    for name in reload["REQUIRED_SELF_TESTS"] + reload["OPTIONAL_SELF_TESTS"]:
        assert (WORKSPACE / "omniverse_scene_mcp" / "tests" / name).is_file(), name
    assert "test_composer_scripts.py" not in reload["REQUIRED_SELF_TESTS"] + reload["OPTIONAL_SELF_TESTS"]
    assert {"test_kit_live_extras.py", "test_physics.py"} <= set(reload["OPTIONAL_SELF_TESTS"])
    assert {"test_scene_tour.py", "test_assignments.py"} <= set(reload["OPTIONAL_SELF_TESTS"])


class _StubbornStock(LiveSceneControlServer):
    """A stock server whose stop fails (after it already stopped once elsewhere it would have to be restarted)."""

    async def stop(self) -> None:
        self.events.append("stop")
        raise RuntimeError("stop failed (fake)")


def test_start_stops_the_unguarded_stock_server_on_9909_after_the_guarded_bridge_runs():
    if _inside_kit():
        return
    stock = LiveSceneControlServer(9909)
    extension = CreateSetupExtension(stock)
    other = LiveSceneControlServer(9911)
    with _Sandbox() as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        pool = [extension, stock, other, object()]
        server = sandbox.run(start["start_bridge"](port_in_use=_no_probe, log=sandbox.log, objects=lambda: pool))
        assert server._running and server.rpc_version == RPC_VERSION
        assert stock.events == ["stop"] and not stock._running  # stopped once, through either holder
        assert other.events == [] and other._running
        assert extension._live_scene_control is stock  # the extension object is never edited
        assert any("Stopped Composer's stock scene server on 9909" in message for message in sandbox.messages)
        # The bridge itself (extended, on 9910) is never treated as a stock server.
        assert start["find_stock_servers"](9910, lambda: [server]) == []


def test_start_keeps_the_stock_server_when_stopping_it_fails_or_the_bridge_is_basic():
    if _inside_kit():
        return
    first = LiveSceneControlServer(9909)
    stubborn = _StubbornStock(9909)
    with _Sandbox() as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        server = sandbox.run(
            start["start_bridge"](port_in_use=_no_probe, log=sandbox.log, objects=lambda: [first, stubborn])
        )
        assert server._running
        report = sandbox.run(start["stop_stock_servers"](server, objects=lambda: [], log=sandbox.log))
        assert report["status"] == "none_found" and report["stopped"] == []
        # The failed stop restarted the one it had stopped: both keep serving, as before this change.
        assert first.events == ["stop", "start"] and first._running
        assert stubborn.events == ["stop"]
        assert any("keeps running" in message for message in sandbox.messages)

        basic = LiveSceneControlServer(9910)  # the basic bridge (kit_rpc unavailable) is unguarded itself
        stock = LiveSceneControlServer(9909)
        kept = sandbox.run(start["stop_stock_servers"](basic, objects=lambda: [stock], log=sandbox.log))
        assert kept["status"] == "kept" and stock.events == [] and stock._running
        os.environ["OMNIVERSE_SCENE_CONTROL_PORT"] = "9919"
        assert start["stock_server_port"](9910) == 9919
        os.environ["OMNIVERSE_SCENE_CONTROL_PORT"] = "9910"
        assert start["stock_server_port"](9910) == 9909


class _HalfStoppingStock(LiveSceneControlServer):
    """A stock server whose stop closes it and THEN fails (e.g. its serving thread does not join in time)."""

    async def stop(self) -> None:
        self.events.append("stop")
        self._running = False
        raise RuntimeError("thread join timed out (fake)")


def test_start_restarts_a_stock_server_whose_stop_failed_part_way():
    if _inside_kit():
        return
    half = _HalfStoppingStock(9909)
    with _Sandbox() as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        server = sandbox.run(start["start_bridge"](port_in_use=_no_probe, log=sandbox.log, stop_stock=False))
        report = sandbox.run(start["stop_stock_servers"](server, objects=lambda: [half], log=sandbox.log))
        # The report says the stock server keeps running, so it must really be running again.
        assert report["status"] == "kept" and "keeps running" in report["message"]
        assert half.events == ["stop", "start"] and half._running
        assert server._running  # the guarded bridge is untouched either way


# --- start_composer_bridge.py ---------------------------------------------------------------------------------


def test_bridge_port_reads_only_its_own_variable_and_refuses_reserved_ports():
    if _inside_kit():
        return
    with _Sandbox() as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        os.environ["OMNIVERSE_SCENE_CONTROL_PORT"] = "9909"  # the stock server's port never moves the bridge
        assert start["bridge_port"]() == 9910
        os.environ["OMNIVERSE_COMPOSER_BRIDGE_PORT"] = "9920"
        assert start["bridge_port"]() == 9920
        assert start["bridge_port"](9930) == 9930
        for bad in (9911, 9909, 9905, "9911", 80, 70000, "abc", True):
            try:
                start["bridge_port"](bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"port {bad!r} was accepted")
        assert start["BRIDGE_MODULE"] != "isaac_shared_scene_control"
        assert not start["SERVER_ATTR"].startswith("_isaac")
        try:
            start["port_accepts"](9910, host="10.0.0.1")
        except ValueError:
            pass
        else:
            raise AssertionError("a non-loopback host was probed")


def test_start_serves_the_extended_server_on_9910_and_a_second_run_starts_nothing():
    if _inside_kit():
        return
    with _Sandbox() as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        server = sandbox.run(start["start_bridge"](port_in_use=_no_probe, log=sandbox.log))
        assert getattr(builtins, SERVER_ATTR) is server
        assert type(server).__name__ == "ExtendedLiveSceneControlServer"
        assert server.rpc_version == RPC_VERSION and server._port == 9910 and server._host == "127.0.0.1"
        assert server._running and server.events == ["start"]
        answer = server._dispatch({"method": "capabilities", "params": {}})
        assert answer["ok"] and answer["result"]["version"] == RPC_VERSION and answer["result"]["dry_run_default"]
        assert any("9910/rpc" in message and RPC_VERSION in message for message in sandbox.messages)

        again = sandbox.run(start["start_bridge"](port_in_use=_no_probe, log=sandbox.log))
        assert again is server and len(sandbox.servers) == 1 and server.events == ["start"]
        assert "already active" in sandbox.messages[-1]
        assert sandbox.stage.touched == []


def test_start_waits_for_a_stage_and_never_touches_it():
    if _inside_kit():
        return
    with _Sandbox(stage_open=False, stage_after_updates=3) as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        server = sandbox.run(start["start_bridge"](port_in_use=_no_probe, log=sandbox.log))
        assert sandbox.updates == 3 and server._running
        assert set(sandbox.contexts) == {""}
        assert sandbox.stage.touched == []


def test_start_refuses_when_9910_already_answers():
    if _inside_kit():
        return
    with _Sandbox() as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        probed: list[int] = []

        def busy(port: int) -> bool:
            probed.append(port)
            return True

        try:
            sandbox.run(start["start_bridge"](port_in_use=busy, log=sandbox.log))
        except RuntimeError as error:
            assert "reload_composer_bridge.py" in str(error)
        else:
            raise AssertionError("started on a busy port")
        assert probed == [9910]
        assert sandbox.servers == [] and getattr(builtins, SERVER_ATTR, None) is None


def test_start_failure_leaves_nothing_registered():
    if _inside_kit():
        return
    with _Sandbox(fail_start=True) as sandbox:
        start = sandbox.script(START_SCRIPT.name)
        try:
            sandbox.run(start["start_bridge"](port_in_use=_no_probe, log=sandbox.log))
        except OSError:
            pass
        else:
            raise AssertionError("a failed start was reported as success")
        assert getattr(builtins, SERVER_ATTR, None) is None
        assert sandbox.servers[0].events == ["start", "stop"]


def test_start_autorun_schedules_one_task_and_probes_only_loopback_9910():
    if _inside_kit():
        return
    connects: list[tuple] = []

    def refused(address, timeout=None, *_args, **_kwargs):
        connects.append((address, timeout))
        raise ConnectionRefusedError("nothing listens (fake)")

    saved_connect = socket.create_connection
    with _Sandbox(stage_open=False, stage_after_updates=2) as sandbox:
        socket.create_connection = refused
        try:
            script = str(sandbox.root / "composer-study" / START_SCRIPT.name)
            runpy.run_path(script)
            first = builtins._composer_scene_control_start_task
            runpy.run_path(script)  # still starting: no second task
            assert builtins._composer_scene_control_start_task is first
            server = sandbox.run(first)
            assert server is getattr(builtins, SERVER_ATTR) and server._running
            assert connects == [(("127.0.0.1", 9910), 0.3)]

            runpy.run_path(script)  # finished: a new task that finds the running bridge
            assert sandbox.run(builtins._composer_scene_control_start_task) is server
            assert len(sandbox.servers) == 1 and len(connects) == 1
        finally:
            socket.create_connection = saved_connect


# --- reload_composer_bridge.py --------------------------------------------------------------------------------


def test_reload_upgrades_the_stock_server_and_leaves_other_ports_alone():
    if _inside_kit() or not _has_pxr():
        return
    stock = LiveSceneControlServer(9910)
    extension = CreateSetupExtension(stock)
    moved_stock = LiveSceneControlServer(9909)
    isaac = LiveSceneControlServer(9911)
    with _Sandbox() as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        pool = [extension, stock, CreateSetupExtension(moved_stock), moved_stock, isaac, object(), {"_port": 9910}]
        report = sandbox.run(
            reload["reload_bridge"](objects=lambda: pool, port_in_use=_no_probe, log=sandbox.log)
        )
        written = json.loads((sandbox.root / "composer-study" / "bridge-reload-result.json").read_text("utf-8"))
        new = getattr(builtins, SERVER_ATTR)
        assert report["status"] == "reloaded", report
        assert report["rpc_version"] == RPC_VERSION and report["endpoint"] == "http://127.0.0.1:9910/rpc"
        assert report["stopped"] == [
            {"class": "LiveSceneControlServer", "port": 9910, "held_by": ["CreateSetupExtension"], "registered": False}
        ]
        assert report["self_tests"]["passed"] == ["test_kit_bridge_usd.py::test_passes", "test_kit_rpc.py::test_passes"]
        assert report["self_tests"]["missing"] == [
            "test_edits.py", "test_undo.py", "test_kit_live_extras.py", "test_physics.py", "test_scene_tour.py",
            "test_assignments.py",
        ]
        assert written["status"] == "reloaded" and written["stopped"] == report["stopped"]
        # Once the guarded bridge serves 9910, the extension's unguarded stock server (moved to 9909) is stopped too;
        # Isaac Sim's 9911 bridge is never touched.
        assert stock.events == ["stop"] and moved_stock.events == ["stop"] and isaac.events == []
        assert not moved_stock._running and isaac._running
        assert report["stock_server"]["status"] == "stopped" and report["stock_server"]["port"] == 9909
        assert report["stock_server"]["stopped"] == [{"class": "LiveSceneControlServer", "port": 9909}]
        assert written["stock_server"]["status"] == "stopped"
        assert any("Stopped Composer's stock scene server on 9909" in message for message in sandbox.messages)
        assert new._running and new._port == 9910 and type(new).__name__ == "ExtendedLiveSceneControlServer"
        assert extension._live_scene_control is stock  # the extension object itself is never edited
        # A later restart of the setup extension in this process would put its stock server on 9909, not 9910.
        assert os.environ["OMNIVERSE_SCENE_CONTROL_PORT"] == "9909" and "9909" in report["stock_port_env"]
        assert sandbox.stage.touched == []
        json.dumps(report)


def test_reload_self_test_failure_keeps_the_stock_server():
    if _inside_kit() or not _has_pxr():
        return
    stock = LiveSceneControlServer(9910)
    extension = CreateSetupExtension(stock)
    tests = {"test_kit_bridge_usd.py": PASSING, "test_kit_rpc.py": FAILING}
    with _Sandbox(self_tests=tests) as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        old_package = sys.modules.get("omniverse_scene_mcp")
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [extension], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "kept_old_bridge", report
        assert "failed" in report["error"]
        assert [item["test"] for item in report["self_tests"]["failed"]] == [
            "test_kit_rpc.py::test_breaks_on_this_usd_build"
        ]
        assert stock.events == [] and stock._running
        assert getattr(builtins, SERVER_ATTR, None) is None and sandbox.servers == []
        assert sys.modules.get("omniverse_scene_mcp") is old_package  # the old modules are back


def test_reload_needs_both_core_self_tests_and_tolerates_missing_optional_ones():
    if _inside_kit() or not _has_pxr():
        return
    stock = LiveSceneControlServer(9910)
    with _Sandbox(self_tests={"test_kit_bridge_usd.py": PASSING}) as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [stock], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "kept_old_bridge" and "test_kit_rpc.py" in report["error"], report
        assert stock.events == []

    tests = {"test_kit_bridge_usd.py": PASSING, "test_kit_rpc.py": PASSING, "test_undo.py": PASSING}
    with _Sandbox(self_tests=tests) as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [stock], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "reloaded", report
        assert report["self_tests"]["missing"] == [
            "test_edits.py", "test_kit_live_extras.py", "test_physics.py", "test_scene_tour.py", "test_assignments.py"
        ]
        assert report["self_tests"]["files"]["test_undo.py"]["passed"] == 1
        assert stock.events == ["stop"]

    tests = {"test_kit_bridge_usd.py": PASSING, "test_kit_rpc.py": PASSING, "test_edits.py": FAILING}
    stock = LiveSceneControlServer(9910)
    with _Sandbox(self_tests=tests) as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [stock], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "kept_old_bridge" and stock.events == [], report


def test_reload_failure_after_stopping_restarts_the_stock_server():
    if _inside_kit() or not _has_pxr():
        return
    stock = LiveSceneControlServer(9910)
    extension = CreateSetupExtension(stock)
    with _Sandbox(fail_start=True) as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        old_package = sys.modules.get("omniverse_scene_mcp")
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [extension], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "restored_old_bridge", report
        assert "WinError 10048" in report["error"]
        assert stock.events == ["stop", "start"] and stock._running
        assert report["restarted"] == report["stopped"] and len(report["stopped"]) == 1
        assert sandbox.servers[0].events == ["start", "stop"] and not sandbox.servers[0]._running
        assert getattr(builtins, SERVER_ATTR, None) is None and "OMNIVERSE_SCENE_CONTROL_PORT" not in os.environ
        assert sys.modules.get("omniverse_scene_mcp") is old_package and old_package is not None
        written = json.loads((sandbox.root / "composer-study" / "bridge-reload-result.json").read_text("utf-8"))
        assert written["status"] == "restored_old_bridge"


def test_reload_twice_is_idempotent():
    if _inside_kit() or not _has_pxr():
        return
    stock = LiveSceneControlServer(9910)
    extension = CreateSetupExtension(stock)
    with _Sandbox() as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        first = sandbox.run(reload["reload_bridge"](objects=lambda: [extension, stock], port_in_use=_no_probe, log=sandbox.log))
        bridge_a = getattr(builtins, SERVER_ATTR)
        second = sandbox.run(
            reload["reload_bridge"](objects=lambda: [extension, stock], port_in_use=_no_probe, log=sandbox.log)
        )
        bridge_b = getattr(builtins, SERVER_ATTR)
        assert first["status"] == second["status"] == "reloaded", (first, second)
        assert second["stopped"] == [
            {"class": "ExtendedLiveSceneControlServer", "port": 9910, "held_by": ["builtins"], "registered": True}
        ]
        assert stock.events == ["stop"]  # the stopped stock server is neither stopped again nor restarted
        assert bridge_a.events == ["start", "stop"] and not bridge_a._running
        assert bridge_b is not bridge_a and bridge_b._running and bridge_b._port == 9910
        assert [server for server in sandbox.servers if server._running] == [bridge_b]


def test_reload_without_a_stage_changes_nothing():
    if _inside_kit():
        return
    stock = LiveSceneControlServer(9910)
    with _Sandbox(stage_open=False) as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [stock], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "kept_old_bridge" and "No stage is open" in report["error"], report
        assert "self_tests" not in report and stock.events == [] and sandbox.servers == []


def test_reload_refuses_when_an_unknown_program_holds_9910():
    if _inside_kit():
        return
    probed: list[int] = []

    def busy(port: int) -> bool:
        probed.append(port)
        return True

    with _Sandbox() as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [], port_in_use=busy, log=sandbox.log))
        assert report["status"] == "kept_old_bridge" and "nothing was stopped" in report["error"], report
        assert probed == [9910] and "self_tests" not in report and sandbox.servers == []


def test_reload_refuses_a_registered_bridge_on_another_port():
    if _inside_kit():
        return
    elsewhere = LiveSceneControlServer(9920)
    with _Sandbox() as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        builtins._composer_scene_control_server = elsewhere
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "kept_old_bridge" and "9920" in report["error"], report
        assert elsewhere.events == [] and builtins._composer_scene_control_server is elsewhere


def test_reload_with_a_free_port_just_starts_the_bridge():
    if _inside_kit() or not _has_pxr():
        return
    with _Sandbox() as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        report = sandbox.run(reload["reload_bridge"](objects=lambda: [], port_in_use=_no_probe, log=sandbox.log))
        assert report["status"] == "reloaded" and report["stopped"] == [] and report["holders"] == [], report
        assert getattr(builtins, SERVER_ATTR)._running


def test_find_scene_servers_matches_class_name_port_and_running_state():
    if _inside_kit():
        return
    with _Sandbox() as sandbox:
        find = sandbox.script(RELOAD_SCRIPT.name)["find_scene_servers"]
    stock = LiveSceneControlServer(9910)
    extension = CreateSetupExtension(stock)
    stopped = LiveSceneControlServer(9910, running=False)
    other_port = LiveSceneControlServer(9911)
    look_alike = types.SimpleNamespace(_port=9910, _running=True)  # right attributes, wrong class name
    holder_of_junk = CreateSetupExtension(look_alike)
    found = find(9910, lambda: [extension, stock, stopped, other_port, look_alike, holder_of_junk, 42, "x"])
    assert len(found) == 1 and found[0]["server"] is stock and found[0]["held_by"] == ["CreateSetupExtension"]
    assert find(9911, lambda: [other_port])[0]["server"] is other_port
    # Default: a real gc scan of this process finds the same running stock server.
    assert any(entry["server"] is stock for entry in find(9910))
    assert not any(entry["server"] is stopped for entry in find(9910))


def test_self_test_runner_skips_fixture_and_async_tests_and_imported_helpers():
    if _inside_kit():
        return
    body = (
        "from __future__ import annotations\n"
        "import dataclasses\n"
        "from os.path import join as test_imported_helper\n\n"
        "@dataclasses.dataclass\n"
        "class Point:\n"
        "    x: int = 0\n\n"
        "def test_passes():\n"
        "    assert Point(2).x == 2\n\n"
        "def test_needs_a_fixture(tmp_path):\n"
        "    raise AssertionError('must not run')\n\n"
        "async def test_needs_a_loop():\n"
        "    raise AssertionError('must not run')\n"
    )
    with _Sandbox(self_tests={"test_kit_bridge_usd.py": body, "test_kit_rpc.py": PASSING}) as sandbox:
        reload = sandbox.script(RELOAD_SCRIPT.name)
        report = reload["run_self_tests"](sandbox.root / "omniverse_scene_mcp" / "tests")
        assert report["passed"] == ["test_kit_bridge_usd.py::test_passes", "test_kit_rpc.py::test_passes"], report
        assert sorted(report["skipped"]) == [
            "test_kit_bridge_usd.py::test_needs_a_fixture",
            "test_kit_bridge_usd.py::test_needs_a_loop",
        ]
        assert report["failed"] == [] and reload["self_test_problems"](report) == []
        assert not any(name.startswith("composer_bridge_self_test_") for name in sys.modules)
        empty = {"passed": [], "failed": [], "skipped": [], "files": {"test_kit_rpc.py": {"passed": 0}}, "missing": []}
        assert reload["self_test_problems"](empty)


def test_reload_autorun_writes_the_result_file_once():
    if _inside_kit() or not _has_pxr():
        return
    connects: list[tuple] = []

    def refused(address, timeout=None, *_args, **_kwargs):
        connects.append((address, timeout))
        raise ConnectionRefusedError("nothing listens (fake)")

    stock = LiveSceneControlServer(9910)
    extension = CreateSetupExtension(stock)
    saved_connect = socket.create_connection
    with _Sandbox() as sandbox:
        socket.create_connection = refused
        try:
            script = str(sandbox.root / "composer-study" / RELOAD_SCRIPT.name)
            runpy.run_path(script)
            task = builtins._composer_bridge_reload_task
            runpy.run_path(script)  # still running: no second reload
            assert builtins._composer_bridge_reload_task is task
            report = sandbox.run(task)
        finally:
            socket.create_connection = saved_connect
        written = json.loads((sandbox.root / "composer-study" / "bridge-reload-result.json").read_text("utf-8"))
        assert report["status"] == written["status"] == "reloaded", report
        assert stock.events == ["stop"] and extension._live_scene_control is stock
        assert all(address == ("127.0.0.1", 9910) for address, _timeout in connects)
        assert getattr(builtins, SERVER_ATTR)._running


def test_ci_workflow_runs_when_the_composer_launcher_alone_changes():
    """The launcher sits at the workspace root, outside composer-study/**, so the workflow must list it by name."""
    workflow = WORKSPACE / ".github" / "workflows" / "omniverse-scene-mcp-tests.yml"
    if not workflow.is_file():
        return  # the workflow is not in this checkout
    lines = [line.strip() for line in workflow.read_text(encoding="utf-8").splitlines()]
    for event in ("push:", "pull_request:"):
        start = lines.index(event)
        end = next(index for index in range(start + 1, len(lines)) if lines[index].endswith(":")
                   and lines[index] != "paths:")
        block = lines[start:end]
        for path in ('"composer-study/**"', '"start-composer-study.ps1"', '"isaacsim-study/**"'):
            assert f"- {path}" in block, (event, path)
    assert LAUNCHER.name == "start-composer-study.ps1"
