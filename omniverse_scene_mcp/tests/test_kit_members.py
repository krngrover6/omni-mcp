"""kit_object_members (runs inside Kit) and usd_code_verify's live Kit member check through the router."""
from __future__ import annotations

try:
    from pxr import Usd  # noqa: F401
except ImportError:  # pragma: no cover
    import unittest

    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

from omniverse_scene_mcp import code_verify, features, kit_index
from omniverse_scene_mcp.kit_members import object_members


class _Context:
    def get_stage(self):
        return "stage"

    def new_stage(self):  # must never be called by the tool
        raise AssertionError("new_stage was called")


def _fake_omni():
    omni = types.ModuleType("omni")
    usd = types.ModuleType("omni.usd")
    usd.get_context = lambda: _Context()
    usd.new_stage = lambda: (_ for _ in ()).throw(AssertionError("called"))
    omni.usd = usd
    return {"omni": omni, "omni.usd": usd}


def test_object_members_follows_only_names_and_getters():
    with mock.patch.dict(sys.modules, _fake_omni()):
        result = object_members("omni.usd.get_context()", ["get_stage", "get_stag"])
        assert result["ok"] and result["type"] == "_Context"
        assert result["has"] == {"get_stage": True, "get_stag": False} and "new_stage" in result["members"]
        assert not object_members("omni.usd.new_stage()")["ok"]  # not a getter: never called
        assert not object_members("omni.usd.get_context('x')")["ok"]  # arguments are refused
        assert not object_members("__import__('os').getcwd()")["ok"]
        assert not object_members("os.getcwd()")["ok"]  # only omni / carb / isaacsim
    assert features.FEATURES["kit_object_members"].location == "kit"


def _kit_install(tmp):
    root = Path(tmp)
    usd = root / "extscache" / "omni.usd-1" / "omni" / "usd"
    usd.mkdir(parents=True)
    (root / "kit").mkdir()
    (usd / "__init__.py").write_text("def get_context(name=''):\n    pass\n")
    return kit_index.KitIndex(root)


def test_verify_code_asks_the_live_app_about_kit_object_methods():
    code = ("import omni.usd\nctx = omni.usd.get_context()\nstage = ctx.get_stage()\n"
            "ctx.open_stag('a.usda')\nprint(stage.GetPrimAtPath('/World'))\n")
    asked = []

    def live(expression, names):
        asked.append((expression, sorted(names)))
        return {"ok": True, "type": "UsdContext", "has": {n: n != "open_stag" for n in names},
                "members": ["get_stage", "open_stage", "close_stage"]}

    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(code_verify, "shared_index", return_value=_kit_install(tmp)):
            result = code_verify.verify_code(code, run=False, kit_members=live)
            assert asked == [("omni.usd.get_context()", ["get_stage", "open_stag"])]
            problem = next(p for p in result["problems"] if p["code"] == "unknown_kit_member")
            assert problem["line"] == 4 and problem["did_you_mean"][0] == "open_stage"
            assert result["kit_live"]["members_checked"] == 2
            assert "Usd.Stage.GetPrimAtPath" in result["checked"]  # get_stage still gives a USD stage
            offline = code_verify.verify_code(code, run=False)
            assert offline["kit_live"]["ok"] is False and "bridge" in offline["kit_live"]["note"]
            dead = code_verify.verify_code(code, run=False, kit_members=lambda e, n: None)
            assert dead["kit_live"]["objects_not_reached"] == ["omni.usd.get_context()"]


class _App:
    def __init__(self):
        self.tools = {}

    def tool(self, name):
        def add(fn):
            self.tools[name] = fn
            return fn
        return add


def test_router_tool_passes_scene_file_and_the_live_callback():
    calls = []

    class _Bridge:
        def _rpc(self, method, params):
            calls.append((method, params["tool"], params["arguments"]["expression"]))
            return {"ok": True, "type": "UsdContext", "has": {n: True for n in params["arguments"]["names"]},
                    "members": []}

    router = types.SimpleNamespace(_scene=types.SimpleNamespace(_bridge=_Bridge()))
    app = _App()
    features.register(app, router, file_mode=lambda *a: {"ok": False})
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(code_verify, "shared_index", return_value=_kit_install(tmp)):
            result = app.tools["usd_code_verify"](code="import omni.usd\nomni.usd.get_context().get_stage()\n",
                                                  run=False)
        assert calls == [("feature_call", "kit_object_members", "omni.usd.get_context()")]
        assert result["kit_live"]["members_checked"] == 1
        scene = Path(tmp) / "s.usda"
        scene.write_text('#usda 1.0\ndef Xform "World"\n{\n    def Cube "Box"\n    {\n    }\n}\n')
        ran = app.tools["usd_code_verify"](code="import omni.usd\n"
                                                "print(omni.usd.get_context().get_stage().GetPrimAtPath('/World/Box'))\n",
                                           scene_file=str(scene))
        assert ran.get("scene_file") == str(scene) and "/World/Box" in ran["run"]["stdout"]
        assert "kit_members" not in features.signature("usd_code_verify").parameters  # callers cannot inject it
