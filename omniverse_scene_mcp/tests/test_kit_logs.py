"""Kit log watcher: synthetic logs in Kit's line format (invented content), polling cursors, rotation, redaction."""

import os
from pathlib import Path
import random
import tempfile
import time

from omniverse_scene_mcp import kit_logs
from omniverse_scene_mcp.kit_logs import (
    KitLogError, NOISE_PATTERNS, explain_recent, find_logs, log_dirs, new_errors, redact, summary, tail,
)

ISAAC = ("Isaac-Sim Full", "6.1")
COMPOSER = ("My USD Composer", "0.1")
HOME = os.path.expanduser("~")


def line(level="Info", source="demo.ext", message="hello", second=0, ms=None, stream=None):
    minute, sec = divmod(second, 60)
    elapsed = f"[{ms if ms is not None else second * 1000:,}ms] "
    prefix = f"2026-03-04T10:{minute % 60:02d}:{sec:02d}Z {elapsed}[{level}] [{source}] "
    if stream:
        prefix += f"[py {stream}]: "
    return prefix + message


def py_traceback(second, source="omni.kit.app._impl", exc="AttributeError: 'NoneType' object has no attribute "
                 "'GetAttribute'"):
    rows = [
        "Traceback (most recent call last):",
        '  File "c:/demo/scripts/spin_cube.py", line 12, in on_update',
        "    attr = prim.GetAttribute('size')",
        "           ^^^^^^^^^^^^^^^^^",
        exc,
    ]
    return [line("Error", source, row, second=second, stream="stderr") for row in rows]


def write_log(root, app=ISAAC, name="kit_20260304_100000.log", lines=(), mtime=None, data=None):
    folder = Path(root) / app[0] / app[1]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    if data is None:
        data = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def append(path, lines, newline=True):
    with open(path, "ab") as handle:
        handle.write(("\n".join(lines) + ("\n" if newline else "")).encode("utf-8"))


def header_lines():
    return [
        line("Info", "carb", "Logging to file: demo.log", second=0, ms=0).replace("[0ms] ", ""),
        line("Info", "carb.crashreporter-breakpad.plugin", "crashreporter configured:", second=0, ms=1),
        "    buildVersion: '1.2.3'",
        "    buildBranch: 'demo'",
    ]


# --- discovery -------------------------------------------------------------------------------------------------------

def test_find_logs_classifies_apps_sorts_newest_first_and_marks_active():
    with tempfile.TemporaryDirectory() as root:
        now = time.time()
        old_isaac = write_log(root, ISAAC, "kit_20260301_090000.log", header_lines(), mtime=now - 86400)
        new_isaac = write_log(root, ISAAC, "kit_20260304_100000.log", header_lines(), mtime=now - 30)
        write_log(root, COMPOSER, "kit_20260302_090000.log", header_lines(), mtime=now - 3600)
        write_log(root, ("Isaac-Sim_Compatibility_Checker", "6.1"), "kit_20260303.log", header_lines(),
                  mtime=now - 7200)
        (Path(root) / ISAAC[0] / ISAAC[1] / "notes.txt").write_text("not a log")

        logs = find_logs(dirs=[root])
        assert [log["name"] for log in logs][:2] == [new_isaac.name, "kit_20260302_090000.log"]
        assert {log["app"] for log in logs} == {"isaac", "composer", "Isaac-Sim_Compatibility_Checker"}
        first = logs[0]
        assert set(first) == {"app", "app_name", "version", "name", "path", "size", "modified", "age_seconds",
                              "active"}
        assert first["active"] is True and first["version"] == "6.1" and first["app_name"] == "Isaac-Sim Full"
        assert first["modified"].endswith("Z") and first["size"] > 0
        assert not any(log["active"] for log in logs[1:])
        isaac = find_logs("isaac", dirs=[root])
        assert [log["name"] for log in isaac] == [new_isaac.name, old_isaac.name]
        assert [log["app"] for log in find_logs("composer", dirs=[root])] == ["composer"]
        assert [log["app"] for log in find_logs("My USD Composer", dirs=[root])] == ["composer"]
        assert find_logs("blender", dirs=[root]) == []
        assert find_logs(dirs=[root], active_minutes=24 * 60 * 2)[-1]["active"] is True


def test_paths_are_returned_with_home_redacted_and_can_be_passed_back():
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=header_lines() + [line("Error", "demo", "boom", second=5)])
        log = find_logs(dirs=[root])[0]
        if os.path.realpath(root).lower().startswith(os.path.realpath(HOME).lower()):
            assert log["path"].startswith("~/") and os.path.basename(HOME) not in log["path"]
        entries = tail(log["path"], dirs=[root])
        assert entries[-1]["message"] == "boom"


def test_env_override_lists_several_dirs():
    with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
        write_log(one, ISAAC, lines=header_lines())
        write_log(two, COMPOSER, lines=header_lines())
        saved = os.environ.get(kit_logs.ENV_VAR)
        os.environ[kit_logs.ENV_VAR] = one + os.pathsep + two
        try:
            assert {log["app"] for log in find_logs()} == {"isaac", "composer"}
            dirs = log_dirs()
            assert len(dirs) == 2 and all(d["exists"] and d["source"] == "env" for d in dirs)
        finally:
            if saved is None:
                os.environ.pop(kit_logs.ENV_VAR, None)
            else:
                os.environ[kit_logs.ENV_VAR] = saved


def test_missing_logs_and_outside_paths_raise_beginner_errors():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
        try:
            tail("isaac", dirs=[root])
        except KitLogError as exc:
            assert isinstance(exc, ValueError)
            assert "Isaac Sim" in str(exc) and kit_logs.ENV_VAR in str(exc)
        else:
            raise AssertionError("expected KitLogError")
        stray = Path(other) / "kit_x.log"
        stray.write_text(line("Error", "x", "secret stuff") + "\n")
        for target in (str(stray), os.path.join(root, "..", os.path.basename(other), "kit_x.log")):
            try:
                tail(target, dirs=[root])
            except KitLogError as exc:
                assert "not inside" in str(exc)
            else:
                raise AssertionError("a file outside the log folders was read")
        not_log = write_log(root, ISAAC, "kit.log", header_lines()).with_name("user.config.json")
        not_log.write_text("{}")
        try:
            tail(str(not_log), dirs=[root])
        except KitLogError as exc:
            assert ".log" in str(exc)
        else:
            raise AssertionError("a non-.log file was read")
        for bad in ({"level": "loud"}, {"since": "yesterday-ish"}):
            try:
                tail("isaac", dirs=[root], **bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"bad argument accepted: {bad}")


# --- parsing ---------------------------------------------------------------------------------------------------------

def test_tail_parses_kit_lines_continuations_and_extension_tags():
    rows = header_lines() + [
        line("Warning", "omni.ext.plugin", "[ext: omni.demo.widget-1.4.2+abc.wx64] something odd happened",
             second=2),
        line("Info", "omni.replicator.core.scripts.extension", "ready", second=3),
        line("Error", "omni.graph.core._impl._registration.register_python_ogn", "node failed", second=4),
        "  detail line one",
        "  detail line two",
    ]
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        entries = tail("isaac", dirs=[root])
        assert [e["level"] for e in entries] == ["info", "info", "warning", "info", "error"]
        crash = entries[1]
        assert crash["message"] == "crashreporter configured:\n    buildVersion: '1.2.3'\n    buildBranch: 'demo'"
        assert crash["raw_line_no"] == 2 and entries[0]["elapsed"] is None and crash["elapsed"] == "1ms"
        warning = entries[2]
        assert warning["extension"] == "omni.demo.widget" and warning["source"] == "omni.ext.plugin"
        assert warning["message"] == "something odd happened" and warning["raw_line_no"] == 5
        assert entries[3]["extension"] == "omni.replicator.core"
        assert entries[4]["extension"] == "omni.graph.core"
        assert entries[4]["message"] == "node failed\n  detail line one\n  detail line two"
        assert entries[4]["time"] == "2026-03-04T10:00:04Z" and entries[4]["noise"] is None
        assert set(entries[0]) == {"time", "elapsed", "level", "source", "extension", "stream", "message",
                                   "raw_line_no", "offset", "noise"}


def test_py_stderr_traceback_lines_are_glued_into_one_entry_including_chained_exceptions():
    rows = header_lines() + py_traceback(10) + [
        line("Error", "omni.kit.app._impl", "", second=10, stream="stderr"),
        line("Error", "omni.kit.app._impl", "During handling of the above exception, another exception occurred:",
             second=10, stream="stderr"),
        line("Error", "omni.kit.app._impl", "", second=10, stream="stderr"),
    ] + py_traceback(10, exc="RuntimeError: second failure") + [
        line("Info", "demo", "after", second=11),
    ] + py_traceback(12, exc="KeyError: 'size'")
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        errors = tail("isaac", level="error", dirs=[root])
        assert len(errors) == 2
        first = errors[0]
        assert first["stream"] == "stderr" and first["message"].startswith("Traceback (most recent call last):")
        assert "NoneType" in first["message"] and first["message"].rstrip().endswith("RuntimeError: second failure")
        assert first["message"].count("Traceback") == 2 and "[py stderr]" not in first["message"]
        assert errors[1]["message"].endswith("KeyError: 'size'")
        assert len(tail("isaac", dirs=[root])) == 2 + 2 + 1


def test_unprefixed_traceback_lines_continue_the_error_entry():
    rows = header_lines() + [
        line("Error", "omni.kit.app._impl", "Traceback (most recent call last):", second=3, stream="stderr"),
        '  File "c:/demo/run.py", line 4, in main',
        "    result = parent.submit(payload)",
        "ValueError: bad payload",
    ]
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        entry = tail("isaac", level="error", dirs=[root])[-1]
        assert entry["message"].endswith("ValueError: bad payload") and entry["message"].count("\n") == 3


def test_level_and_since_filters():
    rows = header_lines() + [line("Info", "a", "i", second=10), line("Warning", "b", "w", second=20),
                             line("Error", "c", "e", second=30), line("Fatal", "d", "f", second=40),
                             line("Verbose", "e", "v", second=50)]
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        assert [e["message"] for e in tail("isaac", level="error", dirs=[root])] == ["e", "f"]
        assert [e["message"] for e in tail("isaac", level="warning", dirs=[root])] == ["w", "e", "f"]
        assert [e["message"] for e in tail("isaac", level="WARN", dirs=[root])] == ["w", "e", "f"]
        assert [e["message"] for e in tail("isaac", since="2026-03-04T10:00:30Z", dirs=[root])] == ["e", "f", "v"]
        from datetime import datetime, timezone
        since = datetime(2026, 3, 4, 10, 0, 40, tzinfo=timezone.utc)
        assert [e["message"] for e in tail("isaac", since=since, dirs=[root])] == ["f", "v"]
        assert tail("isaac", since=60, dirs=[root]) == []  # those invented times are long ago
        assert [e["message"] for e in tail("isaac", lines=2, dirs=[root])] == ["f", "v"]


def test_tail_window_grows_to_find_enough_filtered_entries():
    rows = header_lines() + [line("Error", "demo", "early error", second=1)]
    rows += [line("Info", "demo", "x" * 200, second=2) for _ in range(2000)]  # ~0.5 MB of info after it
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        errors = tail("isaac", lines=1, level="error", dirs=[root])
        assert [e["message"] for e in errors] == ["early error"] and errors[0]["raw_line_no"] == 5
        assert len(tail("isaac", lines=300, dirs=[root])) == 300


def test_huge_file_only_its_end_is_read():
    filler = (line("Info", "demo", "y" * 300) + "\n").encode("utf-8")
    with tempfile.TemporaryDirectory() as root:
        path = write_log(root, ISAAC, data=filler * (40_000))  # ~13 MB
        append(path, [line("Error", "demo", "the last error", second=59)])
        read_sizes = []
        real_open = open

        class Counting:
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                data = self._handle.read(size)
                read_sizes.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self._handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()

        kit_logs.open = lambda *a, **k: Counting(real_open(*a, **k))
        try:
            started = time.time()
            last = tail("isaac", lines=5, dirs=[root])
            stats = summary("isaac", dirs=[root])
            elapsed = time.time() - started
        finally:
            del kit_logs.open
        assert last[-1]["message"] == "the last error" and last[-1].get("raw_line_no_relative") is True
        assert stats["truncated"] is True and stats["session_start"] is None and stats["errors"] == 1
        assert stats["entries"] < 40_000
        assert max(read_sizes) <= kit_logs.MAX_READ_BYTES and sum(read_sizes) < 2 * kit_logs.MAX_READ_BYTES
        assert elapsed < 10


def test_binary_garbage_and_odd_encodings_do_not_break_parsing():
    rng = random.Random(7)
    garbage = bytes(rng.randrange(256) for _ in range(50_000))
    data = b"\xef\xbb\xbf" + "\n".join(header_lines()).encode() + b"\n" + garbage + b"\n"
    data += line("Error", "demo", "caf\u00e9 failed").encode("cp1252") + b"\r\n"
    data += line("Warning", "demo", "nul\x00byte \x1b[31mred\x1b[0m").encode() + b"\n"
    data += b"\xff\xfe\x00\x01" * 20_000  # trailing garbage without a newline
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, data=data)
        entries = tail("isaac", lines=1000, dirs=[root])
        assert entries[0]["message"] == "Logging to file: demo.log"
        messages = [e["message"] for e in entries]
        assert "caf\u00e9 failed" in messages[-2] or "caf\u00e9 failed" in "".join(messages)
        assert all("\x00" not in m and "\x1b" not in m for m in messages)
        warning = [e for e in entries if e["level"] == "warning"][-1]
        assert warning["message"].startswith("nulbyte red")
        assert all(len(m) <= kit_logs.MAX_MESSAGE_CHARS for m in messages)
        assert summary("isaac", dirs=[root])["errors"] == 1
        explained = explain_recent("isaac", dirs=[root])
        assert explained["ok"] and explained["distinct"] == 1
        cursor = new_errors("isaac", dirs=[root])["cursor"]
        assert new_errors("isaac", cursor=cursor, dirs=[root])["count"] == 0


# --- redaction -------------------------------------------------------------------------------------------------------

def test_redaction_of_secrets_and_home_paths():
    home_fwd = HOME.replace("\\", "/")
    text = (
        f"open {HOME}\\Documents\\scene.usd and {home_fwd.lower()}/x.usd and C:/Users/someone/y.usd "
        "token=abc123def456 password: hunter2pass api_key='k-998877' Authorization: Bearer abcdefghijklmnop "
        "https://bob:pa55w0rd@example.invalid/a?sig=SIGVALUE&x=1 nvapi-ABCDEFGHIJKLMNOP12 "
        "eyJhbGciOiJI.eyJzdWIiOiIx.c2lnbmF0dXJl /home/alice/f.txt C:/Users/Public/shared.usd"
    )
    out = redact(text)
    name = os.path.basename(HOME)
    for secret in ("abc123def456", "hunter2pass", "k-998877", "abcdefghijklmnop", "pa55w0rd", "SIGVALUE",
                   "nvapi-ABCD", "eyJhbGciOiJI", "someone", "alice"):
        assert secret not in out, secret
    assert name.lower() not in out.lower()
    assert "~\\Documents\\scene.usd" in out and "~/x.usd" in out and "~/y.usd" in out and "~/f.txt" in out
    assert "token=***" in out and "Bearer ***" in out and "sig=***" in out and "&x=1" in out
    assert "C:/Users/Public/shared.usd" in out
    assert redact(None) is None and redact("") == ""


def test_returned_entries_are_redacted():
    rows = header_lines() + [
        line("Error", "demo", f"cannot open {HOME}\\Documents\\cube.usd (access_token=SECRETVALUE1)", second=3),
    ]
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        for result in (tail("isaac", dirs=[root]), explain_recent("isaac", dirs=[root]),
                       new_errors("isaac", cursor=None, dirs=[root])):
            blob = repr(result)
            assert "SECRETVALUE1" not in blob and os.path.basename(HOME) not in blob
        assert "~\\Documents\\cube.usd" in tail("isaac", dirs=[root])[-1]["message"]


# --- noise, dedupe, explanations, summary ----------------------------------------------------------------------------

def test_noise_is_marked_not_dropped():
    rows = header_lines() + [
        line("Warning", "omni.rtx", "Skipping unsupported non-NVIDIA GPU: Demo Integrated Graphics", second=1),
        line("Warning", "omni.ext.plugin", "[ext: omni.demo-1.0.0] Extensions config 'extension.toml' doesn't exist "
             "'c:/demo/exts/omni.demo'", second=2),
        line("Warning", "omni.kit.window.demo", "Restoring dock layout for window 'Stage'", second=3),
        line("Warning", "omni.demo", "pxr.Demo is deprecated - please use Demo instead", second=4),
        line("Error", "omni.demo", "real problem here", second=5),
    ]
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        entries = tail("isaac", level="warning", dirs=[root])
        assert [e["noise"] for e in entries] == ["non_nvidia_gpu", "missing_extension_toml", "window_layout",
                                                 "deprecation", None]
        result = explain_recent("isaac", include_warnings=True, dirs=[root])
        assert [g["message"] for g in result["errors"]] == ["real problem here"]
        assert {n["id"] for n in result["noise"]} == {"non_nvidia_gpu", "missing_extension_toml", "window_layout",
                                                     "deprecation"}
        assert all(n["reason"] and n["count"] == 1 for n in result["noise"])
        stats = summary("isaac", dirs=[root])
        assert stats["noise"]["non_nvidia_gpu"] == 1 and stats["problems_by_extension"] == {"omni.demo": {"error": 1}}
    assert all({"id", "pattern", "reason"} <= set(rule) for rule in NOISE_PATTERNS)
    assert len({rule["id"] for rule in NOISE_PATTERNS}) == len(NOISE_PATTERNS)


def test_explain_recent_groups_repeats_and_explains_each_group():
    rows = header_lines()
    for i in range(3):
        rows += py_traceback(10 + i)
        rows.append(line("Error", "omni.physx.plugin", f"PhysX error: invalid mass {i}.0 on prim [/World/Box{i}]",
                         second=20 + i))
    rows.append(line("Error", "omni.usd", "Failed to open layer 'c:/demo/missing.usd'", second=30))
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        calls = []

        def knowledge(query):
            calls.append(query)
            return [{"title": "Demo doc", "source": "demo", "location": "demo.md", "snippet": "read me"}]

        result = explain_recent("isaac", limit=5, knowledge=knowledge, dirs=[root])
        assert result["error_count"] == 7 and result["distinct"] == 3 and len(result["errors"]) == 3
        groups = result["errors"]
        assert groups[0]["message"].startswith("Failed to open layer")  # most recent first
        tb_group = next(g for g in groups if "Traceback" in g["message"])
        assert tb_group["count"] == 3 and tb_group["first_time"] < tb_group["last_time"]
        explanation = tb_group["explanation"]
        assert explanation["kind"] != "unknown" and explanation["summary"] and explanation["how_to_fix"]
        assert explanation["exception"] == "AttributeError"
        assert next(g for g in groups if "PhysX" in g["message"])["count"] == 3
        assert calls and all(g["explanation"].get("related_docs") for g in groups)
        assert len(explain_recent("isaac", limit=1, dirs=[root])["errors"]) == 1


def test_explain_recent_and_summary_on_a_clean_log():
    with tempfile.TemporaryDirectory() as root:
        write_log(root, COMPOSER, lines=header_lines() + [line("Info", "demo", "all good", second=4)])
        result = explain_recent("composer", dirs=[root])
        assert result["errors"] == [] and result["error_count"] == 0 and "No errors" in result["note"]
        stats = summary("composer", dirs=[root])
        assert stats["app"] == "composer" and stats["errors"] == 0 and stats["entries"] == 3


def test_summary_counts_by_level_and_extension():
    rows = header_lines() + [
        line("Warning", "omni.ext.plugin", "[ext: omni.alpha-2.0.0] odd thing", second=1),
        line("Warning", "omni.ext.plugin", "[ext: omni.alpha-2.0.0] odd thing again", second=2),
        line("Error", "omni.beta.impl.core", "beta broke", second=3),
        line("Info", "omni.beta.impl.core", "beta info", second=4),
    ]
    with tempfile.TemporaryDirectory() as root:
        write_log(root, ISAAC, lines=rows)
        stats = summary("isaac", dirs=[root])
        assert stats["by_level"] == {"verbose": 0, "info": 3, "warning": 2, "error": 1, "fatal": 0}
        assert stats["errors"] == 1 and stats["warnings"] == 2 and stats["entries"] == 6
        assert stats["by_extension"]["omni.alpha"] == 2 and stats["by_extension"]["omni.beta"] == 2
        assert stats["by_source"]["omni.ext.plugin"] == 2
        assert stats["problems_by_extension"]["omni.alpha"] == {"warning": 2}
        assert stats["session_start"] == "2026-03-04T10:00:00Z" and stats["last_time"] == "2026-03-04T10:00:04Z"
        assert stats["truncated"] is False and stats["active"] is True


# --- polling ---------------------------------------------------------------------------------------------------------

def test_cursor_polling_returns_only_new_errors():
    with tempfile.TemporaryDirectory() as root:
        path = write_log(root, ISAAC, lines=header_lines() + [line("Error", "demo", "old error", second=1)])
        first = new_errors("isaac", dirs=[root])
        assert first["started"] is True and first["entries"] == [] and first["cursor"].startswith("kitlog1.")
        cursor = first["cursor"]
        assert os.path.basename(HOME) not in repr(first)

        append(path, [line("Info", "demo", "info", second=2), line("Error", "demo", "new error", second=3),
                      line("Warning", "demo", "a warning", second=4)])
        second = new_errors("isaac", cursor=cursor, dirs=[root])
        assert [e["message"] for e in second["entries"]] == ["new error"] and second["rotated"] is False
        third = new_errors("isaac", cursor=second["cursor"], dirs=[root])
        assert third["entries"] == [] and third["count"] == 0

        append(path, [line("Error", "demo", "half written", second=5)], newline=False)
        held = new_errors("isaac", cursor=third["cursor"], dirs=[root])
        assert held["entries"] == []
        append(path, [" line"])
        done = new_errors("isaac", cursor=held["cursor"], dirs=[root])
        assert [e["message"] for e in done["entries"]] == ["half written line"]

        warnings = new_errors("isaac", cursor=cursor, level="warning", dirs=[root])
        assert [e["message"] for e in warnings["entries"]] == ["new error", "a warning", "half written line"]


def test_cursor_holds_back_a_traceback_that_is_still_being_written():
    with tempfile.TemporaryDirectory() as root:
        path = write_log(root, ISAAC, lines=header_lines())
        cursor = new_errors("isaac", dirs=[root])["cursor"]
        rows = py_traceback(7)
        append(path, rows[:3])
        partial = new_errors("isaac", cursor=cursor, dirs=[root])
        assert partial["entries"] == []
        append(path, rows[3:])
        complete = new_errors("isaac", cursor=partial["cursor"], dirs=[root])
        assert len(complete["entries"]) == 1 and complete["entries"][0]["message"].endswith("'GetAttribute'")

        # a traceback cut off when the app died is returned once the file has gone quiet
        append(path, py_traceback(9)[:2])
        held = new_errors("isaac", cursor=complete["cursor"], dirs=[root])
        assert held["entries"] == []
        past = time.time() - 60
        os.utime(path, (past, past))
        released = new_errors("isaac", cursor=held["cursor"], dirs=[root])
        assert len(released["entries"]) == 1 and released["entries"][0]["message"].startswith("Traceback")


def test_rotation_to_a_new_session_log_reads_old_rest_then_new_file():
    with tempfile.TemporaryDirectory() as root:
        now = time.time()
        old = write_log(root, ISAAC, "kit_20260304_090000.log", header_lines(), mtime=now - 120)
        cursor = new_errors("isaac", dirs=[root])["cursor"]
        append(old, [line("Error", "demo", "last words of old session", second=8)])
        os.utime(old, (now - 100, now - 100))
        write_log(root, ISAAC, "kit_20260304_100000.log",
                  header_lines() + [line("Error", "demo", "first error of new session", second=2)], mtime=now)
        result = new_errors("isaac", cursor=cursor, dirs=[root])
        assert result["rotated"] is True
        assert [e["message"] for e in result["entries"]] == ["last words of old session", "first error of new session"]
        assert result["entries"][0]["file"].endswith("kit_20260304_090000.log")
        assert "kit_20260304_100000.log" in result["path"]
        again = new_errors("isaac", cursor=result["cursor"], dirs=[root])
        assert again["rotated"] is False and again["entries"] == []


def test_truncated_or_replaced_file_counts_as_rotation():
    with tempfile.TemporaryDirectory() as root:
        path = write_log(root, ISAAC, lines=header_lines() + [line("Info", "demo", "x" * 500, second=1)])
        cursor = new_errors("isaac", dirs=[root])["cursor"]
        path.write_text(line("Info", "carb", "Logging to file: a new run", second=0) + "\n" +
                        line("Error", "demo", "fresh error", second=1) + "\n", encoding="utf-8")
        result = new_errors("isaac", cursor=cursor, dirs=[root])
        assert result["rotated"] is True and [e["message"] for e in result["entries"]] == ["fresh error"]


def test_bad_foreign_or_forged_cursors_restart_safely():
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as other:
        write_log(root, ISAAC, lines=header_lines() + [line("Error", "demo", "e1", second=1)])
        write_log(root, COMPOSER, lines=header_lines() + [line("Error", "demo", "c1", second=1)])
        reset = new_errors("isaac", cursor="garbage", dirs=[root])
        assert reset["started"] is True and "cursor_reset" in reset and reset["entries"] == []
        composer_cursor = new_errors("composer", dirs=[root])["cursor"]
        crossed = new_errors("isaac", cursor=composer_cursor, dirs=[root])
        assert crossed["started"] is True and crossed["entries"] == []

        outside = Path(other) / "kit_secret.log"
        outside.write_text(line("Error", "x", "PRIVATE OUTSIDE") + "\n")
        state = kit_logs._decode_cursor(new_errors("isaac", dirs=[root])["cursor"])
        state["p"] = str(outside)
        forged = new_errors("isaac", cursor=state, dirs=[root])
        assert "PRIVATE OUTSIDE" not in repr(forged) and forged["rotated"] is True


def test_poll_more_flag_limits_entries_and_resumes():
    with tempfile.TemporaryDirectory() as root:
        path = write_log(root, ISAAC, lines=header_lines())
        cursor = new_errors("isaac", dirs=[root])["cursor"]
        append(path, [line("Error", "demo", f"error {i}", second=i) for i in range(5)])
        first = new_errors("isaac", cursor=cursor, max_entries=3, dirs=[root])
        assert first["more"] is True and [e["message"] for e in first["entries"]] == ["error 0", "error 1", "error 2"]
        rest = new_errors("isaac", cursor=first["cursor"], max_entries=3, dirs=[root])
        assert rest["more"] is False and [e["message"] for e in rest["entries"]] == ["error 3", "error 4"]


def test_poll_skips_ahead_when_far_behind():
    with tempfile.TemporaryDirectory() as root:
        path = write_log(root, ISAAC, lines=header_lines())
        cursor = new_errors("isaac", dirs=[root])["cursor"]
        append(path, [line("Error", "demo", "skipped error", second=1)])
        append(path, [line("Info", "demo", "z" * 200, second=2) for _ in range(500)])
        append(path, [line("Error", "demo", "kept error", second=3)])
        result = new_errors("isaac", cursor=cursor, max_bytes=20_000, dirs=[root])
        assert result["skipped_bytes"] > 0 and [e["message"] for e in result["entries"]] == ["kept error"]


# --- the real laptop logs (structure only) ---------------------------------------------------------------------------

def test_real_kit_logs_smoke():
    logs = find_logs()
    if not logs:
        return
    for log in logs[:5]:
        assert log["app"] and log["path"] and log["size"] >= 0
        assert os.path.basename(HOME).lower() not in log["path"].lower() or os.path.basename(HOME) in ("", "~")
    app = logs[0]["app"]
    entries = tail(app, lines=20)
    assert len(entries) <= 20
    for entry in entries:
        assert entry["level"] in kit_logs.LEVELS and isinstance(entry["message"], str)
    stats = summary(app)
    assert stats["ok"] and stats["entries"] >= 0 and set(stats["by_level"]) == set(kit_logs.LEVELS)
    explained = explain_recent(app, limit=2)
    assert explained["ok"] and len(explained["errors"]) <= 2
    for group in explained["errors"]:
        assert group["count"] >= 1 and "summary" in group["explanation"]
    poll = new_errors(app)
    assert poll["started"] and poll["cursor"].startswith("kitlog1.")
    assert new_errors(app, cursor=poll["cursor"])["ok"]
