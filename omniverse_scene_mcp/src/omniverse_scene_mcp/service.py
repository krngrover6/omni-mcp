from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .bridge import ChangeRecord, PrimRecord, SceneBridge


@dataclass(slots=True)
class OperationResult:
    ok: bool
    dry_run: bool
    changes: list[ChangeRecord]
    payload: dict[str, Any]


class SceneMCPService:
    """Deterministic scene operations that can back MCP tools."""

    def __init__(self, bridge: SceneBridge) -> None:
        self._bridge = bridge

    def ping(self) -> dict[str, Any]:
        return {"status": self._bridge.ping()}

    def current_selection(self) -> list[str]:
        return self._bridge.get_selection()

    def set_selection(self, paths: list[str]) -> dict[str, Any]:
        self._bridge.set_selection(paths)
        return {"selection": self._bridge.get_selection()}

    def unique_child_path(self, parent_path: str, base_name: str) -> str:
        snapshot = self._bridge.snapshot()
        existing_paths = {prim.path for prim in snapshot.prims}

        parent = parent_path.rstrip("/") if parent_path != "/" else ""
        candidate = f"{parent}/{base_name}" if parent else f"/{base_name}"
        if candidate not in existing_paths:
            return candidate

        index = 1
        while True:
            suffixed = f"{candidate}_{index}"
            if suffixed not in existing_paths:
                return suffixed
            index += 1

    def scene_summary(self) -> dict[str, Any]:
        snapshot = self._bridge.snapshot()
        return {
            "stage_name": snapshot.stage_name,
            "root_path": snapshot.root_path,
            "selection": snapshot.selection,
            "prim_count": snapshot.prim_count,
            "top_level_prims": [prim.path for prim in snapshot.prims if prim.parent == snapshot.root_path],
        }

    def inspect_prim(self, path: str) -> dict[str, Any]:
        prim = self._require_prim(path)
        return asdict(prim)

    def list_children(self, path: str) -> dict[str, Any]:
        prim = self._require_prim(path)
        return {"path": prim.path, "children": self._bridge.list_children(path)}

    def rename_prim(self, path: str, new_name: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        new_path = self._renamed_path(path, new_name)
        changes = [
            ChangeRecord(
                "rename",
                path,
                f"Rename {path} to {new_name}",
                before=asdict(prim),
                after={"path": new_path, "new_name": new_name},
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "new_name": new_name, "new_path": new_path})
        updated = self._bridge.rename_prim(path, new_name)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def create_prim(self, path: str, type_name: str = "Xform", dry_run: bool = False) -> OperationResult:
        self._ensure_available(path, must_exist=False)
        changes = [ChangeRecord("create", path, f"Create {type_name} at {path}")]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "type_name": type_name})
        prim = self._bridge.create_prim(path, type_name)
        return OperationResult(True, False, changes, {"prim": asdict(prim)})

    def delete_prim(self, path: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        changes = [ChangeRecord("delete", path, f"Delete prim at {path}", before=asdict(prim))]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path})
        self._bridge.delete_prim(path)
        return OperationResult(True, False, changes, {"path": path})

    def duplicate_prim(self, source_path: str, target_path: str, dry_run: bool = False) -> OperationResult:
        source = self._require_prim(source_path)
        changes = [
            ChangeRecord(
                "duplicate",
                target_path,
                f"Duplicate {source_path} to {target_path}",
                before=asdict(source),
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"source_path": source_path, "target_path": target_path})
        prim = self._bridge.duplicate_prim(source_path, target_path)
        return OperationResult(True, False, changes, {"prim": asdict(prim)})

    def reparent_prim(self, path: str, new_parent: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        changes = [
            ChangeRecord(
                "reparent",
                path,
                f"Move {path} under {new_parent}",
                before=asdict(prim),
                after={"parent": new_parent},
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "new_parent": new_parent})
        updated = self._bridge.reparent_prim(path, new_parent)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
        dry_run: bool = False,
    ) -> OperationResult:
        prim = self._require_prim(path)
        requested = {"translation": translation, "rotation": rotation, "scale": scale}
        changes = [ChangeRecord("transform", path, f"Update transform for {path}", before=asdict(prim), after=requested)]
        if dry_run:
            return OperationResult(True, True, changes, requested)
        updated = self._bridge.move_prim(path, translation=translation, rotation=rotation, scale=scale)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def set_material(self, path: str, material_path: str, dry_run: bool = False) -> OperationResult:
        prim = self._require_prim(path)
        changes = [
            ChangeRecord(
                "material",
                path,
                f"Bind {material_path} to {path}",
                before=asdict(prim),
                after={"material": material_path},
            )
        ]
        if dry_run:
            return OperationResult(True, True, changes, {"path": path, "material_path": material_path})
        updated = self._bridge.set_material(path, material_path)
        return OperationResult(True, False, changes, {"prim": asdict(updated)})

    def find_prims(self, query: str, scope_path: str | None = None) -> dict[str, Any]:
        matches = self._bridge.find_prims(query, scope_path=scope_path)
        return {
            "query": query,
            "scope_path": scope_path,
            "count": len(matches),
            "prims": [asdict(prim) for prim in matches],
        }

    def preview_changes(self, result: OperationResult) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "dry_run": result.dry_run,
            "changes": [asdict(change) for change in result.changes],
            "payload": result.payload,
        }

    def _require_prim(self, path: str) -> PrimRecord:
        prim = self._bridge.get_prim(path)
        if prim is None:
            raise ValueError(f"Prim does not exist: {path}")
        return prim

    def _ensure_available(self, path: str, must_exist: bool) -> None:
        exists = self._bridge.get_prim(path) is not None
        if must_exist and not exists:
            raise ValueError(f"Prim does not exist: {path}")
        if not must_exist and exists:
            raise ValueError(f"Prim already exists: {path}")

    @staticmethod
    def _renamed_path(path: str, new_name: str) -> str:
        if "/" in new_name or not new_name:
            raise ValueError("Prim name must be a single path token")
        parent = path.rsplit("/", 1)[0] if "/" in path.strip("/") else ""
        return f"{parent}/{new_name}" if parent else f"/{new_name}"
