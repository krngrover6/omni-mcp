"""Project starter: plan and create a new, well-organized OpenUSD project folder from a template.

Every project gets the same shape (see OMNIVERSE_WORKSPACE_CONTEXT.md, "New-project protocol"):

    <name>/
      root.usda            scene entry: stage metadata + sublayers, no prims (a table of contents)
      layers/*.usda        one layer per concern (layout, materials, lighting, physics, ...), strongest first
      assets/ scenes/ renders/ scripts/ docs/
      PROJECT_CONTEXT.md   filled from NEW_PROJECT_CONTEXT_TEMPLATE.md
      README.md            first steps for a beginner
      .gitignore           keeps renders and caches out of git

Safety rules:

* plan_project() and list_projects() never write anything.
* create_project() writes only when dry_run is exactly False (a missing or malformed flag only previews), only inside
  root, and only into a NEW folder: it refuses an existing folder, names with anything but letters, digits, '_'
  and '-', reserved Windows names and over-long names. If writing or the composition check fails, it removes the
  files and folders it created itself (and nothing else).
* root defaults to the workspace: $OMNIVERSE_ROOT, else the folder that holds omniverse_scene_mcp/ (as file_stage).

USD files are authored with Sdf (usd-core) using APIs present in USD 25.11 and 26.8. Template positions and sizes
are stored in meters with Z up and converted to the chosen units and up axis.
"""

from __future__ import annotations

import functools
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .file_stage import default_workspace_root

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = Usd = UsdGeom = UsdShade = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

TEMPLATES_FILE = Path(__file__).resolve().parent / "data" / "project_templates.json"
ROOT_LAYER = "root.usda"
FOLDERS = ("assets", "layers", "scenes", "renders", "scripts", "docs")
MAX_NAME_LENGTH = 64
MAX_GOAL_LENGTH = 500
MAX_LISTED_PROJECTS = 500
_CONTEXT_READ_LIMIT = 64 * 1024

UNITS: dict[str, float] = {"m": 1.0, "cm": 0.01, "mm": 0.001}
_UNIT_ALIASES = {
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "mm": "mm", "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
}
_UNIT_WORDS = {"m": "meter", "cm": "centimeter", "mm": "millimeter"}
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {f"{port}{n}" for port in ("COM", "LPT") for n in range(10)}
)
# Attributes in these namespaces belong to a schema; anything else (twin:*, sdg:*) is declared custom.
_SCHEMA_NAMESPACES = frozenset({"physics", "semantics", "inputs", "outputs", "primvars", "xformOp", "info"})
_SHADER_NAME = "PreviewSurface"


class ProjectStarterError(ValueError):
    """Raised for a bad template, name, root, units or up axis; the public functions turn it into ok=False."""


# --------------------------------------------------------------------------------------------------- public API


def list_templates() -> dict[str, Any]:
    """Every template with its units, up axis, default prim and layer stack (strongest first). Read-only."""
    templates = _templates()
    rows = []
    for name, template in templates.items():
        rows.append({
            "name": name,
            "title": template["title"],
            "app": template["app"],
            "description": template["description"],
            "up_axis": template["up_axis"],
            "units": template["units"],
            "meters_per_unit": UNITS[template["units"]],
            "unit_choices": list(template["unit_choices"]),
            "kilograms_per_unit": template.get("kilograms_per_unit"),
            "default_prim": template["default_prim"],
            "layers": [ROOT_LAYER] + [_layer_file(layer["name"]) for layer in template["layers"]],
        })
    return {
        "ok": True,
        "count": len(rows),
        "templates": rows,
        "how_to_use": "plan_project(name, template) previews a project; create_project(..., dry_run=False) writes it.",
    }


def plan_project(
    name: str,
    template: str = "blank",
    *,
    root: str | os.PathLike[str] | None = None,
    goal: str = "",
    units: str | float | None = None,
    up_axis: str | None = None,
) -> dict[str, Any]:
    """Describe the folder create_project would write, without writing anything (dry_run is always True)."""
    try:
        plan = _plan(name, template, root=root, goal=goal, units=units, up_axis=up_axis)
    except ProjectStarterError as exc:
        return {"ok": False, "dry_run": True, "name": name, "template": template, "error": str(exc)}
    return _public_plan(plan)


def create_project(
    name: str,
    template: str = "blank",
    *,
    root: str | os.PathLike[str] | None = None,
    goal: str = "",
    units: str | float | None = None,
    up_axis: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create a new project folder. Writes only when dry_run is False; otherwise returns the plan."""
    try:
        plan = _plan(name, template, root=root, goal=goal, units=units, up_axis=up_axis)
    except ProjectStarterError as exc:
        return {"ok": False, "dry_run": dry_run is not False, "name": name, "template": template, "error": str(exc)}
    if dry_run is not False:
        result = _public_plan(plan)
        result["message"] = "Dry run: nothing was written. Call project_create(..., dry_run=False) to create it."
        return result
    if _PXR_IMPORT_ERROR is not None:
        return {
            "ok": False, "dry_run": False, "name": plan["name"], "template": plan["template"],
            "project_dir": str(plan["project_dir"]),
            "error": f"Creating a project needs the USD Python bindings (pxr): {_PXR_IMPORT_ERROR}",
        }
    return _create(plan)


def list_projects(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Folders directly under root that have a PROJECT_CONTEXT.md, with goal, scene entry and last change. Read-only."""
    try:
        root_dir, _ = _resolve_root(root)
    except ProjectStarterError as exc:
        return {"ok": False, "error": str(exc), "projects": [], "count": 0}
    projects = []
    truncated = False
    try:
        children = sorted(root_dir.iterdir(), key=lambda path: path.name.lower())
    except OSError as exc:
        return {"ok": False, "root": str(root_dir), "error": f"Could not list {root_dir}: {exc}", "projects": [], "count": 0}
    for child in children:
        if child.name.startswith("."):
            continue
        context = child / "PROJECT_CONTEXT.md"
        try:
            if not child.is_dir() or not context.is_file():
                continue
        except OSError:
            continue
        if len(projects) >= MAX_LISTED_PROJECTS:
            truncated = True
            break
        projects.append(_describe_project(child, context))
    return {
        "ok": True,
        "root": str(root_dir),
        "count": len(projects),
        "truncated": truncated,
        "projects": projects,
        "note": "Read-only: folders directly under root that contain PROJECT_CONTEXT.md.",
    }


# --------------------------------------------------------------------------------------------------- templates


@functools.lru_cache(maxsize=1)
def _templates() -> dict[str, dict[str, Any]]:
    data = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
    templates = data["templates"]
    for name, template in templates.items():
        if template["units"] not in UNITS or any(unit not in UNITS for unit in template["unit_choices"]):
            raise ValueError(f"project template {name!r} uses unknown units")
        if template["up_axis"] not in ("Y", "Z"):
            raise ValueError(f"project template {name!r} has an invalid up_axis")
        known = {layer["name"] for layer in template["layers"]}
        for prim in template["prims"]:
            if prim["layer"] not in known:
                raise ValueError(f"project template {name!r}: prim {prim['path']} names unknown layer {prim['layer']!r}")
    return templates


def _layer_file(layer_name: str) -> str:
    return f"layers/{layer_name}.usda"


# --------------------------------------------------------------------------------------------------- validation


def _check_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProjectStarterError("Give the project a name, for example 'warehouse_study'.")
    if name != name.strip():
        raise ProjectStarterError("The project name must not start or end with spaces.")
    if len(name) > MAX_NAME_LENGTH:
        raise ProjectStarterError(f"The project name is too long ({len(name)} characters, at most {MAX_NAME_LENGTH}).")
    if not _NAME.match(name):
        raise ProjectStarterError(
            f"Invalid project name {name!r}: use only letters, digits, '_' and '-', starting with a letter or digit "
            "(no spaces, dots, slashes or drive letters), for example 'robot_cell_01'."
        )
    if name.upper() in _RESERVED_NAMES:
        raise ProjectStarterError(f"{name!r} is a reserved device name on Windows; pick another project name.")
    return name


def _resolve_root(root: str | os.PathLike[str] | None) -> tuple[Path, list[str]]:
    warnings: list[str] = []
    default = Path(default_workspace_root())
    raw = default if root is None else Path(os.fspath(root)).expanduser()
    text = str(raw)
    if text.startswith(("\\\\", "//")):
        raise ProjectStarterError(f"Network (UNC) roots are not supported: {text}")
    if not raw.is_absolute():
        raise ProjectStarterError(f"root must be an absolute folder path, not {text!r}.")
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectStarterError(f"The root folder does not exist: {raw}") from exc
    if not resolved.is_dir():
        raise ProjectStarterError(f"The root is not a folder: {resolved}")
    if resolved == Path(resolved.anchor):
        raise ProjectStarterError(f"Refusing to use a drive root ({resolved}) as the project root; pick a folder.")
    if root is not None:
        try:
            workspace = default.resolve()
        except OSError:
            workspace = default
        if not _is_within(resolved, workspace):
            warnings.append(f"root {resolved} is outside the Omniverse workspace {workspace}.")
    return resolved, warnings


def _is_within(path: Path, parent: Path) -> bool:
    child_text, parent_text = os.path.normcase(str(path)), os.path.normcase(str(parent))
    try:
        return os.path.commonpath([child_text, parent_text]) == parent_text
    except ValueError:
        return False


def _resolve_units(units: str | float | None, template: dict[str, Any]) -> str:
    if units is None or (isinstance(units, str) and not units.strip()):
        return template["units"]
    if isinstance(units, bool):
        raise ProjectStarterError("units must be 'm', 'cm' or 'mm'.")
    if isinstance(units, (int, float)):
        for key, value in UNITS.items():
            if math.isclose(float(units), value, rel_tol=1e-9):
                return key
        raise ProjectStarterError(f"meters per unit {units!r} is not supported; use 'm' (1), 'cm' (0.01) or 'mm' (0.001).")
    key = _UNIT_ALIASES.get(str(units).strip().lower())
    if key is None:
        raise ProjectStarterError(f"Unknown units {units!r}; use 'm', 'cm' or 'mm'.")
    return key


def _resolve_up_axis(up_axis: str | None, template: dict[str, Any]) -> str:
    if up_axis is None or (isinstance(up_axis, str) and not up_axis.strip()):
        return template["up_axis"]
    value = str(up_axis).strip().upper()
    if value not in ("Y", "Z"):
        raise ProjectStarterError(f"up_axis must be 'Y' or 'Z', not {up_axis!r} (USD supports only these two).")
    return value


def _clean_goal(goal: Any, template: dict[str, Any]) -> str:
    text = "" if goal is None else str(goal)
    text = "".join(ch if ch.isprintable() or ch == "\n" else " " for ch in text).strip()
    if len(text) > MAX_GOAL_LENGTH:
        text = text[: MAX_GOAL_LENGTH - 3].rstrip() + "..."
    return text or template["goal"]


# --------------------------------------------------------------------------------------------------- planning


def _plan(
    name: Any,
    template: Any,
    *,
    root: str | os.PathLike[str] | None,
    goal: Any,
    units: str | float | None,
    up_axis: str | None,
) -> dict[str, Any]:
    templates = _templates()
    template_name = str(template or "blank").strip().lower()
    if template_name not in templates:
        raise ProjectStarterError(f"Unknown template {template!r}; choose one of: {', '.join(templates)}.")
    spec = templates[template_name]
    name = _check_name(name)
    root_dir, warnings = _resolve_root(root)
    project_dir = root_dir / name
    if project_dir.parent != root_dir or not _is_within(project_dir, root_dir):
        raise ProjectStarterError(f"The project folder must be directly inside {root_dir}.")
    if os.path.lexists(project_dir):
        raise ProjectStarterError(
            f"{project_dir} already exists. The starter never overwrites anything: pick a new name or remove it yourself."
        )
    unit_key = _resolve_units(units, spec)
    axis = _resolve_up_axis(up_axis, spec)
    if unit_key not in spec["unit_choices"]:
        warnings.append(
            f"Template {template_name!r} is usually {'/'.join(spec['unit_choices'])}; {unit_key} was requested."
            + (" Isaac Sim physics works best in meters." if spec.get("kilograms_per_unit") else "")
        )
    if axis != spec["up_axis"]:
        warnings.append(
            f"Template {template_name!r} is usually {spec['up_axis']}-up; {axis}-up was requested. "
            "Assets authored for the other axis need a corrective rotation when referenced."
        )
    settings = {
        "units": unit_key,
        "meters_per_unit": UNITS[unit_key],
        "up_axis": axis,
        "kilograms_per_unit": spec.get("kilograms_per_unit"),
        "default_prim": spec["default_prim"],
    }
    return {
        "name": name,
        "template": template_name,
        "spec": spec,
        "root": root_dir,
        "project_dir": project_dir,
        "goal": _clean_goal(goal, spec),
        "settings": settings,
        "warnings": warnings,
    }


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    spec, settings = plan["spec"], plan["settings"]
    project_dir = plan["project_dir"]
    return {
        "ok": True,
        "dry_run": True,
        "name": plan["name"],
        "template": plan["template"],
        "title": spec["title"],
        "app": spec["app"],
        "goal": plan["goal"],
        "root": str(plan["root"]),
        "project_dir": str(project_dir),
        "scene_entry": str(project_dir / ROOT_LAYER),
        "settings": dict(settings),
        "units_explained": _units_sentence(settings),
        "files": _file_list(plan),
        "layer_stack": _layer_stack(spec),
        "starter_prims": _starter_prims(spec),
        "warnings": list(plan["warnings"]),
    }


def _units_sentence(settings: dict[str, Any]) -> str:
    unit = settings["units"]
    text = f"upAxis {settings['up_axis']}, metersPerUnit {settings['meters_per_unit']:g}: 1 unit = 1 {_UNIT_WORDS[unit]}"
    text += "." if unit == "m" else f", so 1 meter is {1 / settings['meters_per_unit']:g} units."
    if settings.get("kilograms_per_unit") is not None:
        text += f" kilogramsPerUnit {settings['kilograms_per_unit']:g}: masses are in kilograms."
    return text


def _layer_stack(spec: dict[str, Any]) -> dict[str, Any]:
    layers = [{
        "file": ROOT_LAYER,
        "strength": 0,
        "owns": "Stage metadata (upAxis, metersPerUnit, defaultPrim) and the list of sublayers. No prims.",
        "why": "Strongest file. Composer saves edits here unless you pick another authoring layer, so it starts "
               "empty: anything that lands here by accident is easy to spot and move.",
    }]
    for strength, layer in enumerate(spec["layers"], start=1):
        layers.append({"file": _layer_file(layer["name"]), "strength": strength, "owns": layer["owns"], "why": layer["why"]})
    return {
        "order": [layer["file"] for layer in layers],
        "layers": layers,
        "explanation": (
            f"{spec['layer_order_why']} The root layer lists its sublayers strongest first; the session layer "
            "(unsaved, in memory) sits above all of them."
        ),
    }


def _starter_prims(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for prim in spec["prims"]:
        does = []
        if prim.get("type"):
            does.append(f"def {prim['type']}")
        else:
            does.append("over")
        if prim.get("bind"):
            does.append(f"binds {prim['bind']}")
        if prim.get("apis"):
            does.append("applies " + ", ".join(prim["apis"]))
        if prim.get("attrs"):
            does.append("sets " + ", ".join(attr["name"] for attr in prim["attrs"]))
        if prim.get("variant_set"):
            vset = prim["variant_set"]
            does.append(f"variant set '{vset['name']}' ({', '.join(vset['variants'])}; default {vset['default']})")
        rows.append({
            "path": prim["path"],
            "layer": _layer_file(prim["layer"]),
            "specifier": "def" if prim.get("type") else "over",
            "type": prim.get("type", ""),
            "authors": "; ".join(does),
        })
    return rows


def _file_list(plan: dict[str, Any]) -> list[dict[str, str]]:
    spec = plan["spec"]
    files = [{"path": f"{folder}/", "kind": "folder", "summary": _FOLDER_PURPOSE[folder]} for folder in FOLDERS]
    sublayers = ", ".join(_layer_file(layer["name"]) for layer in spec["layers"])
    files.append({
        "path": ROOT_LAYER, "kind": "usd_root",
        "summary": f"Scene entry. {_units_sentence(plan['settings'])} defaultPrim /{spec['default_prim']}. "
                   f"Sublayers (strongest first): {sublayers}.",
    })
    for layer in spec["layers"]:
        prims = sorted({prim["path"] for prim in spec["prims"] if prim["layer"] == layer["name"]})
        files.append({
            "path": _layer_file(layer["name"]), "kind": "usd_layer",
            "summary": f"{layer['owns']} Authors: {', '.join(prims) if prims else 'nothing yet'}.",
        })
    for path, kind, summary in _TEXT_FILES:
        files.append({"path": path, "kind": kind, "summary": summary})
    return files


_FOLDER_PURPOSE = {
    "assets": "Source assets (USD, textures). Record each asset's source and license in PROJECT_CONTEXT.md.",
    "layers": "The sublayers of root.usda, one per concern.",
    "scenes": "Extra entry scenes (shots, variations) that sublayer ../root.usda.",
    "renders": "Rendered images and synthetic data output (ignored by git).",
    "scripts": "Python helpers for this project.",
    "docs": "Notes and explanations for this project.",
}
_TEXT_FILES = (
    ("PROJECT_CONTEXT.md", "markdown", "Project record from NEW_PROJECT_CONTEXT_TEMPLATE.md: goal, entry, prims, layers, controls, lessons, next steps."),
    ("README.md", "markdown", "How to open the scene and the first steps."),
    (".gitignore", "gitignore", "Keeps renders/, Replicator output, thumbnails and Python caches out of git."),
    ("docs/layer_stack.md", "markdown", "Why the layers are in this order (strongest first)."),
    ("assets/README.md", "markdown", "Where assets go and how to reference them with relative paths."),
    ("scenes/README.md", "markdown", "What the scenes/ folder is for."),
    ("renders/.gitkeep", "placeholder", "Keeps the empty renders/ folder in git."),
    ("scripts/inspect_scene.py", "python", "Read-only beginner script: prints units, the layer stack and which layer defines each prim."),
)


# --------------------------------------------------------------------------------------------------- creating


def _create(plan: dict[str, Any]) -> dict[str, Any]:
    project_dir: Path = plan["project_dir"]
    created_dirs: list[Path] = []
    created_files: list[Path] = []
    base = {"dry_run": False, "name": plan["name"], "template": plan["template"], "project_dir": str(project_dir)}
    try:
        layers = _build_layers(plan)
        texts = _build_texts(plan)
    except Exception as exc:  # a template bug: nothing has been written yet
        return {**base, "ok": False, "error": f"Could not build the project files: {exc}", "created": []}
    try:
        os.mkdir(project_dir)  # fails if the folder appeared since planning: never overwrite
    except FileExistsError:
        return {**base, "ok": False, "error": f"{project_dir} already exists; nothing was written.", "created": []}
    except OSError as exc:
        return {**base, "ok": False, "error": f"Could not create {project_dir}: {exc}", "created": []}
    created_dirs.append(project_dir)
    try:
        for folder in FOLDERS:
            path = project_dir / folder
            os.mkdir(path)
            created_dirs.append(path)
        for relative, layer in layers.items():
            path = project_dir / relative
            if os.path.lexists(path):
                raise FileExistsError(path)
            if not layer.Export(str(path)):
                raise OSError(f"Sdf could not export {relative}")
            created_files.append(path)
            cached = Sdf.Layer.Find(str(path))
            if cached is not None:  # an old layer object with this path may still be in Sdf's registry
                cached.Reload(True)
        for relative, text in texts.items():
            path = project_dir / relative
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                created_files.append(path)
                handle.write(text)
        verification = _verify(plan)
    except Exception as exc:
        removed = _roll_back(created_files, created_dirs)
        return {**base, "ok": False, "error": f"Creating the project failed: {exc}", "created": [], "rolled_back": removed}
    if not verification["ok"]:
        removed = _roll_back(created_files, created_dirs)
        return {
            **base, "ok": False, "created": [], "rolled_back": removed, "validation": verification,
            "error": "The new root.usda did not compose as expected, so the project was removed again: "
                     + "; ".join(check["detail"] for check in verification["checks"] if not check["ok"]),
        }
    scene_entry = project_dir / ROOT_LAYER
    created = [f"{folder}/" for folder in FOLDERS] + sorted(
        path.relative_to(project_dir).as_posix() for path in created_files
    )
    return {
        **base,
        "ok": True,
        "scene_entry": str(scene_entry),
        "settings": dict(plan["settings"]),
        "created": created,
        "layer_stack": _layer_stack(plan["spec"]),
        "validation": verification,
        "open_in": f"Composer: File > Open {scene_entry}",
        "open_in_isaac": f"Isaac Sim: File > Open {scene_entry}",
        "next_steps": _next_steps(plan),
        "warnings": list(plan["warnings"]),
    }


def _roll_back(files: list[Path], dirs: list[Path]) -> list[str]:
    """Remove only what this call created: its files, then its (now empty) folders, newest first."""
    removed = []
    for path in reversed(files):
        try:
            os.remove(path)
            removed.append(str(path))
        except OSError:
            pass
    for path in reversed(dirs):
        try:
            os.rmdir(path)
            removed.append(str(path))
        except OSError:
            pass
    return removed


def _next_steps(plan: dict[str, Any]) -> list[str]:
    spec = plan["spec"]
    entry = plan["project_dir"] / ROOT_LAYER
    app = "Isaac Sim" if spec["app"].startswith("Isaac") else "USD Composer"
    first_layer = _layer_file(spec["layers"][0]["name"])
    layout_layer = _layer_file("layout")
    steps = [
        f"Open the scene in {app}: File > Open > {entry}.",
        f"Open the Layer window (the Layer tab next to Stage): root.usda lists {', '.join(_layer_file(l['name']) for l in spec['layers'])}, "
        f"strongest first ({first_layer} is the strongest sublayer).",
        f"Before editing, right-click {layout_layer} in the Layer window > Set Authoring Layer. Otherwise your edits "
        "are saved into root.usda, which is meant to stay empty.",
    ]
    steps.extend(spec["next_steps"])
    steps.append(
        "Check the scene from Python (read-only): python scripts/inspect_scene.py with a Python that has pxr, "
        "or open it in the Script Editor (Window > Script Editor) and press Run."
    )
    steps.append("Fill in PROJECT_CONTEXT.md as you go: assets and licenses, decisions, lessons and next steps.")
    return steps


# --------------------------------------------------------------------------------------------------- USD authoring


def _to_stage(vector_m: list[float] | tuple[float, ...], settings: dict[str, Any]) -> tuple[float, float, float]:
    """A template point (meters, Z up) in stage units and axes. Z-up to Y-up is (x, y, z) -> (x, z, -y)."""
    scale = 1.0 / settings["meters_per_unit"]
    x, y, z = (float(value) * scale for value in vector_m)
    if settings["up_axis"] == "Y":
        return (x, z, -y + 0.0)
    return (x, y, z)


def _up_vector(settings: dict[str, Any]) -> tuple[float, float, float]:
    return (0.0, 1.0, 0.0) if settings["up_axis"] == "Y" else (0.0, 0.0, 1.0)


def _stage_metadata(layer: Any, settings: dict[str, Any]) -> None:
    layer.pseudoRoot.SetInfo("upAxis", settings["up_axis"])
    layer.pseudoRoot.SetInfo("metersPerUnit", float(settings["meters_per_unit"]))
    if settings.get("kilograms_per_unit") is not None:
        layer.pseudoRoot.SetInfo("kilogramsPerUnit", float(settings["kilograms_per_unit"]))


def _build_layers(plan: dict[str, Any]) -> dict[str, Any]:
    """Anonymous Sdf layers keyed by their path inside the project; root.usda first."""
    spec, settings = plan["spec"], plan["settings"]
    root = Sdf.Layer.CreateAnonymous(".usda")
    _stage_metadata(root, settings)
    root.defaultPrim = settings["default_prim"]
    root.documentation = (
        f"Scene entry for project '{plan['name']}' ({spec['title']}). This root layer only holds stage metadata and "
        "the sublayer list (strongest first); prims live in ./layers/. See docs/layer_stack.md."
    )
    for layer in spec["layers"]:
        root.subLayerPaths.append(f"./{_layer_file(layer['name'])}")
    layers: dict[str, Any] = {ROOT_LAYER: root}
    for layer_info in spec["layers"]:
        layer = Sdf.Layer.CreateAnonymous(".usda")
        _stage_metadata(layer, settings)  # so the layer reads correctly when opened on its own
        layer.documentation = f"{layer_info['name']}.usda: {layer_info['owns']}"
        layers[_layer_file(layer_info["name"])] = layer
    for prim in spec["prims"]:
        _author_prim(layers[_layer_file(prim["layer"])], prim, settings)
    return layers


def _author_prim(layer: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    path = Sdf.Path(prim["path"])
    spec = layer.GetPrimAtPath(path) or Sdf.CreatePrimInLayer(layer, path)  # missing ancestors become 'over'
    type_name = prim.get("type")
    if type_name:
        spec.specifier = Sdf.SpecifierDef
        spec.typeName = type_name
    if prim.get("kind"):
        spec.kind = prim["kind"]
    if prim.get("doc"):
        spec.documentation = prim["doc"]
    if prim.get("apis"):
        _prepend_apis(spec, prim["apis"])
    builder = _BUILDERS.get(type_name or "")
    if builder is not None:
        builder(spec, prim, settings)
    if prim.get("position_m"):
        _translate(spec, _to_stage(prim["position_m"], settings))
    for attr in prim.get("attrs", ()):
        namespace = attr["name"].split(":", 1)[0]
        _attr(spec, attr["name"], attr["type"], attr["value"], custom=namespace not in _SCHEMA_NAMESPACES)
    if prim.get("bind"):
        _bind(spec, prim["bind"])
    if prim.get("variant_set"):
        _variant_set(spec, prim["variant_set"])


_VALUE_TYPES = {
    "bool": "Bool", "int": "Int", "float": "Float", "double": "Double", "string": "String", "token": "Token",
    "token[]": "TokenArray", "int[]": "IntArray", "float2": "Float2", "float3": "Float3", "double3": "Double3",
    "color3f": "Color3f", "vector3f": "Vector3f", "point3f[]": "Point3fArray", "float3[]": "Float3Array",
}


def _attr(spec: Any, name: str, type_name: str, value: Any, *, custom: bool = False, uniform: bool = False) -> Any:
    value_type = getattr(Sdf.ValueTypeNames, _VALUE_TYPES[type_name])
    variability = Sdf.VariabilityUniform if uniform else Sdf.VariabilityVarying
    attr = spec.attributes.get(name) if hasattr(spec.attributes, "get") else None
    if attr is None:
        attr = Sdf.AttributeSpec(spec, name, value_type, variability, custom)
    if value is not None:
        attr.default = value
    return attr


def _prepend_apis(spec: Any, apis: list[str]) -> None:
    existing = spec.GetInfo("apiSchemas") if spec.HasInfo("apiSchemas") else Sdf.TokenListOp()
    items = list(existing.prependedItems)
    items.extend(api for api in apis if api not in items)
    op = Sdf.TokenListOp()
    op.prependedItems = items
    spec.SetInfo("apiSchemas", op)


def _bind(spec: Any, material_path: str) -> None:
    _prepend_apis(spec, ["MaterialBindingAPI"])
    rel = spec.relationships.get("material:binding") if hasattr(spec.relationships, "get") else None
    if rel is None:
        rel = Sdf.RelationshipSpec(spec, "material:binding", False)
    rel.targetPathList.explicitItems = [Sdf.Path(material_path)]


def _variant_set(spec: Any, info: dict[str, Any]) -> None:
    vset = Sdf.VariantSetSpec(spec, info["name"])
    for variant_name, material in info["variants"].items():
        variant = Sdf.VariantSpec(vset, variant_name)
        target = Sdf.PrimSpec(variant.primSpec, info["target"], Sdf.SpecifierOver)
        _bind(target, material)
    spec.variantSetNameList.prependedItems = [info["name"]]
    spec.variantSelections[info["name"]] = info["default"]


def _translate(spec: Any, position: tuple[float, float, float]) -> None:
    _attr(spec, "xformOp:translate", "double3", Gf.Vec3d(*position))
    _attr(spec, "xformOpOrder", "token[]", ["xformOp:translate"], uniform=True)


def _look_at(spec: Any, eye: tuple[float, float, float], target: tuple[float, float, float], settings: dict[str, Any],
             *, translate: bool = True) -> None:
    """Point the prim's -Z axis (where cameras and distant lights look) from eye towards target."""
    view = Gf.Matrix4d().SetLookAt(Gf.Vec3d(*eye), Gf.Vec3d(*target), Gf.Vec3d(*_up_vector(settings)))
    rotation = view.GetInverse().ExtractRotation()
    angles = rotation.Decompose(Gf.Vec3d.ZAxis(), Gf.Vec3d.YAxis(), Gf.Vec3d.XAxis())
    # Decompose(Z, Y, X) gives the angles in reverse order of xformOp:rotateXYZ (X applied first), as
    # UsdGeomXformCommonAPI does; rounding keeps the .usda readable (no 9.5e-15 noise).
    rotate_xyz = Gf.Vec3f(*(round(float(angles[index]), 4) + 0.0 for index in (2, 1, 0)))
    order = []
    if translate:
        _attr(spec, "xformOp:translate", "double3", Gf.Vec3d(*eye))
        order.append("xformOp:translate")
    _attr(spec, "xformOp:rotateXYZ", "float3", rotate_xyz)
    order.append("xformOp:rotateXYZ")
    _attr(spec, "xformOpOrder", "token[]", order, uniform=True)


def _build_ground(spec: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    half = float(prim.get("size_m", 10)) / 2.0
    corners = [_to_stage(corner, settings) for corner in ((-half, -half, 0), (half, -half, 0), (half, half, 0), (-half, half, 0))]
    low = tuple(min(point[axis] for point in corners) for axis in range(3))
    high = tuple(max(point[axis] for point in corners) for axis in range(3))
    _attr(spec, "points", "point3f[]", [Gf.Vec3f(*point) for point in corners])
    _attr(spec, "faceVertexCounts", "int[]", [4])
    _attr(spec, "faceVertexIndices", "int[]", [0, 1, 2, 3])
    _attr(spec, "extent", "float3[]", [Gf.Vec3f(*low), Gf.Vec3f(*high)])
    _attr(spec, "doubleSided", "bool", True, uniform=True)
    _attr(spec, "subdivisionScheme", "token", "none", uniform=True)


def _build_cube(spec: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    size = float(prim.get("size_m", 1.0)) / settings["meters_per_unit"]
    _attr(spec, "size", "double", size)
    _attr(spec, "extent", "float3[]", [Gf.Vec3f(-size / 2, -size / 2, -size / 2), Gf.Vec3f(size / 2, size / 2, size / 2)])


def _build_camera(spec: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    _look_at(spec, _to_stage(prim["eye_m"], settings), _to_stage(prim["target_m"], settings), settings)
    _attr(spec, "focalLength", "float", float(prim.get("focal_length", 18.0)))
    scale = 1.0 / settings["meters_per_unit"]
    _attr(spec, "clippingRange", "float2", Gf.Vec2f(0.01 * scale, 10000.0 * scale))  # 1 cm to 10 km in stage units


def _build_dome_light(spec: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    _attr(spec, "inputs:intensity", "float", float(prim.get("intensity", 1000)))


def _build_distant_light(spec: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    _attr(spec, "inputs:intensity", "float", float(prim.get("intensity", 3000)))
    _attr(spec, "inputs:angle", "float", float(prim.get("angle", 1.0)))
    # Rotation only: aim the light from the origin along the template direction (scale does not matter here).
    direction = _to_stage(prim.get("direction", (-0.4, 0.5, -0.77)), {**settings, "meters_per_unit": 1.0})
    _look_at(spec, (0.0, 0.0, 0.0), direction, settings, translate=False)


def _build_material(spec: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    shader = Sdf.PrimSpec(spec, _SHADER_NAME, Sdf.SpecifierDef, "Shader")
    _attr(shader, "info:id", "token", "UsdPreviewSurface", uniform=True)
    _attr(shader, "inputs:diffuseColor", "color3f", Gf.Vec3f(*prim.get("color", (0.5, 0.5, 0.5))))
    _attr(shader, "inputs:roughness", "float", float(prim.get("roughness", 0.5)))
    _attr(shader, "inputs:metallic", "float", float(prim.get("metallic", 0.0)))
    _attr(shader, "outputs:surface", "token", None)
    surface = _attr(spec, "outputs:surface", "token", None)
    surface.connectionPathList.explicitItems = [shader.path.AppendProperty("outputs:surface")]


def _build_physics_scene(spec: Any, prim: dict[str, Any], settings: dict[str, Any]) -> None:
    down = tuple(-value + 0.0 for value in _up_vector(settings))
    _attr(spec, "physics:gravityDirection", "vector3f", Gf.Vec3f(*down))
    _attr(spec, "physics:gravityMagnitude", "float", 9.81 / settings["meters_per_unit"])  # stage units per s^2


_BUILDERS = {
    "Mesh": _build_ground,
    "Cube": _build_cube,
    "Camera": _build_camera,
    "DomeLight": _build_dome_light,
    "DistantLight": _build_distant_light,
    "Material": _build_material,
    "PhysicsScene": _build_physics_scene,
}


# --------------------------------------------------------------------------------------------------- checking


def _verify(plan: dict[str, Any]) -> dict[str, Any]:
    """Open the new root.usda with Usd.Stage.Open and check it composes as the template intended (read-only)."""
    spec, settings = plan["spec"], plan["settings"]
    root_file = plan["project_dir"] / ROOT_LAYER
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    stage = Usd.Stage.Open(str(root_file), Usd.Stage.LoadAll)
    if stage is None:
        return {"ok": False, "checks": [{"check": "opens", "ok": False, "detail": f"Usd.Stage.Open failed for {root_file}"}]}
    check("opens", True, f"Usd.Stage.Open opened {root_file.name}")
    default_prim = stage.GetDefaultPrim()
    expected_default = "/" + settings["default_prim"]
    check("default_prim", bool(default_prim) and str(default_prim.GetPath()) == expected_default,
          f"defaultPrim is {default_prim.GetPath() if default_prim else 'missing'} (expected {expected_default})")
    up_axis = str(UsdGeom.GetStageUpAxis(stage))
    check("up_axis", up_axis == settings["up_axis"], f"upAxis is {up_axis} (expected {settings['up_axis']})")
    meters = UsdGeom.GetStageMetersPerUnit(stage)
    check("meters_per_unit", math.isclose(meters, settings["meters_per_unit"]),
          f"metersPerUnit is {meters:g} (expected {settings['meters_per_unit']:g})")
    if settings.get("kilograms_per_unit") is not None:
        kilograms = stage.GetRootLayer().pseudoRoot.GetInfo("kilogramsPerUnit")
        check("kilograms_per_unit", math.isclose(float(kilograms or 0.0), settings["kilograms_per_unit"]),
              f"kilogramsPerUnit is {kilograms} (expected {settings['kilograms_per_unit']:g})")
    names = [Path(layer.realPath).name for layer in stage.GetLayerStack(False)]
    expected_names = [ROOT_LAYER] + [f"{layer['name']}.usda" for layer in spec["layers"]]
    check("layer_stack", names == expected_names, f"layer stack (strongest first): {', '.join(names)}")
    errors = [str(error) for error in stage.GetCompositionErrors()] if hasattr(stage, "GetCompositionErrors") else []
    check("composition_errors", not errors, "no composition errors" if not errors else "; ".join(errors[:5]))
    missing = sorted({prim["path"] for prim in spec["prims"] if not stage.GetPrimAtPath(prim["path"]).IsDefined()})
    check("starter_prims", not missing, "every starter prim is defined" if not missing else f"missing: {', '.join(missing)}")
    for prim in spec["prims"]:
        vset = prim.get("variant_set")
        if not vset:
            continue
        target = stage.GetPrimAtPath(f"{prim['path']}/{vset['target']}")
        bound = UsdShade.MaterialBindingAPI(target).ComputeBoundMaterial()[0] if target else None
        expected = vset["variants"][vset["default"]]
        actual = str(bound.GetPath()) if bound else "nothing"
        check("variant_binding", actual == expected,
              f"variant {vset['name']}={vset['default']} binds {target.GetPath() if target else '?'} to {actual} (expected {expected})")
    summary: dict[str, Any] = {}
    try:
        from . import usd_validate

        report = usd_validate.validate(stage, "exam")
        summary = {"counts": report["counts"], "codes": sorted({issue["code"] for issue in report["issues"]})}
        check("usd_validate", report["ok"], f"exam lint: {report['counts']['error']} error(s), {report['counts']['warning']} warning(s)")
    except Exception as exc:  # the lint is a bonus; composition checks above are what matter
        summary = {"skipped": f"usd_validate did not run: {exc}"}
    return {
        "ok": all(item["ok"] for item in checks),
        "usd_version": ".".join(str(part) for part in Usd.GetVersion()),
        "checks": checks,
        "lint": summary,
    }


# --------------------------------------------------------------------------------------------------- listing


def _describe_project(folder: Path, context: Path) -> dict[str, Any]:
    try:
        with open(context, encoding="utf-8", errors="replace") as handle:
            text = handle.read(_CONTEXT_READ_LIMIT)
    except OSError as exc:
        return {"name": folder.name, "path": str(folder), "error": f"Could not read PROJECT_CONTEXT.md: {exc}"}
    scene_entry = _bullet(text, "Main scene")
    template = _bullet(text, "Template")
    times = [_mtime(context)]
    entry_path = (folder / scene_entry) if scene_entry and not os.path.isabs(scene_entry) else None
    if entry_path is not None and _is_within(entry_path.resolve(), folder.resolve()):
        times.append(_mtime(entry_path))
    layers_dir = folder / "layers"
    try:
        if layers_dir.is_dir():
            for count, child in enumerate(layers_dir.iterdir()):
                if count >= 200:
                    break
                times.append(_mtime(child))
    except OSError:
        pass
    latest = max((value for value in times if value is not None), default=None)
    return {
        "name": folder.name,
        "path": str(folder),
        "goal": _section(text, "Goal"),
        "scene_entry": scene_entry or "",
        "scene_entry_exists": bool(entry_path is not None and entry_path.is_file()),
        "template": template or "",
        "last_modified": datetime.fromtimestamp(latest, timezone.utc).isoformat(timespec="seconds") if latest else "",
    }


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", match.group(1)) if part.strip()]
    goal = " ".join(paragraphs[0].split()) if paragraphs else ""
    return goal[:MAX_GOAL_LENGTH]


def _bullet(text: str, label: str) -> str:
    """The value of a '- Label: value' line; a leading `code` span wins (so '`blank` (Blank project)' gives 'blank')."""
    match = re.search(rf"^\s*[-*]\s*{re.escape(label)}\s*:[ \t]*(.*?)\s*$", text, re.MULTILINE)
    if not match:
        return ""
    value = match.group(1)
    code = re.match(r"`([^`]+)`", value)
    return (code.group(1) if code else value).strip()


# --------------------------------------------------------------------------------------------------- text files


def _build_texts(plan: dict[str, Any]) -> dict[str, str]:
    return {
        "PROJECT_CONTEXT.md": _project_context(plan),
        "README.md": _readme(plan),
        ".gitignore": _GITIGNORE,
        "docs/layer_stack.md": _layer_doc(plan),
        "assets/README.md": _ASSETS_README,
        "scenes/README.md": _SCENES_README,
        "renders/.gitkeep": "",
        "scripts/inspect_scene.py": _INSPECT_SCRIPT,
    }


def _today() -> str:
    return datetime.now().date().isoformat()


def _layer_lines(spec: dict[str, Any]) -> list[str]:
    lines = [f"1. `{ROOT_LAYER}` (strongest): stage metadata and the sublayer list only. No prims."]
    for number, layer in enumerate(spec["layers"], start=2):
        lines.append(f"{number}. `{_layer_file(layer['name'])}`: {layer['owns']}")
    return lines


def _project_context(plan: dict[str, Any]) -> str:
    spec, settings = plan["spec"], plan["settings"]
    prim_rows = []
    seen: dict[str, dict[str, Any]] = {}
    for prim in spec["prims"]:
        row = seen.setdefault(prim["path"], {"type": "", "layers": [], "doc": ""})
        row["type"] = row["type"] or prim.get("type", "")
        row["layers"].append(prim["layer"])
        row["doc"] = row["doc"] or prim.get("doc", "")
    for path, row in seen.items():
        layers = ", ".join(f"{name}.usda" for name in dict.fromkeys(row["layers"]))
        prim_rows.append(f"| `{path}` | {row['type'] or 'over'} | {layers} | {row['doc']} |")
    materials = [prim for prim in spec["prims"] if prim.get("type") == "Material"]
    bindings = [prim for prim in spec["prims"] if prim.get("bind")]
    variant_sets = [prim for prim in spec["prims"] if prim.get("variant_set")]
    material_lines = [f"- Material `{prim['path']}` (UsdPreviewSurface) in `{_layer_file(prim['layer'])}`" for prim in materials]
    material_lines += [f"- `{prim['path']}` binds `{prim['bind']}` in `{_layer_file(prim['layer'])}`" for prim in bindings]
    for prim in variant_sets:
        vset = prim["variant_set"]
        for variant, material in vset["variants"].items():
            material_lines.append(
                f"- Variant `{vset['name']}={variant}` on `{prim['path']}` binds `{prim['path']}/{vset['target']}` to `{material}`"
                + (" (default)" if variant == vset["default"] else "")
            )
    kilograms = settings.get("kilograms_per_unit")
    today = _today()
    layer_order = ", ".join(f"{layer['name']}.usda" for layer in spec["layers"])
    lines = [
        f"# Project Context: {plan['name']}",
        "",
        "## Goal",
        "",
        plan["goal"],
        "",
        "## Independence",
        "",
        "This project reuses the shared infrastructure of the Omniverse workspace (MCP router, Composer build, "
        "launchers, OpenUSD lessons), but it is separate from other demos such as `mini_omk_demo`.",
        "",
        "## Entry points",
        "",
        f"- Project folder: `{plan['name']}/`",
        f"- Main scene: `{ROOT_LAYER}`",
        f"- Template: `{plan['template']}` ({spec['title']}, {spec['app']})",
        "- Project configuration/DataTable: none yet",
        "- Custom extension/scripts: `scripts/inspect_scene.py` (read-only check)",
        "",
        "## Assets and licensing",
        "",
        "| Asset | Source | License | Local path | Notes |",
        "|---|---|---|---|---|",
        "| (none yet) | | | `assets/` | Record every asset you add here. |",
        "",
        "## Stage map",
        "",
        "| Prim | Type | Authored in | Purpose |",
        "|---|---|---|---|",
        *prim_rows,
        "",
        "## Layer stack",
        "",
        "Strongest to weakest:",
        "",
        *_layer_lines(spec),
        "",
        spec["layer_order_why"],
        "",
        "## Axes, units, and pivots",
        "",
        f"- Up axis: {settings['up_axis']}",
        "- Forward axis: not decided yet; inspect each imported asset instead of assuming one.",
        f"- Meters per unit: {settings['meters_per_unit']:g} (1 unit = 1 {_UNIT_WORDS[settings['units']]})",
        *([f"- Kilograms per unit: {kilograms:g}"] if kilograms is not None else []),
        "- Important pivots/joints: none yet.",
        "",
        "## Materials and bindings",
        "",
        *(material_lines or ["None yet."]),
        "",
        "## Animation",
        "",
        "None yet. timeCodesPerSecond is not authored (USD's default is 24).",
        "",
        "## UI and MCP behavior",
        "",
        *[f"- {control}" for control in spec["controls"]],
        "- Safe MCP use: inspect read-only first (scene_summary, scene_inspect), then use dry_run=True before any mutation.",
        "",
        "## Validation checklist",
        "",
        "- [ ] Scene opens after restart",
        "- [ ] Referenced assets resolve",
        "- [ ] Texture paths resolve",
        "- [ ] Expected variants exist",
        "- [ ] Axes/pivots verified",
        "- [ ] Animation ranges verified",
        "- [ ] Router inspection works",
        "- [ ] Project-only facts documented here",
        "- [ ] Reusable lesson copied to shared workspace context",
        "",
        "## Decisions and lessons",
        "",
        f"- {today}: Created from the `{plan['template']}` template by the project starter. {_units_sentence(settings)}",
        f"- {today}: Layer order (strongest first) is {layer_order}; see `docs/layer_stack.md`.",
        *[f"- {lesson}" for lesson in spec["lessons"]],
        "",
        "## Current status and next steps",
        "",
        "Complete: folder layout, root layer with stage metadata, the layer stack and the starter prims. "
        "Not done yet: real assets, project controls and everything in the validation checklist.",
        "",
        *[f"{number}. {step}" for number, step in enumerate(spec["next_steps"], start=1)],
        "",
    ]
    return "\n".join(lines)


def _readme(plan: dict[str, Any]) -> str:
    spec, settings = plan["spec"], plan["settings"]
    app = "Isaac Sim" if spec["app"].startswith("Isaac") else "USD Composer"
    folders = [f"| `{folder}/` | {_FOLDER_PURPOSE[folder]} |" for folder in FOLDERS]
    lines = [
        f"# {plan['name']}",
        "",
        plan["goal"],
        "",
        f"Template: **{spec['title']}** ({spec['app']}). {spec['description']}",
        "",
        "## Open the scene",
        "",
        f"- {app}: **File > Open** and choose `{ROOT_LAYER}` in this folder.",
        "- Python (read-only check): `python scripts/inspect_scene.py` with a Python that has USD (`pxr`).",
        "",
        "## Units and axes",
        "",
        _units_sentence(settings),
        "",
        "## Layer stack (strongest first)",
        "",
        *_layer_lines(spec),
        "",
        "Why this order: see `docs/layer_stack.md`.",
        "",
        "## Folders",
        "",
        "| Folder | What goes in it |",
        "|---|---|",
        *folders,
        "",
        "## First steps",
        "",
        f"1. Open `{ROOT_LAYER}` in {app}.",
        "2. Open the Layer window (the Layer tab next to Stage) and look at the sublayers under root.usda.",
        "3. Right-click `layers/layout.usda` > **Set Authoring Layer** before you edit, so edits do not land in root.usda.",
        *[f"{number}. {step}" for number, step in enumerate(spec["next_steps"], start=4)],
        f"{4 + len(spec['next_steps'])}. Keep `PROJECT_CONTEXT.md` up to date: assets, decisions, lessons, next steps.",
        "",
    ]
    return "\n".join(lines)


def _layer_doc(plan: dict[str, Any]) -> str:
    stack = _layer_stack(plan["spec"])
    lines = [
        f"# Layer stack of {plan['name']}",
        "",
        "`root.usda` sublayers the files below. In a layer stack, an opinion in a stronger layer wins over the same "
        "opinion in a weaker one; the weaker opinion is not deleted, it is just not used. When two layers author "
        "different things (one defines a prim, another adds a material binding), both apply.",
        "",
        "| Strength | File | Owns | Why here |",
        "|---|---|---|---|",
        *[f"| {layer['strength']} | `{layer['file']}` | {layer['owns']} | {layer['why']} |" for layer in stack["layers"]],
        "",
        "## Why this order",
        "",
        stack["explanation"],
        "",
        "## Tips",
        "",
        "- Composer and Isaac Sim save edits into the current authoring layer (root.usda unless you change it). "
        "Right-click a layer in the Layer window > Set Authoring Layer to choose where edits go.",
        "- Paths inside a layer are relative to that layer's own file: from `layers/layout.usda`, an asset in "
        "`assets/` is `../assets/<file>.usd`.",
        "- Muting a layer in the Layer window hides its opinions without deleting them, a quick way to see what it contributes.",
        "",
    ]
    return "\n".join(lines)


_GITIGNORE = """# Generated output stays out of git: renders, Replicator/SDG output, thumbnails and caches.
renders/*
!renders/.gitkeep
_out*/
.thumbs/
__pycache__/
*.pyc
*.log
.DS_Store
Thumbs.db
"""

_ASSETS_README = """# assets/

Put source assets here: USD files, textures, CAD conversions.

- Record each asset's source and license in `../PROJECT_CONTEXT.md` (Assets and licensing).
- Reference assets with relative paths. A path is relative to the layer that authors it, so from
  `layers/layout.usda` an asset here is `../assets/<file>.usd`.
- Check an asset's upAxis and metersPerUnit before referencing it: USD does not convert them for you.
"""

_SCENES_README = """# scenes/

The main scene is `../root.usda`. Put extra entry scenes here, for example a render shot or a test variation
that sublayers `../root.usda` and adds its own overrides on top.
"""

_INSPECT_SCRIPT = '''"""Print this project's units, layer stack and prims. Read-only: nothing is changed or saved.

Run it in either of two ways:
* From a terminal with a Python that has USD (pxr):  python scripts/inspect_scene.py
* Inside USD Composer or Isaac Sim: Window > Script Editor, File > Open this file, then Run.
  There it inspects the stage that is open in the app.
"""

from pathlib import Path

from pxr import Sdf, Usd, UsdGeom


def open_stage():
    """Return the stage to inspect."""
    try:
        script = Path(__file__).resolve()
    except NameError:
        # The Script Editor runs code without __file__: use the stage open in the app instead.
        import omni.usd

        return omni.usd.get_context().get_stage()
    # This file lives in <project>/scripts/, so root.usda is one folder up.
    return Usd.Stage.Open(str(script.parent.parent / "root.usda"))


def layer_name(layer):
    """A short name for a layer: its file name, or its identifier for in-memory layers."""
    return Path(layer.realPath).name if layer.realPath else layer.identifier


def main():
    stage = open_stage()
    print("Scene:", layer_name(stage.GetRootLayer()))
    print("Up axis:", UsdGeom.GetStageUpAxis(stage))
    print("Meters per unit:", UsdGeom.GetStageMetersPerUnit(stage))
    print("Default prim:", stage.GetDefaultPrim().GetPath())

    print()
    print("Layer stack, strongest first:")
    for number, layer in enumerate(stage.GetLayerStack(includeSessionLayers=False), start=1):
        print(f"  {number}. {layer_name(layer)}")

    print()
    print("Prims and the layer that defines each one ('def'):")
    for prim in stage.Traverse():
        # GetPrimStack lists every layer with an opinion about this prim, strongest first.
        defining = [layer_name(spec.layer) for spec in prim.GetPrimStack() if spec.specifier == Sdf.SpecifierDef]
        where = defining[0] if defining else "?"
        print(f"  {prim.GetPath()}  ({prim.GetTypeName() or 'no type'})  defined in {where}")


main()
'''
