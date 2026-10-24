# MCP completion plan

Prepared 2026-10-24. Branch: `wip/round7-9-handoff`. No merge into `main`.

## Goal

Make the existing toolkit dependable for beginner OpenUSD study and small projects.
Prioritize reproducible behavior and clear instructions over increasing the tool count.
This document plans future work; it does not authorize installations, service changes,
pushes or edits to existing user scenes.

## Verified starting point

- 249 registered tools, 21 visible in the default starter profile.
- Last complete automated runs: 1,428 passed on USD 26.8 and 1,428 on Isaac's installed USD 25.11.
  The subsequent MDL test-fixture correction passed all 24 asset-health tests on both versions.
- Isaac bridge reload and router activation were verified on October 23; recheck availability
  next session rather than assuming the processes are still running.
- Nine live checks passed: rubric, semantic labels, graph creation, message delivery,
  graph evaluation, OBJ conversion, robot motion, camera samples and viewport capture.
- A separately authorized Replicator retry generated two 64x64 RGB frames; file checks passed.
- Test-project scene files were unchanged on disk. Temporary live edits were confined to its session layer.
- Latest work is not yet committed/pushed. Last recorded commit: `ecf9fe7`.
- Composer, Blender, ROS delivery, proprietary CAD, actual label/box outputs, sequence rendering,
  and enabling the generated extension still need their own evidence.

Evidence: `mcp_day5_validation/docs/activation-check.json`,
`live_checks_20261023_011103_241ffd.json`, `sdg_retry_20261023_011555_a055a9.json`.

## Work order

2026-10-24 update: direct MCP operability testing completed 42 successful calls with zero call
failures, including apply/observe/undo, event delivery, viewport/sequence capture and real RGB
SDG plus dataset validation. See `mcp_day5_validation/docs/MCP_OPERABILITY_RESULTS.md`.
Saved scene files and semantic readout were restored, but live prim count rose 30 -> 34;
add runtime-prim cleanup/isolation verification before claiming exact live-stage preservation.
Follow-up: robot motion, camera samples and conversion also passed through MCP. Two-frame
labelled SDG passed with matching class/mask/box content, not only file checks. Cancellation
and a follow-up job passed; an initialized repeat run left the compared runtime paths unchanged.
Composer subsequently passed 14 direct MCP checks, including event-driven variants,
label apply/undo and capture; own fixtures were removed with unchanged scene files.
Blender, ROS/CAD and enabling the extension remain outstanding.

### 1. Preserve the completed work

1. Review the diff and identify only this task's source, tests, docs and tiny original fixtures.
2. Reconcile stale README/handoff statements with the actual live reports.
3. Exclude runtime logs, datasets, local credentials, unrelated changes and submodule edits.
4. Run relevant checks and both full suites on the final candidate; record exact results.
5. Create a local commit on the existing work branch. Ask separately before pushing.

Done when: the completed implementation and evidence are recoverable in a reviewed local commit,
with no unrelated files included. Do not wait for ROS/CAD setup to preserve finished work.

### 2. Finish the small Isaac validation gaps

Use the independent warehouse test project and short, separate scripts with visible start,
progress, errors and report paths. The user prefers running Script Editor scripts manually.

1. Label one known object in a disposable session layer; request RGB, semantic segmentation
   and tight bounding boxes for two frames.
2. Check all modality files and matching frame IDs, then confirm the expected class ID and
   a valid box for an object actually visible in the chosen camera. Validate image bounds;
   file integrity alone does not establish correct annotations.
3. Exercise a short render sequence, confirm image count/dimensions, and verify timeline restoration.
4. Compare two deliberately different captures and confirm a meaningful difference is reported.
5. Exercise cancellation and refusal paths on owned jobs only; preserve partial outputs for diagnosis.
6. Keep Replicator's active-run guard. Inspect state and obtain approval before stopping an
   existing run; ordinary capture must not silently stop other work.

Done when: actual files, annotation checks and cleanup/restoration evidence pass. Any required
fix gets a focused regression test; failures stay visible rather than being relabelled as passes.

### 3. Validate Blender -> USD

1. Inspect the existing Blender addon/bridge and version without installing anything.
2. If disconnected, explain the smallest reconnection step; ask before installing or reconfiguring it.
3. Use a separate saved test project, after protecting any unsaved Blender work.
4. Export a simple selected object with a material into a fresh folder using the generated script.
5. Open the USD read-only and check geometry, material, default prim, units, axes and asset paths.

Done when: a real Blender export opens correctly in USD with the expected physical dimensions.
Producing a script or receiving a bridge response is not enough.

### 4. Validate Composer and the extension starter

1. Ask before launching/reloading its bridge or changing the shared router target.
2. Reuse the existing Composer build and a separate test scene; protect unsaved work.
3. Verify inspection, preview/apply/undo, an event-driven variant and viewport capture.
4. Generate the extension in a fresh project folder, inspect it, then ask before enabling it.
5. Verify its stage-inspection button and clean shutdown; document the actual Composer version.
6. Record which app the router targets and restore the agreed target after testing.

Done when: the shared workflow works in Composer and the extension loads/unloads without errors.
Robot physics remains an Isaac check, not a Composer requirement.

### 5. Demonstrate one small project end to end

Extend the independent warehouse validation project, not the user's homework.

1. Create/inspect the layered project through MCP, preview first.
2. Import the verified Blender asset and confirm its units and paths.
3. Preview one variant or layout change, apply it deliberately, and prove undo works.
4. Add semantic labels and generate a small checked dataset plus a screenshot.
5. Produce a project report with links to artifacts and exact pass/fail/blocked results.
6. Document a fresh-session reproduction procedure and verify the saved project can reopen.

Done when: the user can repeat the workflow from the instructions without hidden setup.
Record which steps use MCP and which require manual app interaction.

### 6. Optional integration track: CAD and ROS

These are separate prerequisites, not reasons to delay the core toolkit.

- CAD: obtain an appropriate licensed STEP/IGES or other supported CAD fixture; verify the
  installed converter supports that format; test geometry, units and output. OBJ is not a CAD proof.
- ROS: first agree on the intended robot/topic workflow. Inspect installed components and propose
  the smallest local publisher/subscriber setup. Ask before enabling extensions, changing startup
  configuration, installing dependencies or starting extra services. Verify actual message delivery.

Done when: real conversion or real ROS delivery is observed, respectively. If prerequisites are
unavailable, document the exact blocker and keep the rest of the release independent.

### 7. Final review and handoff

1. Run appropriate regression checks and both full suites after final source changes.
2. Verify the starter profile, schema discovery, default previews and error messages from both clients
   when available; do not assume Codex and Claude share conversation memory or active tool discovery.
3. Review the local commits, update beginner instructions and list remaining limitations explicitly.
4. Ask before pushing the branch. Leave `main` untouched.

Done when: code, live evidence and instructions agree, the work is committed, and optional blockers
are clearly separated from verified capabilities.

## Working rules and session format

- One primary app/test group per session; begin with runtime and unsaved-work checks.
- Preview scene edits by default. Existing homework/course scenes require specific approval.
- No install, bridge reload, router restart/reconfiguration or push without the user's approval.
- Keep new projects separate; never publish generated datasets or large assets accidentally.
- Do not invent completion percentages from tool/test counts. Report verified, unverified and blocked.
- End each session with: what passed, what changed, what remains, and one concrete next action.
- Next action: review and locally commit the already completed Day 5 work, then test semantic and box outputs.
