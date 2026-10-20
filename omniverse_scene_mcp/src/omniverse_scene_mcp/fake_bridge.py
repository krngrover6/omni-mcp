from __future__ import annotations

from copy import deepcopy

from .bridge import PrimRecord, SceneSnapshot


class FakeSceneBridge:
    """In-memory scene bridge for tests and local development."""

    def __init__(self) -> None:
        self._stage_name = "FakeStage"
        self._root_path = "/World"
        self._selection: list[str] = []
        self._variant_selections: dict[str, dict[str, str]] = {}
        self._timeline_state: dict[str, object] = {
            "current_time_code": 0.0,
            "start_time_code": 0.0,
            "end_time_code": 1200.0,
            "time_codes_per_second": 24.0,
            "playing": False,
        }
        self._prims: dict[str, PrimRecord] = {
            "/World": PrimRecord(path="/World", type_name="Xform"),
            "/World/Props": PrimRecord(path="/World/Props", type_name="Xform", parent="/World"),
            "/World/Props/Cube": PrimRecord(path="/World/Props/Cube", type_name="Cube", parent="/World/Props"),
        }
        self._link_child("/World", "/World/Props")
        self._link_child("/World/Props", "/World/Props/Cube")

    def ping(self) -> str:
        return "ok"

    def snapshot(self) -> SceneSnapshot:
        return SceneSnapshot(
            stage_name=self._stage_name,
            root_path=self._root_path,
            selection=list(self._selection),
            prim_count=len(self._prims),
            prims=[deepcopy(prim) for prim in self._prims.values()],
        )

    def get_selection(self) -> list[str]:
        return list(self._selection)

    def set_selection(self, paths: list[str]) -> None:
        self._selection = [path for path in paths if path in self._prims]

    def get_prim(self, path: str) -> PrimRecord | None:
        prim = self._prims.get(path)
        return deepcopy(prim) if prim else None

    def list_children(self, path: str) -> list[str]:
        prim = self._require_prim(path)
        return list(prim.children)

    def create_prim(self, path: str, type_name: str = "Xform") -> PrimRecord:
        if path in self._prims:
            raise ValueError(f"Prim already exists: {path}")
        parent_path = self._parent_path(path)
        if parent_path and parent_path not in self._prims:
            raise ValueError(f"Parent prim does not exist: {parent_path}")
        prim = PrimRecord(path=path, type_name=type_name, parent=parent_path)
        self._prims[path] = prim
        if parent_path:
            self._link_child(parent_path, path)
        return deepcopy(prim)

    def delete_prim(self, path: str) -> None:
        prim = self._require_prim(path)
        for child in list(prim.children):
            self.delete_prim(child)
        if prim.parent:
            parent = self._require_prim(prim.parent)
            parent.children = [child for child in parent.children if child != path]
        self._prims.pop(path, None)
        self._selection = [item for item in self._selection if item != path]

    def duplicate_prim(self, source_path: str, target_path: str) -> PrimRecord:
        source = deepcopy(self._require_prim(source_path))
        if target_path in self._prims:
            raise ValueError(f"Target prim already exists: {target_path}")
        parent_path = self._parent_path(target_path)
        if parent_path and parent_path not in self._prims:
            raise ValueError(f"Parent prim does not exist: {parent_path}")
        clone = PrimRecord(
            path=target_path,
            type_name=source.type_name,
            parent=parent_path,
            translation=source.translation,
            rotation=source.rotation,
            scale=source.scale,
            material=source.material,
        )
        self._prims[target_path] = clone
        if parent_path:
            self._link_child(parent_path, target_path)
        return deepcopy(clone)

    def reparent_prim(self, path: str, new_parent: str) -> PrimRecord:
        prim = self._require_prim(path)
        parent = self._require_prim(new_parent)
        if prim.parent:
            old_parent = self._require_prim(prim.parent)
            old_parent.children = [child for child in old_parent.children if child != path]
        prim.parent = parent.path
        if path not in parent.children:
            parent.children.append(path)
        return deepcopy(prim)

    def rename_prim(self, path: str, new_name: str) -> PrimRecord:
        prim = self._require_prim(path)
        if not new_name or "/" in new_name:
            raise ValueError("Prim name must be a single path token")

        old_path = prim.path
        parent_path = prim.parent
        if parent_path:
            parent = self._require_prim(parent_path)
        else:
            parent = None

        renamed: dict[str, PrimRecord] = {}
        path_map: dict[str, str] = {}
        for existing_path in list(self._prims.keys()):
            if existing_path == old_path or existing_path.startswith(f"{old_path}/"):
                suffix = existing_path[len(old_path) :]
                new_path = f"{parent_path.rstrip('/') if parent_path else ''}/{new_name}{suffix}" if parent_path else f"/{new_name}{suffix}"
                path_map[existing_path] = new_path

        for existing_path, record in list(self._prims.items()):
            new_path = path_map.get(existing_path, existing_path)
            new_parent = path_map.get(record.parent, record.parent) if record.parent else None
            renamed[new_path] = PrimRecord(
                path=new_path,
                type_name=record.type_name,
                parent=new_parent,
                children=[path_map.get(child, child) for child in record.children],
                translation=record.translation,
                rotation=record.rotation,
                scale=record.scale,
                material=record.material,
            )

        self._prims = renamed
        if parent:
            parent.children = [path_map.get(child, child) for child in parent.children]
        self._selection = [path_map.get(item, item) for item in self._selection]
        return deepcopy(self._prims[path_map[old_path]])

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
    ) -> PrimRecord:
        prim = self._require_prim(path)
        if translation is not None:
            prim.translation = translation
        if rotation is not None:
            prim.rotation = rotation
        if scale is not None:
            prim.scale = scale
        return deepcopy(prim)

    def set_material(self, path: str, material_path: str) -> PrimRecord:
        prim = self._require_prim(path)
        prim.material = material_path
        return deepcopy(prim)

    def get_variant_selections(self, path: str) -> dict[str, str]:
        self._require_prim(path)
        return dict(self._variant_selections.get(path, {}))

    def set_variant_selection(self, path: str, variant_set: str, selection: str) -> dict[str, str]:
        self._require_prim(path)
        selections = self._variant_selections.setdefault(path, {})
        selections[variant_set] = selection
        return dict(selections)

    def get_timeline_state(self) -> dict[str, object]:
        return dict(self._timeline_state)

    def set_timeline_time(self, time_code: float) -> dict[str, object]:
        self._timeline_state["current_time_code"] = float(time_code)
        self._timeline_state["playing"] = False
        return self.get_timeline_state()

    def play_timeline(self, start_time_code: float, end_time_code: float) -> dict[str, object]:
        if end_time_code <= start_time_code:
            raise ValueError("Timeline end must be greater than its start")
        self._timeline_state.update(
            {
                "current_time_code": float(start_time_code),
                "start_time_code": float(start_time_code),
                "end_time_code": float(end_time_code),
                "playing": True,
            }
        )
        return self.get_timeline_state()

    def stop_timeline(self) -> dict[str, object]:
        self._timeline_state["playing"] = False
        return self.get_timeline_state()

    def find_prims(self, query: str, scope_path: str | None = None) -> list[PrimRecord]:
        query_lower = query.lower().strip()
        scope_prefix = f"{scope_path.rstrip('/')}/" if scope_path else None
        matches: list[PrimRecord] = []
        for prim in self._prims.values():
            if scope_prefix and not (prim.path == scope_path or prim.path.startswith(scope_prefix)):
                continue
            haystack = " ".join(filter(None, [prim.path, prim.type_name, prim.material or ""])).lower()
            if query_lower in haystack:
                matches.append(deepcopy(prim))
        return matches

    def save_scene(self, path: str) -> str:
        self._stage_name = path.rsplit("/", 1)[-1] or self._stage_name
        return path

    def _require_prim(self, path: str) -> PrimRecord:
        prim = self._prims.get(path)
        if prim is None:
            raise ValueError(f"Prim does not exist: {path}")
        return prim

    def _link_child(self, parent_path: str, child_path: str) -> None:
        parent = self._require_prim(parent_path)
        if child_path not in parent.children:
            parent.children.append(child_path)

    @staticmethod
    def _parent_path(path: str) -> str | None:
        if "/" not in path.strip("/"):
            return None
        parent = path.rsplit("/", 1)[0]
        return parent if parent else "/"
