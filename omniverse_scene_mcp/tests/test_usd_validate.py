"""Read-only validation lab: each exam check fires on a temporary stage and nothing is ever authored."""

import json
import re
import tempfile
from pathlib import Path

from pxr import Ar, Sdf, Tf, Usd, UsdGeom, UsdShade

from omniverse_scene_mcp import usd_validate

VALID_OBJECTIVES = {
    f"{domain}.{number}"
    for domain, count in ((1, 11), (2, 5), (3, 8), (4, 8), (5, 6), (6, 5), (7, 8), (8, 4))
    for number in range(1, count + 1)
}
HEADER = '#usda 1.0\n(\n    defaultPrim = "World"\n    metersPerUnit = 1\n    upAxis = "Z"\n)\n'
QUAD = """
        float3[] extent = [(0, 0, 0), (1, 1, 0)]
        int[] faceVertexCounts = [4]
        int[] faceVertexIndices = [0, 1, 2, 3]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
"""


def _tempdir():
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _open(folder, files, root="scene.usda"):
    for name, text in files.items():
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return Usd.Stage.Open(str(Path(folder) / root))


def _anonymous(text):
    layer = Sdf.Layer.CreateAnonymous(".usda")
    layer.ImportFromString(text)
    return layer


def _memory_stage():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    stage.SetDefaultPrim(UsdGeom.Xform.Define(stage, "/World").GetPrim())
    return stage


def _mesh(stage, path, points=((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)), counts=(4,), indices=(0, 1, 2, 3), extent=True):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr(list(points))
    mesh.CreateFaceVertexCountsAttr(list(counts))
    mesh.CreateFaceVertexIndicesAttr(list(indices))
    if extent:
        mesh.CreateExtentAttr(UsdGeom.Boundable.ComputeExtentFromPlugins(mesh, Usd.TimeCode.Default()))
    return mesh


def _exam(stage, **kwargs):
    return usd_validate.validate(stage, "exam", 1000, **kwargs)


def _issues(result, code):
    return [issue for issue in result["issues"] if issue["code"] == code]


def _paths(result, code):
    return {issue["path"] for issue in _issues(result, code)}


CLEAN_ASSET = """#usda 1.0
(
    defaultPrim = "Chair"
    metersPerUnit = 1
    upAxis = "Z"
)
def Xform "Chair" (
    kind = "component"
)
{
    def Mesh "Geo" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {%s
        color3f[] primvars:displayColor = [(1, 0, 0)] (
            interpolation = "constant"
        )
        texCoord2f[] primvars:st = [(0, 0), (1, 0), (1, 1)] (
            interpolation = "faceVarying"
        )
        int[] primvars:st:indices = [0, 1, 2, 1]
        rel material:binding = </Chair/Looks/Wood>
    }
    def Scope "Looks"
    {
        def Material "Wood"
        {
            token outputs:surface.connect = </Chair/Looks/Wood/Surface.outputs:surface>
            def Shader "Surface"
            {
                uniform token info:id = "UsdPreviewSurface"
                token outputs:surface
            }
            def Shader "Texture"
            {
                uniform token info:id = "UsdUVTexture"
                asset inputs:file = @./wood.png@
            }
        }
    }
}
""" % QUAD

CLEAN_SCENE = """#usda 1.0
(
    defaultPrim = "World"
    endTimeCode = 24
    metersPerUnit = 1
    startTimeCode = 1
    upAxis = "Z"
)
def Xform "World" (
    kind = "assembly"
)
{
    def Xform "Chair_01" (
        prepend references = @./asset.usda@
    )
    {
        double3 xformOp:translate.timeSamples = {
            1: (0, 0, 0),
            24: (2, 0, 0),
        }
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
    def Xform "Chair_02" (
        instanceable = true
        prepend references = @./asset.usda@
    )
    {
        double3 xformOp:translate = (3, 0, 0)
        uniform token[] xformOpOrder = ["xformOp:translate"]
    }
    def Xform "Chair_03" (
        instanceable = true
        prepend references = @./asset.usda@
    )
    {
    }
}
"""


def test_clean_stage_has_no_exam_issues():
    with _tempdir() as folder:
        (Path(folder) / "wood.png").write_bytes(b"")
        stage = _open(folder, {"asset.usda": CLEAN_ASSET, "scene.usda": CLEAN_SCENE})
        assert stage.GetPrimAtPath("/World/Chair_02").IsInstance()
        result = _exam(stage)
        assert result["issues"] == [], result["issues"]
        assert result["ok"] is True
        assert result["counts"] == {"error": 0, "warning": 0, "info": 0}
        assert result["profile"] == "exam" and result["truncated"] is False
        assert {"exam:asset_paths", "exam:composition_errors", "exam:model_hierarchy"} <= set(result["validators_run"])
        json.dumps(result)
        del stage


def test_stage_metadata_checks():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    result = _exam(stage)
    assert {"missing_default_prim", "missing_up_axis", "missing_meters_per_unit"} <= {i["code"] for i in result["issues"]}
    assert result["ok"] is True  # warnings do not fail validation
    assert _issues(result, "missing_default_prim")[0]["details"]["root_prims"] == ["/World"]

    stage.GetRootLayer().defaultPrim = "Nope"
    result = _exam(stage)
    assert _paths(result, "invalid_default_prim") == {"/"}
    assert result["ok"] is False


def test_extent_checks():
    stage = _memory_stage()
    _mesh(stage, "/World/NoExtent", extent=False)
    stale = _mesh(stage, "/World/Stale")
    stale.GetExtentAttr().Set([(0, 0, 0), (5, 5, 5)])
    UsdGeom.Cube.Define(stage, "/World/SmallCube").CreateSizeAttr(1.0)
    UsdGeom.Cube.Define(stage, "/World/DefaultCube")
    result = _exam(stage)
    assert _paths(result, "missing_extent") == {"/World/NoExtent", "/World/SmallCube"}
    [issue] = _issues(result, "stale_extent")
    assert issue["path"] == "/World/Stale"
    assert issue["details"]["authored"] == [[0, 0, 0], [5, 5, 5]]
    assert issue["details"]["computed"] == [[0, 0, 0], [1, 1, 0]]
    assert "5.6" in issue["objective_ids"]


def test_mesh_topology_and_primvar_checks():
    stage = _memory_stage()
    _mesh(stage, "/World/Counts", indices=(0, 1, 2))
    _mesh(stage, "/World/Range", indices=(0, 1, 2, 7))
    mesh = _mesh(stage, "/World/Primvars")
    primvars = UsdGeom.PrimvarsAPI(mesh)
    bad = primvars.CreatePrimvar("bad", Sdf.ValueTypeNames.FloatArray)
    bad.Set([1.0])
    bad.GetAttr().SetMetadata("interpolation", "perVertex")
    indexed = primvars.CreatePrimvar("idx", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.vertex)
    indexed.Set([0.0, 1.0])
    indexed.SetIndices([0, 1, 5, 1])
    sized = primvars.CreatePrimvar("displayColor", Sdf.ValueTypeNames.Color3fArray, UsdGeom.Tokens.vertex)
    sized.Set([(1, 0, 0), (0, 1, 0), (0, 0, 1)])
    good = primvars.CreatePrimvar("good", Sdf.ValueTypeNames.FloatArray, UsdGeom.Tokens.uniform)
    good.Set([1.0])
    result = _exam(stage)
    assert _paths(result, "mesh_topology_mismatch") == {"/World/Counts"}
    assert _paths(result, "mesh_index_out_of_range") == {"/World/Range"}
    assert _paths(result, "primvar_invalid_interpolation") == {"/World/Primvars.primvars:bad"}
    assert _paths(result, "primvar_index_out_of_range") == {"/World/Primvars.primvars:idx"}
    [size] = _issues(result, "primvar_size_mismatch")
    assert size["path"] == "/World/Primvars.primvars:displayColor"
    assert size["details"]["expected"] == 4 and size["details"]["actual"] == 3


def test_asset_path_checks_report_authored_and_resolved():
    with _tempdir() as folder:
        absolute = (Path(folder) / "props" / "asset.usda").as_posix()
        files = {
            "props/asset.usda": '#usda 1.0\n(\n    defaultPrim = "A"\n)\ndef Xform "A"\n{\n}\n',
            "scene.usda": (
                '#usda 1.0\n(\n    defaultPrim = "World"\n    metersPerUnit = 1\n    subLayers = [@./missing_sub.usda@]\n'
                '    upAxis = "Z"\n)\n'
                'def Xform "World"\n{\n'
                '    def "Missing" (\n        prepend references = @./nope.usda@\n    )\n    {\n    }\n'
                '    def "MissingPayload" (\n        prepend payload = @./nope_payload.usda@\n    )\n    {\n    }\n'
                f'    def "Absolute" (\n        prepend references = @{absolute}@\n    )\n    {{\n    }}\n'
                '    def "Backslash" (\n        prepend references = @props\\asset.usda@\n    )\n    {\n    }\n'
                '    def "Remote" (\n        prepend references = @omniverse://server/asset.usda@\n    )\n    {\n    }\n'
                '    def Shader "Tex"\n    {\n        asset inputs:file = @./missing.png@\n    }\n'
                '}\n'
            ),
        }
        stage = _open(folder, files)
        result = _exam(stage)
        unresolved = {issue["details"]["authored"]: issue for issue in _issues(result, "unresolved_asset_path")}
        assert {"./missing_sub.usda", "./nope.usda", "./nope_payload.usda", "./missing.png"} <= set(unresolved)
        texture = unresolved["./missing.png"]
        assert texture["path"] == "/World/Tex.inputs:file"
        assert texture["severity"] == "error" and texture["details"]["resolved"] == ""
        assert texture["details"]["anchored"].endswith("/missing.png")
        assert texture["details"]["usage"] == "asset attribute"
        assert texture["details"]["layer"].endswith("scene.usda")
        assert unresolved["./missing_sub.usda"]["path"] == "/"
        assert unresolved["./nope_payload.usda"]["details"]["usage"] == "payload"
        assert absolute not in unresolved
        [absolute_issue] = _issues(result, "absolute_asset_path")
        assert absolute_issue["path"] == "/World/Absolute"
        assert absolute_issue["details"]["resolved"]
        assert _paths(result, "asset_path_backslashes") == {"/World/Backslash"}
        if "omniverse" not in {scheme.lower() for scheme in Ar.GetRegisteredURISchemes()}:
            assert _paths(result, "asset_uri_scheme_unavailable") == {"/World/Remote"}
        assert not _issues(result, "composition_error") and not _issues(result, "asset_failed_to_open")
        assert result["ok"] is False
        del stage


def test_reference_target_and_unreadable_asset_checks():
    with _tempdir() as folder:
        files = {
            "asset.usda": '#usda 1.0\n(\n    defaultPrim = "A"\n)\ndef Xform "A"\n{\n}\n',
            "nodef.usda": '#usda 1.0\ndef Xform "A"\n{\n}\n',
            "broken.usda": "#usda 1.0\ndef Xform {{{ not usd\n",
            "scene.usda": HEADER + (
                'def Xform "World"\n{\n'
                '    def "BadPath" (\n        prepend references = @./asset.usda@</Nope>\n    )\n    {\n    }\n'
                '    def "NoDefault" (\n        prepend references = @./nodef.usda@\n    )\n    {\n    }\n'
                '    def "Broken" (\n        prepend references = @./broken.usda@\n    )\n    {\n    }\n'
                '}\n'
            ),
        }
        stage = _open(folder, files)
        result = _exam(stage)
        missing = {issue["path"]: issue for issue in _issues(result, "reference_target_missing")}
        assert set(missing) == {"/World/BadPath", "/World/NoDefault"}
        assert "defaultPrim" in missing["/World/NoDefault"]["fix_hint"]
        assert missing["/World/BadPath"]["details"]["pcp_error"] == "ErrorUnresolvedPrimPath"
        assert _paths(result, "asset_failed_to_open") == {"/World/Broken"}
        assert not _issues(result, "unresolved_asset_path")
        del stage


def test_model_hierarchy_checks():
    with _tempdir() as folder:
        scene = HEADER + """def Xform "World"
{
    def Xform "Chair" (
        kind = "component"
    )
    {
        def Xform "Leg" (
            kind = "component"
        )
        {
        }
    }
}
def Xform "Set" (
    kind = "assembly"
)
{
    def Mesh "Floor"
    {%s    }
    def Xform "Odd" (
        kind = "notAKind"
    )
    {
    }
    def Xform "Props"
    {
        def Xform "Lamp" (
            kind = "component"
        )
        {
        }
    }
}
def Xform "Loose"
{
    def Cube "Box"
    {
    }
}
""" % QUAD
        stage = _open(folder, {"scene.usda": scene})
        result = _exam(stage)
        assert _paths(result, "component_under_component") == {"/World/Chair/Leg"}
        assert _paths(result, "model_hierarchy_broken") == {"/World", "/Set/Props"}
        assert _paths(result, "gprim_under_assembly") == {"/Set"}
        assert _paths(result, "unregistered_kind") == {"/Set/Odd"}
        [top_level] = _issues(result, "missing_kind_top_level")
        assert top_level["path"] == "/Loose" and top_level["severity"] == "info"
        broken = {issue["path"]: issue for issue in _issues(result, "model_hierarchy_broken")}
        assert broken["/Set/Props"]["details"]["examples"] == ["/Set/Props/Lamp (kind=component)"]
        del stage


def test_material_binding_checks():
    asset = _anonymous("""#usda 1.0
(
    defaultPrim = "Asset"
)
def Xform "Asset"
{
    def Mesh "Geo" (
        prepend apiSchemas = ["MaterialBindingAPI"]
    )
    {%s
        rel material:binding = </Looks/Red>
    }
}
def Scope "Looks"
{
    def Material "Red"
    {
    }
}
""" % QUAD)
    stage = _memory_stage()
    material = UsdShade.Material.Define(stage, "/World/Looks/Mat")
    UsdGeom.Xform.Define(stage, "/World/NotAMaterial")
    missing = _mesh(stage, "/World/A")
    UsdShade.MaterialBindingAPI.Apply(missing.GetPrim()).GetDirectBindingRel().SetTargets([Sdf.Path("/World/Nope")])
    wrong = _mesh(stage, "/World/B")
    UsdShade.MaterialBindingAPI.Apply(wrong.GetPrim()).GetDirectBindingRel().SetTargets([Sdf.Path("/World/NotAMaterial")])
    no_api = _mesh(stage, "/World/C")
    no_api.GetPrim().CreateRelationship("material:binding").SetTargets([material.GetPath()])
    stage.DefinePrim("/World/Ref").GetReferences().AddReference(asset.identifier)
    result = _exam(stage)
    assert _paths(result, "material_binding_missing_target") == {"/World/A.material:binding"}
    assert _paths(result, "material_binding_not_material") == {"/World/B.material:binding"}
    assert _paths(result, "material_binding_api_missing") == {"/World/C"}
    [dropped] = _issues(result, "material_binding_target_dropped")
    assert dropped["path"] == "/World/Ref/Geo.material:binding"
    assert dropped["details"]["authored"] == ["/Looks/Red"]
    assert result["ok"] is False


def test_instancing_checks_and_prototype_paths():
    asset = _anonymous("""#usda 1.0
(
    defaultPrim = "Asset"
)
def Xform "Asset" (
    kind = "component"
)
{
    def Mesh "Geo"
    {
        int[] faceVertexCounts = [3]
        int[] faceVertexIndices = [0, 1, 2]
        point3f[] points = [(0, 0, 0), (1, 0, 0), (1, 1, 0)]
    }
}
""")
    stage = _memory_stage()
    stage.GetPrimAtPath("/World").SetMetadata("kind", "assembly")
    for name in ("Inst1", "Inst2"):
        prim = stage.DefinePrim(f"/World/{name}")
        prim.GetReferences().AddReference(asset.identifier)
        prim.SetInstanceable(True)
    plain = UsdGeom.Xform.Define(stage, "/World/Plain").GetPrim()
    plain.SetInstanceable(True)
    over = Sdf.CreatePrimInLayer(stage.GetRootLayer(), "/World/Inst1/Geo")
    Sdf.AttributeSpec(over, "doubleSided", Sdf.ValueTypeNames.Bool).default = True
    assert stage.GetPrimAtPath("/World/Inst1").IsInstance()
    result = _exam(stage)
    assert _paths(result, "instanceable_without_prototype") == {"/World/Plain"}
    [ignored] = _issues(result, "ignored_overrides_under_instance")
    assert ignored["path"] == "/World/Inst1" and ignored["details"]["count"] == 1
    [extent] = _issues(result, "missing_extent")
    assert re.fullmatch(r"/World/Inst[12]/Geo", extent["path"])
    assert extent["details"]["instances"] == 2 and extent["details"]["prototype"].startswith("/__Prototype")


def test_time_sample_checks():
    stage = _memory_stage()
    stage.SetStartTimeCode(1)
    stage.SetEndTimeCode(10)
    op = UsdGeom.Xform.Define(stage, "/World/Mover").AddTranslateOp()
    op.Set((0, 0, 0), 0)
    op.Set((1, 0, 0), 20)
    UsdGeom.Xform.Define(stage, "/World/Inside").AddTranslateOp().Set((0, 0, 0), 5)
    result = _exam(stage)
    [issue] = _issues(result, "time_samples_outside_range")
    assert issue["path"] == "/World/Mover.xformOp:translate"
    assert issue["details"]["first_sample"] == 0 and issue["details"]["last_sample"] == 20

    unranged = _memory_stage()
    UsdGeom.Xform.Define(unranged, "/World/Mover").AddTranslateOp().Set((0, 0, 0), 3)
    assert _paths(_exam(unranged), "missing_time_code_range") == {"/"}


def test_invisible_parent_reports_hidden_descendants():
    stage = _memory_stage()
    UsdGeom.Xform.Define(stage, "/World/Hidden").MakeInvisible()
    UsdGeom.Xform.Define(stage, "/World/Hidden/A").GetVisibilityAttr().Set(UsdGeom.Tokens.inherited)
    UsdGeom.Xform.Define(stage, "/World/Hidden/B")
    result = _exam(stage)
    [issue] = _issues(result, "invisible_hides_descendants")
    assert issue["path"] == "/World/Hidden" and issue["severity"] == "info"
    assert issue["details"]["descendants"] == 2
    assert issue["details"]["descendants_authoring_visibility"] == ["/World/Hidden/A"]


def test_layer_unit_and_up_axis_mismatch():
    geo = _anonymous('#usda 1.0\n(\n    defaultPrim = "G"\n    metersPerUnit = 0.01\n    upAxis = "Y"\n)\ndef Xform "G"\n{\n}\n')
    asset = _anonymous(
        '#usda 1.0\n(\n    defaultPrim = "A"\n    metersPerUnit = 0.01\n    upAxis = "Y"\n)\n'
        f'def Xform "A" (\n    prepend references = @{geo.identifier}@\n)\n{{\n}}\n'
    )
    stage = _memory_stage()
    stage.DefinePrim("/World/Asset").GetReferences().AddReference(asset.identifier)
    result = _exam(stage)
    [units] = _issues(result, "layer_units_mismatch")
    assert units["path"] == "/World/Asset" and units["details"]["layer"] == asset.identifier
    assert "100x too large" in units["message"] and units["details"]["apparent_scale"] == 100
    assert _paths(result, "layer_up_axis_mismatch") == {"/World/Asset"}

    meters = _anonymous('#usda 1.0\n(\n    defaultPrim = "A"\n    metersPerUnit = 1\n    upAxis = "Z"\n)\ndef Xform "A"\n{\n}\n')
    centimeters = _memory_stage()
    UsdGeom.SetStageMetersPerUnit(centimeters, UsdGeom.LinearUnits.centimeters)
    centimeters.DefinePrim("/World/Asset").GetReferences().AddReference(meters.identifier)
    [units] = _issues(_exam(centimeters), "layer_units_mismatch")
    assert "100x too small" in units["message"]
    assert not _issues(_exam(centimeters), "layer_up_axis_mismatch")


def test_max_issues_caps_results_and_keeps_errors_first():
    stage = _memory_stage()
    for index in range(30):
        _mesh(stage, f"/World/M{index}", extent=False)
    _mesh(stage, "/World/Broken", indices=(0, 1, 2))
    result = usd_validate.validate(stage, "exam", 5)
    assert result["returned"] == 5 and len(result["issues"]) == 5
    assert result["truncated"] is True and result["total_issues"] >= 31
    assert result["counts"]["warning"] >= 30 and result["counts"]["error"] == 1
    assert result["issues"][0]["severity"] == "error"
    assert any("raise max_issues" in note for note in result["notes"])
    empty = usd_validate.validate(stage, "exam", 0)
    assert empty["issues"] == [] and empty["total_issues"] == result["total_issues"]
    assert usd_validate.validate(stage, "exam", 10**9)["returned"] <= usd_validate.MAX_ISSUES_LIMIT


def test_validate_never_dirties_layers():
    with _tempdir() as folder:
        files = {
            "asset.usda": '#usda 1.0\n(\n    metersPerUnit = 0.01\n)\ndef Xform "A" (\n    kind = "component"\n)\n{\n'
                          '    def Mesh "Geo"\n    {\n        int[] faceVertexCounts = [4]\n    }\n}\n',
            "scene.usda": '#usda 1.0\ndef Xform "World"\n{\n'
                          '    def "Ref" (\n        instanceable = true\n        prepend references = @./asset.usda@</A>\n    )\n    {\n'
                          '        over "Geo"\n        {\n            double size = 2\n        }\n    }\n'
                          '    def "Missing" (\n        prepend references = @./nope.usda@\n    )\n    {\n    }\n'
                          '    def Mesh "Loose" (\n        visibility = "invisible"\n    )\n    {\n'
                          '        rel material:binding = </Nowhere>\n    }\n}\n',
        }
        stage = _open(folder, files)
        layers = stage.GetUsedLayers()
        before = {layer.identifier: (layer.dirty, layer.ExportToString()) for layer in layers}
        notices = []
        listener = Tf.Notice.RegisterGlobally(Sdf.Notice.LayersDidChange, lambda notice, sender: notices.append(notice))
        try:
            for profile in ("exam", "usd", "all"):
                result = usd_validate.validate(stage, profile)
                assert result["total_issues"] > 0
        finally:
            listener.Revoke()
        assert notices == []
        assert {layer.identifier: (layer.dirty, layer.ExportToString()) for layer in layers} == before
        assert not any(layer.dirty for layer in layers)
        del stage

    stage = _memory_stage()
    _mesh(stage, "/World/M", extent=False)
    snapshot = stage.GetRootLayer().ExportToString()
    usd_validate.validate(stage, "all")
    assert stage.GetRootLayer().ExportToString() == snapshot


def test_usd_profile_uses_openusd_validators():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    result = usd_validate.validate(stage, "usd")
    if usd_validate._validation_api() is None:
        assert any("not available" in note for note in result["notes"])
        return
    assert result["issues"] and all(issue["source"] == "usd" for issue in result["issues"])
    assert "MissingDefaultPrim" in {issue["code"] for issue in result["issues"]}
    assert not any("physics" in name.lower() for name in result["validators_run"])
    assert not any(name.startswith("exam:") for name in result["validators_run"])
    assert all(issue["objective_ids"] for issue in result["issues"])

    combined = usd_validate.validate(stage, "all")
    assert any("physics" in name.lower() for name in combined["validators_run"])
    assert {"usd", "exam"} <= {issue["source"] for issue in combined["issues"]}


def test_usd_profile_degrades_without_validation_api():
    original = usd_validate._validation_api
    usd_validate._validation_api = lambda: None
    try:
        stage = Usd.Stage.CreateInMemory()
        result = usd_validate.validate(stage, "usd")
        assert result["issues"] == [] and result["ok"] is True
        assert any("not available" in note for note in result["notes"])
        assert _issues(usd_validate.validate(stage, "all"), "missing_default_prim")
    finally:
        usd_validate._validation_api = original


def test_unknown_profile_and_bad_stage_raise():
    for call in (lambda: usd_validate.validate(Usd.Stage.CreateInMemory(), "strict"), lambda: usd_validate.validate(None)):
        try:
            call()
        except ValueError:
            continue
        raise AssertionError("expected ValueError")


def test_validate_file_opens_saved_files_read_only():
    with _tempdir() as folder:
        path = Path(folder) / "scene.usda"
        path.write_text(HEADER + 'def Xform "World"\n{\n    def Mesh "M"\n    {\n    }\n}\n', encoding="utf-8")
        before = path.read_bytes()
        result = usd_validate.validate_file(str(path))
        assert result["file"] == str(path)
        assert _paths(result, "missing_extent") == {"/World/M"}
        assert path.read_bytes() == before
        no_payloads = usd_validate.validate_file(str(path), load_payloads=False)
        assert any("Payloads were not loaded" in note for note in no_payloads["notes"])
        missing = usd_validate.validate_file(str(Path(folder) / "absent.usda"))
        assert missing["ok"] is False and "error" in missing
        json.dumps(missing)


def test_issue_shape_and_objective_ids():
    rules = usd_validate.exam_rules()
    assert len({rule["code"] for rule in rules}) == len(rules)
    for rule in rules:
        assert rule["severity"] in usd_validate.SEVERITIES and rule["fix_hint"]
        assert rule["objective_ids"] and set(rule["objective_ids"]) <= VALID_OBJECTIVES, rule
    stage = _memory_stage()
    _mesh(stage, "/World/M", extent=False)
    UsdShade.MaterialBindingAPI.Apply(_mesh(stage, "/World/N").GetPrim()).GetDirectBindingRel().SetTargets([Sdf.Path("/X")])
    result = usd_validate.validate(stage, "all")
    json.dumps(result)
    assert set(result) >= {"ok", "profile", "counts", "issues", "validators_run", "notes"}
    for issue in result["issues"]:
        assert set(issue) >= {"severity", "code", "path", "message", "fix_hint", "objective_ids"}
        assert issue["severity"] in usd_validate.SEVERITIES
        assert issue["fix_hint"] and issue["path"].startswith("/")
        assert set(issue["objective_ids"]) <= VALID_OBJECTIVES, issue


def test_variable_expression_asset_paths_are_evaluated_not_reported_unresolved():
    with _tempdir() as folder:
        files = {
            "rock_a.usda": '#usda 1.0\n(\n    defaultPrim = "Rock"\n)\ndef Xform "Rock"\n{\n    def Xform "S"\n    {\n    }\n}\n',
            "scene.usda": (
                '#usda 1.0\n(\n    defaultPrim = "World"\n    expressionVariables = {\n        string VARIANT = "a"\n    }\n'
                '    metersPerUnit = 1\n    upAxis = "Z"\n)\n'
                'def Xform "World"\n{\n'
                '    def Xform "Rock" (\n        prepend references = @`"rock_${VARIANT}.usda"`@\n    )\n    {\n    }\n'
                '    def Xform "Gone" (\n        prepend references = @`"gone_${VARIANT}.usda"`@\n    )\n    {\n    }\n'
                '}\n'
            ),
        }
        stage = _open(folder, files)
        assert stage.GetPrimAtPath("/World/Rock/S").IsValid()
        result = _exam(stage)
        unresolved = {issue["details"]["authored"] for issue in _issues(result, "unresolved_asset_path")}
        assert '`"rock_${VARIANT}.usda"`' not in unresolved
        assert '`"gone_${VARIANT}.usda"`' in unresolved  # evaluates to gone_a.usda, which really is missing
        assert not _issues(result, "absolute_asset_path")


def test_material_binding_api_hint_does_not_claim_bindings_are_ignored():
    [rule] = [rule for rule in usd_validate.exam_rules() if rule["code"] == "material_binding_api_missing"]
    assert "ignores bindings" not in rule["fix_hint"]
    assert "still resolves bindings on prims without the API" in rule["fix_hint"]
    assert "MaterialBindingAPI.Apply(prim)" in rule["fix_hint"] and "Bind() alone does not apply it" in rule["fix_hint"]
    # Neither build has a strict-mode switch, so the hint must not invent one.
    assert "USD_SHADE_" not in rule["fix_hint"] and "strict" not in rule["fix_hint"].lower()
    stage = _memory_stage()
    material = UsdShade.Material.Define(stage, "/World/Looks/Mat")
    cube = _mesh(stage, "/World/C")
    cube.GetPrim().CreateRelationship("material:binding").SetTargets([material.GetPath()])
    assert UsdShade.MaterialBindingAPI(cube.GetPrim()).ComputeBoundMaterial()[0].GetPath() == material.GetPath()
