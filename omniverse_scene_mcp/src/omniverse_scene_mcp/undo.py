"""Exact undo for MCP scene edits: Sdf spec snapshots and a bounded undo stack (pure pxr, no omni imports).

SpecSnapshot.capture(layer, paths) copies each affected prim spec subtree of one layer into a private anonymous
layer, or records that it was absent. restore() puts exactly those specs back: specs that did not exist are
removed, copied ones come back at their old position among their siblings, and every other spec is left alone.
"shallow" paths keep only a prim's own fields and properties (a move, a material binding, a variant selection),
so a one-attribute edit on a big prim does not copy its whole subtree.

Snapshots and entries never hold the stage or the user's layers. A layer is found again through a weak reference
to its Python wrapper (pxr keeps one wrapper per live layer), so closing a stage in Kit still frees its layers,
and an entry whose layer was closed or replaced refuses to undo. undo_last also refuses when the affected specs
changed after the edit (someone edited them in Kit since). Kit's Ctrl+Z applies the same check, but by then
Kit has already undone its own later commands, so only changes made outside Kit's undo history (a script,
OmniGraph) make Ctrl+Z skip an MCP edit. Saving is not undoable: undo changes the open layers in memory; save again to write the reverted state.
"""

from __future__ import annotations

import math
import time
import weakref
from collections import deque
from collections.abc import Iterable
from typing import Any

try:
    from pxr import Sdf
except ImportError as exc:  # pragma: no cover - only available with USD bindings
    Sdf = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: Exception | None = exc
else:
    _PXR_IMPORT_ERROR = None

DEFAULT_MAX_ENTRIES = 50
MAX_ENTRIES_LIMIT = 500
MAX_ROOTS = 5000
MAX_DEPENDENTS = 5000
MAX_LABEL_CHARS = 200
MAX_LISTED_PATHS = 20
MAX_HISTORY = 50
MAX_NOTES = 10
SAVE_NOTE = "Saving is not undoable: undo changes the open layers in memory; save again to write the reverted state."
NOTHING_TO_UNDO = "Nothing to undo: no MCP edit on the open stage is recorded (saves, dry runs and no-op edits are not recorded)."
# Layer metadata a rename/reparent may rewrite (Usd.NamespaceEditor updates defaultPrim, root prim order, relocates).
NAMESPACE_LAYER_KEYS: tuple[str, ...] = ("defaultPrim", "primOrder", "layerRelocates")
_PROPERTY_PATH_FIELDS = {"relationship": "targetPaths", "attribute": "connectionPaths"}
_PRIM_PATH_FIELDS = ("inheritPaths", "specializes")
_PRIM_ARC_FIELDS = ("references", "payload")
_LIST_OP_ITEMS = ("explicitItems", "addedItems", "prependedItems", "appendedItems", "deletedItems", "orderedItems")


class UndoError(ValueError):
    """A snapshot could not be taken or put back."""


class UndoRefused(UndoError):
    """An undo was refused; kind is 'stale', 'changed', 'blocked' or 'undone'."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _Absent:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<absent>"


_ABSENT = _Absent()


def _require_pxr() -> None:
    if Sdf is None:
        raise UndoError(f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}")


def clip(text: Any, limit: int = MAX_LABEL_CHARS) -> str:
    """One-line text of at most limit characters."""
    value = " ".join(str(text).split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


class LayerRef:
    """Find a layer again without keeping it alive; get() is None once it was closed or replaced."""

    __slots__ = ("identifier", "_ref")

    def __init__(self, layer: Any) -> None:
        self.identifier = str(layer.identifier)
        try:
            self._ref: Any = weakref.ref(layer)
        except TypeError:  # pragma: no cover - pxr wrappers support weak references
            self._ref = None

    def get(self) -> Any:
        if self._ref is None:  # pragma: no cover - fallback without weak references
            layer = Sdf.Layer.Find(self.identifier)
            return layer if layer is not None and not layer.expired else None
        layer = self._ref()
        if layer is None or getattr(layer, "expired", False):
            return None
        found = Sdf.Layer.Find(self.identifier)
        if found is not None and found is not layer:
            return None  # another layer now answers to this identifier
        return layer

    def is_layer(self, layer: Any) -> bool:
        live = self.get()
        return live is not None and layer is not None and live is layer


def prim_path(value: Any) -> Any:
    """value as an absolute prim Sdf.Path; UndoError for anything else."""
    if isinstance(value, Sdf.Path):
        path = value
    else:
        try:
            path = Sdf.Path(str(value))
        except Exception as exc:  # noqa: BLE001 - Tf errors for malformed text
            raise UndoError(f"Not a valid USD path: {clip(value, 120)!r}") from exc
    if (
        path.isEmpty
        or not path.IsAbsolutePath()
        or path == Sdf.Path.absoluteRootPath
        or not (path.IsPrimPath() or path.IsPrimVariantSelectionPath())
    ):
        raise UndoError(f"Undo snapshots need absolute prim paths such as /World/Box, not {clip(value, 120)!r}")
    return path


def _spec_root(layer: Any, path: Any) -> tuple[Any, bool]:
    """(path, True) when the layer has a spec there, else (its shallowest missing ancestor, False)."""
    if layer.GetObjectAtPath(path):
        return path, True
    top = path
    parent = top.GetParentPath()
    while not parent.isEmpty and parent != Sdf.Path.absoluteRootPath and not layer.GetObjectAtPath(parent):
        top = parent
        parent = top.GetParentPath()
    return top, False


def _plan_roots(layer: Any, paths: Iterable[Any], shallow: Iterable[Any]) -> list[tuple[Any, bool]]:
    """Normalize to (root, shallow) pairs: no root lies under a full root; a full root wins over a shallow one."""
    modes: dict[Any, bool] = {}
    for raw in paths:
        root, _present = _spec_root(layer, prim_path(raw))
        modes[root] = False
    for raw in shallow:
        root, present = _spec_root(layer, prim_path(raw))
        if present:
            modes.setdefault(root, True)
        else:
            modes[root] = False  # a missing prim is created wholesale by the edit, so remove it wholesale
    if len(modes) > MAX_ROOTS:
        raise UndoError(f"An undo snapshot is limited to {MAX_ROOTS} prims; this edit touches {len(modes)}.")
    full = {path for path, is_shallow in modes.items() if not is_shallow}
    kept = [
        (path, is_shallow)
        for path, is_shallow in modes.items()
        if not any(prefix in full for prefix in path.GetPrefixes()[:-1])
    ]
    return sorted(kept, key=lambda item: str(item[0]))


def _child_names(layer: Any, parent: Any) -> list[str] | None:
    spec = layer.pseudoRoot if parent == Sdf.Path.absoluteRootPath else layer.GetPrimAtPath(parent)
    if not spec:
        return None
    try:
        return [str(name) for name in spec.nameChildren.keys()]
    except Exception:  # noqa: BLE001 - e.g. a variant spec parent
        return None


def _info(spec: Any) -> tuple[tuple[str, Any], ...]:
    return tuple((key, spec.GetInfo(key)) for key in sorted(spec.ListInfoKeys()))


_CHILD_LISTS = ("nameChildren", "properties", "variantSets", "variants")


def _child_order(spec: Any) -> tuple[tuple[str, tuple[str, ...]], ...]:
    order = []
    for name in _CHILD_LISTS:
        try:
            children = getattr(spec, name, None)
            if children is not None:
                order.append((name, tuple(str(key) for key in children.keys())))
        except Exception:  # noqa: BLE001 - a spec type without that child list
            continue
    return tuple(order)


def _subtree_state(layer: Any, root: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Every spec under root (prims, properties, targets, variants) with its fields, plus each spec's ordered
    child names. Keyed by path: Traverse's visiting order depends on how the children fields happen to be stored
    (CopySpec stores them differently), so only the child lists themselves carry order."""
    infos: dict[str, Any] = {}
    orders: dict[str, Any] = {}

    def visit(path: Any) -> None:
        spec = layer.GetObjectAtPath(path)
        if spec:
            key = str(path)
            infos[key] = _info(spec)
            order = _child_order(spec)
            if order:
                orders[key] = order

    layer.Traverse(root, visit)
    return infos, orders


def _properties_state(layer: Any, path: Any) -> tuple[tuple[str, ...], list[Any]]:
    spec = layer.GetPrimAtPath(path)
    names = tuple(str(name) for name in spec.properties.keys())
    return names, [_subtree_state(layer, path.AppendProperty(name)) for name in names]


def _shallow_state(layer: Any, path: Any) -> tuple[Any, ...]:
    spec = layer.GetPrimAtPath(path)
    return (_info(spec), _properties_state(layer, path))


def _equal(first: Any, second: Any) -> bool:
    """Structural equality that tolerates pxr values (Vt arrays, list ops, Gf types)."""
    if isinstance(first, (list, tuple)) and isinstance(second, (list, tuple)):
        return len(first) == len(second) and all(_equal(a, b) for a, b in zip(first, second))
    if isinstance(first, dict) and isinstance(second, dict):
        return first.keys() == second.keys() and all(_equal(first[key], second[key]) for key in first)
    if first is second:
        return True
    if isinstance(first, float) and isinstance(second, float) and math.isnan(first) and math.isnan(second):
        return True  # GetInfo returns a fresh float each time, and nan == nan is False
    try:
        same = first == second
        same = same if isinstance(same, bool) else bool(same)
    except Exception:  # noqa: BLE001 - values that cannot be compared count as different
        return False
    if same or type(first) is not type(second) or type(first).__module__ == "builtins":
        return same
    # Two values of the same pxr type (Vt arrays, Gf types) that compare unequal: a NaN element never equals
    # itself, so compare the exact reprs instead; any real difference (even one float32 ulp) still shows there.
    try:
        return repr(first) == repr(second)
    except Exception:  # noqa: BLE001
        return False


def _remove_spec(layer: Any, path: Any) -> None:
    edit = Sdf.BatchNamespaceEdit()
    edit.Add(Sdf.NamespaceEdit.Remove(path))
    if not layer.Apply(edit):
        raise UndoError(f"Could not remove {path} from {layer.identifier}")


def _free_name(layer: Any, parent: Any, name: str) -> Any:
    for index in range(1000):
        candidate = parent.AppendChild(f"{name}_mcpUndo{index}")
        if not layer.GetObjectAtPath(candidate):
            return candidate
    raise UndoError(f"No free temporary name next to {parent.AppendChild(name)}")


class _Root:
    __slots__ = ("path", "shallow", "present", "siblings")

    def __init__(self, path: Any, shallow: bool, present: bool, siblings: tuple[str, ...]) -> None:
        self.path = path
        self.shallow = shallow
        self.present = present
        self.siblings = siblings


class SpecSnapshot:
    """The specs at a few prim paths of one layer, frozen so they can be put back exactly."""

    def __init__(self, layer: Any, roots: list[_Root], store: Any, keys: dict[str, Any]) -> None:
        self._ref = LayerRef(layer)
        self._roots = roots
        self._store = store
        self._keys = keys
        self._states: dict[Any, Any] = {}

    @classmethod
    def capture(
        cls,
        layer: Any,
        paths: Iterable[Any] = (),
        *,
        shallow: Iterable[Any] = (),
        layer_keys: Iterable[str] = (),
    ) -> SpecSnapshot:
        """Record each path's spec subtree (shallow: its prim fields and properties only) or that it is absent.

        A path the layer lacks is recorded at its shallowest missing ancestor, because creating it also writes
        'over' specs for missing ancestors. layer_keys are layer metadata fields (e.g. defaultPrim) to record.
        """
        _require_pxr()
        if layer is None or getattr(layer, "expired", True):
            raise UndoError("The layer to snapshot is not open")
        return cls._take(layer, _plan_roots(layer, paths, shallow), tuple(layer_keys))

    @classmethod
    def _take(cls, layer: Any, plan: list[tuple[Any, bool]], layer_keys: tuple[str, ...]) -> SpecSnapshot:
        store = Sdf.Layer.CreateAnonymous("mcp-undo-snapshot")
        roots: list[_Root] = []
        for path, shallow in plan:
            if not layer.GetObjectAtPath(path):
                roots.append(_Root(path, shallow, False, ()))
                continue
            siblings = tuple(_child_names(layer, path.GetParentPath()) or ())
            _copy_into(layer, store, path, shallow)
            roots.append(_Root(path, shallow, True, siblings))
        keys: dict[str, Any] = {}
        pseudo = layer.pseudoRoot
        for key in layer_keys:
            try:
                keys[key] = pseudo.GetInfo(key) if pseudo.HasInfo(key) else _ABSENT
            except Exception:  # noqa: BLE001 - a field this USD build does not know
                continue
        return cls(layer, roots, store, keys)

    # --- queries -----------------------------------------------------------------------------------------

    @property
    def layer_identifier(self) -> str:
        return self._ref.identifier

    @property
    def paths(self) -> list[str]:
        return [str(root.path) for root in self._roots]

    def live_layer(self) -> Any:
        """The snapshot's layer if it is still the same open layer, else None."""
        return self._ref.get()

    def describe(self) -> dict[str, Any]:
        roots = self._roots[:MAX_LISTED_PATHS]
        info: dict[str, Any] = {
            "layer": clip(self._ref.identifier, 300),
            "paths": [str(root.path) for root in roots],
            "absent": [str(root.path) for root in roots if not root.present],
            "shallow": [str(root.path) for root in roots if root.shallow and root.present],
        }
        if len(self._roots) > MAX_LISTED_PATHS:
            info["paths_truncated"] = len(self._roots) - MAX_LISTED_PATHS
        if self._keys:
            info["layer_metadata"] = sorted(self._keys)
        return info

    def matches(self, layer: Any = None) -> bool:
        """True when the layer's specs at these paths (and recorded metadata) equal this snapshot."""
        layer = self._resolve(layer)
        for root in self._roots:
            if not _equal(self._saved_state(root), _current_state(layer, root)):
                return False
        return all(_equal(value, _key_value(layer, key)) for key, value in self._keys.items())

    def same_state(self, other: SpecSnapshot) -> bool:
        """True when other (a capture of the same roots) holds the same content."""
        if [(r.path, r.shallow) for r in self._roots] != [(r.path, r.shallow) for r in other._roots]:
            return False
        for mine, theirs in zip(self._roots, other._roots):
            if mine.present != theirs.present or not _equal(self._saved_state(mine), other._saved_state(theirs)):
                return False
        return _equal(self._keys, other._keys)

    def restore_problem(self, layer: Any = None) -> str | None:
        """Why restore() would fail on the layer as it is now, or None."""
        try:
            layer = self._resolve(layer)
        except UndoRefused as refusal:
            return str(refusal)
        if not layer.permissionToEdit:
            return f"Layer {clip(layer.identifier, 200)} is read-only (permissionToEdit is False)."
        for root in self._roots:
            if not root.present:
                continue
            if root.shallow and not layer.GetPrimAtPath(root.path):
                return f"{root.path} no longer exists in {clip(layer.identifier, 200)}, so its old values cannot be put back."
            parent = root.path.GetParentPath()
            if parent != Sdf.Path.absoluteRootPath and not layer.GetObjectAtPath(parent):
                return f"The parent {parent} of {root.path} no longer exists in {clip(layer.identifier, 200)}."
        return None

    # --- restore -----------------------------------------------------------------------------------------

    def restore(self, layer: Any = None) -> list[str]:
        """Put the layer's specs at these paths back to this snapshot; return the paths that changed."""
        layer = self._resolve(layer)
        problem = self.restore_problem(layer)
        if problem:
            raise UndoRefused("blocked", problem)
        changed: list[str] = []
        with Sdf.ChangeBlock():
            # Deepest first, so a removed child never strands its parent half-removed.
            for root in sorted(self._roots, key=lambda r: -r.path.pathElementCount):
                if not root.present and layer.GetObjectAtPath(root.path):
                    _remove_spec(layer, root.path)
                    changed.append(str(root.path))
            full = [root for root in self._roots if root.present and not root.shallow]
            full.sort(key=lambda r: (str(r.path.GetParentPath()), _index(r)))
            for root in full:
                if _equal(self._saved_state(root), _current_state(layer, root)):
                    continue
                existed = bool(layer.GetObjectAtPath(root.path))
                if not Sdf.CopySpec(self._store, root.path, layer, root.path):
                    raise UndoError(f"Could not copy {root.path} back into {layer.identifier}")
                if not existed:
                    _restore_position(layer, root)
                changed.append(str(root.path))
            for root in self._roots:
                if root.present and root.shallow and not _equal(self._saved_state(root), _current_state(layer, root)):
                    _restore_shallow(layer, self._store, root.path)
                    changed.append(str(root.path))
            pseudo = layer.pseudoRoot
            for key, value in self._keys.items():
                if _equal(value, _key_value(layer, key)):
                    continue
                if value is _ABSENT:
                    pseudo.ClearInfo(key)
                else:
                    pseudo.SetInfo(key, value)
                changed.append(f"layer metadata {key}")
        return changed

    def recapture(self) -> SpecSnapshot:
        """A new snapshot of the same paths, modes and metadata, as the layer is now."""
        layer = self._resolve(None)
        return SpecSnapshot._take(layer, [(root.path, root.shallow) for root in self._roots], tuple(self._keys))

    # --- internals ---------------------------------------------------------------------------------------

    def _resolve(self, layer: Any) -> Any:
        live = self._ref.get()
        if live is None:
            raise UndoRefused(
                "stale",
                f"Layer {clip(self._ref.identifier, 200)} was closed or replaced after this edit, so it cannot be undone.",
            )
        if layer is not None and layer is not live:
            raise UndoRefused("stale", f"That is not the layer this snapshot was taken from ({clip(self._ref.identifier, 200)}).")
        return live

    def _saved_state(self, root: _Root) -> Any:
        if not root.present:
            return None
        if root.path not in self._states:
            self._states[root.path] = _current_state(self._store, root)
        return self._states[root.path]


def _index(root: _Root) -> int:
    name = root.path.name
    return root.siblings.index(name) if name in root.siblings else len(root.siblings)


def _current_state(layer: Any, root: _Root) -> Any:
    if not layer.GetObjectAtPath(root.path):
        return None
    if root.shallow:
        return _shallow_state(layer, root.path) if layer.GetPrimAtPath(root.path) else None
    return _subtree_state(layer, root.path)


def _key_value(layer: Any, key: str) -> Any:
    pseudo = layer.pseudoRoot
    try:
        return pseudo.GetInfo(key) if pseudo.HasInfo(key) else _ABSENT
    except Exception:  # noqa: BLE001
        return _ABSENT


def _copy_into(layer: Any, store: Any, path: Any, shallow: bool) -> None:
    parent = path.GetParentPath()
    if parent != Sdf.Path.absoluteRootPath and not store.GetObjectAtPath(parent):
        Sdf.CreatePrimInLayer(store, parent)
    if not shallow:
        if not Sdf.CopySpec(layer, path, store, path):
            raise UndoError(f"Could not snapshot {path}")
        return
    source = layer.GetPrimAtPath(path)
    if not source:
        raise UndoError(f"{path} is not a prim spec, so only its whole subtree can be recorded")
    target = Sdf.CreatePrimInLayer(store, path)
    for key in source.ListInfoKeys():
        target.SetInfo(key, source.GetInfo(key))
    for name in source.properties.keys():
        prop_path = path.AppendProperty(name)
        if not Sdf.CopySpec(layer, prop_path, store, prop_path):
            raise UndoError(f"Could not snapshot {prop_path}")


def _restore_shallow(layer: Any, store: Any, path: Any) -> None:
    spec = layer.GetPrimAtPath(path)
    saved = store.GetPrimAtPath(path)
    wanted = {key: saved.GetInfo(key) for key in saved.ListInfoKeys()}
    for key in spec.ListInfoKeys():
        if key not in wanted:
            spec.ClearInfo(key)
    for key, value in wanted.items():
        if not spec.HasInfo(key) or not _equal(spec.GetInfo(key), value):
            spec.SetInfo(key, value)
    if _equal(_properties_state(layer, path), _properties_state(store, path)):
        return
    # Re-adding every property in the saved order also restores the property order.
    for name in list(spec.properties.keys()):
        spec.RemoveProperty(spec.properties[name])
    for name in saved.properties.keys():
        prop_path = path.AppendProperty(name)
        if not Sdf.CopySpec(store, prop_path, layer, prop_path):
            raise UndoError(f"Could not restore {prop_path}")


def _restore_position(layer: Any, root: _Root) -> None:
    """Move a re-created spec back to its old place among its siblings (CopySpec appends it at the end)."""
    path = root.path
    if path.IsPrimVariantSelectionPath() or path.name not in root.siblings:
        return
    parent = path.GetParentPath()
    names = _child_names(layer, parent)
    if not names or path.name not in names:
        return
    others = {name for name in names if name != path.name}
    index = sum(1 for name in root.siblings[: root.siblings.index(path.name)] if name in others)
    if names.index(path.name) == index:
        return
    # Sdf cannot reorder a spec in place, so move it to a temporary sibling name at the index, then rename it
    # back (a rename keeps the position). Sdf moves leave target paths as they were, so the round trip is exact.
    temporary = _free_name(layer, parent, path.name)
    for edit_item in (Sdf.NamespaceEdit(path, temporary, index), Sdf.NamespaceEdit.Rename(temporary, path.name)):
        edit = Sdf.BatchNamespaceEdit()
        edit.Add(edit_item)
        if not layer.Apply(edit):
            raise UndoError(f"Could not move {path} back to position {index} under {parent}")


def _list_op_items(value: Any) -> list[Any]:
    items: list[Any] = []
    for name in _LIST_OP_ITEMS:
        try:
            items.extend(getattr(value, name, None) or ())
        except Exception:  # noqa: BLE001
            continue
    return items


def find_dependents(layer: Any, prefixes: Iterable[Any], *, limit: int = MAX_DEPENDENTS) -> list[str]:
    """Prims in layer whose targets, connections, inherits, specializes or internal references/payloads point at
    or under prefixes. Usd.NamespaceEditor rewrites those when it renames or reparents, so a rename snapshot must
    include them. Raises UndoError above limit prims."""
    _require_pxr()
    roots = [prim_path(prefix) for prefix in prefixes]
    owners: set[Any] = set()

    def points_inside(paths: Iterable[Any]) -> bool:
        return any(isinstance(p, Sdf.Path) and any(p.HasPrefix(root) for root in roots) for p in paths)

    def visit(path: Any) -> None:
        if path.IsPrimPropertyPath():
            spec = layer.GetPropertyAtPath(path)
            if not spec:
                return
            key = _PROPERTY_PATH_FIELDS["relationship" if isinstance(spec, Sdf.RelationshipSpec) else "attribute"]
            if spec.HasInfo(key) and points_inside(_list_op_items(spec.GetInfo(key))):
                owners.add(path.GetPrimPath())
        elif path.IsPrimPath():
            spec = layer.GetPrimAtPath(path)
            if not spec:
                return
            for key in _PRIM_PATH_FIELDS:
                if spec.HasInfo(key) and points_inside(_list_op_items(spec.GetInfo(key))):
                    owners.add(path)
            for key in _PRIM_ARC_FIELDS:
                if spec.HasInfo(key):
                    arcs = _list_op_items(spec.GetInfo(key))
                    if points_inside(arc.primPath for arc in arcs if not arc.assetPath):
                        owners.add(path)
        if len(owners) > limit:
            raise UndoError(f"More than {limit} prims point at the renamed prims; too many to snapshot for undo.")

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return sorted(str(owner) for owner in owners)


# --- undo entries and the stack -------------------------------------------------------------------------------


class UndoEntry:
    """One recorded edit: snapshots of its specs before and after, per layer."""

    def __init__(
        self,
        label: str,
        paths: Iterable[Any],
        before: list[SpecSnapshot],
        after: list[SpecSnapshot],
        *,
        stage_ref: LayerRef | None = None,
        via: str = "direct",
    ) -> None:
        if len(before) != len(after) or not before:
            raise UndoError("An undo entry needs one before and one after snapshot per layer")
        self.label = clip(label)
        self.paths = tuple(str(path) for path in paths)
        self.before = list(before)
        self.after = list(after)
        self.stage_ref = stage_ref
        self.via = via
        self.created = time.time()
        self.state = "done"
        self.stack: UndoStack | None = None
        self.kit_edit: Any = None
        self.last_verified: bool | None = None
        self.changed = not all(b.same_state(a) for b, a in zip(self.before, self.after))

    @property
    def layer(self) -> str:
        return self.before[0].layer_identifier

    def is_stale(self) -> bool:
        return any(snapshot.live_layer() is None for snapshot in self.before)

    def problem(self, stage: Any = None) -> tuple[str, str] | None:
        """(kind, message) when undo_last must refuse this entry, else None. Read-only."""
        if self.state != "done":
            return ("undone", "This edit was already undone (for example with Ctrl+Z in Kit).")
        if stage is not None and self.stage_ref is not None:
            try:
                root = stage.GetRootLayer()
            except Exception:  # noqa: BLE001
                root = None
            if not self.stage_ref.is_layer(root):
                return (
                    "stale",
                    "This edit was made on a stage that is no longer open (it was closed, reopened or replaced), "
                    "so it cannot be undone.",
                )
        for snapshot in self.after:
            if snapshot.live_layer() is None:
                return (
                    "stale",
                    f"Layer {clip(snapshot.layer_identifier, 200)} was closed or replaced after this edit, so it cannot be undone.",
                )
        for snapshot in self.after:
            if not snapshot.matches():
                return (
                    "changed",
                    f"{', '.join(snapshot.paths[:5])} changed after this MCP edit (in Kit's UI, a script or another "
                    "tool), so undoing it would overwrite those changes. Undo the later changes first (Ctrl+Z in "
                    "Kit for edits Kit recorded); changes made outside Kit's undo history (a script) block Ctrl+Z "
                    "on this edit too.",
                )
        for snapshot in self.before:
            reason = snapshot.restore_problem()
            if reason:
                return ("blocked", reason)
        return None

    def undo(self, *, force: bool = False, stage: Any = None) -> bool:
        """Restore every layer to its before snapshot; return whether the result was verified.

        Without force, refuse (UndoRefused) when problem() finds one; Kit's Ctrl+Z uses this too, so it never
        overwrites changes made outside Kit's undo history. force skips every problem() check and is only for a
        caller that has just checked problem() itself (UndoStack.undo_last); closed layers and missing parents still
        refuse in _restore. Undoing twice does nothing.
        """
        if self.state == "undone":
            return bool(self.last_verified)
        if not force:
            problem = self.problem(stage)
            if problem is not None:
                raise UndoRefused(*problem)
        self._restore(self.before)
        self.state = "undone"
        if self.stack is not None:
            self.stack.discard(self)
        return bool(self.last_verified)

    def redo(self) -> bool:
        """Put the edit back from its after snapshot (Kit's Ctrl+Y); return whether the result was verified."""
        if self.state == "done":
            return bool(self.last_verified)
        self._restore(self.after)
        self.state = "done"
        if self.stack is not None:
            self.stack.push(self)
        return bool(self.last_verified)

    def _restore(self, snapshots: list[SpecSnapshot]) -> None:
        for snapshot in snapshots:
            reason = snapshot.restore_problem()
            if reason:
                kind = "stale" if snapshot.live_layer() is None else "blocked"
                raise UndoRefused(kind, reason)
        for snapshot in snapshots:
            snapshot.restore()
        self.last_verified = all(snapshot.matches() for snapshot in snapshots)

    def summary(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "label": self.label,
            "paths": list(self.paths[:MAX_LISTED_PATHS]),
            "layer": clip(self.layer, 300),
        }
        if len(self.paths) > MAX_LISTED_PATHS:
            info["paths_truncated"] = len(self.paths) - MAX_LISTED_PATHS
        if len(self.before) > 1:
            info["layers"] = [clip(snapshot.layer_identifier, 300) for snapshot in self.before[:10]]
        return info

    def describe(self) -> dict[str, Any]:
        info = self.summary()
        info.update(
            {
                "via": self.via,
                "state": self.state,
                "created": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created)),
                "age_s": round(max(0.0, time.time() - self.created), 1),
                "snapshots": [snapshot.describe() for snapshot in self.before[:10]],
            }
        )
        return info


class UndoStack:
    """The last max_entries recorded edits, newest last. Entries that were undone leave the stack."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.max_entries = max(1, min(int(max_entries), MAX_ENTRIES_LIMIT))
        self._entries: deque[UndoEntry] = deque()
        self._notes: deque[str] = deque(maxlen=MAX_NOTES)
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._entries)

    def push(self, entry: UndoEntry) -> None:
        """Add entry on top (moving it there if present); the oldest entries fall off past max_entries."""
        self.discard(entry)
        entry.stack = self
        self._entries.append(entry)
        while len(self._entries) > self.max_entries:
            old = self._entries.popleft()
            old.stack = None
            self.dropped += 1

    def discard(self, entry: UndoEntry) -> bool:
        for index, existing in enumerate(self._entries):
            if existing is entry:
                del self._entries[index]
                return True
        return False

    def peek(self) -> UndoEntry | None:
        """The newest entry that is still applied."""
        while self._entries and self._entries[-1].state != "done":
            self._entries.pop().stack = None
        return self._entries[-1] if self._entries else None

    def entries(self) -> list[UndoEntry]:
        """Newest first."""
        return [entry for entry in reversed(self._entries) if entry.state == "done"]

    def clear(self) -> None:
        for entry in self._entries:
            entry.stack = None
        self._entries.clear()

    def note(self, message: str) -> None:
        self._notes.append(f"{time.strftime('%H:%M:%S')} {clip(message, 400)}")

    @property
    def notes(self) -> list[str]:
        return list(self._notes)

    def undo_last(self, *, stage: Any = None, dry_run: bool = True) -> dict[str, Any]:
        """Undo (dry_run=True, the default: only preview) the newest recorded edit. JSON-safe result."""
        dry = dry_run is not False
        entry = self.peek()
        if entry is None:
            return {"ok": False, "dry_run": dry, "undone": None, "message": NOTHING_TO_UNDO, "note": SAVE_NOTE}
        problem = entry.problem(stage)
        if problem is not None:
            kind, message = problem
            result: dict[str, Any] = {
                "ok": False,
                "dry_run": dry,
                "undone": None,
                "refused": kind,
                "message": message,
                "edit": entry.summary(),
                "note": SAVE_NOTE,
            }
            if kind == "stale" and not dry:
                self.discard(entry)
                entry.stack = None
                result["discarded"] = True
                result["message"] += " It was removed from the MCP undo history."
            return result
        if dry:
            return {
                "ok": True,
                "dry_run": True,
                "undone": None,
                "would_undo": entry.summary(),
                "message": "Dry run: nothing changed. Call again with dry_run=False to undo this edit.",
                "remaining": len(self),
                "note": SAVE_NOTE,
            }
        try:
            verified = entry.undo(force=True)  # problem() was just checked
        except UndoError as exc:
            return {
                "ok": False,
                "dry_run": False,
                "undone": None,
                "refused": getattr(exc, "kind", "blocked"),
                "message": clip(f"The undo could not be applied: {exc}", 600),
                "edit": entry.summary(),
                "note": SAVE_NOTE,
            }
        return {
            "ok": True,
            "dry_run": False,
            "undone": entry.summary(),
            "verified": verified,
            "remaining": len(self),
            "note": SAVE_NOTE,
        }

    def history(self, limit: int = 20, *, stage: Any = None) -> list[dict[str, Any]]:
        """Newest-first descriptions of up to limit (1-50) recorded edits."""
        try:
            count = max(1, min(int(limit), MAX_HISTORY))
        except (TypeError, ValueError):
            count = 20
        root = None
        if stage is not None:
            try:
                root = stage.GetRootLayer()
            except Exception:  # noqa: BLE001
                root = None
        items: list[dict[str, Any]] = []
        for index, entry in enumerate(self.entries()[:count], start=1):
            info = entry.describe()
            info["index"] = index
            info["stale"] = entry.is_stale()
            if stage is not None and entry.stage_ref is not None:
                info["on_open_stage"] = entry.stage_ref.is_layer(root)
            items.append(info)
        return items
