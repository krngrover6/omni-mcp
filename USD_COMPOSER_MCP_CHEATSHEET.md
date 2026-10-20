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
