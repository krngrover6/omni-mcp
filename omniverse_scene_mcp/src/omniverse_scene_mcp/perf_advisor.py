"""Performance report and instancing advisor for big scenes (factory digital twins with thousands of repeated parts).

perf_report(stage) is a pure, read-only pxr pass that answers "why is my scene slow and what do I change first?":
totals (prims by type, meshes, triangles stored and drawn, points, materials, textures, lights, cameras, instancing,
point instancers, payloads, layers, time samples), hotspots (heavy meshes, deep hierarchies, many lights, huge
textures, many unique / duplicate materials, dense subtrees), instancing groups (repeated copies of one asset that
are not instanced, pasted identical meshes, what is already instanced) with an estimate of what instancing would
save, payload candidates, light advice, a 0-100 score and a plain summary.

make_instanceable(stage, paths) sets instanceable = true on prims that reference / payload an asset. Dry run by
default (nothing is written); it refuses prims without a reference or payload, instance proxies, prototypes and,
above all, prims whose children carry local edits that instancing would silently discard (those edits are listed).
A real write snapshots the edit target layer first (undo.SpecSnapshot) and rolls back if any prim does not become an
instance. convert_to_payload(stage, paths) only PLANS a reference -> payload conversion (steps and a Python snippet).

live_frame_stats() runs inside Kit only (lazy omni/carb imports) and reads the viewport FPS, frame time, GPU memory
and the app loop rate from APIs present in the installed Kit 110 / Isaac Sim 6.1.0:
  - omni.kit.widget.viewport api.py: ViewportAPI.fps -> frame_info["fps"]; frame_info["subframe_count"],
    frame_info["device_mask"] (extscache/omni.kit.widget.viewport-110.0.0/omni/kit/widget/viewport/api.py:323-330)
  - the viewport HUD computes "FPS x subframe_count" and "1000 / fps ms" the same way
    (extscache/omni.kit.viewport.window-110.0.1/omni/kit/viewport/window/stats/__init__.py:297-322)
  - GPU memory: omni.hydra.engine.stats.get_device_info() -> [{description, budget, usage}], as the HUD's
    ViewportMemoryStat uses it (same stats/__init__.py:236-261; omni.hydra.engine.stats-1.0.5 tests/test_bindings.py)
  - app loop: omni.kit.app.get_app().get_update_number() / get_time_since_start_s() (kit/kernel/py/omni/kit/app/_app.pyi)
  - frame cap: carb setting /app/runLoops/main/rateLimitEnabled + rateLimitFrequency (apps/isaacsim.exp.*.kit)
Without Kit it returns {"ok": False, "reason": ...}.

Reuse: thresholds and helpers come from asset_health (DEEP_HIERARCHY, LARGE_TEXTURE_*, REFERENCE_MIN_COPIES,
IDENTICAL_MESH_MIN_COPIES, _arc_key, _image_size, _extension, _short_path, _jsonable), time budgets from usd_insight,
edit-target checks from authoring, spec snapshots from undo and the lazy Kit import helper from kit_live_extras.
Uses only APIs present in USD 25.11 (Kit) and usd-core 26.8.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections import Counter
from typing import Any

from . import asset_health, usd_validate
from .asset_health import (
    DEEP_HIERARCHY,
    IDENTICAL_MESH_MIN_COPIES,
    IMAGE_EXTENSIONS,
    LARGE_TEXTURE_BYTES,
    LARGE_TEXTURE_PIXELS,
    MAX_MESH_READS,
    MAX_TEXTURE_FILES,
    REFERENCE_MIN_COPIES,
)
from .usd_insight import TimeBudget, resolve_budget

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Sdf = Usd = UsdGeom = UsdShade = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

__all__ = [
    "DEFAULT_MAX_PRIMS",
    "convert_to_payload",
    "live_frame_stats",
    "make_instanceable",
    "perf_report",
]

DEFAULT_MAX_PRIMS = 200_000
HEAVY_MESH_TRIANGLES = 250_000
MANY_LIGHTS = 50
MANY_UNIQUE_MATERIALS = 150
DUPLICATE_MATERIALS_MIN = 10
DENSE_SUBTREE_SHARE = 0.5
DENSE_SUBTREE_TRIANGLES = 2_000_000
DENSE_SUBTREE_PRIMS = 20_000
PAYLOAD_MIN_TRIANGLES = 250_000
PAYLOAD_MIN_PRIMS = 2_000
POINT_INSTANCER_MIN_COPIES = 500
POINT_INSTANCER_MAX_TRIANGLES = 2_000
EXACT_FACE_COUNT_LIMIT = 262_144  # faceVertexCounts longer than this are sampled
TRIANGLE_SAMPLES = 65_536
PI_INDEX_EXACT_LIMIT = 1_000_000
MAX_BINDING_READS = 50_000
MAX_MATERIAL_SIGNATURES = 5_000
MAX_SUBTREE_PRIMS = 20_000  # per point-instancer prototype measured afterwards
MAX_GROUPS = 20
MAX_HOTSPOTS_PER_KIND = 5
MAX_EXAMPLE_PATHS = 5
MAX_EDIT_CHECKS_PER_GROUP = 50
MAX_PATHS = 5_000
MAX_EDITS_LISTED = 20
MAX_EDIT_SPECS = 20_000
_POINT_SAMPLES = 32
_BUCKET_LEVELS = 3

# hotspot kinds in the order a beginner should look at them
_HOTSPOT_PRIORITY = {"heavy mesh": 0, "dense subtree": 1, "many lights": 2, "huge texture": 3,
                     "many unique materials": 4, "deep hierarchy": 5}

APPROACH_SCENEGRAPH ="scenegraph instancing: set instanceable on the referencing prims"
APPROACH_POINT_INSTANCER = "PointInstancer for many simple copies"
APPROACH_ALREADY = "already instanced"

_INSTANCING_GOTCHA = (
    "Instancing shares one prototype between all copies, so edits on the CHILDREN of a copy (a moved part, a "
    "different material on one sub-mesh) are ignored once it is instanced. Per-copy changes must go on the copy's "
    "root prim (its transform, visibility, a material bound on the root, primvars it inherits)."
)


# ================================================================================================= perf_report


def perf_report(
    stage: Any,
    *,
    root_path: str | None = None,
    deadline: Any = None,
    budget_ms: float | None = None,
    max_prims: int = DEFAULT_MAX_PRIMS,
) -> dict[str, Any]:
    """Read-only performance report for an opened Usd.Stage (or the subtree at root_path). Never authors anything.

    deadline: a time.monotonic() value or a usd_insight.TimeBudget; budget_ms and an ambient TimeBudget also apply.
    A cut-short run reports truncated / truncated_by_time and covers only what it reached; max_prims caps the prims
    visited (scene plus instancing prototypes).
    """
    if _PXR_IMPORT_ERROR is not None:
        return {"ok": False, "error": f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}"}
    if stage is None or not hasattr(stage, "GetRootLayer"):
        return {"ok": False, "error": "perf_report needs an opened Usd.Stage."}
    try:
        budget = resolve_budget(deadline, budget_ms, now=time.monotonic())
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"Invalid time budget: {exc}"}
    try:
        max_prims = max(1, min(int(max_prims or DEFAULT_MAX_PRIMS), 10_000_000))
    except (TypeError, ValueError):
        return {"ok": False, "error": "max_prims must be a whole number."}
    root = stage.GetPseudoRoot()
    if root_path not in (None, "", "/"):
        try:
            root = stage.GetPrimAtPath(Sdf.Path(str(root_path)))
        except Exception:
            root = None
        if not root:
            return {"ok": False, "error": f"No prim at {root_path!r} on this stage."}
    started = time.perf_counter()
    dirty_before = {layer.identifier for layer in stage.GetUsedLayers() if layer.dirty}
    scan = _Scan(stage, root, budget, max_prims)
    with usd_validate._captured_diagnostics(scan.notes, "while measuring performance"):
        scan.run()
    dirty_after = {layer.identifier for layer in stage.GetUsedLayers() if layer.dirty}
    if dirty_after - dirty_before:  # pragma: no cover - would be a bug here
        scan.notes.append(f"Unexpected: layers became dirty during the report: {sorted(dirty_after - dirty_before)}")
    return scan.result(time.perf_counter() - started)


class _Node:
    """A prim whose subtree totals matter: a referencing prim (arc root), a point-instancer prototype or a bucket."""

    __slots__ = ("path", "tris", "prims", "arc", "pi_proto", "bucket", "is_mesh", "key", "payload")

    def __init__(self, path: Any) -> None:
        self.path = path
        self.tris = 0
        self.prims = 0
        self.arc = False
        self.pi_proto = False
        self.bucket = False
        self.is_mesh = False
        self.key: tuple = ()
        self.payload = False


class _Scan:
    def __init__(self, stage: Any, root: Any, budget: TimeBudget | None, max_prims: int) -> None:
        self.stage = stage
        self.root = root
        self.root_path = root.GetPath()
        self.root_depth = 0 if root.IsPseudoRoot() else self.root_path.pathElementCount
        self.budget = budget
        self.max_prims = max_prims
        self.notes: list[str] = []
        self.visited = 0
        self.stop: str | None = None  # "time" | "max_prims"
        self.stopped_phases: list[str] = []
        self.sampled_triangles = False
        self.meshes_measured = 0
        self.meshes_unmeasured = 0
        # totals
        self.types: Counter[str] = Counter()
        self.counts: Counter[str] = Counter()
        self.lights: Counter[str] = Counter()
        self.light_paths: list[Any] = []
        self.dome_lights: list[dict[str, Any]] = []
        self.tris_stored = 0
        self.tris_rendered = 0
        self.points = 0
        self.max_depth = 0
        # collections
        self.mesh_tris: dict[Any, int] = {}
        self.heavy: list[tuple[int, str, int]] = []  # (triangles, display path, copies)
        self.deep: dict[str, list[Any]] = {}  # branch -> [count, max depth, deepest path]
        self.arc_nodes: list[_Node] = []
        self.buckets: list[_Node] = []
        self.identical: dict[tuple, list[str]] = {}
        self.identical_tris: dict[tuple, int] = {}
        # (instance path, prototype path, ancestors; None = inside a point-instancer prototype)
        self.instances: list[tuple[Any, Any, list[_Node] | None]] = []
        self.instances_by_proto: dict[Any, list[str]] = {}
        self.pi_records: list[dict[str, Any]] = []
        self.pi_targets: set[Any] = set()
        self.gprims: list[Any] = []
        self.materials: list[Any] = []
        self.textures: dict[str, dict[str, Any]] = {}
        self.proto_direct: dict[Any, list[int]] = {}  # prototype -> [triangles, prims]
        self.proto_children: dict[Any, list[Any]] = {}
        self.proto_total_memo: dict[Any, tuple[int, int]] = {}
        self.time_sampled = 0
        self.check_time_samples = True
        self.bound_unique = 0
        self.bound_checked = 0
        self.duplicate_materials: list[dict[str, Any]] = []
        self.texture_stats: dict[str, Any] = {}
        self.payloads = {"loaded": 0, "unloaded": 0, "unloaded_examples": []}
        self.prototype_prims = 0
        self.no_effect_examples: list[str] = []

    # ------------------------------------------------------------------------------------------------- driving

    def tick(self) -> bool:
        if self.stop is not None:
            return True
        if self.visited >= self.max_prims:
            self.stop = "max_prims"
            return True
        if self.budget is not None and self.budget.tick():
            self.stop = "time"
            return True
        return False

    def run(self) -> None:
        self.check_time_samples = self._layers_have_time_samples()
        self._main_pass()
        self._prototype_pass()
        self._point_instancers()
        self._credit_instances()
        for phase, fn in (("materials", self._materials), ("textures", self._texture_facts), ("payloads", self._payloads)):
            if self.budget is not None and self.budget.expired():
                self.stopped_phases.append(phase)
                continue
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - one phase must not hide the rest
                self.notes.append(asset_health._clip(f"{phase} could not be measured: {type(exc).__name__}: {exc}"))

    def _layers_have_time_samples(self) -> bool:
        try:
            return any(layer.ListAllTimeSamples() for layer in self.stage.GetUsedLayers())
        except Exception:  # noqa: BLE001
            return True

    # ----------------------------------------------------------------------------------------------- main pass

    def _main_pass(self) -> None:
        prims = self.stage.Traverse() if self.root.IsPseudoRoot() else Usd.PrimRange(self.root)
        stack: list[_Node] = []
        for prim in prims:
            if self.tick():
                break
            self.visited += 1
            try:
                self._visit_main(prim, stack)
            except Exception as exc:  # noqa: BLE001 - one odd prim must not end the pass
                if len(self.notes) < 40:
                    self.notes.append(asset_health._clip(f"Skipped {prim.GetPath()}: {type(exc).__name__}: {exc}"))

    def _visit_main(self, prim: Any, stack: list[_Node]) -> None:
        path = prim.GetPath()
        while stack and not path.HasPrefix(stack[-1].path):
            stack.pop()
        depth = path.pathElementCount
        rel_depth = depth - self.root_depth
        type_name = str(prim.GetTypeName()) or "(typeless)"
        self.types[type_name] += 1
        if depth > self.max_depth:
            self.max_depth = depth
        if depth > DEEP_HIERARCHY:
            branch = str(path.GetPrefixes()[DEEP_HIERARCHY - 1])
            entry = self.deep.setdefault(branch, [0, 0, ""])
            entry[0] += 1
            if depth > entry[1]:
                entry[1], entry[2] = depth, str(path)
        is_instance = prim.IsInstance()
        instanceable = prim.IsInstanceable()
        if instanceable:
            self.counts["instanceable"] += 1
            if not is_instance:
                self.counts["instanceable_without_effect"] += 1
                if len(self.no_effect_examples) < MAX_EXAMPLE_PATHS:
                    self.no_effect_examples.append(str(path))
        has_refs = prim.HasAuthoredReferences()
        has_payloads = prim.HasAuthoredPayloads()
        node = None
        if (has_refs or has_payloads) and not is_instance and not instanceable:
            node = _Node(path)
            node.arc = True
            node.key = asset_health._arc_key(prim)
            node.payload = has_payloads
            self.arc_nodes.append(node)
        if path in self.pi_targets:
            node = node or _Node(path)
            node.pi_proto = True
        if 1 <= rel_depth <= _BUCKET_LEVELS:
            node = node or _Node(path)
            node.bucket = True
            self.buckets.append(node)
        if node is not None:
            stack.append(node)
        for item in stack:
            item.prims += 1
        in_arc = any(item.arc for item in stack)
        pi_index = next((index for index, item in enumerate(stack) if item.pi_proto), None)

        if is_instance:
            self.counts["instances"] += 1
            prototype = prim.GetPrototype()
            if prototype:
                proto_path = prototype.GetPath()
                self.instances.append((path, proto_path, list(stack) if pi_index is None else None))
                self.instances_by_proto.setdefault(proto_path, []).append(str(path))
        if prim.IsA(UsdGeom.Gprim):
            self.counts["gprims"] += 1
            self.gprims.append(prim)
        if type_name == "Mesh" or (type_name != "(typeless)" and prim.IsA(UsdGeom.Mesh)):
            if node is not None:
                node.is_mesh = True
            tris = self._mesh(prim, str(path), copies=1, sign=not in_arc and pi_index is None)
            if pi_index is None:
                self.tris_rendered += tris
                for item in stack:
                    item.tris += tris
            else:
                for item in stack[pi_index:]:
                    item.tris += tris
        elif prim.IsA(UsdGeom.PointInstancer):
            self._record_point_instancer(prim, list(stack) if pi_index is None else [], owner=None)
        elif prim.IsA(UsdShade.Material):
            self.materials.append(prim)
        elif prim.IsA(UsdShade.Shader):
            self._shader_textures(prim)
        elif prim.IsA(UsdGeom.Camera):
            self.counts["cameras"] += 1
        if _is_light(prim):
            self._light(prim, type_name)
        if self.check_time_samples:
            self._time_samples(prim)

    # ------------------------------------------------------------------------------------------------- meshes

    def _mesh(self, prim: Any, display: str, *, copies: int, sign: bool) -> int:
        self.counts["meshes"] += 1
        if self.meshes_measured >= MAX_MESH_READS:
            self.meshes_unmeasured += 1
            self.sampled_triangles = True
            return 0
        self.meshes_measured += 1
        mesh = UsdGeom.Mesh(prim)
        counts = mesh.GetFaceVertexCountsAttr().Get()
        tris, estimated = _triangles(counts)
        if estimated:
            self.sampled_triangles = True
        points_attr = mesh.GetPointsAttr()
        points = points_attr.Get()
        n_points = len(points) if points is not None else 0
        faces = len(counts) if counts is not None else 0
        self.points += n_points
        self.tris_stored += tris
        self.mesh_tris[prim.GetPath()] = tris
        if tris >= HEAVY_MESH_TRIANGLES:
            self.heavy.append((tris, display, copies))
        if sign and n_points and not points_attr.GetNumTimeSamples():
            step = max(1, n_points // _POINT_SAMPLES)
            sample = tuple(round(float(c), 4) for i in range(0, n_points, step)[:_POINT_SAMPLES] for c in points[i])
            key = (n_points, faces, sample)
            self.identical.setdefault(key, []).append(display)
            self.identical_tris[key] = tris
        return tris

    # -------------------------------------------------------------------------------------- point instancers

    def _record_point_instancer(self, prim: Any, ancestors: list[_Node], owner: Any) -> None:
        instancer = UsdGeom.PointInstancer(prim)
        indices = instancer.GetProtoIndicesAttr().Get()
        count = len(indices) if indices is not None else 0
        targets = list(instancer.GetPrototypesRel().GetForwardedTargets())
        self.pi_targets.update(targets)
        if count <= PI_INDEX_EXACT_LIMIT:
            per_proto = Counter(int(i) for i in indices) if count else Counter()
            estimated = False
        else:
            step = max(1, count // TRIANGLE_SAMPLES)
            sample = indices[::step]
            factor = count / max(1, len(sample))
            per_proto = Counter()
            for index, n in Counter(int(i) for i in sample).items():
                per_proto[index] = int(round(n * factor))
            estimated = True
        self.counts["point_instancers"] += 1
        self.counts["point_instances"] += count
        self.pi_records.append({"path": prim.GetPath(), "targets": targets, "per_proto": per_proto, "count": count,
                                "ancestors": ancestors, "owner": owner, "estimated": estimated, "expansion": 0,
                                "unique": 0})

    def _point_instancers(self) -> None:
        memo: dict[Any, int] = {}
        for record in self.pi_records:
            expansion = unique = 0
            for index, target in enumerate(record["targets"]):
                if target not in memo:
                    memo[target] = self._subtree_triangles(target)
                unique += memo[target]
                expansion += memo[target] * record["per_proto"].get(index, 0)
            record["expansion"], record["unique"] = expansion, unique
            if record["owner"] is not None:
                self.proto_direct.setdefault(record["owner"], [0, 0])[0] += expansion
                continue
            self.tris_rendered += expansion
            for item in record["ancestors"]:
                item.tris += expansion

    def _subtree_triangles(self, path: Any) -> int:
        prim = self.stage.GetPrimAtPath(path)
        if not prim:
            return 0
        total = 0
        predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
        for seen, item in enumerate(Usd.PrimRange(prim, predicate)):
            if seen >= MAX_SUBTREE_PRIMS or (self.budget is not None and self.budget.tick()):
                self.notes.append(f"Point-instancer prototype {path} was only partly measured.")
                break
            if not item.IsA(UsdGeom.Mesh):
                continue
            cached = self.mesh_tris.get(item.GetPath())
            if cached is None:
                cached, estimated = _triangles(UsdGeom.Mesh(item).GetFaceVertexCountsAttr().Get())
                self.sampled_triangles |= estimated
            total += cached
        return total

    # ------------------------------------------------------------------------------------------- prototypes

    def _prototype_pass(self) -> None:
        queue = list(dict.fromkeys(proto for _p, proto, _a in self.instances))
        done: set[Any] = set()
        while queue and self.stop is None:
            proto_path = queue.pop(0)
            if proto_path in done:
                continue
            done.add(proto_path)
            prototype = self.stage.GetPrimAtPath(proto_path)
            if not prototype:
                continue
            direct = self.proto_direct.setdefault(proto_path, [0, 0])
            children = self.proto_children.setdefault(proto_path, [])
            first_instance = (self.instances_by_proto.get(proto_path) or [None])[0]
            for prim in Usd.PrimRange(prototype):
                if prim.GetPath() == proto_path:
                    continue
                if self.tick():
                    break
                self.visited += 1
                self.prototype_prims += 1
                direct[1] += 1
                try:
                    path = prim.GetPath()
                    display = str(path.ReplacePrefix(proto_path, Sdf.Path(first_instance))) if first_instance else str(path)
                    if prim.IsInstance():
                        nested = prim.GetPrototype()
                        if nested:
                            children.append(nested.GetPath())
                            queue.append(nested.GetPath())
                    if prim.IsA(UsdGeom.Gprim):
                        self.counts["gprims"] += 1
                        self.gprims.append(prim)
                    if prim.IsA(UsdGeom.Mesh):
                        copies = len(self.instances_by_proto.get(proto_path, [])) or 1
                        direct[0] += self._mesh(prim, display, copies=copies, sign=False)
                    elif prim.IsA(UsdGeom.PointInstancer):
                        self._record_point_instancer(prim, [], owner=proto_path)
                    elif prim.IsA(UsdShade.Material):
                        self.materials.append(prim)
                    elif prim.IsA(UsdShade.Shader):
                        self._shader_textures(prim)
                    if _is_light(prim):
                        self._light(prim, str(prim.GetTypeName()) or "LightAPI")
                    if self.check_time_samples:
                        self._time_samples(prim)
                except Exception as exc:  # noqa: BLE001
                    if len(self.notes) < 40:
                        self.notes.append(asset_health._clip(f"Skipped {prim.GetPath()}: {type(exc).__name__}: {exc}"))
        self.counts["prototypes"] = len(done)

    def _proto_total(self, proto_path: Any, guard: frozenset = frozenset()) -> tuple[int, int]:
        if proto_path in self.proto_total_memo:
            return self.proto_total_memo[proto_path]
        if proto_path in guard:
            return (0, 0)
        tris, prims = self.proto_direct.get(proto_path, [0, 0])
        for child in self.proto_children.get(proto_path, []):
            child_tris, child_prims = self._proto_total(child, guard | {proto_path})
            tris += child_tris
            prims += child_prims
        self.proto_total_memo[proto_path] = (tris, prims)
        return tris, prims

    def _credit_instances(self) -> None:
        for _path, proto_path, ancestors in self.instances:
            if ancestors is None:  # inside a point-instancer prototype: drawn through the instancer's expansion
                continue
            tris, prims = self._proto_total(proto_path)
            self.tris_rendered += tris
            for item in ancestors:
                item.tris += tris
                item.prims += prims

    # --------------------------------------------------------------------------------- lights, time samples

    def _light(self, prim: Any, type_name: str) -> None:
        self.lights[type_name] += 1
        self.light_paths.append(prim.GetPath())
        attr = prim.GetAttribute("inputs:texture:file")  # DomeLight HDR, or a RectLight's texture
        key = self._texture(attr.Get(), str(prim.GetPath())) if attr and attr.HasAuthoredValue() else None
        if type_name.startswith("DomeLight"):
            self.dome_lights.append({"path": str(prim.GetPath()), "texture": key})

    def _time_samples(self, prim: Any) -> None:
        for attr in prim.GetAuthoredAttributes():
            if attr.GetNumTimeSamples():
                self.time_sampled += 1

    # -------------------------------------------------------------------------------------------- textures

    def _shader_textures(self, prim: Any) -> None:
        for shader_input in UsdShade.Shader(prim).GetInputs():
            attr = shader_input.GetAttr()
            if attr.GetTypeName() != Sdf.ValueTypeNames.Asset or not attr.HasAuthoredValue():
                continue
            self._texture(attr.Get(), str(prim.GetPath()))

    def _texture(self, value: Any, user: str) -> str | None:
        authored = str(getattr(value, "path", "") or "")
        if not authored or asset_health._extension(authored) not in IMAGE_EXTENSIONS:
            return None
        resolved = str(getattr(value, "resolvedPath", "") or "")
        key = resolved or authored
        entry = self.textures.setdefault(key, {"authored": authored, "resolved": resolved, "users": 0, "user": user})
        entry["users"] += 1
        return key

    def _texture_facts(self) -> None:
        total_bytes = 0
        inspected = 0
        missing = 0
        formats: Counter[str] = Counter()
        largest: list[dict[str, Any]] = []
        huge: list[dict[str, Any]] = []
        for key, entry in self.textures.items():
            ext = asset_health._extension(entry["authored"])
            formats[ext or "?"] += 1
            resolved = entry["resolved"]
            if not resolved:
                missing += 1
                continue
            if "[" in resolved or "<" in entry["authored"] or inspected >= MAX_TEXTURE_FILES:
                continue
            if self.budget is not None and self.budget.tick():
                self.stopped_phases.append("textures")
                break
            inspected += 1
            try:
                size = os.stat(resolved).st_size
            except OSError:
                continue
            total_bytes += size
            dims = _texture_size(resolved, ext)
            record: dict[str, Any] = {"path": asset_health._short_path(resolved), "bytes": size, "used_by": entry["user"]}
            if dims:
                record["width"], record["height"] = dims
            entry["facts"] = record
            largest.append(record)
            if (dims and max(dims) >= LARGE_TEXTURE_PIXELS) or size >= LARGE_TEXTURE_BYTES:
                huge.append(record)
        largest.sort(key=lambda item: -item["bytes"])
        huge.sort(key=lambda item: -(item.get("width", 0) * item.get("height", 0) or item["bytes"]))
        self.texture_stats = {"count": len(self.textures), "missing": missing, "formats": dict(formats),
                              "local_files_inspected": inspected, "local_bytes": total_bytes,
                              "largest": largest[:5], "huge": huge}
        for dome in self.dome_lights:
            key = dome.get("texture")
            if key and key in self.textures:
                dome["texture"] = self.textures[key].get("facts") or {"path": asset_health._short_path(key)}

    # ------------------------------------------------------------------------------------------- materials

    def _materials(self) -> None:
        gprims = self.gprims[:MAX_BINDING_READS]
        if len(self.gprims) > MAX_BINDING_READS:
            self.notes.append(f"Material bindings were read for the first {MAX_BINDING_READS:,} gprims only.")
        bound: set[str] = set()
        if gprims:
            try:
                materials, _rels = UsdShade.MaterialBindingAPI.ComputeBoundMaterials(gprims)
                bound = {str(material.GetPath()) for material in materials if material}
            except Exception:  # noqa: BLE001 - older bindings: one prim at a time
                for prim in gprims:
                    if self.budget is not None and self.budget.tick():
                        self.stopped_phases.append("materials")
                        break
                    material, _rel = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
                    if material:
                        bound.add(str(material.GetPath()))
        self.bound_unique = len(bound)
        self.bound_checked = len(gprims)
        groups: dict[str, list[str]] = {}
        for material in self.materials[:MAX_MATERIAL_SIGNATURES]:
            if self.budget is not None and self.budget.tick():
                self.stopped_phases.append("materials")
                break
            try:
                signature = _material_signature(material)
            except Exception:  # noqa: BLE001 - an odd shader must not end the material pass
                signature = None
            if signature:
                groups.setdefault(signature, []).append(str(material.GetPath()))
        duplicates = [paths for paths in groups.values() if len(paths) > 1]
        duplicates.sort(key=len, reverse=True)
        self.duplicate_materials = [{"copies": len(paths), "examples": paths[:MAX_EXAMPLE_PATHS]} for paths in duplicates[:10]]
        self.counts["duplicate_material_extra"] = sum(len(paths) - 1 for paths in duplicates)

    # -------------------------------------------------------------------------------------------- payloads

    def _payloads(self) -> None:
        loadable = list(self.stage.FindLoadable(self.root_path))
        unloaded = []
        loaded = 0
        for path in loadable:
            prim = self.stage.GetPrimAtPath(path)
            if prim and prim.IsLoaded():
                loaded += 1
            else:
                unloaded.append(str(path))
        self.payloads = {"loaded": loaded, "unloaded": len(unloaded), "unloaded_examples": unloaded[:MAX_EXAMPLE_PATHS]}

    # ------------------------------------------------------------------------------------------- reporting

    def _instancing(self) -> dict[str, Any]:
        groups: list[dict[str, Any]] = []
        by_key: dict[tuple, list[_Node]] = {}
        for node in self.arc_nodes:
            if node.key:
                by_key.setdefault(node.key, []).append(node)
        qualifying = {key: nodes for key, nodes in by_key.items() if len(nodes) >= REFERENCE_MIN_COPIES}
        member_owner: dict[Any, tuple] = {}
        for key, nodes in qualifying.items():
            for node in nodes:
                member_owner[node.path] = key
        for key, nodes in qualifying.items():
            count = len(nodes)
            tris = int(round(sum(node.tris for node in nodes) / count))
            prims = int(round(sum(node.prims for node in nodes) / count))
            nested_in = None
            for node in nodes[:MAX_EDIT_CHECKS_PER_GROUP]:
                owners = {member_owner.get(prefix) for prefix in node.path.GetPrefixes()[:-1]} - {None, key}
                if not owners:
                    nested_in = None
                    break
                nested_in = next(iter(owners))
            sample = nodes[:MAX_EDIT_CHECKS_PER_GROUP]
            edited = sum(1 for node in sample if _local_child_edits(self.stage, node.path, limit=1)[1])
            asset = asset_health._short_path(key[0][0]) + (f"<{key[0][1]}>" if key[0][1] else "")
            simple = prims <= 3 and tris <= POINT_INSTANCER_MAX_TRIANGLES
            approach = APPROACH_POINT_INSTANCER if count >= POINT_INSTANCER_MIN_COPIES and simple else APPROACH_SCENEGRAPH
            saved = (count - 1) * tris
            arc = "payload" if all(node.payload for node in nodes) else "reference"
            plain = (f"{count} prims bring in {asset} by {arc} and none is instanced: each copy stores its own "
                     f"{prims:,} prims and {tris:,} triangles.")
            if approach == APPROACH_POINT_INSTANCER:
                plain += " They are many small, simple copies, so one PointInstancer (one prototype, an array of " \
                         "positions) is the lightest option; instanceable also works."
            else:
                plain += " Marking the referencing prims instanceable lets USD share one prototype."
            if edited:
                plain += (f" {edited} of the {len(sample)} copies checked have local edits on their children; those "
                          "edits would be ignored once instanced (make_instanceable refuses them and lists the edits).")
            group = {
                "kind": "same_reference",
                "signature": f"{arc} @{asset}",
                "asset": key[0][0],
                "count": count,
                "example_paths": [str(node.path) for node in nodes[:MAX_EXAMPLE_PATHS]],
                "triangles_per_copy": tris,
                "prims_per_copy": prims,
                "est_triangles_saved": 0 if nested_in else saved,
                "est_prims_saved": 0 if nested_in else (count - 1) * max(0, prims - 1),
                "copies_with_local_edits": edited,
                "local_edits_checked": len(sample),
                "approach": approach,
                "plain": plain,
            }
            if nested_in:
                outer = asset_health._short_path(nested_in[0][0])
                group["nested_in"] = outer
                group["plain"] += (f" These copies sit inside copies of {outer}; instancing those outer copies "
                                   "shares these too, so they are not counted in the total saving.")
            groups.append(group)
        for (n_points, faces, _sample), paths in self.identical.items():
            if len(paths) < IDENTICAL_MESH_MIN_COPIES:
                continue
            tris = self.identical_tris.get((n_points, faces, _sample), 0)
            count = len(paths)
            approach = APPROACH_POINT_INSTANCER if tris <= POINT_INSTANCER_MAX_TRIANGLES and count >= 50 else APPROACH_SCENEGRAPH
            digest = hashlib.sha1(repr(_sample).encode()).hexdigest()[:8]
            plain = (f"{count} meshes are identical copies ({n_points:,} points, {tris:,} triangles each) pasted into "
                     "the scene, not referenced.")
            if approach == APPROACH_POINT_INSTANCER:
                plain += " Replace them with one PointInstancer that holds the mesh once plus a position per copy."
            else:
                plain += (" Move the mesh into its own file, reference it at each location and mark those prims "
                          "instanceable.")
            groups.append({
                "kind": "identical_meshes",
                "signature": f"mesh {n_points} points / {faces} faces #{digest}",
                "count": count,
                "example_paths": paths[:MAX_EXAMPLE_PATHS],
                "triangles_per_copy": tris,
                "prims_per_copy": 1,
                "est_triangles_saved": (count - 1) * tris,
                "est_prims_saved": 0,
                "approach": approach,
                "plain": plain,
            })
        for proto_path, paths in self.instances_by_proto.items():
            tris, prims = self._proto_total(proto_path)
            groups.append({
                "kind": "scenegraph_instances",
                "signature": f"prototype {proto_path}",
                "count": len(paths),
                "example_paths": paths[:MAX_EXAMPLE_PATHS],
                "triangles_per_copy": tris,
                "prims_per_copy": prims + 1,
                "est_triangles_saved": 0,
                "already_saving_triangles": (len(paths) - 1) * tris,
                "approach": APPROACH_ALREADY,
                "plain": f"{len(paths)} instances already share one prototype ({tris:,} triangles stored once).",
            })
        for record in self.pi_records:
            if record["owner"] is not None:
                continue
            groups.append({
                "kind": "point_instancer",
                "signature": f"PointInstancer {record['path']}",
                "count": record["count"],
                "example_paths": [str(record["path"])],
                "prototypes": len(record["targets"]),
                "triangles_per_copy": int(record["expansion"] / record["count"]) if record["count"] else 0,
                "est_triangles_saved": 0,
                "already_saving_triangles": max(0, record["expansion"] - record["unique"]),
                "approach": APPROACH_ALREADY,
                "plain": (f"PointInstancer with {record['count']:,} instances of {len(record['targets'])} prototype(s): "
                          f"{record['expansion']:,} triangles drawn from {record['unique']:,} stored."),
            })
        groups.sort(key=lambda g: (g["approach"] == APPROACH_ALREADY, -g["est_triangles_saved"], -g["count"]))
        opportunities = [g for g in groups if g["approach"] != APPROACH_ALREADY]
        saved_tris = sum(g["est_triangles_saved"] for g in opportunities)
        saved_prims = sum(g["est_prims_saved"] for g in opportunities)
        share = saved_tris / self.tris_stored if self.tris_stored else 0.0
        if opportunities:
            plain = (f"Instancing the {len(opportunities)} repeated group(s) would stop storing about {saved_tris:,} "
                     f"duplicate triangles ({share:.0%} of the stored geometry) and {saved_prims:,} duplicate prims.")
        else:
            plain = "No repeated copies that need instancing were found."
        no_effect = self.counts.get("instanceable_without_effect", 0)
        if no_effect:
            plain += (f" {no_effect} prim(s) are marked instanceable but have no reference or payload, so the flag does "
                      f"nothing (e.g. {self.no_effect_examples[0]}).")
        return {
            "groups": groups[:MAX_GROUPS],
            "groups_total": len(groups),
            "opportunities": len(opportunities),
            "instanceable_without_effect": no_effect,
            "instanceable_without_effect_examples": list(self.no_effect_examples),
            "total_saving_estimate": {"triangles": saved_tris, "prims": saved_prims,
                                      "share_of_stored_triangles": round(share, 4), "plain": plain},
            "gotcha": _INSTANCING_GOTCHA,
        }

    def _payload_candidates(self) -> list[dict[str, Any]]:
        by_key: dict[tuple, list[_Node]] = {}
        for node in self.arc_nodes:
            if node.payload or not node.key:
                continue
            if node.tris >= PAYLOAD_MIN_TRIANGLES or node.prims >= PAYLOAD_MIN_PRIMS:
                by_key.setdefault(node.key, []).append(node)
        out = []
        for key, nodes in sorted(by_key.items(), key=lambda item: -sum(n.tris for n in item[1])):
            asset = asset_health._short_path(key[0][0])
            tris = max(node.tris for node in nodes)
            prims = max(node.prims for node in nodes)
            out.append({
                "asset": key[0][0],
                "copies": len(nodes),
                "example_paths": [str(node.path) for node in nodes[:MAX_EXAMPLE_PATHS]],
                "triangles_per_copy": tris,
                "prims_per_copy": prims,
                "plain": (f"{asset} is brought in by reference ({tris:,} triangles, {prims:,} prims per copy). As a "
                          "payload it could be unloaded while you work on other parts and loaded only when needed."),
                "suggestion": "Ask for convert_to_payload on these paths: it returns the steps and a Python snippet.",
            })
        return out[:10]

    def _hotspots(self) -> list[dict[str, Any]]:
        spots: list[dict[str, Any]] = []
        for tris, display, copies in sorted(self.heavy, reverse=True)[:MAX_HOTSPOTS_PER_KIND]:
            measure: dict[str, Any] = {"triangles": tris}
            if copies > 1:
                measure["instances"] = copies
            spots.append({
                "path": display, "kind": "heavy mesh", "measure": measure,
                "plain": f"{display} has {tris:,} triangles" + (f" (drawn {copies} times)" if copies > 1 else "") + ".",
                "suggestion": "Decimate it (Blender Decimate modifier, or re-tessellate the CAD coarser) or keep a "
                              "lighter version for the viewport; hidden or far-away detail still costs memory.",
            })
        for branch, (count, depth, deepest) in sorted(self.deep.items(), key=lambda item: -item[1][1])[:MAX_HOTSPOTS_PER_KIND]:
            spots.append({
                "path": branch, "kind": "deep hierarchy", "measure": {"max_depth": depth, "prims_below_limit": count},
                "plain": f"Below {branch} the tree goes {depth} levels deep (e.g. {deepest}); CAD exports often nest "
                         "empty groups like this.",
                "suggestion": f"Flatten groups that carry no transform or meaning (keep under {DEEP_HIERARCHY} levels), "
                              "ideally in the source tool before export.",
            })
        total_lights = sum(self.lights.values())
        if total_lights > MANY_LIGHTS:
            parents = Counter(path.GetParentPath() for path in self.light_paths)
            parent, in_parent = parents.most_common(1)[0]
            spots.append({
                "path": str(parent), "kind": "many lights", "measure": {"lights": total_lights, "under_this_prim": in_parent},
                "plain": f"The scene has {total_lights} lights ({in_parent} under {parent}). Every light adds render "
                         "cost and, in real-time RTX, noise.",
                "suggestion": "Replace rows of small lights with a few large rect lights, use emissive materials for "
                              "fixtures you only need to see glowing, and light the hall mainly with a dome + one sun.",
            })
        for record in self.texture_stats.get("huge", [])[:MAX_HOTSPOTS_PER_KIND]:
            size = f"{record['width']}x{record['height']}" if "width" in record else f"{record['bytes'] / 1048576:.0f} MB"
            spots.append({
                "path": record["used_by"], "kind": "huge texture",
                "measure": {key: record[key] for key in ("bytes", "width", "height") if key in record},
                "plain": f"{record['path']} is {size}; one 8K texture uses about 256 MB of GPU memory uncompressed.",
                "suggestion": "Downsize to 2K/4K unless the camera gets very close; use a 4K HDR for the dome.",
            })
        extra = self.counts.get("duplicate_material_extra", 0)
        if self.bound_unique >= MANY_UNIQUE_MATERIALS or extra >= DUPLICATE_MATERIALS_MIN:
            measure = {"unique_bound_materials": self.bound_unique, "duplicate_materials": extra}
            plain = f"{self.bound_unique} different materials are bound in the scene"
            if extra:
                plain += f", and {extra} materials are exact duplicates of another (same shader and values)"
            spots.append({
                "path": str(self.root_path), "kind": "many unique materials", "measure": measure,
                "plain": plain + ". Each unique material is compiled and kept on the GPU separately.",
                "suggestion": "Bind one shared material per look (e.g. /World/Looks/Steel) instead of one per part; "
                              "CAD and FBX imports often create a copy per mesh.",
            })
        total = self.tris_rendered or 0
        total_prims = max([self.visited - self.prototype_prims, 1] + [node.prims for node in self.buckets])
        qualifying = []
        for node in self.buckets:
            if node.is_mesh:
                continue
            tri_share = node.tris / total if total else 0.0
            prim_share = node.prims / total_prims
            if max(tri_share, prim_share) > 0.98:
                continue
            if (tri_share >= DENSE_SUBTREE_SHARE and node.tris >= DENSE_SUBTREE_TRIANGLES) or \
                    (prim_share >= DENSE_SUBTREE_SHARE and node.prims >= DENSE_SUBTREE_PRIMS):
                qualifying.append((node, tri_share, prim_share))
        deepest = [entry for entry in qualifying
                   if not any(other[0].path != entry[0].path and other[0].path.HasPrefix(entry[0].path) for other in qualifying)]
        for node, tri_share, prim_share in deepest[:MAX_HOTSPOTS_PER_KIND]:
            spots.append({
                "path": str(node.path), "kind": "dense subtree",
                "measure": {"triangles": node.tris, "prims": node.prims, "triangle_share": round(tri_share, 3),
                            "prim_share": round(prim_share, 3)},
                "plain": f"{node.path} holds {tri_share:.0%} of the drawn triangles ({node.tris:,}) and {prim_share:.0%} "
                         "of the prims: this is the first place to optimise.",
                "suggestion": "Instance its repeated parts, make it a payload you can unload, or swap in a lighter "
                              "version (variant set) while you work elsewhere.",
            })
        spots.sort(key=lambda spot: _HOTSPOT_PRIORITY.get(spot["kind"], 9))  # stable: keeps size order inside a kind
        return spots

    def _light_advice(self) -> dict[str, Any]:
        total = sum(self.lights.values())
        advice: list[str] = []
        if total > MANY_LIGHTS:
            advice.append(f"{total} lights is a lot for real-time RTX: merge them into fewer, larger lights or use "
                          "emissive materials for decorative fixtures.")
        domes = self.dome_lights
        if len(domes) > 1:
            advice.append(f"{len(domes)} dome lights: one dome is normally enough; extra domes add cost.")
        for dome in domes:
            texture = dome.get("texture")
            if isinstance(texture, dict) and max(texture.get("width", 0), texture.get("height", 0)) >= LARGE_TEXTURE_PIXELS:
                advice.append(f"Dome {dome['path']} uses a {texture['width']}x{texture['height']} HDR; a 4K HDR looks the "
                              "same in most views and uses a quarter of the memory.")
        if total == 0:
            advice.append("No lights found: the viewport falls back to its default lighting.")
        if not advice:
            advice.append("Lighting looks reasonable for real-time use.")
        return {"lights": total, "by_type": dict(self.lights.most_common()), "too_many": total > MANY_LIGHTS,
                "dome_lights": domes[:5], "advice": advice}

    def result(self, elapsed: float) -> dict[str, Any]:
        instancing = self._instancing()
        hotspots = self._hotspots()
        payload_candidates = self._payload_candidates()
        light_advice = self._light_advice()
        score = 100
        kinds = Counter(spot["kind"] for spot in hotspots)
        score -= min(18, 6 * kinds["heavy mesh"])
        score -= 4 if kinds["deep hierarchy"] else 0
        score -= 10 if kinds["many lights"] else 0
        score -= min(12, 4 * kinds["huge texture"])
        score -= 6 if kinds["many unique materials"] else 0
        score -= min(8, 4 * kinds["dense subtree"])
        share = instancing["total_saving_estimate"]["share_of_stored_triangles"]
        if instancing["opportunities"]:
            score -= min(30, 5 * instancing["opportunities"] + int(round(20 * min(1.0, share))))
        score -= min(6, 2 * len(payload_candidates))
        score = max(0, min(100, score))
        grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 55 else "F"
        truncated_by_time = self.stop == "time" or bool(self.stopped_phases)
        truncated_by_prims = self.stop == "max_prims"
        truncated = truncated_by_time or truncated_by_prims
        notes = list(self.notes)
        if truncated_by_time:
            notes.insert(0, "Time budget reached: the totals cover only the prims visited; allow more time for a full report.")
        if truncated_by_prims:
            notes.insert(0, f"Stopped after max_prims={self.max_prims:,} prims; the rest of the scene was not measured.")
        if self.sampled_triangles:
            notes.append("Some triangle counts are estimates (very long faceVertexCounts were sampled, or meshes past "
                         f"the first {MAX_MESH_READS:,} were counted but not measured).")
        if self.root_path != Sdf.Path.absoluteRootPath:
            notes.append(f"The report covers {self.root_path} only.")
        if not self.check_time_samples:
            notes.append("No layer holds time samples, so the scene is static (value clips were not checked).")
        notes.append("Read-only: nothing was changed or saved.")
        totals = {
            "prims": self.visited - self.prototype_prims,
            "prims_in_prototypes": self.prototype_prims,
            "prims_by_type": dict(self.types.most_common(30)),
            "gprims": self.counts["gprims"],
            "meshes": self.counts["meshes"],
            "meshes_measured": self.meshes_measured,
            "triangles": self.tris_stored,
            "triangles_drawn": self.tris_rendered,
            "triangles_estimated": self.sampled_triangles,
            "points": self.points,
            "materials": len(self.materials),
            "unique_bound_materials": self.bound_unique,
            "duplicate_materials": self.counts.get("duplicate_material_extra", 0),
            "textures": self.texture_stats.get("count", len(self.textures)),
            "textures_missing": self.texture_stats.get("missing", 0),
            "texture_local_bytes": self.texture_stats.get("local_bytes", 0),
            "texture_formats": self.texture_stats.get("formats", {}),
            "largest_textures": self.texture_stats.get("largest", []),
            "lights": sum(self.lights.values()),
            "lights_by_type": dict(self.lights.most_common()),
            "cameras": self.counts["cameras"],
            "instanceable_prims": self.counts["instanceable"],
            "instanceable_without_effect": self.counts["instanceable_without_effect"],
            "instances": self.counts["instances"],
            "prototypes": self.counts["prototypes"],
            "point_instancers": self.counts["point_instancers"],
            "point_instances": self.counts["point_instances"],
            "payloads_loaded": self.payloads["loaded"],
            "payloads_unloaded": self.payloads["unloaded"],
            "unloaded_payload_examples": self.payloads["unloaded_examples"],
            "layers": len(self.stage.GetUsedLayers()),
            "time_sampled_attributes": self.time_sampled,
            "max_depth": self.max_depth,
        }
        result = {
            "ok": True,
            "stage": usd_validate._layer_name(self.stage.GetRootLayer(), full=True),
            "root_path": str(self.root_path),
            "score": score,
            "grade": grade,
            "score_is_partial": truncated,
            "totals": totals,
            "hotspots": hotspots,
            "instancing": instancing,
            "payload_candidates": payload_candidates,
            "light_advice": light_advice,
            "summary": "",
            "truncated": truncated,
            "truncated_by_time": truncated_by_time,
            "truncated_by_prims": truncated_by_prims,
            "prims_visited": self.visited,
            "elapsed_ms": round(elapsed * 1000.0, 1),
            "notes": notes,
        }
        if self.budget is not None:
            result["time_budget"] = self.budget.report(self.visited, truncated_by_time,
                                                       stopped_early=list(dict.fromkeys(self.stopped_phases)))
        result["summary"] = _summary(result)
        return asset_health._jsonable(result)


def _summary(result: dict[str, Any]) -> str:
    totals = result["totals"]
    sentences = [
        f"This scene scores {result['score']}/100 for performance (grade {result['grade']}): {totals['prims']:,} prims, "
        f"{totals['meshes']:,} meshes, {totals['triangles_drawn']:,} triangles drawn ({totals['triangles']:,} stored), "
        f"{totals['materials']:,} materials and {totals['lights']:,} lights."
    ]
    instancing = result["instancing"]
    opportunities = [g for g in instancing["groups"] if g["approach"] != APPROACH_ALREADY and not g.get("nested_in")]
    if opportunities:
        top = opportunities[0]
        caveat = ""
        if top.get("copies_with_local_edits"):
            caveat = (f" (but {top['copies_with_local_edits']} of {top['local_edits_checked']} copies checked have local "
                      "edits on their children that instancing would drop: look at those first)")
        sentences.append(f"Biggest win: {top['plain'].split(':')[0]}; {top['approach'].split(':')[0]} would avoid about "
                         f"{top['est_triangles_saved']:,} duplicate triangles{caveat}.")
    else:
        sentences.append(instancing["total_saving_estimate"]["plain"])
    hotspots = result["hotspots"]
    if hotspots:
        kinds = Counter(spot["kind"] for spot in hotspots)
        first = hotspots[0]["path"]
        where = "the whole scene" if first in ("/", result["root_path"]) else first
        sentences.append("Hotspots: " + ", ".join(f"{n} {kind}" for kind, n in kinds.items()) +
                         f"; start with {where} ({hotspots[0]['kind']}).")
    if result["payload_candidates"]:
        sentences.append(f"{len(result['payload_candidates'])} heavy referenced asset(s) could become payloads so you can "
                         "unload them while you work.")
    if result["truncated"]:
        sentences.append("The report stopped early (time or prim limit), so it covers only part of the scene.")
    return " ".join(sentences[:5])


# ================================================================================================= helpers


def _triangles(counts: Any) -> tuple[int, bool]:
    """Triangles of a polygon mesh from faceVertexCounts (an n-gon is n-2 triangles); long arrays are sampled."""
    if counts is None:
        return 0, False
    n = len(counts)
    if n == 0:
        return 0, False
    if n <= EXACT_FACE_COUNT_LIMIT:
        return int(sum(max(0, int(c) - 2) for c in counts)) if n < 4096 else max(0, int(sum(counts)) - 2 * n), False
    step = max(1, n // TRIANGLE_SAMPLES)
    sample = counts[::step]
    mean = sum(max(0, int(c) - 2) for c in sample) / max(1, len(sample))
    return int(round(mean * n)), True


def _is_light(prim: Any) -> bool:
    from .authoring import _is_light as authoring_is_light

    return authoring_is_light(prim)


def _material_signature(material: Any) -> str | None:
    """A digest of the material's shader network (ids / MDL sources and authored input values)."""
    parts = []
    base = material.GetPath()
    for index, prim in enumerate(Usd.PrimRange(material)):
        if index > 64:
            return None
        if not prim.IsA(UsdShade.Shader):
            continue
        shader = UsdShade.Shader(prim)
        source = str(shader.GetImplementationSource())
        ident = str(shader.GetShaderId() or "") if source == "id" else ""
        if source == "sourceAsset":
            ident = f"{getattr(shader.GetSourceAsset('mdl'), 'path', '')}::{shader.GetSourceAssetSubIdentifier('mdl')}"
        inputs = []
        for shader_input in shader.GetInputs():
            attr = shader_input.GetAttr()
            if shader_input.HasConnectedSource():
                sources = shader_input.GetConnectedSources()[0]
                inputs.append((shader_input.GetBaseName(), "->" + ",".join(
                    str(info.source.GetPath().MakeRelativePath(base)) + "." + str(info.sourceName) for info in sources)))
            elif attr.HasAuthoredValue():
                value = attr.Get()
                inputs.append((shader_input.GetBaseName(), str(getattr(value, "path", value))[:200]))
        parts.append((str(prim.GetPath().MakeRelativePath(base)), ident, tuple(sorted(inputs))))
    if not parts:
        return None
    return hashlib.sha1(repr(sorted(parts)).encode()).hexdigest()


_HDR_RES = re.compile(rb"\n\s*([-+])([YX])\s+(\d+)\s+([-+])([YX])\s+(\d+)")


def _texture_size(path: str, ext: str) -> tuple[int, int] | None:
    """Pixel size from the file header (PNG/JPEG via asset_health, Radiance HDR and OpenEXR here); read-only."""
    if ext in ("png", "jpg", "jpeg"):
        return asset_health._image_size(path, ext)
    if ext not in ("hdr", "exr"):
        return None
    try:
        with open(path, "rb") as handle:
            head = handle.read(65536)
    except OSError:
        return None
    if ext == "hdr":
        match = _HDR_RES.search(head)
        if not match:
            return None
        first, second = int(match.group(3)), int(match.group(6))
        return (second, first) if match.group(2) == b"Y" else (first, second)
    if head[:4] != b"\x76\x2f\x31\x01":
        return None
    index = 8
    while index < len(head):
        end = head.find(b"\0", index)
        if end < 0 or end == index:
            return None
        name = head[index:end]
        type_end = head.find(b"\0", end + 1)
        if type_end < 0 or type_end + 5 > len(head):
            return None
        size = int.from_bytes(head[type_end + 1:type_end + 5], "little")
        value = head[type_end + 5:type_end + 5 + size]
        if name == b"dataWindow" and len(value) >= 16:
            xmin, ymin, xmax, ymax = (int.from_bytes(value[i:i + 4], "little", signed=True) for i in range(0, 16, 4))
            return xmax - xmin + 1, ymax - ymin + 1
        index = type_end + 5 + size
    return None


def _local_child_edits(stage: Any, path: Any, *, limit: int = MAX_EDITS_LISTED) -> tuple[list[dict[str, Any]], int]:
    """Opinions on DESCENDANTS of path in the stage's local layer stack (incl. session layer): ignored once instanced.

    Returns (edits listed, total). An empty 'over' (no fields) is not an edit. Variant content is part of the
    instance's arcs and is kept, so variant specs are not reported.
    """
    edits: list[dict[str, Any]] = []
    total = 0
    scanned = 0
    for layer in stage.GetLayerStack(True):
        spec = layer.GetPrimAtPath(path)
        if not spec:
            continue
        pending = list(spec.nameChildren)
        while pending:
            scanned += 1
            if scanned > MAX_EDIT_SPECS:
                return edits, max(total, 1)
            child = pending.pop()
            pending.extend(child.nameChildren)
            keys = sorted(set(child.ListInfoKeys()) - {"specifier"})
            properties = [prop.name for prop in child.properties]
            if not properties and not keys and child.specifier == Sdf.SpecifierOver:
                continue
            total += 1
            if len(edits) >= limit:
                continue
            what = []
            if child.specifier != Sdf.SpecifierOver:
                what.append("defines a new prim here" if child.specifier == Sdf.SpecifierDef else "defines a class")
            if properties:
                what.append("overrides " + ", ".join(properties[:8]) + (" ..." if len(properties) > 8 else ""))
            if keys:
                what.append("sets " + ", ".join(keys[:8]))
            edits.append({
                "path": str(child.path),
                "layer": usd_validate._layer_name(layer),
                "specifier": str(child.specifier).rsplit(".", 1)[-1].replace("Specifier", "").lower(),
                "properties": properties[:20],
                "metadata": keys[:20],
                "plain": f"{child.path} in {usd_validate._layer_name(layer)} {'; '.join(what)}; instancing would ignore this.",
            })
    return edits, total


# ============================================================================================ make_instanceable


def make_instanceable(stage: Any, paths: Any, *, dry_run: bool = True, undo_stack: Any = None) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author instanceable = true on prims that reference / payload an asset.

    All or nothing: if any requested prim is refused (no reference/payload, inside an instance, a prototype, local
    child edits that instancing would discard, a stronger layer saying instanceable = false) nothing is written and
    each refusal says why. Already-instanceable prims are skipped. A real write snapshots the edit target layer and
    rolls back if a prim does not become an instance. undo_stack: an undo.UndoStack to record the edit for undo_last.
    In Kit the edit is not on Kit's own undo stack.
    """
    from . import authoring

    action = "make_instanceable"
    dry = authoring._dry(dry_run)
    warnings: list[str] = []
    base = {"ok": False, "dry_run": dry, "action": action, "written": False}
    if _PXR_IMPORT_ERROR is not None:
        return {**base, "error": f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}"}
    if stage is None or not hasattr(stage, "GetEditTarget"):
        return {**base, "error": "No USD stage is open."}
    try:
        authoring._check_edit_target(stage, warnings)
    except authoring._Refusal as refusal:
        return {**base, "error": str(refusal), "warnings": warnings}
    requested = _path_list(paths)
    if isinstance(requested, str):
        return {**base, "error": requested}
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    local = list(stage.GetLayerStack(True))
    local_ids = [item.identifier for item in local]
    target_index = local_ids.index(layer.identifier) if layer.identifier in local_ids else -1
    if target_index < 0:
        warnings.append("The edit target is not in the stage's local layer stack; the result is checked after writing.")
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    requested_set = set(requested)
    for text in requested:
        path = Sdf.Path(text)
        prim = stage.GetPrimAtPath(path)
        reason = None
        extra: dict[str, Any] = {}
        if not prim:
            reason = "No prim exists at this path."
        elif prim.IsInstanceProxy():
            owner = next((p for p in path.GetPrefixes()[::-1] if stage.GetPrimAtPath(p).IsInstance()), None)
            reason = (f"This prim is inside an instance ({owner}); instanceable can only be set on the instance root, "
                      "and that prim already is one.")
        elif prim.IsPrototype() or prim.IsInPrototype():
            reason = "This is an instancing prototype (internal to USD); set instanceable on the referencing prims."
        elif not prim.IsActive():
            reason = "This prim is inactive (deactivated), so instancing it has no effect."
        elif any(other != text and path.HasPrefix(Sdf.Path(other)) for other in requested_set):
            reason = "An ancestor of this prim is in the same request; once the ancestor is instanced this prim is inside it."
        elif prim.IsInstanceable() and prim.IsInstance():
            skipped.append({"path": text, "reason": "Already instanceable (it is an instance)."})
            continue
        elif not (prim.HasAuthoredReferences() or prim.HasAuthoredPayloads()):
            reason = ("This prim has no reference or payload, so there is no asset to share; instanceable would do "
                      "nothing. Put the part in its own file and reference it first.")
        else:
            stronger = _stronger_false(prim, local_ids, target_index)
            edits, total = _local_child_edits(stage, path)
            if stronger:
                reason = (f"{stronger} (a stronger layer than the edit target) sets instanceable = false; writing in the "
                          "edit target would not change the result. Edit that layer instead.")
            elif total:
                reason = (f"{total} local edit(s) on this prim's children would be silently discarded by instancing. "
                          "Move per-copy changes onto the prim itself, remove them, or keep this copy un-instanced.")
                extra = {"local_edits": edits, "local_edits_total": total}
        if reason:
            refused.append({"path": text, "reason": reason, **extra})
            continue
        key = asset_health._arc_key(prim)
        planned.append({
            "path": text,
            "asset": key[0][0] if key else None,
            "edit_spec_path": str(edit_target.MapToSpecPath(path)),
            "spec_exists": bool(layer.GetPrimAtPath(edit_target.MapToSpecPath(path))),
        })
    assets = Counter(item["asset"] for item in planned)
    if len(assets) > 1:
        warnings.append(f"The prims reference {len(assets)} different assets, so USD makes one prototype per asset "
                        "(still a saving for each).")
    groups = [{"asset": asset, "count": n} for asset, n in assets.most_common()]
    common = {"would_change": planned, "skipped": skipped, "refused": refused, "groups": groups,
              "edit_target_layer": layer.identifier, "gotcha": _INSTANCING_GOTCHA}
    if refused:
        return {**base, **common, "warnings": warnings,
                "error": f"{len(refused)} prim(s) were refused (see refused); nothing was written. Fix them or leave "
                         "them out and try again."}
    if not planned:
        return {**base, **common, "ok": True, "warnings": warnings, "message": "Nothing to change."}
    if dry:
        return {**base, **common, "ok": True, "warnings": warnings,
                "next_step": "Dry run: nothing was written. Call again with dry_run=False to set instanceable."}

    from .undo import LayerRef, SpecSnapshot, UndoEntry

    spec_paths = [Sdf.Path(item["edit_spec_path"]) for item in planned]
    existing = [p for p, item in zip(spec_paths, planned) if item["spec_exists"]]
    absent = [p for p, item in zip(spec_paths, planned) if not item["spec_exists"]]
    try:
        snapshot = SpecSnapshot.capture(layer, absent, shallow=existing)
    except Exception as exc:  # noqa: BLE001
        return {**base, **common, "warnings": warnings, "error": f"Could not snapshot the edit target, nothing written: {exc}"}
    failure = None
    try:
        with Sdf.ChangeBlock():
            for spec_path in spec_paths:
                _author_instanceable(layer, spec_path)
    except Exception as exc:  # noqa: BLE001
        failure = f"Writing failed: {exc}"
    if failure is None:
        not_instanced = [item["path"] for item in planned if not stage.GetPrimAtPath(item["path"]).IsInstance()]
        if not_instanced:
            failure = (f"{len(not_instanced)} prim(s) did not become instances after writing (e.g. {not_instanced[0]}); "
                       "a stronger opinion or an unusual composition prevents it.")
    if failure is not None:
        try:
            snapshot.restore(layer)
            rolled_back = snapshot.matches(layer)
        except Exception as exc:  # noqa: BLE001
            rolled_back = False
            failure += f" Rollback failed too: {exc}"
        return {**base, **common, "warnings": warnings, "rolled_back": rolled_back, "written": not rolled_back,
                "error": failure + (" The edit target was put back exactly as it was." if rolled_back else "")}
    changed = []
    for item in planned:
        prim = stage.GetPrimAtPath(item["path"])
        prototype = prim.GetPrototype()
        changed.append({"path": item["path"], "is_instance": prim.IsInstance(),
                        "prototype": str(prototype.GetPath()) if prototype else None})
    undo_info: dict[str, Any] = {"recorded": False}
    if undo_stack is not None:
        try:
            entry = UndoEntry(f"make instanceable ({len(planned)} prims)", [item["path"] for item in planned],
                              [snapshot], [snapshot.recapture()], stage_ref=LayerRef(stage.GetRootLayer()))
            undo_stack.push(entry)
            undo_info = {"recorded": True, "label": entry.label}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Instancing worked but could not be recorded for undo: {exc}")
    warnings.append(f"{asset_health._short_path(layer.identifier)} now has unsaved changes; save it to keep instancing.")
    return {**base, **common, "ok": True, "written": True, "changed": changed,
            "prototypes_after": len(stage.GetPrototypes()), "undo": undo_info, "warnings": warnings}


def _author_instanceable(layer: Any, spec_path: Any) -> None:
    """Sdf-level write (safe inside Sdf.ChangeBlock): an 'over' spec if needed, then instanceable = true."""
    spec = layer.GetPrimAtPath(spec_path) or Sdf.CreatePrimInLayer(layer, spec_path)
    if not spec:
        raise ValueError(f"could not create a prim spec at {spec_path}")
    spec.instanceable = True


def _stronger_false(prim: Any, local_ids: list[str], target_index: int) -> str | None:
    """The layer name when a local layer stronger than the edit target authors instanceable = false."""
    if target_index <= 0:
        return None
    for spec in prim.GetPrimStack():
        identifier = spec.layer.identifier
        if identifier not in local_ids:
            continue
        if local_ids.index(identifier) >= target_index:
            break
        if spec.HasInfo("instanceable") and not spec.instanceable:
            return usd_validate._layer_name(spec.layer)
    return None


def _path_list(paths: Any) -> list[str] | str:
    if isinstance(paths, (str, bytes)) or (paths is not None and hasattr(paths, "pathString")):
        paths = [paths]
    try:
        items = [str(getattr(p, "pathString", p)).strip() for p in (paths or [])]
    except TypeError:
        return "paths must be a prim path or a list of prim paths."
    items = list(dict.fromkeys(item for item in items if item))
    if not items:
        return "Give at least one prim path, e.g. ['/World/Machine_01']."
    if len(items) > MAX_PATHS:
        return f"At most {MAX_PATHS} paths per call."
    for item in items:
        if not item.startswith("/") or not Sdf.Path.IsValidPathString(item) or not Sdf.Path(item).IsPrimPath():
            return f"Not an absolute prim path: {item!r}"
    return items


# ============================================================================================ convert_to_payload


def convert_to_payload(stage: Any, paths: Any) -> dict[str, Any]:
    """PLAN ONLY: how to turn the references on these prims into payloads (steps + a Python snippet). Writes nothing."""
    base = {"ok": False, "action": "convert_to_payload", "plan_only": True, "written": False}
    if _PXR_IMPORT_ERROR is not None:
        return {**base, "error": f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}"}
    if stage is None or not hasattr(stage, "GetPrimAtPath"):
        return {**base, "error": "No USD stage is open."}
    requested = _path_list(paths)
    if isinstance(requested, str):
        return {**base, "error": requested}
    local_ids = {layer.identifier for layer in stage.GetLayerStack(True)}
    plans: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    for text in requested:
        prim = stage.GetPrimAtPath(text)
        if not prim:
            refused.append({"path": text, "reason": "No prim exists at this path."})
            continue
        if prim.IsInstanceProxy():
            refused.append({"path": text, "reason": "Inside an instance; plan the conversion on the instance root."})
            continue
        references = []
        for spec in prim.GetPrimStack():
            for item in usd_validate._list_op_items(spec.referenceList):
                offset = item.layerOffset
                references.append({
                    "asset_path": item.assetPath,
                    "prim_path": str(item.primPath) if not item.primPath.isEmpty else "",
                    "layer_offset": None if offset.IsIdentity() else {"offset": offset.offset, "scale": offset.scale},
                    "authored_in": spec.layer.identifier,
                    "spec_path": str(spec.path),
                    "local": spec.layer.identifier in local_ids,
                })
        if not references:
            refused.append({"path": text, "reason": "This prim has no references to convert"
                            + (" (it already uses a payload)." if prim.HasAuthoredPayloads() else ".")})
            continue
        notes = []
        if any(not ref["local"] for ref in references):
            notes.append("Some references are authored inside another asset file (authored_in), not in your scene's "
                         "layers; convert them in that file, or override the arcs explicitly in your layer.")
        if prim.HasAuthoredPayloads():
            notes.append("The prim already has a payload too; a payload is weaker than a reference (LIVRPS), so check "
                         "the result.")
        if len(references) > 1:
            notes.append("Several references are listed: convert only the one that brings in the heavy asset.")
        plans.append({"path": text, "references": references, "notes": notes})
    if not plans:
        return {**base, "refused": refused, "error": "Nothing to plan (see refused)."}
    return {**base, "ok": True, "plans": plans, "refused": refused, "steps": [
        "Pick one copy first and convert it; check it still looks right, then do the rest.",
        "In the layer that authors the reference (authored_in), remove that reference and add a payload with the "
        "same asset path, prim path and layer offset (the snippet below does exactly this).",
        "Save that layer. Nothing else changes while the payload is loaded: payload content composes like a "
        "reference, just one step weaker (LIVRPS: Local, Inherits, VariantSets, References, Payloads, Specializes).",
        "Now you can unload it while you work: Kit Stage window right-click > Unload, stage.Unload(path) in Python, "
        "or open the scene with Usd.Stage.Open(file, Usd.Stage.LoadNone) and load only what you need.",
        "Instancing still works: instanceable copies with the same payload share one prototype while loaded.",
    ], "python": _payload_snippet(plans),
        "notes": ["Plan only: nothing was written. This tool does not convert; run the snippet yourself after "
                  "checking it (dry run in a copy of the scene first)."]}


def _payload_snippet(plans: list[dict[str, Any]]) -> str:
    lines = [
        "from pxr import Sdf, Usd",
        "",
        "# In Kit: import omni.usd; stage = omni.usd.get_context().get_stage()",
        "# Outside Kit: stage = Usd.Stage.Open(r'<your scene file>')",
        "",
    ]
    for plan in plans[:20]:
        for ref in plan["references"]:
            if not ref["local"]:
                continue
            args = [repr(ref["asset_path"])]
            if ref["prim_path"] or ref["layer_offset"]:
                args.append(repr(ref["prim_path"]))
            if ref["layer_offset"]:
                args.append(f"Sdf.LayerOffset({ref['layer_offset']['offset']!r}, {ref['layer_offset']['scale']!r})")
            joined = ", ".join(args)
            lines += [
                f"layer = Sdf.Layer.Find({ref['authored_in']!r})",
                "with Usd.EditContext(stage, layer):  # edit the layer that authors the reference",
                f"    prim = stage.GetPrimAtPath({plan['path']!r})",
                f"    prim.GetReferences().RemoveReference(Sdf.Reference({joined}))",
                f"    prim.GetPayloads().AddPayload(Sdf.Payload({joined}))",
                "",
            ]
    if len(plans) > 20:
        lines.append(f"# ... {len(plans) - 20} more prims: repeat the same three lines for each.")
    lines.append("# Then save the layer: layer.Save()  (in Kit: File > Save)")
    return "\n".join(lines)


# ============================================================================================ live_frame_stats

_LAST_APP_SAMPLE: dict[str, float] = {}


def live_frame_stats() -> dict[str, Any]:
    """Kit only: viewport FPS / frame time, GPU memory and the app loop rate. Read-only; never changes settings.

    Returns {"ok": False, "reason": ...} when not running inside Kit or when no API answers.
    """
    from .kit_live_extras import _kit_module

    utility = _kit_module("omni.kit.viewport.utility")
    engine_stats = _kit_module("omni.hydra.engine.stats")
    kit_app = _kit_module("omni.kit.app")
    settings_module = _kit_module("carb.settings")
    if utility is None and engine_stats is None and kit_app is None:
        return {"ok": False, "reason": "Kit not available: live_frame_stats runs inside Isaac Sim / USD Composer "
                                       "(omni.kit.viewport.utility, omni.hydra.engine.stats and omni.kit.app could not be imported)."}
    result: dict[str, Any] = {"ok": False, "viewport": None, "gpu_memory": [], "app_loop": None, "rate_limit": None,
                              "notes": [], "sources": []}
    notes = result["notes"]
    device_mask = None
    if utility is not None:
        try:
            viewport = utility.get_active_viewport()
            if viewport is None:
                notes.append("No active viewport (headless app or the viewport window is closed).")
            else:
                info = dict(getattr(viewport, "frame_info", None) or {})
                fps = _number(getattr(viewport, "fps", None))
                if fps is None:
                    fps = _number(info.get("fps"))
                subframes = max(1, int(_number(info.get("subframe_count")) or 1))
                effective = fps * subframes if fps is not None else None
                device_mask = info.get("device_mask")
                resolution = getattr(viewport, "resolution", None)
                result["viewport"] = {
                    "fps": round(fps, 2) if fps is not None else None,
                    "subframe_count": subframes,
                    "effective_fps": round(effective, 2) if effective is not None else None,
                    "frame_time_ms": round(1000.0 / effective, 2) if effective else None,
                    "resolution": [int(v) for v in resolution] if resolution else None,
                }
                result["sources"].append("omni.kit.viewport.utility.get_active_viewport().fps / frame_info")
                if not fps:
                    notes.append("The viewport reports 0 FPS: it has not rendered yet or rendering is paused.")
        except Exception as exc:  # noqa: BLE001
            notes.append(asset_health._clip(f"Viewport FPS could not be read: {exc}"))
    if engine_stats is not None and hasattr(engine_stats, "get_device_info"):
        try:
            for index, device in enumerate(engine_stats.get_device_info() or []):
                budget = _number(device.get("budget"))
                used = _number(device.get("usage"))
                entry = {
                    "device": device.get("description") or f"GPU {index}",
                    "budget_bytes": int(budget) if budget is not None else None,
                    "used_bytes": int(used) if used is not None else None,
                    "available_bytes": int(max(0.0, budget - used)) if budget is not None and used is not None else None,
                    "used_fraction": round(used / budget, 4) if budget and used is not None else None,
                }
                if isinstance(device_mask, int):
                    entry["rendering"] = bool(device_mask & (1 << index))
                result["gpu_memory"].append(entry)
            result["sources"].append("omni.hydra.engine.stats.get_device_info()")
        except Exception as exc:  # noqa: BLE001
            notes.append(asset_health._clip(f"GPU memory could not be read: {exc}"))
    if kit_app is not None:
        try:
            app = kit_app.get_app()
            updates = int(app.get_update_number())
            seconds = float(app.get_time_since_start_s())
            loop: dict[str, Any] = {"update_number": updates, "seconds_since_start": round(seconds, 3),
                                    "average_fps_since_start": round(updates / seconds, 2) if seconds > 0 else None,
                                    "fps_since_last_call": None}
            last = dict(_LAST_APP_SAMPLE)
            if last and seconds > last["seconds"] and updates >= last["updates"]:
                loop["fps_since_last_call"] = round((updates - last["updates"]) / (seconds - last["seconds"]), 2)
                loop["seconds_since_last_call"] = round(seconds - last["seconds"], 3)
            _LAST_APP_SAMPLE.update({"updates": float(updates), "seconds": seconds})
            result["app_loop"] = loop
            result["sources"].append("omni.kit.app.get_app().get_update_number() / get_time_since_start_s()")
        except Exception as exc:  # noqa: BLE001
            notes.append(asset_health._clip(f"App loop rate could not be read: {exc}"))
    if settings_module is not None:
        try:
            settings = settings_module.get_settings()
            enabled = settings.get("/app/runLoops/main/rateLimitEnabled")
            frequency = _number(settings.get("/app/runLoops/main/rateLimitFrequency"))
            result["rate_limit"] = {"enabled": None if enabled is None else bool(enabled), "frequency_hz": frequency}
            result["sources"].append("carb.settings /app/runLoops/main/rateLimitEnabled + rateLimitFrequency")
        except Exception as exc:  # noqa: BLE001
            notes.append(asset_health._clip(f"Frame-rate limit setting could not be read: {exc}"))
    result["ok"] = result["viewport"] is not None or result["app_loop"] is not None or bool(result["gpu_memory"])
    if not result["ok"]:
        result["reason"] = "Kit is running but no frame statistic could be read (see notes)."
    result["plain"] = _frame_plain(result)
    return asset_health._jsonable(result)


def _frame_plain(result: dict[str, Any]) -> str:
    parts = []
    viewport = result.get("viewport") or {}
    if viewport.get("effective_fps"):
        parts.append(f"The viewport renders at {viewport['effective_fps']:g} FPS ({viewport['frame_time_ms']:g} ms per frame).")
    rate = result.get("rate_limit") or {}
    if rate.get("enabled") and rate.get("frequency_hz") and viewport.get("effective_fps"):
        if viewport["effective_fps"] >= rate["frequency_hz"] * 0.95:
            parts.append(f"That is the app's {rate['frequency_hz']:g} Hz frame cap, so the scene is not the bottleneck.")
    for device in result.get("gpu_memory", [])[:2]:
        if device.get("used_fraction") is not None:
            parts.append(f"{device['device']}: {device['used_fraction']:.0%} of GPU memory in use.")
            if device["used_fraction"] > 0.9:
                parts.append("GPU memory is nearly full: instancing repeated parts and smaller textures help most.")
    loop = result.get("app_loop") or {}
    if loop.get("fps_since_last_call"):
        parts.append(f"The app loop ran at {loop['fps_since_last_call']:g} updates/s since the last check.")
    return " ".join(parts) or (result.get("reason") or "No frame statistics available.")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None
