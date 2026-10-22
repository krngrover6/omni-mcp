"""Link router actions to "show me the Python": which python_teacher snippet matches a tool call or a sentence.

The editing tools take ``show_python=True``; they then attach ``python_for(...)`` for the same task with the same
values, so a beginner sees the OpenUSD code that does what the tool just did (or would do). Pure mapping: nothing
here reads the scene or runs code, and a mapping problem never fails the edit (the attachment then says why).
"""

from __future__ import annotations

from typing import Any, Callable

from .python_teacher import LIGHT_TYPES, VALUE_TYPES, python_for

NOT_AVAILABLE = "No Python snippet matches this action yet; python_topics() lists the ones that exist."


def _vec(value: Any) -> tuple[float, ...] | None:
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return None
    return None


def _color(value: Any) -> tuple[float, ...] | None:
    """[r, g, b], or a '#RRGGBB' / '#RGB' string the tools also accept, as 0-1 floats."""
    if isinstance(value, str):
        if not value.strip().startswith("#"):
            return None
        text = value.strip()[1:]
        if len(text) == 3:
            text = "".join(ch * 2 for ch in text)
        if len(text) == 6:
            try:
                return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
            except ValueError:
                return None
        return None
    return _vec(value)


def _clean(args: dict[str, Any]) -> dict[str, Any]:
    """Drop None values, so python_for uses its own defaults for anything the caller left out."""
    return {key: value for key, value in args.items() if value is not None}


def _transform(path: Any, translation: Any, rotation: Any, scale: Any) -> tuple[str, dict[str, Any], str | None]:
    moved, turned, sized = _vec(translation), _vec(rotation), _vec(scale)
    given = [item for item in (moved, turned, sized) if item is not None]
    if len(given) > 1:
        note = ("This changes several transform parts at once, so the snippet sets translate, rotate and scale "
                "together; a part you did not give is written as its neutral value (0 0 0 or 1 1 1).")
        return "set_transform_isaac", {
            "prim_path": path,
            "translate": moved or (0.0, 0.0, 0.0),
            "rotate": turned or (0.0, 0.0, 0.0),
            "scale": sized or (1.0, 1.0, 1.0),
        }, note
    if turned is not None:
        return "rotate", {"prim_path": path, "rotate": turned}, None
    if sized is not None:
        return "scale", {"prim_path": path, "scale": sized}, None
    return "move", _clean({"prim_path": path, "translate": moved}), None


def _value_type(name: Any, value: Any, create_type: Any) -> str | None:
    if isinstance(create_type, str) and create_type.strip() in VALUE_TYPES:
        return create_type.strip()
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if _vec(value) is not None:
        return "color3f" if "color" in str(name).lower() else "double3"
    return None


def _light_type(value: Any) -> str | None:
    text = str(value or "").strip().lower().removesuffix("light").strip()
    return text if text in LIGHT_TYPES else None


def _for_tool(tool: str, a: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None]:
    """(python_teacher action, its args, an optional note) for one MCP tool call; action None when there is none."""
    path = a.get("path")
    if tool == "scene_create":
        type_name = a.get("type_name") or "Xform"
        return "create_prim", _clean({"prim_path": path, "type_name": type_name}), None
    if tool == "scene_delete":
        return "delete", _clean({"prim_path": path}), None
    if tool == "scene_duplicate":
        return "duplicate", _clean({"source_path": a.get("source_path"), "target_path": a.get("target_path")}), None
    if tool == "scene_rename":
        return "rename", _clean({"prim_path": path, "new_name": a.get("new_name")}), None
    if tool == "scene_move":
        return _transform(path, a.get("translation"), a.get("rotation"), a.get("scale"))
    if tool in ("scene_material", "scene_bind_material"):
        return "bind_material", _clean({"prim_path": a.get("prim_path") or path, "material_path": a.get("material_path"),
                                        "strength": a.get("strength")}), None
    if tool == "scene_variant_set":
        return "select_variant", _clean({"prim_path": path, "set_name": a.get("variant_set"),
                                         "selection": a.get("selection")}), None
    if tool == "scene_set_attribute":
        name, value = a.get("name"), a.get("value")
        return "set_attribute", _clean({"prim_path": path, "attr_name": name,
                                        "value_type": _value_type(name, value, a.get("create_type")), "value": value}), None
    if tool == "scene_create_light":
        return "create_light", _clean({"light_type": _light_type(a.get("light_type")), "prim_path": path,
                                       "intensity": a.get("intensity"), "color": _color(a.get("color")),
                                       "translate": _vec(a.get("translation"))}), None
    if tool == "scene_create_camera":
        return "create_camera", _clean({"prim_path": path, "position": _vec(a.get("translation")),
                                        "target": _vec(a.get("aim_at")), "focal_length": a.get("focal_length")}), None
    if tool == "scene_aim":
        # Not the aimed prim's path: UsdGeom.Camera.Define on a light (or any other prim) would turn it into a camera.
        note = ("There is no separate aim snippet: this example makes a NEW demo camera at /World/MyCamera (it does "
                f"not touch {path}) and shows the look-at rotation; the same math turns a light, because both look "
                "down their -Z axis.")
        return "create_camera", _clean({"target": _vec(a.get("target"))}), note
    if tool == "scene_create_material":
        return "create_material", {**_clean({"material_path": path, "color": _color(a.get("diffuse_color")),
                                             "roughness": a.get("roughness"), "metallic": a.get("metallic")}),
                                   "bind_to": None}, None
    if tool == "scene_add_asset":
        return "add_reference", _clean({"prim_path": a.get("prim_path"), "asset_path": a.get("asset_path"),
                                        "ref_prim_path": a.get("asset_prim_path")}), None
    if tool == "scene_add_physics_scene":
        gravity = a.get("gravity")
        return "add_physics_scene", _clean({"prim_path": path,
                                            "gravity": gravity if isinstance(gravity, (int, float)) else None}), None
    if tool == "scene_make_rigid_body":
        return "make_rigid_body", _clean({"prim_path": a.get("prim_path"), "add_collider": a.get("add_collider"),
                                          "mass": a.get("mass")}), None
    if tool == "scene_make_collider":
        return "make_collider", _clean({"prim_path": a.get("prim_path"), "approximation": a.get("approximation")}), None
    if tool == "scene_save":
        return "save", {}, None
    return None, {}, None


def _for_parsed(parsed: dict[str, Any]) -> tuple[str | None, dict[str, Any], str | None]:
    """The snippet for one router-parsed scene command (router._parse_scene_command)."""
    action = parsed.get("action")
    path = parsed.get("path")
    if action == "create":
        return _for_tool("scene_create", {"path": path, "type_name": parsed.get("type_name")})
    if action == "move":
        if parsed.get("offset") is not None:
            note = ("You asked for a relative move: the router adds this offset to the current position, while the "
                    "snippet sets the translate value itself (add the offset to the old value first).")
            return "move", _clean({"prim_path": path, "translate": _vec(parsed.get("offset"))}), note
        scale = parsed.get("scale") or parsed.get("scale_factor")
        return _transform(path, parsed.get("translation"), parsed.get("rotation"), scale)
    if action in ("delete", "rename", "duplicate"):
        return _for_tool(f"scene_{action}", {**parsed, "path": path})
    if action == "material":
        return _for_tool("scene_material", parsed)
    if action == "paint":
        return "create_material", {**_clean({"color": _vec(parsed.get("color"))}), "bind_to": path}, None
    if action == "set_attribute":
        value = parsed.get("value")
        if isinstance(value, str):  # the router keeps sentence values as text; '5000' is meant as a number
            try:
                value = float(value)
            except ValueError:
                pass
        return _for_tool("scene_set_attribute", {"path": path, "name": parsed.get("attribute"), "value": value})
    if action == "create_light":
        return _for_tool("scene_create_light", {"path": path, "light_type": parsed.get("light_type"),
                                                "translation": parsed.get("translation")})
    if action == "create_camera":
        return _for_tool("scene_create_camera", {"path": path, "aim_at": parsed.get("aim_at"),
                                                 "translation": parsed.get("translation")})
    if action == "rigid_body":
        return _for_tool("scene_make_rigid_body", {"prim_path": path, "mass": parsed.get("mass")})
    if action == "collider":
        return _for_tool("scene_make_collider", {"prim_path": path, "approximation": parsed.get("approximation")})
    if action == "inspect":
        return "inspect_prim", _clean({"prim_path": path}), None
    if action == "children":
        return "list_children", _clean({"prim_path": path}), None
    if action == "save":
        return "save", {}, None
    return None, {}, None


def _attach(mapping: Callable[[], tuple[str | None, dict[str, Any], str | None]], context: str) -> dict[str, Any]:
    try:
        action, args, note = mapping()
        if action is None:
            return {"ok": False, "error": NOT_AVAILABLE}
        snippet = python_for(action, args, context=context)
    except Exception as exc:  # noqa: BLE001 - the snippet is a bonus; it must never fail the edit itself
        return {"ok": False, "error": f"Could not build the Python snippet: {type(exc).__name__}: {exc}"}
    if isinstance(snippet, dict) and note and snippet.get("ok"):
        snippet = {**snippet, "note": note}
    return snippet if isinstance(snippet, dict) else {"ok": False, "error": "python_for returned no snippet"}


def python_for_tool(tool: str, arguments: dict[str, Any], context: str = "script_editor") -> dict[str, Any]:
    """python_for(...) for an MCP tool call, e.g. ('scene_move', {'path': ..., 'translation': [0, 0, 1]}); never raises."""
    return _attach(lambda: _for_tool(tool, dict(arguments or {})), context)


def python_for_command(parsed: dict[str, Any] | None, context: str = "script_editor") -> dict[str, Any]:
    """python_for(...) for a router-parsed scene command; never raises."""
    if not isinstance(parsed, dict):
        return {"ok": False, "error": NOT_AVAILABLE}
    return _attach(lambda: _for_parsed(parsed), context)
