import asyncio

from omniverse_scene_mcp.fake_bridge import FakeSceneBridge
from omniverse_scene_mcp.router import OmniverseRouter, RouteTarget
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
    for request in (
        "what is selected in usd composer?",
        "how many prims are in my usd stage?",
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
