# Omniverse Scene MCP

Single-router MCP for Omniverse scene actions and knowledge backends.

## What this is for

This project is the custom layer that sits in front of the existing NVIDIA
Omniverse MCP servers. The goal is to handle live scene state and scene
mutations with small, predictable tools, while forwarding knowledge lookups to
the right upstream MCP:

- inspect stage, hierarchy, selection, cameras, lights, and materials
- create, delete, duplicate, reparent, and transform prims
- preview risky actions with `dry_run`
- return human-readable diffs
- keep knowledge questions routed to the existing USD/Kit/OmniUI/Isaac/Bowerbot MCPs

## What is included

- a bridge abstraction for live Omniverse access
- an in-memory fake bridge for tests and local iteration
- a service layer that implements safe scene operations
- a compact set of initial tool primitives
- a runtime selector so you can switch between fake and live Kit mode
- a router front door that chooses the correct backend for each request
- a FastMCP server that exposes the router as a single MCP surface

## Running

By default, `python -m omniverse_scene_mcp` uses the fake scene so it works
outside Kit.

For the easiest full-stack startup from the repo root, use:

```powershell
cd C:\Users\krngr\Downloads\Omniverse
.\start-omniverse.ps1
```

Add `-LaunchComposer` if you want USD Composer opened too, with live scene
awareness enabled through the Composer setup extension:

```powershell
.\start-omniverse.ps1 -LaunchComposer
```

To start the router as an MCP server:

```bash
python -m omniverse_scene_mcp serve
```

The same server can be launched with:

```bash
omniverse-router-mcp
```

If you want to register it in a client once and keep the Omniverse routing
centralized, use the bundled config file at
[omniverse-router.mcp.json](C:/Users/krngr/Downloads/Omniverse/omniverse-router.mcp.json).
It launches the router over stdio and forwards requests to the configured
upstream MCP endpoints.

To point it at a live Omniverse Kit session, set:

```bash
OMNIVERSE_SCENE_MCP_MODE=live
OMNIVERSE_USD_CONTEXT_NAME=
```

Then instantiate `LiveKitSceneBridge` through the runtime helper.

The router also looks for optional upstream MCP endpoints:

```env
OMNIVERSE_USD_MCP_ENDPOINT=http://127.0.0.1:9903/mcp
OMNIVERSE_KIT_MCP_ENDPOINT=http://127.0.0.1:9902/mcp
OMNIVERSE_OMNI_UI_MCP_ENDPOINT=http://127.0.0.1:9901/mcp
OMNIVERSE_ISAAC_SIM_MCP_ENDPOINT=http://127.0.0.1:9904/mcp
OMNIVERSE_BOWERBOT_MCP_ENDPOINT=
```

If an endpoint is missing, that backend stays connected as a safe placeholder
instead of failing the whole router.

## Router behavior

- `scene ...` commands go to the live scene service
- questions about USD, Kit, OmniUI, Isaac Sim, or Bowerbot are routed to the
  matching knowledge backend
- the `router_dispatch` tool is the single generic entry point if you want the
  server to decide for you
- if a knowledge backend is not connected yet, the router returns a structured
  handoff instead of guessing

## Why this shape

The existing `kit-usd-agents` repo already covers the individual Omniverse
knowledge servers. What was missing here was a deterministic, scene-first MCP
surface that can sit in front of them and route commands cleanly without
duplicating the underlying documentation tools.

## MCP Tools

The server exposes these tools:

- `router_dispatch`
- `router_route`
- `router_status`
- `scene_summary`
- `scene_inspect`
- `scene_children`
- `scene_create`
- `scene_delete`
- `scene_duplicate`
- `scene_reparent`
- `scene_move`
- `scene_material`
- `usd_knowledge`
- `kit_knowledge`
- `omni_ui_knowledge`
- `isaac_sim_knowledge`
- `bowerbot_knowledge`

## Next step

Point the optional upstream endpoints at the real Omniverse MCP servers you
want the router to talk to.
