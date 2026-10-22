"""Run in USD Composer via Kit --exec (start-composer-study.ps1) or its Script Editor: serve the extended scene bridge.

The server is live_scene_control.LiveSceneControlServer, loaded read-only from the setup extension's file (never
edited), extended by omniverse_scene_mcp.kit_rpc: capabilities, read-only live insight and light/camera authoring
(dry_run by default). start-composer-study.ps1 moves the extension's own stock server to port 9909, so this one owns
http://127.0.0.1:9910/rpc. If 9910 already answers (a stock server from another launcher, or another app), nothing
is started: run composer-study/reload_composer_bridge.py in the Script Editor to swap it safely. If kit_rpc cannot be
imported, the basic bridge still starts. The open stage is never edited, selected or saved.
"""
from __future__ import annotations

import asyncio
import builtins
import importlib
import importlib.util
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable

LOG_PREFIX = "[Composer MCP]"
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


async def start_bridge(
    workspace: Path | str | None = None,
    port: Any = None,
    *,
    port_in_use: Callable[[int], bool] | None = None,
    log: Callable[[str], Any] = print,
) -> Any:
    """Start the extended bridge on 127.0.0.1:port once a stage is open and return the running server.

    A running bridge registered by an earlier call is returned as is (a second run starts nothing). Raises, without
    starting anything, when the port is reserved or already accepts connections. port_in_use(port) -> bool replaces
    the loopback probe (tests).
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
