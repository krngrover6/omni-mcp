"""Tier 1 MCP tools: offline docs, exam prep, composition sandbox and saved-file insight, all offline."""

import os

# The stdlib test runner has no conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import asyncio
import contextlib
import json
import re
import socket
import tempfile
import urllib.request
from pathlib import Path

from omniverse_scene_mcp import runtime
from omniverse_scene_mcp.exam import bank as exam_bank
from omniverse_scene_mcp.exam import progress as exam_progress
from omniverse_scene_mcp.knowledge import StreamableMCPKnowledgeBackend
from omniverse_scene_mcp.offline_knowledge import OFFLINE_NOTE, FallbackKnowledgeBackend, OfflineUSDKnowledge
from omniverse_scene_mcp.router import RouteTarget
from omniverse_scene_mcp.server import SAVED_FILE_NOTE, build_server
from omniverse_scene_mcp.service import SceneMCPService

SERVER_SOURCE = Path(__file__).resolve().parents[1] / "src" / "omniverse_scene_mcp" / "server.py"
REAL_PROGRESS = Path(__file__).resolve().parents[2] / "exam-prep" / "progress.json"

NEW_TOOLS = {
    "usd_docs_search",
    "learning_glossary",
    "learning_exam_blueprint",
    "learning_exam_plan",
    "learning_exam_quiz",
    "learning_exam_answer",
    "learning_exam_progress",
    "learning_exam_review",
    "learning_exam_reset",
    "usd_playground_compose",
    "learning_composition_puzzle",
    "learning_composition_puzzle_answer",
    "scene_stage_info",
    "scene_composition",
    "scene_explain_value",
    "scene_properties",
    "scene_bounds",
    "usd_validate",
}
FILE_MODE_TOOLS = {"scene_stage_info", "scene_composition", "scene_explain_value", "scene_properties", "scene_bounds", "usd_validate"}

GLOSSARY_MD = """# OpenUSD Glossary

:::{glossary}
:sorted:

LIVERPS Strength Ordering

    LIVERPS is the strength ordering of composition arcs, from local opinions down to specializes.

    **Also Known As:** *strength ordering, LIVRPS*

Payload

    A payload is a composition arc like a reference that can be loaded or unloaded on demand.

:::
"""

PAYLOADS_MD = """# Payloads

Payloads are deferred references: a stage can open with payloads unloaded and load them on demand later.
"""

ASSET_USDA = """#usda 1.0
(
    defaultPrim = "Asset"
    metersPerUnit = 0.01
    upAxis = "Y"
)

def Xform "Asset" (
    kind = "component"
)
{
    def Cube "Geom"
    {
        double size = 1
        float3[] extent = [(-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)]
    }
}
"""

OVERRIDES_USDA = """#usda 1.0

over "World"
{
    over "Hero"
    {
        over "Geom"
        {
            double size = 2
        }
    }
}
"""

SHOT_USDA = """#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 0.01
    upAxis = "Y"
    subLayers = [
        @./overrides.usda@
    ]
)

def Xform "World" (
    kind = "assembly"
)
{
    def "Hero" (
        prepend references = @./asset.usda@
    )
    {
        over "Geom"
        {
            double size = 3
        }
    }
}
"""


# --------------------------------------------------------------------------- helpers


def _file_state(path: Path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_mtime_ns, info.st_size


def _folder_listing(folder: Path):
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else None


@contextlib.contextmanager
def _guard():
    """Refuse every socket connection and check that the real exam-prep/progress.json is untouched."""
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(repr(args[-1:] or kwargs))
        raise AssertionError(f"network access attempted: {args!r}")

    async def refuse_async(*args, **kwargs):
        refuse(*args, **kwargs)

    patched = [
        (socket, "create_connection", refuse),
        (socket.socket, "connect", refuse),
        (socket.socket, "connect_ex", refuse),
        (urllib.request, "urlopen", refuse),
        (runtime, "urlopen", refuse),
        (asyncio, "open_connection", refuse_async),
    ]
    originals = [(owner, name, getattr(owner, name)) for owner, name, _ in patched]
    progress_before = _file_state(REAL_PROGRESS)
    listing_before = _folder_listing(REAL_PROGRESS.parent)
    # The Windows event loop builds its self-pipe with socket.connect, so create it before patching.
    loop = asyncio.new_event_loop()
    for owner, name, replacement in patched:
        setattr(owner, name, replacement)
    try:
        yield loop
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)
        loop.close()
    assert attempts == [], attempts
    assert _file_state(REAL_PROGRESS) == progress_before, "exam-prep/progress.json changed"
    assert _folder_listing(REAL_PROGRESS.parent) == listing_before, "exam-prep/ gained or lost files"


@contextlib.contextmanager
def _env(**values):
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _make_workspace(root: Path) -> dict[str, Path]:
    """Synthetic workspace: a tiny glossary and doc page, an empty offline data dir and a three-layer scene."""
    docs = root / "LearnOpenUSD-main" / "docs"
    (docs / "composition").mkdir(parents=True)
    (docs / "glossary.md").write_text(GLOSSARY_MD, encoding="utf-8")
    (docs / "composition" / "payloads.md").write_text(PAYLOADS_MD, encoding="utf-8")
    (root / "data").mkdir()
    scenes = root / "scenes"
    scenes.mkdir()
    files = {
        "shot": scenes / "shot.usda",
        "asset": scenes / "asset.usda",
        "overrides": scenes / "overrides.usda",
    }
    files["shot"].write_text(SHOT_USDA, encoding="utf-8")
    files["asset"].write_text(ASSET_USDA, encoding="utf-8")
    files["overrides"].write_text(OVERRIDES_USDA, encoding="utf-8")
    (root / "notes.txt").write_text("not usd", encoding="utf-8")
    return files


@contextlib.contextmanager
def _server(exam_prep_dir: Path | None = None):
    """A test-mode server over a temp workspace; yields (call, files, root)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        files = _make_workspace(root)
        with _env(
            OMNIVERSE_SCENE_MCP_TEST="1",
            OMNIVERSE_ROOT=root,
            OMNIVERSE_USD_KNOWLEDGE_DATA=root / "data",
            OMNIVERSE_EXAM_PREP_DIR=exam_prep_dir,
        ):
            with _guard() as loop:
                app = build_server()

                def call(name, **arguments):
                    result = loop.run_until_complete(app._tool_manager.call_tool(name, arguments))
                    json.dumps(result)  # every tool result must be JSON-ready
                    return result

                call.app = app
                call.loop = loop
                yield call, files, root


def _bank_items() -> dict[str, dict]:
    return {item["id"]: item for item in exam_bank.load_bank()}


# --------------------------------------------------------------------------- registration


def test_new_tools_are_registered_exactly_once():
    with _server() as (call, _files, _root):
        app = call.app
        names = [tool.name for tool in app._tool_manager.list_tools()]
        assert NEW_TOOLS | {"router_health", "scene_save"} <= set(names)
        assert len(names) == len(set(names))
        declared = re.findall(r'@app\.tool\(name="([a-z_]+)"\)', SERVER_SOURCE.read_text(encoding="utf-8"))
        duplicates = sorted({name for name in declared if declared.count(name) > 1})
        assert duplicates == [], duplicates
        assert set(declared) == set(names)
        assert declared.count("router_health") == 1 and declared.count("scene_save") == 1
        save = app._tool_manager.get_tool("scene_save")
        assert save.parameters["properties"]["dry_run"]["default"] is False
        for name in NEW_TOOLS:
            description = app._tool_manager.get_tool(name).description
            assert description.strip() and "\n " not in description, name
        listed = {tool.name: tool for tool in call.loop.run_until_complete(app.list_tools())}
        assert NEW_TOOLS <= set(listed) and "\n " not in listed["usd_docs_search"].description
        for name in FILE_MODE_TOOLS:
            description = app._tool_manager.get_tool(name).description
            assert "SAVED file" in description and "live" in description, name


# --------------------------------------------------------------------------- offline docs


def test_usd_docs_search_and_glossary_work_offline():
    with _server() as (call, _files, _root):
        search = call("usd_docs_search", query="payload load on demand", top_k=3)
        assert search["ok"] is True and search["note"] == OFFLINE_NOTE
        assert 1 <= search["count"] <= 3
        assert any("payload" in hit["snippet"].lower() for hit in search["results"])
        assert all({"title", "source", "url_or_path", "snippet"} <= set(hit) for hit in search["results"])

        empty = call("usd_docs_search", query="zzqx")
        assert empty["ok"] is False and empty["results"] == [] and empty["message"]

        listing = call("learning_glossary")
        assert listing["ok"] is True and listing["count"] == 2
        assert "Payload" in listing["terms"]

        livrps = call("learning_glossary", term="LIVRPS")
        assert livrps["ok"] is True and livrps["found"] is True
        assert livrps["term"] == "LIVERPS Strength Ordering"
        assert livrps["url_or_path"].endswith("glossary.md:6")

        miss = call("learning_glossary", term="zebra crossing")
        assert miss["ok"] is False and miss["found"] is False and "suggestions" in miss


def test_router_usd_backend_falls_back_to_shared_offline_docs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _make_workspace(root)
        with _env(
            OMNIVERSE_ROOT=root,
            OMNIVERSE_USD_KNOWLEDGE_DATA=root / "data",
            OMNIVERSE_USD_MCP_ENDPOINT="http://127.0.0.1:9903/mcp",  # local, so the (faked) probe decides
        ):
            with _guard() as loop:
                shared = OfflineUSDKnowledge()
                router = runtime.build_router({}, detect_endpoint=lambda: None, offline_knowledge=shared)
                backend = router._backends[RouteTarget.USD]
                assert isinstance(backend, FallbackKnowledgeBackend)
                assert backend.offline.knowledge is shared
                assert backend.selector is backend.primary.selector
                assert isinstance(router._backends[RouteTarget.KIT], StreamableMCPKnowledgeBackend)
                status = {row["target"]: row for row in router.backend_status()["backends"]}
                assert status["usd"]["endpoint"] == backend.primary.endpoint

                probes = []

                async def refused(host, port, timeout):
                    probes.append((host, port))
                    return "connection refused (simulated)"

                backend._probe = refused  # never touch a real port
                answer = loop.run_until_complete(router.ask_backend(RouteTarget.USD, "when should I use payloads?"))
                routed = loop.run_until_complete(router.handle("when should I use payloads?"))
        assert probes and all(host in ("127.0.0.1", "::1") for host, _port in probes)
        assert answer["ok"] is True and answer["note"] == OFFLINE_NOTE
        assert "simulated" in answer["primary_error"]
        assert routed.decision.target is RouteTarget.USD
        assert routed.payload["ok"] is True and routed.payload["endpoint"] == "offline"


def test_runtime_builds_one_progress_store_with_safe_defaults():
    expected = exam_progress.workspace_root() / "exam-prep" / "progress.json"
    assert expected == REAL_PROGRESS
    before = _file_state(REAL_PROGRESS)
    assert runtime.build_progress_store({}).path == expected  # path only; nothing is read or written here
    test_store = runtime.build_progress_store({"OMNIVERSE_SCENE_MCP_TEST": "1"})
    assert test_store.memory_only is True and test_store.path is None
    with tempfile.TemporaryDirectory() as tmp:
        chosen = runtime.build_progress_store({"OMNIVERSE_SCENE_MCP_TEST": "1", "OMNIVERSE_EXAM_PREP_DIR": tmp})
        assert chosen.memory_only is False and chosen.path == Path(tmp) / "progress.json"
    assert _file_state(REAL_PROGRESS) == before


# --------------------------------------------------------------------------- exam prep


def test_exam_tools_round_trip_in_memory():
    items = _bank_items()
    with _server() as (call, _files, _root):
        overview = call("learning_exam_blueprint", include_objectives=False)
        assert overview["ok"] is True and overview["domain_count"] == 8
        assert overview["question_bank"]["total"] == len(items)
        assert all("objective_ids" in dom and "objectives" not in dom for dom in overview["domains"])
        assert call("learning_exam_blueprint")["domains"][0]["objectives"]

        quiz = call("learning_exam_quiz", domain="Composition", count=3)
        assert quiz["ok"] is True and quiz["count"] == 3 and quiz["how_to_answer"]
        assert all(q["domain"] == "Composition" for q in quiz["questions"])
        assert all("answer" not in q and "explanations" not in q for q in quiz["questions"])

        first, second = quiz["questions"][0]["id"], quiz["questions"][1]["id"]
        right = items[first]["answer"]
        good = call("learning_exam_answer", question_id=first, chosen=right, seconds=30)
        assert good["ok"] is True and good["correct"] is True
        assert good["progress"]["ok"] is True and good["progress"]["persisted"] is False
        assert good["progress"]["domain"]["name"] == "Composition"

        wrong_choice = next(i for i in range(len(items[second]["options"])) if [i] != sorted(items[second]["answer"]))
        letter = "ABCDEF"[wrong_choice]
        bad = call("learning_exam_answer", question_id=second, chosen=[letter])
        assert bad["ok"] is True and bad["correct"] is False and bad["progress"]["box"] == 1

        review = call("learning_exam_review", limit=5)
        assert review["ok"] is True and [row["question_id"] for row in review["reviews"]] == [second]
        assert review["reviews"][0]["kind"] == "question"
        assert "answer" not in review["reviews"][0]["question"]

        again = call("learning_exam_quiz", count=2, review_due=True)
        assert again["questions"][0]["id"] == second
        assert again["review_due"] == {"due_cards": 1, "included": 1}

        progress = call("learning_exam_progress")
        assert progress["ok"] is True and progress["totals"]["answered"] == 2
        assert progress["totals"]["correct"] == 1 and progress["due_count"] == 1
        assert set(exam_bank.DOMAINS) <= set(progress["domains"])
        assert progress["storage"]["memory_only"] is True and progress["storage"]["path"] is None
        assert [row["question_id"] for row in progress["recent"]] == [second, first]

        plan = call("learning_exam_plan", days_until_exam=7, minutes_per_day=45)
        assert plan["ok"] is True and plan["progress_used"] is True
        assert plan["progress"]["answered"] == 2 and len(plan["schedule"]) == 7
        assert sum(row["minutes_per_day"] for row in plan["domains"]) == 45

        assert call("learning_exam_quiz", domain="no such domain")["ok"] is False
        assert call("learning_exam_quiz", objective="9.9")["ok"] is False
        assert call("learning_exam_answer", question_id="nope-001", chosen=[0])["ok"] is False
        assert call("learning_exam_answer", question_id=first, chosen=[99])["ok"] is False
        unsaved = call("learning_exam_answer", question_id=first, chosen=right, seconds=-5)
        assert unsaved["ok"] is True and unsaved["progress"]["ok"] is False

        refused = call("learning_exam_reset")
        assert refused["ok"] is False and refused["reset"] is False
        assert call("learning_exam_progress")["totals"]["answered"] == 2
        done = call("learning_exam_reset", confirm=True)
        assert done["ok"] is True and done["backup"] is None and done["path"] is None
        assert call("learning_exam_progress")["totals"]["answered"] == 0
        fresh_plan = call("learning_exam_plan")
        assert fresh_plan["progress_used"] is False and "schedule" not in fresh_plan


def test_exam_progress_persists_only_in_the_chosen_exam_prep_dir():
    with tempfile.TemporaryDirectory() as prep:
        folder = Path(prep)
        question = next(iter(_bank_items().values()))
        with _server(exam_prep_dir=folder) as (call, _files, _root):
            answered = call("learning_exam_answer", question_id=question["id"], chosen=question["answer"])
            assert answered["progress"]["persisted"] is True
            assert answered["progress"]["path"] == str(folder / "progress.json")
            assert (folder / "progress.json").is_file()
        with _server(exam_prep_dir=folder) as (call, _files, _root):
            assert call("learning_exam_progress")["totals"]["answered"] == 1
            reset = call("learning_exam_reset", confirm=True)
            assert reset["ok"] is True and Path(reset["backup"]).is_file()
            assert call("learning_exam_progress")["totals"]["answered"] == 0


# --------------------------------------------------------------------------- sandbox and puzzles


def test_composition_puzzle_tools_record_composition_progress():
    with _server() as (call, _files, _root):
        puzzle = call("learning_composition_puzzle", difficulty="medium", seed=42)
        assert puzzle["ok"] is True and puzzle["puzzle_id"] == "liverps-medium-42"
        assert "correct_answer" not in puzzle and puzzle["layers"] and puzzle["how_to_answer"]
        assert "1.6" in puzzle["objective_ids"]
        assert call("learning_composition_puzzle", difficulty="medium", seed=42)["question"] == puzzle["question"]

        first = call("learning_composition_puzzle_answer", puzzle_id=puzzle["puzzle_id"], answer="A")
        assert first["ok"] is True and first["correct_answer"] and first["explanation"]
        assert first["progress"]["ok"] is True
        assert first["progress"]["domain"]["name"] == "Composition"
        assert {row["objective_id"] for row in first["progress"]["objectives"]} == set(puzzle["objective_ids"])

        wrong = next(option for option in puzzle["options"] if option != first["correct_answer"])
        second = call("learning_composition_puzzle_answer", puzzle_id=puzzle["puzzle_id"], answer=wrong)
        assert second["correct"] is False and second["progress"]["box"] == 1

        review = call("learning_exam_review")
        assert review["reviews"][0]["question_id"] == puzzle["puzzle_id"]
        assert review["reviews"][0]["kind"] == "puzzle"
        assert review["reviews"][0]["puzzle"]["question"] == puzzle["question"]

        progress = call("learning_exam_progress")
        assert progress["domains"]["Composition"]["answered"] == 2
        assert progress["recent"][0]["source"] == "puzzle"

        bad_id = call("learning_composition_puzzle_answer", puzzle_id="not-a-puzzle", answer="A")
        assert bad_id["ok"] is False and bad_id["message"]
        assert call("learning_composition_puzzle", difficulty="impossible")["ok"] is False
        assert call("learning_exam_progress")["totals"]["answered"] == 2


def test_usd_playground_compose_explains_strength():
    layers = {
        "shot.usda": '#usda 1.0\n(\n    subLayers = [@./strong.usda@, @./weak.usda@]\n)\n\ndef Xform "World"\n{\n}\n',
        "strong.usda": '#usda 1.0\n\nover "World"\n{\n    double size = 5\n}\n',
        "weak.usda": '#usda 1.0\n\nover "World"\n{\n    double size = 1\n}\n',
    }
    with _server() as (call, _files, _root):
        result = call("usd_playground_compose", layers=layers, root="shot.usda", queries=["/World.size", "/World"])
        assert result["ok"] is True and result["layer_stack"][0] == "shot.usda"
        value = result["queries"][0]
        assert value["ok"] is True and value["value"] == 5 and value["winning_layer"] == "strong.usda"
        assert result["queries"][1]["query_type"] == "prim"

        rejected = call("usd_playground_compose", layers={"../evil.usda": "#usda 1.0\n"}, root="../evil.usda")
        assert rejected["ok"] is False and rejected["message"]


# --------------------------------------------------------------------------- saved-file insight and validation


def test_file_mode_tools_read_the_saved_file_read_only():
    with _server() as (call, files, root):
        before = {name: _file_state(path) for name, path in files.items()}
        shot = str(files["shot"])

        info = call("scene_stage_info", file=shot)
        assert info["ok"] is True and info["mode"] == "saved_file" and info["note"] == SAVED_FILE_NOTE
        assert "fallback_note" not in info  # an explicit file means the saved file, with no live attempt
        assert Path(info["file"]) == files["shot"].resolve()
        assert info["default_prim"] == "/World" and info["up_axis"] == "Y"

        relative = call("scene_stage_info", file="scenes/shot.usda")
        assert relative["ok"] is True and relative["file"] == info["file"]

        composed = call("scene_composition", prim_path="/World/Hero", file=shot)
        assert composed["ok"] is True and composed["mode"] == "saved_file"
        assert any(arc["arc_type"] == "reference" for arc in composed["arcs"])

        explained = call("scene_explain_value", prim_path="/World/Hero/Geom", attribute="size", file=shot)
        assert explained["ok"] is True and explained["value"] == 3 and explained["why"]
        timed = call("scene_explain_value", prim_path="/World/Hero/Geom", attribute="size", time=1, file=shot)
        assert timed["ok"] is True and timed["value"] == 3
        assert call("scene_explain_value", prim_path="/World/Hero/Geom", attribute="size", time="default", file=shot)["ok"]

        props = call("scene_properties", prim_path="/World/Hero/Geom", file=shot)
        assert props["ok"] is True and props["mode"] == "saved_file"
        assert "size" in json.dumps(props)

        box = call("scene_bounds", prim_path="/World/Hero/Geom", file=shot)
        assert box["ok"] is True and box["mode"] == "saved_file" and box["world_bound"]["empty"] is False

        lint = call("usd_validate", file=shot)
        assert lint["mode"] == "saved_file" and lint["note"] == SAVED_FILE_NOTE and lint["profile"] == "exam"
        assert "stale_extent" in {issue["code"] for issue in lint["issues"]}
        assert call("usd_validate", file=shot, profile="bogus")["ok"] is False

        missing_prim = call("scene_composition", prim_path="/World/Nope", file=shot)
        assert missing_prim["ok"] is False and "does not exist" in missing_prim["message"]
        assert missing_prim["mode"] == "saved_file"
        missing_attr = call("scene_explain_value", prim_path="/World/Hero/Geom", attribute="nope", file=shot)
        assert missing_attr["ok"] is False
        assert call("scene_bounds", prim_path="not a path", file=shot)["ok"] is False
        assert call("scene_stage_info", file=str(root / "scenes" / "gone.usda"))["ok"] is False
        assert call("scene_stage_info", file=str(root / "notes.txt"))["ok"] is False
        with tempfile.TemporaryDirectory() as elsewhere:
            outside = Path(elsewhere) / "outside.usda"
            outside.write_text(ASSET_USDA, encoding="utf-8")
            refused = call("scene_stage_info", file=str(outside))
            assert refused["ok"] is False and "outside the workspace" in refused["message"]

        after = {name: _file_state(path) for name, path in files.items()}
        assert after == before


def test_file_mode_defaults_to_the_live_scenes_saved_root_layer():
    with _server() as (call, files, _root):
        fake = call("scene_stage_info")
        assert fake["ok"] is False and fake["mode"] == "saved_file"
        assert "not saved" in fake["message"] and "fake scene" in fake["message"]
        # mode='auto' (the default) tried the live bridge first; the fake scene has no live insight.
        assert fake["mode_requested"] == "auto" and "fake scene" in fake["fallback_note"]
        assert fake["live_fallback"]["needs_bridge_update"] is False

        with tempfile.TemporaryDirectory() as elsewhere:
            live = Path(elsewhere) / "live.usda"
            live.write_text(SHOT_USDA.replace("@./overrides.usda@", ""), encoding="utf-8")
            original = SceneMCPService.scene_summary

            def saved_scene(self):
                return {**original(self), "stage_name": live.resolve().as_uri()}

            SceneMCPService.scene_summary = saved_scene
            try:
                info = call("scene_stage_info")
                lint = call("usd_validate", profile="exam")
            finally:
                SceneMCPService.scene_summary = original
            assert info["ok"] is True and Path(info["file"]) == live.resolve()
            assert info["default_prim"] == "/World"
            assert lint["mode"] == "saved_file" and Path(lint["file"]) == live.resolve()

        def broken(self):
            raise ConnectionError("bridge offline")

        SceneMCPService.scene_summary = broken
        try:
            down = call("scene_properties", prim_path="/World")
        finally:
            SceneMCPService.scene_summary = original
        assert down["ok"] is False and "bridge offline" in down["message"]
