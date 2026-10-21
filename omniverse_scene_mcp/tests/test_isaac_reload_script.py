"""isaacsim-study/reload_mcp_bridge.py outside Kit: omni.usd is faked, no server starts, only a temp folder is written.

Not one of the reload script's own self-test files, so it never runs inside Isaac Sim (where it would touch the live
bridge); it also returns early whenever a real omni module is loaded.
"""

import asyncio
import builtins
import json
import os
import runpy
import sys
import tempfile
import types
from pathlib import Path

RELOAD_SCRIPT = Path(__file__).resolve().parents[2] / "isaacsim-study" / "reload_mcp_bridge.py"
_BUILTINS = ("_isaac_bridge_reload_task", "_isaac_scene_control_server", "_isaac_scene_control_start_task")
_MISSING = object()


class _OldServer:
    """Stands in for the running bridge; records whether the reload stopped or restarted it."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def stop(self) -> None:
        self.events.append("stop")

    async def start(self) -> None:
        self.events.append("start")


def test_reload_without_an_open_stage_keeps_the_old_bridge_and_reports_it():
    if not RELOAD_SCRIPT.is_file() or "omni" in sys.modules:
        return  # script missing, or running inside Kit: never touch a live bridge
    old = _OldServer()
    saved_builtins = {name: getattr(builtins, name, _MISSING) for name in _BUILTINS}
    saved_modules = {name: sys.modules.get(name, _MISSING) for name in ("omni", "omni.usd")}
    saved_path = list(sys.path)
    saved_root = os.environ.get("OMNIVERSE_ROOT")
    loop = asyncio.new_event_loop()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "isaacsim-study").mkdir()
        usd = types.ModuleType("omni.usd")
        usd.get_context = lambda _name="": types.SimpleNamespace(get_stage=lambda: None)  # no stage is open
        omni = types.ModuleType("omni")
        omni.usd = usd
        try:
            os.environ["OMNIVERSE_ROOT"] = str(root)
            sys.modules["omni"], sys.modules["omni.usd"] = omni, usd
            for name in _BUILTINS:
                if hasattr(builtins, name):
                    delattr(builtins, name)
            builtins._isaac_scene_control_server = old
            asyncio.set_event_loop(loop)
            runpy.run_path(str(RELOAD_SCRIPT), run_name="__main__")
            loop.run_until_complete(asyncio.wait_for(builtins._isaac_bridge_reload_task, 60))
            report = json.loads((root / "isaacsim-study" / "bridge-reload-result.json").read_text(encoding="utf-8"))
            still_registered = builtins._isaac_scene_control_server
        finally:
            asyncio.set_event_loop(None)
            loop.close()
            if saved_root is None:
                os.environ.pop("OMNIVERSE_ROOT", None)
            else:
                os.environ["OMNIVERSE_ROOT"] = saved_root
            for name, module in saved_modules.items():
                if module is _MISSING:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
            for name, value in saved_builtins.items():
                if value is _MISSING:
                    if hasattr(builtins, name):
                        delattr(builtins, name)
                else:
                    setattr(builtins, name, value)
            sys.path[:] = saved_path
    assert report["status"] == "kept_old_bridge" and "No stage is open" in report["error"], report
    assert "self_tests" not in report  # refused before any new code was imported or tested
    assert old.events == [] and still_registered is old  # the running bridge was never stopped
