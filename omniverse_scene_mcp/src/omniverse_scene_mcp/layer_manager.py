"""Layer manager: inspect and safely change a stage's layer stack (pure pxr, dry run by default).

Read-only:  layer_stack, where_is, flatten_preview.
Writing:    add_sublayer, remove_sublayer, set_edit_target, mute_layer, unmute_layer, move_opinions.
Every writing function plans first and only changes something when called with dry_run=False; a dry run never
dirties a layer. Results are JSON-safe dicts {ok, action, dry_run, applied, written, warnings, explanation, ...}.

Composition reminder (the NCP-OUSD "L" in LIVRPS): all layers of the local layer stack -- the session layer, the
root layer and its sublayers, nested sublayers right after their parent -- are ordered strongest to weakest, and any
opinion authored in them beats inherits, variants, references, payloads and specializes. Layer offsets only shift
and scale time; they never change strength.

Imports only pxr, the standard library and this package's pure-pxr helpers, so the same functions run on temp
stages in tests and on the live stage inside Kit / Isaac Sim. Inside Kit these are raw USD calls: they bypass Kit's
own layer commands and undo stack (Kit's Layer window may need a refresh to show a mute or edit-target change).
"""

from __future__ import annotations

import math
import os
from typing import Any

from .authoring import _check_stage, _dry, _Refusal, _require_pxr, _text
from .usd_insight import _arc_for_spec, _arc_records, explain_value, to_jsonable

try:
    from pxr import Sdf, Usd, UsdUtils
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Sdf = Usd = UsdUtils = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

USD_EXTENSIONS = (".usd", ".usda", ".usdc", ".usdz")
POSITIONS = ("strongest", "weakest")
MAX_LAYERS = 200
MAX_DEPTH = 64
MAX_SPEC_COUNT = 100_000
MAX_SCAN_PRIMS = 5000
MAX_LISTED = 50
MAX_COMPARE_PRIMS = 500
MAX_COMPARE_PROPERTIES = 5000
MAX_CHANGES = 50
MAX_WARNINGS = 40
MAX_PATH_CHARS = 4096
MAX_PROPERTY_NAMES = 500
DEFAULT_FLATTEN_CHARS = 4000
MAX_FLATTEN_CHARS = 20_000
_LIST_OP_KEYS = ("references", "payload", "inheritPaths", "specializes")


# --- read-only ----------------------------------------------------------------------------------------------


def layer_stack(stage: Any) -> dict[str, Any]:
    """Every layer of the local layer stack, strongest first, plus the layers that references and payloads bring in.

    The list is the session layer (and its sublayers), then the root layer and its sublayers depth-first: a nested
    sublayer sits right after its parent and before the parent's next sibling. Muted and missing sublayers are
    listed too (with strength_rank None) because they are still authored in their parent layer.
    """
    action = "layer_stack"
    warnings: list[str] = []
    try:
        _ready(stage)
        records = _walk(stage)
        active = _active_ids(stage)
        edit_id = _edit_target_id(stage)
        layers = [_public_record(stage, record, active, edit_id) for record in records]
        referenced = _referenced_layers(stage, {_norm(record["identifier"]) for record in records}, warnings)
        for record in records:
            if record.get("cycle"):
                warnings.append(f"{record['display_name']} is a sublayer of itself (a cycle); USD skips it.")
            elif record.get("missing"):
                warnings.append(
                    f"Sublayer {record['authored_path']} of {_short(record['parent'])} could not be found, so it adds nothing."
                )
        if len(records) > MAX_LAYERS:
            warnings.append(f"Only the first {MAX_LAYERS} layers are listed.")
        explanation = _stack_explanation(layers, referenced)
    except _Refusal as refusal:
        return _result(action, warnings=warnings, error=str(refusal), read_only=True)
    except Exception as exc:  # noqa: BLE001 - read-only, so nothing can be half-done
        return _result(action, warnings=warnings, error=f"Could not read the layer stack: {exc}", read_only=True)
    return _result(
        action,
        warnings=warnings,
        explanation=explanation,
        read_only=True,
        root_layer=_text(stage.GetRootLayer().identifier),
        session_layer=_text(stage.GetSessionLayer().identifier) if stage.GetSessionLayer() else None,
        edit_target=edit_id,
        layer_count=len(layers),
        layers=layers[:MAX_LAYERS],
        muted_layers=[_text(identifier) for identifier in list(stage.GetMutedLayers())[:MAX_LISTED]],
        referenced_layers=referenced,
        strength_order="Local layer stack (this list, top = strongest), then Inherits, Variants, rElocates, References, Payloads, Specializes",
    )


def where_is(stage: Any, prim_path: str, attribute: str | None = None, *, time: Any = None) -> dict[str, Any]:
    """List every layer holding an opinion on a prim (or one of its properties), strongest first, and say which wins.

    attribute may also be given as part of prim_path ("/World/Cube.size"). For an attribute the winner comes from
    USD's own value resolution (usd_insight.explain_value) at time (default: the Default time).
    """
    action = "where_is"
    warnings: list[str] = []
    try:
        _ready(stage)
        path = _prim_path_arg(prim_path, allow_property=True)
        if path.IsPropertyPath():
            if attribute:
                raise _Refusal("Give the property either inside prim_path or as attribute, not both.")
            attribute = path.name
            path = path.GetPrimPath()
        prim = stage.GetPrimAtPath(path)
        if not prim or not prim.IsValid():
            raise _Refusal(_missing_prim_message(stage, path))
        ranks = _rank_map(stage)
        if attribute:
            payload = _where_property(stage, prim, str(attribute), time, ranks, warnings)
        else:
            payload = _where_prim(stage, prim, ranks)
    except _Refusal as refusal:
        return _result(action, warnings=warnings, error=str(refusal), read_only=True)
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Could not look up the opinions: {exc}", read_only=True)
    explanation = payload.pop("explanation")
    return _result(action, warnings=warnings, explanation=explanation, read_only=True, **payload)


def flatten_preview(stage: Any, prim_path: str | None = None, *, max_chars: int = DEFAULT_FLATTEN_CHARS) -> dict[str, Any]:
    """Show (as .usda text, capped) what a prim looks like once every layer and arc is baked into one layer.

    Only the prim's subtree is composed and flattened (a population-masked copy of the stage opened on the same
    layers), so the open stage and its layers are not changed. prim_path defaults to the default prim, else the
    whole stage.
    """
    action = "flatten_preview"
    warnings: list[str] = []
    try:
        _ready(stage)
        limit = _whole_number(max_chars, "max_chars", 200, MAX_FLATTEN_CHARS)
        if prim_path in (None, ""):
            default = stage.GetDefaultPrim()
            path = default.GetPath() if default and default.IsValid() else Sdf.Path.absoluteRootPath
        else:
            path = _prim_path_arg(prim_path)
        if path != Sdf.Path.absoluteRootPath:
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                raise _Refusal(_missing_prim_message(stage, path))
            arcs, _ = _arc_records(prim)
        else:
            arcs = []
        text = _flatten_text(stage, path, warnings)
    except _Refusal as refusal:
        return _result(action, warnings=warnings, error=str(refusal), read_only=True)
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Could not flatten: {exc}", read_only=True)
    truncated = len(text) > limit
    shown = text[:limit] + ("\n# ... (truncated)" if truncated else "")
    arc_words = sorted({arc["arc_type"] for arc in arcs if not arc.get("implicit")})
    explanation = (
        "Flattening bakes every layer and composition arc into a single layer: only the winning opinion of each "
        "attribute is kept, and sublayers, references, payloads, inherits and variants disappear (their results are "
        "copied in). This preview only shows the text; nothing was saved or changed."
    )
    if arc_words:
        explanation += f" Arcs baked into {path}: {', '.join(arc_words)}."
    return _result(
        action,
        warnings=warnings,
        explanation=explanation,
        read_only=True,
        prim_path=str(path),
        usda=shown,
        chars=len(text),
        truncated=truncated,
        layers_baked=len(stage.GetUsedLayers()),
    )


# --- writing: sublayers -------------------------------------------------------------------------------------


def add_sublayer(
    stage: Any,
    layer_path: str,
    *,
    position: Any = "strongest",
    create_if_missing: bool = False,
    relative: bool = True,
    allow_outside: bool = False,
    offset: float = 0.0,
    scale: float = 1.0,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or add a sublayer to the root layer.

    layer_path is a .usd/.usda/.usdc/.usdz file, relative to the root layer's folder or absolute. position is
    'strongest' (first in the list), 'weakest' (last) or an index into the root layer's sublayer list. With
    relative=True the path is authored relative to the root layer ("./layers/a.usda"). Refuses duplicates, cycles,
    non-USD files, missing files (unless create_if_missing, which writes a new empty file) and files outside the
    root layer's folder (unless allow_outside). offset/scale author a layer offset (time only, never strength).
    """
    action = "add_sublayer"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        guard = _DirtyGuard(stage)
        root = stage.GetRootLayer()
        _require_editable(root, "root layer")
        text = _path_text(layer_path, "layer_path")
        offset_value = _finite(offset, "offset")
        scale_value = _finite(scale, "scale")
        if scale_value <= 0:
            raise _Refusal(f"scale must be greater than 0, got {scale_value:g}")
        index = _insert_index(position, len(root.subLayerPaths))
        plan = _plan_new_sublayer(stage, root, text, bool(create_if_missing), bool(relative), bool(allow_outside), warnings)
        plan.update(
            index=index,
            position=_position_word(index, len(root.subLayerPaths)),
            offset={"offset": _num(offset_value), "scale": _num(scale_value)},
            parent_layer=_text(root.identifier),
        )
        explanation = (
            f"{plan['display_name']} would be inserted into the root layer's sublayer list at index {index} "
            f"({plan['position']}). Sublayers listed earlier are stronger, so its opinions would "
            + _strength_phrase(index, [str(item) for item in root.subLayerPaths])
            + " Only the root layer's sublayer list changes; save the root layer to keep it."
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001 - planning only reads
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    identifier_norm = plan.pop("identifier_norm")
    if dry:
        guard.check(warnings)
        return _result(action, dry=True, warnings=warnings, explanation=explanation, plan=plan, **guard.extra())

    created = None
    try:
        if plan["would_create_file"]:
            created = Sdf.Layer.CreateNew(plan["absolute_path"])
            if created is None:
                raise RuntimeError(f"could not create {plan['absolute_path']}")
        root.subLayerPaths.insert(index, plan["authored_path"])
        if offset_value != 0.0 or scale_value != 1.0:
            root.subLayerOffsets[index] = Sdf.LayerOffset(offset_value, scale_value)
    except Exception as exc:  # noqa: BLE001
        rolled = _undo_insert(root, index, plan["authored_path"])
        note = "the root layer's sublayer list was put back" if rolled else "check the root layer's sublayer list"
        return _result(action, warnings=warnings, error=f"Adding the sublayer failed ({note}): {exc}", plan=plan,
                       applied=not rolled)
    if created is not None:
        warnings.append(f"Created the new empty layer file {plan['absolute_path']}.")
    active = _active_ids(stage)
    loaded = identifier_norm in {_norm(identifier) for identifier in active}
    if not loaded:
        warnings.append("The sublayer path was added but USD could not load the layer; check the path.")
    warnings.append("The root layer now has unsaved changes; save it to keep the new sublayer.")
    return _result(
        action,
        applied=True,
        warnings=warnings,
        explanation=explanation.replace("would be inserted", "was inserted").replace("would ", ""),
        plan=plan,
        loaded=loaded,
        sublayers=[str(item) for item in root.subLayerPaths],
        undo={"function": "remove_sublayer", "layer_identifier": plan["authored_path"]},
    )


def remove_sublayer(
    stage: Any, layer_identifier: str, *, discard_unsaved_edits: bool = False, dry_run: bool = True
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or remove a sublayer from its parent layer's sublayer list.

    The file on disk is never deleted. Refuses the root and session layers, the current edit target (or a layer
    that contains it), and -- unless discard_unsaved_edits=True -- a layer with unsaved edits or an in-memory
    (anonymous) layer, because once the stage lets go of such a layer its unsaved content is gone.
    """
    action = "remove_sublayer"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        guard = _DirtyGuard(stage)
        records = _walk(stage)
        record = _find_layer(stage, layer_identifier, records, roles=("sublayer", "session_sublayer"))
        parent = Sdf.Layer.Find(record["parent"]) if record["parent"] else None
        if parent is None:
            raise _Refusal(f"The parent layer of {record['display_name']} is not open.")
        _require_editable(parent, "parent layer")
        index = _sublayer_index(parent, record)
        subtree = _subtree(records, record)
        edit_id = _edit_target_id(stage)
        if any(item["identifier"] == edit_id for item in subtree):
            raise _Refusal(
                f"{record['display_name']} is (or contains) the current edit target. Call set_edit_target to pick "
                "another layer first, so new edits do not go to a layer that is leaving the stage."
            )
        _unsaved_check(subtree, discard_unsaved_edits, "removing", warnings)
        offset = parent.subLayerOffsets[index]
        layer = record["layer"]
        spec_count, capped = _count_prim_specs(layer) if layer is not None else (0, False)
        plan = {
            "layer": _text(record["identifier"]),
            "display_name": record["display_name"],
            "authored_path": record["authored_path"],
            "parent_layer": _text(parent.identifier),
            "index": index,
            "offset": _offset(offset),
            "prim_spec_count": spec_count,
            "prim_spec_count_capped": capped,
            "nested_sublayers_also_leaving": [item["display_name"] for item in subtree[1:]][:MAX_LISTED],
        }
        explanation = (
            f"{record['display_name']} would be taken out of {_short(parent.identifier)}'s sublayer list, so its "
            f"{spec_count} prim spec(s)"
            + (f" and its {len(subtree) - 1} nested sublayer(s)" if len(subtree) > 1 else "")
            + " would stop contributing opinions. The file itself is not deleted; add_sublayer can put it back."
        )
        restore = {
            "function": "add_sublayer",
            "layer_path": record["authored_path"],
            "position": index,
            "offset": _num(offset.offset),
            "scale": _num(offset.scale),
            "relative": False,
            "allow_outside": True,
        }
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    if dry:
        guard.check(warnings)
        return _result(action, dry=True, warnings=warnings, explanation=explanation, plan=plan, restore_with=restore,
                       **guard.extra())
    try:
        del parent.subLayerPaths[index]
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Removing the sublayer failed (nothing was changed): {exc}", plan=plan)
    warnings.append(f"{_short(parent.identifier)} now has unsaved changes; save it to keep the removal.")
    return _result(
        action,
        applied=True,
        warnings=warnings,
        explanation=explanation.replace("would be taken", "was taken").replace("would stop", "stopped"),
        plan=plan,
        sublayers=[str(item) for item in parent.subLayerPaths],
        restore_with=restore,
    )


# --- writing: edit target and muting ------------------------------------------------------------------------


def set_edit_target(stage: Any, layer_identifier: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or make a layer of the local layer stack the stage's edit target.

    The result's previous_edit_target (and restore_with) name the old target so it can be set back. Changes stage
    state only: no layer is edited.
    """
    action = "set_edit_target"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        records = _walk(stage)
        record = _find_layer(stage, layer_identifier, records, roles=("session", "session_sublayer", "root", "sublayer"))
        layer = record["layer"]
        if layer is None or record.get("missing"):
            raise _Refusal(f"{record['display_name']} is not loaded, so it cannot be the edit target.")
        if record.get("muted"):
            raise _Refusal(f"{record['display_name']} is muted; unmute it first.")
        if record["identifier"] not in _active_ids(stage):
            raise _Refusal(f"{record['display_name']} is not part of the stage's local layer stack.")
        _require_editable(layer, record["display_name"])
        previous = _edit_target_record(stage)
        if previous.get("mapped"):
            warnings.append(
                "The current edit target maps paths (for example a variant edit context); restoring it by layer alone "
                "would not bring that mapping back."
            )
        if record["role"].startswith("session"):
            warnings.append("The session layer is never saved with the scene: edits there are temporary.")
        elif layer.anonymous:
            warnings.append("This layer is in memory only (anonymous); edits there are lost unless it is exported.")
        if os.path.splitext(layer.realPath or "")[1].lower() == ".usdz":
            warnings.append("A .usdz package cannot be saved back; edits there are lost.")
        already = previous.get("identifier") == record["identifier"] and not previous.get("mapped")
        explanation = (
            f"New edits (set_attribute, create_light, ...) would be written into {record['display_name']}. "
            + _strength_sentence(record, records)
        )
        if already:
            explanation = f"{record['display_name']} is already the edit target; nothing to change."
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    restore = {"function": "set_edit_target", "layer_identifier": previous.get("identifier")}
    common = {"layer": _text(record["identifier"]), "display_name": record["display_name"],
              "previous_edit_target": previous, "restore_with": restore}
    if dry or already:
        return _result(action, dry=dry, warnings=warnings, explanation=explanation, already=already, **common)
    try:
        stage.SetEditTarget(Usd.EditTarget(layer))
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Setting the edit target failed (nothing was changed): {exc}", **common)
    return _result(action, applied=True, warnings=warnings,
                   explanation=explanation.replace("would be written", "will be written"), **common)


def mute_layer(stage: Any, identifier: str, *, discard_unsaved_edits: bool = False, dry_run: bool = True) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or mute a layer: the stage then ignores it (and its sublayers) entirely.

    Muting is stage state (not saved in any layer) and is undone with unmute_layer. Refuses the root and session
    layers and the current edit target (or a layer that contains it). USD releases a muted layer, so a layer with
    unsaved edits or an in-memory layer is refused unless discard_unsaved_edits=True.
    """
    action = "mute_layer"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        records = _walk(stage)
        record = _find_layer(stage, identifier, records, roles=("sublayer", "session_sublayer", "referenced"),
                             include_referenced=True)
        if record.get("muted"):
            explanation = f"{record['display_name']} is already muted; nothing to change."
            return _result(action, dry=dry, explanation=explanation, already=True, layer=_text(record["identifier"]),
                           restore_with={"function": "unmute_layer", "identifier": record["identifier"]})
        subtree = _subtree(records, record) if record["role"] != "referenced" else [record]
        edit_id = _edit_target_id(stage)
        if any(item["identifier"] == edit_id for item in subtree):
            raise _Refusal(
                f"{record['display_name']} is (or contains) the current edit target; muting it would leave the stage "
                "with no valid edit target. Call set_edit_target to pick another layer first."
            )
        _unsaved_check(subtree, discard_unsaved_edits, "muting", warnings)
        layer = record["layer"]
        spec_count, capped = _count_prim_specs(layer) if layer is not None else (0, False)
        explanation = (
            f"Muting {record['display_name']} would make the stage ignore its {spec_count} prim spec(s)"
            + (f" and its {len(subtree) - 1} nested sublayer(s)" if len(subtree) > 1 else "")
            + ", as if it were not in the list. The layer stays listed in its parent, nothing is saved, and "
            "unmute_layer brings it back."
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    common = {
        "layer": _text(record["identifier"]),
        "display_name": record["display_name"],
        "prim_spec_count": spec_count,
        "prim_spec_count_capped": capped,
        "restore_with": {"function": "unmute_layer", "identifier": record["identifier"]},
    }
    if dry:
        return _result(action, dry=True, warnings=warnings, explanation=explanation, **common)
    try:
        stage.MuteLayer(record["identifier"])
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Muting failed (nothing was changed): {exc}", **common)
    if not stage.IsLayerMuted(record["identifier"]):
        return _result(action, warnings=warnings, error="USD did not mute the layer.", **common)
    return _result(action, applied=True, warnings=warnings,
                   explanation=explanation.replace("would make", "made"), **common)


def unmute_layer(stage: Any, identifier: str, *, dry_run: bool = True) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or unmute a muted layer so its opinions count again."""
    action = "unmute_layer"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        muted = [str(item) for item in stage.GetMutedLayers()]
        if not muted:
            raise _Refusal("No layer is muted on this stage.")
        match = _match_muted(stage, identifier, muted)
        explanation = (
            f"Unmuting {_short(match)} would make the stage read its opinions again, at its old place in the "
            "layer order."
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    common = {"layer": _text(match), "display_name": _short(match),
              "restore_with": {"function": "mute_layer", "identifier": match}}
    if dry:
        return _result(action, dry=True, warnings=warnings, explanation=explanation, **common)
    try:
        stage.UnmuteLayer(match)
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Unmuting failed (nothing was changed): {exc}", **common)
    if stage.IsLayerMuted(match):
        return _result(action, warnings=warnings, error="USD did not unmute the layer.", **common)
    if Sdf.Layer.Find(match) is None and not any(_norm(match) == _norm(layer.identifier) for layer in stage.GetUsedLayers()):
        warnings.append("The layer was unmuted but could not be loaded again; check that the file still exists.")
    return _result(action, applied=True, warnings=warnings,
                   explanation=explanation.replace("would make", "made"), **common)


# --- writing: moving opinions between layers ----------------------------------------------------------------


def move_opinions(
    stage: Any,
    prim_path: str,
    src_layer: str,
    dst_layer: str,
    *,
    include_children: bool = True,
    properties: Any = None,
    allow_changes: bool = False,
    undo_stack: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or move a prim's opinions from one local layer to another.

    By default the whole prim spec subtree moves (its fields, properties, variant sets and, with include_children,
    child prims); properties=[names] moves only those property specs. Where the destination already has opinions
    the two are merged the way composition would combine them (the stronger layer's value is kept, list edits and
    dictionaries are combined). Missing ancestors in the destination are created as 'over' specs.

    The move is simulated first on in-memory copies of the layer stack, and the composed values before and after
    are compared. Any predicted change is reported (composed_changes) with a warning; dry_run=False then refuses
    unless allow_changes=True. On failure both layers are rolled back from undo.SpecSnapshot snapshots. Pass
    undo_stack (an undo.UndoStack) to record the move for undo_last.
    """
    action = "move_opinions"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _ready(stage)
        guard = _DirtyGuard(stage)
        path = _prim_path_arg(prim_path)
        records = _walk(stage)
        local = ("session", "session_sublayer", "root", "sublayer")
        src_record = _find_layer(stage, src_layer, records, roles=local)
        dst_record = _find_layer(stage, dst_layer, records, roles=local)
        ranks = _rank_map(stage)
        for record, label in ((src_record, "src_layer"), (dst_record, "dst_layer")):
            if record["layer"] is None or _norm(record["identifier"]) not in ranks:
                raise _Refusal(f"{label} {record['display_name']} is muted or not loaded, so it is not in the layer stack.")
        src, dst = src_record["layer"], dst_record["layer"]
        if src.identifier == dst.identifier:
            raise _Refusal("src_layer and dst_layer are the same layer.")
        _require_editable(src, f"src_layer {src_record['display_name']}")
        _require_editable(dst, f"dst_layer {dst_record['display_name']}")
        if not src.GetPrimAtPath(path):
            raise _Refusal(f"{src_record['display_name']} has no prim spec at {path}, so there is nothing to move. "
                           "Use where_is to see which layers hold opinions on it.")
        names = _property_names(properties, src, path)
        src_rank, dst_rank = ranks[_norm(src.identifier)], ranks[_norm(dst.identifier)]
        src_stronger = src_rank < dst_rank
        moved = _describe_move(src, dst, path, bool(include_children), names)
        between = [
            record["display_name"] for record in records
            if record["layer"] is not None and _norm(record["identifier"]) in ranks
            and min(src_rank, dst_rank) < ranks[_norm(record["identifier"])] < max(src_rank, dst_rank)
            and record["layer"].GetPrimAtPath(path)
        ]
        simulation = _simulate_move(stage, records, src, dst, path, src_stronger, bool(include_children), names)
        changes = simulation["changes"]
        if src_record["role"].startswith("session") or dst_record["role"].startswith("session"):
            warnings.append("The session layer is never saved with the scene.")
        if changes:
            warnings.append(
                f"Moving these opinions would change {len(changes)} composed value(s) (see composed_changes): "
                + ("a layer in between (" + ", ".join(between[:5]) + ") holds opinions that would now win or lose."
                   if between else "the destination's position in the layer order changes which opinion wins.")
            )
        if moved["specifier"] == "class":
            warnings.append("This prim is a class; prims that inherit or specialize it elsewhere were not re-checked.")
        if moved["conflicts"]:
            warnings.append(
                f"Both layers author {', '.join(moved['conflicts'][:8])}; the stronger layer's value "
                f"({src_record['display_name'] if src_stronger else dst_record['display_name']}) is kept."
            )
        direction = "stronger" if not src_stronger else "weaker"
        plan = {
            "prim_path": str(path),
            "src_layer": _text(src.identifier),
            "dst_layer": _text(dst.identifier),
            "src_strength_rank": src_rank,
            "dst_strength_rank": dst_rank,
            "destination_is": direction,
            "include_children": bool(include_children),
            "properties": names,
            "moves": moved,
            "layers_in_between_with_opinions": between[:MAX_LISTED],
        }
        explanation = (
            f"The opinions on {path} in {src_record['display_name']} would move to {dst_record['display_name']}, "
            f"which is {direction} in the layer order (rank {dst_rank} vs {src_rank}; 0 is strongest). "
            + ("The simulated composed result is unchanged." if not changes else
               f"The simulation shows {len(changes)} composed value change(s), listed in composed_changes.")
        )
    except _Refusal as refusal:
        return _result(action, dry=dry, warnings=warnings, error=str(refusal))
    except Exception as exc:  # noqa: BLE001 - planning works on copies only
        return _result(action, dry=dry, warnings=warnings, error=f"Unexpected error while planning (nothing was changed): {exc}")
    common = {"plan": plan, "composed_changes": changes[:MAX_CHANGES], "composed_values_compared": simulation["compared"]}
    if len(changes) > MAX_CHANGES:
        common["composed_changes_truncated"] = len(changes) - MAX_CHANGES
    if dry:
        guard.check(warnings)
        return _result(action, dry=True, warnings=warnings, explanation=explanation, **common, **guard.extra())
    if changes and not allow_changes:
        return _result(
            action,
            warnings=warnings,
            error=(f"Refused: the move would change {len(changes)} composed value(s). Review composed_changes and call "
                   "again with allow_changes=True if that is what you want."),
            explanation=explanation,
            **common,
        )
    return _apply_move_on_stage(stage, path, src, dst, src_stronger, bool(include_children), names, undo_stack,
                                warnings, explanation, common)


# --- helpers: validation ------------------------------------------------------------------------------------


def _ready(stage: Any) -> None:
    _require_pxr()
    _check_stage(stage)
    if not hasattr(stage, "GetRootLayer") or stage.GetRootLayer() is None:
        raise _Refusal("No USD stage is open.")


def _path_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Refusal(f"{name} must be a non-empty file path string.")
    text = value.strip()
    if len(text) > MAX_PATH_CHARS or "\x00" in text:
        raise _Refusal(f"{name} is not a usable path.")
    return text


def _prim_path_arg(value: Any, *, allow_property: bool = False) -> Any:
    text = str(value).strip() if value is not None else ""
    if not text.startswith("/") or len(text) > MAX_PATH_CHARS or not Sdf.Path.IsValidPathString(text):
        raise _Refusal(f"prim_path must be an absolute USD path such as /World/Car, got {value!r}")
    path = Sdf.Path(text)
    if path.ContainsPrimVariantSelection():
        raise _Refusal("prim_path must not contain a variant selection ({set=variant}); use the plain prim path.")
    if path.IsPrimPath() or (allow_property and path.IsPropertyPath() and path.GetPrimPath().IsPrimPath()):
        return path
    raise _Refusal(f"prim_path must be a prim path such as /World/Car, got {text}")


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Refusal(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise _Refusal(f"{name} must be finite, got {value!r}")
    return number


def _whole_number(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Refusal(f"{name} must be a whole number from {low} to {high}")
    return max(low, min(int(value), high))


def _require_editable(layer: Any, label: str) -> None:
    if not layer.permissionToEdit:
        raise _Refusal(f"The {label} ({_short(layer.identifier)}) is read-only (permissionToEdit is False).")


def _insert_index(position: Any, count: int) -> int:
    if isinstance(position, str):
        word = position.strip().lower()
        if word == "strongest":
            return 0
        if word == "weakest":
            return count
        raise _Refusal(f"position must be 'strongest', 'weakest' or an index from 0 to {count}, got {position!r}")
    if isinstance(position, bool) or not isinstance(position, int):
        raise _Refusal(f"position must be 'strongest', 'weakest' or an index from 0 to {count}, got {position!r}")
    if not 0 <= position <= count:
        raise _Refusal(f"position index must be from 0 to {count} (the root layer has {count} sublayer(s)), got {position}")
    return int(position)


def _position_word(index: int, count: int) -> str:
    if index == 0:
        return "strongest sublayer"
    if index >= count:
        return "weakest sublayer"
    return f"sublayer number {index + 1} of {count + 1}"


def _strength_phrase(index: int, existing: list[str]) -> str:
    stronger = existing[:index]
    weaker = existing[index:]
    parts = []
    if stronger:
        parts.append(f"lose to {', '.join(stronger[:4])}{' ...' if len(stronger) > 4 else ''}")
    if weaker:
        parts.append(f"beat {', '.join(weaker[:4])}{' ...' if len(weaker) > 4 else ''}")
    parts.append("lose to anything authored directly in the root layer or the session layer")
    return "; ".join(parts) + "."


def _plan_new_sublayer(
    stage: Any, root: Any, text: str, create: bool, relative: bool, allow_outside: bool, warnings: list[str]
) -> dict[str, Any]:
    folder = os.path.dirname(root.realPath) if root.realPath and not root.anonymous else None
    records = _walk(stage)
    known = {_norm(record["identifier"]): record for record in records}
    if text.startswith("anon:"):
        layer = Sdf.Layer.Find(text)
        if layer is None:
            raise _Refusal(f"No open in-memory layer is called {text}.")
        if _norm(layer.identifier) in known:
            raise _Refusal(f"{_short(layer.identifier)} is already in the layer stack ({_where_listed(known[_norm(layer.identifier)])}).")
        _check_cycle(root, layer, known, warnings)
        warnings.append("This is an in-memory (anonymous) layer: the root layer can only refer to it while it stays open, "
                        "and it is never saved.")
        return {
            "layer_path": text, "absolute_path": None, "authored_path": layer.identifier, "identifier": layer.identifier,
            "identifier_norm": _norm(layer.identifier), "display_name": _short(layer.identifier), "exists": True,
            "would_create_file": False, "relative": False, "outside_root_folder": False,
        }
    extension = os.path.splitext(text)[1].lower()
    if extension not in USD_EXTENSIONS:
        raise _Refusal(f"{text} is not a USD layer: use a {', '.join(USD_EXTENSIONS)} file.")
    if os.path.isabs(text):
        absolute = os.path.normpath(text)
    elif folder is None:
        raise _Refusal("The root layer is in memory (not saved), so a relative path has nothing to be relative to; "
                       "give an absolute path, with allow_outside=True.")
    else:
        absolute = os.path.normpath(os.path.join(folder, text))
    outside = folder is None or not _inside(absolute, folder)
    if outside and not allow_outside:
        where = f"the root layer's folder ({folder})" if folder else "a saved root layer's folder (the root is in memory)"
        raise _Refusal(f"{absolute} is outside {where}. Pass allow_outside=True if you really mean it; scenes that "
                       "point outside their folder break when the folder is moved or shared.")
    if outside:
        warnings.append("The sublayer is outside the root layer's folder, so the scene depends on that location.")
    exists = os.path.isfile(absolute)
    if not exists and not create:
        raise _Refusal(f"{absolute} does not exist. Pass create_if_missing=True to create a new empty layer there.")
    if not exists and not os.path.isdir(os.path.dirname(absolute)):
        raise _Refusal(f"The folder {os.path.dirname(absolute)} does not exist; create it first.")
    if not exists and extension == ".usdz":
        raise _Refusal("A new .usdz package cannot be created as an empty layer; use .usda or .usd.")
    identifier = absolute.replace("\\", "/")
    if _norm(absolute) == _norm(root.realPath or root.identifier):
        raise _Refusal("A layer cannot be a sublayer of itself (that is the root layer).")
    if _norm(absolute) in known:
        raise _Refusal(f"{os.path.basename(absolute)} is already in the layer stack ({_where_listed(known[_norm(absolute)])}).")
    for path in root.subLayerPaths:
        resolved = Sdf.ComputeAssetPathRelativeToLayer(root, str(path)) or str(path)
        if _norm(resolved) == _norm(absolute):
            raise _Refusal(f"The root layer already lists {path} as a sublayer.")
    if exists:
        layer = Sdf.Layer.FindOrOpen(absolute)
        if layer is None:
            raise _Refusal(f"{absolute} could not be opened as a USD layer.")
        identifier = layer.identifier
        _check_cycle(root, layer, known, warnings)
        if extension == ".usdz":
            warnings.append("A .usdz package is read-only: it can be a sublayer but can never be the edit target.")
    authored = identifier
    if relative and folder is not None:
        try:
            rel = os.path.relpath(absolute, folder).replace("\\", "/")
            authored = rel if rel.startswith("../") else "./" + rel
        except ValueError:  # another drive on Windows
            warnings.append("The layer is on another drive, so an absolute path is authored.")
    elif relative:
        warnings.append("The root layer is in memory, so an absolute path is authored.")
    return {
        "layer_path": text,
        "absolute_path": absolute,
        "authored_path": authored,
        "identifier": _text(identifier),
        "identifier_norm": _norm(identifier),
        "display_name": os.path.basename(absolute),
        "exists": exists,
        "would_create_file": not exists,
        "relative": authored != identifier,
        "outside_root_folder": outside,
    }


def _check_cycle(root: Any, layer: Any, known: dict[str, Any], warnings: list[str]) -> None:
    """Refuse when the new layer (through its own sublayers) leads back to the root layer."""
    root_norm = _norm(root.identifier)
    seen: set[str] = set()
    stack = [(layer, 0)]
    repeated: list[str] = []
    while stack:
        current, depth = stack.pop()
        key = _norm(current.identifier)
        if key in seen or depth > MAX_DEPTH:
            continue
        seen.add(key)
        for sub in current.subLayerPaths:
            resolved = Sdf.ComputeAssetPathRelativeToLayer(current, str(sub)) or str(sub)
            if _norm(resolved) == root_norm:
                raise _Refusal(
                    f"{_short(current.identifier)} already has the root layer as a sublayer, so adding it would make a "
                    "cycle (a layer inside itself)."
                )
            if _norm(resolved) in known:
                repeated.append(_short(resolved))
            child = Sdf.Layer.Find(resolved) or (Sdf.Layer.FindOrOpen(resolved) if os.path.isfile(resolved) else None)
            if child is not None:
                stack.append((child, depth + 1))
    if repeated:
        warnings.append(f"The new layer's own sublayers ({', '.join(sorted(set(repeated))[:5])}) are already in the "
                        "stack, so they would appear twice.")


def _undo_insert(root: Any, index: int, authored: str) -> bool:
    try:
        paths = [str(item) for item in root.subLayerPaths]
        if index < len(paths) and paths[index] == authored:
            del root.subLayerPaths[index]
        return authored not in [str(item) for item in root.subLayerPaths]
    except Exception:  # noqa: BLE001
        return False


def _sublayer_index(parent: Any, record: dict[str, Any]) -> int:
    paths = [str(item) for item in parent.subLayerPaths]
    if record["authored_path"] in paths:
        return paths.index(record["authored_path"])
    for index, item in enumerate(paths):
        resolved = Sdf.ComputeAssetPathRelativeToLayer(parent, item) or item
        if _norm(resolved) == _norm(record["identifier"]):
            return index
    raise _Refusal(f"{record['display_name']} is no longer listed in {_short(parent.identifier)}.")


def _unsaved_check(subtree: list[dict[str, Any]], discard: bool, verb: str, warnings: list[str]) -> None:
    risky = [
        item["display_name"] for item in subtree
        if item["layer"] is not None and (item["layer"].dirty or item["layer"].anonymous)
    ]
    if not risky:
        return
    if not discard:
        raise _Refusal(
            f"{', '.join(risky[:5])} has unsaved edits or exists only in memory. After {verb}, the stage stops "
            "holding it and those edits can be lost. Save it first, or pass discard_unsaved_edits=True."
        )
    warnings.append(f"{', '.join(risky[:5])} has unsaved or in-memory content that may be lost after {verb}.")


def _property_names(properties: Any, src: Any, path: Any) -> list[str] | None:
    if properties is None:
        return None
    if isinstance(properties, str):
        properties = [properties]
    try:
        names = [str(item).strip() for item in properties]
    except TypeError:
        raise _Refusal("properties must be a list of property names such as ['size', 'xformOp:translate'].") from None
    if not names or len(names) > MAX_PROPERTY_NAMES:
        raise _Refusal(f"properties must list 1 to {MAX_PROPERTY_NAMES} names.")
    spec = src.GetPrimAtPath(path)
    missing = [name for name in names if not Sdf.Path.IsValidNamespacedIdentifier(name) or not spec.properties.get(name)]
    if missing:
        have = sorted(str(key) for key in spec.properties.keys())[:20]
        raise _Refusal(f"The source layer has no property spec {', '.join(missing[:5])} on {path}. It has: {have or 'none'}.")
    return list(dict.fromkeys(names))


# --- helpers: layer records ---------------------------------------------------------------------------------


def _walk(stage: Any) -> list[dict[str, Any]]:
    """Local layer stack records strongest first: session tree, then root tree (depth-first), muted/missing too."""
    records: list[dict[str, Any]] = []
    session = stage.GetSessionLayer()
    identity = Sdf.LayerOffset()
    if session is not None:
        _walk_tree(stage, session, "session", None, None, identity, identity, 0, records, frozenset())
    _walk_tree(stage, stage.GetRootLayer(), "root", None, None, identity, identity, 0, records, frozenset())
    return records


def _walk_tree(
    stage: Any, layer: Any, role: str, authored: str | None, parent: str | None, offset: Any, to_root: Any,
    depth: int, out: list[dict[str, Any]], ancestors: frozenset,
) -> None:
    if len(out) > MAX_LAYERS:
        return
    identifier = layer.identifier
    record = {
        "layer": layer,
        "identifier": identifier,
        "display_name": _display(layer, role),
        "role": role,
        "authored_path": authored,
        "parent": parent,
        "depth": depth,
        "offset": offset,
        "offset_to_root": to_root,
        "muted": bool(stage.IsLayerMuted(identifier)) if role not in ("root", "session") else False,
        "missing": False,
    }
    out.append(record)
    if identifier in ancestors:
        record["cycle"] = True
        return
    if record["muted"] or depth >= MAX_DEPTH:
        return
    child_role = "session_sublayer" if role.startswith("session") else "sublayer"
    inner = ancestors | {identifier}
    for sub_path, sub_offset in zip(layer.subLayerPaths, layer.subLayerOffsets):
        sub_path = str(sub_path)
        resolved = Sdf.ComputeAssetPathRelativeToLayer(layer, sub_path) or sub_path
        muted = bool(stage.IsLayerMuted(resolved) or stage.IsLayerMuted(sub_path))
        sub_layer = None if muted else Sdf.Layer.FindRelativeToLayer(layer, sub_path)
        cumulative = to_root * sub_offset
        if sub_layer is not None:
            _walk_tree(stage, sub_layer, child_role, sub_path, identifier, sub_offset, cumulative, depth + 1, out, inner)
            continue
        out.append(
            {
                "layer": None,
                "identifier": resolved,
                "display_name": _short(resolved),
                "role": child_role,
                "authored_path": sub_path,
                "parent": identifier,
                "depth": depth + 1,
                "offset": sub_offset,
                "offset_to_root": cumulative,
                "muted": muted,
                "missing": not muted,
            }
        )


def _subtree(records: list[dict[str, Any]], record: dict[str, Any]) -> list[dict[str, Any]]:
    """The record and every record nested under it (records are depth-first)."""
    start = next(index for index, item in enumerate(records) if item is record)
    out = [record]
    for item in records[start + 1:]:
        if item["depth"] <= record["depth"]:
            break
        out.append(item)
    return out


def _active_ids(stage: Any) -> list[str]:
    return [layer.identifier for layer in stage.GetLayerStack(True)]


def _rank_map(stage: Any) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for index, identifier in enumerate(_active_ids(stage)):
        ranks.setdefault(_norm(identifier), index)
    return ranks


def _edit_target_id(stage: Any) -> str | None:
    try:
        layer = stage.GetEditTarget().GetLayer()
    except Exception:  # noqa: BLE001
        return None
    return layer.identifier if layer is not None else None


def _edit_target_record(stage: Any) -> dict[str, Any]:
    target = stage.GetEditTarget()
    layer = target.GetLayer() if target is not None else None
    if layer is None:
        return {"identifier": None, "display_name": None, "mapped": False}
    mapped = False
    try:
        mapped = not target.GetMapFunction().isIdentity
    except Exception:  # noqa: BLE001
        mapped = False
    session_ids = {item.identifier for item in stage.GetLayerStack(True)} - {item.identifier for item in stage.GetLayerStack(False)}
    role = "session" if layer.identifier in session_ids else "root" if layer.identifier == stage.GetRootLayer().identifier else "sublayer"
    return {"identifier": _text(layer.identifier), "display_name": _display(layer, role), "mapped": mapped}


def _find_layer(
    stage: Any, query: Any, records: list[dict[str, Any]], *, roles: tuple[str, ...], include_referenced: bool = False
) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise _Refusal("Name a layer by identifier, file path, file name, 'root' or 'session'.")
    text = query.strip()
    candidates = list(records)
    if include_referenced:
        local = {_norm(record["identifier"]) for record in records}
        for layer in stage.GetUsedLayers():
            if _norm(layer.identifier) not in local:
                candidates.append({
                    "layer": layer, "identifier": layer.identifier, "display_name": _short(layer.identifier),
                    "role": "referenced", "authored_path": None, "parent": None, "depth": 0,
                    "muted": False, "missing": False,
                })
    word = text.lower()
    if word in ("root", "root layer"):
        matches = [record for record in candidates if record["role"] == "root"]
    elif word in ("session", "session layer"):
        matches = [record for record in candidates if record["role"] == "session"]
    else:
        matches = [record for record in candidates if record["identifier"] == text]
        if not matches:
            wanted = {_norm(text)}
            root = stage.GetRootLayer()
            if not os.path.isabs(text) and not text.startswith("anon:") and root.realPath:
                wanted.add(_norm(os.path.join(os.path.dirname(root.realPath), text)))
            matches = [record for record in candidates if _norm(record["identifier"]) in wanted
                       or (record["layer"] is not None and record["layer"].realPath and _norm(record["layer"].realPath) in wanted)]
        if not matches:
            matches = [record for record in candidates if record["authored_path"] == text]
        if not matches:
            matches = [record for record in candidates
                       if record["display_name"].lower() == word or _short(record["identifier"]).lower() == word]
    unique: list[dict[str, Any]] = []
    for record in matches:
        if all(_norm(record["identifier"]) != _norm(item["identifier"]) for item in unique):
            unique.append(record)
    if not unique:
        listed = ", ".join(sorted({_short(record["identifier"]) for record in candidates})[:20])
        raise _Refusal(f"No layer of this stage matches {text!r}. Known layers: {listed}.")
    if len(unique) > 1:
        listed = ", ".join(record["identifier"] for record in unique[:5])
        raise _Refusal(f"{text!r} matches more than one layer ({listed}); use the full identifier.")
    record = unique[0]
    if record["role"] not in roles:
        allowed = {"root": "the root layer", "session": "the session layer", "referenced": "a referenced layer",
                   "sublayer": "a sublayer", "session_sublayer": "a session sublayer"}
        raise _Refusal(f"{record['display_name']} is {allowed.get(record['role'], record['role'])}, which this "
                       "action does not accept.")
    return record


def _match_muted(stage: Any, query: Any, muted: list[str]) -> str:
    if not isinstance(query, str) or not query.strip():
        raise _Refusal("Name the muted layer by identifier, file path or file name.")
    text = query.strip()
    wanted = {_norm(text)}
    root = stage.GetRootLayer()
    if not os.path.isabs(text) and root.realPath:
        wanted.add(_norm(os.path.join(os.path.dirname(root.realPath), text)))
    matches = [item for item in muted if item == text or _norm(item) in wanted]
    if not matches:
        matches = [item for item in muted if _short(item).lower() == text.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise _Refusal(f"{text!r} matches more than one muted layer; use the full identifier.")
    raise _Refusal(f"{text!r} is not muted. Muted layers: {', '.join(_short(item) for item in muted[:20])}.")


def _public_record(stage: Any, record: dict[str, Any], active: list[str], edit_id: str | None) -> dict[str, Any]:
    layer = record["layer"]
    rank = active.index(record["identifier"]) if layer is not None and record["identifier"] in active else None
    item: dict[str, Any] = {
        "strength_rank": rank,
        "identifier": _text(record["identifier"]),
        "display_name": record["display_name"],
        "role": record["role"],
        "authored_path": record["authored_path"],
        "parent": _text(record["parent"]) if record["parent"] else None,
        "depth": record["depth"],
        "offset": _offset(record["offset"]),
        "offset_to_root": _offset(record["offset_to_root"]),
        "muted": record["muted"],
        "missing": record["missing"],
        "is_edit_target": record["identifier"] == edit_id,
    }
    if record.get("cycle"):
        item["cycle"] = True
    if layer is None:
        item.update(anonymous=None, dirty=None, permission_to_edit=None, permission_to_save=None, file_writable=None,
                    read_only=None, prim_spec_count=None)
        return item
    count, capped = _count_prim_specs(layer)
    writable = _file_writable(layer)
    package = os.path.splitext(layer.realPath or "")[1].lower() == ".usdz"
    item.update(
        real_path=layer.realPath or None,
        anonymous=bool(layer.anonymous),
        dirty=bool(layer.dirty),
        permission_to_edit=bool(layer.permissionToEdit),
        permission_to_save=bool(layer.permissionToSave),
        file_writable=writable,
        read_only=bool(not layer.permissionToEdit or writable is False or package),
        prim_spec_count=count,
        prim_spec_count_capped=capped,
        sublayer_count=len(layer.subLayerPaths),
    )
    return item


def _file_writable(layer: Any) -> bool | None:
    real = layer.realPath
    if layer.anonymous or not real:
        return None
    if os.path.exists(real):
        return os.access(real, os.W_OK)
    folder = os.path.dirname(real)
    return os.access(folder, os.W_OK) if os.path.isdir(folder) else None


def _count_prim_specs(layer: Any, cap: int = MAX_SPEC_COUNT) -> tuple[int, bool]:
    count = 0
    stack = list(layer.rootPrims)
    while stack:
        spec = stack.pop()
        count += 1
        if count >= cap:
            return count, True
        stack.extend(spec.nameChildren)
        for variant_set in spec.variantSets.values():
            for variant in variant_set.variants.values():
                stack.extend(variant.primSpec.nameChildren)
    return count, False


def _referenced_layers(stage: Any, local: set[str], warnings: list[str]) -> dict[str, Any]:
    """Layers brought in by arcs (references, payloads, ...), found by scanning prim indexes (capped)."""
    found: dict[str, dict[str, Any]] = {}
    scanned = 0
    truncated = False
    unloaded: list[str] = []
    for prim in Usd.PrimRange.Stage(stage, Usd.PrimAllPrimsPredicate):
        if scanned >= MAX_SCAN_PRIMS:
            truncated = True
            break
        scanned += 1
        if prim.HasAuthoredPayloads() and not prim.IsLoaded() and len(unloaded) < MAX_LISTED:
            unloaded.append(str(prim.GetPath()))
        try:
            index = prim.GetPrimIndex()
            nodes = list(index.rootNode.children)
        except Exception:  # noqa: BLE001
            continue
        while nodes:
            node = nodes.pop()
            nodes.extend(node.children)
            arc = _arc_word(node.arcType)
            for layer in node.layerStack.layers:
                key = _norm(layer.identifier)
                if key in local:
                    continue
                entry = found.setdefault(key, {
                    "identifier": _text(layer.identifier),
                    "display_name": _short(layer.identifier),
                    "anonymous": bool(layer.anonymous),
                    "dirty": bool(layer.dirty),
                    "arcs": set(),
                    "used_by": [],
                })
                entry["arcs"].add(arc)
                if not node.IsDueToAncestor() and len(entry["used_by"]) < 5 and str(prim.GetPath()) not in entry["used_by"]:
                    entry["used_by"].append(str(prim.GetPath()))
    listed = []
    for entry in list(found.values())[:MAX_LISTED]:
        entry["arcs"] = sorted(entry["arcs"])
        listed.append(entry)
    other = [
        _text(layer.identifier) for layer in stage.GetUsedLayers()
        if _norm(layer.identifier) not in local and _norm(layer.identifier) not in found
    ]
    if truncated:
        warnings.append(f"Only the first {MAX_SCAN_PRIMS} prims were scanned for referenced layers.")
    return {
        "count": len(found),
        "layers": listed,
        "truncated": truncated or len(found) > MAX_LISTED,
        "other_used_layers": other[:MAX_LISTED],
        "unloaded_payload_prims": unloaded,
        "note": "These layers are weaker than every layer of the local layer stack for the prims that use them.",
    }


def _arc_word(arc_type: Any) -> str:
    name = getattr(arc_type, "name", None) or str(arc_type).rsplit(".", 1)[-1]
    return name.replace("ArcType", "").lower() or "unknown"


def _stack_explanation(layers: list[dict[str, Any]], referenced: dict[str, Any]) -> str:
    active = [item for item in layers if item["strength_rank"] is not None]
    order = " > ".join(item["display_name"] for item in active[:8]) + (" > ..." if len(active) > 8 else "")
    sentences = [
        f"Strongest to weakest: {order}.",
        "When two of these layers set the same attribute, the one higher in this list wins. A sublayer listed first "
        "in its parent is stronger than the next one, and a nested sublayer sits right after its parent.",
        "All of them together are the 'L' (local) in LIVRPS, so any opinion here beats inherits, variants, "
        "references, payloads and specializes.",
    ]
    if any(item["role"] == "session" for item in active):
        sentences.append("The session layer is the strongest but is never saved with the scene.")
    if any(not _offset_is_identity(item["offset_to_root"]) for item in active):
        sentences.append("Layer offsets shift or scale a layer's time samples only; they never change its strength.")
    muted = [item["display_name"] for item in layers if item["muted"]]
    if muted:
        sentences.append(f"Muted layers are ignored entirely: {', '.join(muted[:5])}.")
    if referenced["count"]:
        sentences.append(
            f"{referenced['count']} other layer(s) come in through references or payloads; they are weaker than every "
            "layer above for the prims that use them."
        )
    return " ".join(sentences)


def _strength_sentence(record: dict[str, Any], records: list[dict[str, Any]]) -> str:
    active = [item for item in records if item["layer"] is not None and not item["muted"] and not item.get("missing")]
    names = [item["display_name"] for item in active]
    if record["display_name"] not in names:
        return ""
    index = names.index(record["display_name"])
    stronger = names[:index]
    if not stronger:
        return "It is the strongest layer, so those edits win over every other layer."
    return (f"Layers above it ({', '.join(stronger[:4])}) are stronger: if they already set the same attribute, "
            "your edit there will not show.")


def _where_listed(record: dict[str, Any]) -> str:
    if record["role"] in ("root", "session"):
        return f"it is the {record['role']} layer"
    return f"sublayer of {_short(record['parent'])}" if record["parent"] else record["role"]


def _display(layer: Any, role: str) -> str:
    name = layer.GetDisplayName() or _short(layer.identifier)
    return f"session layer ({name})" if role == "session" else name


def _short(identifier: Any) -> str:
    text = str(identifier or "")
    if not text:
        return "an unknown layer"
    return text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _norm(identifier: Any) -> str:
    text = str(identifier or "")
    if text.startswith("anon:") or "://" in text:
        return text
    try:
        return os.path.normcase(os.path.normpath(os.path.abspath(text)))
    except Exception:  # noqa: BLE001
        return text


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))]) \
            == os.path.normcase(os.path.abspath(folder))
    except ValueError:  # different drives
        return False


def _offset(offset: Any) -> dict[str, Any]:
    if offset is None:
        return {"offset": 0.0, "scale": 1.0}
    return {"offset": _num(offset.offset), "scale": _num(offset.scale)}


def _offset_is_identity(offset: dict[str, Any]) -> bool:
    return offset.get("offset") in (0, 0.0) and offset.get("scale") in (1, 1.0)


def _num(value: Any) -> Any:
    number = float(value)
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() and abs(number) < 1e15 else number


# --- helpers: where_is --------------------------------------------------------------------------------------


def _missing_prim_message(stage: Any, path: Any) -> str:
    parent = path.GetParentPath()
    while parent != Sdf.Path.absoluteRootPath and not stage.GetPrimAtPath(parent):
        parent = parent.GetParentPath()
    holder = stage.GetPrimAtPath(parent) if parent != Sdf.Path.absoluteRootPath else None
    if holder and not holder.IsActive():
        return f"{path} is not on the stage because its ancestor {parent} is deactivated (active = false)."
    return f"Prim does not exist on the stage: {path}"


def _spec_entry(spec: Any, offset: Any, arcs: list[dict[str, Any]], ranks: dict[str, int], node_path: str) -> dict[str, Any]:
    layer = spec.layer
    arc = _arc_for_spec(arcs, layer.identifier, node_path)
    rank = ranks.get(_norm(layer.identifier))
    return {
        "layer": _text(layer.identifier),
        "display_name": _short(layer.identifier),
        "spec_path": str(spec.path),
        "layer_offset": _offset(offset),
        "arc_type": arc["arc_type"] if arc else ("local" if rank is not None else None),
        "liverps": arc["liverps"] if arc else ("L" if rank is not None else None),
        "in_local_layer_stack": rank is not None and (arc is None or arc["arc_type"] == "local"),
        "local_strength_rank": rank,
    }


def _where_prim(stage: Any, prim: Any, ranks: dict[str, int]) -> dict[str, Any]:
    arcs, _ = _arc_records(prim)
    getter = getattr(prim, "GetPrimStackWithLayerOffsets", None)
    pairs = list(getter()) if getter else [(spec, None) for spec in prim.GetPrimStack()]
    entries = []
    specifier_winner = type_winner = None
    for index, (spec, offset) in enumerate(pairs[:MAX_LISTED * 2]):
        entry = _spec_entry(spec, offset, arcs, ranks, str(spec.path))
        specifier = _enum(spec.specifier, "Specifier")
        fields = sorted(str(key) for key in spec.ListInfoKeys() if key != "specifier")
        entry.update(
            index=index,
            spec_type="prim",
            specifier=specifier,
            type_name=spec.typeName or None,
            fields=fields[:40],
            property_count=len(spec.properties),
            child_prim_count=len(spec.nameChildren),
            value_summary=f"{specifier} {spec.typeName or '(no type)'}; {len(spec.properties)} propert(ies), "
                          f"{len(fields)} field(s)",
        )
        if specifier_winner is None and specifier in ("def", "class"):
            specifier_winner = entry
        if type_winner is None and spec.typeName:
            type_winner = entry
        entries.append(entry)
    for entry in entries:
        entry["wins"] = []
        if entry is specifier_winner:
            entry["wins"].append("specifier")
        if entry is type_winner:
            entry["wins"].append("type_name")
    strongest = entries[0] if entries else None
    sentences = [f"{len(pairs)} layer spec(s) hold opinions on {prim.GetPath()}, listed strongest first."]
    if strongest:
        sentences.append(f"The strongest is {strongest['display_name']} ({strongest['spec_path']}, "
                         f"{strongest['arc_type'] or 'unknown arc'}): for any field it sets, its value wins.")
    if type_winner:
        sentences.append(f"The prim type {prim.GetTypeName()} comes from {type_winner['display_name']}, the strongest "
                         "spec that names a type.")
    if specifier_winner and specifier_winner is not strongest:
        sentences.append(f"It is 'def' because of {specifier_winner['display_name']}; 'over' specs above it only add "
                         "opinions without defining the prim.")
    if any(entry["arc_type"] not in (None, "local") for entry in entries):
        sentences.append("Specs reached through arcs (references, variants, ...) are weaker than every local layer, "
                         "following LIVRPS.")
    sentences.append("List-edited metadata such as references or apiSchemas combine across all of these specs "
                     "instead of one simply winning.")
    return {
        "prim_path": str(prim.GetPath()),
        "type_name": prim.GetTypeName() or None,
        "specifier": _enum(prim.GetSpecifier(), "Specifier"),
        "opinions": entries,
        "opinion_count": len(pairs),
        "winner": strongest,
        "explanation": " ".join(sentences),
    }


def _where_property(stage: Any, prim: Any, name: str, time: Any, ranks: dict[str, int], warnings: list[str]) -> dict[str, Any]:
    prop = prim.GetProperty(name)
    if not prop or not prop.IsValid():
        have = sorted(prim.GetPropertyNames())[:20]
        raise _Refusal(f"{prim.GetPath()} has no property {name!r}. It has: {have or 'none'}.")
    arcs, _ = _arc_records(prim)
    if prim.GetAttribute(name):
        info = explain_value(stage, str(prim.GetPath()), name, time)
        entries = []
        for item in info["property_stack"]:
            rank = ranks.get(_norm(item["layer"]))
            value = "a value block (None)" if item["blocked"] else (
                f"default {item['default']}" if item["has_default"] else "no default")
            if item["time_sample_count"]:
                value += f", {item['time_sample_count']} time sample(s)"
            entries.append({
                "index": item["index"],
                "layer": _text(item["layer"]),
                "display_name": _short(item["layer"]),
                "spec_path": item["spec_path"],
                "spec_type": "attribute",
                "layer_offset": item["layer_offset"],
                "arc_type": item["arc_type"] or ("local" if rank is not None else None),
                "liverps": item["liverps"] or ("L" if rank is not None else None),
                "in_local_layer_stack": rank is not None and item["arc_type"] in (None, "local"),
                "local_strength_rank": rank,
                "value_summary": value,
                "default": item["default"],
                "time_sample_count": item["time_sample_count"],
                "wins": item["wins"],
            })
        winner = next((entry for entry in entries if entry["wins"]), None)
        return {
            "prim_path": str(prim.GetPath()),
            "property": name,
            "property_type": "attribute",
            "value": info["value"],
            "value_source": info["source"],
            "time": info["time"],
            "opinions": entries,
            "opinion_count": info["property_stack_count"],
            "winner": winner,
            "explanation": info["why"],
        }
    rel = prim.GetRelationship(name)
    pairs = list(rel.GetPropertyStackWithLayerOffsets(Usd.TimeCode.Default()))
    entries = []
    explicit_winner = None
    for index, (spec, offset) in enumerate(pairs[:MAX_LISTED * 2]):
        entry = _spec_entry(spec, offset, arcs, ranks, str(spec.path.GetPrimPath()))
        targets = spec.targetPathList
        explicit = bool(targets.isExplicit)
        entry.update(index=index, spec_type="relationship", explicit=explicit,
                     value_summary=_list_op_summary(targets), wins=False)
        if explicit and explicit_winner is None:
            explicit_winner = entry
        entries.append(entry)
    targets = [str(path) for path in rel.GetTargets()]
    if explicit_winner is not None:
        explicit_winner["wins"] = True
        why = (f"Relationship targets are list-edited. {explicit_winner['display_name']} sets an explicit target list, "
               "which replaces everything weaker; stronger prepend/append/delete edits are applied on top of it.")
    else:
        why = ("Relationship targets are list-edited: no layer sets an explicit list, so every layer's prepend, "
               "append and delete edits are combined, strongest first.")
    return {
        "prim_path": str(prim.GetPath()),
        "property": name,
        "property_type": "relationship",
        "value": targets[:MAX_LISTED],
        "opinions": entries,
        "opinion_count": len(pairs),
        "winner": explicit_winner,
        "explanation": why,
    }


def _list_op_summary(list_op: Any) -> str:
    parts = []
    for label, attr in (("explicit", "explicitItems"), ("prepend", "prependedItems"), ("append", "appendedItems"),
                        ("delete", "deletedItems"), ("add", "addedItems"), ("reorder", "orderedItems")):
        items = list(getattr(list_op, attr, []) or [])
        if items:
            parts.append(f"{label} {', '.join(str(item) for item in items[:5])}{' ...' if len(items) > 5 else ''}")
    if not parts:
        return "explicit (empty)" if getattr(list_op, "isExplicit", False) else "no targets"
    return "; ".join(parts)


def _enum(value: Any, prefix: str) -> str:
    name = getattr(value, "name", None) or str(value).rsplit(".", 1)[-1]
    return name[len(prefix):].lower() if name.startswith(prefix) else name


# --- helpers: flatten ---------------------------------------------------------------------------------------


def _flatten_text(stage: Any, path: Any, warnings: list[str]) -> str:
    whole = path == Sdf.Path.absoluteRootPath
    mask = Usd.StagePopulationMask.All() if whole else Usd.StagePopulationMask([path])
    try:
        masked = Usd.Stage.OpenMasked(stage.GetRootLayer(), stage.GetSessionLayer(), mask, Usd.Stage.LoadNone)
    except Exception:  # noqa: BLE001 - an overload this build lacks
        masked = Usd.Stage.OpenMasked(stage.GetRootLayer(), mask, Usd.Stage.LoadNone)
        warnings.append("The session layer could not be included in the preview.")
    muted = [str(item) for item in stage.GetMutedLayers()]
    if muted:
        masked.MuteAndUnmuteLayers(muted, [])
    masked.SetLoadRules(stage.GetLoadRules())
    flat = masked.Flatten(False)
    if whole:
        return flat.ExportToString()
    out = Sdf.Layer.CreateAnonymous("flatten-preview")
    parent = path.GetParentPath()
    if parent != Sdf.Path.absoluteRootPath:
        Sdf.CreatePrimInLayer(out, parent)
    if not flat.GetPrimAtPath(path) or not Sdf.CopySpec(flat, path, out, path):
        raise _Refusal(f"{path} has no flattened content (it may be inactive or unloaded).")
    return out.ExportToString()


# --- helpers: moving opinions -------------------------------------------------------------------------------


def _describe_move(src: Any, dst: Any, path: Any, include_children: bool, names: list[str] | None) -> dict[str, Any]:
    spec = src.GetPrimAtPath(path)
    dst_spec = dst.GetPrimAtPath(path)
    if names is not None:
        props = names
        fields: list[str] = []
        children: list[str] = []
        variant_sets: list[str] = []
    else:
        props = sorted(str(key) for key in spec.properties.keys())
        fields = sorted(str(key) for key in spec.ListInfoKeys())
        children = [str(key) for key in spec.nameChildren.keys()] if include_children else []
        variant_sets = [str(key) for key in spec.variantSets.keys()]
    conflicts: list[str] = []
    if dst_spec:
        conflicts += [f".{name}" for name in props if dst_spec.properties.get(name)]
        conflicts += [f"[{key}]" for key in fields if key != "specifier" and dst_spec.HasInfo(key)
                      and dst_spec.GetInfo(key) != spec.GetInfo(key)]
        conflicts += [f"/{name}" for name in children if dst_spec.nameChildren.get(name)]
    kept = [str(key) for key in spec.nameChildren.keys()] if names is None and not include_children else []
    return {
        "specifier": _enum(spec.specifier, "Specifier"),
        "fields": fields,
        "properties": props[:MAX_LISTED * 4],
        "child_prims": children[:MAX_LISTED],
        "variant_sets": variant_sets,
        "child_prims_left_in_source": kept[:MAX_LISTED],
        "destination_had_spec": bool(dst_spec),
        "conflicts": conflicts[:MAX_LISTED],
    }


def _ensure_parent(layer: Any, path: Any) -> None:
    parent = path.GetParentPath()
    if parent != Sdf.Path.absoluteRootPath and not layer.GetPrimAtPath(parent):
        Sdf.CreatePrimInLayer(layer, parent)


def _remove_spec(layer: Any, path: Any) -> None:
    edit = Sdf.BatchNamespaceEdit()
    edit.Add(Sdf.NamespaceEdit.Remove(path))
    if not layer.Apply(edit):
        raise RuntimeError(f"could not remove {path} from {_short(layer.identifier)}")


def _moving_part(src: Any, path: Any, include_children: bool, names: list[str] | None) -> Any:
    """An in-memory layer holding exactly the specs that move (ancestors as empty overs)."""
    part = Sdf.Layer.CreateAnonymous("lm-moving")
    _ensure_parent(part, path)
    if names is None:
        if not Sdf.CopySpec(src, path, part, path):
            raise RuntimeError(f"could not copy {path}")
        if not include_children:
            spec = part.GetPrimAtPath(path)
            for child in [str(key) for key in spec.nameChildren.keys()]:
                _remove_spec(part, path.AppendChild(child))
        return part
    Sdf.CreatePrimInLayer(part, path)
    for name in names:
        prop_path = path.AppendProperty(name)
        if not Sdf.CopySpec(src, prop_path, part, prop_path):
            raise RuntimeError(f"could not copy {prop_path}")
    return part


def _destination_part(dst: Any, path: Any) -> Any:
    part = Sdf.Layer.CreateAnonymous("lm-destination")
    if dst.GetPrimAtPath(path):
        _ensure_parent(part, path)
        if not Sdf.CopySpec(dst, path, part, path):
            raise RuntimeError(f"could not copy {path} from the destination")
    return part


def _value_rules(layer: Any, root: Any) -> dict[Any, set]:
    """For each attribute spec with a value in the stronger part: the time samples it keeps (empty: default only)."""
    rules: dict[Any, set] = {}

    def visit(path: Any) -> None:
        spec = layer.GetAttributeAtPath(path)
        if not spec:
            return
        times = set(layer.ListTimeSamplesForPath(path))
        if times:
            rules[path] = times
        elif spec.HasDefaultValue():
            rules[path] = set()

    if layer.GetObjectAtPath(root):
        layer.Traverse(root, visit)
    return rules


def _merge(strong: Any, weak: Any, path: Any) -> Any:
    """Merge weak into strong the way composition combines two layers; return the merged layer (strong)."""
    rules = _value_rules(strong, path)
    UsdUtils.StitchLayers(strong, weak)
    # StitchLayers unions time samples, but composition takes all samples from the strongest spec that has any,
    # and a stronger default beats weaker time samples, so drop the weaker samples again.
    for attr_path, keep in rules.items():
        for time in strong.ListTimeSamplesForPath(attr_path):
            if time not in keep:
                strong.EraseTimeSample(attr_path, time)
    return strong


def _remove_from_source(src: Any, path: Any, include_children: bool, names: list[str] | None) -> None:
    spec = src.GetPrimAtPath(path)
    if names is not None:
        for name in names:
            prop = spec.properties.get(name)
            if prop:
                spec.RemoveProperty(prop)
        return
    if include_children:
        _remove_spec(src, path)
        return
    for key in list(spec.ListInfoKeys()):
        if key != "specifier":
            spec.ClearInfo(key)
    spec.specifier = Sdf.SpecifierOver
    for prop in list(spec.properties):
        spec.RemoveProperty(prop)
    for name in [str(key) for key in spec.variantSets.keys()]:
        del spec.variantSets[name]


def _move_specs(src: Any, dst: Any, path: Any, src_stronger: bool, include_children: bool, names: list[str] | None) -> None:
    moving = _moving_part(src, path, include_children, names)
    existing = _destination_part(dst, path)
    merged = _merge(moving, existing, path) if src_stronger else _merge(existing, moving, path)
    _ensure_parent(dst, path)
    if not Sdf.CopySpec(merged, path, dst, path):
        raise RuntimeError(f"could not write {path} into {_short(dst.identifier)}")
    _remove_from_source(src, path, include_children, names)


def _anchor(layer: Any, asset_path: str) -> str:
    if not asset_path or layer.anonymous or asset_path.startswith("anon:"):
        return asset_path
    try:
        return Sdf.ComputeAssetPathRelativeToLayer(layer, asset_path) or asset_path
    except Exception:  # noqa: BLE001
        return asset_path


def _simulate_move(
    stage: Any, records: list[dict[str, Any]], src: Any, dst: Any, path: Any, src_stronger: bool,
    include_children: bool, names: list[str] | None,
) -> dict[str, Any]:
    """Run the move on in-memory copies of the local layer stack and compare composed values before and after."""
    offsets = {_norm(record["identifier"]): record["offset_to_root"] for record in records if record["layer"] is not None}
    root = stage.GetRootLayer()
    sim_root = Sdf.Layer.CreateAnonymous("lm-sim-root")
    for key in ("timeCodesPerSecond", "framesPerSecond", "startTimeCode", "endTimeCode", "expressionVariables"):
        try:
            if root.pseudoRoot.HasInfo(key):
                sim_root.pseudoRoot.SetInfo(key, root.pseudoRoot.GetInfo(key))
        except Exception:  # noqa: BLE001 - a field this build does not know
            continue
    copies: dict[str, Any] = {}
    keep_alive = []
    for layer in stage.GetLayerStack(True):
        copy = Sdf.Layer.CreateAnonymous("lm-sim")
        copy.TransferContent(layer)
        UsdUtils.ModifyAssetPaths(copy, lambda asset, source=layer: _anchor(source, asset))
        copy.subLayerPaths = []
        copies[_norm(layer.identifier)] = copy
        keep_alive.append(copy)
        sim_root.subLayerPaths.append(copy.identifier)
        offset = offsets.get(_norm(layer.identifier))
        if offset is not None and (offset.offset != 0.0 or offset.scale != 1.0):
            sim_root.subLayerOffsets[len(sim_root.subLayerPaths) - 1] = offset
    sim = Usd.Stage.OpenMasked(sim_root, Usd.StagePopulationMask([path]), Usd.Stage.LoadNone)
    sim.SetLoadRules(stage.GetLoadRules())
    before = _fingerprint(sim, path)
    _move_specs(copies[_norm(src.identifier)], copies[_norm(dst.identifier)], path, src_stronger, include_children, names)
    after = _fingerprint(sim, path)
    return {"changes": _compare(before, after), "compared": before.get("__count__", 0)}


def _fingerprint(stage: Any, path: Any) -> dict[str, Any]:
    """Composed state of the prim subtree at path: what a user would see (capped)."""
    prim = stage.GetPrimAtPath(path)
    out: dict[str, Any] = {}
    if not prim or not prim.IsValid():
        out[str(path)] = {"exists": False}
        out["__count__"] = 0
        return out
    prims = 0
    props = 0
    for item in Usd.PrimRange(prim, Usd.PrimAllPrimsPredicate):
        if prims >= MAX_COMPARE_PRIMS or props >= MAX_COMPARE_PROPERTIES:
            break
        prims += 1
        record: dict[str, Any] = {
            "exists": True,
            "type": item.GetTypeName() or None,
            "specifier": _enum(item.GetSpecifier(), "Specifier"),
            "active": item.IsActive(),
            "instanceable": item.IsInstanceable(),
            "kind": to_jsonable(item.GetMetadata("kind")),
            "children": [str(child.GetName()) for child in item.GetAllChildren()],
            "applied_schemas": [str(name) for name in item.GetAppliedSchemas()],
            "variant_selections": to_jsonable(dict(item.GetVariantSets().GetAllVariantSelections())),
            "custom_data": to_jsonable(item.GetCustomData()),
        }
        for attr in item.GetAttributes():
            props += 1
            times = attr.GetTimeSamples()
            record["." + attr.GetName()] = {
                "value": to_jsonable(attr.Get()),
                "time_samples": len(times),
                "samples": [[_num(t), to_jsonable(attr.Get(t))] for t in list(times)[:3]],
            }
        for rel in item.GetRelationships():
            props += 1
            record["." + rel.GetName()] = {"targets": [str(target) for target in rel.GetTargets()]}
        out[str(item.GetPath())] = record
    out["__count__"] = props
    return out


def _compare(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for prim_path in [key for key in before if key != "__count__"] + [key for key in after if key not in before and key != "__count__"]:
        old = before.get(prim_path, {"exists": False})
        new = after.get(prim_path, {"exists": False})
        for field in list(old) + [key for key in new if key not in old]:
            if old.get(field) != new.get(field):
                changes.append({
                    "prim_path": prim_path,
                    "what": field.lstrip(".") if field.startswith(".") else field,
                    "kind": "property" if field.startswith(".") else "prim",
                    "before": old.get(field),
                    "after": new.get(field),
                })
    return changes


def _apply_move_on_stage(
    stage: Any, path: Any, src: Any, dst: Any, src_stronger: bool, include_children: bool, names: list[str] | None,
    undo_stack: Any, warnings: list[str], explanation: str, common: dict[str, Any],
) -> dict[str, Any]:
    from . import undo  # pure pxr

    action = "move_opinions"
    before_state = _fingerprint(stage, path)
    try:
        snapshots = [undo.SpecSnapshot.capture(src, [path]), undo.SpecSnapshot.capture(dst, [path])]
    except Exception as exc:  # noqa: BLE001
        return _result(action, warnings=warnings, error=f"Could not snapshot the layers, so nothing was changed: {exc}",
                       **common)
    try:
        with Sdf.ChangeBlock():
            _move_specs(src, dst, path, src_stronger, include_children, names)
    except Exception as exc:  # noqa: BLE001
        problems = []
        for snapshot in snapshots:
            try:
                if not snapshot.matches():
                    snapshot.restore()
            except Exception as restore_exc:  # noqa: BLE001
                problems.append(str(restore_exc))
        rolled_back = not problems and all(snapshot.matches() for snapshot in snapshots)
        note = "both layers were rolled back" if rolled_back else f"the rollback was incomplete ({'; '.join(problems)})"
        return _result(action, warnings=warnings, error=f"Moving failed ({note}): {exc}", applied=not rolled_back,
                       rolled_back=rolled_back, **common)
    after_state = _fingerprint(stage, path)
    actual = _compare(before_state, after_state)
    result_common = dict(common)
    result_common["composed_changes"] = actual[:MAX_CHANGES]
    result_common["composed_values_compared"] = before_state.get("__count__", 0)
    if actual:
        warnings.append(f"{len(actual)} composed value(s) changed (see composed_changes).")
    undo_info: dict[str, Any] = {"recorded": False}
    if undo_stack is not None:
        try:
            after = [snapshot.recapture() for snapshot in snapshots]
            entry = undo.UndoEntry(f"move opinions on {path}", [path], snapshots, after,
                                   stage_ref=undo.LayerRef(stage.GetRootLayer()))
            undo_stack.push(entry)
            undo_info = {"recorded": True, "label": entry.label}
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"The move worked but could not be recorded for undo: {exc}")
    for layer in (src, dst):
        warnings.append(f"{_short(layer.identifier)} now has unsaved changes; save it to keep the move.")
    return _result(
        action,
        applied=True,
        warnings=warnings,
        explanation=explanation.replace("would move", "moved"),
        undo=undo_info,
        **result_common,
    )


# --- helpers: results ---------------------------------------------------------------------------------------


class _DirtyGuard:
    """Remembers which layers were dirty so a dry run can prove it changed none of them."""

    def __init__(self, stage: Any) -> None:
        self._states = {layer.identifier: bool(layer.dirty) for layer in stage.GetUsedLayers()}
        self._layers = list(stage.GetUsedLayers())
        self.dirtied: list[str] = []

    def check(self, warnings: list[str]) -> None:
        self.dirtied = [
            _text(layer.identifier) for layer in self._layers
            if not layer.expired and bool(layer.dirty) != self._states.get(layer.identifier, False)
        ]
        if self.dirtied:
            warnings.append(f"Unexpected: the dry run marked {', '.join(self.dirtied[:5])} as changed.")

    def extra(self) -> dict[str, Any]:
        return {"dry_run_dirtied_layers": self.dirtied}


def _result(
    action: str,
    *,
    dry: bool = False,
    applied: bool = False,
    warnings: list[str] | tuple = (),
    explanation: str = "",
    error: str | None = None,
    read_only: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    unique: list[str] = []
    for warning in warnings:
        text = _text(warning)
        if text not in unique:
            unique.append(text)
    result: dict[str, Any] = {
        "ok": error is None,
        "action": action,
        "dry_run": bool(dry),
        "applied": bool(applied),
        "written": bool(applied),
    }
    if read_only:
        result["read_only"] = True
    if error is not None:
        result["error"] = _text(error)
    result["explanation"] = explanation or (_text(error) if error else "")
    result["warnings"] = unique[:MAX_WARNINGS]
    if len(unique) > MAX_WARNINGS:
        result["warnings_truncated"] = len(unique) - MAX_WARNINGS
    result.update(extra)
    if error is None and dry and not read_only:
        result["next_step"] = "Dry run: nothing was changed. Call again with dry_run=False to apply it."
    return result
