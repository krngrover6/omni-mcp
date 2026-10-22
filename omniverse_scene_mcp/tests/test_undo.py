"""Undo snapshots, the undo stack and the Kit command glue, on temporary in-memory stages with a fake Kit.

Fixture-free test_* functions, so they also run inside Isaac Sim's bridge reload self-test.
"""

import ast
import gc
import json
import types
from pathlib import Path

try:
    from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
except ImportError:  # pragma: no cover - depends on the local environment
    import unittest

    # unittest.SkipTest at import is a module skip for pytest and tools/run_tests.py alike, with or without pytest.
    raise unittest.SkipTest("USD Python bindings (pxr) are not installed")

from omniverse_scene_mcp import kit_commands, undo
from omniverse_scene_mcp.undo import NAMESPACE_LAYER_KEYS, SpecSnapshot, UndoEntry, UndoError, UndoStack


def _scene():
    """A small stage whose specs exercise ordering, targets, connections, arcs, variants and defaultPrim."""
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.Xform.Define(stage, "/World")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    UsdGeom.Cube.Define(stage, "/World/A").CreateSizeAttr(2.0)
    UsdGeom.Xform.Define(stage, "/World/B")
    kid = UsdGeom.Cube.Define(stage, "/World/B/K").GetPrim()
    kid.CreateRelationship("up").AddTarget("/World/B")
    kid.CreateAttribute("inp", Sdf.ValueTypeNames.Float).AddConnection("/World/B/K.other")
    cube_c = UsdGeom.Cube.Define(stage, "/World/C")
    variants = cube_c.GetPrim().GetVariantSets().AddVariantSet("look")
    variants.AddVariant("red")
    variants.AddVariant("blue")
    variants.SetVariantSelection("red")
    UsdShade.Material.Define(stage, "/World/Looks/Paint")
    stage.GetPrimAtPath("/World/A").CreateRelationship("peer").AddTarget("/World/B")
    UsdGeom.Xform.Define(stage, "/Other")
    return stage


def _text(stage):
    return stage.GetRootLayer().ExportToString()


def _capture(stage, paths=(), shallow=(), keys=()):
    return SpecSnapshot.capture(stage.GetRootLayer(), paths, shallow=shallow, layer_keys=keys)


def _round_trip(stage, edit, paths=(), shallow=(), keys=()):
    """Snapshot, edit, then check undo (restore before) and redo (restore after) are byte-exact."""
    before_text = _text(stage)
    before = _capture(stage, paths, shallow, keys)
    edit()
    after_text = _text(stage)
    assert after_text != before_text, "the edit should change the layer"
    after = before.recapture()
    assert after.matches() and not before.matches()
    before.restore()
    assert _text(stage) == before_text
    assert before.matches() and not after.matches()
    after.restore()
    assert _text(stage) == after_text
    before.restore()
    assert _text(stage) == before_text
    return before, after


def test_create_with_new_ancestors_is_removed_exactly():
    stage = _scene()
    before, _after = _round_trip(stage, lambda: stage.DefinePrim("/World/New/Deep/Leaf", "Cube"), ["/World/New/Deep/Leaf"])
    # Define wrote 'over' specs for the missing ancestors, so the shallowest missing one is what gets removed.
    assert before.describe()["absent"] == ["/World/New"]


def test_delete_comes_back_at_its_old_position_with_its_subtree():
    stage = _scene()
    _round_trip(stage, lambda: stage.RemovePrim("/World/B"), ["/World/B"])
    assert [p.GetName() for p in stage.GetPrimAtPath("/World").GetChildren()][:3] == ["A", "B", "C"]
    # A root prim too: /World is first, /Other second.
    _round_trip(stage, lambda: stage.RemovePrim("/World"), ["/World"])
    assert [p.GetName() for p in stage.GetPseudoRoot().GetChildren()] == ["World", "Other"]


def test_shallow_move_keeps_children_and_copies_only_the_prim():
    stage = _scene()

    def move():
        common = UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/B"))
        common.SetTranslate(Gf.Vec3d(1.0, 2.0, 3.0))
        common.SetRotate(Gf.Vec3f(0.0, 45.0, 0.0))

    before, _after = _round_trip(stage, move, shallow=["/World/B"])
    assert before.describe()["shallow"] == ["/World/B"]
    assert stage.GetPrimAtPath("/World/B/K").IsValid()


def test_rename_undo_restores_targets_arcs_and_default_prim():
    stage = _scene()
    layer = stage.GetRootLayer()
    stage.DefinePrim("/World/D", "Xform").GetInherits().AddInherit("/World/B")
    stage.DefinePrim("/World/E", "Xform").GetReferences().AddInternalReference("/World/B")
    dependents = undo.find_dependents(layer, ["/World/B"])
    assert {"/World/A", "/World/D", "/World/E"} <= set(dependents)

    def rename_b():
        editor = Usd.NamespaceEditor(stage)
        assert editor.RenamePrim(stage.GetPrimAtPath("/World/B"), "X") and editor.ApplyEdits()

    _round_trip(stage, rename_b, ["/World/B", "/World/X"], dependents, NAMESPACE_LAYER_KEYS)

    def rename_world():
        editor = Usd.NamespaceEditor(stage)
        assert editor.RenamePrim(stage.GetPrimAtPath("/World"), "Scene") and editor.ApplyEdits()

    _round_trip(stage, rename_world, ["/World", "/Scene"], undo.find_dependents(layer, ["/World"]), NAMESPACE_LAYER_KEYS)
    assert layer.defaultPrim == "World"


def test_reparent_undo_puts_the_prim_back_in_order():
    stage = _scene()

    def reparent():
        editor = Usd.NamespaceEditor(stage)
        assert editor.ReparentPrim(stage.GetPrimAtPath("/World/B"), stage.GetPrimAtPath("/World/C"))
        assert editor.ApplyEdits()

    deps = undo.find_dependents(stage.GetRootLayer(), ["/World/B"])
    _round_trip(stage, reparent, ["/World/B", "/World/C/B"], deps, NAMESPACE_LAYER_KEYS)


def test_duplicate_undo_removes_the_copy():
    stage = _scene()
    layer = stage.GetRootLayer()
    _round_trip(stage, lambda: Sdf.CopySpec(layer, "/World/B", layer, "/World/B_copy"), ["/World/B_copy"])


def test_material_binding_and_variant_selection_round_trip():
    stage = _scene()
    material = UsdShade.Material(stage.GetPrimAtPath("/World/Looks/Paint"))

    def bind():
        UsdShade.MaterialBindingAPI.Apply(stage.GetPrimAtPath("/World/A")).Bind(material)

    _round_trip(stage, bind, shallow=["/World/A"])
    _round_trip(stage, lambda: stage.GetPrimAtPath("/World/C").GetVariantSet("look").SetVariantSelection("blue"), shallow=["/World/C"])
    # A variant selection on a prim that has no spec in the layer yet creates an 'over' that undo removes.
    with_ref = Usd.Stage.CreateInMemory()
    sub = Sdf.Layer.CreateAnonymous()
    with_ref.GetRootLayer().subLayerPaths.append(sub.identifier)
    sub_stage = Usd.Stage.Open(sub)
    sub_prim = sub_stage.DefinePrim("/Thing", "Xform")
    sub_vs = sub_prim.GetVariantSets().AddVariantSet("size")
    sub_vs.AddVariant("s")
    sub_vs.AddVariant("l")
    _round_trip(with_ref, lambda: with_ref.GetPrimAtPath("/Thing").GetVariantSet("size").SetVariantSelection("l"), shallow=["/Thing"])


def test_restore_leaves_unrelated_specs_alone():
    stage = _scene()
    before = _capture(stage, shallow=["/World/A"])
    UsdGeom.Cube(stage.GetPrimAtPath("/World/A")).GetSizeAttr().Set(9.0)
    UsdGeom.Cube(stage.GetPrimAtPath("/World/C")).CreateSizeAttr(5.0)
    assert before.restore() == ["/World/A"]
    assert UsdGeom.Cube(stage.GetPrimAtPath("/World/A")).GetSizeAttr().Get() == 2.0
    assert UsdGeom.Cube(stage.GetPrimAtPath("/World/C")).GetSizeAttr().Get() == 5.0
    assert before.restore() == []  # nothing left to change


def test_state_comparison_ignores_field_storage_order_but_not_child_order():
    """Traverse visits properties and children in the order their fields were stored, which CopySpec changes."""

    def build(names, attribute_first):
        layer = Sdf.Layer.CreateAnonymous()
        prim = Sdf.CreatePrimInLayer(layer, "/M")
        prim.specifier = Sdf.SpecifierDef
        if attribute_first:
            Sdf.AttributeSpec(prim, "out", Sdf.ValueTypeNames.Token)
        for name in names:
            Sdf.CreatePrimInLayer(layer, f"/M/{name}").specifier = Sdf.SpecifierDef
        if not attribute_first:
            Sdf.AttributeSpec(prim, "out", Sdf.ValueTypeNames.Token)
        return undo._subtree_state(layer, Sdf.Path("/M"))

    assert undo._equal(build(["S", "T"], True), build(["S", "T"], False))
    assert not undo._equal(build(["S", "T"], True), build(["T", "S"], True))
    stage = _scene()
    stage.DefinePrim("/World/Looks/Red/Shader", "Shader")
    stage.GetPrimAtPath("/World/Looks/Red").CreateAttribute("outputs:surface", Sdf.ValueTypeNames.Token)
    snapshot = _capture(stage, ["/World/Looks/Red"])
    assert snapshot.matches() and snapshot.same_state(snapshot.recapture())


def test_capture_rejects_paths_that_are_not_absolute_prim_paths():
    stage = _scene()
    for bad in ("World/A", "/World/A.size", "/", "", "not a path!"):
        try:
            _capture(stage, [bad])
        except UndoError:
            pass
        else:
            raise AssertionError(f"capture should reject {bad!r}")


def _entry(stage, label, edit, paths=(), shallow=()):
    before = [_capture(stage, paths, shallow)]
    edit()
    return UndoEntry(label, list(paths) + list(shallow), before, [before[0].recapture()], stage_ref=undo.LayerRef(stage.GetRootLayer()))


def test_stack_is_bounded_and_history_is_newest_first_and_json_safe():
    stage = _scene()
    stack = UndoStack(max_entries=3)
    for index in range(5):
        path = f"/World/P{index}"
        stack.push(_entry(stage, f"Create {path}", lambda p=path: stage.DefinePrim(p, "Xform"), [path]))
    assert len(stack) == 3 and stack.dropped == 2
    history = stack.history(10, stage=stage)
    assert [item["label"] for item in history] == ["Create /World/P4", "Create /World/P3", "Create /World/P2"]
    assert [item["index"] for item in history] == [1, 2, 3]
    assert all(item["on_open_stage"] and not item["stale"] for item in history)
    assert len(stack.history(1)) == 1 and len(stack.history(0)) == 1 and len(stack.history("x")) == 3
    json.dumps(history)
    assert UndoStack(max_entries=0).max_entries == 1 and UndoStack(max_entries=10**6).max_entries == undo.MAX_ENTRIES_LIMIT


def test_undo_last_dry_run_is_byte_identical_and_real_run_undoes_in_order():
    stage = _scene()
    stack = UndoStack()
    texts = [_text(stage)]
    stack.push(_entry(stage, "Create /World/N", lambda: stage.DefinePrim("/World/N", "Cube"), ["/World/N"]))
    texts.append(_text(stage))
    stack.push(_entry(stage, "Delete /World/B", lambda: stage.RemovePrim("/World/B"), ["/World/B"]))
    texts.append(_text(stage))
    dirty = stage.GetRootLayer().dirty

    preview = stack.undo_last(stage=stage)
    assert preview["ok"] and preview["dry_run"] and preview["would_undo"]["label"] == "Delete /World/B"
    assert _text(stage) == texts[2] and stage.GetRootLayer().dirty == dirty and len(stack) == 2

    first = stack.undo_last(stage=stage, dry_run=False)
    assert first["ok"] and first["undone"]["label"] == "Delete /World/B" and first["verified"] is True
    assert _text(stage) == texts[1]
    second = stack.undo_last(stage=stage, dry_run=False)
    assert second["undone"]["paths"] == ["/World/N"] and _text(stage) == texts[0]
    empty = stack.undo_last(stage=stage, dry_run=False)
    assert empty["ok"] is False and "Nothing to undo" in empty["message"] and "not undoable" in empty["note"]
    json.dumps([preview, first, second, empty])


def test_undo_refuses_a_replaced_or_closed_stage():
    stage = _scene()
    other = _scene()
    stack = UndoStack()
    stack.push(_entry(stage, "Create /World/N", lambda: stage.DefinePrim("/World/N"), ["/World/N"]))

    preview = stack.undo_last(stage=other)
    assert preview["ok"] is False and preview["refused"] == "stale" and len(stack) == 1
    refused = stack.undo_last(stage=other, dry_run=False)
    assert refused["refused"] == "stale" and refused["discarded"] is True and len(stack) == 0
    assert stage.GetPrimAtPath("/World/N").IsValid()

    def record_on_a_temporary_stage():
        temp = _scene()
        return _entry(temp, "Create /World/T", lambda: temp.DefinePrim("/World/T"), ["/World/T"])

    entry = record_on_a_temporary_stage()
    gc.collect()
    assert entry.is_stale()
    assert entry.problem()[0] == "stale"
    try:
        entry.undo(force=True)
    except undo.UndoRefused as exc:
        assert exc.kind == "stale"
    else:
        raise AssertionError("undo on a closed layer should be refused")


def test_undo_refuses_after_a_later_change_and_force_overrides():
    stage = _scene()
    stack = UndoStack()
    size = UsdGeom.Cube(stage.GetPrimAtPath("/World/A")).GetSizeAttr()
    stack.push(_entry(stage, "Resize /World/A", lambda: size.Set(3.0), shallow=["/World/A"]))
    size.Set(4.0)  # someone else edits the same prim afterwards
    text = _text(stage)
    refused = stack.undo_last(stage=stage, dry_run=False)
    assert refused["ok"] is False and refused["refused"] == "changed" and _text(stage) == text
    size.Set(3.0)  # their change undone: the MCP edit is on top again
    assert stack.undo_last(stage=stage, dry_run=False)["ok"] is True and size.Get() == 2.0


def test_run_undoable_direct_records_rolls_back_and_skips_no_ops():
    stage = _scene()
    stack = UndoStack()
    start = _text(stage)
    prim = kit_commands.run_undoable("Create /World/N", stage, ["/World/N"], lambda: stage.DefinePrim("/World/N", "Cube"), stack=stack, commands=None)
    assert prim.GetPath() == Sdf.Path("/World/N") and len(stack) == 1 and stack.peek().via == "direct"

    def failing():
        stage.DefinePrim("/World/Half/Done", "Xform")
        raise ValueError("boom")

    before_fail = _text(stage)
    try:
        kit_commands.run_undoable("Broken edit", stage, ["/World/Half/Done"], failing, stack=stack, commands=None)
    except ValueError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("the edit's exception should propagate")
    assert _text(stage) == before_fail and len(stack) == 1  # rolled back, nothing recorded

    kit_commands.run_undoable("No-op", stage, [], lambda: None, shallow=["/World/A"], stack=stack, commands=None)
    assert len(stack) == 1
    assert stack.undo_last(stage=stage, dry_run=False)["ok"] and _text(stage) == start


def test_run_undoable_namespace_edit_snapshots_dependents_and_layer_stack():
    stage = Usd.Stage.CreateInMemory()
    sub = Sdf.Layer.CreateAnonymous("sub")
    stage.GetRootLayer().subLayerPaths.append(sub.identifier)
    with Usd.EditContext(stage, sub):
        UsdGeom.Xform.Define(stage, "/World")
        UsdGeom.Cube.Define(stage, "/World/Box")
    UsdGeom.XformCommonAPI(stage.GetPrimAtPath("/World/Box")).SetTranslate(Gf.Vec3d(1.0, 0.0, 0.0))
    stage.DefinePrim("/World/Watcher", "Xform").CreateRelationship("watch").AddTarget("/World/Box")
    stage.SetDefaultPrim(stage.GetPrimAtPath("/World"))
    root_text, sub_text = _text(stage), sub.ExportToString()
    stack = UndoStack()

    def rename():
        editor = Usd.NamespaceEditor(stage)
        assert editor.RenamePrim(stage.GetPrimAtPath("/World/Box"), "Crate") and editor.ApplyEdits()

    kit_commands.run_undoable("Rename", stage, ["/World/Box", "/World/Crate"], rename, namespace_edit=True, stack=stack, commands=None)
    assert stage.GetPrimAtPath("/World/Crate").IsValid() and sub.GetPrimAtPath("/World/Crate")
    entry = stack.peek()
    assert len(entry.before) == 2 and entry.summary()["layers"][1] == sub.identifier
    result = stack.undo_last(stage=stage, dry_run=False)
    assert result["ok"] and result["verified"]
    assert _text(stage) == root_text and sub.ExportToString() == sub_text


def test_snapshot_failure_still_runs_the_edit_and_leaves_a_note():
    stage = _scene()
    stack = UndoStack()
    ran = []
    kit_commands.run_undoable("Odd path", stage, ["relative/path"], lambda: ran.append(1), stack=stack, commands=None)
    assert ran == [1] and len(stack) == 0 and "not undoable" in stack.notes[-1]


# --- a fake omni.kit.commands / omni.kit.undo pair -------------------------------------------------------------


def _fake_kit(run_do=True):
    log = types.SimpleNamespace(registered=[], unregistered=[], executed=[], undo_stack=[], redo_stack=[])
    commands = types.ModuleType("fake_omni_kit_commands")
    registry = {}

    class Command:
        def do(self):
            raise NotImplementedError

        def undo(self):
            pass

    def register(cls):
        name = cls.__name__[: -len("Command")] if cls.__name__.endswith("Command") else cls.__name__
        registry[name] = cls
        log.registered.append(cls)

    def unregister(cls):
        for name, value in list(registry.items()):
            if value is cls:
                del registry[name]
        log.unregistered.append(cls)

    def execute(name, **kwargs):
        log.executed.append((name, kwargs))
        cls = registry.get(name)
        if cls is None or not run_do:
            return (False, None)
        command = cls(**kwargs)
        result = command.do()
        log.undo_stack.append(types.SimpleNamespace(command=command, name=name, level=0))
        log.redo_stack.clear()
        return (True, result)

    commands.Command, commands.register, commands.unregister, commands.execute = Command, register, unregister, execute
    kit_undo = types.ModuleType("fake_omni_kit_undo")

    def get_undo_stack():
        return log.undo_stack

    def undo_once():
        entry = log.undo_stack.pop()
        entry.command.undo()
        log.redo_stack.append(entry)

    def redo_once():
        entry = log.redo_stack.pop()
        entry.command.do()
        log.undo_stack.append(entry)

    kit_undo.get_undo_stack, kit_undo.undo, kit_undo.redo = get_undo_stack, undo_once, redo_once
    return commands, kit_undo, log


def test_run_undoable_goes_through_kit_commands_and_ctrl_z_restores():
    stage = _scene()
    commands, kit_undo, log = _fake_kit()
    stack = UndoStack()
    before = _text(stage)
    kit_commands.run_undoable("Delete /World/B", stage, ["/World/B"], lambda: stage.RemovePrim("/World/B"), stack=stack, commands=commands)
    after = _text(stage)
    assert [name for name, _ in log.executed] == ["MCPEdit"]
    kwargs = log.executed[0][1]
    assert kwargs["label"] == "Delete /World/B" and kwargs["paths"] == ["/World/B"]
    assert len(log.registered) == 1 and log.registered[0].__name__ == "MCPEditCommand"
    assert len(stack) == 1 and stack.peek().via == "kit" and repr(kwargs["edit"]) == "<MCP edit 'Delete /World/B'>"

    kit_undo.undo()  # Ctrl+Z in Kit
    assert _text(stage) == before and len(stack) == 0
    kit_undo.redo()  # Ctrl+Y in Kit
    assert _text(stage) == after and len(stack) == 1

    result = kit_commands.undo_last(stage, dry_run=False, stack=stack, undo_module=kit_undo)
    assert result["ok"] and result["via"] == "kit_undo" and _text(stage) == before
    assert log.undo_stack == [] and len(log.redo_stack) == 1  # Kit's own history moved too
    json.dumps(result)


def test_kit_undo_last_falls_back_when_a_newer_kit_edit_is_on_top():
    stage = _scene()
    commands, kit_undo, log = _fake_kit()
    stack = UndoStack()
    before = _text(stage)
    kit_commands.run_undoable("Create /World/N", stage, ["/World/N"], lambda: stage.DefinePrim("/World/N"), stack=stack, commands=commands)
    foreign = types.SimpleNamespace(command=types.SimpleNamespace(undo=lambda: None), level=0)
    log.undo_stack.append(foreign)  # a UI edit on another prim, newer in Kit's history
    result = kit_commands.undo_last(stage, dry_run=False, stack=stack, undo_module=kit_undo)
    assert result["ok"] and result["via"] == "mcp_snapshot" and "kit_note" in result
    assert _text(stage) == before and log.undo_stack[-1] is foreign
    log.undo_stack.pop()
    kit_undo.undo()  # Ctrl+Z reaches the MCP command later: already undone, so nothing happens
    assert _text(stage) == before


def test_undo_of_an_edit_on_a_prim_holding_nan_values_is_not_refused_as_changed():
    nan = float("nan")
    for kind, type_name, value, later in (
        ("scalar", Sdf.ValueTypeNames.Float, nan, 1.0),
        ("float array", Sdf.ValueTypeNames.FloatArray, [1.0, nan], [2.0, nan]),
        ("float3", Sdf.ValueTypeNames.Float3, Gf.Vec3f(nan, 0.0, 1.0), Gf.Vec3f(nan, 0.0, 2.0)),
    ):
        stage = _scene()
        prim = stage.GetPrimAtPath("/World/C")
        prim.CreateAttribute("weight", type_name).Set(value)
        before = _text(stage)
        stack = UndoStack()
        size = prim.GetAttribute("size")
        kit_commands.run_undoable("Set size", stage, [], lambda: size.Set(9.0), shallow=["/World/C"], stack=stack,
                                  commands=None)
        assert stack.undo_last(stage=stage, dry_run=True)["ok"] is True, kind
        result = stack.undo_last(stage=stage, dry_run=False)
        assert result["ok"] is True and result["verified"] is True, (kind, result)
        assert _text(stage) == before, kind

        # A real later change to the NaN-holding value is still refused.
        kit_commands.run_undoable("Set size", stage, [], lambda: size.Set(9.0), shallow=["/World/C"], stack=stack,
                                  commands=None)
        prim.GetAttribute("weight").Set(later)
        assert stack.undo_last(stage=stage, dry_run=False)["refused"] == "changed", kind
    assert undo._equal(nan, nan) and not undo._equal(nan, 1.0) and not undo._equal(1.0, 2.0)
    assert not undo._equal(Gf.Vec3f(0.1, 0, 0), Gf.Vec3d(0.1, 0, 0))  # different types stay different


def test_ctrl_z_never_overwrites_changes_made_outside_kits_undo_history():
    # A script (raw USD, no Kit command) edits the same prim after the MCP edit: Ctrl+Z skips the MCP edit.
    stage = _scene()
    commands, kit_undo, log = _fake_kit()
    stack = UndoStack()
    size = stage.GetPrimAtPath("/World/A").GetAttribute("size")
    kit_commands.run_undoable("Set size", stage, ["/World/A"], lambda: size.Set(5.0), stack=stack, commands=commands)
    stage.GetPrimAtPath("/World/A").CreateAttribute("note", Sdf.ValueTypeNames.String).Set("keep me")
    stage.DefinePrim("/World/A/Notes")
    edited = _text(stage)
    assert stack.undo_last(stage=stage, dry_run=False)["refused"] == "changed"
    kit_undo.undo()  # Ctrl+Z in Kit
    assert _text(stage) == edited  # nothing overwritten: the note, the child and size 5 all stay
    assert stage.GetPrimAtPath("/World/A").GetAttribute("note").Get() == "keep me" and size.Get() == 5.0
    assert "Ctrl+Z skipped 'Set size'" in stack.notes[-1] and len(stack) == 1 and stack.peek().state == "done"

    # Later edits recorded by Kit are undone first in Kit's order, so Ctrl+Z still reaches and undoes the MCP edit.
    stage = _scene()
    commands, kit_undo, log = _fake_kit()
    stack = UndoStack()
    before = _text(stage)
    size = stage.GetPrimAtPath("/World/A").GetAttribute("size")
    kit_commands.run_undoable("Set size", stage, ["/World/A"], lambda: size.Set(5.0), stack=stack, commands=commands)
    after = _text(stage)
    size.Set(8.0)  # a Kit ChangeProperty-like command on the same attribute
    log.undo_stack.append(types.SimpleNamespace(command=types.SimpleNamespace(undo=lambda: size.Set(5.0)), level=0))
    kit_undo.undo()
    assert _text(stage) == after
    kit_undo.undo()
    assert _text(stage) == before and len(stack) == 0


def test_registration_is_idempotent_across_a_hot_reload():
    commands, kit_undo, log = _fake_kit()
    first = kit_commands.ensure_registered(commands)
    assert kit_commands.ensure_registered(commands) is first and len(log.registered) == 1
    stage = _scene()
    stack = UndoStack()
    before = _text(stage)
    kit_commands.run_undoable("Create /World/N", stage, ["/World/N"], lambda: stage.DefinePrim("/World/N"), stack=stack, commands=commands)

    kit_commands._COMMAND_CLASSES.pop(id(commands))  # what a reloaded copy of the module starts with
    second = kit_commands.ensure_registered(commands)
    assert second is not first and log.unregistered == [first] and log.registered[-1] is second
    kit_undo.undo()  # the command recorded by the old class still undoes
    assert _text(stage) == before
    assert kit_commands.unregister(commands) is True and kit_commands.unregister(commands) is False
    assert kit_commands.ensure_registered(None) is None


def test_kit_that_never_runs_the_command_falls_back_to_the_mcp_stack():
    stage = _scene()
    commands, _kit_undo, log = _fake_kit(run_do=False)
    stack = UndoStack()
    kit_commands.run_undoable("Create /World/N", stage, ["/World/N"], lambda: stage.DefinePrim("/World/N"), stack=stack, commands=commands)
    assert len(log.executed) == 1 and len(stack) == 1 and stack.peek().via == "direct"
    assert "undo_last" in stack.notes[-1]
    assert stack.undo_last(stage=stage, dry_run=False)["ok"] and not stage.GetPrimAtPath("/World/N").IsValid()

    broken = types.ModuleType("broken_commands")  # no Command base class: registration fails cleanly
    kit_commands.run_undoable("Create /World/M", stage, ["/World/M"], lambda: stage.DefinePrim("/World/M"), stack=stack, commands=broken)
    assert stack.peek().via == "direct" and "registration failed" in stack.notes[-1]


def test_failed_edit_never_reaches_kit():
    stage = _scene()
    commands, _kit_undo, log = _fake_kit()
    stack = UndoStack()

    def refuse():
        raise ValueError("Prim does not exist: /Nope")

    try:
        kit_commands.run_undoable("Move /Nope", stage, [], refuse, shallow=["/World/A"], stack=stack, commands=commands)
    except ValueError:
        pass
    else:
        raise AssertionError("the refusal should propagate")
    assert log.executed == [] and len(stack) == 0


def test_undo_is_pure_pxr_and_kit_imports_are_guarded():
    """undo.py runs anywhere pxr does; kit_commands.py may import omni only inside try/except ImportError."""
    allowed = {
        undo: {"__future__", "math", "time", "weakref", "collections", "typing", "pxr"},
        kit_commands: {"__future__", "logging", "collections", "typing", "omni"},
    }
    for module, roots_allowed in allowed.items():
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        guarded = {id(node) for block in ast.walk(tree) if isinstance(block, ast.Try) for node in ast.walk(block)}
        roots = set()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module]
            for name in names:
                roots.add(name.split(".")[0])
                if name.split(".")[0] == "omni":
                    assert id(node) in guarded, (module.__name__, name)
        assert roots <= roots_allowed, (module.__name__, roots)
