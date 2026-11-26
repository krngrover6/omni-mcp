"""click_snapshot / click_changes: "what did my click write into the file?" (read-only).

A beginner clicks Create > Shape > Cube or types a value in the Property panel and cannot see what that did to the
USD file. click_snapshot remembers the text of every layer in the open stage's layer stack (root, its sublayers, and
the session layer separately); after the clicks, click_changes shows, per layer, the lines that were added or removed
(a unified diff) and explains each added line in plain words (usda_explain). Layers added or removed since the
snapshot are listed too. Nothing is saved or changed; the snapshot lives in memory inside the app until the next one.
"""
from __future__ import annotations

import difflib
import time
from typing import Any

MAX_LAYER_CHARS = 3_000_000
MAX_DIFF_LINES = 120
_SNAPSHOTS: dict[str, dict[str, Any]] = {}


def _layers(stage: Any, include_session: bool) -> list[Any]:
    layers = list(stage.GetLayerStack(includeSessionLayers=False))
    if include_session and stage.GetSessionLayer() is not None:
        layers.append(stage.GetSessionLayer())
    return layers


def _key(stage: Any) -> str:
    return stage.GetRootLayer().identifier


def _name(layer: Any, stage: Any) -> str:
    if layer == stage.GetSessionLayer():
        return "session layer (never saved)"
    if layer == stage.GetRootLayer():
        return f"root layer {layer.GetDisplayName()}"
    return layer.GetDisplayName() or layer.identifier


def click_snapshot(stage: Any, include_session: Any = False) -> dict[str, Any]:
    """Remember the open stage's layer texts now, then do your clicks and call click_changes to see exactly what they
    wrote. include_session=True also watches the session layer (Isaac writes there while playing). Read-only."""
    texts: dict[str, str] = {}
    skipped = []
    for layer in _layers(stage, include_session is True):
        text = layer.ExportToString()
        if len(text) > MAX_LAYER_CHARS:
            skipped.append(layer.GetDisplayName())
            continue
        texts[layer.identifier] = text
    _SNAPSHOTS[_key(stage)] = {"texts": texts, "time": time.time(), "include_session": include_session is True}
    return {"ok": True, "read_only": True, "layers": [_name(l, stage) for l in _layers(stage, include_session is True)],
            "skipped_too_big": skipped,
            "next_step": "Now do your clicks in the app (no need to save), then call click_changes."}


def click_changes(stage: Any, explain: Any = True) -> dict[str, Any]:
    """What changed in each layer since click_snapshot: added and removed lines per layer (a diff), each added line
    explained in plain words, plus layers added or removed. Read-only; the snapshot is kept, so you can call it again."""
    snap = _SNAPSHOTS.get(_key(stage))
    if snap is None:
        return {"ok": False, "error": "No snapshot for this stage yet: call click_snapshot first, then click."}
    from .usda_explain import explain_usda

    before = snap["texts"]
    now_layers = _layers(stage, snap["include_session"])
    now_ids = {layer.identifier for layer in now_layers}
    changes = []
    for layer in now_layers:
        new = layer.ExportToString()
        old = before.get(layer.identifier)
        if old is None:
            changes.append({"layer": _name(layer, stage), "status": "added since the snapshot",
                            "means": "A new layer joined the layer stack (e.g. Layer panel > Create Sublayer)."})
            continue
        if new == old:
            continue
        diff = [line for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
                if not line.startswith(("---", "+++"))]
        added_lines: list[int] = []
        number = 0
        for line in diff:
            if line.startswith("@@"):
                number = int(line.split("+")[1].split(",")[0].split(" ")[0]) - 1
            elif line.startswith("+"):
                number += 1
                added_lines.append(number)
        meanings: dict[int, str] = {}
        if explain is True and added_lines:
            explained = explain_usda(text=new)
            if explained.get("ok"):
                wanted = set(added_lines)
                meanings = {item["line"]: item["means"] for item in explained["explanation"] if item["line"] in wanted}
        shown = []
        number = 0
        for line in diff[:MAX_DIFF_LINES]:
            if line.startswith("@@"):
                number = int(line.split("+")[1].split(",")[0].split(" ")[0]) - 1
                continue
            entry: dict[str, Any] = {"change": "added" if line.startswith("+") else "removed", "text": line[1:].rstrip()}
            if line.startswith("+"):
                number += 1
                entry["line"] = number
                if number in meanings:
                    entry["means"] = meanings[number]
            shown.append(entry)
        changes.append({"layer": _name(layer, stage), "status": "changed",
                        "added": sum(1 for e in shown if e["change"] == "added"),
                        "removed": sum(1 for e in shown if e["change"] == "removed"),
                        "lines": shown, "truncated": len(diff) > MAX_DIFF_LINES,
                        "dirty": bool(layer.dirty)})
    for identifier in before:
        if identifier not in now_ids:
            changes.append({"layer": identifier.replace("\\", "/").rsplit("/", 1)[-1], "status": "removed since the "
                            "snapshot", "means": "This layer is no longer in the layer stack."})
    seconds = int(time.time() - snap["time"])
    return {
        "ok": True, "read_only": True, "since_seconds": seconds, "changed_layers": len(changes), "changes": changes,
        "summary": ("Nothing changed in the watched layers since the snapshot." if not changes else
                    f"{len(changes)} layer(s) changed. Each added line says what it means; 'dirty' = not saved yet."),
        "note": "Composer writes into the authoring layer (the pen icon in the Layer panel). Edits not saved yet still "
                "show here; saving writes this same text to the file.",
    }
