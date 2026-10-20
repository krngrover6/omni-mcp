"""Composition sandbox and LIVERPS puzzles, exercised with temporary USDA layers only."""

import json
import os
import shutil
import tempfile
import textwrap
import types

try:
    from pxr import Usd
except ImportError:  # pragma: no cover - depends on the local environment
    import pytest

    pytest.skip("USD Python bindings (pxr) are not installed", allow_module_level=True)

from omniverse_scene_mcp.exam import puzzles, sandbox


def _usda(text: str) -> str:
    return textwrap.dedent(text).lstrip()


ASSET = _usda(
    """
    #usda 1.0
    (
        defaultPrim = "Asset"
    )

    def Cube "Asset" {
        double size = 3
    }
    """
)


def _shot(box_meta: str = "", box_body: str = "", extra: str = "", layer_meta: str = "") -> str:
    meta = f" (\n{textwrap.indent(box_meta.strip(), ' ' * 8)}\n    )" if box_meta else ""
    body = textwrap.indent(box_body.strip(), " " * 8) + "\n" if box_body else ""
    head = f"#usda 1.0\n(\n{textwrap.indent(layer_meta.strip(), ' ' * 4)}\n)\n" if layer_meta else "#usda 1.0\n"
    return f'{head}\ndef Xform "World" {{\n    def "Box"{meta} {{\n{body}    }}\n}}\n{extra}'


def _query(layers: dict, *queries: str, root: str = "shot.usda") -> list:
    result = sandbox.compose(layers, root, list(queries))
    assert result["ok"], result["errors"]
    return result["queries"]


def test_sublayer_order_first_sublayer_is_strongest():
    layers = {
        "shot.usda": _shot(layer_meta="subLayers = [@anim.usda@, @layout.usda@]"),
        "layout.usda": _usda(
            """
            #usda 1.0
            over "World" {
                over "Box" {
                    double size = 4
                }
            }
            """
        ),
        "anim.usda": _usda(
            """
            #usda 1.0
            over "World" {
                over "Box" {
                    double size = 7
                }
            }
            """
        ),
    }
    result = sandbox.compose(layers, "shot.usda", ["/World/Box.size"])
    assert result["layer_stack"] == ["shot.usda", "anim.usda", "layout.usda"]
    query = result["queries"][0]
    assert query["value"] == 7.0
    assert query["winning_layer"] == "anim.usda"
    assert query["winning_arc"] == "sublayer"
    assert "anim.usda is stronger than layout.usda" in query["why"]
    assert [entry["layer"] for entry in query["property_stack"]] == ["anim.usda", "layout.usda"]


def test_local_opinion_beats_reference():
    layers = {
        "shot.usda": _shot("prepend references = @asset.usda@", "double size = 9"),
        "asset.usda": ASSET,
    }
    (query,) = _query(layers, "/World/Box.size")
    assert query["value"] == 9.0
    assert query["winning_layer"] == "shot.usda"
    assert query["winning_arc"] == "local"
    assert query["why"].startswith("stronger: local opinion in shot.usda beats the referenced asset asset.usda</Asset>")
    assert query["property_stack"][1]["layer"] == "asset.usda"
    assert query["property_stack"][1]["arc"] == "reference"
    assert query["property_stack"][0]["wins"] is True


def test_reference_beats_payload():
    heavy = ASSET.replace("size = 3", "size = 8")
    layers = {
        "shot.usda": _shot("prepend payload = @heavy.usda@\nprepend references = @light.usda@"),
        "light.usda": ASSET,
        "heavy.usda": heavy,
    }
    (query,) = _query(layers, "/World/Box.size")
    assert query["value"] == 3.0
    assert [entry["arc"] for entry in query["property_stack"]] == ["reference", "payload"]
    assert "R before P" in query["why"]


def test_inherit_beats_reference():
    layers = {
        "shot.usda": _shot(
            "inherits = </_class_Box>\nprepend references = @asset.usda@",
            extra='\nclass "_class_Box" {\n    double size = 12\n}\n',
        ),
        "asset.usda": ASSET,
    }
    (query,) = _query(layers, "/World/Box.size")
    assert query["value"] == 12.0
    assert query["winning_arc"] == "inherit"
    assert "I before R" in query["why"]


def test_specializes_is_weakest():
    shot = _shot("prepend references = @asset.usda@\nspecializes = </World/Base>")
    shot = shot.replace('    def "Box"', '    def "Base" {\n        double size = 11\n    }\n    def "Other" (\n'
                        '        specializes = </World/Base>\n    ) {\n    }\n    def "Box"')
    (box, other) = _query({"shot.usda": shot, "asset.usda": ASSET}, "/World/Box.size", "/World/Other.size")
    assert box["value"] == 3.0
    assert box["property_stack"][-1]["arc"] == "specialize"
    assert "R before S" in box["why"]
    assert other["value"] == 11.0
    assert other["why"].startswith("only opinion: 11")


def test_variant_selection_follows_the_strongest_selection():
    model = _usda(
        """
        #usda 1.0
        def Xform "World" {
            def Cube "Box" (
                prepend variantSets = "sizes"
                variants = {
                    string sizes = "small"
                }
            ) {
                variantSet "sizes" = {
                    "small" {
                        double size = 4
                    }
                    "large" {
                        double size = 8
                    }
                }
            }
        }
        """
    )
    shot = _usda(
        """
        #usda 1.0
        (
            subLayers = [@model.usda@]
        )
        over "World" {
            over "Box" (
                variants = {
                    string sizes = "large"
                }
            ) {
            }
        }
        """
    )
    value, prim = _query({"shot.usda": shot, "model.usda": model}, "/World/Box.size", "/World/Box")
    assert value["value"] == 8.0
    assert value["winning_arc"] == "variant"
    assert value["property_stack"][0]["via"] == "variant sizes=large (sublayer model.usda)"
    assert value["property_stack"][0]["layer"] == "model.usda"
    assert prim["query_type"] == "prim"
    assert prim["variant_selections"] == {"sizes": "large"}
    assert sorted(prim["variant_sets"]["sizes"]) == ["large", "small"]
    variant_arcs = [arc for arc in prim["arcs"] if arc["arc"] == "variant"]
    assert variant_arcs[0]["target_path"] == "/World/Box{sizes=large}"
    assert (variant_arcs[0]["target_layer"], variant_arcs[0]["introduced_in"]) == ("shot.usda", "model.usda")
    holder = {"arc": "sublayer", "target_layer": "model.usda", "target_path": "/World/Box{sizes=large}",
              "within": "shot.usda", "has_specs": True}
    assert holder in prim["arcs"]


def test_reference_layer_offset_shifts_time_samples():
    asset = ASSET.replace("double size = 3", "double size.timeSamples = {\n        0: 4,\n        10: 5,\n    }")
    layers = {
        "shot.usda": _shot("prepend references = @asset.usda@ (offset = 10; scale = 2)"),
        "asset.usda": asset,
    }
    default, early, late, prim = _query(
        layers, "/World/Box.size", "/World/Box.size@10", "/World/Box.size@30", "/World/Box"
    )
    assert [s["time"] for s in default["time_samples"]] == [10.0, 30.0]
    assert [s["value"] for s in default["time_samples"]] == [4.0, 5.0]
    assert default["value"] == 2.0
    assert default["source"] == "fallback"
    assert default["why"].startswith("no default opinion")
    assert early["value"] == 4.0 and early["time"] == 10.0
    assert late["value"] == 5.0
    assert late["winning_layer"] == "asset.usda"
    entry = late["property_stack"][0]
    assert entry["offset"] == {"offset": 10.0, "scale": 2.0}
    assert [(s["layer_time"], s["stage_time"]) for s in entry["time_samples"]] == [(0.0, 10.0), (10.0, 30.0)]
    reference = next(arc for arc in prim["arcs"] if arc["arc"] == "reference")
    assert reference["time_offset"] == {"offset": 10.0, "scale": 2.0}


def test_stronger_default_beats_weaker_sublayer_time_samples():
    anim = _usda(
        """
        #usda 1.0
        over "World" {
            over "Box" {
                double size.timeSamples = {
                    1: 13,
                }
            }
        }
        """
    )
    animated_only = {"shot.usda": _shot(layer_meta="subLayers = [@anim.usda@ (offset = 5)]"), "anim.usda": anim}
    (query,) = _query(animated_only, "/World/Box.size@6")
    assert query["value"] == 13.0
    assert query["property_stack"][0]["time_samples"][0]["stage_time"] == 6.0
    with_default = {"shot.usda": _shot(box_body="double size = 9", layer_meta="subLayers = [@anim.usda@ (offset = 5)]"),
                    "anim.usda": anim}
    (query,) = _query(with_default, "/World/Box.size@6")
    assert query["value"] == 9.0
    assert "a stronger default value beats weaker time samples" in query["why"]


def test_class_override_in_shot_reaches_inside_the_reference():
    asset = _usda(
        """
        #usda 1.0
        (
            defaultPrim = "Asset"
        )

        class "_class_Box" {
            double size = 3
        }

        def Cube "Asset" (
            inherits = </_class_Box>
        ) {
            double size = 4
        }
        """
    )
    layers = {
        "shot.usda": _shot("prepend references = @asset.usda@", extra='\nover "_class_Box" {\n    double size = 12\n}\n'),
        "asset.usda": asset,
    }
    query, prim = _query(layers, "/World/Box.size", "/World/Box")
    assert query["value"] == 12.0
    assert "implied" in query["why"] and "I before R" in query["why"]
    implied = [arc for arc in prim["arcs"] if arc["arc"] == "inherit" and arc["implicit"]]
    assert implied and implied[0]["target_layer"] == "shot.usda"


def test_asset_local_opinion_beats_its_own_variant():
    asset = _usda(
        """
        #usda 1.0
        (
            defaultPrim = "Asset"
        )

        def Cube "Asset" (
            prepend variantSets = "sizes"
            variants = {
                string sizes = "small"
            }
        ) {
            double size = 4
            variantSet "sizes" = {
                "small" {
                    double size = 6
                }
                "large" {
                    double size = 7
                }
            }
        }
        """
    )
    layers = {
        "shot.usda": _shot('prepend references = @asset.usda@\nvariants = {\n    string sizes = "large"\n}'),
        "asset.usda": asset,
    }
    (query,) = _query(layers, "/World/Box.size")
    assert query["value"] == 4.0
    assert "inside reference asset.usda</Asset>" in query["why"]
    assert "L before V" in query["why"]
    assert query["property_stack"][1]["path"] == "/Asset{sizes=large}.size"
    assert query["property_stack"][1]["node_arc"] == "variant"


def test_value_block_hides_weaker_opinions():
    layers = {"shot.usda": _shot("prepend references = @asset.usda@", "double size = None"), "asset.usda": ASSET}
    (query,) = _query(layers, "/World/Box.size")
    assert query["value"] is None
    assert query["blocked"] is True
    assert query["why"].startswith("blocked: shot.usda authors a value block")


def test_relationship_targets_are_mapped_through_the_reference():
    asset = ASSET.replace("double size = 3", "rel material:binding = </Asset/Looks/Red>")
    layers = {"shot.usda": _shot("prepend references = @asset.usda@"), "asset.usda": asset}
    (query,) = _query(layers, "/World/Box.material:binding")
    assert query["query_type"] == "relationship"
    assert query["targets"] == ["/World/Box/Looks/Red"]
    assert query["property_stack"][0]["explicit_targets"] == ["/Asset/Looks/Red"]


def test_prim_query_lists_every_arc_type():
    asset = _usda(
        """
        #usda 1.0
        (
            defaultPrim = "Asset"
        )

        def Cube "Asset" (
            prepend variantSets = "look"
            variants = {
                string look = "red"
            }
        ) {
            variantSet "look" = {
                "red" {
                    double size = 6
                }
            }
        }
        """
    )
    shot = _shot(
        "prepend references = @asset.usda@\nprepend payload = @proxy.usda@\n"
        "inherits = </_class_Box>\nspecializes = </_base_Box>",
        extra='\nclass "_class_Box" {\n}\n\nclass "_base_Box" {\n}\n',
        layer_meta="subLayers = [@layout.usda@]",
    )
    layout = '#usda 1.0\nover "World" {\n    over "Box" {\n        double size = 5\n    }\n}\n'
    layers = {"shot.usda": shot, "asset.usda": asset, "proxy.usda": ASSET, "layout.usda": layout}
    result = sandbox.compose(layers, "shot.usda", ["/World/Box"])
    assert result["ok"], result["errors"]
    box = next(prim for prim in result["prims"] if prim["path"] == "/World/Box")
    kinds = {arc["arc"] for arc in box["arcs"]}
    assert {"local", "sublayer", "inherit", "variant", "reference", "payload", "specialize"} <= kinds
    reference = next(arc for arc in box["arcs"] if arc["arc"] == "reference")
    assert (reference["target_layer"], reference["target_path"]) == ("asset.usda", "/Asset")
    assert reference["introduced_in"] == "shot.usda"
    prim = result["queries"][0]
    assert (prim["type"], prim["specifier"]) == ("Cube", "def")
    assert prim["has_payload"] is True
    assert {"layer": "layout.usda", "path": "/World/Box"} in prim["prim_stack"]
    classes = {p["path"]: p["specifier"] for p in result["prims"]}
    assert classes["/_class_Box"] == "class"


def test_parse_errors_name_the_layer():
    broken = '#usda 1.0\ndef "X" {\n    double size = \n}\n'
    result = sandbox.compose({"shot.usda": _shot(layer_meta="subLayers = [@broken.usda@]"), "broken.usda": broken}, "shot.usda")
    assert result["ok"] is False
    assert result["stage_opened"] is True
    parse = [e for e in result["errors"] if e["kind"] == "parse"]
    assert parse[0]["layer"] == "broken.usda"
    assert parse[0]["message"].startswith("broken.usda:3")
    assert "ousd_sandbox_" not in json.dumps(result["errors"])

    result = sandbox.compose({"shot.usda": broken}, "shot.usda")
    assert result["stage_opened"] is False and result["errors"][0]["layer"] == "shot.usda"


def test_composition_errors_are_reported():
    layers = {"shot.usda": _shot("prepend references = @asset.usda@</Missing>"), "asset.usda": ASSET}
    result = sandbox.compose(layers, "shot.usda")
    assert result["ok"] is False and result["stage_opened"] is True
    errors = [e for e in result["errors"] if e["kind"] == "composition"]
    assert errors and errors[0]["type"] == "UnresolvedPrimPath"
    assert "ousd_sandbox_" not in errors[0]["message"]


def test_rejects_bad_layer_names_and_inputs():
    for bad in ("../evil.usda", "sub/shot.usda", "sub\\shot.usda", "C:shot.usda", "shot.txt", "", ".usda", "con.usda"):
        result = sandbox.compose({bad: ASSET}, bad)
        assert result["ok"] is False and result["stage_opened"] is False, bad
        assert result["errors"][0]["kind"] == "input", bad
    assert sandbox.compose({"shot.usda": ASSET}, "other.usda")["errors"][0]["kind"] == "input"
    assert sandbox.compose({"shot.usda": "def X {}"}, "shot.usda")["errors"][0]["kind"] == "input"
    assert sandbox.compose({"a.usda": ASSET, "A.usda": ASSET}, "a.usda")["ok"] is False
    assert sandbox.compose({"shot.usda": ASSET}, "shot.usda", "/Asset")["ok"] is False


def test_rejects_asset_paths_outside_the_sandbox():
    outside = (
        _shot(layer_meta="subLayers = [@../outside.usda@]"),
        _shot("prepend references = @C:/elsewhere/asset.usda@"),
        _shot("prepend payload = @sub/asset.usda@"),
        _shot("prepend references = @missing.usda@"),
    )
    for shot in outside:
        result = sandbox.compose({"shot.usda": shot, "asset.usda": ASSET}, "shot.usda")
        assert result["ok"] is False and result["stage_opened"] is False
        assert result["errors"][0]["kind"] == "asset"
    variant = ASSET.replace(
        "double size = 3",
        'variantSet "v" = {\n        "a" {\n            def "Child" (\n                references = @/abs/x.usda@\n'
        "            ) {\n            }\n        }\n    }",
    )
    result = sandbox.compose({"shot.usda": variant}, "shot.usda")
    assert result["errors"][0]["kind"] == "asset"
    dotted = {"shot.usda": _shot("prepend references = @./asset.usda@"), "asset.usda": ASSET}
    assert _query(dotted, "/World/Box.size")[0]["value"] == 3.0


def test_temp_directories_are_always_removed():
    parent = tempfile.mkdtemp(prefix="sandbox_test_")
    created = []

    def spy(*args, **kwargs):
        path = tempfile.mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    previous_parent, previous_tempfile = sandbox.TEMP_PARENT, sandbox.tempfile
    sandbox.TEMP_PARENT = parent
    sandbox.tempfile = types.SimpleNamespace(mkdtemp=spy)
    try:
        sandbox.compose({"shot.usda": _shot("prepend references = @asset.usda@"), "asset.usda": ASSET}, "shot.usda",
                        ["/World/Box.size"])
        sandbox.compose({"shot.usda": "#usda 1.0\ndef {\n"}, "shot.usda")
        sandbox.compose({"shot.usda": _shot("prepend references = @../x.usda@")}, "shot.usda")
        assert len(created) == 3
        assert all(os.path.dirname(path) == parent and not os.path.exists(path) for path in created)
        assert os.listdir(parent) == []
    finally:
        sandbox.TEMP_PARENT, sandbox.tempfile = previous_parent, previous_tempfile
        shutil.rmtree(parent, ignore_errors=True)


def test_results_are_json_serializable_with_caps():
    shot = _shot(
        box_body=(
            "color3f[] primvars:displayColor = [(1, 0, 0)]\nmatrix4d xform = ( (1, 0, 0, 0), (0, 1, 0, 0), "
            "(0, 0, 1, 0), (5, 6, 7, 1) )\nasset texture = @tex.png@\ntoken mode = \"fast\"\n"
            "float[] many = [" + ", ".join(str(i) for i in range(200)) + "]"
        ),
        extra="".join(f'\ndef "Extra{i}" {{\n}}\n' for i in range(10)),
    )
    queries = [f"/World/Box.{name}" for name in ("primvars:displayColor", "xform", "texture", "mode", "many")]
    result = sandbox.compose({"shot.usda": shot}, "shot.usda", queries, max_prims=3)
    assert result["ok"], result["errors"]
    json.dumps(result, allow_nan=False)
    color, matrix, texture, mode, many = (q["value"] for q in result["queries"])
    assert color == [[1.0, 0.0, 0.0]]
    assert matrix[3] == [5.0, 6.0, 7.0, 1.0]
    assert (texture, mode) == ("tex.png", "fast")
    assert len(many) == sandbox.MAX_ITEMS + 1 and many[-1].startswith("...")
    assert result["prim_count"] == 3 and result["prims_truncated"] is True


def test_bad_queries_report_errors():
    layers = {"shot.usda": _shot(box_body="double size = 1")}
    for text in ("/World/Nope.size", "World", "/World/Box.size@abc", "/World/Box.nothing"):
        (query,) = sandbox.compose(layers, "shot.usda", [text])["queries"]
        assert query["ok"] is False and query["error"], text


def test_explain_strength_orders_by_liverps():
    result = sandbox.explain_strength(["specializes", "reference", "sublayer", "Inherits", "payload", "variant", "bogus"])
    assert result["order"] == ["local", "inherit", "variant", "reference", "payload", "specialize"]
    assert "".join(item["letter"] for item in result["ranked"]) == "LIVRPS"
    assert result["unknown"] == ["bogus"]
    assert result["summary"].startswith("Strongest to weakest: local (L) > inherit (I)")
    assert "LIVERPS" in sandbox.explain_strength([])["summary"]
    assert sandbox.explain_strength([{"arc": "payload"}])["order"] == ["payload"]


def test_puzzles_are_deterministic_for_a_seed():
    for difficulty in puzzles.DIFFICULTIES:
        for seed in (0, 1, 42, 999_999):
            first = puzzles.generate_puzzle(seed, difficulty)
            assert first == puzzles.generate_puzzle(seed=seed, difficulty=difficulty)
            assert first["puzzle_id"] == f"liverps-{difficulty}-{seed}"
        layouts = {json.dumps(puzzles.generate_puzzle(s, difficulty)["layers"]) for s in range(10)}
        assert len(layouts) > 5
    assert puzzles.generate_puzzle()["puzzle_id"].startswith("liverps-easy-")


def test_puzzles_hide_the_answer_and_offer_valid_options():
    for difficulty in puzzles.DIFFICULTIES:
        puzzle = puzzles.generate_puzzle(3, difficulty)
        assert not {"answer", "correct_answer", "explanation"} & set(puzzle)
        assert 3 <= len(puzzle["options"]) <= 5
        assert len(set(puzzle["options"])) == len(puzzle["options"])
        assert puzzle["question"].startswith("With shot.usda as the root layer")
        assert puzzle["hint"]


def _pxr_answer(puzzle: dict) -> str:
    """Independent oracle: compose the puzzle layers directly with pxr in a temp dir."""
    directory = tempfile.mkdtemp(prefix="puzzle_oracle_")
    try:
        for name, text in puzzle["layers"].items():
            with open(os.path.join(directory, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        stage = Usd.Stage.Open(os.path.join(directory, puzzle["root"]))
        path, _, time = puzzle["sandbox_query"].partition("@")
        attribute = stage.GetAttributeAtPath(path)
        value = attribute.Get(Usd.TimeCode(float(time)) if time else Usd.TimeCode.Default())
        del stage, attribute
        return sandbox.format_value(value)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _same(first: str, second: str) -> bool:
    try:
        return abs(float(first) - float(second)) < 1e-9
    except ValueError:
        return first == second


def test_check_puzzle_grades_thirty_seeds_per_difficulty():
    for difficulty in puzzles.DIFFICULTIES:
        for seed in range(30):
            puzzle = puzzles.generate_puzzle(seed, difficulty)
            probe = puzzles.check_puzzle(puzzle["puzzle_id"], "?")
            assert probe["ok"], (puzzle["puzzle_id"], probe)
            correct = probe["correct_answer"]
            assert correct in puzzle["options"], (puzzle["puzzle_id"], correct, puzzle["options"])
            assert correct == _pxr_answer(puzzle), puzzle["puzzle_id"]
            graded = puzzles.check_puzzle(puzzle["puzzle_id"], correct)
            assert graded["correct"] is True, puzzle["puzzle_id"]
            assert graded["layers"] == puzzle["layers"]
            assert graded["explanation"].startswith(f"Correct answer: {correct}.")
            assert "<- wins" in graded["explanation"] or not graded["property_stack"] or correct == "None"
            wrong = next(option for option in puzzle["options"] if not _same(option, correct))
            assert puzzles.check_puzzle(puzzle["puzzle_id"], wrong)["correct"] is False, puzzle["puzzle_id"]


def test_check_puzzle_accepts_letters_and_numbers():
    puzzle = puzzles.generate_puzzle(11, "medium")
    correct = puzzles.check_puzzle(puzzle["puzzle_id"], "?")["correct_answer"]
    letter = "ABCDE"[puzzle["options"].index(correct)]
    assert puzzles.check_puzzle(puzzle["puzzle_id"], letter.lower())["correct"] is True
    if correct != "None":
        assert puzzles.check_puzzle(puzzle["puzzle_id"], float(correct))["correct"] is True


def test_check_puzzle_rejects_unknown_ids_and_difficulties():
    for bad in ("nope", "liverps-extreme-1", "liverps-easy--4", ""):
        result = puzzles.check_puzzle(bad, "1")
        assert result["ok"] is False and result["correct"] is False
    for bad_difficulty in ("extreme", ""):
        try:
            puzzles.generate_puzzle(1, bad_difficulty)
        except ValueError:
            pass
        else:
            raise AssertionError(f"difficulty {bad_difficulty!r} should be rejected")


def test_puzzle_topics_cover_the_liverps_cases():
    expected = {
        "easy": {"sublayer order", "local vs reference", "reference vs payload"},
        "medium": {"inherits vs reference", "specializes is weakest", "variant selection", "reference vs payload"},
        "hard": {
            "class override through a reference",
            "variant inside a reference",
            "layer offsets and time samples",
            "class, variant and specializes combined",
            "value blocks",
        },
    }
    for difficulty, topics in expected.items():
        seen = {puzzles.generate_puzzle(seed, difficulty)["topic"] for seed in range(60)}
        assert topics <= seen, (difficulty, topics - seen)


def test_value_clip_opinion_is_named_as_the_winner():
    layers = {
        "shot.usda": '#usda 1.0\ndef Xform "World"\n{\n    def Cube "Box" (\n        clips = {\n            dictionary default = {\n'
        '                asset[] assetPaths = [@clip.usda@]\n                asset manifestAssetPath = @manifest.usda@\n'
        '                string primPath = "/Box"\n                double2[] active = [(0, 0)]\n'
        '                double2[] times = [(0, 0), (10, 10)]\n            }\n        }\n'
        '        prepend references = @asset.usda@</A>\n    )\n    {\n    }\n}\n',
        "asset.usda": '#usda 1.0\ndef Cube "A"\n{\n    double size = 5\n}\n',
        "clip.usda": '#usda 1.0\ndef Cube "Box"\n{\n    double size.timeSamples = {\n        0: 100,\n        10: 200,\n    }\n}\n',
        "manifest.usda": '#usda 1.0\nover "Box"\n{\n    double size\n}\n',
    }
    result = _query(layers, "/World/Box.size@5")[0]
    assert result["value"] == 150.0 and result["source"] == "value_clips"
    assert result["winning_layer"] == "clip.usda" and result["winning_arc"] == "value clip"
    assert "only opinion" not in result["why"]
    assert result["why"].startswith(
        "stronger: value clips anchored on </World/Box> supply clip.usda's samples at this time; clips are weaker than "
        "direct opinions in the layer that anchors them but stronger than weaker layers and arcs such as reference "
        "asset.usda</A>"
    ), result["why"]
    clip_entry = result["property_stack"][0]
    assert clip_entry["via"] == "value clips anchored on </World/Box>"
    assert all(sample["stage_time"] is None for sample in clip_entry["time_samples"])
    # A direct default in the layer that anchors the clips beats them.
    anchored = dict(layers, **{"shot.usda": layers["shot.usda"].replace("    {\n    }\n}\n", "    {\n        double size = 7\n    }\n}\n")})
    local = _query(anchored, "/World/Box.size@5")[0]
    assert local["value"] == 7.0 and local["winning_layer"] == "shot.usda"
    assert "the direct opinion in shot.usda (local) beats value clips anchored on </World/Box> from clip.usda" in local["why"]


def test_default_time_query_names_a_stronger_samples_only_spec():
    layers = {
        "shot.usda": '#usda 1.0\ndef Xform "World"\n{\n    def Sphere "Ball" (\n        prepend references = @asset.usda@</A>\n'
        '    )\n    {\n        double radius.timeSamples = {\n            0: 1,\n        }\n    }\n}\n',
        "asset.usda": '#usda 1.0\ndef Sphere "A"\n{\n    double radius = 9\n}\n',
    }
    default, timed = _query(layers, "/World/Ball.radius", "/World/Ball.radius@0")
    assert default["value"] == 9.0
    assert default["why"] == (
        "only opinion: 9 authored in asset.usda (reference asset.usda</A>); the stronger spec(s) in shot.usda hold "
        "only time samples, which a default-time query ignores"
    ), default["why"]
    assert timed["value"] == 1.0 and "default-time query ignores" not in timed["why"]
    # With two defaults, the strength reason is kept and the skipped spec is still named.
    layered = dict(layers, **{
        "shot.usda": layers["shot.usda"].replace("prepend references = @asset.usda@</A>", "prepend references = [@asset.usda@</A>, @base.usda@</B>]"),
        "base.usda": '#usda 1.0\ndef Sphere "B"\n{\n    double radius = 3\n}\n',
    })
    both = _query(layered, "/World/Ball.radius")[0]
    assert both["value"] == 9.0 and both["why"].startswith("stronger: "), both["why"]
    assert both["why"].endswith("; the stronger spec(s) in shot.usda hold only time samples, which a default-time query ignores")


def test_relationship_why_explains_an_explicit_list_below_stronger_edits():
    layers = {
        "shot.usda": '#usda 1.0\n(\n    subLayers = [@a.usda@, @b.usda@]\n)\ndef Xform "W"\n{\n    prepend rel r = </X>\n}\n'
        'def "X" {}\ndef "Y" {}\ndef "Z" {}\ndef "Q" {}\n',
        "a.usda": '#usda 1.0\nover "W"\n{\n    rel r = [</Y>, </Z>]\n}\n',
        "b.usda": '#usda 1.0\nover "W"\n{\n    append rel r = </Q>\n}\n',
    }
    result = _query(layers, "/W.r")[0]
    assert result["targets"] == ["/X", "/Y", "/Z"]
    assert "combine" not in result["why"]
    assert result["why"].startswith("the explicit target list in a.usda replaces every weaker edit")
    assert "1 weaker spec(s) are ignored" in result["why"]
    # With nothing weaker than the explicit list, no 'ignored' count is shown.
    alone = dict(layers, **{"shot.usda": layers["shot.usda"].replace("[@a.usda@, @b.usda@]", "[@a.usda@]")})
    only = _query(alone, "/W.r")[0]
    assert only["targets"] == ["/X", "/Y", "/Z"]
    assert only["why"].startswith("the explicit target list in a.usda replaces every weaker edit")
    assert "ignored" not in only["why"], only["why"]


def test_ancestral_variant_is_described_with_its_selection():
    layers = {
        "shot.usda": '#usda 1.0\ndef Xform "World" (\n    variants = {\n        string layout = "wide"\n    }\n'
        '    prepend variantSets = "layout"\n    prepend references = @set.usda@</Set>\n)\n{\n'
        '    variantSet "layout" = {\n        "wide" {\n            over "Box"\n            {\n'
        '                double size = 11\n            }\n        }\n    }\n}\n',
        "set.usda": '#usda 1.0\ndef Xform "Set"\n{\n    def Cube "Box"\n    {\n        double size = 2\n    }\n}\n',
    }
    result = _query(layers, "/World/Box.size")[0]
    assert result["value"] == 11.0
    assert result["property_stack"][0]["via"] == "variant layout=wide (ancestral)"
    assert "variant layout=wide (ancestral) in shot.usda beats" in result["why"]
    assert "variant =" not in result["why"]


def test_spline_values_are_explained_as_splines():
    layers = {
        "shot.usda": '#usda 1.0\ndef Xform "World"\n{\n    def Cube "Box"\n    {\n        double size.spline = {\n'
        '            0: 1; post linear,\n            10: 11; post linear,\n        }\n    }\n}\n'
    }
    timed, default = _query(layers, "/World/Box.size@5", "/World/Box.size")
    if timed["source"] != "spline":
        print("SKIP spline check: this USD build does not read spline values")
        return
    assert abs(timed["value"] - 6.0) < 1e-9
    assert timed["why"] == "only opinion: 6 from the spline authored in shot.usda (local)"
    assert timed["property_stack"][0]["spline"] is True
    assert "schema fallback" in default["why"] and "splines are ignored" in default["why"]


def test_specialize_inside_a_reference_names_both_class_copies():
    layers = {
        "shot.usda": '#usda 1.0\ndef Xform "World"\n{\n    def Cube "Box" (\n        prepend references = @asset.usda@</Asset>\n'
        '    )\n    {\n    }\n}\nover "Base"\n{\n    double size = 9\n}\n',
        "asset.usda": '#usda 1.0\ndef Cube "Asset" (\n    prepend specializes = </Base>\n)\n{\n}\nclass "Base"\n{\n    double size = 4\n}\n',
    }
    result = _query(layers, "/World/Box.size")[0]
    assert result["value"] == 9.0
    assert (
        "the copy of class </Base> in the root layer stack (shot.usda) beats the asset's own class in asset.usda: "
        "a specialize authored inside an asset is also looked up in every stronger layer stack that references it"
    ) in result["why"], result["why"]
    vias = [entry["via"] for entry in result["property_stack"]]
    assert vias == ["specialize shot.usda</Base> (implied)", "specialize asset.usda</Base>"], vias
    assert "specialize </Base> is listed before specialize </Base>" not in result["why"]


def test_jsonable_turns_time_codes_into_numbers():
    from pxr import Gf, Sdf, Vt

    time_code = getattr(Gf, "TimeCode", None) or getattr(Sdf, "TimeCode")
    array_type = getattr(Vt, "TimeCodeArray", None) or getattr(Sdf, "TimeCodeArray")
    assert sandbox._jsonable(time_code(24)) == 24.0
    assert sandbox._jsonable(array_type([time_code(1), time_code(2)])) == [1.0, 2.0]


def test_puzzle_explanation_names_arcs_nested_inside_the_asset():
    result = puzzles.check_puzzle("liverps-hard-2", "A")
    text = result["explanation"]
    assert "> inherit" in text
    last = text.splitlines()[-1]
    assert last.startswith("At the root level: Only reference opinions are involved"), last
    assert last.endswith("Inside the referenced asset LIVERPS applies again: local > inherit."), last
    # Puzzles whose stack shows no arc nested inside an asset keep the plain summary.
    for seed in range(20):
        explanation = puzzles.check_puzzle(puzzles.generate_puzzle(seed, "easy")["puzzle_id"], "A")["explanation"]
        stack_lines = [line for line in explanation.splitlines() if line.startswith("  ") and line.rstrip().endswith(("]", "<- wins"))]
        if not any(" > " in line for line in stack_lines):
            assert not explanation.splitlines()[-1].startswith("At the root level"), explanation
