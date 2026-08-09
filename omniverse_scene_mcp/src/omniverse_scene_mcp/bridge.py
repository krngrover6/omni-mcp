from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


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

    def find_prims(self, query: str, scope_path: str | None = None) -> list[PrimRecord]: ...
