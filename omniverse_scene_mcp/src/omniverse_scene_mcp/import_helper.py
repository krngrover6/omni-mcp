"""Import helper: bring FBX / OBJ / glTF / STL / CAD files into a project as USD, with units and up axis fixed.

The beginner problem this solves: a converted file keeps the SOURCE's units and up axis (an FBX chair is often
centimetres and Y-up), while a project such as the factory digital twin is Z-up millimetres. Referencing the raw file
makes the chair 10x too big or lying on its back. The fix used here is a small WRAPPER layer per asset:

    <project>/assets/<name>/<name>.usd            the converter's output (never edited afterwards)
    <project>/assets/<name>/<name>_wrapped.usda   defaultPrim /<Name> (kind=component), the PROJECT's metersPerUnit
                                                   and upAxis, a relative reference to ./<name>.usd and two xformOps:
                                                   xformOp:rotateX:unitsResolve (+90 for Y-up -> Z-up, -90 for
                                                   Z-up -> Y-up) and xformOp:scale:unitsResolve = source mpu / target
                                                   mpu (e.g. source centimetres 0.01, project metres 1 -> scale 0.01)

The layout layer then references the WRAPPER at /World/<Name>; its units already match the project.
The op names are the ones Kit's Metrics Assembler (omni.usd.metrics.assembler) uses for the same correction, and
the converted root prim's own xformOps are kept after them (a local xformOpOrder would otherwise hide them).

API (all return JSON-safe dicts with ok):
  supported_formats()                     converters verified in this workspace's Isaac Sim 6.1.0 / Composer build
  plan_import(source_path, project_dir=)  dry run: converter, output and wrapper paths, steps, warnings
  import_asset(..., dry_run=True)         Kit-only: starts the converter task (async), then wrap_units and an
                                          asset_health_file post-check; poll import_status(job_id)
  wrap_units(converted, wrapper, ...)     pure pxr (no Kit): authors/previews the wrapper layer and verifies the
                                          composed bounding box
  detect_units(usd_path)                  read-only: metersPerUnit / upAxis metadata + a bounding-box size
                                          heuristic ("a chair 80 units tall is probably centimetres")

Safety: plan_import, supported_formats and detect_units never write. wrap_units and import_asset write only when
dry_run is exactly False, never overwrite an existing file unless overwrite=True, and only inside the project's
assets/<name>/ folder (import_asset) or at the wrapper path given (wrap_units). Source files are never modified,
nothing is placed into a scene layer (how_to_place explains that step), and nothing touches the network.
Converter facts below were read (read-only) from the installed extension sources on 2026-10-22.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import math
import os
import re
import time
import uuid
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
    "ASSET_CONVERTER_OPTIONS",
    "ImportHelperError",
    "detect_units",
    "import_asset",
    "import_status",
    "plan_import",
    "supported_formats",
    "wrap_units",
]

VERIFIED_ON = "2026-10-22"
ROOT_LAYER = "root.usda"
LAYOUT_LAYER = "layers/layout.usda"
MAX_NAME_LENGTH = 64
LARGE_FILE_BYTES = 200 * 1024 * 1024
HUGE_FILE_BYTES = 2 * 1024 * 1024 * 1024
SCALE_OP = "xformOp:scale:unitsResolve"
ROTATE_OP = "xformOp:rotateX:unitsResolve"
USD_EXTENSIONS = frozenset({"usd", "usda", "usdc", "usdz"})
_NOT_KIT = "Kit not available"
_MAX_JOBS = 32
_JOBS: dict[str, dict[str, Any]] = {}

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})
_CREO_VERSIONED = re.compile(r"\.(asm|prt)\.\d+$", re.IGNORECASE)
_URL_UNSAFE = set("#%?&;")

UNITS: dict[str, float] = {"m": 1.0, "dm": 0.1, "cm": 0.01, "mm": 0.001, "in": 0.0254, "ft": 0.3048}
_UNIT_WORDS = {"m": "metres", "dm": "decimetres", "cm": "centimetres", "mm": "millimetres", "in": "inches",
               "ft": "feet"}
_UNIT_ALIASES = {
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "dm": "dm", "decimeter": "dm", "decimeters": "dm", "decimetre": "dm", "decimetres": "dm",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "mm": "mm", "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "in": "in", "inch": "in", "inches": "in", "ft": "ft", "foot": "ft", "feet": "ft",
}

# ------------------------------------------------------------------------------------- verified converter data

_ISAAC = "isaacsim-6.1.0/extscache"
_COMPOSER = "kit-app-template/_build/windows-x86_64/release/extscache"

# Every option of omni.kit.asset_converter.AssetConverterContext (impl/context.py, identical in 6.0.5 and 6.0.1).
ASSET_CONVERTER_OPTIONS: dict[str, bool] = {
    "ignore_materials": False, "ignore_animations": False, "ignore_camera": False, "ignore_light": False,
    "single_mesh": False, "smooth_normals": True, "export_preview_surface": False, "support_point_instancer": False,
    "embed_mdl_in_usd": True, "use_meter_as_world_unit": False, "create_world_as_default_root_prim": True,
    "embed_textures": True, "convert_fbx_to_y_up": False, "convert_fbx_to_z_up": False, "keep_all_materials": False,
    "merge_all_meshes": False, "use_double_precision_to_usd_transform_op": False, "ignore_pivots": False,
    "disabling_instancing": False, "export_hidden_props": False, "baking_scales": False,
    "ignore_flip_rotations": False, "ignore_unbound_bones": False, "bake_mdl_material": False,
    "export_separate_gltf": False, "export_mdl_gltf_extension": False, "convert_stage_up_y": False,
    "convert_stage_up_z": False,
}

_ASSET_CONVERTER_FORMATS = ("fbx", "obj", "gltf", "glb", "stl", "ply", "bvh", "lxo", "md5")
_HOOPS_FORMATS = (
    "catpart", "catproduct", "cgr", "3dxml", "ifc", "ifczip", "prt", "asm", "xmt", "x_t", "x_b", "xmt_txt",
    "sldprt", "sldasm", "stl", "ipt", "iam", "dwg", "dxf", "rvt", "rfa", "par", "pwd", "psm", "step", "stp",
    "iges", "igs", "3dm", "dae", "fbx", "obj", "3ds", "3mf", "gltf", "glb", "sat", "sab", "jt", "dgn",
)

CONVERTERS: dict[str, dict[str, Any]] = {
    "omni.kit.asset_converter": {
        "title": "Asset Converter (Assimp/FBX SDK based mesh importer)",
        "versions": {"isaacsim-6.1.0": "6.0.5", "composer_build": "6.0.1"},
        "module": "omni.kit.asset_converter",
        "api": ("get_instance().create_converter_task(import_path, output_path, progress_callback, "
                "asset_converter_context, material_loader=None, close_stage_and_reopen_if_opened=False) -> task; "
                "success = await task.wait_until_finished(); task.get_status(), task.get_error_message()"),
        "options": "AssetConverterContext attributes: " + ", ".join(ASSET_CONVERTER_OPTIONS),
        "formats": list(_ASSET_CONVERTER_FORMATS),
        "enabled_in": ["isaacsim.exp.full.kit / isaacsim.exp.base.kit", "my_company.my_usd_composer.kit"],
        "sources": [
            f"{_ISAAC}/omni.kit.asset_converter-6.0.5+110.1.1.wx64.r.cp312.u7f4/omni/kit/asset_converter/impl/extension.py",
            f"{_ISAAC}/omni.kit.asset_converter-6.0.5+110.1.1.wx64.r.cp312.u7f4/omni/kit/asset_converter/impl/context.py",
            f"{_ISAAC}/omni.kit.asset_converter-6.0.5+110.1.1.wx64.r.cp312.u7f4/omni/kit/asset_converter/impl/task_manager.py",
            f"{_ISAAC}/omni.kit.tool.asset_importer-6.0.2+110.1.1/omni/kit/tool/asset_importer/importer_delegate.py",
            f"{_COMPOSER}/omni.kit.asset_converter-6.0.1+110.1.1.wx64.r.cp312.u7f4/omni/kit/asset_converter/impl/context.py",
        ],
        "notes": [
            "Format list = the 6.0.5 docstring (fbx, obj, glTF/glb, stl, ply, bvh, lxo, md5, USD) and the File > Import "
            "filter list of omni.kit.tool.asset_importer (6.0.2 Isaac / 6.0.1 Composer).",
            "The 6.0.1 docstring (Composer build) only promises fbx/obj/glTF as fully verified.",
            "create_converter_task fails when the output file is the stage currently open, unless "
            "close_stage_and_reopen_if_opened=True.",
            "AssetConverterContext.export_separate_gltf is passed to the native converter as export_embedded_gltf.",
        ],
    },
    "omni.kit.converter.hoops_core": {
        "title": "CAD Converter - HOOPS Exchange (part of omni.kit.converter.cad)",
        "versions": {"isaacsim-6.1.0": "512.0.0 (bundle omni.kit.converter.cad 209.5.2)",
                     "composer_build": "511.3.2 (bundle omni.kit.converter.cad 209.4.0)"},
        "module": "omni.kit.converter.hoops_core",
        "api": ("get_instance().create_converter_task(input_path, output_path, file_format_args: dict[str, str]) "
                "-> coroutine returning (output_url, ConverterStatus(error_code, error_msg)); '' url = failed"),
        "options": ("file_format_args keys of HoopsOptions, e.g. instancing, convertHidden, convertMetadata, tessLOD, "
                    "useMaterials, upAxis (0 file default, 1 Y, 2 Z). Unit/up overrides are named "
                    "sceneOpMetersPerUnit/sceneOpUpAxis in 512.0.0 but dMetersPerUnit/iUpAxis in 511.3.2, so this "
                    "helper leaves them unset and fixes units in the wrapper instead."),
        "formats": list(_HOOPS_FORMATS),
        "enabled_in": ["isaacsim.exp.full.kit (omni.kit.converter.cad ~209.5)", "my_company.my_usd_composer.kit"],
        "sources": [
            f"{_ISAAC}/omni.kit.converter.hoops_core-512.0.0+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/hoops_core/impl/filters.py",
            f"{_ISAAC}/omni.kit.converter.hoops_core-512.0.0+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/hoops_core/impl/extension.py",
            f"{_ISAAC}/omni.kit.converter.hoops_core-512.0.0+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/hoops_core/impl/options.py",
            f"{_ISAAC}/omni.kit.converter.cad-209.5.2+110.1.2.wx64.r.cp312.u7f4/docs/Overview.md",
            f"{_COMPOSER}/omni.kit.converter.hoops_core-511.3.2+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/hoops_core/impl/filters.py",
        ],
        "notes": [
            "NVIDIA recommends at least 16 GB RAM (complex files up to 32 GB) for CAD conversion.",
            "Creo files may carry a version suffix (part.prt.3); .prt is both Siemens NX and Creo.",
            "Overview.md also lists ply/lxo/md5/e57/pts as Asset Converter formats; e57/pts were not found in the "
            "installed Asset Converter filters, so they are not offered here.",
        ],
    },
    "omni.kit.converter.jt_core": {
        "title": "CAD Converter - JT (priority 1 over HOOPS for .jt)",
        "versions": {"isaacsim-6.1.0": "511.0.0", "composer_build": "510.1.1"},
        "module": "omni.kit.converter.jt_core",
        "api": "get_instance().create_converter_task(input_path, output_path, file_format_args) -> coroutine",
        "options": "file_format_args (JT options); left empty by this helper.",
        "formats": ["jt"],
        "enabled_in": ["via omni.kit.converter.cad"],
        "sources": [f"{_ISAAC}/omni.kit.converter.jt_core-511.0.0+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/jt_core/impl/filters.py",
                    f"{_ISAAC}/omni.kit.converter.jt_core-511.0.0+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/jt_core/impl/extension.py"],
        "notes": [],
    },
    "omni.kit.converter.dgn_core": {
        "title": "CAD Converter - DGN (ODA SDK; priority 1 over HOOPS for .dgn)",
        "versions": {"isaacsim-6.1.0": "513.0.0", "composer_build": "512.3.3"},
        "module": "omni.kit.converter.dgn_core",
        "api": "get_instance().create_converter_task(input_path, output_path, file_format_args) -> coroutine",
        "options": "file_format_args (DGN options); left empty by this helper.",
        "formats": ["dgn"],
        "enabled_in": ["via omni.kit.converter.cad"],
        "sources": [f"{_ISAAC}/omni.kit.converter.dgn_core-513.0.0+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/dgn_core/impl/filters.py",
                    f"{_ISAAC}/omni.kit.converter.dgn_core-513.0.0+110.1.2.wx64.r.cp312.u7f4/omni/kit/converter/dgn_core/impl/extension.py"],
        "notes": [],
    },
}

# Robots are imported by the robot importers, not by this helper (noted so plan_import can point the way).
ROBOT_IMPORTERS: dict[str, dict[str, Any]] = {
    "urdf": {"extension": "isaacsim.asset.importer.urdf", "version": "3.11.10 (bundles urdf_usd_converter 0.3.2)",
             "source": "isaacsim-6.1.0/exts/isaacsim.asset.importer.urdf/config/extension.toml"},
    "mjcf": {"extension": "isaacsim.asset.importer.mjcf", "version": "3.12.1 (Newton bundles mujoco_usd_converter 0.5.0)",
             "source": "isaacsim-6.1.0/exts/isaacsim.asset.importer.mjcf/config/extension.toml"},
}

_CAD_MODULE_FOR = {"jt": "omni.kit.converter.jt_core", "dgn": "omni.kit.converter.dgn_core"}

# What a source file of each kind USUALLY carries. detect_units on the converted file is the real answer.
_EXPECTED: dict[str, dict[str, Any]] = {
    "gltf": {"mpu": 1.0, "up": "Y", "sure": True, "why": "the glTF specification fixes metres and Y-up"},
    "glb": {"mpu": 1.0, "up": "Y", "sure": True, "why": "the glTF specification fixes metres and Y-up"},
    "fbx": {"mpu": 0.01, "up": "Y", "sure": False,
            "why": "FBX stores its own unit and up axis; exports from Maya/3ds Max/Blender are often centimetres, Y-up"},
    "obj": {"mpu": None, "up": "Y", "sure": False,
            "why": "OBJ has no unit field; Y-up is the usual convention, the unit is whatever the author used"},
    "stl": {"mpu": 0.001, "up": "Z", "sure": False,
            "why": "STL has no unit field; CAD and 3D-printing exports are usually millimetres, Z-up"},
    "ply": {"mpu": None, "up": None, "sure": False, "why": "PLY has no unit or up-axis field"},
}
_CAD_EXPECTED = {"mpu": 0.001, "up": "Z", "sure": False,
                 "why": "CAD models are usually millimetres; most CAD tools are Z-up (SolidWorks is Y-up)"}

# Typical largest dimension (metres) of everyday and factory objects, for the bounding-box unit heuristic.
TYPICAL_SIZES_M: dict[str, float] = {
    "chair": 0.9, "stool": 0.6, "table": 1.4, "desk": 1.4, "workbench": 1.8, "sofa": 2.0, "bed": 2.0,
    "cabinet": 1.8, "shelf": 2.0, "shelves": 2.0, "rack": 2.5, "locker": 1.9, "door": 2.1, "window": 1.2,
    "lamp": 1.5, "light": 0.5, "monitor": 0.6, "laptop": 0.35, "keyboard": 0.45, "phone": 0.15, "button": 0.04,
    "bottle": 0.25, "cup": 0.1, "mug": 0.1, "can": 0.12, "box": 0.5, "carton": 0.5, "crate": 0.8, "tote": 0.6,
    "bin": 0.6, "bag": 0.5, "pallet": 1.2, "barrel": 0.9, "drum": 0.9, "cone": 0.7, "bolt": 0.03, "screw": 0.03,
    "nut": 0.02, "person": 1.75, "human": 1.75, "worker": 1.75, "character": 1.75, "car": 4.5,
    "truck": 8.0, "forklift": 3.0, "robot": 1.2, "arm": 1.0, "gripper": 0.25, "scara": 0.8, "conveyor": 3.0,
    "machine": 2.5, "cnc": 3.0, "feeder": 1.5, "fence": 2.0, "tree": 8.0, "house": 15.0, "building": 30.0,
    "warehouse": 100.0, "factory": 150.0,
}
_HEURISTIC_UNITS = ("m", "cm", "mm")


class ImportHelperError(ValueError):
    """Raised internally for invalid input; public functions turn it into {ok: False, error}."""


# ------------------------------------------------------------------------------------------------ small helpers


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


def _not_kit(detail: str) -> dict[str, Any]:
    return {"ok": False, "message": _NOT_KIT, "error": f"{_NOT_KIT}: {detail}", "detail": detail}


def _need_pxr() -> None:
    if _PXR_IMPORT_ERROR is not None:
        raise ImportHelperError(f"USD Python bindings (pxr) are required: {_PXR_IMPORT_ERROR}")


def _posix(path: str | os.PathLike[str]) -> str:
    return os.fspath(path).replace("\\", "/")


def _is_unc(text: str) -> bool:
    return text.startswith(("\\\\", "//"))


def _is_within(path: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    child, root = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(parent))
    try:
        return os.path.commonpath([child, root]) == root
    except ValueError:
        return False


def _unit_key(mpu: float) -> str | None:
    for key, value in UNITS.items():
        if math.isclose(mpu, value, rel_tol=1e-6):
            return key
    return None


def _unit_text(mpu: float) -> str:
    key = _unit_key(mpu)
    return f"{_UNIT_WORDS[key]} ({mpu:g})" if key else f"{mpu:g} metres per unit"


def _parse_mpu(value: Any, label: str) -> float:
    """metersPerUnit from 'm'/'cm'/'mm'/'in'/'ft' (and spelled-out forms) or a positive number."""
    if isinstance(value, bool) or value is None:
        raise ImportHelperError(f"{label} must be a unit such as 'm', 'cm' or 'mm', or a metersPerUnit number.")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().lower()
        if text in _UNIT_ALIASES:
            return UNITS[_UNIT_ALIASES[text]]
        try:
            number = float(text)
        except ValueError as exc:
            raise ImportHelperError(f"Unknown {label} {value!r}; use 'm', 'cm', 'mm', 'in', 'ft' or a number "
                                    "like 0.01.") from exc
    if not math.isfinite(number) or number <= 0:
        raise ImportHelperError(f"{label} must be a positive metersPerUnit value, not {value!r}.")
    return number


def _parse_up(value: Any, label: str) -> str:
    text = str(value or "").strip().upper()
    if text not in ("Y", "Z"):
        raise ImportHelperError(f"{label} must be 'Y' or 'Z' (the only up axes USD supports), not {value!r}.")
    return text


def _source_extension(path: str) -> str:
    match = _CREO_VERSIONED.search(path)
    if match:
        return match.group(1).lower()
    return os.path.splitext(path)[1].lstrip(".").lower()


def _converter_for(ext: str) -> tuple[str | None, list[str]]:
    """(preferred converter module, alternatives) for a lowercase extension."""
    if ext in _ASSET_CONVERTER_FORMATS:
        alternatives = ["omni.kit.converter.hoops_core"] if ext in _HOOPS_FORMATS else []
        return "omni.kit.asset_converter", alternatives
    if ext in _CAD_MODULE_FOR:
        return _CAD_MODULE_FOR[ext], ["omni.kit.converter.hoops_core"]
    if ext in _HOOPS_FORMATS:
        return "omni.kit.converter.hoops_core", []
    return None, []


def _expected_for(ext: str) -> dict[str, Any]:
    if ext in _EXPECTED:
        return dict(_EXPECTED[ext])
    if ext in _HOOPS_FORMATS or ext in _CAD_MODULE_FOR:
        return dict(_CAD_EXPECTED)
    return {"mpu": None, "up": None, "sure": False, "why": "unknown"}


def _default_name(source: str) -> str:
    stem = Path(source).name
    stem = _CREO_VERSIONED.sub("", stem)
    stem = os.path.splitext(stem)[0]
    name = re.sub(r"[^A-Za-z0-9_]+", "_", stem)
    name = re.sub(r"_+", "_", name).strip("_") or "Asset"
    if name[0].isdigit():
        name = f"Asset_{name}"
    return name[:MAX_NAME_LENGTH]


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ImportHelperError("name must be a non-empty string such as 'office_chair'.")
    if len(name) > MAX_NAME_LENGTH:
        raise ImportHelperError(f"name is too long ({len(name)} characters, at most {MAX_NAME_LENGTH}).")
    if not _NAME.match(name):
        raise ImportHelperError(
            f"Invalid asset name {name!r}: use letters, digits and '_' only, not starting with a digit (no spaces, "
            "dots, slashes or '..'); it becomes both the folder name and the USD prim name.")
    if name.upper() in _RESERVED:
        raise ImportHelperError(f"{name!r} is a reserved device name on Windows; pick another name.")
    return name


def _path_warnings(label: str, path: str) -> list[str]:
    warnings = []
    if " " in path:
        warnings.append(f"The {label} path contains spaces ({path}). Kit handles them, but some converters, "
                        "command lines and USD asset paths are fragile with spaces; prefer underscores.")
    if any(ord(ch) > 127 for ch in path):
        warnings.append(f"The {label} path contains non-ASCII characters ({path}); older converters and some USD "
                        "asset resolvers mis-handle them. Prefer plain ASCII folder and file names.")
    unsafe = sorted({ch for ch in path if ch in _URL_UNSAFE})
    if unsafe:
        warnings.append(f"The {label} path contains {' '.join(unsafe)}, which omni.client treats as URL syntax; "
                        "rename the file or folder.")
    return warnings


def _relative_asset_path(target: str, from_dir: str) -> tuple[str, bool]:
    """(asset path for a reference authored in a layer living in from_dir, is_relative)."""
    try:
        rel = os.path.relpath(target, from_dir)
    except ValueError:  # different drives on Windows
        return _posix(os.path.abspath(target)), False
    rel = _posix(rel)
    return (rel if rel.startswith("../") else f"./{rel}"), True


def _read_layer_metrics(path: str) -> dict[str, Any]:
    """metersPerUnit / upAxis / defaultPrim authored in one layer file (opened anonymously, never saved)."""
    _need_pxr()
    try:
        layer = Sdf.Layer.OpenAsAnonymous(path)
    except Exception as exc:  # noqa: BLE001 - Tf.ErrorException on a broken file
        raise ImportHelperError(f"Could not open the USD layer {path}: {exc}") from exc
    if layer is None:
        raise ImportHelperError(f"Could not open the USD layer {path}")
    root = layer.pseudoRoot
    mpu = root.GetInfo("metersPerUnit") if root.HasInfo("metersPerUnit") else None
    up = root.GetInfo("upAxis") if root.HasInfo("upAxis") else None
    return {"meters_per_unit": None if mpu is None else float(mpu), "up_axis": None if up is None else str(up),
            "default_prim": str(layer.defaultPrim or "") or None,
            "root_prims": [spec.name for spec in layer.rootPrims]}


def _resolve_project(project_dir: Any) -> str:
    if project_dir is None or not str(project_dir).strip():
        raise ImportHelperError("project_dir is required: the project folder that holds root.usda and assets/.")
    text = os.fspath(project_dir)
    if _is_unc(text):
        raise ImportHelperError(f"Network (UNC) project folders are not supported: {text}")
    if not os.path.isabs(text):
        raise ImportHelperError(f"project_dir must be an absolute folder path, not {text!r}.")
    real = os.path.realpath(text)
    if not os.path.isdir(real):
        raise ImportHelperError(f"The project folder does not exist: {text}")
    if Path(real) == Path(Path(real).anchor):
        raise ImportHelperError(f"Refusing to use a drive root ({real}) as a project folder.")
    return real


def _resolve_source(source_path: Any, project: str) -> str:
    if source_path is None or not str(source_path).strip():
        raise ImportHelperError("source_path is required: the FBX/OBJ/glTF/CAD file to import.")
    text = os.fspath(source_path).strip()
    if _is_unc(text):
        raise ImportHelperError(f"Network (UNC) source files are not supported; copy it to a local folder: {text}")
    if re.match(r"^[a-z][a-z0-9+.-]+://", text.lower()):
        raise ImportHelperError(f"Only local files can be imported here, not {text!r}.")
    candidate = text if os.path.isabs(text) else os.path.join(project, text)
    candidate = os.path.abspath(candidate)
    if not os.path.exists(candidate):
        raise ImportHelperError(f"The source file does not exist: {candidate}")
    if not os.path.isfile(candidate):
        raise ImportHelperError(f"The source path is not a file: {candidate}")
    return candidate


def _project_units(project: str, target_units: Any, up_axis: Any) -> tuple[float, str, list[str], str]:
    """(target mpu, target up, warnings, where they came from)."""
    warnings: list[str] = []
    root = os.path.join(project, ROOT_LAYER)
    metrics: dict[str, Any] = {}
    if (target_units is None or up_axis is None) and os.path.isfile(root):
        metrics = _read_layer_metrics(root)
    origin = []
    if target_units is not None:
        mpu = _parse_mpu(target_units, "target_units")
        origin.append("target_units argument")
    elif metrics.get("meters_per_unit") is not None:
        mpu = float(metrics["meters_per_unit"])
        origin.append(f"metersPerUnit of {ROOT_LAYER}")
    elif os.path.isfile(root):
        mpu = 0.01
        warnings.append(f"{ROOT_LAYER} has no metersPerUnit, so USD's fallback 0.01 (centimetres) is assumed; "
                        "author metersPerUnit in root.usda or pass target_units.")
        origin.append("USD fallback metersPerUnit 0.01")
    else:
        raise ImportHelperError(f"{project} has no {ROOT_LAYER}; pass target_units ('m', 'cm' or 'mm') and up_axis "
                                "so the wrapper can match the scene.")
    if up_axis is not None:
        up = _parse_up(up_axis, "up_axis")
        origin.append("up_axis argument")
    elif metrics.get("up_axis") in ("Y", "Z"):
        up = str(metrics["up_axis"])
        origin.append(f"upAxis of {ROOT_LAYER}")
    elif os.path.isfile(root):
        up = "Y"
        warnings.append(f"{ROOT_LAYER} has no upAxis, so USD's fallback Y is assumed; pass up_axis to be explicit.")
        origin.append("USD fallback upAxis Y")
    else:
        raise ImportHelperError(f"{project} has no {ROOT_LAYER}; pass up_axis ('Y' or 'Z').")
    return mpu, up, warnings, ", ".join(origin)


def _correction(source_mpu: float, source_up: str, target_mpu: float, target_up: str) -> dict[str, Any]:
    scale = source_mpu / target_mpu
    if math.isclose(scale, 1.0, rel_tol=1e-9):
        scale = 1.0
    rotate = 0.0
    if source_up == "Y" and target_up == "Z":
        rotate = 90.0
    elif source_up == "Z" and target_up == "Y":
        rotate = -90.0
    scale_text = (f"source is {_unit_text(source_mpu)}, project is {_unit_text(target_mpu)} -> scale {scale:g}"
                  + ("" if scale != 1.0 else " (no scaling needed)"))
    if rotate == 90.0:
        rotate_text = "source is Y-up, project is Z-up -> rotateX +90 (the source's up axis Y becomes Z)"
    elif rotate == -90.0:
        rotate_text = "source is Z-up, project is Y-up -> rotateX -90 (the source's up axis Z becomes Y)"
    else:
        rotate_text = f"source and project are both {target_up}-up -> no rotation"
    return {"scale": scale, "rotate_x": rotate, "scale_explained": scale_text, "rotate_explained": rotate_text,
            "ops": ([ROTATE_OP] if rotate else []) + ([SCALE_OP] if scale != 1.0 else [])}


# ------------------------------------------------------------------------------------------- supported formats


def supported_formats() -> dict[str, Any]:
    """Converters and input formats verified in this workspace's installs (read-only facts, no Kit needed)."""
    formats: dict[str, Any] = {}
    for ext in sorted(set(_ASSET_CONVERTER_FORMATS) | set(_HOOPS_FORMATS)):
        converter, alternatives = _converter_for(ext)
        expected = _expected_for(ext)
        formats[ext] = {
            "converter": converter, "alternatives": alternatives,
            "category": "mesh" if converter == "omni.kit.asset_converter" else "cad",
            "typical_source_units": None if expected["mpu"] is None else _unit_text(expected["mpu"]),
            "typical_source_up_axis": expected["up"], "why": expected["why"],
        }
    return {
        "ok": True,
        "verified_on": VERIFIED_ON,
        "how_verified": "Read the installed extension sources (read-only) under C:/Users/krngr/Omniverse/"
                        + _ISAAC + " and " + _COMPOSER + "; no converter was run.",
        "converters": {name: dict(info) for name, info in CONVERTERS.items()},
        "formats": formats,
        "not_handled_here": {
            "usd": "usd/usda/usdc/usdz need no conversion: use asset_wrap_units(source, wrapper, ...) directly.",
            **{fmt: f"robot description: use {info['extension']} {info['version']} ({info['source']})"
               for fmt, info in ROBOT_IMPORTERS.items()},
        },
        "units_note": ("Converters keep the source's units and up axis. Fix them with the wrapper layer "
                       "(wrap_units) instead of editing the converted file."),
    }


# ------------------------------------------------------------------------------------------------- plan_import


def plan_import(
    source_path: Any,
    *,
    project_dir: Any,
    name: Any = None,
    target_units: Any = None,
    up_axis: Any = None,
    as_reference: bool = True,
    instanceable: bool = False,
) -> dict[str, Any]:
    """Dry run of an import: which converter, where the files go, the wrapper fix, steps and warnings. Never writes."""
    try:
        return _plan(source_path, project_dir, name, target_units, up_axis, as_reference, instanceable)
    except ImportHelperError as exc:
        return _error(str(exc), dry_run=True)


def _plan(source_path: Any, project_dir: Any, name: Any, target_units: Any, up_axis: Any, as_reference: Any,
          instanceable: Any) -> dict[str, Any]:
    project = _resolve_project(project_dir)
    raw_source = "" if source_path is None else os.fspath(source_path)
    ext = _source_extension(raw_source)
    if ext in ("urdf", "mjcf") or (ext == "xml" and "mjcf" in raw_source.lower()):
        info = ROBOT_IMPORTERS["urdf" if ext == "urdf" else "mjcf"]
        raise ImportHelperError(f"{Path(raw_source).name} is a robot description; import it with "
                                f"{info['extension']} ({info['version']}), not this mesh/CAD helper.")
    source = _resolve_source(source_path, project)
    ext = _source_extension(source)
    is_usd = ext in USD_EXTENSIONS
    converter, alternatives = _converter_for(ext)
    if converter is None and not is_usd:
        known = sorted(set(_ASSET_CONVERTER_FORMATS) | set(_HOOPS_FORMATS))
        raise ImportHelperError(f"Unsupported file type '.{ext}' ({Path(source).name}). Supported: "
                                f"{', '.join('.' + item for item in known)}; USD files need no conversion.")
    asset_name = _check_name(name) if name is not None else _default_name(source)
    target_mpu, target_up, warnings, units_from = _project_units(project, target_units, up_axis)

    assets_dir = os.path.join(project, "assets")
    asset_dir = os.path.join(assets_dir, asset_name)
    if not _is_within(os.path.realpath(asset_dir), project):
        raise ImportHelperError(f"assets/{asset_name} resolves outside the project folder (a link?); refusing.")
    output = os.path.join(asset_dir, f"{asset_name}.usd")
    wrapper = os.path.join(asset_dir, f"{asset_name}_wrapped.usda")
    if os.path.normcase(source) in (os.path.normcase(output), os.path.normcase(wrapper)):
        raise ImportHelperError("The source file is the planned output file; pick another name.")

    size = os.path.getsize(source)
    warnings += _path_warnings("source", source) + _path_warnings("output", output)
    if size >= HUGE_FILE_BYTES:
        warnings.append(f"The source is very large ({size / 2**30:.1f} GB): conversion can take a long time and "
                        "needs a lot of RAM; consider splitting it in the authoring tool first.")
    elif size >= LARGE_FILE_BYTES:
        warnings.append(f"The source is large ({size / 2**20:.0f} MB): conversion may take minutes; keep Kit open "
                        "and poll import_status.")
    if converter and converter != "omni.kit.asset_converter":
        warnings.append("CAD conversion: NVIDIA recommends at least 16 GB RAM (complex files up to 32 GB).")
    for path, label in ((output, "converted file"), (wrapper, "wrapper")):
        if os.path.exists(path):
            warnings.append(f"The {label} {path} already exists; import_asset refuses unless overwrite=True.")
    if not os.path.isdir(assets_dir):
        warnings.append(f"{assets_dir} does not exist yet; import_asset creates assets/{asset_name}/ only.")
    if not _is_within(source, project):
        warnings.append("The source is outside the project; that is fine: it is only read, never modified.")

    expected = _expected_for(ext)
    if is_usd:
        expected = {"mpu": None, "up": None, "sure": False, "why": "read from the USD file's own metadata"}
        try:
            metrics = _read_layer_metrics(source)
            expected.update(mpu=metrics["meters_per_unit"], up=metrics["up_axis"], sure=True)
        except ImportHelperError as exc:
            warnings.append(f"Could not read the USD source's metadata: {exc}")
    wrapper_info: dict[str, Any] = {
        "path": wrapper,
        "prim_path": f"/{asset_name}",
        "kind": "component",
        "meters_per_unit": target_mpu,
        "up_axis": target_up,
        "references": _relative_asset_path(source if is_usd else output, asset_dir)[0],
        "xform_ops": f"{ROTATE_OP} (axis fix) and {SCALE_OP} (unit fix), before the source root's own ops",
        "factor_rule": "scale = source metersPerUnit / project metersPerUnit",
    }
    if expected["mpu"] is not None and expected["up"] in ("Y", "Z"):
        fix = _correction(expected["mpu"], expected["up"], target_mpu, target_up)
        wrapper_info.update(expected_scale=fix["scale"], expected_rotate_x=fix["rotate_x"],
                            explanation=f"{'' if expected['sure'] else 'If typical: '}{fix['scale_explained']}; "
                                        f"{fix['rotate_explained']}.")
    else:
        wrapper_info["explanation"] = ("The source format has no reliable unit/up-axis field, so the factor is "
                                       "decided after conversion by detect_units on the converted file.")
    if not expected["sure"]:
        wrapper_info["note"] = f"Typical for .{ext}: {expected['why']}. detect_units on the converted file decides."

    layout = os.path.join(project, *LAYOUT_LAYER.split("/"))
    layout_ref = _relative_asset_path(wrapper, os.path.dirname(layout))[0]
    arc = "reference" if as_reference is not False else "payload"
    snippet = [
        "from pxr import Sdf, Usd",
        f"stage = Usd.Stage.Open(r\"{os.path.join(project, ROOT_LAYER)}\")",
        f"layout = Sdf.Layer.FindOrOpen(r\"{layout}\")",
        "stage.SetEditTarget(Usd.EditTarget(layout))",
        f"prim = stage.DefinePrim(\"/World/{asset_name}\", \"Xform\")",
        (f"prim.GetReferences().AddReference(\"{layout_ref}\")" if arc == "reference"
         else f"prim.GetPayloads().AddPayload(\"{layout_ref}\")"),
    ]
    if instanceable is True:
        snippet.append("prim.SetInstanceable(True)")
    snippet.append("layout.Save()")
    how_to_place = {
        "layer": LAYOUT_LAYER,
        "layer_exists": os.path.isfile(layout),
        "prim_path": f"/World/{asset_name}",
        "arc": arc,
        "asset_path": layout_ref,
        "instanceable": instanceable is True,
        "explanation": (
            f"Reference the WRAPPER (not the raw converted file) from {LAYOUT_LAYER} at /World/{asset_name}; the "
            "wrapper already matches the project's units and up axis, so no extra scale is needed in the layout."
            + (" A payload instead of a reference lets you unload this heavy asset." if arc == "payload" else "")
            + (" instanceable=True shares one copy of the geometry between many placements (good for repeated "
               "props); you then cannot edit prims inside a single copy." if instanceable is True else "")),
        "python": "\n".join(snippet),
    }
    if not how_to_place["layer_exists"]:
        how_to_place["note"] = f"{LAYOUT_LAYER} was not found; use the layer that holds the scene's placements."

    if is_usd:
        steps = [
            "No conversion is needed: the source is already USD.",
            f"Copy it into assets/{asset_name}/ first if it lives outside the project (keeps references relative).",
            f"wrap_units(source, '{_posix(wrapper)}', target_mpu={target_mpu:g}, target_up='{target_up}', "
            "dry_run=False) authors the wrapper.",
            f"Reference the wrapper from {LAYOUT_LAYER} at /World/{asset_name} (see how_to_place).",
        ]
    else:
        steps = [
            f"Convert {Path(source).name} with {converter} to assets/{asset_name}/{asset_name}.usd "
            "(import_asset(..., dry_run=False) inside Kit; it runs as an async task, poll import_status).",
            "detect_units reads the converted file's metersPerUnit/upAxis and checks them against its size.",
            f"wrap_units authors assets/{asset_name}/{asset_name}_wrapped.usda with the project's units "
            f"({_unit_text(target_mpu)}, {target_up}-up) and the scale/rotate correction.",
            "asset_health_file checks the wrapper (missing textures, absolute paths, heavy meshes, ...).",
            f"Reference the wrapper from {LAYOUT_LAYER} at /World/{asset_name} (see how_to_place), then look at it "
            "in the viewport: it should stand upright at a believable size.",
        ]
    return {
        "ok": True,
        "dry_run": True,
        "source": source,
        "source_format": ext,
        "source_size_bytes": size,
        "converter": None if is_usd else converter,
        "converter_info": None if is_usd else {
            "module": converter, "versions": CONVERTERS[converter]["versions"], "api": CONVERTERS[converter]["api"]},
        "alternatives": alternatives,
        "name": asset_name,
        "project_dir": project,
        "asset_dir": asset_dir,
        "output_path": None if is_usd else output,
        "wrapper": wrapper_info,
        "target": {"meters_per_unit": target_mpu, "units": _unit_text(target_mpu), "up_axis": target_up,
                   "from": units_from},
        "expected_source": {"meters_per_unit": expected["mpu"], "up_axis": expected["up"],
                            "certain": bool(expected["sure"]), "why": expected["why"]},
        "how_to_place": how_to_place,
        "steps": steps,
        "warnings": warnings,
        "note": "Plan only: nothing was converted or written.",
    }


# -------------------------------------------------------------------------------------------------- detect_units


def detect_units(
    usd_path: Any,
    *,
    name_hint: str | None = None,
    workspace_root: Any = None,
    allow_outside_workspace: bool = False,
) -> dict[str, Any]:
    """Read metersPerUnit/upAxis and judge them against the asset's bounding-box size. Read-only.

    name_hint (default: the file name and defaultPrim name) is matched against TYPICAL_SIZES_M, so 'office_chair'
    80 units tall suggests centimetres. Without a known word the heuristic only prefers sizes near 1 m (low
    confidence). confidence is 'high' when authored metadata and the size agree.
    """
    from . import file_stage

    try:
        _need_pxr()
        resolved = file_stage.resolve_scene_file(usd_path, workspace_root=workspace_root,
                                                 allow_outside_workspace=allow_outside_workspace)
        with file_stage.readonly_stage(resolved) as stage:
            report = _detect(stage, resolved, name_hint)
            del stage
    except (file_stage.SceneFileError, file_stage.ReadOnlyViolation, ImportHelperError, ImportError) as exc:
        return _error(str(exc), file=os.fspath(usd_path) if usd_path is not None else None)
    return report


def _hint_size(words: list[str]) -> tuple[str | None, float | None]:
    tokens: list[str] = []
    for word in words:
        if not word:
            continue
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", word)
        tokens += [token for token in re.split(r"[^A-Za-z]+", spaced.lower()) if token]
    for token in tokens:
        if token in TYPICAL_SIZES_M:
            return token, TYPICAL_SIZES_M[token]
        if token.endswith("s") and token[:-1] in TYPICAL_SIZES_M:
            return token[:-1], TYPICAL_SIZES_M[token[:-1]]
    return None, None


def _world_range(stage: Any) -> Any:
    cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
                              useExtentsHint=True)
    prim = stage.GetDefaultPrim() or stage.GetPseudoRoot()
    box = cache.ComputeWorldBound(prim).ComputeAlignedRange()
    if box.IsEmpty() and prim != stage.GetPseudoRoot():
        box = cache.ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
    return box


def _range_dict(box: Any) -> dict[str, Any] | None:
    if box is None or box.IsEmpty():
        return None
    low, high = box.GetMin(), box.GetMax()
    size = [float(high[i] - low[i]) for i in range(3)]
    largest = max(size)
    return {"min": [float(v) for v in low], "max": [float(v) for v in high], "size": size,
            "largest": largest, "largest_axis": "XYZ"[size.index(largest)]}


def _detect(stage: Any, path: str, name_hint: str | None) -> dict[str, Any]:
    authored_mpu = UsdGeom.StageHasAuthoredMetersPerUnit(stage)
    mpu = float(UsdGeom.GetStageMetersPerUnit(stage))
    up = str(UsdGeom.GetStageUpAxis(stage))
    up_authored = stage.GetRootLayer().pseudoRoot.HasInfo("upAxis")
    default_prim = stage.GetDefaultPrim()
    warnings: list[str] = []
    notes: list[str] = []
    bbox = _range_dict(_world_range(stage))
    words = [name_hint or "", Path(path).stem, default_prim.GetName() if default_prim else ""]
    if default_prim:
        words += [child.GetName() for child in list(default_prim.GetChildren())[:3]]
    hint_word, typical = _hint_size(words if name_hint is None else [name_hint])
    heuristic: dict[str, Any] = {"hint": hint_word, "typical_size_m": typical}
    if bbox is None or bbox["largest"] <= 0:
        heuristic.update(suggested_units=None, suggested_meters_per_unit=None, confidence="none",
                         reason="No geometry bounds could be computed, so size cannot hint at the units.")
        confidence = "medium" if authored_mpu else "low"
        suggested = mpu
        agrees = None
    else:
        largest = bbox["largest"]
        reference = typical or 1.0
        candidates = []
        for key in _HEURISTIC_UNITS:
            size_m = largest * UNITS[key]
            candidates.append({"units": key, "meters_per_unit": UNITS[key], "largest_size_m": size_m,
                               "score": abs(math.log10(size_m / reference))})
        candidates.sort(key=lambda item: item["score"])
        best = candidates[0]
        strong = typical is not None and best["score"] < 0.4 and candidates[1]["score"] - best["score"] > 0.6
        heuristic.update(
            candidates=candidates, suggested_units=best["units"], suggested_meters_per_unit=best["meters_per_unit"],
            confidence="medium" if strong else "low",
            reason=(f"The asset is {largest:g} units across (largest axis {bbox['largest_axis']}). "
                    + (f"A {hint_word} is typically about {typical:g} m, so {_UNIT_WORDS[best['units']]} "
                       f"({largest * UNITS[best['units']]:g} m) fits best."
                       if typical else
                       f"With no known object word, sizes near 1 m are preferred: {_UNIT_WORDS[best['units']]} gives "
                       f"{largest * UNITS[best['units']]:g} m. Pass name_hint (e.g. 'chair') for a better guess.")))
        size_now = largest * mpu
        if typical:
            agrees = abs(math.log10(size_now / typical)) < 0.5
        else:
            agrees = 0.01 <= size_now <= 200.0
        if authored_mpu and agrees:
            confidence, suggested = "high", mpu
        elif authored_mpu:
            confidence, suggested = "medium", mpu
            warnings.append(
                f"metersPerUnit says {_unit_text(mpu)}, which makes this asset {size_now:g} m across"
                + (f" - unusual for a {hint_word}" if hint_word else " - an unusual size") +
                f". If it looks wrong in the viewport, the exporter probably wrote the wrong unit; "
                f"{_UNIT_WORDS[best['units']]} ({best['meters_per_unit']:g}) would give "
                f"{largest * best['meters_per_unit']:g} m.")
        else:
            confidence, suggested = heuristic["confidence"], best["meters_per_unit"]
            warnings.append(f"metersPerUnit is not authored (USD falls back to 0.01, centimetres). The size suggests "
                            f"{_UNIT_WORDS[best['units']]}; pass source_mpu to wrap_units to be explicit.")
    if not up_authored:
        warnings.append(f"upAxis is not authored, so USD uses the fallback {up}; check that the asset stands upright.")
    if bbox is not None:
        notes.append(f"bbox is in the file's own units ({_unit_text(mpu)}); size_m converts with metersPerUnit.")
    return {
        "ok": True,
        "file": path,
        "meters_per_unit": mpu,
        "meters_per_unit_authored": bool(authored_mpu),
        "units": _unit_text(mpu),
        "up_axis": up,
        "up_axis_authored": bool(up_authored),
        "default_prim": str(default_prim.GetPath()) if default_prim else None,
        "bbox": bbox,
        "size_m": None if bbox is None else [value * mpu for value in bbox["size"]],
        "heuristic": heuristic,
        "metadata_matches_size": agrees,
        "suggested_meters_per_unit": suggested,
        "suggested_units": _unit_text(suggested),
        "confidence": confidence,
        "warnings": warnings,
        "notes": notes + ["Read-only: nothing was changed or saved."],
    }


# ---------------------------------------------------------------------------------------------------- wrap_units


def wrap_units(
    converted_usd: Any,
    wrapper_path: Any,
    *,
    source_mpu: Any = None,
    source_up: Any = None,
    target_mpu: Any,
    target_up: Any,
    dry_run: bool = True,
    overwrite: bool = False,
    prim_name: str | None = None,
    kind: str = "component",
) -> dict[str, Any]:
    """Author (dry_run=False) or preview a wrapper layer that fixes units and up axis of a converted asset.

    source_mpu / source_up default to the converted file's authored metadata (an unauthored metersPerUnit is an
    error: run detect_units and pass source_mpu). The wrapper gets defaultPrim /<prim_name> (default: the wrapper
    file stem without '_wrapped'), kind, the target metersPerUnit/upAxis, a RELATIVE reference to the converted
    file and the unitsResolve xformOps. Pure pxr, works without Kit. Never overwrites unless overwrite=True.
    """
    try:
        return _wrap(converted_usd, wrapper_path, source_mpu, source_up, target_mpu, target_up, dry_run, overwrite,
                     prim_name, kind)
    except ImportHelperError as exc:
        return _error(str(exc), dry_run=dry_run is not False)


def _wrap(converted_usd: Any, wrapper_path: Any, source_mpu: Any, source_up: Any, target_mpu: Any, target_up: Any,
          dry_run: Any, overwrite: Any, prim_name: Any, kind: Any) -> dict[str, Any]:
    _need_pxr()
    if not converted_usd or not wrapper_path:
        raise ImportHelperError("converted_usd and wrapper_path are both required.")
    for label, raw in (("converted_usd", converted_usd), ("wrapper_path", wrapper_path)):
        if _is_unc(os.fspath(raw)):
            raise ImportHelperError(f"Network (UNC) paths are not supported for {label}.")
    converted = os.path.abspath(os.fspath(converted_usd))
    wrapper = os.path.abspath(os.fspath(wrapper_path))
    if _source_extension(converted) not in USD_EXTENSIONS or not os.path.isfile(converted):
        raise ImportHelperError(f"converted_usd must be an existing USD file: {converted}")
    if os.path.splitext(wrapper)[1].lower() not in (".usda", ".usd"):
        raise ImportHelperError("wrapper_path must end in .usda (readable text) or .usd.")
    if os.path.normcase(converted) == os.path.normcase(wrapper):
        raise ImportHelperError("wrapper_path must be a different file from converted_usd.")
    if not os.path.isdir(os.path.dirname(wrapper)):
        raise ImportHelperError(f"The wrapper's folder does not exist: {os.path.dirname(wrapper)}")
    write = dry_run is False
    exists = os.path.exists(wrapper)
    if exists and overwrite is not True:
        raise ImportHelperError(f"The wrapper {wrapper} already exists; pass overwrite=True to replace it.")
    if prim_name is None:
        stem = Path(wrapper).stem
        prim_name = stem[: -len("_wrapped")] if stem.endswith("_wrapped") and len(stem) > 8 else stem
        prim_name = _default_name(prim_name)
    name = _check_name(prim_name)
    if kind not in ("component", "assembly", "group", "subcomponent", "model", None, ""):
        raise ImportHelperError(f"kind must be a USD model kind such as 'component', not {kind!r}.")
    t_mpu = _parse_mpu(target_mpu, "target_mpu")
    t_up = _parse_up(target_up, "target_up")

    metrics = _read_layer_metrics(converted)
    warnings: list[str] = []
    if source_mpu is None:
        if metrics["meters_per_unit"] is None:
            raise ImportHelperError(
                "The converted file has no metersPerUnit; run asset_detect_units on it and pass source_mpu "
                "(e.g. 'cm' or 0.01).")
        s_mpu = float(metrics["meters_per_unit"])
    else:
        s_mpu = _parse_mpu(source_mpu, "source_mpu")
        if metrics["meters_per_unit"] is not None and not math.isclose(s_mpu, metrics["meters_per_unit"],
                                                                        rel_tol=1e-6):
            warnings.append(f"source_mpu {s_mpu:g} overrides the converted file's metersPerUnit "
                            f"{metrics['meters_per_unit']:g}; the wrapper scales as if the file were "
                            f"{_unit_text(s_mpu)}.")
    if source_up is None:
        if metrics["up_axis"] in ("Y", "Z"):
            s_up = str(metrics["up_axis"])
        else:
            s_up = "Y"
            warnings.append("The converted file has no upAxis; USD's fallback Y-up is assumed (pass source_up).")
    else:
        s_up = _parse_up(source_up, "source_up")

    target_prim = metrics["default_prim"]
    if not target_prim:
        roots = metrics["root_prims"]
        if len(roots) != 1:
            raise ImportHelperError(
                f"The converted file has no defaultPrim and {len(roots)} root prims, so a reference cannot pick one. "
                "Set a defaultPrim on it (or convert with create_world_as_default_root_prim=True).")
        target_prim = roots[0]
        warnings.append(f"The converted file has no defaultPrim; the wrapper references its only root prim "
                        f"/{target_prim} explicitly.")
    ref_path, relative = _relative_asset_path(converted, os.path.dirname(wrapper))
    if not relative:
        warnings.append("The converted file is on another drive, so the reference had to be ABSOLUTE; move both "
                        "files into the project so the asset stays portable.")
    fix = _correction(s_mpu, s_up, t_mpu, t_up)

    # The referenced root's composed xformOps (kept after ours) and its world bounds, for the verification.
    source_stage = Usd.Stage.Open(converted, Usd.Stage.LoadAll)
    if source_stage is None:
        raise ImportHelperError(f"Could not open {converted} with USD.")
    src_prim = source_stage.GetPrimAtPath(f"/{target_prim}")
    their_order = [str(op) for op in (UsdGeom.Xformable(src_prim).GetXformOpOrderAttr().Get() or [])] \
        if src_prim and src_prim.IsA(UsdGeom.Xformable) else []
    their_order = [op for op in their_order if op not in (SCALE_OP, ROTATE_OP)]
    src_range = _world_range(source_stage)
    del source_stage

    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.comment = (f"Wrapper written by omniverse_scene_mcp.import_helper: {fix['scale_explained']}; "
                     f"{fix['rotate_explained']}. Edit placements in the layout layer, not here.")
    layer.defaultPrim = name
    layer.pseudoRoot.SetInfo("metersPerUnit", t_mpu)
    layer.pseudoRoot.SetInfo("upAxis", t_up)
    spec = Sdf.CreatePrimInLayer(layer, f"/{name}")
    spec.specifier = Sdf.SpecifierDef
    spec.typeName = "Xform"
    if kind:
        spec.SetInfo("kind", kind)
    reference = Sdf.Reference(ref_path, Sdf.Path(f"/{target_prim}") if not metrics["default_prim"] else Sdf.Path())
    spec.referenceList.Prepend(reference)
    ours: list[str] = []
    if fix["rotate_x"]:
        attr = Sdf.AttributeSpec(spec, ROTATE_OP, Sdf.ValueTypeNames.Float)
        attr.default = float(fix["rotate_x"])
        ours.append(ROTATE_OP)
    if fix["scale"] != 1.0:
        attr = Sdf.AttributeSpec(spec, SCALE_OP, Sdf.ValueTypeNames.Float3)
        attr.default = Gf.Vec3f(fix["scale"], fix["scale"], fix["scale"])
        ours.append(SCALE_OP)
    # Our ops go first (outermost), the source root's own ops after them, so they still apply (in source units).
    reset = [op for op in their_order if op == "!resetXformStack!"]
    rest = [op for op in their_order if op != "!resetXformStack!"]
    final_order = reset + ours + rest if ours else their_order
    if ours:
        order = Sdf.AttributeSpec(spec, "xformOpOrder", Sdf.ValueTypeNames.TokenArray, Sdf.VariabilityUniform)
        order.default = final_order
    text = layer.ExportToString()

    # Row-vector convention: order [rotateX, scale] -> points are scaled first, then rotated (S * R).
    matrix = Gf.Matrix4d(1.0)
    if fix["scale"] != 1.0:
        matrix = Gf.Matrix4d(1.0).SetScale(float(fix["scale"]))
    if fix["rotate_x"]:
        matrix = matrix * Gf.Matrix4d(1.0).SetRotate(Gf.Rotation(Gf.Vec3d(1, 0, 0), fix["rotate_x"]))
    expected = None if src_range.IsEmpty() else Gf.BBox3d(src_range, matrix).ComputeAlignedRange()

    result: dict[str, Any] = {
        "ok": True,
        "dry_run": not write,
        "wrapper_path": wrapper,
        "converted_usd": converted,
        "prim_path": f"/{name}",
        "reference": ref_path,
        "reference_is_relative": relative,
        "source": {"meters_per_unit": s_mpu, "units": _unit_text(s_mpu), "up_axis": s_up},
        "target": {"meters_per_unit": t_mpu, "units": _unit_text(t_mpu), "up_axis": t_up},
        "scale": fix["scale"],
        "rotate_x": fix["rotate_x"],
        "explanation": f"{fix['scale_explained']}; {fix['rotate_explained']}.",
        "xform_op_order": final_order,
        "kept_source_xform_ops": their_order,
        "expected_bbox": _range_dict(expected),
        "usda": text,
        "warnings": warnings,
    }
    if not write:
        result["note"] = "Preview only (dry_run): nothing was written. Pass dry_run=False to author the wrapper."
        return result
    if not layer.Export(wrapper):
        raise ImportHelperError(f"Could not write {wrapper}")
    existing = Sdf.Layer.Find(wrapper)
    if existing is not None and exists:
        existing.Reload(True)  # a Kit stage that has the old wrapper open sees the new one
    result["written"] = True
    result["replaced_existing"] = exists
    result["verify"] = _verify_wrapper(wrapper, t_mpu, t_up, name, expected)
    if not result["verify"]["ok"]:
        result["warnings"].append("The written wrapper did not compose as expected: " + "; ".join(
            result["verify"]["problems"]))
    return result


def _verify_wrapper(path: str, mpu: float, up: str, name: str, expected: Any) -> dict[str, Any]:
    problems: list[str] = []
    bbox: dict[str, Any] | None = None
    stage = Usd.Stage.Open(path, Usd.Stage.LoadAll)
    if stage is None:
        return {"ok": False, "problems": [f"USD could not open {path}"]}
    try:
        if not math.isclose(float(UsdGeom.GetStageMetersPerUnit(stage)), mpu, rel_tol=1e-9):
            problems.append("metersPerUnit differs from the target")
        if str(UsdGeom.GetStageUpAxis(stage)) != up:
            problems.append("upAxis differs from the target")
        prim = stage.GetDefaultPrim()
        if not prim or prim.GetName() != name:
            problems.append(f"defaultPrim is not /{name}")
        elif not list(prim.GetChildren()) and not prim.IsA(UsdGeom.Gprim):
            problems.append("the referenced asset brought no prims (broken reference?)")
        composed = _world_range(stage)
        bbox = _range_dict(composed)
        if expected is not None and not expected.IsEmpty() and bbox is not None:
            span = max(max(bbox["size"]), 1e-12)
            for got, want in zip(list(composed.GetMin()) + list(composed.GetMax()),
                                 list(expected.GetMin()) + list(expected.GetMax())):
                if abs(got - want) > 1e-4 * span + 1e-9:
                    problems.append("the composed bounding box differs from the expected corrected box")
                    break
        elif expected is not None and bbox is None:
            problems.append("the wrapper composes to empty bounds")
    finally:
        del stage
    return {"ok": not problems, "problems": problems, "composed_bbox": bbox,
            "composed_size_m": None if bbox is None else [value * mpu for value in bbox["size"]]}


# -------------------------------------------------------------------------------------------- import_asset (Kit)


def _kit_module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - ImportError, or a Kit extension that fails while loading
        return None


def _schedule(coro: Any) -> str:
    """Run the job coroutine on Kit's loop; outside Kit (tests) run it to completion right here."""
    engine = _kit_module("omni.kit.async_engine")
    runner = getattr(engine, "run_coroutine", None)
    if callable(runner):
        runner(coro)
        return "omni.kit.async_engine.run_coroutine"
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return "asyncio.run (synchronous)"
    loop.create_task(coro)
    return "asyncio task"


def _remember(job: dict[str, Any]) -> None:
    while len(_JOBS) >= _MAX_JOBS:
        oldest = next((key for key, value in _JOBS.items() if value["status"] != "running"), None)
        _JOBS.pop(oldest if oldest is not None else next(iter(_JOBS)))
    _JOBS[job["job_id"]] = job


def import_status(job_id: str | None = None) -> dict[str, Any]:
    """The state of one import_asset job (running / succeeded / failed), or a list of recent jobs."""
    if job_id is None:
        return {"ok": True, "jobs": [{key: job[key] for key in ("job_id", "status", "source", "output_path")}
                                     for job in _JOBS.values()]}
    job = _JOBS.get(str(job_id))
    if job is None:
        return _error(f"No import job {job_id!r} in this Kit session (jobs are kept in memory only).")
    return {"ok": True, **_public_job(job)}


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in job.items() if not key.startswith("_")}


def import_asset(
    source_path: Any,
    *,
    project_dir: Any,
    name: Any = None,
    target_units: Any = None,
    up_axis: Any = None,
    as_reference: bool = True,
    instanceable: bool = False,
    dry_run: bool = True,
    overwrite: bool = False,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Kit-only: convert source_path into <project>/assets/<name>/<name>.usd, wrap its units, health-check it.

    dry_run (default True) returns plan_import's plan. With dry_run=False inside Kit it starts the converter task and
    returns {status: 'running', job_id}; poll import_status(job_id). When the converter finishes it runs
    detect_units, wrap_units(dry_run=False) and asset_health_file on the wrapper. Existing outputs are never
    overwritten unless overwrite=True. options: AssetConverterContext attributes (mesh formats) or CAD
    file_format_args (string values). Nothing is placed into a scene layer.
    """
    plan = plan_import(source_path, project_dir=project_dir, name=name, target_units=target_units, up_axis=up_axis,
                       as_reference=as_reference, instanceable=instanceable)
    if not plan.get("ok"):
        return plan
    if dry_run is not False:
        plan["note"] = "Dry run: nothing was converted. Pass dry_run=False inside Kit to convert."
        return plan
    if plan["converter"] is None:
        return _error("The source is already USD: no conversion is needed; use asset_wrap_units on it directly.",
                      plan=plan)
    output, wrapper = plan["output_path"], plan["wrapper"]["path"]
    if overwrite is not True:
        for path in (output, wrapper):
            if os.path.exists(path):
                return _error(f"{path} already exists; pass overwrite=True to replace it.", plan=plan)

    module_name = plan["converter"]
    module = _kit_module(module_name)
    if module is None:
        return _not_kit(f"{module_name} is not importable (run this inside Isaac Sim / USD Composer with the "
                        "extension enabled)")
    getter = getattr(module, "get_instance", None)
    instance = getter() if callable(getter) else None
    if instance is None:
        return _error(f"{module_name} is importable but not started; enable it in Window > Extensions.")
    asset_dir = plan["asset_dir"]
    job: dict[str, Any] = {
        "job_id": uuid.uuid4().hex[:12],
        "status": "running",
        "source": plan["source"],
        "converter": module_name,
        "output_path": output,
        "wrapper_path": wrapper,
        "options": None,
        "started": time.time(),
        "progress": None,
        "_plan": plan,
        "_created_dirs": [path for path in (os.path.dirname(asset_dir), asset_dir) if not os.path.isdir(path)],
        "_output_existed": os.path.exists(output),
        "_overwrite": overwrite is True,
    }
    try:
        call, job["options"] = _converter_call(module, module_name, instance, plan, options, job)
    except ImportHelperError as exc:
        return _error(str(exc), plan=plan)
    try:
        os.makedirs(asset_dir, exist_ok=True)
    except OSError as exc:
        return _error(f"Could not create {asset_dir}: {exc}", plan=plan)
    _remember(job)
    try:
        how = _schedule(_run_job(job, call))
    except Exception as exc:  # noqa: BLE001
        _fail(job, f"Could not start the converter task: {type(exc).__name__}: {exc}")
        return {"ok": False, "error": job["error"], **_public_job(job)}
    job["scheduled_with"] = how
    result = {"ok": job["status"] != "failed", **_public_job(job), "plan": plan,
              "message": ("Converting in the background: poll import_status(job_id)." if job["status"] == "running"
                          else f"Import {job['status']}.")}
    if job["status"] == "failed":
        result["error"] = job.get("error")
    return result


def _converter_call(module: Any, module_name: str, instance: Any, plan: dict[str, Any],
                    options: dict[str, Any] | None, job: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """A no-argument callable that creates the converter task, and the options it uses."""
    if options is not None and not isinstance(options, dict):
        raise ImportHelperError("options must be a dict of option names to values.")
    options = dict(options or {})
    source, output = plan["source"], plan["output_path"]

    def progress(current: Any, total: Any = None) -> None:
        job["progress"] = [current, total]

    if module_name == "omni.kit.asset_converter":
        unknown = sorted(set(options) - set(ASSET_CONVERTER_OPTIONS))
        if unknown:
            raise ImportHelperError(f"Unknown AssetConverterContext option(s) {unknown}; valid: "
                                    f"{', '.join(ASSET_CONVERTER_OPTIONS)}.")
        context_cls = getattr(module, "AssetConverterContext", None)
        if context_cls is None:
            raise ImportHelperError("omni.kit.asset_converter has no AssetConverterContext in this Kit version.")
        context = context_cls()
        # Keep the source's units: the wrapper fixes them, so the converted file stays a faithful copy.
        settings = {"use_meter_as_world_unit": False, "create_world_as_default_root_prim": True, **options}
        for key, value in settings.items():
            if not isinstance(value, bool):
                raise ImportHelperError(f"AssetConverterContext.{key} must be True or False, not {value!r}.")
            setattr(context, key, value)

        def call() -> Any:
            return instance.create_converter_task(source, output, progress, context)

        return call, settings
    args = {str(key): str(value) for key, value in options.items()}

    def cad_call() -> Any:
        return instance.create_converter_task(source, output, args)

    return cad_call, args


def _fail(job: dict[str, Any], message: str, **extra: Any) -> None:
    job.update(status="failed", error=message, finished=time.time(), **extra)
    cleaned = []
    if not job.get("_output_existed") and os.path.isfile(job["output_path"]):
        try:
            os.remove(job["output_path"])  # a partial file this job created
            cleaned.append(job["output_path"])
        except OSError:
            pass
    for folder in reversed(job.get("_created_dirs", [])):
        try:
            os.rmdir(folder)  # only removes folders this job created and that are still empty
            cleaned.append(folder)
        except OSError:
            pass
    if cleaned:
        job["cleaned_up"] = cleaned


async def _run_job(job: dict[str, Any], call: Any) -> None:
    try:
        task = call()
        if job["converter"] == "omni.kit.asset_converter":
            success = await task.wait_until_finished()
            if not success:
                _fail(job, f"The converter failed: {task.get_error_message() or 'no message'}",
                      converter_status=str(task.get_status()))
                return
            message = task.get_error_message()
            if message:
                job["converter_message"] = message
        else:
            outcome = await task if inspect.isawaitable(task) else task
            url, status = (outcome if isinstance(outcome, tuple) and len(outcome) == 2 else (outcome, None))
            code = getattr(status, "error_code", 0 if url else -1)
            if not url or code not in (0, None):
                _fail(job, f"The CAD converter failed: {getattr(status, 'error_msg', '') or 'no message'}",
                      converter_status=code)
                return
    except Exception as exc:  # noqa: BLE001 - converter raised
        _fail(job, f"The converter raised {type(exc).__name__}: {exc}")
        return
    if not os.path.isfile(job["output_path"]):
        _fail(job, "The converter reported success but wrote no output file.")
        return
    try:
        _post_process(job)
    except Exception as exc:  # noqa: BLE001 - keep the converted file; report what went wrong
        job.update(status="failed", error=f"Converted, but post-processing raised {type(exc).__name__}: {exc}",
                   finished=time.time())


def _post_process(job: dict[str, Any]) -> None:
    from . import asset_health

    plan = job["_plan"]
    target = plan["target"]
    detected = detect_units(job["output_path"], name_hint=plan["name"], workspace_root=plan["project_dir"])
    job["detected_units"] = detected
    source_mpu = None
    if detected.get("ok") and not detected.get("meters_per_unit_authored"):
        source_mpu = detected.get("suggested_meters_per_unit")
    wrapped = wrap_units(job["output_path"], job["wrapper_path"], source_mpu=source_mpu,
                         target_mpu=target["meters_per_unit"], target_up=target["up_axis"], dry_run=False,
                         overwrite=job["_overwrite"], prim_name=plan["name"])
    job["wrap"] = {key: value for key, value in wrapped.items() if key != "usda"}
    if not wrapped.get("ok"):
        job.update(status="failed", error=f"Converted, but the wrapper failed: {wrapped.get('error')}",
                   finished=time.time())
        return
    health = asset_health.asset_health_file(job["wrapper_path"], project_root=plan["project_dir"],
                                            workspace_root=plan["project_dir"])
    job["health"] = {
        key: health.get(key) for key in ("ok", "healthy", "score", "grade", "summary", "error")
        if key in health
    }
    if health.get("ok"):
        job["health"]["problems"] = [{"id": c["id"], "title": c["title"], "status": c["status"]}
                                     for c in health.get("checks", []) if c.get("status") in ("fail", "warn")]
    job.update(status="succeeded", finished=time.time(), how_to_place=plan["how_to_place"],
               message=(f"Converted and wrapped. {wrapped.get('explanation', '')} Next: reference "
                        f"{plan['how_to_place']['asset_path']} at {plan['how_to_place']['prim_path']} "
                        f"in {plan['how_to_place']['layer']}."))
