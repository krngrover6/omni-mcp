"""Layer manager: stack order, opinion lookup, sublayer/mute/edit-target changes and moving opinions between layers.

Fixture-free test_* functions on temporary folders and in-memory layers only (no network, no Kit).
"""

import json
import os
import tempfile
from contextlib import contextmanager

try:
    from pxr import Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import layer_manager as lm
from omniverse_scene_mcp.undo import UndoStack

ASSET = '''#usda 1.0
def Xform "A"
{
    double x = 9
    double onlyRef = 4
}
'''
C_LAYER = '''#usda 1.0
over "W"
{
    double x = 3
}
'''
B_LAYER = '''#usda 1.0
(
    subLayers = [
        @./c.usda@ (offset = 5)
    ]
)

over "W"
{
    double x = 2

    def Cube "C"
    {
        double size = 3
        rel friend = </W>

        def Xform "K"
        {
        }
    }
}
'''
A_LAYER = '''#usda 1.0
over "W"
{
    double x = 1
}
'''
ROOT = '''#usda 1.0
(
    subLayers = [
        @./a.usda@,
        @./b.usda@ (offset = 10; scale = 2)
    ]
)

def Xform "W" (
    references = @./asset.usda@</A>
)
{
}
'''


@contextmanager
def _folder():
    with tempfile.TemporaryDirectory(prefix="lm-test-", ignore_cleanup_errors=True) as path:
        yield path


def _write(folder, name, text):
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _scene(folder):
    _write(folder, "asset.usda", ASSET)
    _write(folder, "c.usda", C_LAYER)
    _write(folder, "b.usda", B_LAYER)
    _write(folder, "a.usda", A_LAYER)
    root = _write(folder, "root.usda", ROOT)
    return Usd.Stage.Open(root)


def _layer(stage, name):
    for layer in stage.GetLayerStack(True):
        if layer.identifier.replace("\\", "/").endswith("/" + name):
            return layer
    raise AssertionError(f"{name} not in the stack")


def _state(stage):
    """Text and dirty flag of every layer the stage uses: a dry run must leave all of them identical."""
    return {layer.identifier: (layer.ExportToString(), layer.dirty) for layer in stage.GetUsedLayers()}


def _json_ok(result):
    json.dumps(result)
    return result


# --- layer_stack ------------------------------------------------------------------------------------------------


def test_layer_stack_orders_session_root_and_nested_sublayers_with_offsets():
    with _folder() as folder:
        stage = _scene(folder)
        result = _json_ok(lm.layer_stack(stage))
        assert result["ok"] and result["read_only"] and not result["applied"]
        names = [item["display_name"] for item in result["layers"]]
        assert names[0].startswith("session layer")
        assert names[1:] == ["root.usda", "a.usda", "b.usda", "c.usda"]
        assert [item["strength_rank"] for item in result["layers"]] == [0, 1, 2, 3, 4]
        roles = [item["role"] for item in result["layers"]]
        assert roles == ["session", "root", "sublayer", "sublayer", "sublayer"]
        by_name = {item["display_name"]: item for item in result["layers"]}
        assert by_name["c.usda"]["parent"].endswith("b.usda") and by_name["c.usda"]["depth"] == 2
        assert by_name["b.usda"]["offset"] == {"offset": 10, "scale": 2}
        assert by_name["c.usda"]["offset"] == {"offset": 5, "scale": 1}
        assert by_name["c.usda"]["offset_to_root"] == {"offset": 20, "scale": 2}  # 10 + 2 * 5
        assert by_name["root.usda"]["is_edit_target"] and not by_name["a.usda"]["is_edit_target"]
        assert by_name["b.usda"]["prim_spec_count"] == 3  # W, C, K
        assert by_name["a.usda"]["dirty"] is False and by_name["a.usda"]["read_only"] is False
        assert by_name["a.usda"]["file_writable"] is True and by_name["a.usda"]["permission_to_edit"] is True
        assert by_name[names[0]]["anonymous"] is True
        refs = result["referenced_layers"]
        assert refs["count"] == 1 and refs["layers"][0]["display_name"] == "asset.usda"
        assert refs["layers"][0]["arcs"] == ["reference"] and refs["layers"][0]["used_by"] == ["/W"]
        assert "LIVRPS" in result["explanation"] and "root.usda > a.usda > b.usda > c.usda" in result["explanation"]
        assert "time samples only" in result["explanation"]


def test_layer_stack_lists_muted_and_missing_sublayers():
    with _folder() as folder:
        stage = _scene(folder)
        stage.GetRootLayer().subLayerPaths.append("./ghost.usda")
        stage.MuteLayer(_layer(stage, "c.usda").identifier)
        result = lm.layer_stack(stage)
        by_name = {item["display_name"]: item for item in result["layers"]}
        assert by_name["c.usda"]["muted"] and by_name["c.usda"]["strength_rank"] is None
        assert by_name["ghost.usda"]["missing"] and by_name["ghost.usda"]["strength_rank"] is None
        assert any("ghost.usda" in warning for warning in result["warnings"])
        assert "Muted layers are ignored" in result["explanation"]


# --- where_is ---------------------------------------------------------------------------------------------------


def test_where_is_attribute_lists_sublayers_and_reference_and_names_the_winner():
    with _folder() as folder:
        stage = _scene(folder)
        result = _json_ok(lm.where_is(stage, "/W", "x"))
        assert result["ok"] and result["value"] == 1.0
        names = [entry["display_name"] for entry in result["opinions"]]
        assert names == ["a.usda", "b.usda", "c.usda", "asset.usda"]
        assert result["winner"]["display_name"] == "a.usda" and result["winner"]["wins"]
        assert result["opinions"][-1]["arc_type"] == "reference" and not result["opinions"][-1]["in_local_layer_stack"]
        assert result["opinions"][0]["in_local_layer_stack"] and result["opinions"][0]["local_strength_rank"] == 2
        assert "a.usda" in result["explanation"]
        same = lm.where_is(stage, "/W.x")
        assert same["winner"]["display_name"] == "a.usda"


def test_where_is_shows_variant_opinions_weaker_than_local():
    stage = Usd.Stage.CreateInMemory()
    cube = UsdGeom.Cube.Define(stage, "/Car")
    variants = cube.GetPrim().GetVariantSets().AddVariantSet("size")
    variants.AddVariant("big")
    variants.SetVariantSelection("big")
    with variants.GetVariantEditContext():
        cube.CreateSizeAttr(10.0)
    result = lm.where_is(stage, "/Car", "size")
    assert result["ok"] and result["value"] == 10.0
    assert result["winner"]["arc_type"] == "variant" and "{size=big}" in result["winner"]["spec_path"]
    cube.GetSizeAttr().Set(2.0)  # a local opinion now beats the variant
    result = lm.where_is(stage, "/Car", "size")
    assert result["value"] == 2.0
    assert [entry["arc_type"] for entry in result["opinions"]] == ["local", "variant"]
    assert result["opinions"][0]["wins"] and not result["opinions"][1]["wins"]


def test_where_is_prim_and_relationship():
    with _folder() as folder:
        stage = _scene(folder)
        prim = lm.where_is(stage, "/W")
        assert prim["ok"] and prim["type_name"] == "Xform"
        layers = [entry["display_name"] for entry in prim["opinions"]]
        assert layers == ["root.usda", "a.usda", "b.usda", "c.usda", "asset.usda"]
        assert "type_name" in prim["opinions"][0]["wins"] and "specifier" in prim["opinions"][0]["wins"]
        rel = lm.where_is(stage, "/W/C", "friend")
        assert rel["property_type"] == "relationship" and rel["value"] == ["/W"]
        assert rel["winner"]["display_name"] == "b.usda" and "list-edited" in rel["explanation"]
        missing = lm.where_is(stage, "/W/Nope")
        assert not missing["ok"] and "does not exist" in missing["error"]
        bad = lm.where_is(stage, "/W/C", "nothing")
        assert not bad["ok"] and "size" in bad["error"]


# --- add / remove sublayer ----------------------------------------------------------------------------------------


def test_add_sublayer_dry_run_is_byte_identical_then_applies_relative_path():
    with _folder() as folder:
        stage = _scene(folder)
        _write(folder, "lighting.usda", '#usda 1.0\nover "W"\n{\n    double x = 7\n}\n')
        before = _state(stage)
        plan = _json_ok(lm.add_sublayer(stage, "lighting.usda"))
        assert plan["ok"] and plan["dry_run"] and not plan["applied"]
        assert plan["plan"]["authored_path"] == "./lighting.usda" and plan["plan"]["index"] == 0
        assert plan["dry_run_dirtied_layers"] == [] and _state(stage) == before
        done = _json_ok(lm.add_sublayer(stage, "lighting.usda", dry_run=False))
        assert done["ok"] and done["applied"] and done["loaded"]
        assert list(stage.GetRootLayer().subLayerPaths)[0] == "./lighting.usda"
        assert stage.GetPrimAtPath("/W").GetAttribute("x").Get() == 7.0  # strongest sublayer now wins
        assert done["undo"] == {"function": "remove_sublayer", "layer_identifier": "./lighting.usda"}


def test_add_sublayer_weakest_with_offset_and_create_if_missing():
    with _folder() as folder:
        stage = _scene(folder)
        target = os.path.join(folder, "new_layer.usda")
        plan = lm.add_sublayer(stage, "new_layer.usda", position="weakest", create_if_missing=True, offset=3, scale=0.5)
        assert plan["ok"] and plan["plan"]["would_create_file"] and not os.path.exists(target)
        assert plan["plan"]["index"] == 2
        done = lm.add_sublayer(stage, "new_layer.usda", position="weakest", create_if_missing=True, offset=3, scale=0.5,
                               dry_run=False)
        assert done["ok"] and os.path.isfile(target)
        root = stage.GetRootLayer()
        assert list(root.subLayerPaths)[-1] == "./new_layer.usda"
        assert root.subLayerOffsets[2] == Sdf.LayerOffset(3, 0.5)


def test_add_sublayer_refusals():
    with _folder() as folder:
        stage = _scene(folder)
        before = _state(stage)
        cases = {
            "a.usda": "already in the layer stack",
            "c.usda": "already in the layer stack",
            "notes.txt": "is not a USD layer",
            "missing.usda": "does not exist",
            "root.usda": "itself",
        }
        for path, words in cases.items():
            result = lm.add_sublayer(stage, path)
            assert not result["ok"] and words in result["error"], (path, result["error"])
        outside_dir = tempfile.mkdtemp(prefix="lm-outside-", dir=os.path.dirname(folder))
        try:
            outside = _write(outside_dir, "far.usda", "#usda 1.0\n")
            refused = lm.add_sublayer(stage, outside)
            assert not refused["ok"] and "outside" in refused["error"]
            allowed = lm.add_sublayer(stage, outside, allow_outside=True)
            assert allowed["ok"] and allowed["plan"]["outside_root_folder"]
            assert allowed["plan"]["authored_path"].startswith("../")
        finally:
            for name in os.listdir(outside_dir):
                os.remove(os.path.join(outside_dir, name))
            os.rmdir(outside_dir)
        loop = _write(folder, "loop.usda", "#usda 1.0\n(\n    subLayers = [\n        @./root.usda@\n    ]\n)\n")
        cycle = lm.add_sublayer(stage, loop)
        assert not cycle["ok"] and "cycle" in cycle["error"]
        bad_position = lm.add_sublayer(stage, "a.usda", position=7)
        assert not bad_position["ok"]
        assert not lm.add_sublayer(stage, "x.usda", position="middle")["ok"]
        assert _state(stage) == before


def test_remove_sublayer_keeps_file_and_reports_restore():
    with _folder() as folder:
        stage = _scene(folder)
        before = _state(stage)
        plan = _json_ok(lm.remove_sublayer(stage, "b.usda"))
        assert plan["ok"] and plan["plan"]["index"] == 1 and plan["plan"]["nested_sublayers_also_leaving"] == ["c.usda"]
        assert plan["restore_with"]["offset"] == 10 and plan["restore_with"]["scale"] == 2
        assert _state(stage) == before
        done = lm.remove_sublayer(stage, "b.usda", dry_run=False)
        assert done["ok"] and done["applied"]
        assert [str(path) for path in stage.GetRootLayer().subLayerPaths] == ["./a.usda"]
        assert os.path.isfile(os.path.join(folder, "b.usda"))
        assert not stage.GetPrimAtPath("/W/C")
        restore = done["restore_with"]
        back = lm.add_sublayer(stage, restore["layer_path"], position=restore["position"], offset=restore["offset"],
                               scale=restore["scale"], relative=False, allow_outside=True, dry_run=False)
        assert back["ok"] and stage.GetPrimAtPath("/W/C")
        assert stage.GetRootLayer().subLayerOffsets[1] == Sdf.LayerOffset(10, 2)


def test_remove_sublayer_refuses_root_edit_target_unknown_and_unsaved():
    with _folder() as folder:
        stage = _scene(folder)
        assert "root layer" in lm.remove_sublayer(stage, "root")["error"]
        unknown = lm.remove_sublayer(stage, "zzz.usda")
        assert not unknown["ok"] and "Known layers" in unknown["error"] and "a.usda" in unknown["error"]
        stage.SetEditTarget(Usd.EditTarget(_layer(stage, "c.usda")))
        refused = lm.remove_sublayer(stage, "b.usda")
        assert not refused["ok"] and "edit target" in refused["error"]
        stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
        _layer(stage, "a.usda").GetPrimAtPath("/W").SetInfo("comment", "unsaved")
        unsaved = lm.remove_sublayer(stage, "a.usda")
        assert not unsaved["ok"] and "unsaved" in unsaved["error"]
        forced = lm.remove_sublayer(stage, "a.usda", discard_unsaved_edits=True)
        assert forced["ok"] and forced["warnings"]


# --- edit target and muting ---------------------------------------------------------------------------------------


def test_set_edit_target_switches_and_restores():
    with _folder() as folder:
        stage = _scene(folder)
        plan = _json_ok(lm.set_edit_target(stage, "b.usda"))
        assert plan["ok"] and plan["dry_run"] and stage.GetEditTarget().GetLayer() == stage.GetRootLayer()
        assert "stronger" in plan["explanation"]
        done = lm.set_edit_target(stage, "b.usda", dry_run=False)
        assert done["ok"] and done["applied"]
        assert stage.GetEditTarget().GetLayer() == _layer(stage, "b.usda")
        assert done["previous_edit_target"]["identifier"] == stage.GetRootLayer().identifier
        restore = done["restore_with"]
        back = lm.set_edit_target(stage, restore["layer_identifier"], dry_run=False)
        assert back["ok"] and stage.GetEditTarget().GetLayer() == stage.GetRootLayer()
        again = lm.set_edit_target(stage, "root", dry_run=False)
        assert again["ok"] and again["already"] and not again["applied"]
        session = lm.set_edit_target(stage, "session")
        assert session["ok"] and any("session" in warning for warning in session["warnings"])
        assert not lm.set_edit_target(stage, "asset.usda")["ok"]  # referenced, not local
        _layer(stage, "a.usda").SetPermissionToEdit(False)
        locked = lm.set_edit_target(stage, "a.usda")
        assert not locked["ok"] and "read-only" in locked["error"]


def test_mute_and_unmute_layer():
    with _folder() as folder:
        stage = _scene(folder)
        attr = stage.GetPrimAtPath("/W").GetAttribute("x")
        plan = _json_ok(lm.mute_layer(stage, "a.usda"))
        assert plan["ok"] and not stage.IsLayerMuted(_layer(stage, "a.usda").identifier)
        done = lm.mute_layer(stage, "a.usda", dry_run=False)
        assert done["ok"] and done["applied"] and attr.Get() == 2.0
        listed = {item["display_name"]: item for item in lm.layer_stack(stage)["layers"]}
        assert listed["a.usda"]["muted"]
        assert lm.mute_layer(stage, "a.usda")["already"]
        back_plan = lm.unmute_layer(stage, "a.usda")
        assert back_plan["ok"] and attr.Get() == 2.0
        back = lm.unmute_layer(stage, "a.usda", dry_run=False)
        assert back["ok"] and back["applied"] and attr.Get() == 1.0
        assert not lm.unmute_layer(stage, "a.usda")["ok"]
        ref = lm.mute_layer(stage, "asset.usda", dry_run=False)
        assert ref["ok"] and stage.GetPrimAtPath("/W").GetAttribute("onlyRef").Get() is None
        assert lm.unmute_layer(stage, "asset.usda", dry_run=False)["ok"]


def test_mute_layer_refusals():
    with _folder() as folder:
        stage = _scene(folder)
        assert not lm.mute_layer(stage, "root")["ok"]
        assert not lm.mute_layer(stage, "session")["ok"]
        stage.SetEditTarget(Usd.EditTarget(_layer(stage, "c.usda")))
        refused = lm.mute_layer(stage, "b.usda")  # contains the edit target c.usda
        assert not refused["ok"] and "edit target" in refused["error"]
        stage.SetEditTarget(Usd.EditTarget(stage.GetRootLayer()))
        _layer(stage, "b.usda").GetPrimAtPath("/W").SetInfo("comment", "unsaved")
        unsaved = lm.mute_layer(stage, "b.usda")
        assert not unsaved["ok"] and "unsaved" in unsaved["error"]


# --- move_opinions ------------------------------------------------------------------------------------------------


def test_move_opinions_to_stronger_layer_keeps_composed_values():
    with _folder() as folder:
        stage = _scene(folder)
        before = _state(stage)
        plan = _json_ok(lm.move_opinions(stage, "/W/C", "b.usda", "root"))
        assert plan["ok"] and plan["dry_run"] and plan["composed_changes"] == []
        assert plan["plan"]["destination_is"] == "stronger" and plan["composed_values_compared"] >= 2
        assert plan["plan"]["moves"]["child_prims"] == ["K"] and "size" in plan["plan"]["moves"]["properties"]
        assert plan["dry_run_dirtied_layers"] == [] and _state(stage) == before
        done = lm.move_opinions(stage, "/W/C", "b.usda", "root", dry_run=False)
        assert done["ok"] and done["applied"] and done["composed_changes"] == []
        root, b_layer = stage.GetRootLayer(), _layer(stage, "b.usda")
        assert root.GetPrimAtPath("/W/C") and root.GetPrimAtPath("/W/C/K") and not b_layer.GetPrimAtPath("/W/C")
        assert root.GetPrimAtPath("/W/C").specifier == Sdf.SpecifierDef
        cube = stage.GetPrimAtPath("/W/C")
        assert cube.GetTypeName() == "Cube" and cube.GetAttribute("size").Get() == 3.0
        assert [str(t) for t in cube.GetRelationship("friend").GetTargets()] == ["/W"]


def test_move_opinions_to_weaker_layer_warns_when_a_middle_layer_overrides():
    with _folder() as folder:
        stage = _scene(folder)
        root = stage.GetRootLayer()
        Sdf.CreatePrimInLayer(root, "/W/C")
        Sdf.AttributeSpec(root.GetPrimAtPath("/W/C"), "size", Sdf.ValueTypeNames.Double).default = 1.0
        a_layer = _layer(stage, "a.usda")
        Sdf.CreatePrimInLayer(a_layer, "/W/C")
        Sdf.AttributeSpec(a_layer.GetPrimAtPath("/W/C"), "size", Sdf.ValueTypeNames.Double).default = 2.0
        assert stage.GetPrimAtPath("/W/C").GetAttribute("size").Get() == 1.0
        before = _state(stage)
        plan = lm.move_opinions(stage, "/W/C", "root", "b.usda", properties=["size"])
        assert plan["ok"] and plan["plan"]["destination_is"] == "weaker"
        change = next(item for item in plan["composed_changes"] if item["what"] == "size")
        assert change["before"]["value"] == 1.0 and change["after"]["value"] == 2.0
        assert any("a.usda" in warning for warning in plan["warnings"])
        assert plan["plan"]["layers_in_between_with_opinions"] == ["a.usda"]
        assert _state(stage) == before
        refused = lm.move_opinions(stage, "/W/C", "root", "b.usda", properties=["size"], dry_run=False)
        assert not refused["ok"] and "allow_changes" in refused["error"] and _state(stage) == before
        done = lm.move_opinions(stage, "/W/C", "root", "b.usda", properties=["size"], allow_changes=True, dry_run=False)
        assert done["ok"] and done["applied"] and stage.GetPrimAtPath("/W/C").GetAttribute("size").Get() == 2.0
        assert _layer(stage, "b.usda").GetAttributeAtPath("/W/C.size").default == 1.0  # b's old 3 lost to stronger root
        assert not root.GetAttributeAtPath("/W/C.size")


def test_move_opinions_merges_into_existing_destination_by_strength():
    stage = Usd.Stage.CreateInMemory()
    root = stage.GetRootLayer()
    weak = Sdf.Layer.CreateAnonymous("weak")
    root.subLayerPaths.append(weak.identifier)
    weak.ImportFromString('''#usda 1.0
def Xform "P" (
    customData = {int fromWeak = 1}
)
{
    double a = 5
    double b.timeSamples = {
        1: 10,
        2: 20,
    }
    double c = 7
}
''')
    root.ImportFromString('''#usda 1.0
(
    subLayers = [
        @''' + weak.identifier + '''@
    ]
)

over "P" (
    customData = {int fromRoot = 2}
)
{
    double a = 1
    double b.timeSamples = {
        3: 30,
    }
}
''')
    prim = stage.GetPrimAtPath("/P")
    expected = (prim.GetAttribute("a").Get(), prim.GetAttribute("b").GetTimeSamples(), prim.GetAttribute("c").Get())
    assert expected == (1.0, [3.0], 7.0)
    result = lm.move_opinions(stage, "/P", weak.identifier, "root", dry_run=False)
    assert result["ok"] and result["composed_changes"] == [], result["composed_changes"]
    spec = root.GetPrimAtPath("/P")
    assert spec.specifier == Sdf.SpecifierDef and spec.typeName == "Xform"
    assert root.GetAttributeAtPath("/P.a").default == 1.0  # the stronger value is kept
    assert root.ListTimeSamplesForPath("/P.b") == [3.0]  # weaker samples are not mixed in
    assert root.GetAttributeAtPath("/P.c").default == 7.0
    assert dict(spec.customData) == {"fromRoot": 2, "fromWeak": 1}
    assert not weak.GetPrimAtPath("/P")
    assert (prim.GetAttribute("a").Get(), prim.GetAttribute("b").GetTimeSamples(), prim.GetAttribute("c").Get()) == expected


def test_move_opinions_without_children_leaves_them_in_an_over():
    with _folder() as folder:
        stage = _scene(folder)
        done = lm.move_opinions(stage, "/W/C", "b.usda", "a.usda", include_children=False, dry_run=False)
        assert done["ok"] and done["composed_changes"] == []
        a_layer, b_layer = _layer(stage, "a.usda"), _layer(stage, "b.usda")
        assert a_layer.GetPrimAtPath("/W/C").typeName == "Cube" and not a_layer.GetPrimAtPath("/W/C/K")
        left = b_layer.GetPrimAtPath("/W/C")
        assert left.specifier == Sdf.SpecifierOver and not left.typeName and not left.properties
        assert b_layer.GetPrimAtPath("/W/C/K")
        assert stage.GetPrimAtPath("/W/C/K").IsDefined()


def test_move_opinions_rolls_back_on_failure():
    with _folder() as folder:
        stage = _scene(folder)
        texts = {layer.identifier: layer.ExportToString() for layer in stage.GetLayerStack(True)}
        original = lm._remove_from_source
        calls = {"n": 0}

        def fail_second(*args, **kwargs):
            # The first call is the simulation on in-memory copies; the second is the real write, after the
            # destination was already written, so the rollback has to undo that too.
            calls["n"] += 1
            if calls["n"] == 1:
                return original(*args, **kwargs)
            raise RuntimeError("disk on fire")

        lm._remove_from_source = fail_second
        try:
            result = lm.move_opinions(stage, "/W/C", "b.usda", "root", dry_run=False)
        finally:
            lm._remove_from_source = original
        assert not result["ok"] and result["rolled_back"] and "disk on fire" in result["error"]
        assert not result["applied"]
        assert {layer.identifier: layer.ExportToString() for layer in stage.GetLayerStack(True)} == texts
        assert stage.GetPrimAtPath("/W/C").GetAttribute("size").Get() == 3.0


def test_move_opinions_records_undo_entry():
    with _folder() as folder:
        stage = _scene(folder)
        texts = {layer.identifier: layer.ExportToString() for layer in stage.GetLayerStack(True)}
        stack = UndoStack()
        done = lm.move_opinions(stage, "/W/C", "b.usda", "root", undo_stack=stack, dry_run=False)
        assert done["ok"] and done["undo"]["recorded"] and len(stack) == 1
        undone = stack.undo_last(stage=stage, dry_run=False)
        assert undone["ok"], undone
        assert {layer.identifier: layer.ExportToString() for layer in stage.GetLayerStack(True)} == texts


def test_move_opinions_refusals():
    with _folder() as folder:
        stage = _scene(folder)
        before = _state(stage)
        assert "same layer" in lm.move_opinions(stage, "/W/C", "b.usda", "b.usda")["error"]
        assert "nothing to move" in lm.move_opinions(stage, "/W/C", "a.usda", "root")["error"]
        assert "Known layers" in lm.move_opinions(stage, "/W/C", "nope.usda", "root")["error"]
        assert not lm.move_opinions(stage, "/W/C", "asset.usda", "root")["ok"]  # referenced layer
        assert not lm.move_opinions(stage, "W/C", "b.usda", "root")["ok"]
        assert not lm.move_opinions(stage, "/W/C{v=a}", "b.usda", "root")["ok"]
        assert "no property spec" in lm.move_opinions(stage, "/W/C", "b.usda", "root", properties=["nope"])["error"]
        stage.MuteLayer(_layer(stage, "a.usda").identifier)
        assert "muted" in lm.move_opinions(stage, "/W/C", "b.usda", "a.usda")["error"]
        stage.UnmuteLayer(stage.GetMutedLayers()[0])
        _layer(stage, "a.usda").SetPermissionToEdit(False)
        assert "read-only" in lm.move_opinions(stage, "/W/C", "b.usda", "a.usda")["error"]
        assert _state(stage) == before


# --- flatten_preview and dry-run guarantees --------------------------------------------------------------------------


def test_flatten_preview_bakes_sublayers_and_references():
    with _folder() as folder:
        stage = _scene(folder)
        before = _state(stage)
        result = _json_ok(lm.flatten_preview(stage, "/W"))
        assert result["ok"] and result["read_only"] and not result["truncated"]
        text = result["usda"]
        assert "double x = 1" in text and "onlyRef = 4" in text and 'def Cube "C"' in text
        assert "references" not in text and "subLayers" not in text
        assert "reference" in result["explanation"]
        assert not lm.flatten_preview(stage, "/Nope")["ok"]
        whole = lm.flatten_preview(stage)
        assert whole["ok"] and whole["prim_path"] == "/"
        assert _state(stage) == before
    big = Usd.Stage.CreateInMemory()
    for index in range(40):
        UsdGeom.Cube.Define(big, f"/Big/Cube{index}")
    small = lm.flatten_preview(big, "/Big", max_chars=200)
    assert small["ok"] and small["truncated"] and small["chars"] > 200 and len(small["usda"]) < 260


def test_dry_runs_never_change_or_dirty_any_layer():
    with _folder() as folder:
        stage = _scene(folder)
        _write(folder, "extra.usda", "#usda 1.0\n")
        before = _state(stage)
        target = stage.GetEditTarget().GetLayer()
        results = [
            lm.add_sublayer(stage, "extra.usda"),
            lm.add_sublayer(stage, "brand_new.usda", create_if_missing=True),
            lm.remove_sublayer(stage, "a.usda"),
            lm.set_edit_target(stage, "b.usda"),
            lm.mute_layer(stage, "c.usda"),
            lm.move_opinions(stage, "/W/C", "b.usda", "root"),
            lm.move_opinions(stage, "/W/C", "b.usda", "a.usda", include_children=False),
            lm.move_opinions(stage, "/W/C", "b.usda", "session", properties=["size"]),
        ]
        for result in results:
            assert result["ok"] and result["dry_run"] and not result["applied"], result
            json.dumps(result)
        assert _state(stage) == before
        assert stage.GetEditTarget().GetLayer() == target and not stage.GetMutedLayers()
        assert not os.path.exists(os.path.join(folder, "brand_new.usda"))


def test_no_stage_and_bad_arguments_are_refused():
    for call in (
        lambda: lm.layer_stack(None),
        lambda: lm.where_is(None, "/A"),
        lambda: lm.add_sublayer(None, "a.usda"),
        lambda: lm.flatten_preview(None),
    ):
        result = call()
        assert not result["ok"] and "No USD stage" in result["error"]
    stage = Usd.Stage.CreateInMemory()
    in_memory = lm.add_sublayer(stage, "a.usda")
    assert not in_memory["ok"] and "in memory" in in_memory["error"]
    assert not lm.add_sublayer(stage, "")["ok"]
    assert not lm.where_is(stage, "not a path")["ok"]
