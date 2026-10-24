# assets/

Put source assets here: USD files, textures, CAD conversions.

Validation fixtures authored locally for this project:

- `triangle.obj`: three vertices and one triangle; tests OBJ conversion, not proprietary CAD.
- `mcp_probe_robot.usda`: two cubes, a world-fixed base and one driven revolute joint;
  tests PhysX movement through MCP without downloading a robot.
- Converted subfolders are generated test output; they are not source fixtures or required assets.
- `blender_mcp_20261024_232131/asset.usdc`: original 2x1x1 m green box created in an isolated
  Blender test scene and exported using the custom MCP's generated script; verified in Composer.

- Record each asset's source and license in `../PROJECT_CONTEXT.md` (Assets and licensing).
- Reference assets with relative paths. A path is relative to the layer that authors it, so from
  `layers/layout.usda` an asset here is `../assets/<file>.usd`.
- Check an asset's upAxis and metersPerUnit before referencing it: USD does not convert them for you.
