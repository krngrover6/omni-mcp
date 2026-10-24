# Using the Omniverse router with Codex, Claude Pro and Claude Code

This guide is for moving from Claude Code on the Max plan to cheaper plans: **Claude Pro** (Claude Desktop) and
**OpenAI Codex**. The router itself does not change: the same `omniverse_scene_mcp` server, the same scripts, the
same Composer and Isaac Sim bridges. Only the client side changes, plus a few switches that make each session cheaper.

## Why the tool count matters

The server can advertise a small tool list while keeping the remaining tools available on demand.
How a client sends or caches tool schemas varies; these estimates compare the server's schemas, not billing.
Measured locally on 2026-10-22 using compact JSON characters / 4 (real tokenizers differ):

| Profile (`OMNIVERSE_MCP_PROFILE`) | Tools listed | Approximate schema tokens |
|---|---:|---:|
| `starter` (default) | 21 | 2,260 |
| `study` | 60 | 7,210 |
| `build` | 85 | 13,280 |
| `robotics` | 57 | 7,460 |
| `pipeline` | 97 | 12,470 |
| `all` | 249 | 33,680 |

Counts (updated for Day 5) include the three discovery tools. Estimates are rounded and will change when descriptions change.
The default lists about 91% fewer tools. No existing client configuration is rewritten automatically.

Every profile keeps `router_dispatch`, `router_list_tools`, `router_enable_tools` and `router_call_tool`.
Natural language covers a documented subset; use discovery and an exact tool call for other operations.

## Profiles

| Profile | What it is for |
|---|---|
| `starter` | First choice for any plan. Plain-English entry (`router_dispatch`, `router_route`), help, status, scene target, scene summary/inspect/children/find, `scene_explain`, `explain_error`, `python_show`, `learning_today`, `scene_undo_last`, `usd_knowledge`, `workspace_context`. |
| `study` | NCP-OUSD exam prep: exam/mock/puzzles, drills, practice, tutor, glossary, Python lessons, homework checks, class notes. |
| `build` | Scene editing: create/move/rename/delete, layout, materials, lights, cameras, variants, layers, checkpoints, undo and journal, assets, projects. |
| `robotics` | Physics, robots, simulation controls, screenshots. Also picks up joint, sensor, gripper, pick-and-place and ROS 2 tools automatically when they are added. |
| `pipeline` | Asset intake and health, import, performance, variants, OmniGraph, message bus, animation, reports, projects, Kit logs, Docker docs. |
| `all` | Everything; explicitly set `OMNIVERSE_MCP_PROFILE=all` for the full list. |

Combine them with commas: `starter,study`. You can also add single tools or patterns:
`starter,scene_layers,scene_checkpoint*`. Aliases: `minimal`/`basic` = starter, `exam`/`learn` = study,
`edit` = build, `isaac`/`robot` = robotics, `assets` = pipeline, `full` = all.

A misspelled profile never leaves you with an empty server: if nothing matches, all tools stay listed.

## Step 1: start the router (same as today)

The router is the shared HTTP server on `http://127.0.0.1:9905/mcp`. Start it with the existing scripts from
`C:/Users/krngr/Omniverse` (see the workspace `AGENTS.md`; nothing new to install):

```powershell
./start-omniverse.ps1                 # router (+ knowledge MCPs)
./start-omniverse.ps1 -LaunchComposer # also USD Composer with the live bridge on 9910
./start-isaac-mcp.ps1                 # router pointed at Isaac Sim's bridge on 9911
```

To choose a profile for this router, set it in the same PowerShell window **before** starting it (the scripts start
the router with `Start-Process`, which inherits the window's environment):

```powershell
$env:OMNIVERSE_MCP_PROFILE = "starter,study"
$env:OMNIVERSE_MCP_COMPACT = "1"      # optional: shorter tool results (see "Compact results")
./start-omniverse.ps1
```

Profiles and compact results are integrated in `build_server()`. A running router keeps its previously loaded code
until an approved restart. Changing the environment in a different terminal does not update that process.

The HTTP router is shared: every client connected to port 9905 sees the same profile. For a different profile per
client, use the client-side filter (Codex `enabled_tools`) or a stdio entry (Claude Desktop; Codex option B), which
starts a private router process with its own environment.

## Step 2a: OpenAI Codex

**Transport facts (checked locally on this laptop: `codex-cli 0.162.1`, `codex mcp add --help`, and the
config keys inside the installed `codex.exe`):**

* Codex supports **streamable HTTP** servers directly: `url = "http://127.0.0.1:9905/mcp"` in
  `[mcp_servers.<name>]`, or `codex mcp add <name> --url <URL>`. No stdio proxy is needed.
* It also supports stdio servers (`command`, `args`, `[mcp_servers.<name>.env]`); the router supports that too
  (`python -m omniverse_scene_mcp serve --transport stdio`).
* Per-server keys found in this Codex build: `url`, `command`, `args`, `env`, `startup_timeout_sec`,
  `tool_timeout_sec`, `enabled_tools`, `disabled_tools`, `bearer_token_env_var`, `http_headers`,
  `env_http_headers`. `enabled_tools` is the cheap-usage switch: Codex only shows the model those tools.
* Your `%USERPROFILE%\.codex\config.toml` **already has** an `[mcp_servers.omniverse_router]` table with a
  localhost `url`, so Codex is connected today; you only need to add the filter.
* Codex 0.162.1 lists its `tool_search` feature flags as "removed", so do not count on Codex loading MCP tools lazily:
  filter them.

What to do:

1. Start the router (step 1).
2. Open `%USERPROFILE%\.codex\config.toml` and add `enabled_tools` (and the two timeouts) to the existing
   `[mcp_servers.omniverse_router]` table. Copy them from
   [`client-configs/codex-config.toml.example`](client-configs/codex-config.toml.example) (option A = the
   `starter` profile). Back up the file first; change nothing else.
3. Restart Codex (or start a new session) and run `codex mcp list`: `omniverse_router` should be listed.
4. Ask: "Use omniverse_router: router_status, then scene_summary."

Want more tools for one session? Add names to `enabled_tools`, or use option B (stdio + `OMNIVERSE_MCP_PROFILE`).

### Using the workspace AGENTS.md with Codex

Codex reads `AGENTS.md` files automatically: the one in `C:/Users/krngr/Omniverse` applies when you start Codex in
that folder (your config already trusts it). Start Codex from the workspace (`cd C:/Users/krngr/Omniverse; codex`)
so it gets the same rules Claude Code follows: read `OMNIVERSE_WORKSPACE_CONTEXT.md` first, read-only inspection
before changes, dry run before broad actions, ask before launching services or changing configs, never print
secrets. Nothing to copy: the same file serves both tools.

## Step 2b: Claude Pro (Claude Desktop)

* Claude Desktop's local config (`claude_desktop_config.json`) starts **stdio** servers (`command`, `args`, `env`).
  Remote "custom connectors" are reached from Anthropic's cloud, which cannot see `127.0.0.1`, so they cannot reach
  this router. Use the stdio entry: Claude Desktop starts a private router process that talks to the same Composer
  (9910) / Isaac Sim (9911) bridges.
* On this laptop no `claude_desktop_config.json` exists yet (checked `%APPDATA%\Claude` and the Microsoft Store
  app's folder). Open it from Claude Desktop: **Settings > Developer > Edit Config**, which creates the file in the
  right place.

What to do:

1. Open the config as above, paste the `omniverse_router` entry from
   [`client-configs/claude-desktop.json.example`](client-configs/claude-desktop.json.example) inside
   `"mcpServers"` (create the key if the file is empty `{}`), save, and fully quit and restart Claude Desktop.
2. The example sets `OMNIVERSE_MCP_PROFILE = "starter,study"` and `OMNIVERSE_MCP_COMPACT = "1"`; change them per
   the profile table.
3. The private router auto-detects Composer on 9910. For Isaac Sim, ask Claude to call
   `router_set_scene_target(target="isaac")`.
4. Check: ask "call router_status". If Claude shows no hammer/tools icon, see Troubleshooting.

Two routers (the shared HTTP one and Claude Desktop's private one) can run at the same time; each keeps its own undo
history, so undo an edit from the client that made it.

## Step 2c: Claude Code

Already set up: the workspace `.mcp.json` (same as
[`client-configs/claude-code.mcp.json.example`](client-configs/claude-code.mcp.json.example)) points at
`http://127.0.0.1:9905/mcp` with `"type": "http"`. Recent Claude Code versions load large MCP tool sets lazily (tool
search), so the full list costs less there; check with `/context`. Claude Code also shows the router's prompts (below)
as slash commands, `/mcp__omniverse_router__daily_study`. Commands: `claude mcp list`, and `/mcp` inside a session.

## Ready-made recipes (MCP prompts)

`workflows.py` defines nine prompts. A prompt costs nothing until you pick it; then it gives the model a short,
numbered plan naming the exact tools, with the safety rules (preview with `dry_run=True`, apply only after you agree,
never save without asking):

| Prompt | Arguments | What it does |
|---|---|---|
| `start_project` | name, template, goal | project_templates > project_list > project_plan > (ask) > project_create |
| `daily_study` | minutes | learning_today, then drills/quizzes, then learning_exam_progress |
| `fix_my_error` | paste | explain_error (or kit_explain_recent_errors), then a previewed fix |
| `check_homework` | assignment_id | assignment_list > assignment_check, explains failed checks |
| `build_configurator_step` | prim_path, step | inspect, checkpoint, previewed material/variant edit, scene_diff |
| `digital_twin_import` | source_file, prim_path | import preview, usd_asset_health, scene_add_asset preview, scene_bounds |
| `robot_pick_place` | robot_path, object_path | robot_inspect, physics report and previews, sim_play/step/status |
| `review_my_scene` | focus_path | read-only: scene_explain, usd_validate, usd_asset_health, scene_layers |
| `make_portfolio_report` | project_dir, title | project_report preview, then write only after you agree |

Where to find them: Claude Code `/` menu; Claude Desktop "+" menu > the router's prompts. The Codex binary speaks
`prompts/list` and `prompts/get`, but how (and whether) this Codex version shows server prompts in its UI was not
verified. In any client you can ask "call router_help" and then "follow the daily_study steps", or paste the
rendered text (`python -c "from omniverse_scene_mcp import workflows; print(workflows.render_prompt('daily_study', minutes='30'))"`).

## Compact results

`OMNIVERSE_MCP_COMPACT=1` makes tool results shorter: lists are cut to 10 items, long texts to 400 characters, and
`glossary`, `python` and `preview` blocks are dropped unless you asked for them (`show_python=True` keeps the Python).
A `_trimmed` note says what was cut. Teaching tools whose whole point is text (`python_show`, `learning_glossary`,
`workspace_document`, `project_report`, ...) are never trimmed. Turn it off for one-off deep dives.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Codex/Claude Code: "connection refused" for `omniverse_router` | Router not running | Run the start script (step 1). Check with `Get-NetTCPConnection -LocalPort 9905 -State Listen`. |
| Tools missing that you expected | Profile or `enabled_tools` hides them | Use `router_dispatch` in plain English, add the tool to the profile/filter, or use `all`. |
| `Port 9905 belongs to a different router instance` | Another router (Composer vs Isaac) holds the port | Stop it with `./stop-omniverse.ps1` (check its targets first) or switch target with `router_set_scene_target`. |
| Wrong port in a config | `9905` is the router; `9910` Composer bridge; `9911` Isaac bridge; `8181` Bowerbot | MCP clients connect to **9905** only. |
| Scene tools say "fake scene" or edits do not appear | No bridge answered | Start Composer/Isaac Sim; then `router_status`. For Isaac use `router_set_scene_target(target="isaac")`. |
| `rpc_version` mismatch, new tool says "bridge needs reload" | Bridge runs older code | Run `composer-study/reload_composer_bridge.py` (Composer) or `isaacsim-study/reload_mcp_bridge.py` (Isaac Sim) in the Script Editor. |
| Claude Desktop shows no tools | Config JSON invalid, or Python path wrong | Validate the JSON; check `C:/Program Files/Python312/python.exe` exists; read the MCP log via Settings > Developer. |
| Stdio server exits at once | `PYTHONPATH` missing the pywin32 folders | Copy the full `PYTHONPATH` from the example (same folders as `start-isaac-mcp.ps1`). |

## Cheap-usage checklist

- [ ] Pick the smallest profile for the task (`starter`; add `study` or `build` only when needed).
- [ ] Codex: set `enabled_tools` (client-side filter) for the shared router.
- [ ] Claude Desktop: set `OMNIVERSE_MCP_PROFILE` in the stdio entry's `env`.
- [ ] Turn on `OMNIVERSE_MCP_COMPACT=1` for everyday work.
- [ ] Start from a recipe prompt (`daily_study`, `fix_my_error`, ...) instead of explaining the workflow each time.
- [ ] Ask in plain English through `router_dispatch` instead of enabling a big profile for one call.
- [ ] Prefer `scene_summary` / `scene_children` over full dumps; pass `max_prims`/`limit` arguments.
- [ ] Start a new session per topic: long sessions resend the whole history.
- [ ] Disable MCP servers you are not using in that client (Blender, VRED, ...): their tools cost tokens too.

## Discovery and calls (implemented)

Start with `router_list_tools(pattern="sensor_*")` to see names and brief descriptions.
For one tool's full argument schema, call:

```python
router_list_tools(pattern="sensor_add_camera", include_schema=True)
router_call_tool(name="sensor_add_camera", arguments={"parent_path": "/World/Robot/Link"})
```

That sensor call previews only. Applying it requires an explicitly authorized second call with
`"dry_run": False`. Only literal JSON `false` authorizes a write; missing, null, string and numeric flags preview.
Saved-file inspection is read-only. Generated pick-and-place scripts are returned as text, never executed.

To expose more schemas, use `router_enable_tools(names="robotics")` or an exact tool name/pattern.
The list is shared by all clients on that router process. The server advertises and sends a tool-list-change
notification; clients may require a refresh. `router_call_tool` works without refreshing the list and uses the
same argument validation as a directly listed tool. Recursive discovery calls are refused.

Nine workflow prompts are registered. Optional compact results preserve generated pick-and-place scripts and
schema-discovery results; other large results may be shortened and marked `_trimmed`.

Implementation order: register all tools and journal hooks, register prompts and discovery, wrap optional compact
outputs, then apply the profile. The allowlist in `features.py` is shared with Kit RPC `feature_call` (tier8-1).
No arbitrary function or module can be named through that RPC. An old bridge reports that it needs updating;
the router never reloads it automatically.
