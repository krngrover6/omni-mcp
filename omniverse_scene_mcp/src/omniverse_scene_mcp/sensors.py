"""Sensors helper: plan, author, list and (inside Isaac Sim) create and read robot sensors.

Five sensor kinds: 'rgb_camera', 'depth_camera', 'lidar', 'imu' and 'contact'. A sensor is a prim placed UNDER a
robot link, so it moves with that link. Every API name below was checked (read-only) against the installed
Isaac Sim 6.1.0 (isaacsim-6.1.0/VERSION: 6.1.0-rc.26); see VERIFIED for the files and versions.

What is where in Isaac Sim 6.1 (verified):
  - Cameras: a plain UsdGeom.Camera prim. Pixels are read with isaacsim.sensors.experimental.rtx (1.9.0):
    CameraSensor(RtxCamera(path), resolution=(height, width), annotators=["rgb", "distance_to_image_plane", ...])
    and sensor.get_data("rgb") -> (warp array shaped (H, W, C) or None while warming up, info).
  - RTX lidar: an 'OmniLidar' prim (omni.usd.schema.omni_sensors) loaded from a lidar asset:
    Lidar.create(path, config="Example_Rotary", ...) then LidarSensor(lidar, annotators=["generic-model-output"])
    and parse_generic_model_output_data(data).numElements for the point count. The OmniLidar settings are a large
    applied-API attribute set from the asset, so this module never hand-authors a lidar (plan / Kit only).
  - IMU and contact: 'IsaacImuSensor' / 'IsaacContactSensor' typed prims (isaacsim.robot.schema 7.0.2 sensor
    schema, inherits Xformable). isaacsim.sensors.experimental.physics (3.3.0): IMU.create / Contact.create author
    the prim; IMUSensor(path) / ContactSensor(path) read it with get_data(). Both need an enabled rigid-body
    ancestor (the C++ plugins walk up the hierarchy to find it); Contact.create raises ValueError without one and,
    on PhysX, applies PhysxContactReportAPI to that rigid body.
  - The older isaacsim.sensors.camera / .rtx / .physics extensions (and their omni.kit.commands such as
    'IsaacSensorCreateRtxLidar', 'IsaacSensorCreateImuSensor', 'IsaacSensorCreateContactSensor') are deprecated
    since Isaac Sim 6.0. The camera and rtx ones are still enabled by apps/isaacsim.exp.base.kit; the physics one
    (which owns the IMU/contact commands) is NOT enabled by default. OLD_TO_NEW maps older tutorial names.

Functions:
  read-only : sensor_catalog(), plan_sensor(...), list_sensors(stage)
  write     : add_camera_sensor(stage, ...), add_physics_sensor(stage, ...)  (dry_run=True by default; pure pxr)
  Kit-only  : create_sensor_live(...) (dry_run=True by default), read_sensor_live(prim_path),
              release_sensor_live(prim_path=None). They import omni / isaacsim lazily and return
              {"ok": False, "message": "Kit not available"} outside Kit. Reads return summaries only, never arrays.
"""

from __future__ import annotations

import difflib
import importlib
import math
import pathlib
import re
from typing import Any

from . import authoring, look_at

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = UsdPhysics = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

__all__ = [
    "LIDAR_CONFIG_ASSETS",
    "OLD_TO_NEW",
    "SENSOR_KINDS",
    "VERIFIED",
    "add_camera_sensor",
    "add_physics_sensor",
    "create_sensor_live",
    "list_sensors",
    "plan_sensor",
    "read_sensor_live",
    "release_sensor_live",
    "sensor_catalog",
]

SENSOR_KINDS = ("rgb_camera", "depth_camera", "lidar", "imu", "contact")
CAMERA_KINDS = ("rgb_camera", "depth_camera")
PHYSICS_KINDS = ("imu", "contact")
DEFAULT_NAMES = {
    "rgb_camera": "rgb_camera",
    "depth_camera": "depth_camera",
    "lidar": "lidar",
    "imu": "imu",
    "contact": "contact_sensor",
}
MAX_SCAN_PRIMS = 5000
MAX_GEOMETRY_PRIMS = 3000
MAX_LISTED = 200
MAX_LIVE_RUNTIMES = 8
_NOT_KIT = "Kit not available"
_Refusal = authoring._Refusal  # one refusal type, so authoring's helpers and ours stop planning the same way

VERIFIED: dict[str, Any] = {
    "isaac_sim": "6.1.0-rc.26+release.49347.2d230af4 (isaacsim-6.1.0/VERSION)",
    "checked_on": "2026-10-22, read-only inspection of the installed files",
    "extensions": {
        "isaacsim.sensors.experimental.rtx": "1.9.0 - RtxCamera/CameraSensor, Lidar/LidarSensor (current API)",
        "isaacsim.sensors.experimental.physics": "3.3.0 - IMU/IMUSensor, Contact/ContactSensor (current API)",
        "isaacsim.robot.schema": "7.0.2 - IsaacImuSensor / IsaacContactSensor prim schemas",
        "omni.usd.schema.omni_sensors": "OmniLidar prim type (extscache, build 00c488ae)",
        "omni.replicator.core": "1.13.36 - annotators behind CameraSensor/LidarSensor",
        "isaacsim.sensors.camera": "1.7.13 - DEPRECATED since 6.0, still enabled by apps/isaacsim.exp.base.kit",
        "isaacsim.sensors.rtx": "15.17.5 - DEPRECATED since 6.0 (IsaacSensorCreateRtxLidar), enabled by base.kit",
        "isaacsim.sensors.physics": "1.1.10 - DEPRECATED since 6.0 (IsaacSensorCreateImu/ContactSensor), NOT enabled by default",
    },
    "sources": [
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.rtx/docs/Overview.md",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.rtx/isaacsim/sensors/experimental/rtx/impl/lidar.py",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.rtx/isaacsim/sensors/experimental/rtx/impl/rtx_lidar_configs.py",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.rtx/isaacsim/sensors/experimental/rtx/impl/camera_sensor.py",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.rtx/isaacsim/sensors/experimental/rtx/impl/utils.py",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.physics/docs/Overview.md",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.physics/isaacsim/sensors/experimental/physics/impl/imu.py",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.physics/isaacsim/sensors/experimental/physics/impl/contact.py",
        "isaacsim-6.1.0/exts/isaacsim.sensors.experimental.physics/include/isaacsim/sensors/experimental/physics/IImuSensor.hpp",
        "isaacsim-6.1.0/exts/isaacsim.robot.schema/pip_prebundle/usd/schema/isaac/sensor_schema/generatedSchema.usda",
        "isaacsim-6.1.0/extscache/omni.usd.schema.omni_sensors-0.0.0+00c488ae.wx64.r.cp312/usd_plugins/generatedSchema.usda",
        "isaacsim-6.1.0/extsDeprecated/isaacsim.sensors.physics/isaacsim/sensors/physics/impl/commands.py",
        "isaacsim-6.1.0/extsDeprecated/isaacsim.sensors.rtx/isaacsim/sensors/rtx/impl/commands.py",
        "isaacsim-6.1.0/exts/isaacsim.core.deprecation_manager/config/extension.toml",
        "isaacsim-6.1.0/apps/isaacsim.exp.base.kit",
    ],
}

# Older tutorial name -> what to use in Isaac Sim 6.1 (verified against the files in VERIFIED["sources"]).
OLD_TO_NEW: list[dict[str, str]] = [
    {"old": "omni.isaac.sensor (Isaac Sim 2023/4.0 extension)",
     "new": "isaacsim.sensors.experimental.rtx (cameras, RTX lidar) + isaacsim.sensors.experimental.physics (IMU, contact)",
     "note": "4.5-5.x split it into isaacsim.sensors.camera / .rtx / .physics; those are deprecated since 6.0."},
    {"old": "from omni.isaac.sensor import Camera  /  from isaacsim.sensors.camera import Camera",
     "new": "from isaacsim.sensors.experimental.rtx import RtxCamera, CameraSensor",
     "note": "Camera(prim_path=..., resolution=(w, h)) -> CameraSensor(RtxCamera(path), resolution=(h, w), annotators=[...])."},
    {"old": "camera.initialize(); camera.get_rgba() / camera.get_current_frame()['rgba']",
     "new": "rgb, info = sensor.get_data('rgb')", "note": "Returns a warp array (H, W, 3); .numpy() converts it."},
    {"old": "camera.add_distance_to_image_plane_to_frame() / camera.get_depth()",
     "new": "CameraSensor(..., annotators=['distance_to_image_plane']); sensor.get_data('distance_to_image_plane')",
     "note": "Annotators are chosen when the CameraSensor is made (or with attach_annotators)."},
    {"old": "camera.add_semantic_segmentation_to_frame()",
     "new": "annotators=['semantic_segmentation']", "note": "Prims need semantic labels to show up."},
    {"old": "omni.kit.commands.execute('IsaacSensorCreateRtxLidar', path=..., parent=..., config='Example_Rotary')",
     "new": "Lidar.create(path, config='Example_Rotary') from isaacsim.sensors.experimental.rtx",
     "note": "The command still exists in the deprecated isaacsim.sensors.rtx (enabled by default in 6.1)."},
    {"old": "from isaacsim.sensors.rtx import LidarRtx; lidar.attach_annotator('IsaacCreateRTXLidarScanBuffer')",
     "new": "LidarSensor(lidar, annotators=['generic-model-output']); parse_generic_model_output_data(data)",
     "note": "Point count: gmo.numElements."},
    {"old": "Old RTX lidar .json configs (e.g. 'Example_Rotary' json in omni.isaac.sensor/data/lidar_configs)",
     "new": "USD lidar assets, e.g. /Isaac/Sensors/NVIDIA/Example_Rotary.usda (SUPPORTED_LIDAR_CONFIGS)",
     "note": "Lidar.create(config=...) accepts the file stem, e.g. 'Example_Rotary' or 'OS1' + variant."},
    {"old": "omni.kit.commands.execute('IsaacSensorCreateImuSensor', path='/Imu_Sensor', parent=...)",
     "new": "IMU.create('/World/robot/link/imu', translations=[[x, y, z]]) from isaacsim.sensors.experimental.physics",
     "note": "The command lives only in the deprecated isaacsim.sensors.physics, which 6.1 does not enable by default."},
    {"old": "omni.kit.commands.execute('IsaacSensorCreateContactSensor', path='/Contact_Sensor', parent=...)",
     "new": "Contact.create('/World/robot/foot/contact_sensor', min_threshold=..., max_threshold=..., radius=...)",
     "note": "Same as the IMU: the command is only in the deprecated, not-enabled isaacsim.sensors.physics."},
    {"old": "from omni.isaac.sensor import IMUSensor; IMUSensor(prim_path=..., name=...)",
     "new": "IMUSensor(IMU.create(path, ...)) or IMUSensor(existing_path)",
     "note": "isaacsim.sensors.experimental.physics 3.0.0 split authoring (IMU/Contact) from runtime; 'name' was dropped."},
    {"old": "imu.get_current_frame() / contact.get_current_frame()",
     "new": "imu_sensor.get_data() / contact_sensor.get_data()",
     "note": "Keys: linear_acceleration, angular_velocity, orientation, time, physics_step / in_contact, force, number_of_contacts."},
    {"old": "omni.isaac.range_sensor (PhysX lidar, 'RangeSensorCreateLidar')",
     "new": "isaacsim.sensors.experimental.physics Raycast/RaycastSensor (IsaacRaycastSensor) or RTX Lidar",
     "note": "The PhysX lidar now lives in the deprecated isaacsim.sensors.physx."},
    {"old": "omni.isaac.isaac_sensor", "new": "isaacsim.sensors.rtx (deprecated) -> isaacsim.sensors.experimental.rtx",
     "note": "From isaacsim.core.deprecation_manager's rename table."},
]

# Lidar assets Lidar.create(config=...) knows (rtx_lidar_configs.py SUPPORTED_LIDAR_CONFIGS keys).
LIDAR_CONFIG_ASSETS = (
    "/Isaac/Sensors/HESAI/XT32_SD10/HESAI_XT32_SD10.usd",
    "/Isaac/Sensors/NVIDIA/Example_Rotary_2D.usda",
    "/Isaac/Sensors/NVIDIA/Example_Rotary.usda",
    "/Isaac/Sensors/NVIDIA/Example_Solid_State.usda",
    "/Isaac/Sensors/NVIDIA/Simple_Example_Solid_State.usda",
    "/Isaac/Sensors/Ouster/OS0/OS0.usd",
    "/Isaac/Sensors/Ouster/OS1/OS1.usd",
    "/Isaac/Sensors/Ouster/OS2/OS2.usd",
    "/Isaac/Sensors/Ouster/VLS_128/Ouster_VLS_128.usd",
    "/Isaac/Sensors/SICK/LMS4000/SICK_LMS4000.usd",
    "/Isaac/Sensors/SICK/LMS5xx/SICK_LMS5xx.usd",
    "/Isaac/Sensors/SICK/LRS4000/SICK_LRS4000.usd",
    "/Isaac/Sensors/SICK/microScan3/SICK_microScan3.usd",
    "/Isaac/Sensors/SICK/MRS1000/SICK_MRS1000.usd",
    "/Isaac/Sensors/SICK/multiScan100/SICK_multiScan100.usd",
    "/Isaac/Sensors/SICK/nanoScan3/SICK_nanoScan3.usd",
    "/Isaac/Sensors/SICK/picoScan100/SICK_picoScan100.usd",
    "/Isaac/Sensors/Slamtec/RPLIDAR_S2E/Slamtec_RPLIDAR_S2E.usd",
    "/Isaac/Sensors/ZVISION/ZVISION_ML30S.usda",
    "/Isaac/Sensors/ZVISION/ZVISION_MLXS.usda",
)
_LIDAR_WITH_VARIANTS = {"OS0", "OS1", "OS2"}  # sensor variant set, e.g. OS1 + "OS1_REV6_32ch20hz512res"
DEFAULT_LIDAR_CONFIG = "Example_Rotary"

# Isaac sensor prim schemas (isaacsim.robot.schema 7.0.2 generatedSchema.usda): attribute -> (Sdf type, default).
PHYSICS_SCHEMAS: dict[str, dict[str, Any]] = {
    "imu": {
        "type": "IsaacImuSensor",
        "attributes": {
            "enabled": ("bool", True),
            "linearAccelerationFilterWidth": ("int", 1),
            "angularVelocityFilterWidth": ("int", 1),
            "orientationFilterWidth": ("int", 1),
        },
    },
    "contact": {
        "type": "IsaacContactSensor",
        "attributes": {
            "enabled": ("bool", True),
            "threshold": ("float2", (0.0, 100000.0)),  # Contact.create defaults (the bare schema default is 0.01)
            "radius": ("float", -1.0),
            "color": ("float4", (1.0, 1.0, 1.0, 1.0)),
        },
    },
}
SENSOR_PRIM_TYPES = {
    "IsaacImuSensor": "imu",
    "IsaacContactSensor": "contact",
    "IsaacRaycastSensor": "raycast",
    "IsaacLightBeamSensor": "light_beam (deprecated)",
    "OmniLidar": "lidar",
    "OmniRadar": "radar",
    "OmniAcoustic": "acoustic",
    "OmniGeneralSensor": "general_sensor",
}
VIEWPORT_CAMERAS = ("/OmniverseKit_Persp", "/OmniverseKit_Front", "/OmniverseKit_Top", "/OmniverseKit_Right")

_CAMERA_DEFAULTS = {
    "focal_length": 24.0,  # mm; the RtxCamera example in the 6.1 docs uses 24
    "horizontal_aperture": 20.955,  # mm; the USD Camera default
    "resolution": (640, 480),  # (width, height) pixels; sets verticalAperture so pixels are square
    "clipping_range_m": (0.01, 1000.0),  # metres; converted to stage units with metersPerUnit
}
_PARAMS = {
    "rgb_camera": tuple(_CAMERA_DEFAULTS),
    "depth_camera": tuple(_CAMERA_DEFAULTS),
    "lidar": ("config", "variant", "usd_path"),
    "imu": ("linear_acceleration_filter_size", "angular_velocity_filter_size", "orientation_filter_size"),
    "contact": ("min_threshold", "max_threshold", "radius", "color", "apply_contact_report"),
}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_CATALOG = {
    "rgb_camera": {
        "title": "RGB camera",
        "measures": "A colour picture of what is in front of the lens, one red/green/blue value per pixel.",
        "typical_use": "Object detection, following a line, recording training images, teleoperation views.",
        "prim_type": "Camera (UsdGeom.Camera); RtxCamera/CameraSensor add OmniSensorAPI when reading",
        "data": "get_data('rgb') -> (H, W, 3) uint8 array (warp; .numpy() for numpy).",
        "api_6_1": "isaacsim.sensors.experimental.rtx: RtxCamera, CameraSensor",
        "needs": "Nothing physical; it just has to be under the link it should move with and not inside a mesh.",
    },
    "depth_camera": {
        "title": "Depth camera",
        "measures": "How far away things are: one distance per pixel, measured from the camera's image plane.",
        "typical_use": "Obstacle avoidance, grasping, measuring distances, building 3D maps.",
        "prim_type": "Camera (UsdGeom.Camera) read with the 'distance_to_image_plane' annotator",
        "data": "get_data('distance_to_image_plane') -> (H, W, 1) float32 in stage units; inf where nothing is hit.",
        "api_6_1": "isaacsim.sensors.experimental.rtx: RtxCamera, CameraSensor (or SingleViewDepthCameraSensor for stereo-style depth)",
        "needs": "Same as the RGB camera; set the near clipping distance below the closest thing it must see.",
    },
    "lidar": {
        "title": "RTX lidar",
        "measures": "Laser distance points all around (or in front of) the sensor: a 3D point cloud each scan.",
        "typical_use": "Mapping (SLAM), localisation, obstacle detection for mobile robots.",
        "prim_type": "OmniLidar (omni.usd.schema.omni_sensors), loaded from a lidar USD asset",
        "data": "LidarSensor(..., annotators=['generic-model-output']); parse_generic_model_output_data(data).numElements points.",
        "api_6_1": "isaacsim.sensors.experimental.rtx: Lidar.create(config=...), LidarSensor",
        "needs": "A config/asset name (e.g. 'Example_Rotary'); the asset loads from the Isaac assets root.",
    },
    "imu": {
        "title": "IMU (inertial measurement unit)",
        "measures": "Acceleration (m/s^2 on a metre stage), turning speed (rad/s) and orientation of the body it sits on.",
        "typical_use": "Balance and tilt for legged robots and drones, odometry, detecting bumps and falls.",
        "prim_type": "IsaacImuSensor (isaacsim.robot.schema)",
        "data": "IMUSensor(path).get_data() -> linear_acceleration [x,y,z], angular_velocity [x,y,z], orientation [w,x,y,z].",
        "api_6_1": "isaacsim.sensors.experimental.physics: IMU.create, IMUSensor",
        "needs": "An enabled rigid-body ancestor (a robot link with Rigid Body); press Play to get readings.",
    },
    "contact": {
        "title": "Contact sensor",
        "measures": "Whether the body is touching something and how hard (force in newtons on a metre/kg stage).",
        "typical_use": "Foot contact for walking robots, bumpers, detecting a successful grasp.",
        "prim_type": "IsaacContactSensor (isaacsim.robot.schema)",
        "data": "ContactSensor(path).get_data() -> in_contact, force, number_of_contacts, time.",
        "api_6_1": "isaacsim.sensors.experimental.physics: Contact.create, ContactSensor",
        "needs": "An enabled rigid-body ancestor with colliders; on PhysX that body gets PhysxContactReportAPI.",
    },
}

_LIVE_RUNTIMES: dict[tuple[Any, ...], Any] = {}


# ------------------------------------------------------------------------------------------------ read-only


def sensor_catalog() -> dict[str, Any]:
    """What each sensor kind measures, typical uses, the verified Isaac Sim 6.1 API and the old -> new names."""
    sensors = [{"kind": kind, **_CATALOG[kind], "params": list(_PARAMS[kind])} for kind in SENSOR_KINDS]
    return {
        "ok": True,
        "isaac_sim": VERIFIED["isaac_sim"],
        "sensors": sensors,
        "lidar_configs": [_lidar_aliases(path)[1] for path in LIDAR_CONFIG_ASSETS],
        "default_lidar_config": DEFAULT_LIDAR_CONFIG,
        "old_to_new": OLD_TO_NEW,
        "verified": VERIFIED,
        "how_sensors_attach": (
            "A sensor is a child prim of a robot link, so it moves with that link. The 'offset' is its position in "
            "the link's own frame; rotation turns it relative to the link. USD cameras look along their local -Z "
            "axis with +Y up; Isaac robots usually drive forward along +X with +Z up."
        ),
    }


def plan_sensor(
    kind: str,
    parent_path: str,
    *,
    stage: Any = None,
    name: str | None = None,
    offset: Any = (0.0, 0.0, 0.0),
    rotation: Any = None,
    look_at: Any = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Plan a sensor without writing anything: prim path/type, attributes, transform, Script Editor python,
    how to view the data and warnings.

    rotation is rotateXYZ degrees relative to the parent link; look_at (cameras only) is a world point (x, y, z) or
    a prim path to aim at and needs the stage. Without either, a camera faces the link's +X axis (robot forward)
    and other sensors keep the link's axes. With a stage the parent, rigid body, colliders and 'inside geometry'
    checks run; without one they are skipped (and say so).
    """
    warnings: list[str] = []
    try:
        plan = _plan(kind, parent_path, stage, name, offset, rotation, look_at, params, warnings)
    except _Refusal as refusal:
        return {"ok": False, "dry_run": True, "error": str(refusal), "warnings": _unique(warnings)}
    except Exception as exc:  # noqa: BLE001 - planning only reads
        return {"ok": False, "dry_run": True, "error": f"Unexpected error while planning (nothing was written): {exc}",
                "warnings": _unique(warnings)}
    return {"ok": True, "dry_run": True, **_public(plan), "warnings": _unique(warnings)}


def list_sensors(stage: Any, *, root_path: str = "/", max_prims: int = MAX_SCAN_PRIMS) -> dict[str, Any]:
    """Read-only: every camera and Isaac/RTX sensor prim under root_path and which robot link carries it."""
    if _PXR_IMPORT_ERROR is not None:
        return {"ok": False, "error": f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}"}
    if stage is None or not hasattr(stage, "GetPrimAtPath"):
        return {"ok": False, "error": "No USD stage is open."}
    root = stage.GetPrimAtPath(root_path) if isinstance(root_path, str) and root_path.startswith("/") else None
    if root is None or not root.IsValid():
        return {"ok": False, "error": f"root_path does not exist: {root_path}"}
    limit = max(1, min(int(max_prims), 200000)) if isinstance(max_prims, int) and not isinstance(max_prims, bool) else MAX_SCAN_PRIMS
    xform_cache = UsdGeom.XformCache(Usd.TimeCode.Default())
    found: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    for prim in Usd.PrimRange(root, Usd.TraverseInstanceProxies()):
        scanned += 1
        if scanned > limit:
            truncated = True
            break
        kind = _sensor_kind_of(prim)
        if kind is None:
            continue
        if len(found) >= MAX_LISTED:
            truncated = True
            break
        found.append(_describe_sensor(prim, kind, xform_cache))
    counts: dict[str, int] = {}
    for item in found:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    result: dict[str, Any] = {
        "ok": True,
        "root_path": str(root.GetPath()),
        "count": len(found),
        "counts": counts,
        "sensors": found,
        "scanned_prims": min(scanned, limit),
        "truncated": truncated,
    }
    if not found:
        result["note"] = "No cameras or Isaac sensor prims were found here."
    return result


# ---------------------------------------------------------------------------------------------------- write


def add_camera_sensor(
    stage: Any,
    parent_path: str,
    *,
    name: str | None = None,
    kind: str = "rgb_camera",
    offset: Any = (0.0, 0.0, 0.0),
    rotation: Any = None,
    look_at: Any = None,
    focal_length: float | None = None,
    horizontal_aperture: float | None = None,
    resolution: Any = None,
    clipping_range_m: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author a UsdGeom.Camera under a robot link with plain pxr.

    The camera is a child of parent_path, so it moves with that link. offset is its position in the link's frame;
    rotation (rotateXYZ degrees) or look_at (world point or prim path) turns it; with neither it faces the link's
    +X axis. focal_length / horizontal_aperture are millimetres (field of view = 2*atan(aperture / (2*focal));
    resolution (width, height) sets verticalAperture so pixels are square; clipping_range_m (near, far) is in
    metres and converted to stage units. Authors in the stage's current edit target; nothing is saved.
    """
    action = "add_camera_sensor"
    dry = authoring._dry(dry_run)
    warnings: list[str] = []
    params = {
        key: value
        for key, value in (
            ("focal_length", focal_length),
            ("horizontal_aperture", horizontal_aperture),
            ("resolution", resolution),
            ("clipping_range_m", clipping_range_m),
        )
        if value is not None
    }
    try:
        authoring._require_pxr()
        authoring._check_stage(stage)
        authoring._check_edit_target(stage, warnings)
        if kind not in CAMERA_KINDS:
            raise _Refusal(f"add_camera_sensor makes cameras; kind must be one of {', '.join(CAMERA_KINDS)}, got {kind!r}")
        plan = _plan(kind, parent_path, stage, name, offset, rotation, look_at, params, warnings)
    except _Refusal as refusal:
        return authoring._finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return authoring._finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")
    public = _public(plan)
    extra = {key: public[key] for key in ("python", "how_to_view_data")}
    if dry:
        return authoring._finish(action, True, stage, warnings, key="would_create", plan=_plan_summary(public), extra=extra)

    values = plan["usd_values"]
    sdf_path = plan["sdf_path"]
    xform = plan["xform"]

    def author() -> Any:
        camera = UsdGeom.Camera.Define(stage, sdf_path)
        prim = camera.GetPrim()
        for attr_name, value in values.items():
            if not prim.GetAttribute(attr_name).Set(value):
                raise ValueError(f"Could not set {attr_name}")
        authoring._author_new_xform(prim, xform["translation"], xform["rotation"])
        return prim

    result = authoring._create(action, stage, sdf_path, _plan_summary(public), xform, list(values), author, warnings)
    if result.get("written") and result.get("ok"):
        result.setdefault("created", {}).update(_world_pose(stage.GetPrimAtPath(sdf_path), camera=True))
    result.update(extra)
    return result


def add_physics_sensor(
    stage: Any,
    kind: str,
    parent_path: str,
    *,
    name: str | None = None,
    offset: Any = (0.0, 0.0, 0.0),
    rotation: Any = None,
    params: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (default) or author an IsaacImuSensor / IsaacContactSensor prim with plain pxr (typeName + attributes).

    The Isaac sensor schemas are typed Xformable prims whose attributes are plain bool/int/float values, so they
    can be authored safely without Isaac: typeName, 'enabled', the filter widths (IMU) or threshold/radius/color
    (contact), plus xformOp:translate + xformOp:orient like Isaac's own XformPrim. For a contact sensor the
    rigid-body ancestor also gets PhysxContactReportAPI (as Contact.create does on PhysX) unless
    params['apply_contact_report'] is False. A contact sensor without an enabled rigid-body ancestor is refused
    (Contact.create raises ValueError there). kind 'lidar' is plan-only: its OmniLidar settings come from a lidar
    asset, so the result carries python for Lidar.create instead of authoring anything.
    """
    action = "add_physics_sensor"
    dry = authoring._dry(dry_run)
    warnings: list[str] = []
    try:
        authoring._require_pxr()
        authoring._check_stage(stage)
        if kind in CAMERA_KINDS:
            raise _Refusal("Cameras are made with add_camera_sensor.")
        if kind == "lidar":
            plan = _plan(kind, parent_path, stage, name, offset, rotation, None, params, warnings)
            public = _public(plan)
            warnings.append(
                "RTX lidar is plan-only here: an OmniLidar prim needs the lidar asset's many sensor settings, so "
                "nothing was written. Run the python in Isaac Sim's Script Editor or use sensor_create."
            )
            return authoring._finish(action, True, stage, warnings, key="would_create", plan=_plan_summary(public),
                                     extra={"plan_only": True, "python": public["python"],
                                            "how_to_view_data": public["how_to_view_data"]})
        authoring._check_edit_target(stage, warnings)
        plan = _plan(kind, parent_path, stage, name, offset, rotation, None, params, warnings)
        if kind == "contact" and plan["rigid_body"] is None:
            raise _Refusal(
                f"A contact sensor needs an enabled rigid-body ancestor, but neither {plan['parent_path']} nor any "
                "prim above it has Rigid Body enabled (Isaac's Contact.create refuses this too). Put it under a robot link."
            )
        report_target = None
        if kind == "contact" and plan["values"]["apply_contact_report"]:
            report_target = stage.GetPrimAtPath(plan["rigid_body"])
            if report_target.IsInstanceProxy() or report_target.IsInPrototype():
                warnings.append(
                    f"{plan['rigid_body']} is inside an instance, so PhysxContactReportAPI cannot be added here; "
                    "PhysX will not report contacts until that body has it."
                )
                report_target = None
            elif "PhysxContactReportAPI" in (report_target.GetAppliedSchemas() or []):
                report_target = None  # already there
    except _Refusal as refusal:
        return authoring._finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return authoring._finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    public = _public(plan)
    if report_target is not None:
        public["also_modifies"] = {
            "path": str(report_target.GetPath()),
            "adds_api_schema": "PhysxContactReportAPI",
            "sets": {"physxContactReport:threshold": plan["values"]["min_threshold"]},
            "why": "PhysX only reports contacts for bodies with this API (Contact.create applies it the same way).",
        }
    summary = _plan_summary(public)
    extra = {key: public[key] for key in ("python", "how_to_view_data")}
    if dry:
        return authoring._finish(action, True, stage, warnings, key="would_create", plan=summary, extra=extra)

    sdf_path = plan["sdf_path"]
    schema = PHYSICS_SCHEMAS[kind]

    def author() -> Any:
        prim = stage.DefinePrim(sdf_path, schema["type"])
        for attr_name, (type_name, _default) in schema["attributes"].items():
            value = plan["usd_values"][attr_name]
            attr = prim.CreateAttribute(attr_name, getattr(Sdf.ValueTypeNames, _SDF_TYPES[type_name]), False)
            if not attr.Set(value):
                raise ValueError(f"Could not set {attr_name}")
        translate = prim.CreateAttribute("xformOp:translate", Sdf.ValueTypeNames.Double3, False)
        orient = prim.CreateAttribute("xformOp:orient", Sdf.ValueTypeNames.Quatd, False)
        order = prim.CreateAttribute("xformOpOrder", Sdf.ValueTypeNames.TokenArray, False, Sdf.VariabilityUniform)
        w, x, y, z = plan["quaternion"]
        if not (
            translate.Set(Gf.Vec3d(*plan["translation"]))
            and orient.Set(Gf.Quatd(w, Gf.Vec3d(x, y, z)))
            and order.Set(["xformOp:translate", "xformOp:orient"])
        ):
            raise ValueError("Could not author the sensor transform")
        if report_target is not None:
            if not report_target.AddAppliedSchema("PhysxContactReportAPI"):
                raise ValueError(f"Could not add PhysxContactReportAPI to {report_target.GetPath()}")
            threshold = report_target.CreateAttribute("physxContactReport:threshold", Sdf.ValueTypeNames.Float, False)
            if not threshold.Set(float(plan["values"]["min_threshold"])):
                raise ValueError("Could not set physxContactReport:threshold")
        return prim

    # The typed Isaac prim is not Xformable outside Isaac, so the generic xform readback is skipped (no target).
    xform = {"target_world": None}
    result = authoring._create(action, stage, sdf_path, summary, xform, list(schema["attributes"]), author, warnings)
    if result.get("written") and result.get("ok"):
        created = result.setdefault("created", {})
        created["xform_op_order"] = ["xformOp:translate", "xformOp:orient"]
        created.update(_physics_world_pose(stage, plan))
        if report_target is not None:
            created["contact_report_api_on"] = str(report_target.GetPath())
    result.update(extra)
    return result


# -------------------------------------------------------------------------------------------------- Kit-only


def create_sensor_live(
    kind: str,
    parent_path: str,
    *,
    name: str | None = None,
    offset: Any = (0.0, 0.0, 0.0),
    rotation: Any = None,
    look_at: Any = None,
    params: dict[str, Any] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Kit-only: create a sensor on the LIVE Isaac Sim stage with the verified 6.1 API (dry_run=True by default).

    Cameras: a UsdGeom.Camera via add_camera_sensor on the live stage. IMU / contact:
    isaacsim.sensors.experimental.physics IMU.create / Contact.create, else the deprecated
    'IsaacSensorCreateImuSensor' / 'IsaacSensorCreateContactSensor' commands. Lidar:
    isaacsim.sensors.experimental.rtx Lidar.create(config=...), else the deprecated 'IsaacSensorCreateRtxLidar'.
    """
    usd = _kit_module("omni.usd")
    if usd is None:
        return _not_kit("omni.usd is not importable")
    from . import joint_control  # local: keeps this module's import light

    # Seen live in Composer: the dry run planned happily and invited dry_run=False in an app without Isaac.
    needs_isaac = joint_control._isaac_problem(sensors=True, kit_module=_kit_module)
    if needs_isaac:
        return {"ok": False, "dry_run": authoring._dry(dry_run), "error": needs_isaac, "message": needs_isaac}
    stage = _live_stage(usd)
    if stage is None:
        return {"ok": False, "message": "No stage is open in Kit."}
    dry = authoring._dry(dry_run)
    if kind in CAMERA_KINDS:
        values = dict(params or {})
        unknown = sorted(set(values) - set(_PARAMS[kind]))
        if unknown:
            return {"ok": False, "dry_run": dry, "error": f"Unknown params for {kind}: {', '.join(unknown)}"}
        result = add_camera_sensor(stage, parent_path, name=name, kind=kind, offset=offset, rotation=rotation,
                                   look_at=look_at, dry_run=dry, **values)
        result["api"] = "pxr UsdGeom.Camera.Define on the live stage (not on Kit's undo stack)"
        return result

    public = plan_sensor(kind, parent_path, stage=stage, name=name, offset=offset, rotation=rotation,
                         look_at=look_at, params=params)
    if not public.get("ok"):
        return public
    if dry:
        public["would_call"] = _live_api_text(kind)
        public["next_step"] = "Dry run: nothing was created. Call again with dry_run=False to create it in Isaac Sim."
        return public
    # The live calls need the internal plan (values, translation, quaternion), which the public answer leaves out.
    plan = _plan(kind, parent_path, stage, name, offset, rotation, look_at, params, [])
    plan["warnings"] = public["warnings"]
    try:
        if kind == "lidar":
            created_path, api = _create_lidar_live(plan)
        else:
            created_path, api = _create_physics_live(kind, plan)
    except KeyError as exc:  # a KeyError is a LookupError too, but it means a bug here, not a missing Kit API
        return {"ok": False, "dry_run": False, "error": f"Creating the {kind} failed: KeyError: {exc}",
                "warnings": plan["warnings"]}
    except LookupError as exc:
        return {"ok": False, "dry_run": False, "error": str(exc), "warnings": plan["warnings"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dry_run": False, "error": f"Creating the {kind} failed: {type(exc).__name__}: {exc}",
                "warnings": plan["warnings"]}
    prim = stage.GetPrimAtPath(created_path) if created_path else None
    exists = bool(prim) and prim.IsValid()
    result = {
        "ok": exists,
        "dry_run": False,
        "written": exists,
        "kind": kind,
        "requested_path": plan["prim_path"],
        "prim_path": created_path,
        "prim_type": prim.GetTypeName() if exists else None,
        "api": api,
        "how_to_view_data": plan["how_to_view_data"],
        "warnings": plan["warnings"],
        "note": "Created on the live stage; save the scene to keep it. read_sensor_live(prim_path) reads it while playing.",
    }
    if not exists:
        result["error"] = f"The {api} call returned but no prim was found at {created_path or plan['prim_path']}."
    elif created_path != plan["prim_path"]:
        result["warnings"] = [*plan["warnings"], f"Isaac picked the free path {created_path} instead of {plan['prim_path']}."]
    return result


def read_sensor_live(prim_path: str, *, resolution: Any = None) -> dict[str, Any]:
    """Kit-only: a SUMMARY of a sensor's latest data (never the raw arrays).

    Camera: rgb and depth shape/min/max/mean; lidar: point count; IMU: linear_acceleration, angular_velocity,
    orientation; contact: in_contact, force, number_of_contacts. The reader (render product / sensor runtime) is
    made on the first call and reused (at most MAX_LIVE_RUNTIMES); cameras and lidar need a few rendered frames
    before data appears. resolution (width, height) is only used the first time a camera is read.
    """
    usd = _kit_module("omni.usd")
    if usd is None:
        return _not_kit("omni.usd is not importable")
    stage = _live_stage(usd)
    if stage is None:
        return {"ok": False, "message": "No stage is open in Kit."}
    if not isinstance(prim_path, str) or not prim_path.startswith("/"):
        return {"ok": False, "error": "prim_path must be an absolute prim path such as /World/robot/base_link/imu"}
    prim = stage.GetPrimAtPath(prim_path)
    if not prim or not prim.IsValid():
        return {"ok": False, "error": f"No prim at {prim_path}"}
    kind = _sensor_kind_of(prim)
    if kind not in ("camera", "lidar", "imu", "contact"):
        return {"ok": False, "error": f"{prim_path} ({prim.GetTypeName() or 'typeless'}) is not a camera, lidar, IMU or contact sensor."}
    playing = _timeline_playing()
    try:
        if kind == "camera":
            width, height = _resolution(resolution if resolution is not None else _CAMERA_DEFAULTS["resolution"])
            data = _read_camera(prim_path, width, height)
        elif kind == "lidar":
            data = _read_lidar(prim_path)
        else:
            data = _read_physics(kind, prim_path)
    except LookupError as exc:
        return {"ok": False, "kind": kind, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "kind": kind, "error": f"Reading {prim_path} failed: {type(exc).__name__}: {exc}"}
    result = {"ok": True, "kind": kind, "prim_path": prim_path, "playing": playing, **data}
    if playing is False:
        result["note"] = "The simulation is not playing: sensors only update while it plays (press Play), so this may be stale."
    return result


def release_sensor_live(prim_path: str | None = None) -> dict[str, Any]:
    """Forget the cached live readers (all, or those for prim_path) so their render products can be freed."""
    keys = [key for key in _LIVE_RUNTIMES if prim_path is None or key[1] == prim_path]
    for key in keys:
        _drop_runtime(key)
    return {"ok": True, "released": [key[1] for key in keys], "remaining": len(_LIVE_RUNTIMES)}


# ------------------------------------------------------------------------------------------- planning core


_SDF_TYPES = {"bool": "Bool", "int": "Int", "float": "Float", "float2": "Float2", "float4": "Float4"}


def _plan(
    kind: Any, parent_path: Any, stage: Any, name: Any, offset: Any, rotation: Any, look_at: Any,
    params: Any, warnings: list[str],
) -> dict[str, Any]:
    """Everything about one sensor; raises _Refusal. Reads the stage (when given) and never writes."""
    if kind not in SENSOR_KINDS:
        raise _Refusal(f"kind must be one of {', '.join(SENSOR_KINDS)}, got {kind!r}")
    parent_text = _parent_text(parent_path)
    sensor_name = DEFAULT_NAMES[kind] if name is None else name
    if not isinstance(sensor_name, str) or not _NAME.match(sensor_name):
        raise _Refusal(f"name must be a simple prim name (letters, digits, _; not starting with a digit), got {name!r}")
    prim_path = ("" if parent_text == "/" else parent_text) + "/" + sensor_name
    translation = authoring._vec3(offset if offset is not None else (0.0, 0.0, 0.0), "offset")
    if rotation is not None and look_at is not None:
        raise _Refusal("Give either rotation or look_at, not both.")
    if look_at is not None and kind not in CAMERA_KINDS:
        raise _Refusal("look_at aims a camera's lens (-Z); for other sensors give rotation (degrees about X, Y, Z).")
    if look_at is not None and stage is None:
        raise _Refusal("look_at needs the stage to find the target and the link's world pose; pass stage=...")
    values = _params(kind, params, warnings)
    meters_per_unit = 1.0
    sdf_path = parent = None
    if stage is not None:
        authoring._require_pxr()
        authoring._check_stage(stage)
        sdf_path, parent = authoring._creation_target(stage, prim_path, warnings)
        meters_per_unit = float(UsdGeom.GetStageMetersPerUnit(stage)) or 1.0
    elif _PXR_IMPORT_ERROR is None:
        authoring._prim_path(prim_path, "sensor path")
    up_axis = str(UsdGeom.GetStageUpAxis(stage)) if stage is not None else "Z"
    if up_axis not in ("Y", "Z"):
        up_axis = "Z"

    xform: dict[str, Any]
    if kind in CAMERA_KINDS:
        if look_at is not None:
            xform = authoring._plan_new_xform(stage, sdf_path, parent, translation, look_at, None, warnings)
            rotation_xyz = tuple(xform["rotation"])
            aim = xform["public"]["aim"]
            facing = "aimed at the look_at target"
        else:
            if rotation is not None:
                rotation_xyz = authoring._vec3(rotation, "rotation")
                facing = "the given rotation"
            else:
                ahead = (translation[0] + 1.0, translation[1], translation[2])
                look = look_at_module().look_at_rotation(translation, ahead, up_axis)
                rotation_xyz = tuple(look["rotate_xyz"])
                facing = f"the link's +X axis (robot forward), level with the {up_axis} up axis"
            xform = {"translation": translation, "rotation": rotation_xyz, "target_world": None}
            aim = None
        quaternion = look_at.matrix_to_quaternion(look_at.euler_to_matrix(rotation_xyz, "XYZ")) if False else \
            look_at_module().matrix_to_quaternion(look_at_module().euler_to_matrix(rotation_xyz, "XYZ"))
    else:
        rotation_xyz = authoring._vec3(rotation, "rotation") if rotation is not None else (0.0, 0.0, 0.0)
        quaternion = look_at_module().matrix_to_quaternion(look_at_module().euler_to_matrix(rotation_xyz, "XYZ"))
        xform = {"translation": translation, "rotation": rotation_xyz, "target_world": None}
        aim = None
        facing = "the link's own axes" if rotation is None else "the given rotation"

    plan: dict[str, Any] = {
        "kind": kind,
        "prim_path": prim_path,
        "parent_path": parent_text,
        "sdf_path": sdf_path,
        "translation": tuple(translation),
        "rotation_xyz": tuple(rotation_xyz),
        "quaternion": tuple(_clean(v) for v in quaternion),
        "aim": aim,
        "facing": facing,
        "xform": xform,
        "values": values,
        "meters_per_unit": meters_per_unit,
        "up_axis": up_axis,
        "stage_checked": stage is not None,
        "rigid_body": None,
    }
    plan["usd_values"], plan["attributes"], plan["explained"] = _attributes(kind, values, meters_per_unit)
    if stage is not None:
        _stage_checks(stage, plan, parent, warnings)
    else:
        warnings.append("No stage was given, so the parent, rigid-body and inside-geometry checks were skipped.")
    plan["python"] = _python(plan)
    plan["how_to_view_data"] = _how_to_view(kind)
    return plan


def look_at_module() -> Any:
    """The look_at math module (the look_at keyword argument shadows its name inside _plan)."""
    return _LOOK_AT


_LOOK_AT = look_at


def _parent_text(parent_path: Any) -> str:
    if not isinstance(parent_path, str) or not parent_path.strip().startswith("/"):
        raise _Refusal("parent_path must be an absolute prim path such as /World/robot/base_link")
    text = parent_path.strip()
    if text != "/":
        text = text.rstrip("/")
    if len(text) > authoring.MAX_PATH_CHARS:
        raise _Refusal(f"parent_path is longer than {authoring.MAX_PATH_CHARS} characters")
    if text != "/" and not all(_NAME.match(part) for part in text.split("/")[1:]):
        raise _Refusal(f"parent_path is not a valid prim path: {text}")
    return text


def _params(kind: str, params: Any, warnings: list[str]) -> dict[str, Any]:
    given = dict(params or {}) if isinstance(params, dict) or params is None else None
    if given is None:
        raise _Refusal("params must be a dict such as {'focal_length': 24}")
    unknown = sorted(set(given) - set(_PARAMS[kind]))
    if unknown:
        raise _Refusal(f"Unknown params for {kind}: {', '.join(unknown)}. Accepted: {', '.join(_PARAMS[kind])}")
    if kind in CAMERA_KINDS:
        values = dict(_CAMERA_DEFAULTS)
        if "focal_length" in given:
            values["focal_length"] = authoring._number(given["focal_length"], "focal_length", minimum=0.0, exclusive=True)
        if "horizontal_aperture" in given:
            values["horizontal_aperture"] = authoring._number(given["horizontal_aperture"], "horizontal_aperture", minimum=0.0, exclusive=True)
        if "resolution" in given:
            values["resolution"] = _resolution(given["resolution"])
        if "clipping_range_m" in given:
            values["clipping_range_m"] = authoring._clipping_range(given["clipping_range_m"])
        return values
    if kind == "lidar":
        config = given.get("config")
        usd_path = given.get("usd_path")
        variant = given.get("variant")
        if config is not None and usd_path is not None:
            raise _Refusal("Give either params['config'] or params['usd_path'], not both (Lidar.create refuses both).")
        if usd_path is not None:
            if not isinstance(usd_path, str) or not usd_path.strip():
                raise _Refusal("params['usd_path'] must be a lidar USD file path")
            return {"config": None, "usd_path": usd_path.strip(), "variant": variant, "asset": usd_path.strip()}
        if config is None:
            config = DEFAULT_LIDAR_CONFIG
            warnings.append(
                f"No lidar config was given, so '{DEFAULT_LIDAR_CONFIG}' (a generic spinning lidar, "
                "/Isaac/Sensors/NVIDIA/Example_Rotary.usda) is planned. Pass params={'config': ...} to choose one."
            )
        asset = _match_lidar_config(config)
        stem = _lidar_aliases(asset)[1]
        if variant is not None and not isinstance(variant, (str, dict)):
            raise _Refusal("params['variant'] must be a variant name (or a dict of variant set -> name)")
        if stem in _LIDAR_WITH_VARIANTS and variant is None:
            warnings.append(f"{stem} has several variants (e.g. {stem}_REV6_32ch20hz512res); without one the asset's default is used.")
        if stem not in _LIDAR_WITH_VARIANTS and isinstance(variant, str) and not stem.startswith("SICK_"):
            warnings.append(f"{stem} has no 'sensor' variants in Isaac Sim 6.1; the variant {variant!r} may be rejected.")
        warnings.append(
            "Lidar assets load from the Isaac Sim assets root (get_assets_root_path()); without internet or a "
            "local asset copy Lidar.create cannot find the file."
        )
        return {"config": config, "usd_path": None, "variant": variant, "asset": asset}
    if kind == "imu":
        values = {}
        for key in _PARAMS["imu"]:
            raw = given.get(key, 1)
            if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 1000:
                raise _Refusal(f"{key} must be a whole number from 1 to 1000 (readings averaged), got {raw!r}")
            values[key] = raw
        return values
    # contact
    min_threshold = authoring._number(given.get("min_threshold", 0.0), "min_threshold", minimum=0.0)
    max_threshold = authoring._number(given.get("max_threshold", 100000.0), "max_threshold", minimum=0.0)
    if max_threshold <= min_threshold:
        raise _Refusal(f"max_threshold ({max_threshold:g}) must be greater than min_threshold ({min_threshold:g})")
    radius = authoring._number(given.get("radius", -1.0), "radius")
    color = given.get("color", (1.0, 1.0, 1.0, 1.0))
    items = authoring._items(color, 4, "color")
    color_values = tuple(authoring._number(item, "color", minimum=0.0, maximum=1.0) for item in items)
    apply_report = given.get("apply_contact_report", True)
    if not isinstance(apply_report, bool):
        raise _Refusal("apply_contact_report must be True or False")
    return {"min_threshold": min_threshold, "max_threshold": max_threshold, "radius": radius,
            "color": color_values, "apply_contact_report": apply_report}


def _attributes(kind: str, values: dict[str, Any], mpu: float) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """(USD values to author, JSON attributes, plain-words explanation)."""
    if kind in CAMERA_KINDS:
        width, height = values["resolution"]
        h_ap = values["horizontal_aperture"]
        v_ap = h_ap * height / width
        near_m, far_m = values["clipping_range_m"]
        near, far = near_m / mpu, far_m / mpu
        fov = math.degrees(2.0 * math.atan(h_ap / (2.0 * values["focal_length"])))
        json_attrs = {
            "focalLength": _clean(values["focal_length"]),
            "horizontalAperture": _clean(h_ap),
            "verticalAperture": _clean(v_ap),
            "clippingRange": [_clean(near), _clean(far)],
            "projection": "perspective",
        }
        usd = {}
        if _PXR_IMPORT_ERROR is None:
            usd = {
                "focalLength": float(values["focal_length"]),
                "horizontalAperture": float(h_ap),
                "verticalAperture": float(v_ap),
                "clippingRange": Gf.Vec2f(near, far),
            }
        explained = {
            "focalLength": f"{values['focal_length']:g} mm lens. Bigger zooms in (narrower view).",
            "horizontalAperture": f"{h_ap:g} mm sensor width. With the focal length it gives a {fov:.1f} degree horizontal field of view.",
            "verticalAperture": f"{v_ap:.4g} mm = width x {height}/{width}, so a {width}x{height} image has square pixels.",
            "clippingRange": f"Sees from {near_m:g} m to {far_m:g} m ({near:g} to {far:g} stage units at {mpu:g} m per unit); things nearer than 'near' are cut away.",
            "resolution": f"{width}x{height} pixels is chosen when reading (CameraSensor resolution=(height, width)), not stored on the prim.",
            "direction": "A USD camera looks along its local -Z axis with +Y up.",
        }
        if kind == "depth_camera":
            explained["depth"] = "Depth is read with the 'distance_to_image_plane' annotator: stage units per pixel, inf where nothing is hit."
        return usd, json_attrs, explained
    if kind == "lidar":
        json_attrs = {"config": values["config"], "usd_path": values["usd_path"], "variant": values["variant"],
                      "asset": values["asset"]}
        explained = {
            "config": "Which lidar model to load; its scan pattern, range and channels come from that asset.",
            "prim": "Lidar.create makes an OmniLidar prim (inside a reference to the asset when the asset nests it).",
            "direction": "Spinning lidars sweep 360 degrees around their local up axis; check the point cloud in the viewport.",
        }
        return {}, json_attrs, explained
    schema = PHYSICS_SCHEMAS[kind]
    if kind == "imu":
        chosen = {
            "enabled": True,
            "linearAccelerationFilterWidth": values["linear_acceleration_filter_size"],
            "angularVelocityFilterWidth": values["angular_velocity_filter_size"],
            "orientationFilterWidth": values["orientation_filter_size"],
        }
        explained = {
            "enabled": "On/off switch for the sensor.",
            "linearAccelerationFilterWidth": "How many readings are averaged for acceleration (1 = raw, bigger = smoother but slower).",
            "angularVelocityFilterWidth": "Same averaging for turning speed.",
            "orientationFilterWidth": "Same averaging for orientation.",
            "units": "At rest the IMU reads about +9.81 on the axis pointing away from gravity (metre stage): it measures specific force like a real accelerometer.",
        }
    else:
        chosen = {
            "enabled": True,
            "threshold": (values["min_threshold"], values["max_threshold"]),
            "radius": values["radius"],
            "color": values["color"],
        }
        explained = {
            "enabled": "On/off switch for the sensor.",
            "threshold": "Forces below min or above max are ignored (newtons on a metre/kilogram stage).",
            "radius": "Only contacts within this distance of the sensor count; -1 means the whole body.",
            "color": "Colour of the sensor's debug sphere (R, G, B, A from 0 to 1).",
        }
    usd: dict[str, Any] = {}
    if _PXR_IMPORT_ERROR is not None:
        return usd, {key: _json(value) for key, value in chosen.items()}, explained
    for attr_name, (type_name, _default) in schema["attributes"].items():
        value = chosen[attr_name]
        if type_name == "float2":
            usd[attr_name] = Gf.Vec2f(*value)
        elif type_name == "float4":
            usd[attr_name] = Gf.Vec4f(*value)
        elif type_name == "float":
            usd[attr_name] = float(value)
        else:
            usd[attr_name] = value
    return usd, {key: _json(value) for key, value in chosen.items()}, explained


def _stage_checks(stage: Any, plan: dict[str, Any], parent: Any, warnings: list[str]) -> None:
    kind = plan["kind"]
    rigid = _rigid_ancestor(parent)
    plan["rigid_body"] = str(rigid.GetPath()) if rigid is not None else None
    scales = _world_scales(parent)
    if scales and max(scales) > 1.01 * min(scales):
        # Seen live in Isaac 6.1: a camera under a Cube scaled (0.5, 0.3, 0.1) sat 0.2 m above it for offset z = 2,
        # and its Replicator image shrank a 1 x 2 m door 3 m away to a dot; under /World the door filled the frame.
        shown = ", ".join(f"{value:g}" for value in scales)
        warnings.append(
            f"{plan['parent_path']} is scaled unevenly ({shown}), and a child sensor inherits that: the offset is "
            "squashed by the same amounts and a camera's picture comes out badly distorted. Put sensors under an "
            "unscaled Xform (e.g. the link itself, with the scaled shape as a sibling)."
        )
    if kind == "imu" and rigid is None:
        warnings.append(
            f"No enabled rigid body at or above {plan['parent_path']}: the IMU finds its body by walking up the "
            "hierarchy, so it will read nothing. Put it under a robot link that has Rigid Body enabled."
        )
    if kind == "contact":
        if rigid is None:
            warnings.append(
                f"No enabled rigid body at or above {plan['parent_path']}: Contact.create raises ValueError here, and "
                "add_physics_sensor refuses. Put it under a robot link (e.g. a foot or gripper finger) with Rigid Body."
            )
        elif not _has_collider(rigid):
            warnings.append(
                f"{rigid.GetPath()} has no collider (CollisionAPI) under it, so nothing can touch it and the contact "
                "sensor will always read 'no contact'. Add a collision shape to the link."
            )
    if kind in CAMERA_KINDS or kind == "lidar":
        if rigid is None and kind in CAMERA_KINDS:
            warnings.append(
                f"{plan['parent_path']} is not (inside) a rigid-body link, so the sensor only moves if that prim "
                "moves. To ride on a robot, put it under one of the robot's links."
            )
        parent_world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(parent)
        eye = parent_world.Transform(Gf.Vec3d(*plan["translation"]))
        plan["world_position"] = [_clean(v) for v in eye]
        inside = _inside_geometry(stage, eye)
        if inside:
            listed = ", ".join(inside[:3]) + (" ..." if len(inside) > 3 else "")
            what = "camera" if kind in CAMERA_KINDS else "lidar"
            warnings.append(
                f"The {what} position {plan['world_position']} is inside the bounding box of {listed}; it may be "
                "inside that mesh and see only its inside. Move it outward with offset or, for a camera, raise the "
                "near clipping distance."
            )


def _world_scales(prim: Any) -> list[float]:
    """The prim's world scale along its own x, y and z axes (lengths of the matrix rows); [] when unknown."""
    try:
        matrix = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)
        return [round(Gf.Vec3d(matrix[row][0], matrix[row][1], matrix[row][2]).GetLength(), 6) for row in range(3)]
    except Exception:  # noqa: BLE001 - a non-xformable parent simply skips the check
        return []


def _rigid_ancestor(prim: Any) -> Any:
    """Nearest prim at or above prim with an enabled rigid body (the rule Isaac's Contact._find_physics_parent uses)."""
    current = prim
    while current and current.IsValid() and current.GetPath() != Sdf.Path.absoluteRootPath:
        attr = current.GetAttribute("physics:rigidBodyEnabled")
        if current.HasAPI(UsdPhysics.RigidBodyAPI):
            if not attr or not attr.IsValid() or attr.Get() is not False:
                return current
        elif attr and attr.IsValid() and attr.HasAuthoredValue() and attr.Get():
            return current
        current = current.GetParent()
    return None


def _has_collider(body: Any, limit: int = 2000) -> bool:
    for count, prim in enumerate(Usd.PrimRange(body, Usd.TraverseInstanceProxies())):
        if count >= limit:
            return True  # too big to check; do not warn
        if prim.HasAPI(UsdPhysics.CollisionAPI):
            return True
    return False


def _inside_geometry(stage: Any, point: Any) -> list[str]:
    """Gprims whose world axis-aligned box contains point (a cheap, conservative 'inside a mesh' test)."""
    tokens = [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), tokens, useExtentsHint=True)
    hits: list[str] = []
    for count, prim in enumerate(Usd.PrimRange(stage.GetPseudoRoot(), Usd.TraverseInstanceProxies())):
        if count >= MAX_GEOMETRY_PRIMS:
            break
        if not prim.IsA(UsdGeom.Gprim):
            continue
        box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
        if box.IsEmpty():
            continue
        lo, hi = box.GetMin(), box.GetMax()
        if all(lo[i] < point[i] < hi[i] for i in range(3)):
            hits.append(str(prim.GetPath()))
    return hits


def _world_pose(prim: Any, *, camera: bool) -> dict[str, Any]:
    world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(prim)
    pose = {"world_translation": [_clean(v) for v in world.ExtractTranslation()]}
    if camera:
        forward = world.TransformDir(Gf.Vec3d(0.0, 0.0, -1.0))
        if forward.GetLength() > 1e-12:
            pose["forward_world"] = [_clean(v) for v in forward.GetNormalized()]
    return pose


def _physics_world_pose(stage: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """World position of a typed Isaac sensor (not Xformable in plain USD, so composed by hand: local * parent)."""
    parent = stage.GetPrimAtPath(plan["parent_path"])
    parent_world = UsdGeom.XformCache(Usd.TimeCode.Default()).GetLocalToWorldTransform(parent)
    w, x, y, z = plan["quaternion"]
    local = Gf.Matrix4d(1.0)
    local.SetRotateOnly(Gf.Quatd(w, Gf.Vec3d(x, y, z)))
    local.SetTranslateOnly(Gf.Vec3d(*plan["translation"]))
    world = local * parent_world
    return {"world_translation": [_clean(v) for v in world.ExtractTranslation()]}


# ------------------------------------------------------------------------------------------- lidar configs


def _lidar_aliases(config_path: str) -> tuple[str, str, str, str, str]:
    """Same five aliases as Isaac's _config_aliases: full path, stem, stem with spaces, vendor-less stem (+ spaces)."""
    path = pathlib.PurePosixPath(config_path)
    vendor = path.parts[3] if len(path.parts) > 3 else ""
    stem = path.stem
    no_vendor = stem[len(vendor) + 1:] if vendor and stem.startswith(vendor + "_") else stem
    return (config_path, stem, stem.replace("_", " "), no_vendor, no_vendor.replace("_", " "))


def _match_lidar_config(config: Any) -> str:
    if not isinstance(config, str) or not config.strip():
        raise _Refusal("params['config'] must be a lidar name such as 'Example_Rotary' or 'OS1'")
    text = config.strip()
    names: list[str] = []
    for asset in LIDAR_CONFIG_ASSETS:
        aliases = _lidar_aliases(asset)
        if text in aliases:
            return asset
        names.extend(aliases[1:])
    close = difflib.get_close_matches(text, names, n=3, cutoff=0.5)
    hint = f" Did you mean {', '.join(repr(name) for name in close)}?" if close else ""
    short = ", ".join(_lidar_aliases(asset)[3] for asset in LIDAR_CONFIG_ASSETS)
    raise _Refusal(f"Unknown lidar config {text!r}.{hint} Isaac Sim 6.1 knows: {short}")


# ------------------------------------------------------------------------------------------- python text


def _python(plan: dict[str, Any]) -> str:
    kind = plan["kind"]
    path = plan["prim_path"]
    t = ", ".join(_fmt(v) for v in plan["translation"])
    r = ", ".join(_fmt(v) for v in plan["rotation_xyz"])
    q = ", ".join(_fmt(v) for v in plan["quaternion"])
    rotated = any(abs(v) > 1e-9 for v in plan["rotation_xyz"])
    head = (
        "# Isaac Sim 6.1: open Window > Script Editor, paste this, press Run.\n"
        f"# It adds a {kind.replace('_', ' ')} at {path}. Being a child of {plan['parent_path']},\n"
        "# it moves together with that link.\n"
    )
    if kind in CAMERA_KINDS:
        a = plan["attributes"]
        width, height = plan["values"]["resolution"]
        annotator = "distance_to_image_plane" if kind == "depth_camera" else "rgb"
        reading = (
            "depth, info = sensor.get_data(\"distance_to_image_plane\")  # None while warming up\n"
            "if depth is not None:\n"
            "    d = depth.numpy()                      # (height, width, 1) distances in stage units\n"
            "    print(\"depth image\", d.shape, \"closest\", d[d < float(\"inf\")].min())\n"
            if kind == "depth_camera" else
            "rgb, info = sensor.get_data(\"rgb\")       # None for the first few frames (warming up)\n"
            "if rgb is not None:\n"
            "    image = rgb.numpy()                    # (height, width, 3) numbers from 0 to 255\n"
            "    print(\"picture\", image.shape, \"brightest\", image.max())\n"
        )
        return head + (
            "from pxr import Gf, UsdGeom\n"
            "import omni.usd\n\n"
            "stage = omni.usd.get_context().get_stage()   # the scene that is open right now\n"
            f"camera = UsdGeom.Camera.Define(stage, \"{path}\")\n"
            f"camera.CreateFocalLengthAttr({_fmt(a['focalLength'])})            # lens in mm: bigger = zoomed in\n"
            f"camera.CreateHorizontalApertureAttr({_fmt(a['horizontalAperture'])})  # sensor width in mm (sets the field of view)\n"
            f"camera.CreateVerticalApertureAttr({_fmt(a['verticalAperture'])})   # sensor height = width * {height}/{width}: square pixels\n"
            f"camera.CreateClippingRangeAttr(Gf.Vec2f({_fmt(a['clippingRange'][0])}, {_fmt(a['clippingRange'][1])}))  # nearest / farthest it sees (stage units)\n"
            "xform = UsdGeom.Xformable(camera)\n"
            f"xform.AddTranslateOp().Set(Gf.Vec3d({t}))   # position in the link's frame\n"
            f"xform.AddRotateXYZOp().Set(Gf.Vec3f({r}))   # turn the lens (-Z): {plan['facing']}\n\n"
            "# --- read the camera (press Play first; run this part again after a few frames) ---\n"
            "from isaacsim.sensors.experimental.rtx import RtxCamera, CameraSensor\n"
            f"sensor = CameraSensor(RtxCamera(\"{path}\"), resolution=({height}, {width}),  # (height, width)\n"
            f"                      annotators=[\"{annotator}\"])\n"
            + reading
        )
    if kind == "lidar":
        values = plan["values"]
        source = f"usd_path={values['usd_path']!r}" if values["usd_path"] else f"config={values['config']!r}"
        variant = f", variant={values['variant']!r}" if values["variant"] is not None else ""
        orient = f",\n    orientations=[[{q}]],  # turn (w, x, y, z) relative to the link" if rotated else ""
        return head + (
            "from isaacsim.sensors.experimental.rtx import Lidar, LidarSensor, parse_generic_model_output_data\n\n"
            f"lidar = Lidar.create(\n    path=\"{path}\",\n    {source}{variant},  # which lidar model to load\n"
            f"    translations=[[{t}]]{orient},  # position in the link's frame\n)\n"
            "sensor = LidarSensor(lidar, annotators=[\"generic-model-output\"])\n"
            "sensor.attach_writer(\"RtxSensorDebugDrawPointCloud\")   # draw the points in the viewport\n\n"
            "# --- press Play, wait a few frames, then run: ---\n"
            "data, info = sensor.get_data(\"generic-model-output\")\n"
            "scan = parse_generic_model_output_data(data)\n"
            "print(\"lidar points this frame:\", scan.numElements)\n"
        )
    orient = f",\n    orientations=[[{q}]],  # turn (w, x, y, z) relative to the link" if rotated else ""
    if kind == "imu":
        v = plan["values"]
        return head + (
            "from isaacsim.sensors.experimental.physics import IMU, IMUSensor\n\n"
            "# The IMU must sit under a link with Rigid Body: it measures how that body moves.\n"
            f"imu = IMU.create(\n    \"{path}\",\n    translations=[[{t}]]{orient},  # position in the link's frame\n"
            f"    linear_acceleration_filter_size={v['linear_acceleration_filter_size']},  # readings averaged (1 = raw)\n"
            f"    angular_velocity_filter_size={v['angular_velocity_filter_size']},\n"
            f"    orientation_filter_size={v['orientation_filter_size']},\n)\n"
            "sensor = IMUSensor(imu)\n\n"
            "# --- press Play, then run: ---\n"
            "frame = sensor.get_data()\n"
            "print(\"acceleration\", frame[\"linear_acceleration\"])   # about +9.81 upward when still (metre stage)\n"
            "print(\"turning speed (rad/s)\", frame[\"angular_velocity\"])\n"
            "print(\"orientation (w, x, y, z)\", frame[\"orientation\"])\n"
        )
    v = plan["values"]
    return head + (
        "from isaacsim.sensors.experimental.physics import Contact, ContactSensor\n\n"
        "# The contact sensor must sit under a link with Rigid Body and colliders (e.g. a foot).\n"
        "# On PhysX, Contact.create also adds PhysxContactReportAPI to that rigid body.\n"
        f"contact = Contact.create(\n    \"{path}\",\n    translations=[[{t}]]{orient},  # position in the link's frame\n"
        f"    min_threshold={_fmt(v['min_threshold'])},    # ignore touches lighter than this (N)\n"
        f"    max_threshold={_fmt(v['max_threshold'])},  # and heavier than this\n"
        f"    radius={_fmt(v['radius'])},  # -1 = contacts anywhere on the body count\n)\n"
        "sensor = ContactSensor(contact)\n\n"
        "# --- press Play, then run: ---\n"
        "frame = sensor.get_data()\n"
        "print(\"touching?\", frame[\"in_contact\"], \"force\", frame[\"force\"], \"contacts\", frame[\"number_of_contacts\"])\n"
    )


def _how_to_view(kind: str) -> list[str]:
    if kind in CAMERA_KINDS:
        steps = [
            "Look through it: in the Viewport, open the camera menu (top left) and pick the new camera; or open "
            "Window > Viewport > Viewport 2 and choose it there so the main view stays free.",
            "Press Play and run the 'read the camera' lines of the python (or sensor_read(prim_path)).",
        ]
        if kind == "depth_camera":
            steps.append("Depth is 'distance_to_image_plane': stage units per pixel, inf where the ray hits nothing.")
        return steps
    if kind == "lidar":
        return [
            "Press Play: sensor.attach_writer('RtxSensorDebugDrawPointCloud') draws the points in the viewport.",
            "parse_generic_model_output_data(data).numElements is the number of points this frame "
            "(read_sensor_live(prim_path) reports it too).",
        ]
    if kind == "imu":
        return [
            "Press Play, then IMUSensor(path).get_data() (or sensor_read(prim_path)).",
            "Still robot: about +9.81 on the axis pointing up, ~0 turning speed. Moving or tilting it changes the numbers.",
        ]
    return [
        "Press Play, then ContactSensor(path).get_data() (or sensor_read(prim_path)).",
        "in_contact turns True and force grows when the link presses on something.",
    ]


def _live_api_text(kind: str) -> str:
    if kind == "lidar":
        return "isaacsim.sensors.experimental.rtx.Lidar.create(...) (fallback: omni.kit.commands 'IsaacSensorCreateRtxLidar')"
    if kind == "imu":
        return "isaacsim.sensors.experimental.physics.IMU.create(...) (fallback: omni.kit.commands 'IsaacSensorCreateImuSensor')"
    return "isaacsim.sensors.experimental.physics.Contact.create(...) (fallback: omni.kit.commands 'IsaacSensorCreateContactSensor')"


# ------------------------------------------------------------------------------------------- Kit internals


def _kit_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - ImportError, or a Kit extension that fails while loading
        return None


def _not_kit(detail: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "message": _NOT_KIT}
    if detail:
        result["detail"] = detail
    return result


def _live_stage(usd: Any) -> Any:
    try:
        return usd.get_context().get_stage()
    except Exception:  # noqa: BLE001
        return None


def _timeline_playing() -> bool | None:
    timeline = _kit_module("omni.timeline")
    getter = getattr(timeline, "get_timeline_interface", None)
    if not callable(getter):
        return None
    try:
        return bool(getter().is_playing())
    except Exception:  # noqa: BLE001
        return None


def _first_path(obj: Any, fallback: str) -> str:
    paths = getattr(obj, "paths", None)
    if isinstance(paths, (list, tuple)) and paths:
        return str(paths[0])
    if obj is not None and hasattr(obj, "GetPath"):
        return str(obj.GetPath())
    if obj is not None and hasattr(obj, "GetPrim"):
        return str(obj.GetPrim().GetPath())
    return fallback


def _execute_command(name: str, **kwargs: Any) -> Any:
    commands = _kit_module("omni.kit.commands")
    execute = getattr(commands, "execute", None)
    if not callable(execute):
        raise LookupError(f"Neither the Isaac Sim 6.1 sensor API nor omni.kit.commands '{name}' is available.")
    outcome = execute(name, **kwargs)
    success, value = outcome if isinstance(outcome, tuple) and len(outcome) == 2 else (bool(outcome), outcome)
    if not success or value is None:
        raise LookupError(
            f"omni.kit.commands '{name}' did not create a prim (the deprecated extension that registers it may "
            "be disabled; enable it in Window > Extensions or use the Isaac Sim 6.1 API)."
        )
    return value


def _create_physics_live(kind: str, plan: dict[str, Any]) -> tuple[str, str]:
    values = plan["values"]
    translations = [list(plan["translation"])]
    rotated = any(abs(v) > 1e-9 for v in plan["rotation_xyz"])
    module = _kit_module("isaacsim.sensors.experimental.physics")
    cls = getattr(module, "IMU" if kind == "imu" else "Contact", None)
    if cls is not None and callable(getattr(cls, "create", None)):
        kwargs: dict[str, Any] = {"translations": translations}
        if rotated:
            kwargs["orientations"] = [list(plan["quaternion"])]
        if kind == "imu":
            kwargs.update(values)
            api = "isaacsim.sensors.experimental.physics.IMU.create"
        else:
            kwargs.update(min_threshold=values["min_threshold"], max_threshold=values["max_threshold"],
                          radius=values["radius"])
            api = "isaacsim.sensors.experimental.physics.Contact.create"
        return _first_path(cls.create(plan["prim_path"], **kwargs), plan["prim_path"]), api
    w, x, y, z = plan["quaternion"]
    name = "/" + plan["prim_path"].rsplit("/", 1)[1]
    if kind == "imu":
        prim = _execute_command(
            "IsaacSensorCreateImuSensor", path=name, parent=plan["parent_path"],
            translation=Gf.Vec3d(*plan["translation"]), orientation=Gf.Quatd(w, Gf.Vec3d(x, y, z)), **values,
        )
        return _first_path(prim, plan["prim_path"]), "omni.kit.commands IsaacSensorCreateImuSensor (deprecated)"
    prim = _execute_command(
        "IsaacSensorCreateContactSensor", path=name, parent=plan["parent_path"],
        min_threshold=values["min_threshold"], max_threshold=values["max_threshold"], radius=values["radius"],
        color=Gf.Vec4f(*values["color"]), translation=Gf.Vec3d(*plan["translation"]),
    )
    return _first_path(prim, plan["prim_path"]), "omni.kit.commands IsaacSensorCreateContactSensor (deprecated)"


def _create_lidar_live(plan: dict[str, Any]) -> tuple[str, str]:
    values = plan["values"]
    rotated = any(abs(v) > 1e-9 for v in plan["rotation_xyz"])
    module = _kit_module("isaacsim.sensors.experimental.rtx")
    lidar_cls = getattr(module, "Lidar", None)
    if lidar_cls is not None and callable(getattr(lidar_cls, "create", None)):
        kwargs: dict[str, Any] = {"path": plan["prim_path"], "translations": [list(plan["translation"])]}
        if values["usd_path"]:
            kwargs["usd_path"] = values["usd_path"]
        else:
            kwargs["config"] = values["config"]
        if values["variant"] is not None:
            kwargs["variant"] = values["variant"]
        if rotated:
            kwargs["orientations"] = [list(plan["quaternion"])]
        return _first_path(lidar_cls.create(**kwargs), plan["prim_path"]), "isaacsim.sensors.experimental.rtx.Lidar.create"
    w, x, y, z = plan["quaternion"]
    kwargs = {
        "path": "/" + plan["prim_path"].rsplit("/", 1)[1],
        "parent": plan["parent_path"],
        "translation": Gf.Vec3d(*plan["translation"]),
        "orientation": Gf.Quatd(w, Gf.Vec3d(x, y, z)),
    }
    if values["usd_path"]:
        kwargs["usd_path"] = values["usd_path"]
    else:
        kwargs["config"] = values["config"]
    if values["variant"] is not None:
        kwargs["variant"] = values["variant"]
    prim = _execute_command("IsaacSensorCreateRtxLidar", **kwargs)
    return _first_path(prim, plan["prim_path"]), "omni.kit.commands IsaacSensorCreateRtxLidar (deprecated)"


def _runtime(key: tuple[Any, ...], factory: Any) -> tuple[Any, bool]:
    """Cached live reader for key; makes one with factory() the first time. Returns (runtime, created_now)."""
    if key in _LIVE_RUNTIMES:
        return _LIVE_RUNTIMES[key], False
    while len(_LIVE_RUNTIMES) >= MAX_LIVE_RUNTIMES:
        _drop_runtime(next(iter(_LIVE_RUNTIMES)))
    runtime = factory()
    _LIVE_RUNTIMES[key] = runtime
    return runtime, True


def _drop_runtime(key: tuple[Any, ...]) -> None:
    runtime = _LIVE_RUNTIMES.pop(key, None)
    detach = getattr(runtime, "detach_annotators", None)
    annotators = getattr(runtime, "annotators", None)
    if callable(detach) and annotators:
        try:
            detach(list(annotators))
        except Exception:  # noqa: BLE001 - the runtime is dropped either way
            pass


def _read_camera(path: str, width: int, height: int) -> dict[str, Any]:
    module = _kit_module("isaacsim.sensors.experimental.rtx")
    camera_cls = getattr(module, "RtxCamera", None)
    sensor_cls = getattr(module, "CameraSensor", None)
    if camera_cls is None or sensor_cls is None:
        raise LookupError("isaacsim.sensors.experimental.rtx (RtxCamera, CameraSensor) is not available in this Kit.")
    key = ("camera", path, width, height)
    runtime, created = _runtime(
        key, lambda: sensor_cls(camera_cls(path), resolution=(height, width), annotators=["rgb", "distance_to_image_plane"])
    )
    rgb, _info = runtime.get_data("rgb")
    depth, _depth_info = runtime.get_data("distance_to_image_plane")
    result: dict[str, Any] = {
        "resolution": [width, height],
        "rgb": _summarize_array(rgb),
        "depth": _summarize_array(depth),
        "ready": rgb is not None,
        "api": "isaacsim.sensors.experimental.rtx CameraSensor(RtxCamera(path)) annotators rgb + distance_to_image_plane",
    }
    if created:
        result["created_reader"] = (
            "Made a render product for this camera (RtxCamera applies OmniSensorAPI to the camera prim if missing). "
            "release_sensor_live(prim_path) frees it."
        )
    if rgb is None:
        result["note_ready"] = "No image yet: the annotator is warming up. Let a few frames render (Play) and read again."
    hints = _camera_hints(result["rgb"], result["depth"], width * height)
    if hints:
        result["hints"] = hints
    return result


def _camera_hints(rgb: Any, depth: Any, pixels: int) -> list[str]:
    """Plain warnings for a picture that is almost black or a depth image that sees almost nothing."""
    hints: list[str] = []
    mean = rgb.get("mean") if isinstance(rgb, dict) else None
    if isinstance(mean, (int, float)) and mean < 5:
        # Seen live in Isaac 6.1: a scene with no lights renders a near-black image (mean about 1 of 255).
        hints.append(f"The picture is almost black (average brightness {mean:.1f} of 255). The scene probably has "
                     "no light: add a dome or distant light (scene_create_light), or check the camera is not inside "
                     "a mesh.")
    empty = depth.get("non_finite") if isinstance(depth, dict) else None
    if isinstance(empty, int) and pixels and empty / pixels > 0.9:
        hints.append(f"{100 * empty / pixels:.0f}% of the depth pixels hit nothing (inf: empty sky or beyond the "
                     "far clipping distance); only the rest of the image sees an object.")
    return hints


def _read_lidar(path: str) -> dict[str, Any]:
    module = _kit_module("isaacsim.sensors.experimental.rtx")
    sensor_cls = getattr(module, "LidarSensor", None)
    parse = getattr(module, "parse_generic_model_output_data", None)
    if sensor_cls is None or not callable(parse):
        raise LookupError("isaacsim.sensors.experimental.rtx (LidarSensor) is not available in this Kit.")
    runtime, created = _runtime(("lidar", path), lambda: sensor_cls(path, annotators=["generic-model-output"]))
    data, _info = runtime.get_data("generic-model-output")
    size = getattr(data, "size", None)
    result: dict[str, Any] = {"api": "isaacsim.sensors.experimental.rtx LidarSensor generic-model-output"}
    if data is None or size == 0:
        result.update(ready=False, point_count=None,
                      note_ready="No scan yet: the lidar is warming up. Let a few frames render (Play) and read again.")
    else:
        scan = parse(data)
        count = getattr(scan, "numElements", None)
        result.update(ready=True, point_count=int(count) if count is not None else None)
    if created:
        result["created_reader"] = "Made a render product for this lidar; release_sensor_live(prim_path) frees it."
    return result


def _read_physics(kind: str, path: str) -> dict[str, Any]:
    module = _kit_module("isaacsim.sensors.experimental.physics")
    cls = getattr(module, "IMUSensor" if kind == "imu" else "ContactSensor", None)
    if cls is None:
        raise LookupError("isaacsim.sensors.experimental.physics is not available in this Kit.")
    runtime, created = _runtime((kind, path), lambda: cls(path))
    frame = runtime.get_data() or {}
    result: dict[str, Any] = {"api": f"isaacsim.sensors.experimental.physics {type(runtime).__name__}.get_data()",
                              "time": _num(frame.get("time")), "physics_step": _num(frame.get("physics_step"))}
    if kind == "imu":
        result.update(
            linear_acceleration=_small_vector(frame.get("linear_acceleration"), 3),
            angular_velocity=_small_vector(frame.get("angular_velocity"), 3),
            orientation=_small_vector(frame.get("orientation"), 4),
            units="linear_acceleration: stage units/s^2 (specific force: about +g upward at rest); angular_velocity: rad/s; orientation: w, x, y, z",
        )
    else:
        contacts = frame.get("number_of_contacts")
        result.update(
            in_contact=bool(frame.get("in_contact")) if frame.get("in_contact") is not None else None,
            force=_num(frame.get("force")),
            number_of_contacts=int(contacts) if isinstance(contacts, (int, float)) and not isinstance(contacts, bool) else None,
            units="force: newtons on a metre/kilogram stage",
        )
    if created:
        result["created_reader"] = "Made a sensor reader; values refresh every physics step while playing."
    return result


def _summarize_array(data: Any) -> dict[str, Any] | None:
    """shape, dtype, min, max, mean (finite values only) of an annotator array; never the values themselves."""
    if data is None:
        return None
    array = data
    to_numpy = getattr(data, "numpy", None)
    if callable(to_numpy):
        try:
            array = to_numpy()
        except Exception:  # noqa: BLE001
            array = data
    shape = [int(value) for value in getattr(array, "shape", ())]
    summary: dict[str, Any] = {"shape": shape, "dtype": str(getattr(array, "dtype", type(array).__name__))}
    numpy = _kit_module("numpy")
    try:
        if numpy is not None and isinstance(array, numpy.ndarray):
            if array.size == 0:
                summary["empty"] = True
                return summary
            values = array
            if array.dtype.kind == "f":
                finite = numpy.isfinite(array)
                summary["non_finite"] = int(array.size - int(finite.sum()))
                values = array[finite]
            if values.size:
                summary.update(min=_num(values.min()), max=_num(values.max()), mean=_num(values.mean()))
        else:
            for stat in ("min", "max", "mean"):
                fn = getattr(array, stat, None)
                if callable(fn):
                    summary[stat] = _num(fn())
    except Exception as exc:  # noqa: BLE001
        summary["stats_error"] = f"{type(exc).__name__}: {exc}"
    if summary.get("max") == 0.0 and summary.get("min") == 0.0:
        summary["all_zero"] = True
    return summary


def _small_vector(value: Any, count: int) -> list[float] | None:
    if value is None:
        return None
    try:
        items = list(value)[:count]
    except TypeError:
        return None
    return [_num(item) for item in items]


# ------------------------------------------------------------------------------------------------ helpers


def _resolution(value: Any) -> tuple[int, int]:
    items = authoring._items(value, 2, "resolution")
    if any(isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 16384 for item in items):
        raise _Refusal(f"resolution must be (width, height) whole pixels from 1 to 16384, got {value!r}")
    return int(items[0]), int(items[1])


def _sensor_kind_of(prim: Any) -> str | None:
    type_name = str(prim.GetTypeName())
    if type_name in SENSOR_PRIM_TYPES:
        return SENSOR_PRIM_TYPES[type_name]
    if type_name == "Camera":
        return "camera"
    applied = prim.GetAppliedSchemas() or []
    if "IsaacRtxLidarSensorAPI" in applied:
        return "lidar"
    return None


def _describe_sensor(prim: Any, kind: str, xform_cache: Any) -> dict[str, Any]:
    path = str(prim.GetPath())
    rigid = _rigid_ancestor(prim.GetParent())
    item: dict[str, Any] = {
        "path": path,
        "kind": kind,
        "type": str(prim.GetTypeName()),
        "parent": str(prim.GetParent().GetPath()),
        "attached_to_link": str(rigid.GetPath()) if rigid is not None else None,
    }
    if kind == "camera":
        item["viewport_camera"] = path in VIEWPORT_CAMERAS
        for attr_name in ("focalLength", "horizontalAperture", "verticalAperture", "clippingRange"):
            value = prim.GetAttribute(attr_name).Get() if prim.GetAttribute(attr_name) else None
            if value is not None:
                item.setdefault("lens", {})[attr_name] = _json(value)
        applied = prim.GetAppliedSchemas() or []
        if "OmniSensorAPI" in applied:
            item["omni_sensor_api"] = True
    enabled = prim.GetAttribute("enabled")
    if enabled and enabled.IsValid() and enabled.Get() is not None:
        item["enabled"] = bool(enabled.Get())
    for attr_name in ("threshold", "radius", "linearAccelerationFilterWidth"):
        attr = prim.GetAttribute(attr_name)
        if attr and attr.HasAuthoredValue():
            item.setdefault("settings", {})[attr_name] = _json(attr.Get())
    try:
        if prim.IsA(UsdGeom.Xformable):
            item["world_translation"] = [_clean(v) for v in xform_cache.GetLocalToWorldTransform(prim).ExtractTranslation()]
    except Exception:  # noqa: BLE001
        pass
    if prim.IsInstanceProxy():
        item["inside_instance"] = True
    if kind != "camera" and item["attached_to_link"] is None and kind in ("imu", "contact"):
        item["problem"] = "No enabled rigid-body ancestor: this sensor cannot read anything."
    return item


def _public(plan: dict[str, Any]) -> dict[str, Any]:
    kind = plan["kind"]
    if kind in CAMERA_KINDS:
        prim_type, schema = "Camera", "UsdGeom.Camera (read with isaacsim.sensors.experimental.rtx CameraSensor)"
        transform = {
            "xform_op_order": ["xformOp:translate", "xformOp:rotateXYZ"],
            "translate": _vecj(plan["translation"]),
            "rotate_xyz": _vecj(plan["rotation_xyz"]),
        }
        authoring_fn = "add_camera_sensor"
    elif kind == "lidar":
        prim_type, schema = "OmniLidar", "omni.usd.schema.omni_sensors OmniLidar (from a lidar asset via Lidar.create)"
        transform = {"translate": _vecj(plan["translation"]), "orient_wxyz": _vecj(plan["quaternion"]),
                     "rotate_xyz": _vecj(plan["rotation_xyz"])}
        authoring_fn = None
    else:
        prim_type = PHYSICS_SCHEMAS[kind]["type"]
        schema = f"{prim_type} (isaacsim.robot.schema sensor schema, inherits Xformable)"
        transform = {
            "xform_op_order": ["xformOp:translate", "xformOp:orient"],
            "translate": _vecj(plan["translation"]),
            "orient_wxyz": _vecj(plan["quaternion"]),
            "rotate_xyz": _vecj(plan["rotation_xyz"]),
        }
        authoring_fn = "add_physics_sensor"
    transform["facing"] = plan["facing"]
    if plan["aim"] is not None:
        transform["aim"] = plan["aim"]
    public = {
        "kind": kind,
        "prim_path": plan["prim_path"],
        "parent_path": plan["parent_path"],
        "prim_type": prim_type,
        "schema": schema,
        "attributes": plan["attributes"],
        "attributes_explained": plan["explained"],
        "transform": transform,
        "rigid_body_link": plan["rigid_body"],
        "stage_checked": plan["stage_checked"],
        "python": plan["python"],
        "how_to_view_data": plan["how_to_view_data"],
        "pure_usd_authoring": authoring_fn,
        "verified_with": VERIFIED["isaac_sim"],
    }
    if "world_position" in plan:
        public["world_position"] = plan["world_position"]
    return public


def _plan_summary(public: dict[str, Any]) -> dict[str, Any]:
    keep = ("kind", "prim_path", "parent_path", "prim_type", "schema", "attributes", "attributes_explained",
            "transform", "rigid_body_link", "world_position", "also_modifies")
    return {key: public[key] for key in keep if key in public}


def _unique(warnings: list[str]) -> list[str]:
    seen: list[str] = []
    for warning in warnings:
        text = authoring._text(warning)
        if text not in seen:
            seen.append(text)
    return seen[: authoring.MAX_WARNINGS]


def _json(value: Any) -> Any:
    return authoring._json(value)


def _clean(value: float) -> float:
    rounded = round(float(value), 9)
    return 0.0 if rounded == 0.0 else rounded


def _vecj(values: Any) -> list[float]:
    return [_clean(v) for v in values]


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return _clean(number) if math.isfinite(number) else None


def _fmt(value: Any) -> str:
    return repr(_clean(value))
