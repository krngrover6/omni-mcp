# Omniverse Router Quickstart

This is the simplest way to use the router you asked for.

## 1) Start the upstream MCPs

The router can talk to these Omniverse MCPs:

- USD Code MCP on `http://127.0.0.1:9903/mcp`
- Kit MCP on `http://127.0.0.1:9902/mcp`
- OmniUI MCP on `http://127.0.0.1:9901/mcp`
- Isaac Sim MCP on `http://127.0.0.1:9904/mcp`
- Bowerbot MCP on `http://127.0.0.1:8181/mcp`

Start whichever ones you want to use. If one is not running, the router will
still start, but requests for that backend will return a safe error instead of
guessing.

## 2) Start the router

From `C:\Users\krngr\Downloads\Omniverse\omniverse_scene_mcp`:

```powershell
python -m omniverse_scene_mcp serve
```

That starts the router as an MCP server.

If you want a client to launch it directly, keep the stdio mode:

```powershell
python -m omniverse_scene_mcp serve --transport stdio
```

If you want the easiest single command from the repo root, use:

```powershell
cd C:\Users\krngr\Downloads\Omniverse
.\start-omniverse.ps1
```

Or just double-click / run:

```powershell
.\start-omniverse.bat
```

That starts Bowerbot and the Omniverse router if they are not already
listening.

To also launch USD Composer with live scene awareness:

```powershell
.\start-omniverse.ps1 -LaunchComposer
```

If USD Composer is already open, close it first and relaunch with the switch
above so the setup extension and live scene control endpoint load cleanly.

When live mode is active, the Composer setup extension exposes a local scene
control endpoint at `http://127.0.0.1:9910/rpc`. The router uses that endpoint
instead of the fake test stage.

To stop the launcher-managed processes:

```powershell
cd C:\Users\krngr\Downloads\Omniverse
.\stop-omniverse.ps1
```

## 3) Register the router in Claude Code

If you want Claude Code to talk to one Omniverse entry point, run:

```powershell
claude mcp add --transport http omniverse-router http://127.0.0.1:9905/mcp
```

Or use the bundled file:

- [omniverse-router.mcp.json](C:/Users/krngr/Downloads/Omniverse/omniverse-router.mcp.json)

## 4) Use the router tools

The main tools are:

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

For a simple study-first version of the same workflow, see:

- [USD_COMPOSER_MCP_CHEATSHEET.md](C:/Users/krngr/Downloads/Omniverse/USD_COMPOSER_MCP_CHEATSHEET.md)

Simple examples:

```text
inspect /World/Props/Cube
move /World/Props/Cube 1 2 3
What is a USD prim?
How do I build a Kit extension?
```

## 5) Launch USD Composer simply

You already have the `kit-app-template` repo locally.

From `C:\Users\krngr\Downloads\Omniverse\kit-app-template`:

```powershell
.\repo.bat build
.\repo.bat launch
```

Then choose the USD Composer app when the launcher asks.

If you have already built it once, you usually only need:

```powershell
.\repo.bat launch
```

## 6) What each piece does

- USD Composer: the app where you open and edit a USD scene
- USD Code MCP: USD/OpenUSD knowledge and API docs
- Kit MCP: Omniverse Kit extension and app knowledge
- OmniUI MCP: UI and `omni.ui` knowledge
- Isaac Sim MCP: simulation and robotics knowledge
- Bowerbot: scene-assembly workflows for USD content
- Omniverse Router: one front door that decides where each request should go
