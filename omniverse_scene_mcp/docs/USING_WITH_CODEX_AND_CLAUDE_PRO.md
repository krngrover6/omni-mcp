# Using the Omniverse router with Codex, Claude Pro and Claude Code

This guide is for moving from Claude Code on the Max plan to cheaper plans: **Claude Pro** (Claude Desktop) and
**OpenAI Codex**. The router itself does not change: the same `omniverse_scene_mcp` server, the same scripts, the
same Composer and Isaac Sim bridges. Only the client side changes, plus a few switches that make each session cheaper.

## Why the tool count matters

Every MCP client sends the name, description and argument schema of **every listed tool** to the model when a
session starts, and again in every request that carries the tool list. The router lists about 155 tools. Measured
on the real tool list (2026-10-22, test mode, `profiles.estimate_schema_tokens`, compact JSON characters / 4):

| Profile (`OMNIVERSE_MCP_PROFILE`) | Tools listed | Approx. tokens | Share of `all` |
|---|---:|---:|---:|
| `all` (default, today's behaviour) | 155 | 22,272 | 100% |
| `build` | 64 | 10,786 | 48% |
| `starter,study` | 66 | 7,700 | 35% |
| `study` | 57 | 6,950 | 31% |
| `pipeline` | 32 | 4,376 | 20% |
| `robotics` | 22 | 3,069 | 14% |
| `starter` | 18 | 2,006 | 9% |

Real tokenizers differ by model, so read the numbers as a comparison. Other agents are adding tools right now; when
they land, `all` grows and the profile rows grow only by the tools that belong to them. Recompute with:

```powershell
$env:OMNIVERSE_SCENE_MCP_TEST = "1"   # fake scene, offline knowledge, no ports opened
& "C:/Program Files/Python312/python.exe" -c "import json; from omniverse_scene_mcp import server, profiles; app = server.build_server(); tools = [{'name': t.name, 'description': t.description, 'inputSchema': t.parameters} for t in app._tool_manager.list_tools()]; [print(r) for r in profiles.estimate_profile_tokens(tools)]"
```

(Run it with the same `PYTHONPATH` as `start-isaac-mcp.ps1` sets: `omniverse_scene_mcp\src` plus the
`bowerbot-venv\Lib\site-packages` folders.)

**Every profile keeps `router_dispatch`.** It understands plain English ("move the chair 2 m to the left",
"quiz me on variants") and reaches every router feature, even tools the profile hides. A small profile costs less;
you lose only the model's ability to call the hidden tools *by name*.

## Profiles

| Profile | What it is for |
|---|---|
| `starter` | First choice for any plan. Plain-English entry (`router_dispatch`, `router_route`), help, status, scene target, scene summary/inspect/children/find, `scene_explain`, `explain_error`, `python_show`, `learning_today`, `scene_undo_last`, `usd_knowledge`, `workspace_context`. |
| `study` | NCP-OUSD exam prep: exam/mock/puzzles, drills, practice, tutor, glossary, Python lessons, homework checks, class notes. |
| `build` | Scene editing: create/move/rename/delete, layout, materials, lights, cameras, variants, layers, checkpoints, undo and journal, assets, projects. |
| `robotics` | Physics, robots, simulation controls, screenshots. Also picks up joint, sensor, gripper, pick-and-place and ROS 2 tools automatically when they are added. |
| `pipeline` | Asset intake and health, import, performance, variants, OmniGraph, message bus, animation, reports, projects, Kit logs, Docker docs. |
| `all` | Everything (the default, so nothing changes until you choose a profile). |

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

Note: profiles and compact results take effect once `server.py` applies them (see "For the integrator" below).
Until then the variables are simply ignored and all tools are listed.

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

## For the integrator (server.py)

Three calls at the end of `build_server()`, after the last `@app.tool` and the docstring-cleanup loop, before
`return app`:

```python
from . import profiles, workflows

workflows.register_prompts(app)                 # nine MCP prompts; tolerant of FastMCP versions
profiles.install_compact_outputs(app)           # wraps tools; acts only while OMNIVERSE_MCP_COMPACT=1
stash, profile_report = profiles.apply_profile_to_app(app)   # reads OMNIVERSE_MCP_PROFILE; "all" = no change
profiles.advertise_tool_list_changed(app)       # only needed with router_enable_tools (below)
```

Order matters: compact wrapping before pruning, so hidden tools are wrapped too. Pruning must come after every tool
is registered. `router_dispatch` calls the Python functions directly (the call table in `build_server`), not the
tool list, so it keeps reaching hidden tools. Optionally log `profile_report` to stderr (never stdout: stdio
transport uses stdout).

### Meta tools (design): `router_enable_tools`, `router_list_tools`, `router_call_tool`

Installed MCP SDK: **`mcp` 1.28.1** in `bowerbot-venv` (the router's FastMCP). What it supports:

* `FastMCP.add_tool` / `FastMCP.remove_tool` at runtime (tools live in `app._tool_manager._tools`, one list per
  process, shared by every connected session).
* `ServerSession.send_tool_list_changed()` sends `notifications/tools/list_changed` (reach it in a tool through a
  `ctx: Context` parameter: `await ctx.session.send_tool_list_changed()`).
* But FastMCP advertises `tools.listChanged = false`: `create_initialization_options()` is called without
  `NotificationOptions(tools_changed=True)` for stdio, SSE and streamable HTTP. A client may ignore the
  notification unless the capability is advertised. `profiles.advertise_tool_list_changed(app)` fixes that (it
  wraps `app._mcp_server.create_initialization_options` to pass `NotificationOptions(tools_changed=True)`; checked
  against 1.28.1: the advertised capability becomes `listChanged=True`).
* Client support for re-listing varies: Claude Code handles `list_changed`; the Codex binary contains the
  notification name but its behaviour was not verified here; do not rely on it.

So implement three always-on tools (already in `profiles.ALWAYS_ON`, so every profile keeps them):

```python
@app.tool(name="router_list_tools")
def router_list_tools(group: str = "*") -> dict:
    """Hidden tools you can enable or call. group: a profile name (study, build, ...) or a pattern."""
    names = profiles.resolve_profile(group, stash.hidden)["tools"] if group != "*" else stash.names()
    return {"hidden": [row for row in stash.describe() if row["name"] in names], "profiles": profiles.profile_names()}

@app.tool(name="router_enable_tools")
async def router_enable_tools(group: str, ctx: Context) -> dict:
    """List more tools for this session: a profile name (study, build, robotics, pipeline), tool names or patterns."""
    enabled = stash.enable(group)
    if enabled:
        await ctx.session.send_tool_list_changed()
    return {"enabled": enabled, "note": "If your client does not refresh its tool list, use router_call_tool."}

@app.tool(name="router_call_tool")
async def router_call_tool(name: str, arguments: dict | None = None) -> Any:
    """Call any router tool by name, including hidden ones (same validation and safety defaults)."""
    return await stash.call(name, arguments or {})
```

Register these three **before** `apply_profile_to_app` (so they are never pruned) but they need `stash`: create it
with a holder (`holder = {}` then `holder["stash"]`) or register them after pruning (they are in `ALWAYS_ON`, so a
later registration is fine either way). `router_call_tool` is the portable path: it works in every client, whether
or not it re-lists tools, and costs one small schema. Since the tool list is process-wide, enabling tools on the
shared HTTP router enables them for every connected client until the router restarts.
