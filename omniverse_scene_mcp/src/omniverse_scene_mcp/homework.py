"""Homework checkers for mentorship sessions 3-5, read-only.

Each check looks for what the homework asks for by meaning (references, materials, rigid bodies, joint types and
drives), not by exact prim paths, so any reasonable scene layout is graded. Results follow assignments.py: each rule
is pass / partial / fail / unknown with evidence, why it matters and how to fix it, plus the single next step.
Session 2's warehouse is graded by assignment_check.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Callable

try:
    from pxr import Usd, UsdGeom, UsdPhysics, UsdShade
except ImportError:  # pragma: no cover
    Usd = UsdGeom = UsdPhysics = UsdShade = None

MAX_PRIMS = 50000
POINTS = {"pass": 1.0, "partial": 0.5, "fail": 0.0}


def _row(rule: str, title: str, weight: int, status: str, evidence: Any, why: str, how: list[str]) -> dict[str, Any]:
    return {"rule": rule, "title": title, "weight": weight, "status": status, "evidence": evidence,
            "why": why, "how_to_fix": [] if status == "pass" else how}


def _prims(stage: Any) -> list[Any]:
    out = []
    for prim in stage.Traverse():
        if len(out) >= MAX_PRIMS:
            break
        name = prim.GetName()
        if name == "Render" or name.startswith("OmniverseKit"):
            continue
        out.append(prim)
    return out


def _named(prims: list[Any], *words: str) -> list[Any]:
    """Objects whose name contains a word: a prim with its own rigid body, or a shape not inside such an object.

    A group folder ("Pins") is not an object, and neither is a pin asset's inner mesh. Falls back to the top-most
    matches when nothing qualifies (e.g. shelves without physics).
    """
    hits = [p for p in prims if any(w in p.GetName().lower() for w in words) and not p.IsA(UsdPhysics.Joint)]
    rigid = {p.GetPath() for p in hits if p.HasAPI(UsdPhysics.RigidBodyAPI)}
    objects = [p for p in hits if p.GetPath() in rigid or (
        p.IsA(UsdGeom.Gprim) and not any(p.GetPath().HasPrefix(q) and p.GetPath() != q for q in rigid))]
    if objects:
        return objects
    paths = {p.GetPath() for p in hits}
    return [p for p in hits if not any(p.GetPath().HasPrefix(q) and p.GetPath() != q for q in paths)]


def _in_subtree(prim: Any, test: Callable[[Any], bool], cap: int = 300) -> bool:
    for count, item in enumerate(Usd.PrimRange(prim)):
        if count >= cap:
            return False
        if test(item):
            return True
    return False


def _rigid(prim: Any) -> bool:
    return _in_subtree(prim, lambda p: p.HasAPI(UsdPhysics.RigidBodyAPI))


def _collider(prim: Any) -> bool:
    return _in_subtree(prim, lambda p: p.HasAPI(UsdPhysics.CollisionAPI))


def _kinematic(prim: Any) -> bool:
    return _in_subtree(prim, lambda p: bool(p.GetAttribute("physics:kinematicEnabled").Get())
                       if p.GetAttribute("physics:kinematicEnabled") else False)


def _material(prim: Any) -> str | None:
    material, _ = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    return str(material.GetPath()) if material else None


def _ref_assets(prim: Any) -> list[str]:
    assets = []
    for spec in prim.GetPrimStack():
        for item in list(spec.referenceList.GetAddedOrExplicitItems()) + list(spec.payloadList.GetAddedOrExplicitItems()):
            if item.assetPath:
                assets.append(item.assetPath.replace("\\", "/").rsplit("/", 1)[-1])
    return assets


def _score(rows: list[dict[str, Any]]) -> tuple[float, bool]:
    known = [r for r in rows if r["status"] in POINTS]
    total = sum(r["weight"] for r in known) or 1
    earned = sum(r["weight"] * POINTS[r["status"]] for r in known)
    return round(100 * earned / total, 1), all(r["status"] == "pass" for r in rows)


# ------------------------------------------------------------------------------------------------ session 3
def _session3(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    rows = []
    groups: Counter = Counter()
    for prim in prims:
        for asset in set(_ref_assets(prim)):
            groups[asset] += 1
    biggest = groups.most_common(1)[0] if groups else (None, 0)
    status = "pass" if biggest[1] >= 10 else "partial" if biggest[1] >= 3 else "fail"
    rows.append(_row("cartons_referenced", "Carton boxes are references", 30, status,
                     {"most_referenced_asset": biggest[0], "copies": biggest[1]},
                     "Each carton should point to the same asset file, so one change to it updates all ten.",
                     ["Drag the carton asset from Content > Isaac Sim > Environments > Simple_Warehouse > Props ten times "
                      "(each drag adds a reference), and stack them 4-3-2-1.",
                      "Remove the plain cube boxes they replace."]))
    distinct = len(groups)
    rows.append(_row("library_props", "Props from the asset library", 20,
                     "pass" if distinct >= 3 else "partial" if distinct >= 1 else "fail",
                     {"different_assets_referenced": distinct, "examples": [a for a, _ in groups.most_common(5)]},
                     "The homework asks you to build with ready-made props, not only plain shapes.",
                     ["Add at least three different props (shelves, crates, signs) from the asset library."]))
    for rule, title, words, weight in (("shelf_materials", "Shelves have your own material", ("shelf", "rack"), 25),
                                       ("wall_materials", "Walls have materials or are wall props", ("wall",), 15)):
        found = _named(prims, *words)
        bound = [p for p in found if _material(p) or _ref_assets(p)]
        status = ("unknown" if not found else "pass" if len(bound) == len(found)
                  else "partial" if bound else "fail")
        rows.append(_row(rule, title, weight, status,
                         {"found": [str(p.GetPath()) for p in found[:8]], "with_material": len(bound)},
                         "Session 3 moves from plain white shapes to real materials.",
                         [f"Create a material (Create > Material, e.g. OmniPBR) and drag it onto each {words[0]} "
                          "(or bind it in the Property panel).",
                          f"Name the prims so they include '{words[0]}' if they are not found."]))
    plain = [p for p in prims if p.IsA(UsdGeom.Cube) and any(w in p.GetName().lower() for w in ("box", "cube"))
             and not _ref_assets(p)]
    rows.append(_row("plain_boxes_replaced", "Plain cube boxes replaced", 10, "pass" if not plain else "partial",
                     {"plain_cube_boxes": [str(p.GetPath()) for p in plain[:8]], "count": len(plain)},
                     "The cubes from Session 2 should become carton assets.",
                     ["Delete the remaining plain cube boxes once the referenced cartons are in place."]))
    return rows


# ------------------------------------------------------------------------------------------------ session 4
def _session4(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    rows = []
    scenes = [p for p in prims if p.IsA(UsdPhysics.Scene)]
    rows.append(_row("physics_scene", "A physics scene", 10, "pass" if scenes else "partial",
                     {"physics_scenes": [str(p.GetPath()) for p in scenes]},
                     "The physics scene holds gravity. Isaac Sim uses a default one, but adding it lets you tune it.",
                     ["Create > Physics > Physics Scene (or scene_add_physics_scene)."]))
    floors = [p for p in prims if p.HasAPI(UsdPhysics.CollisionAPI) and not _rigid(p)
              and (p.IsA(UsdGeom.Plane) or any(w in p.GetPath().pathString.lower() for w in ("ground", "floor")))]
    rows.append(_row("floor_collider", "The floor has collision", 15, "pass" if floors else "fail",
                     {"floors": [str(p.GetPath()) for p in floors[:4]]},
                     "Without a floor collider the pins and ball fall forever.",
                     ["Create > Physics > Ground Plane, or give your floor a collider."]))
    pins = _named(prims, "pin")
    ready = [p for p in pins if _rigid(p) and _collider(p) and not _kinematic(p)]
    status = ("fail" if not pins else "pass" if len(ready) == len(pins) and len(pins) >= 3
              else "partial" if ready else "fail")
    rows.append(_row("pins_dynamic", "Pins are dynamic rigid bodies with colliders", 30, status,
                     {"pins": len(pins), "ready": len(ready),
                      "not_ready": [str(p.GetPath()) for p in pins if p not in ready][:8]},
                     "Pins must fall when hit: rigid body to move, collider to be hit, not kinematic.",
                     ["Name your pins so they include 'pin'.",
                      "Select each pin: Add > Physics > Rigid Body with Colliders Preset; leave Kinematic off."]))
    balls = _named(prims, "ball") or [p for p in prims if p.IsA(UsdGeom.Sphere) and _rigid(p)]
    ball = next((b for b in balls if _rigid(b) and _collider(b) and not _kinematic(b)), None)
    rows.append(_row("ball_dynamic", "The ball is a dynamic rigid body with a collider", 25,
                     "pass" if ball else "partial" if balls else "fail",
                     {"ball": str(ball.GetPath()) if ball else None, "candidates": [str(b.GetPath()) for b in balls[:4]]},
                     "The ball has to move and hit the pins.",
                     ["Create a sphere named Ball and add Rigid Body with Colliders Preset."]))
    moving = None
    if ball:
        for item in Usd.PrimRange(ball):
            for name in ("physics:velocity", "physics:angularVelocity"):
                attr = item.GetAttribute(name)
                value = attr.Get() if attr else None
                if value is not None and any(abs(v) > 1e-6 for v in value):
                    moving = {"prim": str(item.GetPath()), name: [float(v) for v in value]}
    rows.append(_row("ball_launched", "The ball starts with a push", 20,
                     "pass" if moving else "partial" if ball else "fail", moving or {},
                     "Something has to send the ball at the pins: an initial velocity, a spin or a slope.",
                     ["Select the ball, and in Property > Physics > Rigid Body set Linear Velocity toward the pins "
                      "(e.g. 0, 5, 0), then Play.",
                      "Or place it on a slope so gravity rolls it."]))
    return rows


# ------------------------------------------------------------------------------------------------ session 5
def _bodies(joint: Any) -> list[Any]:
    schema = UsdPhysics.Joint(joint)
    out = []
    for rel in (schema.GetBody0Rel(), schema.GetBody1Rel()):
        for target in rel.GetTargets() if rel else []:
            prim = joint.GetStage().GetPrimAtPath(target)
            if prim:
                out.append(prim)
    return out


def _session5(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    rows = []
    joints = [p for p in prims if p.IsA(UsdPhysics.Joint)]
    prismatic = [j for j in joints if j.IsA(UsdPhysics.PrismaticJoint)]

    def limited(joint: Any, schema: Any) -> bool:
        lo, hi = schema(joint).GetLowerLimitAttr().Get(), schema(joint).GetUpperLimitAttr().Get()
        return lo is not None and hi is not None and abs(lo) < 1e30 and abs(hi) < 1e30 and lo < hi

    drawers = [j for j in prismatic if limited(j, UsdPhysics.PrismaticJoint)]
    rows.append(_row("drawers", "Two drawers on prismatic joints with limits", 30,
                     "pass" if len(drawers) >= 2 else "partial" if prismatic else "fail",
                     {"prismatic_joints": len(prismatic), "with_limits": len(drawers),
                      "joints": [str(j.GetPath()) for j in prismatic[:6]]},
                     "A drawer slides along one axis and must stop at both ends.",
                     ["Select the table, then a drawer: right-click > Create > Physics > Joint > Prismatic Joint "
                      "(or scene_create_joint kind=prismatic).",
                      "Set lower and upper limits so the drawer stops when open and closed."]))
    revolute = [j for j in joints if j.IsA(UsdPhysics.RevoluteJoint)]
    closing = [j for j in revolute if j.HasAPI(UsdPhysics.DriveAPI, "angular")
               and (UsdPhysics.DriveAPI(j, "angular").GetStiffnessAttr().Get() or 0) > 0]
    rows.append(_row("self_closing_door", "A door that swings back by itself", 25,
                     "pass" if closing else "partial" if revolute else "fail",
                     {"revolute_joints": len(revolute), "with_spring_drive": [str(j.GetPath()) for j in closing[:4]]},
                     "A self-closing door is a revolute joint whose drive pulls it back to closed.",
                     ["Put a revolute joint on the door's edge (scene_create_joint kind=revolute with a pivot on the edge).",
                      "Add an angular drive with target position 0 and stiffness > 0 (scene_add_drive), plus some damping."]))
    shared: dict[str, list[str]] = defaultdict(list)
    for joint in revolute:
        for body in _bodies(joint):
            shared[str(body.GetPath())].append(str(joint.GetPath()))
    base, wheels = max(shared.items(), key=lambda kv: len(kv[1]), default=(None, []))
    rows.append(_row("robot_wheels", "A ground robot with wheels on revolute joints", 30,
                     "pass" if len(wheels) >= 4 else "partial" if len(wheels) >= 2 else "fail",
                     {"base": base, "wheel_joints": len(wheels)},
                     "Each wheel turns on its own revolute joint attached to the robot's base.",
                     ["Make a box base and four cylinders turned 90 degrees on Y, all rigid bodies with colliders.",
                      "Join each wheel to the base with a revolute joint on the wheel's axle axis."]))
    static_pairs = [str(j.GetPath()) for j in joints if _bodies(j) and not any(_rigid(b) for b in _bodies(j))]
    rows.append(_row("joint_bodies", "Every joint has a moving body", 10, "pass" if not static_pairs else "fail",
                     {"joints_between_static_bodies": static_pairs[:6]},
                     "A joint needs at least one dynamic rigid body, or physics ignores it.",
                     ["Give the moving part of each listed joint a rigid body."]))
    roots = [str(p.GetPath()) for p in prims if p.HasAPI(UsdPhysics.ArticulationRootAPI)]
    rows.append(_row("articulation_root", "The robot has an articulation root (recommended)", 5,
                     "pass" if roots else "partial", {"articulation_roots": roots[:4]},
                     "An articulation root makes PhysX treat the robot as one machine, which helps drives behave.",
                     ["Add an articulation root to the robot's base (scene_add_articulation_root)."]))
    return rows


SESSIONS = {
    3: ("Session 3: upgrade your warehouse with real assets", _session3),
    4: ("Session 4: bowling, knock every pin down", _session4),
    5: ("Session 5: drawers, a self-closing door and a ground robot", _session5),
}


def homework_sessions() -> dict[str, Any]:
    """The homework checkers available (Session 2's warehouse is assignment_check)."""
    return {"ok": True, "sessions": [{"session": k, "title": v[0]} for k, v in SESSIONS.items()],
            "also": "Session 2 warehouse: assignment_check()"}


def check_homework(stage: Any, session: int) -> dict[str, Any]:
    """Read-only: grade the open scene against a session's homework (3, 4 or 5). Each check is pass, partial, fail
    or unknown with evidence and how to fix it; next_step names the most useful thing to do now."""
    if Usd is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    if stage is None:
        return {"ok": False, "error": "No stage is open"}
    if isinstance(session, bool) or session not in SESSIONS:
        return {"ok": False, "error": "session must be 3, 4 or 5 (Session 2's warehouse is assignment_check)"}
    dirty = {layer.identifier: layer.dirty for layer in stage.GetUsedLayers()}
    title, checker = SESSIONS[session]
    rows = checker(stage, _prims(stage))
    score, passed = _score(rows)
    order = {"fail": 0, "partial": 1, "unknown": 2}
    todo = sorted((r for r in rows if r["status"] != "pass"), key=lambda r: (order[r["status"]], -r["weight"]))
    next_step = (f"{todo[0]['title']}: {todo[0]['how_to_fix'][0]}" if todo and todo[0]["how_to_fix"]
                 else "Everything is in place. Press Play to check it behaves, then save.")
    changed = any(layer.dirty != dirty.get(layer.identifier, layer.dirty) for layer in stage.GetUsedLayers())
    counts = Counter(r["status"] for r in rows)
    return {
        "ok": True, "session": session, "title": title, "score_pct": score, "passed": passed,
        "counts": dict(counts), "results": rows, "next_step": next_step,
        "summary": f"{counts.get('pass', 0)} of {len(rows)} checks pass ({score}%).",
        "read_only": not changed,
        "note": "Checks the scene's structure. Press Play to see whether it behaves as the homework asks.",
    }
