"""Stdlib test runner for omniverse_scene_mcp: runs every fixture-free ``test_*`` function, no pytest needed.

Works from a fresh checkout without installing this package (src/ and tests/ go on sys.path) and from any folder:

    python omniverse_scene_mcp/tools/run_tests.py                      # every tests/test_*.py
    python omniverse_scene_mcp/tools/run_tests.py test_router service  # only these modules ("service" = test_service)
    python omniverse_scene_mcp/tools/run_tests.py test_router::test_router_sends_scene_commands_to_scene_service
    python omniverse_scene_mcp/tools/run_tests.py -k dry_run -q        # name filter, print only failures/skips

It loads tests/conftest.py first, exactly as pytest does, so both runs share one setup: OMNIVERSE_SCENE_MCP_TEST=1
(fake scene, offline knowledge, no probe of 127.0.0.1:9910), the network guard, and the skips for tests that need
local-only workspace files. Exit code: 0 all passed, 1 a test failed or would not import, 5 nothing ran (like pytest).

Interpreters without httpx/mcp/pxr (the workspace's plain Python 3.12) borrow them from
``<workspace>/bowerbot-venv`` when that venv exists and matches this Python version; set
OMNIVERSE_SCENE_MCP_EXTRA_SITE (os.pathsep-separated folders) to use other folders, or pass --no-extra-site.
Those folders are appended, so packages installed in the running interpreter always win.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import os
import sys
import time
import traceback
import unittest
from pathlib import Path
from typing import Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
TESTS_DIR = PROJECT_ROOT / "tests"
WORKSPACE_ROOT = PROJECT_ROOT.parent
EXTRA_SITE_ENV = "OMNIVERSE_SCENE_MCP_EXTRA_SITE"
EXIT_OK, EXIT_FAILED, EXIT_NOTHING_RAN = 0, 1, 5


def _venv_site_packages(venv: Path) -> list[Path]:
    """site-packages (plus pywin32's folders) of a venv made with this interpreter's major.minor, else []."""
    try:
        config = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    version = ""
    for line in config.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            version = value.strip()
            break
    if version.split(".")[:2] != [str(sys.version_info[0]), str(sys.version_info[1])]:
        return []  # compiled wheels (pxr, pydantic_core) only load in the Python they were built for
    candidates = [venv / "Lib" / "site-packages"]
    candidates += [venv / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages"]
    for site in candidates:
        if site.is_dir():
            # Same extra folders start-isaac-mcp.ps1 gives the router for pywin32.
            pywin32 = (site / "win32", site / "win32" / "lib", site / "pywin32_system32")
            return [site] + [folder for folder in pywin32 if folder.is_dir()]
    return []


def extra_site_dirs(disabled: bool = False) -> list[Path]:
    """Folders appended to sys.path: $OMNIVERSE_SCENE_MCP_EXTRA_SITE, else the workspace bowerbot-venv when usable."""
    if disabled:
        return []
    configured = os.environ.get(EXTRA_SITE_ENV, "").strip()
    if configured:
        return [Path(part) for part in configured.split(os.pathsep) if part.strip()]
    return _venv_site_packages(WORKSPACE_ROOT / "bowerbot-venv")


def prepare_environment(extra_sites: Iterable[Path] = ()) -> object:
    """Set the test switch and sys.path, append extra site folders, then load tests/conftest.py (the shared setup)."""
    os.environ["OMNIVERSE_SCENE_MCP_TEST"] = "1"
    for path in (SRC_DIR, TESTS_DIR):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    for path in extra_sites:
        if str(path) not in sys.path:
            sys.path.append(str(path))
    return importlib.import_module("conftest")


def discover_modules() -> list[str]:
    """Every tests/test_*.py module name, sorted."""
    return sorted(path.stem for path in TESTS_DIR.glob("test_*.py"))


def parse_selection(selectors: Iterable[str], available: list[str]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """'test_x', 'x', 'test_x.py', 'tests/test_x.py' or 'test_x::test_y' -> (module, test or None); plus unknowns."""
    chosen: list[tuple[str, str | None]] = []
    unknown: list[str] = []
    for raw in selectors:
        module_part, _, test_name = raw.partition("::")
        stem = Path(module_part.replace("\\", "/")).name
        stem = stem[:-3] if stem.endswith(".py") else stem
        if stem not in available and f"test_{stem}" in available:
            stem = f"test_{stem}"
        if stem not in available:
            unknown.append(raw)
            continue
        chosen.append((stem, test_name or None))
    return chosen, unknown


def _skip_reason(exc: BaseException) -> str | None:
    """Reason text when ``exc`` is a skip (unittest.SkipTest or pytest.skip), else None."""
    if isinstance(exc, unittest.SkipTest):
        return str(exc) or "skipped"
    if type(exc).__name__ == "Skipped" and type(exc).__module__.startswith(("_pytest", "pytest")):
        return str(getattr(exc, "msg", "") or exc) or "skipped"
    return None


def _module_tests(module: object) -> list[tuple[str, Callable[..., object]]]:
    """test_* functions defined in ``module`` itself (not imported into it), sorted by name like the old runner."""
    return [
        (name, func)
        for name, func in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_") and func.__module__ == getattr(module, "__name__", None)
    ]


class Results:
    """Counts and printing for one run."""

    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet
        self.passed = self.failed = self.skipped = 0

    def ok(self, label: str) -> None:
        self.passed += 1
        if not self.quiet:
            print(f"ok   {label}")

    def skip(self, label: str, reason: str) -> None:
        self.skipped += 1
        print(f"SKIP {label} ({reason})")

    def fail(self, label: str, kind: str = "FAIL") -> None:
        self.failed += 1
        print(f"{kind} {label}")
        traceback.print_exc()


def run_module(name: str, only: set[str] | None, keyword: str | None, results: Results, conftest: object) -> None:
    """Import one test module and run its selected fixture-free tests."""
    shared_skip = getattr(conftest, "skip_reason", None)
    try:
        module = importlib.import_module(name)
    except KeyboardInterrupt:
        raise
    except BaseException as exc:  # pytest.skip(allow_module_level=True) is a BaseException
        reason = _skip_reason(exc)
        if reason is not None:
            results.skip(name, reason)
        else:
            results.fail(name, "IMPORT FAIL")
        return
    tests = _module_tests(module)
    if only is not None:
        missing = sorted(only - {test_name for test_name, _ in tests})
        for test_name in missing:
            results.failed += 1
            print(f"NOT FOUND {name}::{test_name}")
        tests = [(test_name, func) for test_name, func in tests if test_name in only]
    for test_name, func in tests:
        if keyword and keyword.lower() not in f"{name}::{test_name}".lower():
            continue
        label = f"{name}.{test_name}"
        if inspect.signature(func).parameters:
            results.skip(label, "needs pytest fixtures")
            continue
        reason = shared_skip(name, test_name) if callable(shared_skip) else None
        if reason:
            results.skip(label, reason)
            continue
        try:
            outcome = func()
            if inspect.iscoroutine(outcome):
                outcome.close()
                raise TypeError("async test functions are not supported; wrap the body in asyncio.run()")
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            reason = _skip_reason(exc)
            if reason is not None:
                results.skip(label, reason)
            else:
                results.fail(label)
        else:
            results.ok(label)


def main(argv: list[str] | None = None) -> int:
    """Run the selected tests and return the process exit code."""
    parser = argparse.ArgumentParser(description="Run omniverse_scene_mcp tests without pytest.")
    parser.add_argument("modules", nargs="*", help="test modules, optionally module::test (default: all)")
    parser.add_argument("-k", dest="keyword", help="only tests whose 'module::name' contains this text")
    parser.add_argument("-q", "--quiet", action="store_true", help="print only failures, skips and the summary")
    parser.add_argument("--no-extra-site", action="store_true", help="do not borrow packages from bowerbot-venv")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:  # a cp1252 console or pipe must not turn a printed non-ASCII character into a test failure
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass

    extra = extra_site_dirs(disabled=args.no_extra_site)
    conftest = prepare_environment(extra)
    guard = "on" if getattr(conftest, "NETWORK_GUARD_ACTIVE", False) else "off"
    print(f"python {sys.version.split()[0]} on {sys.platform}; OMNIVERSE_SCENE_MCP_TEST=1; network guard {guard}")
    if extra:
        print("extra site-packages (appended): " + os.pathsep.join(str(path) for path in extra))

    available = discover_modules()
    if args.modules:
        chosen, unknown = parse_selection(args.modules, available)
    else:
        chosen, unknown = [(name, None) for name in available], []
    selection: dict[str, set[str] | None] = {}
    for module_name, test_name in chosen:
        if test_name is None or selection.get(module_name, set()) is None:
            selection[module_name] = None
        else:
            selection.setdefault(module_name, set()).add(test_name)  # type: ignore[union-attr]

    results = Results(args.quiet)
    for raw in unknown:
        results.failed += 1
        print(f"NOT FOUND {raw} (no tests/test_*.py module by that name)")
    started = time.perf_counter()
    for module_name, only in selection.items():
        run_module(module_name, only, args.keyword, results, conftest)

    blocked = list(getattr(conftest, "BLOCKED_ATTEMPTS", []))
    if blocked:
        print(f"\nnetwork guard refused {len(blocked)} connection attempt(s): {blocked[:5]}")
    elapsed = time.perf_counter() - started
    print(f"\n{results.passed} passed, {results.failed} failed, {results.skipped} skipped in {elapsed:.1f}s")
    if results.failed:
        return EXIT_FAILED
    return EXIT_OK if results.passed else EXIT_NOTHING_RAN


if __name__ == "__main__":
    sys.exit(main())
