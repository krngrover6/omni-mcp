"""Live data link: real numbers drive the scene while it plays - the "digital twin" part of a digital twin.

A DIGITAL TWIN is a virtual copy of a real machine that follows the real one's data. Here the data comes from a
file on disk:
- a CSV table with a `time` column (seconds) and value columns, replayed against the simulation clock
  (e.g. yesterday's belt speeds: time,speed / 0,0.2 / 10,0.5 ...), values interpolated between rows; or
- a JSON file such as {"speed": 0.4} that another program keeps overwriting (a live feed); it is re-read
  every `period` seconds.

Each mapping sends one column to one target: a graph variable (e.g. the conveyor's Velocity), an OmniGraph node
input, or a USD attribute (written in the session layer, so it is never saved and Stop/stop puts it back),
with value = column * scale + offset, clamped to [min, max] when given.
"""

from __future__ import annotations

import csv
import json
import math
import time
from pathlib import Path
from typing import Any

from . import joint_control as jc

MAX_ROWS = 100000
MAX_MAPPINGS = 8
_LINK: dict[str, Any] = {}


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, float]]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = [c.strip() for c in reader.fieldnames or []]
        rows = []
        for index, raw in enumerate(reader):
            if index >= MAX_ROWS:
                break
            row = {}
            for key, value in raw.items():
                try:
                    row[(key or "").strip()] = float(value)
                except (TypeError, ValueError):
                    continue
            if "time" in row:
                rows.append(row)
    rows.sort(key=lambda r: r["time"])
    return columns, rows


def _read_json(path: Path) -> dict[str, float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("the JSON file must hold an object such as {\"speed\": 0.4}")
    return {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _at(rows: list[dict[str, float]], column: str, t: float) -> float | None:
    usable = [r for r in rows if column in r]
    if not usable:
        return None
    if t <= usable[0]["time"]:
        return usable[0][column]
    for a, b in zip(usable, usable[1:]):
        if a["time"] <= t <= b["time"]:
            span = b["time"] - a["time"] or 1.0
            return a[column] + (b[column] - a[column]) * (t - a["time"]) / span
    return usable[-1][column]


def _source(path_text: str) -> tuple[Path | None, str | None]:
    path = Path(path_text).expanduser()
    if path.suffix.lower() not in (".csv", ".json"):
        return None, "source must be a .csv (time + value columns) or .json ({name: number}) file."
    if not path.is_file():
        return None, f"No file {path}."
    return path, None


def preview_source(source: str) -> dict[str, Any]:
    """Read-only: what a data file holds (columns, rows, ranges), to choose a mapping."""
    path, problem = _source(source)
    if problem:
        return {"ok": False, "error": problem}
    try:
        if path.suffix.lower() == ".csv":
            columns, rows = _read_csv(path)
            if not rows:
                return {"ok": False, "error": "The CSV needs a 'time' column (seconds) with numbers."}
            values = [c for c in columns if c != "time"]
            return {"ok": True, "read_only": True, "kind": "csv (replayed against the simulation clock)",
                    "columns": values, "rows": len(rows), "seconds": [rows[0]["time"], rows[-1]["time"]],
                    "ranges": {c: [min(r[c] for r in rows if c in r), max(r[c] for r in rows if c in r)]
                               for c in values if any(c in r for r in rows)},
                    "first_rows": rows[:3]}
        values = _read_json(path)
        return {"ok": True, "read_only": True, "kind": "json (live: re-read every period)", "columns": sorted(values),
                "now": values}
    except (OSError, ValueError, csv.Error) as exc:
        return {"ok": False, "error": f"Could not read {path.name}: {exc}"}


def _check_mappings(mappings: Any, columns: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(mappings, list) or not 1 <= len(mappings) <= MAX_MAPPINGS:
        return [], f"mappings must be a list of 1-{MAX_MAPPINGS} objects."
    clean = []
    for item in mappings:
        if not isinstance(item, dict) or not {"column", "target"} <= set(item):
            return [], "each mapping needs column and target, e.g. {'column': 'speed', 'target': " \
                       "'/World/FactoryCell/BeltGraph.graph:variable:Velocity'}."
        if item["column"] not in columns:
            return [], f"column {item['column']!r} is not in the file ({columns})."
        target = str(item["target"])
        if not target.startswith("/") or "." not in target:
            return [], f"target {target!r} must be <prim path>.<attribute>, e.g. /World/Light.inputs:intensity."
        try:
            scale, offset = float(item.get("scale", 1.0)), float(item.get("offset", 0.0))
            low = float(item["min"]) if "min" in item else -math.inf
            high = float(item["max"]) if "max" in item else math.inf
        except (TypeError, ValueError):
            return [], "scale, offset, min and max must be numbers."
        clean.append({"column": item["column"], "target": target, "scale": scale, "offset": offset,
                      "min": low, "max": high})
    return clean, None


def _write(target: str, value: float) -> str:
    """Send one value to a graph variable, a node input or a USD attribute; returns how it was written."""
    prim_path, _, attribute = target.partition(".")
    og = jc._kit_module("omni.graph.core")
    if attribute.startswith("graph:variable:") and og is not None:
        graph = og.get_graph_by_path(prim_path)
        variable = graph.find_variable(attribute[len("graph:variable:"):]) if graph else None
        if variable is not None:
            context = graph.get_default_graph_context()
            # Seen live: a value written here outlived data_link_stop AND Stop/Play (the belt stayed at 0), so the
            # first write remembers the original and stop_link puts it back.
            originals = _LINK.setdefault("variable_originals", {})
            if target not in originals:
                originals[target] = (variable, context, variable.get(context))
            variable.set(context, value)
            return "graph variable"
    if attribute.startswith("inputs:") and og is not None:
        try:
            og.Controller.set(og.Controller.attribute(target), value)
            return "node input"
        except Exception:  # noqa: BLE001 - not an OmniGraph node: fall back to USD
            pass
    usd = jc._kit_module("omni.usd")
    stage = usd.get_context().get_stage() if usd else None
    if stage is None:
        raise RuntimeError("no open stage")
    from pxr import Usd

    prim = stage.GetPrimAtPath(prim_path)
    attr = prim.GetAttribute(attribute) if prim else None
    if not attr:
        raise ValueError(f"{target} does not exist")
    with Usd.EditContext(stage, stage.GetSessionLayer()):  # never saved; data_link_stop clears it
        attr.Set(type(attr.Get())(value) if attr.Get() is not None else value)
    _LINK.setdefault("session_attrs", set()).add(target)
    return "USD attribute (session layer)"


def start_link(source: str, mappings: Any, period: float = 0.5, *, dry_run: bool = True) -> dict[str, Any]:
    """KIT-ONLY, SIMULATION STATE. Drive scene values from a CSV (replayed on the simulation clock) or a JSON file
    (re-read every `period` seconds) while the simulation plays. dry_run=True only checks the file and mappings."""
    base = {"action": "data_link_start", "source": source, "dry_run": jc._dry(dry_run)}
    preview = preview_source(source)
    if not preview.get("ok"):
        return {**base, **preview}
    clean, problem = _check_mappings(mappings, preview["columns"])
    if problem:
        return {**base, "ok": False, "error": problem}
    if isinstance(period, bool) or not isinstance(period, (int, float)) or not 0.05 <= period <= 10:
        return {**base, "ok": False, "error": "period must be 0.05-10 seconds."}
    if _LINK.get("running"):
        return {**base, "ok": False, "error": "A data link is already running: data_link_stop first."}
    # Seen live: -inf showed as "-Infinity", which is not valid JSON (strict clients such as Codex reject it).
    shown = [{**m, "min": None if math.isinf(m["min"]) else m["min"], "max": None if math.isinf(m["max"]) else m["max"]}
             for m in clean]
    plan = {**base, "ok": True, "kind": preview["kind"], "mappings": shown}
    if base["dry_run"]:
        plan["next_step"] = "Dry run: nothing is linked. Press Play, then call again with dry_run=False."
        return plan
    path = Path(source).expanduser()
    is_csv = path.suffix.lower() == ".csv"
    rows = _read_csv(path)[1] if is_csv else None
    timeline = jc._kit_module("omni.timeline")
    clock = timeline.get_timeline_interface() if timeline else None
    _LINK.clear()
    _LINK.update({"running": True, "updates": 0, "last": {}, "errors": [], "next": 0.0, "mappings": clean,
                  "source": str(path), "session_attrs": set()})

    def on_update(_event: Any) -> None:
        if not _LINK.get("running") or clock is None or not clock.is_playing():
            return
        now = time.monotonic()
        if now < _LINK["next"]:
            return
        _LINK["next"] = now + period
        try:
            data = None if is_csv else _read_json(path)
        except (OSError, ValueError) as exc:  # the writer may be mid-write: try again next period
            _LINK["errors"].append(f"read: {exc}"[:200])
            return
        for mapping in clean:
            raw = _at(rows, mapping["column"], clock.get_current_time()) if is_csv else data.get(mapping["column"])
            if raw is None:
                continue
            value = min(mapping["max"], max(mapping["min"], raw * mapping["scale"] + mapping["offset"]))
            try:
                how = _write(mapping["target"], value)
                _LINK["last"][mapping["target"]] = {"value": round(value, 4), "via": how}
            except Exception as exc:  # noqa: BLE001
                _LINK["errors"].append(f"{mapping['target']}: {exc}"[:200])
        _LINK["updates"] += 1
        del _LINK["errors"][:-10]

    app = jc._kit_module("omni.kit.app")
    if app is None:
        _LINK.clear()
        return {**base, "ok": False, "error": "Kit is not available: run this in Isaac Sim."}
    _LINK["handle"] = app.get_app().get_update_event_stream().create_subscription_to_pop(on_update,
                                                                                       name="mcp_data_link")
    return {**plan, "running": True,
            "note": "Values update while the simulation plays. data_link_status shows the latest; data_link_stop "
                    "ends it and clears the session-layer values."}


def link_status() -> dict[str, Any]:
    """KIT-ONLY, READ-ONLY. Whether the data link runs, how many updates, the latest value per target, errors."""
    if not _LINK:
        return {"ok": True, "read_only": True, "running": False, "message": "No data link has been started."}
    return {"ok": True, "read_only": True, "running": bool(_LINK.get("running")), "source": _LINK.get("source"),
            "updates": _LINK.get("updates", 0), "latest": _LINK.get("last", {}), "errors": _LINK.get("errors", [])}


def stop_link() -> dict[str, Any]:
    """KIT-ONLY. Stop the data link and clear the USD values it wrote in the session layer."""
    was = bool(_LINK.get("running"))
    handle = _LINK.pop("handle", None)
    if handle is not None and hasattr(handle, "unsubscribe"):
        handle.unsubscribe()
    cleared = 0
    restored = 0
    for target, (variable, context, original) in sorted(_LINK.pop("variable_originals", {}).items()):
        try:
            variable.set(context, original)
            restored += 1
        except Exception:  # noqa: BLE001 - the graph was deleted meanwhile
            continue
    usd = jc._kit_module("omni.usd")
    stage = usd.get_context().get_stage() if usd else None
    for target in sorted(_LINK.get("session_attrs", ())):
        if stage is None:
            break
        from pxr import Sdf

        spec = stage.GetSessionLayer().GetAttributeAtPath(Sdf.Path(target))
        if spec is not None:
            spec.ClearDefaultValue()
            cleared += 1
    _LINK["running"] = False
    return {"ok": True, "was_running": was, "updates": _LINK.get("updates", 0), "session_values_cleared": cleared,
            "graph_variables_restored": restored}
