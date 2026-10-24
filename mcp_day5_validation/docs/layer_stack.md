# Layer stack of mcp_day5_validation

`root.usda` sublayers the files below. In a layer stack, an opinion in a stronger layer wins over the same opinion in a weaker one; the weaker opinion is not deleted, it is just not used. When two layers author different things (one defines a prim, another adds a material binding), both apply.

| Strength | File | Owns | Why here |
|---|---|---|---|
| 0 | `root.usda` | Stage metadata (upAxis, metersPerUnit, defaultPrim) and the list of sublayers. No prims. | Strongest file. Composer saves edits here unless you pick another authoring layer, so it starts empty: anything that lands here by accident is easy to spot and move. |
| 1 | `layers/layout.usda` | Floor, racks group, crate and camera: what is in the warehouse and where it sits. | Strongest of the department layers: placement is what you change most, and if another layer ever authors a transform by mistake, layout still wins. |
| 2 | `layers/materials.usda` | Material prims under /World/Looks and the material:binding relationships on geometry. | Looks only add bindings and shaders; they should never move anything, so they sit below layout. |
| 3 | `layers/lighting.usda` | The sky (DomeLight) and sun (DistantLight) under /World/Lights. | Weakest: lights own their own prims and rarely conflict. Keeping them separate lets you swap a whole lighting setup by replacing one file. |

## Why this order

Strongest to weakest: layout, materials, lighting. Each layer owns different prims, so order only matters when two layers author the same property. Then the stronger one wins without deleting the weaker opinion. Putting layout on top keeps placement in charge. The root layer lists its sublayers strongest first; the session layer (unsaved, in memory) sits above all of them.

## Tips

- Composer and Isaac Sim save edits into the current authoring layer (root.usda unless you change it). Right-click a layer in the Layer window > Set Authoring Layer to choose where edits go.
- Paths inside a layer are relative to that layer's own file: from `layers/layout.usda`, an asset in `assets/` is `../assets/<file>.usd`.
- Muting a layer in the Layer window hides its opinions without deleting them, a quick way to see what it contributes.
