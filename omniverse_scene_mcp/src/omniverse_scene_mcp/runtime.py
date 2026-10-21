from __future__ import annotations

import json
import os
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import ProxyHandler, build_opener

# Loopback only: never route the scene-bridge probe through HTTP(S)_PROXY.
urlopen = build_opener(ProxyHandler({})).open

from .exam import daily as exam_daily
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
from .offline_knowledge import FallbackKnowledgeBackend, OfflineUSDKnowledge, OfflineUSDKnowledgeBackend
from .router import NullKnowledgeBackend, OmniverseRouter
from .service import SceneMCPService

TEST_ENV = "OMNIVERSE_SCENE_MCP_TEST"
AUTO_SCENE_ENDPOINT = "http://127.0.0.1:9910"
_OFF_VALUES = {"", "0", "false", "no", "off"}


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


def build_router(
    environ: Mapping[str, str] | None = None,
    detect_endpoint: Callable[[], str | None] | None = None,
    *,
    offline_knowledge: OfflineUSDKnowledge | None = None,
    progress_store: ProgressStore | None = None,
    mock_store: MockExamStore | None = None,
) -> OmniverseRouter:
    """Build the single interface used to route Omniverse requests.

    Outside test mode the USD backend falls back to the bundled offline docs when the USD docs MCP is down;
    ``offline_knowledge`` lets the server share one lazily loaded offline index with its doc tools.
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
    return OmniverseRouter(
        scene,
        usd_backend=build_usd_backend_with_fallback(offline_knowledge),
        kit_backend=build_kit_backend(),
        omni_ui_backend=build_omni_ui_backend(),
        isaac_sim_backend=build_isaac_sim_backend(),
        bowerbot_backend=build_bowerbot_backend(),
        **exam_hooks,
    )


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

        hooks["exam_day"] = exam_day
    if mock_store is not None:
        hooks["mock_status"] = mock_store.active
    return hooks


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
        with urlopen(f"{candidate}/health", timeout=1) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPException, ValueError):
        return None
    if isinstance(data, dict) and data.get("ok") is True:
        return candidate
    return None
