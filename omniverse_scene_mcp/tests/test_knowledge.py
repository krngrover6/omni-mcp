from omniverse_scene_mcp.knowledge import _extract_usd_name, build_usd_backend


def test_extract_usd_name_finds_pxr_modules_and_classes():
    assert _extract_usd_name("class detail for UsdGeom.Xform") == "UsdGeom.Xform"
    assert _extract_usd_name("module docs for pxr.Sdf please") == "Sdf"
    assert _extract_usd_name("Are there class docs for Gf.Vec3d?") == "Gf.Vec3d"
    assert _extract_usd_name("class reference for the stage") == ""
    assert _extract_usd_name("Kind of stuck: class docs for UsdGeom.Mesh?") == "UsdGeom.Mesh"
    assert _extract_usd_name("Kind of confused, module docs for Sdf") == "Sdf"
    assert _extract_usd_name("Usd class docs for UsdGeom.Mesh") == "UsdGeom.Mesh"


def test_usd_selector_builds_class_and_module_detail_requests():
    select = build_usd_backend("http://127.0.0.1:1/mcp").selector

    class_selection = select("class detail for UsdGeom.Xform")
    assert class_selection.tool_name == "get_usd_class_detail"
    assert class_selection.arguments == {"class_names": "UsdGeom.Xform"}

    module_selection = select("module docs for UsdShade")
    assert module_selection.tool_name == "get_usd_module_detail"
    assert module_selection.arguments == {"module_names": "UsdShade"}

    unnamed = select("class reference please")
    assert unnamed.arguments == {"class_names": "class reference please"}


def test_usd_selector_reads_method_names_instead_of_filler_words():
    select = build_usd_backend("http://127.0.0.1:1/mcp").selector

    dotted = select("method detail for Usd.Stage.Open")
    assert dotted.tool_name == "get_usd_method_detail"
    assert dotted.arguments == {"method_names": "Open", "class_name": "Usd.Stage"}

    named = select("Is there a signature for GetAttribute on UsdPrim?")
    assert named.arguments == {"method_names": "GetAttribute", "class_name": "UsdPrim"}

    class_only = select("method detail for UsdGeom.Xform")
    assert class_only.tool_name == "get_usd_class_detail"
    assert class_only.arguments == {"class_names": "UsdGeom.Xform"}
