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

Still pending: Blender, proprietary CAD, ROS connectivity and enabling the generated
extension. The sections below record earlier checkpoints, not the latest coverage.

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
