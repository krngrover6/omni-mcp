"""Plain-English error explainer: realistic Script Editor / Kit / USD / PhysX samples for every pattern."""

import builtins
import json
import random
import re
import time

from omniverse_scene_mcp import error_explainer
from omniverse_scene_mcp.error_explainer import MAX_INPUT_CHARS, PATTERNS_PATH, explain_error, list_patterns

TF = "pxrInternal_v0_25_11__pxrReserved__"
STAGE_CPP = r"C:\b\work\USD\pxr\usd\usd\stage.cpp"


def tb(*frames_and_lines):
    """A Script Editor traceback: frames are (file, line, func, code); the final strings are the exception lines."""
    out = ["Traceback (most recent call last):"]
    for item in frames_and_lines:
        if isinstance(item, tuple):
            file, line, func, code = item
            out.append(f'  File "{file}", line {line}, in {func}')
            if code:
                out.append(f"    {code}")
        else:
            out.append(item)
    return "\n".join(out)


def tf_exception(function, message, line=1234, cpp=STAGE_CPP):
    return f"pxr.Tf.ErrorException: \n\tError in '{TF}::{function}' at line {line} in file {cpp} : '{message}'"


USER = ("<string>", 3, "<module>", None)

# kind -> realistic samples (every pattern in data/error_patterns.json has at least one).
SAMPLES = {
    "windows_path_escape": [
        '  File "<string>", line 2\n    path = "C:\\Users\\me\\scene.usda"\n           ^^^^^^^^^^^^^^^^^^^^^^^^\n'
        "SyntaxError: (unicode error) 'unicodeescape' codec can't decode bytes in position 2-3: truncated "
        "\\UXXXXXXXX escape",
    ],
    "await_outside_async_function": [
        '  File "<string>", line 4\n    await omni.usd.get_context().open_stage_async("C:/scenes/demo.usda")\n'
        "    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^\nSyntaxError: 'await' outside function",
    ],
    "indentation_error": [
        '  File "<string>", line 3\n    print(prim.GetPath())\n    ^\n'
        "IndentationError: expected an indented block after 'for' statement on line 2",
        '  File "<string>", line 5\n    stage = omni.usd.get_context().get_stage()\nTabError: inconsistent use of tabs and spaces in indentation',
    ],
    "syntax_error": [
        '  File "<string>", line 6\n    if prim.IsValid()\n                     ^\nSyntaxError: expected \':\'',
        '  File "<string>", line 2\n    print("hello\n          ^\nSyntaxError: unterminated string literal (detected at line 2)',
    ],
    "asyncio_run_in_kit": [
        tb(("<string>", 9, "<module>", "asyncio.run(main())"),
           ("C:/isaacsim/kit/python/Lib/asyncio/runners.py", 186, "run", None),
           "RuntimeError: asyncio.run() cannot be called from a running event loop"),
    ],
    "coroutine_never_awaited": [
        "<string>:5: RuntimeWarning: coroutine 'UsdContext.open_stage_async' was never awaited\n"
        "RuntimeWarning: Enable tracemalloc to get the object allocation traceback",
    ],
    "await_on_non_async": [
        tb(("<string>", 6, "main", "await omni.usd.get_context().open_stage(path)"),
           "TypeError: object bool can't be used in 'await' expression"),
    ],
    "kit_command_not_registered": [
        '2026-10-22T10:00:01Z [12,345ms] [Error] [omni.kit.commands.command] Can\'t execute command: "CreatPrim", '
        "it wasn't registered.",
    ],
    "kit_command_bad_argument": [
        tb(("<string>", 3, "<module>",
            'omni.kit.commands.execute("CreatePrim", prim_pth="/World/Cube", prim_type="Cube")'),
           ("c:/isaacsim/kit/kernel/py/omni/kit/commands/command.py", 512, "execute", "return execute(name, **kwargs)"),
           "TypeError: CreatePrimCommand.__init__() got an unexpected keyword argument 'prim_pth'"),
    ],
    "kit_command_failed": [
        "[Error] [omni.kit.commands.command] Failed to execute a command: CreatePayload.",
    ],
    "module_not_found_omni_outside_kit": [
        tb(("C:/Users/me/projects/spin.py", 1, "<module>", "import omni.usd"),
           "ModuleNotFoundError: No module named 'omni'"),
    ],
    "module_not_found_pxr": [
        tb(("C:/Users/me/projects/make_scene.py", 1, "<module>", "from pxr import Usd, UsdGeom"),
           "ModuleNotFoundError: No module named 'pxr'"),
        "ImportError: DLL load failed while importing _tf: The specified module could not be found.",
    ],
    "isaac_old_module_name": [
        tb(("<string>", 1, "<module>", "from omni.isaac.core import World"),
           "ModuleNotFoundError: No module named 'omni.isaac.core'"),
    ],
    "extension_not_enabled": [
        tb(("<string>", 2, "<module>", "import omni.physx"), "ModuleNotFoundError: No module named 'omni.physx'"),
        "[Error] [omni.ext.plugin] Can't find extension with name: omni.anim.curve.core",
        "[Error] [omni.ext.plugin] Failed to solve all extension dependencies:",
    ],
    "import_name_error": [
        tb(("<string>", 1, "<module>", "from pxr.Usd import Cube"),
           "ImportError: cannot import name 'Cube' from 'pxr.Usd' (C:\\isaacsim\\kit\\python\\pxr\\Usd\\__init__.py)"),
    ],
    "module_not_found": [
        tb(("<string>", 1, "<module>", "import trimesh"), "ModuleNotFoundError: No module named 'trimesh'"),
    ],
    "omni_usd_context_none": [
        tb(("C:/Users/me/projects/tool.py", 4, "<module>", "stage = omni.usd.get_context().get_stage()"),
           "AttributeError: 'NoneType' object has no attribute 'get_stage'"),
    ],
    "stage_is_none": [
        tb(("<string>", 4, "<module>", 'prim = stage.GetPrimAtPath("/World/Cube")'),
           "AttributeError: 'NoneType' object has no attribute 'GetPrimAtPath'"),
    ],
    "isaac_world_not_initialized": [
        tb(("<string>", 12, "<module>", "print(robot.get_joint_positions())"),
           ("c:/isaacsim/exts/isaacsim.core.prims/isaacsim/core/prims/impl/articulation.py", 1033, "get_joint_positions",
            "self._physics_view.get_dof_positions()"),
           "AttributeError: 'NoneType' object has no attribute 'get_dof_positions'"),
        "[Error] [isaacsim.core.api.world.world] Physics Simulation View is not created yet in order to use get_world_poses",
    ],
    "usd_invalid_prim": [
        tb(("<string>", 5, "<module>", 'size = prim.GetAttribute("size").Get()'),
           "RuntimeError: Accessed invalid null prim"),
        tb(("<string>", 7, "<module>", "print(prim.GetChildren())"), "RuntimeError: Accessed invalid expired prim"),
    ],
    "usd_invalid_attribute": [
        "[Error] [omni.usd] Attribute 'physics:mas' does not exist on prim </World/Box>",
    ],
    "sdf_path_ill_formed": [
        tb(("<string>", 2, "<module>", 'path = Sdf.Path("/World/My Cube")'),
           tf_exception("SdfPath::SdfPath", "Ill-formed SdfPath </World/My Cube>: syntax error", 74,
                        r"C:\b\work\USD\pxr\usd\sdf\path.cpp")),
    ],
    "invalid_prim_path": [
        tb(("<string>", 3, "<module>", 'cube = UsdGeom.Cube.Define(stage, "Cube")'),
           tf_exception("UsdStage::_IsValidPathForCreatingPrim", "Path must be an absolute path: <Cube>", 3436)),
    ],
    "gf_vector_precision_mismatch": [
        tb(("<string>", 6, "<module>", "api.SetTranslate(Gf.Vec3f(1, 2, 3))"),
           "Boost.Python.ArgumentError: Python argument types in",
           "    XformCommonAPI.SetTranslate(XformCommonAPI, Vec3f)",
           "did not match C++ signature:",
           "    SetTranslate(UsdGeomXformCommonAPI {lvalue}, GfVec3d translation, UsdTimeCode time=Usd.TimeCode.Default())"),
        tb(("<string>", 4, "<module>", "m.SetTranslate(Gf.Vec3f(0, 1, 0))"),
           "Boost.Python.ArgumentError: Python argument types in",
           "    Matrix4d.SetTranslate(Matrix4d, Vec3f)",
           "did not match C++ signature:",
           "    SetTranslate(GfMatrix4d {lvalue}, GfVec3d)"),
    ],
    "cpp_signature_mismatch": [
        tb(("<string>", 4, "<module>", "attr = prim.GetAttribute(5)"),
           "Boost.Python.ArgumentError: Python argument types in",
           "    Prim.GetAttribute(Prim, int)",
           "did not match C++ signature:",
           "    GetAttribute(UsdPrim {lvalue}, TfToken attrName)"),
    ],
    "value_type_mismatch": [
        tb(("<string>", 5, "<module>", 'prim.GetAttribute("size").Set("2.0")'),
           tf_exception("UsdStage::_SetValueImpl", "Type mismatch for </World/Cube.size>: expected 'double', got 'string'",
                        5862)),
        tb(("<string>", 3, "<module>", 'attr.Set("red")'),
           "TypeError: No registered converter was able to produce a C++ rvalue of type GfVec3f from this Python "
           "object of type str"),
    ],
    "xform_common_api_incompatible": [
        tb(("<string>", 8, "<module>", "UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(0, 1, 0))"),
           tf_exception("UsdGeomXformCommonAPI::SetTranslate",
                        "XformCommonAPI is not compatible with the xformOpOrder of </World/Robot>", 211,
                        r"C:\b\work\USD\pxr\usd\usdGeom\xformCommonAPI.cpp")),
    ],
    "xform_op_order": [
        tb(("<string>", 6, "<module>", "xformable.AddTranslateOp().Set(Gf.Vec3d(0, 1, 0))"),
           tf_exception("UsdGeomXformable::AddXformOp",
                        "XformOp <xformOp:translate> already exists in xformOpOrder [xformOp:translate, xformOp:orient, "
                        "xformOp:scale].", 168, r"C:\b\work\USD\pxr\usd\usdGeom\xformable.cpp")),
    ],
    "instance_proxy_edit": [
        tb(("<string>", 9, "<module>", 'leg.GetAttribute("visibility").Set("invisible")'),
           tf_exception("UsdStage::_ValidateEditPrim",
                        "Cannot create property spec at path </World/Chair_02/Geom/Leg>; authoring to an instance proxy "
                        "is not allowed.", 1283)),
    ],
    "asset_unresolved": [
        f"Warning: in _ReportErrors at line 3133 of {STAGE_CPP} -- In </World/Chair>: Could not open asset "
        "@./assets/chair.usda@ for reference introduced by @anon:0000020A:root.usda@<World/Chair>. "
        "(recomposing stage on stage @anon:0000020A:root.usda@ <000001F2C>)",
    ],
    "reference_default_prim_missing": [
        f"Warning: in _ReportErrors at line 3133 of {STAGE_CPP} -- In </World/Chair>: Unresolved reference prim "
        "path @./assets/chair.usda@<> introduced by @anon:0000020A:root.usda@<World/Chair>",
        "[Warning] [omni.usd] Referenced layer @./assets/chair.usda@ has no defaultPrim; nothing was referenced.",
    ],
    "reference_prim_path_unresolved": [
        f"Warning: in _ReportErrors at line 3133 of {STAGE_CPP} -- In </World/Chair>: Unresolved reference prim "
        "path @./assets/chair.usda@</Chiar> introduced by @anon:0000020A:root.usda@<World/Chair>",
    ],
    "variant_not_found": [
        "[Error] [omni.usd] Invalid variant selection {color=blue} at </World/Chair>",
        tb(("<string>", 4, "<module>", 'vset = prim.GetVariantSets().GetVariantSet("colour")'),
           "RuntimeError: Accessed invalid null VariantSet"),
    ],
    "layer_not_found": [
        tb(("<string>", 2, "<module>", 'stage = Usd.Stage.Open("C:/scenes/mising.usda")'),
           tf_exception("UsdStage::Open", "Failed to open layer @C:/scenes/mising.usda@", 1069)),
    ],
    "layer_permission_denied": [
        tb(("<string>", 7, "<module>", 'with open("C:/Program Files/scene.usda", "w") as f:'),
           "PermissionError: [Errno 13] Permission denied: 'C:/Program Files/scene.usda'"),
        "[Error] [omni.usd] Failed to save layer @C:/isaacsim/assets/robot.usd@: the file is read-only",
    ],
    "file_not_found": [
        tb(("<string>", 2, "<module>", 'data = open("data/settings.json").read()'),
           "FileNotFoundError: [Errno 2] No such file or directory: 'data/settings.json'"),
    ],
    "physx_triangle_mesh_dynamic": [
        "2026-10-22 10:15:02 [Warning] [omni.physx.plugin] PhysicsUSD: Parse collision - triangle mesh collision "
        "(approximation None/MeshSimplifcation) cannot be a part of a dynamic body, falling back to convexHull "
        "approximation: /World/Bag/Mesh.",
    ],
    "physx_rigid_body_no_collider": [
        "[Warning] [omni.physx.plugin] Rigid body /World/Box has no collision shapes and will fall through the ground.",
    ],
    "physx_joint_missing_bodies": [
        "[Error] [omni.physx.plugin] PhysicsUSD: CreateJoint - no bodies defined at body0 and body1, joint prim: /World/Hinge",
    ],
    "physx_no_scene": [
        "[Warning] [omni.physx.plugin] No physics scene found on the stage, creating a default one.",
    ],
    "simulation_view_invalidated": [
        "[Error] [omni.physics.tensors.plugin] Simulation view object is invalidated and cannot be used again to call "
        "getDofPositions",
    ],
    "simulation_running_edit": [
        "[Warning] [omni.physx.plugin] Adding a rigid body while the simulation is running is not supported; stop "
        "the simulation first.",
    ],
    "name_error_missing_import": [
        tb(("<string>", 3, "<module>", 'cube = UsdGeom.Cube.Define(stage, "/World/Cube")'),
           "NameError: name 'UsdGeom' is not defined"),
    ],
    "unbound_local_error": [
        tb(("<string>", 9, "<module>", "add_cube()"), ("<string>", 6, "add_cube", "count = count + 1"),
           "UnboundLocalError: cannot access local variable 'count' where it is not associated with a value"),
    ],
    "name_error": [
        tb(("<string>", 4, "<module>", 'prim = stgae.GetPrimAtPath("/World")'), "NameError: name 'stgae' is not defined"),
    ],
    "missing_call_parentheses": [
        tb(("<string>", 5, "<module>", "for child in prim.GetChildren:"), "TypeError: 'method' object is not iterable"),
    ],
    "type_error_not_callable": [
        tb(("<string>", 6, "<module>", "print(layer.identifier())"), "TypeError: 'str' object is not callable"),
    ],
    "type_error_arg_count": [
        tb(("<string>", 12, "<module>", "s.spin(5)"),
           "TypeError: Spinner.spin() takes 1 positional argument but 2 were given"),
        tb(("<string>", 3, "<module>", "make_cube()"),
           "TypeError: make_cube() missing 1 required positional argument: 'stage'"),
    ],
    "none_value_used": [
        tb(("<string>", 6, "<module>", "print(size * 2.0)"),
           "TypeError: unsupported operand type(s) for *: 'NoneType' and 'float'"),
        tb(("<string>", 4, "<module>", "x, y, z = attr.Get()"), "TypeError: cannot unpack non-iterable NoneType object"),
    ],
    "module_attribute_error": [
        tb(("<string>", 3, "<module>", 'cube = Usd.Cube.Define(stage, "/World/Cube")'),
           "AttributeError: module 'pxr.Usd' has no attribute 'Cube'"),
    ],
    "key_error": [
        tb(("<string>", 4, "<module>", 'speed = settings["speed"]'), "KeyError: 'speed'"),
    ],
    "index_error": [
        tb(("<string>", 5, "<module>", "first = prim.GetChildren()[0]"), "IndexError: list index out of range"),
    ],
    "zero_division": [
        tb(("<string>", 7, "<module>", "average = total / len(prims)"), "ZeroDivisionError: division by zero"),
    ],
    "type_error": [
        tb(("<string>", 2, "<module>", 'name = "Cube_" + 1'), 'TypeError: can only concatenate str (not "int") to str'),
    ],
    "attribute_error_none": [
        tb(("<string>", 4, "<module>", "print(selected.GetName())"),
           "AttributeError: 'NoneType' object has no attribute 'GetName'"),
    ],
    "attribute_error": [
        tb(("<string>", 4, "<module>", "points = prim.GetPointsAttr().Get()"),
           "AttributeError: 'Prim' object has no attribute 'GetPointsAttr'"),
    ],
    "value_error": [
        tb(("<string>", 2, "<module>", 'height = float("abc")'), "ValueError: could not convert string to float: 'abc'"),
    ],
    "usd_tf_error": [
        "[Error] [omni.usd] Coding Error: in _GetSpecializedInterface at line 210 of "
        "C:\\b\\work\\USD\\pxr\\usd\\usd\\schemaBase.cpp -- Something unusual happened in the schema",
        tb(("<string>", 3, "<module>", "stage.SetEditTarget(layer)"),
           tf_exception("UsdStage::SetEditTarget", "Layer is not in the local LayerStack of this stage", 1500)),
    ],
    "physx_error": [
        "[Error] [omni.physx.plugin] PhysicsUSD: Articulation /World/Robot has a closed loop.",
    ],
    "python_exception": [
        tb(("<string>", 8, "<module>", 'raise RuntimeError("bad robot config")'), "RuntimeError: bad robot config"),
        tb(("<string>", 2, "<module>", "assert prim.IsValid()"), "AssertionError"),
    ],
    "kit_error": [
        "[Error] [omni.kit.window.file] Unable to read the recent files list.",
    ],
    "kit_warning": [
        "[Warning] [carb] Client omni.kit.renderer.plugin has acquired [carb::settings::ISettings v1.0] 100 times. "
        "Consider accessing this interface with carb::getCachedInterface() (Performance warning)",
    ],
}

RESULT_KEYS = {"ok", "kind", "summary", "what_happened", "likely_causes", "how_to_fix", "example_fix", "where",
               "confidence"}


def _check_explanation(result, kind, sample):
    assert result["ok"] is True, sample
    assert result["kind"] == kind, f"expected {kind}, got {result['kind']} for:\n{sample}"
    assert RESULT_KEYS <= set(result)
    assert result["summary"].strip() and result["what_happened"].strip()
    assert result["how_to_fix"] and all(step.strip() for step in result["how_to_fix"])
    assert result["likely_causes"]
    assert result["confidence"] in ("high", "medium", "low")
    assert "related_docs" not in result  # only with knowledge=
    texts = [result["summary"], result["what_happened"], *result["likely_causes"], *result["how_to_fix"],
             result["example_fix"] or ""]
    assert not any("{{" in text or "}}" in text for text in texts), texts


# --- pattern data --------------------------------------------------------------------------------------------------------


def test_pattern_file_lives_in_the_package_data_folder_and_is_valid_json():
    assert PATTERNS_PATH.parent.name == "data"
    assert PATTERNS_PATH.parent.parent.name == "omniverse_scene_mcp"
    raw = json.loads(PATTERNS_PATH.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    kinds = [entry["kind"] for entry in raw["patterns"]]
    assert len(kinds) == len(set(kinds))
    assert len(kinds) >= 30


def test_every_pattern_compiles_and_has_fix_steps():
    for entry in error_explainer.load_patterns():
        for key in ("match", "require", "context_match"):
            for rx in entry.get(key, []):
                re.compile(rx, re.I | re.M)
        assert entry["how_to_fix"] and entry["likely_causes"] and entry["summary"] and entry["what_happened"]
        assert entry["category"] in ("python", "usd", "kit", "isaac", "physics")


def test_every_pattern_has_a_sample_in_this_file():
    kinds = {entry["kind"] for entry in error_explainer.load_patterns()}
    assert kinds == set(SAMPLES), (kinds - set(SAMPLES), set(SAMPLES) - kinds)


def test_list_patterns_gives_one_line_per_kind():
    listed = list_patterns()
    assert len(listed) == len(SAMPLES)
    assert all({"kind", "category", "severity", "summary"} <= set(item) for item in listed)


def test_required_beginner_topics_are_covered():
    kinds = {entry["kind"] for entry in error_explainer.load_patterns()}
    required = {
        "syntax_error", "indentation_error", "name_error", "type_error_arg_count", "attribute_error_none", "key_error",
        "index_error", "module_not_found", "module_not_found_omni_outside_kit", "module_not_found_pxr",
        "zero_division", "unbound_local_error", "invalid_prim_path", "usd_invalid_prim", "sdf_path_ill_formed",
        "value_type_mismatch", "gf_vector_precision_mismatch", "xform_op_order", "xform_common_api_incompatible",
        "instance_proxy_edit", "layer_not_found", "layer_permission_denied", "asset_unresolved", "variant_not_found",
        "reference_default_prim_missing", "stage_is_none", "omni_usd_context_none", "await_outside_async_function",
        "extension_not_enabled", "physx_rigid_body_no_collider", "simulation_running_edit", "kit_command_not_registered",
    }
    assert required <= kinds, required - kinds


# --- every sample ----------------------------------------------------------------------------------------------------------


def test_every_sample_gets_the_right_kind_and_fix_steps():
    for kind, samples in SAMPLES.items():
        for sample in samples:
            _check_explanation(explain_error(sample), kind, sample)


def test_specific_patterns_are_high_confidence_and_generic_ones_medium():
    generic = {entry["kind"] for entry in error_explainer.load_patterns() if entry.get("generic")}
    for kind, samples in SAMPLES.items():
        result = explain_error(samples[0])
        assert result["confidence"] == ("medium" if kind in generic else "high"), (kind, result["confidence"])


def test_samples_also_work_with_kit_log_prefixes_on_every_line():
    sample = SAMPLES["stage_is_none"][0]
    prefixed = "\n".join(f"2026-10-22 10:00:00 [Error] [omni.kit.app._impl] [py stderr]: {line}"
                         for line in sample.split("\n"))
    result = explain_error(prefixed)
    assert result["kind"] == "stage_is_none"
    assert result["where"]["line"] == 4


def test_placeholders_are_filled_from_the_error_text():
    assert "'stgae'" in explain_error(SAMPLES["name_error"][0])["summary"]
    assert "UsdGeom" in explain_error(SAMPLES["name_error_missing_import"][0])["summary"]
    assert "GetPrimAtPath" in explain_error(SAMPLES["stage_is_none"][0])["summary"]
    assert "omni.isaac.core" in explain_error(SAMPLES["isaac_old_module_name"][0])["summary"]
    assert "CreatPrim" in explain_error(SAMPLES["kit_command_not_registered"][0])["summary"]
    assert "prim_pth" in explain_error(SAMPLES["kit_command_bad_argument"][0])["summary"]
    mismatch = explain_error(SAMPLES["value_type_mismatch"][0])["summary"]
    assert "'double'" not in mismatch and "double" in mismatch and "string" in mismatch
    gf = explain_error(SAMPLES["gf_vector_precision_mismatch"][0])["summary"]
    assert "XformCommonAPI.SetTranslate(XformCommonAPI, Vec3f)" in gf
    assert "./assets/chair.usda" in explain_error(SAMPLES["asset_unresolved"][0])["summary"]


def test_missing_placeholder_groups_fall_back_to_plain_words():
    result = explain_error("NameError")
    assert result["kind"] == "name_error"
    assert "that name" in result["summary"]


def test_vec3d_vs_vec3f_explains_precision_and_shows_the_fix():
    result = explain_error(SAMPLES["gf_vector_precision_mismatch"][0])
    assert "Vec3d" in result["what_happened"] and "Vec3f" in result["what_happened"]
    assert "Gf.Vec3d(" in result["example_fix"]
    assert result["exception"] == "ArgumentError"
    assert result["message"].startswith("Boost.Python.ArgumentError: Python argument types in XformCommonAPI.SetTranslate")
    assert "GfVec3d translation" in result["message"]


def test_tf_exception_message_on_the_following_lines_is_used():
    result = explain_error(SAMPLES["instance_proxy_edit"][0])
    assert result["kind"] == "instance_proxy_edit"
    assert result["exception"] == "ErrorException"
    assert "SetInstanceable(False)" in result["example_fix"]
    assert result["where"]["code"].startswith("leg.GetAttribute")


def test_warning_lines_stay_warnings():
    result = explain_error(SAMPLES["physx_triangle_mesh_dynamic"][0])
    assert result["severity"] == "warning"
    assert explain_error(SAMPLES["layer_not_found"][0])["severity"] == "error"


def test_omni_outside_kit_and_extension_not_enabled_are_told_apart():
    outside = explain_error(SAMPLES["module_not_found_omni_outside_kit"][0])
    inside = explain_error(SAMPLES["extension_not_enabled"][0])
    assert outside["kind"] == "module_not_found_omni_outside_kit"
    assert any("Script Editor" in step for step in outside["how_to_fix"])
    assert inside["kind"] == "extension_not_enabled"
    assert any("Window > Extensions" in step for step in inside["how_to_fix"])


def test_bare_message_without_type_or_log_prefix_still_matches():
    result = explain_error("Cannot create property spec at path </World/A/B>; authoring to an instance proxy is not allowed.")
    assert result["kind"] == "instance_proxy_edit"
    assert result["confidence"] == "medium"


# --- root cause in multi-error logs ------------------------------------------------------------------------------------


def test_multi_error_log_picks_the_first_tracebacks_last_line():
    log = "\n".join([
        "[Warning] [carb] Plugin interface omni::foo not found",
        "[Error] [omni.kit.app._impl] Something unrelated happened earlier",
        tb(("<string>", 2, "<module>", 'prim = stgae.GetPrimAtPath("/World")'), "NameError: name 'stgae' is not defined"),
        "",
        "During handling of the above exception, another exception occurred:",
        "",
        tb(("<string>", 5, "<module>", 'print(cfg["x"])'), "KeyError: 'x'"),
        "",
        tb(("<string>", 9, "<module>", "stage.GetPrimAtPath('/a')"),
           "AttributeError: 'NoneType' object has no attribute 'GetPrimAtPath'"),
    ])
    result = explain_error(log)
    assert result["kind"] == "name_error"
    assert result["message"] == "NameError: name 'stgae' is not defined"
    assert result["where"]["line"] == 2
    other_kinds = [item["kind"] for item in result["other_errors"]]
    assert "key_error" in other_kinds and "stage_is_none" in other_kinds
    assert result["error_count"] >= 4
    assert "first error first" in result["note"]


def test_errors_win_over_earlier_warnings_without_a_traceback():
    log = "\n".join([
        "[Warning] [omni.physx.plugin] No physics scene found on the stage, creating a default one.",
        "[Info] [omni.usd] Opened stage C:/scenes/demo.usda",
        "[Error] [omni.physx.plugin] PhysicsUSD: CreateJoint - no bodies defined at body0 and body1, joint prim: /World/Hinge",
        "[Error] [omni.kit.window.file] Unable to read the recent files list.",
    ])
    result = explain_error(log)
    assert result["kind"] == "physx_joint_missing_bodies"
    assert result["severity"] == "error"
    assert [item["kind"] for item in result["other_errors"]] == ["kit_error"]


def test_tracebacks_win_over_earlier_log_errors():
    log = "\n".join([
        "[Error] [omni.kit.window.file] Unable to read the recent files list.",
        tb(("<string>", 1, "<module>", "from omni.isaac.core import World"),
           "ModuleNotFoundError: No module named 'omni.isaac.core'"),
    ])
    assert explain_error(log)["kind"] == "isaac_old_module_name"


def test_generically_recognised_root_stays_the_root():
    log = "\n".join([
        "[Error] something odd",  # only the generic kit_error fits, but it is still the first error
        "[Error] [omni.physx.plugin] PhysicsUSD: CreateJoint - no bodies defined at body0 and body1",
    ])
    result = explain_error(log)
    assert result["kind"] == "kit_error"
    assert result["confidence"] == "medium"


def test_unrecognised_root_falls_back_to_a_recognised_later_error():
    log = "\n".join([
        tb(("<string>", 3, "<module>", "run()"), "mytools.robot.Halt: stopped by user"),  # matches no pattern at all
        "[Error] [omni.physx.plugin] PhysicsUSD: CreateJoint - no bodies defined at body0 and body1",
    ])
    result = explain_error(log)
    assert result["kind"] == "physx_joint_missing_bodies"
    assert result["confidence"] == "medium"


# --- where: file, line and the user's line ---------------------------------------------------------------------------


def test_where_skips_library_frames_and_shows_the_users_line():
    text = tb(
        ("C:/Users/me/projects/spin.py", 12, "spin", "robot.set_joint_positions(targets)"),
        ("c:/isaacsim/exts/isaacsim.core.prims/isaacsim/core/prims/impl/articulation.py", 900, "set_joint_positions",
         "self._physics_view.set_dof_positions(positions, indices)"),
        ("C:/isaacsim/kit/python/Lib/site-packages/numpy/core/fromnumeric.py", 50, "_wrapit", "return func(*args)"),
        "IndexError: index 7 is out of bounds for axis 0 with size 6",
    )
    result = explain_error(text)
    assert result["kind"] == "index_error"
    where = result["where"]
    assert where["file"] == "C:/Users/me/projects/spin.py"
    assert where["line"] == 12
    assert where["function"] == "spin"
    assert where["code"] == "robot.set_joint_positions(targets)"
    assert where["location"] == "C:/Users/me/projects/spin.py:12"


def test_where_uses_the_given_code_when_the_traceback_has_no_source_line():
    code = "from pxr import Usd\nimport omni.usd\nprim = stage.GetPrimAtPath('/World')\n"
    text = tb(USER, "NameError: name 'stage' is not defined")
    result = explain_error(text, code=code)
    assert result["where"]["line"] == 3
    assert result["where"]["code"] == "prim = stage.GetPrimAtPath('/World')"
    assert result["where"]["found_by"] == "line number applied to your code"


def test_where_searches_the_code_when_there_is_no_line_number():
    code = "import omni.usd\n# stgae is a typo below\nstage = omni.usd.get_context().get_stage()\nprim = stgae.GetPrimAtPath('/World')\n"
    result = explain_error("NameError: name 'stgae' is not defined", code=code)
    assert result["where"]["line"] == 4  # the comment on line 2 is skipped
    assert "stgae.GetPrimAtPath" in result["where"]["code"]


def test_syntax_error_without_traceback_header_still_has_where():
    result = explain_error(SAMPLES["syntax_error"][0])
    assert result["where"]["file"] == "<string>"
    assert result["where"]["line"] == 6
    assert result["where"]["code"] == "if prim.IsValid()"


def test_kit_at_frames_after_a_logged_exception():
    text = "\n".join([
        "[Error] [omni.ui.python] AttributeError: 'NoneType' object has no attribute 'GetPrimAtPath'",
        "",
        "At:",
        "  c:/users/me/projects/panel.py(42): _on_click",
        "  c:/isaacsim/exts/omni.ui/omni/ui/_impl.py(10): __call__",
    ])
    result = explain_error(text)
    assert result["kind"] == "stage_is_none"
    assert result["where"]["file"] == "c:/users/me/projects/panel.py"
    assert result["where"]["line"] == 42


def test_where_is_none_when_nothing_points_at_code():
    assert explain_error(SAMPLES["physx_no_scene"][0])["where"] is None


# --- unknown, empty, robustness --------------------------------------------------------------------------------------


def test_unknown_text_gives_generic_guidance_and_low_confidence():
    result = explain_error("The robot looks a bit too blue today.")
    assert result["ok"] is True
    assert result["kind"] == "unknown"
    assert result["confidence"] == "low"
    assert result["how_to_fix"] and result["summary"]


def test_empty_input_is_not_ok_but_still_helpful():
    for empty in ("", "   \n\t ", None):
        result = explain_error(empty)
        assert result["ok"] is False
        assert result["kind"] == "empty"
        assert result["how_to_fix"]


def test_bytes_and_non_string_input_are_accepted():
    sample = SAMPLES["key_error"][0]
    assert explain_error(sample.encode("utf-8"))["kind"] == "key_error"
    assert explain_error(12345)["kind"] == "unknown"


def test_huge_input_is_capped_and_still_finds_the_root_cause():
    noise = "[Info] [omni.kit.app] tick\n" * 400_000  # ~10 MB
    text = SAMPLES["zero_division"][0] + "\n" + noise
    start = time.perf_counter()
    result = explain_error(text)
    elapsed = time.perf_counter() - start
    assert result["truncated"] is True
    assert result["kind"] == "zero_division"
    assert elapsed < 10, elapsed


def test_garbage_input_does_not_crash_or_hang():
    rng = random.Random(1234)
    garbage = [
        "".join(chr(rng.randrange(0, 0x2FFF)) for _ in range(50_000)),
        bytes(rng.randrange(256) for _ in range(50_000)),
        "(" * 300_000,
        "Error " * 100_000,
        "\x00\x1b[31m" * 10_000 + "KeyError: 'x'",
        "Traceback (most recent call last):\n" * 5_000,
        '  File "' + "a" * 10_000 + '", line 99999999999, in x\n' * 50,
        "[Error] [" + "x" * 5_000 + "] " + "y" * 5_000,
        "did not match C++ signature " * 5_000 + "Vec3f " * 5_000,
    ]
    for item in garbage:
        start = time.perf_counter()
        result = explain_error(item)
        assert isinstance(result, dict) and "kind" in result
        assert time.perf_counter() - start < 10
    json.dumps(explain_error(garbage[1]))  # always JSON-serialisable


def test_ansi_colours_are_stripped():
    result = explain_error("\x1b[31mKeyError: 'speed'\x1b[0m")
    assert result["kind"] == "key_error"
    assert "\x1b" not in result["message"]


def test_nothing_from_the_text_or_code_is_executed():
    marker = "_ERROR_EXPLAINER_RAN"
    payload = f"import builtins; setattr(builtins, '{marker}', True)"
    explain_error(f"NameError: name 'x' is not defined\n{payload}", code=payload)
    explain_error(tb(("<string>", 1, "<module>", payload), "SyntaxError: invalid syntax"), code=payload)
    assert not hasattr(builtins, marker)


def test_result_is_json_serialisable_for_every_sample():
    for samples in SAMPLES.values():
        for sample in samples:
            json.dumps(explain_error(sample, code="print('hi')"))


# --- related docs ------------------------------------------------------------------------------------------------------


class _FakeUSDKnowledge:
    def __init__(self):
        self.queries = []

    def search(self, query, top_k=5):
        self.queries.append((query, top_k))
        return [
            {"title": f"USD hit {i}", "source": "usd_docs", "url_or_path": f"usd/page{i}.html", "snippet": "x " * 400}
            for i in range(top_k)
        ]


class _FakeKitDocs:
    def search(self, query, top_k=5):
        return [{"title": "Kit hit", "extension": "omni.kit.commands", "source_path": "exts/omni.kit.commands/docs/a.md:3",
                 "snippet": "commands"}]


class _BrokenKnowledge:
    def search(self, query, top_k=5):
        raise RuntimeError("index is broken")


def test_related_docs_come_from_the_knowledge_search():
    knowledge = _FakeUSDKnowledge()
    result = explain_error(SAMPLES["usd_invalid_prim"][0], knowledge=knowledge)
    assert len(result["related_docs"]) == 3
    assert result["related_docs"][0] == {
        "title": "USD hit 0", "source": "usd_docs", "location": "usd/page0.html",
        "snippet": result["related_docs"][0]["snippet"],
    }
    assert len(result["related_docs"][0]["snippet"]) <= 240
    assert knowledge.queries[0][0] == "GetPrimAtPath IsValid invalid prim"


def test_related_docs_interleave_several_backends_and_skip_broken_ones():
    result = explain_error(SAMPLES["kit_command_failed"][0], knowledge=[_BrokenKnowledge(), _FakeUSDKnowledge(), _FakeKitDocs()])
    titles = [hit["title"] for hit in result["related_docs"]]
    assert titles == ["USD hit 0", "Kit hit", "USD hit 1"]
    assert result["related_docs"][1]["location"] == "exts/omni.kit.commands/docs/a.md:3"
    assert result["related_docs"][1]["source"] == "omni.kit.commands"


def test_related_docs_accept_a_callable_and_unknown_text_uses_the_message():
    seen = []

    def search(query):
        seen.append(query)
        return [{"title": "Hit", "url_or_path": "a.md"}]

    result = explain_error("The robot looks a bit too blue today.", knowledge=search)
    assert result["related_docs"] == [{"title": "Hit", "source": "", "location": "a.md", "snippet": ""}]
    assert seen and "robot" in seen[0]


def test_related_docs_skipped_without_knowledge_and_for_empty_input():
    assert "related_docs" not in explain_error(SAMPLES["key_error"][0])
    assert "related_docs" not in explain_error("", knowledge=_FakeUSDKnowledge())
