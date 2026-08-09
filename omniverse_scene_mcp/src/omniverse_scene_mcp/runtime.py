from __future__ import annotations

import os
import json
from urllib.error import URLError
from urllib.request import urlopen

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
from .router import OmniverseRouter
from .service import SceneMCPService


def build_scene_service() -> SceneMCPService:
    """Build a scene service for either fake or live Omniverse usage."""
    http_endpoint = os.environ.get("OMNIVERSE_SCENE_HTTP_ENDPOINT", "").strip()
    if not http_endpoint:
        http_endpoint = _detect_live_scene_endpoint()
    if http_endpoint:
        return SceneMCPService(HttpSceneBridge(http_endpoint))

    mode = os.environ.get("OMNIVERSE_SCENE_MCP_MODE", "fake").strip().lower()
    if mode == "live":
        context_name = os.environ.get("OMNIVERSE_USD_CONTEXT_NAME", "").strip()
        return SceneMCPService(LiveKitSceneBridge(context_name))
    return SceneMCPService(FakeSceneBridge())


def build_router() -> OmniverseRouter:
    """Build the single interface used to route Omniverse requests."""
    return OmniverseRouter(
        build_scene_service(),
        usd_backend=build_usd_backend(),
        kit_backend=build_kit_backend(),
        omni_ui_backend=build_omni_ui_backend(),
        isaac_sim_backend=build_isaac_sim_backend(),
        bowerbot_backend=build_bowerbot_backend(),
    )


def _detect_live_scene_endpoint() -> str | None:
    candidate = "http://127.0.0.1:9910"
    try:
        with urlopen(f"{candidate}/health", timeout=1) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError):
        return None
    if data.get("ok") is True:
        return candidate
    return None
