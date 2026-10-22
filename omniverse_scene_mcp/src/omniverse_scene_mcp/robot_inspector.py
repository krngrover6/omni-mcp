"""Robot inspector: a read-only, beginner-friendly report on the robots (articulations) in a USD stage.

inspect_robot(stage) finds every prim with UsdPhysics.ArticulationRootAPI (and groups of jointed rigid bodies that
have no articulation root) and describes each robot: its links (rigid parts) with mass and colliders, its joints in
plain words (hinge, slider, fixed...) with axis, limits and drive (motor) settings, the parent -> child chain from
the base, the number of degrees of freedom, whether the base is fixed to the world, and a list of setup problems,
each with why it matters and how to fix it.

It imports only pxr and the standard library (no omni.*), so it runs on a file stage or inside Kit / Isaac Sim. It
never edits, saves or reloads anything: it only reads composed values, relationships and bounding boxes, and it
reads through instanceable prims (Usd.TraverseInstanceProxies), as Isaac Sim robot assets use instancing.

Units: revolute (hinge) limits and angular drive targets are in degrees (UsdPhysics stores them that way); prismatic
(slider) limits and linear drive targets are converted to metres with the stage metersPerUnit; masses are converted
to kilograms with the stage kilogramsPerUnit. Joint axes (X/Y/Z) are in the joint's own frame (localRot0/1).
"""

from __future__ import annotations

import math
import re
import time
from typing import Any

from .usd_insight import TimeBudget, resolve_budget

try:
    from pxr import Sdf, Usd, UsdGeom, UsdPhysics
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Sdf = Usd = UsdGeom = UsdPhysics = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

DEFAULT_MAX_PRIMS = 5000
MAX_PROBLEMS_PER_ROBOT = 100
FLOOR_SCAN_PRIMS = 2000  # extra floor search when root_path limits the main scan
FLOOR_SCAN_DEPTH = 5
FLOAT_TOLERANCE_M = 0.02  # gaps to the floor smaller than 2 cm are ignored
TREE_TEXT_MAX_CHARS = 2000

ROOT_API = "PhysicsArticulationRootAPI"
RIGID_API = "PhysicsRigidBodyAPI"
COLLISION_API = "PhysicsCollisionAPI"
D6_AXES = ("transX", "transY", "transZ", "rotX", "rotY", "rotZ")
_FLOOR_NAME = re.compile(r"floor|ground", re.IGNORECASE)

# kind -> (plain type word, what it does, plural phrase for summaries)
JOINT_WORDS = {
    "revolute": ("hinge", "turns around one axis, like an elbow or a door hinge", "hinge joint"),
    "prismatic": ("slider", "slides along one axis, like a drawer or a gripper finger", "sliding joint"),
    "fixed": ("fixed", "welds two parts together so they cannot move against each other", "fixed joint"),
    "spherical": ("ball joint", "swivels in every direction, like a shoulder", "ball joint"),
    "distance": ("distance", "keeps two points between a minimum and maximum distance, like a rope", "distance joint"),
    "d6": ("d6 (generic)", "lets each of its 6 directions (3 slides, 3 turns) be free, limited or locked", "generic (D6) joint"),
}
JOINT_DOF = {"revolute": 1, "prismatic": 1, "fixed": 0, "spherical": 3, "distance": 0}

GLOSSARY = {
    "articulation": "An articulation is a robot that PhysX simulates as one connected chain of links and joints; this is "
    "more accurate and stable than separate bodies held together by joints.",
    "articulation root": "The articulation root (PhysicsArticulationRootAPI) marks the top of a robot so PhysX builds it "
    "as one articulation; each robot needs exactly one.",
    "link": "A link is one rigid part of a robot (a rigid body), such as an upper arm, a forearm or a finger.",
    "joint": "A joint connects two links and decides how they may move against each other: turn (hinge), slide "
    "(slider) or not at all (fixed).",
    "drive": "A drive (PhysicsDriveAPI) is the motor of a joint: stiffness pulls it toward a target position, damping "
    "pulls it toward a target speed, and max force limits how hard it can push.",
    "DOF": "DOF (degrees of freedom) counts the independent ways a robot can move: each hinge or slider adds 1, a ball "
    "joint adds 3 and a fixed joint adds 0.",
    "collider": "A collider (PhysicsCollisionAPI) gives a link a solid shape; without one the link passes through "
    "everything and PhysX cannot compute its mass from its shape.",
    "mass": "Mass (PhysicsMassAPI, in kg) is how heavy a link is; if it is not set, PhysX computes it from the "
    "colliders' volume and density.",
    "rigid body": "A rigid body (PhysicsRigidBodyAPI) is a part that physics moves as one solid piece.",
    "fixed base": "A fixed base is a robot base attached to the world by a joint, like an arm bolted to a table; a free "
    "(floating) base can move and fall, like a mobile robot or a humanoid.",
    "limits": "Joint limits stop a joint at a lowest and highest position (degrees for hinges, metres for sliders).",
    "stiffness": "Stiffness is how strongly a drive pulls its joint toward the target position, like a spring.",
    "damping": "Damping is how strongly a drive resists speed differences from the target velocity, like a shock absorber.",
    "instanceable": "An instanceable prim shares its insides with other copies to save memory; Isaac robot assets use "
    "it for link meshes, and this inspector reads through it.",
}


def inspect_robot(
    stage: Any,
    root_path: str | None = None,
    *,
    deadline: Any = None,
    max_prims: int = DEFAULT_MAX_PRIMS,
) -> dict[str, Any]:
    """Describe the robots in a stage (or under root_path) for a beginner. Read-only: nothing is edited or saved.

    root_path limits the search to one prim and everything below it; when root_path is inside an articulation
    (say a single link), the whole enclosing articulation is inspected instead. deadline is a usd_insight.TimeBudget
    or a time.monotonic() value to stop at (an ambient ``with TimeBudget(ms):`` also applies); max_prims caps how
    many prims are visited. When either stops the scan the result says so (truncated, truncated_by_time) and
    describes what was seen.

    Returns {ok, robots: [{root, name, articulation_root, base, links, joints, tree, tree_edges, dof_count,
    joint_counts, problems: [{severity, code, path, problem, why_it_matters, how_to_fix}], plain_summary}],
    robot_count, glossary, notes, stage, scanned, truncated, truncated_by_time, time_budget?, elapsed_ms}.
    """
    started = time.perf_counter()
    base: dict[str, Any] = {
        "ok": False,
        "action": "inspect_robot",
        "read_only": True,
        "root_path": root_path if isinstance(root_path, str) else None,
    }
    if _PXR_IMPORT_ERROR is not None:
        return {**base, "error": f"pxr (OpenUSD) is not available here: {_PXR_IMPORT_ERROR}"}
    if stage is None or not hasattr(stage, "GetPseudoRoot"):
        return {**base, "error": "stage must be an open Usd.Stage."}
    if root_path is not None and not isinstance(root_path, str):
        return {**base, "error": "root_path must be a prim path string such as '/World/franka', or None."}
    if isinstance(max_prims, bool) or not isinstance(max_prims, int) or max_prims < 1:
        return {**base, "error": "max_prims must be a whole number, 1 or more."}
    try:
        if deadline is None or isinstance(deadline, TimeBudget):
            budget = resolve_budget(deadline)
        else:
            budget = resolve_budget(deadline, now=time.monotonic())
    except (TypeError, ValueError) as exc:
        return {**base, "error": f"deadline: {exc}"}
    try:
        return _Inspector(stage, budget, max_prims).run(root_path, base, started)
    except Exception as exc:  # the inspector only reads; report instead of raising into a tool call
        return {**base, "error": f"Unexpected error while reading the stage (nothing was changed): {exc}"}


# --- the inspector ----------------------------------------------------------------------------------------


class _Inspector:
    def __init__(self, stage: Any, budget: Any, max_prims: int) -> None:
        self.stage = stage
        self.budget = budget
        self.max_prims = max_prims
        self.mpu = _positive(UsdGeom.GetStageMetersPerUnit(stage), 1.0)
        try:
            self.kgpu = _positive(UsdPhysics.GetStageKilogramsPerUnit(stage), 1.0)
        except Exception:
            self.kgpu = 1.0
        self.up = "Y" if str(UsdGeom.GetStageUpAxis(stage)) == "Y" else "Z"
        self.up_index = 1 if self.up == "Y" else 2
        self.notes: list[str] = []
        self.scanned = 0
        self.truncated = False
        self.stopped_by_time = False
        self.whole_stage = True
        self.scope_path = "/"
        self.order: dict[str, int] = {}
        self.roots: list[Any] = []
        self.joints: list[Any] = []
        self.rigid: dict[str, Any] = {}
        self.colliders: list[Any] = []
        self.floors: list[Any] = []
        self._applied_cache: dict[str, set[str]] = {}
        self._body_cache: dict[str, tuple[str, str | None]] = {}
        self._bbox_cache: Any = None
        self._owners: dict[str, int] | None = None

    # -- driver --

    def run(self, root_path: str | None, base: dict[str, Any], started: float) -> dict[str, Any]:
        scope = self.stage.GetPseudoRoot()
        text = root_path.strip() if isinstance(root_path, str) else ""
        if text and text != "/":
            if not Sdf.Path.IsValidPathString(text) or not Sdf.Path(text).IsAbsoluteRootOrPrimPath():
                return {**base, "error": f"root_path is not a valid absolute prim path: {text!r}"}
            prim = self.stage.GetPrimAtPath(text)
            if not prim or not prim.IsValid():
                return {**base, "error": f"No prim at root_path {text}."}
            ancestor = prim.GetParent()
            while ancestor and ancestor.IsValid() and not ancestor.IsPseudoRoot():
                if ROOT_API in self._applied(ancestor):
                    self.notes.append(
                        f"{text} is inside the robot {ancestor.GetPath()}, so the whole robot was inspected."
                    )
                    prim = ancestor
                    break
                ancestor = ancestor.GetParent()
            scope = prim
        self.whole_stage = scope.IsPseudoRoot()
        self.scope_path = str(scope.GetPath())
        self._scan(scope)
        if not self.whole_stage:
            self._scan_floors()
        robots = self._build_robots()

        if self.truncated:
            self.notes.append(
                f"Stopped after max_prims={self.max_prims} prims; the robots below may be incomplete. Pass a "
                "larger max_prims or a root_path to inspect one robot."
            )
        if self.stopped_by_time:
            self.notes.append("Stopped at the time limit; the robots below may be incomplete.")
        if not robots:
            self.notes.append(
                "No robots were found: no prim has PhysicsArticulationRootAPI and no rigid bodies are connected by "
                "joints" + (f" under {self.scope_path}." if not self.whole_stage else ".")
            )
        self.notes.append(
            "Hinge limits and angular drive targets are in degrees; slider limits and linear targets in metres; "
            "masses in kilograms. Joint axes are in the joint's own frame."
        )
        self.notes.append("Read-only: nothing was edited, saved or reloaded.")
        result = {
            **base,
            "ok": True,
            "root_path": self.scope_path,
            "robots": robots,
            "robot_count": len(robots),
            "glossary": dict(GLOSSARY),
            "notes": self.notes,
            "stage": {
                "up_axis": self.up,
                "meters_per_unit": _round(self.mpu, 9),
                "kilograms_per_unit": _round(self.kgpu, 9),
            },
            "scanned": self.scanned,
            "max_prims": self.max_prims,
            "truncated": bool(self.truncated or self.stopped_by_time),
            "truncated_by_time": bool(self.stopped_by_time),
        }
        if self.budget is not None:
            result["time_budget"] = self.budget.report(self.scanned, self.stopped_by_time)
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        return result

    # -- scanning --

    def _applied(self, prim: Any) -> set[str]:
        key = str(prim.GetPath())
        cached = self._applied_cache.get(key)
        if cached is None:
            try:
                cached = {str(name) for name in prim.GetAppliedSchemas()}
            except Exception:
                cached = set()
            self._applied_cache[key] = cached
        return cached

    def _time_up(self) -> bool:
        return self.budget is not None and self.budget.tick()

    def _scan(self, scope: Any) -> None:
        predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
        for prim in Usd.PrimRange(scope, predicate):
            if prim.IsPseudoRoot():
                continue
            if self._time_up():
                self.stopped_by_time = True
                break
            if self.scanned >= self.max_prims:
                self.truncated = True
                break
            self.scanned += 1
            path = str(prim.GetPath())
            self.order[path] = self.scanned
            applied = self._applied(prim)
            if prim.IsA(UsdPhysics.Joint):
                self.joints.append(prim)
            if ROOT_API in applied:
                self.roots.append(prim)
            if RIGID_API in applied:
                self.rigid[path] = prim
            if COLLISION_API in applied:
                self.colliders.append(prim)
            if self.whole_stage and self._floor_like(prim):
                self.floors.append(prim)

    def _scan_floors(self) -> None:
        predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
        iterator = iter(Usd.PrimRange(self.stage.GetPseudoRoot(), predicate))
        seen = 0
        for prim in iterator:
            if prim.IsPseudoRoot():
                continue
            seen += 1
            if seen > FLOOR_SCAN_PRIMS or self._time_up():
                break
            if prim.GetPath().pathElementCount >= FLOOR_SCAN_DEPTH:
                iterator.PruneChildren()
            if self._floor_like(prim):
                self.floors.append(prim)

    @staticmethod
    def _floor_like(prim: Any) -> bool:
        if not prim.IsA(UsdGeom.Gprim):
            return False
        if prim.GetTypeName() == "Plane":
            return True
        current = prim
        for _ in range(3):
            if not current or not current.IsValid() or current.IsPseudoRoot():
                break
            if _FLOOR_NAME.search(current.GetName()):
                return True
            current = current.GetParent()
        return False

    # -- bodies --

    def _body(self, target: Any) -> tuple[str, str | None]:
        """(status, effective rigid body path) for a joint body target: rigid, ancestor, static or missing."""
        key = str(target)
        cached = self._body_cache.get(key)
        if cached is not None:
            return cached
        prim = self.stage.GetPrimAtPath(target)
        if not prim or not prim.IsValid():
            found: tuple[str, str | None] = ("missing", None)
        elif RIGID_API in self._applied(prim):
            found = ("rigid", key)
        else:
            found = ("static", None)
            current = prim.GetParent()
            while current and current.IsValid() and not current.IsPseudoRoot():
                if RIGID_API in self._applied(current):
                    found = ("ancestor", str(current.GetPath()))
                    break
                current = current.GetParent()
        self._body_cache[key] = found
        return found

    def _collider_owners(self) -> dict[str, int]:
        if self._owners is None:
            self._owners = {}
            for collider in self.colliders:
                owner = self._owner(collider)
                if owner is not None:
                    self._owners[owner] = self._owners.get(owner, 0) + 1
        return self._owners

    def _owner(self, prim: Any) -> str | None:
        current = prim
        while current and current.IsValid() and not current.IsPseudoRoot():
            if RIGID_API in self._applied(current):
                return str(current.GetPath())
            current = current.GetParent()
        return None

    def _joint_record(self, prim: Any) -> dict[str, Any]:
        joint = UsdPhysics.Joint(prim)
        record: dict[str, Any] = {"prim": prim, "path": str(prim.GetPath()), "kind": _joint_kind(prim)}
        for key, rel in (("body0", joint.GetBody0Rel()), ("body1", joint.GetBody1Rel())):
            targets = list(rel.GetTargets()) if rel else []
            record[key] = str(targets[0]) if targets else None
            record[key + "_count"] = len(targets)
            record[key + "_status"], record[key + "_body"] = self._body(targets[0]) if targets else ("empty", None)
        record["enabled"] = _value(prim, "physics:jointEnabled", True) is not False
        record["excluded"] = bool(_value(prim, "physics:excludeFromArticulation", False))
        return record

    # -- grouping into robots --

    def _build_robots(self) -> list[dict[str, Any]]:
        records = [self._joint_record(prim) for prim in self.joints]
        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for path in self.rigid:
            find(path)
        for record in records:
            bodies = [body for body in (record["body0_body"], record["body1_body"]) if body]
            for body in bodies:
                find(body)
            if len(bodies) == 2 and bodies[0] != bodies[1]:
                union(bodies[0], bodies[1])
        members: dict[str, set[str]] = {}
        for node in list(parent):
            members.setdefault(find(node), set()).add(node)

        # Articulation roots, merged when they share links (more than one root in one tree).
        groups: list[dict[str, Any]] = []
        for root in sorted(self.roots, key=lambda prim: self._sort_key(str(prim.GetPath()))):
            path = str(root.GetPath())
            seeds = self._seeds(root, records)
            links: set[str] = set(seeds)
            for seed in seeds:
                links |= members.get(find(seed), {seed})
            target = None
            for group in groups:
                if links & group["links"] or any(_is_under(path, str(other.GetPath())) for other in group["roots"]):
                    target = group
                    break
            if target is None:
                groups.append({"roots": [root], "links": links, "joints": []})
            else:
                target["roots"].append(root)
                target["links"] |= links

        unassigned: list[dict[str, Any]] = []
        for record in records:
            bodies = {record["body0_body"], record["body1_body"]} - {None}
            for group in groups:
                if bodies & group["links"] or any(_is_under(record["path"], str(root.GetPath())) for root in group["roots"]):
                    group["joints"].append(record)
                    break
            else:
                unassigned.append(record)

        # Jointed bodies without any articulation root.
        orphans: dict[str, dict[str, Any]] = {}
        for record in unassigned:
            body = record["body0_body"] or record["body1_body"]
            key = find(body) if body else "joint:" + record["path"]
            group = orphans.setdefault(key, {"roots": [], "links": set(members.get(key, set())), "joints": []})
            group["joints"].append(record)
        robots = [self._robot(group) for group in groups]
        for group in orphans.values():
            structural = any(_structural(record) for record in group["joints"])
            if len(group["links"]) >= 2 or structural:
                robots.append(self._robot(group))
            else:
                for record in group["joints"]:
                    other = record["body0"] or record["body1"]
                    self.notes.append(
                        f"The joint {record['path']} only attaches {other} to the world; that is not a robot, so it "
                        "is not listed."
                    )
        return robots

    def _seeds(self, root: Any, records: list[dict[str, Any]]) -> set[str]:
        path = str(root.GetPath())
        if root.IsA(UsdPhysics.Joint):
            for record in records:
                if record["path"] == path:
                    return {body for body in (record["body0_body"], record["body1_body"]) if body}
            return set()
        if RIGID_API in self._applied(root):
            return {path}
        return {body for body in self.rigid if _is_under(body, path)}

    def _sort_key(self, path: str) -> tuple[int, str]:
        return (self.order.get(path, 10**9), path)

    # -- one robot --

    def _robot(self, group: dict[str, Any]) -> dict[str, Any]:
        roots = group["roots"]
        links = sorted(group["links"], key=self._sort_key)
        link_set = set(links)
        records = sorted(group["joints"], key=lambda record: self._sort_key(record["path"]))
        problems: list[dict[str, Any]] = []

        if roots:
            root_path = str(roots[0].GetPath())
            articulation_root: str | None = root_path
        else:
            root_path = _common_ancestor(links or [record["path"] for record in records])
            articulation_root = None
            problems.append(
                _problem(
                    "warning",
                    "no_articulation_root",
                    root_path,
                    "These parts are connected by joints but no prim has PhysicsArticulationRootAPI.",
                    "Without an articulation root PhysX simulates every part as a separate body held together by "
                    "joints, which is less accurate (joints can stretch and wobble), and Isaac Sim's robot tools "
                    "(Articulation, controllers, joint state readers) will not find a robot here.",
                    f"Apply PhysicsArticulationRootAPI to the robot's top prim ({root_path}) or to its base link "
                    "(Isaac Sim: right-click > Add > Physics > Articulation Root).",
                )
            )
        name = Sdf.Path(root_path).name or root_path
        for extra in roots[1:]:
            problems.append(
                _problem(
                    "error",
                    "multiple_articulation_roots",
                    str(extra.GetPath()),
                    f"A second articulation root in the same robot (the first is {root_path}).",
                    "Each connected robot needs exactly one articulation root; with two, PhysX refuses to build the "
                    "articulation or builds a broken one, and the robot falls apart or will not move.",
                    f"Remove PhysicsArticulationRootAPI from {extra.GetPath()} and keep only the one on {root_path}.",
                )
            )
        if roots and not links:
            problems.append(
                _problem(
                    "error",
                    "root_without_links",
                    root_path,
                    "The articulation root has no rigid bodies (links) below it or connected to it.",
                    "An articulation is built from rigid bodies; without any, nothing will simulate.",
                    "Add PhysicsRigidBodyAPI to the robot's parts (links) below the root and connect them with joints.",
                )
            )

        internal: list[dict[str, Any]] = []
        world: list[dict[str, Any]] = []
        joint_entries: list[dict[str, Any]] = []
        for record in records:
            b0, b1 = record["body0_body"], record["body1_body"]
            if b0 and b1 and b0 != b1 and b0 in link_set and b1 in link_set:
                internal.append(record)
                role = "connects two links"
            elif (b0 in link_set and b1 is None and record["body1_status"] in ("empty", "static")) or (
                b1 in link_set and b0 is None and record["body0_status"] in ("empty", "static")
            ):
                world.append(record)
                role = "attaches the robot to the world"
            else:
                role = "broken or outside this robot"
            entry = self._joint_entry(record, problems, to_world=bool(world) and world[-1] is record)
            entry["role"] = role
            joint_entries.append(entry)

        base_info, base_link = self._base(roots, links, internal, world)
        tree_text, edges, reached = self._tree(base_link, links, internal, base_info["fixed_to_world"], problems)
        if base_link is not None:
            for link in links:
                if link not in reached:
                    problems.append(
                        _problem(
                            "warning",
                            "disconnected_link",
                            link,
                            f"The link {_name(link)} is not connected to the base {_name(base_link)} by any joint.",
                            "A rigid body that is not joined to the rest of the robot falls or floats on its own "
                            "instead of moving with the robot.",
                            "Connect it with a joint (a fixed joint if it should simply stay attached), or move it out "
                            "of the robot if it is a separate object.",
                        )
                    )
        link_entries = [self._link_entry(link, records, problems) for link in links]

        dof = 0
        for record in internal:
            if record["enabled"] and not record["excluded"]:
                dof += _record_dof(record)
        for record in world:
            if record["kind"] in ("revolute", "prismatic", "spherical") and record["enabled"]:
                dof += _record_dof(record)
        self._floor_check(root_path, links, base_info["fixed_to_world"], problems)

        world_paths = {record["path"] for record in world}
        counts: dict[str, int] = {}
        for record in records:
            if record["path"] in world_paths:
                continue
            word = JOINT_WORDS[record["kind"]][0]
            counts[word] = counts.get(word, 0) + 1
        severity_rank = {"error": 0, "warning": 1, "info": 2}
        problems.sort(key=lambda item: severity_rank.get(item["severity"], 3))
        robot: dict[str, Any] = {
            "root": root_path,
            "name": name,
            "articulation_root": articulation_root,
            "articulation_roots": [str(root.GetPath()) for root in roots],
            "base": base_info,
            "links": link_entries,
            "joints": joint_entries,
            "tree": tree_text,
            "tree_edges": edges,
            "dof_count": dof,
            "joint_counts": counts,
            "link_count": len(links),
            "joint_count": len(records),
            "scan_incomplete": bool(self.truncated or self.stopped_by_time),
        }
        robot["plain_summary"] = self._summary(robot, records, world, problems)
        if len(problems) > MAX_PROBLEMS_PER_ROBOT:
            robot["problems_truncated"] = len(problems) - MAX_PROBLEMS_PER_ROBOT
            problems = problems[:MAX_PROBLEMS_PER_ROBOT]
        robot["problems"] = problems
        robot["problem_counts"] = {
            level: sum(1 for item in problems if item["severity"] == level) for level in ("error", "warning", "info")
        }
        return robot

    def _base(
        self, roots: list[Any], links: list[str], internal: list[dict[str, Any]], world: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], str | None]:
        base_link: str | None = None
        via: dict[str, Any] | None = None
        enabled_world = [record for record in world if record["enabled"]]
        if enabled_world:
            preferred = sorted(enabled_world, key=lambda record: record["kind"] not in ("fixed", "d6"))
            via = preferred[0]
            base_link = via["body0_body"] if via["body0_body"] in links else via["body1_body"]
        if base_link is None and links:
            root_path = str(roots[0].GetPath()) if roots else None
            if root_path in links:
                base_link = root_path
            else:
                children = {record["body1_body"] for record in internal}
                candidates = [link for link in links if link not in children]
                base_link = candidates[0] if candidates else links[0]
        kinematic = bool(base_link and _value(self.stage.GetPrimAtPath(base_link), "physics:kinematicEnabled", False))
        fixed = via is not None
        info: dict[str, Any] = {
            "link": base_link,
            "name": _name(base_link) if base_link else None,
            "fixed_to_world": fixed,
            "via_joint": via["path"] if via else None,
            "via_joint_type": JOINT_WORDS[via["kind"]][0] if via else None,
            "kinematic": kinematic,
        }
        if via is not None:
            other = via["body1"] if via["body0_body"] == base_link else via["body0"]
            anchor = f"the static prim {other}" if other else "the world"
            info["plain"] = (
                f"The base {_name(base_link)} is attached to {anchor} by the joint {_name(via['path'])}, so the robot "
                "stays in place like an arm bolted to a table."
            )
            if via["kind"] not in ("fixed", "d6"):
                info["plain"] += (
                    f" That joint is a {JOINT_WORDS[via['kind']][0]}, so the whole robot can still "
                    f"{'turn' if via['kind'] != 'prismatic' else 'slide'} on it."
                )
        elif kinematic:
            info["plain"] = (
                f"The base {_name(base_link)} is kinematic, so code or animation holds it in place; gravity does not move it."
            )
        elif base_link:
            info["plain"] = (
                f"The base {_name(base_link)} is not attached to anything (a free or floating base), so the robot can "
                "move and fall like a mobile robot or a humanoid."
            )
        else:
            info["plain"] = "No base link was found because the robot has no links."
        return info, base_link

    def _tree(
        self,
        base: str | None,
        links: list[str],
        internal: list[dict[str, Any]],
        fixed: bool,
        problems: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], set[str]]:
        if base is None:
            return "", [], set()
        neighbours: dict[str, list[tuple[str, dict[str, Any]]]] = {link: [] for link in links}
        for record in internal:
            if record["excluded"]:
                continue
            a, b = record["body0_body"], record["body1_body"]
            neighbours.setdefault(a, []).append((b, record))
            neighbours.setdefault(b, []).append((a, record))
        children: dict[str, list[str]] = {}
        edges: list[dict[str, Any]] = []
        reached = {base}
        used: set[str] = set()
        queue = [base]
        while queue:
            current = queue.pop(0)
            for other, record in neighbours.get(current, []):
                if record["path"] in used:
                    continue
                used.add(record["path"])
                if other in reached:
                    problems.append(
                        _problem(
                            "warning",
                            "closed_loop",
                            record["path"],
                            f"The joint {_name(record['path'])} closes a loop: {_name(other)} is already connected to "
                            "the base another way.",
                            "An articulation must be a tree (every link reached by exactly one path from the base); "
                            "PhysX cannot build a loop into it.",
                            "Set physics:excludeFromArticulation = true on this joint so PhysX solves it as a separate "
                            "loop-closing joint.",
                        )
                    )
                    continue
                reached.add(other)
                children.setdefault(current, []).append(other)
                edges.append(
                    {
                        "parent": current,
                        "child": other,
                        "joint": record["path"],
                        "type": JOINT_WORDS[record["kind"]][0],
                    }
                )
                queue.append(other)

        def render(node: str, depth: int) -> str:
            kids = children.get(node, [])
            label = _name(node)
            if not kids or depth > 200:
                return label
            if len(kids) == 1:
                return f"{label} -> {render(kids[0], depth + 1)}"
            return f"{label} -> (" + ", ".join(render(kid, depth + 1) for kid in kids) + ")"

        text = render(base, 0)
        if fixed:
            text = "world => " + text
        if len(text) > TREE_TEXT_MAX_CHARS:
            text = text[: TREE_TEXT_MAX_CHARS - 3] + "..."
        return text, edges, reached

    # -- links --

    def _link_entry(self, link: str, records: list[dict[str, Any]], problems: list[dict[str, Any]]) -> dict[str, Any]:
        prim = self.stage.GetPrimAtPath(link)
        rigid = RIGID_API in self._applied(prim)
        entry: dict[str, Any] = {
            "path": link,
            "name": _name(link),
            "rigid_body": rigid,
            "kinematic": bool(_value(prim, "physics:kinematicEnabled", False)),
            "instance_proxy": bool(prim.IsInstanceProxy()),
        }
        mass_attr = prim.GetAttribute("physics:mass")
        mass = _float(mass_attr.Get()) if mass_attr and mass_attr.HasAuthoredValue() else None
        density_attr = prim.GetAttribute("physics:density")
        density = _float(density_attr.Get()) if density_attr and density_attr.HasAuthoredValue() else None
        if mass is not None and mass > 0:
            entry["mass"] = _round(mass * self.kgpu, 4)
            entry["mass_source"] = "set on the link with PhysicsMassAPI (kg)"
        else:
            entry["mass"] = "computed by PhysX"
            if density is not None and density > 0:
                entry["mass_source"] = "PhysX computes it from the colliders' volume and the density set on the link"
            else:
                entry["mass_source"] = "PhysX computes it from the colliders' volume and density (default 1000 kg/m^3)"
        if mass is not None and mass < 0:
            problems.append(
                _problem(
                    "error",
                    "negative_mass",
                    link,
                    f"The link {_name(link)} has a negative mass ({_round(mass * self.kgpu, 4)} kg).",
                    "A negative mass is physically impossible; PhysX rejects it or the robot explodes when you press Play.",
                    "Set physics:mass to the real weight of the part in kg (Property panel > Physics > Mass).",
                )
            )
        elif mass is not None and mass == 0:
            problems.append(
                _problem(
                    "warning",
                    "zero_mass",
                    link,
                    f"The link {_name(link)} has its mass set to 0.",
                    "UsdPhysics reads 0 as 'not set', so PhysX guesses the mass from the collider shapes (or uses a "
                    "default if there are none); a wrong guess makes the motors too strong or too weak.",
                    "Set physics:mass to the real weight of the part in kg, or remove the 0 so the choice is explicit.",
                )
            )
        owned = self._collider_owners().get(link, 0)
        incomplete = self.truncated or self.stopped_by_time
        if owned:
            entry["has_collider"] = True
            entry["colliders"] = owned
        elif incomplete:
            entry["has_collider"] = None
            entry["collider_note"] = "unknown: the scan stopped before every prim was read"
        else:
            entry["has_collider"] = False
            entry["colliders"] = 0
            touching = [record for record in records if link in (record["body0_body"], record["body1_body"])]
            frame_only = bool(touching) and all(record["kind"] == "fixed" for record in touching)
            problems.append(
                _problem(
                    "info" if frame_only else "warning",
                    "link_without_collider",
                    link,
                    f"The link {_name(link)} has no collider on itself or on its child geometry.",
                    ("It is attached only by fixed joints, so it is probably a reference frame (such as a tool centre "
                     "point) and needs no collider; check that its mass is small but not zero. " if frame_only else "")
                    + "Without a collider the link passes through the floor, objects and other links, and PhysX cannot "
                    "compute its mass from its shape.",
                    "Select the link's mesh and add a collider (Add > Physics > Collider, approximation convexHull or "
                    "convexDecomposition), or set the mass by hand if it should be a ghost part.",
                )
            )
        parent = prim.GetParent()
        outer = self._owner(parent) if parent and parent.IsValid() else None
        if outer is not None:
            joined = any(
                {record["body0_body"], record["body1_body"]} == {outer, link} for record in records
            )
            if not joined:
                problems.append(
                    _problem(
                        "warning",
                        "nested_rigid_body",
                        link,
                        f"The rigid body {_name(link)} sits inside another rigid body ({_name(outer)}) with no joint "
                        "between them.",
                        "Nested rigid bodies simulate separately, so the inner one does not follow the outer one; it "
                        "falls off or drifts away unless a joint holds it.",
                        f"Add a fixed joint between {_name(outer)} and {_name(link)}, or remove PhysicsRigidBodyAPI "
                        "from the inner prim so it is part of the outer body.",
                    )
                )
        return entry

    # -- joints --

    def _joint_entry(self, record: dict[str, Any], problems: list[dict[str, Any]], *, to_world: bool = False) -> dict[str, Any]:
        prim = record["prim"]
        kind = record["kind"]
        word, meaning, _plural = JOINT_WORDS[kind]
        path = record["path"]
        entry: dict[str, Any] = {
            "path": path,
            "name": _name(path),
            "type": word,
            "kind": kind,
            "usd_type": prim.GetTypeName() or "",
            "meaning": meaning,
            "body0": record["body0"],
            "body1": record["body1"],
            "axis": None,
            "limits": None,
            "dof": _record_dof(record),
            "enabled": record["enabled"],
            "exclude_from_articulation": record["excluded"],
            "instance_proxy": bool(prim.IsInstanceProxy()),
            "connects_to_world": to_world,
        }
        if to_world and kind in ("fixed", "d6"):
            entry["dof"] = 0
            entry["note"] = "A joint from the base link to the world gives the robot a fixed base; it adds no DOF."
        for key in ("body0", "body1"):
            status = record[key + "_status"]
            target = record[key]
            if record[key + "_count"] > 1:
                problems.append(
                    _problem(
                        "warning",
                        "several_body_targets",
                        path,
                        f"{key} of the joint {_name(path)} lists {record[key + '_count']} prims; only the first counts.",
                        "A joint connects exactly two bodies, so the extra targets are ignored.",
                        f"Keep a single target in {key}.",
                    )
                )
            if status == "missing":
                problems.append(
                    _problem(
                        "error",
                        "missing_body",
                        path,
                        f"The joint {_name(path)} {key} points to {target}, which does not exist.",
                        "A joint with a missing body is ignored by PhysX (or attaches to the world), so the parts it "
                        "should connect fall apart.",
                        f"Point {key} at the right link (Property panel > Joint > Body {key[-1]}); the link may have "
                        "been renamed or moved.",
                    )
                )
            elif status == "ancestor":
                problems.append(
                    _problem(
                        "info",
                        "body_not_rigid",
                        path,
                        f"{key} of {_name(path)} points to {target}, which is not itself a rigid body; PhysX uses the "
                        f"rigid body above it ({_name(record[key + '_body'])}).",
                        "This works, but the joint frame is easier to understand when it targets the link itself.",
                        f"Point {key} at {record[key + '_body']} directly.",
                    )
                )
            elif status == "static":
                target_prim = self.stage.GetPrimAtPath(target)
                is_collider = COLLISION_API in self._applied(target_prim)
                problems.append(
                    _problem(
                        "info" if is_collider else "warning",
                        "body_not_rigid",
                        path,
                        f"{key} of {_name(path)} points to {target}, which is not a rigid body (no "
                        "PhysicsRigidBodyAPI), so PhysX treats that side as the fixed world.",
                        "If that prim should move, the joint will instead pin the other part in place.",
                        f"Add PhysicsRigidBodyAPI to {target} if it should move; leave it if it is a static anchor "
                        "such as a table.",
                    )
                )
        if record["body0"] is None and record["body1"] is None:
            problems.append(
                _problem(
                    "error",
                    "joint_connects_nothing",
                    path,
                    f"The joint {_name(path)} has neither body0 nor body1.",
                    "A joint without bodies connects nothing, so it has no effect.",
                    "Set body0 and body1 to the two links it should connect, or delete the joint.",
                )
            )
        elif record["body0_body"] and record["body0_body"] == record["body1_body"]:
            problems.append(
                _problem(
                    "error",
                    "same_body",
                    path,
                    f"The joint {_name(path)} connects {_name(record['body0_body'])} to itself.",
                    "A joint must connect two different bodies; PhysX rejects this one.",
                    "Point body0 at the parent link and body1 at the child link.",
                )
            )
        if not record["enabled"]:
            problems.append(
                _problem(
                    "info",
                    "joint_disabled",
                    path,
                    f"The joint {_name(path)} is disabled (physics:jointEnabled = false).",
                    "A disabled joint holds nothing, so the parts it connects separate.",
                    "Set physics:jointEnabled to true if that was not intended.",
                )
            )

        axis = prim.GetAttribute("physics:axis")
        if kind in ("revolute", "prismatic", "spherical") and axis and axis.IsValid():
            entry["axis"] = str(axis.Get())
        entry["limits"] = self._limits(prim, kind, path, problems)

        drives = self._drives(prim, kind)
        entry["drives"] = drives
        entry["drive"] = drives[0] if drives else None
        mimic = _mimic(prim)
        for drive in drives:
            if not drive["stiffness"] and not drive["damping"] and not mimic:
                problems.append(
                    _problem(
                        "warning",
                        "drive_without_strength",
                        path,
                        f"The {drive['type']} drive on {_name(path)} has stiffness 0 and damping 0.",
                        "A drive with no stiffness and no damping pushes with zero force, so the joint flops around "
                        "under gravity and ignores every target you send it.",
                        "For position control set stiffness (and some damping); for speed control (wheels) set damping "
                        "with stiffness 0. Isaac Sim's Gain Tuner helps pick values.",
                    )
                )
        if mimic:
            entry["mimics"] = mimic
        movable = kind in ("revolute", "prismatic", "spherical", "d6") and not (to_world and kind == "d6")
        if not drives and not mimic and movable and not record["excluded"]:
            problems.append(
                _problem(
                    "info",
                    "no_drive",
                    path,
                    f"The {word} {_name(path)} has no drive (motor).",
                    "Without a drive nothing can move this joint on purpose; it only swings with gravity and contacts.",
                    "Add a drive (Add > Physics > Angular Drive or Linear Drive) if code or a controller should move it.",
                )
            )
        entry["plain"] = self._joint_sentence(entry, record)
        return entry

    def _limits(self, prim: Any, kind: str, path: str, problems: list[dict[str, Any]]) -> Any:
        if kind in ("revolute", "prismatic"):
            lower = _float(_value(prim, "physics:lowerLimit", None))
            upper = _float(_value(prim, "physics:upperLimit", None))
            angular = kind == "revolute"
            if lower is None or upper is None or not math.isfinite(lower) or not math.isfinite(upper):
                problems.append(
                    _problem(
                        "warning",
                        "no_limits",
                        path,
                        f"The {JOINT_WORDS[kind][0]} {_name(path)} has no limits"
                        + (", so it can spin forever." if angular else ", so it can slide forever."),
                        "Real robot joints have end stops; without limits the arm can pass through itself, wrap cables "
                        "or reach poses the real robot never can. (Wheels are the exception: they should spin freely.)",
                        "Set physics:lowerLimit and physics:upperLimit (degrees for hinges, stage units for sliders) "
                        "from the robot's datasheet.",
                    )
                )
                return None
            scale = 1.0 if angular else self.mpu
            limits = {
                "lower": _round(lower * scale, 3 if angular else 5),
                "upper": _round(upper * scale, 3 if angular else 5),
                "unit": "degrees" if angular else "metres",
            }
            if lower > upper:
                limits["locked"] = True
                problems.append(
                    _problem(
                        "warning",
                        "inverted_limits",
                        path,
                        f"The lower limit of {_name(path)} is above its upper limit.",
                        "PhysX treats a joint whose lower limit is above its upper limit as locked, so it cannot move.",
                        "Swap the two values so lowerLimit is the smaller one.",
                    )
                )
            return limits
        if kind == "spherical":
            cone0 = _float(_value(prim, "physics:coneAngle0Limit", None))
            cone1 = _float(_value(prim, "physics:coneAngle1Limit", None))
            if cone0 is None or cone1 is None or cone0 < 0 or cone1 < 0:
                return None
            return {"cone_angle0": _round(cone0, 3), "cone_angle1": _round(cone1, 3), "unit": "degrees"}
        if kind == "distance":
            low = _float(_value(prim, "physics:minDistance", None))
            high = _float(_value(prim, "physics:maxDistance", None))
            return {
                "min": _round(low * self.mpu, 5) if low is not None and low >= 0 else None,
                "max": _round(high * self.mpu, 5) if high is not None and high >= 0 else None,
                "unit": "metres",
            }
        if kind == "d6":
            axes: dict[str, Any] = {}
            for axis in D6_AXES:
                if f"PhysicsLimitAPI:{axis}" not in self._applied(prim):
                    continue
                low = _float(_value(prim, f"limit:{axis}:physics:low", None))
                high = _float(_value(prim, f"limit:{axis}:physics:high", None))
                angular = axis.startswith("rot")
                if low is not None and high is not None and math.isfinite(low) and math.isfinite(high) and low > high:
                    axes[axis] = "locked"
                    continue
                scale = 1.0 if angular else self.mpu
                axes[axis] = {
                    "lower": _round(low * scale, 5) if low is not None else None,
                    "upper": _round(high * scale, 5) if high is not None else None,
                    "unit": "degrees" if angular else "metres",
                }
            return axes or None
        return None

    def _drives(self, prim: Any, kind: str) -> list[dict[str, Any]]:
        instances = sorted(name.split(":", 1)[1] for name in self._applied(prim) if name.startswith("PhysicsDriveAPI:"))
        if kind == "revolute" and "angular" in instances:
            instances.remove("angular")
            instances.insert(0, "angular")
        drives = []
        for instance in instances:
            angular = instance == "angular" or instance.startswith("rot")
            prefix = f"drive:{instance}:physics:"
            stiffness = _float(_value(prim, prefix + "stiffness", 0.0)) or 0.0
            damping = _float(_value(prim, prefix + "damping", 0.0)) or 0.0
            max_force = _float(_value(prim, prefix + "maxForce", None))
            position = _float(_value(prim, prefix + "targetPosition", 0.0)) or 0.0
            velocity = _float(_value(prim, prefix + "targetVelocity", 0.0)) or 0.0
            scale = 1.0 if angular else self.mpu
            unit = "degrees" if angular else "metres"
            if stiffness > 0:
                control = "position control: stiffness pulls the joint toward target_position like a spring"
            elif damping > 0:
                control = "velocity control: damping pushes the joint toward target_velocity (stiffness is 0)"
            else:
                control = "no strength: stiffness and damping are both 0"
            target = f"{_round(position * scale, 3)} {unit}" if stiffness > 0 else f"{_round(velocity * scale, 3)} {unit}/s"
            if max_force is not None and math.isfinite(max_force):
                limit = f"at most {_round(max_force, 3)} " + ("N*m of torque" if angular else "N of force")
            else:
                limit = "no force limit"
            drives.append(
                {
                    "instance": instance,
                    "type": "angular" if angular else "linear",
                    "mode": str(_value(prim, prefix + "type", "force")),
                    "stiffness": _round(stiffness, 6),
                    "damping": _round(damping, 6),
                    "max_force": _round(max_force, 6) if max_force is not None and math.isfinite(max_force) else None,
                    "target_position": _round(position * scale, 6),
                    "target_velocity": _round(velocity * scale, 6),
                    "units": {
                        "target_position": unit,
                        "target_velocity": unit + "/second",
                        "max_force": "N*m (torque)" if angular else "N (force)",
                        "stiffness_damping": "per degree, as authored" if angular else "per stage unit, as authored",
                    },
                    "control": control,
                    "plain": f"A motor ({'turning' if angular else 'sliding'}) with stiffness {_round(stiffness, 3)} and "
                    f"damping {_round(damping, 3)} drives it toward {target}, with {limit}.",
                }
            )
        return drives

    def _joint_sentence(self, entry: dict[str, Any], record: dict[str, Any]) -> str:
        a = _name(record["body0"]) if record["body0"] else "the world"
        b = _name(record["body1"]) if record["body1"] else "the world"
        text = f"{entry['name']} is a {entry['type']} joint between {a} and {b}: it {entry['meaning']}."
        limits = entry["limits"]
        if entry["kind"] in ("revolute", "prismatic"):
            verb = "turns around" if entry["kind"] == "revolute" else "slides along"
            if limits:
                unit = "degrees" if entry["kind"] == "revolute" else "m"
                text += f" It {verb} its {entry['axis']} axis from {limits['lower']} to {limits['upper']} {unit}."
            else:
                text += f" It {verb} its {entry['axis']} axis with no limits."
        if entry.get("mimics"):
            mimic = entry["mimics"]
            ref = _name(mimic["reference_joint"]) if mimic.get("reference_joint") else "another joint"
            text += f" It copies {ref} (gearing {mimic.get('gearing')}) instead of having its own motor."
        elif entry["drive"]:
            text += " " + entry["drive"]["plain"]
        return text

    # -- floor --

    def _floor_check(self, root_path: str, links: list[str], fixed: bool, problems: list[dict[str, Any]]) -> None:
        if not links or not self.floors or (self.budget is not None and self.budget.expired()):
            return
        try:
            if self._bbox_cache is None:
                self._bbox_cache = UsdGeom.BBoxCache(
                    Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy], True
                )
            cache = self._bbox_cache
            bottom = None
            for link in links:
                box = cache.ComputeWorldBound(self.stage.GetPrimAtPath(link)).ComputeAlignedRange()
                if box.IsEmpty():
                    continue
                low = float(box.GetMin()[self.up_index])
                bottom = low if bottom is None else min(bottom, low)
            if bottom is None:
                return
            best = None
            robot_paths = set(links) | {root_path}
            for floor in self.floors:
                floor_path = str(floor.GetPath())
                if any(_is_under(floor_path, path) for path in robot_paths):
                    continue
                box = cache.ComputeWorldBound(floor).ComputeAlignedRange()
                if box.IsEmpty():
                    continue
                size = box.GetSize()
                sides = [index for index in range(3) if index != self.up_index]
                area = abs(float(size[sides[0]]) * float(size[sides[1]]))
                if best is None or area > best[0]:
                    best = (area, float(box.GetMax()[self.up_index]), floor_path)
        except Exception as exc:  # bounding boxes are only a hint
            self.notes.append(f"The floor check was skipped: {exc}")
            return
        if best is None:
            return
        gap = (bottom - best[1]) * self.mpu
        if gap > FLOAT_TOLERANCE_M:
            then = (
                "it is fixed to the world, so it will hang in the air."
                if fixed
                else "its base is free, so it will drop onto the floor when you press Play."
            )
            problems.append(
                _problem(
                    "info",
                    "floating_above_ground",
                    root_path,
                    f"The robot's lowest point is {_round(gap, 3)} m above the floor {_name(best[2])}; {then}",
                    "A robot that starts in the air either drops with a jolt or is mounted in mid-air, which is "
                    "usually not what you meant.",
                    f"Move the robot down by about {_round(gap, 3)} m (or raise the floor) so it starts touching it.",
                )
            )
        elif gap < -FLOAT_TOLERANCE_M:
            problems.append(
                _problem(
                    "info",
                    "below_ground",
                    root_path,
                    f"The robot's lowest point is {_round(-gap, 3)} m below the top of the floor {_name(best[2])}.",
                    "Parts that start inside the floor are pushed out violently when the simulation starts.",
                    f"Move the robot up by about {_round(-gap, 3)} m so it starts on the floor.",
                )
            )

    # -- summary --

    def _summary(
        self,
        robot: dict[str, Any],
        records: list[dict[str, Any]],
        world: list[dict[str, Any]],
        problems: list[dict[str, Any]],
    ) -> str:
        name = robot["name"]
        links = robot["link_count"]
        world_paths = {record["path"] for record in world}
        counted = [record for record in records if record["path"] not in world_paths]
        phrases = []
        for kind in JOINT_WORDS:
            count = sum(1 for record in counted if record["kind"] == kind)
            if count:
                plural = JOINT_WORDS[kind][2]
                phrases.append(f"{count} {plural}{'s' if count != 1 else ''}")
        joined = _join(phrases) if phrases else "no joints"
        parts = []
        if robot["articulation_root"]:
            parts.append(
                f"{name} is a robot with {links} link{'s' if links != 1 else ''} (rigid parts) connected by {joined}."
            )
        else:
            parts.append(
                f"{name} looks like a robot or mechanism with {links} part{'s' if links != 1 else ''} connected by "
                f"{joined}, but it has no articulation root."
            )
        dof = robot["dof_count"]
        if dof:
            parts.append(f"It can move in {dof} independent way{'s' if dof != 1 else ''} ({dof} degrees of freedom).")
        else:
            parts.append("It has no moving joints (0 degrees of freedom).")
        parts.append(robot["base"]["plain"])
        movable = [record for record in counted if record["kind"] in ("revolute", "prismatic", "spherical", "d6")]
        if movable:
            entries = {entry["path"]: entry for entry in robot["joints"]}
            driven = sum(1 for record in movable if entries[record["path"]]["drive"])
            mimics = sum(1 for record in movable if entries[record["path"]].get("mimics"))
            sentence = f"{driven} of its {len(movable)} moving joints have motors (drives)"
            if mimics:
                sentence += f" and {mimics} cop{'ies' if mimics == 1 else 'y'} another joint"
            parts.append(sentence + ".")
        if problems:
            levels = {level: sum(1 for item in problems if item["severity"] == level) for level in ("error", "warning", "info")}
            parts.append(
                f"I found {len(problems)} thing{'s' if len(problems) != 1 else ''} to check ({levels['error']} "
                f"error(s), {levels['warning']} warning(s), {levels['info']} note(s)); first: {problems[0]['problem']}"
            )
        else:
            parts.append("No setup problems were found.")
        if robot["scan_incomplete"]:
            parts.append("The scan stopped early, so this description may be incomplete.")
        return " ".join(parts)


# --- helpers ----------------------------------------------------------------------------------------------


def _joint_kind(prim: Any) -> str:
    if prim.IsA(UsdPhysics.RevoluteJoint):
        return "revolute"
    if prim.IsA(UsdPhysics.PrismaticJoint):
        return "prismatic"
    if prim.IsA(UsdPhysics.FixedJoint):
        return "fixed"
    if prim.IsA(UsdPhysics.SphericalJoint):
        return "spherical"
    if prim.IsA(UsdPhysics.DistanceJoint):
        return "distance"
    return "d6"


def _record_dof(record: dict[str, Any]) -> int:
    kind = record["kind"]
    if kind != "d6":
        return JOINT_DOF[kind]
    prim = record["prim"]
    applied = {str(name) for name in prim.GetAppliedSchemas()}
    free = 0
    for axis in D6_AXES:
        if f"PhysicsLimitAPI:{axis}" in applied:
            low = _float(_value(prim, f"limit:{axis}:physics:low", None))
            high = _float(_value(prim, f"limit:{axis}:physics:high", None))
            if low is not None and high is not None and math.isfinite(low) and math.isfinite(high) and low > high:
                continue
        free += 1
    return free


def _structural(record: dict[str, Any]) -> bool:
    statuses = (record["body0_status"], record["body1_status"])
    if "missing" in statuses:
        return True
    if record["body0"] is None and record["body1"] is None:
        return True
    return bool(record["body0_body"] and record["body0_body"] == record["body1_body"])


def _mimic(prim: Any) -> dict[str, Any] | None:
    """PhysX mimic joints (physxMimicJoint:*) are read from plain properties, so no PhysxSchema is needed."""
    try:
        reference = None
        gearing = None
        found = False
        for prop in prim.GetProperties():
            name = prop.GetName()
            if not name.startswith("physxMimicJoint:"):
                continue
            found = True
            if name.endswith(":referenceJoint") and hasattr(prop, "GetTargets"):
                targets = prop.GetTargets()
                reference = str(targets[0]) if targets else reference
            elif name.endswith(":gearing") and hasattr(prop, "Get"):
                gearing = _round(_float(prop.Get()), 6)
        return {"reference_joint": reference, "gearing": gearing} if found else None
    except Exception:
        return None


def _problem(severity: str, code: str, path: str, problem: str, why: str, fix: str) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "path": path,
        "problem": problem,
        "why_it_matters": why,
        "how_to_fix": fix,
    }


def _value(prim: Any, name: str, fallback: Any) -> Any:
    try:
        attr = prim.GetAttribute(name)
        if not attr or not attr.IsValid():
            return fallback
        value = attr.Get()
    except Exception:
        return fallback
    return fallback if value is None else value


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: Any, digits: int) -> float | None:
    number = _float(value)
    if number is None or not math.isfinite(number):
        return None
    rounded = round(number, digits)
    return 0.0 if rounded == 0 else rounded


def _positive(value: Any, fallback: float) -> float:
    number = _float(value)
    return number if number is not None and math.isfinite(number) and number > 0 else fallback


def _name(path: str | None) -> str:
    if not path:
        return ""
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _is_under(path: str, ancestor: str) -> bool:
    return path == ancestor or path.startswith(ancestor.rstrip("/") + "/")


def _common_ancestor(paths: list[str]) -> str:
    if not paths:
        return "/"
    split = [path.strip("/").split("/") for path in paths]
    common: list[str] = []
    for parts in zip(*split):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    if len(paths) == 1 and len(common) > 1:
        common = common[:-1]  # one body: use its parent as the group
    return "/" + "/".join(common) if common else "/"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
