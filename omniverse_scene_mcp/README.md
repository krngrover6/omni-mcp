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

## Handoff integration (2026-10-22)

The complete surface has **228 tools** and **9 workflow prompts**. The default `starter` profile lists **21**
tools; discover the rest with `router_list_tools(pattern="robot_*", include_schema=True)`, call them through
`router_call_tool`, or expose a group with `router_enable_tools(names="robotics")`.
Set `OMNIVERSE_MCP_PROFILE=all` before an approved router restart to advertise everything.
See [profiles and client examples](docs/USING_WITH_CODEX_AND_CLAUDE_PRO.md).

The handoff modules now cover projects/layers/checkpoints, asset health/import, graph recipes and events,
variants, performance, animation, robot imports/joints, sensors, pick-and-place script generation, and ROS 2
checks. New live operations use the allowlisted `feature_call` RPC in **tier8-1**; pure planning and teaching
operations run without Kit. Runtime-only tools report missing Kit capabilities rather than pretending to work.

**Scene edit tools preview by default.** Only literal `dry_run=False` permits a write. Natural-language routes
for the new tools always preview edits, even when their JSON contains false. Reading a saved `scene_file`
cannot edit it. Pick-and-place code is generated as text and is never executed by its generation tool.
Runtime robot motion, converter jobs and sensors still need verification in the running app after an approved
bridge reload. Unit tests and standalone USD checks do not prove physical simulation or ROS connectivity.

### Verification without starting apps

From the workspace root, using the existing matching Python and dependencies:

```powershell
& 'C:/Program Files/Python312/python.exe' -I omniverse_scene_mcp/tools/run_tests.py -q
& 'C:/Program Files/Python312/python.exe' -I omniverse_scene_mcp/tools/check_kit_usd.py isaacsim-6.1.0
```

The second command loads the installed USD 25.11 libraries into a separate Python process and runs the suite
on temporary stages. It does not start Isaac, reload a bridge, or contact live MCP services. Tests also exercise
fake bridge-reload scripts: their printed reload messages describe mocks, not the running apps.

## Running

By default, `python -m omniverse_scene_mcp` uses the fake scene so it works
outside Kit.

For the easiest full-stack startup from the repo root, use:

```powershell
cd C:\Users\krngr\Omniverse
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

The server exposes 129 tools; `router_help()` lists all of them grouped by task.
The main ones (Tier 4 below adds the asset, physics, screenshot, simulation and
dashboard tools):

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
- `router_help`, `router_scene_targets`, `router_set_scene_target`
- `scene_set_attribute`, `scene_create_material`, `scene_bind_material`
- `scene_undo_last`, `scene_undo_history`
- `asset_search`, `asset_info`, `scene_add_asset`
- `scene_physics_report`, `scene_add_physics_scene`, `scene_make_rigid_body`,
  `scene_make_collider`, `scene_remove_physics`
- `scene_capture_viewport`, `scene_capture_status`
- `sim_status`, `sim_play`, `sim_pause`, `sim_stop`, `sim_step`
- `learning_exam_dashboard`
- `docs_search`, `extension_info`
- `usd_knowledge`
- `kit_knowledge`
- `omni_ui_knowledge`
- `isaac_sim_knowledge`
- `bowerbot_knowledge`

Safety and health:

- `router_health` - read-only health report (scene backend choice, 300 ms port
  probes, router PID, redacted stderr tail); it never restarts anything
- `scene_save` - `dry_run=True` previews in-place save versus Save As

### Exam prep and USD insight (Tier 1)

Offline USD docs (no network, no docs MCP needed):

- `usd_docs_search(query, top_k=5)` - BM25 search over the bundled USD 25.11
  API atlas, openusd.org doc chunks, Learn OpenUSD pages and the workspace
  `OPENUSD_*_ONE_BY_ONE.md` notes; every hit carries a citation
- `learning_glossary(term=None)` - Learn OpenUSD glossary lookup; no term lists
  all terms
- `usd_knowledge` and `router_dispatch` now fall back to the same offline docs
  when the USD docs MCP (127.0.0.1:9903) is not running; the answer carries
  `note` and `primary_error`

NCP-OUSD exam prep (one shared progress store per server):

- `learning_exam_blueprint(include_objectives=True)` - domains, weights,
  objectives, readings and question-bank counts
- `learning_exam_plan(days_until_exam=None, minutes_per_day=60)` - weighted plan
  boosted for weak domains, with an optional day-by-day schedule
- `learning_exam_quiz(domain=None, objective=None, count=5, review_due=False)` -
  original practice questions, answers withheld; `review_due=True` puts due
  spaced-repetition cards first
- `learning_exam_answer(question_id, chosen, seconds=None)` - grades with
  per-option explanations and records progress
- `learning_exam_progress()`, `learning_exam_review(limit=10)`
- `learning_exam_reset(confirm=False)` - does nothing without `confirm=True`;
  the old file is copied to `progress.json.bak-<timestamp>` first

Progress lives in `<workspace>/exam-prep/progress.json`, or in
`$OMNIVERSE_EXAM_PREP_DIR/progress.json` when that variable is set. Under
`OMNIVERSE_SCENE_MCP_TEST` it stays in memory unless `OMNIVERSE_EXAM_PREP_DIR`
is set.

Composition sandbox (temp folder only, never your scene files):

- `usd_playground_compose(layers, root, queries=None)` - compose USDA strings
  and explain layer stack, arcs and values (`/Prim`, `/Prim.attr`,
  `/Prim.attr@24`, `/Prim.rel`)
- `learning_composition_puzzle(difficulty="easy", seed=None)` and
  `learning_composition_puzzle_answer(puzzle_id, answer)` - LIVERPS puzzles
  graded by really composing the layers; answers are recorded under the
  Composition domain

Insight and validation, read-only. Since Tier 2 each tool takes
`mode="auto"` (default), `"live"` or `"saved_file"` and every result says which
`mode` it used (plus `mode_requested`):

- `auto` with `file=...` reads that SAVED file. Without `file`, it reads the
  LIVE open stage (unsaved edits included) when `scene_capabilities` reports
  live insight; otherwise it falls back to the saved root layer and adds
  `fallback_note` and `live_fallback` (with `needs_bridge_update`)
- `live` never falls back; `saved_file` never asks the bridge
- in saved-file mode `file` defaults to the live scene's saved root layer;
  relative paths resolve against the workspace root, and explicit paths must
  be inside the workspace

- `scene_stage_info(file=None, mode="auto")`
- `scene_composition(prim_path, file=None, mode="auto")`
- `scene_explain_value(prim_path, attribute, time=None, file=None, mode="auto")`
- `scene_properties(prim_path, file=None, mode="auto")`
- `scene_bounds(prim_path, file=None, mode="auto")`
- `usd_validate(file=None, profile="exam", mode="auto")` - `exam`, `usd` or `all`

The command-line form `python -m omniverse_scene_mcp <request>` previews
(dry run) unless `--apply` is given.

### Daily lessons, mock exam and live tools (Tier 2)

Daily lessons (plan settings live in the same progress store):

- `learning_exam_day(day=None)` - one plan day as a lesson: objectives with
  plain explanations, offline doc hits, readings, a hands-on exercise (LIVERPS
  puzzle, "predict the value" item or try-it layers for
  `usd_playground_compose`) and quiz questions with answers withheld;
  `day=None` is the next day not marked complete
- `learning_exam_day_complete(day)` - mark a day done; reports streak, days
  left and the next day
- `learning_exam_set_date(exam_date=None, days_until_exam=None,
  minutes_per_day=60)` - re-plan from today (default 30 days); completed-day
  marks are cleared, answers and reviews are kept

Timed mock exam (one open at a time, sessions in `mock_sessions.json` next to
`progress.json`; memory-only under `OMNIVERSE_SCENE_MCP_TEST` unless
`OMNIVERSE_EXAM_PREP_DIR` is set):

- `learning_mock_exam_start(count=65, minutes=120)` - starts the clock; draws
  questions per domain at the official weights and shows the first 10
- `learning_mock_exam_questions(session_id=None, offset=0, limit=10)` - page
  through the questions with your current choices and flags
- `learning_mock_exam_answer(session_id, question_id, chosen)` - record or
  change an answer (id or 1-based number; letters or 0-based indices)
- `learning_mock_exam_flag(session_id, question_id, flagged=True)`
- `learning_mock_exam_status(session_id=None)` - time left, unanswered and
  flagged questions; lists recent mocks when none is open
- `learning_mock_exam_finish(session_id)` - per-domain score, wrong questions
  with explanations, a readiness verdict against a self-set 80% target (NVIDIA
  publishes no passing score); answered questions go to exam progress
- `learning_mock_exam_abandon(session_id)` - close without scoring

`router_dispatch` understands "start day 3", "day 1", "next lesson" and
"today's lesson" (it returns the lesson) and "mock exam" (it only explains how
to start one; it never starts the timer).

Live scene tools (need the extended Isaac Sim bridge, see `kit_rpc.py`):

- `scene_capabilities()` - bridge RPC version, live insight queries, whether
  light/camera authoring is available and whether a bridge reload is needed
- `scene_create_light(path, light_type, intensity=None, exposure=None,
  color=None, size=None, translation=None, aim_at=None, dry_run=True)` -
  rect, sphere, disk, distant, cylinder or dome; `size` is one number or
  `[width, height]` / `[length, radius]`
- `scene_create_camera(path, focal_length=None, horizontal_aperture=None,
  clipping_range=None, translation=None, aim_at=None, copy_optics_from=None,
  dry_run=True)`
- `scene_aim(path, target, dry_run=True)` - turn a prim so its -Z axis faces a
  prim path or an `[x, y, z]` point

The three authoring tools are DRY RUN BY DEFAULT: show the preview to the user
and call again with `dry_run=False` only after they agree. Applying changes the
user's open scene in memory (not saved; since RPC `tier3-1` the edit is also
recorded for `scene_undo_last` and Kit's Ctrl+Z). On the fake scene or an old
bridge they return `ok: false` with `needs_bridge_update`.

To turn the live tools on: run `isaacsim-study/reload_mcp_bridge.py` in Isaac
Sim's Script Editor (it self-tests on Isaac's USD, then restarts the bridge on
9911 with RPC version `tier3-1`; it was `tier2-1` before Tier 3), restart the
router so it loads this code (or use `router_set_scene_target`), then check
`scene_capabilities()`.

### Safe edits, undo, offline docs, help and scene targets (Tier 3)

Safe edits (`edits.py`; DRY RUN BY DEFAULT at every layer: server tool, service,
HTTP client, RPC and Kit bridge; a dry run leaves every layer byte-identical):

- `scene_set_attribute(path, name, value, time=None, create_type=None,
  dry_run=True)` - the value is coerced to the attribute's USD type (colors as
  `[r, g, b]` or `[[r, g, b]]` for `primvars:displayColor`, `#RRGGBB`, 0-255
  whole numbers); the preview shows the value before, where it comes from, and
  whether a stronger opinion would hide the edit
- `scene_create_material(path, diffuse_color=None, roughness=None,
  metallic=None, opacity=None, emissive_color=None, ior=None,
  create_parent=True, dry_run=True)` - a `Material` with a `PreviewSurface`
  (UsdPreviewSurface) shader wired to `outputs:surface`; `create_parent` also
  plans a missing direct parent such as `/World/Looks` as a `Scope`
- `scene_bind_material(prim_path, material_path, strength=None, purpose=None,
  dry_run=True)` - MaterialBindingAPI binding with an optional
  `strongerThanDescendants` strength and `preview`/`full` purpose; the preview
  predicts the material the prim resolves to. On a bridge older than `tier3-1`
  a plain binding falls back to the basic `material` method
  (`fallback: "set_material"`)

Undo (`undo.py`, `kit_commands.py`):

- `scene_undo_last(dry_run=True)` - the preview names the edit that would be
  undone; `dry_run=False` restores the edited specs (inside Kit through the
  `MCPEdit` command, so Ctrl+Z / Ctrl+Y in the app work too). It refuses when
  the stage was replaced or the prims changed since. Saving is not undoable:
  undo reverts the open layers in memory, so save again afterwards
- `scene_undo_history(limit=20)` - read-only list of recorded edits, newest
  first

Offline Kit / Isaac Sim / omni.ui docs (`offline_docs_knowledge.py`, reads the
installed `isaacsim-6.1.0` or `$OMNIVERSE_KIT_DOCS_ROOT`, never writes it):

- `docs_search(query, domain="isaac_sim", top_k=5)` - domains `isaac_sim`,
  `kit`, `omni_ui`; every hit has a `path:line` source; deprecated extensions
  are flagged
- `extension_info(name)` - version, docs, dependencies, deprecation and latest
  changelog entry of one installed extension (renamed `omni.isaac.*` ids get
  suggestions)
- `kit_knowledge`, `omni_ui_knowledge` and `isaac_sim_knowledge` now fall back
  to these docs when their docs MCP (9902 / 9901 / 9904) is not running (the
  answer carries `note` and `primary_error`), like `usd_knowledge` does

Guide and scene targets:

- `router_help(topic=None)` - every tool grouped by task with examples and
  `router_dispatch` phrases; `topic` is a group (`lights`, `undo`), a tool
  name or a keyword. `router_dispatch("help")` gives the same guide
- `router_scene_targets()` - read-only: `isaac` (9911), `composer` (9910) and
  `fake`, and which one is active
- `router_set_scene_target(target, force=False)` - switch the scene tools to
  another app without restarting the router; the app's `/health` is probed
  first (300 ms, 127.0.0.1 only) and a target that does not answer is refused
  unless `force=True`. No scene data changes

`router_dispatch` phrases that reach these: "set intensity of /World/RectLight
to 5000", "make a red material", "paint /World/Props/Cube red", "bind
/World/Looks/Steel to /World/Props/Cube", "undo" (preview), "confirm undo"
(applies with `dry_run=False`), "what did you change", "help undo". Materials
and paint from a sentence always preview and return the exact tool calls that
apply them.

The in-memory fake scene implements the edits and undo in memory too (results
say `fake_scene: true`), so these flows work offline and in tests.

### Assets, physics, screenshots, simulation and the dashboard (Tier 4, RPC `tier4-1`)

Assets (`asset_library.py`). The index of the local Isaac Sim 6.1 asset pack
(`isaacsim-assets-6.1.0`, or `$OMNIVERSE_ISAAC_ASSETS_ROOT`) lives in the
ROUTER process: it is walked once (about 1 s for 13k files) and only read.

- `asset_search(query, top_k=10, category=None)` - ranked absolute USD paths by
  file and folder name ('franka', 'warehouse shelf', 'carter'), read-only
- `asset_info(asset_path)` - category, size, thumbnail and the header
  (defaultPrim, upAxis, metersPerUnit), read-only
- `scene_add_asset(prim_path, asset_path, translation=None, rotation=None,
  scale=None, instanceable=None, correct_units=False, asset_prim_path=None,
  dry_run=True)` - DRY RUN BY DEFAULT: an Xform at `prim_path` that references
  the asset (resolved to an absolute path by the router); the preview lists the
  xform ops and any upAxis / metersPerUnit mismatch, `correct_units=True` fills
  in the suggested rotation/scale. Undoable when applied

Physics (`physics.py`, UsdPhysics; edits DRY RUN BY DEFAULT and undoable):

- `scene_physics_report(prim_path=None, file=None, mode="auto")` - read-only:
  PhysicsScene and gravity, rigid bodies, colliders, mass, approximations,
  joints and common mistakes; auto falls back to the saved file
- `scene_add_physics_scene(path="/physicsScene", gravity=None, dry_run=True)`
- `scene_make_rigid_body(prim_path, mass=None, density=None, kinematic=False,
  add_collider=True, approximation=None, dry_run=True)`
- `scene_make_collider(prim_path, approximation=None, dry_run=True)`
- `scene_remove_physics(prim_path, include_descendants=False, dry_run=True)`

Screenshots and simulation (`kit_live_extras.py`, Kit only; no authored data):

- `scene_capture_viewport(output_dir=None, width=None, height=None)` - a new
  PNG of the ACTIVE viewport (camera and render settings untouched), returned
  as `pending`; `scene_capture_status(path)` says when it is written
- `sim_status()` (read-only), `sim_play()`, `sim_pause()`, `sim_stop()` (resets
  the timeline; PhysX restores pre-play poses), `sim_step(frames=1)` (1-600,
  paused only). These CHANGE SIMULATION STATE: call them only when asked

Exam dashboard (`exam/dashboard.py`):

- `learning_exam_dashboard(write=True)` - domains, weighted accuracy, plan,
  mocks, coverage and focus; `write=True` also saves a self-contained
  `dashboard.html` next to `progress.json` (never the real `exam-prep/` under
  `OMNIVERSE_SCENE_MCP_TEST`)

`router_dispatch` phrases: "add a franka robot", "add a warehouse shelf at 0 0
0", "find a carter robot" (asset search, then a PREVIEW of adding the best hit
with `candidates` and an `apply_with` scene_add_asset call; a sentence never
adds an asset), "make /World/Box a rigid body", "add collision to /World/Box
with convex hull", "remove physics from /World/Box", "add a physics scene",
"physics report" (physics edits apply only for an exact path or the selection
with `dry_run=False`; a prim named by name previews), "take a screenshot", "is
my screenshot ready", "play / pause / stop the simulation", "step 10 frames"
(these run when asked; `dry_run=True` previews them), "show my dashboard".

Bridge guard (`kit_rpc.guard_handler_class`): the extended bridge answers 403
to browser requests (Origin / Sec-Fetch-*), a foreign Host and non-JSON POSTs;
with `OMNIVERSE_BRIDGE_TOKEN` set in Kit, POSTs need the same token
(`X-Omniverse-Bridge-Token`), which the router (RPCs and `/health` probes) sends
when the variable is set in its environment too. A 403 comes back as a
structured `forbidden` result and nothing runs in Kit.

On a bridge older than `tier4-1` every new tool answers `needs_bridge_update`
with `how_to_enable` (reload the Isaac or Composer bridge). The in-memory fake
scene supports asset references and a physics-free timeline in memory; physics
and screenshots say they are not supported there.

### Learning Python, homework and class notes (Tier 5, RPC `tier5-1`)

Six beginner helpers. Every tool here is read-only except `python_practice_run`,
which saves the graded attempt to your exam progress.

"Show me the Python" (`python_teacher.py`, `python_links.py`):

- `python_show(action, args=None, context="script_editor")` - a short runnable
  OpenUSD snippet for one task (inspect, create, move/rotate/scale, attributes,
  time samples, lights, cameras, materials, references, payloads, variants,
  sublayers, physics, save/export, undo) with every line explained, the USD
  concepts, where to run it, what it changes and the exam objectives. Router
  tool names work as actions (`scene_move`, `scene_create_light`, ...).
  `context="standalone"` gives a usd-core script that opens a `.usda` file.
  Nothing runs; user values only become `repr()` literals
- `python_topics()` - every supported task with its arguments
- `show_python=False` on `router_dispatch` and the editing tools (`scene_create`,
  `scene_delete`, `scene_duplicate`, `scene_rename`, `scene_move`,
  `scene_material`, `scene_variant_set`, `scene_set_attribute`,
  `scene_create_light`, `scene_create_camera`, `scene_aim`,
  `scene_create_material`, `scene_bind_material`, `scene_add_asset`,
  `scene_add_physics_scene`, `scene_make_rigid_body`, `scene_make_collider`):
  `True` adds `result["python"]`, the snippet for the same change with the same
  values. A snippet problem is reported inside `python` and never fails the
  edit; with `False` (the default) results are unchanged

Python practice (`exam/python_practice.py`, 24 exercises in
`exam/data/python_exercises.json`):

- `python_practice_list(level=None)` - exercises in order with `passed` flags
  and `next_exercise`
- `python_practice_get(exercise_id=None, reveal=False, level=None)` - goal,
  starter code, hints and checks; no id picks your next unpassed exercise; the
  solution only with `reveal=True`
- `python_practice_run(exercise_id, code, timeout_s=10, record=True)` - runs
  your code in a separate `python -I` process on a throwaway temp copy of the
  exercise scene, grades it, and with `record=True` saves the result to exam
  progress (source `python_practice`)

Errors (`error_explainer.py`, `data/error_patterns.json`):

- `explain_error(text, code=None, search_docs=True)` - plain-English
  explanation, likely causes, fix steps and an example for a traceback, Kit
  `[Error]` line, USD/Tf or PhysX message; `search_docs` adds related offline
  USD and Isaac Sim / Kit doc pages. The text is only read with regexes

Homework and scene tour (`assignments.py` + `data/assignments/*.json`,
`scene_tour.py`; pure pxr, read-only):

- `assignment_list()` and `assignment_check(assignment_id="isaac_session02_warehouse",
  scene_file=None)` - pass / partial / fail per rubric rule with how-to-fix
  steps, a score and the ONE next step
- `scene_explain(focus_path=None, depth=3, max_prims=400, scene_file=None)` -
  a friendly tour: groups, geometry, lights, cameras, materials, physics,
  layers and references, a glossary and questions to explore
- Without `scene_file` both read the LIVE stage through the new RPC methods
  `assignment_check` and `scene_tour` (time-budgeted like insight, never
  writing). On a bridge older than `tier5-1` or the fake scene they fall back
  to the live scene's saved file; if there is none they answer
  `needs_bridge_update` (with `how_to_enable`) and suggest `scene_file=...`

Class notes (`class_notes.py`, offline BM25, read-only): the class folder
(`~/Downloads/Issac Sim Classes`, or `$OMNIVERSE_CLASS_NOTES_ROOT`) and
`isaacsim-mentorship/**/REVISION_NOTES.md`:

- `class_sessions()`, `class_ask(question, session=None, top_k=6)`,
  `class_search(query, session=None, top_k=5)` - passages with session,
  source and timestamp (subtitle and Gemini clocks differ; each hit says which)

`router_dispatch` phrases (all read-only): "show me the python for moving a
cube", "how do I write python to add a light", "python code for creating a
camera" (add "standalone" for a usd-core script), "give me a python exercise",
"list python exercises", "explain this error: ..." (or paste a traceback),
"check my homework", "what's next on my homework", "check my homework in
isaacsim-mentorship/session02/main.usda", "explain my scene", "describe the
scene under /World/Shelves", "what did the teacher say about ground plane",
"what did we learn in session 2", "list class sessions".

### Drills, today's plan, change journal, layout, robots and Docker (Round 7, RPC `tier6-1`)

Seven additions for daily practice and for building the warehouse scene. Read-only unless noted.

Timed drills (`exam/drills.py`; sessions in `drill_sessions.json` beside `progress.json`, memory-only in test mode):

- `learning_drill_focuses()` - the focuses (weak, mistakes, mixed, objective, domain, flash) and limits
- `learning_drill_start(focus="weak", objective=None, domain=None, count=10, seconds_per_question=45)` - questions
  without answers; saves only the drill session. Questions of an open mock exam are never used
- `learning_drill_answer(drill_id, answers, elapsed_s=None)` - grades everything at once and SAVES the answers to
  exam progress (source `drill`; flash prompts are not saved)
- `learning_drill_status(drill_id)` - time left, or the saved result

Today and the reminder (`exam/reminder.py`):

- `learning_today(minutes=60)` - "what should I do today?": a time-boxed plan from saved progress (a read-only copy
  of `progress.json`; nothing is written)
- `learning_reminder_setup(at="19:00")` - returns the PowerShell text to create and remove a daily Windows reminder;
  it creates NOTHING (`created: false`)

Change journal (`journal.py`): every APPLIED edit made through the journaled tools (all scene edit tools, undo, save,
layout and `router_dispatch`) is appended to `<workspace>/scene-journal/change_journal.jsonl` (or
`$OMNIVERSE_SCENE_MCP_JOURNAL`; memory-only under `OMNIVERSE_SCENE_MCP_TEST`). Previews are never recorded; secrets
in arguments are dropped; the file rotates at 5 MB. The default folder gets its own `.gitignore` (`*`), so it never
shows up in git.

- `scene_journal(limit=20, since=None, prim=None, tool=None, target=None)`, `scene_journal_explain(...)` and
  `scene_journal_summary(day=None)` - entries, plain sentences ("14:02 - You moved /World/Cube from (0, 0, 0) to
  (1, 2, 3) in Composer. Undo with scene_undo_last (id 7).") and the story of a day
- `scene_journal_clear(confirm=False)` - empties only the journal, with a backup file; needs `confirm=True`

Smart layout (`layout.py`, pure pxr; only translations change; DRY RUN BY DEFAULT; one undoable edit when applied):

- `scene_layout_snap(paths, surface=None, dry_run=True)` - rest prims on the floor (found automatically), a surface
  prim or a height
- `scene_layout_stack(paths, rows=None, anchor=None, gap=0.0, facing=None, dry_run=True)` - rows bottom first
  (default `[4, 3, 2, 1]`), each row resting on the one below
- `scene_layout_distribute(paths, surface_path, margin=None, dry_run=True)` - spread along a shelf top
- `scene_layout_space(paths, axis="x", spacing=None, dry_run=True)` and
  `scene_layout_align(paths, axis="x", mode="min", dry_run=True)`
- Live only (new RPC methods `layout_snap`, `layout_stack`, `layout_distribute`, `layout_space`, `layout_align`;
  a missing or malformed `dry_run` is a dry run; stale requests are dropped like other writes). The fake scene says
  it is not supported; an older bridge answers `needs_bridge_update` with `how_to_enable`

Robot inspector (`robot_inspector.py`, pure pxr, read-only):

- `robot_inspect(root_path=None, scene_file=None, max_prims=5000)` - articulations, links (mass, colliders), joints
  in plain words (hinge, slider, fixed) with axes, limits and drives, the chain from the base, degrees of freedom and
  setup problems with fixes. Live through the new read-only RPC method `robot_inspect` (time-budgeted like insight),
  or a saved scene / robot asset under the workspace (for example
  `isaacsim-assets-6.1.0/Assets/Isaac/6.1/Isaac/Robots/FrankaRobotics/FrankaPanda/franka.usd`) opened read-only

Docker docs helper (`docker_docs.py`; never pulls, builds, starts or stops anything; key values are never read):

- `docs_docker_status(service="usd", check_endpoint=False)` - read-only `docker version` / `image ls` / `ps`
- `docs_docker_setup_plan(service="usd")` - the steps YOU would run, each marked when it needs your approval
- `docs_docker_explain()` - is Docker worth it, compared with the offline docs the router already has

`router_dispatch` phrases (`nl_tier6.py`): "start a drill on weak areas", "drill me on composition", "drill me on
objective 1.6", "quick flash quiz", "what should I do today" (", i have 30 minutes"), "set up a daily reminder at
7pm", "what did you change today", "show the change journal", "what did you change on /World/Cube", "stack these 10
cubes 4 3 2 1" (the selection), "stack /World/Box_01 /World/Box_02 /World/Box_03 in rows 2 1", "snap /World/Box_01
to the floor", "put boxes evenly on /World/Warehouse/Shelves/UpperShelf", "align /World/A /World/B on x", "inspect
my robot", "inspect /World/Franka", "is docker worth it", "docker status", "how do I set up docker".

Dry-run policy for sentences: a layout sentence ALWAYS previews (even with exact paths and
`router_dispatch(dry_run=False)`); the answer lists the moves and the exact `scene_layout_*(..., dry_run=False)`
call that applies them. Layout moves many prims at once and sentences may name prims by selection or by name, so
applying is left to the explicit tool call. `router_dispatch(dry_run=True)` never starts a drill. Phrases that
already had a route keep it ("what did you change" is still the undo history, "drill me on prims" is still a
lesson, "inspect /World/Props/Cube" still inspects the prim; "inspect /World/<RobotName>" goes to the robot
inspector only when the last path part is a robot name such as Franka, UR10 or Carter).

## Testing

Run the whole suite offline (it sets `OMNIVERSE_SCENE_MCP_TEST=1`, refuses
non-loopback and live-stack connections, and never touches your scenes or
`exam-prep/`):

```powershell
& 'C:/Program Files/Python312/python.exe' -I omniverse_scene_mcp/tools/run_tests.py
& 'C:/Program Files/Python312/python.exe' -I omniverse_scene_mcp/tools/run_tests.py test_twelve_gaps_integration
```

With pytest installed outside Kit (`pip install -e "omniverse_scene_mcp[usd,test]"`;
never into Kit, Isaac Sim or Composer): `pytest -q omniverse_scene_mcp/tests`.
The Isaac Sim bridge reload runs `test_kit_bridge_usd.py`, `test_kit_rpc.py`,
`test_edits.py`, `test_undo.py`, `test_kit_live_extras.py`, `test_physics.py`,
`test_scene_tour.py`, `test_assignments.py`, `test_layout.py` and
`test_robot_inspector.py` inside Isaac before it swaps the bridge (Composer's reload runs the same optional files
when present).

## Next step

Point the optional upstream endpoints at the real Omniverse MCP servers you
want the router to talk to.
