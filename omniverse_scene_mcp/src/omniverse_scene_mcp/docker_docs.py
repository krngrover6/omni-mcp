"""Docker USD docs helper: read-only status, a user-run setup plan and a plain-English Docker-vs-offline comparison.

The NVIDIA knowledge MCPs in ``kit-usd-agents/source/mcp`` (USD Code, Kit, OmniUI, Isaac Sim) ship Dockerfiles and two
compose files. This module only *describes* them and *looks* at Docker:

* it runs read-only Docker CLI queries (``docker version``, ``docker info``, ``docker image ls``, ``docker ps``) with
  short timeouts, through an allowlist that refuses anything else;
* it never pulls, builds, runs, stops or removes containers, installs nothing and edits no config file;
* it reports whether API-key environment variables are *set* (presence only) and never reads or returns their values,
  and it never opens ``.env`` files (only notes whether one exists);
* the only network action is an optional TCP connect to the configured docs endpoint on loopback, and only when the
  caller passes ``check_endpoint=True``; the router/bridge ports are refused.

Every function returns JSON-safe dicts. ``setup_plan`` lists the exact commands *the user* would run, each flagged when
it needs their approval.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

# --------------------------------------------------------------------------------------------------------------------
# What kit-usd-agents/source/mcp ships for containers (read from its Dockerfiles, compose files, .env.example and
# LOCAL_DEPLOYMENT.md). Names only: no key values were read or copied.
# --------------------------------------------------------------------------------------------------------------------

MCP_SUBPATH = ("kit-usd-agents", "source", "mcp")

# Environment variable *names* the containers use. Values are never read by this module beyond "is it set?".
SECRET_ENV_VARS = ("NVIDIA_API_KEY", "NGC_API_KEY")
REQUIRED_ENV_VARS = ("NVIDIA_API_KEY",)  # cloud ("ngc" compose) mode: embeddings/reranking/LLM via build.nvidia.com
OPTIONAL_ENV_VARS = (
    "NGC_API_KEY",  # only for pulling local NIM images (docker-compose.local.yaml)
    "MCP_PORT",
    "MCP_HOST",
    "KIT_EMBEDDER_BACKEND",  # nvidia_api (default) or local
    "KIT_LOCAL_EMBEDDER_URL",
    "KIT_RERANKER_BACKEND",
    "KIT_LOCAL_RERANKER_URL",
    "KIT_MCP_DISABLE_USAGE_LOGGING",
    "OMNI_UI_DISABLE_USAGE_LOGGING",
)

SERVICES: dict[str, dict[str, Any]] = {
    "usd": {
        "title": "USD Code MCP",
        "compose_service": "usd-code-mcp",
        "folder": "usd_code_mcp",
        "port": 9903,
        "build_tag": "usd-code-mcp:latest",  # build-docker.bat tag; compose uses :ngc / :local
        "image_repos": ("usd-code-mcp",),
        "container_names": ("usd-code-mcp",),
        "router_env": "OMNIVERSE_USD_MCP_ENDPOINT",
        "wheel_target": "usd",
        "wheel_prefixes": ("usd_code_aiq-", "usd_code_mcp-"),
        "tools": (
            "search_usd_code_examples",
            "search_usd_knowledge",
            "list_usd_modules",
            "list_usd_classes",
            "get_usd_module_detail",
            "get_usd_class_detail",
            "get_usd_method_detail",
        ),
    },
    "kit": {
        "title": "Kit MCP",
        "compose_service": "kit-mcp",
        "folder": "kit_mcp",
        "port": 9902,
        "build_tag": "kit-mcp:latest",
        "image_repos": ("kit-mcp",),
        "container_names": ("kit-mcp",),
        "router_env": "OMNIVERSE_KIT_MCP_ENDPOINT",
        "wheel_target": "kit",
        "wheel_prefixes": ("kit_",),
        "tools": ("search_kit_extensions", "get_kit_instructions"),
    },
    "omni_ui": {
        "title": "OmniUI MCP",
        "compose_service": "omni-ui-mcp",
        "folder": "omni_ui_mcp",
        "port": 9901,
        "build_tag": "omni-ui-mcp:latest",
        "image_repos": ("omni-ui-mcp",),
        "container_names": ("omni-ui-mcp",),
        "router_env": "OMNIVERSE_OMNI_UI_MCP_ENDPOINT",
        "wheel_target": "omni",
        "wheel_prefixes": ("omni_ui_",),
        "tools": ("search_ui_code_examples", "get_ui_instructions"),
    },
    "isaac_sim": {
        "title": "Isaac Sim MCP",
        "compose_service": "isaacsim-mcp",
        "folder": "isaacsim_mcp",
        "port": 9904,
        "build_tag": "isaacsim-mcp:latest",
        "image_repos": ("isaacsim-mcp",),
        "container_names": ("isaacsim-mcp",),
        "router_env": "OMNIVERSE_ISAAC_SIM_MCP_ENDPOINT",
        "wheel_target": "isaac",
        "wheel_prefixes": ("isaacsim_",),
        "tools": ("search_isaac_sim_extensions", "get_isaac_sim_instructions"),
    },
}

_SERVICE_ALIASES = {
    "usd": "usd", "usd_code": "usd", "usd-code": "usd", "usd_code_mcp": "usd", "usd-code-mcp": "usd",
    "kit": "kit", "kit_mcp": "kit", "kit-mcp": "kit",
    "omni_ui": "omni_ui", "omniui": "omni_ui", "omni-ui": "omni_ui", "ui": "omni_ui", "omni-ui-mcp": "omni_ui",
    "isaac_sim": "isaac_sim", "isaac": "isaac_sim", "isaacsim": "isaac_sim", "isaac-sim": "isaac_sim",
    "isaacsim-mcp": "isaac_sim",
}

KIT_USD_AGENTS_DOCKER: dict[str, Any] = {
    "source": "kit-usd-agents/source/mcp (Dockerfile per server, docker-compose.ngc.yaml, docker-compose.local.yaml, "
              ".env.example, LOCAL_DEPLOYMENT.md)",
    "prebuilt_images": "None published: every MCP image is built locally from its Dockerfile "
                       "(python:3.13-slim base + locally built wheels in <server>/dist).",
    "base_image": "python:3.13-slim (pulled from Docker Hub during the build)",
    "needs_api_key": True,
    "api_key_env_vars": {
        "NVIDIA_API_KEY": "Required in every mode. From build.nvidia.com; used for cloud embeddings, reranking and "
                          "the NIM LLM named in workflow/config.yaml.",
        "NGC_API_KEY": "Only for docker-compose.local.yaml, to pull the local embedder/reranker NIM images from nvcr.io.",
    },
    "env_file": "Compose reads source/mcp/.env (template: .env.example). This module never opens either file.",
    "compose_files": {
        "docker-compose.ngc.yaml": {
            "what": "The 4 MCP servers only; embeddings + reranking via NVIDIA's cloud API. No GPU needed.",
            "needs": ["NVIDIA_API_KEY", "internet", "wheels built into each <server>/dist"],
            "images": ["omni-ui-mcp:ngc", "kit-mcp:ngc", "usd-code-mcp:ngc", "isaacsim-mcp:ngc"],
            "network": "mcp-ngc-network",
        },
        "docker-compose.local.yaml": {
            "what": "Same 4 servers plus local embedder/reranker NIM containers on NVIDIA GPUs.",
            "needs": ["NVIDIA_API_KEY", "NGC_API_KEY", "2 NVIDIA GPUs (or 1 with enough VRAM)",
                      "NVIDIA Container Toolkit", "docker login nvcr.io"],
            "images": ["omni-ui-mcp:local", "kit-mcp:local", "usd-code-mcp:local", "isaacsim-mcp:local",
                       "nvcr.io/nim/nvidia/nv-embedqa-e5-v5:latest",
                       "nvcr.io/nim/nvidia/llama-nemotron-rerank-1b-v2:latest"],
            "network": "mcp-local-network",
        },
    },
    "ports": {
        "omni_ui": 9901, "kit": 9902, "usd": 9903, "isaac_sim": 9904,
        "embedder_nim": 8001, "reranker_nim": 8002,
    },
    "nim_images": ("nvcr.io/nim/nvidia/nv-embedqa-e5-v5:latest", "nvcr.io/nim/nvidia/llama-nemotron-rerank-1b-v2:latest"),
    "nim_container_names": ("mcp-embedder", "mcp-reranker"),
    "health_check": "POST an MCP 'initialize' JSON-RPC request to http://localhost:<port>/mcp "
                    "(Accept: application/json, text/event-stream).",
    "build_prereqs": "build-wheels.bat <usd|kit|omni|isaac> needs Poetry and git-lfs (FAISS indices are LFS files).",
    "usd_extra_corpus": "usd_code_fns data v25.11/code_rag (~234 MB of real USD code examples + a 278 MB FAISS index) "
                        "is searched semantically; the router's offline index does not cover code_rag.",
    "disk_estimate": "USD Code image roughly 2-3 GB (estimate, not measured: ~392 MB data wheel + Python deps such as "
                     "langchain, litellm, ragas, faiss-cpu), plus build cache. Docker Desktop's own WSL2 disk is extra.",
    "memory_note": "LOCAL_DEPLOYMENT.md recommends >= 8 GB Docker memory; builds can be OOM-killed with less.",
}

# Router and bridge ports: the optional endpoint check must never touch these.
FORBIDDEN_PORTS = frozenset({9905, 9909, 9910, 9911})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

# Read-only Docker CLI subcommands; anything else is refused before a process is started.
_READ_ONLY_PREFIXES: tuple[tuple[str, ...], ...] = (("version",), ("info",), ("image", "ls"), ("ps",))
_TIMEOUTS = {"version": 8.0, "info": 12.0, "image": 10.0, "ps": 10.0}
_REDACTED = "<redacted>"

Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]
Probe = Callable[[str, int, float], None]


# --------------------------------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------------------------------


def workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def mcp_dir(root: Path | None = None) -> Path:
    return (root or workspace_root()).joinpath(*MCP_SUBPATH)


def normalize_service(service: str | None) -> str:
    key = (service or "usd").strip().lower().replace(" ", "_")
    if key in _SERVICE_ALIASES:
        return _SERVICE_ALIASES[key]
    raise ValueError(f"Unknown docs service {service!r}; choose one of {', '.join(SERVICES)}")


def env_presence(env: Mapping[str, str] | None = None) -> dict[str, bool]:
    """Which known variables are set (non-empty). Booleans only: values are never copied."""
    env = os.environ if env is None else env
    return {name: bool(str(env.get(name, "") or "").strip()) for name in (*REQUIRED_ENV_VARS, *OPTIONAL_ENV_VARS)}


def _secret_values(env: Mapping[str, str] | None) -> list[str]:
    env = os.environ if env is None else env
    values = []
    for name in SECRET_ENV_VARS:
        value = str(env.get(name, "") or "").strip()
        if len(value) >= 4:
            values.append(value)
    return values


def scrub(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Defense in depth: replace any secret env var value that shows up in CLI output with <redacted>."""
    secrets = _secret_values(env)
    if not secrets:
        return value

    def _clean(item: Any) -> Any:
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, _REDACTED)
            return item
        if isinstance(item, dict):
            return {_clean(k): _clean(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [_clean(v) for v in item]
        return item

    return _clean(value)


def _short(text: str | bytes | None, limit: int = 300) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def cli_candidates() -> list[Path]:
    """Where Docker Desktop puts docker.exe (per-user and machine-wide installs)."""
    candidates = []
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidates.append(Path(local) / "Programs" / "DockerDesktop" / "resources" / "bin" / "docker.exe")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(Path(program_files) / "Docker" / "Docker" / "resources" / "bin" / "docker.exe")
    return candidates


def find_docker_cli(which: Callable[[str], str | None] | None = None,
                    candidates: Iterable[Path] | None = None) -> dict[str, Any]:
    which = shutil.which if which is None else which
    on_path = which("docker")
    if on_path:
        return {"path": str(on_path), "on_path": True, "source": "PATH"}
    for candidate in cli_candidates() if candidates is None else candidates:
        try:
            if Path(candidate).is_file():
                return {"path": str(candidate), "on_path": False, "source": "Docker Desktop install folder"}
        except OSError:
            continue
    return {"path": None, "on_path": False, "source": None}


def _is_read_only(args: Sequence[str]) -> bool:
    return any(tuple(args[: len(prefix)]) == prefix for prefix in _READ_ONLY_PREFIXES)


def _default_runner(args: Sequence[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(  # noqa: S603 - fixed argv, read-only allowlist enforced by run_docker
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def run_docker(cli: str, args: Sequence[str], runner: Runner | None = None) -> dict[str, Any]:
    """Run one allowlisted read-only docker query. Never raises for missing CLI or timeouts."""
    args = [str(a) for a in args]
    if not _is_read_only(args):
        raise ValueError(f"docker_docs only runs read-only docker queries, refused: docker {' '.join(args[:2])}")
    timeout = _TIMEOUTS.get(args[0], 8.0)
    runner = _default_runner if runner is None else runner
    try:
        proc = runner([cli, *args], timeout)
    except FileNotFoundError:
        return {"ok": False, "returncode": None, "stdout": "", "error": "docker CLI not found"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "error": f"docker {args[0]} timed out after {timeout:g}s"}
    except OSError as exc:
        return {"ok": False, "returncode": None, "stdout": "", "error": _short(f"{type(exc).__name__}: {exc}")}
    stdout = proc.stdout if isinstance(proc.stdout, str) else (proc.stdout or b"").decode("utf-8", "replace")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "error": _short(proc.stderr) if proc.returncode else "",
    }


def _parse_version(stdout: str) -> tuple[str | None, str | None]:
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        client = (data.get("Client") or {}).get("Version") if isinstance(data, dict) else None
        server = (data.get("Server") or {}).get("Version") if isinstance(data, dict) else None
        return client, server
    return None, None


def _rows(stdout: str, width: int) -> list[list[str]]:
    rows = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        rows.append((parts + [""] * width)[:width])
    return rows


def _repo(image: str) -> str:
    name = image.rsplit("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    return name.rsplit(":", 1)[0] if ":" in last else name


def endpoint_for(service: str = "usd", env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    spec = SERVICES[normalize_service(service)]
    configured = str(env.get(spec["router_env"], "") or "").strip()
    return configured or f"http://127.0.0.1:{spec['port']}/mcp"


def _default_probe(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def check_endpoint_reachable(endpoint: str, probe: Probe | None = None, timeout: float = 1.0) -> dict[str, Any]:
    """TCP connect to the docs endpoint only: loopback hosts, never the router/bridge ports."""
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return {"checked": False, "reachable": None, "reason": "endpoint has an invalid port"}
    if host not in _LOOPBACK:
        return {"checked": False, "reachable": None, "reason": f"refused: {host or '?'} is not a loopback host"}
    if port in FORBIDDEN_PORTS:
        return {"checked": False, "reachable": None,
                "reason": f"refused: port {port} belongs to the router/bridge, not a docs MCP"}
    probe = _default_probe if probe is None else probe
    try:
        probe(host, port, timeout)
    except (OSError, socket.timeout) as exc:
        return {"checked": True, "reachable": False, "host": host, "port": port, "reason": type(exc).__name__}
    return {"checked": True, "reachable": True, "host": host, "port": port}


def _wheels(spec: Mapping[str, Any], root: Path | None) -> dict[str, Any]:
    dist = mcp_dir(root) / spec["folder"] / "dist"
    try:
        names = sorted(p.name for p in dist.glob("*.whl")) if dist.is_dir() else []
    except OSError:
        names = []
    built = bool(names) and all(any(n.startswith(prefix) for n in names) for prefix in spec["wheel_prefixes"])
    return {"dist_dir": str(dist), "wheels": names, "built": built}


# --------------------------------------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------------------------------------


def docker_status(
    service: str = "usd",
    check_endpoint: bool = False,
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    candidates: Iterable[Path] | None = None,
    probe: Probe | None = None,
    env: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Read-only snapshot: is Docker here, is the daemon up, are the docs images built, is the container running.

    Runs at most ``docker version``, ``docker image ls`` and ``docker ps`` (short timeouts). Opens a TCP connection only
    when ``check_endpoint`` is True, and only to the configured loopback docs endpoint (never 9905/9909/9910/9911).
    """
    key = normalize_service(service)
    spec = SERVICES[key]
    env_map = os.environ if env is None else env
    needs: list[str] = []

    cli = find_docker_cli(which, candidates)
    result: dict[str, Any] = {
        "service": key,
        "title": spec["title"],
        "docker_installed": cli["path"] is not None,
        "docker_cli": cli,
        "client_version": None,
        "server_version": None,
        "daemon_running": False,
        "images_present": [],
        "nim_images_present": [],
        "container_running": False,
        "containers": [],
        "endpoint": endpoint_for(key, env_map),
        "endpoint_reachable": None,
        "env_vars_set": env_presence(env_map),
        "env_file_present": False,
        "wheels": _wheels(spec, root),
        "needs": needs,
        "read_only": True,
    }
    try:
        result["env_file_present"] = (mcp_dir(root) / ".env").is_file()  # existence only; never opened
    except OSError:
        pass

    if cli["path"] is None:
        needs.append("Docker Desktop is not installed (no docker CLI on PATH or in the Docker Desktop folder)")
    else:
        if not cli["on_path"]:
            needs.append(f"docker CLI is not on PATH; it is at {cli['path']}")
        version = run_docker(cli["path"], ["version", "--format", "{{json .}}"], runner)
        client, server = _parse_version(version["stdout"])
        result["client_version"], result["server_version"] = client, server
        result["daemon_running"] = bool(version["ok"] and server)
        if not result["daemon_running"]:
            detail = version["error"] or "no server version reported"
            result["daemon_error"] = detail
            needs.append("Docker engine is not running: start Docker Desktop (needs your approval)")

    if result["daemon_running"]:
        images = run_docker(cli["path"], ["image", "ls", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"], runner)
        known_repos = set(spec["image_repos"])
        nim = set(KIT_USD_AGENTS_DOCKER["nim_images"])
        for image, size in _rows(images["stdout"], 2):
            if _repo(image) in known_repos:
                result["images_present"].append({"image": image, "size": size})
            elif image in nim:
                result["nim_images_present"].append({"image": image, "size": size})
        if not images["ok"]:
            result["images_error"] = images["error"]
        ps = run_docker(cli["path"], ["ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"], runner)
        for name, image, status, ports in _rows(ps["stdout"], 4):
            if name in spec["container_names"] or _repo(image) in known_repos:
                result["containers"].append({"name": name, "image": image, "status": status, "ports": ports})
        result["container_running"] = bool(result["containers"])
        if not ps["ok"]:
            result["ps_error"] = ps["error"]
        if not result["images_present"]:
            needs.append(f"No {spec['title']} image is built yet (expected {spec['build_tag']}); see setup_plan")
        elif not result["container_running"]:
            needs.append(f"The {spec['title']} container is not running; see setup_plan step 'run'")

    if not result["wheels"]["built"] and not result["images_present"]:
        needs.append(
            f"{spec['title']} wheels are not built in {spec['folder']}/dist "
            f"(build-wheels.bat {spec['wheel_target']} needs Poetry + git-lfs)"
        )
    for name in REQUIRED_ENV_VARS:
        if not result["env_vars_set"][name]:
            hint = (" (a kit-usd-agents/source/mcp/.env file exists; it was not opened, so check yourself that it "
                    "defines it)" if result["env_file_present"] else "")
            needs.append(f"API key env var {name} is not set in this environment{hint}")

    if check_endpoint:
        reach = check_endpoint_reachable(result["endpoint"], probe)
        result["endpoint_check"] = reach
        result["endpoint_reachable"] = reach.get("reachable")
        if reach.get("checked") and not reach.get("reachable"):
            needs.append(f"Nothing is listening on {result['endpoint']}; the router keeps using offline docs")
    else:
        result["endpoint_check"] = {"checked": False, "reason": "pass check_endpoint=True to test the TCP port"}

    result["ready"] = bool(result["container_running"] and (result["endpoint_reachable"] in (True, None)))
    result["summary"] = _status_summary(result)
    return scrub(result, env_map)


def _status_summary(result: Mapping[str, Any]) -> str:
    if not result["docker_installed"]:
        return "Docker is not installed; the router's offline USD docs keep working without it."
    if not result["daemon_running"]:
        return "Docker is installed but its engine is not running; nothing Docker-based is available right now."
    if result["container_running"]:
        return f"{result['title']} container is running; the router can use {result['endpoint']}."
    if result["images_present"]:
        return f"{result['title']} image is built but not running."
    return f"Docker is running, but no {result['title']} image has been built."


def setup_plan(service: str = "usd", *, env: Mapping[str, str] | None = None, root: Path | None = None) -> dict[str, Any]:
    """Ordered steps with the exact commands the USER would run (PowerShell). Nothing here is executed."""
    key = normalize_service(service)
    spec = SERVICES[key]
    base = mcp_dir(root)
    folder = base / spec["folder"]
    wheels = _wheels(spec, root)
    port = spec["port"]
    name = spec["container_names"][0]
    tag = spec["build_tag"]
    env_file = base / ".env"

    steps: list[dict[str, Any]] = [
        {
            "id": "check",
            "title": "Check Docker is installed and its engine is running",
            "commands": ["docker version"],
            "needs_your_approval": False,
            "why": "Read-only. If the Server section is missing, start Docker Desktop first.",
        },
        {
            "id": "start_docker",
            "title": "Start Docker Desktop (only if the engine is not running)",
            "commands": ["Start Docker Desktop from the Start menu, wait until it says 'Engine running'"],
            "needs_your_approval": True,
            "why": "Starts a WSL2 virtual machine that uses several GB of RAM while it runs.",
        },
        {
            "id": "api_key",
            "title": "Get an NVIDIA API key (you do this yourself; never paste it into chat or a committed file)",
            "commands": ["Open https://build.nvidia.com/settings/api-keys, sign in, Generate API Key"],
            "needs_your_approval": True,
            "why": "Required: every search tool calls NVIDIA's cloud for embeddings/reranking (NVIDIA_API_KEY).",
        },
        {
            "id": "provide_key",
            "title": "Make the key available to Docker by NAME (pick one)",
            "commands": [
                "# Option A, this PowerShell window only (Read-Host keeps the key out of command history):",
                "$env:NVIDIA_API_KEY = Read-Host 'Paste your NVIDIA API key'",
                f"# Option B: put NVIDIA_API_KEY=... in {env_file} (gitignored; a file "
                f"{'already exists' if env_file.is_file() else 'does not exist yet'} there; copy .env.example)",
            ],
            "env_vars": list(REQUIRED_ENV_VARS),
            "needs_your_approval": True,
            "why": "The run step passes the variable by name (-e NVIDIA_API_KEY), so the value never appears in a command.",
        },
    ]
    if wheels["built"]:
        steps.append({
            "id": "wheels",
            "title": "Wheels: already built",
            "commands": [],
            "needs_your_approval": False,
            "why": f"Found {', '.join(wheels['wheels'])} in {wheels['dist_dir']}; no Poetry/git-lfs step needed.",
        })
    else:
        steps.append({
            "id": "wheels",
            "title": f"Build the {spec['title']} wheels (Dockerfile copies dist/*.whl)",
            "commands": [f'cd "{base}"', f".\\build-wheels.bat {spec['wheel_target']}"],
            "needs_your_approval": True,
            "why": "Needs Poetry and git-lfs installed; downloads Python packages and LFS data.",
        })
    steps += [
        {
            "id": "build",
            "title": f"Build the {spec['title']} image",
            "commands": [f'cd "{folder}"', f"docker build -t {tag} ."],
            "needs_your_approval": True,
            "why": "Downloads python:3.13-slim and Python dependencies; takes a while. "
                   + KIT_USD_AGENTS_DOCKER["disk_estimate"],
            "disk_estimate": KIT_USD_AGENTS_DOCKER["disk_estimate"],
        },
        {
            "id": "run",
            "title": f"Run it on 127.0.0.1:{port} (loopback only)",
            "commands": [
                f"docker run -d --rm --name {name} -p 127.0.0.1:{port}:{port} -e NVIDIA_API_KEY {tag}",
                f'# or, with the .env file: docker run -d --rm --name {name} -p 127.0.0.1:{port}:{port} '
                f'--env-file "{env_file}" {tag}',
            ],
            "env_vars": list(REQUIRED_ENV_VARS),
            "ports": {"host": port, "container": port},
            "needs_your_approval": True,
            "why": "Starts a background container; queries you send it go to NVIDIA's cloud for embeddings.",
        },
        {
            "id": "verify",
            "title": "Verify it started (read-only)",
            "commands": [
                f'docker ps --filter "name={name}"',
                f'docker logs {name} 2>&1 | Select-String "mcp-startup"',
                f"# from the router package: docker_docs.docker_status('{key}', check_endpoint=True)",
            ],
            "needs_your_approval": False,
            "why": "The [mcp-startup] banner shows which embedder backend it picked.",
        },
        {
            "id": "point_router",
            "title": "Point the router at it",
            "commands": [
                f"# Nothing to change for port {port}: the router's {key} knowledge already defaults to "
                f"http://127.0.0.1:{port}/mcp and falls back to offline docs when it is down.",
                f"# Different port? set it before starting the router: $env:{spec['router_env']} = "
                f"'http://127.0.0.1:<port>/mcp'",
                f'# Optional direct client entry (.mcp.json): "{spec["compose_service"]}": '
                f'{{"type": "http", "url": "http://127.0.0.1:{port}/mcp"}}',
            ],
            "env_vars": [spec["router_env"]],
            "needs_your_approval": True,
            "why": "Restarting the router or editing .mcp.json is a config change.",
        },
        {
            "id": "stop",
            "title": "Stop it (the --rm flag also removes the container)",
            "commands": [f"docker stop {name}"],
            "needs_your_approval": True,
            "why": "Frees the RAM; the router drops back to offline docs automatically.",
        },
        {
            "id": "remove",
            "title": "Remove the image to free disk space",
            "commands": [f"docker image rm {tag}", "docker builder prune"],
            "needs_your_approval": True,
            "why": "Deletes the built image and build cache (you can rebuild later).",
        },
    ]
    for index, step in enumerate(steps, 1):
        step["step"] = index
    return {
        "service": key,
        "title": spec["title"],
        "mode": "NVIDIA cloud API (docker-compose.ngc.yaml equivalent, single container, no GPU)",
        "executed": False,
        "note": "Nothing in this plan has been run. Each step marked needs_your_approval is for you to run yourself.",
        "port": port,
        "image": tag,
        "container_name": name,
        "required_env_vars": list(REQUIRED_ENV_VARS),
        "wheels_built": wheels["built"],
        "disk_estimate": KIT_USD_AGENTS_DOCKER["disk_estimate"],
        "compose_alternative": (
            f'cd "{base}"; docker compose -f docker-compose.ngc.yaml up --build {spec["compose_service"]}  '
            "(builds every service's image in parallel unless named; all wheels must exist)"
        ),
        "not_recommended_here": "docker-compose.local.yaml needs NGC_API_KEY, NVIDIA GPUs for the NIM containers and "
                                "the NVIDIA Container Toolkit; too heavy for a laptop study setup.",
        "steps": steps,
    }


def explain_choice(status: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Plain-English pros/cons of the Docker docs MCP versus the router's existing offline knowledge."""
    answer: dict[str, Any] = {
        "short_answer": (
            "You do not need Docker to study or to get USD answers: the router already answers usd_knowledge "
            "offline. The Docker USD Code MCP adds meaning-based (embedding) search, mainly over a large corpus of "
            "real USD code examples, at the cost of an NVIDIA API key, internet, a few GB of disk and RAM."
        ),
        "offline_already_gives": [
            "BM25 keyword search over the USD 25.11 API atlas (every class/method signature), the USD docs "
            "knowledge chunks, Learn OpenUSD pages + glossary and your OPENUSD_*_ONE_BY_ONE notes",
            "Installed Isaac Sim / Kit / omni.ui extension docs (offline_docs_knowledge)",
            "No key, no internet, instant start, nothing leaves the laptop, nothing to keep running",
            "Exact API names and signatures (list/get module, class, method detail) answered locally",
        ],
        "docker_adds": [
            "Semantic search: finds passages that mean the same thing even when the words differ",
            "search_usd_code_examples over the code_rag corpus (~234 MB of USD code samples) that the offline "
            "index does not cover",
            "NVIDIA's reranker on top of the hits, so the first few results are usually better",
            "Zero router changes: usd_knowledge already targets 127.0.0.1:9903 and falls back offline when it is down",
        ],
        "docker_costs": [
            "Needs NVIDIA_API_KEY (build.nvidia.com account); every search sends your query text to NVIDIA's cloud",
            "Needs internet; API rate limits apply",
            "Image ~2-3 GB (estimate) plus Docker Desktop's WSL2 VM, which uses several GB of RAM while running",
            "One more thing to start, update and troubleshoot (build can fail if Docker has < 8 GB memory)",
        ],
        "worth_it_when": [
            "You write a lot of USD Python and want real code samples for vague, wording-different questions",
            "You already keep Docker Desktop running for other work and are fine with cloud queries",
        ],
        "not_worth_it_when": [
            "Studying for NCP-OUSD: concepts, composition and API names are covered offline",
            "You want everything to work offline or on battery, or RAM is tight while Isaac Sim / Composer runs",
            "You do not want an NVIDIA API key or to send queries to a cloud service",
        ],
        "recommendation": (
            "Stay with offline knowledge by default. Try Docker later only if offline code-example answers feel thin; "
            "the router will pick it up automatically on port 9903 with no config change."
        ),
    }
    if status:
        if not status.get("docker_installed"):
            situation = "Docker is not installed, so offline knowledge is the only option today (and it is enough)."
        elif not status.get("daemon_running"):
            situation = "Docker Desktop is installed but its engine is stopped; nothing Docker-based is running."
        elif status.get("container_running"):
            situation = "The docs container is already running, so you are getting the Docker benefits now."
        elif status.get("images_present"):
            situation = "The image is built; starting it is one docker run away (see setup_plan)."
        else:
            situation = "Docker is running but the docs image is not built; see setup_plan for the steps."
        answer["your_situation"] = situation
        answer["missing"] = list(status.get("needs", []))
    return answer


__all__ = [
    "FORBIDDEN_PORTS",
    "KIT_USD_AGENTS_DOCKER",
    "OPTIONAL_ENV_VARS",
    "REQUIRED_ENV_VARS",
    "SECRET_ENV_VARS",
    "SERVICES",
    "check_endpoint_reachable",
    "docker_status",
    "endpoint_for",
    "env_presence",
    "explain_choice",
    "find_docker_cli",
    "normalize_service",
    "run_docker",
    "scrub",
    "setup_plan",
]
