"""scene_safe_edit on a fake router: previews change nothing, codes are one-time and bound to the same edit."""
import asyncio

from omniverse_scene_mcp import safe_edit

_MOVE_SCHEMA = {"properties": {"path": {}, "translation": {}, "dry_run": {}}}
_SCHEMAS = {"scene_move": _MOVE_SCHEMA, "scene_inspect": {"properties": {"path": {}}},
            "scene_save": {"properties": {"dry_run": {}}}}


class FakeRouter:
    """A one-prim scene with router undo history and checkpoints, recording every call."""

    def __init__(self, *, unsaved=False, anonymous=False, fail_apply=False):
        self.prims = {"/World/Box": {"type_name": "Cube", "translation": [0, 0, 0], "children": []}}
        self.unsaved, self.anonymous, self.fail_apply = unsaved, anonymous, fail_apply
        self.undo, self.checkpoints, self.calls = 0, [], []

    async def call(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "scene_inspect":
            if args["path"] not in self.prims:
                raise ValueError(f"Prim does not exist: {args['path']}")
            return {"path": args["path"], **self.prims[args["path"]]}
        if name == "scene_layers":
            return {"ok": True, "stage_identifier": "file:/scene.usda", "root_layer_anonymous": self.anonymous,
                    "has_unsaved_changes": self.unsaved}
        if name == "scene_undo_history":
            return {"ok": True, "count": self.undo}
        if name == "scene_checkpoint":
            self.checkpoints.append(args["note"])
            return {"ok": True, "id": f"000{len(self.checkpoints)}-test"}
        if name == "scene_move":
            if args["path"] not in self.prims:
                return {"ok": False, "error": f"Prim does not exist: {args['path']}"}
            if args["dry_run"]:
                return {"ok": True, "dry_run": True, "undoable": True,
                        "changes": [{"after": {"translation": args["translation"]}}]}
            if self.fail_apply:
                return {"ok": False, "error": "bridge went away"}
            self.prims[args["path"]]["translation"] = list(args["translation"])
            self.unsaved, self.undo = True, self.undo + 1
            return {"ok": True, "dry_run": False, "applied": True}
        raise KeyError(name)


def _run(router, tool, arguments, **kwargs):
    return asyncio.run(safe_edit.safe_edit(router.call, _SCHEMAS.get, tool, arguments, **kwargs))


def test_preview_changes_nothing_and_apply_reports_before_and_after_with_undo_and_checkpoint():
    router = FakeRouter()
    move = {"path": "/World/Box", "translation": [0, 0, 1]}
    preview = _run(router, "scene_move", move)
    assert preview["ok"] and preview["step"] == "preview" and not preview["changed"], preview
    assert router.prims["/World/Box"]["translation"] == [0, 0, 0] and not router.checkpoints
    assert preview["safety_net"]["checkpoint_first"] and "undo" in preview["safety_net"]["undo"]
    applied = _run(router, "scene_move", move, confirm=preview["confirm"])
    assert applied["ok"] and applied["step"] == "applied", applied
    assert applied["changes"] == ["/World/Box: translation [0, 0, 0] -> [0, 0, 1]"], applied["changes"]
    assert safe_edit._show((0.0, -2.0, 0.20000000298023224)) == "[0, -2, 0.2]"
    assert router.checkpoints == ["before scene_move (scene_safe_edit)"]
    assert applied["safety_net"]["checkpoint"] == "0001-test" and "scene_undo_last" in applied["safety_net"]["undo"]
    assert "scene_restore_checkpoint(checkpoint='0001-test'" in applied["restore"] and applied["saved"] is False
    again = _run(router, "scene_move", move, confirm=preview["confirm"])
    assert not again["ok"] and "unknown or expired" in again["error"], "a code works once"


def test_code_is_bound_to_the_same_edit_and_to_an_unchanged_scene():
    router = FakeRouter()
    move = {"path": "/World/Box", "translation": [0, 0, 1]}
    code = _run(router, "scene_move", move)["confirm"]
    other = _run(router, "scene_move", {"path": "/World/Box", "translation": [5, 5, 5]}, confirm=code)
    assert not other["ok"] and "differ from the preview" in other["error"], other
    code = _run(router, "scene_move", move)["confirm"]
    router.prims["/World/Box"]["translation"] = [9, 9, 9]  # someone moved it in the app after the preview
    stale = _run(router, "scene_move", move, confirm=code)
    assert not stale["ok"] and "changed after the preview" in stale["error"], stale
    assert router.prims["/World/Box"]["translation"] == [9, 9, 9] and not any(
        name == "scene_move" and not args["dry_run"] for name, args in router.calls)


def test_checkpoint_is_skipped_with_a_reason_when_it_would_miss_unsaved_changes():
    for router, reason in ((FakeRouter(unsaved=True), "unsaved changes"), (FakeRouter(anonymous=True), "never saved")):
        move = {"path": "/World/Box", "translation": [1, 0, 0]}
        preview = _run(router, "scene_move", move)
        assert not preview["safety_net"]["checkpoint_first"] and reason in preview["safety_net"]["why"]
        applied = _run(router, "scene_move", move, confirm=preview["confirm"])
        assert applied["ok"] and applied["safety_net"]["checkpoint"] is None and not router.checkpoints
        assert reason in applied["safety_net"]["checkpoint_skipped"]


def test_refusals_change_nothing():
    router = FakeRouter()
    cases = [
        ("scene_save", {}, "does not wrap scene_save"),
        ("scene_inspect", {"path": "/World/Box"}, "no dry_run preview"),
        ("scene_move", {"path": "/World/Box", "translation": [1, 0, 0], "dry_run": False}, "Leave dry_run out"),
        ("scene_move", {"path": "/World/Box", "translaton": [1, 0, 0]}, "did you mean 'translation'"),
        ("scene_nope", {}, "Unknown tool"),
    ]
    for tool, arguments, text in cases:
        result = _run(router, tool, arguments)
        assert not result["ok"] and text in result["error"], (tool, result)
    failed = _run(router, "scene_move", {"path": "/World/Missing", "translation": [1, 0, 0]})
    assert not failed["ok"] and "preview failed" in failed["error"] and "confirm" not in failed, failed
    assert not _run(router, "scene_move", {"path": "/World/Box", "translation": [1, 0, 0]}, confirm="nope")["ok"]
    assert router.prims["/World/Box"]["translation"] == [0, 0, 0] and not router.checkpoints


def test_a_failed_apply_says_whether_anything_changed():
    router = FakeRouter(fail_apply=True)
    move = {"path": "/World/Box", "translation": [0, 0, 1]}
    applied = _run(router, "scene_move", move, confirm=_run(router, "scene_move", move)["confirm"])
    assert not applied["ok"] and "bridge went away" in applied["error"] and applied["left_unchanged"], applied
    assert "restore" in applied  # the checkpoint taken first is offered


def test_prim_paths_are_found_in_values_and_mapping_keys_but_not_file_paths():
    found = safe_edit.prim_paths({"path": "/World/A", "mapping": {"/World/B": "ball"}, "asset": "/assets/x.usd",
                                  "targets": ["/World/C", "/World/A"], "note": "not a path"})
    assert found == ["/World/A", "/World/B", "/World/C"], found
