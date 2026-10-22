"""Shared test setup, loaded by pytest and by tools/run_tests.py so both runs behave the same.

* src/ and tests/ go on sys.path, so a fresh checkout imports the package without installing it.
* OMNIVERSE_SCENE_MCP_TEST=1: runtime then uses the fake scene and offline knowledge and never probes 127.0.0.1:9910.
* A best-effort network guard (set OMNIVERSE_SCENE_MCP_ALLOW_NETWORK=1 to turn it off): no DNS lookups of non-local
  hosts, no connections to non-loopback addresses or to the loopback ports of the live Omniverse stack (router,
  Composer/Isaac bridges, knowledge MCPs, Bowerbot). A loopback listener a test opens itself still works.
* Tests that need workspace files which are not in git (on GitHub Actions, say) are skipped when those files are
  absent; see LOCAL_ONLY_TESTS. Tests that already return early on their own are listed in RETURNS_EARLY_WHEN_ABSENT.

Never loaded inside Kit or Isaac Sim: isaacsim-study/reload_mcp_bridge.py imports its two self-test files directly.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
SRC_DIR = PROJECT_ROOT / "src"
WORKSPACE_ROOT = PROJECT_ROOT.parent

# Same order as tools/run_tests.py: tests/ first, then src/.
for _path in (SRC_DIR, TESTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

# Offline by default: runtime then uses the fake scene and NullKnowledgeBackend and never probes 127.0.0.1:9910.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

# ------------------------------------------------------------------------------------- local-only data, platforms

# "module::test" (or "module::*" for every test in it) -> workspace-relative paths it needs that are not tracked in
# git. Skipped when any is missing; runs again by itself once those files are committed.
LOCAL_ONLY_TESTS: dict[str, tuple[str, ...]] = {
    # Asserts the workspace instructions file exists next to omniverse_scene_mcp/; AGENTS.md is untracked.
    "test_exam_progress::test_default_path_resolves_to_workspace_exam_prep_without_creating_it": ("AGENTS.md",),
    # Every sandbox copies the Composer bridge scripts; composer-study/ is untracked.
    "test_composer_scripts::*": (
        "composer-study/start_composer_bridge.py",
        "composer-study/reload_composer_bridge.py",
    ),
    "test_composer_scripts::test_launcher_moves_the_stock_server_to_9909_and_execs_the_bridge": (
        "start-composer-study.ps1",
    ),
}

# "module::test" -> why it only holds on Windows. Skipped on other platforms (the ubuntu CI job) with that reason.
WINDOWS_ONLY_TESTS: dict[str, str] = {
    "test_usd_insight::test_resolve_scene_file_rejects_unc_and_outside_paths_before_touching_the_filesystem": (
        "expects 'C:/...' and '\\\\host\\share\\...' to be absolute paths outside the workspace; on POSIX they "
        "are relative names inside it"
    ),
    "test_kit_bridge_usd::test_save_with_file_uri_to_the_open_root_layer_saves_in_place": (
        "builds 'file:/' + an absolute path, a Kit-style URI only for drive-letter paths; on POSIX it becomes "
        "'file://home/...' and kit_bridge._same_file keeps the leading '//'"
    ),
}

# Documentation only: tests that need local-only data but already return early (and pass) when it is absent.
RETURNS_EARLY_WHEN_ABSENT: dict[str, str] = {
    "test_exam_bank::test_local_citations_exist": "LearnOpenUSD-main/docs (untracked)",
    "test_exam_bank::test_no_ten_word_sequence_copied_from_study_guide": "study guide.txt in a session scratchpad",
    "test_exam_blueprint::test_objective_count_and_numbering_match_guide": "study guide.txt ($NCP_OUSD_GUIDE_TXT)",
    "test_exam_blueprint::test_blueprint_text_is_not_copied_from_guide": "study guide.txt ($NCP_OUSD_GUIDE_TXT)",
    "test_offline_docs_knowledge::test_real_isaac_sim_61_docs_answer_articulation_and_vstack_quickly": (
        "isaacsim-6.1.0/exts and extscache (untracked Isaac Sim install)"
    ),
    "test_kit_rpc::test_real_live_scene_control_server_when_running_inside_kit": (
        "kit-app-template/.../live_scene_control.py (submodule, not checked out)"
    ),
    "test_kit_rpc::test_basic_method_list_matches_live_scene_control_source": (
        "kit-app-template/.../live_scene_control.py (submodule, not checked out)"
    ),
    "test_isaac_reload_script::test_reload_without_an_open_stage_keeps_the_old_bridge_and_reports_it": (
        "isaacsim-study/reload_mcp_bridge.py (tracked, so it normally runs)"
    ),
}


def skip_reason(module: str, test_name: str) -> str | None:
    """Why ``module::test_name`` must be skipped in this checkout or on this platform, or None when it can run."""
    key = f"{module}::{test_name}"
    if key in WINDOWS_ONLY_TESTS and os.name != "nt":
        return "Windows-only: " + WINDOWS_ONLY_TESTS[key]
    needed = (*LOCAL_ONLY_TESTS.get(f"{module}::*", ()), *LOCAL_ONLY_TESTS.get(key, ()))
    missing = [path for path in needed if not (WORKSPACE_ROOT / path).exists()]
    if missing:
        return "needs local-only workspace files that are not in git: " + ", ".join(missing)
    return None


# ------------------------------------------------------------------------------------------------- network guard

# Bowerbot 8181; OmniUI/Kit/USD/Isaac knowledge MCPs 9901-9904; router 9905; scene service and bridges up to 9911.
LIVE_STACK_PORTS = frozenset({8181, *range(9901, 9912)})
ALLOW_NETWORK_ENV = "OMNIVERSE_SCENE_MCP_ALLOW_NETWORK"
BLOCKED_ATTEMPTS: list[str] = []
_LOCAL_NAMES = frozenset({"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_ORIGINALS: dict[str, object] = {}


class NetworkBlockedError(ConnectionRefusedError):
    """Raised instead of a connection the tests must never make; looks like 'nothing is listening' to the code."""


def _is_local_host(host: object) -> bool:
    """True for loopback/unspecified addresses, localhost names and None (getaddrinfo's passive lookup)."""
    if host is None:
        return True
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    text = str(host).strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text in _LOCAL_NAMES or text.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def blocked_reason(address: object) -> str | None:
    """Why connecting to ``address`` is refused under the guard, or None (AF_UNIX paths are always allowed)."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None
    host, port = address[0], address[1]
    if not _is_local_host(host):
        return f"non-local address {host}:{port}"
    if isinstance(port, int) and port in LIVE_STACK_PORTS:
        return f"live Omniverse service port {host}:{port}"
    return None


def _record(reason: str) -> str:
    if len(BLOCKED_ATTEMPTS) < 100:
        BLOCKED_ATTEMPTS.append(reason)
    return f"network access blocked in tests ({reason}); set {ALLOW_NETWORK_ENV}=1 to allow"


def _guarded_connect(self: socket.socket, address: object) -> None:
    reason = blocked_reason(address)
    if reason:
        raise NetworkBlockedError(errno.ECONNREFUSED, _record(reason))
    return _ORIGINALS["connect"](self, address)  # type: ignore[operator]


def _guarded_connect_ex(self: socket.socket, address: object) -> int:
    reason = blocked_reason(address)
    if reason:
        _record(reason)
        return errno.ECONNREFUSED
    return _ORIGINALS["connect_ex"](self, address)  # type: ignore[operator]


def _guarded_getaddrinfo(host: object, port: object, *args: object, **kwargs: object) -> list:
    if not _is_local_host(host):
        raise socket.gaierror(socket.EAI_NONAME, _record(f"DNS lookup of {host!r}"))
    return _ORIGINALS["getaddrinfo"](host, port, *args, **kwargs)  # type: ignore[operator]


def install_network_guard() -> bool:
    """Patch socket connect/connect_ex/getaddrinfo once; False when disabled by env or running inside Kit."""
    if _ORIGINALS:
        return True
    if os.environ.get(ALLOW_NETWORK_ENV, "").strip().lower() in ("1", "true", "yes"):
        return False
    if "carb" in sys.modules or "omni.kit.app" in sys.modules:
        return False  # never patch a live Kit process
    _ORIGINALS.update(
        connect=socket.socket.connect, connect_ex=socket.socket.connect_ex, getaddrinfo=socket.getaddrinfo
    )
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
    return True


NETWORK_GUARD_ACTIVE = install_network_guard()

# ------------------------------------------------------------------------------------------------- pytest hooks

try:
    import pytest
except ImportError:  # tools/run_tests.py imports this file without pytest installed
    pytest = None
else:

    @pytest.fixture(autouse=True)
    def _offline_runtime(monkeypatch):
        monkeypatch.setenv("OMNIVERSE_SCENE_MCP_TEST", "1")

    def pytest_collection_modifyitems(config, items):
        """Apply skip_reason() to every collected test, exactly as tools/run_tests.py does."""
        for item in items:
            path = getattr(item, "path", None) or Path(str(item.fspath))
            name = getattr(item, "originalname", None) or item.name
            reason = skip_reason(Path(path).stem, name)
            if reason:
                item.add_marker(pytest.mark.skip(reason=reason))

    def pytest_terminal_summary(terminalreporter, exitstatus, config):
        """Report connection attempts the network guard refused (they do not fail the run by themselves)."""
        if BLOCKED_ATTEMPTS:
            terminalreporter.write_line(
                f"network guard refused {len(BLOCKED_ATTEMPTS)} connection attempt(s): {BLOCKED_ATTEMPTS[:5]}"
            )
