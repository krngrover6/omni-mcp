"""ROS 2 readiness checker for Isaac Sim 6.1 on Windows: verified facts, a read-only environment check, a graph check
and text-only setup plans.

Four public functions, all returning JSON-safe dicts:

* ros2_info() - what the INSTALLED Isaac Sim 6.1 ships for ROS 2 (extension names and versions, bundled distros and
  RMW libraries, launcher behaviour, OmniGraph node type ids, Windows notes), with the file each fact came from.
* environment_check() - read-only look at this PC: is a ``ros2`` command on PATH, which ROS variables are set (only
  safe, validated values are echoed: distro, RMW name, domain id), is the bridge extension installed and in which
  .kit files / autoload list it is enabled, and a list of what is still needed. The only process it can start is an
  allowlisted ``ros2 --help`` with a short timeout, and only when ``probe_cli=True``.
* graph_check(stage) - pure pxr: finds ROS 2 bridge nodes in saved OmniGraph (Action Graph) prims, lists publishers
  and subscribers with their topics, frame ids and target prims, and reports common mistakes (no ROS2Context, joint
  state publisher pointed at a prim that is not an articulation, duplicate topics, camera helper without a render
  product, no OnPlaybackTick trigger, ...). Nothing is edited.
* setup_plan(goal) - steps, a beginner-commented og.Controller.edit script and the ``ros2`` commands to verify the
  result, for goal = 'publish_joint_states' | 'camera' | 'lidar' | 'clock' | 'teleop'. TEXT ONLY: nothing is run.

Facts were read (read-only) from C:/Users/krngr/Omniverse/isaacsim-6.1.0 (6.1.0-rc.26): see SOURCES. This module
never installs anything, never edits configs or scenes, opens no network connection and never echoes secret values.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence

try:
    from pxr import Usd
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Usd = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

__all__ = [
    "GOALS",
    "NODE_TYPES",
    "SOURCES",
    "environment_check",
    "graph_check",
    "ros2_info",
    "run_ros2_cli",
    "scrub",
    "setup_plan",
]

# ------------------------------------------------------------------------------------------------ verified facts

ISAAC_DIR_NAME = "isaacsim-6.1.0"
ISAAC_VERSION = "6.1.0-rc.26"
_EXT_DIR = "exts"
BRIDGE_EXT = "isaacsim.ros2.bridge"

# Relative to the Isaac Sim root. Every fact in this module was read from one of these files.
SOURCES: dict[str, str] = {
    "version": "VERSION",
    "bridge_toml": "exts/isaacsim.ros2.bridge/config/extension.toml",
    "bridge_overview": "exts/isaacsim.ros2.bridge/docs/Overview.md",
    "core_toml": "exts/isaacsim.ros2.core/config/extension.toml",
    "core_overview": "exts/isaacsim.ros2.core/docs/Overview.md",
    "core_settings": "exts/isaacsim.ros2.core/docs/SETTINGS.md",
    "core_startup": "exts/isaacsim.ros2.core/isaacsim/ros2/core/impl/extension.py",
    "core_env_helpers": "exts/isaacsim.ros2.core/isaacsim/ros2/core/impl/ros2_common.py",
    "nodes_toml": "exts/isaacsim.ros2.nodes/config/extension.toml",
    "node_ids": "exts/isaacsim.ros2.nodes/ogn/nodes.json",
    "node_docs": "exts/isaacsim.ros2.nodes/ogn/docs/",
    "graph_api": "exts/isaacsim.ros2.nodes/isaacsim/ros2/nodes/impl/graph_api.py",
    "teleop_graph_example": "exts/isaacsim.ros2.nodes/isaacsim/ros2/nodes/tests/test_differential_base.py",
    "lidar_graph_example": "exts/isaacsim.ros2.nodes/isaacsim/ros2/nodes/tests/test_rtx_sensor.py",
    "ros_menu": "exts/isaacsim.ros2.ui/isaacsim/ros2/ui/extension.py",
    "message_types": "exts/isaacsim.ros2.core/include/isaacsim/ros2/core/",
    "core_node_docs": "exts/isaacsim.core.nodes/ogn/docs/",
    "read_joint_state_docs": "exts/isaacsim.sensors.physics.nodes/ogn/docs/",
    "differential_docs": "exts/isaacsim.robot.wheeled_robots.nodes/ogn/docs/",
    "lidar_create": "exts/isaacsim.sensors.experimental.rtx/isaacsim/sensors/experimental/rtx/impl/lidar.py",
    "setup_ros_env": "setup_ros_env.bat",
    "launcher": "isaac-sim.bat",
    "python_launcher": "python.bat",
    "full_kit": "apps/isaacsim.exp.full.kit",
    "startup_enable": "exts/isaacsim.app.setup/isaacsim/app/setup/startup.py",
    "autoload_key": "extscache/omni.kit.window.extensions-1.5.16+00c488ae/omni/kit/window/extensions/ext_controller.py",
    "ros_workspace_skill": "skills/isaac-sim-ros-workspaces/SKILL.md",
    "ros_bridge_skill": "skills/isaac-sim-ros2-bridge/SKILL.md",
}

EXTENSIONS: dict[str, dict[str, str]] = {
    "isaacsim.ros2.bridge": {"version": "5.1.4", "role": "Top-level 'ROS 2 Bridge' extension; enabling it enables "
                             "ros2.core, ros2.nodes, ros2.examples and ros2.ui (its [dependencies])."},
    "isaacsim.ros2.core": {"version": "1.11.0", "role": "C++ backend (rcl), bundled ROS 2 libraries (humble/, jazzy/), "
                           "all bridge settings under /exts/isaacsim.ros2.bridge/*."},
    "isaacsim.ros2.nodes": {"version": "1.22.7", "role": "The OmniGraph nodes (ids keep the 'isaacsim.ros2.bridge.' "
                            "prefix) plus the create_ros2_*_graph Python API."},
    "isaacsim.ros2.ui": {"version": "1.9.2", "role": "Menu Tools > Robotics > ROS 2 OmniGraphs (Camera, RTX Lidar, "
                         "RTX Radar, TF Publisher, Odometry Publisher, Joint States, Clock, Generic Publisher)."},
    "isaacsim.ros2.examples": {"version": "1.2.9", "role": "Sample ROS 2 scenes."},
    "isaacsim.ros2.control": {"version": "0.1.6", "role": "ros2_control ControllerManager node."},
    "isaacsim.ros2.sim_control": {"version": "1.6.8", "role": "Simulation control through ROS 2 services."},
    "isaacsim.ros2.tf_viewer": {"version": "2.3.8", "role": "TF tree viewer."},
    "isaacsim.ros2.urdf": {"version": "2.3.9", "role": "URDF import with ROS 2 conventions."},
}

BUNDLED_DISTROS = ("humble", "jazzy")  # exts/isaacsim.ros2.core/<distro>/{lib,rclpy,share}
BUNDLED_RMW = ("rmw_fastrtps_cpp", "rmw_cyclonedds_cpp", "rmw_zenoh_cpp")  # <distro>/lib/<name>.dll, both distros
LAUNCHER_DEFAULTS = {"ROS_DISTRO": "jazzy", "RMW_IMPLEMENTATION": "rmw_zenoh_cpp"}  # setup_ros_env.bat

BRIDGE_SETTINGS = {  # exts."isaacsim.ros2.bridge".* in isaacsim.ros2.core extension.toml / SETTINGS.md
    "ros_distro": "system_default",
    "publish_without_verification": False,
    "publish_multithreading_disabled": False,
    "publish_with_queue_thread": True,
    "publish_queue_thread_sleep_us": 1000,
    "enable_nitros_bridge": False,
}

WINDOWS_NOTES = [
    "The bridge is NOT enabled automatically on Windows: apps/isaacsim.exp.full.kit sets "
    "isaac.startup.ros_bridge_extension = \"\" for windows-x86_64 (\"isaacsim.ros2.bridge\" only for Linux). Enable it "
    "in Window > Extensions (search 'ROS 2 Bridge', toggle it, tick AUTOLOAD to keep it) or launch with "
    "--enable isaacsim.ros2.bridge.",
    "isaac-sim.bat and python.bat call setup_ros_env.bat unless --no-ros-env is passed. When ROS_DISTRO is not set it "
    "sets ROS_DISTRO=jazzy, puts exts/isaacsim.ros2.core/jazzy/lib first on PATH, adds it to AMENT_PREFIX_PATH, and "
    "sets RMW_IMPLEMENTATION=rmw_zenoh_cpp if that is not set.",
    "If Kit starts without ROS_DISTRO (for example kit.exe directly or --no-ros-env), the core extension falls back "
    "to the bundled 'humble' libraries on Windows (warning \"Using 'humble' as default\").",
    "Bundled ROS 2 libraries exist for humble and jazzy, each with rmw_fastrtps_cpp, rmw_cyclonedds_cpp and "
    "rmw_zenoh_cpp DLLs. No zenoh router executable (rmw_zenohd) ships with Isaac Sim.",
    "Isaac's ROS workspace skill supports Windows 11 x64 with ROS 2 Jazzy through Pixi only; Windows Humble and WSL2 "
    "are listed as deprecated/unsupported flows (skills/isaac-sim-ros-workspaces/SKILL.md).",
    "Some tests are excluded on Windows (configured SRTX sensor sets) and parts of the lidar generic model output "
    "plugin are 'not included in Windows' (exts/isaacsim.ros2.nodes/config/extension.toml).",
    "ROS 2 publishers, subscribers and services are active only while the timeline plays (Press Play).",
]

GENERAL_ROS_NOTES = [  # general ROS 2 knowledge, NOT read from the Isaac install
    "Both sides must use the same RMW_IMPLEMENTATION and ROS_DOMAIN_ID (default 0) or they will not see each other.",
    "rmw_zenoh_cpp needs a Zenoh router for discovery: in a sourced ROS 2 shell run 'ros2 run rmw_zenoh_cpp rmw_zenohd' "
    "before starting the other nodes (or set RMW_IMPLEMENTATION=rmw_fastrtps_cpp in BOTH shells before launching).",
    "Nodes that should follow simulation time need use_sim_time:=true and a /clock publisher.",
]

# OmniGraph node types. Ids, versions and inputs come from exts/isaacsim.ros2.nodes/ogn/nodes.json and ogn/docs/*.rst;
# message types from the bridge's typed messages (exts/isaacsim.ros2.core/include/isaacsim/ros2/core/*.hpp).
NODE_TYPES: dict[str, dict[str, Any]] = {}
_B = "isaacsim.ros2.bridge."


def _nt(name: str, version: int, kind: str, *, msg: str | None = None, topic: str | None = None,
        frames: Sequence[str] = (), targets: Sequence[str] = (), render: bool = False, exec_in: str | None = "execIn",
        context: bool = True, timestamp: bool = False, type_id: str | None = None) -> None:
    NODE_TYPES[type_id or _B + name] = {
        "ui": re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).replace("R O S2", "ROS2").replace("ROS2 ", "ROS2 "),
        "version": version, "kind": kind, "message_type": msg, "default_topic": topic, "frame_inputs": tuple(frames),
        "target_inputs": tuple(targets), "needs_render_product": render, "exec_input": exec_in,
        "has_context": context, "has_timestamp": timestamp, "extension": "isaacsim.ros2.nodes",
    }


_nt("ROS2Context", 2, "context", exec_in=None, context=False)
_nt("ROS2QoSProfile", 1, "qos", exec_in=None, context=False)
_nt("ROS2PublishClock", 1, "publisher", msg="rosgraph_msgs/msg/Clock", topic="clock", timestamp=True)
_nt("ROS2PublishJointState", 1, "publisher", msg="sensor_msgs/msg/JointState", topic="joint_states",
    targets=("targetPrim",), timestamp=True)
_nt("ROS2PublishTransformTree", 1, "publisher", msg="tf2_msgs/msg/TFMessage", topic="tf",
    targets=("targetPrims", "parentPrim"), timestamp=True)
_nt("ROS2PublishRawTransformTree", 1, "publisher", msg="tf2_msgs/msg/TFMessage", topic="tf",
    frames=("parentFrameId", "childFrameId"), timestamp=True)
_nt("ROS2PublishOdometry", 1, "publisher", msg="nav_msgs/msg/Odometry", topic="odom",
    frames=("odomFrameId", "chassisFrameId"), timestamp=True)
_nt("ROS2PublishImu", 1, "publisher", msg="sensor_msgs/msg/Imu", topic="imu", frames=("frameId",), timestamp=True)
_nt("ROS2PublishLaserScan", 3, "publisher", msg="sensor_msgs/msg/LaserScan", topic="scan", frames=("frameId",),
    timestamp=True)
_nt("ROS2PublishPointCloud", 3, "publisher", msg="sensor_msgs/msg/PointCloud2", topic="point_cloud",
    frames=("frameId",), timestamp=True)
_nt("ROS2PublishImage", 1, "publisher", msg="sensor_msgs/msg/Image", topic="rgb", frames=("frameId",), timestamp=True)
_nt("ROS2PublishCompressedImage", 2, "publisher", msg="sensor_msgs/msg/CompressedImage",
    topic="image_raw/compressed", frames=("frameId",), timestamp=True)
_nt("ROS2PublishCameraInfo", 2, "publisher", msg="sensor_msgs/msg/CameraInfo", topic="camera_info",
    frames=("frameId",), timestamp=True)
_nt("ROS2PublishBbox2D", 1, "publisher", msg="vision_msgs/msg/Detection2DArray", topic="bbox2d", frames=("frameId",),
    timestamp=True)
_nt("ROS2PublishBbox3D", 1, "publisher", msg="vision_msgs/msg/Detection3DArray", topic="bbox3d", frames=("frameId",),
    timestamp=True)
_nt("ROS2PublishSemanticLabels", 1, "publisher", msg="std_msgs/msg/String", topic="labels", timestamp=True)
_nt("ROS2PublishObjectIdMap", 1, "publisher", msg=None, topic="labels", timestamp=True)
_nt("ROS2PublishAckermannDrive", 1, "publisher", msg="ackermann_msgs/msg/AckermannDriveStamped",
    topic="ackermann_cmd", frames=("frameId",), timestamp=True)
_nt("ROS2Publisher", 1, "publisher", msg=None, topic="topic")
_nt("ROS2SubscribeJointState", 2, "subscriber", msg="sensor_msgs/msg/JointState", topic="joint_command")
_nt("ROS2SubscribeTwist", 1, "subscriber", msg="geometry_msgs/msg/Twist", topic="cmd_vel")
_nt("ROS2SubscribeClock", 1, "subscriber", msg="rosgraph_msgs/msg/Clock", topic="clock")
_nt("ROS2SubscribeTransformTree", 1, "subscriber", msg="tf2_msgs/msg/TFMessage", topic="tf")
_nt("ROS2SubscribeAckermannDrive", 1, "subscriber", msg="ackermann_msgs/msg/AckermannDriveStamped",
    topic="ackermann_cmd")
_nt("ROS2Subscriber", 1, "subscriber", msg=None, topic="topic")
_nt("ROS2CameraHelper", 4, "sensor_helper", msg=None, topic="rgb", frames=("frameId",), render=True)
_nt("ROS2CameraInfoHelper", 1, "sensor_helper", msg="sensor_msgs/msg/CameraInfo", topic="camera_info",
    frames=("frameId",), render=True)
_nt("ROS2RtxLidarHelper", 3, "sensor_helper", msg=None, topic="scan", frames=("frameId",), render=True)
_nt("ROS2RtxRadarHelper", 1, "sensor_helper", msg="sensor_msgs/msg/PointCloud2", topic="radar_point_cloud",
    frames=("frameId",), render=True)
_nt("ROS2RtxLidarPointCloudConfig", 1, "config", exec_in=None, context=False)
_nt("ROS2ServicePrim", 1, "service")
_nt("OgnROS2ServiceClient", 1, "service")
_nt("OgnROS2ServiceServerRequest", 1, "service")
_nt("OgnROS2ServiceServerResponse", 1, "service", exec_in="onReceived")
_nt("ROS2ControlManager", 2, "control", targets=("targetPrim",), context=False,
    type_id="isaacsim.ros2.control.ROS2ControlManager")
for _info in NODE_TYPES.values():
    _info["ui"] = _info["ui"].replace("Ogn ROS2", "ROS2")

# ROS2CameraHelper inputs:type allowedTokens (OgnROS2CameraHelper.ogn) -> message type.
CAMERA_HELPER_TYPES = {
    "rgb": "sensor_msgs/msg/Image", "rgb_h264": "sensor_msgs/msg/CompressedImage",
    "rgb_hevc": "sensor_msgs/msg/CompressedImage", "depth": "sensor_msgs/msg/Image",
    "depth_pcl": "sensor_msgs/msg/PointCloud2", "instance_segmentation": "sensor_msgs/msg/Image",
    "semantic_segmentation": "sensor_msgs/msg/Image", "bbox_2d_tight": "vision_msgs/msg/Detection2DArray",
    "bbox_2d_loose": "vision_msgs/msg/Detection2DArray", "bbox_3d": "vision_msgs/msg/Detection3DArray",
}
LIDAR_HELPER_TYPES = {"laser_scan": "sensor_msgs/msg/LaserScan", "point_cloud": "sensor_msgs/msg/PointCloud2"}

# Non-ROS helper nodes the recipes use (exts/isaacsim.core.nodes, isaacsim.sensors.physics.nodes,
# isaacsim.robot.wheeled_robots.nodes ogn docs; omni.graph.action / omni.graph.nodes nodes.json).
HELPER_NODE_TYPES = {
    "omni.graph.action.OnPlaybackTick": "On Playback Tick (fires every frame while playing)",
    "isaacsim.core.nodes.IsaacReadSimulationTime": "Isaac Read Simulation Time (v1, outputs:simulationTime)",
    "isaacsim.core.nodes.OgnIsaacRunOneSimulationFrame": "Isaac Run One Simulation Frame (v1, outputs:step)",
    "isaacsim.core.nodes.IsaacCreateRenderProduct": "Isaac Create Render Product (v3, inputs:cameraPrim/width/height, "
                                                    "outputs:renderProductPath/execOut)",
    "isaacsim.core.nodes.IsaacArticulationController": "Isaac Articulation Controller (v1, inputs:targetPrim, "
                                                       "jointNames, positionCommand/velocityCommand/effortCommand)",
    "isaacsim.core.nodes.IsaacComputeOdometry": "Isaac Compute Odometry (v1)",
    "isaacsim.sensors.physics.IsaacReadJointState": "Isaac Read Joint State (v1, inputs:prim, outputs:jointNames, "
                                                    "jointPositions, jointVelocities, jointEfforts, ...)",
    "isaacsim.robot.wheeled_robots.DifferentialController": "Differential Controller (v1, inputs:wheelRadius, "
                                                            "wheelDistance, linearVelocity, angularVelocity)",
    "omni.graph.nodes.BreakVector3": "Break 3-Vector (outputs:x, y, z)",
}
_TICK_TYPES = {"omni.graph.action.OnPlaybackTick", "omni.graph.action.OnTick", "isaacsim.core.nodes.OnPhysicsStep"}
_READ_JOINT_STATE = "isaacsim.sensors.physics.IsaacReadJointState"
_ART_CONTROLLER = "isaacsim.core.nodes.IsaacArticulationController"
_CREATE_RENDER_PRODUCT = "isaacsim.core.nodes.IsaacCreateRenderProduct"
_LEGACY_PREFIXES = ("omni.isaac.ros2_bridge.", "omni.isaac.ros_bridge.")
_ROOT_API = "PhysicsArticulationRootAPI"
_TF_TOPICS = {"/tf", "/tf_static"}

_NAME_PART = r"[A-Za-z_][A-Za-z0-9_]*"
_TOPIC_RE = re.compile(rf"^[~/]?{_NAME_PART}(/{_NAME_PART})*$")
_NAMESPACE_RE = re.compile(rf"^/?({_NAME_PART}(/{_NAME_PART})*)?/?$")
_PRIM_PATH_RE = re.compile(rf"^(/{_NAME_PART})+$")
_DISTRO_RE = re.compile(r"^[a-z]{3,20}$")
_RMW_RE = re.compile(r"^rmw_[a-z0-9_]{2,40}$")

# ------------------------------------------------------------------------------------------------ small helpers

_REDACTED = "<redacted>"
_SECRET_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION)", re.IGNORECASE)
_SAFE_SECRET_LOOKING = {"SESSIONNAME"}  # Windows sets SESSIONNAME=Console; harmless and too short to matter


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _secret_values(env: Mapping[str, str] | None) -> list[str]:
    values = []
    for name, value in _env(env).items():
        if name.upper() in _SAFE_SECRET_LOOKING or not _SECRET_NAME_RE.search(str(name)):
            continue
        text = str(value or "").strip()
        if len(text) >= 4:
            values.append(text)
    return sorted(set(values), key=len, reverse=True)


def scrub(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Replace the value of every secret-looking environment variable (*KEY*, *TOKEN*, *SECRET*, *PASSWORD*, ...)
    with <redacted> wherever it appears in strings, dict keys or lists. Defense in depth for CLI output."""
    secrets = _secret_values(env)
    if not secrets:
        return value

    def _clean(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, _REDACTED)
            return item
        if isinstance(item, dict):
            return {_clean(k): _clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [_clean(v) for v in item]
        return item

    return _clean(value)


def _short(text: Any, limit: int = 300) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def default_isaac_root() -> Path:
    return workspace_root() / ISAAC_DIR_NAME


def _read_text(path: Path, limit: int = 2_000_000) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _problem(severity: str, code: str, node: str | None, message: str, fix: str, **extra: Any) -> dict[str, Any]:
    return {"severity": severity, "code": code, "node": node, "message": message, "fix": fix, **extra}


# ------------------------------------------------------------------------------------------------ ros2_info


def ros2_info(isaac_root: str | Path | None = None, *, check_disk: bool = True) -> dict[str, Any]:
    """The verified ROS 2 facts for Isaac Sim 6.1 on Windows, with the source file of each. check_disk only tests that
    the cited files exist (no file is changed)."""
    root = Path(isaac_root) if isaac_root else default_isaac_root()
    sources = {key: f"{root.as_posix()}/{rel}" for key, rel in SOURCES.items()}
    result: dict[str, Any] = {
        "ok": True,
        "isaac_sim": ISAAC_VERSION,
        "isaac_root": root.as_posix(),
        "extensions": {name: dict(info) for name, info in EXTENSIONS.items()},
        "extension_to_enable": BRIDGE_EXT,
        "how_to_enable": [
            "GUI: Window > Extensions, search 'ROS 2 Bridge' (isaacsim.ros2.bridge), toggle it on; tick AUTOLOAD so it "
            "starts next time (saved in user.config.json under /persistent/app/exts/enabled).",
            "Command line: isaac-sim.bat --enable isaacsim.ros2.bridge",
            "A .kit file: add \"isaacsim.ros2.bridge\" = {} to [dependencies], or set isaac.startup.ros_bridge_extension "
            "= \"isaacsim.ros2.bridge\" (the full app sets it to \"\" on Windows).",
        ],
        "bundled_ros_distros": list(BUNDLED_DISTROS),
        "bundled_rmw_implementations": list(BUNDLED_RMW),
        "launcher_defaults_when_unset": dict(LAUNCHER_DEFAULTS),
        "fallback_without_launcher": {"windows": "humble", "linux": "humble on Ubuntu 22.04, jazzy on Ubuntu 24.04"},
        "officially_supported_distros": ["humble", "jazzy"],
        "windows_ros_side": "ROS 2 Jazzy via the Pixi workspace (isaac-sim/IsaacSim-ros_workspaces); Windows Humble "
                            "and WSL2 are deprecated flows in Isaac's own workspace skill.",
        "environment_variables": {
            "ROS_DISTRO": "Which bundled/sourced distro the bridge loads (jazzy from setup_ros_env.bat when unset).",
            "RMW_IMPLEMENTATION": "Middleware; setup_ros_env.bat defaults to rmw_zenoh_cpp. Must match the ROS side.",
            "ROS_DOMAIN_ID": "Read by ROS2Context when inputs:useDomainIDEnvVar is true (the default); else "
                             "inputs:domain_id (default 0).",
            "AMENT_PREFIX_PATH": "setup_ros_env.bat appends exts/isaacsim.ros2.core/<distro> when using bundled libs.",
            "PATH": "setup_ros_env.bat prepends exts/isaacsim.ros2.core/<distro>/lib; the core extension appends its "
                    "bin folder on Windows.",
        },
        "settings": {f"/exts/{BRIDGE_EXT}/{key}": value for key, value in BRIDGE_SETTINGS.items()},
        "internal_setting": f"/exts/{BRIDGE_EXT}/internal_lib_fallback (set True by the core extension when "
                            "ROS_DISTRO was not set by the user)",
        "node_types": {type_id: {k: (list(v) if isinstance(v, tuple) else v) for k, v in info.items()}
                       for type_id, info in NODE_TYPES.items()},
        "node_id_note": "Node ids keep the 'isaacsim.ros2.bridge.' prefix although the nodes live in the "
                        "isaacsim.ros2.nodes extension (ROS2ControlManager is isaacsim.ros2.control.*). Old "
                        "'omni.isaac.ros2_bridge.*' ids are from Isaac Sim 2023.x and are not registered in 6.1.",
        "camera_helper_types": dict(CAMERA_HELPER_TYPES),
        "lidar_helper_types": dict(LIDAR_HELPER_TYPES),
        "helper_node_types": dict(HELPER_NODE_TYPES),
        "menu": "Tools > Robotics > ROS 2 OmniGraphs",
        "python_api": "from isaacsim.ros2.nodes import create_ros2_clock_graph, Ros2ClockGraphConfig (also camera, "
                      "rtx_lidar, rtx_radar, joint_states, tf, odometry, generic_publisher)",
        "activation": "ROS 2 publishers, subscribers and services are active only while the simulation plays.",
        "windows_notes": list(WINDOWS_NOTES),
        "general_ros_notes": list(GENERAL_ROS_NOTES),
        "sources": sources,
    }
    if check_disk:
        missing = [key for key, rel in SOURCES.items() if not (root / rel).exists()]
        result["sources_on_disk"] = {"checked": len(SOURCES), "missing": missing, "isaac_root_found": root.is_dir()}
        if not root.is_dir():
            result["note"] = "Isaac Sim root not found here; the facts are what 6.1.0-rc.26 shipped when checked."
    return result


# ------------------------------------------------------------------------------------------------ environment_check

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
_CLI_ALLOWED: tuple[tuple[str, ...], ...] = (("--help",), ("-h",))
_CLI_TIMEOUT = 8.0


def _default_runner(args: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - fixed argv, allowlist enforced by run_ros2_cli
        list(args), capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def run_ros2_cli(cli: str, args: Sequence[str] = ("--help",), runner: Runner | None = None,
                 env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Run one allowlisted, local-only ros2 query ('--help'). Anything else is refused before a process starts.
    Never raises for a missing CLI or a timeout; output is shortened and scrubbed of secret values."""
    args = [str(item) for item in args]
    if tuple(args) not in _CLI_ALLOWED:
        raise ValueError(f"ros2_check only runs 'ros2 --help'; refused: ros2 {' '.join(args[:3])}")
    runner = _default_runner if runner is None else runner
    try:
        proc = runner([cli, *args], _CLI_TIMEOUT)
    except FileNotFoundError:
        return {"ran": False, "ok": False, "returncode": None, "usage_seen": False, "error": "ros2 CLI not found"}
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "returncode": None, "usage_seen": False,
                "error": f"ros2 --help timed out after {_CLI_TIMEOUT:g}s"}
    except OSError as exc:
        return {"ran": False, "ok": False, "returncode": None, "usage_seen": False,
                "error": _short(f"{type(exc).__name__}: {exc}")}
    stdout = proc.stdout if isinstance(proc.stdout, str) else (proc.stdout or b"").decode("utf-8", "replace")
    stderr = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b"").decode("utf-8", "replace")
    result = {
        "ran": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "usage_seen": "usage: ros2" in (stdout + stderr).lower(),
        "first_line": _short((stdout or stderr).strip().splitlines()[0] if (stdout or stderr).strip() else "", 160),
        "error": _short(stderr) if proc.returncode else "",
    }
    return scrub(result, env)


def _safe_var(env: Mapping[str, str], name: str, pattern: re.Pattern[str] | None) -> dict[str, Any]:
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return {"set": False, "value": None}
    text = str(raw).strip()
    if pattern is not None and pattern.match(text):
        return {"set": True, "value": text}
    return {"set": True, "value": None, "note": "set, but not a normal value for this variable (not shown)"}


def _domain(env: Mapping[str, str]) -> dict[str, Any]:
    raw = str(env.get("ROS_DOMAIN_ID", "") or "").strip()
    if not raw:
        return {"set": False, "value": None, "effective": 0, "valid": True}
    if raw.isdigit() and int(raw) <= 232:
        number = int(raw)
        return {"set": True, "value": number, "effective": number, "valid": number <= 101 or 215 <= number <= 232}
    return {"set": True, "value": None, "effective": None, "valid": False,
            "note": "not a whole number from 0 to 232 (value not shown)"}


def _path_list(env: Mapping[str, str], name: str) -> dict[str, Any]:
    raw = str(env.get(name, "") or "").strip()
    entries = [item for item in raw.split(os.pathsep) if item.strip()] if raw else []
    return {"set": bool(raw), "entries": len(entries)}


def _toml_version(text: str | None) -> str | None:
    if not text:
        return None
    match = re.search(r'^\s*version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _scan_kit_files(isaac_root: Path) -> list[dict[str, Any]]:
    records = []
    apps = isaac_root / "apps"
    try:
        kit_files = sorted(apps.glob("*.kit")) if apps.is_dir() else []
    except OSError:
        kit_files = []
    for path in kit_files:
        text = _read_text(path)
        if not text or "isaacsim.ros2" not in text:
            continue
        section = ""
        startup: dict[str, str] = {}
        dependency_sections: list[str] = []
        settings: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.split("#", 1)[0].strip()
            header = re.match(r"^\[(.+)\]$", stripped)
            if header:
                section = header.group(1).strip()
                continue
            key = re.match(r'^isaac\.startup\.ros_bridge_extension\s*=\s*"([^"]*)"', stripped)
            if key:
                platform = re.search(r'"filter:platform"\."([^"]+)"', section)
                startup[platform.group(1) if platform else "all"] = key.group(1)
                continue
            if section.startswith("dependencies") and re.match(r'^"isaacsim\.ros2\.bridge"\s*=', stripped):
                dependency_sections.append(section)
                continue
            setting = re.match(r'^exts\."isaacsim\.ros2\.bridge"\.([A-Za-z_]+)\s*=\s*(.+)$', stripped)
            if setting:
                settings[setting.group(1)] = setting.group(2).strip()
        if not (startup or dependency_sections or settings):
            continue
        windows_value = startup.get("windows-x86_64", startup.get("all"))
        on_windows = bool(windows_value == BRIDGE_EXT) or any(
            "filter:platform" not in sec or "windows" in sec for sec in dependency_sections)
        records.append({
            "file": path.as_posix(),
            "startup_ros_bridge_extension": startup,
            "bridge_dependency_sections": dependency_sections,
            "bridge_settings": settings,
            "enables_bridge_on_windows": on_windows,
        })
    return records


def _default_user_config(env: Mapping[str, str]) -> Path | None:
    local = str(env.get("LOCALAPPDATA", "") or "").strip()
    return Path(local) / "ov" / "data" / "Kit" / "Isaac-Sim Full" / "6.1" / "user.config.json" if local else None


def _autoload(config_path: Path | None) -> dict[str, Any]:
    if config_path is None:
        return {"config_found": False, "path": None, "bridge_autoload": False}
    text = _read_text(config_path, limit=20_000_000)
    if text is None:
        return {"config_found": False, "path": config_path.as_posix(), "bridge_autoload": False}
    try:
        data = json.loads(text)
        enabled = data.get("persistent", {}).get("app", {}).get("exts", {}).get("enabled", [])
    except (ValueError, AttributeError):
        return {"config_found": True, "path": config_path.as_posix(), "bridge_autoload": False,
                "note": "user.config.json could not be parsed"}
    names = [str(item) for item in enabled] if isinstance(enabled, list) else []
    ros = sorted(item for item in names if item.startswith("isaacsim.ros2"))
    return {"config_found": True, "path": config_path.as_posix(), "key": "/persistent/app/exts/enabled",
            "bridge_autoload": any(item.split("-", 1)[0] == BRIDGE_EXT for item in ros), "ros_autoload_entries": ros}


def _user_launcher(workspace: Path) -> dict[str, Any]:
    path = workspace / "start-isaacsim.ps1"
    text = _read_text(path)
    if text is None:
        return {"found": False, "path": path.as_posix()}
    return {
        "found": True,
        "path": path.as_posix(),
        "uses_isaac_sim_bat": "isaac-sim.bat" in text,
        "passes_no_ros_env": "--no-ros-env" in text,
        "enables_bridge": BRIDGE_EXT in text,
    }


def _isaac_python(isaac_root: Path) -> str | None:
    try:
        for dll in sorted((isaac_root / "kit" / "python").glob("python3*.dll")):
            match = re.match(r"python3(\d+)\.dll$", dll.name)
            if match:
                return f"3.{match.group(1)}"
    except OSError:
        return None
    return None


def _install_check(isaac_root: Path) -> dict[str, Any]:
    ext_root = isaac_root / _EXT_DIR
    extensions = {}
    for name in EXTENSIONS:
        toml = ext_root / name / "config" / "extension.toml"
        version = _toml_version(_read_text(toml))
        extensions[name] = {"present": toml.is_file(), "version": version}
    core = ext_root / "isaacsim.ros2.core"
    distros = [d for d in BUNDLED_DISTROS if (core / d / "lib").is_dir()]
    rmw = {d: sorted(n for n in BUNDLED_RMW if (core / d / "lib" / f"{n}.dll").is_file()) for d in distros}
    setup_text = _read_text(isaac_root / "setup_ros_env.bat")
    setup = {"present": setup_text is not None}
    if setup_text:
        for key, label in (("DEFAULT_ROS_DISTRO", "default_ros_distro"), ("DEFAULT_RMW_IMPLEMENTATION", "default_rmw")):
            match = re.search(rf"^set {key}=(\S+)", setup_text, re.MULTILINE)
            setup[label] = match.group(1) if match else None
    return {
        "root": isaac_root.as_posix(),
        "found": isaac_root.is_dir(),
        "version_file": _short(_read_text(isaac_root / "VERSION") or "", 80) or None,
        "extensions": extensions,
        "bridge_present": extensions[BRIDGE_EXT]["present"],
        "bundled_distros": distros,
        "bundled_rmw": rmw,
        "zenoh_router_bundled": any((core / d / "lib" / "rmw_zenohd.exe").is_file() for d in distros),
        "setup_ros_env": setup,
        "python": _isaac_python(isaac_root),
    }


def environment_check(*, env: Mapping[str, str] | None = None, which: Callable[[str], str | None] | None = None,
                      runner: Runner | None = None, probe_cli: bool = False, isaac_root: str | Path | None = None,
                      user_config: str | Path | None = None, workspace: str | Path | None = None) -> dict[str, Any]:
    """Read-only ROS 2 readiness check of this PC. Only safe values are echoed (distro, RMW name, domain id); path
    variables are reported as set/entry counts and secret-looking values are scrubbed. With probe_cli=True and a ros2
    command on PATH, 'ros2 --help' is run once (allowlisted, 8 s timeout); nothing else is executed."""
    env = _env(env)
    which = shutil.which if which is None else which
    workspace_path = Path(workspace) if workspace else workspace_root()
    root = Path(isaac_root) if isaac_root else workspace_path / ISAAC_DIR_NAME
    cli_path = None
    for name in ("ros2",):
        found = which(name)
        if found:
            cli_path = str(found)
            break
    probe = None
    if cli_path and probe_cli:
        probe = run_ros2_cli(cli_path, ("--help",), runner, env)

    distro = _safe_var(env, "ROS_DISTRO", _DISTRO_RE)
    rmw = _safe_var(env, "RMW_IMPLEMENTATION", _RMW_RE)
    domain = _domain(env)
    localhost = _safe_var(env, "ROS_LOCALHOST_ONLY", re.compile(r"^[01]$"))
    discovery = _safe_var(env, "ROS_AUTOMATIC_DISCOVERY_RANGE", re.compile(r"^(SUBNET|LOCALHOST|OFF|SYSTEM_DEFAULT)$"))
    ros_version = _safe_var(env, "ROS_VERSION", re.compile(r"^[12]$"))
    ros_python = _safe_var(env, "ROS_PYTHON_VERSION", re.compile(r"^[23]$"))
    ament = _path_list(env, "AMENT_PREFIX_PATH")
    pixi = which("pixi")
    install = _install_check(root)
    kit_files = _scan_kit_files(root) if install["found"] else []
    config_path = Path(user_config) if user_config else _default_user_config(env)
    autoload = _autoload(config_path)
    launcher = _user_launcher(workspace_path)

    launcher_sets_env = bool(launcher.get("uses_isaac_sim_bat")) and not launcher.get("passes_no_ros_env")
    isaac_distro = distro["value"] or (install["setup_ros_env"].get("default_ros_distro") if launcher_sets_env
                                        else None) or ("humble" if not distro["set"] else None)
    isaac_rmw = rmw["value"] or (install["setup_ros_env"].get("default_rmw") if launcher_sets_env else None)
    prediction = {
        "launcher": "isaac-sim.bat (via start-isaacsim.ps1)" if launcher_sets_env else "unknown launcher",
        "ros_distro": isaac_distro,
        "rmw_implementation": isaac_rmw or "bundled default (not set by a launcher)",
        "uses_bundled_ros_libraries": not distro["set"],
        "domain_id": domain["effective"],
        "note": "What Isaac Sim will most likely use when started from this environment. ROS_DISTRO/RMW set in this "
                "shell would override the launcher defaults.",
    }

    enabled_in = []
    if any(record["enables_bridge_on_windows"] for record in kit_files):
        enabled_in += [record["file"] for record in kit_files if record["enables_bridge_on_windows"]]
    if autoload.get("bridge_autoload"):
        enabled_in.append(f"autoload list in {autoload['path']}")
    if launcher.get("enables_bridge"):
        enabled_in.append(f"--enable in {launcher['path']}")

    needs: list[dict[str, Any]] = []

    def need(level: str, code: str, what: str, how: str) -> None:
        needs.append({"severity": level, "code": code, "what": what, "how": how})

    if not install["found"]:
        need("error", "isaac_not_found", f"Isaac Sim was not found at {root.as_posix()}.",
             "Pass isaac_root= with the folder that holds isaac-sim.bat.")
    elif not install["bridge_present"]:
        need("error", "bridge_missing", "The isaacsim.ros2.bridge extension is not in this Isaac Sim install.",
             "Use the full Isaac Sim 6.1 package; the bridge ships in exts/isaacsim.ros2.bridge.")
    if install["found"] and install["bridge_present"] and not enabled_in:
        need("action", "bridge_not_enabled",
             "The ROS 2 Bridge is installed but not enabled at startup on Windows (the full .kit leaves it off).",
             "In Isaac Sim: Window > Extensions > search 'ROS 2 Bridge' > toggle on (tick AUTOLOAD to keep it), or "
             "launch with --enable isaacsim.ros2.bridge. Needs your approval: it changes how Isaac starts.")
    if not cli_path:
        need("action", "ros_cli_missing",
             "No 'ros2' command on PATH, so you cannot list or echo topics from this shell yet.",
             "Isaac Sim publishes without it. To verify topics on Windows, NVIDIA's route is a ROS 2 Jazzy Pixi "
             "workspace (skills/isaac-sim-ros-workspaces: setup_pixi_workspace.ps1; needs MSVC Build Tools 2022). "
             "This is a large install - only with your approval. Then run commands inside 'pixi shell'.")
    elif probe is not None and not probe.get("ok"):
        need("warning", "ros_cli_broken", "A 'ros2' command exists but 'ros2 --help' failed.",
             "Open the shell where ROS 2 is set up (for Pixi: 'pixi shell' in the workspace) and try again.")
    if distro["set"] and distro["value"] and distro["value"] not in BUNDLED_DISTROS:
        need("warning", "distro_experimental", f"ROS_DISTRO={distro['value']} is not humble or jazzy.",
             "Isaac Sim 6.1 treats other distros as experimental; use jazzy on Windows.")
    if distro["value"] == "humble":
        need("warning", "humble_on_windows", "ROS_DISTRO=humble: Windows Humble is a deprecated flow for Isaac 6.1.",
             "Prefer jazzy (the launcher default) on Windows.")
    if distro["set"] and not ament["set"]:
        need("warning", "distro_without_ament",
             "ROS_DISTRO is set but AMENT_PREFIX_PATH is empty, so Isaac will look for a sourced ROS install and "
             "will not use its bundled libraries.",
             "Unset ROS_DISTRO before starting Isaac Sim, or start it from a fully sourced ROS 2 shell.")
    if not distro["set"] and not launcher_sets_env:
        need("info", "no_launcher_env", "ROS_DISTRO is not set and isaac-sim.bat is not confirmed as the launcher.",
             "Start Isaac with isaac-sim.bat (or start-isaacsim.ps1) so setup_ros_env.bat picks jazzy; kit.exe alone "
             "falls back to the bundled humble libraries.")
    effective_rmw = isaac_rmw if isinstance(isaac_rmw, str) and isaac_rmw.startswith("rmw_") else None
    if effective_rmw == "rmw_zenoh_cpp":
        need("info", "zenoh_router",
             "Isaac Sim will use rmw_zenoh_cpp (launcher default). ROS 2 tools must use the same RMW, and Zenoh needs "
             "a router running (none ships with Isaac Sim).",
             "In the ROS 2 shell: set RMW_IMPLEMENTATION=rmw_zenoh_cpp and run 'ros2 run rmw_zenoh_cpp rmw_zenohd' "
             "first - or set RMW_IMPLEMENTATION=rmw_fastrtps_cpp in BOTH shells before launching.")
    if rmw["set"] and rmw["value"] and rmw["value"] not in BUNDLED_RMW:
        need("warning", "rmw_not_bundled", f"RMW_IMPLEMENTATION={rmw['value']} is not one Isaac bundles.",
             f"Use one of {', '.join(BUNDLED_RMW)} or source a ROS install that provides it.")
    if not domain["valid"]:
        need("warning", "bad_domain_id", "ROS_DOMAIN_ID is outside the usual 0-101 / 215-232 range or not a number.",
             "Use a small whole number such as 0 on both sides.")

    router_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    python = {
        "router": router_python,
        "isaac_sim": install["python"],
        "ros_python_version_env": ros_python["value"],
        "note": "Isaac Sim's Python is 3.12: custom ROS message packages for Isaac's rclpy must be built for Python "
                "3.12 (Jazzy Pixi on Windows uses its own Python environment).",
    }
    ready_isaac = bool(install["found"] and install["bridge_present"])
    ready_ros = bool(cli_path) and (probe is None or bool(probe.get("ok")))
    story_bits = []
    story_bits.append("Isaac Sim's ROS 2 Bridge is installed" if ready_isaac else "Isaac Sim's ROS 2 Bridge was not found")
    story_bits.append("and enabled at startup" if enabled_in else "but not switched on at startup yet")
    story = " ".join(story_bits) + ". "
    story += (f"Isaac will most likely use ROS 2 {isaac_distro} with {prediction['rmw_implementation']} on domain "
              f"{domain['effective']}. ")
    story += ("A ros2 command is available here." if cli_path else
              "There is no ros2 command on this PC yet, so topics can be published by Isaac but not checked from a "
              "terminal.")
    result = {
        "ok": ready_isaac,
        "ready": {"isaac_side": ready_isaac, "ros_side": ready_ros, "bridge_enabled_at_startup": bool(enabled_in)},
        "ros_cli_found": bool(cli_path),
        "ros_cli_path": cli_path,
        "ros_cli_probe": probe,
        "pixi_found": bool(pixi),
        "ros_distro": distro,
        "rmw_implementation": rmw,
        "ros_domain_id": domain,
        "ros_localhost_only": localhost,
        "ros_automatic_discovery_range": discovery,
        "ros_version": ros_version,
        "ament_prefix_path": ament,
        "python": python,
        "isaac": install,
        "kit_files": kit_files,
        "autoload": autoload,
        "user_launcher": launcher,
        "enabled_in": enabled_in,
        "isaac_will_use": prediction,
        "needs": needs,
        "story": story,
        "privacy": "Only ROS_DISTRO, RMW_IMPLEMENTATION, ROS_DOMAIN_ID and similar short, validated values are shown; "
                   "path variables are counted, and secret-looking values are redacted.",
    }
    return scrub(result, env)


# ------------------------------------------------------------------------------------------------ graph_check


def _require_pxr() -> None:
    if _PXR_IMPORT_ERROR is not None:
        raise ImportError("USD Python bindings (pxr) are required for graph_check.") from _PXR_IMPORT_ERROR


def _resolve_topic(topic: str, namespace: str) -> str:
    topic = str(topic or "").strip()
    if topic.startswith("/"):
        return topic
    if topic.startswith("~"):
        topic = topic.lstrip("~/")
    ns = str(namespace or "").strip().strip("/")
    return "/" + "/".join(part for part in (ns, topic) if part)


def _applied(prim: Any) -> list[str]:
    try:
        return [str(name) for name in prim.GetAppliedSchemas()]
    except Exception:  # noqa: BLE001
        return []


def _articulation_root_near(stage: Any, path: str) -> str | None:
    """The articulation root at, below or above path (Isaac accepts the robot prim or its root link)."""
    prim = stage.GetPrimAtPath(path)
    if not prim or not prim.IsValid():
        return None
    for item in Usd.PrimRange(prim):
        if any(name.startswith(_ROOT_API) for name in _applied(item)):
            return str(item.GetPath())
    parent = prim.GetParent()
    while parent and parent.IsValid() and not parent.IsPseudoRoot():
        if any(name.startswith(_ROOT_API) for name in _applied(parent)):
            return str(parent.GetPath())
        parent = parent.GetParent()
    return None


class _GraphRun:
    def __init__(self, stage: Any, root: Any, max_nodes: int) -> None:
        from . import omnigraph_helper as ogh  # pxr-only reader of OmniGraph prims (same package)

        self.ogh = ogh
        self.stage = stage
        self.nodes: dict[str, Any] = {}
        self.graph_of: dict[str, str | None] = {}
        self.graphs: dict[str | None, list[str]] = {}
        self.graph_meta: dict[str | None, dict[str, Any]] = {}
        self.truncated = False
        self.problems: list[dict[str, Any]] = []
        for prim in Usd.PrimRange(root):
            if ogh._is_graph(prim) and not ogh._is_node(prim):
                evaluator = prim.GetAttribute("evaluator:type")
                self.graph_meta[str(prim.GetPath())] = {
                    "evaluator": str(evaluator.Get()) if evaluator and evaluator.HasAuthoredValue() else None}
                continue
            if not ogh._is_node(prim):
                continue
            if len(self.nodes) >= max_nodes:
                self.truncated = True
                break
            graph_path = None
            parent = prim.GetParent()
            while parent and parent.IsValid() and not parent.IsPseudoRoot():
                if ogh._is_graph(parent) and not ogh._is_node(parent):
                    graph_path = str(parent.GetPath())
                    break
                parent = parent.GetParent()
            node = ogh._Node(prim, graph_path)
            self.nodes[node.path] = node
            self.graph_of[node.path] = graph_path
            self.graphs.setdefault(graph_path, []).append(node.path)

    # -- helpers
    def wired_from(self, node: Any, local: str) -> list[Any]:
        return [self.nodes[w["from_node"]] for w in node.wires_into(local) if w["from_node"] in self.nodes]

    def consumers(self, path: str) -> list[str]:
        out = []
        for other in self.nodes.values():
            for wire in other.in_wires:
                if wire["from_node"] == path and not wire["from_attr"].endswith("execOut"):
                    out.append(other.path)
        return out

    def trigger(self, node: Any, exec_input: str) -> dict[str, Any]:
        wires = node.wires_into(exec_input)
        if not wires:
            return {"wired": False, "root": None, "root_type": None, "kind": "none"}
        seen: set[str] = set()
        current = self.nodes.get(wires[0]["from_node"])
        for _ in range(30):
            if current is None or current.path in seen:
                break
            seen.add(current.path)
            if current.type in _TICK_TYPES:
                only = current.value("onlyPlayback")
                return {"wired": True, "root": current.path, "root_type": current.type,
                        "kind": "tick", "only_playback": None if only is None else bool(only)}
            tail = (current.type or "").rsplit(".", 1)[-1]
            if re.match(r"On[A-Z]", tail):
                return {"wired": True, "root": current.path, "root_type": current.type, "kind": "event"}
            upstream = [w for w in current.in_wires
                        if "exec" in w["input"].lower() or w["input"] == "inputs:onReceived"]
            current = self.nodes.get(upstream[0]["from_node"]) if upstream else None
        return {"wired": True, "root": None, "root_type": None, "kind": "unknown"}


def _value(node: Any, local: str, default: Any = None) -> Any:
    value = node.value(local)
    return default if value is None else value


def graph_check(stage: Any, root_path: str | None = None, *, max_nodes: int = 2000) -> dict[str, Any]:
    """Read-only check of the ROS 2 bridge nodes saved in OmniGraph prims of a USD stage (pure pxr, no Kit).

    Returns {ok, summary, story, publishers, subscribers, sensor_helpers, services, contexts, clock_publisher,
    graphs, problems: [{severity, code, node, message, fix}], counts, verify_commands, truncated}. ok is False when an
    error-level problem was found. Nothing is edited or saved.
    """
    _require_pxr()
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise ValueError("max_nodes must be a whole number, 1 or more")
    root = stage.GetPseudoRoot()
    if root_path:
        prim = stage.GetPrimAtPath(str(root_path)) if _PRIM_PATH_RE.match(str(root_path)) else None
        if not prim or not prim.IsValid():
            return {"ok": False, "error": f"No prim at root_path {root_path!r}.", "root_path": str(root_path)}
        root = prim
    run = _GraphRun(stage, root, max_nodes)
    problems = run.problems
    records: list[dict[str, Any]] = []
    contexts: list[dict[str, Any]] = []
    ros_graphs: set[str | None] = set()

    for node in run.nodes.values():
        node_type = node.type or ""
        if node_type.startswith(_LEGACY_PREFIXES):
            new_id = _B + node_type.rsplit(".", 1)[-1]
            ros_graphs.add(node.graph_path)
            problems.append(_problem(
                "error", "legacy_node_type", node.path,
                f"{node.name} uses the old node id '{node_type}' (Isaac Sim 2023.x); Isaac Sim 6.1 does not register it.",
                f"Recreate it as '{new_id}' (or rebuild the graph from Tools > Robotics > ROS 2 OmniGraphs)."))
            continue
        if not node_type.startswith(("isaacsim.ros2.",)):
            continue
        ros_graphs.add(node.graph_path)
        info = NODE_TYPES.get(node_type)
        if info is None:
            problems.append(_problem("warning", "unknown_ros2_node", node.path,
                                     f"'{node_type}' is not a ROS 2 node type known in Isaac Sim 6.1.",
                                     "Check the spelling against ros2_info()['node_types']."))
            continue
        if info["kind"] == "context":
            contexts.append({"node": node.path, "graph": node.graph_path,
                             "domain_id": int(_value(node, "domain_id", 0) or 0),
                             "use_domain_id_env_var": bool(_value(node, "useDomainIDEnvVar", True))})
            continue
        if info["kind"] in ("qos", "config"):
            continue
        namespace = str(_value(node, "nodeNamespace", "") or "")
        topic = _value(node, "topicName", info["default_topic"])
        message = info["message_type"]
        helper_type = None
        if node_type == _B + "ROS2CameraHelper":
            helper_type = str(_value(node, "type", "rgb"))
            message = CAMERA_HELPER_TYPES.get(helper_type)
            if message is None:
                problems.append(_problem("error", "bad_helper_type", node.path,
                                         f"Camera helper type '{helper_type}' is not one Isaac 6.1 accepts.",
                                         f"Use one of: {', '.join(CAMERA_HELPER_TYPES)}."))
        elif node_type == _B + "ROS2RtxLidarHelper":
            helper_type = str(_value(node, "type", "laser_scan"))
            message = LIDAR_HELPER_TYPES.get(helper_type)
            if message is None:
                problems.append(_problem("error", "bad_helper_type", node.path,
                                         f"Lidar helper type '{helper_type}' must be 'laser_scan' or 'point_cloud'.",
                                         "Set inputs:type to laser_scan (2D scan) or point_cloud."))
        elif node_type in (_B + "ROS2Publisher", _B + "ROS2Subscriber"):
            package, name = _value(node, "messagePackage", ""), _value(node, "messageName", "")
            sub = _value(node, "messageSubfolder", "msg")
            message = f"{package}/{sub}/{name}" if package and name else None
            if not message:
                problems.append(_problem("error", "generic_no_message_type", node.path,
                                         f"{node.name} has no message type (messagePackage / messageName are empty).",
                                         "Set messagePackage (e.g. std_msgs) and messageName (e.g. String)."))
        record: dict[str, Any] = {
            "node": node.path, "name": node.name, "graph": node.graph_path, "type": node_type, "ui": info["ui"],
            "kind": info["kind"], "namespace": namespace or None, "message_type": message, "helper_type": helper_type,
        }
        if info["kind"] != "service" and info["kind"] != "control":
            record["topic"] = topic
            record["resolved_topic"] = _resolve_topic(topic, namespace) if topic else None
            if topic and not _TOPIC_RE.match(str(topic)):
                problems.append(_problem("error", "invalid_topic_name", node.path,
                                         f"Topic name '{topic}' is not a valid ROS 2 name.",
                                         "Use letters, digits, '_' and '/', not starting with a digit (e.g. 'joint_states')."))
            if namespace and not _NAMESPACE_RE.match(namespace):
                problems.append(_problem("error", "invalid_namespace", node.path,
                                         f"Namespace '{namespace}' is not a valid ROS 2 namespace.",
                                         "Use something like 'robot1' or '/robot1'."))
        frames = {}
        for local in info["frame_inputs"]:
            value = _value(node, local, None)
            frames[local] = value
            if isinstance(value, str) and value.startswith("/"):
                problems.append(_problem("warning", "frame_leading_slash", node.path,
                                         f"Frame id '{value}' starts with '/'; tf2 rejects frame ids with a leading slash.",
                                         f"Use '{value.lstrip('/')}'."))
        if frames:
            record["frame_ids"] = frames
        record["targets"] = {local: node.targets.get(local, []) for local in info["target_inputs"]}
        # -- trigger
        if info["exec_input"]:
            record["trigger"] = run.trigger(node, info["exec_input"])
        # -- context
        if info["has_context"]:
            sources = run.wired_from(node, "context")
            record["context"] = sources[0].path if sources else None
        records.append(record)
        _node_checks(run, node, info, record, problems)

    # -- graph-level checks
    for graph_path in sorted(ros_graphs, key=lambda item: item or ""):
        members = [run.nodes[path] for path in run.graphs.get(graph_path, [])]
        ros_members = [n for n in members if (n.type or "").startswith("isaacsim.ros2.")]
        graph_contexts = [c for c in contexts if c["graph"] == graph_path]
        needs_context = [n for n in ros_members if NODE_TYPES.get(n.type or "", {}).get("has_context")]
        if needs_context and not graph_contexts:
            problems.append(_problem(
                "warning", "missing_context", graph_path,
                f"Graph {graph_path} has ROS 2 nodes but no ROS2Context node, so they use the default context "
                "(domain from ROS_DOMAIN_ID, else 0) and you cannot see or change the domain in the graph.",
                "Add isaacsim.ros2.bridge.ROS2Context and wire outputs:context into every ROS 2 node's inputs:context."))
        elif graph_contexts:
            for record in records:
                if record["graph"] == graph_path and "context" in record and record["context"] is None:
                    problems.append(_problem(
                        "warning", "context_not_wired", record["node"],
                        f"{record['name']} is not wired to the graph's ROS2Context, so it may run on a different "
                        "domain than the other nodes.",
                        f"Connect {graph_contexts[0]['node']}.outputs:context to {record['node']}.inputs:context."))
        exec_members = [n for n in ros_members if NODE_TYPES.get(n.type or "", {}).get("exec_input")]
        has_tick = any(n.type in _TICK_TYPES for n in members)
        if exec_members and not has_tick:
            problems.append(_problem(
                "error", "missing_playback_tick", graph_path,
                f"Graph {graph_path} has no On Playback Tick (or On Tick / On Physics Step) node, so its ROS 2 nodes "
                "never run.",
                "Add omni.graph.action.OnPlaybackTick and wire outputs:tick into each ROS 2 node's inputs:execIn.",
                nodes=[n.path for n in exec_members]))
        elif has_tick:
            for record in records:
                trig = record.get("trigger")
                if record["graph"] == graph_path and trig is not None and not trig["wired"]:
                    problems.append(_problem(
                        "error", "not_triggered", record["node"],
                        f"Nothing drives {record['name']}.inputs:execIn, so it never publishes or reads anything.",
                        "Wire OnPlaybackTick.outputs:tick (or the execOut of the node before it) into inputs:execIn."))
                elif record["graph"] == graph_path and trig is not None and trig["kind"] == "event":
                    problems.append(_problem(
                        "info", "event_triggered", record["node"],
                        f"{record['name']} only runs when {trig['root_type']} fires, not every frame.",
                        "That is fine for one-off messages; streams (joint states, clock, sensors) need OnPlaybackTick."))
        evaluator = run.graph_meta.get(graph_path or "", {}).get("evaluator")
        if graph_path and evaluator and evaluator != "execution":
            problems.append(_problem("info", "not_action_graph", graph_path,
                                     f"Graph evaluator is '{evaluator}'; Isaac's ROS 2 tools build Action Graphs "
                                     "('execution').", "Recreate it as an Action Graph if the exec wires do nothing."))
    domains = {(c["domain_id"], c["use_domain_id_env_var"]) for c in contexts}
    if len({d for d, _ in domains}) > 1:
        problems.append(_problem("warning", "mixed_domains", None,
                                 "ROS2Context nodes use different domain ids: "
                                 + ", ".join(f"{c['node']}={c['domain_id']}" for c in contexts) + ".",
                                 "Use one domain id everywhere unless you separate robots on purpose."))

    publishers = [r for r in records if r["kind"] in ("publisher", "sensor_helper")]
    subscribers = [r for r in records if r["kind"] == "subscriber"]
    services = [r for r in records if r["kind"] in ("service", "control")]
    # -- duplicate topics
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for record in publishers:
        if record.get("resolved_topic"):
            by_topic.setdefault(record["resolved_topic"], []).append(record)
    for topic, items in sorted(by_topic.items()):
        if len(items) < 2:
            continue
        types = {item["message_type"] for item in items if item["message_type"]}
        if len(types) > 1:
            problems.append(_problem("error", "topic_type_conflict", items[0]["node"],
                                     f"Topic {topic} is published with different message types: {', '.join(sorted(types))}.",
                                     "Give each publisher its own topic name.", nodes=[i["node"] for i in items]))
        elif topic not in _TF_TOPICS:
            problems.append(_problem("warning", "duplicate_topic", items[0]["node"],
                                     f"{len(items)} nodes publish on {topic}; their messages interleave and "
                                     "subscribers cannot tell them apart.",
                                     "Rename one topic or put the nodes in different namespaces (inputs:nodeNamespace).",
                                     nodes=[i["node"] for i in items]))
    clock = any(r["type"] == _B + "ROS2PublishClock" for r in publishers)
    if publishers and not clock:
        problems.append(_problem("info", "no_clock", None,
                                 "No ROS2PublishClock: ROS 2 nodes started with use_sim_time:=true will wait for /clock.",
                                 "Add a clock graph (setup_plan('clock')) if your ROS side uses simulation time."))

    severity = {lvl: sum(1 for p in problems if p["severity"] == lvl) for lvl in ("error", "warning", "info")}
    topics_out = sorted({r["resolved_topic"] for r in publishers if r.get("resolved_topic")})
    topics_in = sorted({r["resolved_topic"] for r in subscribers if r.get("resolved_topic")})
    if not records and not contexts and not ros_graphs:
        summary = "No ROS 2 bridge nodes were found" + (f" under {root_path}." if root_path else " in this stage.")
        story = summary + " Use ros2_setup_plan(...) to add one."
    else:
        summary = (f"Found {len(publishers)} publisher(s), {len(subscribers)} subscriber(s), {len(services)} service "
                   f"node(s) and {len(contexts)} ROS2Context node(s) in {len(ros_graphs)} graph(s); "
                   f"{severity['error']} error(s), {severity['warning']} warning(s).")
        story = "When you press Play, Isaac Sim "
        story += ("will publish " + ", ".join(
            f"{r['resolved_topic']} ({r['message_type'] or 'see node'})" for r in publishers if r.get("resolved_topic"))
            if publishers else "publishes nothing")
        story += ("; it listens on " + ", ".join(topics_in) if topics_in else "; it subscribes to nothing")
        domain_text = ", ".join(str(c["domain_id"]) + (" (or ROS_DOMAIN_ID)" if c["use_domain_id_env_var"] else "")
                                for c in contexts) or "ROS_DOMAIN_ID or 0 (default context)"
        story += f". Domain: {domain_text}."
        if severity["error"]:
            story += f" Fix the {severity['error']} error(s) first - those nodes will not work."
    verify = ["ros2 topic list"] + [f"ros2 topic echo {t} --once" for t in topics_out[:12]]
    if topics_out:
        verify.append(f"ros2 topic hz {topics_out[0]}")
    return {
        "ok": severity["error"] == 0,
        "root_path": str(root_path) if root_path else None,
        "summary": summary,
        "story": story,
        "publishers": publishers,
        "subscribers": subscribers,
        "sensor_helpers": [r for r in publishers if r["kind"] == "sensor_helper"],
        "services": services,
        "contexts": contexts,
        "clock_publisher": clock,
        "topics_published": topics_out,
        "topics_subscribed": topics_in,
        "graphs": [{"path": g, "nodes": run.graphs.get(g, []),
                    "evaluator": run.graph_meta.get(g or "", {}).get("evaluator")}
                   for g in sorted(ros_graphs, key=lambda item: item or "")],
        "problems": problems,
        "counts": {"nodes_scanned": len(run.nodes), "ros_nodes": len(records) + len(contexts), **severity},
        "verify_commands": verify,
        "verify_note": "Run these in a ROS 2 shell (same RMW_IMPLEMENTATION and ROS_DOMAIN_ID as Isaac) while Isaac "
                       "Sim is playing. Text only: nothing was run.",
        "truncated": run.truncated,
        "node_types_source": f"Isaac Sim {ISAAC_VERSION}: {SOURCES['node_ids']}",
    }


def _check_robot(run: _GraphRun, node: Any, paths: Iterable[str], problems: list[dict[str, Any]], what: str) -> None:
    for path in paths:
        prim = run.stage.GetPrimAtPath(path) if _PRIM_PATH_RE.match(str(path)) else None
        if not prim or not prim.IsValid():
            problems.append(_problem("error", "target_missing", node.path,
                                     f"{node.name} points at {path}, but no prim exists there.",
                                     "Pick the robot prim again (the path may have changed)."))
            continue
        if _articulation_root_near(run.stage, str(path)) is None:
            problems.append(_problem(
                "error", "not_articulation", node.path,
                f"{node.name} {what} {path}, which is not an articulation (no PhysicsArticulationRootAPI on it, "
                "below it or above it), so there are no joints to read or drive.",
                "Point it at the robot prim that has Articulation Root (Add > Physics > Articulation Root), "
                "for example the prim robot_inspector reports."))


def _node_checks(run: _GraphRun, node: Any, info: dict[str, Any], record: dict[str, Any],
                 problems: list[dict[str, Any]]) -> None:
    node_type = node.type
    if node_type == _B + "ROS2PublishJointState":
        readers = [n for n in run.wired_from(node, "jointNames") if n.type == _READ_JOINT_STATE]
        if node.wires_into("jointNames"):
            robot_paths = [p for reader in readers for p in reader.targets.get("prim", [])]
            if readers and not robot_paths:
                problems.append(_problem("error", "joint_state_no_robot", readers[0].path,
                                         f"{readers[0].name} (Isaac Read Joint State) has no robot in inputs:prim.",
                                         "Set inputs:prim to the robot's articulation root."))
            _check_robot(run, node, robot_paths, problems, "reads joints from")
            record["robot"] = robot_paths
        else:
            robot_paths = node.targets.get("targetPrim", [])
            if not robot_paths:
                problems.append(_problem("error", "joint_state_no_robot", node.path,
                                         f"{node.name} has no targetPrim and no Isaac Read Joint State wired in, so it "
                                         "has no robot to publish.",
                                         "Wire Isaac Read Joint State outputs into it (preferred in 6.1) or set "
                                         "inputs:targetPrim to the robot."))
            _check_robot(run, node, robot_paths, problems, "targets")
            record["robot"] = robot_paths
        if not node.wires_into("timeStamp") and not node.wires_into("sensorTime"):
            problems.append(_problem("info", "timestamp_zero", node.path,
                                     f"{node.name} has no time wired in, so messages carry time 0.",
                                     "Wire Isaac Read Simulation Time.outputs:simulationTime into inputs:timeStamp."))
    elif node_type == _B + "ROS2PublishClock":
        if not node.wires_into("timeStamp") and not _value(node, "timeStamp", 0):
            problems.append(_problem("error", "clock_no_time", node.path,
                                     f"{node.name}.inputs:timeStamp is not wired, so /clock stays at 0.",
                                     "Wire Isaac Read Simulation Time.outputs:simulationTime into inputs:timeStamp."))
    elif node_type == _B + "ROS2PublishTransformTree":
        targets = node.targets.get("targetPrims", [])
        if not targets and not node.wires_into("childFrames") and not node.wires_into("targetPrims"):
            problems.append(_problem("error", "tf_no_targets", node.path,
                                     f"{node.name} has no targetPrims, so it publishes no transforms.",
                                     "Set inputs:targetPrims to the prims (robot, camera, lidar) whose poses ROS needs."))
        for path in targets + node.targets.get("parentPrim", []):
            prim = run.stage.GetPrimAtPath(path) if _PRIM_PATH_RE.match(str(path)) else None
            if not prim or not prim.IsValid():
                problems.append(_problem("error", "target_missing", node.path,
                                         f"{node.name} points at {path}, but no prim exists there.",
                                         "Pick the prim again."))
    elif info["kind"] == "subscriber" and node_type in (_B + "ROS2SubscribeJointState", _B + "ROS2SubscribeTwist",
                                                         _B + "ROS2SubscribeAckermannDrive"):
        if not run.consumers(node.path):
            problems.append(_problem("warning", "subscriber_unused", node.path,
                                     f"{node.name} receives commands but none of its outputs are wired, so nothing "
                                     "moves.",
                                     "Wire its outputs into an Isaac Articulation Controller (joint commands) or a "
                                     "Differential Controller (cmd_vel)."))
    if info["needs_render_product"]:
        sources = run.wired_from(node, "renderProductPath")
        value = _value(node, "renderProductPath", "")
        record["render_product"] = sources[0].path if sources else (value or None)
        if not sources and not value:
            problems.append(_problem("error", "no_render_product", node.path,
                                     f"{node.name} has no render product, so it has no camera/sensor image to publish.",
                                     "Add Isaac Create Render Product (cameraPrim = your camera or lidar) and wire "
                                     "outputs:renderProductPath and outputs:execOut into this helper."))
        for source in sources:
            if source.type != _CREATE_RENDER_PRODUCT:
                continue
            cameras = source.targets.get("cameraPrim", [])
            if not cameras:
                problems.append(_problem("error", "no_sensor_prim", source.path,
                                         f"{source.name} (Create Render Product) has no cameraPrim.",
                                         "Set inputs:cameraPrim to the camera or RTX lidar prim."))
            for path in cameras:
                prim = run.stage.GetPrimAtPath(path) if _PRIM_PATH_RE.match(str(path)) else None
                if not prim or not prim.IsValid():
                    problems.append(_problem("error", "target_missing", source.path,
                                             f"{source.name}.inputs:cameraPrim points at {path}, but no prim exists there.",
                                             "Pick the sensor prim again."))
                    continue
                type_name = prim.GetTypeName()
                expect = "OmniLidar" if "Lidar" in (node_type or "") else "OmniRadar" if "Radar" in (node_type or "") \
                    else "Camera"
                if type_name != expect:
                    problems.append(_problem("warning", "render_product_wrong_sensor", node.path,
                                             f"{node.name} expects a {expect} prim but its render product looks "
                                             f"through {path} ({type_name or 'untyped'}).",
                                             f"Point Create Render Product at a {expect} prim."))
    if info["target_inputs"] and node_type == "isaacsim.ros2.control.ROS2ControlManager":
        _check_robot(run, node, node.targets.get("targetPrim", []), problems, "controls")
    # articulation controllers fed by this subscriber
    if info["kind"] == "subscriber":
        for consumer_path in run.consumers(node.path):
            consumer = run.nodes.get(consumer_path)
            if consumer is not None and consumer.type == _ART_CONTROLLER:
                paths = consumer.targets.get("targetPrim", []) or (
                    [str(consumer.value("robotPath"))] if consumer.value("robotPath") else [])
                if not paths:
                    problems.append(_problem("error", "controller_no_robot", consumer.path,
                                             f"{consumer.name} (Articulation Controller) has no targetPrim.",
                                             "Set inputs:targetPrim to the robot's articulation root."))
                _check_robot(run, consumer, paths, problems, "drives")


# ------------------------------------------------------------------------------------------------ setup_plan

GOALS: dict[str, dict[str, Any]] = {
    "publish_joint_states": {
        "title": "Publish a robot's joint states on /joint_states",
        "params": {"robot_prim": "articulation root of the robot (default /World/robot)",
                   "graph_path": "default /World/ROS2/JointStates", "topic": "default joint_states",
                   "namespace": "optional node namespace", "subscribe": "also add /joint_command -> robot (default False)",
                   "command_topic": "default joint_command"},
        "menu": "Tools > Robotics > ROS 2 OmniGraphs > Joint States",
    },
    "camera": {
        "title": "Publish a camera's RGB, depth and camera_info",
        "params": {"camera_prim": "a Camera prim (default /World/Camera)", "graph_path": "default /World/ROS2/Camera",
                   "rgb_topic": "default rgb", "depth": "also publish depth (default True)", "depth_topic": "default depth",
                   "info_topic": "default camera_info", "frame_id": "default sim_camera", "width": "default 640",
                   "height": "default 480", "namespace": "optional"},
        "menu": "Tools > Robotics > ROS 2 OmniGraphs > Camera",
    },
    "lidar": {
        "title": "Publish an RTX lidar as LaserScan (and optionally PointCloud2)",
        "params": {"lidar_prim": "new lidar path (default /World/Lidar)", "config": "default Example_Rotary_2D",
                   "graph_path": "default /World/ROS2/Lidar", "topic": "default scan", "frame_id": "default sim_lidar",
                   "point_cloud": "also publish point_cloud (default False)", "namespace": "optional"},
        "menu": "Tools > Robotics > ROS 2 OmniGraphs > RTX Lidar",
    },
    "clock": {
        "title": "Publish simulation time on /clock",
        "params": {"graph_path": "default /World/ROS2/Clock", "topic": "default clock"},
        "menu": "Tools > Robotics > ROS 2 OmniGraphs > Clock",
    },
    "teleop": {
        "title": "Drive a two-wheeled robot from /cmd_vel (keyboard teleop)",
        "params": {"robot_prim": "articulation root (default /World/robot)",
                   "left_wheel_joint": "default left_wheel", "right_wheel_joint": "default right_wheel",
                   "wheel_radius": "metres (default 0.24 - CHANGE to your robot)",
                   "wheel_distance": "metres between wheels (default 0.5 - CHANGE)",
                   "graph_path": "default /World/ROS2/Teleop", "topic": "default cmd_vel", "namespace": "optional"},
        "menu": None,
    },
}
_GOAL_ALIASES = {"joint_states": "publish_joint_states", "jointstate": "publish_joint_states",
                 "joint_state": "publish_joint_states", "rgb": "camera", "scan": "lidar", "laser_scan": "lidar",
                 "cmd_vel": "teleop", "drive": "teleop"}

_COMMON_STEPS = [
    "Save a copy of your scene first (File > Save As) - the script adds a new graph to the open stage.",
    "Make sure the ROS 2 Bridge extension is enabled (environment_check() 'bridge_not_enabled').",
    "Press Stop (not Pause) before running the script.",
    "Open Window > Script Editor, paste the script, press Run. Ctrl+Z undoes it.",
    "Press Play: ROS 2 nodes only run while the timeline plays.",
]
_ROS_SHELL_STEPS = [
    "Open a ROS 2 Jazzy shell (Pixi: 'pixi shell' in the workspace folder).",
    "Match Isaac's middleware: Isaac's launcher defaults to RMW_IMPLEMENTATION=rmw_zenoh_cpp - either run "
    "'ros2 run rmw_zenoh_cpp rmw_zenohd' in another ROS shell first, or set RMW_IMPLEMENTATION=rmw_fastrtps_cpp in "
    "BOTH shells before starting Isaac and the ROS tools.",
    "Use the same ROS_DOMAIN_ID on both sides (unset = 0).",
]


def _check_params(params: Mapping[str, Any]) -> str | None:
    for key, value in params.items():
        if key.endswith("_prim") or key == "graph_path":
            if not isinstance(value, str) or not _PRIM_PATH_RE.match(value):
                return f"{key} must be an absolute prim path like /World/robot, got {value!r}"
        elif key.endswith("topic"):
            if not isinstance(value, str) or not _TOPIC_RE.match(value):
                return f"{key} must be a ROS 2 topic name like 'joint_states', got {value!r}"
        elif key == "namespace":
            if not isinstance(value, str) or not _NAMESPACE_RE.match(value):
                return f"namespace must look like 'robot1', got {value!r}"
        elif key in ("frame_id", "left_wheel_joint", "right_wheel_joint", "config"):
            if not isinstance(value, str) or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
                return f"{key} must be a simple name (letters, digits, _), got {value!r}"
        elif key in ("width", "height"):
            if isinstance(value, bool) or not isinstance(value, int) or not 16 <= value <= 8192:
                return f"{key} must be a whole number of pixels (16-8192), got {value!r}"
        elif key in ("wheel_radius", "wheel_distance"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) < 100:
                return f"{key} must be a positive number of metres, got {value!r}"
        elif key in ("subscribe", "depth", "point_cloud"):
            if not isinstance(value, bool):
                return f"{key} must be True or False, got {value!r}"
    return None


def _header(title: str, graph_path: str) -> list[str]:
    return [
        f"# {title}",
        "# Run in Isaac Sim 6.1: Window > Script Editor, paste, press Run (press Stop first). Ctrl+Z undoes it.",
        "# Node ids verified against exts/isaacsim.ros2.nodes/ogn/nodes.json (Isaac Sim 6.1.0-rc.26).",
        "import omni.graph.core as og",
        "import omni.usd",
        "import usdrt  # prim pickers ('target' inputs) take a list of usdrt.Sdf.Path values",
        "",
        f"GRAPH_PATH = {graph_path!r}",
        "stage = omni.usd.get_context().get_stage()",
        "if stage.GetPrimAtPath(GRAPH_PATH).IsValid():",
        "    raise RuntimeError(f'{GRAPH_PATH} already exists - pick another graph_path or delete the old graph.')",
        "keys = og.Controller.Keys",
        "",
    ]


def _edit_block(nodes: list[tuple[str, str, str]], connects: list[tuple[str, str, str]],
                values: list[tuple[str, str, str]]) -> list[str]:
    lines = ["graph, created, _, _ = og.Controller.edit(",
             "    {'graph_path': GRAPH_PATH, 'evaluator_name': 'execution'},  # 'execution' = Action Graph",
             "    {",
             "        # 1. Nodes: (short name, node type id)",
             "        keys.CREATE_NODES: ["]
    lines += [f"            ({name!r}, {type_id!r}),  # {why}" for name, type_id, why in nodes]
    lines += ["        ],", "        # 2. Wires: (from an output, to an input)", "        keys.CONNECT: ["]
    lines += [f"            ({src!r}, {dst!r}),  # {why}" for src, dst, why in connects]
    lines += ["        ],", "        # 3. Values typed into inputs", "        keys.SET_VALUES: ["]
    lines += [f"            ({attr!r}, {code}),  # {why}" for attr, code, why in values]
    lines += ["        ],", "    },", ")", "print('Created', [node.get_prim_path() for node in created])", ""]
    return lines


def _target(path: str) -> str:
    return f"[usdrt.Sdf.Path({path!r})]"


def _common_nodes(sim_time: bool = True) -> list[tuple[str, str, str]]:
    nodes = [("OnPlaybackTick", "omni.graph.action.OnPlaybackTick", "fires every frame while Play runs"),
             ("Context", _B + "ROS2Context", "ROS 2 context: domain id (uses ROS_DOMAIN_ID when set)")]
    if sim_time:
        nodes.append(("ReadSimTime", "isaacsim.core.nodes.IsaacReadSimulationTime", "simulation time for timestamps"))
    return nodes


def _plan_joint_states(p: dict[str, Any]) -> dict[str, Any]:
    robot, ns = p["robot_prim"], p["namespace"]
    nodes = _common_nodes() + [
        ("ReadJointState", _READ_JOINT_STATE, "reads joint names/positions/velocities/efforts of the robot"),
        ("PublishJointState", _B + "ROS2PublishJointState", "publishes sensor_msgs/msg/JointState"),
    ]
    connects = [
        ("OnPlaybackTick.outputs:tick", "ReadJointState.inputs:execIn", "read every frame"),
        ("ReadJointState.outputs:execOut", "PublishJointState.inputs:execIn", "then publish"),
        ("Context.outputs:context", "PublishJointState.inputs:context", "same ROS 2 context/domain"),
        ("ReadSimTime.outputs:simulationTime", "PublishJointState.inputs:timeStamp", "message time = sim time"),
    ] + [(f"ReadJointState.outputs:{name}", f"PublishJointState.inputs:{name}", "joint data")
         for name in ("jointNames", "jointPositions", "jointVelocities", "jointEfforts", "jointDofTypes",
                      "stageMetersPerUnit", "sensorTime")]
    values = [
        ("ReadSimTime.inputs:resetOnStop", "True", "time restarts at 0 after Stop"),
        ("ReadJointState.inputs:prim", _target(robot), "the robot (articulation root)"),
        ("PublishJointState.inputs:topicName", repr(p["topic"]), "topic name"),
        ("PublishJointState.inputs:nodeNamespace", repr(ns), "optional namespace (empty = none)"),
    ]
    topics = [_resolve_topic(p["topic"], ns)]
    if p["subscribe"]:
        nodes += [("SubscribeJointState", _B + "ROS2SubscribeJointState", "receives joint commands"),
                  ("ArticulationController", _ART_CONTROLLER, "moves the robot's joints")]
        connects += [
            ("OnPlaybackTick.outputs:tick", "SubscribeJointState.inputs:execIn", "listen every frame"),
            ("Context.outputs:context", "SubscribeJointState.inputs:context", "same context"),
            ("OnPlaybackTick.outputs:tick", "ArticulationController.inputs:execIn", "apply every frame"),
        ] + [(f"SubscribeJointState.outputs:{src}", f"ArticulationController.inputs:{dst}", "command data")
             for src, dst in (("jointNames", "jointNames"), ("positionCommand", "positionCommand"),
                              ("velocityCommand", "velocityCommand"), ("effortCommand", "effortCommand"))]
        values += [("SubscribeJointState.inputs:topicName", repr(p["command_topic"]), "command topic"),
                   ("SubscribeJointState.inputs:nodeNamespace", repr(ns), "namespace"),
                   ("ArticulationController.inputs:targetPrim", _target(robot), "the robot to move")]
    script = _header(GOALS["publish_joint_states"]["title"], p["graph_path"]) + _edit_block(nodes, connects, values)
    out = topics[0]
    verify = ["ros2 topic list", f"ros2 topic echo {out} --once", f"ros2 topic hz {out}"]
    if p["subscribe"]:
        cmd = _resolve_topic(p["command_topic"], ns)
        verify.append(f"ros2 topic pub --once {cmd} sensor_msgs/msg/JointState "
                      "\"{name: ['<joint name from the echo>'], position: [0.3]}\"")
    return {"script_lines": script, "nodes": nodes, "topics": topics, "verify": verify,
            "alternative": "from isaacsim.ros2.nodes import create_ros2_joint_states_graph, Ros2JointStatesGraphConfig\n"
                           f"create_ros2_joint_states_graph(Ros2JointStatesGraphConfig(graph_path={p['graph_path']!r}, "
                           f"articulation_root={robot!r}, publish_joint_states=True, "
                           f"subscribe_joint_states={p['subscribe']!r}))",
            "prerequisites": [f"{robot} must be an articulation root (check with the robot inspector).",
                              "The robot needs joints with drives if you also send commands."]}


def _sensor_graph(p: dict[str, Any], sensor: str, helpers: list[tuple[str, str, list[tuple[str, str, str]]]],
                  width: int, height: int) -> tuple[list, list, list]:
    nodes = _common_nodes(sim_time=False) + [
        ("RunOnce", "isaacsim.core.nodes.OgnIsaacRunOneSimulationFrame", "runs the render-product setup once"),
        ("RenderProduct", _CREATE_RENDER_PRODUCT, "creates the image source for the sensor"),
    ]
    connects = [("OnPlaybackTick.outputs:tick", "RunOnce.inputs:execIn", "start on Play"),
                ("RunOnce.outputs:step", "RenderProduct.inputs:execIn", "create the render product once")]
    values = [("RenderProduct.inputs:cameraPrim", _target(sensor), "which camera/lidar to look through"),
              ("RenderProduct.inputs:width", str(width), "pixels"),
              ("RenderProduct.inputs:height", str(height), "pixels")]
    for name, type_id, inputs in helpers:
        nodes.append((name, type_id, "sets up the ROS 2 publisher for this stream"))
        connects += [("RenderProduct.outputs:execOut", f"{name}.inputs:execIn", "after the render product exists"),
                     ("RenderProduct.outputs:renderProductPath", f"{name}.inputs:renderProductPath", "image source"),
                     ("Context.outputs:context", f"{name}.inputs:context", "same context")]
        values += [(f"{name}.inputs:{attr}", code, why) for attr, code, why in inputs]
    return nodes, connects, values


def _plan_camera(p: dict[str, Any]) -> dict[str, Any]:
    ns, frame = p["namespace"], p["frame_id"]
    common = [("frameId", repr(frame), "frame id in message headers"), ("nodeNamespace", repr(ns), "namespace"),
              ("resetSimulationTimeOnStop", "True", "timestamps restart after Stop")]
    helpers = [("CameraInfoPublish", _B + "ROS2CameraInfoHelper",
                [("topicName", repr(p["info_topic"]), "camera_info topic")] + common),
               ("RGBPublish", _B + "ROS2CameraHelper",
                [("topicName", repr(p["rgb_topic"]), "image topic"), ("type", "'rgb'", "raw RGB image")] + common)]
    topics = [_resolve_topic(p["info_topic"], ns), _resolve_topic(p["rgb_topic"], ns)]
    if p["depth"]:
        helpers.append(("DepthPublish", _B + "ROS2CameraHelper",
                        [("topicName", repr(p["depth_topic"]), "depth topic"), ("type", "'depth'", "depth image")]
                        + common))
        topics.append(_resolve_topic(p["depth_topic"], ns))
    nodes, connects, values = _sensor_graph(p, p["camera_prim"], helpers, p["width"], p["height"])
    script = _header(GOALS["camera"]["title"], p["graph_path"]) + _edit_block(nodes, connects, values)
    verify = ["ros2 topic list", f"ros2 topic echo {topics[0]} --once", f"ros2 topic hz {topics[1]}",
              "ros2 run rqt_image_view rqt_image_view   # pick " + topics[1]]
    return {"script_lines": script, "nodes": nodes, "topics": topics, "verify": verify,
            "alternative": "from isaacsim.ros2.nodes import create_ros2_camera_graph, Ros2CameraGraphConfig\n"
                           f"create_ros2_camera_graph(Ros2CameraGraphConfig(graph_path={p['graph_path']!r}, "
                           f"camera_prim={p['camera_prim']!r}, frame_id={frame!r}))",
            "prerequisites": [f"{p['camera_prim']} must be a Camera prim (Create > Camera)."]}


def _plan_lidar(p: dict[str, Any]) -> dict[str, Any]:
    ns, frame, lidar = p["namespace"], p["frame_id"], p["lidar_prim"]
    common = [("frameId", repr(frame), "frame id in message headers"), ("nodeNamespace", repr(ns), "namespace")]
    helpers = [("LaserScanPublish", _B + "ROS2RtxLidarHelper",
                [("topicName", repr(p["topic"]), "scan topic"), ("type", "'laser_scan'", "2D LaserScan")] + common)]
    topics = [_resolve_topic(p["topic"], ns)]
    if p["point_cloud"]:
        helpers.append(("PointCloudPublish", _B + "ROS2RtxLidarHelper",
                        [("topicName", "'point_cloud'", "point cloud topic"), ("type", "'point_cloud'", "PointCloud2")]
                        + common))
        topics.append(_resolve_topic("point_cloud", ns))
    nodes, connects, values = _sensor_graph(p, lidar, helpers, 128, 128)
    create = [
        "# 0. Create the RTX lidar (isaacsim.sensors.experimental.rtx.Lidar.create, Isaac Sim 6.1).",
        "#    LaserScan needs a 2D rotary lidar such as Example_Rotary_2D (assets: /Isaac/Sensors/NVIDIA/).",
        "from isaacsim.sensors.experimental.rtx import Lidar",
        f"if not stage.GetPrimAtPath({lidar!r}).IsValid():",
        f"    Lidar.create(path={lidar!r}, config={p['config']!r})",
        "",
    ]
    header = _header(GOALS["lidar"]["title"], p["graph_path"])
    script = header + create + _edit_block(nodes, connects, values)
    verify = ["ros2 topic list", f"ros2 topic echo {topics[0]} --once",
              f"ros2 run rviz2 rviz2   # Fixed Frame = {frame}, add LaserScan on {topics[0]}"]
    return {"script_lines": script, "nodes": nodes, "topics": topics, "verify": verify,
            "alternative": "from isaacsim.ros2.nodes import create_ros2_rtx_lidar_graph, Ros2RtxLidarGraphConfig\n"
                           f"create_ros2_rtx_lidar_graph(Ros2RtxLidarGraphConfig(graph_path={p['graph_path']!r}, "
                           f"lidar_prim={lidar!r}, laser_scan_topic={topics[0]!r}))",
            "prerequisites": ["Something for the lidar to hit (a ground plane and a few boxes).",
                              "The lidar prim must be an OmniLidar (the script creates one if the path is empty)."]}


def _plan_clock(p: dict[str, Any]) -> dict[str, Any]:
    nodes = [("OnPlaybackTick", "omni.graph.action.OnPlaybackTick", "fires every frame while Play runs"),
             ("ReadSimTime", "isaacsim.core.nodes.IsaacReadSimulationTime", "simulation time"),
             ("PublishClock", _B + "ROS2PublishClock", "publishes rosgraph_msgs/msg/Clock"),
             ("Context", _B + "ROS2Context", "ROS 2 context/domain")]
    connects = [("OnPlaybackTick.outputs:tick", "PublishClock.inputs:execIn", "publish every frame"),
                ("Context.outputs:context", "PublishClock.inputs:context", "same context"),
                ("ReadSimTime.outputs:simulationTime", "PublishClock.inputs:timeStamp", "the time to publish")]
    values = [("ReadSimTime.inputs:resetOnStop", "True", "time restarts at 0 after Stop"),
              ("PublishClock.inputs:topicName", repr(p["topic"]), "topic name")]
    topic = _resolve_topic(p["topic"], "")
    script = _header(GOALS["clock"]["title"], p["graph_path"]) + _edit_block(nodes, connects, values)
    return {"script_lines": script, "nodes": nodes, "topics": [topic],
            "verify": ["ros2 topic list", f"ros2 topic echo {topic} --once", f"ros2 topic hz {topic}"],
            "alternative": "from isaacsim.ros2.nodes import create_ros2_clock_graph, Ros2ClockGraphConfig\n"
                           f"create_ros2_clock_graph(Ros2ClockGraphConfig(graph_path={p['graph_path']!r}))",
            "prerequisites": ["ROS nodes that should follow it need use_sim_time:=true."]}


def _plan_teleop(p: dict[str, Any]) -> dict[str, Any]:
    ns, robot = p["namespace"], p["robot_prim"]
    nodes = [("OnPlaybackTick", "omni.graph.action.OnPlaybackTick", "fires every frame while Play runs"),
             ("Context", _B + "ROS2Context", "ROS 2 context/domain"),
             ("SubscribeTwist", _B + "ROS2SubscribeTwist", "receives geometry_msgs/msg/Twist"),
             ("BreakLinVel", "omni.graph.nodes.BreakVector3", "splits linear velocity into x, y, z"),
             ("BreakAngVel", "omni.graph.nodes.BreakVector3", "splits angular velocity into x, y, z"),
             ("DiffController", "isaacsim.robot.wheeled_robots.DifferentialController", "turns (v, w) into wheel speeds"),
             ("ArticulationController", _ART_CONTROLLER, "applies wheel speeds to the joints")]
    connects = [("OnPlaybackTick.outputs:tick", "SubscribeTwist.inputs:execIn", "listen every frame"),
                ("OnPlaybackTick.outputs:tick", "ArticulationController.inputs:execIn", "apply every frame"),
                ("Context.outputs:context", "SubscribeTwist.inputs:context", "same context"),
                ("SubscribeTwist.outputs:execOut", "DiffController.inputs:execIn", "on each new message"),
                ("SubscribeTwist.outputs:linearVelocity", "BreakLinVel.inputs:tuple", "forward speed vector"),
                ("BreakLinVel.outputs:x", "DiffController.inputs:linearVelocity", "forward speed (m/s)"),
                ("SubscribeTwist.outputs:angularVelocity", "BreakAngVel.inputs:tuple", "turn rate vector"),
                ("BreakAngVel.outputs:z", "DiffController.inputs:angularVelocity", "turn rate (rad/s)"),
                ("DiffController.outputs:velocityCommand", "ArticulationController.inputs:velocityCommand",
                 "wheel speeds")]
    values = [("SubscribeTwist.inputs:topicName", repr(p["topic"]), "topic name"),
              ("SubscribeTwist.inputs:nodeNamespace", repr(ns), "namespace"),
              ("DiffController.inputs:wheelRadius", repr(float(p["wheel_radius"])), "metres - measure your robot"),
              ("DiffController.inputs:wheelDistance", repr(float(p["wheel_distance"])), "metres between wheels"),
              ("ArticulationController.inputs:jointNames",
               repr([p["left_wheel_joint"], p["right_wheel_joint"]]), "left, right wheel joint names"),
              ("ArticulationController.inputs:targetPrim", _target(robot), "the robot (articulation root)")]
    topic = _resolve_topic(p["topic"], ns)
    script = _header(GOALS["teleop"]["title"], p["graph_path"]) + _edit_block(nodes, connects, values)
    return {"script_lines": script, "nodes": nodes, "topics": [topic],
            "verify": ["ros2 topic list", f"ros2 topic info {topic}   # Subscription count should be 1",
                       f"ros2 topic pub --rate 10 {topic} geometry_msgs/msg/Twist "
                       "\"{linear: {x: 0.2}, angular: {z: 0.0}}\"",
                       f"ros2 run teleop_twist_keyboard teleop_twist_keyboard --ros-args -r cmd_vel:={topic}"],
            "alternative": None,
            "prerequisites": [f"{robot} is an articulation root with two wheel joints that have velocity drives.",
                              "Replace the wheel joint names, wheel radius and wheel distance with your robot's values "
                              "(defaults come from NVIDIA's differential-base test)."]}


_PLANNERS = {"publish_joint_states": _plan_joint_states, "camera": _plan_camera, "lidar": _plan_lidar,
             "clock": _plan_clock, "teleop": _plan_teleop}
_DEFAULTS: dict[str, dict[str, Any]] = {
    "publish_joint_states": {"robot_prim": "/World/robot", "graph_path": "/World/ROS2/JointStates",
                             "topic": "joint_states", "namespace": "", "subscribe": False,
                             "command_topic": "joint_command"},
    "camera": {"camera_prim": "/World/Camera", "graph_path": "/World/ROS2/Camera", "rgb_topic": "rgb", "depth": True,
               "depth_topic": "depth", "info_topic": "camera_info", "frame_id": "sim_camera", "width": 640,
               "height": 480, "namespace": ""},
    "lidar": {"lidar_prim": "/World/Lidar", "config": "Example_Rotary_2D", "graph_path": "/World/ROS2/Lidar",
              "topic": "scan", "frame_id": "sim_lidar", "point_cloud": False, "namespace": ""},
    "clock": {"graph_path": "/World/ROS2/Clock", "topic": "clock"},
    "teleop": {"robot_prim": "/World/robot", "left_wheel_joint": "left_wheel", "right_wheel_joint": "right_wheel",
               "wheel_radius": 0.24, "wheel_distance": 0.5, "graph_path": "/World/ROS2/Teleop", "topic": "cmd_vel",
               "namespace": ""},
}


def setup_plan(goal: str = "publish_joint_states", **params: Any) -> dict[str, Any]:
    """Text-only plan for one ROS 2 goal: steps, an og.Controller.edit script (verified node ids), the official
    one-call alternative and the ros2 commands to verify. Nothing is executed or written."""
    key = str(goal or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = _GOAL_ALIASES.get(key, key)
    if key not in _PLANNERS:
        return {"ok": False, "error": f"Unknown goal {goal!r}; choose one of {', '.join(GOALS)}.",
                "goals": {name: spec["title"] for name, spec in GOALS.items()}}
    unknown = sorted(set(params) - set(_DEFAULTS[key]))
    if unknown:
        return {"ok": False, "goal": key, "error": f"Unknown parameter(s) for {key}: {', '.join(unknown)}.",
                "params": GOALS[key]["params"]}
    merged = {**_DEFAULTS[key], **params}
    problem = _check_params(merged)
    if problem:
        return {"ok": False, "goal": key, "error": problem, "params": GOALS[key]["params"]}
    built = _PLANNERS[key](merged)
    script = "\n".join(built["script_lines"])
    steps = list(_COMMON_STEPS)
    if GOALS[key]["menu"]:
        steps.insert(3, f"(No-code alternative: {GOALS[key]['menu']}.)")
    steps += _ROS_SHELL_STEPS + [f"Verify: {built['verify'][1] if len(built['verify']) > 1 else built['verify'][0]}"]
    return {
        "ok": True,
        "goal": key,
        "title": GOALS[key]["title"],
        "params": merged,
        "prerequisites": built["prerequisites"],
        "steps": steps,
        "script": script,
        "script_language": "python",
        "script_runs_in": "Isaac Sim 6.1 Script Editor (Kit) - not in the router",
        "alternative_python": built["alternative"],
        "menu": GOALS[key]["menu"],
        "nodes": [{"name": name, "type": type_id, "why": why} for name, type_id, why in built["nodes"]],
        "topics": built["topics"],
        "verify_commands": built["verify"],
        "ros_notes": list(GENERAL_ROS_NOTES),
        "executed": False,
        "approval_note": "Text only - nothing was run. Running the script changes the open stage (save a copy first); "
                         "installing ROS 2 or changing launch settings needs your approval.",
        "sources": {"node_ids": SOURCES["node_ids"], "pattern": SOURCES["graph_api"],
                    "teleop_pattern": SOURCES["teleop_graph_example"], "lidar_pattern": SOURCES["lidar_graph_example"]},
    }
