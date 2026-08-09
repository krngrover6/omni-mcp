import bpy

usd_path = r"C:\Users\krngr\Downloads\OpenUSD_Lab-Assets\OpenUSD_Lab-Assets\OpenUSD_Lab\Assemblies\Factory_Complete.usda"
glb_path = r"C:\Users\krngr\Downloads\Omniverse\factory-viewer\factory.glb"

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.usd_import(filepath=usd_path)

bpy.ops.export_scene.gltf(
    filepath=glb_path,
    export_format="GLB",
    use_selection=False,
    export_yup=True,
    export_materials="EXPORT",
    export_draco_mesh_compression_enable=True,
    export_draco_mesh_compression_level=6,
    export_image_quality=75,
)

print("Exported GLB to:", glb_path)
