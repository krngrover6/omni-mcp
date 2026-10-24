# Day 5 tools

The router now has 249 tools; the default profile still lists only 21. Use
`router_list_tools(pattern="sdg_*", include_schema=True)` to discover exact arguments, then
`router_call_tool(name="sdg_plan", arguments={...})` to call a hidden tool.

## Preview, apply, observe

Every writer previews by default. Only literal JSON `false` in `dry_run` authorizes a write.
Generated scripts also start with `DRY_RUN = True`. Writing an extension folder does not install or enable it.
Outputs go into a newly created folder under the explicit project directory; existing folders are refused.
For renders and datasets, pass the project's `renders/` as `project_dir` to keep generated output out of git.

| Area | Tools | Behavior |
|---|---|---|
| Custom rubric | `rubric_template`, `rubric_check` | Weighted prim/type/count/property/metadata comparisons; stage reads only, no executable expressions |
| Capture | `render_plan`, `render_capture` | Reuses active viewport capture; no camera/renderer changes |
| Sequences | `render_sequence_plan`, `render_turntable_plan`, `render_sequence`, `render_job_status`, `render_cancel` | Up to 60 samples of the stopped timeline; restore time on completion/failure |
| Comparison | `render_compare` | Exact changed-pixel percentage and 4x4 regions for RGB/RGBA PNGs up to one megapixel |
| Blender handoff | `blender_export_plan`, `blender_handoff_check` | Selection-only export script and read-only USD metadata/dependency inspection |
| Kit extension | `extension_create` | New Python/TOML skeleton with one read-only stage-inspection button |
| Semantics | `semantic_audit`, `semantic_labels` | Modern class labels, unlabelled geometry report, preview and one snapshot/undo transaction |
| Synthetic data | `sdg_plan`, `sdg_script`, `sdg_start`, `sdg_status`, `sdg_cancel` | Bounded BasicWriter RGB, semantic-segmentation and tight-box output; 1–100 frames, max 2048 per side |
| Dataset checks | `dataset_check` | Read-only frame counts/IDs, PNG CRC and bounded decompression, numeric NPY layout and JSON validity |

## Tiny dataset example

1. Inspect `semantic_audit` on the test project.
2. Preview `semantic_labels` with `mapping={"/World/Racks/Crate_01/Geom": "crate"}`. Apply separately only after review.
3. Call `sdg_plan` with the project `renders/`, a fresh run name, camera `/World/Cameras/Overview`,
   `frames=3`, `width=320`, `height=240`, `seed=42`, and `annotations=["rgb"]`.
4. In an approved live session, call `sdg_start` with the same arguments and `dry_run=False`.
5. Poll `sdg_status(job_id)` and then run `dataset_check` on its output directory with `expected_frames=3`.

The seed controls Replicator RNG; it does not guarantee bit-identical GPU images. This first implementation captures
the existing scene and does not add automatic domain randomization. Class labels are required for useful label/box data.
PNG comparison supports non-interlaced 8-bit RGB/RGBA; dataset validation also accepts other bounded non-interlaced PNG
bit depths. NPY object/pickle data is always refused. A file scan reaching its limit cannot pass.

## What passing tests mean

Offline tests use synthetic files, temporary USD stages, fake Kit interfaces and in-process transports. They check
preview defaults, no overwrite, script syntax, frame coverage, rollback, undo, asynchronous cleanup and timeline restoration.
They do not prove real rendering, robot motion, ROS delivery or proprietary CAD conversion.

The independent `mcp_day5_validation/` warehouse was created through the running MCP, preview first. Its
`docs/e2e-before-activation.json` records that workflow and confirms the active Isaac stage did not change.
`scripts/live_checks.py` runs only when that project's `root.usda` is already open; it writes a report with
separate pass/fail/blocked statuses. Temporary scene edits stay in a disposable session sublayer. It never reloads a
bridge, restarts a router, saves a scene, or silently switches the active project.

Live Isaac checks passed both through manually run scripts and the MCP client, including robot motion,
camera readings, events, OBJ conversion, capture/sequence output and two-frame RGB SDG. Evidence and limitations:
`mcp_day5_validation/docs/MCP_OPERABILITY_RESULTS.md`. Simulation can invalidate edit undo snapshots;
the guard refuses rather than overwriting changes. Camera/Replicator interaction and runtime prim cleanup
were checked further: labelled output has matching crate masks/boxes; cancellation and a fresh job
passed with stable runtime paths on an initialized stage. Composer, Blender, proprietary CAD,
ROS delivery and enabling the generated extension remain pending.

## API references

- [Replicator BasicWriter example](https://docs.omniverse.nvidia.com/extensions/latest/ext_replicator/basic_functionalities.html)
- [Viewport capture API](https://docs.omniverse.nvidia.com/kit/docs/omni.kit.viewport.utility/latest/omni.kit.viewport.utility/omni.kit.viewport.utility.capture_viewport_to_file.html)
- [Blender USD export options](https://docs.blender.org/api/current/bpy.ops.wm.html)

Installed sources verified for Isaac 6.1: `extscache/omni.replicator.core-1.13.36+110.3.0.wx64.r.cp312/`
(`scripts/orchestrator.py`, `scripts/create.py`, `scripts/writers_default/basicwriter.py`,
`scripts/functional/modify.py`, `tests/test_semantic_updates.py`). Capture delegates reuse
`omniverse_scene_mcp/kit_live_extras.py`; the extension follows Kit's `IExt` startup/shutdown pattern.

Next lesson: inspect a three-frame RGB dataset and explain why successful rendering alone does not prove correct labels.
