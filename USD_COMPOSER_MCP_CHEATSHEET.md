# USD Composer + MCP Cheat Sheet

This is the shortest practical guide for using MCPs with USD Composer.

## Big idea

USD Composer is where you open and edit the scene.
MCPs are the assistants you ask for help, knowledge, and scene actions.

In this repo, the usual flow is:

1. Start the Omniverse launcher
2. Open USD Composer
3. Ask the router or Codex to talk to the right MCP

## 1) Start everything

From the repo root:

```powershell
cd C:\Users\krngr\Downloads\Omniverse
.\start-omniverse.ps1 -LaunchComposer
```

If you only want the background MCP stack:

```powershell
.\start-omniverse.ps1
```

If you want to stop the launcher-managed processes:

```powershell
.\stop-omniverse.ps1
```

## 2) Launch USD Composer manually

From the Kit repo:

```powershell
cd C:\Users\krngr\Downloads\Omniverse\kit-app-template
.\repo.bat build
.\repo.bat launch
```

After the launcher opens, choose USD Composer.

If you already built it once, you usually only need:

```powershell
.\repo.bat launch
```

## 3) Register the router as the single MCP entry point

If you want one front door for all Omniverse MCPs, register:

```powershell
claude mcp add --transport http omniverse-router http://127.0.0.1:9905/mcp
```

Or use:

- [omniverse-router.mcp.json](C:/Users/krngr/Downloads/Omniverse/omniverse-router.mcp.json)

## 4) What to ask the router

### Scene commands

These go to the scene side:

```text
ping
summary
inspect /World
children /World
create /World/NewPrim
create /World/NewLight type DistantLight
delete /World/NewPrim
duplicate /World/Props/Cube to /World/Props/CubeCopy
reparent /World/Props/CubeCopy to /World/Props
move /World/Props/Cube 1 2 3
material /World/Props/Cube to /World/Looks/RedMaterial
```

### Knowledge questions

These get routed to the matching knowledge MCP:

```text
What is a USD prim?
How do references work in USD?
How do I build a Kit extension?
How do I make a UI with omni.ui?
How do I set up an Isaac Sim robot scene?
What is Bowerbot used for?
```

## 5) Use dry-run when you are unsure

For the MCP tool `router_dispatch`, use `dry_run=true` first when the action is risky.

Example idea:

- preview a delete before you actually delete it
- preview a move before you commit it

## 6) Study path

If you want to learn this in order, study these first:

1. `router_status`
2. `router_route`
3. `router_dispatch`
4. `scene_summary`
5. `scene_inspect`
6. `scene_children`
7. `scene_create`
8. `scene_move`
9. `scene_duplicate`
10. `scene_reparent`
11. `scene_material`
12. `usd_knowledge`
13. `kit_knowledge`
14. `omni_ui_knowledge`
15. `isaac_sim_knowledge`
16. `bowerbot_knowledge`

## 7) The simple mental model

- Use USD Composer to view and edit the live scene
- Use the router for one command surface
- Let the router choose scene vs knowledge MCP
- Use Bowerbot when you are doing asset assembly or project setup

