"""Tier 2 integration: daily lessons, timed mock exams, live-or-saved insight modes and dry-run light/camera tools.

Every tool runs on a test-mode server: fake scene (or an in-process HTTP transport into the extended live RPC with a
temporary pxr stage), memory-only stores or temp folders. Sockets are refused and the real exam-prep folder is
checked (listing and mtimes) after every server.
"""

import os

# The stdlib test runner has no conftest: switch the runtime offline before anything builds a router.
os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"

import asyncio
import contextlib
import json
import socket
import tempfile
import types
import urllib.request
from pathlib import Path

from omniverse_scene_mcp import http_bridge, kit_bridge, kit_rpc, runtime
from omniverse_scene_mcp import server as server_module
from omniverse_scene_mcp.exam import bank as exam_bank
from omniverse_scene_mcp.exam.mock import MockExamStore
from omniverse_scene_mcp.http_bridge import HttpSceneBridge
from omniverse_scene_mcp.service import SceneMCPService

try:
    from pxr import Usd, UsdGeom
except ImportError:  # pragma: no cover - the live-stage tests below then return early
    Usd = UsdGeom = None

REAL_EXAM_PREP = Path(__file__).resolve().parents[2] / "exam-prep"
INSIGHT_TOOLS = ("scene_stage_info", "scene_composition", "scene_explain_value", "scene_properties", "scene_bounds", "usd_validate")
AUTHORING_TOOLS = ("scene_create_light", "scene_create_camera", "scene_aim")
STAR_TOOLS = {
    "learning_exam_day",
    "learning_exam_day_complete",
    "learning_exam_set_date",
    "learning_mock_exam_start",
    "learning_mock_exam_questions",
    "learning_mock_exam_answer",
    "learning_mock_exam_flag",
    "learning_mock_exam_status",
    "learning_mock_exam_finish",
    "learning_mock_exam_abandon",
    "scene_capabilities",
    *AUTHORING_TOOLS,
}

DOC_MD = """# Composition arcs

LIVERPS orders opinions: local, inherits, variant sets, relocates, references, payloads, specializes.
A sublayer stack is strongest first, and a reference brings in the defaultPrim of another layer.
"""

SHOT_USDA = """#usda 1.0
(
    defaultPrim = "World"
    metersPerUnit = 1
    upAxis = "Z"
)

def Xform "World"
{
    def Cube "Box"
    {
        double size = 3
    }
}
"""


# --------------------------------------------------------------------------- helpers


def _exam_prep_state():
    """Listing and mtimes of the real exam-prep folder (None when it does not exist)."""
    if not REAL_EXAM_PREP.is_dir():
        return None
    return {entry.name: (entry.stat().st_mtime_ns, entry.stat().st_size) for entry in REAL_EXAM_PREP.iterdir()}


@contextlib.contextmanager
def _guard():
    """Refuse every socket connection and check that the real exam-prep folder is untouched."""
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
    before = _exam_prep_state()
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
    assert _exam_prep_state() == before, "the real exam-prep folder changed"


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


@contextlib.contextmanager
def _server(exam_prep_dir=None, scene=None):
    """A test-mode server over a temp workspace; yields (call, root). scene(root) may replace the fake scene service."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        docs = root / "LearnOpenUSD-main" / "docs"
        docs.mkdir(parents=True)
        (docs / "composition.md").write_text(DOC_MD, encoding="utf-8")
        (root / "data").mkdir()
        (root / "shot.usda").write_text(SHOT_USDA, encoding="utf-8")
        with _env(
            OMNIVERSE_SCENE_MCP_TEST="1",
            OMNIVERSE_ROOT=root,
            OMNIVERSE_USD_KNOWLEDGE_DATA=root / "data",
            OMNIVERSE_EXAM_PREP_DIR=exam_prep_dir,
        ):
            with _guard() as loop:
                original = server_module.build_router

                def build_router(*args, **kwargs):
                    router = original(*args, **kwargs)
                    if scene is not None:
                        router._scene = scene(root)
                    return router

                server_module.build_router = build_router
                try:
                    app = server_module.build_server()
                finally:
                    server_module.build_router = original

                def call(name, **arguments):
                    result = loop.run_until_complete(app._tool_manager.call_tool(name, arguments))
                    json.dumps(result)  # every tool result must be JSON-ready
                    return result

                call.app = app
                yield call, root


class _FakeResponse:
    def __init__(self, body) -> None:
        self._data = json.dumps(body).encode("utf-8")

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@contextlib.contextmanager
def _in_process_transport(server):
    """HttpSceneBridge RPCs go straight into server._dispatch (exceptions -> ok False, like the live pump)."""
    calls = []

    def fake_urlopen(request, timeout=None):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append(payload["method"])
        try:
            body = server._dispatch(payload)
        except Exception as exc:  # noqa: BLE001 - mirrors LiveSceneControlServer._pump
            body = {"ok": False, "message": str(exc)}
        return _FakeResponse(body)

    previous = http_bridge.urlopen
    http_bridge.urlopen = fake_urlopen
    try:
        yield calls
    finally:
        http_bridge.urlopen = previous


class _BasicServer:
    """An old live_scene_control server: ping and snapshot only; anything else is 'Unsupported live scene method'."""

    def __init__(self, bridge, stage_name: str) -> None:
        self._bridge = bridge
        self.stage_name = stage_name

    def _dispatch(self, payload):
        method = str(payload.get("method", "")).strip().lower()
        if method == "ping":
            return {"ok": True, "result": {"status": "ok"}}
        if method == "snapshot":
            snapshot = {"stage_name": self.stage_name, "root_path": "/", "selection": [], "prim_count": 2, "prims": []}
            return {"ok": True, "result": snapshot}
        raise ValueError(f"Unsupported live scene method: {method}")


class _FakeSelection:
    def __init__(self) -> None:
        self.paths = []

    def get_selected_prim_paths(self):
        return list(self.paths)

    def set_selected_prim_paths(self, paths, _expand):
        self.paths = list(paths)


@contextlib.contextmanager
def _live_kit_bridge(stage):
    """A real LiveKitSceneBridge on a temporary stage; only Kit's omni.usd context is faked (always restored)."""
    selection = _FakeSelection()
    context = types.SimpleNamespace(get_stage=lambda: stage, get_selection=lambda: selection)
    previous = kit_bridge.omni
    kit_bridge.omni = types.SimpleNamespace(usd=types.SimpleNamespace(get_context=lambda _name="": context))
    try:
        yield kit_bridge.LiveKitSceneBridge(), selection
    finally:
        kit_bridge.omni = previous


def _bank_items():
    return {item["id"]: item for item in exam_bank.load_bank()}


def _wrong_choice(item):
    return next(index for index in range(len(item["options"])) if [index] != sorted(item["answer"]))


# --------------------------------------------------------------------------- registration and defaults


def _require_pxr() -> None:
    """Skip (not fail) a test that needs the USD Python bindings when pxr is missing or does not import."""
    try:
        import pxr  # noqa: F401
    except ImportError:
        import unittest

        raise unittest.SkipTest("USD Python bindings (pxr) are not installed")


def test_star_tools_are_registered_with_safe_defaults():
    with _server() as (call, _root):
        tools = {tool.name: tool for tool in call.app._tool_manager.list_tools()}
        assert STAR_TOOLS <= set(tools)
        for name in INSIGHT_TOOLS:
            mode = tools[name].parameters["properties"]["mode"]
            assert mode["default"] == "auto", name
            assert "SAVED file" in tools[name].description and "live" in tools[name].description, name
        for name in AUTHORING_TOOLS:
            assert tools[name].parameters["properties"]["dry_run"]["default"] is True, name
            description = " ".join(tools[name].description.split())
            assert description.startswith("DRY RUN BY DEFAULT"), name
            assert "changes the user's open scene" in description and "preview" in description, name
        start = tools["learning_mock_exam_start"].parameters["properties"]
        assert start["count"]["default"] == 65 and start["minutes"]["default"] == 120
        assert "TIMED" in tools["learning_mock_exam_start"].description
        # Omitted minutes keep the saved value (F4), so the tool default is None, not 60.
        assert tools["learning_exam_set_date"].parameters["properties"]["minutes_per_day"].get("default") is None
        assert "day" in tools["learning_exam_day_complete"].parameters.get("required", [])
        for name in STAR_TOOLS:
            assert tools[name].description.strip() and "\n " not in tools[name].description, name


def test_runtime_builds_the_mock_store_with_the_progress_store_rules():
    expected = REAL_EXAM_PREP / "mock_sessions.json"
    before = _exam_prep_state()
    assert runtime.build_mock_store({}).path == expected  # path only; nothing is read or written here
    test_store = runtime.build_mock_store({"OMNIVERSE_SCENE_MCP_TEST": "1"})
    assert isinstance(test_store, MockExamStore) and test_store.memory_only is True and test_store.path is None
    with tempfile.TemporaryDirectory() as tmp:
        chosen = runtime.build_mock_store({"OMNIVERSE_SCENE_MCP_TEST": "1", "OMNIVERSE_EXAM_PREP_DIR": tmp})
        assert chosen.memory_only is False and chosen.path == Path(tmp) / "mock_sessions.json"
        assert list(Path(tmp).iterdir()) == []
    assert _exam_prep_state() == before


# --------------------------------------------------------------------------- daily lessons


def test_daily_lesson_tools_round_trip_in_memory():
    with _server() as (call, _root):
        lesson = call("learning_exam_day")
        assert lesson["ok"] is True and lesson["day"] == 1 and lesson["total_days"] == 30
        assert lesson["completed"] is False and lesson["how_to_continue"]
        assert any("learning_exam_day_complete(day=1)" in step for step in lesson["how_to_continue"])
        for block in lesson.get("blocks") or []:
            for question in block["quiz"]["questions"]:
                assert "answer" not in question and "explanations" not in question

        done = call("learning_exam_day_complete", day=1)
        assert done["ok"] is True and done["next_day"] == 2 and done["already_completed"] is False
        assert call("learning_exam_day_complete", day=1)["already_completed"] is True
        assert call("learning_exam_day")["day"] == 2
        assert call("learning_exam_day", day=1)["completed"] is True
        assert call("learning_exam_day", day=99)["ok"] is False
        assert call("learning_exam_day_complete", day=0)["ok"] is False

        plan = call("learning_exam_set_date", days_until_exam=10, minutes_per_day=45)
        assert plan["ok"] is True and plan["scheduled_days"] == 10 and plan["cleared_completed_days"] == 1
        assert plan["settings"]["minutes_per_day"] == 45 and len(plan["outline"]) == 10
        assert call("learning_exam_day")["day"] == 1
        assert call("learning_exam_set_date", exam_date="2026-12-01", days_until_exam=5)["ok"] is False
        assert call("learning_exam_set_date", exam_date="next tuesday")["ok"] is False
        assert call("learning_exam_set_date", exam_date="2000-01-01")["ok"] is False
        assert call("learning_exam_set_date", days_until_exam=7, minutes_per_day=5)["ok"] is False
        assert call("learning_exam_progress")["storage"]["memory_only"] is True


def test_router_dispatch_runs_the_daily_lesson_and_only_explains_mock_exams():
    with _server() as (call, _root):
        routed = call("router_dispatch", request="please start day 2")
        assert routed["decision"]["target"] == "learn"
        lesson = routed["payload"]
        assert lesson["style"] == "exam_day" and lesson["executed"] is True
        assert lesson["ok"] is True and lesson["day"] == 2 and lesson["arguments"] == {"day": 2}
        assert call("router_dispatch", request="next lesson")["payload"]["day"] == 1
        assert call("router_route", request="day 3")["target"] == "learn"

        mock = call("router_dispatch", request="start a mock exam")["payload"]
        assert mock["ok"] is True and mock["started"] is False and mock["style"] == "mock_exam"
        assert mock["tool"] == "learning_mock_exam_start" and mock["arguments"] == {"count": 65, "minutes": 120}
        assert "open_mock" not in mock
        status = call("learning_mock_exam_status")
        assert status["ok"] is True and status["open"] is False and status["recent"] == []

        started = call("learning_mock_exam_start", count=3, minutes=10)
        again = call("router_dispatch", request="mock exam")["payload"]
        assert again["started"] is False and again["open_mock"]["session_id"] == started["session_id"]
        assert again["tool"] == "learning_mock_exam_status"
        assert call("learning_mock_exam_status")["session_id"] == started["session_id"]  # still the same one mock


# --------------------------------------------------------------------------- mock exam


def test_mock_exam_tools_full_cycle_in_memory():
    items = _bank_items()
    with _server() as (call, _root):
        started = call("learning_mock_exam_start", count=12, minutes=30)
        assert started["ok"] is True and started["count"] == 12 and started["minutes"] == 30
        assert started["storage"]["memory_only"] is True
        assert len(started["questions"]) == 10 and started["next_page"]["offset"] == 10
        assert started["how_to_answer"].startswith("Answer with learning_mock_exam_answer")
        for question in started["questions"]:
            assert not {"answer", "explanations", "citations", "verify"} & set(question)
        session = started["session_id"]
        ids = started["question_ids"]

        page = call("learning_mock_exam_questions", session_id=session, offset=10)
        assert [q["number"] for q in page["questions"]] == [11, 12] and "next_page" not in page

        first = items[ids[0]]
        right = call("learning_mock_exam_answer", session_id=session, question_id=ids[0], chosen=first["answer"])
        assert right["ok"] is True and right["answered"] is True
        second = items[ids[1]]
        wrong = call("learning_mock_exam_answer", session_id=session, question_id=2, chosen=["ABCDEF"[_wrong_choice(second)]])
        assert wrong["ok"] is True and wrong["number"] == 2
        assert call("learning_mock_exam_answer", session_id=session, question_id=3, chosen="Z")["ok"] is False
        assert call("learning_mock_exam_answer", session_id=session, question_id="nope-001", chosen="A")["ok"] is False
        assert call("learning_mock_exam_answer", session_id="mock-unknown", question_id=1, chosen="A")["ok"] is False

        flag = call("learning_mock_exam_flag", session_id=session, question_id=ids[2])
        assert flag["ok"] is True and flag["flagged_ids"] == [ids[2]]
        status = call("learning_mock_exam_status", session_id=session)
        assert status["open"] is True and status["answered"] == 2 and status["flagged"] == [ids[2]]
        assert status["state"] == "in_progress" and status["remaining_seconds"] > 0

        blocked = call("learning_mock_exam_start")
        assert blocked["ok"] is False and blocked["open_mock"]["session_id"] == session and blocked["hint"]

        report = call("learning_mock_exam_finish", session_id=session)
        assert report["score"]["questions"] == 12 and report["score"]["answered"] == 2
        assert report["score"]["correct"] == 1 and report["score"]["unanswered"] == 10
        assert report["progress"]["recorded"] == 2 and report["progress"]["source"] == "mock"
        assert "no passing score" in report["readiness"]["note"]
        assert any(row["question_id"] == ids[1] for row in report["wrong"])
        progress = call("learning_exam_progress")
        assert progress["totals"]["answered"] == 2 and progress["recent"][0]["source"] == "mock"
        assert call("learning_mock_exam_finish", session_id=session)["ok"] is False
        after = call("learning_mock_exam_status")
        assert after["open"] is False and after["recent"][0]["score_pct"] == report["score"]["pct"]

        other = call("learning_mock_exam_start", count=2, minutes=5)
        dropped = call("learning_mock_exam_abandon", session_id=other["session_id"])
        assert dropped["ok"] is True and dropped["status"] == "abandoned"
        assert call("learning_mock_exam_answer", session_id=other["session_id"], question_id=1, chosen="A")["ok"] is False
        assert call("learning_exam_progress")["totals"]["answered"] == 2


def test_mock_and_lesson_state_persist_only_in_the_chosen_exam_prep_dir():
    with tempfile.TemporaryDirectory() as prep:
        folder = Path(prep)
        with _server(exam_prep_dir=folder) as (call, _root):
            started = call("learning_mock_exam_start", count=2, minutes=5)
            assert started["storage"]["path"] == str(folder / "mock_sessions.json")
            assert call("learning_mock_exam_answer", session_id=started["session_id"], question_id=1, chosen="A")["ok"]
            first = call("learning_exam_day")
            assert first["ok"] is True
            assert call("learning_exam_day_complete", day=1)["ok"] is True
        with _server(exam_prep_dir=folder) as (call, _root):  # a restarted server finds the open mock and the plan
            status = call("learning_mock_exam_status")
            assert status["open"] is True and status["answered"] == 1
            report = call("learning_mock_exam_finish", session_id=started["session_id"])
            assert report["progress"]["persisted"] is True
            assert report["progress"]["path"] == str(folder / "progress.json")
            lesson = call("learning_exam_day")
            assert lesson["plan_start"] == first["plan_start"] and lesson["day"] == 2
        names = {path.name for path in folder.iterdir()}
        assert {"mock_sessions.json", "progress.json"} <= names


# --------------------------------------------------------------------------- live or saved-file insight


def test_insight_modes_fall_back_to_the_saved_file_on_the_fake_scene():
    _require_pxr()
    with _server() as (call, root):
        shot = str(root / "shot.usda")
        explicit = call("scene_explain_value", prim_path="/World/Box", attribute="size", file=shot)
        assert explicit["ok"] is True and explicit["mode"] == "saved_file" and explicit["mode_requested"] == "auto"
        assert explicit["value"] == 3 and "fallback_note" not in explicit

        for name, arguments in (
            ("scene_stage_info", {}),
            ("scene_composition", {"prim_path": "/World/Box"}),
            ("scene_explain_value", {"prim_path": "/World/Box", "attribute": "size"}),
            ("scene_properties", {"prim_path": "/World/Box"}),
            ("scene_bounds", {"prim_path": "/World/Box"}),
            ("usd_validate", {}),
        ):
            auto = call(name, **arguments)
            assert auto["mode"] == "saved_file" and auto["mode_requested"] == "auto", name
            assert "fake scene" in auto["fallback_note"] and auto["live_fallback"]["needs_bridge_update"] is False, name
            assert auto["ok"] is False and "not saved" in auto["message"], name  # the fake scene has no file
            live = call(name, mode="live", **arguments)
            assert live["ok"] is False and live["mode"] == "live" and live["needs_bridge_update"] is False, name
            saved = call(name, mode="saved_file", file=shot, **arguments)
            assert saved["mode"] == "saved_file" and saved["mode_requested"] == "saved_file", name
            assert saved["ok"] is True or (name == "usd_validate" and "issues" in saved), name

        assert call("scene_stage_info", mode="bogus")["ok"] is False
        mixed = call("scene_stage_info", mode="live", file=shot)
        assert mixed["ok"] is False and mixed["mode"] == "live" and "saved_file" in mixed["message"]

        capabilities = call("scene_capabilities")
        assert capabilities["ok"] is True and capabilities["needs_bridge_update"] is False
        assert capabilities["live_insight"]["available"] is False and capabilities["live_authoring"]["available"] is False
        assert capabilities["backend"]["bridge"] == "FakeSceneBridge" and capabilities["how_to_enable"]


def test_old_live_bridge_reports_reload_needed_and_falls_back_to_its_saved_file():
    _require_pxr()
    def scene(root):
        return SceneMCPService(HttpSceneBridge("http://127.0.0.1:9911"))

    with _server(scene=scene) as (call, root):
        old = _BasicServer(None, str((root / "shot.usda").resolve()))
        with _in_process_transport(old) as rpc_calls:
            capabilities = call("scene_capabilities")
            assert capabilities["needs_bridge_update"] is True and capabilities["how_to_enable"]
            assert "reload_mcp_bridge.py" in " ".join(capabilities["how_to_enable"])
            assert capabilities["live_insight"]["available"] is False

            auto = call("scene_explain_value", prim_path="/World/Box", attribute="size")
            assert auto["ok"] is True and auto["mode"] == "saved_file" and auto["value"] == 3
            assert auto["live_fallback"]["needs_bridge_update"] is True and "reload" in auto["fallback_note"]
            live = call("scene_stage_info", mode="live")
            assert live["ok"] is False and live["needs_bridge_update"] is True and live["how_to_enable"]

            light = call("scene_create_light", path="/World/Key", light_type="rect")
            assert light["ok"] is False and light["applied"] is False and light["needs_bridge_update"] is True
            assert light["changes"] == [] and light["how_to_enable"]
        assert "insight" not in rpc_calls  # auto mode asked for capabilities first and never sent the query
        assert {"capabilities", "snapshot", "create_light"} <= set(rpc_calls)


def test_live_insight_and_authoring_through_the_extended_rpc():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Box").CreateSizeAttr(4.0)  # differs from the saved shot.usda (3)

    def scene(root):
        return SceneMCPService(HttpSceneBridge("http://127.0.0.1:9911"))

    with _live_kit_bridge(stage) as (bridge, selection):
        server = kit_rpc.make_extended_server_class(_BasicServer)(bridge, stage.GetRootLayer().identifier)
        with _server(scene=scene) as (call, root), _in_process_transport(server):
            capabilities = call("scene_capabilities")
            assert capabilities["needs_bridge_update"] is False and capabilities["rpc_version"] == "tier7-1"
            assert capabilities["live_insight"]["available"] is True and capabilities["live_authoring"]["available"] is True
            assert "how_to_enable" not in capabilities

            live = call("scene_explain_value", prim_path="/World/Box", attribute="size")
            assert live["ok"] is True and live["mode"] == "live" and live["mode_requested"] == "auto"
            assert live["value"] == 4.0 and "fallback_note" not in live
            saved = call("scene_explain_value", prim_path="/World/Box", attribute="size", file=str(root / "shot.usda"))
            assert saved["ok"] is True and saved["mode"] == "saved_file" and saved["value"] == 3
            assert call("scene_stage_info", mode="live")["mode"] == "live"
            assert call("usd_validate", mode="live")["profile"] == "exam"
            missing = call("scene_composition", prim_path="/World/Nope")
            assert missing["ok"] is False and missing["mode"] == "live" and "does not exist" in missing["message"]

            before = stage.GetRootLayer().ExportToString()
            preview = call("scene_create_light", path="/World/Key", light_type="rect", intensity=800, size=[2, 1],
                           translation=[0, -4, 3], aim_at="/World/Box")
            assert preview["ok"] is True and preview["dry_run"] is True and preview["preview"] is True
            assert preview["applied"] is False and "only after they agree" in preview["message"]
            assert preview["payload"]["would_create"]["type"] == "RectLight"
            assert stage.GetRootLayer().ExportToString() == before and not stage.GetPrimAtPath("/World/Key")
            camera = call("scene_create_camera", path="/World/Cam", focal_length=35, translation=[0, -6, 2], aim_at="/World/Box")
            assert camera["ok"] is True and camera["preview"] is True and not stage.GetPrimAtPath("/World/Cam")
            assert call("scene_aim", path="/World/Box", target=[0, 0, 10])["preview"] is True
            assert stage.GetRootLayer().ExportToString() == before

            applied = call("scene_create_light", path="/World/Key", light_type="rect", intensity=800, size=[2, 1],
                           translation=[0, -4, 3], aim_at="/World/Box", dry_run=False)
            assert applied["ok"] is True and applied["applied"] is True and applied["dry_run"] is False
            key = stage.GetPrimAtPath("/World/Key")
            assert key and key.GetTypeName() == "RectLight"
            assert key.GetAttribute("inputs:width").Get() == 2 and key.GetAttribute("inputs:height").Get() == 1
            assert selection.paths == ["/World/Key"]
            aimed = call("scene_aim", path="/World/Key", target="/World/Box")
            assert aimed["ok"] is True and aimed["preview"] is True


def test_authoring_tools_check_their_inputs_and_change_nothing_on_the_fake_scene():
    with _server() as (call, _root):
        summary = call("scene_summary")
        for name, arguments, fragment in (
            ("scene_create_light", {"path": "/World/Key", "light_type": "rect", "color": [1, 2]}, "color"),
            ("scene_create_light", {"path": "/World/Key", "light_type": "rect", "size": [1, 2, 3]}, "size"),
            ("scene_create_light", {"path": "", "light_type": "rect"}, "required"),
            ("scene_create_camera", {"path": "/World/Cam", "clipping_range": [1]}, "clipping_range"),
            ("scene_aim", {"path": "/World/Props/Cube", "target": " "}, "target"),
            ("scene_aim", {"path": "/World/Props/Cube", "target": [1, 2]}, "target"),
        ):
            result = call(name, **arguments)
            assert result["ok"] is False and result["applied"] is False and fragment in result["message"], (name, result)
        for name, arguments in (
            ("scene_create_light", {"path": "/World/Key", "light_type": "distant", "intensity": 3}),
            ("scene_create_camera", {"path": "/World/Cam"}),
            ("scene_aim", {"path": "/World/Props/Cube", "target": "/World"}),
        ):
            result = call(name, **arguments)
            assert result["ok"] is False and result["dry_run"] is True and result["fake_scene"] is True, name
            assert result["needs_bridge_update"] is False and result["changes"] == [], name
        assert call("scene_summary") == summary


# --------------------------------------------------------------------------- regressions from the exam-tools review


def test_open_mock_questions_are_not_graded_or_served_elsewhere():
    items = _bank_items()
    with _server() as (call, _root):
        started = call("learning_mock_exam_start", count=65, minutes=120)
        mock_ids = set(started["question_ids"])
        first = started["question_ids"][0]
        blocked = call("learning_exam_answer", question_id=first, chosen=items[first]["answer"])
        assert blocked["ok"] is False and "learning_mock_exam_answer" in blocked["message"], blocked
        assert "correct_letters" not in blocked and "explanations" not in blocked
        assert call("learning_exam_progress")["totals"]["answered"] == 0
        quiz = call("learning_exam_quiz", count=50)
        assert quiz["ok"] is True and quiz["count"] == 50 and not mock_ids & {q["id"] for q in quiz["questions"]}
        for day in (1, 2, 7):  # two study days and a review day
            lesson = call("learning_exam_day", day=day)
            served = set((lesson.get("mixed_practice") or {}).get("question_ids") or [])
            for block in lesson.get("blocks") or []:
                served |= set((block.get("quiz") or {}).get("question_ids") or [])
                if (block.get("exercise") or {}).get("kind") == "predict":
                    served.add(block["exercise"]["question_id"])
            assert lesson["ok"] is True and served and not mock_ids & served, (day, mock_ids & served)
        call("learning_mock_exam_abandon", session_id=started["session_id"])
        graded = call("learning_exam_answer", question_id=first, chosen=items[first]["answer"])
        assert graded["ok"] is True and graded["correct"] is True


def test_set_date_without_minutes_keeps_the_saved_minutes():
    with _server() as (call, _root):
        first = call("learning_exam_set_date", days_until_exam=20, minutes_per_day=90)
        assert first["ok"] is True and first["settings"]["minutes_per_day"] == 90
        moved = call("learning_exam_set_date", days_until_exam=25)
        assert moved["ok"] is True and moved["settings"]["minutes_per_day"] == 90 and moved["minutes_per_day"] == 90
        assert moved["previous"]["minutes_per_day"] == 90 and "schedule" not in moved["settings"]


def test_mock_answer_tool_offers_allow_late_after_the_deadline():
    from datetime import timedelta

    from omniverse_scene_mcp.exam import srs as exam_srs

    with _server() as (call, _root):
        tools = {tool.name: tool for tool in call.app._tool_manager.list_tools()}
        assert tools["learning_mock_exam_answer"].parameters["properties"]["allow_late"]["default"] is False
        started = call("learning_mock_exam_start", count=2, minutes=5)
        session = started["session_id"]
        real_now = exam_srs.utc_now
        later = real_now() + timedelta(minutes=10)
        exam_srs.utc_now = lambda: later
        try:
            refused = call("learning_mock_exam_answer", session_id=session, question_id=1, chosen="A")
            assert refused["ok"] is False and "allow_late=True" in refused["message"], refused
            late = call("learning_mock_exam_answer", session_id=session, question_id=1, chosen="A", allow_late=True)
            assert late["ok"] is True and late["late"] is True and late["answered"] is True, late
        finally:
            exam_srs.utc_now = real_now


def test_live_explain_value_accepts_the_property_path_form():
    if Usd is None:  # pragma: no cover - pxr missing
        return
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    UsdGeom.Cube.Define(stage, "/World/Box").CreateSizeAttr(4.0)  # differs from the saved shot.usda (3)

    def scene(root):
        return SceneMCPService(HttpSceneBridge("http://127.0.0.1:9911"))

    with _live_kit_bridge(stage) as (bridge, _selection):
        server = kit_rpc.make_extended_server_class(_BasicServer)(bridge, stage.GetRootLayer().identifier)
        with _server(scene=scene) as (call, root), _in_process_transport(server):
            for mode in ("auto", "live"):
                live = call("scene_explain_value", prim_path="/World/Box.size", attribute="", mode=mode)
                assert live["ok"] is True and live["mode"] == "live" and live["value"] == 4.0, (mode, live)
            saved = call("scene_explain_value", prim_path="/World/Box.size", attribute="", file=str(root / "shot.usda"))
            assert saved["ok"] is True and saved["mode"] == "saved_file" and saved["value"] == 3
            named = call("scene_explain_value", prim_path="/World/Box", attribute="size", mode="live")
            assert named["ok"] is True and named["value"] == 4.0
