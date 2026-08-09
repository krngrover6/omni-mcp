from pxr import Usd, UsdShade, Sdf, Gf

materials_file = r"C:\Users\krngr\Downloads\OpenUSD_Lab-Assets\OpenUSD_Lab-Assets\OpenUSD_Lab\Materials\Materials_Samples.usd"

stage = Usd.Stage.Open(materials_file)
looks = stage.GetPrimAtPath("/World/Looks")

converted = 0
for mat_prim in looks.GetChildren():
    if mat_prim.GetTypeName() != "Material":
        continue
    material = UsdShade.Material(mat_prim)

    mdl_shader = None
    for prim in Usd.PrimRange(mat_prim):
        if prim.GetTypeName() == "Shader":
            sh = UsdShade.Shader(prim)
            if sh.GetImplementationSource() == "sourceAsset":
                mdl_shader = sh
                break
    if mdl_shader is None:
        continue

    def get_input(name, default=None):
        inp = mdl_shader.GetInput(name)
        if inp is None:
            return default
        val = inp.Get()
        return val if val is not None else default

    diffuse_color = get_input("diffuse_color_constant")
    diffuse_texture = get_input("diffuse_texture")
    glass_color = get_input("glass_color")
    metallic = get_input("metallic_constant", 0.0)
    roughness = get_input("reflection_roughness_constant", 0.5)
    opacity = 1.0

    if diffuse_color is None and glass_color is not None:
        diffuse_color = glass_color
        opacity = 0.35
        roughness = get_input("frosting_roughness", 0.1)

    if diffuse_color is None and diffuse_texture is None:
        continue

    ps_path = mat_prim.GetPath().AppendChild("PreviewSurface")
    ps_shader = UsdShade.Shader.Define(stage, ps_path)
    ps_shader.CreateIdAttr("UsdPreviewSurface")
    ps_shader.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(float(metallic))
    ps_shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(roughness))
    ps_shader.CreateInput("opacity", Sdf.ValueTypeNames.Float).Set(float(opacity))

    if diffuse_texture and str(diffuse_texture) not in ("", "@@"):
        st_reader_path = mat_prim.GetPath().AppendChild("stReader")
        st_reader = UsdShade.Shader.Define(stage, st_reader_path)
        st_reader.CreateIdAttr("UsdPrimvarReader_float2")
        st_reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
        st_reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

        tex_path = mat_prim.GetPath().AppendChild("diffuseTexture")
        tex_shader = UsdShade.Shader.Define(stage, tex_path)
        tex_shader.CreateIdAttr("UsdUVTexture")
        tex_shader.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(diffuse_texture)
        tex_shader.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(
            st_reader.ConnectableAPI(), "result"
        )
        tex_shader.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)

        ps_shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            tex_shader.ConnectableAPI(), "rgb"
        )
    else:
        r, g, b = diffuse_color
        ps_shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(r, g, b))

    material.CreateSurfaceOutput().ConnectToSource(ps_shader.ConnectableAPI(), "surface")
    converted += 1
    print("Converted:", mat_prim.GetName())

stage.GetRootLayer().Save()
print(f"\nDone. Converted {converted} materials to UsdPreviewSurface fallback.")
