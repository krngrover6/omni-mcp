from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

# The scene bridge is a loopback service: never route its RPCs through HTTP(S)_PROXY.
urlopen = build_opener(ProxyHandler({})).open

from .bridge import UNSUPPORTED_METHOD_PREFIX, PrimRecord, SceneSnapshot, unsupported_result

DEFAULT_TIMEOUT = 10.0
# The live server waits up to 15 s for Kit's main thread; wait a little longer so its own timeout message arrives.
LIVE_TIMEOUT = 20.0
COMPOSER_PORT = 9910


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

    def _rpc_optional(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        """Call a method an older bridge may not have; 'Unsupported live scene method' becomes a structured result."""
        try:
            result = self._rpc(method, params, timeout=timeout)
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
                "Composer's scene extension on port 9910 serves only the basic scene methods; use the Isaac Sim "
                "bridge on 9911 for live insight and authoring, or the saved-file tools (scene_stage_info, ...)."
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
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                data = response.read().decode("utf-8")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise ValueError(f"Scene bridge HTTP error {exc.code}: {body or exc.reason}") from exc
        except URLError as exc:
            raise ValueError(f"Scene bridge endpoint is unavailable at {self.endpoint}: {exc.reason}") from exc

        result = json.loads(data)
        if not result.get("ok", False):
            raise ValueError(result.get("message", "Scene bridge request failed"))
        return result["result"]

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


def _without_none(**params: Any) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _listed(value: Any) -> Any:
    """Tuples become JSON lists; strings (prim paths) and None pass through."""
    return list(value) if isinstance(value, (list, tuple)) else value
