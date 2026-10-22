"""Change journal (journal.py): temp folders only, no network, no scenes, no live apps."""

from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import threading
from pathlib import Path

from omniverse_scene_mcp import journal as journal_module
from omniverse_scene_mcp.journal import (
    ChangeJournal,
    JOURNALED_TOOLS,
    build_change_journal,
    default_journal_path,
    explain_entry,
    install_journal,
    journaled_tool,
    target_from_backend,
)

T = "2026-10-22T14:02:00"  # naive ISO = local time, so the clock reads 14:02 everywhere


def _temp() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(prefix="journal-test-")


def _move_result(dry_run: bool = False, undo_id=None) -> dict:
    payload = {"prim": {"path": "/World/Cube", "translation": [1.0, 2.0, 3.0]}}
    if undo_id is not None:
        payload["undo_id"] = undo_id
    return {
        "ok": True,
        "target": "scene",
        "dry_run": dry_run,
        "changes": [
            {
                "action": "transform",
                "path": "/World/Cube",
                "summary": "Update transform for /World/Cube",
                "before": {"path": "/World/Cube", "translation": [0.0, 0.0, 0.0], "rotation": [0.0, 0.0, 0.0],
                           "scale": [1.0, 1.0, 1.0], "material": None},
                "after": {"translation": [1.0, 2.0, 3.0], "rotation": None, "scale": None},
            }
        ],
        "payload": payload,
    }


MOVE_ARGS = {"path": "/World/Cube", "translation": [1, 2, 3], "rotation": None, "scale": None, "dry_run": False,
             "show_python": False}


def _authoring(action: str, path: str, inner: dict, *, ok: bool = True, dry_run: bool = False) -> dict:
    payload = {"ok": ok, "dry_run": dry_run, "action": action, "edit_target_layer": "anon:0x1:stage.usda", **inner}
    result = {
        "ok": ok,
        "target": "scene",
        "dry_run": dry_run,
        "changes": [{"action": action, "path": path, "summary": f"{action} {path}", "before": None, "after": {}}] if ok else [],
        "payload": payload,
        "applied": ok and not dry_run,
    }
    if dry_run:
        result["preview"] = True
    if "undoable" in inner:
        result["undoable"] = inner["undoable"]
    return result


# --- record / read ---------------------------------------------------------------------------------------------


def test_record_result_round_trip_and_ids():
    with _temp() as folder:
        path = Path(folder) / "j.jsonl"
        journal = ChangeJournal(path)
        first = journal.record_result("scene_move", MOVE_ARGS, _move_result(), target="composer")
        assert first is not None and first["id"] == 1
        assert first["kind"] == "edit" and first["action"] == "move" and first["ok"] is True
        assert first["prims"] == ["/World/Cube"]
        assert first["before"] == {"translation": [0.0, 0.0, 0.0]}
        assert first["after"] == {"translation": [1, 2, 3]}
        assert "show_python" not in first["args"]
        second = journal.record({"tool": "scene_delete", "action": "delete", "prims": ["/World/Old"], "target": "isaac"})
        assert second["id"] == 2
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2 and json.loads(lines[0])["tool"] == "scene_move"
        newest_first = ChangeJournal(path).entries()
        assert [entry["id"] for entry in newest_first] == [2, 1]
        # A fresh journal on the same file continues the ids.
        assert ChangeJournal(path).record({"tool": "x"})["id"] == 3


def test_dry_runs_previews_and_reads_are_ignored():
    with _temp() as folder:
        path = Path(folder) / "j.jsonl"
        journal = ChangeJournal(path)
        assert journal.record_result("scene_move", MOVE_ARGS, _move_result(dry_run=True)) is None
        preview = _authoring("create_light", "/World/Key", {"would_create": {"light_type": "rect"}}, dry_run=True)
        assert journal.record_result("scene_create_light", {"path": "/World/Key"}, preview) is None
        assert journal.record_result("scene_summary", {}, {"stage_name": "x", "prims": 3}) is None
        assert journal.record_result("scene_move", MOVE_ARGS, "not a dict") is None
        undo_preview = {"target": "scene", "ok": True, "dry_run": True, "would_undo": {"label": "Move"}, "preview": True}
        assert journal.record_result("scene_undo_last", {"dry_run": True}, undo_preview) is None
        failed_preview = _authoring("set_attribute", "/World/Cube", {"error": "nope"}, ok=False, dry_run=True)
        assert journal.record_result("scene_set_attribute", {}, failed_preview) is None
        assert not path.exists()
        assert journal.entries() == []


def test_secrets_are_dropped_and_values_capped():
    with _temp() as folder:
        path = Path(folder) / "j.jsonl"
        journal = ChangeJournal(path)
        args = {
            "path": "/World/Cube",
            "api_key": "AKIA-SECRET-1",
            "token": "tok-SECRET-2",
            "password": "pw-SECRET-3",
            "bridge_auth": "SECRET-4",
            "options": {"client_secret": "SECRET-5", "keep": "yes", "private_key": "SECRET-6"},
            "request": "move the cube, token=SECRET-7 and Authorization: Bearer SECRET-8abc",
            "long": "x" * 5000,
            "many": list(range(100)),
        }
        entry = journal.record_result("scene_move", args, _move_result(), target="composer")
        text = path.read_text(encoding="utf-8")
        for secret in ("SECRET-1", "SECRET-2", "SECRET-3", "SECRET-4", "SECRET-5", "SECRET-6", "SECRET-7", "SECRET-8"):
            assert secret not in text, secret
        for key in ("api_key", "token", "password", "bridge_auth"):
            assert key not in entry["args"]
        assert entry["args"]["options"] == {"keep": "yes"}
        assert "[removed]" in entry["args"]["request"]
        assert len(entry["args"]["long"]) <= 300
        assert len(entry["args"]["many"]) == 21 and entry["args"]["many"][-1] == "... 80 more"
        # Ordinary names that merely contain letters of a secret word survive.
        kept = journal.record({"tool": "t", "args": {"keyframe": 1, "monkey": 2, "authored": 3, "path": "/A"}})
        assert kept["args"] == {"keyframe": 1, "monkey": 2, "authored": 3, "path": "/A"}


def test_rotation_keeps_one_backup_and_reads_both():
    with _temp() as folder:
        path = Path(folder) / "j.jsonl"
        journal = ChangeJournal(path, max_bytes=2048)
        for index in range(40):
            journal.record({"tool": "scene_move", "action": "move", "prims": [f"/World/Cube{index}"], "label": "x" * 60})
        rotated = Path(str(path) + ".1")
        assert rotated.exists() and path.exists()
        assert path.stat().st_size <= 2048 and rotated.stat().st_size <= 2048
        assert not Path(str(path) + ".2").exists()
        found = journal.entries(limit=1000)
        ids = [entry["id"] for entry in found]
        assert ids == sorted(ids, reverse=True) and ids[0] == 40
        assert len(found) < 40  # the oldest rotated out with the second rotation
        assert found[-1]["id"] > 1


def test_corrupt_and_torn_lines_are_skipped():
    with _temp() as folder:
        path = Path(folder) / "j.jsonl"
        journal = ChangeJournal(path)
        journal.record({"tool": "scene_move", "action": "move", "prims": ["/A"]})
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")
            handle.write('["a list, not an entry"]\n')
            handle.write('{"id": 99, "tool": "torn')  # a crash mid-write: no newline at the end
        after = journal.record({"tool": "scene_delete", "action": "delete", "prims": ["/B"]})
        found = journal.entries()
        assert [entry["tool"] for entry in found] == ["scene_delete", "scene_move"]
        assert found[0]["id"] == after["id"]
        assert journal.corrupt_lines == 3
        assert journal.status()["corrupt_lines"] == 3
        assert journal.explain()  # sentences still work


def test_filters_prim_tool_target_since_limit():
    with _temp() as folder:
        journal = ChangeJournal(Path(folder) / "j.jsonl")
        journal.record({"time": "2026-10-21T09:00:00", "tool": "scene_move", "action": "move", "prims": ["/World/Car"],
                        "target": "isaac"})
        journal.record({"time": "2026-10-22T10:00:00", "tool": "scene_set_attribute", "action": "set_attribute",
                        "prims": ["/World/Car/Wheel"], "target": "composer"})
        journal.record({"time": "2026-10-22T11:00:00", "tool": "scene_delete", "action": "delete",
                        "prims": ["/World/Carpet"], "target": "Isaac Sim"})
        assert [e["prims"][0] for e in journal.entries(prim="/World/Car")] == ["/World/Car/Wheel", "/World/Car"]
        assert [e["tool"] for e in journal.entries(tool="move")] == ["scene_move"]
        assert [e["tool"] for e in journal.entries(tool="scene_move")] == ["scene_move"]
        assert [e["tool"] for e in journal.entries(tool="set_attribute")] == ["scene_set_attribute"]
        assert [e["tool"] for e in journal.entries(target="isaac")] == ["scene_delete", "scene_move"]
        assert [e["tool"] for e in journal.entries(target="composer")] == ["scene_set_attribute"]
        assert len(journal.entries(since="2026-10-22")) == 2
        assert len(journal.entries(since="2026-10-22T10:30")) == 1
        assert len(journal.entries(limit=1)) == 1
        assert journal.explain(tool="delete", target="isaac") == [
            "11:00 — You deleted /World/Carpet (and everything under it) in Isaac Sim. Undo with scene_undo_last."
        ]
        try:
            journal.entries(since="last tuesday")
        except ValueError:
            pass
        else:
            raise AssertionError("a bad since must be refused")


# --- explain wording -------------------------------------------------------------------------------------------


def _one(tool: str, args: dict, result: dict, target: str = "composer") -> str:
    with _temp() as folder:
        journal = ChangeJournal(Path(folder) / "j.jsonl")
        entry = journal.record_result(tool, args, result, target=target)
        assert entry is not None
        entry["time"] = T
        return explain_entry(entry)


def test_explain_move():
    assert _one("scene_move", MOVE_ARGS, _move_result(undo_id=7)) == (
        "14:02 — You moved /World/Cube from (0, 0, 0) to (1, 2, 3) in Composer. Undo with scene_undo_last (id 7)."
    )
    rotate_args = {"path": "/World/Cube", "rotation": [0, 90, 0], "scale": [2, 2, 2], "dry_run": False}
    text = _one("scene_move", rotate_args, _move_result())
    assert "You rotated /World/Cube from (0, 0, 0) to (0, 90, 0) and scaled it from (1, 1, 1) to (2, 2, 2)" in text


def test_explain_rename_and_delete():
    rename = {
        "ok": True, "target": "scene", "dry_run": False,
        "changes": [{"action": "rename", "path": "/World/Cube", "summary": "Rename /World/Cube to Box",
                     "before": {"path": "/World/Cube"}, "after": {"path": "/World/Box", "new_name": "Box"}}],
        "payload": {"prim": {"path": "/World/Box"}},
    }
    text = _one("scene_rename", {"path": "/World/Cube", "new_name": "Box", "dry_run": False}, rename, target="isaac")
    assert text == ("14:02 — You renamed /World/Cube to Box (it is now /World/Box) in Isaac Sim. "
                    "Undo with scene_undo_last.")
    delete = {"ok": True, "target": "scene", "dry_run": False,
              "changes": [{"action": "delete", "path": "/World/Old", "summary": "Delete prim at /World/Old"}],
              "payload": {"path": "/World/Old"}}
    assert _one("scene_delete", {"path": "/World/Old", "dry_run": False}, delete) == (
        "14:02 — You deleted /World/Old (and everything under it) in Composer. Undo with scene_undo_last."
    )


def test_explain_light_material_attribute():
    light = _authoring("create_light", "/World/Key", {"created": {"path": "/World/Key", "light_type": "sphere"},
                                                     "undoable": True})
    text = _one("scene_create_light", {"path": "/World/Key", "light_type": "sphere", "intensity": 5000,
                                       "dry_run": False}, light)
    assert text == ("14:02 — You added a sphere light at /World/Key with intensity 5000 in Composer. "
                    "Undo with scene_undo_last.")

    bind = _authoring("bind_material", "/World/Cube", {"material": "/World/Looks/Red", "path": "/World/Cube",
                                                       "before": {"bound_material": "/World/Looks/Blue"}})
    text = _one("scene_bind_material", {"prim_path": "/World/Cube", "material_path": "/World/Looks/Red",
                                        "dry_run": False}, bind)
    assert text == ("14:02 — You gave /World/Cube the material /World/Looks/Red (it had /World/Looks/Blue before) "
                    "in Composer. Undo with scene_undo_last.")

    created = _authoring("create_material", "/World/Looks/Red", {"path": "/World/Looks/Red"})
    text = _one("scene_create_material", {"path": "/World/Looks/Red", "diffuse_color": [1, 0, 0], "dry_run": False},
                created)
    assert "You created the material /World/Looks/Red with color (1, 0, 0) in Composer." in text

    attribute = _authoring("set_attribute", "/World/Key", {
        "path": "/World/Key", "attribute": "inputs:intensity",
        "before": {"exists": True, "value": 1000.0, "source": "default"}, "after": 5000.0,
    })
    text = _one("scene_set_attribute", {"path": "/World/Key", "name": "inputs:intensity", "value": 5000,
                                        "dry_run": False}, attribute)
    assert text == ("14:02 — You changed inputs:intensity on /World/Key from 1000 to 5000 in Composer. "
                    "Undo with scene_undo_last.")
    assert "not recorded for undo" in _one(
        "scene_aim", {"path": "/World/Cam", "target": "/World/Cube", "dry_run": False},
        {**_authoring("aim", "/World/Cam", {}),
         "message": "Applied ... This bridge did not record it for undo, so change it back with another edit if it is wrong."},
    )


def test_explain_undo_and_failures():
    undone = {"target": "scene", "ok": True, "dry_run": False, "applied": True, "verified": True, "remaining": 2,
              "undone": {"label": "Set inputs:intensity on /World/Key", "paths": ["/World/Key"], "layer": "anon:x"}}
    with _temp() as folder:
        journal = ChangeJournal(Path(folder) / "j.jsonl")
        entry = journal.record_result("scene_undo_last", {"dry_run": False}, undone, target="composer")
        assert entry["kind"] == "undo" and entry["prims"] == ["/World/Key"] and entry["stage"] == "anon:x"
        entry["time"] = T
        assert explain_entry(entry) == (
            "14:02 — You undid the change 'Set inputs:intensity on /World/Key' in Composer. "
            "Save again if you want the saved file to match."
        )
        refused = {"target": "scene", "ok": False, "dry_run": False, "refused": "changed",
                   "message": "/World/Key changed after this MCP edit.", "applied": False}
        failed = journal.record_result("scene_undo_last", {"dry_run": False}, refused, target="composer")
        assert failed["ok"] is False and failed["error"] == "/World/Key changed after this MCP edit."
        failed["time"] = T
        assert explain_entry(failed) == (
            "14:02 — An undo in Composer did not go through: /World/Key changed after this MCP edit. Nothing changed."
        )
        bad_edit = {"ok": False, "message": "Prim does not exist: /World/Nope", "target": "scene", "dry_run": False,
                    "applied": False, "path": "/World/Nope"}
        edit = journal.record_result("scene_set_attribute", {"path": "/World/Nope", "name": "x", "value": 1,
                                                             "dry_run": False}, bad_edit, target="composer")
        edit["time"] = T
        assert explain_entry(edit).startswith("14:02 — You tried to change /World/Nope in Composer with "
                                              "scene_set_attribute, but it did not go through: Prim does not exist")
        saved = journal.record_result("scene_save", {"path": "C:/tmp/x.usda", "dry_run": False},
                                      {"saved_path": "C:/tmp/x.usda"}, target="composer")
        assert saved is not None and saved["action"] == "save"
        assert "You saved the scene to C:/tmp/x.usda" in explain_entry(saved)


def test_router_dispatch_nested_result():
    nested = {
        "decision": {"target": "scene", "confidence": 0.9},
        "payload": _move_result(),
    }
    with _temp() as folder:
        journal = ChangeJournal(Path(folder) / "j.jsonl")
        entry = journal.record_result("router_dispatch", {"request": "move the cube up", "dry_run": False}, nested,
                                      target="fake")
        assert entry["action"] == "move" and entry["prims"] == ["/World/Cube"]
        assert entry["after"] == {"translation": [1.0, 2.0, 3.0]}
        assert "in the practice (in-memory) scene" in explain_entry(entry)
        preview = {"decision": {}, "payload": _move_result(dry_run=True)}
        assert journal.record_result("router_dispatch", {"request": "x", "dry_run": True}, preview) is None


# --- concurrency, clear, summary, paths, hook -------------------------------------------------------------------


def test_threads_write_whole_lines_with_unique_ids():
    with _temp() as folder:
        path = Path(folder) / "j.jsonl"
        journals = [ChangeJournal(path), ChangeJournal(path)]  # two objects, one file: they share one lock
        errors: list[BaseException] = []

        def work(index: int) -> None:
            try:
                for step in range(25):
                    journals[index % 2].record_result("scene_move", MOVE_ARGS, _move_result(), target="composer")
                    journals[index % 2].entries(limit=5)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        found = ChangeJournal(path).entries(limit=1000)
        assert len(found) == 200
        assert sorted(entry["id"] for entry in found) == list(range(1, 201))
        assert journals[0].corrupt_lines == 0


def test_clear_needs_confirm_and_keeps_backup():
    with _temp() as folder:
        path = Path(folder) / "j.jsonl"
        journal = ChangeJournal(path)
        journal.record({"tool": "scene_move", "action": "move", "prims": ["/A"]})
        journal.record({"tool": "scene_delete", "action": "delete", "prims": ["/B"]})
        refused = journal.clear()
        assert refused["ok"] is False and refused["cleared"] == 0 and len(journal.entries()) == 2
        assert journal.clear(confirm="yes")["ok"] is False  # only True itself confirms
        done = journal.clear(confirm=True)
        assert done["ok"] is True and done["cleared"] == 2
        backup = Path(done["backup"])
        assert backup.exists() and backup.parent == path.parent
        assert len(backup.read_text(encoding="utf-8").splitlines()) == 2
        assert journal.entries() == [] and not path.exists()
        assert journal.record({"tool": "x"})["id"] == 3  # ids keep counting after a clear
        assert journal.clear(confirm=True)["cleared"] == 1
        assert len(list(Path(folder).glob("j.jsonl.cleared-*.bak"))) == 2
        empty = journal.clear(confirm=True)
        assert empty["ok"] is True and empty["cleared"] == 0 and empty["backup"] is None


def test_summary_counts_and_three_line_story():
    with _temp() as folder:
        journal = ChangeJournal(Path(folder) / "j.jsonl")
        assert len(journal.summary("2026-10-22")["story"]) == 3
        journal.record({"time": "2026-10-22T09:00:00", "tool": "scene_move", "action": "move", "prims": ["/World/Cube"],
                        "target": "composer", "after": {"translation": [1, 0, 0]}})
        journal.record({"time": "2026-10-22T09:05:00", "tool": "scene_move", "action": "move", "prims": ["/World/Cube"],
                        "target": "composer", "after": {"translation": [2, 0, 0]}})
        journal.record({"time": "2026-10-22T09:10:00", "tool": "scene_create_light", "action": "create_light",
                        "prims": ["/World/Key"], "target": "isaac", "details": {"light_type": "rect"}})
        journal.record({"time": "2026-10-22T09:20:00", "tool": "scene_undo_last", "action": "undo", "kind": "undo",
                        "prims": ["/World/Key"], "target": "isaac", "details": {"label": "Create light /World/Key"}})
        journal.record({"time": "2026-10-21T09:00:00", "tool": "scene_delete", "action": "delete", "prims": ["/X"]})
        result = journal.summary("2026-10-22")
        assert result["total"] == 4 and result["edits"] == 3 and result["undos"] == 1 and result["failed"] == 0
        assert result["by_tool"] == {"scene_move": 2, "scene_create_light": 1, "scene_undo_last": 1}
        assert result["by_prim"]["/World/Cube"] == 2
        story = result["story"]
        assert len(story) == 3
        assert story[0] == "On 2026-10-22 you made 3 changes in Composer and Isaac Sim (undid 1)."
        assert story[1] == "You worked most on /World/Cube (2 times); the tool you used most was scene_move (2)."
        assert story[2].startswith("Last: 09:20 — You undid the change 'Create light /World/Key' in Isaac Sim.")
        assert journal.summary("2026-10-20")["story"][0] == "No scene changes were recorded on 2026-10-20."


def test_default_path_env_override_and_test_mode():
    with _temp() as folder:
        as_file = str(Path(folder) / "mine.jsonl")
        assert default_journal_path({"OMNIVERSE_SCENE_MCP_JOURNAL": as_file}) == Path(as_file)
        assert default_journal_path({"OMNIVERSE_SCENE_MCP_JOURNAL": folder}) == Path(folder) / "change_journal.jsonl"
        built = build_change_journal({"OMNIVERSE_SCENE_MCP_JOURNAL": as_file, "OMNIVERSE_SCENE_MCP_TEST": "1"})
        assert built.path == Path(as_file) and not built.memory_only
    memory = build_change_journal({"OMNIVERSE_SCENE_MCP_TEST": "1"})
    assert memory.memory_only and memory.path is None
    default = default_journal_path({})
    assert default.name == "change_journal.jsonl" and default.parent.name == "scene-journal"
    assert "exam-prep" not in str(default)
    # Memory-only journals work fully and never touch the disk.
    memory.record_result("scene_move", MOVE_ARGS, _move_result(), target="fake")
    assert len(memory.entries()) == 1 and memory.status()["path"] is None
    assert memory.clear(confirm=True)["cleared"] == 1 and memory.entries() == []


def test_target_from_backend_info():
    assert target_from_backend({"mode": "http_env", "target": "Isaac Sim"}) == "isaac"
    assert target_from_backend({"mode": "http_autodetect", "endpoint": "http://127.0.0.1:9910"}) == "composer"
    assert target_from_backend({"mode": "http_env", "endpoint": "http://127.0.0.1:9911/"}) == "isaac"
    assert target_from_backend({"mode": "test"}) == "fake"
    assert target_from_backend(None) is None


def test_journaled_tool_wraps_sync_and_async_and_swallows_errors():
    with _temp() as folder:
        journal = ChangeJournal(Path(folder) / "j.jsonl")

        def scene_move(path: str, translation: list | None = None, dry_run: bool = False) -> dict:
            """Move it."""
            return _move_result(dry_run=dry_run)

        async def router_dispatch(request: str, dry_run: bool = False) -> dict:
            return {"decision": {}, "payload": _move_result(dry_run=dry_run)}

        wrapped = journaled_tool(journal, "scene_move", scene_move, target=lambda: "composer")
        assert wrapped.__name__ == "scene_move" and wrapped.__doc__ == "Move it."
        assert inspect.signature(wrapped) == inspect.signature(scene_move)
        assert wrapped("/World/Cube", [1, 2, 3])["dry_run"] is False
        wrapped("/World/Cube", [1, 2, 3], dry_run=True)
        found = journal.entries()
        assert len(found) == 1 and found[0]["args"]["path"] == "/World/Cube" and found[0]["target"] == "composer"

        async_wrapped = journaled_tool(journal, "router_dispatch", router_dispatch, target="isaac")
        assert inspect.iscoroutinefunction(async_wrapped)
        asyncio.run(async_wrapped(request="move it", dry_run=False))
        assert journal.entries()[0]["tool"] == "router_dispatch"

        class Broken(ChangeJournal):
            def record_result(self, *args, **kwargs):
                raise OSError("disk full")

        safe = journaled_tool(Broken(memory_only=True), "scene_move", scene_move)
        assert safe("/World/Cube")["ok"] is True  # the tool's answer survives a broken journal


def test_install_journal_wraps_only_mutating_tools():
    class FakeApp:
        def __init__(self) -> None:
            self.registered: dict[str, object] = {}

        def tool(self, name=None, **kwargs):
            def decorator(fn):
                self.registered[name or fn.__name__] = fn
                return fn

            return decorator

    with _temp() as folder:
        journal = ChangeJournal(Path(folder) / "j.jsonl")
        app = install_journal(FakeApp(), journal, target=lambda: "composer")

        @app.tool(name="scene_move")
        def scene_move(path: str, dry_run: bool = False) -> dict:
            return _move_result(dry_run=dry_run)

        @app.tool(name="scene_summary")
        def scene_summary() -> dict:
            return {"ok": True, "dry_run": False, "stage_name": "x"}

        assert app.registered["scene_move"] is not scene_move  # a journaled copy was registered
        assert app.registered["scene_summary"] is scene_summary
        app.registered["scene_move"](path="/World/Cube")
        app.registered["scene_summary"]()
        scene_move("/World/Cube")  # the original function is returned unchanged and is not journaled
        assert [entry["tool"] for entry in journal.entries()] == ["scene_move"]
    assert {"scene_move", "scene_undo_last", "scene_set_attribute", "router_dispatch"} <= JOURNALED_TOOLS
    assert "scene_summary" not in JOURNALED_TOOLS
    assert journal_module.ENV_PATH == "OMNIVERSE_SCENE_MCP_JOURNAL"
