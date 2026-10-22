# Handoff: Omniverse scene MCP (Claude -> Codex), 2026-10-22

## Prompt to paste into Codex

```
Read C:\Users\krngr\Omniverse\AGENTS.md, then OMNIVERSE_WORKSPACE_CONTEXT.md, then this file (HANDOFF_FOR_CODEX.md).
I am a Python beginner learning OpenUSD for the NCP-OUSD exam, using Isaac Sim 6.1 and USD Composer.
My custom MCP lives in omniverse_scene_mcp/ (router on http://127.0.0.1:9905/mcp).
Work on git branch wip/round7-9-handoff. Keep the dry-run-by-default rule for every scene edit.
Do not edit my scenes (isaacsim-mentorship/**, working_scene.usda, course scenes) and do not edit kit-app-template or kit-usd-agents.
Ask before: reloading Isaac/Composer bridges, restarting the router, committing, pushing, installing anything.
Step 1: run the full test suite (see below) and tell me the result.
Step 2: finish the unfinished list in this file, in order, smallest first.
Explain things simply, one step at a time.
```

## State
- Branch `feat/ncp-ousd-tier1` pushed at b9293b9 (rounds 1-6, 110 tools, CI green).
- Branch `wip/round7-9-handoff` (local, NOT pushed) holds everything after: ~1384 passing tests, 8 failing (all in unfinished Day-4 pieces).
- Round 7 (wired, 129 tools, RPC tier6-1): drills, daily reminder (`learning_today`), change journal, smart layout (`scene_layout_*`), robot inspector (`robot_inspect`), Docker docs helper.
- Group A and Day 3 modules (built + tested, NOT yet wired into server/router/help): project_starter, layer_manager, checkpoints, kit_logs, asset_health, journal_script, project_report, omnigraph_helper, message_bus, variant_manager, import_helper, perf_advisor, animation.
- Day 4 (interrupted, partly written): robot_import, joint_control, sensors, pick_place, ros2_check, profiles, workflows, docs/.
- Live apps still run tier5-1 (Composer). New code needs a bridge reload (Script Editor, run composer-study/reload_composer_bridge.py) and a router restart (start-isaac-mcp.ps1).

## Unfinished list (in order)
1. Fix the 8 failing tests: test_joint_control (5), test_profiles (2), test_pxr_guards (1: unguarded `pxr` import in a new test or module).
2. Finish and test: robot_import, sensors, pick_place, ros2_check, workflows (check each has a passing test file).
3. Wire group A + Day 3 + Day 4 modules into server.py, router NL (nl_tier7+), help.py, kit_rpc.py (see how round 6/7 did it: `git show b9293b9` and the round 7 diff).
4. Tool profiles (profiles.py): default to a small tool set, so cheap clients don't load ~150 schemas. See docs/USING_WITH_CODEX_AND_CLAUDE_PRO.md.
5. Update README + USD_COMPOSER_MCP_CHEATSHEET.md, run full suite and the Kit 25.11 check, commit, push (account krngrover6), reload bridges, restart router.

## How to run things
- Tests: `& 'C:/Program Files/Python312/python.exe' -I 'C:/Users/krngr/AppData/Local/Temp/claude/C--Users-krngr-Omniverse/95cdf3be-6b57-4a88-a9a9-b1b598787514/scratchpad/run_tests.py'` (or `omniverse_scene_mcp/tools/run_tests.py`). Set `OMNIVERSE_SCENE_MCP_TEST=1`. A few timing/network tests are flaky under load; rerun alone.
- Push: `GCM_INTERACTIVE=never git -c credential.username=krngrover6 push`.
- PowerShell 5.1 mangles quotes in `git commit -m`; use bash with `git commit -F -`.
- Rules for new tools: pure-pxr functions, `dry_run=True` default, JSON results, guarded `omni`/`pxr` imports, only USD APIs present in both 25.11 and 26.8, tests use temp dirs only.

## Plan after that
Project plan (30 days): warehouse -> configurator (bag_configurator_course) -> digital twin (digital_twin_physical_ai_course) -> Isaac robot cell -> capstone. Day 1 exam answers are still pending (see memory/mcp-next-build-pending.md).
