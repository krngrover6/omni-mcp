from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    """Refuse 3xx: a loopback-only request must never be sent on to another host (it raises HTTPError instead)."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


# The scene bridge is a loopback service: never route its RPCs through HTTP(S)_PROXY or follow a redirect.
urlopen = build_opener(ProxyHandler({}), NoRedirect()).open

from .bridge import UNSUPPORTED_METHOD_PREFIX, PrimRecord, SceneSnapshot, unsupported_result
from .kit_rpc import BRIDGE_TOKEN_ENV, BRIDGE_TOKEN_HEADER

DEFAULT_TIMEOUT = 10.0
# The live server waits up to 15 s for Kit's main thread; wait a little longer so its own timeout message arrives.
LIVE_TIMEOUT = 20.0
COMPOSER_PORT = 9910
_FORBIDDEN_HINTS = {
    "token": (
        f"The bridge in Kit has {BRIDGE_TOKEN_ENV} set: set {BRIDGE_TOKEN_ENV} to the same value in the router's "
        "environment and restart the router."
    ),
    "host": "Point OMNIVERSE_SCENE_HTTP_ENDPOINT at http://127.0.0.1:<port> or http://localhost:<port>.",
}
_DEFAULT_FORBIDDEN_HINT = "The bridge's localhost guard refused the request; nothing was run in Kit."


class BridgeForbiddenError(ValueError):
    """HTTP 403 from the bridge's localhost guard; .result is the structured error (nothing was run in Kit)."""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result["message"])
        self.result = result


class HttpSceneBridge:
    """Bridge that talks to a live Composer-side scene control endpoint."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")

    def ping(self) -> str:
        response = self._rpc("ping")
        return str(response.get("status", "unknown"))

    def snapshot(self) -> SceneSnapshot:
        response = self._rpc("snapshot")
        return self._snapshot_from_dict(response)

    def get_selection(self) -> list[str]:
        response = self._rpc("selection")
        return list(response.get("selection", []))

    def set_selection(self, paths: list[str]) -> None:
        self._rpc("select", {"paths": paths})

    def get_prim(self, path: str) -> PrimRecord | None:
        response = self._rpc("inspect", {"path": path})
        prim = response.get("prim")
        return self._prim_from_dict(prim) if prim else None

    def list_children(self, path: str) -> list[str]:
        response = self._rpc("children", {"path": path})
        return list(response.get("children", []))

    def create_prim(self, path: str, type_name: str = "Xform") -> PrimRecord:
        response = self._rpc("create", {"path": path, "type_name": type_name})
        return self._prim_from_dict(response["prim"])

    def delete_prim(self, path: str) -> None:
        self._rpc("delete", {"path": path})

    def duplicate_prim(self, source_path: str, target_path: str) -> PrimRecord:
        response = self._rpc("duplicate", {"source_path": source_path, "target_path": target_path})
        return self._prim_from_dict(response["prim"])

    def reparent_prim(self, path: str, new_parent: str) -> PrimRecord:
        response = self._rpc("reparent", {"path": path, "new_parent": new_parent})
        return self._prim_from_dict(response["prim"])

    def rename_prim(self, path: str, new_name: str) -> PrimRecord:
        response = self._rpc("rename", {"path": path, "new_name": new_name})
        return self._prim_from_dict(response["prim"])

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
    ) -> PrimRecord:
        payload = {"path": path, "translation": translation, "rotation": rotation, "scale": scale}
        response = self._rpc("move", payload)
        return self._prim_from_dict(response["prim"])

    def set_material(self, path: str, material_path: str) -> PrimRecord:
        response = self._rpc("material", {"path": path, "material_path": material_path})
        return self._prim_from_dict(response["prim"])

    def get_variant_selections(self, path: str) -> dict[str, str]:
        response = self._rpc("variants", {"path": path})
        return dict(response.get("selections", {}))

    def set_variant_selection(self, path: str, variant_set: str, selection: str) -> dict[str, str]:
        response = self._rpc(
            "variant_set",
            {"path": path, "variant_set": variant_set, "selection": selection},
        )
        return dict(response.get("selections", {}))

    def get_timeline_state(self) -> dict[str, object]:
        return dict(self._rpc("timeline"))

    def set_timeline_time(self, time_code: float) -> dict[str, object]:
        return dict(self._rpc("timeline_set", {"time_code": float(time_code)}))

    def play_timeline(self, start_time_code: float, end_time_code: float) -> dict[str, object]:
        return dict(
            self._rpc(
                "timeline_play",
                {"start_time_code": float(start_time_code), "end_time_code": float(end_time_code)},
            )
        )

    def stop_timeline(self) -> dict[str, object]:
        return dict(self._rpc("timeline_stop"))

    def find_prims(self, query: str, scope_path: str | None = None) -> list[PrimRecord]:
        response = self._rpc("find", {"query": query, "scope_path": scope_path})
        return [self._prim_from_dict(item) for item in response.get("prims", [])]

    def save_scene(self, path: str) -> str:
        response = self._rpc("save", {"path": path})
        return str(response["saved_path"])

    # --- extended live RPC (kit_rpc.py); an older bridge gets a structured needs_bridge_update result -----------

    def capabilities(self) -> dict[str, Any]:
        """RPC version, methods and insight queries of the live bridge."""
        result = self._rpc_optional("capabilities")
        result.setdefault("endpoint", self.endpoint)
        return result

    def insight(self, query: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one read-only insight query on the live stage (unsaved edits included)."""
        return self._rpc_optional("insight", {"query": query, "args": dict(args or {})}, timeout=LIVE_TIMEOUT)

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
    ) -> dict[str, Any]:
        params = _without_none(
            path=path,
            light_type=light_type,
            intensity=intensity,
            exposure=exposure,
            color=_listed(color),
            size=size,
            translation=_listed(translation),
            aim_at=_listed(aim_at),
            up_axis=up_axis,
            dry_run=dry_run is not False,
        )
        return self._rpc_optional("create_light", params, timeout=LIVE_TIMEOUT)

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
    ) -> dict[str, Any]:
        params = _without_none(
            path=path,
            focal_length=focal_length,
            horizontal_aperture=horizontal_aperture,
            clipping_range=_listed(clipping_range),
            translation=_listed(translation),
            aim_at=_listed(aim_at),
            copy_optics_from=copy_optics_from,
            up_axis=up_axis,
            dry_run=dry_run is not False,
        )
        return self._rpc_optional("create_camera", params, timeout=LIVE_TIMEOUT)

    def aim(
        self,
        path: str,
        target: str | tuple[float, float, float],
        *,
        up_axis: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        params = _without_none(path=path, target=_listed(target), up_axis=up_axis, dry_run=dry_run is not False)
        return self._rpc_optional("aim", params, timeout=LIVE_TIMEOUT)

    # --- safe edits and undo (RPC tier3-1); only an explicit dry_run=False is sent as false ------------------------

    def set_attribute(
        self,
        path: str,
        name: str,
        value: Any,
        *,
        time: float | None = None,
        create_type: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        params = _without_none(
            path=path,
            name=name,
            value=_listed(value),
            time=time,
            create_type=create_type,
            dry_run=dry_run is not False,
        )
        return self._rpc_optional("set_attribute", params, timeout=LIVE_TIMEOUT)

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
        create_parent: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        params = _without_none(
            path=path,
            diffuse_color=_listed(diffuse_color),
            roughness=roughness,
            metallic=metallic,
            opacity=opacity,
            emissive_color=_listed(emissive_color),
            ior=ior,
            create_parent=True if create_parent is True else None,
            dry_run=dry_run is not False,
        )
        return self._rpc_optional("create_material", params, timeout=LIVE_TIMEOUT)

    def bind_material(
        self,
        prim_path: str,
        material_path: str,
        *,
        strength: str | None = None,
        purpose: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        params = _without_none(
            prim_path=prim_path,
            material_path=material_path,
            strength=strength,
            purpose=purpose,
            dry_run=dry_run is not False,
        )
        return self._rpc_optional("bind_material", params, timeout=LIVE_TIMEOUT)

    def undo_last(self, dry_run: bool = True) -> dict[str, Any]:
        """Undo (dry_run=True, the default: only preview) the newest MCP edit the live bridge recorded."""
        return self._rpc_optional("undo_last", {"dry_run": dry_run is not False}, timeout=LIVE_TIMEOUT)

    def undo_history(self, limit: int = 20) -> dict[str, Any]:
        """The newest MCP edits the live bridge recorded (read-only)."""
        return self._rpc_optional("undo_history", {"limit": int(limit)}, timeout=LIVE_TIMEOUT)

    # --- RPC tier4-1: add_reference and physics (dry_run=True unless dry_run is exactly False) ------------------

    def add_reference(
        self,
        prim_path: str,
        asset_path: str,
        *,
        translation: Any = None,
        rotation: Any = None,
        scale: Any = None,
        instanceable: bool | None = None,
        asset_prim_path: str | None = None,
        correct_units: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Preview (default) or author an Xform referencing asset_path (an absolute file path) on the live stage."""
        params = _without_none(
            prim_path=prim_path,
            asset_path=asset_path,
            translation=_listed(translation),
            rotation=_listed(rotation),
            scale=_listed(scale),
            instanceable=instanceable if isinstance(instanceable, bool) else None,
            asset_prim_path=asset_prim_path,
            correct_units=True if correct_units is True else None,
            dry_run=dry_run is not False,
        )
        return self._rpc_optional("add_reference", params, timeout=LIVE_TIMEOUT)

    def physics_report(self, prim_path: str | None = None) -> dict[str, Any]:
        """Read-only UsdPhysics summary of the live stage (or one subtree)."""
        return self._rpc_optional("physics_report", _without_none(prim_path=prim_path), timeout=LIVE_TIMEOUT)

    def add_physics_scene(self, path: str = "/physicsScene", *, gravity: Any = None, dry_run: bool = True) -> dict[str, Any]:
        params = _without_none(path=path, gravity=_listed(gravity), dry_run=dry_run is not False)
        return self._rpc_optional("add_physics_scene", params, timeout=LIVE_TIMEOUT)

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
    ) -> dict[str, Any]:
        params = _without_none(
            prim_path=prim_path,
            mass=mass,
            density=density,
            kinematic=kinematic is True,
            add_collider=add_collider is not False,
            approximation=approximation,
            dry_run=dry_run is not False,
        )
        return self._rpc_optional("make_rigid_body", params, timeout=LIVE_TIMEOUT)

    def make_collider(self, prim_path: str, *, approximation: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        params = _without_none(prim_path=prim_path, approximation=approximation, dry_run=dry_run is not False)
        return self._rpc_optional("make_collider", params, timeout=LIVE_TIMEOUT)

    def remove_physics(self, prim_path: str, *, include_descendants: bool = False, dry_run: bool = True) -> dict[str, Any]:
        params = _without_none(
            prim_path=prim_path, include_descendants=include_descendants is True, dry_run=dry_run is not False
        )
        return self._rpc_optional("remove_physics", params, timeout=LIVE_TIMEOUT)

    # --- RPC tier4-1: viewport capture and simulation (Kit only; no dry_run: they author no scene data) ---------

    def capture_viewport(self, output_dir: str | None = None, *, width: int | None = None, height: int | None = None) -> dict[str, Any]:
        params = _without_none(output_dir=output_dir, width=width, height=height)
        return self._rpc_optional("capture_viewport", params, timeout=LIVE_TIMEOUT)

    def capture_status(self, path: str) -> dict[str, Any]:
        return self._rpc_optional("capture_status", {"path": path}, timeout=LIVE_TIMEOUT)

    def simulation_status(self) -> dict[str, Any]:
        return self._rpc_optional("simulation_status", timeout=LIVE_TIMEOUT)

    def simulation_play(self) -> dict[str, Any]:
        return self._rpc_optional("simulation_play", timeout=LIVE_TIMEOUT)

    def simulation_pause(self) -> dict[str, Any]:
        return self._rpc_optional("simulation_pause", timeout=LIVE_TIMEOUT)

    def simulation_stop(self) -> dict[str, Any]:
        return self._rpc_optional("simulation_stop", timeout=LIVE_TIMEOUT)

    def simulation_step(self, frames: int = 1) -> dict[str, Any]:
        return self._rpc_optional("simulation_step", {"frames": frames}, timeout=LIVE_TIMEOUT)

    # --- RPC tier5-1: read-only homework check and scene tour on the live stage --------------------------------

    def assignment_check(self, assignment_id: str = "isaac_session02_warehouse") -> dict[str, Any]:
        """Check the open stage against a homework rubric (read-only)."""
        return self._rpc_optional("assignment_check", {"assignment_id": assignment_id}, timeout=LIVE_TIMEOUT)

    def scene_tour(self, focus_path: str | None = None, *, depth: int = 3, max_prims: int = 400) -> dict[str, Any]:
        """A beginner tour of the open stage, or one subtree (read-only)."""
        params = _without_none(focus_path=focus_path, depth=depth, max_prims=max_prims)
        return self._rpc_optional("scene_tour", params, timeout=LIVE_TIMEOUT)

    def _rpc_optional(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        """Call a method an older bridge may not have; 'Unsupported live scene method' becomes a structured result."""
        try:
            result = self._rpc(method, params, timeout=timeout)
        except BridgeForbiddenError as exc:
            return dict(exc.result)
        except ValueError as exc:
            if UNSUPPORTED_METHOD_PREFIX.lower() not in str(exc).lower():
                raise
            return self._needs_bridge_update(method)
        return dict(result) if isinstance(result, dict) else {"ok": True, "result": result}

    def _needs_bridge_update(self, method: str) -> dict[str, Any]:
        try:
            port = urlparse(self.endpoint).port
        except ValueError:
            port = None
        if port == COMPOSER_PORT:
            hint = (
                "Composer's stock scene extension serves only the basic scene methods: run "
                "composer-study/reload_composer_bridge.py in Composer's Script Editor (or relaunch Composer with "
                "start-composer-study.ps1) to serve the extended bridge on 9910, or use the Isaac Sim bridge on 9911 "
                "or the saved-file tools (scene_stage_info, ...)."
            )
        else:
            hint = (
                "This bridge predates the extended RPC: run isaacsim-study/reload_mcp_bridge.py in Isaac Sim's "
                "Script Editor (or restart Isaac with start_mcp_bridge.py), then retry."
            )
        return unsupported_result(
            method,
            f"The live scene bridge at {self.endpoint} does not support '{method}' yet. {hint}",
            needs_bridge_update=True,
            endpoint=self.endpoint,
        )

    def _rpc(self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        payload = json.dumps({"method": method, "params": params or {}}).encode("utf-8")
        request = Request(
            f"{self.endpoint}/rpc",
            data=payload,
            headers=bridge_request_headers(),
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            if exc.code == 403:
                raise BridgeForbiddenError(self._forbidden_result(method, body)) from None
            raise ValueError(f"Scene bridge HTTP error {exc.code}: {body or exc.reason}") from exc
        except URLError as exc:
            raise ValueError(f"Scene bridge endpoint is unavailable at {self.endpoint}: {exc.reason}") from exc

        result = json.loads(data)
        if not result.get("ok", False):
            raise ValueError(result.get("message", "Scene bridge request failed"))
        return result["result"]

    def _forbidden_result(self, method: str, body: str) -> dict[str, Any]:
        """Structured error for a 403 (the guard's JSON gives the reason; any other 403 body is shown clipped)."""
        try:
            info = json.loads(body) if body else {}
        except ValueError:
            info = {}
        if not isinstance(info, dict):
            info = {}
        reason = str(info.get("reason") or "unknown")[:40]
        detail = str(info.get("message") or body or "Forbidden")[:300]
        hint = _FORBIDDEN_HINTS.get(reason, _DEFAULT_FORBIDDEN_HINT)
        return {
            "ok": False,
            "forbidden": True,
            "status": 403,
            "method": method,
            "reason": reason,
            "endpoint": self.endpoint,
            "message": f"The live scene bridge at {self.endpoint} refused '{method}' (HTTP 403, {reason}): {detail} {hint}",
        }

    @staticmethod
    def _prim_from_dict(data: dict[str, Any]) -> PrimRecord:
        return PrimRecord(
            path=data["path"],
            type_name=data.get("type_name", "Xform"),
            parent=data.get("parent"),
            children=list(data.get("children", [])),
            translation=tuple(data.get("translation", (0.0, 0.0, 0.0))),
            rotation=tuple(data.get("rotation", (0.0, 0.0, 0.0))),
            scale=tuple(data.get("scale", (1.0, 1.0, 1.0))),
            material=data.get("material"),
        )

    @classmethod
    def _snapshot_from_dict(cls, data: dict[str, Any]) -> SceneSnapshot:
        return SceneSnapshot(
            stage_name=data["stage_name"],
            root_path=data["root_path"],
            selection=list(data.get("selection", [])),
            prim_count=int(data.get("prim_count", 0)),
            prims=[cls._prim_from_dict(item) for item in data.get("prims", [])],
        )


def bridge_request_headers() -> dict[str, str]:
    """Headers of every bridge RPC: JSON content type, never Origin, and the shared token when the router has one."""
    headers = {"Content-Type": "application/json"}
    token = os.environ.get(BRIDGE_TOKEN_ENV, "").strip()
    if token:
        headers[BRIDGE_TOKEN_HEADER] = token
    return headers


def _without_none(**params: Any) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _listed(value: Any) -> Any:
    """Tuples become JSON lists; strings (prim paths) and None pass through."""
    return list(value) if isinstance(value, (list, tuple)) else value
