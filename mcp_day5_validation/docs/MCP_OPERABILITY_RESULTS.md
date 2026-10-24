# Direct MCP operability check — 2026-10-24

## Latest follow-up

- `mcp_remaining_20261024_225103.json`: actual robot movement 0 -> 24.763578 degrees,
  ready camera samples and OBJ conversion worked through MCP. The temporary robot was
  removed explicitly after stale undo safely refused (see `mcp_robot_cleanup.json`).
- After the user stopped Replicator manually, `mcp_labels_20261024_225812.json` captured
  two RGB, two segmentation and two tight-box frames at 256x256 through MCP. File checks
  passed for all modalities. `mcp_annotation_quality.json` also confirms 115 visible crate
  pixels per frame, class `crate`, the expected geometry path and an in-bounds box
  [134,123,145,134] enclosing the mask. Occlusion ratio -1 means unavailable, not measured.
- The raw label report's `labels_restored:false` compared the entire audit, including the
  changing runtime prim count (30 -> 39). The quality report checks semantic fields and
  confirms their restoration. The raw report remains intact; the client now distinguishes
  entire-audit equality from semantic restoration.
- `mcp_cancel_cleanup_20261024_230006.json`: preview did not cancel; explicit cancellation
  succeeded; a fresh one-frame job completed and validated. No paths were added or removed
  under World, Render, Replicator or Orchestrator in this already-initialized run. This does
  not remove initial runtime setup or prove zero GPU memory leaks.
- `mcp_positive_image_comparison.json`: RGB versus segmentation gives a positive difference;
  the earlier self-comparison gives zero difference.
- `mcp_generated_artifacts.json`: MCP extension creation wrote parseable Python/TOML, and
  generated Blender/SDG scripts parse. Enabling the extension and Blender execution remain separate.
- Final full suites: 1,428 passed on USD 26.8 (159.7 s); 1,428 on USD 25.11 (227.1 s).

Composer follow-up `mcp_composer_20261024_231805.json` passed all 14 checks through MCP:
stage/rubric/graph/variant reads, label preview/apply/undo, variant creation, graph creation,
event delivery, actual selection MCPTest=B, and viewport capture. Temporary cube and graph
were removed; prim count returned to 32 and saved scene files stayed unchanged. The router
currently targets Composer on 9910; Isaac's bridge was offline at the connection check.

Blender follow-up: Blender 5.2.2 LTS connected through its existing protocol-13 addon.
No update/install was performed. The MCP-generated export script previewed, then exported
a separate original 2x1x1 m green box. The original Blender scene/objects were restored;
the additional `MCP_USD_Validation` test scene is retained for review and no .blend file was saved.
`blender_usd_verification.json` confirms a real mesh, Z-up, metre units, expected dimensions,
material binding and the expected Preview Surface color. Composer then loaded the USD through
MCP with the same bounds and material. The first capture stalled while Composer was behind
Blender; it succeeded with Composer visible. A dedicated visual check temporarily hid the
existing test crate and placed the exported box at its position, verified its green material,
then undid both edits. Scene summary and saved-file hashes were restored.

Evidence: `blender_export_plan_live.json`, `blender_live_export.json`,
`blender_usd_verification.json`, `blender_composer_handoff.json` (initial capture limitation),
`blender_composer_capture_retry.json`, `blender_visual_check.json`, and
`blender_composer_preview.png`. The tiny exported asset is under `assets/blender_mcp_20261024_232131/`.

Generated-extension follow-up: `extension_live_20261024_233954.json` passed all nine live
checks. The extension enabled, created its UI, and the user's actual Inspect stage click
displayed the correct stage identifier. It then disabled, hid/released its window, cleared
widget references, removed its test search path and restored the original enabled-extension
set. Scene and extension files stayed unchanged during the successful run.

The first attempts found a helper assumption about Kit clearing instance attributes and a
real shutdown gap: the generated window needed to be hidden before release. Both generator
and test copy were fixed; 16 focused tests pass on USD 26.8 and 25.11, including a new shutdown
regression. These focused results supplement the earlier full-suite baseline of 1,428;
the complete suite was not rerun after this small lifecycle fix. The updated generator is
on disk; the running router needs an approved restart before newly generated templates use it.

Still pending: proprietary CAD and ROS connectivity.
The sections below record earlier checkpoints, not the latest coverage.

Codex ran the external MCP client `scripts/mcp_operability.py` against the user's running
router at port 9905 and live Isaac bridge at 9911. The user opened the dedicated test
project manually. No Script Editor script was needed for these checks.

Evidence: `mcp_operability_20261024_224501.json` (42 successful calls, zero call failures).
Default discovery returned 21 visible tools; full discovery returned 249 registered tools.
This is representative coverage, not validation of all 249 tools.

## Verified through MCP

- Discover hidden tools and call them through `router_call_tool`.
- Query real scene capabilities, variants, graphs, sensors, animation and runtime frame stats.
- Retrieve robot/sensor catalogs, graph recipes and import format information.
- Inspect ROS readiness; this does not establish ROS connectivity.
- Evaluate a structural rubric on the actual live stage.
- Preview semantic labels, apply a real test label, observe it, undo it and confirm the complete
  semantic-audit result returns to its previous value.
- Start a listener, send a unique message with payload `probe=42`, confirm delivery, and release it.
- Preview SDG, extension generation and image-sequence requests.
- Capture a real viewport screenshot and poll until its file is ready.
- Capture a two-frame 64x64 viewport sequence, poll completion, and compare an image with itself
  (zero changed pixels). This is a basic comparison check, not a positive difference test.
- Start a real two-frame 64x64 RGB Replicator job through MCP, poll completion, and validate its
  output through MCP: exactly two valid RGB images, zero reported issues.
- Revalidate the earlier manual-run dataset through MCP.

## Preservation and limitations

- Test-project root/layer files remained byte-identical on disk; nothing was saved.
- The label edit was undone and semantic readout restored.
- The live summary was NOT identical: prim count increased from 30 to 34 during rendering/SDG.
  Read-only follow-up found renderer, Replicator and Orchestrator runtime prims. There was no
  before/after full-path snapshot to attribute the exact four prims. Runtime cleanup/isolation
  needs further checking; do not claim the live stage was completely unchanged.
- This run did not create a robot or command robot movement through MCP. Earlier robot motion
  evidence came from the user's manually run Script Editor helper.
- Blender, Composer, proprietary CAD, ROS message delivery, segmentation/bounding-box output,
  enabling the generated extension and every other uncalled tool remain unverified here.
- An API success or catalog response is not evidence that all underlying workflows work.

## Conclusion

Follow-up `mcp_remaining_20261024_225103.json` proves direct MCP OBJ conversion, robot
drive commands and actual PhysX motion from 0 to 24.763578 degrees, plus a ready 64x64
camera sample. The subsequent stale-undo refusal was preserved; only the uniquely named
test robot was explicitly removed through MCP (preview then apply), verified absent in
`mcp_robot_cleanup.json`. Scene files stayed unchanged on disk. The sensor run again left
Replicator STARTED, blocking the annotation capture until it is explicitly stopped.

Codex can operate representative Claude-added tools through the MCP interface, including actual
Kit actions and artifact creation, without the user running helper code. Remaining work should
focus on the explicitly untested workflows rather than counting tools.
