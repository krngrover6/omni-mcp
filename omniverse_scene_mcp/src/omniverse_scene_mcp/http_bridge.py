from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .bridge import PrimRecord, SceneSnapshot


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

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = json.dumps({"method": method, "params": params or {}}).encode("utf-8")
        request = Request(
            f"{self.endpoint}/rpc",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
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
