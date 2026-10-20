from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import posixpath
import re
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

    def __init__(self, bridge: SceneBridge, backend_info: dict[str, Any] | None = None) -> None:
        self._bridge = bridge
        self._backend_info = dict(backend_info) if backend_info else {
            "mode": "direct",
            "bridge": type(bridge).__name__,
            "endpoint": getattr(bridge, "endpoint", None),
            "reason": "Service built directly around a bridge, not through runtime",
            "silent_fallback": False,
            "warnings": [],
        }

    @property
    def backend_info(self) -> dict[str, Any]:
        """How the scene bridge was chosen: mode, bridge class, endpoint, reason and warnings."""
        return {**self._backend_info, "warnings": list(self._backend_info.get("warnings", []))}

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

    def get_variant_selections(self, path: str) -> dict[str, Any]:
        self._require_prim(path)
        return {"path": path, "selections": self._bridge.get_variant_selections(path)}

    def set_variant_selection(
        self,
        path: str,
        variant_set: str,
        selection: str,
        dry_run: bool = False,
    ) -> OperationResult:
        self._require_prim(path)
        before = self._bridge.get_variant_selections(path)
        changes = [
            ChangeRecord(
                "variant",
                path,
                f"Set {variant_set}={selection} on {path}",
                before={"selections": before},
                after={"variant_set": variant_set, "selection": selection},
            )
        ]
        if dry_run:
            return OperationResult(
                True,
                True,
                changes,
                {"path": path, "variant_set": variant_set, "selection": selection},
            )
        selections = self._bridge.set_variant_selection(path, variant_set, selection)
        return OperationResult(True, False, changes, {"path": path, "selections": selections})

    def get_timeline_state(self) -> dict[str, object]:
        return self._bridge.get_timeline_state()

    def set_timeline_time(self, time_code: float) -> dict[str, object]:
        return self._bridge.set_timeline_time(time_code)

    def play_timeline(self, start_time_code: float, end_time_code: float) -> dict[str, object]:
        return self._bridge.play_timeline(start_time_code, end_time_code)

    def stop_timeline(self) -> dict[str, object]:
        return self._bridge.stop_timeline()

    def find_prims(self, query: str, scope_path: str | None = None) -> dict[str, Any]:
        matches = self._bridge.find_prims(query, scope_path=scope_path)
        return {
            "query": query,
            "scope_path": scope_path,
            "count": len(matches),
            "prims": [asdict(prim) for prim in matches],
        }

    def save_scene(self, path: str, dry_run: bool = False) -> dict[str, Any]:
        if dry_run:
            return self.preview_save(path)
        saved_path = self._bridge.save_scene(path)
        return {"saved_path": saved_path}

    def preview_save(self, path: str) -> dict[str, Any]:
        """Describe what save_scene(path) would do without calling the bridge's save."""
        if not path or not path.strip():
            raise ValueError("Save path is required")
        stage_name = str(self.scene_summary()["stage_name"])
        local = "://" not in path
        relative = local and not _is_absolute(path) and path.strip()[:5].lower() != "file:"
        # A relative path is resolved by the bridge against its own working directory, which this process cannot
        # see, so whether it hits the open root layer is unknown (an anonymous root can only be a Save As).
        in_place = None if relative and not stage_name.startswith("anon:") else _layer_key(stage_name) == _layer_key(path)
        warnings: list[str] = []
        if in_place is None:
            would = (
                "depends on the scene bridge's working directory: if the path resolves to the open root layer, this "
                "overwrites it and saves every dirty sublayer; otherwise it exports a root-layer-only copy"
            )
            warnings.append(
                f"May overwrite {stage_name} and save every dirty sublayer; use an absolute path to know for sure."
            )
        elif in_place:
            would = "save the root layer in place"
            warnings.append(f"Overwrites {path} and also saves every dirty sublayer of the root layer stack to its own file.")
        else:
            would = "Save As: export a copy of the root layer only"
            warnings.append(
                "Only the root layer is copied (asset paths re-anchored to the new folder); "
                "sublayer and reference files are not written and dirty sublayers stay unsaved in memory."
            )
            warnings.append(f"The open stage stays on {stage_name}; later edits keep going there, not to {path}.")
            if local and _is_absolute(path) and os.path.exists(path):
                warnings.append(f"{path} already exists and would be overwritten.")
        if stage_name.startswith("anon:"):
            warnings.append("The open stage's root layer is anonymous (never saved), so only a Save As is possible.")
        if stage_name == "NoStage":
            warnings.append("No stage is open; a real save would fail.")
        if local and not _is_absolute(path):
            warnings.append("Relative path: the scene bridge resolves it against its own working directory.")
        if type(self._bridge).__name__ == "FakeSceneBridge":
            warnings.append("Scene backend is the in-memory fake bridge; saving would not write any file.")
        return {
            "dry_run": True,
            "path": path,
            "stage_name": stage_name,
            "in_place": in_place,
            "would": would,
            "warnings": warnings,
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


def _layer_key(path: str) -> str:
    """Comparable form of a layer identifier or file path: no file: prefix, forward slashes, lower case."""
    text = path.strip()
    if text[:5].lower() == "file:":
        text = re.sub(r"^/+(?=[A-Za-z]:)", "", text[5:])
    text = text.replace("\\", "/")
    return posixpath.normpath(text).lower() if text else ""


def _is_absolute(path: str) -> bool:
    return os.path.isabs(path) or bool(re.match(r"^[A-Za-z]:[\\/]", path))
