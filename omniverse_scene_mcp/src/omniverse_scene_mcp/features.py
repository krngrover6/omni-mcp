"""Allowlisted adapters for the handoff modules; no Kit or USD imports at module import.

Each entry names one reviewed function, never a caller-supplied module. The same
adapter is used by MCP and Kit RPC so dry-run coercion cannot diverge.
"""
from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Feature:
    module: str
    function: str
    location: str = "stage"  # stage = pure pxr, kit = main-thread runtime, local = router-only
    mutates: bool = False
    group: str = "Live insight"


FEATURES: dict[str, Feature] = {}


def _add(module: str, entries: str, location: str, group: str, writes: str = "") -> None:
    for item in entries.split():
        name, _, function = item.partition(":")
        FEATURES[name] = Feature(module, function or name, location, name in writes.split(), group)


_add("omnigraph_helper", "graph_inspect:inspect_graphs", "stage", "Live insight")
_add("omnigraph_helper", "graph_node_type:describe_node_type graph_recipes:list_recipes", "local", "Knowledge & docs")
_add("omnigraph_helper", "graph_plan:plan_recipe", "local", "Knowledge & docs")
_add("omnigraph_helper", "graph_apply:apply_recipe", "stage", "Scene: change safely", "graph_apply")
_add("graph_builder", "graph_node_search:node_search", "local", "Knowledge & docs")
_add("graph_builder", "graph_create:create_graph graph_add_node:add_node graph_connect:connect "
     "graph_disconnect:disconnect graph_set_value:set_value graph_add_variable:add_variable "
     "graph_delete_node:delete_node", "stage", "Scene: change safely",
     "graph_create graph_add_node graph_connect graph_disconnect graph_set_value graph_add_variable graph_delete_node")
_add("message_bus", "event_plan:plan_event event_names:event_name_variants", "local", "Knowledge & docs")
_add("message_bus", "event_send:send_event event_listen:start_listener event_stop:stop_listener event_status:listener_status",
     "kit", "Simulation & screenshots", "event_send event_listen event_stop")
_add("message_bus", "event_check_graph:check_graph_listener", "stage", "Live insight")
_add("variant_manager", "variant_list:list_variants variant_contents variant_compare:compare_variants variant_check:check_variants",
     "stage", "Layers")
_add("variant_manager", "variant_duplicate:duplicate_variant variant_rename:rename_variant variant_remove:remove_variant "
     "variant_create_set:create_variant_set variant_select:set_selection", "stage", "Layers",
     "variant_duplicate variant_rename variant_remove variant_create_set variant_select")
_add("import_helper", "asset_import_formats:supported_formats asset_import_plan:plan_import asset_detect_units:detect_units "
     "asset_wrap_units:wrap_units", "local", "Assets & physics", "asset_wrap_units")
_add("import_helper", "asset_import:import_asset asset_import_status:import_status", "kit", "Assets & physics", "asset_import")
_add("perf_advisor", "scene_performance:perf_report scene_payload_plan:convert_to_payload", "stage", "Live insight")
_add("perf_advisor", "scene_make_instanceable:make_instanceable", "stage", "Scene: change safely", "scene_make_instanceable")
_add("perf_advisor", "scene_frame_stats:live_frame_stats", "kit", "Live insight")
_add("animation", "animation_info:timeline_info animation_sample:sample animation_conveyor_plan:conveyor_plan", "stage", "Live insight")
_add("animation", "animation_timeline:set_timeline animation_keyframes:add_keyframes animation_turntable:turntable "
     "animation_move_along:move_along animation_swing:swing animation_clear:clear_animation", "stage", "Scene: change safely",
     "animation_timeline animation_keyframes animation_turntable animation_move_along animation_swing animation_clear")
_add("robot_import", "robot_urdf_summary:parse_urdf_summary robot_catalog:list_ready_robots robot_importer_info:importer_info "
     "robot_import_plan:plan_robot_import", "local", "Robots")
_add("robot_import", "robot_import:import_robot", "kit", "Robots", "robot_import")
_add("robot_import", "robot_add_ready:add_ready_robot", "stage", "Robots", "robot_add_ready")
_add("joint_control", "robot_joint_table:joint_table", "stage", "Robots")
_add("joint_control", "robot_drive_targets:set_drive_targets robot_drive_gains:set_drive_gains robot_apply_pose:apply_pose",
     "stage", "Robots", "robot_drive_targets robot_drive_gains robot_apply_pose")
_add("joint_control", "robot_save_pose:save_pose", "stage", "Robots", "robot_save_pose")
_add("joint_control", "robot_poses:list_poses robot_pose_targets:pose_targets", "local", "Robots")
_add("joint_control", "robot_joint_state:live_joint_state robot_live_move:live_move", "kit", "Robots", "robot_live_move")
_add("sensors", "sensor_catalog sensor_plan:plan_sensor", "local", "Robots")
_add("sensors", "sensor_list:list_sensors", "stage", "Robots")
_add("sensors", "sensor_add_camera:add_camera_sensor sensor_add_physics:add_physics_sensor", "stage", "Robots",
     "sensor_add_camera sensor_add_physics")
_add("sensors", "sensor_create:create_sensor_live sensor_read:read_sensor_live sensor_release:release_sensor_live", "kit", "Robots",
     "sensor_create sensor_release")
_add("pick_place", "robot_pick_place_lesson:lesson_plan robot_pick_place_script:generate_script", "local", "Robots")
_add("pick_place", "robot_pick_place_check:readiness_check", "stage", "Robots")
_add("ros2_check", "ros2_info ros2_environment:environment_check ros2_setup_plan:setup_plan", "local", "Robots")
_add("ros2_check", "ros2_graph_check:graph_check", "stage", "Robots")
_add("dataset", "dataset_check:check_dataset", "local", "Asset health")
_add("sdg", "sdg_plan:plan_sdg sdg_script", "local", "Simulation & screenshots")
_add("sdg", "sdg_start:start_sdg sdg_status sdg_cancel", "kit", "Simulation & screenshots", "sdg_start sdg_cancel")
_add("render_helper", "render_plan", "local", "Simulation & screenshots")
_add("render_helper", "render_capture", "kit", "Simulation & screenshots", "render_capture")
_add("render_helper", "render_sequence_plan:plan_image_sequence render_compare:compare_screenshots", "local", "Simulation & screenshots")
_add("render_helper", "render_turntable_plan:plan_turntable_render", "stage", "Simulation & screenshots")
_add("render_helper", "render_sequence render_job_status render_cancel", "kit", "Simulation & screenshots", "render_sequence render_cancel")
_add("blender_handoff", "blender_export_plan", "local", "Assets & physics")
_add("blender_handoff", "blender_handoff_check", "stage", "Asset health")
_add("extension_starter", "extension_create", "local", "Projects", "extension_create")
_add("rubric", "rubric_template", "local", "Learning Python & homework")
_add("rubric", "rubric_check:check_rubric", "stage", "Learning Python & homework")
_add("semantic_labels", "semantic_audit", "stage", "Live insight")
_add("semantic_labels", "semantic_labels:add_semantic_labels", "stage", "Scene: change safely", "semantic_labels")

_add("joints", "scene_create_joint:create_joint scene_add_drive:add_drive scene_set_joint_limits:set_joint_limits "
     "scene_add_articulation_root:add_articulation_root scene_create_gear_joint:create_gear_joint "
     "scene_create_rack_and_pinion_joint:create_rack_and_pinion_joint scene_set_joint_properties:set_joint_properties "
     "scene_set_distance_spring:set_distance_spring", "stage", "Robots",
     "scene_create_joint scene_add_drive scene_set_joint_limits scene_add_articulation_root scene_create_gear_joint "
     "scene_create_rack_and_pinion_joint scene_set_joint_properties scene_set_distance_spring")
_add("physics_extras", "scene_list_payloads:list_payloads", "stage", "Assets & physics")
_add("physics_extras", "scene_add_payload:add_payload scene_load_payloads:load_payloads "
     "scene_unload_payloads:unload_payloads scene_create_physics_material:create_physics_material "
     "scene_bind_physics_material:bind_physics_material scene_set_rigid_body_properties:set_rigid_body_properties",
     "stage", "Assets & physics",
     "scene_add_payload scene_load_payloads scene_unload_payloads scene_create_physics_material "
     "scene_bind_physics_material scene_set_rigid_body_properties")
_add("homework", "homework_check:check_homework", "stage", "Learning Python & homework")
_add("homework", "homework_sessions", "local", "Learning Python & homework")
_add("value_story","scene_why_value:why_value scene_what_if:what_if", "stage", "Live insight")
_add("session_lessons", "class_transcripts:find_transcripts class_session_plan:session_plan class_lesson:lesson "
     "class_check_claims:check_claims", "local", "Learning Python & homework")

# search_dirs stays internal so MCP callers cannot point a reader at arbitrary folders.
_INTERNAL = {"stage", "undo_stack", "deadline", "runner", "which", "env", "wait", "search_dirs"}


def function(name: str) -> Any:
    spec = FEATURES[name]
    return getattr(importlib.import_module("." + spec.module, __package__), spec.function)


def signature(name: str) -> inspect.Signature:
    """JSON-friendly public signature; no injected callbacks, stage objects or code execution."""
    fn = function(name)
    parameters = []
    for param in inspect.signature(fn).parameters.values():
        if param.name in _INTERNAL or param.kind == param.VAR_POSITIONAL:
            continue
        if param.kind == param.VAR_KEYWORD:
            parameters.append(inspect.Parameter("options", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=dict[str, Any] | None))
            continue
        # Preserve real validation in the function. In particular bool coercion by
        # Pydantic must not turn a malformed "false" into permission to write.
        default = True if param.name == "dry_run" else param.default
        parameters.append(param.replace(kind=inspect.Parameter.KEYWORD_ONLY, annotation=Any, default=default))
    if FEATURES[name].mutates and not any(p.name == "dry_run" for p in parameters):
        parameters.append(inspect.Parameter("dry_run", inspect.Parameter.KEYWORD_ONLY, default=True, annotation=Any))
    return inspect.Signature(parameters, return_annotation=dict)


def invoke(name: str, arguments: dict[str, Any], *, stage: Any = None, allow_kit: bool = False,
           undo_stack: Any = None) -> dict:
    """Call only an allowlisted function. Only literal False can authorize a write."""
    if name not in FEATURES:
        return {"ok": False, "error": f"Unknown feature: {name}"}
    spec = FEATURES[name]
    try:
        json.dumps(arguments, allow_nan=False)
        bound = signature(name).bind(**arguments)
        bound.apply_defaults()
        values = dict(bound.arguments)
        dry = values.pop("dry_run", True) is not False
        options = values.pop("options", None) or {}
        if not isinstance(options, dict) or any(k in _INTERNAL or k in values or k == "dry_run" for k in options):
            raise ValueError("options cannot override reserved or named parameters")
        fn = function(name)
        raw = inspect.signature(fn).parameters
        if spec.mutates and "dry_run" not in raw and dry:
            return {"ok": True, "dry_run": True, "written": False, "tool": name, "plan": values}
        values.update(options)
        if "dry_run" in raw:
            values["dry_run"] = dry
        if spec.location == "kit" and not allow_kit:
            return {"ok": False, "unsupported": True, "needs_bridge_update": True,
                    "message": "This operation requires the updated live Kit bridge.", "written": False}
        if spec.location == "stage":
            if stage is None:
                return {"ok": False, "error": "A USD stage is required", "written": False}
            values["stage"] = stage
        if "undo_stack" in raw and not dry:
            values["undo_stack"] = undo_stack
        result = fn(**values)
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    except (ValueError, TypeError, KeyError, ImportError, OSError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc), "written": False, "tool": name}


def rpc_call(bridge: Any, params: dict) -> dict:
    """Kit main-thread handler. Router-local operations cannot be invoked through RPC."""
    name = params.get("tool")
    arguments = params.get("arguments", {})
    if not isinstance(name, str) or name not in FEATURES or FEATURES[name].location == "local" or not isinstance(arguments, dict):
        return {"ok": False, "error": "Unknown live feature or malformed arguments", "written": False}
    stage = bridge._require_stage() if FEATURES[name].location == "stage" else None
    return invoke(name, arguments, stage=stage, allow_kit=True,
                  undo_stack=getattr(bridge, "_undo_stack", None))


def register(app: Any, router: Any, file_mode: Any) -> dict[str, Any]:
    """Register typed tools while sharing the allowlist and validation with RPC."""
    tools = {}
    def make_call(name, spec):
        def call(**arguments):
            scene_file = arguments.pop("scene_file", None)
            if spec.location == "local":
                return invoke(name, arguments)
            if scene_file is not None:
                if spec.mutates or spec.location != "stage":
                    return {"ok": False, "error": "Saved-file mode is read-only", "written": False}
                return file_mode(router, scene_file, lambda stage: invoke(name, arguments, stage=stage))
            bridge = router._scene._bridge
            rpc = getattr(bridge, "_rpc", None)
            if callable(rpc):
                try:
                    return rpc("feature_call", {"tool": name, "arguments": arguments})
                except ValueError as exc:
                    return {"ok": False, "error": str(exc), "needs_bridge_update": True, "written": False}
            return {"ok": False, "unsupported": True, "needs_bridge_update": True,
                    "message": "This operation needs the updated live bridge or an explicit scene_file for reading.", "written": False}

        return call

    for name, spec in FEATURES.items():
        call = make_call(name, spec)

        parameters = list(signature(name).parameters.values())
        if spec.location == "stage" and not spec.mutates:
            parameters.append(inspect.Parameter("scene_file", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=str | None))
        call.__name__ = name
        call.__doc__ = (inspect.getdoc(function(name)) or name).split("\n\n")[0] + "\nOnly dry_run=False permits an edit; no automatic saving."
        call.__signature__ = inspect.Signature(parameters, return_annotation=dict)
        call.__annotations__ = {p.name: p.annotation for p in parameters} | {"return": dict}
        app.tool(name=name)(call)
        tools[name] = call
    return tools
