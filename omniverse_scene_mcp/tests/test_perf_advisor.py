"""perf_advisor: performance report, instancing advisor, make_instanceable, convert_to_payload and live_frame_stats.

Fixture-free test_* functions on synthetic temp stages (in-memory root layers referencing small asset files in a
temporary folder). live_frame_stats runs against FAKE omni/carb modules that are put into sys.modules only for one
test; inside a real Kit process those tests return at once so Kit's modules are never replaced.
"""

import contextlib
import json
import os
import struct
import sys
import tempfile
import types

from pxr import Gf, Sdf, Usd, UsdGeom, UsdLux, UsdShade, Vt

from omniverse_scene_mcp import perf_advisor as pa
from omniverse_scene_mcp import undo
from omniverse_scene_mcp.usd_insight import TimeBudget

_IN_KIT = "carb" in sys.modules or "omni.kit.app" in sys.modules
_CUBE_POINTS = [(-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1), (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1)]
_CUBE_INDICES = [0, 3, 2, 1, 4, 5, 6, 7, 0, 1, 5, 4, 1, 2, 6, 5, 2, 3, 7, 6, 3, 0, 4, 7]


# ------------------------------------------------------------------------------------------------ builders


def _cube(stage, path, offset=0.0):
    mesh = UsdGeom.Mesh.Define(stage, path)
    mesh.CreatePointsAttr([Gf.Vec3f(x + offset, y, z) for x, y, z in _CUBE_POINTS])
    mesh.CreateFaceVertexCountsAttr([4] * 6)
    mesh.CreateFaceVertexIndicesAttr(_CUBE_INDICES)
    return mesh


def _asset(folder):
    """asset.usda: /Asset (defaultPrim) with one cube mesh /Asset/Body = 12 triangles, 8 points."""
    path = os.path.join(folder, "asset.usda")
    stage = Usd.Stage.CreateNew(path)
    UsdGeom.Xform.Define(stage, "/Asset")
    _cube(stage, "/Asset/Body")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/Asset"))
    stage.GetRootLayer().Save()
    return path


def _big_scene(folder):
    asset = _asset(folder)
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    UsdGeom.Xform.Define(stage, "/World/Copies")
    for i in range(100):
        prim = UsdGeom.Xform.Define(stage, f"/World/Copies/C_{i:03d}").GetPrim()
        prim.GetReferences().AddReference(asset)
    instancer = UsdGeom.PointInstancer.Define(stage, "/World/PI")
    UsdGeom.Xform.Define(stage, "/World/PI/Protos")
    _cube(stage, "/World/PI/Protos/Box")
    instancer.CreatePrototypesRel().SetTargets([Sdf.Path("/World/PI/Protos/Box")])
    instancer.CreateProtoIndicesAttr([0] * 50)
    instancer.CreatePositionsAttr([Gf.Vec3f(i * 3.0, 0, 0) for i in range(50)])
    heavy = UsdGeom.Mesh.Define(stage, "/World/Heavy")
    heavy.CreatePointsAttr([Gf.Vec3f(i * 0.01, (i % 7) * 0.1, (i % 13) * 0.1) for i in range(1000)])
    heavy.CreateFaceVertexCountsAttr(Vt.IntArray([3] * 300_000))
    heavy.CreateFaceVertexIndicesAttr(Vt.IntArray([i % 1000 for i in range(900_000)]))
    UsdGeom.Xform.Define(stage, "/World/Lights")
    for i in range(60):
        UsdLux.SphereLight.Define(stage, f"/World/Lights/L_{i:02d}")
    path = "/World/Deep"
    UsdGeom.Xform.Define(stage, path)
    for i in range(1, 31):
        path += f"/D{i:02d}"
        UsdGeom.Xform.Define(stage, path)
    payload = UsdGeom.Xform.Define(stage, "/World/Payloaded").GetPrim()
    payload.GetPayloads().AddPayload(asset)
    stage.Unload("/World/Payloaded")
    return stage


def _small_scene(folder, copies=5):
    asset = _asset(folder)
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    for i in range(copies):
        prim = UsdGeom.Xform.Define(stage, f"/World/C_{i:03d}").GetPrim()
        prim.GetReferences().AddReference(asset)
        UsdGeom.XformCommonAPI(prim).SetTranslate((i * 3.0, 0, 0))
    UsdGeom.Xform.Define(stage, "/World/Plain")
    return stage, [f"/World/C_{i:03d}" for i in range(copies)]


@contextlib.contextmanager
def _patched(**values):
    saved = {name: getattr(pa, name) for name in values}
    try:
        for name, value in values.items():
            setattr(pa, name, value)
        yield
    finally:
        for name, value in saved.items():
            setattr(pa, name, value)


def _png_header(path, width, height):
    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
                     + b"\x08\x06\x00\x00\x00" + b"\x00" * 4)


# --------------------------------------------------------------------------------------------- perf_report


def test_perf_report_big_scene_exact_totals():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _big_scene(tmp)
        before = stage.GetRootLayer().ExportToString()
        report = pa.perf_report(stage)
        assert stage.GetRootLayer().ExportToString() == before
    json.dumps(report)
    assert report["ok"] is True and report["truncated"] is False
    totals = report["totals"]
    assert totals["prims"] == 298
    assert totals["meshes"] == 102
    assert totals["triangles"] == 1200 + 12 + 300_000
    assert totals["triangles_drawn"] == 1200 + 50 * 12 + 300_000
    assert totals["triangles_estimated"] is True  # the 300k-face array was sampled (exact here: all triangles)
    assert totals["points"] == 100 * 8 + 8 + 1000
    assert totals["lights"] == 60 and totals["lights_by_type"] == {"SphereLight": 60}
    assert totals["point_instancers"] == 1 and totals["point_instances"] == 50
    assert totals["payloads_unloaded"] == 1 and totals["payloads_loaded"] == 0
    assert totals["instanceable_prims"] == 0 and totals["prototypes"] == 0
    assert totals["max_depth"] == 32
    assert totals["prims_by_type"]["SphereLight"] == 60 and totals["prims_by_type"]["Mesh"] == 102
    assert totals["time_sampled_attributes"] == 0

    groups = report["instancing"]["groups"]
    refs = [g for g in groups if g["kind"] == "same_reference"]
    assert len(refs) == 1
    group = refs[0]
    assert group["count"] == 100 and group["triangles_per_copy"] == 12 and group["prims_per_copy"] == 2
    assert group["est_triangles_saved"] == 99 * 12 and group["est_prims_saved"] == 99
    assert group["approach"] == pa.APPROACH_SCENEGRAPH and group["copies_with_local_edits"] == 0
    assert group["example_paths"][0] == "/World/Copies/C_000"
    pis = [g for g in groups if g["kind"] == "point_instancer"]
    assert len(pis) == 1 and pis[0]["approach"] == pa.APPROACH_ALREADY and pis[0]["count"] == 50
    assert pis[0]["already_saving_triangles"] == 50 * 12 - 12
    assert report["instancing"]["opportunities"] == 1
    assert report["instancing"]["total_saving_estimate"]["triangles"] == 1188

    kinds = {}
    for spot in report["hotspots"]:
        kinds.setdefault(spot["kind"], []).append(spot)
    assert set(kinds) == {"heavy mesh", "deep hierarchy", "many lights"}
    assert kinds["heavy mesh"][0]["path"] == "/World/Heavy"
    assert kinds["heavy mesh"][0]["measure"]["triangles"] == 300_000
    assert kinds["deep hierarchy"][0]["measure"] == {"max_depth": 32, "prims_below_limit": 8}
    assert kinds["many lights"][0]["path"] == "/World/Lights"
    assert kinds["many lights"][0]["measure"]["lights"] == 60
    assert report["light_advice"]["too_many"] is True
    assert report["payload_candidates"] == []
    assert report["score"] == 100 - 6 - 4 - 10 - 5 and report["grade"] == "C"
    assert 3 <= report["summary"].count(". ") + 1 <= 6
    assert "Biggest win" in report["summary"]


def test_perf_report_scenegraph_instances_counted_once():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp, copies=10)
        for path in paths:
            stage.GetPrimAtPath(path).SetInstanceable(True)
        report = pa.perf_report(stage)
    totals = report["totals"]
    assert totals["instances"] == 10 and totals["instanceable_prims"] == 10 and totals["prototypes"] == 1
    assert totals["prims"] == 12 and totals["prims_in_prototypes"] == 1
    assert totals["triangles"] == 12 and totals["triangles_drawn"] == 120 and totals["meshes"] == 1
    already = [g for g in report["instancing"]["groups"] if g["kind"] == "scenegraph_instances"]
    assert len(already) == 1 and already[0]["count"] == 10 and already[0]["already_saving_triangles"] == 108
    assert report["instancing"]["opportunities"] == 0
    assert report["score"] == 100 and report["grade"] == "A"


def test_perf_report_materials_textures_and_identical_meshes():
    with tempfile.TemporaryDirectory() as tmp:
        texture = os.path.join(tmp, "steel_8k.png")
        _png_header(texture, 8192, 8192)
        hdr = os.path.join(tmp, "sky.hdr")
        with open(hdr, "wb") as handle:
            handle.write(b"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y 4096 +X 8192\n" + b"\x00" * 16)
        stage = Usd.Stage.CreateInMemory()
        UsdGeom.Xform.Define(stage, "/World")
        for i in range(12):
            mesh = _cube(stage, f"/World/Part_{i:02d}")
            material = UsdShade.Material.Define(stage, f"/World/Looks/M_{i:02d}")
            shader = UsdShade.Shader.Define(stage, f"/World/Looks/M_{i:02d}/PBR")
            shader.CreateIdAttr("UsdPreviewSurface")
            shader.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(0.4)
            tex = UsdShade.Shader.Define(stage, f"/World/Looks/M_{i:02d}/Tex")
            tex.CreateIdAttr("UsdUVTexture")
            tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(texture)
            shader.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(tex.ConnectableAPI(), "rgb")
            material.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
            UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(material)
        dome = UsdLux.DomeLight.Define(stage, "/World/Sky")
        dome.CreateTextureFileAttr(hdr)
        UsdLux.RectLight.Define(stage, "/World/Panel").CreateTextureFileAttr(texture)  # a rect light is not a dome
        report = pa.perf_report(stage)
    json.dumps(report)
    totals = report["totals"]
    assert totals["materials"] == 12 and totals["unique_bound_materials"] == 12 and totals["duplicate_materials"] == 11
    assert totals["textures"] == 2 and totals["textures_missing"] == 0 and totals["texture_local_bytes"] > 0
    kinds = {spot["kind"] for spot in report["hotspots"]}
    assert {"many unique materials", "huge texture"} <= kinds
    huge = [spot for spot in report["hotspots"] if spot["kind"] == "huge texture"]
    assert {(s["measure"]["width"], s["measure"]["height"]) for s in huge} == {(8192, 8192), (8192, 4096)}
    domes = report["light_advice"]["dome_lights"]
    assert len(domes) == 1 and report["light_advice"]["by_type"] == {"DomeLight": 1, "RectLight": 1}
    assert domes[0]["texture"]["width"] == 8192 and domes[0]["texture"]["height"] == 4096
    assert any("4K HDR" in line for line in report["light_advice"]["advice"])
    identical = [g for g in report["instancing"]["groups"] if g["kind"] == "identical_meshes"]
    assert len(identical) == 1 and identical[0]["count"] == 12 and identical[0]["est_triangles_saved"] == 11 * 12
    assert identical[0]["approach"] == pa.APPROACH_SCENEGRAPH


def test_perf_report_point_instancer_approach_for_many_simple_copies():
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    for i in range(60):
        _cube(stage, f"/World/Bolt_{i:02d}")
    for i in range(3):  # instanceable without a reference does nothing
        stage.GetPrimAtPath(f"/World/Bolt_{i:02d}").SetInstanceable(True)
    report = pa.perf_report(stage)
    identical = [g for g in report["instancing"]["groups"] if g["kind"] == "identical_meshes"]
    assert len(identical) == 1 and identical[0]["count"] == 60
    assert identical[0]["approach"] == pa.APPROACH_POINT_INSTANCER
    assert report["totals"]["instanceable_without_effect"] == 3 and report["totals"]["instances"] == 0
    assert report["instancing"]["instanceable_without_effect_examples"][0] == "/World/Bolt_00"
    assert "does nothing" in report["instancing"]["total_saving_estimate"]["plain"]


def test_perf_report_dense_subtree_and_payload_candidates():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _big_scene(tmp)
        with _patched(PAYLOAD_MIN_TRIANGLES=10, DENSE_SUBTREE_PRIMS=100):
            report = pa.perf_report(stage)
    candidates = report["payload_candidates"]
    assert len(candidates) == 1 and candidates[0]["copies"] == 100 and candidates[0]["triangles_per_copy"] == 12
    assert candidates[0]["asset"].replace("\\", "/").endswith("asset.usda")
    dense = [spot for spot in report["hotspots"] if spot["kind"] == "dense subtree"]
    assert [spot["path"] for spot in dense] == ["/World/Copies"]
    assert dense[0]["measure"]["prims"] == 201


def test_perf_report_root_path_and_errors():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _big_scene(tmp)
        report = pa.perf_report(stage, root_path="/World/Lights")
        assert report["ok"] and report["totals"]["prims"] == 61 and report["totals"]["lights"] == 60
        assert report["totals"]["meshes"] == 0
        assert pa.perf_report(stage, root_path="/Nope")["ok"] is False
    assert pa.perf_report(None)["ok"] is False
    assert pa.perf_report(Usd.Stage.CreateInMemory(), deadline="soon")["ok"] is False


def test_perf_report_deadline_and_max_prims_truncation():
    with tempfile.TemporaryDirectory() as tmp:
        stage = _big_scene(tmp)
        for report in (pa.perf_report(stage, budget_ms=0), pa.perf_report(stage, deadline=TimeBudget(0))):
            json.dumps(report)
            assert report["ok"] and report["truncated"] and report["truncated_by_time"] and report["score_is_partial"]
            assert report["time_budget"]["truncated_by_time"] is True
            assert report["prims_visited"] < 298
        capped = pa.perf_report(stage, max_prims=10)
    assert capped["truncated_by_prims"] and not capped["truncated_by_time"] and capped["prims_visited"] == 10
    assert "stopped early" in capped["summary"]


def test_perf_report_counts_time_samples():
    stage = Usd.Stage.CreateInMemory()
    xform = UsdGeom.Xform.Define(stage, "/World")
    op = xform.AddTranslateOp()
    op.Set(Gf.Vec3d(0, 0, 0), 0)
    op.Set(Gf.Vec3d(1, 0, 0), 10)
    assert pa.perf_report(stage)["totals"]["time_sampled_attributes"] == 1


# ------------------------------------------------------------------------------------- make_instanceable


def test_make_instanceable_dry_run_is_byte_identical():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp)
        layer = stage.GetRootLayer()
        before = layer.ExportToString()
        result = pa.make_instanceable(stage, paths)
        json.dumps(result)
        assert result["ok"] and result["dry_run"] and result["written"] is False
        assert [item["path"] for item in result["would_change"]] == paths
        assert result["groups"][0]["count"] == 5
        assert layer.ExportToString() == before
        assert not stage.GetPrototypes()


def test_make_instanceable_success_and_undo():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp)
        layer = stage.GetRootLayer()
        before = layer.ExportToString()
        stack = undo.UndoStack()
        result = pa.make_instanceable(stage, paths, dry_run=False, undo_stack=stack)
        json.dumps(result)
        assert result["ok"] and result["written"] and result["prototypes_after"] == 1
        assert all(item["is_instance"] for item in result["changed"])
        assert result["undo"]["recorded"] is True
        assert all(stage.GetPrimAtPath(path).IsInstance() for path in paths)
        assert layer.ExportToString().count("instanceable = true") == 5
        report = pa.perf_report(stage)
        assert report["totals"]["triangles"] == 12 and report["totals"]["triangles_drawn"] == 60
        again = pa.make_instanceable(stage, paths, dry_run=False)
        assert again["ok"] and len(again["skipped"]) == 5 and not again["would_change"]
        proxy = pa.make_instanceable(stage, ["/World/C_000/Body"])
        assert proxy["ok"] is False and "inside an instance" in proxy["refused"][0]["reason"]
        stack.peek().undo(stage=stage)
        assert not any(stage.GetPrimAtPath(path).IsInstance() for path in paths)
        assert layer.ExportToString() == before


def test_make_instanceable_refuses_local_child_edits_and_lists_them():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp)
        body = stage.OverridePrim("/World/C_002/Body")
        body.CreateAttribute("primvars:displayColor", Sdf.ValueTypeNames.Color3fArray).Set([(1, 0, 0)])
        stage.DefinePrim("/World/C_003/Extra", "Xform")
        stage.OverridePrim("/World/C_004/Body")  # an empty over is not an edit
        layer = stage.GetRootLayer()
        before = layer.ExportToString()
        for dry in (True, False):
            result = pa.make_instanceable(stage, paths, dry_run=dry)
            json.dumps(result)
            assert result["ok"] is False and result["written"] is False
            refused = {item["path"]: item for item in result["refused"]}
            assert set(refused) == {"/World/C_002", "/World/C_003"}
            edit = refused["/World/C_002"]["local_edits"][0]
            assert edit["path"] == "/World/C_002/Body" and edit["properties"] == ["primvars:displayColor"]
            assert "ignore" in edit["plain"]
            added = refused["/World/C_003"]["local_edits"][0]
            assert added["path"] == "/World/C_003/Extra" and added["specifier"] == "def"
            assert layer.ExportToString() == before
        report = pa.perf_report(stage)
        group = [g for g in report["instancing"]["groups"] if g["kind"] == "same_reference"][0]
        assert group["copies_with_local_edits"] == 2


def test_make_instanceable_refuses_prims_without_arcs_and_bad_input():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp)
        result = pa.make_instanceable(stage, paths + ["/World/Plain", "/World/Missing"], dry_run=False)
        reasons = {item["path"]: item["reason"] for item in result["refused"]}
        assert result["ok"] is False and set(reasons) == {"/World/Plain", "/World/Missing"}
        assert "no reference or payload" in reasons["/World/Plain"]
        assert not any(stage.GetPrimAtPath(path).IsInstance() for path in paths)
        assert pa.make_instanceable(stage, [])["ok"] is False
        assert pa.make_instanceable(stage, ["World/C_000"])["ok"] is False
        nested = pa.make_instanceable(stage, ["/World", "/World/C_000"])
        assert nested["ok"] is False


def test_make_instanceable_rolls_back_when_writing_fails():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp)
        layer = stage.GetRootLayer()
        before = layer.ExportToString()
        real = pa._author_instanceable
        calls = []

        def flaky(target_layer, spec_path):
            calls.append(spec_path)
            if len(calls) == 3:
                raise RuntimeError("simulated failure")
            real(target_layer, spec_path)

        with _patched(_author_instanceable=flaky):
            result = pa.make_instanceable(stage, paths, dry_run=False)
        assert result["ok"] is False and result["rolled_back"] is True and result["written"] is False
        assert "simulated failure" in result["error"]
        assert layer.ExportToString() == before
        assert not any(stage.GetPrimAtPath(path).IsInstance() for path in paths)


def test_make_instanceable_refuses_when_a_stronger_layer_says_false():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp)
        stage.GetPrimAtPath(paths[0]).SetInstanceable(False)  # in the root layer (strongest)
        sub = Sdf.Layer.CreateAnonymous("weaker")
        stage.GetRootLayer().subLayerPaths.append(sub.identifier)
        stage.SetEditTarget(Usd.EditTarget(sub))
        before = sub.ExportToString()
        result = pa.make_instanceable(stage, paths, dry_run=False)
        assert result["ok"] is False and [item["path"] for item in result["refused"]] == [paths[0]]
        assert "stronger layer" in result["refused"][0]["reason"]
        assert sub.ExportToString() == before


def test_convert_to_payload_is_plan_only():
    with tempfile.TemporaryDirectory() as tmp:
        stage, paths = _small_scene(tmp)
        before = stage.GetRootLayer().ExportToString()
        plan = pa.convert_to_payload(stage, paths[:2] + ["/World/Plain"])
        json.dumps(plan)
        assert plan["ok"] and plan["plan_only"] and plan["written"] is False
        assert [p["path"] for p in plan["plans"]] == paths[:2]
        ref = plan["plans"][0]["references"][0]
        assert ref["asset_path"].replace("\\", "/").endswith("asset.usda") and ref["local"] is True
        assert "AddPayload(Sdf.Payload(" in plan["python"] and "RemoveReference(Sdf.Reference(" in plan["python"]
        compile(plan["python"], "<snippet>", "exec")
        assert plan["refused"][0]["path"] == "/World/Plain"
        assert stage.GetRootLayer().ExportToString() == before
        assert pa.convert_to_payload(stage, ["/World/Plain"])["ok"] is False


# -------------------------------------------------------------------------------------- live_frame_stats

_KIT_NAMES = ("omni", "omni.kit", "omni.kit.viewport", "omni.kit.viewport.utility", "omni.hydra", "omni.hydra.engine",
              "omni.hydra.engine.stats", "omni.kit.app", "carb", "carb.settings")
_MISSING = object()


@contextlib.contextmanager
def _fake_kit(modules):
    saved = {name: sys.modules.get(name, _MISSING) for name in _KIT_NAMES}
    try:
        for name in _KIT_NAMES:
            sys.modules[name] = None  # `import name` raises ImportError
        sys.modules.update(modules)
        pa._LAST_APP_SAMPLE.clear()
        yield
    finally:
        pa._LAST_APP_SAMPLE.clear()
        for name, module in saved.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def test_live_frame_stats_without_kit():
    if _IN_KIT:
        return
    with _fake_kit({}):
        result = pa.live_frame_stats()
    assert result["ok"] is False and "Kit not available" in result["reason"]


def test_live_frame_stats_with_fake_kit_modules():
    if _IN_KIT:
        return
    viewport = types.SimpleNamespace(fps=30.0, frame_info={"fps": 30.0, "subframe_count": 2, "device_mask": 1},
                                     resolution=(1280, 720))
    utility = types.ModuleType("omni.kit.viewport.utility")
    utility.get_active_viewport = lambda: viewport
    stats = types.ModuleType("omni.hydra.engine.stats")
    stats.get_device_info = lambda: [{"description": "RTX 4070", "budget": 8_000_000_000, "usage": 2_000_000_000},
                                     {"description": "", "budget": 4_000_000_000, "usage": 1_000_000_000}]
    clock = {"updates": 600, "seconds": 10.0}
    app_obj = types.SimpleNamespace(get_update_number=lambda: clock["updates"],
                                    get_time_since_start_s=lambda: clock["seconds"])
    kit_app = types.ModuleType("omni.kit.app")
    kit_app.get_app = lambda: app_obj
    values = {"/app/runLoops/main/rateLimitEnabled": True, "/app/runLoops/main/rateLimitFrequency": 60}
    settings = types.ModuleType("carb.settings")
    settings.get_settings = lambda: types.SimpleNamespace(get=values.get)
    with _fake_kit({"omni.kit.viewport.utility": utility, "omni.hydra.engine.stats": stats, "omni.kit.app": kit_app,
                    "carb.settings": settings}):
        first = pa.live_frame_stats()
        clock.update(updates=900, seconds=15.0)
        second = pa.live_frame_stats()
    json.dumps(first)
    assert first["ok"] is True
    assert first["viewport"] == {"fps": 30.0, "subframe_count": 2, "effective_fps": 60.0, "frame_time_ms": 16.67,
                                 "resolution": [1280, 720]}
    gpu = first["gpu_memory"]
    assert gpu[0]["device"] == "RTX 4070" and gpu[0]["used_fraction"] == 0.25 and gpu[0]["available_bytes"] == 6_000_000_000
    assert gpu[0]["rendering"] is True and gpu[1]["rendering"] is False and gpu[1]["device"] == "GPU 1"
    assert first["app_loop"]["average_fps_since_start"] == 60.0 and first["app_loop"]["fps_since_last_call"] is None
    assert second["app_loop"]["fps_since_last_call"] == 60.0
    assert first["rate_limit"] == {"enabled": True, "frequency_hz": 60.0}
    assert "frame cap" in first["plain"]
    assert len(first["sources"]) == 4


def test_live_frame_stats_viewport_missing_but_app_loop_works():
    if _IN_KIT:
        return
    utility = types.ModuleType("omni.kit.viewport.utility")
    utility.get_active_viewport = lambda: None
    kit_app = types.ModuleType("omni.kit.app")
    kit_app.get_app = lambda: types.SimpleNamespace(get_update_number=lambda: 10, get_time_since_start_s=lambda: 0.0)
    with _fake_kit({"omni.kit.viewport.utility": utility, "omni.kit.app": kit_app}):
        result = pa.live_frame_stats()
    assert result["ok"] is True and result["viewport"] is None
    assert result["app_loop"]["average_fps_since_start"] is None
    assert any("No active viewport" in note for note in result["notes"])
