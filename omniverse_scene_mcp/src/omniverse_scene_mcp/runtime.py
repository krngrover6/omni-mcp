from __future__ import annotations

import json
import os
import threading
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .http_bridge import NoRedirect, bridge_request_headers

# Loopback only: never route the scene-bridge probe through HTTP(S)_PROXY, and never follow a redirect to another
# host (a 3xx raises HTTPError, an OSError, so the probe reports the endpoint as not reachable).
urlopen = build_opener(ProxyHandler({}), NoRedirect()).open

from .exam import daily as exam_daily
from .exam import dashboard as exam_dashboard
from .exam.mock import FILE_NAME as MOCK_FILE_NAME
from .exam.mock import MockExamStore
from .exam.progress import ENV_DIR as EXAM_PREP_DIR_ENV
from .exam.progress import FILE_NAME as PROGRESS_FILE_NAME
from .exam.progress import ProgressStore
from .exam.progress import workspace_root as exam_workspace_root
from .fake_bridge import FakeSceneBridge
from .http_bridge import HttpSceneBridge
from .kit_bridge import LiveKitSceneBridge
from .knowledge import (
    build_bowerbot_backend,
    build_isaac_sim_backend,
    build_kit_backend,
    build_omni_ui_backend,
    build_usd_backend,
)
from .offline_docs_knowledge import DOMAINS as DOCS_DOMAINS
from .offline_docs_knowledge import OfflineDocsKnowledge, OfflineDocsKnowledgeBackend
from .offline_docs_knowledge import normalize_domain as normalize_docs_domain
from .offline_knowledge import FallbackKnowledgeBackend, OfflineUSDKnowledge, OfflineUSDKnowledgeBackend
from .router import NullKnowledgeBackend, OmniverseRouter
from .service import SceneMCPService

TEST_ENV = "OMNIVERSE_SCENE_MCP_TEST"
AUTO_SCENE_ENDPOINT = "http://127.0.0.1:9910"
_OFF_VALUES = {"", "0", "false", "no", "off"}

# Scene targets the running router can switch between without a restart (switch_scene_target).
SCENE_TARGET_HOST = "127.0.0.1"
SCENE_TARGET_PROBE_TIMEOUT = 0.3
DEFAULT_SCENE_TARGETS: dict[str, str | None] = {
    "isaac": "http://127.0.0.1:9911",
    "composer": "http://127.0.0.1:9910",
    "fake": None,
}
SCENE_TARGET_ENV: dict[str, str] = {
    "isaac": "OMNIVERSE_ISAAC_SCENE_ENDPOINT",
    "composer": "OMNIVERSE_COMPOSER_SCENE_ENDPOINT",
}
_SCENE_TARGET_ALIASES = {
    "isaac sim": "isaac",
    "isaacsim": "isaac",
    "isaac_sim": "isaac",
    "isaac-sim": "isaac",
    "usd composer": "composer",
    "usd_composer": "composer",
    "usd-composer": "composer",
    "in-memory": "fake",
    "memory": "fake",
}
_SWITCH_LOCK = threading.Lock()


def is_test_mode(environ: Mapping[str, str] | None = None) -> bool:
    """True when OMNIVERSE_SCENE_MCP_TEST asks for a fake scene and offline knowledge backends."""
    env = os.environ if environ is None else environ
    return env.get(TEST_ENV, "").strip().lower() not in _OFF_VALUES


def choose_scene_backend(
    environ: Mapping[str, str] | None = None,
    detect_endpoint: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Decide which scene bridge to use and record why, without building it.

    Modes: test, http_env, http_autodetect, live_kit, fake (deliberate) and fake_fallback (silent fallback).
    """
    env = os.environ if environ is None else environ
    if is_test_mode(env):
        return _backend_info("test", "FakeSceneBridge", f"{TEST_ENV} is set: offline fake scene, no endpoint read or probed")

    mode_raw = env.get("OMNIVERSE_SCENE_MCP_MODE", "").strip()
    mode = mode_raw.lower()
    endpoint = env.get("OMNIVERSE_SCENE_HTTP_ENDPOINT", "").strip()
    info = None
    if endpoint:
        info = _backend_info("http_env", "HttpSceneBridge", "OMNIVERSE_SCENE_HTTP_ENDPOINT is set", endpoint)
    else:
        detected = (detect_endpoint or _detect_live_scene_endpoint)()
        if detected:
            info = _backend_info("http_autodetect", "HttpSceneBridge", f"{detected}/health answered ok", detected)
    if info is not None:
        if mode:
            info["warnings"].append(f"OMNIVERSE_SCENE_MCP_MODE={mode_raw} is ignored because an HTTP scene endpoint wins.")
        return info

    if mode == "live":
        info = _backend_info("live_kit", "LiveKitSceneBridge", "OMNIVERSE_SCENE_MCP_MODE=live (in-process Kit stage)")
        info["usd_context_name"] = env.get("OMNIVERSE_USD_CONTEXT_NAME", "").strip()
        return info
    if mode == "fake":
        return _backend_info("fake", "FakeSceneBridge", "OMNIVERSE_SCENE_MCP_MODE=fake")

    why = f"OMNIVERSE_SCENE_MCP_MODE={mode_raw!r} is not live or fake" if mode else "OMNIVERSE_SCENE_MCP_MODE is unset"
    info = _backend_info(
        "fake_fallback",
        "FakeSceneBridge",
        f"{why}, OMNIVERSE_SCENE_HTTP_ENDPOINT is unset and nothing answered {AUTO_SCENE_ENDPOINT}/health",
    )
    info["silent_fallback"] = True
    info["warnings"].append("Scene tools use an in-memory fake scene; changes do not reach Composer or Isaac Sim.")
    return info


def build_scene_service(
    environ: Mapping[str, str] | None = None,
    detect_endpoint: Callable[[], str | None] | None = None,
) -> SceneMCPService:
    """Build a scene service for either fake or live Omniverse usage; service.backend_info says which."""
    info = choose_scene_backend(environ, detect_endpoint)
    if info["bridge"] == "HttpSceneBridge":
        bridge = HttpSceneBridge(info["endpoint"])
    elif info["bridge"] == "LiveKitSceneBridge":
        bridge = LiveKitSceneBridge(info["usd_context_name"])
    else:
        bridge = FakeSceneBridge()
    return SceneMCPService(bridge, backend_info=info)


def scene_targets(environ: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """Named scene bridges switch_scene_target can use: isaac (9911), composer (9910) and the in-memory fake.

    $OMNIVERSE_ISAAC_SCENE_ENDPOINT / $OMNIVERSE_COMPOSER_SCENE_ENDPOINT override a URL; an override that is not
    exactly http://127.0.0.1:<port> is ignored, so a switch only ever probes this machine's loopback.
    """
    env = os.environ if environ is None else environ
    targets = dict(DEFAULT_SCENE_TARGETS)
    for name, variable in SCENE_TARGET_ENV.items():
        override = _loopback_endpoint(env.get(variable, ""))
        if override:
            targets[name] = override
    return targets


def probe_scene_health(endpoint: str, timeout: float = SCENE_TARGET_PROBE_TIMEOUT) -> dict[str, Any]:
    """GET <endpoint>/health (short timeout, http://127.0.0.1:<port> only); {reachable, probed, status|error}."""
    url = _loopback_endpoint(endpoint)
    if url is None:
        return {"reachable": False, "probed": False, "error": "only http://127.0.0.1:<port> endpoints are probed"}
    try:
        with urlopen(_health_request(url), timeout=timeout) as response:
            data = json.loads(response.read(4096).decode("utf-8"))
    except (OSError, HTTPException, ValueError) as exc:
        return {"reachable": False, "probed": True, "error": _clip(f"{type(exc).__name__}: {exc}")}
    if isinstance(data, dict) and data.get("ok") is True:
        return {"reachable": True, "probed": True, "status": _clip(str(data.get("status", "ok")))}
    return {"reachable": False, "probed": True, "error": "/health answered without ok=true"}


def switch_scene_target(
    service: SceneMCPService,
    target: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    environ: Mapping[str, str] | None = None,
    probe: Callable[[str, float], Any] | None = None,
    bridge_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Point the running router's scene service at 'isaac', 'composer' or 'fake' without restarting anything.

    An HTTP target is probed first (GET /health, 300 ms, 127.0.0.1 only) and refused when it does not answer unless
    force=True. Then a new HttpSceneBridge (or a fresh FakeSceneBridge) is swapped into the SAME service, so the
    router and every server tool use it at once; no scene or layer is touched. dry_run=True only probes and reports.
    Under OMNIVERSE_SCENE_MCP_TEST nothing opens a socket: without an injected probe the target counts as not
    reachable, and capabilities are never fetched from an HttpSceneBridge. Never raises for a bad or down target.
    Returns {ok, target, endpoint, previous, current, changed, reachable, health?, capabilities?, warnings, error?}.
    """
    scene = getattr(service, "_scene", None)
    if not callable(getattr(service, "swap_bridge", None)) and callable(getattr(scene, "swap_bridge", None)):
        service = scene  # an OmniverseRouter was passed; switch its scene service
    env = os.environ if environ is None else environ
    test_mode = is_test_mode(env)
    targets = scene_targets(env)
    name = _scene_target_name(target)
    with _SWITCH_LOCK:
        previous = service.bridge_info()
        result: dict[str, Any] = {
            "ok": False,
            "target": name or _clip(repr(target)),
            "endpoint": None,
            "previous": previous,
            "current": previous,
            "changed": False,
            "dry_run": bool(dry_run),
            "forced": bool(force),
            "reachable": None,
            "warnings": [],
        }
        if name not in targets:
            result["error"] = f"Unknown scene target {_clip(repr(target))}; use one of {sorted(targets)}"
            result["targets"] = targets
            return result
        endpoint = targets[name]
        result["endpoint"] = endpoint
        if endpoint is None:
            _switch_to_fake(service, result, dry_run)
        else:
            if probe is None and test_mode:
                health = {"reachable": False, "probed": False, "error": f"{TEST_ENV} is set: not probed (no sockets)"}
            else:
                health = _health_result(probe or probe_scene_health, endpoint)
            reachable = health["reachable"]
            result["reachable"], result["health"] = reachable, health
            if not reachable and not force:
                result["error"] = (
                    f"The {name} scene bridge at {endpoint} did not answer /health "
                    f"({health.get('error') or 'no details'}); {_SCENE_TARGET_HINTS[name]}, or pass force=True to "
                    "switch anyway."
                )
                return result
            same = previous.get("endpoint") == endpoint
            if dry_run:
                result.update(ok=True, would_change=not same)
            else:
                # Same endpoint: keep the (stateless) bridge; only an earlier switch's record is refreshed.
                bridge = None if same else (bridge_factory or HttpSceneBridge)(endpoint)
                if bridge is None and previous.get("mode") == "http_switch":
                    bridge = service._bridge
                if bridge is not None:
                    how = "answered /health" if reachable else "did not answer /health (force=True)"
                    reason = f"switch_scene_target('{name}'): {endpoint} {how}"
                    info = _backend_info("http_switch", type(bridge).__name__, reason, endpoint)
                    info["target"] = name
                    if not reachable:
                        info["warnings"].append(f"Switched with force=True; scene tools fail until {endpoint} is up.")
                    service.swap_bridge(bridge, info)
                result.update(ok=True, changed=not same, current=service.bridge_info())
            if not reachable:
                result["warnings"].append(f"{endpoint} did not answer /health; scene tools fail until it is up.")
    if result["ok"] and not dry_run and (endpoint is None or result["reachable"]):
        if test_mode and result["current"].get("bridge") == "HttpSceneBridge":
            result["warnings"].append(f"Capabilities not fetched: {TEST_ENV} is set and test mode opens no sockets.")
        else:
            result["capabilities"] = service.live_capabilities()  # never raises
    return result


_SCENE_TARGET_HINTS = {
    "isaac": (
        "start Isaac Sim with start-isaacsim.ps1 (or run isaacsim-study/reload_mcp_bridge.py in its Script Editor)"
    ),
    "composer": (
        "start Composer with start-composer-study.ps1 (or run composer-study/reload_composer_bridge.py in its "
        "Script Editor)"
    ),
}


def _switch_to_fake(service: SceneMCPService, result: dict[str, Any], dry_run: bool) -> None:
    """Swap a fresh FakeSceneBridge in, unless the service already uses one (its in-memory edits are kept)."""
    same = result["previous"].get("bridge") == "FakeSceneBridge"
    if dry_run:
        result.update(ok=True, would_change=not same)
        return
    if same:
        result["warnings"].append("Already on the in-memory fake scene; kept it and its edits.")
    else:
        info = _backend_info("fake", "FakeSceneBridge", "switch_scene_target('fake'): in-memory fake scene")
        info["target"] = "fake"
        info["warnings"].append("Scene tools use an in-memory fake scene; changes do not reach Composer or Isaac Sim.")
        service.swap_bridge(FakeSceneBridge(), info)
    result.update(ok=True, changed=not same, current=service.bridge_info())


def _health_result(probe: Callable[[str, float], Any], endpoint: str) -> dict[str, Any]:
    """Run a probe (bool or dict result) and normalize it; a probe that raises counts as not reachable."""
    try:
        raw = probe(endpoint, SCENE_TARGET_PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - a failing probe is a down target, not a router error
        return {"reachable": False, "probed": True, "error": _clip(f"{type(exc).__name__}: {exc}")}
    if isinstance(raw, bool):
        return {"reachable": raw, "probed": True}
    if not isinstance(raw, dict):
        return {"reachable": False, "probed": True, "error": "the probe returned no result"}
    health = {key: _clip(str(raw[key])) for key in ("status", "error") if raw.get(key) is not None}
    health["reachable"] = raw.get("reachable") is True
    health["probed"] = raw.get("probed", True) is not False
    return health


def _scene_target_name(target: Any) -> str:
    text = " ".join(target.strip().lower().split()) if isinstance(target, str) else ""
    return _SCENE_TARGET_ALIASES.get(text, text)


def _loopback_endpoint(text: Any) -> str | None:
    """'http://127.0.0.1:<port>' for such a URL (trailing slash allowed), else None: other hosts, paths or creds."""
    raw = text.strip().rstrip("/") if isinstance(text, str) else ""
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.lower() != "http"
        or parts.hostname != SCENE_TARGET_HOST
        or not port
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
    ):
        return None
    return f"http://{SCENE_TARGET_HOST}:{port}"


def _clip(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_router(
    environ: Mapping[str, str] | None = None,
    detect_endpoint: Callable[[], str | None] | None = None,
    *,
    offline_knowledge: OfflineUSDKnowledge | None = None,
    progress_store: ProgressStore | None = None,
    mock_store: MockExamStore | None = None,
    docs_knowledge: Mapping[str, OfflineDocsKnowledge] | None = None,
) -> OmniverseRouter:
    """Build the single interface used to route Omniverse requests.

    Outside test mode the USD backend falls back to the bundled offline docs when the USD docs MCP is down, and the
    Kit, OmniUI and Isaac Sim backends fall back to the installed Isaac Sim extension docs (offline_docs_knowledge);
    ``offline_knowledge`` / ``docs_knowledge`` let the server share its lazily loaded indexes with its doc tools.
    Under the test switch every knowledge backend is a NullKnowledgeBackend: no socket, no install folder read.
    With ``progress_store`` the router answers "start day 3" / "next lesson" with the daily lesson itself; with
    ``mock_store`` its mock-exam instructions mention an open mock. Without them it only names the tool to call.
    """
    scene = build_scene_service(environ, detect_endpoint)
    exam_hooks = _exam_hooks(progress_store, mock_store, offline_knowledge)
    if is_test_mode(environ):
        # Offline: no upstream endpoints are read and NullKnowledgeBackend never opens a socket.
        return OmniverseRouter(
            scene,
            usd_backend=NullKnowledgeBackend("usd"),
            kit_backend=NullKnowledgeBackend("kit"),
            omni_ui_backend=NullKnowledgeBackend("omni_ui"),
            isaac_sim_backend=NullKnowledgeBackend("isaac_sim"),
            bowerbot_backend=NullKnowledgeBackend("bowerbot"),
            **exam_hooks,
        )
    docs = docs_knowledge or {}
    return OmniverseRouter(
        scene,
        usd_backend=build_usd_backend_with_fallback(offline_knowledge),
        kit_backend=build_docs_backend_with_fallback("kit", docs.get("kit")),
        omni_ui_backend=build_docs_backend_with_fallback("omni_ui", docs.get("omni_ui")),
        isaac_sim_backend=build_docs_backend_with_fallback("isaac_sim", docs.get("isaac_sim")),
        bowerbot_backend=build_bowerbot_backend(),
        **exam_hooks,
    )


# Upstream docs MCP builders for the domains the installed Isaac Sim / Kit extension docs can answer offline.
_DOCS_BACKEND_BUILDERS: dict[str, Callable[..., Any]] = {
    "kit": build_kit_backend,
    "omni_ui": build_omni_ui_backend,
    "isaac_sim": build_isaac_sim_backend,
}


def build_docs_knowledge(roots: Any = None) -> dict[str, OfflineDocsKnowledge]:
    """One OfflineDocsKnowledge per domain (kit, isaac_sim, omni_ui), sharing the process-wide docs catalog.

    Building them only resolves folder paths; the catalog and each domain's index load on the first question.
    """
    return {domain: OfflineDocsKnowledge(domain, roots=roots) for domain in DOCS_DOMAINS}


def build_docs_backend_with_fallback(
    domain: str,
    knowledge: OfflineDocsKnowledge | None = None,
    *,
    endpoint: str | None = None,
    probe: Callable[..., Any] | None = None,
) -> FallbackKnowledgeBackend:
    """The Kit / OmniUI / Isaac Sim docs MCP backend, answered from the installed extension docs when it is down.

    Building it opens nothing; the fallback's 300 ms TCP probe of the upstream endpoint only runs when a question
    is asked (probe= replaces it in tests). The offline answer carries a note and primary_error.
    """
    name = normalize_docs_domain(domain)
    primary = _DOCS_BACKEND_BUILDERS[name](endpoint)
    offline = OfflineDocsKnowledgeBackend(name, knowledge)
    return FallbackKnowledgeBackend(primary, offline, probe=probe)


def _exam_hooks(
    progress_store: ProgressStore | None,
    mock_store: MockExamStore | None,
    offline_knowledge: OfflineUSDKnowledge | None,
) -> dict[str, Any]:
    """Router callbacks for the daily lesson and the open-mock check; the same calls the server's exam tools make."""
    hooks: dict[str, Any] = {}
    if progress_store is not None:

        def exam_day(day: int | None) -> dict[str, Any]:
            return exam_daily.build_day(day, store=progress_store, knowledge=offline_knowledge)

        def dashboard(write: bool) -> dict[str, Any]:
            return exam_dashboard_result(progress_store, mock_store, write=write)

        hooks["exam_day"] = exam_day
        hooks["dashboard"] = dashboard
    if mock_store is not None:
        hooks["mock_status"] = mock_store.active
    return hooks


def exam_dashboard_result(
    progress_store: ProgressStore,
    mock_store: MockExamStore | None = None,
    *,
    write: bool = True,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Dashboard data (read-only) plus, with write=True, dashboard.html saved next to progress.json.

    The page goes to the exam-prep folder ($OMNIVERSE_EXAM_PREP_DIR, else <workspace>/exam-prep) or the progress
    store's own folder, never elsewhere (exam.dashboard.write_dashboard). Under the test switch the real workspace's
    exam-prep folder is never written: the result says why and nothing is saved. Never raises.
    """
    try:
        data = exam_dashboard.dashboard_data(progress_store, mock_store=mock_store)
    except Exception as exc:  # noqa: BLE001 - an unreadable store is reported, not raised
        return {"ok": False, "message": f"Could not build the dashboard: {type(exc).__name__}: {exc}"}
    page: dict[str, Any] = {"written": False, "path": None}
    if write is True:
        refusal = _dashboard_write_refusal(environ)
        if refusal is not None:
            page.update(message=refusal, skipped=True)
        else:
            try:
                saved = exam_dashboard.write_dashboard(progress_store, mock_store=mock_store)
            except Exception as exc:  # noqa: BLE001
                saved = {"ok": False, "path": None, "message": f"{type(exc).__name__}: {exc}"}
            page.update(written=bool(saved.get("ok")), path=saved.get("path"), bytes=saved.get("bytes", 0))
            if saved.get("ok"):
                page["message"] = (
                    "Saved a self-contained page (no scripts, fonts or network); open it in a browser. It is rebuilt "
                    "each time, so it always shows your latest progress."
                )
            else:
                page["message"] = f"The dashboard page was not saved: {saved.get('message') or 'unknown reason'}"
    else:
        page["message"] = "write=False: nothing was saved."
    return {**data, "dashboard": page}


def _dashboard_write_refusal(environ: Mapping[str, str] | None = None) -> str | None:
    """Why the dashboard must not be written now (test mode aimed at the real exam-prep folder), or None."""
    if not is_test_mode(environ):
        return None
    real = (Path(__file__).resolve().parents[3] / "exam-prep").resolve()
    try:
        target = exam_dashboard.default_dashboard_path().resolve()
    except OSError:
        return "Test mode: the dashboard path could not be resolved, so nothing was saved."
    if target.parent == real or real in target.parents:
        return (
            f"Test mode ({TEST_ENV}): the real exam-prep folder is never written; set {EXAM_PREP_DIR_ENV} to a "
            "temporary folder to save the page."
        )
    return None


def build_usd_backend_with_fallback(offline_knowledge: OfflineUSDKnowledge | None = None) -> FallbackKnowledgeBackend:
    """The USD docs MCP backend, answered from the bundled offline USD docs whenever that MCP is not running.

    Building it opens nothing; the fallback's 300 ms TCP probe only runs when a question is asked.
    """
    primary = build_usd_backend()
    return FallbackKnowledgeBackend(primary, OfflineUSDKnowledgeBackend(offline_knowledge, selector=primary.selector))


def build_progress_store(environ: Mapping[str, str] | None = None) -> ProgressStore:
    """The exam ProgressStore shared by one server.

    ``$OMNIVERSE_EXAM_PREP_DIR/progress.json`` when that is set; otherwise memory-only under the test switch
    (nothing touches the disk); otherwise ``<workspace>/exam-prep/progress.json``. Nothing is created until a write.
    """
    env = os.environ if environ is None else environ
    folder = env.get(EXAM_PREP_DIR_ENV, "").strip()
    if folder:
        return ProgressStore(Path(folder).expanduser() / PROGRESS_FILE_NAME)
    if is_test_mode(env):
        return ProgressStore(memory_only=True)
    return ProgressStore(exam_workspace_root() / "exam-prep" / PROGRESS_FILE_NAME)


def build_mock_store(environ: Mapping[str, str] | None = None) -> MockExamStore:
    """The timed mock-exam session store, with the same folder rules as build_progress_store.

    ``$OMNIVERSE_EXAM_PREP_DIR/mock_sessions.json`` when that is set; otherwise memory-only under the test switch;
    otherwise ``<workspace>/exam-prep/mock_sessions.json``. Nothing is created until a mock starts.
    """
    env = os.environ if environ is None else environ
    folder = env.get(EXAM_PREP_DIR_ENV, "").strip()
    if folder:
        return MockExamStore(Path(folder).expanduser() / MOCK_FILE_NAME)
    if is_test_mode(env):
        return MockExamStore(memory_only=True)
    return MockExamStore(exam_workspace_root() / "exam-prep" / MOCK_FILE_NAME)


def _backend_info(mode: str, bridge: str, reason: str, endpoint: str | None = None) -> dict[str, Any]:
    return {
        "mode": mode,
        "bridge": bridge,
        "endpoint": endpoint,
        "reason": reason,
        "silent_fallback": False,
        "warnings": [],
    }


def _detect_live_scene_endpoint() -> str | None:
    candidate = AUTO_SCENE_ENDPOINT
    try:
        with urlopen(_health_request(candidate), timeout=1) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPException, ValueError):
        return None
    if isinstance(data, dict) and data.get("ok") is True:
        return candidate
    return None


def _health_request(endpoint: str) -> Request:
    """GET <endpoint>/health with the bridge's request headers (JSON type, the shared token when set, never Origin),
    so the probes keep working when OMNIVERSE_BRIDGE_TOKEN is set on both sides."""
    return Request(f"{endpoint}/health", headers=bridge_request_headers(), method="GET")
