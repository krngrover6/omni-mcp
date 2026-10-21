import asyncio
import os
import tempfile

from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import _PRACTICE_TOPICS, OmniverseRouter, RouteTarget, _saved_up_axis
from omniverse_scene_mcp.service import SceneMCPService


def test_router_sends_scene_commands_to_scene_service():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("inspect /World/Props/Cube"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["result"]["path"] == "/World/Props/Cube"


def test_router_routes_usd_questions_to_knowledge_backend():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("What is a USD prim?"))
    assert result.decision.target is RouteTarget.USD
    assert result.payload["backend"] == "usd"
    assert result.payload["ok"] is False


def test_router_attaches_builtin_lesson_when_knowledge_backend_fails():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    payload = asyncio.run(router.handle("What is a USD prim?")).payload
    assert payload["ok"] is False
    assert payload["message"] == "Knowledge backend is not connected yet."
    assert payload["fallback"]["topic"] == "prims"
    assert payload["fallback"]["title"] == "What a Prim Is"
    assert router.learning_history()["history"] == []


def test_router_returns_knowledge_answer_without_fallback_when_backend_answers():
    class AnsweringBackend:
        async def ask(self, query):
            return {"ok": True, "backend": "usd", "query": query, "content": "A prim is a scene graph node."}

    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()), usd_backend=AnsweringBackend())
    payload = asyncio.run(router.handle("What is a USD prim?")).payload
    assert payload["ok"] is True
    assert "fallback" not in payload


def test_router_keeps_explicit_learning_requests_in_learning_mode():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for request in ("can you explain usd schemas?", "quiz me on usd schemas?", "teach me prims", "What is a prim?"):
        assert router.route(request).target is RouteTarget.LEARN, request


def test_router_matches_usd_and_kit_as_whole_words():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    assert router.route("How do materials work in USD?").target is RouteTarget.USD
    assert router.route("How do I add a light in Kit?").target is RouteTarget.KIT
    assert router.route("open scene.usd").target is RouteTarget.SCENE
    assert router.route("what is in the kitchen scene?").target is not RouteTarget.KIT


def test_router_routes_extensions_and_module_names_to_their_domain():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    expected = {
        "what is a .usd file?": RouteTarget.USD,
        "what does omni.usd do?": RouteTarget.USD,
        "how do i open scene.usd in composer?": RouteTarget.USD,
        "what is a .kit file?": RouteTarget.KIT,
        "what does omni.kit do?": RouteTarget.KIT,
        "what file extension does a usd stage use?": RouteTarget.USD,
    }
    for request, target in expected.items():
        assert router.route(request).target is target, request


def test_router_keeps_scene_aware_and_weak_keyword_questions_on_their_old_routes():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    # "how many prims are in my usd stage?" now gets the live prim count (see the prim count test below).
    for request in (
        "what is selected in usd composer?",
        "what is the schema of /World/Props/Cube?",
        "how do i add a light in usd composer?",
        "what's a good layout for my stage hierarchy?",
        "how do i start a new project with a dome light?",
    ):
        assert router.route(request).target is RouteTarget.LEARN, request
    assert router.route("find cube in usd?").target is RouteTarget.SCENE


def test_router_fallback_lesson_matches_whole_topic_words():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    assert "fallback" not in asyncio.run(router.handle("Are USD references lightweight?")).payload
    assert "fallback" not in asyncio.run(router.handle("How do I remove a payload in USD?")).payload
    assert asyncio.run(router.handle("What is a USD schema?")).payload["fallback"]["topic"] == "schemas"


def test_router_runs_polite_scene_requests():
    bridge = FakeSceneBridge()
    router = OmniverseRouter(SceneMCPService(bridge))
    assert router.route("can you move the cube up in kit?").target is RouteTarget.SCENE
    moved = asyncio.run(router.handle("could you please move /World/Props/Cube 1 2 3?")).payload
    assert moved["ok"] is True
    assert moved["payload"]["prim"]["translation"] == (1.0, 2.0, 3.0)
    for request in ("can you explain usd schemas?", "could you find docs about usd references?"):
        assert router.route(request).target is not RouteTarget.SCENE, request


def test_router_never_creates_a_prim_for_requests_about_an_existing_one():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    before = router.scene_summary()
    for request in ("could you make the light brighter in usd?", "make the cube red", "add the cube to the shelf"):
        payload = asyncio.run(router.handle(request)).payload
        assert payload.get("changes", [{}])[0].get("action") != "create", request
    assert router.scene_summary() == before
    created = asyncio.run(router.handle("add a cube")).payload
    assert created["payload"]["prim"]["type_name"] == "Cube"


def test_router_lists_candidates_instead_of_guessing_a_vague_target():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    before = router.scene_summary()
    payload = asyncio.run(router.handle("can you move the cube up in kit?")).payload
    assert payload["ok"] is False
    assert "/World/Props/Cube" in payload["candidates"]
    assert "Nothing was changed" in payload["hint"]
    assert router.scene_summary() == before


def test_router_understands_plain_english_scene_summary():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("show me the scene"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["result"]["root_path"] == "/World"


def test_router_uses_selection_for_natural_language_actions():
    bridge = FakeSceneBridge()
    bridge._selection = ["/World/Props/Cube"]
    router = OmniverseRouter(SceneMCPService(bridge))
    result = asyncio.run(router.handle("move this 1 2 3"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["payload"]["prim"]["path"] == "/World/Props/Cube"


def test_router_can_create_a_prim_from_plain_english():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("add a cube"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["payload"]["prim"]["type_name"] == "Cube"
    assert result.payload["payload"]["prim"]["path"].startswith("/World/")


def test_router_can_select_a_prim_from_plain_english():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("select /World/Props/Cube"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["result"]["selection"] == ["/World/Props/Cube"]


def test_router_can_rename_a_selected_prim():
    bridge = FakeSceneBridge()
    bridge.set_selection(["/World/Props/Cube"])
    router = OmniverseRouter(SceneMCPService(bridge))
    result = asyncio.run(router.handle("rename this to HeroCube"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["payload"]["prim"]["path"] == "/World/Props/HeroCube"


def test_router_can_find_prims_by_text():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("find cube"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["result"]["count"] >= 1


def test_router_keeps_learning_phrases_with_scene_words_in_learning_mode():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    for request in ("move on to the next lesson", "explain how to select a prim", "translate this to a command"):
        assert router.route(request).target is RouteTarget.LEARN, request


def test_router_routes_save_with_usd_path_to_scene():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    decision = router.route("save C:/Temp/scene_copy.usda")
    assert decision.target is RouteTarget.SCENE


def test_router_routes_learning_questions_to_learning_mode():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("teach me schemas"))
    assert result.decision.target is RouteTarget.LEARN
    assert result.payload["ok"] is True
    assert result.payload["topic"] == "schemas"
    assert result.payload["style"] == "lesson"


def test_router_can_quiz_me_on_schemas():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("quiz me on schemas"))
    assert result.decision.target is RouteTarget.LEARN
    assert result.payload["style"] == "quiz"
    assert len(result.payload["quiz"]["questions"]) >= 2


def test_router_can_offer_a_practical_drill():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    result = asyncio.run(router.handle("practice USD transforms with me"))
    assert result.decision.target is RouteTarget.LEARN
    assert result.payload["style"] == "practice"
    assert result.payload["commands"]
    assert result.payload["next_prompt"]
    assert result.payload["current_step_index"] == 1


def test_router_can_start_and_advance_practice_sessions():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    first = router.start_practice("selection")
    assert first["style"] == "practice"
    assert first["current_step_index"] == 1
    assert first["current_command"]

    second = router.next_practice_step("done")
    assert second["style"] == "practice"
    assert second["current_step_index"] == 2


def test_router_advances_practice_session_when_user_says_done():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    router.start_practice("selection")
    result = asyncio.run(router.handle("done"))
    assert result.decision.target is RouteTarget.LEARN
    assert result.payload["style"] == "practice"
    assert result.payload["current_step_index"] == 2


def test_router_scene_quiz_uses_current_selection():
    bridge = FakeSceneBridge()
    bridge.set_selection(["/World/Props/Cube"])
    router = OmniverseRouter(SceneMCPService(bridge))
    result = router.scene_quiz("schemas")
    assert result["style"] == "quiz"
    assert result["selected_prim"]["path"] == "/World/Props/Cube"
    assert len(result["questions"]) >= 3


def test_router_study_mode_suggests_next_action():
    bridge = FakeSceneBridge()
    bridge.set_selection(["/World/Props/Cube"])
    router = OmniverseRouter(SceneMCPService(bridge))
    result = router.study_mode()
    assert result["style"] == "study"
    assert result["recommendation"]
    assert result["try_next"]


def test_router_can_translate_natural_language_to_a_scene_action():
    bridge = FakeSceneBridge()
    bridge.set_selection(["/World/Props/Cube"])
    router = OmniverseRouter(SceneMCPService(bridge))
    result = router.translate_command("make this bigger")
    assert result["style"] == "translate"
    assert result["suggestion"]["kind"] == "scene_action"
    assert result["suggestion"]["action"] == "move"


def test_router_handles_make_this_bigger_as_a_scene_command():
    bridge = FakeSceneBridge()
    bridge.set_selection(["/World/Props/Cube"])
    router = OmniverseRouter(SceneMCPService(bridge))
    result = asyncio.run(router.handle("make this bigger"))
    assert result.decision.target is RouteTarget.SCENE
    assert result.payload["ok"] is True
    assert result.payload["payload"]["prim"]["scale"] == (10.0, 10.0, 10.0)


def test_router_exposes_learning_history_and_cheatsheet():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    router.start_practice("schemas")
    history = router.learning_history()
    assert history["ok"] is True
    assert history["cheatsheet"]["commands"]["scene"]
    assert history["progress"]["completed_sessions"] >= 0


def test_router_learning_roadmap_is_available():
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()))
    roadmap = router.learning_roadmap()
    assert roadmap["ok"] is True
    assert roadmap["roadmap"]


def _router(selection=None):
    bridge = FakeSceneBridge()
    if selection:
        bridge.set_selection(selection)
    return OmniverseRouter(SceneMCPService(bridge)), bridge


def _run(router, request, dry_run=False):
    return asyncio.run(router.handle(request, dry_run=dry_run)).payload


def _write_layer(directory, name, metadata=""):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#usda 1.0\n" + (f"(\n    {metadata}\n)\n" if metadata else ""))
    return path


def _has_pxr():
    try:
        import pxr  # noqa: F401
    except ImportError:
        return False
    return True


def test_router_strips_short_and_formal_polite_prefixes():
    for prefix in ("pls", "plz", "would you kindly", "kindly", "please kindly", "can you kindly"):
        router, _ = _router()
        request = f"{prefix} move /World/Props/Cube 1 2 3"
        assert router.route(request).target is RouteTarget.SCENE, request
        assert _run(router, request)["payload"]["prim"]["translation"] == (1.0, 2.0, 3.0), request
    assert OmniverseRouter._strip_politeness("would you kindly move the cube up pls") == "move the cube up"
    assert OmniverseRouter._strip_politeness("plz inspect /World") == "inspect /world"


def test_router_previews_an_edit_whose_target_is_a_unique_name():
    router, bridge = _router()
    before = bridge.snapshot()
    payload = _run(router, "move the cube by 0 0 1")
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["resolved_from_name"] == {"name": "cube", "path": "/World/Props/Cube"}
    assert payload["command"] == "move /World/Props/Cube 0 0 1"
    assert "nothing was changed" in payload["message"]
    assert bridge.snapshot() == before
    applied = _run(router, payload["command"])
    assert applied["dry_run"] is False
    assert applied["payload"]["prim"]["translation"] == (0.0, 0.0, 1.0)


def test_router_never_applies_any_edit_resolved_from_a_name():
    router, bridge = _router()
    before = bridge.snapshot()
    expected = {
        "rename the cube to HeroCube": ("rename /World/Props/Cube to HeroCube", "rename"),
        "delete the cube": ("delete /World/Props/Cube", "delete"),
        "move the cube under /World": ("reparent /World/Props/Cube to /World", "reparent"),
        "duplicate the cube to /World/CubeCopy": ("duplicate /World/Props/Cube to /World/CubeCopy", "duplicate"),
        "make the cube twice as big": ("scale /World/Props/Cube 2 2 2", "move"),
        "rotate the cube 0 90 0": ("rotate /World/Props/Cube 0 90 0", "move"),
        "move the Cube to 4 5 6": ("move /World/Props/Cube 4 5 6", "move"),
        "set the material of the cube to /World/Looks/Red": ("bind /World/Looks/Red to /World/Props/Cube", "material"),
    }
    for request, (command, action) in expected.items():
        assert router.route(request).target is RouteTarget.SCENE, request
        payload = _run(router, request, dry_run=False)
        assert payload["dry_run"] is True, request
        assert payload["command"] == command, request
        assert payload["resolved_from_name"]["path"] == "/World/Props/Cube", request
        assert router._parse_scene_command(command)["action"] == action, command
    assert bridge.snapshot() == before


def test_router_runs_read_only_requests_resolved_from_a_name():
    router, bridge = _router()
    bridge.create_prim("/World/Props/Hero_Cube", "Cube")
    payload = _run(router, "inspect the CUBE")
    assert payload["result"]["path"] == "/World/Props/Cube"
    assert payload["resolved_from_name"] == {"name": "cube", "path": "/World/Props/Cube"}
    hero = _run(router, "inspect the hero cube")
    assert hero["result"]["path"] == "/World/Props/Hero_Cube"


def test_router_lists_candidates_when_a_name_is_ambiguous_or_partial():
    router, bridge = _router()
    bridge.create_prim("/World/Cube", "Cube")
    before = bridge.snapshot()
    ambiguous = _run(router, "move the cube by 0 0 1")
    assert ambiguous["ok"] is False
    assert {"/World/Cube", "/World/Props/Cube"} <= set(ambiguous["candidates"])
    assert "Nothing was changed" in ambiguous["hint"]
    partial = _run(router, "move the cub by 0 0 1")
    assert partial["ok"] is False
    assert "/World/Props/Cube" in partial["candidates"]
    assert bridge.snapshot() == before


def test_router_applies_relative_moves_to_explicit_paths_and_the_selection():
    router, _ = _router(["/World/Props/Cube"])
    _run(router, "move /World/Props/Cube by 1 2 3")
    payload = _run(router, "move it by 1, 2, 3")
    assert payload["dry_run"] is False
    assert payload["payload"]["prim"]["translation"] == (2.0, 4.0, 6.0)
    assert payload["relative"] == {"from_translation": [1.0, 2.0, 3.0], "offset": [1.0, 2.0, 3.0]}
    assert "resolved_from_name" not in payload
    assert _run(router, "move this 0 0 0")["payload"]["prim"]["translation"] == (0.0, 0.0, 0.0)


def test_router_moves_up_along_the_saved_stage_up_axis_without_saving():
    if not _has_pxr():
        return
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
        z_up = _write_layer(directory, "z_up.usda", 'upAxis = "Z"')
        y_up = _write_layer(directory, "y_up.usda", 'upAxis = "Y"')
        with open(z_up, encoding="utf-8") as handle:
            z_text = handle.read()
        z_mtime = os.path.getmtime(z_up)
        forms = (
            (z_up, (0.0, 0.0, 2.0)),
            ("file:/" + z_up.replace("\\", "/"), (0.0, 0.0, 2.0)),
            ("file:///" + y_up.replace("\\", "/"), (0.0, 2.0, 0.0)),
            (y_up, (0.0, 2.0, 0.0)),
        )
        for stage_name, expected in forms:
            router, bridge = _router()
            bridge._stage_name = stage_name
            payload = _run(router, "move /World/Props/Cube up by 2")
            assert payload["payload"]["prim"]["translation"] == expected, stage_name
            assert payload["relative"]["up_axis"]["source"] == "authored"
        router, bridge = _router()
        bridge._stage_name = z_up
        assert _run(router, "move /World/Props/Cube down 0.5")["payload"]["prim"]["translation"] == (0.0, 0.0, -0.5)
        preview = _run(router, "move the cube upwards by 1")
        assert preview["dry_run"] is True
        assert preview["command"] == "move /World/Props/Cube 0 0 0.5"
        with open(z_up, encoding="utf-8") as handle:
            assert handle.read() == z_text
        assert os.path.getmtime(z_up) == z_mtime

        from pxr import UsdGeom

        plain = _write_layer(directory, "plain.usda")
        assert _saved_up_axis(plain) == {"axis": str(UsdGeom.GetFallbackUpAxis()), "source": "fallback", "layer": plain}


def test_router_refuses_up_moves_when_the_up_axis_is_unknown():
    router, bridge = _router()
    before = bridge.snapshot()
    payload = _run(router, "move /World/Props/Cube up by 2")
    assert payload["ok"] is False
    assert "upAxis" in payload["message"]
    assert "move /World/Props/Cube by 0 0 2" in payload["hint"]
    assert "move /World/Props/Cube by 0 2 0" in payload["hint"]
    assert bridge.snapshot() == before
    with tempfile.TemporaryDirectory() as directory:
        missing = os.path.join(directory, "missing.usda")
        for stage_name in ("FakeStage", "anon:0000020F:root.usda", "omniverse://server/scene.usd", "", missing):
            assert _saved_up_axis(stage_name) is None, stage_name


def test_router_refuses_camera_relative_directions():
    router, bridge = _router(["/World/Props/Cube"])
    before = bridge.snapshot()
    for request in ("move /World/Props/Cube left by 2", "move the cube forward 1", "move it back", "nudge this right"):
        assert router.route(request).target is RouteTarget.SCENE, request
        payload = _run(router, request)
        assert payload["ok"] is False, request
        assert "upAxis" in payload["message"], request
        assert " by 1 0 0" in payload["hint"], request
    assert bridge.snapshot() == before


def test_router_scales_and_rotates_with_absolute_and_relative_phrasings():
    router, _ = _router(["/World/Props/Cube"])

    def prim(request):
        assert router.route(request).target is RouteTarget.SCENE, request
        return _run(router, request)["payload"]["prim"]

    assert prim("scale /World/Props/Cube 2 3 4")["scale"] == (2.0, 3.0, 4.0)
    assert prim("make /World/Props/Cube twice as big")["scale"] == (4.0, 6.0, 8.0)
    assert prim("make it half as big")["scale"] == (2.0, 3.0, 4.0)
    assert prim("scale it by 3")["scale"] == (6.0, 9.0, 12.0)
    assert prim("make this 3 times as big")["scale"] == (18.0, 27.0, 36.0)
    assert prim("scale it to 1")["scale"] == (1.0, 1.0, 1.0)
    assert prim("rotate /World/Props/Cube 0 90 0")["rotation"] == (0.0, 90.0, 0.0)
    assert prim("rotate it 0 90 0 degrees")["rotation"] == (0.0, 90.0, 0.0)
    for request in ("rotate it 90 degrees", "rotate it by 0 0 90"):
        refused = _run(router, request)
        assert refused["ok"] is False, request
        assert "rotate /World/Props/Cube 0 0 90" in refused["hint"], request


def test_router_does_not_treat_attribute_edits_as_material_bindings():
    router, bridge = _router()
    before = bridge.snapshot()
    for request in (
        "set intensity of /World/Props/Cube to 500",
        "please set the intensity of the cube to 500",
        "set /World/Props/Cube's intensity to 5",
    ):
        assert router.route(request).target is RouteTarget.SCENE, request
        payload = _run(router, request)
        assert payload["ok"] is False, request
        assert payload["action"] == "set_attribute", request
        assert payload["attribute"] == "intensity", request
        assert payload["prim"]["path"] == "/World/Props/Cube", request
        assert "later tier" in payload["message"], request
        assert "changes" not in payload, request
    assert bridge.snapshot() == before
    assert router._parse_scene_command("set intensity of /World/Props/Cube to 500")["action"] == "set_attribute"


def test_router_answers_prim_count_questions_from_the_scene():
    router, _ = _router()
    for request in (
        "how many prims are in this stage?",
        "How many prims are in this scene",
        "how many prims are in my usd stage?",
        "how many prims are there?",
    ):
        result = asyncio.run(router.handle(request))
        assert result.decision.target is RouteTarget.SCENE, request
        assert result.payload["result"]["prim_count"] == 3, request
        assert result.payload["message"] == "The open stage has 3 prims.", request
    assert router.route("how many prims of type Mesh are under /World?").target is not RouteTarget.SCENE


def test_router_detects_learning_topics_as_whole_words():
    detect = OmniverseRouter._detect_learning_topic
    assert detect("what are primvars?") is None
    assert detect("is this lightweight?") is None
    assert detect("how do i remove a payload?") is None
    assert detect("teach me prims") == "prims"
    assert detect("explain lighting") == "lights"
    assert detect("how does moving work") == "transforms"
    assert detect("highlight the rules") == "selection"
    assert detect("show me the workflow") == "workflow"
    assert detect("give me the cheat sheet") == "workflow"


def test_router_routes_exam_terms_to_usd_knowledge():
    router, _ = _router()
    for request in (
        "what is the difference between inherits and specializes?",
        "how do sublayers work?",
        "what is a variant set?",
        "when should I use payloads?",
        "what does LIVRPS stand for?",
        "what is LIVERPS?",
        "what are primvars?",
        "what is model kind?",
        "how does the model hierarchy work?",
        "what does instanceable mean?",
        "how does instancing save memory?",
        "what is metersPerUnit?",
        "what is upAxis?",
        "how do I flatten a stage?",
        "what is an edit target?",
        "what does a layer offset do?",
        "how is opinion strength decided?",
        "how does value resolution work?",
    ):
        assert router.route(request).target is RouteTarget.USD, request
    for request in ("explain payloads", "teach me variant sets", "quiz me on primvars", "practice instancing with me"):
        assert router.route(request).target is RouteTarget.LEARN, request
    assert router.route("what kind of light should I add?").target is not RouteTarget.USD
    assert router.route("variant sets and primvars").target is RouteTarget.USD


def _assert_runnable(router, commands):
    for command in commands:
        assert "/Environment/Cube" not in command, command
        assert router.route(command).target is RouteTarget.SCENE, command
        parsed = router._parse_scene_command(command)
        assert parsed is not None, command
        assert parsed["action"] not in {"unsupported", "set_attribute"}, command
        if parsed["action"] == "select":
            router._scene.set_selection(parsed["paths"])


def test_every_suggested_command_parses_and_routes_to_the_scene():
    for selection in ([], ["/World/Props/Cube"]):
        router, bridge = _router(selection)
        groups = [router.cheat_sheet()["commands"]["scene"]]
        groups += [router._make_practice_session(topic)["commands"] for topic in _PRACTICE_TOPICS]
        groups += [router._build_learning_guide(topic)["commands"] for topic in _PRACTICE_TOPICS]
        groups += [router.study_mode(topic)["try_next"] for topic in ("materials", "prims", "transforms", "workflow")]
        for commands in groups:
            bridge.set_selection(selection)
            _assert_runnable(router, commands)
    router, bridge = _router(["/World/Props/Cube"])
    for request in ("make this bigger", "paint it red", "move it up", "rename it", "nest it under environment", "hmm"):
        suggestion = router._suggest_command_from_request(request)
        _assert_runnable(router, [suggestion["command"]] if "command" in suggestion else suggestion["examples"])
    assert router._suggest_command_from_request("make a group")["kind"] == "explain"


def test_router_practice_drills_use_scene_paths_and_include_lights():
    router, bridge = _router()
    lights = router.start_practice("lights")
    assert lights["topic"] == "lights"
    assert lights["title"] == "Interactive lighting drill"
    assert lights["commands"][1] == "create /World/PracticeLight type DistantLight"
    assert lights["commands"] != lights["steps"]
    assert len(lights["steps"]) == len(lights["commands"])
    assert router.start_practice("transforms")["commands"][0] == "inspect /World/Props/Cube"
    selected, _ = _router(["/World/Props"])
    assert selected.start_practice("transforms")["commands"][0] == "inspect /World/Props"
    bridge.delete_prim("/World/Props/Cube")
    bridge.create_prim("/World/Box", "Cube")
    assert router.start_practice("materials")["commands"][0] == "inspect /World/Box"
    unknown = router.start_practice("variants")
    assert unknown["topic"] == "schemas"
    assert "variants" in unknown["note"]


def test_router_quiz_without_its_own_questions_lists_quiz_topics():
    router, _ = _router()
    for request in ("quiz me on lights", "quiz me on payloads"):
        payload = _run(router, request)
        assert payload["ok"] is False, request
        assert payload["quiz"]["questions"] == [], request
        assert payload["quiz"]["quiz_topics"] == ["prims", "schemas"], request
        assert "prims, schemas" in payload["message"], request
    assert router.scene_quiz("lights")["ok"] is False
    assert _run(router, "quiz me on prims")["quiz"]["questions"][0] == "What does a prim path represent?"
    selected, _ = _router(["/World/Props/Cube"])
    scene_quiz = selected.scene_quiz("lights")
    assert scene_quiz["ok"] is True
    assert "prims, schemas" in scene_quiz["note"]


def test_router_has_no_dead_practice_builder():
    assert not hasattr(OmniverseRouter, "_build_practice_session")


class _BrokenBridge(FakeSceneBridge):
    def snapshot(self):
        raise RuntimeError("scene bridge is down")


def test_router_learning_history_stores_only_stage_name_and_prim_count():
    router, _ = _router()
    router.start_practice("schemas")
    assert router.learning_history()["history"][-1]["scene"] == {"stage_name": "FakeStage", "prim_count": 3}

    broken = OmniverseRouter(SceneMCPService(_BrokenBridge()))
    broken._record_learning_event("request", topic="schemas")
    assert broken.learning_history()["history"][-1]["scene"] == {"stage_name": None, "prim_count": None}
    lesson = _run(broken, "teach me schemas")
    assert lesson["ok"] is True
    assert lesson["scene_context"]["available"] is False
    drill = broken.start_practice("materials")
    assert drill["commands"][0] == "inspect /World/Props/Cube"


def test_router_pronouns_inside_relative_clauses_never_edit_the_selection():
    router, bridge = _router()
    bridge.create_prim("/World/Lights", "Xform")
    bridge.create_prim("/World/Lights/Key", "DistantLight")
    bridge.create_prim("/World/Sphere", "Sphere")
    bridge.set_selection(["/World/Lights/Key"])
    before = bridge.snapshot()
    for request in (
        "make the cube that I just added bigger",
        "move the sphere next to it by 0 0 1",
        "rename the cube that is red to HeroCube",
        "duplicate the cube that is red as CubeCopy",
        "move the cube that is red under /World",
        "rotate the cube that is red 0 90 0",
        "scale the sphere next to it 2 2 2",
        "paint the cube that is red with /World/Looks/Red",
    ):
        payload = _run(router, request, dry_run=False)
        assert payload.get("dry_run") is not False, (request, payload)
        assert bridge.snapshot() == before, request
        assert bridge.get_selection() == ["/World/Lights/Key"], request
    # A whole phrase that means the selection still targets it.
    moved = _run(router, "move this by 0 0 1")
    assert moved["dry_run"] is False and moved["payload"]["prim"]["path"] == "/World/Lights/Key"
    bigger = _run(router, "make the selected light bigger")
    assert bigger["dry_run"] is False and bigger["payload"]["prim"]["path"] == "/World/Lights/Key"


def test_router_never_creates_for_double_spaces_or_other_determiners():
    router, bridge = _router(["/World/Props/Cube"])
    before = bridge.snapshot()
    for request in (
        "create  the cube",
        "please create  the cube",
        "please add this cube to the shelf",
        "could you add my light to the scene?",
        "please add it to the shelf",
        "can you place that cube under /World/Props?",
    ):
        payload = _run(router, request)
        assert all(change.get("action") != "create" for change in payload.get("changes", [])), request
        assert bridge.snapshot() == before, request
    created = _run(router, "create  a  cube")
    assert created["payload"]["prim"]["type_name"] == "Cube"


def test_router_save_needs_a_usd_path_and_never_saves_to_the_sentence():
    router, bridge = _router()
    saves = []
    original_save = bridge.save_scene
    bridge.save_scene = lambda path: saves.append(path) or original_save(path)
    for request in ("save the scene", "save scene", "save it", "save the box", "save"):
        payload = _run(router, request)
        assert payload["ok"] is False and "Nothing was saved" in payload["message"], request
    assert saves == []
    assert router._parse_scene_command("save this scene as backup.usda") == {"action": "save", "path": "backup.usda"}
    assert router._parse_scene_command("save the stage to C:/Temp/x.usda") == {"action": "save", "path": "C:/Temp/x.usda"}
    assert router._parse_scene_command("save /tmp/x.usda") == {"action": "save", "path": "/tmp/x.usda"}
    assert router._parse_scene_command("save as copy.usdc") == {"action": "save", "path": "copy.usdc"}
    _run(router, "save this scene as backup.usda")
    assert saves == ["backup.usda"]


def test_router_dry_run_select_does_not_change_the_selection():
    router, bridge = _router()
    for request in ("select /World/Props/Cube", "highlight /World/Props"):
        payload = _run(router, request, dry_run=True)
        assert payload["dry_run"] is True and payload["would_select"], request
        assert bridge.get_selection() == [], request
    before = bridge.snapshot()
    _run(router, "delete this")
    assert bridge.snapshot() == before
    selected = _run(router, "select /World/Props/Cube")
    assert selected["result"]["selection"] == ["/World/Props/Cube"]


def test_router_dry_run_and_questions_never_reach_bowerbot_write_tools():
    from omniverse_scene_mcp.knowledge import build_bowerbot_backend

    real = build_bowerbot_backend("http://127.0.0.1:1/mcp")  # only its selector is used; nothing connects
    calls = []

    class StubBowerbot:
        name = "bowerbot"
        endpoint = None
        enabled = True
        selector = staticmethod(real.selector)

        async def ask(self, query):
            selection = real.selector(query)
            calls.append(selection.tool_name)
            return {"ok": True, "backend": "bowerbot", "tool": selection.tool_name, "tool_arguments": selection.arguments}

    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()), bowerbot_backend=StubBowerbot())
    expected = {"new project Kitchen": "create_project", "open project Kitchen": "open_project", "package scene": "package_scene"}
    for request, tool in expected.items():
        assert router.route(request).target is RouteTarget.BOWERBOT, request
        preview = _run(router, request, dry_run=True)
        assert preview["dry_run"] is True and preview["would_call"] == tool, request
    assert calls == []
    for question in ("how do I create a new project?", "what does open project do?", "how do i package scene files for delivery?"):
        payload = _run(router, question)
        assert payload["tool"] == "list_scene", question
    assert calls == ["list_scene"] * 3


# --- NCP-OUSD daily lessons and mock exams ---------------------------------------------------------------------


def test_router_routes_daily_lesson_and_mock_exam_phrases():
    router, _ = _router()
    days = {
        "start day 3": 3,
        "day 1": 1,
        "Day one please": 1,
        "can you start day 2 for me?": 2,
        "lesson for day 4": 4,
        "open study day 12": 12,
        "let's do day 5 of my plan": 5,
    }
    for request, day in days.items():
        decision = router.route(request)
        assert decision.target is RouteTarget.LEARN and decision.reason == "Daily exam lesson request", request
        assert router._exam_request(router._normalize(request)) == ("day", day), request
    for request in ("next lesson", "move on to the next lesson", "what's today's lesson?", "continue my study plan"):
        assert router.route(request).target is RouteTarget.LEARN, request
        assert router._exam_request(router._normalize(request)) == ("day", None), request
    for request in ("mock exam", "start a mock exam", "I want a timed practice exam", "mock-test please"):
        decision = router.route(request)
        assert decision.target is RouteTarget.LEARN and decision.reason.startswith("Mock exam request"), request
        assert router._exam_request(router._normalize(request)) == ("mock", None), request
    for request in (
        "day trip",
        "what day is it",
        "quiz me on schemas",
        "teach me prims",
        "next step",
        "practice transforms with me",
        "move /World/Props/Cube by 0 0 1",
        "what is a variant set?",
    ):
        assert router._exam_request(router._normalize(request)) is None, request
    # Existing routes keep their targets.
    assert router.route("move /World/Props/Cube by 0 0 1").target is RouteTarget.SCENE
    assert router.route("what is a variant set?").target is RouteTarget.USD
    assert _run(router, "quiz me on schemas")["style"] == "quiz"


def test_router_without_exam_hooks_names_the_tool_and_never_starts_a_mock():
    router, _ = _router()
    lesson = _run(router, "start day 3")
    assert lesson["ok"] is True and lesson["executed"] is False and lesson["style"] == "exam_day"
    assert lesson["tool"] == "learning_exam_day" and lesson["arguments"] == {"day": 3}
    assert "learning_exam_day(day=3)" in lesson["message"]
    assert _run(router, "next lesson")["arguments"] == {}
    mock = _run(router, "start a mock exam")
    assert mock["ok"] is True and mock["started"] is False and mock["style"] == "mock_exam"
    assert mock["tool"] == "learning_mock_exam_start" and mock["arguments"] == {"count": 65, "minutes": 120}
    assert "never starts one" in mock["message"] and "open_mock" not in mock
    assert any("learning_mock_exam_finish" in step for step in mock["steps"])
    events = [entry["event"] for entry in router.learning_history()["history"]]
    assert events[-3:] == ["exam_day", "exam_day", "mock_exam"]
    assert "start day 1" in router.cheat_sheet()["commands"]["learning"]


def test_router_runs_exam_hooks_and_reports_an_open_mock():
    calls = []

    def exam_day(day):
        calls.append(day)
        return {"ok": True, "day": day or 1, "type": "study"}

    open_mock = {"session_id": "mock-1", "state": "in_progress", "remaining": "10m 0s", "answered": 2, "total": 5}
    router = OmniverseRouter(SceneMCPService(FakeSceneBridge()), exam_day=exam_day, mock_status=lambda: open_mock)
    lesson = _run(router, "start day 3")
    assert lesson["executed"] is True and lesson["day"] == 3 and lesson["type"] == "study"
    assert lesson["style"] == "exam_day" and lesson["target"] == "learn"
    assert _run(router, "today's lesson")["day"] == 1
    assert calls == [3, None]

    mock = _run(router, "mock exam")
    assert mock["started"] is False and mock["open_mock"]["session_id"] == "mock-1"
    assert mock["tool"] == "learning_mock_exam_status" and "already open" in mock["message"]

    def broken(_day):
        raise RuntimeError("progress file is locked")

    failing = OmniverseRouter(SceneMCPService(FakeSceneBridge()), exam_day=broken, mock_status=lambda: None)
    payload = _run(failing, "day 2")
    assert payload["ok"] is False and "RuntimeError" in payload["message"] and "locked" in payload["message"]
    assert "open_mock" not in _run(failing, "mock exam")

    # An active practice session still advances on "next"; only lesson phrases reach the daily plan.
    router.start_practice("selection")
    assert _run(router, "next")["current_step_index"] == 2
    assert calls == [3, None]


def test_mock_exam_phrases_do_not_capture_prim_names_or_usd_questions():
    router, _ = _router()
    for request in (
        "inspect /World/MockTest",
        "delete /World/MockTests",
        "select /World/Props/MockExam",
        "move /World/MockTest by 0 0 1",
        "rename /World/Cube to MockTest",
        "find mocktest",
        "what is a full test suite in usd?",
    ):
        assert router._exam_request(router._normalize(request)) is None, request
        assert not router.route(request).reason.startswith("Mock exam request"), request
    assert router.route("inspect /World/MockTest").target is RouteTarget.SCENE
    assert router.route("find mocktest").target is RouteTarget.SCENE
    assert router.route("what is a full test suite in usd?").target is RouteTarget.USD
    for request in ("start a mock exam", "mock-exam", "timed test", "full exam", "exam simulation",
                    "give me a full practice exam", "run a full mock test"):
        assert router._exam_request(router._normalize(request)) == ("mock", None), request
