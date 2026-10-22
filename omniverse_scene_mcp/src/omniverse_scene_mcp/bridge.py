from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Version of the extended live RPC (kit_rpc.make_extended_server_class); bump when its methods change.
RPC_VERSION = "tier5-1"
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
# Safe attribute/material edits (edits.py) and MCP undo (undo.py, kit_commands.py), added in RPC tier3-1.
EDIT_RPC_METHODS: tuple[str, ...] = ("set_attribute", "create_material", "bind_material")
UNDO_RPC_METHODS: tuple[str, ...] = ("undo_last", "undo_history")
# Added in RPC tier4-1: reference an asset file (asset_library.add_reference), UsdPhysics setup (physics.py), and the
# Kit-only viewport capture and simulation controls (kit_live_extras.py). The asset index itself stays in the router.
ASSET_RPC_METHODS: tuple[str, ...] = ("add_reference",)
PHYSICS_RPC_METHODS: tuple[str, ...] = (
    "physics_report",
    "add_physics_scene",
    "make_rigid_body",
    "make_collider",
    "remove_physics",
)
CAPTURE_RPC_METHODS: tuple[str, ...] = ("capture_viewport", "capture_status")
SIMULATION_RPC_METHODS: tuple[str, ...] = (
    "simulation_status",
    "simulation_play",
    "simulation_pause",
    "simulation_stop",
    "simulation_step",
)
TIER4_RPC_METHODS: tuple[str, ...] = (
    *ASSET_RPC_METHODS,
    *PHYSICS_RPC_METHODS,
    *CAPTURE_RPC_METHODS,
    *SIMULATION_RPC_METHODS,
)
# Added in RPC tier5-1: read-only learning helpers that run on the open stage, the homework check
# (assignments.check_assignment) and the beginner scene tour (scene_tour.scene_tour). Neither writes anything.
HOMEWORK_RPC_METHODS: tuple[str, ...] = ("assignment_check", "scene_tour")
# Methods only an extended bridge answers; an older bridge replies "Unsupported live scene method: <name>".
LIVE_RPC_METHODS: tuple[str, ...] = (
    "capabilities",
    "insight",
    "create_light",
    "create_camera",
    "aim",
    *EDIT_RPC_METHODS,
    *UNDO_RPC_METHODS,
    *TIER4_RPC_METHODS,
    *HOMEWORK_RPC_METHODS,
)
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

    def set_attribute(
        self,
        path: str,
        name: str,
        value: Any,
        *,
        time: float | None = None,
        create_type: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    def create_material(
        self,
        path: str,
        *,
        diffuse_color: tuple[float, float, float] | str | None = None,
        roughness: float | None = None,
        metallic: float | None = None,
        opacity: float | None = None,
        emissive_color: tuple[float, float, float] | str | None = None,
        ior: float | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    def bind_material(
        self,
        prim_path: str,
        material_path: str,
        *,
        strength: str | None = None,
        purpose: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    def undo_last(self, dry_run: bool = True) -> dict[str, Any]: ...

    def undo_history(self, limit: int = 20) -> dict[str, Any]: ...

    # --- RPC tier4-1: assets, physics, viewport capture and simulation ------------------------------------------

    def add_reference(
        self,
        prim_path: str,
        asset_path: str,
        *,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: float | tuple[float, float, float] | None = None,
        instanceable: bool | None = None,
        asset_prim_path: str | None = None,
        correct_units: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    def physics_report(self, prim_path: str | None = None) -> dict[str, Any]: ...

    def add_physics_scene(
        self, path: str = "/physicsScene", *, gravity: Any = None, dry_run: bool = True
    ) -> dict[str, Any]: ...

    def make_rigid_body(
        self,
        prim_path: str,
        *,
        mass: float | None = None,
        density: float | None = None,
        kinematic: bool = False,
        add_collider: bool = True,
        approximation: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]: ...

    def make_collider(self, prim_path: str, *, approximation: str | None = None, dry_run: bool = True) -> dict[str, Any]: ...

    def remove_physics(
        self, prim_path: str, *, include_descendants: bool = False, dry_run: bool = True
    ) -> dict[str, Any]: ...

    def capture_viewport(
        self, output_dir: str | None = None, *, width: int | None = None, height: int | None = None
    ) -> dict[str, Any]: ...

    def capture_status(self, path: str) -> dict[str, Any]: ...

    def simulation_status(self) -> dict[str, Any]: ...

    def simulation_play(self) -> dict[str, Any]: ...

    def simulation_pause(self) -> dict[str, Any]: ...

    def simulation_stop(self) -> dict[str, Any]: ...

    def simulation_step(self, frames: int = 1) -> dict[str, Any]: ...

    # --- RPC tier5-1: read-only homework check and scene tour on the open stage ---------------------------------

    def assignment_check(self, assignment_id: str = "isaac_session02_warehouse") -> dict[str, Any]: ...

    def scene_tour(
        self, focus_path: str | None = None, *, depth: int = 3, max_prims: int = 400
    ) -> dict[str, Any]: ...
