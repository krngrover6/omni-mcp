"""Kit glue for undoable MCP edits: one omni.kit.commands command, so Ctrl+Z in Composer/Isaac undoes MCP edits.

run_undoable(label, stage, paths, fn) snapshots the specs an edit touches (undo.SpecSnapshot), runs fn, and
records an UndoEntry when the layers really changed. When Kit's command system is available it then executes the
'MCPEdit' command (class MCPEditCommand) so the edit sits on Kit's undo stack like a UI edit: Ctrl+Z restores the
before snapshot (unless its specs changed outside Kit's undo history, for example by a script; then Ctrl+Z skips
it and keeps those changes) and Ctrl+Y re-applies the after snapshot. The edit itself runs before the command is
executed, so
a refused edit (bad path, failed validation) raises to the caller as before and never reaches Kit's undo history
or its error log. Every recorded edit also goes on an UndoStack, which undo_last() pops.

Registration happens on first use and is idempotent across bridge hot-reloads: the registered class is remembered
on the omni.kit.commands module itself, so a reloaded copy of this module unregisters the previous class before
registering its own. The command class is a thin shim around its edit object, so commands an older copy put on
Kit's undo stack keep working. Nothing here imports omni unless it is importable; without Kit edits run directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from . import undo
from .undo import SAVE_NOTE, UndoEntry, UndoStack

try:
    import omni.kit.commands as _OMNI_KIT_COMMANDS
except ImportError:  # pragma: no cover - only available inside Kit
    _OMNI_KIT_COMMANDS = None

try:
    import omni.kit.undo as _OMNI_KIT_UNDO
except ImportError:  # pragma: no cover - only available inside Kit
    _OMNI_KIT_UNDO = None

__all__ = [
    "AUTO",
    "COMMAND_CLASS_NAME",
    "COMMAND_NAME",
    "UndoStack",
    "default_stack",
    "ensure_registered",
    "kit_available",
    "run_undoable",
    "undo_last",
    "unregister",
]

T = TypeVar("T")
COMMAND_CLASS_NAME = "MCPEditCommand"
COMMAND_NAME = "MCPEdit"  # Kit registers a command class under its name without the 'Command' suffix
_REGISTERED_ATTR = "_omniverse_scene_mcp_registered_command"
_LOG = logging.getLogger(__name__)


class _Auto:
    __slots__ = ()

    def __repr__(self) -> str:
        return "AUTO"


AUTO: Any = _Auto()
_STACK = UndoStack()
# One generated command class per commands module (keyed by id, with the module kept to rule out id reuse).
_COMMAND_CLASSES: dict[int, tuple[Any, type]] = {}


def default_stack() -> UndoStack:
    """The module-level stack run_undoable uses when no stack is passed."""
    return _STACK


def kit_available() -> bool:
    """True when omni.kit.commands was importable (inside Kit)."""
    return _OMNI_KIT_COMMANDS is not None


def _commands_module(commands: Any) -> Any:
    return _OMNI_KIT_COMMANDS if commands is AUTO else commands


def _undo_module(module: Any) -> Any:
    return _OMNI_KIT_UNDO if module is AUTO else module


def _command_class(commands: Any) -> type:
    cached = _COMMAND_CLASSES.get(id(commands))
    if cached is not None and cached[0] is commands:
        return cached[1]
    base = getattr(commands, "Command", None)
    if not isinstance(base, type):
        raise TypeError("omni.kit.commands has no Command base class")

    class MCPEditCommand(base):  # type: ignore[misc, valid-type]
        """Undo/redo one omniverse_scene_mcp edit that already ran (see omniverse_scene_mcp.kit_commands)."""

        def __init__(self, edit: Any = None, label: str = "MCP edit", paths: Any = None, **_unused: Any) -> None:
            try:
                super().__init__()
            except TypeError:  # a base __init__ that wants arguments; this shim needs none of its state
                pass
            self._edit = edit
            self.label = str(label)
            self.paths = list(paths or [])

        def do(self) -> None:
            if self._edit is not None:
                self._edit.do()

        def undo(self) -> None:
            if self._edit is not None:
                self._edit.undo()

    MCPEditCommand.__name__ = COMMAND_CLASS_NAME
    MCPEditCommand.__qualname__ = COMMAND_CLASS_NAME
    _COMMAND_CLASSES[id(commands)] = (commands, MCPEditCommand)
    return MCPEditCommand


def ensure_registered(commands: Any = AUTO) -> type | None:
    """Register MCPEditCommand with commands (default: omni.kit.commands); None without Kit. Idempotent.

    A class registered by an earlier copy of this module (before a bridge hot-reload) is unregistered first.
    """
    module = _commands_module(commands)
    if module is None:
        return None
    cls = _command_class(module)
    current = getattr(module, _REGISTERED_ATTR, None)
    if current is cls:
        return cls
    if current is not None:
        try:
            module.unregister(current)
        except Exception as exc:  # noqa: BLE001 - registering the new class replaces it anyway
            _LOG.warning("Could not unregister the previous %s: %s", COMMAND_CLASS_NAME, exc)
    module.register(cls)
    try:
        setattr(module, _REGISTERED_ATTR, cls)
    except Exception:  # noqa: BLE001 - a module that refuses attributes just re-registers next time
        pass
    return cls


def unregister(commands: Any = AUTO) -> bool:
    """Remove the registered MCPEditCommand from commands; True when one was removed."""
    module = _commands_module(commands)
    current = getattr(module, _REGISTERED_ATTR, None) if module is not None else None
    if current is None:
        return False
    try:
        module.unregister(current)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("Could not unregister %s: %s", COMMAND_CLASS_NAME, exc)
        return False
    try:
        delattr(module, _REGISTERED_ATTR)
    except Exception:  # noqa: BLE001
        pass
    return True


class _Plan:
    """Which specs one edit touches. Holds the stage only until the edit has run (release())."""

    def __init__(self, stage: Any, paths: Iterable[Any], shallow: Iterable[Any], namespace_edit: bool) -> None:
        self.stage = stage
        self.paths = _unique(paths)
        self.shallow = _unique(shallow)
        self.namespace_edit = bool(namespace_edit)
        try:
            self.stage_ref: undo.LayerRef | None = undo.LayerRef(stage.GetRootLayer())
        except Exception:  # noqa: BLE001 - a stage without a root layer cannot be checked later
            self.stage_ref = None

    def capture(self) -> list[undo.SpecSnapshot]:
        stage = self.stage
        target = stage.GetEditTarget()
        edit_layer = target.GetLayer()
        if edit_layer is None:
            raise undo.UndoError("The stage has no edit target layer")
        mapped = [target.MapToSpecPath(undo.prim_path(path)) for path in self.paths]
        mapped_shallow = [target.MapToSpecPath(undo.prim_path(path)) for path in self.shallow]
        layers = [edit_layer]
        if self.namespace_edit:
            # Usd.NamespaceEditor edits every layer of the layer stack that has specs for the prim.
            for layer in stage.GetLayerStack(includeSessionLayers=True):
                if all(layer.identifier != known.identifier for known in layers):
                    layers.append(layer)
        root_path = undo.Sdf.Path.absoluteRootPath
        snapshots: list[undo.SpecSnapshot] = []
        for layer in layers:
            is_edit = layer.identifier == edit_layer.identifier
            full = mapped if is_edit else [undo.prim_path(path) for path in self.paths]
            shallow = list(mapped_shallow if is_edit else [undo.prim_path(path) for path in self.shallow])
            keys: tuple[str, ...] = ()
            if self.namespace_edit:
                dependents = undo.find_dependents(layer, full)
                if not is_edit and not dependents and not any(layer.GetObjectAtPath(path) for path in full):
                    continue
                shallow.extend(dependents)
                # A rename may also rewrite the parent's 'reorder nameChildren' list.
                for path in full:
                    parent_path = path.GetParentPath()
                    parent = layer.GetPrimAtPath(parent_path) if parent_path != root_path else None
                    if parent and parent.HasInfo("primOrder"):
                        shallow.append(parent_path)
                keys = undo.NAMESPACE_LAYER_KEYS
            snapshots.append(undo.SpecSnapshot.capture(layer, full, shallow=shallow, layer_keys=keys))
        return snapshots

    def release(self) -> None:
        self.stage = None


def _unique(paths: Iterable[Any]) -> list[str]:
    seen: list[str] = []
    for path in paths or ():
        text = str(path)
        if text and text not in seen:
            seen.append(text)
    return seen


def _apply(label: str, plan: _Plan, fn: Callable[[], T], stack: UndoStack) -> tuple[UndoEntry | None, T]:
    """Snapshot, run fn, snapshot again. A failing fn is rolled back and its exception re-raised."""
    try:
        before = plan.capture()
    except Exception as exc:  # noqa: BLE001 - never block an edit because its undo record failed
        plan.release()
        result = fn()
        stack.note(f"'{label}' ran but is not undoable: its snapshot failed ({type(exc).__name__}: {exc})")
        return None, result
    try:
        result = fn()
    except BaseException:
        _rollback(before, label, stack)
        plan.release()
        raise
    try:
        after = [snapshot.recapture() for snapshot in before]
        entry: UndoEntry | None = UndoEntry(label, plan.paths, before, after, stage_ref=plan.stage_ref)
    except Exception as exc:  # noqa: BLE001
        stack.note(f"'{label}' ran but is not undoable: the result could not be recorded ({type(exc).__name__}: {exc})")
        entry = None
    plan.release()
    return entry, result


def _rollback(before: list[undo.SpecSnapshot], label: str, stack: UndoStack) -> None:
    """Put back whatever a failed edit changed; never hides the edit's own exception."""
    for snapshot in before:
        try:
            if not snapshot.matches():
                snapshot.restore()
        except Exception as exc:  # noqa: BLE001
            stack.note(f"'{label}' failed and could not be fully rolled back ({type(exc).__name__}: {exc})")


class _KitEdit:
    """What one MCPEditCommand on Kit's undo stack undoes and redoes. Holds snapshots, never the stage."""

    def __init__(self, entry: UndoEntry, stack: UndoStack) -> None:
        self.entry = entry
        self.applied = False
        self._stack = stack

    def do(self) -> None:
        if not self.applied:  # Kit's execute(): the edit already ran, so just record it
            self.applied = True
            self.entry.via = "kit"
            self.entry.kit_edit = self
            self._stack.push(self.entry)
            return
        try:  # Kit's redo (Ctrl+Y)
            self.entry.redo()
        except Exception as exc:  # noqa: BLE001 - Kit's redo must not crash the app
            self._stack.note(f"Redo of '{self.entry.label}' failed: {exc}")
            _LOG.warning("Redo of '%s' failed: %s", self.entry.label, exc)

    def undo(self) -> None:
        # Never force: when the edit's specs changed outside Kit's undo history (a script, OmniGraph, raw USD),
        # Ctrl+Z skips this edit instead of overwriting those changes. Later edits made through Kit commands are
        # undone first in Kit's own order, so they put the specs back to this edit's 'after' state and do not block.
        try:
            self.entry.undo()
        except undo.UndoRefused as exc:
            self._stack.note(f"Ctrl+Z skipped '{self.entry.label}' and changed nothing: {exc}")
            _LOG.warning(
                "Ctrl+Z skipped '%s' and changed nothing: %s (scene_undo_last(dry_run=True) explains why)",
                self.entry.label,
                exc,
            )
        except Exception as exc:  # noqa: BLE001 - e.g. the stage was closed; Kit's Ctrl+Z must not crash the app
            self._stack.note(f"Ctrl+Z could not undo '{self.entry.label}': {exc}")
            _LOG.warning("Ctrl+Z could not undo '%s': %s", self.entry.label, exc)

    def __copy__(self) -> _KitEdit:
        return self

    def __deepcopy__(self, _memo: Any) -> _KitEdit:
        return self

    def __repr__(self) -> str:
        return f"<MCP edit {self.entry.label!r}>"


def _record_in_kit(module: Any, entry: UndoEntry, stack: UndoStack) -> bool:
    try:
        ensure_registered(module)
    except Exception as exc:  # noqa: BLE001
        stack.note(f"Kit command registration failed ({type(exc).__name__}: {exc}); '{entry.label}' is undoable only with undo_last.")
        return False
    edit = _KitEdit(entry, stack)
    try:
        module.execute(COMMAND_NAME, edit=edit, label=entry.label, paths=list(entry.paths[: undo.MAX_LISTED_PATHS]))
    except Exception as exc:  # noqa: BLE001
        if not edit.applied:
            stack.note(f"Kit could not record '{entry.label}' ({type(exc).__name__}: {exc}); it is undoable only with undo_last.")
            return False
        stack.note(f"Kit recorded '{entry.label}' but then raised ({type(exc).__name__}: {exc}).")
    if not edit.applied:
        stack.note(f"Kit did not run the {COMMAND_NAME} command; '{entry.label}' is undoable only with undo_last.")
        return False
    return True


def run_undoable(
    label: str,
    stage: Any,
    paths: Iterable[Any],
    fn: Callable[[], T],
    *,
    shallow: Iterable[Any] = (),
    namespace_edit: bool = False,
    stack: UndoStack | None = None,
    commands: Any = AUTO,
) -> T:
    """Run fn() as one undoable edit and return its result.

    paths: prims whose whole spec subtree the edit may create, change or remove; shallow: prims whose own fields
    and properties it changes (children untouched). namespace_edit (rename/reparent) also snapshots every layer of
    the layer stack, prims whose targets/arcs point at the paths, and layer metadata such as defaultPrim.
    commands: an omni.kit.commands-like module (AUTO: the real one when importable, None: never use Kit).
    If fn raises, its changes are rolled back and the exception propagates; nothing is recorded. An edit that
    changed nothing is not recorded either.
    """
    stack = _STACK if stack is None else stack
    label = undo.clip(label)
    if undo.Sdf is None or stage is None:
        return fn()
    try:
        plan = _Plan(stage, paths, shallow, namespace_edit)
    except Exception as exc:  # noqa: BLE001
        stack.note(f"'{label}' could not be prepared for undo ({type(exc).__name__}: {exc})")
        return fn()
    entry, result = _apply(label, plan, fn, stack)
    if entry is None or not entry.changed:
        return result
    module = _commands_module(commands)
    if module is None or not _record_in_kit(module, entry, stack):
        entry.via = "direct"
        stack.push(entry)
    return result


def _kit_top_is(module: Any, entry: UndoEntry) -> bool:
    """True when the newest entry on Kit's undo stack is this edit's MCPEditCommand at the top level."""
    getter = getattr(module, "get_undo_stack", None)
    edit = entry.kit_edit
    if not callable(getter) or edit is None:
        return False
    try:
        items = getter()
        top = items[-1] if len(items) else None
    except Exception:  # noqa: BLE001 - an omni.kit.undo whose internals differ
        return False
    command = getattr(top, "command", None)
    level = getattr(top, "level", 0)
    return command is not None and getattr(command, "_edit", None) is edit and level in (0, None)


def undo_last(
    stage: Any = None,
    *,
    dry_run: bool = True,
    stack: UndoStack | None = None,
    undo_module: Any = AUTO,
) -> dict[str, Any]:
    """Undo (dry_run=True, the default: only preview) the newest recorded MCP edit on stage.

    When that edit is also the newest entry on Kit's undo stack, it is undone through omni.kit.undo (exactly like
    Ctrl+Z, so Ctrl+Y can redo it). Otherwise its snapshot is restored directly; Kit's history then still lists the
    command, and Ctrl+Z on it does nothing. Refuses when the stage was replaced or the specs changed since.
    """
    stack = _STACK if stack is None else stack
    dry = dry_run is not False
    entry = stack.peek()
    if dry or entry is None or entry.via != "kit":
        result = stack.undo_last(stage=stage, dry_run=dry)
        if result.get("ok") and not dry:
            result["via"] = "mcp_snapshot"
        return result
    module = _undo_module(undo_module)
    if module is not None and entry.problem(stage) is None and _kit_top_is(module, entry):
        try:
            module.undo()
        except Exception as exc:  # noqa: BLE001
            stack.note(f"omni.kit.undo.undo() failed for '{entry.label}': {exc}")
        if entry.state == "undone":
            return {
                "ok": True,
                "dry_run": False,
                "undone": entry.summary(),
                "verified": bool(entry.last_verified),
                "via": "kit_undo",
                "message": "Undone through Kit's undo history, exactly like Ctrl+Z; Ctrl+Y in Kit redoes it.",
                "remaining": len(stack),
                "note": SAVE_NOTE,
            }
    result = stack.undo_last(stage=stage, dry_run=False)
    if result.get("ok"):
        result["via"] = "mcp_snapshot"
        result["kit_note"] = (
            "Kit's undo history still lists this edit below newer Kit edits; Ctrl+Z on it now does nothing."
        )
    return result
