from pxr import Usd, Sdf, Gf

path = r"C:\Users\krngr\Downloads\OpenUSD_Lab-Assets\OpenUSD_Lab-Assets\OpenUSD_Lab\Assemblies\Factory_Complete.usda"
assets_base = "../Assets"

stage = Usd.Stage.Open(path)

props = stage.DefinePrim("/World/ColorProps", "Xform")

def place(name, asset_file, x, y, z=0, rot_z=0, scale=1.0):
    prim = stage.DefinePrim(f"/World/ColorProps/{name}")
    prim.GetReferences().AddReference(f"{assets_base}/{asset_file}")
    from pxr import UsdGeom
    xformable = UsdGeom.Xformable(prim)
    xformable.ClearXformOpOrder()
    xformable.AddTranslateOp().Set(Gf.Vec3d(x, y, z))
    if rot_z:
        xformable.AddRotateZOp().Set(rot_z)
    if scale != 1.0:
        xformable.AddScaleOp().Set(Gf.Vec3f(scale, scale, scale))

# Blue beams along the left open aisle, scaled up to actually read at this scale
for i, y in enumerate([-2400, -1200, 0, 1200, 2400]):
    place(f"BlueBeam_{i}", "BlueBeamA_01.usda", -1100, y, 0, 90, scale=6.0)

# Yellow metal fencing along the right open aisle
for i, y in enumerate([-2000, -600, 800, 2200]):
    place(f"Fencing_{i}", "MetalFencing_A1.usda", 1300, y, 0, 0, scale=6.0)

# Yellow ramps near an entrance
place("Ramp_01", "RampLong_A1.usda", 0, -2900, 0, 0, scale=4.0)
place("Ramp_02", "RampShort_A1.usda", -600, -2900, 0, 0, scale=4.0)

stage.GetRootLayer().Save()
print("Added color props.")
