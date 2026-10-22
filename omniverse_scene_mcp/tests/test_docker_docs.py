"""Docker USD docs helper: mocked docker CLI only (no real containers, sockets or config edits)."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

from omniverse_scene_mcp import docker_docs
from omniverse_scene_mcp.docker_docs import (
    FORBIDDEN_PORTS,
    KIT_USD_AGENTS_DOCKER,
    SERVICES,
    check_endpoint_reachable,
    docker_status,
    endpoint_for,
    env_presence,
    explain_choice,
    find_docker_cli,
    normalize_service,
    run_docker,
    scrub,
    setup_plan,
)

SENTINEL = "nvapi-SENTINEL-do-not-leak-1234567890"
NGC_SENTINEL = "ngc-SENTINEL-also-secret-0987654321"
CLI = r"C:\fake\docker.exe"
VERSION_UP = json.dumps({"Client": {"Version": "28.3.2"}, "Server": {"Version": "28.3.2"}})
VERSION_DOWN = json.dumps({"Client": {"Version": "28.3.2"}, "Server": None})
DAEMON_ERR = "error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."


def _proc(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeDocker:
    """Injected runner: answers the read-only queries and records every argv it is given."""

    def __init__(self, version=(0, VERSION_UP, ""), images="", ps="", raise_exc=None):
        self.version, self.images, self.ps, self.raise_exc = version, images, ps, raise_exc
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        assert timeout <= 15, "docker queries must use short timeouts"
        if self.raise_exc is not None:
            raise self.raise_exc
        sub = argv[1]
        if sub == "version":
            return _proc(*self.version)
        if sub == "image":
            return _proc(0, self.images)
        if sub == "ps":
            return _proc(0, self.ps)
        raise AssertionError(f"unexpected docker command {argv}")


def _no_probe(*args, **kwargs):
    raise AssertionError("no TCP probe unless check_endpoint=True")


def _refuse(*args, **kwargs):
    raise AssertionError("tests must never start real processes or open real sockets")


@contextlib.contextmanager
def _nothing_real():
    patched = [(subprocess, "Popen"), (subprocess, "run"), (os, "system"), (docker_docs.socket, "create_connection")]
    originals = [(owner, name, getattr(owner, name)) for owner, name in patched]
    for owner, name in patched:
        setattr(owner, name, _refuse)
    try:
        yield
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)


@contextlib.contextmanager
def _tmp_root(with_wheels=False, with_env_file=False):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mcp = root / "kit-usd-agents" / "source" / "mcp"
        dist = mcp / "usd_code_mcp" / "dist"
        dist.mkdir(parents=True)
        if with_wheels:
            (dist / "usd_code_aiq-0.3.0-py3-none-any.whl").write_bytes(b"x")
            (dist / "usd_code_mcp-1.0.0-py3-none-any.whl").write_bytes(b"x")
        if with_env_file:
            (mcp / ".env").write_text(f"NVIDIA_API_KEY={SENTINEL}\n", encoding="utf-8")
        yield root


def _status(fake=None, env=None, root=None, which=lambda name: CLI, **kwargs):
    with _nothing_real():
        return docker_status(
            runner=fake or FakeDocker(),
            which=which,
            candidates=[],
            probe=kwargs.pop("probe", _no_probe),
            env={} if env is None else env,
            root=root,
            **kwargs,
        )


# ---- docker absent / daemon stopped / images / running -------------------------------------------------------------


def test_docker_absent_runs_nothing():
    fake = FakeDocker()
    with _tmp_root() as root:
        status = _status(fake, root=root, which=lambda name: None)
    assert status["docker_installed"] is False
    assert status["daemon_running"] is False
    assert status["images_present"] == [] and status["container_running"] is False
    assert fake.calls == []
    assert any("not installed" in need for need in status["needs"])
    assert "offline" in status["summary"]
    json.dumps(status)


def test_docker_found_in_desktop_folder_when_not_on_path():
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / "docker.exe"
        exe.write_bytes(b"")
        found = find_docker_cli(which=lambda name: None, candidates=[Path(tmp) / "missing.exe", exe])
    assert found == {"path": str(exe), "on_path": False, "source": "Docker Desktop install folder"}
    assert find_docker_cli(which=lambda name: None, candidates=[])["path"] is None
    assert find_docker_cli(which=lambda name: CLI, candidates=[])["on_path"] is True


def test_daemon_stopped_skips_image_and_ps_queries():
    fake = FakeDocker(version=(1, VERSION_DOWN, DAEMON_ERR))
    with _tmp_root(with_wheels=True) as root:
        status = _status(fake, root=root)
    assert status["docker_installed"] is True
    assert status["client_version"] == "28.3.2" and status["server_version"] is None
    assert status["daemon_running"] is False
    assert [argv[1] for argv, _ in fake.calls] == ["version"]
    assert any("engine is not running" in need for need in status["needs"])
    assert "dockerDesktopLinuxEngine" in status["daemon_error"]
    assert "not running" in status["summary"]


def test_cli_missing_and_timeout_are_reported_not_raised():
    for exc, text in ((FileNotFoundError("docker"), "not found"),
                      (subprocess.TimeoutExpired("docker", 8), "timed out")):
        with _tmp_root() as root:
            status = _status(FakeDocker(raise_exc=exc), root=root)
        assert status["daemon_running"] is False
        assert text in status["daemon_error"]


def test_images_present_but_not_running():
    images = "\n".join([
        "usd-code-mcp:latest\t2.71GB",
        "usd-code-mcp:ngc\t2.70GB",
        "kit-mcp:ngc\t1.9GB",
        "python:3.13-slim\t150MB",
        "nvcr.io/nim/nvidia/nv-embedqa-e5-v5:latest\t6GB",
    ])
    fake = FakeDocker(images=images, ps="other\tredis:7\tUp 2 hours\t6379/tcp\n")
    with _tmp_root(with_wheels=True) as root:
        status = _status(fake, root=root)
    assert status["daemon_running"] is True
    assert [i["image"] for i in status["images_present"]] == ["usd-code-mcp:latest", "usd-code-mcp:ngc"]
    assert status["images_present"][0]["size"] == "2.71GB"
    assert [i["image"] for i in status["nim_images_present"]] == ["nvcr.io/nim/nvidia/nv-embedqa-e5-v5:latest"]
    assert status["container_running"] is False and status["containers"] == []
    assert any("not running" in need for need in status["needs"])
    assert [argv[1:3] for argv, _ in fake.calls] == [
        ["version", "--format"], ["image", "ls"], ["ps", "--format"]
    ]


def test_no_image_built_says_so():
    with _tmp_root(with_wheels=True) as root:
        status = _status(FakeDocker(images="python:3.13-slim\t150MB\n"), root=root)
    assert status["images_present"] == []
    assert any("No USD Code MCP image" in need for need in status["needs"])
    assert "no USD Code MCP image" in status["summary"]


def test_container_running_and_endpoint_check():
    ps = "usd-code-mcp\tusd-code-mcp:latest\tUp 5 minutes (healthy)\t127.0.0.1:9903->9903/tcp\n"
    fake = FakeDocker(images="usd-code-mcp:latest\t2.7GB\n", ps=ps)
    calls = []

    def probe(host, port, timeout):
        calls.append((host, port, timeout))

    with _tmp_root(with_wheels=True) as root:
        status = _status(fake, root=root, env={"NVIDIA_API_KEY": SENTINEL}, check_endpoint=True, probe=probe)
    assert status["container_running"] is True
    assert status["containers"][0]["status"].startswith("Up")
    assert status["endpoint"] == "http://127.0.0.1:9903/mcp"
    assert status["endpoint_reachable"] is True and status["ready"] is True
    assert calls == [("127.0.0.1", 9903, 1.0)]
    assert status["needs"] == []
    assert "running" in status["summary"]


def test_endpoint_not_checked_unless_asked():
    with _tmp_root() as root:
        status = _status(FakeDocker(), root=root)  # _no_probe would raise if called
    assert status["endpoint_reachable"] is None
    assert status["endpoint_check"]["checked"] is False


def test_endpoint_check_refuses_router_ports_and_remote_hosts():
    def probe(*args):
        raise AssertionError("must not connect")

    for port in sorted(FORBIDDEN_PORTS):
        result = check_endpoint_reachable(f"http://127.0.0.1:{port}/mcp", probe)
        assert result["checked"] is False and "router" in result["reason"]
    remote = check_endpoint_reachable("http://example.com:9903/mcp", probe)
    assert remote["checked"] is False and "loopback" in remote["reason"]
    # A router-port override in the env is refused too.
    with _tmp_root() as root:
        status = _status(FakeDocker(), root=root, env={"OMNIVERSE_USD_MCP_ENDPOINT": "http://127.0.0.1:9905/mcp"},
                         check_endpoint=True, probe=probe)
    assert status["endpoint_check"]["checked"] is False


def test_endpoint_unreachable_is_a_need():
    def probe(host, port, timeout):
        raise ConnectionRefusedError("refused")

    with _tmp_root() as root:
        status = _status(FakeDocker(), root=root, check_endpoint=True, probe=probe)
    assert status["endpoint_reachable"] is False
    assert any("Nothing is listening" in need for need in status["needs"])


def test_run_docker_refuses_mutating_commands():
    for args in (["pull", "x"], ["build", "."], ["run", "img"], ["stop", "c"], ["rm", "c"], ["compose", "up"],
                 ["image", "rm", "x"], ["image", "pull", "x"], ["login"], ["system", "prune"], ["container", "ls"]):
        try:
            run_docker(CLI, args, runner=_refuse)
        except ValueError as exc:
            assert "read-only" in str(exc)
        else:
            raise AssertionError(f"docker {args} should have been refused")


def test_default_runner_uses_subprocess_with_timeout():
    seen = {}
    original = subprocess.run

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return _proc(0, VERSION_UP)

    subprocess.run = fake_run
    try:
        result = run_docker(CLI, ["version", "--format", "{{json .}}"])
    finally:
        subprocess.run = original
    assert result["ok"] is True
    assert seen["argv"][:2] == [CLI, "version"]
    assert seen["kwargs"]["timeout"] <= 15 and seen["kwargs"]["check"] is False
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL


def test_wheels_missing_and_key_missing_are_needs():
    with _tmp_root(with_env_file=True) as root:
        status = _status(FakeDocker(version=(1, VERSION_DOWN, DAEMON_ERR)), root=root)
    needs = " | ".join(status["needs"])
    assert "build-wheels.bat usd" in needs
    assert "API key env var NVIDIA_API_KEY is not set" in needs
    assert status["env_file_present"] is True
    assert ".env file exists; it was not opened" in needs


# ---- secret safety ---------------------------------------------------------------------------------------------------


def test_env_presence_is_boolean_only():
    presence = env_presence({"NVIDIA_API_KEY": SENTINEL, "NGC_API_KEY": "  "})
    assert presence["NVIDIA_API_KEY"] is True and presence["NGC_API_KEY"] is False
    assert all(isinstance(v, bool) for v in presence.values())
    assert SENTINEL not in json.dumps(presence)


def test_secret_values_never_appear_in_any_output():
    env = {"NVIDIA_API_KEY": SENTINEL, "NGC_API_KEY": NGC_SENTINEL}
    # Docker output that (pathologically) echoes the keys must be scrubbed.
    leaky = FakeDocker(
        version=(1, VERSION_DOWN, f"error with key {SENTINEL}"),
    )
    leaky_up = FakeDocker(
        images=f"usd-code-mcp:latest\t2GB\n",
        ps=f"usd-code-mcp\tusd-code-mcp:latest\tUp {NGC_SENTINEL}\t127.0.0.1:9903->9903/tcp\n",
    )
    with _tmp_root(with_wheels=True, with_env_file=True) as root:
        outputs = [
            _status(leaky, root=root, env=env),
            _status(leaky_up, root=root, env=env, check_endpoint=True, probe=lambda *a: None),
            setup_plan("usd", env=env, root=root),
            setup_plan("isaac", env=env, root=root),
            explain_choice(_status(leaky_up, root=root, env=env)),
        ]
    text = json.dumps(outputs)
    assert SENTINEL not in text and NGC_SENTINEL not in text
    assert "<redacted>" in text
    assert "NVIDIA_API_KEY" in text  # names are fine


def test_status_with_real_environment_does_not_leak():
    previous = os.environ.get("NVIDIA_API_KEY")
    os.environ["NVIDIA_API_KEY"] = SENTINEL
    try:
        with _nothing_real():
            status = docker_status(runner=FakeDocker(), which=lambda n: CLI, candidates=[], probe=_no_probe)
            plan = setup_plan()
    finally:
        if previous is None:
            os.environ.pop("NVIDIA_API_KEY", None)
        else:
            os.environ["NVIDIA_API_KEY"] = previous
    assert status["env_vars_set"]["NVIDIA_API_KEY"] is True
    assert SENTINEL not in json.dumps([status, plan])


def test_scrub_handles_nesting_and_short_values():
    env = {"NVIDIA_API_KEY": SENTINEL}
    cleaned = scrub({"a": [SENTINEL, {"b": f"x{SENTINEL}y"}], SENTINEL: 1}, env)
    assert SENTINEL not in json.dumps(cleaned)
    assert scrub("abc", {"NVIDIA_API_KEY": "ab"}) == "abc"  # too short to be a key; left alone


# ---- setup_plan / explain_choice / facts ------------------------------------------------------------------------------


def test_setup_plan_usd_content():
    with _tmp_root(with_wheels=True) as root, _nothing_real():
        plan = setup_plan("usd", env={}, root=root)
    assert plan["executed"] is False
    assert plan["port"] == 9903 and plan["image"] == "usd-code-mcp:latest"
    ids = [step["id"] for step in plan["steps"]]
    assert ids == ["check", "start_docker", "api_key", "provide_key", "wheels", "build", "run", "verify",
                   "point_router", "stop", "remove"]
    assert [step["step"] for step in plan["steps"]] == list(range(1, len(ids) + 1))
    steps = {step["id"]: step for step in plan["steps"]}
    commands = " ".join(c for step in plan["steps"] for c in step["commands"])
    assert "docker build -t usd-code-mcp:latest ." in commands
    assert "docker run -d --rm --name usd-code-mcp -p 127.0.0.1:9903:9903 -e NVIDIA_API_KEY usd-code-mcp:latest" in commands
    assert "docker stop usd-code-mcp" in commands and "docker image rm usd-code-mcp:latest" in commands
    assert "OMNIVERSE_USD_MCP_ENDPOINT" in commands
    assert "GB" in steps["build"]["disk_estimate"]
    # Downloads, installs, starting and config changes all need approval; read-only checks do not.
    for key in ("start_docker", "api_key", "provide_key", "build", "run", "point_router", "stop", "remove"):
        assert steps[key]["needs_your_approval"] is True, key
    assert steps["check"]["needs_your_approval"] is False and steps["verify"]["needs_your_approval"] is False
    assert steps["wheels"]["needs_your_approval"] is False and plan["wheels_built"] is True
    assert "docker-compose.ngc.yaml" in plan["compose_alternative"]
    assert "NGC_API_KEY" in plan["not_recommended_here"]


def test_setup_plan_without_wheels_and_other_services():
    with _tmp_root() as root:
        usd = setup_plan("usd", env={}, root=root)
        isaac = setup_plan("isaacsim", env={}, root=root)
        kit = setup_plan("kit-mcp", env={}, root=root)
    wheels = next(step for step in usd["steps"] if step["id"] == "wheels")
    assert wheels["needs_your_approval"] is True and ".\\build-wheels.bat usd" in wheels["commands"]
    assert isaac["port"] == 9904 and isaac["image"] == "isaacsim-mcp:latest"
    assert kit["port"] == 9902
    assert all(port not in FORBIDDEN_PORTS for port in (s["port"] for s in SERVICES.values()))


def test_normalize_service_and_endpoint_override():
    assert normalize_service(None) == "usd"
    assert normalize_service("OmniUI") == "omni_ui"
    try:
        normalize_service("blender")
    except ValueError as exc:
        assert "usd" in str(exc)
    else:
        raise AssertionError("unknown service accepted")
    assert endpoint_for("usd", {}) == "http://127.0.0.1:9903/mcp"
    assert endpoint_for("usd", {"OMNIVERSE_USD_MCP_ENDPOINT": "http://127.0.0.1:9999/mcp"}) == "http://127.0.0.1:9999/mcp"


def test_explain_choice_is_plain_and_tailored():
    generic = explain_choice()
    for key in ("short_answer", "offline_already_gives", "docker_adds", "docker_costs", "worth_it_when",
                "not_worth_it_when", "recommendation"):
        assert generic[key], key
    assert "your_situation" not in generic
    assert "offline" in generic["recommendation"].lower()
    assert "Docker is not installed" in explain_choice({"docker_installed": False})["your_situation"]
    stopped = explain_choice({"docker_installed": True, "daemon_running": False, "needs": ["start it"]})
    assert "stopped" in stopped["your_situation"] and stopped["missing"] == ["start it"]


def test_kit_usd_agents_facts_are_names_only():
    facts = json.dumps(KIT_USD_AGENTS_DOCKER)
    assert KIT_USD_AGENTS_DOCKER["needs_api_key"] is True
    assert set(KIT_USD_AGENTS_DOCKER["api_key_env_vars"]) == {"NVIDIA_API_KEY", "NGC_API_KEY"}
    assert "nvapi-" not in facts
    assert KIT_USD_AGENTS_DOCKER["ports"]["usd"] == 9903


def test_module_source_has_no_mutating_docker_calls():
    source = Path(docker_docs.__file__).read_text(encoding="utf-8")
    code_lines = [line for line in source.splitlines() if "run_docker(" in line and "def run_docker" not in line]
    assert code_lines
    for line in code_lines:
        assert any(f'["{verb}"' in line for verb in ("version", "image", "ps", "info")), line


# ---- optional real smoke: read-only `docker version`, tolerates absence ---------------------------------------------


def test_smoke_real_docker_version_read_only():
    cli = shutil.which("docker")
    if not cli:
        return  # Docker not on PATH: nothing to smoke-test
    result = run_docker(cli, ["version", "--format", "{{json .}}"])
    assert set(result) == {"ok", "returncode", "stdout", "error"}
