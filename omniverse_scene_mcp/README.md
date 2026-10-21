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
user's open scene in memory (not saved, not on Kit's undo stack). On the fake
scene or an old bridge they return `ok: false` with `needs_bridge_update`.

To turn the live tools on: run `isaacsim-study/reload_mcp_bridge.py` in Isaac
Sim's Script Editor (it self-tests on Isaac's USD, then restarts the bridge on
9911 with RPC version `tier2-1`), restart the router so it loads this code,
then check `scene_capabilities()`.

## Next step

Point the optional upstream endpoints at the real Omniverse MCP servers you
want the router to talk to.
