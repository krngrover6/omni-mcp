from __future__ import annotations

import argparse
import os
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


def build_server(host: str | None = None, port: int | None = None) -> FastMCP:
    router = build_router()
    app = FastMCP(
        name="omniverse-router-mcp",
        instructions="Single front door for Omniverse scene operations and routed knowledge lookups.",
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

    @app.tool(name="scene_find")
    def scene_find(query: str, scope_path: str | None = None) -> dict:
        """Search for prims by path, type, or material text."""
        return router._scene.find_prims(query, scope_path=scope_path)

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
