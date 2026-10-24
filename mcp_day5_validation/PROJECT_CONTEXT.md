# Project Context: mcp_day5_validation

## Goal

Validate Day 5 tools on a new independent warehouse project; preserve existing user scenes.

## Independence

This project reuses the shared infrastructure of the Omniverse workspace (MCP router, Composer build, launchers, OpenUSD lessons), but it is separate from other demos such as `mini_omk_demo`.

## Entry points

- Project folder: `mcp_day5_validation/`
- Main scene: `root.usda`
- Template: `warehouse` (Warehouse study scene, Isaac Sim or USD Composer)
- Project configuration/DataTable: none yet
- Custom extension/scripts: `scripts/inspect_scene.py` (read-only check)

## Assets and licensing

| Asset | Source | License | Local path | Notes |
|---|---|---|---|---|
| (none yet) | | | `assets/` | Record every asset you add here. |
| Triangle fixture | Authored locally by Codex for this project | Original test fixture; no third-party content | `assets/triangle.obj` | OBJ converter smoke test |
| One-joint robot | Authored locally by Codex for this project | Original test fixture; no third-party content | `assets/mcp_probe_robot.usda` | Direct MCP motion test |

## Stage map

| Prim | Type | Authored in | Purpose |
|---|---|---|---|
| `/World` | Xform | layout.usda | Default prim of the warehouse. |
| `/World/Environment` | Xform | layout.usda | Floor, walls and other static surroundings (a component model, so its geometry sits inside a model). |
| `/World/Environment/Floor` | Mesh | layout.usda, materials.usda | A 30 m x 30 m floor quad. Replace it with an Isaac warehouse environment reference later. |
| `/World/Racks` | Xform | layout.usda | Reference Isaac warehouse racks here, for example Isaac/Environments/Simple_Warehouse/Props/. |
| `/World/Racks/Crate_01` | Xform | layout.usda | A crate asset to practice selecting, moving and duplicating: move this Xform, not its Geom. |
| `/World/Racks/Crate_01/Geom` | Cube | layout.usda, materials.usda | The crate's 1 m geometry. |
| `/World/Cameras` | Xform | layout.usda |  |
| `/World/Cameras/Overview` | Camera | layout.usda |  |
| `/World/Looks` | Scope | materials.usda | Material library for the scene. |
| `/World/Looks/Concrete` | Material | materials.usda |  |
| `/World/Looks/Cardboard` | Material | materials.usda |  |
| `/World/Lights` | Xform | lighting.usda |  |
| `/World/Lights/Sky` | DomeLight | lighting.usda |  |
| `/World/Lights/Sun` | DistantLight | lighting.usda |  |

## Layer stack

Strongest to weakest:

1. `root.usda` (strongest): stage metadata and the sublayer list only. No prims.
2. `layers/layout.usda`: Floor, racks group, crate and camera: what is in the warehouse and where it sits.
3. `layers/materials.usda`: Material prims under /World/Looks and the material:binding relationships on geometry.
4. `layers/lighting.usda`: The sky (DomeLight) and sun (DistantLight) under /World/Lights.

Strongest to weakest: layout, materials, lighting. Each layer owns different prims, so order only matters when two layers author the same property. Then the stronger one wins without deleting the weaker opinion. Putting layout on top keeps placement in charge.

## Axes, units, and pivots

- Up axis: Z
- Forward axis: not decided yet; inspect each imported asset instead of assuming one.
- Meters per unit: 1 (1 unit = 1 meter)
- Important pivots/joints: none yet.

## Materials and bindings

- Material `/World/Looks/Concrete` (UsdPreviewSurface) in `layers/materials.usda`
- Material `/World/Looks/Cardboard` (UsdPreviewSurface) in `layers/materials.usda`
- `/World/Environment/Floor` binds `/World/Looks/Concrete` in `layers/materials.usda`
- `/World/Racks/Crate_01/Geom` binds `/World/Looks/Cardboard` in `layers/materials.usda`

## Animation

None yet. timeCodesPerSecond is not authored (USD's default is 24).

## UI and MCP behavior

- Composer/Isaac viewport: select /World/Racks/Crate_01 and move it (W key).
- MCP router: scene_inspect('/World'), then dry-run moves on Crate_01.
- Safe MCP use: inspect read-only first (scene_summary, scene_inspect), then use dry_run=True before any mutation.

## Validation checklist

- [ ] Scene opens after restart
- [ ] Referenced assets resolve
- [ ] Texture paths resolve
- [ ] Expected variants exist
- [ ] Axes/pivots verified
- [ ] Animation ranges verified
- [ ] Router inspection works
- [ ] Project-only facts documented here
- [ ] Reusable lesson copied to shared workspace context

## Decisions and lessons

- 2026-10-22: Created from the `warehouse` template by the project starter. upAxis Z, metersPerUnit 1: 1 unit = 1 meter.
- 2026-10-22: Layer order (strongest first) is layout.usda, materials.usda, lighting.usda; see `docs/layer_stack.md`.
- A material binding is a relationship from geometry to a material prim; it lives in materials.usda, not on the mesh's own layer.
- Bindings need MaterialBindingAPI applied on the bound prim; the starter authors both in the same layer.

## Current status and next steps

Complete: folder layout, root layer with stage metadata, the layer stack and the starter prims. Not done yet: real assets, project controls and everything in the validation checklist.

1. Reference a rack asset under /World/Racks (Composer: drag a USD file from the Content browser onto /World/Racks with layout.usda as the authoring layer).
2. Look through the Overview camera: viewport camera menu > Cameras > Overview.


## Day 5 validation record (2026-10-23)

Latest update (2026-10-24): labelled SDG passed through MCP, including two files per modality
and actual crate-mask/box checks. Cancellation and subsequent job execution passed. See
`docs/MCP_OPERABILITY_RESULTS.md` for evidence and the distinction between label restoration
and changing renderer prim counts. The saved baseline root layer now contains Kit runtime
metadata/prims from manual application saves; it is preserved, not silently cleaned or rewritten.
The original template's metadata-only description below is historical.

2026-10-24 direct-MCP follow-up: see `docs/MCP_OPERABILITY_RESULTS.md` and the JSON
reports. Actual robot movement, camera samples, OBJ conversion, labels/undo, events,
screenshots, a sequence and RGB SDG are now verified through MCP itself. The robot
was explicitly cleaned up after simulation invalidated its undo snapshot. The guard
was not bypassed. Semantic/box output subsequently passed after the explicit Replicator stop.
Extension files were generated through MCP and parsed as Python/TOML, but not enabled.
Blender and SDG example scripts returned through MCP also parse; execution in Blender is pending.

Live run `docs/live_checks_20261023_011103_241ffd.json` completed: rubric, semantic labels,
Action Graph creation/evaluation, message delivery, OBJ conversion, actual robot motion,
camera readings and viewport capture all passed. Scene-file hashes remained unchanged.
SDG correctly refused to start because Replicator reported an active run; no SDG files
were written, and dataset validation therefore did not run. Do not bypass this guard
or silently stop an existing Replicator job. Proprietary CAD and ROS connectivity remain
blocked by missing fixtures/setup. Composer and Blender live validation remain pending.

Authorized retry `docs/sdg_retry_20261023_011555_a055a9.json` subsequently passed:
Replicator transitioned STARTED -> STOPPED, then BasicWriter generated two 64x64 RGB
frames. The dataset checker found exactly two valid RGB frames and zero issues. Scene
files remained byte-identical. This validates RGB capture only; segmentation and bounding
box output were not requested or tested in this retry.

- New independent project created through the real shared MCP: preview, create, read saved-file stage info,
  run USD exam validation, and compare the live scene summary before/after. All six calls passed;
  the active scene did not change. Transcript: `docs/e2e-before-activation.json`.
- Custom rubric: `docs/custom_rubric.json`. Locally authored conversion fixture: `assets/triangle.obj` (no third-party asset).
- Live runner: `scripts/live_checks.py`. It refuses any active stage except this project's `root.usda`.
  Temporary scene edits use an anonymous session sublayer removed after the run. Scene files are never saved.
  It records pass/fail/blocked separately for events/graphs, conversion, robot motion, actual camera samples,
  viewport capture, Replicator frames, dataset QA and ROS readiness/connectivity.
- Captures/datasets belong under `renders/` and are gitignored. Live reports go under `docs/`.
- Initial runtime probe: Isaac bridge tier8-1 answered; current anonymous stage had unsaved edits.
  Composer bridge and Blender addon did not answer. No scene switch has been performed.
- Pending: user's saved/closed confirmation, bridge/router activation approval, then live checks.
- Limitations: no proprietary CAD fixture or configured local ROS loopback; these cannot be marked passed from OBJ
  conversion or a ROS environment check. Basic SDG captures the existing scene; automatic domain randomization
  is not part of this first implementation.

Next lesson: preview one change, explain the plan, and compare the dataset files with the requested frame count.
