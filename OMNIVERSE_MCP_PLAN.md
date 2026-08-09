# Omniverse MCP Plan

## Goal
Build a custom, scene-aware Omniverse MCP that works alongside the existing NVIDIA MCP servers.

The new MCP should focus on live scene state, beginner-friendly guidance, and custom workflows that the official knowledge MCPs do not cover.

## What Already Exists

- USD Code MCP for OpenUSD API knowledge
- Kit MCP for Omniverse Kit extension and app development knowledge
- OmniUI MCP for UI development inside Omniverse
- Isaac Sim MCP for simulation and robotics workflows
- Bowerbot as a third-party Omniverse scene assembly MCP: [https://github.com/binary-core-llc/bowerbot](https://github.com/binary-core-llc/bowerbot)

## What We Will Build

### 1. Scene-Aware MCP
Core tools for live Omniverse scene control:

- inspect stage, prims, cameras, lights, materials, and selections
- create, delete, duplicate, group, ungroup, and reparent prims
- move, rotate, scale, and reassign materials
- read scene hierarchy and relationships
- capture snapshots and scene summaries

### 2. Beginner Learning Layer
Tools that explain the scene in simple language:

- what a prim is
- what the current selection means
- how parenting works
- what variants, payloads, and references are
- what changed after each command

### 3. Tool Routing Layer
A coordinator that chooses the right source:

- Omniverse scene MCP for live scene actions
- USD Code MCP for USD API and concepts
- Kit MCP for extension and app development
- OmniUI MCP for UI development
- Isaac Sim MCP for simulation-specific guidance
- optional Bowerbot side-by-side for asset assembly workflows

### 4. Safety and Preview
Every risky action should support:

- `dry_run`
- preview before execution
- human-readable diff of what changed
- clear errors when a prim or path is missing

## Suggested Repo Structure

- `src/` for the MCP server
- `tools/` for scene, hierarchy, material, camera, light, and learning tools
- `bridge/` if a local Omniverse process bridge is needed
- `tests/` for fake stage and tool tests
- `docs/` for beginner guides and workflow notes

## Build Order

### Phase 1
- connect to a live Omniverse/KIT session
- add ping/version/scene summary
- read selection and stage hierarchy

### Phase 2
- add create/delete/duplicate/group/reparent
- add transform and material tools
- add camera and light tools

### Phase 3
- add timeline and animation controls
- add import/export and asset placement helpers
- add snapshot and compare tools

### Phase 4
- add learning/explanation tools
- add “what changed” summaries
- add guided workflows for beginners

### Phase 5
- add multi-MCP coordination logic
- route knowledge requests to the right MCP
- keep custom scene actions inside your MCP

## Design Rules

- Keep tools small and predictable
- Prefer `unique_path` or exact identifiers over ambiguous names
- Use `dry_run` wherever possible
- Return structured results, not just text
- Keep everything local and workspace-controlled

## Success Criteria

- I can inspect the current Omniverse scene
- I can modify the scene safely
- I can learn what each tool did in plain language
- I can use this MCP together with the official NVIDIA MCP servers
