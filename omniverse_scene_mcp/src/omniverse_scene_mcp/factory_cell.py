"""Factory cell builder (class sessions 13-15 capstone): a whole pick-and-place cell in one previewable step.

What it builds under one NEW group (default /World/FactoryCell), all placed relative to `origin`:
- Belt: a long flat box that is a KINEMATIC rigid body (moves things without falling) with a collider, plus an
  Action Graph with a Conveyor Belt node that makes its surface carry packages along +X (class session 6).
- Stopper: a small wall at the belt's end, so packages gather at the pick spot instead of falling off.
- Packages: small coloured boxes (5-6 cm, rigid bodies) dropped at the belt's start (class session 7).
- Franka: a ready robot arm placed so the pick spot is well within its 85 cm reach (class session 14).
- Bins: one per colour, within reach on the far side, where the arm puts packages.
- Camera: above the pick spot looking down, for colour detection or a dataset (class sessions 9 and 13).

Nothing is saved. With dry_run=False each part is created in order; if one fails, the whole group is removed again
so you never keep half a cell. Then: Play, wait for a package at the stopper, and run robot_live_pick on it.
"""

from __future__ import annotations

from typing import Any

try:
    from pxr import Gf, Sdf, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover
    Gf = Sdf = UsdGeom = UsdPhysics = None

COLOURS = {"red": (0.85, 0.15, 0.12), "blue": (0.15, 0.35, 0.9), "green": (0.15, 0.7, 0.25),
           "yellow": (0.95, 0.8, 0.1)}


def _layout(origin: list[float], colours: list[str]) -> dict[str, Any]:
    ox, oy, oz = origin

    def at(x: float, y: float, z: float) -> list[float]:
        return [round(ox + x, 4), round(oy + y, 4), round(oz + z, 4)]

    import math

    bins = {colour: at(0.45 + 0.45 * i, 1.15, 0.025) for i, colour in enumerate(colours)}
    # Face the pick spot (0.85, 0) from the base (0.75, 0.6). Seen live: with the package 76 degrees to the
    # arm's side the hand arrived tilted and never held it; a cube straight ahead was picked 5 mm from target.
    facing = round(math.degrees(math.atan2(0.0 - 0.6, 0.85 - 0.75)), 1)
    return {
        "belt": {"position": at(0.0, 0.0, 0.05), "size": [2.4, 0.4, 0.1]},
        "stopper": {"position": at(0.95, 0.0, 0.15), "size": [0.05, 0.4, 0.15]},
        "pick_spot": at(0.85, 0.0, 0.13),
        "franka": at(0.75, 0.6, 0.0),
        "franka_yaw": facing,
        "bins": bins,
        "camera": at(0.85, 0.0, 2.0),
        "packages_region": (at(-1.1, -0.12, 0.25), at(-0.6, 0.12, 0.35)),
    }


def _box(stage: Any, path: str, position: list[float], size: list[float], rgb: Any, kinematic: bool | None) -> None:
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    api = UsdGeom.XformCommonAPI(cube)
    api.SetTranslate(Gf.Vec3d(*position))
    api.SetScale(Gf.Vec3f(*size))
    cube.CreateDisplayColorAttr([Gf.Vec3f(*rgb)])
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    if kinematic is not None:
        UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim()).CreateKinematicEnabledAttr(kinematic)


def build_cell(stage: Any, root: str = "/World/FactoryCell", origin: Any = (0.0, 0.0, 0.0),
               colours: Any = ("red", "blue"), packages: int = 1, belt_speed: float = 0.2, seed: int = 3,
               dry_run: bool = True) -> dict[str, Any]:
    """Preview (dry_run=True) or build a complete pick-and-place factory cell under a NEW group `root`: belt with a
    conveyor graph, stopper, packages, Franka, one bin per colour and a top camera. Nothing is saved.
    packages defaults to 1: seen live, several packages pile up at the stopper with about 3 cm between them and the
    gripper's fingers land on the neighbours; deliver one at a time (spawn the next after each pick)."""
    from . import omnigraph_helper, robot_import, spawner

    if UsdGeom is None:
        return {"ok": False, "error": "pxr (USD) is not available"}
    if stage is None:
        return {"ok": False, "error": "No stage is open"}
    if not isinstance(root, str) or not Sdf.Path.IsValidPathString(root) or not root.startswith("/"):
        return {"ok": False, "error": "root must be an absolute prim path such as /World/FactoryCell."}
    if stage.GetPrimAtPath(root):
        return {"ok": False, "error": f"{root} already exists: pick another root (this tool never edits a cell)."}
    try:
        origin = [float(v) for v in origin]
        assert len(origin) == 3
    except (TypeError, ValueError, AssertionError):
        return {"ok": False, "error": "origin must be three numbers [x, y, z] in metres."}
    colours = list(colours or [])
    if not 1 <= len(colours) <= 4 or any(c not in COLOURS for c in colours):
        return {"ok": False, "error": f"colours must be 1-4 of {sorted(COLOURS)}."}
    if isinstance(packages, bool) or not isinstance(packages, int) or not 1 <= packages <= 12:
        return {"ok": False, "error": "packages must be a whole number from 1 to 12."}
    if isinstance(belt_speed, bool) or not isinstance(belt_speed, (int, float)) or not 0 < belt_speed <= 1:
        return {"ok": False, "error": "belt_speed must be more than 0 and at most 1 m/s."}
    if UsdGeom.GetStageMetersPerUnit(stage) not in (0.0, 1.0):
        return {"ok": False, "error": "This builder places things in metres: the stage must use metersPerUnit = 1."}
    layout = _layout(origin, colours)
    region_min, region_max = layout["packages_region"]
    parts = [
        {"part": "belt", "path": f"{root}/Belt", "what": "kinematic rigid body + collider, 2.4 m long",
         "at": layout["belt"]["position"]},
        {"part": "stopper", "path": f"{root}/Stopper", "what": "static wall that stops packages at the pick spot",
         "at": layout["stopper"]["position"]},
        *({"part": f"bin_{c}", "path": f"{root}/Bin_{c}", "what": f"drop-off for {c} packages", "at": p}
          for c, p in layout["bins"].items()),
        {"part": "camera", "path": f"{root}/TopCam", "what": "looks straight down at the pick spot",
         "at": layout["camera"]},
        {"part": "packages", "path": f"{root}/Packages", "what": f"{packages} boxes, 5-6 cm, colours {colours}",
         "at": region_min},
        {"part": "franka", "path": f"{root}/Franka",
         "what": f"Franka arm (ready robot), turned {layout['franka_yaw']} degrees to face the pick spot",
         "at": layout["franka"]},
        {"part": "belt_graph", "path": f"{root}/BeltGraph", "what": f"Conveyor Belt node at {belt_speed} m/s",
         "at": None},
    ]
    robot_preview = robot_import.add_ready_robot(stage, "franka", f"{root}/Franka", position=layout["franka"])
    graph_preview = omnigraph_helper.plan_recipe("conveyor", stage=None, graph_path=f"{root}/BeltGraph",
                                                 belt=f"{root}/Belt", velocity=float(belt_speed))
    problems = [p for p, r in (("franka", robot_preview), ("belt_graph", graph_preview)) if not r.get("ok")]
    base = {"ok": not problems, "dry_run": dry_run is not False, "root": root, "parts": parts,
            "pick_spot": layout["pick_spot"], "bins": layout["bins"],
            "problems": {p: (robot_preview if p == "franka" else graph_preview).get("error") for p in problems}}
    if problems or base["dry_run"]:
        base["next_step"] = ("Fix the problems above." if problems else
                             "Dry run: nothing was created. Call again with dry_run=False to build the cell.")
        return base

    created: list[str] = []

    def undo(reason: str, detail: Any) -> dict[str, Any]:
        stage.RemovePrim(root)
        return {**base, "ok": False, "built": False, "failed_at": reason, "error": detail,
                "note": f"Removed {root} again, so no half-built cell is left."}

    UsdGeom.Xform.Define(stage, root)
    _box(stage, f"{root}/Belt", layout["belt"]["position"], layout["belt"]["size"], (0.25, 0.25, 0.27), True)
    _box(stage, f"{root}/Stopper", layout["stopper"]["position"], layout["stopper"]["size"], (0.9, 0.6, 0.1), None)
    for colour, position in layout["bins"].items():
        _box(stage, f"{root}/Bin_{colour}", position, [0.3, 0.3, 0.05], COLOURS[colour], None)
    camera = UsdGeom.Camera.Define(stage, f"{root}/TopCam")
    camera.CreateFocalLengthAttr(24.0)
    camera.CreateHorizontalApertureAttr(20.955)
    camera.CreateVerticalApertureAttr(15.71625)  # 640x480: square pixels
    UsdGeom.XformCommonAPI(camera).SetTranslate(Gf.Vec3d(*layout["camera"]))  # a camera looks down -Z already
    created += [p["path"] for p in parts[:3 + len(colours)]]
    spawned = spawner.spawn_packages(stage, f"{root}/Packages", packages, region_min=region_min,
                                     region_max=region_max, size_range=(0.05, 0.06), colors=colours, seed=seed,
                                     dry_run=False)
    if not spawned.get("ok"):
        return undo("packages", spawned.get("error"))
    robot = robot_import.add_ready_robot(stage, "franka", f"{root}/Franka", position=layout["franka"], dry_run=False)
    if not robot.get("ok"):
        return undo("franka", robot.get("error"))
    arm = UsdGeom.Xformable(stage.GetPrimAtPath(f"{root}/Franka"))
    arm.AddRotateZOp(opSuffix="facePickSpot").Set(float(layout["franka_yaw"]))  # after translate: turns in place
    graph = omnigraph_helper.apply_recipe(stage, "conveyor", dry_run=False, graph_path=f"{root}/BeltGraph",
                                          belt=f"{root}/Belt", velocity=float(belt_speed))
    if not graph.get("ok"):
        return undo("belt_graph", graph.get("error") or graph)
    created += [f"{root}/Packages", f"{root}/Franka", f"{root}/BeltGraph"]
    first_package = ((spawned.get("created") or {}).get("packages") or [{}])[0]
    first = first_package.get("path")
    # Seen live: the hint sent a blue package to the red bin; use the package's own colour.
    target_bin = layout["bins"].get(first_package.get("color"), layout["bins"][colours[0]])
    return {**base, "ok": True, "dry_run": False, "built": True, "created": created,
            "try_it": [
                "Press Play: the packages drop onto the belt and ride to the stopper.",
                f"When one rests at the stopper, pick it: robot_live_pick(robot_root='{root}/Franka', "
                f"object_path='{first}', place_position={target_bin[:2] + [0.1]}) (its "
                f"{first_package.get('color', colours[0])} bin).",
                f"Check the whole cell like the class does: homework_check(session=14).",
            ],
            "note": "Nothing is saved. Ctrl+Z or deleting the group removes it; Stop puts moved things back."}
