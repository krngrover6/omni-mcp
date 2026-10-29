"""Pick-and-place starter: a guided Franka lesson, a readiness checker and a beginner script generator.

The classic Isaac Sim end-to-end demo is a Franka Panda arm picking a small cube and placing it somewhere else.
This module helps a beginner get there in three ways:

* lesson_plan() lists the steps in order (scene, physics, controller, run, debug), each with the concept in plain
  words and the MCP phrase / router tool to use for it.
* readiness_check(stage, robot_root=None, object_path=None) reads a USD stage (pure pxr, read-only) and says whether
  it is ready for a pick-and-place: physics scene, ground collider, a fixed-base robot with driven gripper fingers,
  and a target object that is a rigid body with a collider, a sensible mass, a graspable size, within reach and
  resting on a surface. Every check has a status, why it matters and how to fix it.
* generate_script(mode=..., ...) writes a commented Python script for Isaac Sim 6.1 (standalone with python.bat, or
  for the Script Editor with the async World pattern). User values only reach the code as validated float / str
  literals; the code is compile()d as a syntax self-check and never run here.

Isaac Sim 6.1 API facts used by the generated code (verified in C:/Users/krngr/Omniverse/isaacsim-6.1.0):

* The classic API (isaacsim.core.api World / DynamicCuboid, isaacsim.robot.manipulators, the Franka example class and
  its PickPlaceController) ships in extsDeprecated/ (deprecated since 6.0.0, still loaded: apps/isaacsim.exp.base.kit
  enables isaacsim.core.api, isaacsim.robot.manipulators and isaacsim.robot_motion.motion_generation and lists
  ${app}/../extsDeprecated as an extension folder). isaacsim.robot.manipulators.examples (Franka, PickPlaceController)
  is NOT enabled by default, so the scripts enable it (standalone_examples/deprecated/api/isaacsim.robot.manipulators/
  franka_pick_up.py does the same with "--enable").
* The maintained 6.1 path is isaacsim.core.experimental.* plus isaacsim.robot_motion.examples (ManipulationScenario,
  cuMotion RmpFlowController): standalone_examples/api/isaacsim.robot_motion.examples/manipulation/pick_place.py. It is
  much harder to read for a beginner, so the starter teaches the classic API and points to the new one.

Imports only the standard library and pxr (optional: readiness_check needs it, the rest does not).
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .file_stage import default_workspace_root
from .robot_inspector import inspect_robot

try:
    from pxr import Usd, UsdGeom, UsdPhysics
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Usd = UsdGeom = UsdPhysics = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

# --- Franka numbers (metres / kg) -------------------------------------------------------------------------------
FRANKA_REACH_M = 0.855  # Franka Panda maximum reach from the base axis
REACH_EDGE_M = 0.75  # beyond this the arm is near full stretch: works, but slowly and with awkward wrist angles
MIN_HORIZONTAL_FROM_BASE_M = 0.2  # closer than this the arm collides with its own base
FRANKA_FINGER_OPENING_M = 0.08  # 2 fingers x 0.04 m joint upper limit (panda_finger_joint1/2 in franka.usd)
GRASP_MARGIN_M = 0.01  # leave at least 1 cm of clearance when the gripper is open
MIN_GRASP_WIDTH_M = 0.01  # thinner objects slip out of the fingers
MIN_MASS_KG = 0.01  # Isaac's DynamicCuboid default is 0.02 kg; lighter objects get flung by contacts
MAX_MASS_KG = 1.0  # finger drives (max force 7.2 N in franka.usd) only hold about 0.7 kg by friction
REST_TOLERANCE_M = 0.02  # gaps to the surface below smaller than 2 cm count as resting
PENETRATION_TOLERANCE_M = 0.01
DEFAULT_DENSITY_KG_M3 = 1000.0  # PhysX default density when neither mass nor density is authored
MAX_SCAN = 20_000

MODES = ("standalone", "script_editor")
CUBE_PRIM = "/World/Cube"
GROUND_PRIM = "/World/defaultGroundPlane"
SCRIPT_FILENAME = "franka_pick_place.py"
EXAMPLES_EXTENSION = "isaacsim.robot.manipulators.examples"
LOCAL_FRANKA_RELATIVE = "isaacsim-assets-6.1.0/Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd"
ISAAC_FOLDER = "isaacsim-6.1.0"

_PRIM_PATH = re.compile(r"^(/[A-Za-z_][A-Za-z0-9_]*)+$")
_FLOOR_NAME = re.compile(r"floor|ground", re.IGNORECASE)
_OBJECT_NAME = re.compile(r"cube|object|box|block|part|workpiece|target|item", re.IGNORECASE)
_ROBOT_NAME = re.compile(r"franka|panda", re.IGNORECASE)
_FINGER_NAME = re.compile(r"finger", re.IGNORECASE)

STATUSES = ("pass", "warn", "fail", "skip")

# Verified in the Isaac Sim 6.1 install (paths relative to isaacsim-6.1.0/).
VERIFIED_APIS: tuple[dict[str, str], ...] = (
    {"name": "SimulationApp", "module": "isaacsim",
     "source": "standalone_examples/deprecated/api/isaacsim.robot.manipulators/franka_pick_up.py"},
    {"name": "World(stage_units_in_meters=...)", "module": "isaacsim.core.api",
     "source": "extsDeprecated/isaacsim.core.api/isaacsim/core/api/world/world.py"},
    {"name": "World.reset / reset_async / clear_instance / scene", "module": "isaacsim.core.api",
     "source": "extsDeprecated/isaacsim.core.api/isaacsim/core/api/world/world.py"},
    {"name": "initialize_simulation_context_async / play_async / add_physics_callback / is_playing / is_stopped",
     "module": "isaacsim.core.api",
     "source": "extsDeprecated/isaacsim.core.api/isaacsim/core/api/simulation_context/simulation_context.py"},
    {"name": "scene.add / scene.add_default_ground_plane", "module": "isaacsim.core.api",
     "source": "extsDeprecated/isaacsim.core.api/isaacsim/core/api/scenes/scene.py"},
    {"name": "DynamicCuboid(prim_path, name, position, scale, size, color, mass)", "module": "isaacsim.core.api.objects",
     "source": "extsDeprecated/isaacsim.core.api/isaacsim/core/api/objects/cuboid.py"},
    {"name": "Franka(prim_path, name, usd_path)", "module": "isaacsim.robot.manipulators.examples.franka",
     "source": "extsDeprecated/isaacsim.robot.manipulators.examples/isaacsim/robot/manipulators/examples/franka/franka.py"},
    {"name": "PickPlaceController(name, gripper, robot_articulation).forward/reset/is_done",
     "module": "isaacsim.robot.manipulators.examples.franka.controllers",
     "source": "extsDeprecated/isaacsim.robot.manipulators.examples/isaacsim/robot/manipulators/examples/franka/"
               "controllers/pick_place_controller.py (forward in extsDeprecated/isaacsim.robot.manipulators/isaacsim/"
               "robot/manipulators/controllers/pick_place_controller.py)"},
    {"name": "ParallelGripper.set_default_state / joint_opened_positions", "module": "isaacsim.robot.manipulators.grippers",
     "source": "extsDeprecated/isaacsim.robot.manipulators/isaacsim/robot/manipulators/grippers/parallel_gripper.py"},
    {"name": "get_articulation_controller / get_joint_positions / get_world_pose", "module": "isaacsim.core.prims",
     "source": "extsDeprecated/isaacsim.core.prims/isaacsim/core/prims/impl/single_articulation.py, single_prim_wrapper.py"},
    {"name": "run_coroutine", "module": "omni.kit.async_engine",
     "source": "kit/extscore/omni.kit.async_engine/omni/kit/async_engine/async_engine.py"},
    {"name": "set_extension_enabled_immediate", "module": "omni.kit.app (extension manager)",
     "source": "standalone_examples/api/isaacsim.robot_motion.examples/manipulation/pick_place.py"},
)

# Older tutorial names (Isaac Sim <= 4.2) and their isaacsim.* homes in 6.1.
OLD_TO_NEW: tuple[dict[str, str], ...] = (
    {"old": "from omni.isaac.kit import SimulationApp", "new": "from isaacsim import SimulationApp",
     "status": "4.5 rename; isaacsim.SimulationApp verified in 6.1 examples"},
    {"old": "omni.isaac.core (World, objects.DynamicCuboid, tasks)", "new": "isaacsim.core.api",
     "status": "renamed in 4.5 (isaacsim.core.api CHANGELOG); deprecated since 6.0, still shipped"},
    {"old": "omni.isaac.core.utils.* (stage, prims, rotations)", "new": "isaacsim.core.utils.*",
     "status": "4.5 rename; deprecated since 6.0, still shipped"},
    {"old": "omni.isaac.core.utils.nucleus.get_assets_root_path", "new": "isaacsim.storage.native.get_assets_root_path",
     "status": "renamed in 4.5 (isaacsim.storage.native CHANGELOG); maintained"},
    {"old": "omni.isaac.core.prims / articulations (XFormPrim, RigidPrim, Articulation)",
     "new": "isaacsim.core.prims (SingleXFormPrim, SingleRigidPrim, SingleArticulation)",
     "status": "4.5 rename and Single* prefix; deprecated since 6.0, still shipped"},
    {"old": "omni.isaac.franka (Franka, PickPlaceController, tasks.PickPlace, KinematicsSolver)",
     "new": "isaacsim.robot.manipulators.examples.franka",
     "status": "merged in 4.5; extension deprecated since 6.0, still shipped, disabled by default"},
    {"old": "omni.isaac.manipulators (SingleManipulator, grippers.ParallelGripper)", "new": "isaacsim.robot.manipulators",
     "status": "renamed in 4.5 (isaacsim.robot.manipulators CHANGELOG); deprecated since 6.0, still shipped"},
    {"old": "omni.isaac.motion_generation (RmpFlow, ArticulationMotionPolicy, LulaKinematicsSolver)",
     "new": "isaacsim.robot_motion.motion_generation",
     "status": "4.5 rename; deprecated since 6.0 (successor isaacsim.robot_motion.experimental.motion_generation)"},
    {"old": "omni.isaac.examples (BaseSample, hello_world, pick_place)",
     "new": "isaacsim.examples.interactive + isaacsim.examples.base (BaseSample now uses core.experimental, not World)",
     "status": "4.5 rename; 6.1 interactive samples no longer use World"},
    {"old": "(new in 6.x) maintained pick-and-place",
     "new": "isaacsim.robot_motion.examples.manipulation (ManipulationScenario, PickPlaceController) + "
            "isaacsim.robot_motion.cumotion.RmpFlowController + isaacsim.core.experimental.*",
     "status": "verified: standalone_examples/api/isaacsim.robot_motion.examples/manipulation/pick_place.py"},
)


# --------------------------------------------------------------------------------------------------- lesson plan


def lesson_plan() -> dict[str, Any]:
    """The pick-and-place lesson, one concept per step, in the order to do them. Read-only, no pxr needed."""
    steps = [
        {
            "id": "stage_setup",
            "title": "Start a clean robot cell (metres, Z up)",
            "concept": "Stage units and up axis",
            "explain": "Isaac Sim and the Franka asset work in metres with Z pointing up. If the stage uses centimetres "
            "or Y up, every distance and gravity direction in your script is off by 100x or points sideways.",
            "do": "Create a new project from the robot_cell template (Z up, metres, kilograms, a PhysicsScene, a "
            "colliding ground and a /World/Robot slot), or start from an empty stage in Isaac Sim (File > New).",
            "mcp_phrase": "Start a new robot_cell project called franka_pick_place",
            "tools": ["project_plan", "project_create"],
            "check": "scene_stage_info shows metersPerUnit 1 and upAxis Z.",
        },
        {
            "id": "ground",
            "title": "Add a ground that collides",
            "concept": "Collider (PhysicsCollisionAPI)",
            "explain": "A collider gives a prim a solid shape for physics. Without a colliding ground the cube and "
            "anything you drop fall forever.",
            "do": "Use the template's ground, or in a script world.scene.add_default_ground_plane().",
            "mcp_phrase": "Make /World/Environment/GroundPlane a collider",
            "tools": ["scene_make_collider", "scene_physics_report"],
            "check": "scene_physics_report lists the ground under colliders.",
        },
        {
            "id": "table",
            "title": "(Optional) Add a table",
            "concept": "Static collider vs rigid body",
            "explain": "A table only needs a collider (it never moves), not a rigid body. Rigid bodies are moved by "
            "physics; static colliders are walls, floors and tables that things rest on. Keep the table top low "
            "(about 0.0-0.3 m) so the cube stays within the Franka's reach.",
            "do": "Create a Cube prim scaled into a table top and make it a collider only.",
            "mcp_phrase": "Create a cube at /World/Table, scale it to 0.6 x 0.6 x 0.02 m and make it a collider",
            "tools": ["scene_create", "scene_set_attribute", "scene_make_collider"],
            "check": "readiness check 'object_resting' names the table as the support.",
        },
        {
            "id": "cube",
            "title": "Add the cube to pick",
            "concept": "Rigid body + collider + mass",
            "explain": "The cube must be a rigid body (physics moves it), have a collider (the fingers can touch it) "
            "and a sensible mass (0.01-1 kg for the Franka). It must also fit between the open fingers: the Franka "
            "opens 0.08 m, so 5 cm is the classic size. The robot_cell template's 10 cm workpiece is too wide.",
            "do": "Create a 0.05 m cube resting on the ground (centre z = 0.025 m), make it a rigid body with a mass.",
            "mcp_phrase": "Make /World/Cube a rigid body with mass 0.1 kg and a collider",
            "tools": ["scene_create", "scene_make_rigid_body", "scene_make_collider"],
            "check": "readiness check 'object_size' and 'object_mass' pass.",
        },
        {
            "id": "robot",
            "title": "Bring in the Franka",
            "concept": "Reference + articulation root + fixed base",
            "explain": "The Franka comes from franka.usd (Isaac assets: Isaac/Robots/FrankaRobotics/FrankaPanda). "
            "It is an articulation: links joined by joints, with PhysicsArticulationRootAPI on /World/Franka and a "
            "rootJoint that bolts panda_link0 to the world (a fixed base). Its two finger joints are sliders with "
            "0..0.04 m limits; panda_finger_joint1 has the drive and joint2 mimics it.",
            "do": "Reference franka.usd at /World/Franka (robot import helper or asset search), then inspect it.",
            "mcp_phrase": "Add the Franka robot at /World/Franka and inspect it",
            "tools": ["asset_search", "scene_add_asset", "robot_inspect", "robot_import (planned)"],
            "check": "robot_inspect shows 9 DOF, a fixed base and drives on the joints.",
        },
        {
            "id": "physics_scene",
            "title": "Make sure there is a physics scene",
            "concept": "PhysicsScene (gravity and the solver)",
            "explain": "A PhysicsScene prim switches simulation on and sets gravity. In a script, World() creates one "
            "at /physicsScene if the stage has none.",
            "do": "Add a physics scene if the stage has none.",
            "mcp_phrase": "Add a physics scene with normal gravity",
            "tools": ["scene_add_physics_scene"],
            "check": "readiness check 'physics_scene' passes.",
        },
        {
            "id": "readiness",
            "title": "Run the readiness check",
            "concept": "Debug before you simulate",
            "explain": "Most failed pick-and-place demos are scene problems (cube too big, out of reach, floating, no "
            "collider), not code problems. The readiness check finds them with the scene stopped.",
            "do": "Run the pick-and-place readiness check on the open stage and fix every 'fail'.",
            "mcp_phrase": "Is my scene ready for a Franka pick and place?",
            "tools": ["pick_place_readiness (pick_place.readiness_check)"],
            "check": "ready is true.",
        },
        {
            "id": "controller",
            "title": "Understand the controller",
            "concept": "PickPlaceController: a 10-phase state machine on top of RMPflow",
            "explain": "Each physics step you call controller.forward(picking_position, placing_position, "
            "current_joint_positions). It walks through 10 phases: move above the cube, lower, wait, close the "
            "gripper, lift, move over the target, lower, open, lift, go home. RMPflow (a motion policy) turns each "
            "end-effector target into joint targets; the articulation controller applies them to the joint drives.",
            "do": "Read the generated script top to bottom; every line is commented.",
            "mcp_phrase": "Show me the pick and place script for the Script Editor",
            "tools": ["pick_place_script (pick_place.generate_script)", "python_show"],
            "check": "You can say what each of the 10 phases does.",
        },
        {
            "id": "run",
            "title": "Run it",
            "concept": "Standalone app vs Script Editor",
            "explain": "Standalone: python.bat starts Isaac Sim from your script, and your while-loop steps the world. "
            "Script Editor: Isaac Sim is already running, so you must not block it; you use await and a physics "
            "callback that runs once per physics step.",
            "do": "Generate the script in the mode you want and follow how_to_run.",
            "mcp_phrase": "Give me the standalone pick and place script",
            "tools": ["pick_place_script (pick_place.generate_script)", "sim_play", "sim_stop"],
            "check": "The cube ends up at the place position and the console prints 'done picking and placing'.",
        },
        {
            "id": "debug",
            "title": "Debug what went wrong",
            "concept": "Read the error, then re-check the scene",
            "explain": "Paste the error into the error explainer; re-run the readiness check; look at the joint "
            "drives with the robot inspector. The script's troubleshooting list covers the 8 most common failures.",
            "do": "Explain the last error and re-run the readiness check.",
            "mcp_phrase": "Explain the last Isaac Sim error",
            "tools": ["explain_error", "kit_explain_recent_errors", "robot_inspect", "joint_control (planned)",
                      "sensors (planned)"],
            "check": "The fix is applied and the readiness check is still ready.",
        },
    ]
    for number, step in enumerate(steps, start=1):
        step["step"] = number
    return {
        "ok": True,
        "action": "pick_place_lesson",
        "title": "Franka pick-and-place, step by step (Isaac Sim 6.1)",
        "steps": steps,
        "step_count": len(steps),
        "api_note": _api_note(),
        "old_to_new_names": [dict(item) for item in OLD_TO_NEW],
        "next_lesson": "After the cube moves: change place_position, add a second cube, then try the maintained 6.1 "
        "example standalone_examples/api/isaacsim.robot_motion.examples/manipulation/pick_place.py.",
    }


def _api_note() -> str:
    return (
        "Isaac Sim 6.1 still ships the classic API used by most videos (isaacsim.core.api World, "
        "isaacsim.robot.manipulators, the Franka example class), but it is deprecated since 6.0 and lives in "
        "extsDeprecated/. The example extension isaacsim.robot.manipulators.examples is off by default, so the "
        "scripts enable it. The maintained replacement is isaacsim.core.experimental.* with "
        "isaacsim.robot_motion.examples (see standalone_examples/api/isaacsim.robot_motion.examples/manipulation/"
        "pick_place.py); expect deprecation warnings in the console, they are not errors."
    )


# --------------------------------------------------------------------------------------------------- readiness


def readiness_check(stage: Any, *, robot_root: str | None = None, object_path: str | None = None) -> dict[str, Any]:
    """Is this stage ready for a Franka pick-and-place? Pure pxr, read-only: nothing is edited or saved.

    robot_root is the robot's top prim (default: the first articulation whose name says franka/panda, else the first
    robot); object_path is the object to pick (default: the first free rigid body that is not part of a robot,
    preferring names like cube/box/workpiece).

    Returns {ok, action, read_only, ready, robot_root, object_path, checks: [{id, title, status, why, how_to_fix,
    detail}], counts, measurements, summary, notes}. status is pass, warn, fail or skip (skipped because an earlier
    check failed); ready is True when nothing failed.
    """
    base: dict[str, Any] = {"ok": False, "action": "pick_place_readiness", "read_only": True}
    if _PXR_IMPORT_ERROR is not None:
        return {**base, "error": f"pxr (OpenUSD) is not available here: {_PXR_IMPORT_ERROR}"}
    if stage is None or not hasattr(stage, "GetPseudoRoot"):
        return {**base, "error": "stage must be an open Usd.Stage."}
    for label, value in (("robot_root", robot_root), ("object_path", object_path)):
        if value is None:
            continue
        if not isinstance(value, str) or not _PRIM_PATH.match(value.strip()):
            return {**base, "error": f"{label} must be an absolute prim path such as /World/Franka, or None."}
    try:
        return _Readiness(stage).run(
            robot_root.strip() if isinstance(robot_root, str) else None,
            object_path.strip() if isinstance(object_path, str) else None,
            base,
        )
    except Exception as exc:  # read-only: report instead of raising into a tool call
        return {**base, "error": f"Unexpected error while reading the stage (nothing was changed): {exc}"}


def _check(cid: str, title: str, status: str, why: str, how_to_fix: str | None = None, **detail: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "title": title,
        "status": status,
        "why": why,
        "how_to_fix": None if status == "pass" else how_to_fix,
        "detail": detail,
    }


def _skip(cid: str, title: str, reason: str) -> dict[str, Any]:
    return _check(cid, title, "skip", f"Skipped: {reason}", f"Fix the earlier failed check first ({reason}).")


class _Readiness:
    def __init__(self, stage: Any) -> None:
        self.stage = stage
        self.mpu = _positive(UsdGeom.GetStageMetersPerUnit(stage), 1.0)
        try:
            self.kgpu = _positive(UsdPhysics.GetStageKilogramsPerUnit(stage), 1.0)
        except Exception:
            self.kgpu = 1.0
        self.up = "Y" if str(UsdGeom.GetStageUpAxis(stage)) == "Y" else "Z"
        self.up_index = 1 if self.up == "Y" else 2
        self.flat = (0, 2) if self.up == "Y" else (0, 1)
        self.bbox = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(),
            [UsdGeom.Tokens.default_, UsdGeom.Tokens.render, UsdGeom.Tokens.proxy],
            useExtentsHint=False,
        )
        self.xf = UsdGeom.XformCache(Usd.TimeCode.Default())
        self.notes: list[str] = []
        self.scenes: list[Any] = []
        self.colliders: list[Any] = []
        self.rigid: list[Any] = []
        self.truncated = False

    # -- scan --

    def _scan(self) -> None:
        predicate = Usd.TraverseInstanceProxies(Usd.PrimDefaultPredicate)
        for count, prim in enumerate(Usd.PrimRange(self.stage.GetPseudoRoot(), predicate)):
            if count > MAX_SCAN:
                self.truncated = True
                self.notes.append(f"Stopped scanning after {MAX_SCAN} prims; results may be incomplete.")
                break
            if prim.IsPseudoRoot():
                continue
            if prim.IsA(UsdPhysics.Scene):
                self.scenes.append(prim)
            if prim.HasAPI(UsdPhysics.CollisionAPI):
                self.colliders.append(prim)
            if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                self.rigid.append(prim)

    # -- driver --

    def run(self, robot_root: str | None, object_path: str | None, base: dict[str, Any]) -> dict[str, Any]:
        self._scan()
        checks: list[dict[str, Any]] = []
        measurements: dict[str, Any] = {"meters_per_unit": self.mpu, "up_axis": self.up}

        checks.append(self._units_check())
        checks.append(self._up_check())
        checks.append(self._physics_scene_check())
        checks.append(self._ground_check())

        robot = self._find_robot(robot_root)
        checks.append(robot["check"])
        robot_info = robot.get("robot")
        base_pos: tuple[float, float, float] | None = None
        opening = FRANKA_FINGER_OPENING_M
        robot_path = robot.get("root")
        if robot_info is None:
            checks.append(_skip("robot_fixed_base", "Robot base is fixed to the world", "no robot was found"))
            checks.append(_skip("gripper_fingers", "Gripper fingers with drives", "no robot was found"))
        else:
            checks.append(self._fixed_base_check(robot_info))
            finger_check, opening = self._gripper_check(robot_info)
            checks.append(finger_check)
            base_pos = self._base_position(robot_info)
            measurements["robot_base_position_m"] = [round(v, 4) for v in base_pos]
        measurements["gripper_opening_m"] = round(opening, 4)

        obj = self._find_object(object_path, robot_path)
        checks.append(obj["check"])
        obj_prim = obj.get("prim")
        object_names = (
            ("object_rigid_body", "Object is a rigid body"),
            ("object_collider", "Object has a collider"),
            ("object_mass", "Object mass is reasonable"),
            ("object_size", "Object fits in the gripper"),
            ("object_reach", "Object is within the Franka's reach"),
            ("object_resting", "Object rests on a surface"),
        )
        if obj_prim is None:
            for cid, title in object_names:
                checks.append(_skip(cid, title, "no object to pick was found"))
        else:
            box = self._world_box(obj_prim)
            body, body_check = self._rigid_check(obj_prim)
            checks.append(body_check)
            checks.append(self._collider_check(obj_prim))
            checks.append(self._mass_check(obj_prim, body, box, measurements))
            checks.append(self._size_check(box, opening, measurements))
            checks.append(self._reach_check(box, base_pos, measurements))
            checks.append(self._resting_check(obj_prim, box, robot_path, measurements))

        counts = {status: sum(1 for item in checks if item["status"] == status) for status in STATUSES}
        ready = counts["fail"] == 0
        return {
            **base,
            "ok": True,
            "ready": ready,
            "robot_root": robot_path,
            "object_path": str(obj_prim.GetPath()) if obj_prim is not None else None,
            "checks": checks,
            "counts": counts,
            "measurements": measurements,
            "summary": self._summary(ready, checks, counts),
            "notes": self.notes,
            "scan_truncated": self.truncated,
        }

    # -- stage-level checks --

    def _units_check(self) -> dict[str, Any]:
        title = "Stage units are metres"
        if abs(self.mpu - 1.0) < 1e-9:
            return _check("units_metres", title, "pass", "metersPerUnit is 1: one stage unit is one metre, like "
                          "the Franka asset and the World(stage_units_in_meters=1.0) scripts.", meters_per_unit=self.mpu)
        return _check(
            "units_metres", title, "warn",
            f"metersPerUnit is {self.mpu:g}, so one stage unit is not a metre. The Franka asset and the generated "
            "scripts use metres; positions you type would be read in the wrong unit. (These checks convert, so the "
            "numbers below are still in metres.)",
            "Start the pick-and-place in a metres stage (robot_cell template, or File > New in Isaac Sim), or set "
            "World(stage_units_in_meters=...) to match this stage.",
            meters_per_unit=self.mpu,
        )

    def _up_check(self) -> dict[str, Any]:
        title = "Up axis is Z"
        if self.up == "Z":
            return _check("up_axis_z", title, "pass", "upAxis is Z, as Isaac Sim and the Franka asset expect.")
        return _check(
            "up_axis_z", title, "warn",
            "upAxis is Y. Isaac Sim robots and the PickPlaceController assume Z up, so heights and gravity would "
            "point the wrong way in a script.",
            "Use a Z-up stage for robotics (robot_cell template, or File > New in Isaac Sim).",
        )

    def _physics_scene_check(self) -> dict[str, Any]:
        title = "Physics scene present"
        if self.scenes:
            paths = [str(prim.GetPath()) for prim in self.scenes]
            return _check("physics_scene", title, "pass",
                          f"Found a PhysicsScene at {paths[0]}: physics will simulate with gravity.", paths=paths)
        return _check(
            "physics_scene", title, "fail",
            "No PhysicsScene prim: pressing Play simulates nothing, so the cube never falls and the gripper cannot "
            "push it.",
            "Add one (MCP: 'add a physics scene', tool scene_add_physics_scene). In a script, World() creates "
            "/physicsScene for you when the stage has none.",
        )

    def _ground_check(self) -> dict[str, Any]:
        title = "Ground has a collider"
        floors = [prim for prim in self.colliders if self._floor_like(prim) and not self._dynamic_owner(prim)]
        if floors:
            return _check("ground_collider", title, "pass",
                          f"{floors[0].GetPath()} is a colliding ground: objects land on it instead of falling forever.",
                          paths=[str(prim.GetPath()) for prim in floors[:10]])
        return _check(
            "ground_collider", title, "fail",
            "No ground or floor prim has a collider, so anything that falls (a dropped cube, a missed grasp) falls "
            "forever.",
            "Make the ground a collider (MCP: 'make the ground a collider', tool scene_make_collider), or in a "
            "script call world.scene.add_default_ground_plane().",
        )

    # -- robot --

    def _find_robot(self, robot_root: str | None) -> dict[str, Any]:
        title = "Robot found (articulation root)"
        if robot_root is not None:
            prim = self.stage.GetPrimAtPath(robot_root)
            if not prim or not prim.IsValid():
                return {"check": _check("robot_found", title, "fail", f"There is no prim at {robot_root}.",
                                        "Check the path (scene_find 'Franka'), or add the Franka at that path.")}
            report = inspect_robot(self.stage, robot_root)
        else:
            report = inspect_robot(self.stage)
        if not report.get("ok"):
            return {"check": _check("robot_found", title, "fail",
                                    f"The robot could not be read: {report.get('error')}", "Inspect the robot "
                                    "with robot_inspect and fix the error it reports.")}
        if report.get("truncated") or report.get("truncated_by_time"):
            self.notes.append("The robot scan stopped early; pass robot_root to check one robot.")
        robots = report.get("robots") or []
        if not robots:
            where = f" at {robot_root}" if robot_root else " in the stage"
            return {"check": _check(
                "robot_found", title, "fail",
                f"No robot (articulation){where}: nothing to pick with.",
                "Add the Franka (Isaac assets Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd) at /World/Franka; "
                "MCP: 'add the Franka robot'.",
            )}
        rooted = [item for item in robots if item.get("articulation_root")]
        named = [item for item in rooted if _ROBOT_NAME.search(item.get("root") or "")]
        robot = (named or rooted or robots)[0]
        root = robot.get("root")
        if not robot.get("articulation_root"):
            return {"root": root, "check": _check(
                "robot_found", title, "fail",
                f"{root} has jointed parts but no PhysicsArticulationRootAPI, so Isaac Sim's Franka / Articulation "
                "classes cannot find it as one robot and PhysX simulates loose bodies.",
                f"Apply PhysicsArticulationRootAPI to {root} (Isaac Sim: right-click > Add > Physics > Articulation "
                "Root), or reference the original franka.usd, which already has it.",
            )}
        others = len(robots) - 1
        extra = f" ({others} other robot(s) ignored; pass robot_root to choose)" if others and robot_root is None else ""
        franka = bool(_ROBOT_NAME.search(root or ""))
        if not franka:
            self.notes.append(f"{root} does not look like a Franka; the size and reach limits below are the Franka's.")
        return {"root": root, "robot": robot, "check": _check(
            "robot_found", title, "pass",
            f"Found the robot {root} with {robot.get('dof_count')} degrees of freedom{extra}.",
            root=root, dof=robot.get("dof_count"), looks_like_franka=franka,
        )}

    def _fixed_base_check(self, robot: dict[str, Any]) -> dict[str, Any]:
        title = "Robot base is fixed to the world"
        info = robot.get("base") or {}
        if info.get("fixed_to_world"):
            return _check("robot_fixed_base", title, "pass",
                          f"{info.get('name')} is attached to the world by {info.get('via_joint')}, so the arm "
                          "stays put while it swings.", via_joint=info.get("via_joint"))
        if info.get("kinematic"):
            return _check("robot_fixed_base", title, "warn",
                          f"{info.get('name')} is kinematic (held by code, not by a joint). It will not fall, but "
                          "Isaac's Franka asset normally uses a fixed root joint.",
                          "Prefer a fixed joint from the base link to the world (franka.usd has /rootJoint).")
        return _check(
            "robot_fixed_base", title, "fail",
            "The base is free (no joint to the world), so the arm topples over or slides away when it moves.",
            "Use the original franka.usd (it has a fixed rootJoint), or add a fixed joint from panda_link0 to the "
            "world (Isaac Sim: right-click the base link > Create > Physics > Joint > Fixed Joint).",
        )

    def _gripper_check(self, robot: dict[str, Any]) -> tuple[dict[str, Any], float]:
        title = "Gripper fingers with drives"
        joints = [j for j in robot.get("joints") or [] if j.get("kind") == "prismatic"
                  and _FINGER_NAME.search(j.get("name") or "")]
        if len(joints) < 2:
            return _check(
                "gripper_fingers", title, "fail",
                f"Found {len(joints)} finger joint(s) (sliding joints named *finger*); a parallel gripper needs two, "
                "so the robot cannot hold anything.",
                "Use the Franka with its default hand (franka.usd, Gripper variant 'Default' or 'AlternateFinger'); "
                "the joints are panda_finger_joint1 and panda_finger_joint2.",
                finger_joints=[j.get("path") for j in joints],
            ), FRANKA_FINGER_OPENING_M
        driven = [j for j in joints if any((d.get("stiffness") or 0) > 0 or (d.get("damping") or 0) > 0
                                           for d in j.get("drives") or [])]
        mimics = [j for j in joints if j.get("mimics")]
        limp = [j.get("path") for j in joints if j not in driven and j not in mimics]
        uppers = [(j.get("limits") or {}).get("upper") for j in joints]
        opening = (
            float(sum(uppers)) if all(isinstance(u, (int, float)) and u > 0 for u in uppers)
            else FRANKA_FINGER_OPENING_M
        )
        names = [j.get("path") for j in joints]
        if limp or not driven:
            return _check(
                "gripper_fingers", title, "fail",
                "Finger joints without a drive (motor) or mimic link cannot close: "
                + (", ".join(limp) if limp else "no finger has its own drive") + ".",
                "Add a linear PhysicsDriveAPI (stiffness about 400, damping about 80) to panda_finger_joint1, or "
                "re-reference the original franka.usd.",
                finger_joints=names, gripper_opening_m=round(opening, 4),
            ), opening
        return _check(
            "gripper_fingers", title, "pass",
            f"{len(joints)} finger joints, {len(driven)} driven"
            + (f" and {len(mimics)} mimicking" if mimics else "")
            + f"; the open gripper is about {opening * 100:.1f} cm wide.",
            finger_joints=names, gripper_opening_m=round(opening, 4),
        ), opening

    def _base_position(self, robot: dict[str, Any]) -> tuple[float, float, float]:
        info = robot.get("base") or {}
        path = info.get("link") or robot.get("root")
        prim = self.stage.GetPrimAtPath(path) if path else None
        if prim and prim.IsValid() and prim.IsA(UsdGeom.Xformable):
            t = self.xf.GetLocalToWorldTransform(prim).ExtractTranslation()
            return (t[0] * self.mpu, t[1] * self.mpu, t[2] * self.mpu)
        return (0.0, 0.0, 0.0)

    # -- object --

    def _find_object(self, object_path: str | None, robot_root: str | None) -> dict[str, Any]:
        title = "Object to pick found"
        if object_path is not None:
            prim = self.stage.GetPrimAtPath(object_path)
            if not prim or not prim.IsValid():
                return {"check": _check("object_found", title, "fail", f"There is no prim at {object_path}.",
                                        "Check the path (scene_find 'Cube'), or create the cube first.")}
            return {"prim": prim, "check": _check("object_found", title, "pass", f"Checking {object_path}.",
                                                  path=object_path)}
        robots = self._robot_roots()
        if robot_root:
            robots.append(robot_root)
        # Seen live in Isaac 6.1: a door hinged to the world was "picked"; a body held by a joint cannot be lifted.
        jointed = self._jointed_bodies()
        candidates = [
            prim for prim in self.rigid
            if not any(_is_under(str(prim.GetPath()), root) for root in robots) and not _kinematic(prim)
            and str(prim.GetPath()) not in jointed
        ]
        if not candidates:
            return {"check": _check(
                "object_found", title, "fail",
                "No free rigid body outside the robot: there is nothing for the gripper to pick.",
                "Create a 0.05 m cube resting on the ground and make it a rigid body with a collider (MCP: 'create "
                "a cube at /World/Cube' then 'make /World/Cube a rigid body'), or pass object_path.",
            )}
        named = [prim for prim in candidates if _OBJECT_NAME.search(prim.GetName())]
        prim = (named or candidates)[0]
        extra = f" ({len(candidates) - 1} other free rigid bodies; pass object_path to choose)" if len(candidates) > 1 else ""
        return {"prim": prim, "check": _check("object_found", title, "pass", f"Picked {prim.GetPath()}{extra}.",
                                              path=str(prim.GetPath()))}

    def _jointed_bodies(self) -> set[str]:
        """Bodies a physics joint holds (its body0/body1 targets): hinged doors, sliding drawers, and so on."""
        held: set[str] = set()
        for prim in self.stage.Traverse():
            if prim.IsA(UsdPhysics.Joint):
                joint = UsdPhysics.Joint(prim)
                for rel in (joint.GetBody0Rel(), joint.GetBody1Rel()):
                    held.update(str(target) for target in rel.GetTargets())
        return held

    def _robot_roots(self) -> list[str]:
        roots = []
        for prim in self.stage.Traverse():
            if prim.HasAPI(UsdPhysics.ArticulationRootAPI):
                roots.append(str(prim.GetPath()))
        return roots

    def _rigid_check(self, prim: Any) -> tuple[Any, dict[str, Any]]:
        title = "Object is a rigid body"
        body = _rigid_owner(prim)
        path = str(prim.GetPath())
        if body is None:
            return None, _check(
                "object_rigid_body", title, "fail",
                f"{path} is not a rigid body, so physics never moves it: the fingers close on it and lift nothing.",
                f"Make it a rigid body (MCP: 'make {path} a rigid body', tool scene_make_rigid_body).",
            )
        if _kinematic(body):
            return body, _check(
                "object_rigid_body", title, "fail",
                f"{body.GetPath()} is a kinematic rigid body: only code moves it, so the gripper cannot pick it up.",
                "Turn off physics:kinematicEnabled on it (Property panel > Rigid Body > Kinematic Enabled).",
            )
        where = "" if body == prim else f" (via its parent {body.GetPath()})"
        return body, _check("object_rigid_body", title, "pass", f"{path} is a dynamic rigid body{where}.",
                            body=str(body.GetPath()))

    def _collider_check(self, prim: Any) -> dict[str, Any]:
        title = "Object has a collider"
        path = str(prim.GetPath())
        body = _rigid_owner(prim) or prim
        found = [p for p in self.colliders if _is_under(str(p.GetPath()), str(body.GetPath()))]
        if found:
            return _check("object_collider", title, "pass",
                          f"{found[0].GetPath()} gives the object a solid shape the fingers can touch.",
                          colliders=[str(p.GetPath()) for p in found[:10]])
        return _check(
            "object_collider", title, "fail",
            f"{path} has no collider: it falls through the ground and the fingers pass straight through it.",
            f"Add a collider (MCP: 'make {path} a collider', tool scene_make_collider).",
        )

    def _mass_check(self, prim: Any, body: Any, box: Any, measurements: dict[str, Any]) -> dict[str, Any]:
        title = "Object mass is reasonable"
        target = body or prim
        mass, source = self._mass(target, box)
        if mass is None:
            return _check("object_mass", title, "warn",
                          "The mass could not be read or estimated (no mass, no density and no size).",
                          f"Set a mass of about 0.1 kg (MCP: 'set the mass of {target.GetPath()} to 0.1 kg').")
        measurements["object_mass_kg"] = round(mass, 4)
        measurements["object_mass_source"] = source
        text = f"{mass:.3g} kg ({source})"
        fix = f"Set the mass to about 0.1 kg (MCP: 'set the mass of {target.GetPath()} to 0.1 kg')."
        if mass <= 0:
            return _check("object_mass", title, "fail", f"Mass is {text}: PhysX needs a positive mass.", fix,
                          mass_kg=mass, source=source)
        if mass > MAX_MASS_KG:
            return _check(
                "object_mass", title, "fail",
                f"Mass is {text}: heavier than {MAX_MASS_KG:g} kg slips out of the Franka's fingers (they squeeze "
                "with only a few newtons).", fix, mass_kg=mass, source=source,
            )
        if mass < MIN_MASS_KG:
            return _check(
                "object_mass", title, "warn",
                f"Mass is {text}: lighter than {MIN_MASS_KG:g} kg gets flung around by contacts.", fix,
                mass_kg=mass, source=source,
            )
        return _check("object_mass", title, "pass", f"Mass is {text}, easy for the Franka to lift.",
                      mass_kg=mass, source=source)

    def _mass(self, body: Any, box: Any) -> tuple[float | None, str]:
        mass = _float_attr(body, "physics:mass")
        if mass is not None and mass > 0:
            return mass * self.kgpu, "authored mass"
        child_masses = [
            _float_attr(p, "physics:mass") for p in Usd.PrimRange(body, Usd.TraverseInstanceProxies())
            if p != body
        ]
        child_total = sum(m for m in child_masses if m is not None and m > 0)
        if child_total > 0:
            return child_total * self.kgpu, "authored mass on its parts"
        if mass is not None and mass < 0:
            return mass * self.kgpu, "authored mass"
        volume = None
        if box is not None:
            size = box["size_m"]
            volume = size[0] * size[1] * size[2]
        density = _float_attr(body, "physics:density")
        if density is not None and density > 0 and volume is not None:
            density_kg_m3 = density * self.kgpu / (self.mpu ** 3)
            return density_kg_m3 * volume, f"density {density_kg_m3:g} kg/m3 x bounding-box volume"
        if volume is not None and volume > 0:
            return DEFAULT_DENSITY_KG_M3 * volume, "estimated: PhysX default density 1000 kg/m3 x bounding-box volume"
        return None, "unknown"

    def _size_check(self, box: Any, opening: float, measurements: dict[str, Any]) -> dict[str, Any]:
        title = "Object fits in the gripper"
        if box is None:
            return _check("object_size", title, "warn", "The object has no geometry to measure.",
                          "Give the object a shape (a Cube or Mesh child).")
        size = box["size_m"]
        flat = [size[i] for i in self.flat]
        width = min(flat)
        measurements["object_size_m"] = [round(v, 4) for v in size]
        measurements["object_grasp_width_m"] = round(width, 4)
        dims = " x ".join(f"{v:.3f}" for v in size) + " m"
        detail = {"size_m": [round(v, 4) for v in size], "grasp_width_m": round(width, 4),
                  "gripper_opening_m": round(opening, 4)}
        fix = (f"Scale the object so its narrow side is at most {opening - GRASP_MARGIN_M:.2f} m (0.05 m is the "
               "classic Franka cube).")
        if width > opening:
            return _check("object_size", title, "fail",
                          f"The object is {dims}; its narrowest side ({width:.3f} m) is wider than the open gripper "
                          f"({opening:.3f} m), so the fingers cannot go around it.", fix, **detail)
        if width > opening - GRASP_MARGIN_M:
            return _check("object_size", title, "warn",
                          f"The object is {dims}; its narrowest side ({width:.3f} m) leaves less than "
                          f"{GRASP_MARGIN_M * 100:.0f} cm of clearance, so a small position error knocks it over.",
                          fix, **detail)
        if width < MIN_GRASP_WIDTH_M:
            return _check("object_size", title, "warn",
                          f"The object is {dims}; thinner than {MIN_GRASP_WIDTH_M * 100:.0f} cm slips out of the "
                          "fingers.", "Use an object at least 2 cm wide (5 cm is classic).", **detail)
        return _check("object_size", title, "pass",
                      f"The object is {dims}; its {width:.3f} m side fits in the {opening:.3f} m gripper.", **detail)

    def _reach_check(self, box: Any, base_pos: Any, measurements: dict[str, Any]) -> dict[str, Any]:
        title = "Object is within the Franka's reach"
        if base_pos is None:
            return _skip("object_reach", title, "no robot was found")
        if box is None:
            return _check("object_reach", title, "warn", "The object has no geometry, so its position is unknown.",
                          "Give the object a shape (a Cube or Mesh child).")
        center = box["center_m"]
        delta = [center[i] - base_pos[i] for i in range(3)]
        distance = math.sqrt(sum(d * d for d in delta))
        horizontal = math.sqrt(sum(delta[i] ** 2 for i in self.flat))
        measurements["object_distance_m"] = round(distance, 4)
        measurements["object_horizontal_distance_m"] = round(horizontal, 4)
        detail = {"distance_m": round(distance, 4), "horizontal_m": round(horizontal, 4), "reach_m": FRANKA_REACH_M}
        if distance > FRANKA_REACH_M:
            return _check(
                "object_reach", title, "fail",
                f"The object is {distance:.3f} m from the robot base; the Franka reaches about {FRANKA_REACH_M} m.",
                "Move the object closer: 0.3-0.6 m in front of the base works best (the default cube sits at "
                "x 0.4, y 0.3).", **detail,
            )
        if horizontal < MIN_HORIZONTAL_FROM_BASE_M:
            return _check(
                "object_reach", title, "warn",
                f"The object is only {horizontal:.3f} m (sideways) from the base: the arm folds onto itself and may "
                "hit its own links.", "Move the object 0.3-0.6 m away from the base.", **detail,
            )
        if distance > REACH_EDGE_M:
            return _check(
                "object_reach", title, "warn",
                f"The object is {distance:.3f} m away, near the edge of the {FRANKA_REACH_M} m reach: motions get "
                "slow and the grasp angle awkward.", "Move it within about 0.6 m of the base.", **detail,
            )
        return _check("object_reach", title, "pass", f"The object is {distance:.3f} m from the base, well within "
                      f"the {FRANKA_REACH_M} m reach.", **detail)

    def _resting_check(self, prim: Any, box: Any, robot_root: str | None, measurements: dict[str, Any]) -> dict[str, Any]:
        title = "Object rests on a surface"
        if box is None:
            return _check("object_resting", title, "warn", "The object has no geometry to place.",
                          "Give the object a shape (a Cube or Mesh child).")
        body = _rigid_owner(prim) or prim
        body_path = str(body.GetPath())
        bottom = box["min_m"][self.up_index]
        center = box["center_m"]
        best: tuple[float, str] | None = None
        inside: list[str] = []
        for collider in self.colliders:
            path = str(collider.GetPath())
            if _is_under(path, body_path) or (robot_root and _is_under(path, robot_root)):
                continue
            if self._dynamic_owner(collider):
                continue
            top, low = self._support_top(collider, center)
            if top is None:
                continue
            if top <= bottom + PENETRATION_TOLERANCE_M:
                if best is None or top > best[0]:
                    best = (top, path)
            elif low is not None and low < bottom:
                inside.append(path)
        if inside:
            return _check(
                "object_resting", title, "warn",
                f"The object starts inside {inside[0]}: PhysX pushes it out with a jump when you press Play.",
                f"Raise the object so its bottom sits on the top of {inside[0]}.", inside=inside[:5],
            )
        if best is None:
            return _check(
                "object_resting", title, "fail",
                "Nothing with a collider is under the object: it falls forever when you press Play.",
                "Add a colliding ground (or table) under it; in a script world.scene.add_default_ground_plane().",
            )
        gap = bottom - best[0]
        measurements["object_gap_m"] = round(gap, 4)
        measurements["object_support"] = best[1]
        if gap > REST_TOLERANCE_M:
            return _check(
                "object_resting", title, "warn",
                f"The object floats {gap:.3f} m above {best[1]}: it drops when you press Play and may bounce or "
                "roll away from the pick position.",
                f"Lower it by {gap:.3f} m so it sits on the surface (a 0.05 m cube on the ground has centre z = 0.025).",
                gap_m=round(gap, 4), support=best[1],
            )
        return _check("object_resting", title, "pass", f"The object sits on {best[1]} (gap {max(gap, 0.0):.3f} m).",
                      gap_m=round(gap, 4), support=best[1])

    # -- geometry helpers --

    def _world_box(self, prim: Any) -> dict[str, Any] | None:
        try:
            box = self.bbox.ComputeWorldBound(prim).ComputeAlignedRange()
        except Exception:
            return None
        if box.IsEmpty():
            return None
        low, high = box.GetMin(), box.GetMax()
        low_m = [low[i] * self.mpu for i in range(3)]
        high_m = [high[i] * self.mpu for i in range(3)]
        if not all(math.isfinite(v) for v in low_m + high_m):
            return None
        return {
            "min_m": low_m,
            "max_m": high_m,
            "size_m": [high_m[i] - low_m[i] for i in range(3)],
            "center_m": [(high_m[i] + low_m[i]) / 2 for i in range(3)],
        }

    def _support_top(self, collider: Any, center: list[float]) -> tuple[float | None, float | None]:
        """(top height, bottom height) in metres of a static collider under center, or (None, None)."""
        if collider.IsA(UsdGeom.Plane):  # PhysX treats a collision plane as infinite
            t = self.xf.GetLocalToWorldTransform(collider).ExtractTranslation()
            height = t[self.up_index] * self.mpu
            return height, None
        box = self._world_box(collider)
        if box is None:
            return None, None
        tolerance = 1e-6
        for i in self.flat:
            if not box["min_m"][i] - tolerance <= center[i] <= box["max_m"][i] + tolerance:
                return None, None
        return box["max_m"][self.up_index], box["min_m"][self.up_index]

    def _dynamic_owner(self, prim: Any) -> bool:
        owner = _rigid_owner(prim)
        return owner is not None and not _kinematic(owner)

    @staticmethod
    def _floor_like(prim: Any) -> bool:
        if prim.IsA(UsdGeom.Plane):
            return True
        current = prim
        for _ in range(3):
            if not current or not current.IsValid() or current.IsPseudoRoot():
                break
            if _FLOOR_NAME.search(current.GetName()):
                return True
            current = current.GetParent()
        return False

    # -- summary --

    @staticmethod
    def _summary(ready: bool, checks: list[dict[str, Any]], counts: dict[str, int]) -> str:
        if ready and not counts["warn"]:
            return "Ready: every pick-and-place check passed. Generate the script and press Play."
        failed = [item["title"] for item in checks if item["status"] == "fail"]
        warned = [item["title"] for item in checks if item["status"] == "warn"]
        if ready:
            return f"Ready, with {len(warned)} warning(s) worth a look: " + "; ".join(warned) + "."
        text = f"Not ready: {len(failed)} check(s) failed: " + "; ".join(failed) + "."
        if counts["skip"]:
            text += f" {counts['skip']} check(s) were skipped until those are fixed."
        return text + " Fix them in order (each check says how), then run the check again."


def _rigid_owner(prim: Any) -> Any:
    current = prim
    while current and current.IsValid() and not current.IsPseudoRoot():
        if current.HasAPI(UsdPhysics.RigidBodyAPI):
            return current
        current = current.GetParent()
    return None


def _kinematic(prim: Any) -> bool:
    return _attr_value(prim, "physics:kinematicEnabled") is True


def _attr_value(prim: Any, name: str) -> Any:
    try:
        attr = prim.GetAttribute(name)
        return attr.Get() if attr and attr.HasValue() else None
    except Exception:
        return None


def _float_attr(prim: Any, name: str) -> float | None:
    value = _attr_value(prim, name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _positive(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if math.isfinite(number) and number > 0 else fallback


def _is_under(path: str, ancestor: str) -> bool:
    return path == ancestor or path.startswith(ancestor.rstrip("/") + "/")


# --------------------------------------------------------------------------------------------------- script


class _Refuse(ValueError):
    pass


def generate_script(
    *,
    mode: str = "standalone",
    robot_prim: str = "/World/Franka",
    cube_position: Any = (0.4, 0.3, 0.025),
    place_position: Any = (-0.3, -0.3, 0.025),
    cube_size: Any = 0.05,
) -> dict[str, Any]:
    """A beginner-commented Franka pick-and-place script for Isaac Sim 6.1. Nothing is run or written.

    mode is 'standalone' (run with isaacsim-6.1.0/python.bat) or 'script_editor' (paste into Window > Script
    Editor). Positions are in metres in a Z-up world with the Franka base at the origin; cube_size is the cube edge
    in metres. Values are validated (finite, graspable, within reach) and written into the code as plain literals.

    Returns {ok, action, mode, filename, code, how_to_run, expected_behaviour, troubleshooting, settings, warnings,
    verified_apis, api_note, compiled}; on bad input {ok: False, error}.
    """
    base: dict[str, Any] = {"ok": False, "action": "pick_place_script", "mode": mode if isinstance(mode, str) else None}
    try:
        settings, warnings = _validate(mode, robot_prim, cube_position, place_position, cube_size)
    except _Refuse as exc:
        return {**base, "error": str(exc)}
    builder = _standalone_code if settings["mode"] == "standalone" else _script_editor_code
    code = builder(settings)
    try:
        compile(code, SCRIPT_FILENAME, "exec")  # syntax self-check only; the code is never executed here
    except SyntaxError as exc:  # pragma: no cover - would be a bug in the template
        return {**base, "error": f"Internal error: the generated code does not compile: {exc}"}
    return {
        **base,
        "ok": True,
        "mode": settings["mode"],
        "filename": SCRIPT_FILENAME,
        "code": code,
        "compiled": True,
        "how_to_run": _how_to_run(settings["mode"]),
        "expected_behaviour": _expected(settings),
        "troubleshooting": _troubleshooting(settings["mode"]),
        "settings": {k: settings[k] for k in ("robot_prim", "cube_prim", "cube_position", "place_position", "cube_size")},
        "warnings": warnings,
        "verified_apis": [dict(item) for item in VERIFIED_APIS],
        "api_note": _api_note(),
    }


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Refuse(f"{label} must be a number (metres), got {value!r}.")
    number = float(value)
    if not math.isfinite(number):
        raise _Refuse(f"{label} must be a finite number, got {value!r}.")
    return number


def _vector(value: Any, label: str) -> tuple[float, float, float]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)) or len(value) != 3:
        raise _Refuse(f"{label} must be three numbers [x, y, z] in metres, got {value!r}.")
    x, y, z = (_number(item, f"{label}[{i}]") for i, item in enumerate(value))
    return (x + 0.0, y + 0.0, z + 0.0)


def _validate(mode: Any, robot_prim: Any, cube_position: Any, place_position: Any, cube_size: Any
              ) -> tuple[dict[str, Any], list[str]]:
    if mode not in MODES:
        raise _Refuse(f"mode must be one of {', '.join(MODES)}, got {mode!r}.")
    if not isinstance(robot_prim, str) or len(robot_prim) > 256 or not _PRIM_PATH.match(robot_prim):
        raise _Refuse(f"robot_prim must be an absolute prim path such as /World/Franka (letters, digits and _), "
                      f"got {robot_prim!r}.")
    for reserved in (CUBE_PRIM, GROUND_PRIM, "/World"):
        if _is_under(robot_prim, reserved) and robot_prim != "/World" + robot_prim[len("/World"):] or \
                _is_under(reserved, robot_prim):
            raise _Refuse(f"robot_prim {robot_prim} clashes with {reserved}, which the script uses; pick a "
                          "separate path such as /World/Franka.")
    size = _number(cube_size, "cube_size")
    largest = FRANKA_FINGER_OPENING_M - GRASP_MARGIN_M
    if not 0.02 <= size <= largest:
        raise _Refuse(f"cube_size must be between 0.02 and {largest:.2f} m: the open Franka gripper is "
                      f"{FRANKA_FINGER_OPENING_M} m wide (0.05 is the classic cube), got {size:g}.")
    cube = _vector(cube_position, "cube_position")
    place = _vector(place_position, "place_position")
    warnings: list[str] = []
    for label, point in (("cube_position", cube), ("place_position", place)):
        distance = math.sqrt(sum(v * v for v in point))
        horizontal = math.hypot(point[0], point[1])
        if distance > FRANKA_REACH_M:
            raise _Refuse(f"{label} {list(point)} is {distance:.3f} m from the Franka base (at the origin); it "
                          f"reaches about {FRANKA_REACH_M} m. Use points 0.3-0.6 m away.")
        if horizontal < MIN_HORIZONTAL_FROM_BASE_M:
            raise _Refuse(f"{label} {list(point)} is only {horizontal:.3f} m from the base sideways; the arm would "
                          f"hit itself. Keep x/y at least {MIN_HORIZONTAL_FROM_BASE_M} m away from the origin.")
        if point[2] < size / 2 - 0.005:
            raise _Refuse(f"{label} z = {point[2]:g} puts the cube centre below half its size ({size / 2:g} m): "
                          "it would start inside the ground. Use z = cube_size / 2.")
        if distance > REACH_EDGE_M:
            warnings.append(f"{label} is {distance:.3f} m away, near the edge of the reach; 0.3-0.6 m is easier.")
    if cube[2] > size / 2 + REST_TOLERANCE_M:
        warnings.append(f"cube_position z = {cube[2]:g} is above the ground (z = {size / 2:g} for this cube): the "
                        "cube drops when the simulation starts and may bounce away from the pick point.")
    if place[2] > size / 2 + REST_TOLERANCE_M:
        warnings.append(f"place_position z = {place[2]:g}: the gripper opens that high, so the cube drops "
                        f"{place[2] - size / 2:.3f} m. Use z = cube_size / 2 to set it down gently.")
    if math.hypot(cube[0] - place[0], cube[1] - place[1]) < size * 1.5:
        warnings.append("cube_position and place_position are almost the same spot: the robot will barely move.")
    return {
        "mode": mode,
        "robot_prim": robot_prim,
        "cube_prim": CUBE_PRIM,
        "cube_position": list(cube),
        "place_position": list(place),
        "cube_size": size,
    }, warnings


def _floats(values: Any) -> str:
    return "[" + ", ".join(repr(float(v)) for v in values) + "]"


def _settings_block(s: dict[str, Any]) -> str:
    local = (default_workspace_root() / LOCAL_FRANKA_RELATIVE).as_posix()
    return f'''# ---------------------------------------------------------------------------------------------
# Settings: everything in metres, Z is up, the Franka base sits at the origin (0, 0, 0).
# ---------------------------------------------------------------------------------------------
ROBOT_PRIM = {s["robot_prim"]!r}          # where the Franka goes in the stage
CUBE_PRIM = {s["cube_prim"]!r}            # where the cube goes
CUBE_SIZE = {float(s["cube_size"])!r}                   # cube edge length; the open gripper is 0.08 m wide
CUBE_POSITION = np.array({_floats(s["cube_position"])})    # cube centre (z = CUBE_SIZE / 2 means "on the ground")
PLACE_POSITION = np.array({_floats(s["place_position"])})  # where the cube centre should end up
# None = load franka.usd from the Isaac assets root (get_assets_root_path). If that is slow or fails, point it at
# your local copy, for example: FRANKA_USD = {local!r}
FRANKA_USD = None
'''


def _scene_block(indent: str) -> str:
    lines = '''# 1) A ground plane with a collider, so the cube has something to rest on.
world.scene.add_default_ground_plane()

# 2) The Franka: the Franka class references franka.usd (if ROBOT_PRIM is empty) and builds its ParallelGripper
#    from panda_finger_joint1 / panda_finger_joint2 (open 0.05 per finger, clamped by the 0.04 m joint limit).
franka = world.scene.add(Franka(prim_path=ROBOT_PRIM, name="franka", usd_path=FRANKA_USD))

# 3) The cube: DynamicCuboid = a Cube prim + rigid body + collider + mass (0.02 kg by default).
#    size=1.0 with scale=CUBE_SIZE gives a CUBE_SIZE metre cube, exactly like NVIDIA's franka_pick_up example.
cube = world.scene.add(
    DynamicCuboid(
        prim_path=CUBE_PRIM,
        name="cube",
        position=CUBE_POSITION,
        scale=np.array([CUBE_SIZE, CUBE_SIZE, CUBE_SIZE]),
        size=1.0,
        color=np.array([0.0, 0.0, 1.0]),
    )
)

# 4) Start with the gripper open (otherwise the fingers start closed and hit the cube).
franka.gripper.set_default_state(franka.gripper.joint_opened_positions)
'''
    return "".join(indent + line if line.strip() else line for line in lines.splitlines(keepends=True))


def _standalone_code(s: dict[str, Any]) -> str:
    return f'''"""Franka pick-and-place for Isaac Sim 6.1 (standalone).

Run it with Isaac Sim's own Python, not the system Python:
    & "<isaacsim-6.1.0>\\\\python.bat" {SCRIPT_FILENAME}
It opens Isaac Sim, builds ground + Franka + cube, and the arm picks the cube and places it at PLACE_POSITION.
Uses the classic API (isaacsim.core.api, Franka example classes): deprecated since 6.0 but shipped in 6.1.
"""

# ---------------------------------------------------------------------------------------------
# Step 0: start Isaac Sim FIRST. Every isaacsim / omni import below needs the running app.
# ---------------------------------------------------------------------------------------------
from isaacsim import SimulationApp

simulation_app = SimulationApp(
    {{
        "headless": False,  # True = no window (faster, for servers)
        # The Franka example classes live in a deprecated extension that is off by default in 6.1.
        "extra_args": ["--enable", "{EXAMPLES_EXTENSION}"],
    }}
)

# Now the Isaac Sim modules can be imported.
import numpy as np  # numbers and arrays
from isaacsim.core.api import World  # the simulation: stage, physics, scene registry
from isaacsim.core.api.objects import DynamicCuboid  # a cube that physics moves
from isaacsim.robot.manipulators.examples.franka import Franka  # Franka arm + parallel gripper
from isaacsim.robot.manipulators.examples.franka.controllers import PickPlaceController  # 10-phase state machine

{_settings_block(s)}
# ---------------------------------------------------------------------------------------------
# Step 1: build the scene.
# ---------------------------------------------------------------------------------------------
# World is the "simulation manager". stage_units_in_meters=1.0 says: 1 stage unit = 1 metre.
# It also creates a physics scene (/physicsScene) with gravity if the stage has none.
world = World(stage_units_in_meters=1.0)

{_scene_block("")}
# ---------------------------------------------------------------------------------------------
# Step 2: reset. This creates the physics views, so the robot can now read and move its joints.
# ---------------------------------------------------------------------------------------------
world.reset()

# The controller: RMPflow plans the arm motion, the gripper opens/closes, 10 phases in a row.
controller = PickPlaceController(name="pick_place_controller", gripper=franka.gripper, robot_articulation=franka)
# The articulation controller sends joint targets to the joint drives (motors).
articulation_controller = franka.get_articulation_controller()

# ---------------------------------------------------------------------------------------------
# Step 3: the simulation loop. One pass = one physics step (1/60 s) + one rendered frame.
# ---------------------------------------------------------------------------------------------
reset_needed = False
task_done = False
while simulation_app.is_running():  # until you close the window
    world.step(render=True)  # advance physics and draw the frame
    if world.is_stopped() and not reset_needed:
        reset_needed = True  # you pressed Stop: reset when Play is pressed again
    if world.is_playing():
        if reset_needed:
            world.reset()  # put robot and cube back to their start poses
            controller.reset()  # start again at phase 0
            reset_needed = False
            task_done = False
        cube_position, _cube_orientation = cube.get_world_pose()  # where the cube is right now
        actions = controller.forward(
            picking_position=cube_position,
            placing_position=PLACE_POSITION,
            current_joint_positions=franka.get_joint_positions(),
            end_effector_offset=np.array([0.0, 0.005, 0.0]),  # small sideways offset NVIDIA uses for the Franka
        )
        articulation_controller.apply_action(actions)  # move the joints toward the new targets
        if controller.is_done() and not task_done:
            print("done picking and placing")
            task_done = True

simulation_app.close()  # shut Isaac Sim down cleanly
'''


def _script_editor_code(s: dict[str, Any]) -> str:
    return f'''"""Franka pick-and-place for Isaac Sim 6.1 (Window > Script Editor).

Isaac Sim is already running, so this script must not block it with a while-loop. Instead it:
  1. builds the scene inside an async function (await lets Isaac Sim keep drawing between steps),
  2. registers a physics callback that runs once per physics step,
  3. presses Play.
Run it on a new, empty stage (File > New). Uses the classic API (deprecated since 6.0, shipped in 6.1).
"""

import numpy as np  # numbers and arrays
import omni.kit.app  # the running Kit application
from omni.kit.async_engine import run_coroutine  # runs an async function on Kit's event loop

# The Franka example classes live in a deprecated extension that is off by default in 6.1: switch it on.
omni.kit.app.get_app().get_extension_manager().set_extension_enabled_immediate("{EXAMPLES_EXTENSION}", True)

from isaacsim.core.api import World  # the simulation: stage, physics, scene registry
from isaacsim.core.api.objects import DynamicCuboid  # a cube that physics moves
from isaacsim.robot.manipulators.examples.franka import Franka  # Franka arm + parallel gripper
from isaacsim.robot.manipulators.examples.franka.controllers import PickPlaceController  # 10-phase state machine

{_settings_block(s)}

async def pick_and_place():
    # World is a singleton: clear the one from a previous run (and its callbacks) so we start fresh.
    World.clear_instance()
    world = World(stage_units_in_meters=1.0)
    # Inside the running app, World must be set up with await (the extension-workflow pattern).
    await world.initialize_simulation_context_async()

{_scene_block("    ")}
    # reset_async creates the physics views (it also runs one step), so the robot can read its joints.
    await world.reset_async()

    controller = PickPlaceController(name="pick_place_controller", gripper=franka.gripper, robot_articulation=franka)
    articulation_controller = franka.get_articulation_controller()
    state = {{"done": False}}

    def on_physics_step(step_size):
        # Called by PhysX before every physics step while the simulation plays.
        if not world.is_playing():
            return
        joint_positions = franka.get_joint_positions()
        if joint_positions is None:  # physics views not ready (e.g. after Stop): run the script again
            return
        cube_position, _cube_orientation = cube.get_world_pose()
        actions = controller.forward(
            picking_position=cube_position,
            placing_position=PLACE_POSITION,
            current_joint_positions=joint_positions,
            end_effector_offset=np.array([0.0, 0.005, 0.0]),  # small sideways offset NVIDIA uses for the Franka
        )
        articulation_controller.apply_action(actions)
        if controller.is_done() and not state["done"]:
            print("done picking and placing")
            state["done"] = True

    world.add_physics_callback("pick_place_step", on_physics_step)
    await world.play_async()  # press Play from code; the callback now runs every physics step


run_coroutine(pick_and_place())
'''


def _python_bat() -> str:
    return str(Path(default_workspace_root()) / ISAAC_FOLDER / "python.bat")


def _how_to_run(mode: str) -> list[str]:
    if mode == "standalone":
        bat = _python_bat()
        return [
            f"Save the code as {SCRIPT_FILENAME} in your project's scripts/ folder.",
            "Close any open Isaac Sim window first (standalone starts its own copy).",
            f'In PowerShell: & "{bat}" "<path to>\\{SCRIPT_FILENAME}"',
            "Wait for the window (the first start can take a few minutes while shaders compile).",
            "The simulation starts playing by itself after world.reset(); press Stop then Play to repeat.",
            "Close the window to end the script.",
        ]
    return [
        "Open Isaac Sim 6.1 and create an empty stage: File > New (save your work first).",
        "Open Window > Script Editor and paste the code.",
        "Press Run (Ctrl+Enter). The script builds the scene, then presses Play.",
        "Watch the console at the bottom of the Script Editor for 'done picking and placing'.",
        "To run again: press Stop, then run the script again (it clears the old World first).",
    ]


def _expected(s: dict[str, Any]) -> list[str]:
    cube = ", ".join(f"{v:g}" for v in s["cube_position"])
    place = ", ".join(f"{v:g}" for v in s["place_position"])
    return [
        f"A grey ground, the Franka at the origin and a blue {s['cube_size']:g} m cube at ({cube}).",
        "Phases 0-1: the hand moves above the cube and lowers around it (gripper open).",
        "Phases 2-3: a short wait, then the fingers close on the cube.",
        "Phase 4: the hand lifts the cube (about 0.3 m).",
        f"Phases 5-6: the hand carries the cube over ({place}) and lowers it.",
        "Phases 7-9: the fingers open, the hand lifts and moves back up.",
        "The console prints 'done picking and placing' once; the arm then holds still.",
        "Deprecation warnings about isaacsim.core.api / isaacsim.robot.manipulators in the console are expected.",
    ]


def _troubleshooting(mode: str) -> list[dict[str, str]]:
    first = (
        {"symptom": "ModuleNotFoundError: No module named 'isaacsim' (or 'omni')",
         "cause": "The script was run with the system Python, which knows nothing about Isaac Sim.",
         "fix": f'Run it with Isaac Sim\'s Python: & "{_python_bat()}" {SCRIPT_FILENAME}'}
        if mode == "standalone" else
        {"symptom": "Nothing happens / NameError after pasting only part of the script",
         "cause": "The Script Editor runs exactly what is in the box; a partial paste misses the imports or the "
                  "final run_coroutine(...) line.",
         "fix": "Paste the whole script and press Run (Ctrl+Enter) once."}
    )
    return [
        first,
        {"symptom": f"ModuleNotFoundError: No module named '{EXAMPLES_EXTENSION}'",
         "cause": "The Franka example extension is deprecated in 6.x and disabled by default.",
         "fix": "Keep the --enable line (standalone) or the set_extension_enabled_immediate line (Script Editor); "
                "or enable it in Window > Extensions."},
        {"symptom": "Could not find Isaac Sim assets folder / the Franka or the ground never appears",
         "cause": "get_assets_root_path() could not reach the assets (offline, or the local asset pack is not "
                  "configured).",
         "fix": "Set FRANKA_USD to your local franka.usd (the comment in the settings shows the path) and "
                "configure the local assets root in Isaac Sim (Edit > Preferences > Isaac Sim asset root)."},
        {"symptom": "get_joint_positions() returns None / 'physics simulation view' errors",
         "cause": "The robot was used before world.reset() (await world.reset_async()) created the physics views, "
                  "or after Stop invalidated them.",
         "fix": "Always reset the World after adding objects and before using the robot; after Stop, press Play "
                "(standalone resets for you) or run the Script Editor script again."},
        {"symptom": "The arm reaches but never grabs, or knocks the cube over",
         "cause": "The cube is wider than the open gripper (0.08 m), too far away (> 0.855 m) or not resting where "
                  "you think.",
         "fix": "Keep cube_size <= 0.07 m and positions 0.3-0.6 m from the base; run the pick-and-place readiness "
                "check on the stage."},
        {"symptom": "The cube falls through the ground or flies away",
         "cause": "No colliding ground, the cube starts inside the ground (z < size/2), or a very light/heavy mass.",
         "fix": "Use add_default_ground_plane(), put the cube centre at z = cube_size / 2, keep mass 0.01-1 kg."},
        {"symptom": "'World is already initialized' warning, or the robot moves twice as fast",
         "cause": "A World (and its physics callback) from an earlier run is still alive.",
         "fix": "World.clear_instance() at the start (the Script Editor version does this), or File > New."},
        {"symptom": "The robot falls over, sags or does not move",
         "cause": "The robot prim is not the original franka.usd: no fixed root joint, missing joint drives, or a "
                  "different prim at ROBOT_PRIM.",
         "fix": "Start from an empty stage so the script references franka.usd itself; check with robot_inspect "
                "(fixed base, drives on every joint)."},
    ]
