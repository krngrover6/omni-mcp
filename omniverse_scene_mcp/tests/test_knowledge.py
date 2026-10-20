from omniverse_scene_mcp.knowledge import (
    _extract_name,
    _extract_usd_method,
    _extract_usd_name,
    build_bowerbot_backend,
    build_omni_ui_backend,
    build_usd_backend,
)


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


def test_usd_method_extraction_ignores_plain_english_before_parentheses():
    assert _extract_usd_method("how do layers compose (strongest first) in UsdStage?") == ("UsdStage", "")
    assert _extract_usd_method("what does compose (layers) return?") == ("", "")
    assert _extract_usd_method("Is (this) the right signature?") == ("", "")
    assert _extract_usd_method("what method should I use to compose layers?") == ("", "")
    assert _extract_usd_method("signature of GetPrimAtPath(path) on Usd.Stage") == ("Usd.Stage", "GetPrimAtPath")
    assert _extract_usd_method("arguments of Traverse() on UsdStage") == ("UsdStage", "Traverse")
    assert _extract_usd_method("what arguments does getAttribute( take?") == ("", "getAttribute")


def test_usd_selector_sends_conceptual_signature_questions_to_search():
    select = build_usd_backend("http://127.0.0.1:1/mcp").selector
    for query in (
        "what arguments does a reference arc take?",
        "how do layers compose (strongest first) and what is the signature?",
        "which method decides opinion strength?",
    ):
        selection = select(query)
        assert selection.tool_name == "search_usd_knowledge", query
        assert selection.arguments == {"request": query}, query
    named = select("what arguments does GetAttribute take?")
    assert named.tool_name == "get_usd_method_detail"
    assert named.arguments == {"method_names": "GetAttribute"}


def test_usd_selector_matches_code_words_whole_and_keeps_definitions_conceptual():
    select = build_usd_backend("http://127.0.0.1:1/mcp").selector
    for query in (
        "what is a timecode",  # 'code' inside 'timecode'
        "what is a time code",  # a definition of a term that happens to contain 'code'
        "what is scene description",  # 'script' inside 'description'
        "codeless schema",
        "what is a codeless schema",
        "what does CreateNew do",  # 'create' inside 'CreateNew'
        "how do I add a variant set",
    ):
        assert select(query).tool_name == "search_usd_knowledge", query
    for query in (
        "how to create a stage in python",
        "creating a stage in python",
        "show me code for a payload",
        "show me an example of Usd.Stage.CreateNew",
        "what is the code to create a stage",
        "what is a code example",
        "create a reference",
        "building an asset",
    ):
        assert select(query).tool_name == "search_usd_code_examples", query


def test_usd_selector_sends_qualified_method_names_to_method_detail():
    select = build_usd_backend("http://127.0.0.1:1/mcp").selector
    cases = {
        "Usd.Stage.CreateNew": {"method_names": "CreateNew", "class_name": "Usd.Stage"},
        "what does Usd.Stage.CreateNew do": {"method_names": "CreateNew", "class_name": "Usd.Stage"},
        "UsdShade.MaterialBindingAPI.Bind": {"method_names": "Bind", "class_name": "UsdShade.MaterialBindingAPI"},
    }
    for query, arguments in cases.items():
        selection = select(query)
        assert selection.tool_name == "get_usd_method_detail", query
        assert selection.arguments == arguments, query
    # A class alone is not a method, and an explicit request for code still wins.
    assert select("what is UsdGeom.Xform").tool_name == "search_usd_knowledge"
    assert select("show me an example of Usd.Stage.CreateNew").tool_name == "search_usd_code_examples"


def test_omni_ui_selector_skips_filler_words_in_names():
    select = build_omni_ui_backend("http://127.0.0.1:1/mcp").selector
    assert select("omni.ui class docs for the VStack").arguments == {"name": "VStack"}
    assert select("docs about the ZStack widget").arguments == {"name": "ZStack"}
    assert select("style docs for omni.ui.Button.").arguments == {"name": "omni.ui.Button"}
    assert select("omni.ui class details").arguments == {}
    assert select("omni.ui class details").tool_name == "get_ui_instructions"
    assert select("show code examples of a slider").tool_name == "search_ui_code_examples"
    assert _extract_name("module details about the omni.ui module") == "omni.ui"


def test_bowerbot_selector_skips_filler_words_in_project_names():
    select = build_bowerbot_backend("http://127.0.0.1:1/mcp").selector
    opened = select("open project details for the Kitchen")
    assert opened.tool_name == "open_project"
    assert opened.arguments == {"name": "Kitchen"}
    assert select("open project Kitchen").arguments == {"name": "Kitchen"}
    assert select("please open project Warehouse?").arguments == {"name": "Warehouse"}
    created = select("create project called the Garage Demo")
    assert created.tool_name == "create_project"
    assert created.arguments == {"name": "Garage Demo", "up_axis": "Z", "meters_per_unit": 1.0}


def test_bowerbot_selector_answers_questions_with_read_only_tools():
    select = build_bowerbot_backend("http://127.0.0.1:1/mcp").selector
    for question in (
        "how do I create a new project?",
        "what does open project do?",
        "how do i package scene files for delivery?",
        "can i export a usdz from bowerbot?",
    ):
        assert select(question).tool_name not in {"create_project", "open_project", "package_scene"}, question
    assert select("new project Kitchen").tool_name == "create_project"
    assert select("package scene").tool_name == "package_scene"
    assert select("can you open project Kitchen").tool_name == "open_project"
