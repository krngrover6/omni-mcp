"""kit_live_extras: viewport capture and simulation controls against FAKE omni modules (no real Kit).

Fixture-free test_* functions. The fakes go into sys.modules only for the duration of one test and the previous
entries are put back; inside a real Kit process (carb / omni.kit.app loaded) every test returns at once, so a live
Kit's modules are never replaced.
"""

import contextlib
import os
import sys
import tempfile
import time
import types

from omniverse_scene_mcp import kit_live_extras as extras

_IN_KIT = "carb" in sys.modules or "omni.kit.app" in sys.modules
_NAMES = (
    "omni",
    "omni.kit",
    "omni.kit.viewport",
    "omni.kit.viewport.utility",
    "omni.kit.widget",
    "omni.kit.widget.viewport",
    "omni.kit.widget.viewport.capture",
    "omni.timeline",
    "omni.physx",
)
_MISSING = object()


@contextlib.contextmanager
def _fake_kit(**modules):
    """Install fake omni modules (keyword: dotted name with '_' for '.'); every other omni name fails to import."""
    saved = {name: sys.modules.get(name, _MISSING) for name in _NAMES}
    wanted = {key.replace("_", "."): value for key, value in modules.items()}
    omni = types.ModuleType("omni")
    omni.__path__ = []  # a package with nothing on disk: submodules not faked here cannot be imported
    try:
        for name in _NAMES:
            sys.modules[name] = None  # makes `import name` raise ImportError
        sys.modules["omni"] = omni
        for name, module in wanted.items():
            sys.modules[name] = module
        extras._PENDING.clear()
        yield
    finally:
        extras._PENDING.clear()
        for name, module in saved.items():
            if module is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


@contextlib.contextmanager
def _temp_root():
    """Point tempfile.gettempdir() at a fresh folder so default captures never land in the real temp dir."""
    previous = tempfile.tempdir
    with tempfile.TemporaryDirectory() as tmp:
        tempfile.tempdir = tmp
        try:
            yield tmp
        finally:
            tempfile.tempdir = previous


# ------------------------------------------------------------------------------------------------ viewport fakes


class _Path:
    def __init__(self, text):
        self.pathString = text


class FakeViewport:
    """Records every attribute write and set_active_camera call, so tests can prove nothing was switched."""

    def __init__(self, camera="/OmniverseKit_Persp", resolution=(1280, 720)):
        object.__setattr__(self, "writes", [])
        object.__setattr__(self, "camera_switches", [])
        object.__setattr__(self, "scheduled", [])
        object.__setattr__(self, "camera_path", _Path(camera))
        object.__setattr__(self, "resolution", resolution)
        object.__setattr__(self, "hydra_engine", "rtx")
        object.__setattr__(self, "render_mode", "RaytracedLighting")

    def __setattr__(self, name, value):
        self.writes.append(name)
        object.__setattr__(self, name, value)

    def set_active_camera(self, camera_path):
        self.camera_switches.append(camera_path)

    def schedule_capture(self, delegate):
        self.scheduled.append(delegate)
        return delegate


class _Future:
    def __init__(self):
        self.finished = False

    def done(self):
        return self.finished


class FakeCapture:
    def __init__(self, path):
        self.path = path
        self._Capture__future = _Future()  # the attribute name Kit's Capture base class uses


def _utility(viewport, *, to_file=True):
    module = types.ModuleType("omni.kit.viewport.utility")
    module.calls = []
    module.get_active_viewport = lambda usd_context_name="": viewport
    if to_file:

        def capture_viewport_to_file(viewport_api, file_path=None, is_hdr=False, **_kw):
            module.calls.append((viewport_api, file_path))
            return FakeCapture(file_path)

        module.capture_viewport_to_file = capture_viewport_to_file
    return module


def _settle(path, data=b"\x89PNG fake"):
    """Write the capture file as Kit would and backdate it so it counts as fully written."""
    with open(path, "wb") as handle:
        handle.write(data)
    old = time.time() - 5
    os.utime(path, (old, old))


# ------------------------------------------------------------------------------------------ simulation fakes


class FakeTimeline:
    """play/pause/stop are pending until commit(), like omni.timeline; stop resets to the start time."""

    def __init__(self, start=0.0, end=10.0, current=0.0, rate=60.0, state="stopped"):
        self.start, self.end, self.current, self.rate, self.state = start, end, current, rate, state
        self.pending = None
        self.commits = 0
        self.set_times = []

    def play(self, *_args):
        self.pending = "playing"

    def pause(self):
        # Like Kit (seen live in Isaac 6.1): pausing a stopped timeline does nothing.
        self.pending = None if self.state == "stopped" else "paused"

    def stop(self):
        self.pending = "stopped"

    def commit(self):
        self.commits += 1
        if self.pending:
            self.state, self.pending = self.pending, None
            if self.state == "stopped":
                self.current = self.start

    def is_playing(self):
        return self.state == "playing"

    def is_stopped(self):
        return self.state == "stopped"

    def get_current_time(self):
        return self.current

    def set_current_time(self, seconds):
        self.set_times.append(seconds)
        self.current = seconds

    def get_start_time(self):
        return self.start

    def get_end_time(self):
        return self.end

    def get_time_codes_per_seconds(self):
        return self.rate


class FakePhysX:
    def __init__(self, running=False, async_render=False):
        self.running = running
        self.async_render = async_render
        self.steps = []
        self.started = 0
        self.transform_updates = []

    def is_running(self):
        return self.running

    def is_asyncsimrender_enabled(self):
        return self.async_render

    def start_simulation(self):
        self.started += 1
        self.running = True

    def update_simulation(self, elapsed, current):
        self.steps.append((elapsed, current))

    def update_transformations(self, *args):
        self.transform_updates.append(args)


def _timeline_module(timeline):
    module = types.ModuleType("omni.timeline")
    module.get_timeline_interface = lambda name="": timeline
    return module


def _physx_module(physx):
    module = types.ModuleType("omni.physx")
    module.get_physx_interface = lambda: physx
    return module


# ------------------------------------------------------------------------------------------------ not in Kit


def test_without_kit_every_function_says_kit_not_available():
    if _IN_KIT:
        return
    with _fake_kit():
        for fn in (
            extras.capture_viewport,
            extras.simulation_status,
            extras.simulation_play,
            extras.simulation_pause,
            extras.simulation_stop,
            extras.simulation_step,
        ):
            result = fn()
            assert result["ok"] is False and result["message"] == "Kit not available", (fn.__name__, result)


def test_module_imports_no_omni_at_import_time():
    source = open(extras.__file__, encoding="utf-8").read()
    head = source.split("def _kit_module", 1)[0]
    assert "import omni" not in head and "from omni" not in head


# ------------------------------------------------------------------------------------------------ capture


def test_capture_creates_default_folder_and_returns_pending_with_camera():
    if _IN_KIT:
        return
    viewport = FakeViewport(camera="/World/Cam", resolution=(1920, 1080))
    utility = _utility(viewport)
    with _temp_root() as tmp, _fake_kit(omni_kit_viewport_utility=utility):
        folder = os.path.join(tmp, extras.CAPTURE_DIR_NAME)
        assert not os.path.isdir(folder)
        result = extras.capture_viewport()
        assert result["ok"] is True and result["status"] == "pending"
        assert os.path.isdir(folder)
        assert os.path.dirname(result["path"]) == folder
        name = os.path.basename(result["path"])
        assert name.startswith("viewport_") and name.endswith(".png")
        assert result["camera"] == "/World/Cam"
        assert result["resolution"] == [1920, 1080]
        assert result["renderer"] == "rtx" and result["render_mode"] == "RaytracedLighting"
        assert result["requested_resolution"] is None
        assert "not changed" in result["note"]
        assert utility.calls == [(viewport, result["path"])]


def test_capture_never_switches_camera_or_changes_viewport_settings():
    if _IN_KIT:
        return
    viewport = FakeViewport()
    with tempfile.TemporaryDirectory() as tmp, _fake_kit(omni_kit_viewport_utility=_utility(viewport)):
        result = extras.capture_viewport(tmp, width=640, height=360)
        assert result["ok"] is True
        assert viewport.camera_switches == []  # set_active_camera never called
        assert viewport.writes == []  # camera_path, resolution, hydra_engine, render_mode untouched
        assert result["resolution"] == [1280, 720]
        assert result["requested_resolution"] == [640, 360]


def test_capture_into_a_new_custom_folder_and_unique_names():
    if _IN_KIT:
        return
    viewport = FakeViewport()
    with tempfile.TemporaryDirectory() as tmp, _fake_kit(omni_kit_viewport_utility=_utility(viewport)):
        target = os.path.join(tmp, "nested", "shots")
        first = extras.capture_viewport(target)
        _settle(first["path"])
        second = extras.capture_viewport(target)
        assert os.path.isdir(target)
        assert first["path"] != second["path"]
        assert os.path.dirname(second["path"]) == os.path.abspath(target)


def test_capture_status_reports_pending_then_ready_with_size():
    if _IN_KIT:
        return
    viewport = FakeViewport()
    with tempfile.TemporaryDirectory() as tmp, _fake_kit(omni_kit_viewport_utility=_utility(viewport)):
        result = extras.capture_viewport(tmp)
        status = extras.capture_status(result["path"])
        assert status["ok"] is True and status["ready"] is False and status["size"] == 0
        assert status["capture_done"] is False
        # Kit writes the file a frame later; a file that was just touched does not count yet.
        with open(result["path"], "wb") as handle:
            handle.write(b"partial")
        assert extras.capture_status(result["path"])["ready"] is False
        _settle(result["path"], b"\x89PNG" + b"0" * 96)
        extras._PENDING[os.path.normcase(result["path"])]["capture"]._Capture__future.finished = True
        status = extras.capture_status(result["path"])
        assert status["ready"] is True and status["size"] == 100 and status["capture_done"] is True


def test_capture_status_rejects_non_png_and_handles_unknown_files():
    if _IN_KIT:
        return
    assert extras.capture_status("")["ok"] is False
    assert extras.capture_status("C:/somewhere/notes.txt")["ok"] is False
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "other.png")
        _settle(path)
        status = extras.capture_status(path)
        assert status["ok"] is True and status["ready"] is True and "Not a capture" in status["note"]


def test_capture_status_applies_a_requested_size_as_a_copy_or_explains_why_not():
    if _IN_KIT:
        return
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - Pillow is optional
        Image = None
    viewport = FakeViewport(resolution=(64, 32))
    with tempfile.TemporaryDirectory() as tmp, _fake_kit(omni_kit_viewport_utility=_utility(viewport)):
        result = extras.capture_viewport(tmp, width=16)
        assert result["requested_resolution"] == [16, None]
        if Image is not None:
            Image.new("RGB", (64, 32), (200, 10, 10)).save(result["path"])
            old = time.time() - 5
            os.utime(result["path"], (old, old))
        else:
            _settle(result["path"])
        status = extras.capture_status(result["path"])
        assert status["ready"] is True
        if Image is not None:
            assert status["resized_path"].endswith("_16x8.png")
            with Image.open(status["resized_path"]) as image:
                assert image.size == (16, 8)
            with Image.open(result["path"]) as image:
                assert image.size == (64, 32)  # the original capture is kept
        else:
            assert "Pillow" in status["resize_note"]


def test_capture_falls_back_to_schedule_capture_without_capture_viewport_to_file():
    if _IN_KIT:
        return
    viewport = FakeViewport()
    capture_module = types.ModuleType("omni.kit.widget.viewport.capture")
    capture_module.FileCapture = FakeCapture
    with tempfile.TemporaryDirectory() as tmp, _fake_kit(
        omni_kit_viewport_utility=_utility(viewport, to_file=False), omni_kit_widget_viewport_capture=capture_module
    ):
        result = extras.capture_viewport(tmp)
        assert result["ok"] is True and "schedule_capture" in result["api"]
        assert [delegate.path for delegate in viewport.scheduled] == [result["path"]]
        assert viewport.camera_switches == [] and viewport.writes == []


def test_capture_failures_are_reported_not_raised():
    if _IN_KIT:
        return
    with tempfile.TemporaryDirectory() as tmp:
        with _fake_kit(omni_kit_viewport_utility=_utility(None)):
            assert "no active viewport" in extras.capture_viewport(tmp)["message"]
        with _fake_kit(omni_kit_viewport_utility=_utility(FakeViewport(), to_file=False)):
            result = extras.capture_viewport(tmp)
            assert result["ok"] is False and "failed to start" in result["message"]
        with _fake_kit(omni_kit_viewport_utility=_utility(FakeViewport())):
            for bad in (0, -5, 1.5, True, "640", extras.MAX_CAPTURE_SIDE + 1):
                assert extras.capture_viewport(tmp, width=bad)["ok"] is False, bad
            assert extras.capture_viewport(tmp, height=0)["ok"] is False


# ---------------------------------------------------------------------------------------- simulation controls


def test_status_play_pause_stop_transitions_and_stop_resets_to_start():
    if _IN_KIT:
        return
    timeline = FakeTimeline(start=1.0, current=1.0)
    with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(FakePhysX())):
        status = extras.simulation_status()
        assert status["ok"] and status["stopped"] and not status["playing"] and not status["paused"]
        assert status["time_codes_per_second"] == 60.0 and status["physics_available"] is True

        played = extras.simulation_play()
        assert played["playing"] is True and played["paused"] is False and played["action"] == "play"
        assert "not authored scene data" in played["note"]
        assert extras.simulation_play()["message"].startswith("The simulation was already playing")

        timeline.current = 4.5
        paused = extras.simulation_pause()
        assert paused["paused"] is True and paused["playing"] is False and paused["current_time"] == 4.5
        assert paused["current_time_code"] == 4.5 * 60

        stopped = extras.simulation_stop()
        assert stopped["stopped"] is True and stopped["current_time"] == 1.0
        assert "reset to its start time (1 s)" in stopped["message"]
        assert timeline.commits >= 3  # changes are committed so the returned status is the new state


def test_status_without_physx_reports_physics_unavailable():
    if _IN_KIT:
        return
    with _fake_kit(omni_timeline=_timeline_module(FakeTimeline())):
        assert extras.simulation_status()["physics_available"] is False


def test_step_with_physx_steps_each_frame_while_paused():
    if _IN_KIT:
        return
    timeline = FakeTimeline(current=2.0, state="paused")
    physx = FakePhysX(running=True)
    with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(physx)):
        result = extras.simulation_step(3)
        assert result["ok"] is True and result["via"] == "physx" and result["frames"] == 3
        assert result["capped"] is False and result["paused"] is True
        assert [round(t, 6) for _dt, t in physx.steps] == [round(2.0 + i / 60, 6) for i in (1, 2, 3)]
        assert all(abs(dt - 1 / 60) < 1e-12 for dt, _t in physx.steps)
        assert physx.transform_updates == [(False, True, True)] * 3  # one write-back per frame
        assert physx.started == 0  # already running: not restarted
        assert abs(result["current_time"] - (2.0 + 3 / 60)) < 1e-9


def test_step_from_stopped_is_refused_so_stop_can_restore_poses():
    if _IN_KIT:
        return
    timeline = FakeTimeline(state="stopped")
    physx = FakePhysX(running=False)
    with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(physx)):
        result = extras.simulation_step(5)
        assert result["ok"] is False and result["stopped"] is True
        assert "sim_play, then sim_pause" in result["message"]  # the router's tool names
        assert physx.steps == [] and physx.started == 0 and timeline.set_times == []  # nothing simulated


def test_step_after_play_then_pause_starts_physx_and_stop_resets():
    if _IN_KIT:
        return
    timeline = FakeTimeline(state="stopped")
    physx = FakePhysX(running=False)
    with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(physx)):
        assert extras.simulation_play()["playing"] is True
        assert extras.simulation_pause()["paused"] is True
        result = extras.simulation_step()
        assert result["ok"] is True and result["paused"] is True
        assert physx.started == 1 and len(physx.steps) == 1
        assert extras.simulation_stop()["current_time"] == 0.0


def test_step_caps_frames_and_validates_input():
    if _IN_KIT:
        return
    timeline = FakeTimeline(state="paused", end=5.0)
    physx = FakePhysX(running=True)
    with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(physx)):
        result = extras.simulation_step(10_000)
        assert result["ok"] is True and result["frames"] == extras.MAX_STEP_FRAMES == 600
        assert result["requested_frames"] == 10_000 and result["capped"] is True
        assert len(physx.steps) == 600 and "capped at 600" in result["notes"][0]
        assert "past the timeline end time" in result["notes"][-1]  # 5 s end; 600 frames at 60 fps reach 10 s
        for bad in (0, -1, 1.5, True, "3", None):
            assert extras.simulation_step(bad)["ok"] is False, bad
        assert len(physx.steps) == 600  # rejected calls stepped nothing


def test_step_refuses_while_playing():
    if _IN_KIT:
        return
    timeline = FakeTimeline(state="playing", current=3.0)
    physx = FakePhysX(running=True)
    with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(physx)):
        result = extras.simulation_step(5)
        assert result["ok"] is False and "sim_pause" in result["message"] and result["playing"] is True
        assert physx.steps == [] and timeline.set_times == []


def test_step_without_physx_moves_the_timeline_and_says_physics_may_not_advance():
    if _IN_KIT:
        return
    timeline = FakeTimeline(state="paused", current=1.0, rate=24.0)
    with _fake_kit(omni_timeline=_timeline_module(timeline)):
        result = extras.simulation_step(12)
        assert result["ok"] is True and result["via"] == "timeline"
        assert abs(result["current_time"] - 1.5) < 1e-9 and result["seconds_per_frame"] == 1 / 24
        assert any("physics may not advance" in note for note in result["notes"])
        assert result["physics_available"] is False


def test_step_with_async_sim_render_uses_the_timeline_only():
    if _IN_KIT:
        return
    timeline = FakeTimeline(state="paused")
    physx = FakePhysX(running=True, async_render=True)
    with _fake_kit(omni_timeline=_timeline_module(timeline), omni_physx=_physx_module(physx)):
        result = extras.simulation_step(2)
        assert result["ok"] is True and result["via"] == "timeline" and physx.steps == []
        assert any("async" in note for note in result["notes"])


def test_timeline_errors_are_reported_not_raised():
    if _IN_KIT:
        return

    class Broken(FakeTimeline):
        def play(self, *_args):
            raise RuntimeError("boom")

    with _fake_kit(omni_timeline=_timeline_module(Broken())):
        result = extras.simulation_play()
        assert result["ok"] is False and "boom" in result["message"]
