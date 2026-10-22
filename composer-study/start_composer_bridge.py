"""Run in USD Composer via Kit --exec (start-composer-study.ps1) or its Script Editor: serve the extended scene bridge.

The server is live_scene_control.LiveSceneControlServer, loaded read-only from the setup extension's file (never
edited), extended by omniverse_scene_mcp.kit_rpc: capabilities, read-only live insight and light/camera authoring
(dry_run by default). start-composer-study.ps1 moves the extension's own stock server to port 9909, so this one owns
http://127.0.0.1:9910/rpc. Once the extended (guarded) server runs, the stock server on 9909 is STOPPED: it has no
browser/Host guard, so a web page could otherwise drive the open scene through it; if stopping it fails it keeps
running as before. The basic bridge (kit_rpc not importable) leaves it alone. If 9910 already answers (a stock server from another launcher, or another app), nothing
is started: run composer-study/reload_composer_bridge.py in the Script Editor to swap it safely. If kit_rpc cannot be
imported, the basic bridge still starts. The open stage is never edited, selected or saved.
"""
from __future__ import annotations

import asyncio
import builtins
import gc
import importlib
import importlib.util
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

LOG_PREFIX = "[Composer MCP]"
# The setup extension's own (stock, unguarded) server: start-composer-study.ps1 moves it to 9909.
STOCK_PORT = 9909
SERVER_CLASS_NAME = "LiveSceneControlServer"
STOCK_ATTR = "_live_scene_control"
HOST = "127.0.0.1"
DEFAULT_PORT = 9910
# Deliberately not OMNIVERSE_SCENE_CONTROL_PORT: that one moves the extension's stock server (9909 in the study launch).
PORT_ENV = "OMNIVERSE_COMPOSER_BRIDGE_PORT"
RESERVED_PORTS = {9905: "the MCP router", 9909: "Composer's stock scene server", 9911: "the Isaac Sim bridge"}
PROBE_TIMEOUT = 0.3
# Names distinct from Isaac Sim's (isaac_shared_scene_control, builtins._isaac_scene_control_*).
BRIDGE_MODULE = "composer_shared_scene_control"
SERVER_ATTR = "_composer_scene_control_server"
START_TASK_ATTR = "_composer_scene_control_start_task"
EXTENSION_FOLDER = (
    "kit-app-template/source/extensions/my_company.my_usd_composer_setup_extension/"
    "my_company/my_usd_composer_setup_extension"
)


def workspace_root() -> Path:
    """The Omniverse workspace: $OMNIVERSE_ROOT (set by start-composer-study.ps1) or the laptop default."""
    return Path(os.environ.get("OMNIVERSE_ROOT", "").strip() or "C:/Users/krngr/Omniverse")


def scene_source(workspace: Path | str) -> Path:
    """omniverse_scene_mcp/src: $OMNIVERSE_SCENE_MCP_SRC when set, else inside the workspace."""
    configured = os.environ.get("OMNIVERSE_SCENE_MCP_SRC", "").strip()
    return Path(configured) if configured else Path(workspace) / "omniverse_scene_mcp" / "src"


def bridge_source(workspace: Path | str) -> Path:
    """The setup extension's live_scene_control.py; only read, never edited."""
    return Path(workspace) / EXTENSION_FOLDER / "live_scene_control.py"


def bridge_port(value: Any = None) -> int:
    """The bridge port: value, else $OMNIVERSE_COMPOSER_BRIDGE_PORT, else 9910. Reserved ports raise ValueError."""
    raw = value if value is not None else (os.environ.get(PORT_ENV, "").strip() or DEFAULT_PORT)
    if isinstance(raw, bool):
        raise ValueError(f"Port {raw!r} is not a port number")
    try:
        port = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{PORT_ENV}={raw!r} is not a port number") from None
    if not 1024 <= port <= 65535:
        raise ValueError(f"Port {port} is outside 1024-65535")
    if port in RESERVED_PORTS:
        raise ValueError(f"Port {port} belongs to {RESERVED_PORTS[port]}; the Composer bridge uses {DEFAULT_PORT}")
    return port


def ensure_scene_source(workspace: Path | str) -> Path:
    """Put omniverse_scene_mcp/src on sys.path and refresh the import caches; return that folder."""
    source = scene_source(workspace)
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    # Python caches each folder's file list; modules added after Composer started (kit_rpc.py, authoring.py) must
    # be found too.
    importlib.invalidate_caches()
    return source


def port_accepts(port: int, host: str = HOST, timeout: float = PROBE_TIMEOUT) -> bool:
    """True when something accepts a TCP connection on 127.0.0.1:port (no bytes are sent)."""
    if host != HOST:
        raise ValueError("Only 127.0.0.1 is probed")
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_running(server: Any) -> bool:
    return bool(getattr(server, "_running", False))


def server_port(server: Any) -> int | None:
    port = getattr(server, "_port", None)
    return port if isinstance(port, int) and not isinstance(port, bool) else None


def server_endpoint(server: Any) -> str:
    return f"http://{getattr(server, '_host', HOST)}:{server_port(server)}/rpc"


def load_server_class(workspace: Path | str, log: Callable[[str], Any] = print) -> type:
    """LiveSceneControlServer from the extension file as a fresh module, extended by kit_rpc when it imports."""
    source = bridge_source(workspace)
    if not source.is_file():
        raise FileNotFoundError(f"live_scene_control.py is missing: {source}")
    spec = importlib.util.spec_from_file_location(BRIDGE_MODULE, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    server_class = module.LiveSceneControlServer
    try:
        from omniverse_scene_mcp.kit_rpc import make_extended_server_class

        server_class = make_extended_server_class(server_class)
    except Exception as error:
        log(f"{LOG_PREFIX} Extended RPC unavailable ({type(error).__name__}: {error}); starting the basic bridge")
    return server_class


async def wait_for_stage(context_name: str = "") -> None:
    """Wait, one Kit update at a time, until the USD context has a stage; never touches the stage."""
    import omni.kit.app
    from omni import usd

    app = omni.kit.app.get_app()
    while usd.get_context(context_name).get_stage() is None:
        await app.next_update_async()


def stock_server_port(bridge: int) -> int:
    """Port of the setup extension's stock server: $OMNIVERSE_SCENE_CONTROL_PORT when it is not the bridge's, else 9909."""
    raw = os.environ.get("OMNIVERSE_SCENE_CONTROL_PORT", "").strip()
    try:
        port = int(raw) if raw else STOCK_PORT
    except ValueError:
        port = STOCK_PORT
    return STOCK_PORT if port == bridge else port


def find_stock_servers(port: int, objects: Callable[[], Iterable[Any]] | None = None) -> list[Any]:
    """Running stock (unextended, unguarded) LiveSceneControlServer objects bound to port, each once.

    Found by class name in the gc scan (objects() replaces it in tests), directly or through an object's
    _live_scene_control attribute (the setup extension). Extended servers (they have rpc_version) never match.
    """
    found: dict[int, Any] = {}
    for obj in gc.get_objects() if objects is None else objects():
        candidates = [obj]
        attrs = _instance_dict(obj)
        if attrs is not None and STOCK_ATTR in attrs:
            candidates.append(attrs[STOCK_ATTR])
        for candidate in candidates:
            if not any(getattr(klass, "__name__", "") == SERVER_CLASS_NAME for klass in type(candidate).__mro__):
                continue
            if getattr(type(candidate), "rpc_version", None) is not None:
                continue  # an extended (guarded) bridge, never the stock server
            if server_port(candidate) == port and is_running(candidate):
                found.setdefault(id(candidate), candidate)
    return list(found.values())


def _instance_dict(obj: Any) -> dict | None:
    if not getattr(type(obj), "__dictoffset__", 0):
        return None
    try:
        attrs = object.__getattribute__(obj, "__dict__")
    except Exception:
        return None
    return attrs if isinstance(attrs, dict) else None


async def stop_stock_servers(
    bridge: Any,
    *,
    objects: Callable[[], Iterable[Any]] | None = None,
    log: Callable[[str], Any] = print,
) -> dict[str, Any]:
    """Stop the setup extension's stock server (unguarded, on 9909) once the guarded bridge runs; never raises.

    Only when bridge is the extended server (its HTTP handler has the localhost guard): a basic bridge leaves the
    stock server alone. On any failure every stock server stopped here is started again (the old behaviour).
    """
    bridge_at = server_port(bridge)
    port = stock_server_port(bridge_at if bridge_at is not None else DEFAULT_PORT)
    report: dict[str, Any] = {"port": port, "stopped": [], "status": "none_found"}
    if getattr(type(bridge), "rpc_version", None) is None or not is_running(bridge):
        report["status"] = "kept"
        report["message"] = "The bridge is the basic (unguarded) one, so the stock server was left running."
        log(f"{LOG_PREFIX} {report['message']}")
        return report
    stopped: list[Any] = []
    try:
        servers = find_stock_servers(port, objects)
        for server in servers:
            stopped.append(server)  # before stop(): a stop that fails part-way may already have closed the socket
            await server.stop()
            report["stopped"].append({"class": type(server).__name__, "port": port})
    except Exception as error:
        restart_errors = []
        for server in reversed(stopped):
            try:
                if not is_running(server):
                    await server.start()
            except Exception as again:
                restart_errors.append(f"{type(again).__name__}: {again}")
        report.update(status="kept", stopped=[], error=f"{type(error).__name__}: {error}")
        if restart_errors:
            report["restart_errors"] = restart_errors
            outcome = "restarting it failed too, so it may be stopped (the guarded bridge still serves)"
        else:
            outcome = "it keeps running (unguarded)"
        report["message"] = f"Could not stop the stock scene server on {port} ({report['error']}); {outcome}."
        log(f"{LOG_PREFIX} {report['message']}")
        return report
    if stopped:
        report["status"] = "stopped"
        report["message"] = (
            f"Stopped Composer's stock scene server on {port}: it has no browser/Host guard, and the guarded bridge "
            f"on {bridge_at} serves every method it had. Restarting the setup extension starts it again."
        )
    else:
        report["message"] = f"No running stock scene server on {port} in this Composer; nothing to stop."
    log(f"{LOG_PREFIX} {report['message']}")
    return report


async def start_bridge(
    workspace: Path | str | None = None,
    port: Any = None,
    *,
    port_in_use: Callable[[int], bool] | None = None,
    log: Callable[[str], Any] = print,
    stop_stock: bool = True,
    objects: Callable[[], Iterable[Any]] | None = None,
) -> Any:
    """Start the extended bridge on 127.0.0.1:port once a stage is open and return the running server.

    A running bridge registered by an earlier call is returned as is (a second run starts nothing). Raises, without
    starting anything, when the port is reserved or already accepts connections. port_in_use(port) -> bool replaces
    the loopback probe (tests). Once the new guarded bridge runs, the setup extension's unguarded stock server (moved
    to 9909) is stopped (stop_stock=False skips that; objects() replaces the gc scan in tests).
    """
    workspace = Path(workspace) if workspace is not None else workspace_root()
    port = bridge_port(port)
    context_name = os.environ.get("OMNIVERSE_USD_CONTEXT_NAME", "").strip()
    ensure_scene_source(workspace)
    await wait_for_stage(context_name)
    existing = getattr(builtins, SERVER_ATTR, None)
    if existing is not None and is_running(existing):
        log(f"{LOG_PREFIX} Bridge already active on {server_endpoint(existing)}")
        return existing
    if (port_accepts if port_in_use is None else port_in_use)(port):
        raise RuntimeError(
            f"Port {port} already answers (Composer's stock scene server from another launcher, or another app); "
            "nothing was started. Run composer-study/reload_composer_bridge.py in the Script Editor to swap it safely."
        )
    server_class = load_server_class(workspace, log)
    server = server_class(host=HOST, port=port, usd_context_name=context_name)
    try:
        await server.start()
    except BaseException:
        try:
            await server.stop()
        except Exception:
            pass
        raise
    setattr(builtins, SERVER_ATTR, server)
    version = getattr(server, "rpc_version", "basic")
    stock = os.environ.get("OMNIVERSE_SCENE_CONTROL_PORT", "").strip() or "9910"
    note = f"; the setup extension's stock server uses port {stock}" if stock != str(port) else ""
    log(f"{LOG_PREFIX} Live Composer scene bridge ready: {server_endpoint(server)} (RPC {version}){note}")
    if stop_stock:
        await stop_stock_servers(server, objects=objects, log=log)
    return server


def main() -> asyncio.Future | None:
    """Schedule start_bridge() once on Kit's event loop; a second run while it is starting does nothing."""
    previous = getattr(builtins, START_TASK_ATTR, None)
    if previous is not None and not previous.done():
        print(f"{LOG_PREFIX} The Composer bridge is already starting.")
        return None

    async def run():
        try:
            return await start_bridge()
        except Exception as error:
            print(f"{LOG_PREFIX} Composer bridge did not start: {type(error).__name__}: {error}")
            return None

    task = asyncio.ensure_future(run())
    setattr(builtins, START_TASK_ATTR, task)
    return task


# reload_composer_bridge.py (and the tests) load this file with COMPOSER_BRIDGE_AUTORUN=False to reuse its functions.
if globals().get("COMPOSER_BRIDGE_AUTORUN", True):
    main()
