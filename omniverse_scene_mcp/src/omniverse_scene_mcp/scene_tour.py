"""Explain my scene: a plain-language, read-only tour of a USD stage for beginners.

scene_tour(stage) walks the composed stage once and returns JSON-safe sections (groups, geometry, lights, cameras,
materials, physics, layers and referenced assets, editor helpers) where every item carries one friendly sentence,
plus a short overview, a glossary of the USD words it used and a few questions to explore next.

It imports only pxr and the standard library (no omni.*), so it runs on a file stage or inside Kit. It never edits,
saves or reloads anything: it only reads composed values, transforms and bounding boxes.

Distances are given in metres (scene units times metersPerUnit) and in world space unless a sentence says "local".
Directions use simple stage-relative words: on a Z-up stage +Y is "the back" and +X "the right"; on a Y-up stage
-Z is "the back" and +X "the right" (the view a default camera has). Placement words ("on the floor", "against the
back wall", "on top of LowerShelf") come from bounding-box heuristics, so they are hints, not guarantees.
"""

from __future__ import annotations

import colorsys
import math
import re
import time
from typing import Any

from . import usd_insight
from .usd_insight import TimeBudget, resolve_budget

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = UsdLux = UsdShade = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

MAX_SECTION_ITEMS = 40
MAX_QUESTIONS = 5
MAX_NAMES_LISTED = 4

GEOMETRY_TYPES = {
    "Cube", "Sphere", "Cylinder", "Cylinder_1", "Cone", "Capsule", "Capsule_1", "Plane", "Mesh", "TetMesh",
    "BasisCurves", "NurbsCurves", "HermiteCurves", "NurbsPatch", "Points",
}
GROUP_TYPES = {"Xform", "Scope", ""}
LIGHT_WORDS = {
    "RectLight": "a rectangular panel light (like a photo softbox) that shines out of one face",
    "DiskLight": "a round panel light that shines out of one face",
    "DistantLight": "a sun-like light: it is infinitely far away, so only its direction matters",
    "DomeLight": "a sky dome that lights the whole scene from all around",
    "DomeLight_1": "a sky dome that lights the whole scene from all around",
    "SphereLight": "a light bulb that glows in every direction",
    "CylinderLight": "a tube light (like a fluorescent tube) that glows all around its length",
    "PortalLight": "a window-shaped helper that guides dome light into a room",
    "GeometryLight": "a light that glows from the surface of a shape",
}
DIRECTIONAL_LIGHTS = {"RectLight", "DiskLight", "DistantLight"}
EDITOR_CAMERAS = {
    "OmniverseKit_Persp": "the viewport's built-in Perspective camera",
    "OmniverseKit_Front": "the viewport's built-in Front view camera",
    "OmniverseKit_Top": "the viewport's built-in Top view camera",
    "OmniverseKit_Right": "the viewport's built-in Right view camera",
}
THEME_WORDS = (
    ("warehouse", "a warehouse"), ("factory", "a factory"), ("kitchen", "a kitchen"), ("office", "an office"),
    ("garage", "a garage"), ("street", "a street"), ("city", "a city block"), ("lab", "a lab"),
    ("robot", "a robot setup"), ("shel", "a storage area with shelves"), ("room", "a room"), ("table", "a table setup"),
)
_FLOOR_NAME = re.compile(r"floor|ground", re.IGNORECASE)
_WALL_SIDE_NAME = re.compile(r"back|front|left|right", re.IGNORECASE)

GLOSSARY = {
    "stage": "The stage is the whole scene you see: every layer merged into one result.",
    "prim": "A prim is one item in the scene tree, like a folder or an object; its path (such as /World/Box) says where it lives.",
    "Xform": "An Xform is a prim with a position, rotation and scale; used as a group, it moves everything inside it.",
    "Scope": "A Scope is a plain folder prim with no position of its own, used only to organise.",
    "default prim": "The default prim is the top prim another file gets when it references this file without naming a prim.",
    "upAxis": "upAxis is stage metadata saying which axis points up: Y or Z (Isaac Sim uses Z).",
    "metersPerUnit": "metersPerUnit is stage metadata saying how long one scene unit is (1 = metres, 0.01 = centimetres).",
    "world space": "World space positions are measured from the scene origin, after every parent's move, turn and scale.",
    "local space": "Local space values are relative to the parent prim: what is typed into the prim's own transform.",
    "bounding box": "A bounding box is the smallest axis-aligned box around an object; the sizes in this tour come from it.",
    "Cube": "A Cube is a built-in box shape; its size attribute is the edge length before any scaling.",
    "Sphere": "A Sphere is a built-in ball shape set by its radius.",
    "Cylinder": "A Cylinder is a built-in tube shape set by radius, height and axis.",
    "Cone": "A Cone is a built-in cone shape set by radius, height and axis.",
    "Capsule": "A Capsule is a built-in pill shape: a cylinder with rounded ends.",
    "Plane": "A Plane is a built-in flat rectangle set by width, length and axis.",
    "Mesh": "A Mesh is a surface built from points and polygon faces, the usual format for imported models.",
    "Points": "A Points prim is a cloud of dots, such as particles.",
    "BasisCurves": "BasisCurves are lines or curves, such as hair, wires or paths.",
    "purpose": "purpose tags a prim as default, render, proxy or guide; guide prims are editor helpers and are not rendered.",
    "visibility": "visibility can hide a prim; 'invisible' on a parent also hides everything inside it, because it is inherited.",
    "light": "A light prim adds light to the scene; most panel lights shine along their local -Z axis.",
    "RectLight": "A RectLight is a rectangular area light that shines from one side, like a softbox or ceiling panel.",
    "DiskLight": "A DiskLight is a round area light that shines from one side.",
    "DistantLight": "A DistantLight is a sun: parallel rays from very far away, so only its direction matters.",
    "DomeLight": "A DomeLight surrounds the scene like a sky and lights it from every direction, often from an HDR image.",
    "SphereLight": "A SphereLight is a glowing ball that shines in every direction, like a light bulb.",
    "CylinderLight": "A CylinderLight is a glowing tube, like a fluorescent tube light.",
    "intensity": "Light brightness is intensity x 2^exposure, so raising exposure by 1 doubles the light.",
    "camera": "A Camera prim is a viewpoint you can look through and render from; it looks along its local -Z axis.",
    "focal length": "Focal length (in mm) sets the zoom: small numbers give a wide view, large numbers zoom in.",
    "material": "A Material describes how a surface looks (colour, shininess) with shader prims inside it.",
    "shader": "A Shader prim does the look calculation for a material, for example UsdPreviewSurface or an MDL like OmniPBR.",
    "material binding": "A material binding links a prim to a material; children use their parent's binding unless they have their own.",
    "physics scene": "A PhysicsScene prim switches on simulation for the stage and sets gravity.",
    "rigid body": "A rigid body (PhysicsRigidBodyAPI) is an object physics moves: gravity pulls it and collisions push it.",
    "collider": "A collider (PhysicsCollisionAPI) gives a prim a solid shape that other things bump into.",
    "kinematic": "A kinematic rigid body is moved only by animation or code, never by gravity or collisions.",
    "joint": "A joint connects two bodies and limits how they can move relative to each other, like a hinge.",
    "articulation": "An articulation is a chain of bodies and joints simulated together, like a robot arm.",
    "applied API schema": "An applied API schema (such as PhysicsRigidBodyAPI) adds a bundle of extra properties and behaviour to a prim.",
    "layer": "A layer is one USD file (or in-memory document) holding opinions about prims; a stage stacks layers.",
    "root layer": "The root layer is the file you opened; new edits are saved there by default.",
    "sublayer": "A sublayer is a whole layer stacked under another; its prims merge in and the upper layer's opinions win.",
    "session layer": "The session layer is an in-memory scratch layer above the root layer; it is never saved into your file.",
    "opinion": "An opinion is a value some layer states for a property; when layers disagree, the strongest one wins.",
    "reference": "A reference pulls prims from another file (or another prim) into this prim, like a linked copy you can override.",
    "payload": "A payload is a reference that can be unloaded to save memory and loading time.",
    "variant set": "A variant set is a switch with named choices (such as colour = red or blue); the selected choice adds its opinions.",
    "inherit": "An inherit links a prim to a class prim, so a change to the class flows to every prim that inherits it.",
    "composition": "Composition is how USD merges layers, references, payloads and variants into the one scene you see.",
    "over": "An over only adds tweaks to a prim defined somewhere else; on its own it defines nothing and draws nothing.",
    "class": "A class prim is an abstract template that is never drawn; other prims inherit from it.",
    "inactive": "An inactive prim is switched off: USD ignores it and everything inside it until it is activated again.",
    "instance": "An instanceable prim shares its insides with other copies to save memory; you edit the shared original, not each copy.",
    "editor helper": "Editor helpers are prims an app makes for itself (viewport cameras, render settings); they are not your content.",
}


def scene_tour(
    stage: Any,
    *,
    focus_path: str | None = None,
    depth: int = 3,
    max_prims: int = 400,
    deadline: Any = None,
) -> dict[str, Any]:
    """A beginner-friendly tour of a stage as JSON-safe data. Read-only: nothing is edited, saved or reloaded.

    focus_path limits the tour to one prim and everything below it (layers are still described stage-wide).
    depth limits how many hierarchy levels of groups the "Groups" section lists (1 = only top-level prims);
    geometry, lights and the rest are listed at any depth. max_prims caps how many prims are visited.
    deadline is a usd_insight.TimeBudget, or a time.monotonic() value to stop at; an ambient
    ``with TimeBudget(ms):`` also applies. When a limit stops the walk the result says so (truncated,
    truncated_by_time) and describes what was seen.

    Returns {ok, overview, sections: [{title, items: [{path, kind, plain, tip?, facts?}]}], glossary,
    questions_to_explore, units, counts, truncated, truncated_by_time, ...}.
    """
    _require_pxr()
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        raise ValueError("depth must be a whole number, 0 or more")
    if isinstance(max_prims, bool) or not isinstance(max_prims, int) or max_prims < 1:
        raise ValueError("max_prims must be a whole number, 1 or more")
    if isinstance(deadline, TimeBudget) or deadline is None:
        budget = resolve_budget(deadline)
    else:
        budget = resolve_budget(deadline, now=time.monotonic())
    root_prim = stage.GetPseudoRoot()
    if focus_path:
        text = str(focus_path)
        if not Sdf.Path.IsValidPathString(text) or not Sdf.Path(text).IsAbsoluteRootOrPrimPath():
            return {"ok": False, "error": f"focus_path is not a valid prim path: {text!r}", "focus_path": text}
        root_prim = stage.GetPrimAtPath(text)
        if not root_prim or not root_prim.IsValid():
            return {"ok": False, "error": f"No prim at focus_path {text}.", "focus_path": text}
    return _Tour(stage, root_prim, focus_path or None, depth, max_prims, budget).run()


class _Tour:
    def __init__(self, stage: Any, root: Any, focus: str | None, depth: int, max_prims: int, budget: Any) -> None:
        self.stage = stage
        self.root = root
        self.focus = focus
        self.depth = depth
        self.max_prims = max_prims
        self.budget = budget
        self.time_code = Usd.TimeCode.Default()
        self.up = "Z" if str(UsdGeom.GetStageUpAxis(stage)) == "Z" else "Y"
        self.mpu = float(UsdGeom.GetStageMetersPerUnit(stage)) or 1.0
        self.H = 2 if self.up == "Z" else 1  # index of the up axis
        self.X = 0  # +X is "right"
        self.B = 1 if self.up == "Z" else 2  # index of the back/front axis
        self.back_sign = 1.0 if self.up == "Z" else -1.0  # +Y (Z-up) or -Z (Y-up) is "the back"
        self.coord_names = ("x", "y") if self.up == "Z" else ("x", "z")
        self.xcache = UsdGeom.XformCache(self.time_code)
        self.bcache = UsdGeom.BBoxCache(
            self.time_code,
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy, UsdGeom.Tokens.guide],
            True,
        )
        self.terms: dict[str, None] = {}
        self.visited = 0
        self.truncated = False
        self.time_stopped = False
        self.notes: list[str] = []
        self.groups: list[dict[str, Any]] = []
        self.geometry: list[dict[str, Any]] = []
        self.lights: list[dict[str, Any]] = []
        self.cameras: list[dict[str, Any]] = []
        self.materials: list[dict[str, Any]] = []
        self.bindings: dict[str, list[str]] = {}
        self.effective_material: dict[str, str] = {}
        self.physics: list[dict[str, Any]] = []
        self.colliders: set[str] = set()
        self.rigid_bodies: set[str] = set()
        self.compose_items: list[dict[str, Any]] = []
        self.editor_items: list[dict[str, Any]] = []
        self.special_items: list[dict[str, Any]] = []
        self.overridden_lights: list[dict[str, Any]] = []
        self.hidden_paths: list[str] = []
        self.scaled_groups: list[dict[str, Any]] = []
        self.floor: dict[str, Any] | None = None
        self.scene: tuple[list[float], list[float]] | None = None
        self._content: list[dict[str, Any]] | None = None
        self.walls: list[dict[str, Any]] = []
        session = stage.GetSessionLayer()
        self.session = session if session is not None and not session.empty else None
        self.bound_camera = None
        try:
            settings = dict(stage.GetRootLayer().customLayerData).get("cameraSettings") or {}
            self.bound_camera = settings.get("boundCamera")
        except Exception:
            self.bound_camera = None

    # ------------------------------------------------------------------ walk

    def _out_of_time(self) -> bool:
        if self.budget is not None and self.budget.tick():
            self.time_stopped = True
            return True
        return False

    def run(self) -> dict[str, Any]:
        self._walk()
        self._measure_scene()
        self._find_floor_and_walls()
        sections = []
        for title, items in (
            ("Groups and structure", self._group_items()),
            ("Shapes and geometry", self._geometry_items()),
            ("Lights", self._light_items()),
            ("Cameras", self._camera_items()),
            ("Materials", self._material_items()),
            ("Physics", self._physics_items()),
            ("Layers and referenced assets", self._layer_items()),
            ("Editor helpers (not your content)", self.editor_items),
            ("Switched-off and template prims", self.special_items),
        ):
            if items:
                sections.append(_section(title, items))
        counts = {
            "prims_visited": self.visited,
            "groups": len(self.groups),
            "geometry": len(self.geometry),
            "lights": len(self.lights),
            "cameras": len(self.cameras),
            "materials": len(self.materials),
            "physics": len(self.physics),
            "composition_items": len(self.compose_items),
            "editor_helpers": len(self.editor_items),
        }
        result: dict[str, Any] = {
            "ok": True,
            "focus_path": self.focus,
            "overview": self._overview(),
            "sections": sections,
            "glossary": [{"term": term, "meaning": GLOSSARY[term]} for term in self.terms if term in GLOSSARY],
            "questions_to_explore": self._questions(),
            "units": {
                "up_axis": self.up,
                "meters_per_unit": usd_insight._number(self.mpu),
                "in_words": self._units_words(),
                "directions": self._direction_convention(),
            },
            "counts": counts,
            "max_prims": self.max_prims,
            "depth": self.depth,
            "truncated": self.truncated,
            "truncated_by_time": self.time_stopped,
            "notes": self.notes,
        }
        if self.budget is not None:
            note = None
            if self.time_stopped:
                note = f"Stopped at the time budget after {self.visited} prim(s); the tour describes only what was seen."
            result["time_budget"] = self.budget.report(self.visited, self.time_stopped, note)
        return result

    def _walk(self) -> None:
        predicate = Usd.PrimAllPrimsPredicate
        prim_range = Usd.PrimRange(self.root, predicate)
        iterator = iter(prim_range)
        base_depth = self.root.GetPath().pathElementCount
        if self.root.IsPseudoRoot():
            next(iterator, None)  # skip the pseudo-root itself
        for prim in iterator:
            if self.visited >= self.max_prims:
                self.truncated = True
                self.notes.append(f"Stopped after max_prims={self.max_prims} prims; raise max_prims to see the rest.")
                break
            if self._out_of_time():
                self.notes.append("Ran out of time; the tour covers only the prims visited so far.")
                break
            self.visited += 1
            level = prim.GetPath().pathElementCount - base_depth + (0 if self.root.IsPseudoRoot() else 1)
            if self._visit(prim, level):
                iterator.PruneChildren()

    def _visit(self, prim: Any, level: int) -> bool:
        """Record one prim; True means skip its children."""
        path = str(prim.GetPath())
        name = prim.GetName()
        type_name = str(prim.GetTypeName())
        helper = self._editor_helper(prim)
        if helper is not None:
            self.terms["editor helper"] = None
            self.editor_items.append(helper)
            return True
        if not prim.IsActive():
            self.terms["inactive"] = None
            self.special_items.append(_item(path, "inactive", f"{name} is switched off (inactive), so USD ignores it and everything inside it."))
            return True
        if prim.IsAbstract():
            self.terms["class"] = None
            self.special_items.append(_item(path, "class", f"{name} is a class: a template that is never drawn; other prims can inherit from it."))
            return True
        if not prim.IsDefined():
            self.terms["over"] = None
            self.special_items.append(
                _item(path, "over", f"{name} is only an 'over': it stores tweaks for a prim that is not defined anywhere in this stage, so nothing is drawn.")
            )
            return True
        self._composition_of(prim)
        self._material_binding_of(prim, path)
        self._physics_of(prim, path, type_name)
        if type_name in GEOMETRY_TYPES or (prim.IsA(UsdGeom.Gprim) and type_name not in GROUP_TYPES):
            self._record_geometry(prim, path, name, type_name)
        elif _is_light(prim, type_name):
            self._record_light(prim, path, name, type_name)
        elif type_name == "Camera":
            self._record_camera(prim, path, name)
        elif type_name == "Material":
            self._record_material(prim, path, name)
            return True  # shaders inside are described with the material
        elif type_name in GROUP_TYPES or prim.IsInstance():
            self._record_group(prim, path, name, type_name, level)
        return prim.IsInstance()

    # ------------------------------------------------------------------ records

    def _editor_helper(self, prim: Any) -> dict[str, Any] | None:
        path = prim.GetPath()
        name = prim.GetName()
        text = str(path)
        if name in EDITOR_CAMERAS:
            stored = ""
            if not prim.IsDefined():
                self.terms["over"] = None
                stored = " This file only saves tweaks to it (an 'over')."
            first = not any(i["path"].startswith("/OmniverseKit") for i in self.editor_items)
            return _item(
                text, "editor_helper",
                f"{name} is {EDITOR_CAMERAS[name]}: Kit makes it so you can look around. It is an editor helper, "
                f"not your content.{stored}",
                tip="Use your own Camera prims for shots you want to keep; Kit remakes these every time it opens the stage."
                if first else None,
            )
        if text == "/Render" or text.startswith("/Render/"):
            return _item(
                text, "editor_helper",
                f"{name} holds Kit's render settings and viewport render products: editor helpers, not your content.",
            )
        if name.startswith(("OmniverseKit", "OmniKit")):
            return _item(text, "editor_helper", f"{name} is made by Kit for its own use: an editor helper, not your content.")
        hidden = False
        try:
            hidden = bool(prim.GetCustomDataByKey("omni:kit:hide_in_stage_window"))
        except Exception:
            hidden = False
        if hidden:
            return _item(text, "editor_helper", f"{name} is hidden from Kit's Stage window: an editor helper, not your content.")
        if self.session is not None:
            try:
                stack = prim.GetPrimStack()
            except Exception:
                stack = []
            if stack and all(spec.layer == self.session for spec in stack):
                self.terms["session layer"] = None
                return _item(
                    text, "editor_helper",
                    f"{name} exists only in the session layer (the editor's unsaved scratch layer), so it is not part of your file.",
                )
        return None

    def _world(self, prim: Any) -> Any:
        return self.xcache.GetLocalToWorldTransform(prim)

    def _local(self, prim: Any) -> Any:
        value = self.xcache.GetLocalTransformation(prim)
        return value[0] if isinstance(value, tuple) else value

    def _range_m(self, prim: Any) -> tuple[list[float], list[float]] | None:
        try:
            box = self.bcache.ComputeWorldBound(prim).ComputeAlignedRange()
        except Exception:
            return None
        if box.IsEmpty():
            return None
        lo, hi = box.GetMin(), box.GetMax()
        return [float(lo[i]) * self.mpu for i in range(3)], [float(hi[i]) * self.mpu for i in range(3)]

    def _visible(self, prim: Any) -> bool:
        try:
            return UsdGeom.Imageable(prim).ComputeVisibility(self.time_code) != UsdGeom.Tokens.invisible
        except Exception:
            return True

    def _hidden_by(self, prim: Any) -> str | None:
        """Name of the prim whose visibility hides this one (itself or an ancestor)."""
        current = prim
        while current and not current.IsPseudoRoot():
            attr = current.GetAttribute("visibility")
            if attr and attr.Get(self.time_code) == UsdGeom.Tokens.invisible:
                return str(current.GetPath())
            current = current.GetParent()
        return None

    def _record_geometry(self, prim: Any, path: str, name: str, type_name: str) -> None:
        bounds = self._range_m(prim)
        purpose = str(UsdGeom.Imageable(prim).ComputePurpose()) if prim.IsA(UsdGeom.Imageable) else "default"
        world = self._world(prim)
        local = self._local(prim)
        record = {
            "path": path, "name": name, "type": type_name, "prim": prim, "bounds": bounds, "purpose": purpose,
            "visible": self._visible(prim),
            "world_pos": [float(v) * self.mpu for v in world.ExtractTranslation()],
            "local_pos": [float(v) * self.mpu for v in local.ExtractTranslation()],
            "parent": str(prim.GetPath().GetParentPath()),
            "parent_moved": not _is_identity(self._world(prim.GetParent())) if prim.GetParent() else False,
            "color": _display_color(prim),
        }
        self.terms[_base_type(type_name)] = None
        self.geometry.append(record)

    def _record_light(self, prim: Any, path: str, name: str, type_name: str) -> None:
        world = self._world(prim)
        forward = _unit([float(v) for v in world.TransformDir(Gf.Vec3d(0, 0, -1))])
        intensity_attr = prim.GetAttribute("inputs:intensity")
        intensity = _get(intensity_attr)
        exposure = _get(prim.GetAttribute("inputs:exposure"))
        record = {
            "path": path, "name": name, "type": type_name, "prim": prim,
            "pos": [float(v) * self.mpu for v in world.ExtractTranslation()],
            "forward": forward, "intensity": intensity, "exposure": exposure,
            "visible": self._visible(prim), "world": world,
        }
        if intensity_attr:
            record["override"] = _hidden_opinion(intensity_attr, self.time_code)
        self.terms["light"] = None
        self.terms["intensity"] = None
        if _base_type(type_name) in GLOSSARY:
            self.terms[_base_type(type_name)] = None
        self.lights.append(record)

    def _record_camera(self, prim: Any, path: str, name: str) -> None:
        world = self._world(prim)
        record = {
            "path": path, "name": name, "prim": prim,
            "pos": [float(v) * self.mpu for v in world.ExtractTranslation()],
            "forward": _unit([float(v) for v in world.TransformDir(Gf.Vec3d(0, 0, -1))]),
            "focal": _get(prim.GetAttribute("focalLength")),
            "aperture": _get(prim.GetAttribute("horizontalAperture")),
            "projection": str(_get(prim.GetAttribute("projection")) or "perspective"),
        }
        self.terms["camera"] = None
        self.cameras.append(record)

    def _record_material(self, prim: Any, path: str, name: str) -> None:
        shaders: list[str] = []
        color = None
        for child in Usd.PrimRange(prim):
            if child == prim or not child.IsA(UsdShade.Shader):
                continue
            shader = UsdShade.Shader(child)
            ident = None
            try:
                ident = shader.GetShaderId()
            except Exception:
                ident = None
            if not ident:
                asset = _get(child.GetAttribute("info:mdl:sourceAsset"))
                sub = _get(child.GetAttribute("info:mdl:sourceAsset:subIdentifier"))
                if sub:
                    ident = str(sub)
                elif asset is not None:
                    ident = str(getattr(asset, "path", asset)).rsplit("/", 1)[-1] or None
            if ident:
                shaders.append(str(ident))
            for input_name in ("inputs:diffuseColor", "inputs:diffuse_color_constant", "inputs:base_color"):
                value = _get(child.GetAttribute(input_name))
                if value is not None and color is None:
                    color = _color_word([float(c) for c in value])
        self.terms["material"] = None
        if shaders:
            self.terms["shader"] = None
        physics = "PhysicsMaterialAPI" in _schemas(prim)
        self.materials.append({"path": path, "name": name, "shaders": shaders, "color": color, "physics": physics})

    def _record_group(self, prim: Any, path: str, name: str, type_name: str, level: int) -> None:
        local = self._local(prim)
        scale = [Gf.Vec3d(local[i][0], local[i][1], local[i][2]).GetLength() for i in range(3)]
        record = {
            "path": path, "name": name, "type": type_name or "(no type)", "prim": prim, "level": level,
            "children": [child for child in prim.GetChildren()],
            "scale": scale, "moved": not _is_identity(local), "visible_attr": _get(prim.GetAttribute("visibility")),
            "instance": prim.IsInstance(),
            "bounds": self._range_m(prim) if prim.IsInstance() else None,
        }
        if type_name in ("Xform", "Scope"):
            self.terms[type_name] = None
        if any(abs(s - 1.0) > 1e-4 for s in scale):
            self.scaled_groups.append(record)
        if record["visible_attr"] == UsdGeom.Tokens.invisible:
            self.hidden_paths.append(path)
        self.groups.append(record)

    def _material_binding_of(self, prim: Any, path: str) -> None:
        parent = str(prim.GetPath().GetParentPath())
        inherited = self.effective_material.get(parent)
        own = None
        for rel_name in ("material:binding", "material:binding:full", "material:binding:preview"):
            rel = prim.GetRelationship(rel_name)
            if rel:
                targets = rel.GetTargets()
                if targets:
                    own = str(targets[0])
                    break
        if own:
            self.bindings.setdefault(own, []).append(path)
            self.terms["material binding"] = None
        if own or inherited:
            self.effective_material[path] = own or inherited  # type: ignore[assignment]

    def _physics_of(self, prim: Any, path: str, type_name: str) -> None:
        schemas = _schemas(prim)
        name = prim.GetName()
        if type_name == "PhysicsScene":
            self.terms["physics scene"] = None
            self.physics.append({"kind": "physics_scene", "path": path, "name": name, "prim": prim})
        elif type_name.startswith("Physics") and type_name.endswith("Joint"):
            body0 = _targets(prim, "physics:body0")
            body1 = _targets(prim, "physics:body1")
            self.terms["joint"] = None
            self.physics.append({"kind": "joint", "path": path, "name": name, "type": type_name, "bodies": body0 + body1})
        if "PhysicsArticulationRootAPI" in schemas:
            self.terms["articulation"] = None
            self.physics.append({"kind": "articulation", "path": path, "name": name})
        if "PhysicsRigidBodyAPI" in schemas:
            self.rigid_bodies.add(path)
            self.terms["rigid body"] = None
            self.terms["applied API schema"] = None
            self.physics.append({
                "kind": "rigid_body", "path": path, "name": name,
                "enabled": _get(prim.GetAttribute("physics:rigidBodyEnabled")) is not False,
                "kinematic": bool(_get(prim.GetAttribute("physics:kinematicEnabled"))),
                "mass": _get(prim.GetAttribute("physics:mass")) if "PhysicsMassAPI" in schemas else None,
            })
        if "PhysicsCollisionAPI" in schemas:
            self.colliders.add(path)
            self.terms["collider"] = None
            self.terms["applied API schema"] = None
            self.physics.append({
                "kind": "collider", "path": path, "name": name,
                "enabled": _get(prim.GetAttribute("physics:collisionEnabled")) is not False,
            })

    def _composition_of(self, prim: Any) -> None:
        path = str(prim.GetPath())
        name = prim.GetName()
        try:
            stack = prim.GetPrimStack() if (
                prim.HasAuthoredReferences() or prim.HasAuthoredPayloads() or prim.HasAuthoredInherits()
            ) else []
        except Exception:
            stack = []
        refs: list[Any] = []
        payloads: list[Any] = []
        inherits: list[Any] = []
        for spec in stack:
            refs.extend(_list_items(spec.referenceList))
            payloads.extend(_list_items(spec.payloadList))
            inherits.extend(_list_items(spec.inheritPathList))
        for ref in _dedupe(refs, lambda r: (r.assetPath, str(r.primPath))):
            self.terms["reference"] = None
            if ref.assetPath:
                where = _file_name(ref.assetPath) + (f" (its prim {ref.primPath})" if not ref.primPath.isEmpty else "")
                plain = (
                    f"{name} brings in {where} by reference: its contents live in that file, and anything authored "
                    f"here on top is a local override that wins."
                )
            else:
                plain = f"{name} is an internal reference to {ref.primPath} in this same stage: it copies that prim's contents."
            self.compose_items.append(_item(path, "reference", plain, facts={"asset": ref.assetPath or None, "prim_path": str(ref.primPath) or None}))
        for payload in _dedupe(payloads, lambda p: (p.assetPath, str(p.primPath))):
            self.terms["payload"] = None
            state = "loaded" if prim.IsLoaded() else "unloaded right now, so its contents are not in the scene"
            target = _file_name(payload.assetPath) if payload.assetPath else str(payload.primPath)
            self.compose_items.append(_item(
                path, "payload",
                f"{name} brings in {target} as a payload: like a reference that can be unloaded to save memory. It is {state}.",
                facts={"asset": payload.assetPath or None, "loaded": bool(prim.IsLoaded())},
            ))
        for inherit in _dedupe(inherits, str):
            self.terms["inherit"] = None
            self.compose_items.append(_item(
                path, "inherit", f"{name} inherits from the class {inherit}: changes to that class flow into {name}.",
            ))
        if prim.HasVariantSets():
            variant_sets = prim.GetVariantSets()
            for set_name in variant_sets.GetNames():
                choices = list(variant_sets.GetVariantSet(set_name).GetVariantNames())
                selected = variant_sets.GetVariantSelection(set_name)
                chosen = f"'{selected}' is selected" if selected else "nothing is selected yet"
                self.terms["variant set"] = None
                self.compose_items.append(_item(
                    path, "variant_set",
                    f"{name} has a variant set '{set_name}' with choices {_join([str(c) for c in choices]) or 'none'}; {chosen}.",
                    tip="Switching the selection swaps in that choice's opinions without copying the prim.",
                    facts={"variant_set": set_name, "choices": choices, "selection": selected or None},
                ))

    # ------------------------------------------------------------------ scene geometry

    def _content_geometry(self) -> list[dict[str, Any]]:
        """Visible, measurable, non-guide shapes: what placement and aiming compare against (computed once)."""
        if self._content is None:
            self._content = [g for g in self.geometry if g["bounds"] is not None and g["visible"] and g["purpose"] != "guide"]
        return self._content

    def _measure_scene(self) -> None:
        self._content = None
        boxes = [g["bounds"] for g in self._content_geometry()]
        self.scene = _union(boxes)

    def _find_floor_and_walls(self) -> None:
        content = self._content_geometry()
        if not content or self.scene is None:
            return
        size = _size(self.scene)
        span = max(size[self.X], size[self.B], 1e-9)
        best = None
        best_key = None
        for rec in content:
            s = _size(rec["bounds"])
            horizontal = max(s[self.X], s[self.B])
            flat = s[self.H] <= max(0.02, 0.05 * horizontal)
            named = bool(_FLOOR_NAME.search(rec["name"]) or _FLOOR_NAME.search(rec["parent"].rsplit("/", 1)[-1]))
            if not flat or not (named or horizontal >= 0.3 * span):
                continue
            key = (s[self.X] * s[self.B], named)
            if best_key is None or key > best_key:
                best, best_key = rec, key
        self.floor = best
        height = max(size[self.H], 1e-9)
        for rec in content:
            if rec is self.floor:
                continue
            s = _size(rec["bounds"])
            long_side = max(s[self.X], s[self.B])
            thin_side = min(s[self.X], s[self.B])
            if s[self.H] >= max(0.5, 0.3 * height) and thin_side <= 0.25 * long_side and long_side >= 0.3 * span:
                thin_axis = self.X if s[self.X] < s[self.B] else self.B
                rec["wall_axis"] = thin_axis
                rec["wall_side"] = self._wall_side(rec, thin_axis)
                self.walls.append(rec)

    def _wall_side(self, rec: dict[str, Any], axis: int) -> str:
        match = _WALL_SIDE_NAME.search(rec["name"])
        if match:
            return match.group(0).lower()
        others = [g["bounds"] for g in self._content_geometry() if g is not rec and g is not self.floor]
        center = _center(_union(others) or self.scene)
        offset = _center(rec["bounds"])[axis] - center[axis]
        if axis == self.X:
            return "right" if offset >= 0 else "left"
        return "back" if offset * self.back_sign >= 0 else "front"

    # ------------------------------------------------------------------ wording helpers

    def _m(self, value: float) -> str:
        return _fmt(value)

    def _where(self, point: list[float]) -> str:
        a, b = self.coord_names
        return f"{a}={_fmt(point[self.X])}, {b}={_fmt(point[self.B])}"

    def _height_words(self, height: float) -> str:
        if self.floor is not None:
            above = height - self.floor["bounds"][1][self.H]
            if abs(above) < 0.01:
                return "at floor level"
            return f"{_fmt(abs(above))} m {'above' if above > 0 else 'below'} the floor"
        return f"at height {self.up.lower()}={_fmt(height)}"

    def _size_words(self, rec: dict[str, Any]) -> str:
        s = _size(rec["bounds"])
        w, d, h = s[self.X], s[self.B], s[self.H]
        if max(w, d, h) > 0 and max(w, d, h) - min(w, d, h) <= 0.01 * max(w, d, h):
            return f"{_fmt(w)} m across" if rec["type"] == "Sphere" else f"{_fmt(w)} m on each side"
        if h <= max(0.005, 0.01 * max(w, d)):
            return f"{_fmt(w)} m x {_fmt(d)} m and flat"
        return f"{_fmt(w)} m wide, {_fmt(d)} m deep and {_fmt(h)} m tall"

    def _direction_words(self, d: list[float]) -> str:
        v = d[self.H]
        if v <= -0.97:
            return "straight down"
        if v >= 0.97:
            return "straight up"
        if v <= -0.6:
            vertical = "steeply down"
        elif v <= -0.2:
            vertical = "down"
        elif v < -0.05:
            vertical = "slightly down"
        elif v <= 0.05:
            vertical = "level"
        elif v < 0.2:
            vertical = "slightly up"
        elif v < 0.6:
            vertical = "up"
        else:
            vertical = "steeply up"
        horizontal = self._side_words(d[self.X], d[self.B] * self.back_sign, toward=True)
        return f"{vertical} and {horizontal}" if vertical != "level" else f"level, {horizontal}"

    def _side_words(self, right: float, back: float, toward: bool) -> str:
        length = math.hypot(right, back)
        if length < 1e-9:
            return "toward the middle" if toward else "in the middle"
        right, back = right / length, back / length
        words = []
        if abs(back) >= 0.38:
            words.append("back" if back > 0 else "front")
        if abs(right) >= 0.38:
            words.append("right" if right > 0 else "left")
        side = "-".join(words)
        return f"toward the {side}" if toward else side

    def _ray_target(self, origin: list[float], direction: list[float]) -> tuple[dict[str, Any], float] | None:
        best = None
        for rec in self._content_geometry():
            t = _ray_box(origin, direction, rec["bounds"])
            if t is not None and (best is None or t < best[1]):
                best = (rec, t)
        return best

    def _target_name(self, rec: dict[str, Any]) -> str:
        parent = rec["parent"]
        parent_name = parent.rsplit("/", 1)[-1]
        if rec is self.floor:
            return f"the floor ({rec['name']})"
        if parent.count("/") >= 2 and parent_name:
            return f"{rec['name']} in the {parent_name} group"
        return rec["name"]

    # ------------------------------------------------------------------ sections

    def _group_items(self) -> list[dict[str, Any]]:
        items = []
        hidden_deep = 0
        for rec in self.groups:
            if rec["level"] > self.depth:
                hidden_deep += 1
                continue
            items.append(self._group_item(rec))
        if hidden_deep:
            self.notes.append(f"{hidden_deep} deeper group(s) are not listed in Groups; raise depth (now {self.depth}) to see them.")
        return items

    def _group_item(self, rec: dict[str, Any]) -> dict[str, Any]:
        path, name = rec["path"], rec["name"]
        type_name = rec["type"]
        default = self.stage.GetDefaultPrim()
        is_default = bool(default) and str(default.GetPath()) == path
        if type_name == "Scope":
            what = "a folder (Scope)"
        elif type_name == "Xform":
            what = "a group (Xform)"
        elif rec["instance"]:
            what = f"an instance ({type_name})"
            self.terms["instance"] = None
        else:
            what = "a plain prim with no type, used as a folder"
        parent = str(Sdf.Path(path).GetParentPath())
        place = "at the top of the scene" if parent == "/" else f"inside {parent.rsplit('/', 1)[-1]}"
        if is_default:
            self.terms["default prim"] = None
            place += " (it is the default prim)"
        children = rec["children"]
        sentence = f"{name} is {what} {place}"
        if children:
            sentence += f", holding {self._children_words(children)}"
        sentence += "."
        bounds = rec["bounds"] or _union([g["bounds"] for g in self._content_geometry() if g["path"].startswith(path + "/")])
        if bounds is not None:
            self.terms["bounding box"] = None
            s = _size(bounds)
            sentence += f" Together its contents span about {_fmt(s[self.X])} m x {_fmt(s[self.B])} m x {_fmt(s[self.H])} m (wide x deep x tall)."
        tip = None
        scale = rec["scale"]
        if any(abs(v - 1.0) > 1e-4 for v in scale):
            if max(scale) - min(scale) < 1e-4:
                sentence += f" It scales everything inside by {_fmt(scale[0])}x."
            else:
                sentence += f" It stretches everything inside by {_fmt(scale[0])} x {_fmt(scale[1])} x {_fmt(scale[2])}."
            tip = "Scale on a group multiplies the size and the positions of everything inside it."
            self.terms["local space"] = None
            self.terms["world space"] = None
        if rec["visible_attr"] == UsdGeom.Tokens.invisible:
            self.terms["visibility"] = None
            sentence += " It is set to invisible, so nothing inside it is drawn."
        if rec["instance"]:
            tip = "Instances share their insides; to change one, edit the shared original."
        self.terms["prim"] = None
        return _item(path, "group", sentence, tip=tip, facts={"type": type_name, "children": len(children)})

    def _children_words(self, children: list[Any]) -> str:
        names = [child.GetName() for child in children]
        if len(names) <= MAX_NAMES_LISTED:
            return _join(names)
        counts: dict[str, int] = {}
        for child in children:
            word = str(child.GetTypeName()) or "untyped prim"
            counts[word] = counts.get(word, 0) + 1
        parts = [f"{n} {word}{'s' if n != 1 and not word.endswith('s') else ''}" for word, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        return f"{len(names)} items ({_join(parts)}), such as {_join(names[:3])}"

    def _geometry_items(self) -> list[dict[str, Any]]:
        items = []
        explained_parents: set[str] = set()
        for rec in self.geometry:
            if not self.time_stopped and self._out_of_time():
                self.notes.append("Ran out of time while describing shapes; the rest skip placement words.")
            items.append(self._geometry_item(rec, explained_parents))
        return items

    def _geometry_item(self, rec: dict[str, Any], explained_parents: set[str]) -> dict[str, Any]:
        name, type_name = rec["name"], rec["type"]
        base = _base_type(type_name)
        article = _article(base).capitalize()
        facts: dict[str, Any] = {"type": type_name, "local_position_m": _round_list(rec["local_pos"]), "world_position_m": _round_list(rec["world_pos"])}
        tip = None
        if rec["bounds"] is None:
            sentence = f"{article} {base} named {name}, with no measurable size (it may be empty or have no extent)."
        else:
            lo, hi = rec["bounds"]
            center = _center(rec["bounds"])
            facts.update({"size_m": _round_list(_size(rec["bounds"])), "center_m": _round_list(center), "bottom_m": round(lo[self.H], 3)})
            self.terms["bounding box"] = None
            if rec is self.floor:
                sentence = f"{article} {base} named {name}, {self._size_words(rec)}: this looks like the floor, with its top at {self.up.lower()}={_fmt(hi[self.H])}."
            elif rec in self.walls:
                sentence = f"{article} {base} named {name}, {self._size_words(rec)}: a wall along the {rec['wall_side']} of the scene at {self._where(center)}."
            else:
                placement = f"{self._placement(rec)} " if not self.time_stopped else ""
                sentence = f"{article} {base} named {name}, {self._size_words(rec)}, {placement}at {self._where(center)}."
        if rec["purpose"] == "guide":
            self.terms["purpose"] = None
            sentence += " Its purpose is 'guide', so it only shows as an editor helper and is not rendered."
        elif not rec["visible"]:
            self.terms["visibility"] = None
            sentence += " It is hidden (invisible)."
        material = self.effective_material.get(rec["path"])
        if material:
            sentence += f" It uses the material {material.rsplit('/', 1)[-1]}."
        elif rec["color"]:
            sentence += f" Its display colour is {rec['color']}."
        moved = max(abs(a - b) for a, b in zip(rec["local_pos"], rec["world_pos"])) > 1e-4
        if rec["parent_moved"] and moved and rec["parent"] not in explained_parents and rec["bounds"] is not None:
            explained_parents.add(rec["parent"])
            self.terms["local space"] = None
            self.terms["world space"] = None
            parent_name = rec["parent"].rsplit("/", 1)[-1]
            tip = (
                f"Its own (local) position is ({_fmt_list(rec['local_pos'])}) m, but its parent {parent_name} moves or "
                f"scales it, so in world space it sits at ({_fmt_list(rec['world_pos'])}) m."
            )
        return _item(rec["path"], base.lower(), sentence, tip=tip, facts=facts)

    def _placement(self, rec: dict[str, Any]) -> str:
        lo, hi = rec["bounds"]
        h = self.H
        own_height = hi[h] - lo[h]
        tol = max(0.02, 0.03 * own_height)
        support = None
        for other in self._content_geometry():
            if other is rec or other is self.floor or other in self.walls:
                continue
            olo, ohi = other["bounds"]
            if abs(ohi[h] - lo[h]) > tol or ohi[h] >= hi[h]:
                continue
            area = _overlap(lo[self.X], hi[self.X], olo[self.X], ohi[self.X]) * _overlap(lo[self.B], hi[self.B], olo[self.B], ohi[self.B])
            footprint = max((hi[self.X] - lo[self.X]) * (hi[self.B] - lo[self.B]), 1e-12)
            if area >= 0.25 * footprint and (support is None or ohi[h] > support["bounds"][1][h]):
                support = other
        if support is not None:
            words = f"resting on top of {support['name']}"
        elif self.floor is not None:
            top = self.floor["bounds"][1][h]
            if abs(lo[h] - top) <= tol:
                words = "sitting on the floor"
            elif lo[h] > top:
                words = f"raised {_fmt(lo[h] - top)} m above the floor"
            elif hi[h] > top:
                words = "partly sunk into the floor"
            else:
                words = "below the floor"
        else:
            words = f"with its bottom at {self.up.lower()}={_fmt(lo[h])}"
        wall = self._near_wall(rec)
        if wall is not None:
            words += f" against the {wall['wall_side']} wall ({wall['name']})"
        return words

    def _near_wall(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        lo, hi = rec["bounds"]
        span = max(_size(self.scene)[self.X], _size(self.scene)[self.B]) if self.scene else 0.0
        limit = max(0.3, 0.03 * span)
        best = None
        for wall in self.walls:
            a = wall["wall_axis"]
            o = self.B if a == self.X else self.X
            wlo, whi = wall["bounds"]
            gap = max(wlo[a] - hi[a], lo[a] - whi[a], 0.0)
            if gap <= limit and _overlap(lo[o], hi[o], wlo[o], whi[o]) > 0 and not (lo[a] < wlo[a] and hi[a] > whi[a]):
                if best is None or gap < best[1]:
                    best = (wall, gap)
        return best[0] if best else None

    def _light_items(self) -> list[dict[str, Any]]:
        items = []
        for rec in self.lights:
            name, type_name = rec["name"], rec["type"]
            words = LIGHT_WORDS.get(type_name, "a light")
            sentence = f"{name} is {words}."
            facts: dict[str, Any] = {"type": type_name, "position_m": _round_list(rec["pos"])}
            if rec["intensity"] is not None:
                intensity = float(rec["intensity"])
                exposure = float(rec["exposure"] or 0.0)
                facts["intensity"] = _num(intensity)
                facts["exposure"] = _num(exposure)
                if exposure:
                    facts["effective_intensity"] = _num(intensity * 2 ** exposure)
                    sentence += f" Intensity {_num(intensity)} with exposure {_num(exposure)} (about {_num(intensity * 2 ** exposure)} in total)."
                else:
                    sentence += f" Intensity {_num(intensity)}."
            if type_name in ("RectLight",):
                width = _get(rec["prim"].GetAttribute("inputs:width"))
                height = _get(rec["prim"].GetAttribute("inputs:height"))
                if width is not None and height is not None:
                    sx = Gf.Vec3d(rec["world"][0][0], rec["world"][0][1], rec["world"][0][2]).GetLength()
                    sy = Gf.Vec3d(rec["world"][1][0], rec["world"][1][1], rec["world"][1][2]).GetLength()
                    sentence += f" The panel is {_fmt(float(width) * sx * self.mpu)} m x {_fmt(float(height) * sy * self.mpu)} m."
            if type_name not in ("DistantLight", "DomeLight", "DomeLight_1"):
                sentence += f" It sits at {self._where(rec['pos'])}, {self._height_words(rec['pos'][self.H])}."
            if type_name in DIRECTIONAL_LIGHTS:
                direction = self._direction_words(rec["forward"])
                facts["direction"] = _round_list(rec["forward"])
                if type_name == "DistantLight":
                    sentence += f" Its light travels {direction}, the same everywhere in the scene."
                else:
                    hit = self._ray_target(rec["pos"], rec["forward"])
                    if hit is not None:
                        facts["aimed_at"] = hit[0]["path"]
                        sentence += f" It points {direction} at {self._target_name(hit[0])}, about {_fmt(hit[1])} m away."
                    else:
                        sentence += f" It points {direction}, at nothing in particular."
                cone = _get(rec["prim"].GetAttribute("inputs:shaping:cone:angle"))
                if cone is not None and float(cone) < 90:
                    sentence += f" Its shaping cone narrows it into a {_num(2 * float(cone))} degree spotlight."
            elif type_name in ("DomeLight", "DomeLight_1"):
                texture = _get(rec["prim"].GetAttribute("inputs:texture:file"))
                path_text = str(getattr(texture, "path", texture) or "") if texture is not None else ""
                if path_text:
                    sentence += f" It uses the image {_file_name(path_text)} as the sky."
            tip = None
            if not rec["visible"]:
                self.terms["visibility"] = None
                hider = self._hidden_by(rec["prim"])
                if hider and hider != rec["path"]:
                    sentence += f" It is hidden because its parent {hider.rsplit('/', 1)[-1]} is invisible, so it does not light the scene."
                else:
                    sentence += " It is set to invisible, so it does not light the scene."
                self.hidden_paths.append(rec["path"])
            override = rec.get("override")
            if override:
                self.terms["opinion"] = None
                self.terms["layer"] = None
                tip = (
                    f"Its intensity {override['winning']} comes from {override['winning_layer']}; "
                    f"{override['hidden_layer']} also says {override['hidden']}, but that weaker opinion is hidden."
                )
                self.overridden_lights.append({"name": name, "path": rec["path"], **override})
            items.append(_item(rec["path"], "light", sentence, tip=tip, facts=facts))
        return items

    def _camera_items(self) -> list[dict[str, Any]]:
        items = []
        for rec in self.cameras:
            name = rec["name"]
            facts: dict[str, Any] = {"position_m": _round_list(rec["pos"]), "looks_along": _round_list(rec["forward"])}
            sentence = f"A camera named {name} at {self._where(rec['pos'])}, {self._height_words(rec['pos'][self.H])}"
            side = self._outside_words(rec["pos"])
            if side:
                sentence += f", {side}"
            direction = self._direction_words(rec["forward"])
            hit = self._ray_target(rec["pos"], rec["forward"])
            if hit is not None:
                facts["looking_at"] = hit[0]["path"]
                sentence += f". It looks {direction} at {self._target_name(hit[0])}, about {_fmt(hit[1])} m away."
            else:
                sentence += f". It looks {direction}."
            if rec["projection"] == "orthographic":
                sentence += " It is orthographic: no perspective, like a blueprint."
            elif rec["focal"] and rec["aperture"]:
                focal, aperture = float(rec["focal"]), float(rec["aperture"])
                fov = math.degrees(2 * math.atan(aperture / (2 * focal))) if focal > 0 else 0.0
                lens = "wide-angle" if fov > 60 else "zoomed-in" if fov < 25 else "normal"
                facts.update({"focal_length_mm": _num(focal), "horizontal_fov_deg": round(fov, 1)})
                self.terms["focal length"] = None
                sentence += f" Its {_num(focal)} mm lens gives a {lens} view about {_num(round(fov))} degrees across."
            tip = None
            if self.bound_camera and self.bound_camera == rec["path"]:
                sentence += " This is the camera the viewport was last looking through."
                tip = "In Composer or Isaac Sim, choose another camera from the viewport's camera menu to compare views."
            items.append(_item(rec["path"], "camera", sentence, tip=tip, facts=facts))
        return items

    def _outside_words(self, point: list[float]) -> str:
        # The floor is often much bigger than the content on it, so the "scene area" here leaves it out.
        area = _union([g["bounds"] for g in self._content_geometry() if g is not self.floor]) or self.scene
        if area is None:
            return ""
        lo, hi = area
        right = 0.0 if lo[self.X] <= point[self.X] <= hi[self.X] else (point[self.X] - (hi[self.X] if point[self.X] > hi[self.X] else lo[self.X]))
        depth = 0.0 if lo[self.B] <= point[self.B] <= hi[self.B] else (point[self.B] - (hi[self.B] if point[self.B] > hi[self.B] else lo[self.B]))
        if right == 0.0 and depth == 0.0:
            return "inside the scene area"
        side = self._side_words(right, depth * self.back_sign, toward=False)
        names = {"front": "in front of", "back": "behind", "left": "to the left of", "right": "to the right of"}
        return f"{names.get(side, 'off to the ' + side + ' of')} the scene"

    def _material_items(self) -> list[dict[str, Any]]:
        items = []
        for rec in self.materials:
            name = rec["name"]
            shader = f" using {_join(rec['shaders'])}" if rec["shaders"] else ""
            sentence = f"A material named {name}{shader}"
            if rec["color"]:
                sentence += f", with {_article(rec['color'])} {rec['color']} base colour"
            targets = self.bindings.get(rec["path"], [])
            if targets:
                shown = [t.rsplit("/", 1)[-1] for t in targets[:MAX_NAMES_LISTED]]
                more = f" and {len(targets) - MAX_NAMES_LISTED} more" if len(targets) > MAX_NAMES_LISTED else ""
                inside = "it" if len(targets) == 1 else "those prims"
                sentence += f". It is bound to {_join(shown)}{more}, and everything inside {inside} uses it too unless it binds its own."
            else:
                sentence += ". Nothing in this tour is bound to it yet, so it does not change how anything looks."
            if rec["physics"]:
                sentence += " It also carries physics properties (friction, bounciness)."
            items.append(_item(rec["path"], "material", sentence, facts={"bound_to": targets[:MAX_NAMES_LISTED * 5], "shaders": rec["shaders"]}))
        missing = sorted(set(self.bindings) - {m["path"] for m in self.materials})
        for target in missing:
            prim = self.stage.GetPrimAtPath(target)
            if prim and prim.IsValid():
                continue  # it exists, just outside this tour (focus_path or a truncated walk)
            users = self.bindings[target]
            items.append(_item(
                target, "missing_material",
                f"{_join([u.rsplit('/', 1)[-1] for u in users[:MAX_NAMES_LISTED]])} is bound to {target}, but no material exists there, so it shows the default look.",
                tip="Check the binding path for typos, or create the material at that path.",
            ))
        return items

    def _physics_items(self) -> list[dict[str, Any]]:
        items = []
        has_scene = any(p["kind"] == "physics_scene" for p in self.physics)
        for rec in self.physics:
            kind, name, path = rec["kind"], rec["name"], rec["path"]
            if kind == "physics_scene":
                prim = rec["prim"]
                magnitude = _get(prim.GetAttribute("physics:gravityMagnitude"))
                direction = _get(prim.GetAttribute("physics:gravityDirection"))
                if magnitude is None or not math.isfinite(float(magnitude)) or float(magnitude) < 0:
                    strength = "normal Earth gravity (9.81 m/s^2)"
                else:
                    strength = f"gravity of {_fmt(float(magnitude) * self.mpu)} m/s^2"
                if direction is None or Gf.Vec3f(direction).GetLength() < 1e-9:
                    way = "pointing straight down"
                else:
                    way = f"pointing {self._direction_words(_unit([float(v) for v in direction]))}"
                plain = f"{name} is the physics scene: it switches on simulation with {strength}, {way}."
                items.append(_item(path, "physics_scene", plain))
            elif kind == "rigid_body":
                if not rec["enabled"]:
                    plain = f"{name} has a rigid body that is switched off, so physics leaves it alone."
                elif rec["kinematic"]:
                    self.terms["kinematic"] = None
                    plain = f"{name} is a kinematic rigid body: it moves only when animated or scripted, and pushes other bodies."
                else:
                    plain = f"{name} is a rigid body: when you press Play, gravity pulls it and collisions can push it around."
                collides = any(c == path or c.startswith(path + "/") for c in self.colliders)
                tip = None
                if collides:
                    plain += " It has a collider, so it can hit other things."
                else:
                    tip = "It has no collider on it or inside it, so it would fall straight through everything."
                if rec["mass"]:
                    plain += f" Its mass is set to {_num(float(rec['mass']))} kg."
                items.append(_item(path, "rigid_body", plain, tip=tip))
            elif kind == "collider":
                body = next((rb for rb in self.rigid_bodies if path == rb or path.startswith(rb + "/")), None)
                if body == path:
                    continue  # already described with the rigid body
                if not rec["enabled"]:
                    plain = f"{name} has a collider that is switched off."
                elif body:
                    plain = f"{name} is part of the collision shape of the rigid body {body.rsplit('/', 1)[-1]}."
                else:
                    plain = f"{name} is a static collider: other objects bump into it, but it never moves."
                items.append(_item(path, "collider", plain))
            elif kind == "joint":
                bodies = [b.rsplit("/", 1)[-1] for b in rec["bodies"]]
                joined = f" connecting {_join(bodies)}" if bodies else ""
                items.append(_item(path, "joint", f"{name} is a {rec['type'].replace('Physics', '').replace('Joint', '').lower() or 'fixed'} joint{joined}."))
            elif kind == "articulation":
                items.append(_item(path, "articulation", f"{name} is the root of an articulation: its bodies and joints are simulated together, like a robot."))
        if self.rigid_bodies and not has_scene and not self.focus:
            items.append(_item(
                "/", "physics_note",
                "There are rigid bodies but no PhysicsScene prim. Isaac Sim usually creates a default one when you press Play, "
                "but adding your own makes gravity and settings explicit.",
            ))
        return items

    def _layer_items(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        root = self.stage.GetRootLayer()
        self.terms["layer"] = None
        self.terms["root layer"] = None
        items.append(_item(
            root.identifier, "root_layer",
            f"{_file_name(root.identifier)} is the root layer: the file that was opened. New edits are saved here by default.",
        ))
        seen: set[str] = {root.identifier}
        self._sublayers(root, items, seen, 0)
        if self.session is not None:
            self.terms["session layer"] = None
            items.append(_item(
                self.session.identifier, "session_layer",
                "The session layer holds editor-only, unsaved changes. It is the strongest layer, but it is never saved into your file.",
            ))
        if len(self.compose_items) > MAX_SECTION_ITEMS:
            self.notes.append(f"Only the first {MAX_SECTION_ITEMS} references/payloads/variant sets are listed.")
        items.extend(self.compose_items)
        errors = usd_insight._composition_errors(self.stage) if not self.time_stopped else []
        if errors:
            self.terms["composition"] = None
            items.append(_item(
                root.identifier, "composition_errors",
                f"USD reported {len(errors)} composition problem(s), for example: {errors[0]}",
                tip="A missing file usually means a wrong or absolute path; prefer paths relative to the layer.",
            ))
        return items

    def _sublayers(self, layer: Any, items: list[dict[str, Any]], seen: set[str], level: int) -> None:
        if level > 8:
            return
        for index, sub_path in enumerate(layer.subLayerPaths):
            sub = Sdf.Layer.FindRelativeToLayer(layer, sub_path)
            self.terms["sublayer"] = None
            self.terms["opinion"] = None
            owner = _file_name(layer.identifier)
            if sub is None:
                items.append(_item(
                    str(sub_path), "missing_sublayer",
                    f"{owner} lists the sublayer {_file_name(str(sub_path))}, but that file could not be found, so its contents are missing.",
                ))
                continue
            if sub.identifier in seen:
                continue
            seen.add(sub.identifier)
            order = "the strongest of its sublayers" if index == 0 else f"sublayer number {index + 1}, weaker than the ones listed before it"
            plain = (
                f"{_file_name(sub.identifier)} is a sublayer of {owner} ({order}): its prims and values merge into the scene, "
                f"but opinions in {owner} win when both set the same thing."
            )
            if self.stage.IsLayerMuted(sub.identifier):
                plain += " It is muted right now, so it is ignored."
            items.append(_item(sub.identifier, "sublayer", plain))
            self._sublayers(sub, items, seen, level + 1)

    # ------------------------------------------------------------------ overview, questions

    def _units_words(self) -> str:
        mpu = self.mpu
        if abs(mpu - 1.0) < 1e-9:
            unit = "1 unit = 1 metre"
        elif abs(mpu - 0.01) < 1e-9:
            unit = "1 unit = 1 centimetre (0.01 m), so a value of 100 means one metre"
        elif abs(mpu - 0.001) < 1e-9:
            unit = "1 unit = 1 millimetre (0.001 m)"
        elif abs(mpu - 0.0254) < 1e-9:
            unit = "1 unit = 1 inch (0.0254 m)"
        else:
            unit = f"1 unit = {_fmt(mpu)} m"
        return f"{self.up} is up and {unit}"

    def _direction_convention(self) -> str:
        if self.up == "Z":
            return "In this tour +Z is up, +Y is 'the back' and +X is 'the right'."
        return "In this tour +Y is up, -Z is 'the back' and +X is 'the right'."

    def _overview(self) -> str:
        root_layer = self.stage.GetRootLayer()
        root_name = "an unsaved in-memory stage" if root_layer.anonymous else _file_name(root_layer.identifier)
        self.terms["stage"] = None
        self.terms["upAxis"] = None
        self.terms["metersPerUnit"] = None
        sentences = []
        theme = self._theme()
        default = self.stage.GetDefaultPrim()
        if self.focus:
            first = f"This tour focuses on {self.focus} in {root_name}"
        else:
            first = f"This is {root_name}"
            if default and default.IsValid():
                self.terms["default prim"] = None
                first += f", with its content under {default.GetPath()} (the default prim)"
        if theme:
            first += f"; judging by the names it looks like {theme}"
        sentences.append(first + ".")
        parts = []
        for count, word in (
            (len(self.geometry), "shape"), (len(self.lights), "light"), (len(self.cameras), "camera"),
            (len(self.materials), "material"), (len(self.rigid_bodies), "rigid body"),
        ):
            if count:
                parts.append(_plural(count, word))
        size_text = ""
        if self.scene is not None:
            s = _size(self.scene)
            size_text = f", filling an area about {_fmt(s[self.X])} m wide, {_fmt(s[self.B])} m deep and {_fmt(s[self.H])} m tall"
        counted = f"{self.visited}{'+' if (self.truncated or self.time_stopped) else ''} prims"
        sentences.append(f"It has {counted}{' including ' + _join(parts) if parts else ''}{size_text}.")
        sentences.append(f"{self._units_words()}.")
        extras = []
        refs = sum(1 for i in self.compose_items if i["kind"] in ("reference", "payload"))
        subs = len(self.stage.GetLayerStack(False)) - 1
        if subs > 0:
            extras.append(f"{subs} sublayer{'s' if subs != 1 else ''}")
        if refs:
            extras.append(f"{refs} referenced asset{'s' if refs != 1 else ''}")
        tail = []
        if extras:
            tail.append(f"pulls in {_join(extras)}")
        if self.editor_items:
            tail.append(f"has {len(self.editor_items)} editor helper prim{'s' if len(self.editor_items) != 1 else ''} (Kit's viewport cameras and render settings) that are not your content")
        if tail:
            sentences.append(f"It also {' and '.join(tail)}.")
        if self.truncated or self.time_stopped:
            sentences[-1] = sentences[-1] + " This tour stopped early, so it is partial."
        return " ".join(sentences[:4])

    def _theme(self) -> str | None:
        names = [g["name"].lower() for g in self.groups] + [g["name"].lower() for g in self.geometry[:50]]
        names.append(_file_name(self.stage.GetRootLayer().identifier).lower())
        for key, phrase in THEME_WORDS:
            if any(key in n for n in names):
                return phrase
        return None

    def _questions(self) -> list[str]:
        questions: list[str] = []
        for light in self.overridden_lights[:1]:
            questions.append(
                f"Why is {light['name']}'s intensity {light['hidden']} from {light['hidden_layer']} hidden by a stronger layer? "
                f"Try explaining the value of {light['path']}.inputs:intensity."
            )
        hidden_lights = [l for l in self.lights if not l["visible"]]
        if hidden_lights:
            questions.append(f"Why does {hidden_lights[0]['name']} not light the scene, and what would make it visible again? (Hint: visibility is inherited.)")
        if self.scaled_groups:
            uniform = [g for g in self.scaled_groups if max(g["scale"]) - min(g["scale"]) < 1e-4]
            group = (uniform or self.scaled_groups)[0]
            scale = _fmt(group["scale"][0]) if uniform else f"({_fmt_list(group['scale'])})"
            child = next((g for g in self.geometry if g["parent"] == group["path"]), None)
            subject = child["name"] if child else "the prims inside it"
            questions.append(
                f"{group['name']} has a scale of {scale}: what happens to {subject}'s world size and position if you set it back to 1?"
            )
        moving = [p for p in self.physics if p["kind"] == "rigid_body" and p["enabled"] and not p["kinematic"]]
        if moving:
            static = next((
                p for p in self.physics
                if p["kind"] == "collider" and not any(p["path"] == rb or p["path"].startswith(rb + "/") for rb in self.rigid_bodies)
            ), None)
            still = f", and why does {static['name']} stay still" if static else ""
            questions.append(f"What will {moving[0]['name']} do when you press Play{still}?")
        refs = [i for i in self.compose_items if i["kind"] == "reference" and i["facts"].get("asset")]
        if refs:
            questions.append(f"Open {_file_name(refs[0]['facts']['asset'])}: which values on {refs[0]['path']} come from it, and which are local overrides?")
        if self.stage.GetLayerStack(False)[1:]:
            questions.append("If the root layer and a sublayer both set the same attribute, which one wins and why?")
        if self.cameras:
            cam = self.cameras[0]
            focal = _num(float(cam["focal"])) if cam["focal"] else 50
            questions.append(f"What does {cam['name']} see if you change its focal length from {focal} mm to {focal * 2 if isinstance(focal, (int, float)) else 100} mm?")
        if any(i["kind"] == "editor_helper" for i in self.editor_items):
            questions.append("Why does this file contain Kit's OmniverseKit_* or /Render prims, and what would happen if you deleted them?")
        if self.up == "Y" or abs(self.mpu - 1.0) > 1e-9:
            questions.append(f"With {self._units_words()}, how large is a Cube of size 1 in metres?")
        if self.materials:
            questions.append(f"What changes if you bind {self.materials[0]['name']} to a whole group instead of a single prim?")
        fallbacks = [
            "Pick one shape: what is its local position compared with its world position, and why might they differ?",
            "Which prim is the default prim, and why does that matter when another file references this one?",
            "If you add a light, where would it be best placed to light the main objects?",
        ]
        for question in fallbacks:
            if len(questions) >= 3:
                break
            questions.append(question)
        return questions[:MAX_QUESTIONS]


# ---------------------------------------------------------------------- module helpers


def _require_pxr() -> None:
    if _PXR_IMPORT_ERROR is not None:
        raise ImportError("USD Python bindings (pxr) are required for the scene tour.") from _PXR_IMPORT_ERROR


def _item(path: str, kind: str, plain: str, tip: str | None = None, facts: dict[str, Any] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"path": str(path), "kind": kind, "plain": plain}
    if tip:
        item["tip"] = tip
    if facts:
        item["facts"] = usd_insight.to_jsonable(facts)
    return item


def _section(title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    section: dict[str, Any] = {"title": title, "items": items[:MAX_SECTION_ITEMS]}
    if len(items) > MAX_SECTION_ITEMS:
        hidden = len(items) - MAX_SECTION_ITEMS
        section["items"].append({"path": "", "kind": "summary", "plain": f"...and {hidden} more not listed here."})
        section["truncated"] = True
        section["total"] = len(items)
    return section


def _get(attr: Any) -> Any:
    if not attr:
        return None
    try:
        return attr.Get()
    except Exception:
        return None


def _schemas(prim: Any) -> list[str]:
    try:
        return [str(name) for name in prim.GetAppliedSchemas()]
    except Exception:
        return []


def _targets(prim: Any, name: str) -> list[str]:
    rel = prim.GetRelationship(name)
    return [str(t) for t in rel.GetTargets()] if rel else []


def _is_light(prim: Any, type_name: str) -> bool:
    if type_name in LIGHT_WORDS:
        return True
    try:
        return bool(prim.HasAPI(UsdLux.LightAPI))
    except Exception:
        return type_name.endswith("Light")


def _base_type(type_name: str) -> str:
    return type_name[:-2] if type_name.endswith("_1") else type_name


def _is_identity(matrix: Any, tol: float = 1e-6) -> bool:
    identity = Gf.Matrix4d(1.0)
    return all(abs(matrix[i][j] - identity[i][j]) <= tol for i in range(4) for j in range(4))


def _list_items(list_op: Any) -> list[Any]:
    try:
        return list(list_op.GetAddedOrExplicitItems())
    except Exception:
        return []


def _dedupe(values: list[Any], key: Any) -> list[Any]:
    seen = set()
    out = []
    for value in values:
        k = key(value)
        if k not in seen:
            seen.add(k)
            out.append(value)
    return out


def _hidden_opinion(attr: Any, time_code: Any) -> dict[str, Any] | None:
    """When a weaker layer authors a different default value for attr, describe the winner and the hidden one."""
    try:
        stack = attr.GetPropertyStack(time_code)
    except Exception:
        return None
    values = [(spec.layer, spec.default) for spec in stack if spec.HasDefaultValue()]
    if len(values) < 2:
        return None
    win_layer, winning = values[0]
    for layer, value in values[1:]:
        if value != winning:
            return {
                "winning": _num(winning) if isinstance(winning, (int, float)) else str(winning),
                "winning_layer": usd_insight._short_layer(win_layer.identifier),
                "hidden": _num(value) if isinstance(value, (int, float)) else str(value),
                "hidden_layer": usd_insight._short_layer(layer.identifier),
            }
    return None


def _display_color(prim: Any) -> str | None:
    try:
        values = UsdGeom.Gprim(prim).GetDisplayColorAttr().Get()
    except Exception:
        return None
    if not values or len(values) != 1:
        return None
    return _color_word([float(c) for c in values[0]])


def _color_word(rgb: list[float]) -> str | None:
    if len(rgb) < 3:
        return None
    r, g, b = (min(max(c, 0.0), 1.0) for c in rgb[:3])
    hue, saturation, value = colorsys.rgb_to_hsv(r, g, b)
    if saturation < 0.15 or value < 0.08:
        if value < 0.12:
            return "black"
        if value < 0.4:
            return "dark grey"
        if value < 0.8:
            return "grey"
        return "white"
    degrees = hue * 360
    for limit, word in ((15, "red"), (45, "orange"), (70, "yellow"), (170, "green"), (200, "teal"), (260, "blue"), (300, "purple"), (345, "pink"), (361, "red")):
        if degrees < limit:
            break
    if value < 0.4:
        return f"dark {word}"
    if saturation < 0.4 and value > 0.7:
        return f"light {word}"
    return word


def _unit(vector: list[float]) -> list[float]:
    length = math.sqrt(sum(v * v for v in vector))
    return [v / length for v in vector] if length > 1e-12 else [0.0, 0.0, 0.0]


def _size(bounds: tuple[list[float], list[float]]) -> list[float]:
    return [bounds[1][i] - bounds[0][i] for i in range(3)]


def _center(bounds: tuple[list[float], list[float]]) -> list[float]:
    return [(bounds[0][i] + bounds[1][i]) / 2 for i in range(3)]


def _union(boxes: list[Any]) -> tuple[list[float], list[float]] | None:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return [min(b[0][i] for b in boxes) for i in range(3)], [max(b[1][i] for b in boxes) for i in range(3)]


def _overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> float:
    return max(0.0, min(a_hi, b_hi) - max(a_lo, b_lo))


def _ray_box(origin: list[float], direction: list[float], bounds: tuple[list[float], list[float]]) -> float | None:
    """Distance along a unit ray to where it enters the box, or None (also None when the ray starts inside)."""
    t_near, t_far = -math.inf, math.inf
    for i in range(3):
        lo, hi = bounds[0][i], bounds[1][i]
        if abs(direction[i]) < 1e-12:
            if origin[i] < lo or origin[i] > hi:
                return None
            continue
        t1, t2 = (lo - origin[i]) / direction[i], (hi - origin[i]) / direction[i]
        t_near, t_far = max(t_near, min(t1, t2)), min(t_far, max(t1, t2))
        if t_near > t_far:
            return None
    if t_near <= 1e-9:
        return None
    return t_near


def _fmt(value: float) -> str:
    value = float(value)
    decimals = 1 if abs(value) >= 10 else 2 if abs(value) >= 0.1 else 3
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _fmt_list(values: list[float]) -> str:
    return ", ".join(_fmt(v) for v in values)


def _round_list(values: list[float]) -> list[float]:
    return [round(float(v), 4) + 0.0 for v in values]


def _num(value: Any) -> Any:
    number = float(value)
    if not math.isfinite(number):
        return str(number)
    if abs(number - round(number)) < 1e-3 * max(1.0, abs(number)):
        return int(round(number))
    return round(number, 2)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _plural(count: int, word: str) -> str:
    if count == 1:
        return f"1 {word}"
    if word.endswith("body"):
        return f"{count} {word[:-1]}ies"
    return f"{count} {word}s"


def _join(words: list[str]) -> str:
    words = [str(w) for w in words]
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _file_name(identifier: str) -> str:
    text = str(identifier)
    if text.startswith("anon:"):
        return "an in-memory layer"
    return text.replace("\\", "/").rsplit("/", 1)[-1] or text
