"""Robot import helper: bring a robot into a scene from a URDF file, an MJCF file, or Isaac Sim's ready-made robots.

The beginner problem this solves: there are three ways to get a robot into Isaac Sim 6.1 and each has traps.

* Ready-made robot (Franka, UR10, Jetbot, Carter...): already a tuned USD in the Isaac asset pack. Just REFERENCE it
  (add_ready_robot) - nothing to convert, drives and colliders are already set up.
* URDF (ROS robot description): run Isaac's URDF importer. Traps: mesh files that do not resolve (package:// URIs),
  joints without limits, links without mass, the importer writing next to the URDF by default, and the base being
  free (falls over) or fixed (cannot drive) by mistake.
* MJCF (MuJoCo XML): run Isaac's MJCF importer. Its drives come from the MJCF <actuator> elements.

API (all return JSON-safe dicts with ok):
  importer_info()                      the importer APIs verified (read-only) in this workspace's Isaac Sim 6.1.0
  list_ready_robots(kind=None)         ready robot USDs found in isaacsim-assets-6.1.0, with plain descriptions
  parse_urdf_summary(path)             pure Python: links, joints, meshes (do they resolve?), materials, warnings
  plan_robot_import(source, ...)       dry run: method, importer config, output path, steps, Script Editor code
  import_robot(source, ..., dry_run=True)  Kit-only: runs the URDF/MJCF importer into <project>/assets/robots/,
                                       then robot_inspector.inspect_robot on the result
  add_ready_robot(stage, name, prim_path, position=, dry_run=True)
                                       pure pxr: reference a ready robot (or any robot USD in the workspace)

Safety: everything is a dry run unless dry_run is exactly False. URDF/MJCF files are parsed with xml.etree after
REFUSING any <!DOCTYPE>/<!ENTITY> declaration (entity-expansion attacks; defusedxml is not installed). Source files
and the asset pack are never modified. import_robot writes only inside <project>/assets/robots/ (the importer itself
picks <name>_1, <name>_2... instead of overwriting). add_ready_robot refuses an existing prim and never saves a layer.
Nothing touches the network.

Importer facts below were read (read-only) from the installed extension sources on 2026-10-22.
"""

from __future__ import annotations

import codecs
import importlib
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdGeom
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

__all__ = [
    "READY_ROBOTS",
    "RobotImportError",
    "add_ready_robot",
    "import_robot",
    "importer_info",
    "list_ready_robots",
    "parse_urdf_summary",
    "plan_robot_import",
]

VERIFIED_ON = "2026-10-22"
ROBOTS_SUBDIR = "assets/robots"
MAX_XML_BYTES = 20 * 1024 * 1024
MAX_MESHES = 5000
BIG_MESH_BYTES = 50 * 1024 * 1024
HUGE_TOTAL_MESH_BYTES = 300 * 1024 * 1024
SUPPORTED_MESH_FORMATS = (".stl", ".obj", ".dae")
USD_EXTENSIONS = (".usd", ".usda", ".usdc", ".usdz")
_NOT_KIT = "Kit not available"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PRIM_PATH = re.compile(r"^(/[A-Za-z_][A-Za-z0-9_]*)+$")
_FORBIDDEN_XML = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]+)://")

# ------------------------------------------------------------------------------------------ verified importer data

_EXTS = "isaacsim-6.1.0/exts"
_UTILS = f"{_EXTS}/isaacsim.asset.importer.utils/pip_prebundle/isaacsim/asset/importer/utils/impl"

COLLISION_TYPES = ("Convex Hull", "Convex Decomposition", "Bounding Sphere", "Bounding Cube")
ROBOT_TYPES = ("Default", "End Effector", "Manipulator", "Humanoid", "Wheeled", "Holonomic", "Quadruped",
               "Mobile Manipulators", "Aerial")

# Every field of URDFImporterConfig (impl/config.py) with its default, in source order.
URDF_CONFIG_DEFAULTS: dict[str, Any] = {
    "urdf_path": None, "usd_path": None, "merge_fixed_joints": False, "merge_mesh": False, "debug_mode": False,
    "collision_from_visuals": False, "collision_type": "Convex Hull", "allow_self_collision": False,
    "ros_package_paths": [], "robot_type": "Default", "fix_base": None, "link_density": None,
    "joint_drive_type": None, "joint_target_type": None, "override_joint_stiffness": None,
    "override_joint_damping": None, "run_asset_transformer": True, "run_multi_physics_conversion": True,
}
# Every field of MJCFImporterConfig (impl/config.py) with its default, in source order.
MJCF_CONFIG_DEFAULTS: dict[str, Any] = {
    "mjcf_path": None, "usd_path": None, "import_scene": True, "merge_mesh": False, "debug_mode": False,
    "collision_from_visuals": False, "collision_type": "Convex Hull", "allow_self_collision": False,
    "robot_type": "Default", "fix_base": None, "link_density": None, "joint_drive_type": None,
    "joint_target_type": None, "override_gain_type": None, "override_bias_type": None, "override_gain_prm": None,
    "override_bias_prm": None, "run_asset_transformer": True, "run_multi_physics_conversion": True,
}

FIELD_HELP: dict[str, str] = {
    "urdf_path": "the robot description file to read (never modified)",
    "mjcf_path": "the MuJoCo XML file to read (never modified)",
    "usd_path": "the FOLDER for the result; the importer adds <robot_name>/<robot_name>.usda inside it "
                "(None would write next to the source file)",
    "fix_base": "True = bolted to the world like an arm on a table; False = free base that can drive or fall "
                "(mobile robots, legged robots); None = keep what the file says",
    "merge_fixed_joints": "True merges links joined by fixed joints into one link (fewer bodies, faster)",
    "merge_mesh": "True merges the meshes inside each link (faster rendering)",
    "debug_mode": "True keeps intermediate files in <usd_path>/_debug_<name>",
    "collision_from_visuals": "True builds colliders from the VISUAL meshes and removes the file's own colliders",
    "collision_type": "shape approximation used with collision_from_visuals: " + ", ".join(COLLISION_TYPES),
    "allow_self_collision": "True lets the robot's links hit each other (usually False: neighbours overlap)",
    "ros_package_paths": "where package://<name>/ mesh URIs live: [{'name': pkg, 'path': folder}]",
    "robot_type": "Isaac robot schema category: " + ", ".join(ROBOT_TYPES),
    "link_density": "kg/m^3 for links without an explicit mass (None = leave)",
    "joint_drive_type": "'force' or 'acceleration' drive (None = leave the converter's choice)",
    "joint_target_type": "'position' (hold an angle), 'velocity' (spin at a speed: wheels) or 'none'",
    "override_joint_stiffness": "drive stiffness in Nm/rad (hinges) or N/m (sliders); None = keep the file's",
    "override_joint_damping": "drive damping in Nm*s/rad (hinges) or N*s/m (sliders); None = keep the file's",
    "run_asset_transformer": "True restructures the output into Isaac's layered asset layout (payloads/...)",
    "run_multi_physics_conversion": "True converts joint attributes so PhysX and MuJoCo/Newton both work",
    "import_scene": "True also imports the MJCF <option> simulation settings",
    "override_gain_type": "MuJoCo actuator gain type, 'fixed' for position/velocity control",
    "override_bias_type": "MuJoCo actuator bias type, 'affine' for position/velocity control",
    "override_gain_prm": "10 floats: [kp, 0, ...] (position) or [kd, 0, ...] (velocity)",
    "override_bias_prm": "10 floats: [0, -kp, -kd, 0, ...] (position) or [0, 0, -kd, 0, ...] (velocity)",
}

IMPORTERS: dict[str, dict[str, Any]] = {
    "urdf": {
        "extension": "isaacsim.asset.importer.urdf",
        "version": "3.11.10",
        "converter": "urdf_usd_converter 0.3.2 (pip_prebundle)",
        "module": "isaacsim.asset.importer.urdf",
        "api": ("URDFImporter(config: URDFImporterConfig | None).import_urdf(config=None) -> str: path of the "
                "new <usd_path>/<robot_name>/<robot_name>.usda (robot_name = URDF file name without .urdf; an "
                "existing non-empty folder gets _1, _2... instead of being overwritten). Synchronous. It does NOT "
                "add anything to the open stage."),
        "config_class": "URDFImporterConfig (dataclass)",
        "config_fields": dict(URDF_CONFIG_DEFAULTS),
        "deprecated": [
            "omni.kit.commands 'URDFCreateImportConfig' / 'URDFImportRobot' (isaacsim.asset.importer.urdf.ui "
            "1.4.6 command.py) are deprecated wrappers; 'URDFImportRobot' just calls URDFImporter.import_urdf.",
            "'URDFParseFile' and 'URDFParseText' now raise RuntimeError ('no longer supported').",
            "_urdf.acquire_urdf_interface() / ImportConfig() are deprecated shims returning URDFImporter / "
            "URDFImporterConfig (impl/_urdf.py).",
        ],
        "not_in_6_1": ["import_inertia_tensor", "distance_scale", "make_default_prim", "create_physics_scene",
                       "default_drive_strength", "default_position_drive_damping", "convex_decomp", "dest_path"],
        "facts": [
            "ros_package_paths entries are {'name': <package>, 'path': <folder>}; without one, package:// meshes "
            "are searched by walking up from the URDF folder (urdf_utils._find_package_root, "
            "urdf_usd_converter ros_package.search_ros_packages).",
            "URDF collision meshes become convexHull colliders (urdf_usd_converter geometry.py); collision_type "
            "only applies when collision_from_visuals=True.",
            "Mesh formats read by the converter: .stl, .obj, .dae (urdf_usd_converter mesh.py).",
            "Stiffness/damping overrides are given per radian and converted to USD's per-degree values "
            "(asset_utils._set_stiffness_on_joints).",
            "A dict value for joint_* / override_* maps joint-name regex patterns to values; a pattern that "
            "matches no joint raises ValueError (asset_utils._apply_to_joints).",
            "joint_target_type 'velocity' zeroes stiffness; 'none'/'effort' zero stiffness and damping.",
            "fix_base=True adds a fixed joint to the world (asset_utils.apply_fix_base); False removes it.",
            "No physics scene is created: add Create > Physics > Physics Scene to your stage yourself.",
        ],
        "sources": [
            f"{_EXTS}/isaacsim.asset.importer.urdf/config/extension.toml",
            f"{_EXTS}/isaacsim.asset.importer.urdf/config/python_api.md",
            f"{_EXTS}/isaacsim.asset.importer.urdf/isaacsim/asset/importer/urdf/impl/config.py",
            f"{_EXTS}/isaacsim.asset.importer.urdf/isaacsim/asset/importer/urdf/impl/converter.py",
            f"{_EXTS}/isaacsim.asset.importer.urdf/isaacsim/asset/importer/urdf/impl/_urdf.py",
            f"{_EXTS}/isaacsim.asset.importer.urdf/isaacsim/asset/importer/urdf/impl/urdf_utils.py",
            f"{_EXTS}/isaacsim.asset.importer.urdf.ui/isaacsim/asset/importer/urdf/ui/impl/command.py",
            f"{_EXTS}/isaacsim.asset.importer.urdf/pip_prebundle/urdf_usd_converter/_impl/ros_package.py",
            f"{_UTILS}/importer_utils.py",
            f"{_UTILS}/asset_utils.py",
        ],
        "enabled_in": "isaacsim.exp.full.kit / isaacsim.exp.base.kit (via isaacsim.asset.importer.urdf.ui)",
    },
    "mjcf": {
        "extension": "isaacsim.asset.importer.mjcf",
        "version": "3.12.1",
        "converter": "mujoco_usd_converter 0.5.0 (isaacsim.pip.newton pip_prebundle)",
        "module": "isaacsim.asset.importer.mjcf",
        "api": ("MJCFImporter(config: MJCFImporterConfig | None).import_mjcf(config=None) -> str: path of the new "
                "<usd_path>/<robot_name>/<robot_name>.usda (robot_name = file name without .xml). Synchronous; "
                "raises FileNotFoundError for a missing file. It does NOT add anything to the open stage."),
        "config_class": "MJCFImporterConfig (dataclass)",
        "config_fields": dict(MJCF_CONFIG_DEFAULTS),
        "deprecated": [],
        "not_in_6_1": ["import_inertia_tensor", "distance_scale", "make_default_prim", "create_physics_scene"],
        "facts": [
            "MJCFImporterConfig has joint_drive_type / joint_target_type fields, but import_mjcf in 3.12.1 never "
            "reads them: drives come from the MJCF <actuator> elements, changed only via override_gain_*/bias_*.",
            "Position control: gain_type 'fixed', bias_type 'affine', gain_prm [kp,0..], bias_prm [0,-kp,-kd,0..]; "
            "velocity control: gain_prm [kd,0..], bias_prm [0,0,-kd,0..] (asset_utils.apply_mjc_actuator_gains).",
            "The source must end in .xml (importer_utils.parse_robot_name).",
        ],
        "sources": [
            f"{_EXTS}/isaacsim.asset.importer.mjcf/config/extension.toml",
            f"{_EXTS}/isaacsim.asset.importer.mjcf/config/python_api.md",
            f"{_EXTS}/isaacsim.asset.importer.mjcf/isaacsim/asset/importer/mjcf/impl/config.py",
            f"{_EXTS}/isaacsim.asset.importer.mjcf/isaacsim/asset/importer/mjcf/impl/converter.py",
            f"{_UTILS}/asset_utils.py",
        ],
        "enabled_in": "isaacsim.exp.full.kit / isaacsim.exp.base.kit (via isaacsim.asset.importer.mjcf.ui 1.4.7)",
    },
}

# Ready-made robots verified in isaacsim-assets-6.1.0/Assets/Isaac/6.1 (path relative to that content root).
# All of them have a defaultPrim, upAxis Z and metersPerUnit 1.0 (headers read 2026-10-22).
READY_ROBOTS: dict[str, dict[str, Any]] = {
    "franka": {"prim_name": "Franka", "kind": "arm", "path": "Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd",
               "aliases": ["panda", "franka_panda"], "default_prim": "panda",
               "description": "Franka Emika Panda: 7-joint research arm with a 2-finger gripper; the classic "
                              "pick-and-place teaching robot."},
    "fr3": {"prim_name": "FR3", "kind": "arm", "path": "Isaac/Robots/FrankaRobotics/FrankaFR3/fr3.usd",
            "aliases": ["franka_fr3"], "default_prim": "fr3",
            "description": "Franka Research 3: the newer 7-joint Franka arm."},
    "ur10": {"prim_name": "UR10", "kind": "arm", "path": "Isaac/Robots/UniversalRobots/ur10/ur10.usd",
             "aliases": [], "default_prim": "ur10",
             "description": "Universal Robots UR10: 6-joint industrial arm with a long reach (1.3 m), no gripper."},
    "ur10e": {"prim_name": "UR10e", "kind": "arm", "path": "Isaac/Robots/UniversalRobots/ur10e/ur10e.usd",
              "aliases": [], "default_prim": "ur10e",
              "description": "Universal Robots UR10e: e-Series version of the UR10, 6 joints."},
    "ur5e": {"prim_name": "UR5e", "kind": "arm", "path": "Isaac/Robots/UniversalRobots/ur5e/ur5e.usd",
             "aliases": ["ur5"], "default_prim": "ur5e",
             "description": "Universal Robots UR5e: smaller 6-joint arm (0.85 m reach)."},
    "kinova_gen3": {"prim_name": "KinovaGen3", "kind": "arm", "path": "Isaac/Robots/Kinova/Gen3/gen3n7_instanceable.usd",
                    "aliases": ["gen3", "kinova"], "default_prim": "gen3",
                    "description": "Kinova Gen3: light 7-joint arm."},
    "kuka_iiwa": {"prim_name": "KukaIiwa", "kind": "arm", "path": "Isaac/Robots/Kuka/LBR_iiwa/kuka.usda",
                  "aliases": ["iiwa", "kuka"], "default_prim": "Root",
                  "description": "KUKA LBR iiwa: 7-joint collaborative arm."},
    "xarm6": {"prim_name": "XArm6", "kind": "arm", "path": "Isaac/Robots/Ufactory/xarm6/xarm6.usd",
              "aliases": [], "default_prim": "UF_ROBOT", "description": "UFactory xArm 6: 6-joint arm."},
    "robotiq_2f_85": {"prim_name": "Robotiq2F85", "kind": "gripper",
                      "path": "Isaac/Robots/Robotiq/2F-85/Robotiq_2F_85_edit.usd", "aliases": ["2f85", "robotiq"],
                      "default_prim": "Robotiq_2F_85",
                      "description": "Robotiq 2F-85: 2-finger parallel gripper (85 mm stroke) to mount on an arm."},
    "robotiq_2f_140": {"prim_name": "Robotiq2F140", "kind": "gripper",
                       "path": "Isaac/Robots/Robotiq/2F-140/2f140_instanceable.usd", "aliases": ["2f140"],
                       "default_prim": "robotiq_arg2f_140_model",
                       "description": "Robotiq 2F-140: wider 2-finger parallel gripper (140 mm stroke)."},
    "jetbot": {"prim_name": "Jetbot", "kind": "mobile", "path": "Isaac/Robots/NVIDIA/Jetbot/jetbot.usd",
               "aliases": [], "default_prim": "jetbot",
               "description": "NVIDIA JetBot: tiny 2-wheel differential-drive robot; the simplest robot to drive."},
    "carter_v1": {"prim_name": "Carter", "kind": "mobile", "path": "Isaac/Robots/NVIDIA/Carter/carter_v1.usd",
                  "aliases": ["carter"], "default_prim": "carter",
                  "description": "NVIDIA Carter v1: 2-wheel differential-drive delivery robot with a caster."},
    "nova_carter": {"prim_name": "NovaCarter", "kind": "mobile", "path": "Isaac/Robots/NVIDIA/NovaCarter/nova_carter.usd",
                    "aliases": [], "default_prim": "nova_carter",
                    "description": "NVIDIA Nova Carter: warehouse AMR reference robot with sensors."},
    "kaya": {"prim_name": "Kaya", "kind": "mobile", "path": "Isaac/Robots/NVIDIA/Kaya/kaya.usd", "aliases": [],
             "default_prim": "kaya",
             "description": "NVIDIA Kaya: 3 omni-wheel (holonomic) robot that can drive sideways."},
    "turtlebot3": {"prim_name": "Turtlebot3", "kind": "mobile",
                   "path": "Isaac/Robots/Turtlebot/Turtlebot3/tb3_burger_processed.usda",
                   "aliases": ["turtlebot", "tb3"], "default_prim": "turtlebot3_burger",
                   "description": "TurtleBot3 Burger: small ROS teaching robot, 2-wheel differential drive."},
    "create3": {"prim_name": "Create3", "kind": "mobile", "path": "Isaac/Robots/iRobot/Create3/create_3.usd",
                "aliases": ["irobot"], "default_prim": "create3",
                "description": "iRobot Create 3: round 2-wheel educational robot (Roomba base)."},
    "jackal": {"prim_name": "Jackal", "kind": "mobile", "path": "Isaac/Robots/Clearpath/Jackal/jackal.usd",
               "aliases": [], "default_prim": "jackal",
               "description": "Clearpath Jackal: 4-wheel skid-steer outdoor robot."},
    "iw_hub": {"prim_name": "IwHub", "kind": "mobile", "path": "Isaac/Robots/Idealworks/iwhub/iw_hub.usd",
               "aliases": ["iwhub"], "default_prim": "iw_hub",
               "description": "idealworks iw.hub: factory tugger / cart-lifting AMR."},
    "leatherback": {"prim_name": "Leatherback", "kind": "mobile", "path": "Isaac/Robots/NVIDIA/Leatherback/leatherback.usd",
                    "aliases": [], "default_prim": "Leatherback",
                    "description": "NVIDIA Leatherback: small car-like (Ackermann steering) robot."},
    "ridgeback_franka": {"prim_name": "RidgebackFranka", "kind": "mobile manipulator",
                         "path": "Isaac/Robots/Clearpath/RidgebackFranka/ridgeback_franka.usd", "aliases": [],
                         "default_prim": None,
                         "description": "Clearpath Ridgeback base carrying a Franka arm."},
    "spot": {"prim_name": "Spot", "kind": "legged", "path": "Isaac/Robots/BostonDynamics/spot/spot.usd",
             "aliases": [], "default_prim": "spot", "description": "Boston Dynamics Spot: 4-legged robot dog."},
    "go2": {"prim_name": "Go2", "kind": "legged", "path": "Isaac/Robots/Unitree/Go2/go2.usd", "aliases": [],
            "default_prim": "go2_description", "description": "Unitree Go2: small 4-legged robot dog."},
    "h1": {"prim_name": "H1", "kind": "humanoid", "path": "Isaac/Robots/Unitree/H1/h1.usd", "aliases": [],
           "default_prim": "h1", "description": "Unitree H1: full-size humanoid."},
    "cartpole": {"prim_name": "Cartpole", "kind": "teaching", "path": "Isaac/Robots/IsaacSim/Cartpole/cartpole.usd",
                 "aliases": [], "default_prim": "cartpole",
                 "description": "Cart-pole: a cart on a rail with a free pole, the classic balancing exercise."},
    "simple_articulation": {"prim_name": "SimpleArticulation", "kind": "teaching",
                            "path": "Isaac/Robots/IsaacSim/SimpleArticulation/simple_articulation.usd",
                            "aliases": [], "default_prim": "Articulation",
                            "description": "The smallest articulation: a few links and hinges for learning joints."},
    "differential_base": {"prim_name": "DifferentialBase", "kind": "teaching",
                          "path": "Isaac/Robots/IsaacSim/DifferentialBase/differential_base.usd", "aliases": [],
                          "default_prim": "differential_base",
                          "description": "A bare 2-wheel differential-drive base for learning wheel drives."},
}

KIND_WORDS = {
    "arm": "robot arm (manipulator): bolted down, moves a tool with hinge joints",
    "gripper": "gripper (end effector): fingers you mount on the end of an arm",
    "mobile": "mobile robot: drives around on wheels, free base",
    "mobile manipulator": "mobile robot carrying an arm",
    "legged": "legged robot: walks, free base, needs a walking controller",
    "humanoid": "humanoid: two legs and arms, free base, needs a balance controller",
    "teaching": "small teaching model for learning joints and drives",
}


class RobotImportError(ValueError):
    """Invalid input; public functions turn it into {ok: False, error}."""


# ------------------------------------------------------------------------------------------------ small helpers


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _posix(path: Any) -> str:
    return os.fspath(path).replace("\\", "/")


def _is_within(path: Any, parent: Any) -> bool:
    child, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(parent))
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:
        return False


def _kit_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - ImportError, or a Kit extension that fails while loading
        return None


def _prim_name(text: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_") or "Robot"
    if name[0].isdigit():
        name = f"Robot_{name}"
    return name[:64]


def _check_prim_path(prim_path: Any) -> str:
    if not isinstance(prim_path, str) or not _PRIM_PATH.match(prim_path.strip()):
        raise RobotImportError(f"prim_path must be an absolute prim path such as '/World/Franka', not {prim_path!r} "
                               "(letters, digits and '_' only; each name must not start with a digit).")
    return prim_path.strip()


def _vec3(value: Any, label: str) -> list[float]:
    try:
        items = [float(v) for v in value]
    except (TypeError, ValueError) as exc:
        raise RobotImportError(f"{label} must be three numbers (x, y, z), not {value!r}.") from exc
    if len(items) != 3 or not all(math.isfinite(v) for v in items):
        raise RobotImportError(f"{label} must be three finite numbers (x, y, z), not {value!r}.")
    return items


def _gain(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise RobotImportError(f"{label} must be a number, not {value!r}.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RobotImportError(f"{label} must be a number, not {value!r}.") from exc
    if not math.isfinite(number) or number < 0:
        raise RobotImportError(f"{label} must be zero or a positive finite number, not {value!r}.")
    return number


def _floats(text: Any, count: int) -> list[float] | None:
    try:
        values = [float(part) for part in str(text).split()]
    except ValueError:
        return None
    return values if len(values) == count and all(math.isfinite(v) for v in values) else None


def _attr_float(element: Any, name: str) -> float | None:
    if element is None or element.get(name) is None:
        return None
    try:
        value = float(element.get(name))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


# --------------------------------------------------------------------------------------------- safe XML reading


def _read_xml(path: str, label: str) -> Any:
    """Parse an XML file with xml.etree after refusing DOCTYPE/ENTITY declarations (no defusedxml here)."""
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise RobotImportError(f"Cannot read {label} {path}: {exc}") from exc
    if size > MAX_XML_BYTES:
        raise RobotImportError(f"{label} {path} is {size // (1024 * 1024)} MB; refusing XML over "
                               f"{MAX_XML_BYTES // (1024 * 1024)} MB.")
    with open(path, "rb") as handle:
        raw = handle.read()
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise RobotImportError(f"{label} {path} is UTF-32 encoded; save it as UTF-8.")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
    if "\x00" in text:
        raise RobotImportError(f"{label} {path} contains NUL bytes; it is not a text XML file.")
    if _FORBIDDEN_XML.search(text):
        raise RobotImportError(
            f"Refused {label} {path}: it contains a <!DOCTYPE> or <!ENTITY> declaration. Robot description files do "
            "not need them, and entity declarations can be used for 'billion laughs' / external-entity attacks. "
            "Remove the declaration if you trust the file.")
    text = _XML_DECL.sub("", text, count=1)
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise RobotImportError(f"{label} {path} is not well-formed XML: {exc}") from exc


# ------------------------------------------------------------------------------------------- mesh resolution


def _package_map(ros_package_paths: Any) -> dict[str, str]:
    if ros_package_paths is None:
        return {}
    if isinstance(ros_package_paths, dict):
        items = [{"name": k, "path": v} for k, v in ros_package_paths.items()]
    elif isinstance(ros_package_paths, (list, tuple)):
        items = list(ros_package_paths)
    else:
        raise RobotImportError("ros_package_paths must be [{'name': package, 'path': folder}, ...] or "
                               "{package: folder}.")
    mapping: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not item.get("name") or not item.get("path"):
            raise RobotImportError(f"Each ros_package_paths entry needs 'name' and 'path', got {item!r}.")
        mapping[str(item["name"])] = os.path.abspath(os.fspath(item["path"]))
    return mapping


def _join_inside(root: str, relative: str) -> str | None:
    """root/relative, or None when relative escapes root (same rule as Isaac's _join_package_path)."""
    base = os.path.abspath(root)
    joined = os.path.abspath(os.path.join(base, relative))
    try:
        return joined if os.path.commonpath([base, joined]) == base else None
    except ValueError:
        return None


def _resolve_mesh(filename: str, source_dir: str, packages: dict[str, str]) -> dict[str, Any]:
    info: dict[str, Any] = {"filename": filename, "how": "relative", "package": None, "resolved_path": None,
                            "exists": False, "size_bytes": None}
    scheme = _SCHEME.match(filename)
    if filename.startswith("package://"):
        info["how"] = "package"
        package, _, relative = filename[len("package://"):].partition("/")
        info["package"] = package or None
        if not package or not relative:
            info["problem"] = "invalid package:// URI (needs package://<package>/<path>)"
            return info
        if package in packages:
            info["resolved_path"] = _join_inside(packages[package], relative)
            info["package_found_by"] = "ros_package_paths"
        else:
            current = os.path.abspath(source_dir)
            while True:  # walk up from the URDF folder, like the converter's search_ros_packages
                candidate = _join_inside(current, relative)
                if candidate and os.path.isfile(candidate):
                    info["resolved_path"] = candidate
                    info["package_found_by"] = "searching up from the URDF folder"
                    break
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
            if info["resolved_path"] is None:
                info["resolved_path"] = os.path.abspath(os.path.join(source_dir, relative))
    elif filename.startswith("file://"):
        info["how"] = "file_uri"
        rest = filename[len("file:///"):] if filename.startswith("file:///") else filename[len("file://"):]
        if filename.startswith("file:///") and not re.match(r"^[A-Za-z]:", rest):
            rest = "/" + rest
        info["resolved_path"] = os.path.abspath(rest if os.path.isabs(rest) else os.path.join(source_dir, rest))
    elif scheme:
        info["how"] = "unsupported_uri"
        info["problem"] = f"'{scheme.group(1)}://' is not supported by the importer (only package:// and file://)"
        return info
    elif os.path.isabs(filename):
        info["how"] = "absolute"
        info["resolved_path"] = os.path.abspath(filename)
    else:
        info["resolved_path"] = os.path.abspath(os.path.join(source_dir, filename))
    path = info["resolved_path"]
    if path and os.path.isfile(path):
        info["exists"] = True
        try:
            info["size_bytes"] = os.path.getsize(path)
        except OSError:
            pass
    if path:
        info["resolved_path"] = _posix(path)
    return info


def _mesh_warnings(meshes: list[dict[str, Any]], packages: dict[str, str], warnings: list[str]) -> None:
    missing_packages: set[str] = set()
    found_by_search: set[str] = set()
    total = 0
    for mesh in meshes:
        owner = f"link '{mesh['link']}' ({mesh['role']})"
        if mesh.get("problem"):
            warnings.append(f"Mesh {mesh['filename']} of {owner}: {mesh['problem']}.")
            continue
        if not mesh["exists"]:
            if mesh["how"] == "package" and mesh.get("package") not in packages:
                missing_packages.add(mesh.get("package") or "?")
            warnings.append(f"Mesh {mesh['filename']} of {owner} was not found (looked at {mesh['resolved_path']}); "
                            "the importer will create that link without this shape.")
            continue
        if mesh["how"] == "package" and mesh.get("package_found_by") != "ros_package_paths":
            found_by_search.add(mesh.get("package") or "?")
        ext = os.path.splitext(mesh["resolved_path"] or "")[1].lower()
        if ext not in SUPPORTED_MESH_FORMATS:
            warnings.append(f"Mesh {mesh['filename']} of {owner} is '{ext or 'no extension'}'; Isaac 6.1's URDF "
                            f"converter reads only {', '.join(SUPPORTED_MESH_FORMATS)}.")
        size = mesh.get("size_bytes") or 0
        total += size
        if size > BIG_MESH_BYTES:
            warnings.append(f"Mesh {mesh['filename']} is {size / 1048576:.0f} MB: huge meshes import slowly and make "
                            "slow convex-hull colliders; decimate it (e.g. in Blender) for collisions.")
    if total > HUGE_TOTAL_MESH_BYTES:
        warnings.append(f"The meshes add up to {total / 1048576:.0f} MB; expect a slow import and heavy scene.")
    for package in sorted(missing_packages):
        warnings.append(f"package://{package}/ meshes did not resolve: pass ros_package_paths=[{{'name': "
                        f"'{package}', 'path': '<folder of the {package} package>'}}].")
    for package in sorted(found_by_search):
        warnings.append(f"package://{package}/ was found by searching up from the URDF folder; the importer does the "
                        "same search, but passing ros_package_paths makes it explicit.")


# ------------------------------------------------------------------------------------------------- URDF summary

_JOINT_PLAIN = {
    "revolute": "hinge with limits (turns between a lowest and highest angle)",
    "continuous": "hinge without limits (spins forever, like a wheel)",
    "prismatic": "slider (moves in a straight line)",
    "fixed": "fixed (welds the two links together)",
    "floating": "free (6 directions, rarely used in URDF)",
    "planar": "planar (slides in a plane)",
}
_LEG_WORDS = re.compile(r"hip|knee|thigh|calf|ankle|shin|foot", re.IGNORECASE)
_ARM_UPPER_WORDS = re.compile(r"shoulder|elbow|wrist|torso", re.IGNORECASE)
_FINGER_WORDS = re.compile(r"finger|gripper|jaw|knuckle|claw|pad", re.IGNORECASE)
_WHEEL_WORDS = re.compile(r"wheel|caster|castor", re.IGNORECASE)


def parse_urdf_summary(path: Any, *, ros_package_paths: Any = None) -> dict[str, Any]:
    """Pure Python, read-only: what a URDF contains and what will go wrong when importing it.

    Returns {ok, path, robot_name, root_link, links, joints, meshes, materials, ignored_tags, fixed_to_world,
    wheel_joints, looks_like, warnings}. Files with <!DOCTYPE>/<!ENTITY> are refused.
    """
    try:
        return _urdf_summary(path, ros_package_paths)
    except RobotImportError as exc:
        return _error(str(exc), path=None if path is None else _posix(path))


def _urdf_summary(path: Any, ros_package_paths: Any) -> dict[str, Any]:
    if path is None or not str(path).strip():
        raise RobotImportError("path is required: the .urdf file to read.")
    source = os.path.abspath(os.fspath(path))
    if not os.path.isfile(source):
        raise RobotImportError(f"The URDF file does not exist: {_posix(source)}")
    packages = _package_map(ros_package_paths)
    root = _read_xml(source, "URDF")
    if root.tag != "robot":
        raise RobotImportError(f"{_posix(source)} is not a URDF: its top element is <{root.tag}>, not <robot>.")
    source_dir = os.path.dirname(source)
    warnings: list[str] = []
    materials = []
    for material in root.iter("material"):
        name = material.get("name")
        color = material.find("color")
        texture = material.find("texture")
        if color is None and texture is None and any(m["name"] == name for m in materials):
            continue  # a by-name use of a material defined elsewhere
        materials.append({"name": name, "color": _floats(color.get("rgba"), 4) if color is not None else None,
                          "texture": texture.get("filename") if texture is not None else None})

    links: list[dict[str, Any]] = []
    meshes: list[dict[str, Any]] = []
    names: set[str] = set()
    for link in root.findall("link"):
        name = link.get("name") or ""
        if not name:
            warnings.append("A <link> has no name; the importer will reject or rename it.")
        elif name in names:
            warnings.append(f"Link name '{name}' is used twice; link names must be unique.")
        names.add(name)
        inertial = link.find("inertial")
        mass = _attr_float(inertial.find("mass") if inertial is not None else None, "value")
        inertia = inertial.find("inertia") if inertial is not None else None
        record = {"name": name, "visuals": len(link.findall("visual")), "collisions": len(link.findall("collision")),
                  "mass": mass, "has_inertia": inertia is not None, "meshes": []}
        for role in ("visual", "collision"):
            for block in link.findall(role):
                for mesh in block.iter("mesh"):
                    if len(meshes) >= MAX_MESHES:
                        break
                    filename = (mesh.get("filename") or "").strip()
                    if not filename:
                        warnings.append(f"A <mesh> in link '{name}' has no filename.")
                        continue
                    entry = {"link": name, "role": role, **_resolve_mesh(filename, source_dir, packages),
                             "scale": _floats(mesh.get("scale"), 3) if mesh.get("scale") else None}
                    meshes.append(entry)
                    record["meshes"].append(filename)
        links.append(record)

    joints: list[dict[str, Any]] = []
    children: set[str] = set()
    parents: set[str] = set()
    joint_names: set[str] = set()
    for joint in root.findall("joint"):
        name = joint.get("name") or ""
        kind = (joint.get("type") or "").strip()
        if name in joint_names:
            warnings.append(f"Joint name '{name}' is used twice; joint names must be unique.")
        joint_names.add(name)
        parent = joint.find("parent")
        child = joint.find("child")
        parent_link = parent.get("link") if parent is not None else None
        child_link = child.get("link") if child is not None else None
        axis_el = joint.find("axis")
        axis = _floats(axis_el.get("xyz"), 3) if axis_el is not None else [1.0, 0.0, 0.0]
        limit = joint.find("limit")
        limits = None
        if limit is not None:
            limits = {key: _attr_float(limit, key) for key in ("lower", "upper", "effort", "velocity")}
        mimic = joint.find("mimic")
        record = {"name": name, "type": kind, "plain": _JOINT_PLAIN.get(kind, "unknown joint type"),
                  "parent": parent_link, "child": child_link, "axis": axis, "limits": limits,
                  "mimic": mimic.get("joint") if mimic is not None else None}
        joints.append(record)
        if kind not in _JOINT_PLAIN:
            warnings.append(f"Joint '{name}' has unknown type {kind!r}; URDF types are {', '.join(_JOINT_PLAIN)}.")
        if parent_link not in names or child_link not in names:
            warnings.append(f"Joint '{name}' connects '{parent_link}' -> '{child_link}', but one of them is not a "
                            "<link> in this file.")
        if child_link in children:
            warnings.append(f"Link '{child_link}' is the child of two joints; a URDF must be a tree.")
        if child_link:
            children.add(child_link)
        if parent_link:
            parents.add(parent_link)
        if kind in ("revolute", "prismatic"):
            if limits is None:
                warnings.append(f"Joint '{name}' ({kind}) has no <limit>; URDF requires one for {kind} joints. "
                                "Without it the joint may get no limits (it can turn through the robot) and no "
                                "effort/velocity caps - add <limit lower= upper= effort= velocity=/>.")
            else:
                lower, upper = limits.get("lower"), limits.get("upper")
                if lower is None or upper is None:
                    warnings.append(f"Joint '{name}' <limit> is missing lower/upper (URDF then means 0..0: the "
                                    "joint cannot move).")
                elif lower > upper:
                    warnings.append(f"Joint '{name}' has lower limit {lower} above upper limit {upper}.")
                elif lower == upper:
                    warnings.append(f"Joint '{name}' has lower == upper ({lower}): it cannot move.")
                if not limits.get("effort"):
                    warnings.append(f"Joint '{name}' has no (or zero) effort limit: its motor would have no "
                                    "force. Set effort= in <limit>.")
        if axis is None:
            warnings.append(f"Joint '{name}' has an unreadable <axis xyz>; three numbers are needed.")
        elif kind not in ("fixed", "floating") and not any(axis):
            warnings.append(f"Joint '{name}' has a zero axis (0 0 0).")

    for link in links:
        if link["name"] == "world" or (link["visuals"] == 0 and link["collisions"] == 0):
            continue
        if link["mass"] is None:
            warnings.append(f"Link '{link['name']}' has no <inertial> mass; PhysX will guess a mass from its "
                            "colliders. Add <inertial> or set link_density.")
        elif link["mass"] <= 0:
            warnings.append(f"Link '{link['name']}' has mass {link['mass']}; a moving link needs a positive mass.")
        elif not link["has_inertia"]:
            warnings.append(f"Link '{link['name']}' has a mass but no <inertia> tensor; PhysX will compute one "
                            "from the colliders.")
        if link["visuals"] and not link["collisions"]:
            warnings.append(f"Link '{link['name']}' has visuals but no <collision>: it will pass through things "
                            "unless you import with collision_from_visuals=True.")

    root_links = [link["name"] for link in links if link["name"] not in children]
    if not links:
        warnings.append("The URDF has no <link> elements.")
    elif len(root_links) != 1:
        warnings.append(f"Expected exactly one root link (a link that is nobody's child), found {len(root_links)}: "
                        f"{', '.join(root_links[:10]) or 'none (a loop?)'}.")
    root_link = root_links[0] if len(root_links) == 1 else None
    fixed_to_world = bool(root_link == "world" and any(
        j["parent"] == "world" and j["type"] == "fixed" for j in joints))
    wheel_joints = [j["name"] for j in joints if j["type"] in ("continuous", "revolute")
                    and (_WHEEL_WORDS.search(j["name"] or "") or _WHEEL_WORDS.search(j["child"] or ""))]
    _mesh_warnings(meshes, packages, warnings)
    ignored = {tag: len(root.findall(tag)) for tag in ("transmission", "gazebo", "ros2_control")}
    looks_like = _looks_like(joints, wheel_joints, fixed_to_world)
    for name in [link["name"] for link in links] + [j["name"] for j in joints]:
        if name and not _NAME.match(name):
            warnings.append(f"Name '{name}' has characters USD does not allow in prim names; the importer will "
                            "rename it (joint-name regex patterns must use the new name).")
            break
    return {
        "ok": True, "action": "parse_urdf_summary", "read_only": True, "path": _posix(source),
        "robot_name": root.get("name"), "file_stem": Path(source).stem,
        "root_link": root_link, "root_links": root_links,
        "link_count": len(links), "joint_count": len(joints),
        "joint_counts": {kind: sum(1 for j in joints if j["type"] == kind) for kind in
                         sorted({j["type"] for j in joints})},
        "movable_joint_count": sum(1 for j in joints if j["type"] not in ("fixed",)),
        "links": links, "joints": joints, "meshes": meshes,
        "mesh_count": len(meshes), "missing_mesh_count": sum(1 for m in meshes if not m["exists"]),
        "materials": materials,
        "ignored_tags": {tag: count for tag, count in ignored.items() if count},
        "ignored_tags_note": "<transmission>, <gazebo> and <ros2_control> are ROS/Gazebo controller settings; this "
                             "summary does not use them for physics.",
        "fixed_to_world": fixed_to_world, "wheel_joints": wheel_joints,
        "looks_like": looks_like, "looks_like_plain": KIND_WORDS.get(looks_like, "could not tell"),
        "warnings": warnings,
    }


def _looks_like(joints: list[dict[str, Any]], wheels: list[str], fixed_to_world: bool) -> str:
    movable = [j for j in joints if j["type"] in ("revolute", "continuous", "prismatic")]
    others = [j for j in movable if j["name"] not in wheels]
    legs = [j for j in others if _LEG_WORDS.search(j["name"] or "") or _LEG_WORDS.search(j["child"] or "")]
    fingers = [j for j in others if _FINGER_WORDS.search(j["name"] or "") or _FINGER_WORDS.search(j["child"] or "")]
    if wheels and len(wheels) >= 2:
        return "mobile manipulator" if len(others) - len(fingers) >= 4 else "mobile"
    if len(legs) >= 4:
        upper = [j for j in others if _ARM_UPPER_WORDS.search(j["name"] or "")]
        return "humanoid" if upper else "legged"
    if fingers and len(others) - len(fingers) <= 1:
        return "gripper"
    if fixed_to_world or len(others) >= 2:
        return "arm"
    return "unknown"


# ------------------------------------------------------------------------------------------------- MJCF summary


def _mjcf_summary(source: str) -> dict[str, Any]:
    root = _read_xml(source, "MJCF")
    if root.tag != "mujoco":
        raise RobotImportError(f"{_posix(source)} is not an MJCF file: its top element is <{root.tag}>, not <mujoco>.")
    warnings: list[str] = []
    source_dir = os.path.dirname(source)
    compiler = root.find("compiler")
    meshdir = (compiler.get("meshdir") or compiler.get("assetdir") or "") if compiler is not None else ""
    base = os.path.join(source_dir, meshdir) if meshdir and not os.path.isabs(meshdir) else (meshdir or source_dir)
    meshes = []
    for mesh in root.iter("mesh"):
        filename = (mesh.get("file") or "").strip()
        if not filename:
            continue
        path = os.path.abspath(filename if os.path.isabs(filename) else os.path.join(base, filename))
        exists = os.path.isfile(path)
        meshes.append({"name": mesh.get("name"), "filename": filename, "resolved_path": _posix(path),
                       "exists": exists, "size_bytes": os.path.getsize(path) if exists else None})
        if not exists:
            warnings.append(f"Mesh {filename} was not found (looked at {_posix(path)}).")
        elif os.path.getsize(path) > BIG_MESH_BYTES:
            warnings.append(f"Mesh {filename} is {os.path.getsize(path) / 1048576:.0f} MB; decimate it for colliders.")
    joints = []
    for joint in root.iter("joint"):
        joints.append({"name": joint.get("name"), "type": joint.get("type") or "hinge",
                       "range": _floats(joint.get("range"), 2), "limited": joint.get("limited")})
    free = len(list(root.iter("freejoint"))) + sum(1 for j in joints if j["type"] == "free")
    bodies = len(list(root.iter("body")))
    includes = [inc.get("file") for inc in root.iter("include")]
    if includes:
        warnings.append(f"The file <include>s {', '.join(str(i) for i in includes[:5])}; those files were not "
                        "checked here (the importer reads them).")
    actuators = root.find("actuator")
    actuator_count = len(list(actuators)) if actuators is not None else 0
    if joints and not actuator_count:
        warnings.append("No <actuator> elements: nothing will drive the joints after import (they only swing).")
    return {"ok": True, "path": _posix(source), "model": root.get("model"), "body_count": bodies,
            "joints": joints, "joint_count": len(joints), "free_joint_count": free, "actuator_count": actuator_count,
            "meshes": meshes, "missing_mesh_count": sum(1 for m in meshes if not m["exists"]), "includes": includes,
            "looks_like": "mobile" if free else "arm", "warnings": warnings}


# ------------------------------------------------------------------------------------------------- ready robots


def _content_root(assets_root: Any = None) -> Path | None:
    """The folder holding Isaac/Robots (e.g. <pack>/Assets/Isaac/6.1), or None when the asset pack is absent."""
    from . import asset_library

    root = Path(os.fspath(assets_root)) if assets_root else asset_library.default_root()
    for candidate in (root / "Assets" / "Isaac" / "6.1", root):
        if (candidate / "Isaac" / "Robots").is_dir():
            return candidate
    finder = getattr(asset_library, "_find_content_root", None)
    if callable(finder) and root.is_dir():
        found = Path(finder(root))
        if (found / "Isaac" / "Robots").is_dir():
            return found
    return None


def _ready_key(name: str) -> str | None:
    text = re.sub(r"[\s-]+", "_", name.strip().lower())
    if text in READY_ROBOTS:
        return text
    for key, info in READY_ROBOTS.items():
        if text in info["aliases"] or text == info["prim_name"].lower():
            return key
    return None


def list_ready_robots(kind: Any = None, *, assets_root: Any = None) -> dict[str, Any]:
    """Isaac Sim's ready-made robot USDs (from the local asset pack) with plain descriptions. Read-only."""
    content = _content_root(assets_root)
    wanted = str(kind).strip().lower() if kind else None
    if wanted and wanted not in KIND_WORDS:
        return _error(f"Unknown kind {kind!r}; use one of {', '.join(KIND_WORDS)}.")
    robots = []
    for key, info in READY_ROBOTS.items():
        if wanted and info["kind"] != wanted:
            continue
        full = (content / info["path"]) if content else None
        robots.append({
            "name": key, "prim_name": info["prim_name"], "kind": info["kind"],
            "kind_plain": KIND_WORDS[info["kind"]], "description": info["description"],
            "aliases": list(info["aliases"]), "relative_path": info["path"],
            "path": _posix(full) if full else None, "available": bool(full and full.is_file()),
            "default_prim": info["default_prim"],
            "suggested_prim_path": f"/World/{info['prim_name']}",
            "fixed_base": info["kind"] in ("arm", "gripper"),
        })
    result = {"ok": True, "read_only": True, "content_root": _posix(content) if content else None,
              "count": len(robots), "available_count": sum(1 for r in robots if r["available"]), "robots": robots,
              "kinds": dict(KIND_WORDS),
              "how_to_use": "add_ready_robot(stage, '<name>', '/World/<Name>', position=(x, y, z), dry_run=False) "
                            "references the robot; nothing is converted or copied."}
    if content is None:
        result["warning"] = ("The Isaac asset pack (isaacsim-assets-6.1.0) was not found; set "
                             "OMNIVERSE_ISAAC_ASSETS_ROOT or pass assets_root.")
    return result


# ---------------------------------------------------------------------------------------------- importer info


def importer_info() -> dict[str, Any]:
    """The URDF / MJCF importer APIs as verified (read-only) in this workspace's Isaac Sim 6.1.0 install."""
    return {
        "ok": True, "verified_on": VERIFIED_ON,
        "how_verified": "Read the installed extension sources (read-only) under C:/Users/krngr/Omniverse/"
                        f"{_EXTS}; no importer was run.",
        "importers": {key: {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                            for k, v in info.items()} for key, info in IMPORTERS.items()},
        "field_help": dict(FIELD_HELP),
        "collision_types": list(COLLISION_TYPES), "robot_types": list(ROBOT_TYPES),
        "summary": ("Isaac Sim 6.1 imports robots with Python classes, not omni.kit.commands: "
                    "URDFImporter(URDFImporterConfig(...)).import_urdf() and "
                    "MJCFImporter(MJCFImporterConfig(...)).import_mjcf(). Both WRITE a new USD folder and return its "
                    ".usda path; you then reference that file into your scene."),
    }


# ------------------------------------------------------------------------------------------------- planning

_COLLISION_CHOICES = {
    "convex_hull": "Convex Hull", "convex_decomposition": "Convex Decomposition",
    "bounding_sphere": "Bounding Sphere", "bounding_cube": "Bounding Cube", "mesh": None,
}
_ROBOT_TYPE_FOR = {"arm": "Manipulator", "gripper": "End Effector", "mobile": "Wheeled", "legged": "Quadruped",
                   "humanoid": "Humanoid", "mobile manipulator": "Mobile Manipulators", "unknown": "Default"}


def _resolve_project(project_dir: Any) -> str | None:
    if project_dir is None or not str(project_dir).strip():
        return None
    text = os.fspath(project_dir)
    if text.startswith(("\\\\", "//")):
        raise RobotImportError(f"Network (UNC) project folders are not supported: {text}")
    if not os.path.isabs(text):
        raise RobotImportError(f"project_dir must be an absolute folder path, not {text!r}.")
    real = os.path.realpath(text)
    if not os.path.isdir(real):
        raise RobotImportError(f"The project folder does not exist: {text}")
    if Path(real) == Path(Path(real).anchor):
        raise RobotImportError(f"Refusing to use a drive root ({real}) as a project folder.")
    return real


def _unique_dir(path: str) -> str:
    """The folder name the importer will use (importer_utils.resolve_unique_path for a directory)."""
    def collides(candidate: str) -> bool:
        if not os.path.exists(candidate):
            return False
        if os.path.isdir(candidate):
            try:
                with os.scandir(candidate) as entries:
                    return any(True for _ in entries)
            except OSError:
                return True
        return True

    if not collides(path):
        return path
    for counter in range(1, 1001):
        candidate = f"{path}_{counter}"
        if not collides(candidate):
            return candidate
    raise RobotImportError(f"Could not find a free output folder next to {path}.")


def _source_kind(source: str) -> str:
    lowered = source.lower()
    if lowered.endswith(".urdf"):
        return "urdf"
    if lowered.endswith(".xml"):
        return "mjcf"
    if lowered.endswith(USD_EXTENSIONS):
        return "usd"
    return "ready"


def _resolve_robot_usd(name_or_path: str, assets_root: Any, extra_roots: list[str]) -> tuple[str, dict[str, Any] | None]:
    """(absolute USD path, ready-robot info or None) for a ready-robot name or a USD path within allowed folders."""
    from . import asset_library

    key = _ready_key(name_or_path)
    content = _content_root(assets_root)
    if key is not None:
        if content is None:
            raise RobotImportError("The Isaac asset pack (isaacsim-assets-6.1.0) was not found; set "
                                   "OMNIVERSE_ISAAC_ASSETS_ROOT or pass assets_root.")
        path = content / READY_ROBOTS[key]["path"]
        if not path.is_file():
            raise RobotImportError(f"Ready robot '{key}' is missing from the asset pack: {_posix(path)}")
        return os.path.abspath(path), {"name": key, **READY_ROBOTS[key]}
    text = name_or_path.strip()
    if not text.lower().endswith(USD_EXTENSIONS):
        known = ", ".join(sorted(READY_ROBOTS))
        raise RobotImportError(f"{name_or_path!r} is neither a ready robot ({known}) nor a .usd/.usda/.usdc/.usdz file.")
    if _SCHEME.match(text) or text.startswith(("\\\\", "//")):
        raise RobotImportError(f"Only local robot files are supported, not {text!r}.")
    workspace = os.path.abspath(asset_library.workspace_root())
    roots = [workspace] + ([os.fspath(content)] if content else []) + [r for r in extra_roots if r]
    candidates = [text] if os.path.isabs(text) else [os.path.join(r, text) for r in roots]
    for candidate in candidates:
        if os.path.isfile(candidate):
            real = os.path.abspath(candidate)
            if not any(_is_within(real, r) for r in roots):
                raise RobotImportError(f"{_posix(real)} is outside the workspace ({_posix(workspace)}) and the asset "
                                       "pack; copy the robot into your project first.")
            return real, None
    raise RobotImportError(f"Robot USD not found: {text} (relative paths are looked up in {', '.join(_posix(r) for r in roots)}).")


def plan_robot_import(
    source: Any,
    *,
    project_dir: Any = None,
    prim_path: Any = None,
    fix_base: Any = None,
    drive: Any = None,
    stiffness: Any = None,
    damping: Any = None,
    collision: Any = "convex_hull",
    self_collision: Any = False,
    as_reference: Any = True,
    merge_fixed_joints: Any = False,
    ros_package_paths: Any = None,
    position: Any = (0.0, 0.0, 0.0),
    assets_root: Any = None,
) -> dict[str, Any]:
    """Dry run of a robot import: which method, the exact importer config, where the USD goes, steps, Script Editor
    code and warnings. Never writes. source is a .urdf, an MJCF .xml, a robot .usd, or a ready robot name."""
    try:
        return _plan(source, project_dir, prim_path, fix_base, drive, stiffness, damping, collision, self_collision,
                     as_reference, merge_fixed_joints, ros_package_paths, position, assets_root)
    except RobotImportError as exc:
        return _error(str(exc), dry_run=True, action="plan_robot_import")


def _plan(source: Any, project_dir: Any, prim_path: Any, fix_base: Any, drive: Any, stiffness: Any, damping: Any,
          collision: Any, self_collision: Any, as_reference: Any, merge_fixed_joints: Any, ros_package_paths: Any,
          position: Any, assets_root: Any) -> dict[str, Any]:
    if source is None or not str(source).strip():
        raise RobotImportError("source is required: a .urdf / MJCF .xml file, a robot .usd, or a ready robot name "
                               "such as 'franka' (see list_ready_robots).")
    text = os.fspath(source).strip()
    project = _resolve_project(project_dir)
    pos = _vec3(position, "position")
    if fix_base is not None and not isinstance(fix_base, bool):
        raise RobotImportError("fix_base must be True, False or None (None = decide from the robot).")
    if drive is not None and drive not in ("position", "velocity"):
        raise RobotImportError("drive must be 'position', 'velocity' or None (None = decide from the robot).")
    stiff, damp = _gain(stiffness, "stiffness"), _gain(damping, "damping")
    collision_key = str(collision or "convex_hull").strip().lower()
    if collision_key not in _COLLISION_CHOICES:
        raise RobotImportError(f"collision must be one of {', '.join(_COLLISION_CHOICES)}, not {collision!r}.")
    if not isinstance(self_collision, bool) or not isinstance(as_reference, bool) or \
            not isinstance(merge_fixed_joints, bool):
        raise RobotImportError("self_collision, as_reference and merge_fixed_joints must be True or False.")
    kind = _source_kind(text)
    base = {"ok": True, "dry_run": True, "action": "plan_robot_import", "source": text, "verified_on": VERIFIED_ON}
    if kind in ("ready", "usd"):
        return _plan_ready(base, text, project, prim_path, pos, as_reference, assets_root,
                           ignored={"fix_base": fix_base, "drive": drive, "stiffness": stiff, "damping": damp,
                                    "collision": None if collision_key == "convex_hull" else collision_key,
                                    "self_collision": self_collision or None,
                                    "merge_fixed_joints": merge_fixed_joints or None,
                                    "ros_package_paths": ros_package_paths})
    if _SCHEME.match(text) or text.startswith(("\\\\", "//")):
        raise RobotImportError(f"Only local files can be imported, not {text!r}.")
    path = text if os.path.isabs(text) else os.path.join(project or os.getcwd(), text)
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise RobotImportError(f"The robot file does not exist: {_posix(path)}")
    packages = _package_map(ros_package_paths)
    if kind == "urdf":
        summary = _urdf_summary(path, ros_package_paths)
    else:
        summary = _mjcf_summary(path)
        if packages:
            summary["warnings"].append("ros_package_paths only applies to URDF files; ignored for MJCF.")
    warnings = list(summary["warnings"])
    looks = summary["looks_like"]
    stem = Path(path).stem
    if not _NAME.match(stem):
        warnings.append(f"The file name '{stem}' becomes the robot's folder, file and prim name; letters, digits "
                        "and '_' only are safest (rename the file before importing).")
    name = _prim_name(stem)
    target_prim = _check_prim_path(prim_path if prim_path is not None else f"/World/{name}")

    inferred: dict[str, Any] = {}
    if fix_base is None:
        if kind == "mjcf":
            fix_base = summary["free_joint_count"] == 0
            why = ("the MJCF root body has a free joint, so it is a free-moving robot" if not fix_base
                   else "the MJCF has no free joint, so its base is welded to the world")
        elif looks in ("arm", "gripper"):
            fix_base, why = True, f"it looks like {KIND_WORDS[looks].split(':')[0]}, which is bolted down"
        elif looks in ("mobile", "mobile manipulator", "legged", "humanoid"):
            fix_base, why = False, f"it looks like a {looks} robot ({len(summary.get('wheel_joints', []))} wheel " \
                                   "joints), which must be free to move"
        elif summary.get("fixed_to_world"):
            fix_base, why = True, "the URDF fixes its root link to a 'world' link"
        else:
            fix_base, why = None, "could not tell arm from mobile robot, so the file's own base setting is kept"
        inferred["fix_base"] = {"value": fix_base, "why": why}
    if drive is None:
        drive = "velocity" if looks == "mobile" else "position"
        inferred["drive"] = {"value": drive, "why": "wheels are driven by speed" if drive == "velocity"
                             else "arm-like joints are driven to a target angle"}
    if looks == "mobile manipulator" and drive == "velocity":
        warnings.append("drive='velocity' applies to EVERY joint, including the arm; for a mobile manipulator "
                        "import with 'position' and switch the wheel joints to velocity afterwards.")
    robot_type = _ROBOT_TYPE_FOR.get(looks, "Default")
    collision_type = _COLLISION_CHOICES[collision_key]
    links_without_collision = [link["name"] for link in summary.get("links", [])
                               if link["visuals"] and not link["collisions"]]
    from_visuals = collision_key in ("convex_decomposition", "bounding_sphere", "bounding_cube")
    if collision_key == "convex_hull" and links_without_collision:
        from_visuals = True
        warnings.append(f"{len(links_without_collision)} link(s) have no <collision>, so colliders will be built "
                        "from the visual meshes (collision_from_visuals=True), replacing the file's own colliders.")
    if collision_key == "convex_decomposition":
        warnings.append("convex_decomposition builds colliders from the VISUAL meshes (the only way the 6.1 importer "
                        "decomposes) and replaces the file's own colliders; more accurate but slower.")
    if collision_key == "mesh":
        collision_type = "Convex Hull"
        warnings.append("collision='mesh': the 6.1 URDF converter marks every collision mesh as a convex hull, and "
                        "PhysX cannot simulate triangle-mesh colliders on moving links; the file's own collision "
                        "shapes are kept (as convex hulls).")

    if project is None:
        output_dir = output_usd = None
        usd_path = None
        warnings.append("No project_dir: pass one so the robot USD goes to <project>/assets/robots/<name>/; without "
                        "it the importer would write next to the source file.")
    else:
        usd_path = os.path.join(project, *ROBOTS_SUBDIR.split("/"))
        planned = os.path.join(usd_path, stem)
        output_dir = _unique_dir(planned)
        if output_dir != planned:
            warnings.append(f"{_posix(planned)} already holds files; the importer will write to "
                            f"{_posix(output_dir)} instead (it never overwrites).")
        output_usd = os.path.join(output_dir, f"{stem}.usda")

    if kind == "urdf":
        config = dict(URDF_CONFIG_DEFAULTS)
        config.update({
            "urdf_path": _posix(path), "usd_path": _posix(usd_path) if usd_path else None,
            "merge_fixed_joints": merge_fixed_joints, "collision_from_visuals": from_visuals,
            "collision_type": collision_type, "allow_self_collision": self_collision,
            "ros_package_paths": [{"name": k, "path": _posix(v)} for k, v in packages.items()],
            "robot_type": robot_type, "fix_base": fix_base, "joint_target_type": drive,
            "override_joint_stiffness": stiff, "override_joint_damping": damp,
        })
        method = "URDF importer"
    else:
        config = dict(MJCF_CONFIG_DEFAULTS)
        config.update({
            "mjcf_path": _posix(path), "usd_path": _posix(usd_path) if usd_path else None,
            "collision_from_visuals": from_visuals, "collision_type": collision_type,
            "allow_self_collision": self_collision, "robot_type": robot_type, "fix_base": fix_base,
        })
        if stiff is not None or damp is not None:
            kp, kd = stiff or 0.0, damp or 0.0
            if drive == "velocity":
                gain, bias = [kd] + [0.0] * 9, [0.0, 0.0, -kd] + [0.0] * 7
            else:
                gain, bias = [kp] + [0.0] * 9, [0.0, -kp, -kd] + [0.0] * 7
                if stiff is None:
                    warnings.append("Position control without stiffness gives kp=0: the joints will not hold a "
                                    "target. Pass stiffness.")
            config.update({"override_gain_type": "fixed", "override_bias_type": "affine",
                           "override_gain_prm": gain, "override_bias_prm": bias})
        else:
            warnings.append("MJCF drives come from the file's <actuator> elements (the 6.1 MJCF importer ignores "
                            "joint_target_type); pass stiffness/damping to override the actuator gains.")
        method = "MJCF importer"
    info = IMPORTERS[kind]
    python = _python_import(kind, config, target_prim, pos, as_reference)
    steps = [
        f"Read the warnings below first ({len(warnings)}): missing meshes or limits are easier to fix in the "
        f"{kind.upper()} file than after import.",
        "Open Isaac Sim 6.1, then Window > Script Editor. Paste the 'python' code and press Run (Ctrl+Enter). "
        f"(The same settings exist in File > Import for .{'urdf' if kind == 'urdf' else 'xml'} files.)",
        f"The importer writes {_posix(output_usd) if output_usd else '<usd_path>/<name>/<name>.usda'} plus a "
        "payloads/ folder. It does not change your open scene.",
        (f"The code then references that file at {target_prim}." if as_reference else
         "as_reference=False: open the new .usda itself (File > Open) to work on the robot alone."),
        "Make sure the stage has a Physics Scene (Create > Physics > Physics Scene) and a ground plane, then press "
        "Play.",
        "Run robot_inspector.inspect_robot on the result (import_robot does this for you) and fix its problems.",
    ]
    return {
        **base, "method": method, "importer": {k: info[k] for k in ("extension", "version", "module", "api")},
        "source_path": _posix(path), "robot_name": stem, "prim_path": target_prim, "position": pos,
        "looks_like": looks, "looks_like_plain": KIND_WORDS.get(looks, "could not tell"),
        "inferred": inferred, "config": config,
        "config_explained": {key: FIELD_HELP.get(key, "") for key in config},
        "project_dir": _posix(project) if project else None,
        "output_dir": _posix(output_dir) if output_dir else None,
        "output_usd": _posix(output_usd) if output_usd else None,
        "as_reference": as_reference,
        "summary": {key: summary.get(key) for key in ("robot_name", "model", "root_link", "link_count", "body_count",
                                                     "joint_count", "joint_counts", "mesh_count", "missing_mesh_count",
                                                     "wheel_joints", "fixed_to_world", "ignored_tags",
                                                     "actuator_count", "free_joint_count") if key in summary},
        "steps": steps, "python": python, "warnings": warnings,
        "how_to_place": {"function": "add_ready_robot", "name": _posix(output_usd) if output_usd else None,
                         "prim_path": target_prim, "position": pos},
    }


def _plan_ready(base: dict[str, Any], text: str, project: str | None, prim_path: Any, pos: list[float],
                as_reference: bool, assets_root: Any, ignored: dict[str, Any]) -> dict[str, Any]:
    usd, ready = _resolve_robot_usd(text, assets_root, [project] if project else [])
    name = ready["prim_name"] if ready else _prim_name(Path(usd).stem)
    target_prim = _check_prim_path(prim_path if prim_path is not None else f"/World/{name}")
    warnings = []
    used = sorted(key for key, value in ignored.items() if value not in (None, False, [], {}))
    if used:
        warnings.append(f"{', '.join(used)} only apply to URDF/MJCF imports; a ready robot is already set up. "
                        "Change its drives afterwards on the joints (Property panel > Drive).")
    if not as_reference:
        warnings.append("as_reference=False: do not edit or re-save files inside the Isaac asset pack; reference the "
                        "robot (recommended) or copy it into your project first.")
    asset_path = _posix(usd)
    if project:
        relative, ok = _relative(usd, project)
        if ok:
            asset_path = relative
    python = _python_reference(asset_path, target_prim, pos)
    return {
        **base, "method": "reference ready USD", "robot_name": ready["name"] if ready else Path(usd).stem,
        "kind": ready["kind"] if ready else None, "description": ready["description"] if ready else None,
        "prim_path": target_prim, "position": pos, "output_usd": _posix(usd), "output_dir": None,
        "config": {"asset_path": asset_path, "prim_path": target_prim, "position": pos, "as_reference": as_reference},
        "as_reference": as_reference,
        "steps": [
            "Nothing to convert: Isaac's ready robots already have colliders, masses and tuned drives.",
            f"Reference {Path(usd).name} at {target_prim} (add_ready_robot(stage, '{ready['name'] if ready else asset_path}',"
            f" '{target_prim}', position={tuple(pos)}, dry_run=False), or paste the 'python' code into the Script "
            "Editor).",
            "Make sure the stage has a Physics Scene and a ground plane, then press Play.",
            "Inspect it with robot_inspector.inspect_robot(stage, root_path=prim_path).",
        ],
        "python": python, "warnings": warnings,
        "how_to_place": {"function": "add_ready_robot", "name": ready["name"] if ready else _posix(usd),
                         "prim_path": target_prim, "position": pos},
    }


def _relative(target: str, from_dir: str) -> tuple[str, bool]:
    try:
        rel = _posix(os.path.relpath(target, from_dir))
    except ValueError:  # different drives
        return _posix(os.path.abspath(target)), False
    return (rel if rel.startswith("../") else f"./{rel}"), True


# ------------------------------------------------------------------------------------------- generated python

_PHYSICS_SNIPPET = """
# 3) Physics needs a Physics Scene (gravity + time step). Add one if the stage has none.
from pxr import UsdPhysics
if not any(p.IsA(UsdPhysics.Scene) for p in stage.Traverse()):
    UsdPhysics.Scene.Define(stage, "/physicsScene")
    print("Added /physicsScene")
"""


def _python_reference(asset_path: str, prim_path: str, pos: list[float], *, variable: str | None = None) -> str:
    asset = variable or repr(asset_path)
    return (
        "# Add a robot to the open stage by REFERENCING its USD file (nothing is copied or converted).\n"
        "# Paste into Window > Script Editor in Isaac Sim and press Run (Ctrl+Enter).\n"
        "import omni.usd\n"
        "from pxr import Gf, UsdGeom\n\n"
        "stage = omni.usd.get_context().get_stage()   # the scene that is open right now\n"
        f"prim_path = {prim_path!r}                  # where the robot will live in the scene tree\n"
        "if stage.GetPrimAtPath(prim_path):\n"
        "    raise RuntimeError(prim_path + ' already exists; pick another prim_path')\n\n"
        "# 1) An empty Xform (a transform node) that will hold the robot.\n"
        "robot = UsdGeom.Xform.Define(stage, prim_path)\n"
        "# 2) The reference pulls the robot file in; its defaultPrim becomes the content of prim_path.\n"
        f"robot.GetPrim().GetReferences().AddReference({asset})\n"
        f"robot.AddTranslateOp().Set(Gf.Vec3d({pos[0]!r}, {pos[1]!r}, {pos[2]!r}))   # position in metres\n"
        + _PHYSICS_SNIPPET +
        "print('Robot added at', prim_path)\n"
    )


def _python_import(kind: str, config: dict[str, Any], prim_path: str, pos: list[float], as_reference: bool) -> str:
    cls, cfg_cls, method = (("URDFImporter", "URDFImporterConfig", "import_urdf") if kind == "urdf"
                            else ("MJCFImporter", "MJCFImporterConfig", "import_mjcf"))
    info = IMPORTERS[kind]
    defaults = URDF_CONFIG_DEFAULTS if kind == "urdf" else MJCF_CONFIG_DEFAULTS
    lines = [
        f"# Import a {kind.upper()} robot with Isaac Sim 6.1's {info['extension']} {info['version']}.",
        "# Paste into Window > Script Editor in Isaac Sim and press Run (Ctrl+Enter). It can take a minute.",
        f"from {info['module']} import {cls}, {cfg_cls}",
        "",
        f"config = {cfg_cls}(",
    ]
    for key, value in config.items():
        if key not in (f"{kind}_path", "usd_path") and value == defaults.get(key):
            continue  # leave defaults out so the code stays short
        lines.append(f"    {key}={value!r},  # {FIELD_HELP.get(key, '')}")
    lines += [
        ")",
        f"importer = {cls}(config)",
        f"output_usd = importer.{method}()   # writes <usd_path>/<name>/<name>.usda and returns that path",
        "print('Robot USD written to:', output_usd)",
    ]
    code = "\n".join(lines) + "\n"
    if not as_reference:
        return code + "# as_reference=False: open output_usd with File > Open to work on the robot by itself.\n"
    placement = _python_reference("", prim_path, pos, variable="output_usd")
    placement = placement.split("import omni.usd", 1)[1]
    return code + "\n# Now add the new robot file to the open scene.\nimport omni.usd" + placement


# ------------------------------------------------------------------------------------------------- import_robot


def import_robot(
    source: Any,
    *,
    project_dir: Any = None,
    prim_path: Any = None,
    fix_base: Any = None,
    drive: Any = None,
    stiffness: Any = None,
    damping: Any = None,
    collision: Any = "convex_hull",
    self_collision: Any = False,
    as_reference: Any = True,
    merge_fixed_joints: Any = False,
    ros_package_paths: Any = None,
    position: Any = (0.0, 0.0, 0.0),
    place_in_stage: Any = False,
    dry_run: Any = True,
    assets_root: Any = None,
) -> dict[str, Any]:
    """Kit-only: run Isaac's URDF/MJCF importer into <project>/assets/robots/<name>/, then inspect the result.

    dry_run (default True) returns plan_robot_import's plan. With dry_run=False inside Isaac Sim it calls
    URDFImporter/MJCFImporter synchronously (the UI pauses while it runs), opens the new USD read-only and runs
    robot_inspector.inspect_robot on it. place_in_stage=True also references it into the open stage at prim_path
    (via add_ready_robot). Ready robots / USD sources need no import: use add_ready_robot.
    """
    plan = plan_robot_import(source, project_dir=project_dir, prim_path=prim_path, fix_base=fix_base, drive=drive,
                             stiffness=stiffness, damping=damping, collision=collision, self_collision=self_collision,
                             as_reference=as_reference, merge_fixed_joints=merge_fixed_joints,
                             ros_package_paths=ros_package_paths, position=position, assets_root=assets_root)
    plan["action"] = "import_robot"
    if not plan.get("ok"):
        return plan
    if dry_run is not False:
        plan["note"] = "Dry run: nothing was imported. Pass dry_run=False inside Isaac Sim to import."
        return plan
    if plan["method"] == "reference ready USD":
        return _error("This source is already a USD robot: nothing to import. Use add_ready_robot(stage, name, "
                      "prim_path, dry_run=False) to reference it.", plan=plan, dry_run=False, action="import_robot")
    if plan["output_usd"] is None:
        return _error("project_dir is required to import: the robot USD goes to <project>/assets/robots/<name>/.",
                      plan=plan, dry_run=False, action="import_robot")
    kind = "urdf" if plan["method"] == "URDF importer" else "mjcf"
    names = (("URDFImporter", "URDFImporterConfig", "import_urdf") if kind == "urdf"
             else ("MJCFImporter", "MJCFImporterConfig", "import_mjcf"))
    module = _kit_module(IMPORTERS[kind]["module"])
    if module is None:
        return {"ok": False, "dry_run": False, "action": "import_robot", "message": _NOT_KIT,
                "error": f"{_NOT_KIT}: {IMPORTERS[kind]['module']} is not importable (run this inside Isaac Sim 6.1 "
                         "with the importer extension enabled).", "plan": plan}
    importer_cls, config_cls = getattr(module, names[0], None), getattr(module, names[1], None)
    if importer_cls is None or config_cls is None:
        return _error(f"{IMPORTERS[kind]['module']} has no {names[0]}/{names[1]}: this is not the verified 6.1 API.",
                      plan=plan, dry_run=False, action="import_robot")
    try:
        config = config_cls()
        unknown = [key for key in plan["config"] if not hasattr(config, key)]
        if unknown:
            return _error(f"{names[1]} has no field(s) {unknown}: the installed importer differs from the verified "
                          f"{IMPORTERS[kind]['version']}.", plan=plan, dry_run=False, action="import_robot")
        for key, value in plan["config"].items():
            setattr(config, key, list(value) if isinstance(value, list) else value)
    except Exception as exc:  # noqa: BLE001
        return _error(f"Could not build {names[1]}: {type(exc).__name__}: {exc}", plan=plan, dry_run=False,
                      action="import_robot")

    usd_root = plan["config"]["usd_path"]
    created = []
    probe = usd_root
    while probe and not os.path.isdir(probe):
        created.append(probe)
        probe = os.path.dirname(probe)
    try:
        os.makedirs(usd_root, exist_ok=True)
    except OSError as exc:
        return _error(f"Could not create {usd_root}: {exc}", plan=plan, dry_run=False, action="import_robot")
    started = time.perf_counter()
    try:
        output = importer_cls(config).__getattribute__(names[2])()
    except Exception as exc:  # noqa: BLE001 - the importer raised: report, clean up empty folders we made
        return _error(f"The {kind.upper()} importer raised {type(exc).__name__}: {exc}", plan=plan, dry_run=False,
                      action="import_robot", cleaned_up=_cleanup(created))
    elapsed = round(time.perf_counter() - started, 2)
    output_text = os.fspath(output) if isinstance(output, (str, os.PathLike)) else ""
    if not output_text or not os.path.isfile(output_text):
        return _error(f"The importer returned {output!r}, but no such USD file exists.", plan=plan, dry_run=False,
                      action="import_robot", cleaned_up=_cleanup(created))
    result: dict[str, Any] = {
        "ok": True, "dry_run": False, "action": "import_robot", "method": plan["method"],
        "output_usd": _posix(os.path.abspath(output_text)), "robot_name": plan["robot_name"],
        "elapsed_s": elapsed, "config_used": plan["config"], "warnings": list(plan["warnings"]),
        "how_to_place": {**plan["how_to_place"], "name": _posix(os.path.abspath(output_text))},
    }
    if _posix(os.path.abspath(output_text)) != plan["output_usd"]:
        result["warnings"].append(f"The importer wrote {_posix(output_text)} (planned {plan['output_usd']}).")
    result["inspection"] = _inspect_file(output_text)
    if place_in_stage is True:
        usd_module = _kit_module("omni.usd")
        stage = usd_module.get_context().get_stage() if usd_module is not None else None
        if stage is None:
            result["placed"] = _error("No open stage to place the robot in (omni.usd context has no stage).")
        else:
            result["placed"] = add_ready_robot(stage, output_text, plan["prim_path"], position=plan["position"],
                                               dry_run=False)
    result["message"] = (f"Imported {plan['robot_name']} to {result['output_usd']} in {elapsed}s; "
                         f"{result['inspection'].get('problem_count', '?')} setup problem(s) found.")
    return result


def _cleanup(created: list[str]) -> list[str]:
    cleaned = []
    for folder in created:  # deepest first; only folders this call created and that are still empty
        try:
            os.rmdir(folder)
            cleaned.append(_posix(folder))
        except OSError:
            pass
    return cleaned


def _inspect_file(path: str) -> dict[str, Any]:
    """robot_inspector.inspect_robot on a USD file opened read-only (never saved)."""
    if _PXR_IMPORT_ERROR is not None:
        return _error(f"pxr is not available to inspect the result: {_PXR_IMPORT_ERROR}")
    from .robot_inspector import inspect_robot

    try:
        stage = Usd.Stage.Open(path)
    except Exception as exc:  # noqa: BLE001
        return _error(f"Could not open {_posix(path)}: {exc}")
    if stage is None:
        return _error(f"Could not open {_posix(path)}.")
    report = inspect_robot(stage)
    if not report.get("ok"):
        return {"ok": False, "error": report.get("error")}
    problems = []
    robots = []
    for robot in report.get("robots", []):
        robots.append({key: robot.get(key) for key in ("name", "root", "dof_count", "joint_counts", "plain_summary")})
        for problem in robot.get("problems", []):
            problems.append({"robot": robot.get("name"), **problem})
    return {"ok": True, "robot_count": report.get("robot_count", len(robots)), "robots": robots,
            "problem_count": len(problems), "problems": problems[:50],
            "errors": sum(1 for p in problems if p.get("severity") == "error")}


# ---------------------------------------------------------------------------------------------- add_ready_robot


def add_ready_robot(
    stage: Any,
    name: Any,
    prim_path: Any = None,
    *,
    position: Any = (0.0, 0.0, 0.0),
    relative: Any = True,
    dry_run: Any = True,
    assets_root: Any = None,
) -> dict[str, Any]:
    """Plan (dry_run=True, default) or author an Xform at prim_path that references a robot USD. Pure pxr.

    name is a ready robot ('franka', 'jetbot', ... see list_ready_robots) or a .usd/.usda path inside the workspace,
    the asset pack or the stage's own folder. The reference is authored relative to the edit-target layer when
    possible (relative=True), else as an absolute forward-slash path. An existing prim is refused; the layer is not
    saved.
    """
    dry = dry_run is not False
    base = {"ok": False, "action": "add_ready_robot", "dry_run": dry}
    try:
        if _PXR_IMPORT_ERROR is not None:
            raise RobotImportError(f"pxr (OpenUSD) is not available here: {_PXR_IMPORT_ERROR}")
        if stage is None or not hasattr(stage, "GetPseudoRoot"):
            raise RobotImportError("stage must be an open Usd.Stage.")
        if not isinstance(name, (str, os.PathLike)) or not os.fspath(name).strip():
            raise RobotImportError("name must be a ready robot name such as 'franka' or a robot .usd path.")
        root_layer = stage.GetRootLayer()
        layer_dir = os.path.dirname(root_layer.realPath) if root_layer.realPath else None
        usd, ready = _resolve_robot_usd(os.fspath(name), assets_root, [layer_dir] if layer_dir else [])
        default_name = ready["prim_name"] if ready else _prim_name(Path(usd).stem)
        target = _check_prim_path(prim_path if prim_path is not None else f"/World/{default_name}")
        pos = _vec3(position, "position")
        warnings: list[str] = []
        existing = stage.GetPrimAtPath(target)
        if existing and existing.IsValid():
            raise RobotImportError(f"{target} already exists; pick another prim_path (nothing was changed).")
        stack = {os.path.normcase(os.path.abspath(layer.realPath)) for layer in stage.GetLayerStack() if layer.realPath}
        if os.path.normcase(usd) in stack:
            raise RobotImportError(f"{_posix(usd)} is a layer of this stage itself; it cannot reference itself.")
        from .asset_library import read_header, root_prim_names

        header = read_header(usd)
        default_prim = header.get("default_prim")
        reference_prim = None
        if not default_prim:
            roots = root_prim_names(usd) or []
            if len(roots) == 1:
                reference_prim = f"/{roots[0]}"
                warnings.append(f"{Path(usd).name} has no defaultPrim; referencing its only root prim {reference_prim}.")
            else:
                raise RobotImportError(f"{Path(usd).name} has no defaultPrim and {len(roots)} root prims; cannot tell "
                                       "which one is the robot.")
        stage_mpu = float(UsdGeom.GetStageMetersPerUnit(stage))
        asset_mpu = header.get("meters_per_unit")
        if asset_mpu is not None and not math.isclose(stage_mpu, float(asset_mpu), rel_tol=1e-6):
            warnings.append(f"The stage uses metersPerUnit {stage_mpu:g} but the robot uses {float(asset_mpu):g}; "
                            "robots should live in a metres stage (metersPerUnit 1) or physics will be off.")
        stage_up = str(UsdGeom.GetStageUpAxis(stage))
        if header.get("up_axis") and header["up_axis"] != stage_up:
            warnings.append(f"The stage is {stage_up}-up but the robot is {header['up_axis']}-up; it will lie on its "
                            "side unless you rotate it.")
        edit_layer = stage.GetEditTarget().GetLayer()
        asset_path = _posix(usd)
        if relative is not False and edit_layer.realPath:
            rel, ok = _relative(usd, os.path.dirname(edit_layer.realPath))
            if ok:
                asset_path = rel
        elif relative is not False:
            warnings.append("The edit target layer is not saved to disk yet, so an absolute path is used.")
        missing_parents = []
        parent = Sdf.Path(target).GetParentPath()
        while parent != Sdf.Path.absoluteRootPath and not stage.GetPrimAtPath(parent):
            missing_parents.insert(0, str(parent))
            parent = parent.GetParentPath()
        plan = {"path": target, "type": "Xform", "creates_parents": missing_parents,
                "reference": {"asset_path": asset_path, "prim_path": reference_prim, "absolute_path": _posix(usd)},
                "translate": pos, "robot": ready["name"] if ready else None,
                "kind": ready["kind"] if ready else None,
                "asset": {"default_prim": default_prim, "up_axis": header.get("up_axis"),
                          "meters_per_unit": asset_mpu},
                "edit_layer": edit_layer.identifier}
    except RobotImportError as exc:
        return {**base, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - planning only reads, so nothing was written
        return {**base, "error": f"Unexpected error while planning (nothing was changed): {type(exc).__name__}: {exc}"}
    if dry:
        return {**base, "ok": True, "would_create": plan, "warnings": warnings,
                "python": _python_reference(asset_path, target, pos),
                "note": "Dry run: nothing was changed. Pass dry_run=False to add the robot."}
    try:
        for path in missing_parents:
            UsdGeom.Xform.Define(stage, path)
        xform = UsdGeom.Xform.Define(stage, target)
        refs = xform.GetPrim().GetReferences()
        if reference_prim:
            refs.AddReference(asset_path, Sdf.Path(reference_prim))
        else:
            refs.AddReference(asset_path)
        xform.AddTranslateOp().Set(Gf.Vec3d(*pos))
    except Exception as exc:  # noqa: BLE001
        return {**base, "error": f"Authoring failed: {type(exc).__name__}: {exc}", "plan": plan}
    prim = stage.GetPrimAtPath(target)
    check: dict[str, Any] = {"children": len(prim.GetChildren()) if prim else 0}
    try:
        from .robot_inspector import inspect_robot

        report = inspect_robot(stage, target, max_prims=3000)
        if report.get("ok"):
            check["robot_count"] = report.get("robot_count")
            check["problem_count"] = sum(len(r.get("problems", [])) for r in report.get("robots", []))
    except Exception:  # noqa: BLE001 - the check is a bonus
        pass
    if not check["children"]:
        warnings.append("The referenced robot composed no children; check the asset path and its defaultPrim.")
    return {**base, "ok": True, "created": plan, "check": check, "warnings": warnings, "saved": False,
            "note": "Added in memory on the edit target layer; save the stage (Ctrl+S) to keep it."}
