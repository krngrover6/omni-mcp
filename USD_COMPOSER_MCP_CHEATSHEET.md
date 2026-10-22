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

## 8) Exam prep and USD insight (Tier 1)

Tools for NCP-OUSD study. They work offline: no network and no docs MCP needed.

### Two safety changes first

- The command line now previews by default. `python -m omniverse_scene_mcp delete /World/Props/Cube`
  only shows what would change. Add `--apply` to really do it:
  `python -m omniverse_scene_mcp --apply delete /World/Props/Cube`
- The insight and validation tools read the SAVED file on disk, opened read-only. They never see
  unsaved edits in the live Composer/Isaac Sim stage, so save first (`scene_save`) when you want those
  edits included. Every result says `"mode": "saved_file"` and carries a note.

### Offline USD docs

```text
usd_docs_search(query="payload vs reference", top_k=5)
learning_glossary(term="LIVERPS")
learning_glossary()                      # list every glossary term
```

`usd_knowledge` and `router_dispatch` answer from the same bundled docs when the USD docs MCP
(127.0.0.1:9903) is not running; the result then has a `note` and a `primary_error`.

### Exam practice loop

```text
learning_exam_blueprint(include_objectives=False)
learning_exam_plan(days_until_exam=21, minutes_per_day=60)
learning_exam_quiz(domain="Composition", count=5)
learning_exam_quiz(objective="1.6", count=3)
learning_exam_answer(question_id="comp-020", chosen=[0], seconds=40)
learning_exam_answer(question_id="comp-006", chosen=["A", "C"])     # multi-select: give every pick
learning_exam_review(limit=10)
learning_exam_quiz(review_due=True)      # questions due for spaced repetition come first
learning_exam_progress()
learning_exam_reset(confirm=True)        # without confirm=True nothing changes; a .bak copy is kept
```

Progress is saved in `exam-prep/progress.json`, or in `$OMNIVERSE_EXAM_PREP_DIR/progress.json`
when that variable is set.

### Composition sandbox and LIVERPS puzzles

These compose layers in a throwaway temp folder, never in your scene files.

```text
usd_playground_compose(
    layers={"shot.usda": "#usda 1.0\n(\n    subLayers = [@./anim.usda@]\n)\ndef Xform \"World\" {}\n",
            "anim.usda": "#usda 1.0\nover \"World\" {\n    double size = 5\n}\n"},
    root="shot.usda",
    queries=["/World.size", "/World"])
learning_composition_puzzle(difficulty="medium", seed=42)
learning_composition_puzzle_answer(puzzle_id="liverps-medium-42", answer="B")
```

Puzzle answers are recorded under the Composition domain.

### Saved-file insight and validation

`file` defaults to the live scene's saved root layer. Relative paths start at the workspace folder.

```text
scene_stage_info()
scene_stage_info(file="my_project/scene.usda")   # any saved USD file inside the workspace
scene_composition(prim_path="/World/Car")
scene_explain_value(prim_path="/World/Car", attribute="visibility", time=24)
scene_properties(prim_path="/World/Car")
scene_bounds(prim_path="/World/Car")
usd_validate(profile="exam")             # or "usd" (OpenUSD validators) or "all"
```

If the live scene is not saved yet, or the router is on its fake scene, these return `ok: false`
with a message. Pass `file=...` in that case.

## 9) Daily lessons, mock exam and live tools

Tier 2 adds a day-by-day study plan, a timed mock exam, and tools that read or change the scene that is
open in Isaac Sim right now.

### What to say

| You say | What happens |
|---|---|
| `start day 1` | `learning_exam_day(day=1)`: that day's lesson (explanations, docs, an exercise, quiz questions) |
| `next lesson` | `learning_exam_day()`: the next day you have not marked complete |
| `quiz me` | `learning_exam_quiz(count=5)`: exam-bank questions, answers withheld; answer with `learning_exam_answer` |
| `mock exam` | Nothing starts. You get the steps; `learning_mock_exam_start()` starts the 120-minute clock |
| `why is this value X on my open scene` | `scene_explain_value(prim_path=..., attribute=...)` reads the live stage, unsaved edits included |
| `preview a rect light above /World/Box` | `scene_create_light(path="/World/KeyLight", light_type="rect", translation=[0, 0, 3], aim_at="/World/Box")` previews it |

Through `router_dispatch`, "start day 3", "day 1", "next lesson" and "today's lesson" return the lesson,
and "mock exam" only explains how to start one. "quiz me on schemas" there still gives the short
built-in topic quiz.

### Daily lessons

```text
learning_exam_set_date(exam_date="2026-11-30", minutes_per_day=60)   # or days_until_exam=21
learning_exam_day()                      # the next day not marked complete
learning_exam_day(day=3)
learning_exam_day_complete(day=3)        # streak, days left and the next day
```

- The plan starts the first time you open a lesson, with 30 days of 60 minutes by default.
- `learning_exam_set_date` re-plans from today and restarts at day 1. It clears completed-day marks,
  but your answers and spaced-repetition reviews stay.
- The schedule is rebuilt from your progress each time, so later days move toward weak objectives.
- Exercises: a LIVERPS puzzle (`learning_composition_puzzle_answer`), a "predict the value" item, or
  try-it layers. For the last two, predict first, then check with `usd_playground_compose`.

### Timed mock exam

```text
learning_mock_exam_start(count=65, minutes=120)      # the clock starts now; shows questions 1-10
learning_mock_exam_questions(session_id="mock-...", offset=10)
learning_mock_exam_answer(session_id="mock-...", question_id=3, chosen=["A", "C"])   # [] clears it
learning_mock_exam_flag(session_id="mock-...", question_id=3)
learning_mock_exam_status()                          # time left, unanswered, flagged
learning_mock_exam_finish(session_id="mock-...")     # score per domain, wrong answers explained
learning_mock_exam_abandon(session_id="mock-...")    # close without a score
```

- Questions are drawn per domain at the official weights. Only one mock can be open at a time.
- NVIDIA publishes no passing score. The 80% readiness target in the report is your own goal, not a
  pass mark.
- Finishing saves the answered questions to your progress, so wrong ones come back in
  `learning_exam_review`. Sessions are kept in `exam-prep/mock_sessions.json`.

### Live or saved file

The insight tools from section 8 now take `mode`:

```text
scene_explain_value(prim_path="/World/Box", attribute="size")                     # mode="auto"
scene_explain_value(prim_path="/World/Box", attribute="size", mode="live")        # open stage only
scene_explain_value(prim_path="/World/Box", attribute="size", mode="saved_file")  # file on disk only
scene_capabilities()                     # what the bridge can do, and whether it needs a reload
```

- `auto` reads the live stage when the bridge supports it. Otherwise it reads the saved file and adds
  a `fallback_note`. Passing `file=...` always means that saved file.
- Every result says which `mode` it used. Live answers include unsaved edits and change nothing.

### Lights and cameras: preview first

```text
scene_create_light(path="/World/KeyLight", light_type="rect", intensity=800, size=[2, 1],
                   translation=[0, -4, 3], aim_at="/World/Box")          # preview (dry run)
scene_create_camera(path="/World/ShotCam", focal_length=35, translation=[0, -6, 2], aim_at="/World/Box")
scene_aim(path="/World/KeyLight", target=[0, 0, 0])
```

- All three are dry runs by default. Read the preview, and only then repeat the call with
  `dry_run=False`.
- Applying changes the open scene in memory. It is not saved (use `scene_save`) and it is not on
  Kit's undo stack.
- Light types: rect, sphere, disk, distant, cylinder, dome. Aiming points the prim's -Z axis at the
  target, which is the direction cameras look and rect, disk and distant lights shine.

### Turning the live tools on

1. In Isaac Sim, open the Script Editor and run `isaacsim-study/reload_mcp_bridge.py`. It runs self-tests
   on Isaac's own USD, then restarts the scene bridge on port 9911.
   `isaacsim-study/bridge-reload-result.json` should show status `reloaded` and rpc_version `tier2-1`.
2. Restart the router so it loads the new tools, for example with `start-isaac-mcp.ps1`.
3. Run `scene_capabilities()`. `needs_bridge_update` should be `false`, and live insight and live
   authoring should both be available.

Until then, the live tools return `ok: false` with `needs_bridge_update`, and `auto` falls back to the
saved file. Composer's bridge on port 9910 only serves the basic scene commands.

## 10) Safe edits, undo, Composer and help

This section updates section 9: the bridge RPC version is now `tier3-1`, router edits are undoable, and
Composer can serve the same live tools as Isaac Sim.

### What to say

```text
help                                         # every tool, grouped by task (same as router_help())
help undo                                    # one group; "help lights", "help exam" work too
set intensity of /World/RectLight to 5000    # explicit path + dry_run=False applies; dry_run=True previews
set the color of /World/Props/Cube to 1 0 0  # becomes primvars:displayColor [[1, 0, 0]]
make a red material                          # always a preview; the answer names the tool call to apply it
paint /World/Props/Cube red                  # preview: create /World/Looks/RedMaterial, then bind it
bind /World/Looks/Steel to /World/Props/Cube
what did you change                          # the undo history
undo                                         # preview of what would be undone
confirm undo                                 # undoes it (send with dry_run=False)
```

A prim named in words ("the cube") is never edited directly: you get a preview and the exact command.

### The tools behind them

```text
scene_set_attribute(path="/World/RectLight", name="inputs:intensity", value=5000)       # preview
scene_create_material(path="/World/Looks/Steel", diffuse_color=[0.8, 0.8, 0.8], roughness=0.2, metallic=1)
scene_bind_material(prim_path="/World/Props/Cube", material_path="/World/Looks/Steel")
scene_undo_history()
scene_undo_last()                    # preview
scene_undo_last(dry_run=False)       # undo it
```

- Every edit tool is a dry run by default. Read the preview (value before, value after, which layer
  wins), then repeat the call with `dry_run=False`.
- `scene_create_material` also creates a missing `/World/Looks` as a Scope (`create_parent=True`).
- Undo restores the edited prims in memory. In Kit the same edit is on Ctrl+Z / Ctrl+Y too.
- Saving is not undoable. After an undo, save again if the file should lose the edit as well.
- Undo refuses when the stage was replaced or the prims were changed since; use Ctrl+Z in Kit then.

### Composer: the upgraded bridge

- Start Composer with `start-composer-study.ps1`. The upgraded bridge answers on port 9910 (same tools as
  Isaac Sim) and the stock Composer server moves to port 9909.
- Composer already running: run `composer-study/reload_composer_bridge.py` in its Script Editor, then read
  `composer-study/bridge-reload-result.json` (status `reloaded`, rpc_version `tier3-1`).
- Switch the router without restarting it:

```text
router_scene_targets()                          # isaac 9911, composer 9910, fake; which one is active
router_set_scene_target(target="composer")      # probes 127.0.0.1:9910/health first
router_set_scene_target(target="isaac")
router_set_scene_target(target="fake")          # offline in-memory scene
scene_capabilities()                            # live insight, authoring, scene_edits and undo available?
```

A target that does not answer is refused; `force=True` switches anyway.

### Kit and Isaac Sim docs offline

```text
docs_search(query="articulation controller", domain="isaac_sim")
docs_search(query="VStack spacing", domain="omni_ui")
extension_info(name="isaacsim.sensors.rtx")
```

These read the installed Isaac Sim 6.1 extension docs and never write them. `kit_knowledge`,
`omni_ui_knowledge` and `isaac_sim_knowledge` use the same docs when their MCP servers are not running.

### Turning it on (tier3-1)

1. Isaac Sim: run `isaacsim-study/reload_mcp_bridge.py` again. It now also self-tests `test_edits.py` and
   `test_undo.py` on Isaac's USD. The result file should show rpc_version `tier3-1`.
2. Composer: relaunch with `start-composer-study.ps1`, or run `composer-study/reload_composer_bridge.py`.
3. Restart the router (or use `router_set_scene_target`), then check `scene_capabilities()`:
   `needs_bridge_update` is `false` and `scene_edits` and `undo` are available.
