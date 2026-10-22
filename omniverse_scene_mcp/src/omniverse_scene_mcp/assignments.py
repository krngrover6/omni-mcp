"""Assignment coach: check a USD stage against a homework rubric and say, in plain words, what to do next.

Read-only by design. The checker only reads stage facts (prim types, names, hierarchy, world bounding boxes
from UsdGeom.BBoxCache, world transforms from UsdGeom.XformCache) and never authors, edits or saves anything.
It works on any Usd.Stage: a saved file opened read-only now, or a live stage reached through the bridge later.

Rubrics live in data/assignments/<id>.json. Each rule has id, title, why, how_to_fix (steps), mcp_phrase and
weight. A rule is reported as pass, partial, fail or unknown; unknown means the evidence was too weak to judge,
and unknown rules are left out of the score instead of being guessed.

Public API:
    list_assignments() -> list[dict]
    load_assignment(assignment_id) -> dict
    check_assignment(stage, assignment_id='isaac_session02_warehouse') -> dict
        score_pct weighs rules by weight (pass 1, partial 0.5, fail 0; unknown rules are left out of the score);
        passed is True only when every rule passes.
    check_scene_file(path, assignment_id=..., workspace_root=None, allow_outside_workspace=False) -> dict
"""

from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path
from typing import Any

try:
    from pxr import Gf, Usd, UsdGeom
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Usd = UsdGeom = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

DEFAULT_ASSIGNMENT = "isaac_session02_warehouse"
DATA_DIR = Path(__file__).resolve().parent / "data" / "assignments"
MAX_PRIMS = 50_000
STATUS_CREDIT = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
AIM_PASS_DEG = 25.0
AIM_PARTIAL_DEG = 45.0
SHELF_AIM_DEG = 45.0
EXPECTED_ROWS = [4, 3, 2, 1]
AXES = "XYZ"
# Kit's own viewport cameras live in the session layer of a live stage; /Render holds render products.
IGNORED_ROOTS = ("/OmniverseKit", "/Render")
GENERIC_NAME = re.compile(
    r"^(cube|sphere|cylinder|cone|capsule|plane|mesh|xform|scope|group|rectlight|spherelight|disklight|"
    r"distantlight|domelight|cylinderlight|light|camera|object|prim|shape|null|untitled|newprim|new_prim)"
    r"(_?\d+)?$",
    re.IGNORECASE,
)
ENV_WORDS = re.compile(r"env|room|warehouse|structure|building|wall|world_?geo|level|layout", re.IGNORECASE)
SHELF_WORDS = re.compile(r"shel|rack", re.IGNORECASE)
LIGHT_WORDS = re.compile(r"light|lamp|illum", re.IGNORECASE)
LOCAL_LIGHTS = {"RectLight", "SphereLight", "DiskLight", "CylinderLight"}
EXTRA_LIGHT_TYPES = {"RectLight", "SphereLight"}


class AssignmentError(ValueError):
    """Raised for an unknown assignment id or an unreadable rubric file."""


# ----------------------------------------------------------------------------------------------- rubric files


def list_assignments() -> list[dict[str, Any]]:
    """Every rubric in data/assignments: id, title, session, summary, rule count and total weight."""
    found = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            rubric = _read_rubric(path)
        except AssignmentError:
            continue
        found.append(
            {
                "id": rubric["id"],
                "title": rubric.get("title", rubric["id"]),
                "session": rubric.get("session", ""),
                "summary": rubric.get("summary", ""),
                "rule_count": len(rubric["rules"]),
                "total_weight": sum(float(rule.get("weight", 0)) for rule in rubric["rules"]),
            }
        )
    return found


def load_assignment(assignment_id: str = DEFAULT_ASSIGNMENT) -> dict[str, Any]:
    """Return the rubric dict for assignment_id (raises AssignmentError when it does not exist)."""
    text = str(assignment_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", text) or text.startswith("."):
        raise AssignmentError(f"Unknown assignment {assignment_id!r}.")
    path = DATA_DIR / f"{text}.json"
    if not path.is_file():
        known = ", ".join(item["id"] for item in list_assignments()) or "none"
        raise AssignmentError(f"Unknown assignment {assignment_id!r}. Known assignments: {known}.")
    return _read_rubric(path)


def _read_rubric(path: Path) -> dict[str, Any]:
    try:
        rubric = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AssignmentError(f"Could not read rubric {path.name}: {exc}") from exc
    if not isinstance(rubric, dict) or not isinstance(rubric.get("rules"), list) or "id" not in rubric:
        raise AssignmentError(f"Rubric {path.name} needs an 'id' and a 'rules' list.")
    return rubric


# ------------------------------------------------------------------------------------------------- checking


def check_scene_file(
    path: Any,
    assignment_id: str = DEFAULT_ASSIGNMENT,
    *,
    workspace_root: Any = None,
    allow_outside_workspace: bool = False,
) -> dict[str, Any]:
    """Open a scene file strictly read-only (file_stage.readonly_stage) and check it."""
    from omniverse_scene_mcp import file_stage

    resolved = file_stage.resolve_scene_file(
        path, workspace_root=workspace_root, allow_outside_workspace=allow_outside_workspace
    )
    with file_stage.readonly_stage(resolved) as stage:
        report = check_assignment(stage, assignment_id)
    report["scene_file"] = resolved
    return report


def check_assignment(stage: Any, assignment_id: str = DEFAULT_ASSIGNMENT) -> dict[str, Any]:
    """Check stage against the rubric; returns ok, score_pct, passed, results, next_step and summary."""
    try:
        rubric = load_assignment(assignment_id)
    except AssignmentError as exc:
        return _not_ok(assignment_id, str(exc))
    if Usd is None:
        return _not_ok(assignment_id, f"USD Python bindings (pxr) are required: {_PXR_IMPORT_ERROR}")
    if stage is None or not hasattr(stage, "GetPseudoRoot"):
        return _not_ok(assignment_id, "No USD stage was given. Open the scene first.")

    dirty_before = _dirty_layers(stage)
    scene = _Scene(stage)
    results = []
    for rule in rubric["rules"]:
        checker = _CHECKERS.get(rule.get("id"))
        if checker is None:
            status, evidence, detail = "unknown", {}, "This checker has no test for this rule yet."
        else:
            try:
                status, evidence, detail = checker(scene)
            except Exception as exc:  # one broken heuristic must not hide the other rules
                status, evidence, detail = "unknown", {"error": f"{type(exc).__name__}: {exc}"}, (
                    "The checker hit an error on this rule, so it could not judge it."
                )
        results.append(
            {
                "rule": rule.get("id"),
                "title": rule.get("title", rule.get("id")),
                "status": status,
                "weight": rule.get("weight", 0),
                "detail": detail,
                "evidence": evidence,
                "why": rule.get("why", ""),
                "how_to_fix": list(rule.get("how_to_fix", [])),
                "mcp_phrase": rule.get("mcp_phrase", ""),
            }
        )
    newly_dirty = sorted(_dirty_layers(stage) - dirty_before)

    scored = [item for item in results if item["status"] in STATUS_CREDIT]
    total = sum(float(item["weight"]) for item in scored)
    earned = sum(float(item["weight"]) * STATUS_CREDIT[item["status"]] for item in scored)
    score_pct = round(100.0 * earned / total, 1) if total else 0.0
    counts = {status: sum(1 for item in results if item["status"] == status) for status in ("pass", "partial", "fail", "unknown")}
    passed = bool(results) and counts["pass"] == len(results)

    return {
        "ok": True,
        "assignment": rubric["id"],
        "title": rubric.get("title", rubric["id"]),
        "score_pct": score_pct,
        "passed": passed,
        "counts": counts,
        "results": results,
        "next_step": _next_step(results),
        "summary": _summary(results, counts, score_pct, passed),
        "scene": scene.overview(),
        "read_only": not newly_dirty,
        "notes": list(rubric.get("notes", [])) + (
            [f"Warning: these layers became dirty during the check: {newly_dirty}"] if newly_dirty else []
        ),
    }


def _not_ok(assignment_id: Any, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "assignment": assignment_id,
        "error": error,
        "score_pct": 0.0,
        "passed": False,
        "results": [],
        "next_step": error,
        "summary": error,
    }


def _dirty_layers(stage: Any) -> set[str]:
    try:
        layers = list(stage.GetUsedLayers(True))
        layers.extend(item for item in (stage.GetRootLayer(), stage.GetSessionLayer()) if item is not None)
        return {layer.identifier for layer in layers if layer.dirty}
    except Exception:  # pragma: no cover - exotic stage wrappers
        return set()


def _next_step(results: list[dict[str, Any]]) -> str:
    """The single most useful thing to do now: the first failing rule in build order, else the first partial."""
    for wanted in ("fail", "partial", "unknown"):
        for item in results:
            if item["status"] != wanted:
                continue
            first = item["how_to_fix"][0] if item["how_to_fix"] else ""
            text = f"{item['title']}: {item['detail']}"
            if first:
                text += f" Start with: {first}"
            return text
    return "Every check passes. Look through your camera once more, take a screenshot, and save the scene (Ctrl+S)."


def _summary(results: list[dict[str, Any]], counts: dict[str, int], score: float, passed: bool) -> str:
    parts = [f"{counts['pass']} of {len(results)} checks pass"]
    if counts["partial"]:
        parts.append(f"{counts['partial']} partly done")
    if counts["fail"]:
        parts.append(f"{counts['fail']} not done yet")
    if counts["unknown"]:
        parts.append(f"{counts['unknown']} could not be judged")
    verdict = "Every check passes: looks ready to hand in." if passed else "Not finished yet: every check has to pass."
    return f"{', '.join(parts)}. Score {score:g}% (unknown checks are left out of the score). {verdict}"


# ------------------------------------------------------------------------------------------------ stage facts


class _Geo:
    """One visible gprim with its world-space axis-aligned box."""

    __slots__ = ("path", "prim", "type", "mn", "mx", "size", "center", "role")

    def __init__(self, prim: Any, mn: tuple[float, ...], mx: tuple[float, ...]) -> None:
        self.prim = prim
        self.path = str(prim.GetPath())
        self.type = str(prim.GetTypeName())
        self.mn = mn
        self.mx = mx
        self.size = tuple(b - a for a, b in zip(mn, mx))
        self.center = tuple((a + b) / 2 for a, b in zip(mn, mx))
        self.role = ""


class _Emitter:
    """A visible light or camera with world position and forward direction (local -Z)."""

    __slots__ = ("path", "prim", "type", "pos", "fwd", "intensity", "cone")

    def __init__(self, prim: Any, matrix: Any) -> None:
        self.prim = prim
        self.path = str(prim.GetPath())
        self.type = str(prim.GetTypeName())
        self.pos = tuple(float(v) for v in matrix.ExtractTranslation())
        self.fwd = _unit(tuple(float(v) for v in matrix.TransformDir(Gf.Vec3d(0, 0, -1))))
        self.intensity = 1.0
        self.cone = 180.0


class _Scene:
    """Classifies stage geometry into floor, walls, ceiling, shelves, shelf objects and the cube stack."""

    def __init__(self, stage: Any) -> None:
        self.stage = stage
        up = str(UsdGeom.GetStageUpAxis(stage))
        self.up_axis = "Y" if up == "Y" else "Z"
        self.u = 1 if self.up_axis == "Y" else 2
        self.h = (0, 2) if self.u == 1 else (0, 1)
        self.default_prim = str(stage.GetDefaultPrim().GetPath()) if stage.GetDefaultPrim() else ""
        self.geos: list[_Geo] = []
        self.lights: list[_Emitter] = []
        self.cameras: list[_Emitter] = []
        self.animated: list[str] = []
        self.rigid: list[str] = []
        self.named: list[Any] = []
        self.truncated = False
        self._collect()
        self._classify()

    # -------------------------------------------------------------------------------------------- collection

    def _collect(self) -> None:
        time = Usd.TimeCode.Default()
        cache = UsdGeom.BBoxCache(time, [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=False)
        xcache = UsdGeom.XformCache(time)
        prim_range = Usd.PrimRange.Stage(self.stage, Usd.TraverseInstanceProxies())
        iterator = iter(prim_range)
        seen = 0
        for prim in iterator:
            seen += 1
            if seen > MAX_PRIMS:
                self.truncated = True
                break
            path = str(prim.GetPath())
            if path.startswith(IGNORED_ROOTS):
                iterator.PruneChildren()
                continue
            if prim.IsA(UsdGeom.Imageable):
                imageable = UsdGeom.Imageable(prim)
                if imageable.GetVisibilityAttr().Get(time) == UsdGeom.Tokens.invisible:
                    iterator.PruneChildren()
                    continue
                if imageable.GetPurposeAttr().Get(time) == UsdGeom.Tokens.guide:
                    iterator.PruneChildren()
                    continue
            self.named.append(prim)
            if prim.IsA(UsdGeom.Xformable):
                try:
                    if UsdGeom.Xformable(prim).TransformMightBeTimeVarying():
                        self.animated.append(path)
                except Exception:  # pragma: no cover - malformed xform ops
                    pass
            try:
                if "PhysicsRigidBodyAPI" in prim.GetAppliedSchemas():
                    self.rigid.append(path)
            except Exception:  # pragma: no cover
                pass
            type_name = str(prim.GetTypeName())
            if prim.IsA(UsdGeom.Camera):
                self.cameras.append(_Emitter(prim, xcache.GetLocalToWorldTransform(prim)))
            elif _is_light(prim, type_name):
                light = _Emitter(prim, xcache.GetLocalToWorldTransform(prim))
                light.intensity = _float_attr(prim, ("inputs:intensity", "intensity"), 1.0)
                light.cone = _float_attr(prim, ("inputs:shaping:cone:angle", "shaping:cone:angle"), 180.0)
                if light.intensity > 0:
                    self.lights.append(light)
            elif prim.IsA(UsdGeom.Gprim):
                box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
                if box.IsEmpty():
                    continue
                low, high = tuple(float(v) for v in box.GetMin()), tuple(float(v) for v in box.GetMax())
                if not all(math.isfinite(v) for v in low + high):
                    continue  # a NaN/inf transform: such a box passes every size test (NaN compares False) and became a "shelf"
                self.geos.append(_Geo(prim, low, high))

    # ------------------------------------------------------------------------------------------ classification

    def _classify(self) -> None:
        u, (a, b) = self.u, self.h
        self.floor: _Geo | None = None
        self.extra_floors: list[_Geo] = []
        self.walls: list[_Geo] = []
        self.wall_sides: dict[str, list[_Geo]] = {}
        self.interior_walls: list[_Geo] = []
        self.ceilings: list[_Geo] = []
        self.shelves: list[_Geo] = []
        self.shelf_objects: list[dict[str, Any]] = []
        self.stack: list[_Geo] = []
        self.stack_clusters: list[list[_Geo]] = []
        if not self.geos:
            self.scene_min = self.scene_max = (0.0, 0.0, 0.0)
            self.room_min = self.room_max = (0.0, 0.0, 0.0)
            self.room_center = (0.0, 0.0, 0.0)
            self.floor_top = 0.0
            return
        self.scene_min = tuple(min(g.mn[i] for g in self.geos) for i in range(3))
        self.scene_max = tuple(max(g.mx[i] for g in self.geos) for i in range(3))
        scene_size = [hi - lo for lo, hi in zip(self.scene_min, self.scene_max)]
        scene_area = max(scene_size[a] * scene_size[b], 1e-12)
        height_tol = 0.05 * max(scene_size[u], 1e-6) + 1e-6

        # Floor: the largest flat horizontal surface at the lowest level.
        flats = [
            g for g in self.geos
            if min(g.size[a], g.size[b]) > 0
            and g.size[u] <= 0.1 * min(g.size[a], g.size[b])
            and g.size[a] * g.size[b] >= 0.1 * scene_area
        ]
        flats.sort(key=lambda g: -(g.size[a] * g.size[b]))
        for candidate in flats:
            others = [g.mn[u] for g in self.geos if g is not candidate and g not in flats]
            lowest = min(others) if others else candidate.mx[u]
            if candidate.mx[u] <= lowest + height_tol:
                self.floor = candidate
                break
        if self.floor is not None:
            floor = self.floor
            floor.role = "floor"
            area = floor.size[a] * floor.size[b]
            parent = floor.prim.GetParent().GetPath()
            for g in flats:
                if g is floor or abs(g.mx[u] - floor.mx[u]) > height_tol:
                    continue
                if g.size[a] * g.size[b] >= 0.5 * area and g.prim.GetParent().GetPath() != parent:
                    g.role = "floor"
                    self.extra_floors.append(g)
            self.floor_top = floor.mx[u]
            ref_min, ref_max = floor.mn, floor.mx
        else:
            self.floor_top = self.scene_min[u]
            ref_min, ref_max = self.scene_min, self.scene_max
        ref_span = [max(hi - lo, 1e-9) for lo, hi in zip(ref_min, ref_max)]
        ref_center = [(lo + hi) / 2 for lo, hi in zip(ref_min, ref_max)]

        # Walls: thin, tall, long boxes or planes standing on the floor.
        candidates = []
        for g in self.geos:
            if g.role:
                continue
            thin_axis, long_axis = (a, b) if g.size[a] <= g.size[b] else (b, a)
            thin, length, height = g.size[thin_axis], g.size[long_axis], g.size[u]
            if length <= 0 or thin > 0.25 * length or height < 3 * thin or height < 0.15 * length:
                continue
            if length < 0.1 * ref_span[long_axis]:
                continue
            if self.floor is not None and g.mn[u] > self.floor_top + 0.25 * height:
                continue
            candidates.append((g, thin_axis, long_axis))
        if candidates:
            longest = max(g.size[long_axis] for g, _t, long_axis in candidates)
            candidates = [item for item in candidates if item[0].size[item[2]] >= 0.3 * longest]
        self._wall_axes: dict[str, tuple[int, int]] = {}
        center = list(ref_center)
        for _attempt in range(2):
            sides: dict[str, list[_Geo]] = {}
            interior = []
            span = list(ref_span)
            for g, thin_axis, _long in candidates:
                offset = g.center[thin_axis] - center[thin_axis]
                if abs(offset) < 0.15 * span[thin_axis]:
                    interior.append(g)
                    continue
                sides.setdefault(("+" if offset > 0 else "-") + AXES[thin_axis], []).append(g)
            if len(sides) < 2:
                break
            walled = [g for group in sides.values() for g in group]
            union_min = [min(g.mn[i] for g in walled) for i in range(3)]
            union_max = [max(g.mx[i] for g in walled) for i in range(3)]
            new_center = [(lo + hi) / 2 for lo, hi in zip(union_min, union_max)]
            if all(abs(new_center[i] - center[i]) < 1e-9 for i in range(3)):
                break
            center = new_center
            ref_span = [max(hi - lo, 1e-9) for lo, hi in zip(union_min, union_max)]
        self.wall_sides = sides if candidates else {}
        self.interior_walls = interior if candidates else []
        for g, thin_axis, long_axis in candidates:
            g.role = "wall"
            self.walls.append(g)
            self._wall_axes[g.path] = (thin_axis, long_axis)
        if self.walls:
            self.room_min = tuple(min(g.mn[i] for g in self.walls) for i in range(3))
            self.room_max = tuple(max(g.mx[i] for g in self.walls) for i in range(3))
        elif self.floor is not None:
            self.room_min, self.room_max = self.floor.mn, self.floor.mx
        else:
            self.room_min, self.room_max = self.scene_min, self.scene_max
        self.room_center = tuple(center) if self.wall_sides else tuple((lo + hi) / 2 for lo, hi in zip(self.room_min, self.room_max))
        room_size = [hi - lo for lo, hi in zip(self.room_min, self.room_max)]
        self.room_span = max(room_size[a], room_size[b], 1e-9)
        room_area = max(room_size[a] * room_size[b], 1e-12)
        wall_top = max((g.mx[u] for g in self.walls), default=self.scene_max[u])
        self.wall_top = wall_top

        # Ceiling: a big flat piece high above the floor.
        for g in self.geos:
            if g.role or min(g.size[a], g.size[b]) <= 0 or g.size[u] > 0.1 * min(g.size[a], g.size[b]):
                continue
            if self.walls and g.size[a] * g.size[b] >= 0.5 * room_area and g.mn[u] >= self.floor_top + 0.7 * (wall_top - self.floor_top):
                g.role = "ceiling"
                self.ceilings.append(g)

        # Shelves: thin horizontal boards above the floor, smaller than the room.
        boards = []
        for g in self.geos:
            if g.role or min(g.size[a], g.size[b]) <= 0:
                continue
            if g.size[u] > 0.3 * min(g.size[a], g.size[b]) or max(g.size[a], g.size[b]) < 0.05 * self.room_span:
                continue
            if g.size[a] * g.size[b] > 0.3 * room_area:
                continue
            if g.mn[u] <= self.floor_top + max(2 * g.size[u], 0.02 * self.room_span):
                continue
            boards.append(g)
        resting_on_board = set()
        for g in boards:
            for other in boards:
                if other is not g and _touching_top(g, other, u, self.room_span) and _inside_footprint(g.center, other, (a, b)):
                    resting_on_board.add(g.path)
        for g in boards:
            if g.path not in resting_on_board:
                g.role = "shelf"
                self.shelves.append(g)
        self.shelves.sort(key=lambda g: g.mn[u])

        # Objects on shelves: other gprims whose footprint centre is over a shelf and whose bottom is near its top.
        for g in self.geos:
            if g.role:
                continue
            best = None
            for shelf in self.shelves:
                if not _inside_footprint(g.center, shelf, (a, b)):
                    continue
                if g.mn[u] < shelf.mn[u] - 1e-6:
                    continue
                above = [s.mn[u] for s in self.shelves if s is not shelf and s.mn[u] > shelf.mx[u] and _overlap_2d(s, shelf, (a, b))]
                ceiling = min(above) if above else shelf.mx[u] + 2 * max(g.size[u], 1e-6)
                if g.mn[u] >= ceiling:
                    continue
                gap = g.mn[u] - shelf.mx[u]
                if best is None or abs(gap) < abs(best[1]):
                    best = (shelf, gap)
            if best is None:
                continue
            shelf, gap = best
            tol = max(0.05 * g.size[u], 0.002 * self.room_span, 1e-4)
            state = "resting" if abs(gap) <= tol else ("floating" if gap > 0 else "sunk")
            g.role = "shelf_object"
            self.shelf_objects.append({"geo": g, "shelf": shelf, "gap": gap, "state": state})

        # Cube stack: the largest cluster of touching box-like gprims left over.
        boxes = [g for g in self.geos if not g.role and min(g.size) > 0 and max(g.size) / min(g.size) <= 2.5]
        if boxes:
            unit = statistics.median(min(g.size) for g in boxes)
            gap_tol = 0.25 * unit
            parent = list(range(len(boxes)))

            def find(i: int) -> int:
                while parent[i] != i:
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                return i

            # Sweep along X: once a box starts more than gap_tol past box i's end, every later one does too. This keeps
            # big scenes (thousands of boxes) from an all-pairs loop that froze Kit's main thread for minutes.
            order = sorted(range(len(boxes)), key=lambda i: boxes[i].mn[0])
            for position, i in enumerate(order):
                reach = boxes[i].mx[0] + gap_tol
                for later in range(position + 1, len(order)):
                    j = order[later]
                    if boxes[j].mn[0] > reach:
                        break
                    if all(_axis_gap(boxes[i], boxes[j], k) <= gap_tol for k in range(3)):
                        parent[find(i)] = find(j)
            groups: dict[int, list[_Geo]] = {}
            for i, g in enumerate(boxes):
                groups.setdefault(find(i), []).append(g)
            clusters = sorted(
                groups.values(),
                key=lambda c: (-len(c), _dist2(_center_of(c), self.room_center, (a, b))),
            )
            self.stack_clusters = clusters
            if clusters and len(clusters[0]) >= 3:
                self.stack = clusters[0]
                for g in self.stack:
                    g.role = "stack"

    # ---------------------------------------------------------------------------------------------- helpers

    def stack_center(self) -> tuple[float, ...] | None:
        return _center_of(self.stack) if self.stack else None

    def camera_aims(self, target: tuple[float, ...]) -> list[dict[str, Any]]:
        aims = []
        for cam in self.cameras:
            angle = _angle(cam.fwd, _sub(target, cam.pos))
            aims.append({"camera": cam, "angle": angle})
        aims.sort(key=lambda item: item["angle"])
        return aims

    def aimed_camera(self) -> _Emitter | None:
        center = self.stack_center()
        if center is None:
            return None
        aims = self.camera_aims(center)
        return aims[0]["camera"] if aims and aims[0]["angle"] <= AIM_PARTIAL_DEG else None

    def light_on_target(self, light: _Emitter, target: tuple[float, ...], radius: float, max_deg: float) -> tuple[bool, float | None]:
        """Is target lit by this light? Directional local lights by angle, omni lights by distance."""
        to_target = _sub(target, light.pos)
        if _is_directional(light):
            angle = _angle(light.fwd, to_target)
            return angle <= max_deg, angle
        if light.type in LOCAL_LIGHTS:
            return _norm(to_target) <= radius, None
        return False, None

    def overview(self) -> dict[str, Any]:
        return {
            "up_axis": self.up_axis,
            "default_prim": self.default_prim,
            "gprims": len(self.geos),
            "lights": len(self.lights),
            "cameras": len(self.cameras),
            "floor": self.floor.path if self.floor else None,
            "walls": len(self.walls),
            "shelves": len(self.shelves),
            "shelf_objects": len(self.shelf_objects),
            "stack_cubes": len(self.stack),
            "truncated": self.truncated,
        }

    def side_of(self, direction: tuple[float, ...]) -> str:
        a, b = self.h
        axis = a if abs(direction[a]) >= abs(direction[b]) else b
        return ("+" if direction[axis] > 0 else "-") + AXES[axis]


# ---------------------------------------------------------------------------------------------- rule checkers


def _check_floor(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    if scene.floor is None:
        flats = [g.path for g in scene.geos if g.size[scene.u] <= 0.1 * max(min(g.size[i] for i in scene.h), 1e-9)]
        return "fail", {"flat_prims_seen": flats[:10]}, "No floor found: there is no large flat surface under the scene."
    floor = scene.floor
    a, b = scene.h
    evidence: dict[str, Any] = {
        "floor": floor.path,
        "type": floor.type,
        "size": [_r(floor.size[a]), _r(floor.size[b])],
        "top_height": _r(scene.floor_top),
    }
    if scene.extra_floors:
        evidence["extra_floors"] = [g.path for g in scene.extra_floors]
        return "partial", evidence, f"There is more than one floor ({len(scene.extra_floors) + 1}); the homework asks for one."
    floating = [g.path for g in scene.walls if g.mn[scene.u] > scene.floor_top + 0.02 * g.size[scene.u] + 1e-4]
    if floating:
        evidence["walls_not_touching_floor"] = floating
        return "partial", evidence, "The floor exists, but some walls do not stand on it."
    return "pass", evidence, f"Floor found at {floor.path}."


def _check_three_walls(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    sides = {side: [g.path for g in group] for side, group in sorted(scene.wall_sides.items())}
    evidence: dict[str, Any] = {"walls_by_side": sides, "wall_count": len(scene.walls), "up_axis": scene.up_axis}
    if scene.interior_walls:
        evidence["interior_walls"] = [g.path for g in scene.interior_walls]
    if scene.walls:
        evidence["wall_height"] = _r(max(g.size[scene.u] for g in scene.walls))
    count = len(sides)
    if count == 3:
        return "pass", evidence, f"Three walls found on sides {', '.join(sides)}."
    if count == 4:
        return "partial", evidence, "Walls are on all four sides; the homework wants three, with the front open."
    if count == 2:
        return "partial", evidence, f"Only two walled sides found ({', '.join(sides)}); one wall is missing."
    if count == 1:
        return "fail", evidence, "Only one wall found; the homework needs a back wall and two side walls."
    return "fail", evidence, "No walls found: no thin, tall boxes standing around the floor."


def _check_open_front_top(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    a, b = scene.h
    all_sides = {sign + AXES[axis] for axis in (a, b) for sign in "+-"}
    open_sides = sorted(all_sides - set(scene.wall_sides))
    evidence: dict[str, Any] = {"open_sides": open_sides}
    if scene.ceilings:
        evidence["ceiling"] = [g.path for g in scene.ceilings]
        return "fail", evidence, "Something covers the top of the warehouse like a ceiling; the top should be open."
    if not scene.wall_sides:
        return "unknown", evidence, "No walls were found, so open sides cannot be judged yet."
    camera = scene.aimed_camera()
    if camera is not None:
        looks_from = scene.side_of(tuple(-v for v in camera.fwd))
        evidence["camera"] = camera.path
        evidence["camera_looks_in_from"] = looks_from
    if len(scene.wall_sides) >= 4:
        return "fail", evidence, "All four sides have walls, so the front is closed."
    if len(scene.wall_sides) == 3:
        if camera is not None and evidence["camera_looks_in_from"] not in open_sides:
            return "partial", evidence, (
                f"The open side is {open_sides[0]}, but the camera looks in from {evidence['camera_looks_in_from']}, through a wall."
            )
        return "pass", evidence, f"The front ({open_sides[0]}) and the top are open."
    return "partial", evidence, f"The top is open but {len(open_sides)} sides are open ({', '.join(open_sides)}); only the front should be."


def _check_shelves(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    u = scene.u
    records = []
    attached = 0
    for shelf in scene.shelves:
        wall = _attached_wall(scene, shelf)
        attached += wall is not None
        records.append(
            {
                "shelf": shelf.path,
                "height": _r(shelf.mn[u]),
                "top": _r(shelf.mx[u]),
                "attached_to": wall.path if wall else None,
            }
        )
    evidence: dict[str, Any] = {"shelves": records, "attached_count": attached}
    if not scene.shelves:
        return "fail", evidence, "No shelves found: no thin flat boards above the floor."
    if not scene.walls:
        return "partial", evidence, f"Found {len(scene.shelves)} shelf board(s), but no walls to attach them to."
    if attached >= 2:
        return "pass", evidence, f"{attached} shelves are attached to a wall."
    if attached == 1:
        return "partial", evidence, f"Only one shelf is attached to a wall; found {len(scene.shelves)} shelf board(s) in total."
    return "partial", evidence, f"Found {len(scene.shelves)} shelf board(s), but none touches the inside of a wall."


def _attached_wall(scene: _Scene, shelf: _Geo) -> _Geo | None:
    u = scene.u
    for wall in scene.walls:
        thin_axis, long_axis = scene._wall_axes[wall.path]
        gap = max(0.0, shelf.mn[thin_axis] - wall.mx[thin_axis], wall.mn[thin_axis] - shelf.mx[thin_axis])
        if gap > max(0.25 * shelf.size[thin_axis], 0.02 * scene.room_span):
            continue
        if not (wall.mn[long_axis] <= shelf.center[long_axis] <= wall.mx[long_axis]):
            continue
        if not (wall.mn[u] <= shelf.center[u] <= wall.mx[u]):
            continue
        inside = (shelf.center[thin_axis] - wall.center[thin_axis]) * (scene.room_center[thin_axis] - wall.center[thin_axis])
        if inside > 0:
            return wall
    return None


def _check_shelf_objects(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    if not scene.shelves:
        return "unknown", {}, "No shelves were found, so objects on shelves cannot be checked yet."
    per_shelf = {}
    for shelf in scene.shelves:
        items = [item for item in scene.shelf_objects if item["shelf"] is shelf]
        per_shelf[shelf.path] = {
            "resting": [item["geo"].path for item in items if item["state"] == "resting"],
            "floating": [
                {"path": item["geo"].path, "gap": _r(item["gap"])} for item in items if item["state"] == "floating"
            ],
            "sunk": [{"path": item["geo"].path, "gap": _r(item["gap"])} for item in items if item["state"] == "sunk"],
        }
    evidence = {"per_shelf": per_shelf}
    with_resting = [path for path, info in per_shelf.items() if info["resting"]]
    any_near = [path for path, info in per_shelf.items() if info["resting"] or info["floating"] or info["sunk"]]
    misplaced = sum(len(info["floating"]) + len(info["sunk"]) for info in per_shelf.values())
    if len(with_resting) == len(per_shelf) and not misplaced:
        total = sum(len(info["resting"]) for info in per_shelf.values())
        return "pass", evidence, f"Every shelf has objects resting on it ({total} in total)."
    if any_near:
        bad = [
            f"{_leaf(entry['path'])} floats {entry['gap']:g} above"
            for info in per_shelf.values() for entry in info["floating"]
        ] + [f"{_leaf(entry['path'])} sinks {abs(entry['gap']):g} into" for info in per_shelf.values() for entry in info["sunk"]]
        empty = [_leaf(path) for path in per_shelf if path not in any_near]
        detail = []
        if bad:
            detail.append("Some objects don't touch their shelf (" + "; ".join(bad[:4]) + " the shelf top)")
        if empty:
            detail.append("these shelves are empty: " + ", ".join(empty))
        return "partial", evidence, ("; ".join(detail) or "Objects are only on some shelves") + "."
    return "fail", evidence, "No small objects sit on the shelves yet."


def _check_stack(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    u, (a, b) = scene.u, scene.h
    if not scene.stack:
        largest = scene.stack_clusters[0] if scene.stack_clusters else []
        return "fail", {"largest_cube_group": [g.path for g in largest]}, "No stack of touching cubes found."
    stack = scene.stack
    height = statistics.median(g.size[u] for g in stack)
    rows: list[list[_Geo]] = []
    for g in sorted(stack, key=lambda g: g.mn[u]):
        if rows and abs(g.mn[u] - statistics.mean(x.mn[u] for x in rows[-1])) <= 0.3 * height:
            rows[-1].append(g)
        else:
            rows.append([g])
    counts = [len(row) for row in rows]
    bottom = rows[0]
    row_axis = a if (max(g.center[a] for g in bottom) - min(g.center[a] for g in bottom)) >= (
        max(g.center[b] for g in bottom) - min(g.center[b] for g in bottom)
    ) else b
    depth_axis = b if row_axis == a else a
    depth_extent = max(g.mx[depth_axis] for g in stack) - min(g.mn[depth_axis] for g in stack)
    box_depth = statistics.median(g.size[depth_axis] for g in stack)
    single_layer = depth_extent <= 1.6 * box_depth
    gaps = []
    for lower, upper in zip(rows, rows[1:]):
        gap = statistics.mean(g.mn[u] for g in upper) - max(g.mx[u] for g in lower)
        gaps.append(_r(gap))
    floats = [i + 2 for i, gap in enumerate(gaps) if abs(gap) > 0.15 * height]
    center = _center_of(stack)
    parents = sorted({str(g.prim.GetParent().GetPath()) for g in stack})
    evidence: dict[str, Any] = {
        "cube_count": len(stack),
        "rows_bottom_to_top": counts,
        "row_paths": [[g.path for g in sorted(row, key=lambda g: g.center[row_axis])] for row in rows],
        "row_heights": [_r(statistics.mean(g.mn[u] for g in row)) for row in rows],
        "row_axis": AXES[row_axis],
        "depth_axis": AXES[depth_axis],
        "single_layer_deep": single_layer,
        "center": [_r(v) for v in center],
        "parents": parents,
    }
    if scene.floor is not None:
        evidence["bottom_on_floor"] = abs(min(g.mn[u] for g in stack) - scene.floor_top) <= 0.05 * height + 1e-4
    if floats:
        evidence["rows_not_touching_row_below"] = floats
    camera = scene.aimed_camera()
    faces_camera = None
    if camera is not None:
        horizontal = abs(camera.fwd[depth_axis]) >= abs(camera.fwd[row_axis])
        faces_camera = horizontal
        evidence["faces_camera"] = faces_camera
    problems = []
    if len(stack) != sum(EXPECTED_ROWS):
        problems.append(f"the homework needs {sum(EXPECTED_ROWS)} cubes")
    if counts != EXPECTED_ROWS:
        problems.append(f"the rows from the bottom are {'-'.join(map(str, counts))} instead of 4-3-2-1")
    if not single_layer:
        problems.append("the stack is more than one cube deep (that is a pyramid, not a front-facing stack)")
    if floats:
        problems.append("row " + ", ".join(map(str, floats)) + " does not sit on the row below")
    if faces_camera is False:
        problems.append("the camera sees the side of the stack, not its front")
    if not problems:
        return "pass", evidence, "Ten cubes stacked 4-3-2-1 in one front-facing layer."
    detail = f"Found {len(stack)} touching cubes: " + "; ".join(problems) + "."
    return "partial", evidence, detail


def _check_camera(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    if not scene.cameras:
        return "fail", {"cameras": []}, "There is no camera in the scene yet."
    center = scene.stack_center()
    if center is None:
        return "unknown", {"cameras": [cam.path for cam in scene.cameras]}, (
            "A camera exists, but no cube stack was found to aim at, so the aim cannot be judged."
        )
    aims = scene.camera_aims(center)
    evidence = {
        "stack_center": [_r(v) for v in center],
        "cameras": [
            {
                "camera": item["camera"].path,
                "position": [_r(v) for v in item["camera"].pos],
                "forward": [_r(v) for v in item["camera"].fwd],
                "angle_to_stack_deg": _r(item["angle"], 1),
            }
            for item in aims
        ],
    }
    best = aims[0]
    name = _leaf(best["camera"].path)
    if best["angle"] <= AIM_PASS_DEG:
        return "pass", evidence, f"{name} looks at the stack (off by {best['angle']:.0f} degrees)."
    if best["angle"] <= AIM_PARTIAL_DEG:
        return "partial", evidence, f"{name} is close but {best['angle']:.0f} degrees off the stack; aim within {AIM_PASS_DEG:g}."
    return "fail", evidence, f"No camera looks at the stack; the closest, {name}, is {best['angle']:.0f} degrees off."


def _key_light(scene: _Scene) -> tuple[_Emitter | None, str, list[dict[str, Any]]]:
    """The light that best covers the stack, with 'aimed', 'near' (25-45 deg) or 'global' (distant/dome) quality."""
    center = scene.stack_center()
    records = []
    best: tuple[int, float, _Emitter | None, str] = (99, 0.0, None, "")
    if center is None:
        return None, "", records
    stack_size = max(max(g.mx[i] for g in scene.stack) - min(g.mn[i] for g in scene.stack) for i in range(3))
    radius = max(2.0 * stack_size, 0.25 * scene.room_span)
    for light in scene.lights:
        record = {"light": light.path, "type": light.type, "position": [_r(v) for v in light.pos]}
        rank, value, quality = 99, 0.0, ""
        if _is_directional(light):
            angle = _angle(light.fwd, _sub(center, light.pos))
            record["angle_to_stack_deg"] = _r(angle, 1)
            if angle <= AIM_PASS_DEG:
                rank, value, quality = 0, angle, "aimed"
            elif angle <= AIM_PARTIAL_DEG:
                rank, value, quality = 2, angle, "near"
        elif light.type in LOCAL_LIGHTS:
            distance = _norm(_sub(center, light.pos))
            record["distance_to_stack"] = _r(distance)
            if distance <= radius:
                rank, value, quality = 1, distance, "aimed"
        elif light.type in ("DistantLight", "DomeLight"):
            rank, value, quality = 3, 0.0, "global"
        record["quality"] = quality or "misses"
        records.append(record)
        if rank < best[0] or (rank == best[0] and value < best[1]):
            best = (rank, value, light, quality)
    return best[2], best[3], records


def _check_key_light(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    if not scene.lights:
        return "fail", {"lights": []}, "There are no visible lights with intensity above zero."
    if scene.stack_center() is None:
        return "unknown", {"lights": [light.path for light in scene.lights]}, (
            "Lights exist, but no cube stack was found, so their aim cannot be judged."
        )
    light, quality, records = _key_light(scene)
    evidence = {"lights": records, "key_light": light.path if light else None}
    if light is None:
        return "fail", evidence, "None of the lights points at the stack."
    name = _leaf(light.path)
    if quality == "aimed":
        return "pass", evidence, f"{name} is aimed at the stack."
    if quality == "near":
        return "partial", evidence, f"{name} points near the stack but more than {AIM_PASS_DEG:g} degrees off."
    return "partial", evidence, f"Only a {light.type} lights the stack; add a Rect light aimed at it."


def _check_extra_lights(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    key, quality, _records = _key_light(scene) if scene.stack else (None, "", [])
    extras = [light for light in scene.lights if light.type in EXTRA_LIGHT_TYPES and light is not key]
    evidence: dict[str, Any] = {
        "key_light": key.path if key else None,
        "extra_rect_or_sphere_lights": [light.path for light in extras],
    }
    if not scene.shelves:
        evidence["shelves"] = []
        if extras:
            return "unknown", evidence, "Extra lights exist, but no shelves were found to check the dark shelf areas."
        return "fail", evidence, "There are no extra Rect or Sphere lights yet."
    u, h = scene.u, scene.h
    lit, dark = [], []
    for shelf in scene.shelves:
        above = [s for s in scene.shelves if s is not shelf and s.mn[u] > shelf.mx[u] and _overlap_2d(s, shelf, h)]
        lid = min((s.mn[u] for s in above), default=None)
        target = tuple(shelf.center[i] if i != u else shelf.mx[u] for i in range(3))
        radius = max(1.5 * max(shelf.size[i] for i in h), 0.15 * scene.room_span)
        hitting = []
        for light in scene.lights:
            if lid is not None and light.type in LOCAL_LIGHTS and light.pos[u] >= lid:
                continue  # the shelf above blocks it
            ok, _angle_deg = scene.light_on_target(light, target, radius, SHELF_AIM_DEG)
            if ok:
                hitting.append(light.path)
            elif lid is None and light.pos[u] > shelf.mx[u] and light.type in LOCAL_LIGHTS | {"DistantLight", "DomeLight"}:
                hitting.append(light.path)  # nothing above this shelf: overhead light reaches it
        entry = {"shelf": shelf.path, "covered_by_shelf_above": lid is not None, "lit_by": hitting}
        (lit if hitting else dark).append(entry)
    evidence["shelves"] = lit + dark
    if extras and not dark:
        return "pass", evidence, f"{len(extras)} extra Rect/Sphere light(s), and every shelf gets light."
    dark_names = ", ".join(_leaf(entry["shelf"]) for entry in dark)
    if extras:
        return "partial", evidence, f"Extra lights exist, but no light reaches {dark_names}."
    if not dark:
        return "partial", evidence, "Every shelf gets some light, but there is no extra Rect or Sphere light."
    return "fail", evidence, f"No extra Rect or Sphere lights, and {dark_names} stays dark."


def _check_names(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    relevant = [prim for prim in scene.named if prim.IsA(UsdGeom.Xformable) or str(prim.GetTypeName()).endswith("Light")]
    generic = [str(prim.GetPath()) for prim in relevant if GENERIC_NAME.match(prim.GetName())]
    evidence = {"checked": len(relevant), "default_looking_names": generic[:30]}
    if not relevant:
        return "unknown", evidence, "The scene has no prims to check names on."
    if not generic:
        return "pass", evidence, "All scene parts have descriptive names."
    share = len(generic) / len(relevant)
    examples = ", ".join(_leaf(path) for path in generic[:4])
    if share <= 0.25:
        return "partial", evidence, f"{len(generic)} prim(s) still have default names, for example {examples}."
    return "fail", evidence, f"Most parts still have default names ({len(generic)} of {len(relevant)}), for example {examples}."


def _check_grouping(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    members = {
        "environment": [g.path for g in ([scene.floor] if scene.floor else []) + scene.walls],
        "shelves": [g.path for g in scene.shelves],
        "lights": [light.path for light in scene.lights],
    }
    everything = [g.path for g in scene.geos] + [e.path for e in scene.lights + scene.cameras]
    if not everything or not any(members.values()):
        return "unknown", {}, "No floor, walls, shelves or lights were found to check grouping."
    top = _common_ancestor(everything)
    words = {"environment": ENV_WORDS, "shelves": SHELF_WORDS, "lights": LIGHT_WORDS}
    groups: dict[str, Any] = {}
    score = 0.0
    env_group = None
    for key in ("environment", "shelves", "lights"):
        paths = members[key]
        if not paths:
            groups[key] = {"group": None, "status": "missing parts"}
            continue
        lca = _common_ancestor(paths)
        if lca in paths:
            lca = lca.rsplit("/", 1)[0] or "/"
        prim = scene.stage.GetPrimAtPath(lca) if lca != "/" else None
        grouped = (
            prim is not None and prim.IsValid() and lca != "/" and lca != top and lca != scene.default_prim
            and len(lca) > len(top) and (key != "shelves" or lca != env_group)
        )
        if key == "environment" and grouped:
            env_group = lca
        type_name = str(prim.GetTypeName()) if prim is not None and prim.IsValid() else ""
        named = bool(words[key].search(lca.rsplit("/", 1)[-1]))
        if grouped and type_name == "Xform" and named:
            status, credit = "ok", 1.0
        elif grouped:
            status, credit = ("grouped, but the group name is unclear" if type_name == "Xform" else f"grouped under a {type_name or 'typeless prim'}, not an Xform"), 0.5
        else:
            status, credit = "not grouped", 0.0
        score += credit
        groups[key] = {"group": lca if grouped else None, "type": type_name if grouped else None, "status": status, "members": len(paths)}
    evidence = {"top_container": top, "groups": groups}
    present = [key for key in groups if groups[key]["status"] != "missing parts"]
    ok = [key for key in present if groups[key]["status"] == "ok"]
    if present and len(ok) == len(present) and len(present) == 3:
        return "pass", evidence, "Environment, shelves and lights each have their own Xform group."
    if score > 0:
        todo = [key for key in ("environment", "shelves", "lights") if groups[key]["status"] != "ok"]
        return "partial", evidence, "Some groups are done; still to group or rename: " + ", ".join(todo) + "."
    return "fail", evidence, "Environment, shelves and lights are not grouped under their own Xforms yet."


def _check_static(scene: _Scene) -> tuple[str, dict[str, Any], str]:
    evidence = {"animated_transforms": scene.animated[:20], "rigid_bodies": scene.rigid[:20]}
    if scene.animated:
        return "partial", evidence, f"{len(scene.animated)} prim(s) have animated transforms; the homework is a still scene."
    if scene.rigid:
        return "pass", evidence, "Nothing is animated. Rigid bodies exist, but they only move if you press Play, which isn't needed."
    return "pass", evidence, "Nothing is animated, so the scene is static."


_CHECKERS = {
    "floor": _check_floor,
    "three_walls": _check_three_walls,
    "open_front_top": _check_open_front_top,
    "two_shelves_on_wall": _check_shelves,
    "objects_on_shelves": _check_shelf_objects,
    "ten_cube_stack": _check_stack,
    "camera_aimed": _check_camera,
    "key_light_aimed": _check_key_light,
    "extra_lights_shelves": _check_extra_lights,
    "clear_names": _check_names,
    "grouping": _check_grouping,
    "static_scene": _check_static,
}


# ------------------------------------------------------------------------------------------------ small math


def _is_light(prim: Any, type_name: str) -> bool:
    if type_name.endswith("LightFilter") or type_name == "PortalLight":
        return False
    if type_name.endswith("Light"):
        return True
    try:
        return "LightAPI" in prim.GetAppliedSchemas()
    except Exception:  # pragma: no cover
        return False


def _is_directional(light: _Emitter) -> bool:
    """Rect and Disk lights emit along local -Z; a Sphere light with a shaping cone under 90 degrees acts as a spot."""
    return light.type in ("RectLight", "DiskLight") or (light.type in LOCAL_LIGHTS and light.cone < 90)


def _float_attr(prim: Any, names: tuple[str, ...], default: float) -> float:
    for name in names:
        attr = prim.GetAttribute(name)
        if attr and attr.IsValid():
            value = attr.Get()
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
    return default


def _sub(p: tuple[float, ...], q: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(x - y for x, y in zip(p, q))


def _norm(v: tuple[float, ...]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _unit(v: tuple[float, ...]) -> tuple[float, ...]:
    length = _norm(v)
    return tuple(x / length for x in v) if length > 0 else v


def _angle(v: tuple[float, ...], w: tuple[float, ...]) -> float:
    nv, nw = _norm(v), _norm(w)
    if nv == 0 or nw == 0:
        return 180.0
    cosine = max(-1.0, min(1.0, sum(x * y for x, y in zip(v, w)) / (nv * nw)))
    return math.degrees(math.acos(cosine))


def _axis_gap(p: _Geo, q: _Geo, k: int) -> float:
    return max(0.0, p.mn[k] - q.mx[k], q.mn[k] - p.mx[k])


def _overlap_2d(p: _Geo, q: _Geo, axes: tuple[int, int]) -> bool:
    return all(p.mn[k] < q.mx[k] and q.mn[k] < p.mx[k] for k in axes)


def _inside_footprint(point: tuple[float, ...], g: _Geo, axes: tuple[int, int]) -> bool:
    return all(g.mn[k] - 1e-6 <= point[k] <= g.mx[k] + 1e-6 for k in axes)


def _touching_top(upper: _Geo, lower: _Geo, u: int, span: float) -> bool:
    return abs(upper.mn[u] - lower.mx[u]) <= max(0.05 * upper.size[u], 0.002 * span, 1e-4)


def _center_of(geos: list[_Geo]) -> tuple[float, ...]:
    return tuple((min(g.mn[i] for g in geos) + max(g.mx[i] for g in geos)) / 2 for i in range(3))


def _dist2(p: tuple[float, ...], q: tuple[float, ...], axes: tuple[int, int]) -> float:
    return sum((p[k] - q[k]) ** 2 for k in axes)


def _common_ancestor(paths: list[str]) -> str:
    split = [path.strip("/").split("/") for path in paths]
    common: list[str] = []
    for parts in zip(*split):
        if all(part == parts[0] for part in parts):
            common.append(parts[0])
        else:
            break
    return "/" + "/".join(common) if common else "/"


def _leaf(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits) + 0.0
