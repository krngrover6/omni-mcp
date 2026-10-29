"""Live-challenge trainer (class session 15): small timed tasks on YOUR scene, like the instructor's live test.

challenge_start reads the open scene, sees what it already has (a conveyor graph, an obstacle stop, a colour
sorter, a Delay, packages, rigid bodies) and picks a task that fits: change the belt speed, make red go to the
other bin, stop further from the wall, add a camera that looks at the box... The task is written into its id, so
nothing is stored: challenge_check(id) reads the scene again and says whether it is done. Hints come only when
asked; there is no answer button, because in the live test you have to find it yourself. Both tools are read-only:
you make the change, in Isaac Sim.
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

try:
    from pxr import Gf, Usd, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    Gf = Usd = UsdGeom = UsdPhysics = None

MAX_PRIMS = 20000
_SYSTEM_ROOTS = ("/Render", "/Replicator", "/Orchestrator", "/OmniverseKit")
_CONVEYOR = "isaacsim.asset.gen.conveyor.IsaacConveyor"
LEVELS = {"easy": 3, "medium": 5, "hard": 8}  # minutes allowed


def _prims(stage: Any) -> list[Any]:
    out = []
    for prim in stage.Traverse():
        if len(out) >= MAX_PRIMS:
            break
        if not str(prim.GetPath()).startswith(_SYSTEM_ROOTS):
            out.append(prim)
    return out


def _nodes(prims: list[Any], suffix: str) -> list[Any]:
    """Graph nodes whose type ends with suffix (e.g. 'Compare', 'Delay')."""
    hits = []
    for prim in prims:
        if prim.GetTypeName() in ("OmniGraphNode", "ComputeNode"):
            kind = prim.GetAttribute("node:type")
            if kind and str(kind.Get() or "").rsplit(".", 1)[-1] == suffix:
                hits.append(prim)
    return hits


def _value(prim: Any, name: str) -> Any:
    attribute = prim.GetAttribute(name)
    if attribute and attribute.HasValue():
        return attribute.Get()
    # Seen live: flexible pins (Write Variable's Value, Compare's B) keep their value in OmniGraph, not in USD.
    if prim.GetTypeName() not in ("OmniGraphNode", "ComputeNode"):
        return None
    try:
        import omni.graph.core as og

        value = og.Controller.get(og.Controller.attribute(f"{prim.GetPath()}.{name}"))
    except Exception:  # noqa: BLE001 - outside Kit, or the pin does not exist
        return None
    return value.tolist() if hasattr(value, "tolist") else value


def _close(a: Any, b: float, tolerance: float = 0.011) -> bool:
    try:
        return abs(float(a) - b) <= tolerance
    except (TypeError, ValueError):
        return False


def _graph_of(node: Any) -> Any:
    prim = node.GetParent()
    while prim and prim.GetTypeName() != "OmniGraph" and str(prim.GetPath()) != "/":
        prim = prim.GetParent()
    return prim if prim and prim.GetTypeName() == "OmniGraph" else None


def _routes(node: Any) -> dict[str, str]:
    try:
        routes = json.loads(_value(node, "inputs:routes") or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in routes.items()} if isinstance(routes, dict) else {}


def _world(prim: Any) -> Any:
    return UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())


def _rigid_bodies(prims: list[Any]) -> list[Any]:
    return [p for p in prims if p.HasAPI(UsdPhysics.RigidBodyAPI) and not p.HasAPI(UsdPhysics.ArticulationRootAPI)
            and not (p.GetAttribute("physics:kinematicEnabled") and p.GetAttribute("physics:kinematicEnabled").Get())]


# ------------------------------------------------------------------ what each challenge needs, asks and checks
def _offers(stage: Any, prims: list[Any], rng: random.Random) -> list[tuple[str, str]]:
    """(level, id) for every challenge this scene can host, with fresh numbers."""
    offers = []
    if _nodes(prims, "IsaacConveyor"):
        offers.append(("easy", f"belt_speed:{rng.choice([0.25, 0.5, 0.75, 1.5, 2.0])}"))
    if _nodes(prims, "Delay"):
        offers.append(("easy", f"wait:{rng.choice([2, 3, 4, 7, 8])}"))
    if any(_value(c, "inputs:operation") in ("<", "<=") for c in _nodes(prims, "Compare")):
        offers.append(("easy", f"stop_distance:{rng.choice([0.3, 0.4, 0.6, 0.8])}"))
    if any(_value(w, "inputs:variableName") == "linear" for w in _nodes(prims, "WriteVariable")):
        offers.append(("easy", f"drive_speed:{rng.choice([0.1, 0.15, 0.3])}"))
    for sorter in _nodes(prims, "ScriptNode"):
        routes = _routes(sorter)
        bins = sorted(set(routes.values()))
        if len(bins) >= 2:
            colour = rng.choice(sorted(routes))
            other = rng.choice([b for b in bins if b != routes[colour]])
            offers.append(("medium", f"route:{colour}={other}"))
            break
    groups = [p for p in prims if p.GetName().lower() == "packages"]
    if groups:
        offers.append(("medium", f"packages:{rng.choice([3, 5, 8, 10])}"))
    bodies = _rigid_bodies(prims)
    if bodies:
        body = rng.choice(bodies[:30])
        offers.append(("medium", f"mass:{body.GetPath()}={rng.choice([0.5, 2.0, 5.0, 10.0])}"))
        offers.append(("hard", f"camera_on:{body.GetPath()}"))
    return offers


def _describe(kind: str, arg: str) -> dict[str, Any]:
    """The task in words, plus two hints (never the answer)."""
    if kind == "belt_speed":
        return {"task": f"Make the conveyor belt run at {arg} m/s.",
                "hints": ["The belt's speed lives in the conveyor graph: a graph Variable (Velocity) or the Conveyor "
                          "Belt node's Velocity input.",
                          "Open the graph (Window > Graphs > Action Graph), select the graph's Variables tab and "
                          "change the Velocity default."]}
    if kind == "wait":
        return {"task": f"Make the stop last {arg} seconds instead of the current wait.",
                "hints": ["Waiting is done by a Delay node.", "Select the Delay node; in the Property panel set "
                                                              "Duration."]}
    if kind == "stop_distance":
        return {"task": f"Make the robot stop {arg} m from the obstacle.",
                "hints": ["The decision 'too close?' is a Compare node: Gap < some number.",
                          "Select the Compare node; its B input is the stopping distance in metres."]}
    if kind == "drive_speed":
        return {"task": f"Make the robot drive forward at {arg} m/s.",
                "hints": ["The 'go' speed is written into the linear variable by a Write Variable node.",
                          "Select the Write Variable that writes linear when the way is clear; set its Value."]}
    if kind == "route":
        colour, _, target = arg.partition("=")
        return {"task": f"Send {colour} packages to {target} instead of their current bin.",
                "hints": ["The sorter's Script Node has a routes input: colour -> bin, written as JSON text.",
                          'Select the Sorter node; edit routes so that "' + colour + '" maps to "' + target + '".']}
    if kind == "packages":
        return {"task": f"Have exactly {arg} packages in the Packages group, each a rigid body.",
                "hints": ["Add or delete children of the Packages folder in the Stage panel.",
                          "scene_spawn_packages(count=...) previews a tidy batch; duplicates need Rigid Body too."]}
    if kind == "mass":
        path, _, mass = arg.partition("=")
        return {"task": f"Give {path} a mass of {mass} kg.",
                "hints": ["Mass is a physics property (the Mass API) on the rigid body.",
                          "Select the prim; Property panel > + Add > Physics > Mass; then set Mass."]}
    if kind == "camera_on":
        return {"task": f"Add a camera that looks straight at {arg} (within 10 degrees), from at least 1 m away.",
                "hints": ["Create > Camera adds one; a camera looks along its own -Z axis.",
                          "Move it away from the object, then rotate it until the object sits in the middle of "
                          "the camera's view (Viewport > Cameras > your camera to look through it)."]}
    raise KeyError(kind)


def _check(stage: Any, prims: list[Any], kind: str, arg: str) -> tuple[bool, dict[str, Any]]:
    if kind == "belt_speed":
        target = float(arg)
        seen = []
        for node in _nodes(prims, "IsaacConveyor"):
            seen.append(("node", str(node.GetPath()), _value(node, "inputs:velocity")))
            graph = _graph_of(node)
            for prop in graph.GetAuthoredProperties() if graph else []:
                if prop.GetName().startswith("graph:variable:") and isinstance(prop, Usd.Attribute):
                    seen.append(("variable", prop.GetName()[len("graph:variable:"):], prop.Get()))
        return any(_close(v, target) for _, _, v in seen), {"speeds_found": seen[:8]}
    if kind == "wait":
        durations = [(str(d.GetPath()), _value(d, "inputs:duration")) for d in _nodes(prims, "Delay")]
        return any(_close(v, float(arg)) for _, v in durations), {"delays": durations[:6]}
    if kind == "stop_distance":
        found = [(str(c.GetPath()), _value(c, "inputs:b")) for c in _nodes(prims, "Compare")]
        return any(_close(v, float(arg)) for _, v in found), {"compare_b": found[:6]}
    if kind == "drive_speed":
        found = [(str(w.GetPath()), _value(w, "inputs:value")) for w in _nodes(prims, "WriteVariable")
                 if _value(w, "inputs:variableName") == "linear"]
        return any(_close(v, float(arg)) for _, v in found), {"linear_writes": found[:6]}
    if kind == "route":
        colour, _, target = arg.partition("=")
        found = {str(n.GetPath()): _routes(n) for n in _nodes(prims, "ScriptNode") if _routes(n)}
        return any(r.get(colour) == target for r in found.values()), {"routes": found}
    if kind == "packages":
        groups = [p for p in prims if p.GetName().lower() == "packages"]
        counts = {str(g.GetPath()): sum(1 for c in g.GetChildren() if c.HasAPI(UsdPhysics.RigidBodyAPI))
                  for g in groups}
        return int(arg) in counts.values(), {"rigid_packages": counts}
    if kind == "mass":
        path, _, mass = arg.partition("=")
        prim = stage.GetPrimAtPath(path)
        value = _value(prim, "physics:mass") if prim else None
        return bool(prim) and _close(value, float(mass), 0.01), {"prim": path, "exists": bool(prim), "mass_kg": value}
    if kind == "camera_on":
        target = stage.GetPrimAtPath(arg)
        if not target:
            return False, {"error": f"{arg} is not in the scene any more."}
        centre = _world(target).ExtractTranslation()
        metres = UsdGeom.GetStageMetersPerUnit(stage) or 1.0
        best = []
        for camera in (p for p in prims if p.IsA(UsdGeom.Camera)):
            matrix = _world(camera)
            eye = matrix.ExtractTranslation()
            look = matrix.TransformDir(Gf.Vec3d(0, 0, -1))
            to_target = centre - eye
            distance = to_target.GetLength() * metres
            if distance < 1e-6 or look.GetLength() < 1e-9:
                continue
            cosine = max(-1.0, min(1.0, Gf.Dot(look.GetNormalized(), to_target.GetNormalized())))
            best.append({"camera": str(camera.GetPath()), "off_by_deg": round(math.degrees(math.acos(cosine)), 1),
                         "distance_m": round(distance, 2)})
        best.sort(key=lambda item: item["off_by_deg"])
        done = any(b["off_by_deg"] <= 10 and b["distance_m"] >= 1.0 for b in best)
        return done, {"cameras": best[:4]}
    raise KeyError(kind)


def _parse(challenge_id: Any) -> tuple[str, str] | None:
    if not isinstance(challenge_id, str) or ":" not in challenge_id:
        return None
    kind, _, arg = challenge_id.partition(":")
    try:
        _describe(kind, arg)
        if kind in ("belt_speed", "wait", "stop_distance", "drive_speed", "packages"):
            float(arg)
        if kind == "mass":
            float(arg.partition("=")[2])
        if kind == "route" and "=" not in arg:
            return None
    except (KeyError, ValueError):
        return None
    return kind, arg


def challenge_start(stage: Any, level: str = "any", seed: int | None = None) -> dict[str, Any]:
    """Read-only: pick a live challenge that fits the open scene. level is easy, medium, hard or any; seed repeats a
    pick. Returns the task, the time allowed, and the id to pass to live_challenge_check."""
    if Usd is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    if stage is None:
        return {"ok": False, "error": "No stage is open"}
    if level not in ("any", *LEVELS):
        return {"ok": False, "error": "level must be easy, medium, hard or any."}
    rng = random.Random(seed)
    offers = _offers(stage, _prims(stage), rng)
    fitting = [o for o in offers if level in ("any", o[0])]
    if not fitting:
        return {"ok": True, "read_only": True, "challenge": None,
                "available": sorted({o[0] for o in offers}),
                "message": ("This scene has nothing to challenge yet. Build something first, e.g. "
                            "graph_recipe('conveyor') or scene_spawn_packages, then ask again.") if not offers else
                           f"No {level} challenge fits this scene; try level='any'."}
    chosen_level, challenge_id = rng.choice(fitting)
    kind, arg = _parse(challenge_id)
    text = _describe(kind, arg)
    return {
        "ok": True, "read_only": True, "id": challenge_id, "level": chosen_level, "task": text["task"],
        "minutes": LEVELS[chosen_level],
        "rules": f"Start a timer for {LEVELS[chosen_level]} minutes. Make the change in Isaac Sim yourself, then call "
                 f"live_challenge_check(id='{challenge_id}'). Ask for a hint only if you are stuck (hint=true).",
        "other_options": len(fitting) - 1,
    }


def challenge_check(stage: Any, id: str, hint: bool = False) -> dict[str, Any]:  # noqa: A002 - tool argument name
    """Read-only: is the live challenge done? Reads the scene again; hint=true adds two hints (never the answer)."""
    if Usd is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    if stage is None:
        return {"ok": False, "error": "No stage is open"}
    parsed = _parse(id)
    if parsed is None:
        return {"ok": False, "error": "Unknown challenge id; get one from live_challenge_start."}
    kind, arg = parsed
    done, evidence = _check(stage, _prims(stage), kind, arg)
    result = {"ok": True, "read_only": True, "id": id, "task": _describe(kind, arg)["task"], "done": bool(done),
              "evidence": evidence,
              "message": ("Done! Press Play to see it behave, then ask for the next challenge." if done else
                          "Not yet. Look at the evidence: it shows what the scene has right now.")}
    if hint and not done:
        result["hints"] = _describe(kind, arg)["hints"]
    return result
