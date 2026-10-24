# Handoff: Omniverse scene MCP (Claude -> Codex), 2026-10-22

Extension lifecycle completed: `mcp_day5_validation/docs/extension_live_20261024_233954.json`
passed all 9 checks, including the user's real Inspect stage click and complete cleanup.
Generator shutdown now hides the window before destroying/releasing it, and tolerates
incomplete startup. A focused regression passes with all 16 Day 5 tests on both USD builds.
No full-suite rerun after this small change; prior full baseline remains 1,428 per build.
User manually restarted the router; `router_restart_generator_verified.json` confirms
the Composer target and corrected code in an actual newly generated extension. Saved
scene files were unchanged. No bridge reload was required.
Remaining functional prerequisites: a real CAD fixture and a ROS publisher/subscriber setup.

Blender follow-up, 2026-10-24: existing addon connected (protocol 13, Blender 5.2.2 LTS);
no installation/update. The custom MCP-generated selection-only script exported a separate
2x1x1 m green box. USD handoff checks, actual Composer reference/bounds/material and a visual
capture passed. Original Blender scene/objects were restored. Composer temporary edits were
undone and saved layer hashes unchanged. Evidence: `mcp_day5_validation/docs/blender_*.json`
and `blender_composer_preview.png`. Remaining integrations: CAD fixture, ROS delivery, extension enablement.

Composer follow-up, 2026-10-24: user opened the independent test project; Codex switched
the router to Composer and ran 14 successful direct-MCP checks (see
`mcp_day5_validation/docs/mcp_composer_20261024_231805.json`). Label preview/apply/undo,
graph/event-driven variant A -> B and capture worked. Own fixtures were removed; prim
count returned to 32; saved scene files remained unchanged. Router remains on Composer
9910; Isaac 9911 was offline. Blender addon connection is still unavailable.

## Latest direct MCP evidence, 2026-10-24

See `mcp_day5_validation/docs/MCP_OPERABILITY_RESULTS.md`: 42 calls across 33 tools
completed without call failures, including apply/observe/undo, event delivery, screenshot,
two-frame sequence and two-frame RGB SDG with dataset validation. A follow-up proved OBJ
conversion, ready camera data and actual robot motion from 0 to 24.763578 degrees through MCP.
PhysX runtime opinions made undo of the robot reference stale; the guard refused correctly,
and only the uniquely named test robot was explicitly deleted (preview first) and verified absent.
Saved scene-file hashes were unchanged. Exact runtime prim preservation is not proven.
Sensor use left Replicator active; the user subsequently stopped it with the guarded helper.
Labelled capture then passed through MCP: two RGB, segmentation and tight-box frames, with
matching visible crate pixels and in-bounds boxes. Cancellation and a follow-up job also passed,
with stable compared runtime paths on the initialized stage. Never bypass the active-run guard.
Composer launch and Blender connection are being handled manually by the user.
Final full suites on this candidate: 1,428 passed on USD 26.8 and 1,428 on USD 25.11.
The older activation checkpoint below records earlier states, not current prerequisites.

## Day 5 activation checkpoint, 2026-10-23

Latest live evidence: the user opened the independent test project and manually ran
`scripts/live_checks.py`. Nine checks passed: rubric, semantic labels, graph creation,
message delivery, graph evaluation, OBJ conversion, real articulation motion (0 -> 24.76
degrees), camera samples and viewport capture. SDG initially refused an already active
Replicator run. The user authorized stopping it and manually ran `scripts/retry_sdg.py`:
two 64x64 RGB frames were generated, dataset validation passed with zero issues, and scene
file hashes were unchanged. Reports are in `mcp_day5_validation/docs/`. Proprietary CAD,
ROS connectivity, Composer/Blender live checks and segmentation/bounding-box output are
still unverified; do not describe the entire remaining plan as complete.

- Day 5 source now registers 249 tools in total (21 visible in the default starter profile).
- Full suites: 1,428 passed on system USD 26.8 and 1,428 passed on installed Isaac USD 25.11.
- Live reload first preserved the old bridge because an asset-health test assumed OmniPBR.mdl could not resolve.
  The test now uses a synthetic MDL library name so it exercises the fallback in both environments; all 24
  asset-health tests passed on both USD versions after this test-only change.
- User manually reran the reload: result file confirms `reloaded`, 2026-10-23 00:27:10.
- With explicit user approval, Codex restarted only the verified router process: PID 120092 -> 104384.
  MCP initialization, catalog discovery (249 total, 21 visible), scene summary and the new semantic_audit
  succeeded. The anonymous World0.usd stage still has 14 prims. No scene was edited or switched.
- Activation evidence: `mcp_day5_validation/docs/activation-check.json`; later test runs are documented above.
- Composer/Blender integration and external CAD/ROS prerequisites remain unverified.
  Nothing from this Day 5 work has been pushed or merged into main.

## Prompt to paste into Codex

```
Read C:\Users\krngr\Omniverse\AGENTS.md, then OMNIVERSE_WORKSPACE_CONTEXT.md, then this file (HANDOFF_FOR_CODEX.md).
I am a Python beginner learning OpenUSD for the NCP-OUSD exam, using Isaac Sim 6.1 and USD Composer.
My custom MCP lives in omniverse_scene_mcp/ (router on http://127.0.0.1:9905/mcp).
Work on git branch wip/round7-9-handoff. Keep the dry-run-by-default rule for every scene edit.
You (Codex) built the original setup (kit-app-template live bridge, start scripts, AGENTS.md), so you may edit any of it where needed. Claude only added the newer modules in omniverse_scene_mcp/ since then.
Ask me before editing my own scene files (isaacsim-mentorship/**, working_scene.usda, course scenes), reloading Isaac/Composer bridges, restarting the router, pushing, or installing anything.
Step 1: run the full test suite (see below) and tell me the result.
Step 2: finish the unfinished list in this file, in order, smallest first.
Explain things simply, one step at a time.
```

## State at the original handoff (historical; see completion below)
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


## Codex completion, 2026-10-22

Work stayed on `wip/round7-9-handoff`. Original infrastructure, local user changes and submodules were preserved.
No user scene was edited. No software was installed. No live bridge was reloaded and no router was restarted.

1. **Baseline reproduced:** 1,384 passed, 8 failed. Fixed the joint test fixture (its docstring described an
   undriven elbow but the fixture added a drive), profile tests that mistook documentation for credentials, and
   missing pxr import guards. The test runner's Windows socketpair handling now retries an ephemeral-port
   collision without allowing connections to protected MCP ports. Time-dependent progress comparisons ignore
   the readout timestamp while still comparing the stored progress.
2. **Day 4 tests added:** robot imports, sensors, pick-and-place, ROS 2 checks and workflow prompts. Tests use
   synthetic files, temporary stages, fake Kit interfaces and allowlisted/fake ROS runners; generated scripts
   are compiled for syntax and never executed. Existing implementations were reused.
3. **Integration complete in source:** Group A was already largely wired (the original handoff understated this).
   `features.py` adds a reviewed function allowlist for Day 3/4, shared by server tools and main-thread Kit RPC
   `feature_call`; RPC version is `tier8-1`. `nl_tier8.py` adds conservative phrases and exact tool-name-plus-JSON
   requests. New edit phrases always preview. Help and journal registration include the new tools. The test
   transport exercises MCP -> HTTP client -> extended RPC -> temporary USD stage, including journal recording.
4. **Small profiles enabled:** default `starter` lists 21 tools; `all` lists 228; nine workflow prompts are
   registered. Always-on discovery can show exact schemas (`include_schema=True`), call hidden tools with normal
   validation, and expose tool groups. Optional compaction preserves discovery schemas and generated scripts.
   Only literal `dry_run=False` authorizes writes through public scene-edit tools, including legacy create/move/save.
5. **Docs and verification:** README, client/profile guide and the handoff cheatsheet section are updated.
   `tools/check_kit_usd.py` loads installed USD 25.11 and validator DLLs into a separate Python process, without
   launching Kit or modifying app configuration. Full-suite results are recorded below. Source/tests/docs are
   committed locally; unrelated earlier working-tree changes are excluded. Local shared context lessons remain
   in `OMNIVERSE_WORKSPACE_CONTEXT.md`, which was already untracked.

### Final verification

- System Python 3.12 + usd-core 26.8: **1,413 passed, 0 failed, 0 skipped**.
- Isaac Sim 6.1's installed USD 25.11: **1,413 passed, 0 failed, 0 skipped**.
- `git diff --check` passes for task files.
- Logs are local-only in `tmp/codex-test-run/verified-usd-26.8.log` and `verified-kit-25.11.log`.

### Pending explicit user approval

- Push this branch (do not merge or push `main`).
- Reload whichever live Isaac/Composer bridges the user wants to activate, then restart the router with the
  intended scene target and perform read-only checks. Neither bridge reload is an instruction to edit a scene.
- Live robot motion, converter jobs, actual sensor readings and ROS connectivity remain unverified in the app;
  offline tests establish code/temporary-stage behavior, not hardware or simulation success.

Next learning step after activation: inspect the scene read-only, then preview one change and read its plan.
Ask separately before applying a change to the user's authored scene.
