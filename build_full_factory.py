from pxr import Usd, Sdf

base = r"C:\Users\krngr\Downloads\OpenUSD_Lab-Assets\OpenUSD_Lab-Assets\OpenUSD_Lab\Assemblies"
out_path = base + r"\Factory_Complete.usda"

stage = Usd.Stage.CreateNew(out_path)
stage.SetMetadata("upAxis", "Z")
stage.SetMetadata("metersPerUnit", 0.01)

world = stage.DefinePrim("/World", "Xform")
stage.SetDefaultPrim(world)

def add_ref(name, rel_path):
    scope = stage.DefinePrim(f"/World/{name}")
    scope.GetReferences().AddReference(rel_path)

add_ref("Factory", "./Factory.usda")
add_ref("Walkways", "./Factory_Floor_Walkways.usda")
add_ref("HoodRack", "./HoodRack.usda")
add_ref("Lockers", "./Lockers.usda")

stage.GetRootLayer().Save()

# Hide the roof so the colorful interior is visible from outside (diorama-style cutaway)
roof_prim = stage.GetPrimAtPath("/World/Factory/Roof")
if roof_prim:
    roof_prim.SetActive(False)
    stage.GetRootLayer().Save()

print("Wrote:", out_path)
