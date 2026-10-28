from __future__ import annotations

from copy import deepcopy
import math
import os
import re
import time as _time
from typing import Any

from .bridge import (
    ASSET_RPC_METHODS,
    BASIC_RPC_METHODS,
    CAPTURE_RPC_METHODS,
    EDIT_RPC_METHODS,
    PHYSICS_RPC_METHODS,
    SIMULATION_RPC_METHODS,
    UNDO_RPC_METHODS,
    PrimRecord,
    SceneSnapshot,
    unsupported_result,
)

_FAKE_HINT = (
    "connect the router to a live Isaac Sim bridge (9911) for this, or use the saved-file tools "
    "(scene_stage_info, scene_composition, scene_explain_value, scene_properties, scene_bounds, usd_validate)."
)
# The same wording as undo.SAVE_NOTE / NOTHING_TO_UNDO (kept here so the fake never needs pxr).
SAVE_NOTE = "Saving is not undoable: undo changes the open layers in memory; save again to write the reverted state."
NOTHING_TO_UNDO = "Nothing to undo: no MCP edit on the open stage is recorded (saves, dry runs and no-op edits are not recorded)."
FAKE_EDIT_NOTE = (
    "In-memory fake scene: nothing reached Composer or Isaac Sim, and values are stored as given (the fake scene has "
    "no USD schemas or value types)."
)
MAX_UNDO_ENTRIES = 50
MAX_STEP_FRAMES = 600  # the same cap as kit_live_extras.simulation_step
_NO_USD_STAGE = "The in-memory fake scene has no USD stage, so it has no UsdPhysics schemas."
_USD_EXTENSIONS = (".usd", ".usda", ".usdc", ".usdz")
_PRIM_PATH = re.compile(r"^(?:/[A-Za-z_][A-Za-z0-9_]*)+$")
FAKE_REFERENCE_NOTE = (
    "The fake scene only checks that the asset file exists; it does not open it, so its defaultPrim, upAxis and "
    "metersPerUnit are not checked and nothing is loaded."
)
FAKE_SIM_NOTE = "In-memory fake timeline: there is no physics, so nothing moves; only the time changes."
_ATTRIBUTE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z_][A-Za-z0-9_]*)*$")
_STRENGTHS = {
    "weakerthandescendants": "weakerThanDescendants",
    "weaker": "weakerThanDescendants",
    "strongerthandescendants": "strongerThanDescendants",
    "stronger": "strongerThanDescendants",
}
_PURPOSES = {"allpurpose": "allPurpose", "all": "allPurpose", "": "allPurpose", "preview": "preview", "full": "full"}
SHADER_NAME = "PreviewSurface"


class FakeSceneBridge:
    """In-memory scene bridge for tests and local development.

    Besides the basic scene methods it keeps attribute values, UsdPreviewSurface-like materials and an undo
    history in memory, so set_attribute / create_material / bind_material / undo_last work offline. Every one of
    them is a dry run unless dry_run=False, and a dry run changes nothing.
    """

    def __init__(self) -> None:
        self._stage_name = "FakeStage"
        self._root_path = "/World"
        self._selection: list[str] = []
        self._variant_selections: dict[str, dict[str, str]] = {}
        self._attributes: dict[str, dict[str, dict[str, Any]]] = {}
        self._undo: list[dict[str, Any]] = []
        self._undo_dropped = 0
        self._timeline_state: dict[str, object] = {
            "current_time_code": 0.0,
            "start_time_code": 0.0,
            "end_time_code": 1200.0,
            "time_codes_per_second": 24.0,
            "playing": False,
        }
        self._sim_paused = False
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

    # Every mutating method records an undo entry (only when it changed something); reads and dry runs do not.

    def create_prim(self, path: str, type_name: str = "Xform") -> PrimRecord:
        if path in self._prims:
            raise ValueError(f"Prim already exists: {path}")
        parent_path = self._parent_path(path)
        if parent_path and parent_path not in self._prims:
            raise ValueError(f"Parent prim does not exist: {parent_path}")
        before = self._state()
        prim = PrimRecord(path=path, type_name=type_name, parent=parent_path)
        self._prims[path] = prim
        if parent_path:
            self._link_child(parent_path, path)
        self._record(f"Create {type_name} {path}", [path], before)
        return deepcopy(prim)

    def delete_prim(self, path: str) -> None:
        self._require_prim(path)
        before = self._state()
        self._delete_tree(path)
        self._record(f"Delete {path}", [path], before)

    def _delete_tree(self, path: str) -> None:
        prim = self._require_prim(path)
        for child in list(prim.children):
            self._delete_tree(child)
        if prim.parent:
            parent = self._require_prim(prim.parent)
            parent.children = [child for child in parent.children if child != path]
        self._prims.pop(path, None)
        self._attributes.pop(path, None)
        self._variant_selections.pop(path, None)
        self._selection = [item for item in self._selection if item != path]

    def duplicate_prim(self, source_path: str, target_path: str) -> PrimRecord:
        source = deepcopy(self._require_prim(source_path))
        if target_path in self._prims:
            raise ValueError(f"Target prim already exists: {target_path}")
        parent_path = self._parent_path(target_path)
        if parent_path and parent_path not in self._prims:
            raise ValueError(f"Parent prim does not exist: {parent_path}")
        before = self._state()
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
        if source_path in self._attributes:
            self._attributes[target_path] = deepcopy(self._attributes[source_path])
        if parent_path:
            self._link_child(parent_path, target_path)
        self._record(f"Duplicate {source_path} to {target_path}", [target_path], before)
        return deepcopy(clone)

    def reparent_prim(self, path: str, new_parent: str) -> PrimRecord:
        prim = self._require_prim(path)
        parent = self._require_prim(new_parent)
        before = self._state()
        if prim.parent:
            old_parent = self._require_prim(prim.parent)
            old_parent.children = [child for child in old_parent.children if child != path]
        prim.parent = parent.path
        if path not in parent.children:
            parent.children.append(path)
        self._record(f"Reparent {path} under {new_parent}", [path], before)
        return deepcopy(prim)

    def rename_prim(self, path: str, new_name: str) -> PrimRecord:
        prim = self._require_prim(path)
        if not new_name or "/" in new_name:
            raise ValueError("Prim name must be a single path token")
        before = self._state()

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
        self._attributes = {path_map.get(key, key): value for key, value in self._attributes.items()}
        self._variant_selections = {path_map.get(key, key): value for key, value in self._variant_selections.items()}
        if parent:
            parent.children = [path_map.get(child, child) for child in parent.children]
        self._selection = [path_map.get(item, item) for item in self._selection]
        self._record(f"Rename {old_path} to {new_name}", [old_path, path_map[old_path]], before)
        return deepcopy(self._prims[path_map[old_path]])

    def move_prim(
        self,
        path: str,
        translation: tuple[float, float, float] | None = None,
        rotation: tuple[float, float, float] | None = None,
        scale: tuple[float, float, float] | None = None,
    ) -> PrimRecord:
        prim = self._require_prim(path)
        before = self._state()
        if translation is not None:
            prim.translation = translation
        if rotation is not None:
            prim.rotation = rotation
        if scale is not None:
            prim.scale = scale
        self._record(f"Move {path}", [path], before)
        return deepcopy(prim)

    def set_material(self, path: str, material_path: str) -> PrimRecord:
        prim = self._require_prim(path)
        before = self._state()
        prim.material = material_path
        self._record(f"Bind {material_path} to {path}", [path], before)
        return deepcopy(prim)

    def get_variant_selections(self, path: str) -> dict[str, str]:
        self._require_prim(path)
        return dict(self._variant_selections.get(path, {}))

    def set_variant_selection(self, path: str, variant_set: str, selection: str) -> dict[str, str]:
        self._require_prim(path)
        before = self._state()
        selections = self._variant_selections.setdefault(path, {})
        selections[variant_set] = selection
        self._record(f"Select {variant_set}={selection} on {path}", [path], before)
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
        self._sim_paused = False
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
        """Pretend to save (nothing is written). Saving is not undoable and adds no undo entry."""
        self._stage_name = path.rsplit("/", 1)[-1] or self._stage_name
        return path

    def capabilities(self) -> dict[str, Any]:
        """Basic scene methods plus in-memory edits and undo: the fake scene has no USD stage for insight."""
        return {
            "ok": True,
            "bridge": type(self).__name__,
            "version": "fake",
            "methods": list(BASIC_RPC_METHODS) + list(EDIT_RPC_METHODS) + list(UNDO_RPC_METHODS)
            + list(ASSET_RPC_METHODS) + list(SIMULATION_RPC_METHODS),
            "insight_queries": [],
            "authoring": {"available": False, "error": "The in-memory fake scene has no USD stage."},
            "edits": {"available": True, "in_memory": True, "methods": list(EDIT_RPC_METHODS), "dry_run_default": True},
            "assets": {"available": True, "in_memory": True, "methods": list(ASSET_RPC_METHODS), "dry_run_default": True},
            "physics": {"available": False, "methods": list(PHYSICS_RPC_METHODS), "error": _NO_USD_STAGE},
            "capture": {"available": False, "methods": list(CAPTURE_RPC_METHODS), "error": "The fake scene has no viewport."},
            "simulation": {"available": True, "in_memory": True, "methods": list(SIMULATION_RPC_METHODS),
                           "physics_available": False},
            "undo": {
                "available": True,
                "in_memory": True,
                "kit_ctrl_z": False,
                "methods": list(UNDO_RPC_METHODS),
                "recorded": len(self._undo),
                "max_entries": MAX_UNDO_ENTRIES,
                "dry_run_default": True,
                "note": SAVE_NOTE,
            },
            "dry_run_default": True,
            "fake_scene": True,
            "message": "In-memory fake scene: basic scene methods, in-memory attribute/material edits, asset references, "
            "a timeline without physics, and undo only.",
        }

    def insight(self, query: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._not_supported("insight", "Live insight", query=str(query))

    def create_light(self, path: str, light_type: str = "", **_options: Any) -> dict[str, Any]:
        return self._not_supported("create_light", "Creating lights", path=str(path))

    def create_camera(self, path: str, **_options: Any) -> dict[str, Any]:
        return self._not_supported("create_camera", "Creating cameras", path=str(path))

    def aim(self, path: str, target: Any = None, **_options: Any) -> dict[str, Any]:
        return self._not_supported("aim", "Aiming prims", path=str(path))

    # --- RPC tier4-1 on the fake scene: in-memory asset references and timeline; no physics or viewport ---------

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
        """Add (or with dry_run, preview adding) a fake Xform that records a reference to an existing USD file."""
        dry = dry_run is not False
        fields: dict[str, Any] = {"path": str(prim_path), "asset_path": str(asset_path)}
        notes = [FAKE_EDIT_NOTE, FAKE_REFERENCE_NOTE]
        try:
            if not isinstance(prim_path, str) or not _PRIM_PATH.match(prim_path):
                raise ValueError(f"{prim_path!r} is not an absolute prim path such as /World/Robot.")
            if prim_path in self._prims:
                raise ValueError(f"A prim already exists at {prim_path} ({self._prims[prim_path].type_name}); choose a new path.")
            parent_path = self._parent_path(prim_path)
            if parent_path and parent_path not in self._prims:
                raise ValueError(f"Parent prim does not exist: {parent_path}. Create it first.")
            asset = str(asset_path or "").strip()
            if not asset or not os.path.isfile(asset):
                raise ValueError(f"Asset file not found: {asset or asset_path!r}. Use asset_search to find a path.")
            if os.path.splitext(asset)[1].lower() not in _USD_EXTENSIONS:
                raise ValueError(f"{os.path.basename(asset)} is not a USD file ({', '.join(_USD_EXTENSIONS)}).")
            moved = _vector_or_none(translation, "translation")
            turned = _vector_or_none(rotation, "rotation")
            if isinstance(scale, (int, float)) and not isinstance(scale, bool):
                scale = (scale, scale, scale)
            sized = _vector_or_none(scale, "scale")
            if sized is not None and any(value == 0.0 for value in sized):
                raise ValueError("scale components must not be 0 (the asset would collapse).")
            if asset_prim_path is not None and (not isinstance(asset_prim_path, str) or not _PRIM_PATH.match(asset_prim_path)):
                raise ValueError(f"asset_prim_path {asset_prim_path!r} is not an absolute prim path.")
        except ValueError as exc:
            return self._edit_result("add_reference", dry, fields, error=str(exc), notes=notes)
        if correct_units is True:
            notes.append("correct_units is ignored on the fake scene: it does not read the asset's units.")
        plan: dict[str, Any] = {
            "path": prim_path,
            "parent": parent_path,
            "type": "Xform",
            "reference": {"asset_path": os.path.abspath(asset).replace("\\", "/"), "prim_path": asset_prim_path},
            "translation": moved,
            "rotation": turned,
            "scale": sized,
            "instanceable": instanceable if isinstance(instanceable, bool) else None,
        }
        if dry:
            return self._edit_result("add_reference", True, fields, notes=notes, would_create=plan)
        state = self._state()
        self._add_prim(prim_path, "Xform")
        record = self._prims[prim_path]
        if moved is not None:
            record.translation = tuple(moved)
        if turned is not None:
            record.rotation = tuple(turned)
        if sized is not None:
            record.scale = tuple(sized)
        stored = {"references": {"type_name": "references", "default": [dict(plan["reference"])]}}
        if plan["instanceable"] is not None:
            stored["instanceable"] = {"type_name": "bool", "default": plan["instanceable"]}
        self._attributes[prim_path] = stored
        self._record(f"Add {os.path.basename(asset)} at {prim_path}", [prim_path], state)
        created = {"path": prim_path, "type": "Xform", "references": [dict(plan["reference"])], "reference_resolved": None}
        return self._edit_result("add_reference", False, fields, notes=notes, written=True, would_create=plan,
                                 created=created)

    def physics_report(self, prim_path: str | None = None) -> dict[str, Any]:
        return self._not_supported("physics_report", "The physics report", prim_path=prim_path)

    # --- RPC tier5-1: the homework check and scene tour need a real USD stage -------------------------------------

    def assignment_check(self, assignment_id: str = "isaac_session02_warehouse") -> dict[str, Any]:
        return self._not_supported("assignment_check", "Checking homework", assignment_id=str(assignment_id))

    def scene_tour(self, focus_path: str | None = None, **_options: Any) -> dict[str, Any]:
        return self._not_supported("scene_tour", "The scene tour", focus_path=focus_path)

    # --- RPC tier6-1: smart layout measures real bounding boxes and the robot inspector reads UsdPhysics ---------

    def layout_snap(self, paths: Any = (), **_options: Any) -> dict[str, Any]:
        return self._not_supported("layout_snap", "Snapping prims onto a surface", paths=_listed(paths))

    def layout_stack(self, paths: Any = (), **_options: Any) -> dict[str, Any]:
        return self._not_supported("layout_stack", "Stacking prims in rows", paths=_listed(paths))

    def layout_distribute(self, paths: Any = (), surface_path: str | None = None, **_options: Any) -> dict[str, Any]:
        return self._not_supported("layout_distribute", "Spreading prims on a surface", paths=_listed(paths))

    def layout_space(self, paths: Any = (), **_options: Any) -> dict[str, Any]:
        return self._not_supported("layout_space", "Spacing prims evenly", paths=_listed(paths))

    def layout_align(self, paths: Any = (), **_options: Any) -> dict[str, Any]:
        return self._not_supported("layout_align", "Aligning prims", paths=_listed(paths))

    def robot_inspect(self, root_path: str | None = None, **_options: Any) -> dict[str, Any]:
        return self._not_supported("robot_inspect", "The robot inspector", root_path=root_path)

    # --- RPC tier7-1: the layer manager reads real USD layers and the asset health check a real stage -----------

    def layer_stack(self, **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_stack", "The layer stack")

    def layer_where_is(self, prim_path: str = "", attribute: str | None = None, **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_where_is", "Finding which layer sets a value", prim_path=str(prim_path)[:300])

    def layer_flatten_preview(self, prim_path: str = "", **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_flatten_preview", "The flatten preview", prim_path=str(prim_path)[:300])

    def layer_add_sublayer(self, layer_path: str = "", **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_add_sublayer", "Adding a sublayer", layer=str(layer_path)[:300])

    def layer_remove_sublayer(self, layer: str = "", **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_remove_sublayer", "Removing a sublayer", layer=str(layer)[:300])

    def layer_set_edit_target(self, layer: str = "", **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_set_edit_target", "Changing the edit target", layer=str(layer)[:300])

    def layer_mute(self, layer: str = "", **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_mute", "Muting a layer", layer=str(layer)[:300])

    def layer_unmute(self, layer: str = "", **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_unmute", "Unmuting a layer", layer=str(layer)[:300])

    def layer_move_opinions(self, prim_path: str = "", *_layers: Any, **_options: Any) -> dict[str, Any]:
        return self._not_supported("layer_move_opinions", "Moving opinions between layers", prim_path=str(prim_path)[:300])

    def asset_health(self, root_path: str | None = None, **_options: Any) -> dict[str, Any]:
        return self._not_supported("asset_health", "The asset health check", root_path=root_path)

    def add_physics_scene(self, path: str = "/physicsScene", **_options: Any) -> dict[str, Any]:
        return self._not_supported("add_physics_scene", "Adding a PhysicsScene", path=str(path))

    def make_rigid_body(self, prim_path: str, **_options: Any) -> dict[str, Any]:
        return self._not_supported("make_rigid_body", "Making rigid bodies", path=str(prim_path))

    def make_collider(self, prim_path: str, **_options: Any) -> dict[str, Any]:
        return self._not_supported("make_collider", "Adding colliders", path=str(prim_path))

    def remove_physics(self, prim_path: str, **_options: Any) -> dict[str, Any]:
        return self._not_supported("remove_physics", "Removing physics", path=str(prim_path))

    def capture_viewport(self, output_dir: Any = None, **_options: Any) -> dict[str, Any]:
        return self._not_supported("capture_viewport", "Taking viewport screenshots", hint="The fake scene has no viewport.")

    def capture_status(self, path: Any = None) -> dict[str, Any]:
        return self._not_supported("capture_status", "Checking viewport screenshots", path=str(path))

    def simulation_status(self) -> dict[str, Any]:
        """The fake timeline's state (read-only); there is never any physics."""
        return self._sim_status()

    def simulation_play(self) -> dict[str, Any]:
        """Start the fake timeline (nothing moves: the fake scene has no physics)."""
        was_playing = bool(self._timeline_state["playing"])
        self._timeline_state["playing"] = True
        self._sim_paused = False
        return self._sim_result("play", "The simulation was already playing." if was_playing else "Playing (fake timeline).")

    def simulation_pause(self) -> dict[str, Any]:
        """Pause the fake timeline at its current time."""
        was_paused = self._sim_paused and not self._timeline_state["playing"]
        self._timeline_state["playing"] = False
        self._sim_paused = True
        return self._sim_result("pause", "The simulation was already paused." if was_paused else "Paused at the current time.")

    def simulation_stop(self) -> dict[str, Any]:
        """Stop the fake timeline and reset it to its start time."""
        self._timeline_state["playing"] = False
        self._sim_paused = False
        self._timeline_state["current_time_code"] = float(self._timeline_state["start_time_code"])
        return self._sim_result("stop", "Stopped: the timeline was reset to its start time.")

    def simulation_step(self, frames: Any = 1) -> dict[str, Any]:
        """Advance a paused fake timeline by frames time codes (capped at 600); refused while playing or stopped."""
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
            return {"ok": False, "message": "frames must be a whole number from 1 to 600", "mode": "fake", "fake_scene": True}
        if self._timeline_state["playing"]:
            return {"ok": False, "message": "The simulation is playing; call simulation_pause first, then step.",
                    "mode": "fake", "fake_scene": True}
        if not self._sim_paused:  # like Kit: a stopped timeline must be played and paused first
            return {"ok": False, "message": "The simulation is stopped; call simulation_play, then simulation_pause, "
                    "then step (so Stop can put every body back afterwards).", "mode": "fake", "fake_scene": True}
        count = min(frames, MAX_STEP_FRAMES)
        self._sim_paused = True
        self._timeline_state["current_time_code"] = float(self._timeline_state["current_time_code"]) + count
        result = self._sim_result("step", f"Stepped {count} frame(s) on the fake timeline.")
        rate = float(self._timeline_state["time_codes_per_second"]) or 24.0
        result.update(frames=count, requested_frames=frames, capped=frames > count, seconds_per_frame=1.0 / rate,
                      stepped_seconds=count / rate, via="fake_timeline")
        return result

    def _sim_status(self) -> dict[str, Any]:
        state = self._timeline_state
        rate = float(state["time_codes_per_second"]) or 24.0
        playing = bool(state["playing"])
        paused = not playing and self._sim_paused
        current = float(state["current_time_code"])
        return {
            "ok": True,
            "playing": playing,
            "paused": paused,
            "stopped": not playing and not paused,
            "current_time": current / rate,
            "current_time_code": current,
            "start_time": float(state["start_time_code"]) / rate,
            "end_time": float(state["end_time_code"]) / rate,
            "time_codes_per_second": rate,
            "physics_available": False,
            "mode": "fake",
            "fake_scene": True,
        }

    def _sim_result(self, action: str, message: str) -> dict[str, Any]:
        return {**self._sim_status(), "action": action, "message": message, "note": FAKE_SIM_NOTE}

    # --- in-memory attribute / material edits (the fake counterpart of edits.py) --------------------------------

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
        """Store (or with dry_run, preview storing) one attribute value on a fake prim; any well-formed name works."""
        dry = dry_run is not False
        fields: dict[str, Any] = {"path": str(path), "attribute": str(name)}
        prim = self._prims.get(path) if isinstance(path, str) else None
        if prim is None:
            return self._edit_result("set_attribute", dry, fields, error=f"Prim does not exist: {path}")
        if not isinstance(name, str) or not _ATTRIBUTE_NAME.match(name.strip()):
            return self._edit_result("set_attribute", dry, fields, error=f"{name!r} is not a valid attribute name.")
        name = name.strip()
        if value is None:
            return self._edit_result("set_attribute", dry, fields, error="value is required (None cannot be authored).")
        if time is not None and (isinstance(time, bool) or not isinstance(time, (int, float)) or not math.isfinite(time)):
            return self._edit_result("set_attribute", dry, fields, error=f"time must be a finite number, got {time!r}.")
        key = "default" if time is None else float(time)
        record = self._attributes.get(path, {}).get(name)
        old = None if record is None else (record.get("default") if key == "default" else record.get("samples", {}).get(key))
        after = _plain(value)
        notes = [FAKE_EDIT_NOTE]
        if create_type is not None:
            notes.append(f"create_type {create_type!r} is recorded but not checked: the fake scene has no value types.")
        fields.update(
            type_name=None if record is None else record.get("type_name"),
            time=key,
            creates_attribute=record is None,
            before={"exists": record is not None, "value": deepcopy(old)},
            after=after,
        )
        would_change = record is None or old != after
        if dry:
            return self._edit_result("set_attribute", True, fields, notes=notes, would_change=would_change)
        state = self._state()
        entry = self._attributes.setdefault(path, {}).setdefault(name, {"type_name": create_type})
        if key == "default":
            entry["default"] = after
        else:
            entry.setdefault("samples", {})[key] = after
        self._record(f"Set {name} on {path}", [path], state)
        return self._edit_result("set_attribute", False, fields, notes=notes, written=True, changed=would_change,
                                 readback={"value": deepcopy(after), "shows_new_value": True})

    def create_material(
        self,
        path: str,
        *,
        diffuse_color: Any = None,
        roughness: float | None = None,
        metallic: float | None = None,
        opacity: float | None = None,
        emissive_color: Any = None,
        ior: float | None = None,
        create_parent: bool = False,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Add (or with dry_run, preview adding) a Material prim with a PreviewSurface shader child."""
        dry = dry_run is not False
        fields: dict[str, Any] = {"path": str(path)}
        warnings: list[str] = []
        notes = [FAKE_EDIT_NOTE]
        try:
            if not isinstance(path, str) or not path.startswith("/") or path.endswith("/") or "//" in path:
                raise ValueError(f"{path!r} is not an absolute prim path.")
            if path in self._prims:
                raise ValueError(f"A prim already exists at {path} ({self._prims[path].type_name}); choose a new path.")
            parent_path = self._parent_path(path)
            new_parent = None
            if parent_path and parent_path not in self._prims:
                grandparent = self._parent_path(parent_path)
                if create_parent is True and (grandparent is None or grandparent in self._prims):
                    new_parent = parent_path
                    notes.append(f"Also creates the missing parent {parent_path} as a Scope.")
                else:
                    raise ValueError(f"Parent prim does not exist: {parent_path}. Create it first.")
            inputs: dict[str, Any] = {}
            if diffuse_color is not None:
                inputs["diffuseColor"] = _color(diffuse_color, "diffuse_color", notes, allow_hdr=False)
            if emissive_color is not None:
                inputs["emissiveColor"] = _color(emissive_color, "emissive_color", notes, allow_hdr=True)
            for label, input_name, given in (("roughness", "roughness", roughness), ("metallic", "metallic", metallic),
                                              ("opacity", "opacity", opacity)):
                if given is not None:
                    inputs[input_name] = _unit(given, label)
            if ior is not None:
                number = _finite(ior, "ior")
                if number <= 0.0:
                    raise ValueError(f"ior must be greater than 0, got {number:g}")
                inputs["ior"] = number
        except ValueError as exc:
            return self._edit_result("create_material", dry, fields, error=str(exc), notes=notes)
        shader_path = f"{path}/{SHADER_NAME}"
        plan: dict[str, Any] = {
            "material": path,
            "parent": parent_path,
            "type": "Material",
            "shader": {"path": shader_path, "type": "Shader", "id": "UsdPreviewSurface"},
            "inputs": {
                name: {"attribute": f"inputs:{name}", "type": "color3f" if name.endswith("Color") else "float", "value": value}
                for name, value in inputs.items()
            },
            "connections": {f"{path}.outputs:surface": f"{shader_path}.outputs:surface"},
        }
        if new_parent is not None:
            plan["creates_parent"] = {"path": new_parent, "type": "Scope"}
        fields["shader_path"] = shader_path
        if dry:
            return self._edit_result("create_material", True, fields, warnings=warnings, notes=notes, would_create=plan)
        state = self._state()
        if new_parent is not None:
            self._add_prim(new_parent, "Scope")
        self._add_prim(path, "Material")
        self._add_prim(shader_path, "Shader")
        self._attributes[shader_path] = {
            "info:id": {"type_name": "token", "default": "UsdPreviewSurface"},
            **{f"inputs:{name}": {"type_name": plan["inputs"][name]["type"], "default": value} for name, value in inputs.items()},
        }
        self._attributes[path] = {"outputs:surface": {"type_name": "token", "connection": f"{shader_path}.outputs:surface"}}
        self._record(f"Create material {path}", [new_parent or path], state)
        created = {"path": path, "type": "Material", "shader": shader_path, "shader_id": "UsdPreviewSurface",
                   "inputs": deepcopy(inputs), "surface_source": f"{shader_path}.outputs:surface"}
        if new_parent is not None:
            created["created_parent"] = new_parent
        return self._edit_result("create_material", False, fields, warnings=warnings, notes=notes, written=True,
                                 would_create=plan, created=created)

    def bind_material(
        self,
        prim_path: str,
        material_path: str,
        *,
        strength: str | None = None,
        purpose: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Bind (or with dry_run, preview binding) a fake Material prim to a fake prim."""
        dry = dry_run is not False
        fields: dict[str, Any] = {"path": str(prim_path), "material": str(material_path)}
        notes = [FAKE_EDIT_NOTE]
        prim = self._prims.get(prim_path) if isinstance(prim_path, str) else None
        material = self._prims.get(material_path) if isinstance(material_path, str) else None
        error = None
        if prim is None:
            error = f"Prim does not exist: {prim_path}"
        elif material is None:
            error = f"Material prim does not exist: {material_path}"
        elif material.type_name != "Material":
            error = f"{material_path} is a {material.type_name}, not a Material."
        elif prim.type_name in {"Material", "Shader", "NodeGraph"}:
            error = f"{prim_path} is a {prim.type_name}; bind materials to geometry or its parents instead."
        strength_name = _STRENGTHS.get(str(strength).strip().lower()) if strength is not None else None
        purpose_name = _PURPOSES.get(str(purpose or "").strip().lower())
        if error is None and strength is not None and strength_name is None:
            error = f"strength must be weakerThanDescendants or strongerThanDescendants, not {strength!r}."
        if error is None and purpose_name is None:
            error = f"purpose must be allPurpose, preview or full, not {purpose!r}."
        if error is not None:
            return self._edit_result("bind_material", dry, fields, error=error, notes=notes)
        relationship = "material:binding" if purpose_name == "allPurpose" else f"material:binding:{purpose_name}"
        if purpose_name == "allPurpose":
            current = prim.material
        else:
            current = self._attributes.get(prim_path, {}).get(relationship, {}).get("default")
        fields.update(
            purpose=purpose_name,
            strength=strength_name or "weakerThanDescendants",
            strength_authored=strength_name is not None,
            relationship=relationship,
            before={"bound_material": current},
            predicted={"material": material_path, "binding_wins": True, "method": "fake_scene"},
        )
        would_change = current != material_path or strength_name is not None
        if dry:
            return self._edit_result("bind_material", True, fields, notes=notes, would_change=would_change)
        state = self._state()
        if purpose_name == "allPurpose":
            self._prims[prim_path].material = material_path
        else:
            self._attributes.setdefault(prim_path, {})[relationship] = {"type_name": "rel", "default": material_path}
        if strength_name is not None:
            self._attributes.setdefault(prim_path, {})[f"{relationship}:bindMaterialAs"] = {
                "type_name": "token", "default": strength_name}
        self._record(f"Bind {material_path} to {prim_path}", [prim_path], state)
        return self._edit_result("bind_material", False, fields, notes=notes, written=True, changed=would_change,
                                 after={"bound_material": material_path})

    def get_attribute(self, path: str, name: str) -> Any:
        """The stored default value of a fake attribute (None when it was never set). Test helper; read-only."""
        record = self._attributes.get(path, {}).get(name)
        return deepcopy(record.get("default")) if record else None

    # --- in-memory undo (the fake counterpart of undo.py / kit_commands.py) -------------------------------------

    def undo_last(self, dry_run: bool = True) -> dict[str, Any]:
        """Undo (dry_run=True, the default: only preview) the newest recorded in-memory edit."""
        dry = dry_run is not False
        base = {"mode": "fake", "fake_scene": True, "note": SAVE_NOTE}
        if not self._undo:
            return {"ok": False, "dry_run": dry, "undone": None, "message": NOTHING_TO_UNDO, **base}
        entry = self._undo[-1]
        summary = {"label": entry["label"], "paths": list(entry["paths"]), "layer": "fake:in-memory"}
        if dry:
            return {
                "ok": True,
                "dry_run": True,
                "undone": None,
                "would_undo": summary,
                "message": "Dry run: nothing changed. Call again with dry_run=False to undo this edit.",
                "remaining": len(self._undo),
                **base,
            }
        self._undo.pop()
        self._restore(entry["before"])
        return {"ok": True, "dry_run": False, "undone": summary, "verified": True, "remaining": len(self._undo),
                "via": "fake_snapshot", **base}

    def undo_history(self, limit: int = 20) -> dict[str, Any]:
        """The newest recorded in-memory edits (up to limit, 1-50), newest first. Read-only."""
        try:
            count = max(1, min(int(limit), MAX_UNDO_ENTRIES))
        except (TypeError, ValueError):
            count = 20
        entries = []
        for index, entry in enumerate(reversed(self._undo[-count:] if count else []), start=1):
            entries.append({
                "index": index,
                "label": entry["label"],
                "paths": list(entry["paths"]),
                "layer": "fake:in-memory",
                "via": "fake",
                "state": "done",
                "created": _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(entry["created"])),
                "stale": False,
            })
        return {
            "ok": True,
            "mode": "fake",
            "fake_scene": True,
            "count": len(self._undo),
            "max_entries": MAX_UNDO_ENTRIES,
            "dropped": self._undo_dropped,
            "entries": entries,
            "kit_ctrl_z": False,
            "note": SAVE_NOTE,
        }

    def _state(self) -> dict[str, Any]:
        return {
            "prims": deepcopy(self._prims),
            "variants": deepcopy(self._variant_selections),
            "attributes": deepcopy(self._attributes),
        }

    def _restore(self, state: dict[str, Any]) -> None:
        self._prims = deepcopy(state["prims"])
        self._variant_selections = deepcopy(state["variants"])
        self._attributes = deepcopy(state["attributes"])
        self._selection = [path for path in self._selection if path in self._prims]

    def _record(self, label: str, paths: list[str], before: dict[str, Any]) -> None:
        """Push an undo entry when the edit changed the in-memory scene (no-op edits are not recorded)."""
        if before == self._state():
            return
        self._undo.append({"label": label, "paths": list(dict.fromkeys(paths)), "before": before, "created": _time.time()})
        while len(self._undo) > MAX_UNDO_ENTRIES:
            self._undo.pop(0)
            self._undo_dropped += 1

    def _add_prim(self, path: str, type_name: str) -> None:
        parent_path = self._parent_path(path)
        self._prims[path] = PrimRecord(path=path, type_name=type_name, parent=parent_path)
        if parent_path:
            self._link_child(parent_path, path)

    @staticmethod
    def _edit_result(
        action: str,
        dry: bool,
        fields: dict[str, Any],
        *,
        error: str | None = None,
        warnings: list[str] | None = None,
        notes: list[str] | None = None,
        written: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        """The edits.py result shape: {ok, dry_run, action, written, ..., warnings, notes, next_step?}."""
        result: dict[str, Any] = {"ok": error is None, "dry_run": dry, "action": action, "written": written}
        if error is not None:
            result["error"] = error
        result.update(fields)
        result.update(extra)
        result.update(mode="fake", fake_scene=True, edit_target_layer="fake:in-memory",
                      warnings=list(warnings or []), notes=list(notes or [FAKE_EDIT_NOTE]))
        if error is None and dry:
            result["next_step"] = "Dry run: nothing was written. Call again with dry_run=False to author this."
        return result

    @staticmethod
    def _not_supported(method: str, what: str, **extra: Any) -> dict[str, Any]:
        return unsupported_result(
            method,
            f"{what} is not supported on the in-memory fake scene; {_FAKE_HINT}",
            needs_bridge_update=False,
            fake_scene=True,
            **extra,
        )

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


def _listed(paths: Any) -> list[str]:
    """The prim paths a layout call named (at most 20 of them, as text), for the not-supported answer."""
    items = [paths] if isinstance(paths, str) else list(paths) if isinstance(paths, (list, tuple)) else []
    return [str(item)[:300] for item in items[:20]]


def _plain(value: Any) -> Any:
    """Tuples become lists, recursively, so stored values look like their JSON form."""
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return float(value)


def _vector_or_none(value: Any, label: str) -> list[float] | None:
    """None, or exactly three finite numbers as a list."""
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must be three numbers (x, y, z), got {value!r}")
    return [_finite(item, label) for item in value]


def _unit(value: Any, label: str) -> float:
    number = _finite(value, label)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1, got {number:g}")
    return number


def _color(value: Any, label: str, notes: list[str], *, allow_hdr: bool) -> list[float]:
    """[r, g, b] in 0-1; '#RRGGBB' and whole-number 0-255 values are converted, like edits.py does."""
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if not re.fullmatch(r"[0-9A-Fa-f]{6}", text):
            raise ValueError(f"{label} must be [r, g, b] or '#RRGGBB', got {value!r}")
        notes.append(f"{label} #{text} was converted to 0-1 values.")
        return [round(int(text[i : i + 2], 16) / 255.0, 6) for i in (0, 2, 4)]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must be three numbers [r, g, b]")
    numbers = [_finite(item, label) for item in value]
    if any(number < 0.0 for number in numbers):
        raise ValueError(f"{label} must not be negative")
    if any(number > 1.0 for number in numbers):
        if all(number.is_integer() and number <= 255 for number in numbers) and not allow_hdr:
            notes.append(f"{label} looked like 0-255 values, so it was divided by 255.")
            return [round(number / 255.0, 6) for number in numbers]
        if not allow_hdr:
            raise ValueError(f"{label} values must be 0-1 (or whole numbers 0-255), got {numbers}")
    return numbers
