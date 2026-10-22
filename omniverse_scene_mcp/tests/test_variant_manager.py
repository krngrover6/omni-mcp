"""Variant manager: list, explain, compare, check and safely edit variant sets (the bag configurator's Leather set).

Fixture-free test_* functions on temporary folders and in-memory layers only (no network, no Kit). The smoke test
opens the real course scene read-only when it is present and never saves it.
"""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import variant_manager as vm
from omniverse_scene_mcp.undo import UndoStack

LOOKS = "/World/Bag/Looks"
SHADER_COLOR = "/World/Bag/Looks/Leather_Mat/Shader.inputs:diffuseColor"

STEP2 = '''#usda 1.0
(
    defaultPrim = "World"
)
def Xform "World"
{
    def Xform "Bag" (
        variants = {
            string Style = "Classic"
        }
        prepend variantSets = "Style"
    )
    {
        def Mesh "Body" (
            prepend apiSchemas = ["MaterialBindingAPI"]
        )
        {
        }
        def Scope "Looks" (
            variants = {
                string Leather = "White"
            }
            prepend variantSets = "Leather"
        )
        {
            def Material "Leather_Mat"
            {
                token outputs:surface.connect = </World/Bag/Looks/Leather_Mat/Shader.outputs:surface>
                def Shader "Shader"
                {
                    uniform token info:id = "UsdPreviewSurface"
                    token outputs:surface
                }
            }
            def Material "Black_Mat"
            {
            }
            variantSet "Leather" = {
                "Black" {
                    over "Leather_Mat"
                    {
                        over "Shader"
                        {
                            color3f inputs:diffuseColor = (0.02, 0.02, 0.02)
                            float inputs:roughness = 0.3
                        }
                    }
                }
                "White" {
                    over "Leather_Mat"
                    {
                        over "Shader"
                        {
                            color3f inputs:diffuseColor = (0.9, 0.88, 0.8)
                            float inputs:roughness = 0.6
                        }
                    }
                }
            }
        }
        variantSet "Style" = {
            "Classic" {
                over "Body"
                {
                    rel material:binding = </World/Bag/Looks/Leather_Mat>
                }
            }
            "Modern" {
                over "Body"
                {
                    rel material:binding = </World/Bag/Looks/Black_Mat>
                }
            }
        }
    }
}
'''

MAIN = '''#usda 1.0
(
    subLayers = [
        @./configurator.usda@,
        @./step2.usda@
    ]
)
'''

CONFIGURATOR = '''#usda 1.0
'''


@contextmanager
def bag_scene(configurator: str = CONFIGURATOR, step2: str = STEP2):
    """main.usda -> [configurator.usda (stronger), step2.usda (holds the variants)] in a temp folder."""
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        (root / "step2.usda").write_text(step2, encoding="utf-8")
        (root / "configurator.usda").write_text(configurator, encoding="utf-8")
        (root / "main.usda").write_text(MAIN, encoding="utf-8")
        stage = Usd.Stage.Open(str(root / "main.usda"))
        try:
            yield stage, root
        finally:
            del stage


def _layer(stage, name):
    return next(layer for layer in stage.GetUsedLayers() if layer.identifier.replace("\\", "/").endswith("/" + name))


def _fingerprint(stage):
    """Every used layer's text and dirty flag, plus the session sublayers and edit target."""
    layers = {layer.identifier: (layer.ExportToString(), bool(layer.dirty)) for layer in stage.GetUsedLayers()}
    return layers, list(stage.GetSessionLayer().subLayerPaths), stage.GetEditTarget().GetLayer().identifier


def _files(root):
    return {path.name: path.read_bytes() for path in sorted(Path(root).glob("*.usda"))}


def _assert_json(result):
    json.dumps(result)
    return result


def _color(stage, variant=None):
    looks = stage.GetPrimAtPath(LOOKS)
    if variant is not None:
        looks.GetVariantSet("Leather").SetVariantSelection(variant)
    return stage.GetAttributeAtPath(SHADER_COLOR).Get()


# --- read-only ------------------------------------------------------------------------------------------------


def test_list_variants_reports_sets_selection_layer_and_content():
    with bag_scene() as (stage, root):
        before = _fingerprint(stage)
        result = _assert_json(vm.list_variants(stage))
        assert result["ok"] and result["read_only"], result
        by_path = {prim["path"]: prim for prim in result["prims"]}
        assert set(by_path) == {"/World/Bag", LOOKS}
        leather = by_path[LOOKS]["variant_sets"][0]
        assert leather["name"] == "Leather"
        assert leather["variants"] == ["Black", "White"]
        assert leather["selection"] == "White" and leather["selection_source"] == "authored"
        assert leather["selection_authored_in"]["layer_short"] == "step2.usda"
        assert leather["selection_authored_in"]["local"] is True
        black = next(item for item in leather["variant_details"] if item["name"] == "Black")
        assert black["content"][0]["layer_short"] == "step2.usda"
        assert black["content"][0]["spec_path"] == LOOKS + "{Leather=Black}"
        assert black["opinion_count"] == 2 and not black["changes_nothing"]
        sub = vm.list_variants(stage, LOOKS)
        assert [prim["path"] for prim in sub["prims"]] == [LOOKS]
        assert _fingerprint(stage) == before
        assert not vm.list_variants(stage, "/Nope")["ok"]


def test_variant_contents_explains_colour_in_plain_english():
    with bag_scene() as (stage, _root):
        result = _assert_json(vm.variant_contents(stage, LOOKS, "Leather", "Black"))
        assert result["ok"], result
        assert not result["selected"] and result["current_selection"] == "White"
        assert f"Black sets {SHADER_COLOR} to (0.02, 0.02, 0.02)" in result["summary"]
        colour = next(op for op in result["opinions"] if op["path"] == SHADER_COLOR)
        assert colour["layer_short"] == "step2.usda" and colour["spec_path"].startswith(LOOKS + "{Leather=Black}")
        assert colour["value"] == [0.019999999552965164] * 3 or len(colour["value"]) == 3
        assert "raw" not in colour
        assert "not selected" in result["explanation"]


def test_variant_contents_reports_material_binding():
    with bag_scene() as (stage, _root):
        result = vm.variant_contents(stage, "/World/Bag", "Style", "Modern")
        assert result["ok"], result
        binding = next(op for op in result["opinions"] if op["kind"] == "material_binding")
        assert binding["value"] == ["/World/Bag/Looks/Black_Mat"]
        assert "Modern binds /World/Bag/Body to material /World/Bag/Looks/Black_Mat" in result["summary"]
        refused = vm.variant_contents(stage, LOOKS, "Leather", "Pink")
        assert not refused["ok"] and "Black" in refused["error"]
        assert not vm.variant_contents(stage, LOOKS, "Nope", "Black")["ok"]
        assert not vm.variant_contents(stage, LOOKS + "{Leather=Black}", "Leather", "Black")["ok"]


NESTED = STEP2.replace(
    '''        variantSet "Style" = {''',
    '''        variantSet "Size" = {
            "Small" (
                variants = {
                    string Strap = "Short"
                }
                prepend variantSets = "Strap"
            ) {
                variantSet "Strap" = {
                    "Short" {
                        over "Body"
                        {
                            float strapLength = 10
                        }
                    }
                    "Long" {
                        over "Body"
                        {
                            float strapLength = 40
                        }
                    }
                }
            }
            "Large" {
                over "Body"
                {
                    float bagScale = 2
                }
            }
        }
        variantSet "Style" = {''',
).replace('string Style = "Classic"', 'string Size = "Large"\n            string Style = "Classic"').replace(
    'prepend variantSets = "Style"', 'prepend variantSets = ["Style", "Size"]')


def test_nested_variant_sets_are_listed_and_probed():
    with bag_scene(step2=NESTED) as (stage, _root):
        before = _fingerprint(stage)
        listing = vm.list_variants(stage, "/World/Bag")
        sets = {item["name"]: item for item in listing["prims"][0]["variant_sets"]}
        assert sets["Size"]["selection"] == "Large"
        assert sets["Strap"]["composed"] is False and sets["Strap"]["nested_inside"] == {"Size": "Small"}
        small = next(item for item in sets["Size"]["variant_details"] if item["name"] == "Small")
        assert small["nested_variant_sets"] == ["Strap"]
        contents = vm.variant_contents(stage, "/World/Bag", "Strap", "Long")
        assert contents["ok"], contents
        assert contents["sites"][0]["inside"] == {"Size": "Small"}
        assert "Long sets /World/Bag/Body.strapLength to 40" in contents["summary"]
        compared = vm.compare_variants(stage, "/World/Bag", "Strap", "Short", "Long")
        assert compared["ok"], compared
        assert [d["path"] for d in compared["composed_differences"]] == ["/World/Bag/Body.strapLength"]
        assert compared["composed_differences"][0]["a"] == 10 and compared["composed_differences"][0]["b"] == 40
        assert compared["stage_unchanged"]
        assert _fingerprint(stage) == before


def test_compare_variants_authored_and_composed_without_touching_the_stage():
    with bag_scene() as (stage, root):
        before, files = _fingerprint(stage), _files(root)
        result = _assert_json(vm.compare_variants(stage, LOOKS, "Leather", "Black", "White"))
        assert result["ok"], result
        authored = {(d["path"], d["field"]) for d in result["authored_differences"]}
        assert authored == {(SHADER_COLOR, "default"), ("/World/Bag/Looks/Leather_Mat/Shader.inputs:roughness", "default")}
        composed = {d["path"] for d in result["composed_differences"]}
        assert composed == {SHADER_COLOR, "/World/Bag/Looks/Leather_Mat/Shader.inputs:roughness"}
        assert result["stage_unchanged"] and not result["identical"]
        assert _fingerprint(stage) == before and _files(root) == files
        assert stage.GetPrimAtPath(LOOKS).GetVariantSet("Leather").GetVariantSelection() == "White"
        assert not vm.compare_variants(stage, LOOKS, "Leather", "Black", "Black")["ok"]


def test_compare_shows_a_stronger_local_opinion_hiding_the_variant():
    override = '''#usda 1.0
over "World"
{
    over "Bag"
    {
        over "Looks"
        {
            over "Leather_Mat"
            {
                over "Shader"
                {
                    color3f inputs:diffuseColor = (1, 0, 0)
                }
            }
        }
    }
}
'''
    with bag_scene(configurator=override) as (stage, _root):
        result = vm.compare_variants(stage, LOOKS, "Leather", "Black", "White")
        assert result["ok"], result
        assert SHADER_COLOR in {d["path"] for d in result["authored_differences"]}
        assert SHADER_COLOR not in {d["path"] for d in result["composed_differences"]}
        assert "hides" in result["explanation"]
        check = vm.check_variants(stage, LOOKS)
        hidden = [issue for issue in check["issues"] if issue["kind"] == "hidden_by_stronger_opinion"]
        assert {issue["variant"] for issue in hidden} == {"Black", "White"}
        assert all(issue["hidden_by"]["layer_short"] == "configurator.usda" for issue in hidden)


def test_check_flags_direct_opinions_in_the_same_layer_that_hide_variants():
    hidden_here = STEP2.replace(
        '''                    uniform token info:id = "UsdPreviewSurface"
''', '''                    uniform token info:id = "UsdPreviewSurface"
                    float inputs:roughness = 0.5
''').replace(
        '''            prepend apiSchemas = ["MaterialBindingAPI"]
        )
        {
        }''', '''            prepend apiSchemas = ["MaterialBindingAPI"]
        )
        {
            rel material:binding = </World/Bag/Looks/Leather_Mat>
        }''')
    with bag_scene(step2=hidden_here) as (stage, _root):
        result = vm.check_variants(stage)
        hidden = {(issue["variant"], issue["attribute"]) for issue in result["issues"]
                  if issue["kind"] == "hidden_by_stronger_opinion"}
        roughness = "/World/Bag/Looks/Leather_Mat/Shader.inputs:roughness"
        assert {("Black", roughness), ("White", roughness)} <= hidden, result["issues"]
        assert ("Modern", "/World/Bag/Body.material:binding") in hidden, result["issues"]
        assert any(issue["kind"] == "same_as_other_variant" and issue["variant_set"] == "Style" for issue in result["issues"])


# --- duplicate (the Pink exercise) -----------------------------------------------------------------------------


def test_duplicate_dry_run_plans_pink_and_writes_nothing():
    with bag_scene() as (stage, root):
        before, files = _fingerprint(stage), _files(root)
        result = _assert_json(vm.duplicate_variant(
            stage, LOOKS, "Leather", "White", "Pink",
            overrides={"Leather_Mat/Shader.inputs:diffuseColor": [0.95, 0.55, 0.7]}))
        assert result["ok"] and result["dry_run"] and not result["applied"], result
        plan = result["plan"]
        assert plan["copies"] == [{
            "layer": _layer(stage, "step2.usda").identifier, "layer_short": "step2.usda",
            "source_layer_short": "step2.usda", "from": LOOKS + "{Leather=White}", "to": LOOKS + "{Leather=Pink}"}]
        assert any("pass layer=" in w for w in result["warnings"])  # step2 is not the edit target (main.usda)
        assert plan["overrides"][0]["path"] == SHADER_COLOR and plan["overrides"][0]["type"] == "color3f"
        assert any("Pink sets " + SHADER_COLOR in line for line in plan["predicted_contents"])
        assert "next_step" in result
        assert _fingerprint(stage) == before and _files(root) == files


def test_duplicate_white_to_pink_changes_only_the_colour():
    with bag_scene() as (stage, root):
        step2 = _layer(stage, "step2.usda")
        white_before = step2.GetPrimAtPath(LOOKS + "{Leather=White}").GetAsText() if hasattr(
            step2.GetPrimAtPath(LOOKS + "{Leather=White}"), "GetAsText") else None
        white_value = _color(stage)
        others_before = {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()
                         if layer.identifier != step2.identifier}
        result = _assert_json(vm.duplicate_variant(
            stage, LOOKS, "Leather", "White", "Pink",
            overrides={SHADER_COLOR: [0.95, 0.55, 0.7]}, dry_run=False))
        assert result["ok"] and result["applied"], result
        assert result["hidden_overrides"] == []
        assert result["differences_from_source"] and len(result["differences_from_source"]) == 1
        looks = stage.GetPrimAtPath(LOOKS)
        assert looks.GetVariantSet("Leather").GetVariantNames() == ["Black", "Pink", "White"]
        assert looks.GetVariantSet("Leather").GetVariantSelection() == "White"
        assert _color(stage) == white_value
        assert step2.GetPrimAtPath(LOOKS + "{Leather=Pink}")
        if white_before is not None:
            assert step2.GetPrimAtPath(LOOKS + "{Leather=White}").GetAsText() == white_before
        assert {layer.identifier: layer.ExportToString() for layer in stage.GetUsedLayers()
                if layer.identifier != step2.identifier} == others_before
        compared = vm.compare_variants(stage, LOOKS, "Leather", "White", "Pink")
        assert [(d["path"], d["field"]) for d in compared["authored_differences"]] == [(SHADER_COLOR, "default")]
        assert [d["path"] for d in compared["composed_differences"]] == [SHADER_COLOR]
        pink = compared["composed_differences"][0]["b"]
        assert [round(value, 3) for value in pink] == [0.95, 0.55, 0.7]
        assert _color(stage, "Pink") == Gf.Vec3f(0.95, 0.55, 0.7)
        assert abs(stage.GetAttributeAtPath("/World/Bag/Looks/Leather_Mat/Shader.inputs:roughness").Get() - 0.6) < 1e-6
        assert not (Path(root) / "step2.usda").read_text(encoding="utf-8").count("Pink")  # nothing saved


def test_duplicate_pink_into_my_own_stronger_layer_leaves_the_asset_untouched():
    with bag_scene() as (stage, root):
        step2 = _layer(stage, "step2.usda")
        step2_text = step2.ExportToString()
        result = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink", layer="configurator.usda",
                                      overrides={SHADER_COLOR: [0.95, 0.55, 0.7]}, dry_run=False)
        assert result["ok"], result
        assert result["plan"]["copies"][0]["layer_short"] == "configurator.usda"
        assert result["plan"]["copies"][0]["source_layer_short"] == "step2.usda"
        assert result["plan"]["overrides"][0]["layer_short"] == "configurator.usda"
        assert any("keep both layers" in w for w in result["warnings"])
        assert step2.ExportToString() == step2_text and not step2.dirty
        mine = _layer(stage, "configurator.usda")
        assert mine.GetPrimAtPath(LOOKS + "{Leather=Pink}")
        compared = vm.compare_variants(stage, LOOKS, "Leather", "White", "Pink")
        assert [d["path"] for d in compared["composed_differences"]] == [SHADER_COLOR]
        assert _color(stage, "Pink") == Gf.Vec3f(0.95, 0.55, 0.7)
        assert (root / "configurator.usda").read_text(encoding="utf-8") == CONFIGURATOR


def test_duplicate_warns_when_a_stronger_opinion_hides_the_new_colour():
    override = """#usda 1.0
over "World"
{
    over "Bag"
    {
        over "Looks"
        {
            over "Leather_Mat"
            {
                over "Shader"
                {
                    color3f inputs:diffuseColor = (1, 0, 0)
                }
            }
        }
    }
}
"""
    with bag_scene(configurator=override) as (stage, _root):
        result = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink",
                                      overrides={SHADER_COLOR: [0.95, 0.55, 0.7]}, dry_run=False)
        assert result["ok"], result
        assert result["hidden_overrides"][0]["hidden_by"]["layer_short"] == "configurator.usda"
        assert any("hidden by a stronger opinion" in w for w in result["warnings"])


def test_duplicate_refusals_write_nothing():
    with bag_scene() as (stage, root):
        before = _fingerprint(stage)
        color = {SHADER_COLOR: [0.9, 0.4, 0.6]}
        cases = [
            dict(source="White", new_name="Black"),
            dict(source="White", new_name="1Pink"),
            dict(source="White", new_name="Pi nk"),
            dict(source="Ivory", new_name="Pink"),
            dict(source="White", new_name="Pink", overrides={"Leather_Mat/Shader.inputs:nope": 1.0}),
            dict(source="White", new_name="Pink", overrides={SHADER_COLOR: "pink"}),
            dict(source="White", new_name="Pink", overrides={SHADER_COLOR: [1, 2]}),
            dict(source="White", new_name="Pink", overrides={SHADER_COLOR: [True, 0, 0]}),
            dict(source="White", new_name="Pink", overrides={"/World/Other.x": 1.0}),
            dict(source="White", new_name="Pink", overrides=["not", "a", "dict"]),
            dict(source="White", new_name="Pink", overrides=color, layer="elsewhere.usda"),
        ]
        for case in cases:
            result = _assert_json(vm.duplicate_variant(stage, LOOKS, "Leather", dry_run=False, **case))
            assert not result["ok"] and not result["applied"], (case, result)
        assert not vm.duplicate_variant(stage, LOOKS, "Nope", "White", "Pink", dry_run=False)["ok"]
        assert _fingerprint(stage) == before
        step2 = _layer(stage, "step2.usda")
        step2.SetPermissionToEdit(False)
        try:
            result = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink", dry_run=False)
            assert not result["ok"] and "read-only" in result["error"]
        finally:
            step2.SetPermissionToEdit(True)
        assert _fingerprint(stage) == before


def test_duplicate_rolls_back_when_authoring_fails():
    with bag_scene() as (stage, _root):
        before = _fingerprint(stage)
        original = vm._author_override

        def broken(*_args, **_kwargs):
            raise RuntimeError("simulated failure after the copy")

        vm._author_override = broken
        try:
            result = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink",
                                          overrides={SHADER_COLOR: [0.9, 0.4, 0.6]}, dry_run=False)
        finally:
            vm._author_override = original
        assert not result["ok"] and result["rolled_back"] and not result["applied"], result
        assert "simulated failure" in result["error"]
        texts = {identifier: text for identifier, (text, _dirty) in _fingerprint(stage)[0].items()}
        assert texts == {identifier: text for identifier, (text, _dirty) in before[0].items()}
        assert "Pink" not in stage.GetPrimAtPath(LOOKS).GetVariantSet("Leather").GetVariantNames()


def test_duplicate_rolls_back_when_validation_fails():
    with bag_scene() as (stage, _root):
        before = {identifier: text for identifier, (text, _d) in _fingerprint(stage)[0].items()}
        original = vm._authored_diff
        calls = []

        def lying(first, second, a, b):
            calls.append(1)
            diffs = original(first, second, a, b)
            return diffs + [{"path": "/World/Bag/Looks.fake", "field": "default", "a": 1, "b": 2, "text": "fake diff"}]

        vm._authored_diff = lying
        try:
            result = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink", dry_run=False)
        finally:
            vm._authored_diff = original
        assert calls and not result["ok"] and result["rolled_back"], result
        assert "validation failed" in result["error"]
        after = {identifier: text for identifier, (text, _d) in _fingerprint(stage)[0].items()}
        assert after == before


def test_duplicate_is_undoable():
    with bag_scene() as (stage, _root):
        before = {identifier: text for identifier, (text, _d) in _fingerprint(stage)[0].items()}
        stack = UndoStack()
        result = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink",
                                      overrides={SHADER_COLOR: (0.95, 0.55, 0.7)}, undo_stack=stack, dry_run=False)
        assert result["ok"] and result["undo"]["recorded"], result
        undone = stack.undo_last(stage=stage, dry_run=False)
        assert undone["ok"] and undone["verified"], undone
        after = {identifier: text for identifier, (text, _d) in _fingerprint(stage)[0].items()}
        assert after == before


def test_duplicate_into_variant_authored_in_root_and_sublayer_copies_both():
    extra = '''#usda 1.0
over "World"
{
    over "Bag"
    {
        over "Looks"
        {
            variantSet "Leather" = {
                "White" {
                    over "Leather_Mat"
                    {
                        over "Shader"
                        {
                            float inputs:clearcoat = 1
                        }
                    }
                }
            }
        }
    }
}
'''
    with bag_scene(configurator=extra) as (stage, _root):
        result = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink",
                                      overrides={SHADER_COLOR: [0.95, 0.55, 0.7]}, dry_run=False)
        assert result["ok"], result
        assert [copy["layer_short"] for copy in result["plan"]["copies"]] == ["configurator.usda", "step2.usda"]
        # the colour lives in step2 (not in configurator's part of White), so the override goes there
        assert result["plan"]["overrides"][0]["layer_short"] == "step2.usda"
        assert not any("adds a new opinion" in w for w in result["warnings"])
        compared = vm.compare_variants(stage, LOOKS, "Leather", "White", "Pink")
        assert [d["path"] for d in compared["composed_differences"]] == [SHADER_COLOR]
        only_one = vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Rose", layer="step2.usda")
        assert only_one["ok"] and any("only step2.usda is copied" in w for w in only_one["warnings"])


def test_variant_from_a_referenced_asset_is_reported_and_not_duplicated():
    asset = '''#usda 1.0
(
    defaultPrim = "Asset"
)
def Scope "Asset" (
    variants = {
        string Leather = "White"
    }
    prepend variantSets = "Leather"
)
{
    variantSet "Leather" = {
        "White" {
            float colourKey = 1
        }
        "Black" {
            float colourKey = 2
        }
    }
}
'''
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "asset.usda").write_text(asset, encoding="utf-8")
        stage = Usd.Stage.CreateInMemory()
        prim = stage.DefinePrim("/World/Bag")
        prim.GetReferences().AddReference(str(Path(folder) / "asset.usda"))
        listing = vm.list_variants(stage)
        record = listing["prims"][0]["variant_sets"][0]
        white = next(item for item in record["variant_details"] if item["name"] == "White")
        assert white["content"][0]["local"] is False and white["content"][0]["spec_path"] == "/Asset{Leather=White}"
        assert record["selection_authored_in"]["local"] is False
        contents = vm.variant_contents(stage, "/World/Bag", "Leather", "Black")
        assert "Black sets /World/Bag.colourKey to 2" in contents["summary"]
        refused = vm.duplicate_variant(stage, "/World/Bag", "Leather", "White", "Pink", dry_run=False)
        assert not refused["ok"] and "reference or payload" in refused["error"]
        assert not vm.rename_variant(stage, "/World/Bag", "Leather", "Black", "Ebony", dry_run=False)["ok"]
        selected = vm.set_selection(stage, "/World/Bag", "Leather", "Black", dry_run=False)
        assert selected["ok"] and selected["selection"] == "Black"
        assert stage.GetAttributeAtPath("/World/Bag.colourKey").Get() == 2
        del stage


# --- rename / remove / create / select -------------------------------------------------------------------------


def test_rename_refuses_the_current_selection_unless_switch_to():
    with bag_scene() as (stage, _root):
        before = _fingerprint(stage)
        refused = vm.rename_variant(stage, LOOKS, "Leather", "White", "Ivory", dry_run=False)
        assert not refused["ok"] and "switch_to='Ivory'" in refused["error"]
        assert not vm.rename_variant(stage, LOOKS, "Leather", "White", "Black", switch_to="Black", dry_run=False)["ok"]
        assert not vm.rename_variant(stage, LOOKS, "Leather", "White", "Ivory", switch_to="White", dry_run=False)["ok"]
        dry = vm.rename_variant(stage, LOOKS, "Leather", "White", "Ivory", switch_to="Ivory")
        assert dry["ok"] and dry["dry_run"] and dry["plan"]["selection_rewrites"][0]["to"] == "Ivory"
        assert _fingerprint(stage) == before
        white = _color(stage)
        done = vm.rename_variant(stage, LOOKS, "Leather", "White", "Ivory", switch_to="Ivory", dry_run=False)
        assert done["ok"], done
        looks = stage.GetPrimAtPath(LOOKS)
        assert looks.GetVariantSet("Leather").GetVariantNames() == ["Black", "Ivory"]
        assert looks.GetVariantSet("Leather").GetVariantSelection() == "Ivory"
        assert _color(stage) == white
        other = vm.rename_variant(stage, LOOKS, "Leather", "Black", "Ebony", dry_run=False)
        assert other["ok"] and stage.GetPrimAtPath(LOOKS).GetVariantSet("Leather").GetVariantSelection() == "Ivory"


def test_remove_variant_and_switch():
    with bag_scene() as (stage, _root):
        refused = vm.remove_variant(stage, LOOKS, "Leather", "White", dry_run=False)
        assert not refused["ok"] and "current selection" in refused["error"]
        stack = UndoStack()
        done = vm.remove_variant(stage, LOOKS, "Leather", "White", switch_to="Black", undo_stack=stack, dry_run=False)
        assert done["ok"], done
        looks = stage.GetPrimAtPath(LOOKS)
        assert looks.GetVariantSet("Leather").GetVariantNames() == ["Black"]
        assert looks.GetVariantSet("Leather").GetVariantSelection() == "Black"
        assert _color(stage) == Gf.Vec3f(0.02, 0.02, 0.02)
        assert stack.undo_last(stage=stage, dry_run=False)["ok"]
        assert looks.GetVariantSet("Leather").GetVariantNames() == ["Black", "White"]
        assert looks.GetVariantSet("Leather").GetVariantSelection() == "White"


def test_create_variant_set_and_set_selection_strength():
    with bag_scene() as (stage, root):
        before = _fingerprint(stage)
        bad = [
            dict(name="Leather", variants=["A"]),
            dict(name="Hard ware", variants=["A"]),
            dict(name="Hardware", variants=["Gold", "Gold"]),
            dict(name="Hardware", variants="Gold"),
            dict(name="Hardware", variants=["Gold"], selection="Silver"),
            dict(name="Hardware", variants=["Gold"], layer="nowhere.usda"),
        ]
        for case in bad:
            assert not vm.create_variant_set(stage, LOOKS, dry_run=False, **case)["ok"], case
        assert _fingerprint(stage) == before
        made = vm.create_variant_set(stage, LOOKS, "Hardware", ["Gold", "Silver"], selection="Gold",
                                     layer="configurator.usda", dry_run=False)
        assert made["ok"], made
        looks = stage.GetPrimAtPath(LOOKS)
        assert looks.GetVariantSet("Hardware").GetVariantNames() == ["Gold", "Silver"]
        assert looks.GetVariantSet("Hardware").GetVariantSelection() == "Gold"
        assert _layer(stage, "configurator.usda").GetPrimAtPath(LOOKS).variantSets["Hardware"]
        # step2 is weaker than configurator: a selection there does not win once configurator selects Leather
        strong = vm.set_selection(stage, LOOKS, "Leather", "Black", layer="configurator.usda", dry_run=False)
        assert strong["ok"] and strong["selection"] == "Black", strong
        weak = vm.set_selection(stage, LOOKS, "Leather", "White", layer="step2.usda")
        assert weak["ok"] and weak["plan"]["will_win"] is False
        assert any("configurator.usda is stronger" in w for w in weak["warnings"])
        assert vm.set_selection(stage, LOOKS, "Leather", "White", layer="step2.usda", dry_run=False)["ok"]
        assert looks.GetVariantSet("Leather").GetVariantSelection() == "Black"
        check = vm.check_variants(stage, LOOKS)
        assert any(issue["kind"] == "selection_overridden" for issue in check["issues"])
        assert not vm.set_selection(stage, LOOKS, "Leather", "Pink", dry_run=False)["ok"]
        assert not (root / "configurator.usda").read_text(encoding="utf-8").count("Hardware")


def test_set_selection_defaults_to_the_edit_target_and_is_undoable():
    with bag_scene() as (stage, _root):
        before = {identifier: text for identifier, (text, _d) in _fingerprint(stage)[0].items()}
        stack = UndoStack()
        result = vm.set_selection(stage, LOOKS, "Leather", "Black", undo_stack=stack, dry_run=False)
        assert result["ok"] and result["plan"]["layer"] == stage.GetRootLayer().identifier, result
        assert _color(stage) == Gf.Vec3f(0.02, 0.02, 0.02)
        same = vm.set_selection(stage, LOOKS, "Leather", "Black", dry_run=False)
        assert same["ok"] and not same["applied"]
        assert stack.undo_last(stage=stage, dry_run=False)["ok"]
        after = {identifier: text for identifier, (text, _d) in _fingerprint(stage)[0].items()}
        assert after == before


# --- check_variants --------------------------------------------------------------------------------------------


BROKEN = STEP2.replace(
    '''                "White" {''',
    '''                "Empty" {
                }
                "Twin" {
                    over "Leather_Mat"
                    {
                        over "Shader"
                        {
                            color3f inputs:diffuseColor = (0.9, 0.88, 0.8)
                            float inputs:roughness = 0.6
                        }
                    }
                }
                "Textured" {
                    over "Leather_Mat"
                    {
                        over "Shader"
                        {
                            asset inputs:file = @./textures/missing_leather.png@
                        }
                    }
                }
                "White" {''',
).replace(
    '''            "Modern" {
                over "Body"
                {
                    rel material:binding = </World/Bag/Looks/Black_Mat>
                }
            }''',
    '''            "Modern" {
                over "Body"
                {
                    rel material:binding = </World/Bag/Looks/Black_Mat>
                }
            }
            "Lost" {
                over "Body"
                {
                    rel material:binding = </World/Bag/Looks/Gone_Mat>
                }
            }''',
).replace(
    '''            variantSet "Leather" = {''',
    '''            variantSet "Orphan" = {
                "Only" {
                }
            }
            variantSet "Leather" = {''',
)


def test_check_variants_reports_problems_and_leaves_the_stage_alone():
    with bag_scene(step2=BROKEN) as (stage, root):
        before, files = _fingerprint(stage), _files(root)
        result = _assert_json(vm.check_variants(stage))
        assert result["ok"] and result["stage_unchanged"], result
        kinds = {(issue["kind"], issue.get("variant")) for issue in result["issues"]}
        assert ("changes_nothing", "Empty") in kinds
        assert ("same_as_other_variant", "White") in kinds or ("same_as_other_variant", "Twin") in kinds
        assert ("missing_texture", "Textured") in kinds
        assert ("missing_material", "Lost") in kinds
        assert ("unlisted_variant_set", None) in kinds
        assert not result["ok_variants"] and result["error_count"] >= 2
        assert result["variants_checked"] == 8
        assert _fingerprint(stage) == before and _files(root) == files
        assert stage.GetPrimAtPath(LOOKS).GetVariantSet("Leather").GetVariantSelection() == "White"


def test_check_variants_clean_scene_and_invalid_selection():
    with bag_scene() as (stage, _root):
        clean = vm.check_variants(stage)
        assert clean["ok"] and clean["ok_variants"] and clean["warning_count"] == 0, clean["issues"]
        assert clean["variants_checked"] == 4
        stage.GetSessionLayer()  # selection authored in the session layer for this check only
        with Usd.EditContext(stage, stage.GetSessionLayer()):
            stage.GetPrimAtPath(LOOKS).GetVariantSet("Leather").SetVariantSelection("Teal")
        broken = vm.check_variants(stage, LOOKS)
        assert any(issue["kind"] == "invalid_selection" for issue in broken["issues"]), broken["issues"]
        assert not vm.check_variants(stage, "/World/Bag/Body")["ok"]


def test_dry_runs_of_every_writer_leave_layers_byte_identical():
    with bag_scene() as (stage, root):
        before, files = _fingerprint(stage), _files(root)
        results = [
            vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink", overrides={SHADER_COLOR: [1, 0.5, 0.7]}),
            vm.rename_variant(stage, LOOKS, "Leather", "Black", "Ebony"),
            vm.remove_variant(stage, LOOKS, "Leather", "Black"),
            vm.create_variant_set(stage, LOOKS, "Hardware", ["Gold"]),
            vm.set_selection(stage, LOOKS, "Leather", "Black"),
            vm.duplicate_variant(stage, LOOKS, "Leather", "White", "Pink", dry_run="yes"),
        ]
        for result in results:
            _assert_json(result)
            assert result["ok"] and result["dry_run"] and not result["applied"], result
        assert _fingerprint(stage) == before and _files(root) == files


def test_bad_stage_and_paths_are_refused():
    assert not vm.list_variants(None)["ok"]
    assert not vm.check_variants(None)["ok"]
    stage = Usd.Stage.CreateInMemory()
    stage.DefinePrim("/World")
    assert vm.list_variants(stage)["ok"] and vm.list_variants(stage)["prim_count"] == 0
    assert not vm.variant_contents(stage, "World", "a", "b")["ok"]
    assert not vm.variant_contents(stage, "/World.attr", "a", "b")["ok"]
    assert not vm.check_variants(stage, "/World")["ok"]
    made = vm.create_variant_set(stage, "/World", "Look", ["A", "B"], selection="A", dry_run=False)
    assert made["ok"], made
    assert stage.GetPrimAtPath("/World").GetVariantSet("Look").GetVariantSelection() == "A"
    nothing = vm.check_variants(stage, "/World")
    assert {issue["kind"] for issue in nothing["issues"]} >= {"changes_nothing", "same_as_other_variant"}


# --- read-only smoke on the real course scene ------------------------------------------------------------------


def test_smoke_real_bag_scene_read_only():
    workspace = Path(__file__).resolve().parents[2]  # local-only course assets, not in git
    candidates = [
        workspace / "Bag_Assets" / "Step2_Variants_Savepoint.usd",
        workspace / "bag_configurator_course" / "assets" / "Step2_Variants_Savepoint.usd",
    ]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        return  # the course assets are not on this machine
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    mtime = os.path.getmtime(path)
    stage = Usd.Stage.Open(str(path))
    try:
        before = {layer.identifier: bool(layer.dirty) for layer in stage.GetUsedLayers()}
        listing = vm.list_variants(stage)
        assert listing["ok"], listing
        looks = next((prim for prim in listing["prims"] if prim["path"].endswith("/Looks")), None)
        if looks is not None:
            leather = next(item for item in looks["variant_sets"] if item["name"] == "Leather")
            assert {"Black", "White"} <= set(leather["variants"])
            contents = vm.variant_contents(stage, looks["path"], "Leather", "White")
            assert contents["ok"] and contents["opinion_count"] >= 1, contents
            check = vm.check_variants(stage, looks["path"])
            assert check["ok"] and check["stage_unchanged"], check
        assert {layer.identifier: bool(layer.dirty) for layer in stage.GetUsedLayers()} == before
    finally:
        del stage
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest and os.path.getmtime(path) == mtime
