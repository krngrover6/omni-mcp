from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Iterable, Sequence

from mcp.server.fastmcp import FastMCP

from .router import RouteTarget
from .runtime import build_router


def _coerce_vector(values: Sequence[float] | None) -> tuple[float, float, float] | None:
    if values is None:
        return None
    return (float(values[0]), float(values[1]), float(values[2]))


def _decision_dict(result) -> dict:
    return {
        "target": result.decision.target.value,
        "confidence": result.decision.confidence,
        "reason": result.decision.reason,
        "normalized_request": result.decision.normalized_request,
    }


def _workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def _read_workspace_document(name: str) -> str:
    allowed = {
        "context": "OMNIVERSE_WORKSPACE_CONTEXT.md",
        "new_project_template": "NEW_PROJECT_CONTEXT_TEMPLATE.md",
        "mcp_cheatsheet": "USD_COMPOSER_MCP_CHEATSHEET.md",
    }
    filename = allowed.get(name)
    if filename is None:
        raise ValueError(f"Unknown workspace document: {name}")
    path = _workspace_root() / filename
    return path.read_text(encoding="utf-8")


ZAZ_ANIMATION_ACTIONS = {
    "closed_pose": {"mode": "pose", "frame": 160.0, "description": "Closed panels and centered steering"},
    "open_pose": {"mode": "clip", "start": 1240.0, "end": 1408.0, "description": "Open both doors and both hoods together, then hold four seconds"},
    "open_hold": {"mode": "pose", "frame": 1408.0, "description": "Hold both doors and both hoods open"},
    "open_doors": {"mode": "clip", "start": 1440.0, "end": 1608.0, "description": "Open both doors together, then hold four seconds"},
    "hold_doors": {"mode": "pose", "frame": 1608.0, "description": "Hold both doors open with both hoods closed"},
    "open_hoods": {"mode": "clip", "start": 1640.0, "end": 1808.0, "description": "Open front and rear hoods together, then hold four seconds"},
    "hold_hoods": {"mode": "pose", "frame": 1808.0, "description": "Hold both hoods open with both doors closed"},
    "steering_demo": {"mode": "clip", "start": 0.0, "end": 160.0, "description": "Front-wheel and steering-wheel motion"},
    # Backward-compatible alias retained for existing MCP prompts and clients.
    "open_everything": {"mode": "clip", "start": 160.0, "end": 480.0, "description": "Open doors, front trunk, and rear hood"},
    "close_everything": {"mode": "clip", "start": 688.0, "end": 960.0, "description": "Close trunk, hood, and both doors"},
    "wheel_spin": {"mode": "clip", "start": 960.0, "end": 1200.0, "description": "Spin all four road wheels"},
    "full_demo": {"mode": "clip", "start": 0.0, "end": 1808.0, "description": "Run the complete ZAZ animation demo"},
}


def build_server(host: str | None = None, port: int | None = None) -> FastMCP:
    router = build_router()
    app = FastMCP(
        name="omniverse-router-mcp",
        instructions="Single front door for Omniverse scene operations, workspace context, and routed knowledge lookups.",
        host=host or os.environ.get("MCP_HOST", "127.0.0.1"),
        port=port or int(os.environ.get("MCP_PORT", "9905")),
        streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
    )

    @app.tool(name="router_dispatch")
    async def router_dispatch(request: str, dry_run: bool = False) -> dict:
        """Route a natural-language or command-style request to the right Omniverse MCP backend."""
        result = await router.handle(request, dry_run=dry_run)
        return {"decision": _decision_dict(result), "payload": result.payload}

    @app.tool(name="router_route")
    def router_route(request: str) -> dict:
        """Inspect how the router would classify a request before executing it."""
        decision = router.route(request)
        return {
            "target": decision.target.value,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "normalized_request": decision.normalized_request,
        }

    @app.tool(name="router_status")
    def router_status() -> dict:
        """Summarize which Omniverse backends are configured."""
        return {
            "scene": router.scene_summary(),
            "backends": router.backend_status()["backends"],
            "targets": [target.value for target in RouteTarget],
        }

    @app.tool(name="workspace_context")
    def workspace_context() -> dict:
        """Return shared Omniverse setup and reusable lessons for any project in this workspace."""
        return {
            "scope": "shared_across_projects",
            "warning": "Do not assume the caller wants to continue the ZAZ demo.",
            "content": _read_workspace_document("context"),
        }

    @app.tool(name="workspace_document")
    def workspace_document(name: str = "new_project_template") -> dict:
        """Return an allowlisted shared document: new_project_template or mcp_cheatsheet."""
        return {"name": name, "content": _read_workspace_document(name)}

    @app.tool(name="workspace_learning_index")
    def workspace_learning_index() -> dict:
        """List reusable learning documents and explain which context belongs to an existing demo."""
        return {
            "shared_start": "OMNIVERSE_WORKSPACE_CONTEXT.md",
            "new_project_template": "NEW_PROJECT_CONTEXT_TEMPLATE.md",
            "learning": [
                "OPENUSD_FOUNDATIONS_ONE_BY_ONE.md",
                "OPENUSD_COMPOSITION_AND_PIPELINE_ONE_BY_ONE.md",
                "OPENUSD_BEYOND_BASICS_ONE_BY_ONE.md",
                "USD_COMPOSER_MCP_CHEATSHEET.md",
                "MINI_PROJECT_USD_CONCEPTS.md",
                "OMNIVERSE_MINI_DEMO_CONCEPTS.md",
            ],
            "existing_demo_only": ["RESUME_ON_NEW_PC.md", "mini_omk_demo/", "working_scene.usda"],
        }

    @app.tool(name="scene_summary")
    def scene_summary() -> dict:
        """Return a compact summary of the current scene."""
        return router.scene_summary()

    @app.tool(name="scene_select")
    def scene_select(paths: Sequence[str]) -> dict:
        """Select one or more prim paths in the active scene."""
        return router._scene.set_selection(list(paths))

    @app.tool(name="scene_inspect")
    def scene_inspect(path: str) -> dict:
        """Inspect a prim by path."""
        return router._scene.inspect_prim(path)

    @app.tool(name="scene_children")
    def scene_children(path: str) -> dict:
        """List the immediate children of a prim."""
        return router._scene.list_children(path)

    @app.tool(name="scene_create")
    def scene_create(path: str, type_name: str = "Xform", dry_run: bool = False) -> dict:
        """Create a prim, optionally in dry-run mode."""
        return router._wrap_operation_result("scene", router._scene.create_prim(path, type_name, dry_run=dry_run))

    @app.tool(name="scene_delete")
    def scene_delete(path: str, dry_run: bool = False) -> dict:
        """Delete a prim, optionally in dry-run mode."""
        return router._wrap_operation_result("scene", router._scene.delete_prim(path, dry_run=dry_run))

    @app.tool(name="scene_duplicate")
    def scene_duplicate(source_path: str, target_path: str, dry_run: bool = False) -> dict:
        """Duplicate one prim to another path."""
        return router._wrap_operation_result(
            "scene",
            router._scene.duplicate_prim(source_path, target_path, dry_run=dry_run),
        )

    @app.tool(name="scene_reparent")
    def scene_reparent(path: str, new_parent: str, dry_run: bool = False) -> dict:
        """Reparent a prim under a new parent."""
        return router._wrap_operation_result("scene", router._scene.reparent_prim(path, new_parent, dry_run=dry_run))

    @app.tool(name="scene_move")
    def scene_move(
        path: str,
        translation: Sequence[float] | None = None,
        rotation: Sequence[float] | None = None,
        scale: Sequence[float] | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Apply transform updates to a prim."""
        return router._wrap_operation_result(
            "scene",
            router._scene.move_prim(
                path,
                translation=_coerce_vector(translation),
                rotation=_coerce_vector(rotation),
                scale=_coerce_vector(scale),
                dry_run=dry_run,
            ),
        )

    @app.tool(name="scene_material")
    def scene_material(path: str, material_path: str, dry_run: bool = False) -> dict:
        """Bind a material to a prim."""
        return router._wrap_operation_result(
            "scene",
            router._scene.set_material(path, material_path, dry_run=dry_run),
        )

    @app.tool(name="scene_rename")
    def scene_rename(path: str, new_name: str, dry_run: bool = False) -> dict:
        """Rename a prim and keep its subtree intact."""
        return router._wrap_operation_result("scene", router._scene.rename_prim(path, new_name, dry_run=dry_run))

    @app.tool(name="scene_variants")
    def scene_variants(path: str = "/World/Car") -> dict:
        """List native USD variant selections on a prim."""
        return router._scene.get_variant_selections(path)

    @app.tool(name="scene_variant_set")
    def scene_variant_set(
        variant_set: str,
        selection: str,
        path: str = "/World/Car",
        dry_run: bool = False,
    ) -> dict:
        """Select a native USD variant on a prim."""
        return router._wrap_operation_result(
            "scene",
            router._scene.set_variant_selection(path, variant_set, selection, dry_run=dry_run),
        )

    @app.tool(name="scene_animation_actions")
    def scene_animation_actions() -> dict:
        """List the named ZAZ-965 animation actions available in the live Composer scene."""
        return {"actions": ZAZ_ANIMATION_ACTIONS, "timeline": router._scene.get_timeline_state()}

    @app.tool(name="scene_animation_play")
    def scene_animation_play(action: str) -> dict:
        """Play or pose one named ZAZ-965 action such as open_everything or wheel_spin."""
        key = action.strip().lower()
        if key not in ZAZ_ANIMATION_ACTIONS:
            raise ValueError(f"Unknown animation action '{action}'; expected one of {sorted(ZAZ_ANIMATION_ACTIONS)}")
        spec = ZAZ_ANIMATION_ACTIONS[key]
        if spec["mode"] == "pose":
            timeline = router._scene.set_timeline_time(float(spec["frame"]))
        else:
            timeline = router._scene.play_timeline(float(spec["start"]), float(spec["end"]))
        return {"action": key, "description": spec["description"], "timeline": timeline}

    @app.tool(name="scene_animation_stop")
    def scene_animation_stop() -> dict:
        """Stop the active Composer timeline animation and keep the current pose."""
        return {"timeline": router._scene.stop_timeline()}

    @app.tool(name="scene_find")
    def scene_find(query: str, scope_path: str | None = None) -> dict:
        """Search for prims by path, type, or material text."""
        return router._scene.find_prims(query, scope_path=scope_path)

    @app.tool(name="scene_save")
    def scene_save(path: str) -> dict:
        """Save the current live scene to a USD file."""
        return router._scene.save_scene(path)

    @app.tool(name="learning_tutor")
    def learning_tutor(topic: str = "schemas", style: str = "lesson") -> dict:
        """Get a small USD learning lesson, cheat sheet, or quiz."""
        request = f"{style} me about {topic}"
        return router._dispatch_learning(request)

    @app.tool(name="learning_practice")
    def learning_practice(topic: str = "schemas") -> dict:
        """Get a practical, scene-aware drill you can do in USD Composer."""
        return router._dispatch_learning(f"practice me about {topic}")

    @app.tool(name="learning_practice_start")
    def learning_practice_start(topic: str = "schemas") -> dict:
        """Start an interactive practice session you can step through one command at a time."""
        return router.start_practice(topic)

    @app.tool(name="learning_practice_next")
    def learning_practice_next(feedback: str | None = None) -> dict:
        """Advance the current practice session to the next step."""
        return router.next_practice_step(feedback=feedback)

    @app.tool(name="learning_scene_quiz")
    def learning_scene_quiz(topic: str = "schemas") -> dict:
        """Generate a quiz from the currently selected prim or the current scene."""
        return router.scene_quiz(topic)

    @app.tool(name="learning_study_mode")
    def learning_study_mode(topic: str | None = None) -> dict:
        """Get a study recommendation based on the current scene."""
        return router.study_mode(topic)

    @app.tool(name="learning_translate")
    def learning_translate(request: str) -> dict:
        """Translate natural language into the most likely Omniverse scene action."""
        return router.translate_command(request)

    @app.tool(name="learning_history")
    def learning_history(limit: int = 20) -> dict:
        """Show recent learning actions and progress."""
        return router.learning_history(limit=limit)

    @app.tool(name="learning_cheatsheet")
    def learning_cheatsheet() -> dict:
        """Return the current cheat sheet for scene and learning commands."""
        return router.cheat_sheet()

    @app.tool(name="learning_roadmap")
    def learning_roadmap() -> dict:
        """Return a simple study roadmap based on what you have already practiced."""
        return router.learning_roadmap()

    @app.tool(name="usd_knowledge")
    async def usd_knowledge(query: str) -> dict:
        """Query the USD/OpenUSD knowledge backend directly."""
        return await router.ask_backend(RouteTarget.USD, query)

    @app.tool(name="kit_knowledge")
    async def kit_knowledge(query: str) -> dict:
        """Query the Kit knowledge backend directly."""
        return await router.ask_backend(RouteTarget.KIT, query)

    @app.tool(name="omni_ui_knowledge")
    async def omni_ui_knowledge(query: str) -> dict:
        """Query the OmniUI knowledge backend directly."""
        return await router.ask_backend(RouteTarget.OMNI_UI, query)

    @app.tool(name="isaac_sim_knowledge")
    async def isaac_sim_knowledge(query: str) -> dict:
        """Query the Isaac Sim knowledge backend directly."""
        return await router.ask_backend(RouteTarget.ISAAC_SIM, query)

    @app.tool(name="bowerbot_knowledge")
    async def bowerbot_knowledge(query: str) -> dict:
        """Query the Bowerbot knowledge backend directly."""
        return await router.ask_backend(RouteTarget.BOWERBOT, query)

    return app


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omniverse-router-mcp")
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "9905")))
    parser.add_argument(
        "--transport",
        default=os.environ.get("OMNIVERSE_MCP_TRANSPORT", "streamable-http"),
        choices=("stdio", "sse", "streamable-http"),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    app = build_server(host=args.host, port=args.port)
    app.run(transport=args.transport)
    return 0
