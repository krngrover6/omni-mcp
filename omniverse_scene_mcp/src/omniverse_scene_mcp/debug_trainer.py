"""Debugging practice (class sessions 12 and 15): find out why a simulation does not behave, and train on broken scenes.

scene_diagnose reads a stage and lists the usual reasons a scene misbehaves: each finding names the SYMPTOM you
would see ("the box falls through the floor"), the cause, the prim, and the fix. It is read-only.

Debug challenges are small scenes with one hidden bug, written as NEW files under isaac_study/debug/ (never over
an existing file). Open one, press Play, describe what you see and what you think is wrong; the checker gives
hints first and the answer last, like the class's "instructor breaks a scene" test.
"""

from __future__ import annotations

import os
from itertools import combinations
from pathlib import Path
from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    Gf = Sdf = Usd = UsdGeom = UsdPhysics = None

from .exam.progress import workspace_root

MAX_PRIMS = 20000
MAX_OVERLAP_BODIES = 150
_SYSTEM_ROOTS = ("/Render", "/Replicator", "/Orchestrator", "/OmniverseKit")


def _rigid(prim: Any) -> bool:
    enabled = prim.GetAttribute("physics:rigidBodyEnabled")
    return prim.HasAPI(UsdPhysics.RigidBodyAPI) and not (enabled and enabled.Get() is False)


def _kinematic(prim: Any) -> bool:
    attribute = prim.GetAttribute("physics:kinematicEnabled")
    return bool(attribute and attribute.Get())


def _has_collider(prim: Any) -> bool:
    # Seen live: a Franka's collision shapes sit under instanced prims, which the default traversal skips, so every
    # link looked collider-less. Walk instance proxies too.
    for count, item in enumerate(Usd.PrimRange(prim, Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate))):
        if item.HasAPI(UsdPhysics.CollisionAPI):
            return True
        if count > 2000:
            break
    return False


def _finding(severity: str, code: str, prim: Any, symptom: str, cause: str, fix: str) -> dict[str, Any]:
    return {"severity": severity, "code": code, "prim": str(prim.GetPath()) if prim is not None else None,
            "symptom": symptom, "cause": cause, "fix": fix}


def diagnose(stage: Any, focus: str | None = None) -> dict[str, Any]:
    """Read-only: why might this simulation misbehave? Findings name the symptom, the cause, the prim and the fix.
    focus narrows to one kind: 'physics', 'joints', 'graph', 'assets' or 'scene'."""
    if Usd is None:
        return {"ok": False, "error": "pxr (USD) is not available here."}
    if stage is None:
        return {"ok": False, "error": "No stage is open."}
    if focus is not None and focus not in ("physics", "joints", "graph", "assets", "scene"):
        return {"ok": False, "error": "focus must be physics, joints, graph, assets or scene."}
    prims = []
    for prim in stage.Traverse():
        if len(prims) >= MAX_PRIMS:
            break
        if not str(prim.GetPath()).startswith(_SYSTEM_ROOTS):
            prims.append(prim)
    findings: list[dict[str, Any]] = []
    if focus in (None, "scene"):
        findings += _scene_checks(stage, prims)
    if focus in (None, "physics"):
        findings += _physics_checks(stage, prims)
    if focus in (None, "joints"):
        findings += _joint_checks(stage, prims)
    if focus in (None, "graph"):
        findings += _graph_checks(stage, prims)
    if focus in (None, "assets"):
        findings += _asset_checks(stage, prims)
    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda item: order[item["severity"]])
    head = findings[0] if findings else None
    return {
        "ok": True, "read_only": True, "focus": focus, "checked_prims": len(prims), "count": len(findings),
        "findings": findings[:40], "truncated": len(findings) > 40,
        "start_with": (f"{head['symptom']} -> {head['cause']} Fix: {head['fix']}" if head else
                       "No common problem found. Press Play and describe exactly what goes wrong (it falls? it does "
                       "not move? nothing happens on a key press?), then check kit_explain_recent_errors."),
        "note": "Structure checks only: a scene can pass and still behave unexpectedly, so always press Play to confirm.",
    }


def _scene_checks(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    out = []
    mpu = UsdGeom.GetStageMetersPerUnit(stage)
    if abs(mpu - 1.0) > 1e-6:
        out.append(_finding("warning", "units", None, "Things look 100x too big or small, or physics feels wrong.",
                            f"metersPerUnit is {mpu:g}; Isaac Sim works in metres (1).",
                            "Use a metres stage, or wrap the asset with a scale (asset_wrap_units)."))
    for prim in prims:
        if not prim.IsA(UsdGeom.Imageable):
            continue
        visibility = prim.GetAttribute("visibility")
        if visibility and visibility.Get() == "invisible":
            out.append(_finding("info", "invisible", prim, "It is in the Stage panel but not in the viewport.",
                                "Its visibility is 'invisible' (everything under it hides too).",
                                "Set visibility back to 'inherited' (scene_set_attribute)."))
    return out


def _physics_checks(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    out = []
    rigid = [p for p in prims if _rigid(p)]
    scene = [p for p in prims if p.IsA(UsdPhysics.Scene)] or [p for p in stage.Traverse() if p.IsA(UsdPhysics.Scene)]
    if rigid and not scene:
        out.append(_finding("error", "no_physics_scene", None, "Press Play and nothing falls or moves.",
                            "There is no PhysicsScene, so physics never runs.",
                            "Add one (scene_add_physics_scene)."))
    for prim in rigid:
        if not _has_collider(prim):
            out.append(_finding("error", "no_collider", prim, "It falls straight through the floor.",
                                "A rigid body with no collider has no solid shape.",
                                "Add a collider (scene_make_collider), or re-run scene_make_rigid_body."))
        parent = prim.GetParent()
        while parent and parent.GetPath() != Sdf.Path.absoluteRootPath:
            if _rigid(parent):
                out.append(_finding("error", "nested_rigid_body", prim, "It jitters, sticks or flies off on Play.",
                                    f"It is a rigid body inside another rigid body ({parent.GetPath()}).",
                                    "Keep one rigid body per moving part; join parts with joints instead."))
                break
            parent = parent.GetParent()
        mass = prim.GetAttribute("physics:mass")
        if mass and mass.HasAuthoredValue() and mass.Get() is not None and mass.Get() < 0:
            out.append(_finding("error", "negative_mass", prim, "Physics behaves strangely.",
                                "Its mass is negative.", "Set a positive mass in kg."))
        if _kinematic(prim):
            velocity = prim.GetAttribute("physics:velocity")
            if velocity and velocity.HasAuthoredValue() and any(abs(v) > 1e-9 for v in velocity.Get()):
                out.append(_finding("warning", "kinematic_velocity", prim, "It never moves although it has a speed.",
                                    "It is kinematic, so physics ignores its velocity.",
                                    "Turn kinematic off, or animate/drive it instead."))
            elif not any(prim.GetPath().HasPrefix(p.GetPath()) for p in prims if p.HasAPI(UsdPhysics.ArticulationRootAPI)):
                out.append(_finding("info", "kinematic", prim, "It stays still when you press Play.",
                                    "It is kinematic: physics will not move it (right for a belt, wrong for a box).",
                                    "If it should fall, turn kinematic off (scene_make_rigid_body kinematic=False)."))
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ["default", "render"])
    dynamic = [p for p in rigid if not _kinematic(p) and _has_collider(p) and p.IsA(UsdGeom.Gprim)][:MAX_OVERLAP_BODIES]
    boxes = {p.GetPath(): cache.ComputeWorldBound(p).ComputeAlignedRange() for p in dynamic}
    for a, b in combinations(dynamic, 2):
        box_a, box_b = boxes[a.GetPath()], boxes[b.GetPath()]
        overlap = Gf.Range3d.GetIntersection(box_a, box_b)
        if not overlap.IsEmpty() and all(overlap.GetSize()[i] > 0.15 * min(box_a.GetSize()[i], box_b.GetSize()[i])
                                         for i in range(3)):
            out.append(_finding("warning", "overlap", a, "They jump or explode apart the moment you press Play.",
                                f"It starts inside {b.GetPath()}; PhysX pushes overlapping bodies apart hard.",
                                "Move them apart so they only touch."))
    return out


def _joint_checks(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    out = []
    jointed_bodies = set()
    for prim in prims:
        if not prim.IsA(UsdPhysics.Joint):
            continue
        joint = UsdPhysics.Joint(prim)
        targets = joint.GetBody0Rel().GetTargets() + joint.GetBody1Rel().GetTargets()
        if not targets:
            out.append(_finding("error", "joint_no_bodies", prim, "The parts it should connect move freely.",
                                "The joint connects nothing (no body0/body1).", "Set its bodies (scene_create_joint)."))
        for target in targets:
            body = stage.GetPrimAtPath(target)
            if not body:
                out.append(_finding("error", "wrong_prim_path", prim, "The part falls off or the joint does nothing.",
                                    f"It points at {target}, which does not exist (a typo or a renamed prim).",
                                    "Point body0/body1 at the real prim path (scene_find helps)."))
            else:
                jointed_bodies.add(body.GetPath())
        for kind in ("angular", "linear"):
            if prim.HasAPI(UsdPhysics.DriveAPI, kind):
                drive = UsdPhysics.DriveAPI(prim, kind)
                stiffness, damping = drive.GetStiffnessAttr().Get() or 0, drive.GetDampingAttr().Get() or 0
                if stiffness == 0 and damping == 0:
                    out.append(_finding("warning", "drive_does_nothing", prim, "The motor does not move the joint.",
                                        "Its drive has stiffness 0 and damping 0, so it applies no force.",
                                        "Give it stiffness (position target) or damping (velocity target)."))
        for low_name, high_name in (("physics:lowerLimit", "physics:upperLimit"),):
            low, high = prim.GetAttribute(low_name), prim.GetAttribute(high_name)
            if low and high and low.Get() is not None and high.Get() is not None and low.Get() > high.Get():
                out.append(_finding("error", "limits_reversed", prim, "The joint is locked or snaps around.",
                                    f"Its lower limit {low.Get():g} is above its upper limit {high.Get():g}.",
                                    "Swap the limits (scene_set_joint_limits)."))
    roots = [p for p in prims if p.HasAPI(UsdPhysics.ArticulationRootAPI)]
    for path in jointed_bodies:
        if not any(path.HasPrefix(root.GetPath()) for root in roots):
            body = stage.GetPrimAtPath(path)
            if body and _rigid(body) and len([r for r in prims if r.IsA(UsdPhysics.Joint)]) >= 3:
                out.append(_finding("info", "no_articulation_root", body, "A jointed machine wobbles or drifts.",
                                    "Its joints are not under an articulation root, so PhysX solves them loosely.",
                                    "Add an articulation root to the machine's base (scene_add_articulation_root)."))
                break
    return out


def _graph_checks(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    out = []
    for prim in prims:
        if prim.GetTypeName() not in ("OmniGraphNode", "ComputeNode"):
            continue
        for relation in prim.GetRelationships():
            if not relation.GetName().startswith("inputs:"):
                continue
            for target in relation.GetTargets():
                if not stage.GetPrimAtPath(target.GetPrimPath()):
                    out.append(_finding("error", "wrong_prim_path", prim, "The graph runs but nothing happens to "
                                        "the object.", f"{relation.GetName()} points at {target}, which does not exist.",
                                        "Pick the right prim in that input (graph_set_value)."))
    try:
        from . import omnigraph_helper
        report = omnigraph_helper.inspect_graphs(stage)
        for problem in report.get("problems", []):
            if problem.get("code") in ("no_trigger", "missing_input", "only_playback", "unwired_exec"):
                out.append({"severity": problem.get("severity", "warning"), "code": f"graph_{problem.get('code')}",
                            "prim": problem.get("node"), "symptom": "The Action Graph does not do anything.",
                            "cause": problem.get("message"), "fix": problem.get("fix")})
    except Exception:  # noqa: BLE001 - graph checks are a bonus
        pass
    return out


def _asset_checks(stage: Any, prims: list[Any]) -> list[dict[str, Any]]:
    out = []
    for prim in prims:
        for arc, items in (("reference", prim.GetMetadata("references")), ("payload", prim.GetMetadata("payload"))):
            if not items:
                continue
            for item in list(items.prependedItems) + list(items.appendedItems) + list(items.explicitItems):
                asset = getattr(item, "assetPath", "")
                if not asset or "://" in asset:
                    continue
                layer = next((spec.layer for spec in prim.GetPrimStack() if spec.layer), stage.GetRootLayer())
                resolved = layer.ComputeAbsolutePath(asset) if layer and not layer.anonymous else asset
                if not os.path.exists(resolved):
                    out.append(_finding("error", "missing_asset", prim, "The object is empty or missing.",
                                        f"Its {arc} file {asset} cannot be found.",
                                        "Fix the path (relative to the layer) or restore the file."))
    return out


# ------------------------------------------------------------------------------------------------ challenges
def _box(stage: Any, path: str, at: tuple, size: float = 0.4, *, rigid: bool = True, collider: bool = True) -> Any:
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(size)
    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(*at))
    if rigid:
        UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())
    if collider:
        UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return cube.GetPrim()


def _floor(stage: Any, collider: bool = True) -> None:
    floor = UsdGeom.Cube.Define(stage, "/World/Floor")
    floor.CreateSizeAttr(1.0)
    UsdGeom.XformCommonAPI(floor).SetTranslate(Gf.Vec3d(0, 0, -0.05))
    UsdGeom.XformCommonAPI(floor).SetScale(Gf.Vec3f(6, 6, 0.1))
    if collider:
        UsdPhysics.CollisionAPI.Apply(floor.GetPrim())


def _physics_scene(stage: Any) -> None:
    scene = UsdPhysics.Scene.Define(stage, "/physicsScene")
    scene.CreateGravityDirectionAttr(Gf.Vec3f(0, 0, -1))
    scene.CreateGravityMagnitudeAttr(9.81)


def _build_no_scene(stage):
    _floor(stage)
    _box(stage, "/World/Box", (0, 0, 1))


def _build_floor_no_collider(stage):
    _physics_scene(stage)
    _floor(stage, collider=False)
    _box(stage, "/World/Box", (0, 0, 1))


def _build_box_no_collider(stage):
    _physics_scene(stage)
    _floor(stage)
    _box(stage, "/World/Box", (0, 0, 1), collider=False)


def _build_kinematic(stage):
    _physics_scene(stage)
    _floor(stage)
    box = _box(stage, "/World/Box", (0, 0, 1))
    box.CreateAttribute("physics:kinematicEnabled", Sdf.ValueTypeNames.Bool).Set(True)


def _build_overlap(stage):
    _physics_scene(stage)
    _floor(stage)
    _box(stage, "/World/BoxA", (0, 0, 0.2))
    _box(stage, "/World/BoxB", (0.1, 0, 0.25))


def _build_wrong_joint_path(stage):
    _physics_scene(stage)
    _floor(stage)
    frame = _box(stage, "/World/DoorFrame", (0, 0, 1), rigid=False)
    UsdGeom.XformCommonAPI(frame).SetScale(Gf.Vec3f(0.25, 0.25, 5))
    door = _box(stage, "/World/Door", (0.6, 0, 1))
    UsdGeom.XformCommonAPI(door).SetScale(Gf.Vec3f(2.5, 0.1, 4.5))
    hinge = UsdPhysics.RevoluteJoint.Define(stage, "/World/Door/Hinge")
    hinge.CreateAxisAttr("Z")
    hinge.CreateBody0Rel().SetTargets(["/World/Dor"])  # the bug: a typo, /World/Dor does not exist
    hinge.CreateBody1Rel().SetTargets(["/World/Door"])


def _build_invisible(stage):
    _physics_scene(stage)
    _floor(stage)
    box = _box(stage, "/World/Box", (0, 0, 1))
    UsdGeom.Imageable(box).CreateVisibilityAttr("invisible")


CHALLENGES: dict[str, dict[str, Any]] = {
    "box_wont_fall": {"level": 1, "build": _build_no_scene,
                      "symptom": "Press Play: the box just hangs in the air.",
                      "answer": ["physics scene", "physicsscene", "physics_scene", "no physics"],
                      "hints": ["Physics needs one thing in the scene before anything simulates. Look at the Stage "
                                "panel: what is missing compared with your other physics scenes?",
                                "scene_diagnose(focus='physics') lists it."],
                      "cause": "There is no PhysicsScene, so physics never runs.",
                      "fix": "scene_add_physics_scene, then press Play again."},
    "falls_through_floor": {"level": 1, "build": _build_floor_no_collider,
                            "symptom": "Press Play: the box falls straight through the floor.",
                            "answer": ["floor", "collider", "collision"],
                            "hints": ["The box itself is fine. What makes something solid enough to land on?",
                                      "Check the floor's Physics properties in the Property panel."],
                            "cause": "The floor has no collider, so it is not solid.",
                            "fix": "scene_make_collider on /World/Floor.", "must_mention": ["floor"]},
    "box_passes_through": {"level": 1, "build": _build_box_no_collider,
                           "symptom": "Press Play: the box falls and sinks into the floor without stopping.",
                           "answer": ["box", "collider", "collision"],
                           "hints": ["The floor is solid this time. Look at the box's physics.",
                                     "A rigid body moves; what gives it a shape?"],
                           "cause": "The box is a rigid body without a collider: it has mass but no shape.",
                           "fix": "scene_make_collider on /World/Box.", "must_mention": ["box"]},
    "frozen_box": {"level": 2, "build": _build_kinematic,
                   "symptom": "Press Play: there is a physics scene, colliders everywhere, and still the box hangs.",
                   "answer": ["kinematic"],
                   "hints": ["The box is a rigid body with a collider, so something tells physics to leave it alone.",
                             "Look at the box's Rigid Body settings: one checkbox is for belts, not boxes."],
                   "cause": "The box is kinematic: physics never moves a kinematic body.",
                   "fix": "Turn kinematic off (scene_make_rigid_body with kinematic=False)."},
    "exploding_boxes": {"level": 2, "build": _build_overlap,
                        "symptom": "Press Play: two boxes jump apart violently.",
                        "answer": ["overlap", "inside each other", "intersect", "same place", "penetrat"],
                        "hints": ["Nothing pushes them, so where they START matters.",
                                  "Compare the two boxes' positions and sizes."],
                        "cause": "The boxes start inside each other; PhysX pushes overlapping bodies apart hard.",
                        "fix": "Move BoxB so it sits on top of or beside BoxA (scene_move)."},
    "door_falls_off": {"level": 3, "build": _build_wrong_joint_path,
                       "symptom": "Press Play: the door is supposed to hinge on the frame but simply falls down.",
                       "answer": ["path", "typo", "dor", "body0", "wrong prim"],
                       "hints": ["The hinge joint exists. Read exactly what it connects.",
                                 "Compare the joint's body0 with the names in the Stage panel, letter by letter."],
                       "cause": "The hinge's body0 points at /World/Dor (a typo), which does not exist, so the door "
                                "is not attached to anything.",
                       "fix": "Point body0 at /World/DoorFrame (or leave it empty to hinge on the world)."},
    "missing_box": {"level": 1, "build": _build_invisible,
                    "symptom": "The Stage panel lists /World/Box, but you cannot see it anywhere.",
                    "answer": ["invisible", "visibility", "hidden"],
                    "hints": ["It exists, and physics is fine. Is it being drawn?",
                              "Look for the eye icon next to it in the Stage panel."],
                    "cause": "Its visibility is 'invisible'.",
                    "fix": "Set visibility to 'inherited' (or click the eye icon)."},
}


def list_challenges() -> dict[str, Any]:
    return {"ok": True, "challenges": [{"id": key, "level": spec["level"], "symptom": spec["symptom"]}
                                       for key, spec in CHALLENGES.items()],
            "how": "debug_challenge_make(challenge_id) writes the scene; open it in Isaac Sim, press Play, find the "
                   "bug, then debug_challenge_answer(challenge_id, your explanation)."}


def make_challenge(challenge_id: str, folder: str | None = None, *, dry_run: bool = True) -> dict[str, Any]:
    """Preview (or write) a broken practice scene as a NEW .usda file (default folder isaac_study/debug)."""
    if Usd is None:
        return {"ok": False, "error": "pxr (USD) is not available here."}
    spec = CHALLENGES.get(challenge_id)
    if spec is None:
        return {"ok": False, "error": f"Unknown challenge {challenge_id!r}. Challenges: {', '.join(CHALLENGES)}."}
    target_dir = Path(folder) if folder else workspace_root() / "isaac_study" / "debug"
    path = target_dir / f"{challenge_id}.usda"
    if path.exists():
        return {"ok": False, "error": f"{path.as_posix()} already exists; open it, or delete it to start fresh."}
    result = {"ok": True, "challenge": challenge_id, "level": spec["level"], "file": path.as_posix(),
              "symptom": spec["symptom"],
              "how": ["In Isaac Sim: File > Open the file, press Play, and watch.",
                      "Describe what you see and what you think is wrong: debug_challenge_answer(challenge_id, ...).",
                      "Stuck? Ask for a hint (debug_challenge_answer with hint=True) before reading scene_diagnose."]}
    if dry_run is not False:
        return {**result, "dry_run": True, "written": False, "next_step": "Repeat with dry_run=False to write it."}
    target_dir.mkdir(parents=True, exist_ok=True)
    stage = Usd.Stage.CreateNew(str(path))
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    world = UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(world.GetPrim())
    spec["build"](stage)
    stage.GetRootLayer().Save()
    return {**result, "dry_run": False, "written": True}


def check_answer(challenge_id: str, answer: str = "", *, hint: bool = False, reveal: bool = False) -> dict[str, Any]:
    """Grade an explanation of a challenge's bug: right/not yet, then hints (hint=True), then the answer (reveal)."""
    spec = CHALLENGES.get(challenge_id)
    if spec is None:
        return {"ok": False, "error": f"Unknown challenge {challenge_id!r}."}
    if reveal:
        return {"ok": True, "challenge": challenge_id, "cause": spec["cause"], "fix": spec["fix"]}
    if hint and not answer:
        return {"ok": True, "challenge": challenge_id, "hints": spec["hints"]}
    if not isinstance(answer, str) or not answer.strip():
        return {"ok": False, "error": "Describe what you think is wrong (or pass hint=True)."}
    text = answer.lower()
    found = any(word in text for word in spec["answer"])
    named = all(word in text for word in spec.get("must_mention", []))
    if found and named:
        return {"ok": True, "challenge": challenge_id, "correct": True, "cause": spec["cause"], "fix": spec["fix"],
                "next": "Fix it in the scene, press Play to confirm, then try a harder challenge."}
    return {"ok": True, "challenge": challenge_id, "correct": False,
            "feedback": ("Close: you found the right idea, now say WHICH prim it is on." if found and not named else
                         "Not yet."), "hint": spec["hints"][0],
            "next": "Try again, or debug_challenge_answer(challenge_id, hint=True) for both hints."}
