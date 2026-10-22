"""OpenUSD Python practice: small exercises whose answers run on a throwaway scene and are checked automatically.

How an attempt runs (``run_attempt``):

1. A fresh temporary folder is created. The exercise's practice scene (``scene.usda``) and any setup files are
   written into it. No real scene or project path is ever passed to the learner's code.
2. The code runs in a SEPARATE Python process (``sys.executable -I``: no user site-packages, no PYTHON* variables,
   current folder not on ``sys.path``) whose working directory is that temporary folder. The environment is
   reduced to a small allow-list, so proxies, tokens and keys from the parent are not visible. Only the folder
   holding ``pxr`` is added to ``sys.path``. The process opens ``scene.usda`` as ``stage``, ``exec``s the code,
   then saves the changed layers that live in the temporary folder.
3. The process is killed if it runs longer than the timeout; its printed output is capped.
4. This process then opens the SAVED files and evaluates the exercise's declarative checks.
5. The temporary folder is deleted.

This protects the user's real scenes from accidents (a wrong path, a forgotten Save, an infinite loop). It is not
a security sandbox against deliberately hostile code: the learner's own code still runs with the user's rights.
"""

from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - depends on the local environment
    Sdf = Usd = UsdGeom = UsdShade = None

from . import sandbox

EXERCISES_PATH = Path(__file__).resolve().parent / "data" / "python_exercises.json"
TEMP_PARENT: str | None = None  # where attempt folders are created; None means the system temp dir
SOURCE = "python_practice"
SCENE_FILE = "scene.usda"
RESULT_FILE = "_practice_result.json"
DEFAULT_TIMEOUT_S = 10.0
MAX_TIMEOUT_S = 60.0
MIN_TIMEOUT_S = 1.0
MAX_CODE_CHARS = 50_000
STDOUT_CAP = 8_000
STDERR_CAP = 4_000
MAX_FILE_BYTES = 20_000_000
MAX_SCAN_FILES = 200
DEFAULT_TOL = 1e-6
SANDBOX_NOTE = (
    "Your code ran in a separate Python process on a throwaway practice scene in a temporary folder, which was "
    "deleted afterwards. Your real scenes and projects were not opened or changed."
)
_USD_SUFFIXES = (".usda", ".usd", ".usdc")
_PXR_NAMES = ("Usd", "UsdGeom", "Sdf", "Gf", "UsdShade", "UsdPhysics", "UsdLux", "Vt", "Kind", "Tf")
# Environment variables the child keeps (everything else, including proxies and tokens, is dropped).
_ENV_KEEP = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "LANG")

# Runs inside the child process. It only receives a JSON payload on stdin (code, pxr folder, variable names).
_RUNNER = r'''
import json, math, os, sys, traceback
payload = json.loads(sys.stdin.read())
sys.path.insert(0, payload["pxr_path"])
_bin = os.path.join(payload["pxr_path"], "bin")  # Kit's omni.usd.libs keeps USD's DLLs next to pxr, in bin
if os.path.isdir(_bin) and hasattr(os, "add_dll_directory"):
    os.add_dll_directory(_bin)
    os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")
result = {"started": False, "error": None, "variables": {}, "saved": [], "save_error": None, "setup_error": None}
here = os.path.normcase(os.path.realpath(os.getcwd()))
result_path = os.path.abspath(payload["result_file"])  # absolute: the learner's code may os.chdir()

def write_result():
    with open(result_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle)

def plain(value, depth=0):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return value[:2000]
    if depth > 3:
        return repr(value)[:200]
    try:
        if isinstance(value, Sdf.Path):
            return str(value)
        if hasattr(value, "GetPath") and hasattr(value, "IsValid"):
            return str(value.GetPath())
        if isinstance(value, dict):
            return {str(k): plain(v, depth + 1) for k, v in list(value.items())[:100]}
        if hasattr(value, "__len__") and hasattr(value, "__getitem__"):
            return [plain(value[i], depth + 1) for i in range(min(len(value), 100))]
        if hasattr(value, "__iter__"):
            items = []
            for item in value:
                if len(items) >= 100:
                    break
                items.append(plain(item, depth + 1))
            return items
    except Exception:
        pass
    return repr(value)[:200]

def describe(exc, syntax=False):
    info = {
        "type": type(exc).__name__,
        "bases": [cls.__name__ for cls in type(exc).__mro__],
        "message": str(exc)[:2000],
        "line": None,
    }
    if syntax:
        info["line"] = getattr(exc, "lineno", None)
        info["offset"] = getattr(exc, "offset", None)
        info["message"] = str(getattr(exc, "msg", exc))[:2000]
    else:
        frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == "<your code>"]
        if frames:
            info["line"] = frames[-1].lineno
    name = getattr(exc, "name", None)
    if isinstance(name, str):
        info["name"] = name
    if isinstance(exc, AttributeError):
        try:
            info["object_type"] = type(exc.obj).__name__
        except Exception:
            pass
    return info

try:
    from pxr import Sdf, Usd
    stage = Usd.Stage.Open(os.path.abspath(payload["scene_file"]))
    if stage is None:
        raise RuntimeError("the practice scene could not be opened")
except Exception as exc:
    result["setup_error"] = f"{type(exc).__name__}: {exc}"[:2000]
    write_result()
    sys.exit(3)

namespace = {"__name__": "__main__", "__builtins__": __builtins__, "stage": stage}
result["started"] = True
try:
    compiled = compile(payload["code"], "<your code>", "exec")
except SyntaxError as exc:
    result["error"] = describe(exc, syntax=True)
except Exception as exc:
    result["error"] = describe(exc)
else:
    try:
        exec(compiled, namespace)
    except SystemExit:
        pass
    except BaseException as exc:
        result["error"] = describe(exc)
for name in payload["report_vars"]:
    if name in namespace:
        value = namespace[name]
        result["variables"][name] = {"present": True, "type": type(value).__name__, "value": plain(value)}
    else:
        result["variables"][name] = {"present": False, "type": None, "value": None}
try:
    for layer in Sdf.Layer.GetLoadedLayers():
        real = layer.realPath
        if layer.anonymous or not layer.dirty or not real:
            continue
        if os.path.normcase(os.path.dirname(os.path.realpath(real))) == here:
            layer.Save()
            result["saved"].append(os.path.basename(real))
except Exception as exc:
    result["save_error"] = f"{type(exc).__name__}: {exc}"[:2000]
sys.stdout.flush()
write_result()
'''


# ----------------------------------------------------------------------------------------------- exercise data


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with EXERCISES_PATH.open(encoding="utf-8") as handle:
        data = json.load(handle)
    data["_by_id"] = {exercise["id"]: exercise for exercise in data["exercises"]}
    return data


def _levels() -> list[str]:
    return list(_load()["levels"])


def _exercise(exercise_id: Any) -> dict[str, Any] | None:
    if not isinstance(exercise_id, str):
        return None
    return _load()["_by_id"].get(exercise_id.strip())


def _unknown(exercise_id: Any) -> dict[str, Any]:
    ids = ", ".join(e["id"] for e in _load()["exercises"])
    return {"ok": False, "error": f"unknown exercise {exercise_id!r}; choose one of: {ids}"}


def list_exercises(level: str | None = None) -> dict[str, Any]:
    """All exercises in suggested order (optionally only one level), without starter code or solutions."""
    levels = _levels()
    if level is not None and (not isinstance(level, str) or level.strip().lower() not in levels):
        return {"ok": False, "error": f"level must be one of: {', '.join(levels)}", "levels": levels}
    wanted = level.strip().lower() if isinstance(level, str) else None
    rows = []
    for number, exercise in enumerate(_load()["exercises"], start=1):
        if wanted and exercise["level"] != wanted:
            continue
        rows.append({
            "number": number,
            "id": exercise["id"],
            "title": exercise["title"],
            "level": exercise["level"],
            "objective_ids": list(exercise["objective_ids"]),
            "goal": exercise["goal"],
        })
    return {"ok": True, "level": wanted, "levels": levels, "count": len(rows), "exercises": rows}


def get_exercise(exercise_id: str, reveal: bool = False) -> dict[str, Any]:
    """One exercise: goal, starter code, hints, what is checked and the starting scene. Solution only if reveal."""
    exercise = _exercise(exercise_id)
    if exercise is None:
        return _unknown(exercise_id)
    setup = exercise.get("setup") or {}
    result = {
        "ok": True,
        "id": exercise["id"],
        "title": exercise["title"],
        "level": exercise["level"],
        "objective_ids": list(exercise["objective_ids"]),
        "goal": exercise["goal"],
        "starter_code": exercise["starter_code"],
        "hints": list(exercise["hints"]),
        "checks": [_describe_check(check) for check in exercise["checks"]],
        "variables_checked": list(exercise.get("report_vars", [])),
        "starting_scene": setup.get("scene", "#usda 1.0\n"),
        "setup_files": copy.deepcopy(setup.get("files", {})),
        "how_it_runs": (
            "`stage` is provided for you and is a throwaway copy of the starting scene in a temporary folder "
            "(the current folder while your code runs). Changes are saved automatically and then checked."
        ),
    }
    if reveal:
        result["solution"] = exercise["solution"]
    return result


# ---------------------------------------------------------------------------------------------------- attempt


def run_attempt(exercise_id: str, code: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict[str, Any]:
    """Run learner code for an exercise in a separate Python process on a throwaway scene and check the result.

    Returns {ok, passed, checks: [{check, passed, expected, actual, hint}], stdout, stderr, error, next_hint, ...}.
    ``ok`` is False only when the attempt could not be graded (unknown exercise, bad input, USD missing).
    """
    exercise = _exercise(exercise_id)
    if exercise is None:
        return {**_unknown(exercise_id), "passed": False}
    if not isinstance(code, str):
        return _not_run(exercise, "code must be a string of Python source")
    if len(code) > MAX_CODE_CHARS:
        return _not_run(exercise, f"code is longer than {MAX_CODE_CHARS} characters")
    try:
        timeout = float(timeout_s)
    except (TypeError, ValueError):
        return _not_run(exercise, "timeout_s must be a number of seconds")
    if not math.isfinite(timeout):
        return _not_run(exercise, "timeout_s must be a finite number of seconds")
    timeout = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout))
    pxr_path = _pxr_path()
    if Usd is None or pxr_path is None:
        return _not_run(exercise, "USD Python bindings (pxr, usd-core) are not installed for this Python")

    tmp = tempfile.mkdtemp(prefix="ousd_practice_", dir=TEMP_PARENT)
    try:
        work = os.path.join(tmp, "work")
        child_tmp = os.path.join(tmp, "tmp")
        os.mkdir(work)
        os.mkdir(child_tmp)
        _write_setup(exercise, work)
        run = _run_child(code, exercise, work, child_tmp, pxr_path, timeout)
        return _grade(exercise, run, work, timeout, code)
    finally:
        _remove_tree(tmp)


def _not_run(exercise: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "passed": False,
        "exercise_id": exercise["id"],
        "title": exercise["title"],
        "error": {"type": "InputError", "message": message, "line": None, "explanation": message},
        "checks": [],
        "stdout": "",
        "stderr": "",
        "next_hint": exercise["hints"][0],
        "sandbox_note": SANDBOX_NOTE,
    }


def _pxr_path() -> str | None:
    """Folder that contains the pxr package (usd-core) used by this process, or None."""
    if Usd is None:
        return None
    import pxr

    # Kit's omni.usd.libs ships pxr as a namespace package (no __init__.py), so pxr.__file__ is None there.
    location = getattr(pxr, "__file__", None)
    if location:
        return str(Path(location).resolve().parent.parent)
    folders = [str(folder) for folder in getattr(pxr, "__path__", ()) if folder]
    return str(Path(folders[0]).resolve().parent) if folders else None


def _write_setup(exercise: dict[str, Any], work: str) -> None:
    setup = exercise.get("setup") or {}
    files = {SCENE_FILE: setup.get("scene", "#usda 1.0\n"), **(setup.get("files") or {})}
    for name, text in files.items():
        if os.path.basename(name) != name or name.startswith("."):
            raise ValueError(f"setup file name {name!r} must be a plain file name")
        with open(os.path.join(work, name), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)


def _child_env(child_tmp: str, pxr_path: str) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_KEEP if key in os.environ}
    system_root = env.get("SYSTEMROOT")
    env["PATH"] = os.pathsep.join(
        [os.path.join(system_root, "System32"), system_root] if system_root else ["/usr/bin", "/bin"]
    )
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = child_tmp
    # -I makes the child ignore PYTHON* variables, so the runner also inserts pxr_path into sys.path itself;
    # PYTHONPATH is set only so the child's environment documents where pxr comes from.
    env["PYTHONPATH"] = pxr_path
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class _Capture:
    """Reads a pipe on a thread, keeping at most ``cap`` bytes and discarding the rest."""

    def __init__(self, stream: Any, cap: int) -> None:
        self.data = bytearray()
        self.truncated = False
        self._stream = stream
        self._cap = cap
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(65536) if hasattr(self._stream, "read1") else self._stream.read(65536)
                if not chunk:
                    break
                room = self._cap - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def _run_child(
    code: str, exercise: dict[str, Any], work: str, child_tmp: str, pxr_path: str, timeout: float
) -> dict[str, Any]:
    payload = json.dumps({
        "code": code,
        "pxr_path": pxr_path,
        "scene_file": SCENE_FILE,
        "result_file": RESULT_FILE,
        "report_vars": list(exercise.get("report_vars", [])),
    }).encode("utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-I", "-u", "-X", "utf8", "-c", _RUNNER],
        cwd=work,
        env=_child_env(child_tmp, pxr_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=flags,
    )
    out = _Capture(proc.stdout, STDOUT_CAP)
    err = _Capture(proc.stderr, STDERR_CAP)
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except OSError:
        pass
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - a process that survives kill
            pass
    lingering = False
    for capture, stream in ((out, proc.stdout), (err, proc.stderr)):
        capture.thread.join(timeout=2)
        if capture.thread.is_alive():
            # A process the code started still holds the pipe open; close() would wait for its reader thread,
            # i.e. until that process exits. Leave the daemon reader to finish on its own.
            lingering = True
            continue
        try:
            stream.close()
        except OSError:
            pass
    child: dict[str, Any] | None = None
    result_path = os.path.join(work, RESULT_FILE)
    if not timed_out and os.path.isfile(result_path):
        try:
            with open(result_path, encoding="utf-8") as handle:
                child = json.load(handle)
        except (OSError, ValueError):
            child = None
        try:
            os.remove(result_path)
        except OSError:
            pass
    return {
        "timed_out": timed_out,
        "returncode": proc.returncode,
        "seconds": round(time.monotonic() - started, 2),
        "stdout": out.text(),
        "stdout_truncated": out.truncated,
        "stderr": err.text(),
        "stderr_truncated": err.truncated,
        "child": child,
        "lingering": lingering,
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the practice process and (on Windows) every process it started, so none keeps the pipes open."""
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
        try:
            subprocess.run(
                [os.path.join(system_root, "System32", "taskkill.exe"), "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _grade(exercise: dict[str, Any], run: dict[str, Any], work: str, timeout: float, code: str) -> dict[str, Any]:
    scrub = _scrubber(work)
    child = run["child"] or {}
    error: dict[str, Any] | None = None
    checks: list[dict[str, Any]] = []
    if run["timed_out"]:
        error = {
            "type": "Timeout",
            "message": f"stopped after {timeout:g} seconds",
            "line": None,
            "explanation": (
                f"Your code was still running after {timeout:g} seconds, so it was stopped. This usually means a "
                "loop that never ends, such as 'while True:' without a 'break', or a while-condition that never "
                "becomes False. Nothing was saved from this run."
            ),
        }
    elif not run["child"]:
        error = {
            "type": "ProcessEnded",
            "message": f"the practice process ended early (exit code {run['returncode']})",
            "line": None,
            "explanation": (
                "The Python process running your code stopped before it could report back. This happens when code "
                "calls os._exit(), or when USD itself crashes (for example after using an invalid object). "
                "Look at stderr below for clues."
            ),
        }
    elif child.get("setup_error"):
        return {
            **_not_run(exercise, f"the practice process could not start USD: {scrub(child['setup_error'])}"),
            "stderr": scrub(run["stderr"]),
        }
    else:
        if child.get("error"):
            error = _explain_error(child["error"], scrub)
            error["source_line"] = _source_line(code, error["line"])
        checks = _evaluate(exercise, work, child.get("variables") or {}, run["stdout"], scrub)
        if child.get("save_error"):
            checks.insert(0, {
                "check": "your changes could be saved",
                "passed": False,
                "expected": "saved",
                "actual": scrub(child["save_error"]),
                "hint": "Saving the practice layers failed; see 'actual' for the USD message.",
            })
    passed = error is None and bool(checks) and all(c["passed"] for c in checks)
    if passed:
        next_hint = None
    elif error is not None:
        next_hint = error["explanation"]
    else:
        failing = next((c for c in checks if not c["passed"]), None)
        next_hint = failing["hint"] if failing and failing.get("hint") else exercise["hints"][0]
    return {
        "ok": True,
        "passed": passed,
        "exercise_id": exercise["id"],
        "title": exercise["title"],
        "summary": _summary(passed, checks, error),
        "checks": checks,
        "stdout": scrub(run["stdout"]),
        "stdout_truncated": run["stdout_truncated"],
        "stderr": scrub(run["stderr"]),
        "stderr_truncated": run["stderr_truncated"],
        "error": error,
        "next_hint": next_hint,
        "timed_out": run["timed_out"],
        "seconds": run["seconds"],
        "saved_files": list(child.get("saved") or []),
        "sandbox_note": SANDBOX_NOTE,
        **({"background_process_note": (
            "A program your code started was still running after your code finished, so its output was not "
            "collected and it was left running. Practice code should not start other programs."
        )} if run.get("lingering") else {}),
    }


def _source_line(code: str, line: int | None) -> str | None:
    lines = code.splitlines()
    if line is None or not 1 <= line <= len(lines):
        return None
    return lines[line - 1].strip()[:200]


def _summary(passed: bool, checks: list[dict[str, Any]], error: dict[str, Any] | None) -> str:
    good = sum(1 for c in checks if c["passed"])
    if passed:
        return f"All {len(checks)} checks passed. Well done!"
    if error is not None and error["type"] == "Timeout":
        return "Your code was stopped because it ran too long."
    if error is not None:
        where = f" on line {error['line']}" if error.get("line") else ""
        tail = f" {good} of {len(checks)} checks passed." if checks else ""
        return f"Your code raised {error['type']}{where}.{tail}"
    return f"{good} of {len(checks)} checks passed."


def _scrubber(work: str):
    variants = {work, os.path.realpath(work)}
    pieces = []
    for directory in variants:
        parts = [p for p in re.split(r"[\\/]+", directory) if p]
        pieces.append(r"[\\/]+".join(re.escape(p) for p in parts))
    pattern = re.compile("|".join(sorted(pieces, key=len, reverse=True)), re.IGNORECASE)

    def scrub(text: Any) -> str:
        return pattern.sub("<practice folder>", str(text))

    return scrub


def _remove_tree(path: str) -> None:
    for attempt in range(10):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.1 * (attempt + 1))  # Windows may hold a file briefly after a killed process


# ------------------------------------------------------------------------------------------ error explanations


def _explain_error(info: dict[str, Any], scrub) -> dict[str, Any]:
    kind = str(info.get("type") or "Error")
    bases = set(info.get("bases") or [])
    message = scrub(info.get("message") or "")
    line = info.get("line") if isinstance(info.get("line"), int) else None
    where = f" (line {line})" if line else ""
    if kind in ("IndentationError", "TabError"):
        text = (
            f"Python could not read your code{where}: the indentation (spaces at the start of lines) is off. "
            "Lines inside an if/for/with/def block must be indented by the same amount (4 spaces is usual), and "
            "lines that are not inside a block must start at the left edge. Do not mix tabs and spaces."
        )
    elif kind == "SyntaxError" or "SyntaxError" in bases:
        text = (
            f"Python could not read your code{where}, so none of it ran. Common causes: a missing ':' at the end "
            "of an if/for/with/def line, a missing closing bracket or quote, or '=' where '==' was meant."
        )
    elif kind == "NameError" or "NameError" in bases:
        name = info.get("name") or _quoted(message)
        if name in _PXR_NAMES:
            text = f"Python does not know '{name}' yet: add 'from pxr import {name}' at the top of your code."
        elif name == "stage":
            text = "'stage' is provided for you; check its spelling (lower case) and do not delete it."
        else:
            text = (
                f"Python does not know the name '{name}'{where}. Either it is spelled differently where you created "
                "it (names are case-sensitive), you use it before the line that creates it, or a module import "
                "is missing."
            )
    elif kind == "ArgumentError" or (kind == "TypeError" and "did not match C++ signature" in message) or (
        "ArgumentError" in bases
    ):
        text = (
            f"A USD function received a value of the wrong type{where}. USD's C++ functions are strict: "
            "XformCommonAPI.SetTranslate wants Gf.Vec3d (doubles) while SetRotate and SetScale want Gf.Vec3f "
            "(floats); paths can be strings or Sdf.Path; and the number of arguments must match. Compare the "
            "'did not match C++ signature' list in the message with what you passed."
        )
    elif kind == "AttributeError" or "AttributeError" in bases:
        obj = info.get("object_type") or ""
        if obj == "NoneType" or "'NoneType'" in message:
            text = (
                f"You called a method on None{where}. A previous step returned nothing: for example .Get() on an "
                "attribute with no value, or a function that does not return anything. Print the value before "
                "that line to see which one is None."
            )
        elif obj == "Prim" or "'Prim' object" in message:
            text = (
                f"Usd.Prim does not have that method{where}. Generic prims only know generic things "
                "(GetAttribute, GetChildren...). Typed getters like GetSizeAttr() live on schema classes: wrap the "
                "prim first, e.g. UsdGeom.Cube(prim).GetSizeAttr()."
            )
        else:
            text = (
                f"That object has no attribute or method with this name{where}. Check the spelling and capital "
                "letters (USD methods use CamelCase, e.g. GetAttribute), and that the object is the type you think."
            )
    elif "invalid null prim" in message or "invalid prim" in message.lower() or "expired" in message.lower():
        text = (
            f"You used a prim that does not exist{where}. stage.GetPrimAtPath() returns an INVALID prim (not an "
            "error) when nothing is at that path; using it afterwards fails. Check the path spelling, including "
            "capitals and the leading '/'. You can test with 'if prim:' or prim.IsValid()."
        )
    elif kind == "TypeError" or "TypeError" in bases:
        if "NoneType" in message:
            text = (
                f"An operation got None instead of a value{where}. Something earlier returned None, often .Get() "
                "on an attribute that has no value. Print the value to find out which one."
            )
        else:
            text = (
                f"A function or operator got the wrong kind of value{where}, or the wrong number of arguments. "
                "Check the types (number vs text vs list) and the arguments you pass."
            )
    elif kind in ("ModuleNotFoundError", "ImportError"):
        text = (
            f"An import failed{where}. The practice environment has the Python standard library and USD's pxr "
            "modules (from pxr import Usd, UsdGeom, Sdf, Gf, ...). Check the module name's spelling."
        )
    elif kind == "KeyError":
        text = f"A dictionary has no entry for that key{where}. Print the dictionary's keys to see what is there."
    elif kind == "IndexError":
        text = f"You asked for a list position that does not exist{where}. Lists start at index 0."
    elif kind == "ZeroDivisionError":
        text = f"Your code divided by zero{where}."
    elif kind == "RecursionError":
        text = f"A function kept calling itself without stopping{where}."
    elif kind == "MemoryError":
        text = f"Your code tried to use too much memory{where}."
    elif kind in ("RuntimeError", "ErrorException") or "RuntimeError" in bases:
        text = (
            f"USD reported an error{where}. Read the message: it usually names the path or value USD did not "
            "accept (for example an invalid prim path or an unsupported type)."
        )
    elif kind == "ValueError":
        text = f"A value had the right type but an unacceptable content{where}. Read the message for details."
    else:
        text = f"Your code raised {kind}{where}. Read the message for details."
    return {"type": kind, "message": message, "line": line, "explanation": text}


def _quoted(message: str) -> str:
    match = re.search(r"'([^']+)'", message)
    return match.group(1) if match else ""


# ------------------------------------------------------------------------------------------------------ checks


def _describe_check(check: dict[str, Any]) -> str:
    kind = check["type"]
    path = check.get("path", "")
    expected = _show(check.get("equals"))
    if kind == "prim_exists":
        return f"a prim exists at {path}"
    if kind == "prim_absent":
        return f"there is no prim at {path}"
    if kind == "prim_type":
        return f"{path} is a prim of type {check['equals']}"
    if kind == "attribute":
        at = f" at time {_show(check['time'])}" if "time" in check else ""
        return f"{path}{at} is {expected}" + (" (authored)" if check.get("authored") else "")
    if kind == "attribute_type":
        return f"{path} has type {check['equals']}"
    if kind == "world_translation":
        return f"{path} sits at world position {expected}"
    if kind == "metadata":
        target = "the stage" if path == "/" else path
        return f"{target} has {check['key']} = {expected}"
    if kind == "custom_data":
        target = "the root layer's customLayerData" if path == "/" else f"{path} customData"
        return f"{target} has {check['key']} = {expected}"
    if kind == "sublayers":
        return f"the root layer's sublayers are {expected} (strongest first)"
    if kind == "reference":
        return f"{path} references {check['asset']}"
    if kind == "variant_set":
        return f"{path} has variant set '{check['set']}' with variants {_show(check['includes'])}"
    if kind == "variant_selection":
        return f"{path} has '{check['set']}' set to '{check['equals']}'"
    if kind == "has_api":
        return f"{path} has the {check['api']} API schema applied"
    if kind == "material_binding":
        return f"{path} is bound to the material {check['equals']}"
    if kind == "connection":
        return f"{path} is connected to {check['equals']}"
    if kind == "time_samples":
        return f"{path} has time samples at {expected}"
    if kind == "primvar":
        return f"{path} primvar {check['name']} is {expected}"
    if kind == "primvar_interpolation":
        return f"{path} primvar {check['name']} has '{check['equals']}' interpolation"
    if kind == "layer_spec":
        if "equals" in check:
            return f"{check['file']} holds {path} = {expected}"
        return f"{check['file']} {'holds' if check.get('exists', True) else 'does not hold'} an opinion for {path}"
    if kind == "layer_no_arcs":
        return f"{check['file']} has no sublayers, references or payloads"
    if kind == "file_exists":
        return f"the file {check['file']} was written"
    if kind == "variable":
        return f"your variable {check['name']} is {expected}"
    if kind == "stdout_contains":
        return f"your printed output contains {check['text']!r}"
    return kind


def _show(value: Any) -> str:
    return sandbox.format_value(value) if not isinstance(value, str) else value


def _evaluate(
    exercise: dict[str, Any], work: str, variables: dict[str, Any], stdout: str, scrub
) -> list[dict[str, Any]]:
    stage = None
    open_error = _unsafe_assets(work)
    if not open_error:
        try:
            stage = Usd.Stage.Open(os.path.join(work, SCENE_FILE), Usd.Stage.LoadAll)
        except Exception as exc:  # Tf.ErrorException carries the parser message
            open_error = f"the saved practice scene could not be opened: {scrub(exc)}"
        if stage is None and not open_error:
            open_error = "the saved practice scene could not be opened"
    context = {"stage": stage, "work": work, "variables": variables, "stdout": stdout, "scrub": scrub}
    results = []
    for check in exercise["checks"]:
        needs_stage = check["type"] not in ("variable", "stdout_contains", "file_exists", "layer_spec", "layer_no_arcs")
        if needs_stage and stage is None:
            passed, actual = False, open_error
        else:
            try:
                passed, actual = _CHECKS[check["type"]](check, context)
            except Exception as exc:  # a broken scene must not break grading
                passed, actual = False, f"could not check: {scrub(exc)}"
        results.append({
            "check": _describe_check(check),
            "passed": bool(passed),
            "expected": _expected(check),
            "actual": actual,
            "hint": None if passed else check.get("hint") or exercise["hints"][0],
        })
    del stage, context
    return results


def _expected(check: dict[str, Any]) -> Any:
    kind = check["type"]
    if kind in ("prim_exists", "file_exists"):
        return True
    if kind == "prim_absent":
        return False
    if kind == "layer_spec" and "equals" not in check:
        return bool(check.get("exists", True))
    if kind == "reference":
        return check["asset"]
    if kind == "variant_set":
        return list(check["includes"])
    if kind == "has_api":
        return check["api"]
    if kind == "layer_no_arcs":
        return []
    if kind == "stdout_contains":
        return check["text"]
    return check.get("equals")


def _unsafe_assets(work: str) -> str:
    """Refuse to compose layers whose asset paths leave the practice folder (they could open the user's files)."""
    scanned = 0
    for folder, _dirs, files in os.walk(work):
        for name in files:
            if not name.lower().endswith(_USD_SUFFIXES):
                continue
            scanned += 1
            path = os.path.join(folder, name)
            if scanned > MAX_SCAN_FILES:
                return "too many USD files were written into the practice folder"
            if os.path.getsize(path) > MAX_FILE_BYTES:
                return f"{name} is larger than {MAX_FILE_BYTES // 1_000_000} MB"
            try:
                layer = Sdf.Layer.FindOrOpen(path)
            except Exception:
                continue  # an unreadable extra file only matters if a check opens it
            if layer is None:
                continue
            for asset in sandbox._asset_paths(layer):
                if not _local_asset(asset):
                    return (
                        f"{name} points at {asset!r}, outside the practice folder. Practice scenes may only use "
                        "relative paths such as './chair.usda'."
                    )
    return ""


def _local_asset(asset: str) -> bool:
    text = str(asset).strip()
    if text.startswith("template:"):
        text = text[len("template:"):]
    if not text:
        return True
    if text.startswith(("/", "\\")) or ":" in text:
        return False
    return ".." not in re.split(r"[\\/]+", text)


def _layer(context: dict[str, Any], name: str) -> Any:
    if os.path.basename(name) != name:
        raise ValueError("check file names must be plain file names")
    path = os.path.join(context["work"], name)
    if not os.path.isfile(path):
        return None
    return Sdf.Layer.FindOrOpen(path)


def _value(value: Any) -> Any:
    return sandbox._jsonable(value)


def _matches(expected: Any, actual: Any, tol: float) -> bool:
    if isinstance(expected, bool) or expected is None:
        return actual is expected or (isinstance(actual, bool) and actual == expected and expected is not None)
    if isinstance(expected, (int, float)):
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        return abs(float(actual) - float(expected)) <= tol * max(1.0, abs(float(expected)))
    if isinstance(expected, str):
        return isinstance(actual, str) and actual == expected
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_matches(e, a, tol) for e, a in zip(expected, actual))
        )
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and set(actual) == set(expected)
            and all(_matches(expected[k], actual[k], tol) for k in expected)
        )
    return expected == actual


def _compare(check: dict[str, Any], actual: Any) -> tuple[bool, Any]:
    tol = float(check.get("tol", DEFAULT_TOL))
    return _matches(check.get("equals"), actual, tol), actual


def _prim(context: dict[str, Any], path: str) -> Any:
    prim = context["stage"].GetPrimAtPath(path)
    return prim if prim and prim.IsValid() else None


def _check_prim_exists(check, context):
    prim = _prim(context, check["path"])
    return prim is not None, prim is not None


def _check_prim_absent(check, context):
    prim = _prim(context, check["path"])
    return prim is None, prim is not None


def _check_prim_type(check, context):
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    return _compare(check, str(prim.GetTypeName()) or "(no type)")


def _attribute(context: dict[str, Any], path: str) -> Any:
    attr = context["stage"].GetAttributeAtPath(Sdf.Path(path))
    return attr if attr and attr.IsValid() else None


def _check_attribute(check, context):
    attr = _attribute(context, check["path"])
    if attr is None:
        return False, "attribute not found"
    if check.get("authored") and not attr.HasAuthoredValue():
        return False, f"not authored (fallback {_show(_value(attr.Get()))})"
    code = Usd.TimeCode(float(check["time"])) if "time" in check else Usd.TimeCode.Default()
    return _compare(check, _value(attr.Get(code)))


def _check_attribute_type(check, context):
    attr = _attribute(context, check["path"])
    if attr is None:
        return False, "attribute not found"
    return _compare(check, str(attr.GetTypeName()))


def _check_world_translation(check, context):
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    xformable = UsdGeom.Xformable(prim)
    if not xformable:
        return False, "prim is not transformable"
    matrix = xformable.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    return _compare(check, _value(matrix.ExtractTranslation()))


def _check_metadata(check, context):
    key = check["key"]
    if check["path"] == "/":
        stage = context["stage"]
        if not stage.HasAuthoredMetadata(key):
            return False, "not authored"
        return _compare(check, _value(stage.GetMetadata(key)))
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    if not prim.HasAuthoredMetadata(key):
        return False, "not authored"
    return _compare(check, _value(prim.GetMetadata(key)))


def _check_custom_data(check, context):
    if check["path"] == "/":
        data: Any = dict(context["stage"].GetRootLayer().customLayerData)
        for part in check["key"].split(":"):
            data = data.get(part) if isinstance(data, dict) else None
        return _compare(check, _value(data))
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    return _compare(check, _value(prim.GetCustomDataByKey(check["key"])))


def _check_sublayers(check, context):
    paths = [sandbox._strip_dot(str(p)) for p in context["stage"].GetRootLayer().subLayerPaths]
    return _compare(check, paths)


def _check_reference(check, context):
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    found = []
    for arc in Usd.PrimCompositionQuery(prim).GetCompositionArcs():
        if sandbox._arc_name(arc.GetArcType()) != "reference":
            continue
        layer = arc.GetTargetLayer()
        name = os.path.basename(layer.realPath or layer.identifier) if layer is not None else "(missing layer)"
        found.append(name)
        if name == check["asset"] and (
            "prim_path" not in check or str(arc.GetTargetPrimPath()) == check["prim_path"]
        ):
            return True, name
    return False, found or "no reference"


def _check_variant_set(check, context):
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    sets = prim.GetVariantSets()
    if not sets.HasVariantSet(check["set"]):
        return False, f"no variant set; sets: {list(sets.GetNames())}"
    names = list(sets.GetVariantSet(check["set"]).GetVariantNames())
    return all(n in names for n in check["includes"]), names


def _check_variant_selection(check, context):
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    return _compare(check, prim.GetVariantSets().GetVariantSelection(check["set"]) or None)


def _check_has_api(check, context):
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    applied = [str(name) for name in prim.GetAppliedSchemas()]
    return check["api"] in applied, applied


def _check_material_binding(check, context):
    prim = _prim(context, check["path"])
    if prim is None:
        return False, "no prim at this path"
    material, _rel = UsdShade.MaterialBindingAPI(prim).ComputeBoundMaterial()
    return _compare(check, str(material.GetPath()) if material else None)


def _check_connection(check, context):
    attr = _attribute(context, check["path"])
    if attr is None:
        return False, "attribute not found"
    sources = [str(p) for p in attr.GetConnections()]
    return check["equals"] in sources, sources


def _check_time_samples(check, context):
    attr = _attribute(context, check["path"])
    if attr is None:
        return False, "attribute not found"
    return _compare(check, [float(t) for t in attr.GetTimeSamples()])


def _primvar(context: dict[str, Any], check: dict[str, Any]) -> Any:
    prim = _prim(context, check["path"])
    if prim is None:
        return None
    primvar = UsdGeom.PrimvarsAPI(prim).GetPrimvar(check["name"])
    return primvar if primvar and primvar.IsDefined() else None


def _check_primvar(check, context):
    primvar = _primvar(context, check)
    if primvar is None:
        return False, "primvar not found"
    return _compare(check, _value(primvar.Get()))


def _check_primvar_interpolation(check, context):
    primvar = _primvar(context, check)
    if primvar is None:
        return False, "primvar not found"
    return _compare(check, str(primvar.GetInterpolation()))


def _check_layer_spec(check, context):
    layer = _layer(context, check["file"])
    if layer is None:
        return False, f"{check['file']} was not found"
    spec = layer.GetObjectAtPath(Sdf.Path(check["path"]))
    exists = spec is not None
    if "equals" not in check:
        return exists == bool(check.get("exists", True)), exists
    if not exists:
        return False, "no opinion in this layer"
    default = spec.default if hasattr(spec, "default") else None
    return _compare(check, _value(default))


def _check_layer_no_arcs(check, context):
    layer = _layer(context, check["file"])
    if layer is None:
        return False, f"{check['file']} was not found"
    found = [f"sublayer {p}" for p in layer.subLayerPaths]

    def visit(path: Any) -> None:
        if not path.IsPrimPath():
            return
        spec = layer.GetPrimAtPath(path)
        if spec is None:
            return
        if spec.hasReferences:
            found.append(f"reference on {path}")
        if spec.hasPayloads:
            found.append(f"payload on {path}")

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return not found, found[:20]


def _check_file_exists(check, context):
    name = check["file"]
    if os.path.basename(name) != name:
        return False, "invalid file name"
    exists = os.path.isfile(os.path.join(context["work"], name))
    return exists, exists


def _check_variable(check, context):
    info = context["variables"].get(check["name"]) or {}
    if not info.get("present"):
        return False, f"variable {check['name']} was not created"
    value = info.get("value")
    passed, actual = _compare(check, value)
    if not passed and value is None:
        return False, "None (still the starter placeholder?)"
    return passed, actual


def _check_stdout_contains(check, context):
    return check["text"] in context["stdout"], context["stdout"][-200:]


_CHECKS = {
    "prim_exists": _check_prim_exists,
    "prim_absent": _check_prim_absent,
    "prim_type": _check_prim_type,
    "attribute": _check_attribute,
    "attribute_type": _check_attribute_type,
    "world_translation": _check_world_translation,
    "metadata": _check_metadata,
    "custom_data": _check_custom_data,
    "sublayers": _check_sublayers,
    "reference": _check_reference,
    "variant_set": _check_variant_set,
    "variant_selection": _check_variant_selection,
    "has_api": _check_has_api,
    "material_binding": _check_material_binding,
    "connection": _check_connection,
    "time_samples": _check_time_samples,
    "primvar": _check_primvar,
    "primvar_interpolation": _check_primvar_interpolation,
    "layer_spec": _check_layer_spec,
    "layer_no_arcs": _check_layer_no_arcs,
    "file_exists": _check_file_exists,
    "variable": _check_variable,
    "stdout_contains": _check_stdout_contains,
}
CHECK_TYPES = tuple(_CHECKS)


# ---------------------------------------------------------------------------------------------------- progress


def _domain_name(objective_ids: list[str]) -> str:
    from . import blueprint

    for objective in objective_ids:
        number = str(objective).split(".", 1)[0]
        for domain in blueprint.load_blueprint()["domains"]:
            if str(domain["id"]) == number:
                return str(domain["name"])
    return "Python practice"


def record_progress(store: Any, exercise_id: str, passed: bool, *, seconds: float | None = None) -> dict[str, Any]:
    """Save a graded attempt to a ProgressStore (source 'python_practice') under the exercise's objectives."""
    exercise = _exercise(exercise_id)
    if exercise is None:
        raise ValueError(_unknown(exercise_id)["error"])
    if not isinstance(passed, bool):
        raise TypeError("passed must be True or False")
    return store.record_answer(
        exercise["id"],
        domain=_domain_name(exercise["objective_ids"]),
        objective_ids=exercise["objective_ids"],
        chosen="passed" if passed else "failed",
        correct="passed",
        is_correct=passed,
        seconds=seconds,
        source=SOURCE,
    )
