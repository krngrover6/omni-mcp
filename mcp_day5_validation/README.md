# mcp_day5_validation

Validate Day 5 tools on a new independent warehouse project; preserve existing user scenes.

Template: **Warehouse study scene** (Isaac Sim or USD Composer). A study warehouse: a floor, a crate to practice moving, a racks group for Isaac warehouse assets, materials, a sky and sun, and an overview camera.

## Open the scene

- Isaac Sim: **File > Open** and choose `root.usda` in this folder.
- Python (read-only check): `python scripts/inspect_scene.py` with a Python that has USD (`pxr`).

## Units and axes

upAxis Z, metersPerUnit 1: 1 unit = 1 meter.

## Layer stack (strongest first)

1. `root.usda` (strongest): stage metadata and the sublayer list only. No prims.
2. `layers/layout.usda`: Floor, racks group, crate and camera: what is in the warehouse and where it sits.
3. `layers/materials.usda`: Material prims under /World/Looks and the material:binding relationships on geometry.
4. `layers/lighting.usda`: The sky (DomeLight) and sun (DistantLight) under /World/Lights.

Why this order: see `docs/layer_stack.md`.

## Folders

| Folder | What goes in it |
|---|---|
| `assets/` | Source assets (USD, textures). Record each asset's source and license in PROJECT_CONTEXT.md. |
| `layers/` | The sublayers of root.usda, one per concern. |
| `scenes/` | Extra entry scenes (shots, variations) that sublayer ../root.usda. |
| `renders/` | Rendered images and synthetic data output (ignored by git). |
| `scripts/` | Python helpers for this project. |
| `docs/` | Notes and explanations for this project. |

## First steps

1. Open `root.usda` in Isaac Sim.
2. Open the Layer window (the Layer tab next to Stage) and look at the sublayers under root.usda.
3. Right-click `layers/layout.usda` > **Set Authoring Layer** before you edit, so edits do not land in root.usda.
4. Reference a rack asset under /World/Racks (Composer: drag a USD file from the Content browser onto /World/Racks with layout.usda as the authoring layer).
5. Look through the Overview camera: viewport camera menu > Cameras > Overview.
6. Keep `PROJECT_CONTEXT.md` up to date: assets, decisions, lessons, next steps.
