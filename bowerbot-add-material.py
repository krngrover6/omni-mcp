from pxr import Usd, UsdShade, UsdGeom, Sdf, Gf

scene_path = r"C:\Users\krngr\Downloads\Omniverse\bowerbot-scenes\learning_scene\scene.usda"
stage = Usd.Stage.Open(scene_path)

material = UsdShade.Material.Define(stage, "/Scene/Looks/RedMaterial")
shader = UsdShade.Shader.Define(stage, "/Scene/Looks/RedMaterial/PreviewSurface")
shader.CreateIdAttr("UsdPreviewSurface")
shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0.9, 0.05, 0.05))
shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.4)
shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(0.0)
material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")

cube_prim = stage.GetPrimAtPath("/Scene/MyCube")
UsdShade.MaterialBindingAPI.Apply(cube_prim).Bind(material)

stage.GetRootLayer().Save()
print("Red material bound to /Scene/MyCube")
