from pxr import Usd, UsdGeom, Gf

scene_path = r"C:\Users\krngr\Downloads\Omniverse\bowerbot-scenes\learning_scene\scene.usda"
stage = Usd.Stage.Open(scene_path)

cube = UsdGeom.Cube.Define(stage, "/Scene/MyCube")
cube.GetSizeAttr().Set(100.0)
cube.AddTranslateOp().Set(Gf.Vec3d(0, 0, 50))

stage.GetRootLayer().Save()
print("Cube added at /Scene/MyCube")
