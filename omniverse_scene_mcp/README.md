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

Saved-file insight and validation. These read the SAVED file read-only, never
the live unsaved stage; every result has `"mode": "saved_file"` and a note.
`file` defaults to the live scene's saved root layer; relative paths resolve
against the workspace root, and explicit paths must be inside the workspace:

- `scene_stage_info(file=None)`
- `scene_composition(prim_path, file=None)`
- `scene_explain_value(prim_path, attribute, time=None, file=None)`
- `scene_properties(prim_path, file=None)`
- `scene_bounds(prim_path, file=None)`
- `usd_validate(file=None, profile="exam")` - `exam`, `usd` or `all`

The command-line form `python -m omniverse_scene_mcp <request>` previews
(dry run) unless `--apply` is given.

## Next step

Point the optional upstream endpoints at the real Omniverse MCP servers you
want the router to talk to.
