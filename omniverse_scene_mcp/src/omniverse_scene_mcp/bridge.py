from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Version of the extended live RPC (kit_rpc.make_extended_server_class); bump when its methods change.
RPC_VERSION = "tier2-1"
# Methods every LiveSceneControlServer (live_scene_control.py) answers, Composer's 9910 bridge included.
BASIC_RPC_METHODS: tuple[str, ...] = (
    "ping",
    "selection",
    "select",
    "snapshot",
    "summary",
    "inspect",
    "children",
    "create",
    "delete",
    "duplicate",
    "reparent",
    "rename",
    "move",
    "material",
    "variants",
    "variant_set",
    "timeline",
    "timeline_set",
    "timeline_play",
    "timeline_stop",
    "make_red",
    "find",
    "save",
)
# Methods only an extended bridge answers; an older bridge replies "Unsupported live scene method: <name>".
LIVE_RPC_METHODS: tuple[str, ...] = ("capabilities", "insight", "create_light", "create_camera", "aim")
# Read-only insight queries a live bridge runs on the open stage (usd_insight / usd_validate functions).
INSIGHT_QUERIES: tuple[str, ...] = ("stage_info", "composition", "explain_value", "properties", "bounds", "validate")
UNSUPPORTED_METHOD_PREFIX = "Unsupported live scene method"


def unsupported_result(method: str, message: str, *, needs_bridge_update: bool, **extra: Any) -> dict[str, Any]:
    """Structured 'this bridge cannot do that' result, returned instead of raising."""
    return {
        "ok": False,
        "unsupported": True,
        "needs_bridge_update": bool(needs_bridge_update),
        "method": method,
        "message": message,
        **extra,
    }


@dataclass(slots=True)
class PrimRecord:
    path: str
    type_name: str = "Xform"
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    translation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation: tuple[float, float, float] = (0.0, 0.0, 0.0)
    scale: tuple[float, float, float] = (1.0, 1.0, 1.0)
    material: str | None = None


@dataclass(slots=True)
class SceneSnapshot:
    stage_name: str
    root_path: str
    selection: list[str]
    prim_count: int
    prims: list[PrimRecord]


@dataclass(slots=True)
class ChangeRecord:
    action: str
    path: str
    summary: str
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None


class SceneBridge(Protocol):
    """Adapter contract for a live Omniverse or USD scene."""

    def ping(self) -> str: ...

    def snapshot(self) -> SceneSnapshot: ...

    def get_selection(self) -> list[str]: ...

    def set_selection(self, paths: list[str]) -> None: ...

    def get_prim(self, path: str) -> PrimRecord | None: ...

    def list_children(self, path: str) -> list[str]: ...

    def create_prim(self, path: str, type_name: str = "Xform") -> PrimRecord: ...

    def delete_prim(self, path: str) -> None: ...

    def duplicate_prim(self, source_path: str, target_path: str) -> PrimRecord: ...

    def reparent_prim(self, path: str, new_parent: str) -> PrimRecord: ...

    def rename_prim(self, path: str, new_name: str) -> PrimRecord: ...

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
    ) -> PrimRecord: ...

    def set_material(self, path: str, material_path: str) -> PrimRecord: ...

    def get_variant_selections(self, path: str) -> dict[str, str]: ...

    def set_variant_selection(self, path: str, variant_set: str, selection: str) -> dict[str, str]: ...

    def get_timeline_state(self) -> dict[str, object]: ...

    def set_timeline_time(self, time_code: float) -> dict[str, object]: ...

    def play_timeline(self, start_time_code: float, end_time_code: float) -> dict[str, object]: ...

    def stop_timeline(self) -> dict[str, object]: ...

    def find_prims(self, query: str, scope_path: str | None = None) -> list[PrimRecord]: ...

    def save_scene(self, path: str) -> str: ...


class LiveSceneBridge(SceneBridge, Protocol):
    """Optional live extensions. A SceneBridge may lack these; callers check with getattr on these fixed names.

    Each returns a JSON-safe dict. When the backend cannot run the method (fake scene, bridge that predates it),
    it returns unsupported_result(...) instead of raising. Authoring methods default to dry_run=True.
    """

    def capabilities(self) -> dict[str, Any]: ...

    def insight(self, query: str, args: dict[str, Any] | None = None) -> dict[str, Any]: ...

    def create_light(
        self,
        path: str,
        light_type: str,
        *,
        intensity: float | None = None,
        exposure: float | None = None,
        color: tuple[float, float, float] | None = None,
        size: float | None = None,
        translation: tuple[float, float, float] | None = None,
        aim_at: str | tuple[float, float, float] | None = None,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    def create_camera(
        self,
        path: str,
        *,
        focal_length: float | None = None,
        horizontal_aperture: float | None = None,
        clipping_range: tuple[float, float] | None = None,
        translation: tuple[float, float, float] | None = None,
        aim_at: str | tuple[float, float, float] | None = None,
        copy_optics_from: str | None = None,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    def aim(
        self,
        path: str,
        target: str | tuple[float, float, float],
        *,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...
