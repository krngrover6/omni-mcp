"""Spawn many packages at once: random positions, sizes and colours, with physics (class sessions 8, 11-13).

Pure pxr, so it previews anywhere and applies on the live stage. Packages are Cubes under one new parent group, so
removing the whole batch is one delete. A fixed seed makes the same "random" batch again (useful for homework and
debugging). Positions are drawn on a grid of cells inside the region so packages never start inside each other
(two bodies created overlapping explode apart when Play starts).
"""

from __future__ import annotations

import math
import random
from typing import Any

try:
    from pxr import Gf, Sdf, UsdGeom, UsdPhysics
except ImportError:  # pragma: no cover - planning needs pxr
    Gf = Sdf = UsdGeom = UsdPhysics = None

MAX_COUNT = 200
PALETTE = {
    "red": (0.85, 0.15, 0.12), "green": (0.15, 0.7, 0.25), "blue": (0.15, 0.35, 0.9),
    "yellow": (0.95, 0.8, 0.1), "orange": (0.95, 0.5, 0.1), "purple": (0.55, 0.25, 0.8),
}


def _vec3(value: Any, name: str) -> tuple[float, float, float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in value)):
        raise ValueError(f"{name} must be three numbers such as [0, 0, 1].")
    return float(value[0]), float(value[1]), float(value[2])


def spawn_packages(stage: Any, parent: str = "/World/Packages", count: int = 10, *, region_min: Any = (-1, -1, 0.5),
                   region_max: Any = (1, 1, 1.5), size_range: Any = (0.1, 0.25), colors: Any = None,
                   seed: int | None = None, physics: bool = True, dry_run: bool = True) -> dict[str, Any]:
    """Preview (or with dry_run=False create) `count` (1-200) packages as Cubes under a NEW group `parent`, with random
    positions inside region_min..region_max (world units), edge sizes in size_range and colours picked from `colors`
    (names such as ['red', 'blue'] or [r, g, b] lists; default: red, green, blue). physics=True makes each a rigid
    body with a collider so they fall and pile up. seed repeats the same batch. Never touches existing prims."""
    if Gf is None:
        return {"ok": False, "error": "pxr (USD) is not available here."}
    try:
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_COUNT:
            raise ValueError(f"count must be a whole number from 1 to {MAX_COUNT}.")
        low, high = _vec3(region_min, "region_min"), _vec3(region_max, "region_max")
        if any(h <= l for l, h in zip(low, high)):
            raise ValueError("region_max must be bigger than region_min on every axis.")
        if (not isinstance(size_range, (list, tuple)) or len(size_range) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in size_range)
                or not 0 < size_range[0] <= size_range[1]):
            raise ValueError("size_range must be [smallest, largest] edge length, e.g. [0.1, 0.25].")
        if not isinstance(parent, str) or not Sdf.Path.IsValidPathString(parent) or not parent.startswith("/"):
            raise ValueError("parent must be an absolute prim path such as /World/Packages.")
        if stage.GetPrimAtPath(parent):
            raise ValueError(f"{parent} already exists; pick a new group path (each batch gets its own group).")
        palette = _palette(colors)
    except ValueError as exc:
        return {"ok": False, "dry_run": dry_run is not False, "error": str(exc)}
    rng = random.Random(seed)
    largest = float(size_range[1])
    span = [h - l for l, h in zip(low, high)]
    cells = [max(1, int(s // (largest * 1.2))) for s in span]
    slots = [(i, j, k) for i in range(cells[0]) for j in range(cells[1]) for k in range(cells[2])]
    if len(slots) < count:
        return {"ok": False, "dry_run": dry_run is not False,
                "error": f"The region only fits {len(slots)} packages of edge {largest:g} without overlapping; make it "
                         "bigger or the packages smaller."}
    chosen = rng.sample(slots, count)
    packages = []
    for index, (i, j, k) in enumerate(chosen, start=1):
        edge = rng.uniform(float(size_range[0]), largest)
        cell = [s / c for s, c in zip(span, cells)]
        center = [l + (n + 0.5) * w for l, n, w in zip(low, (i, j, k), cell)]
        jitter = [max(0.0, (w - edge * 1.1) / 2) for w in cell]
        position = [round(c + rng.uniform(-d, d), 4) for c, d in zip(center, jitter)]
        name, rgb = rng.choice(palette)
        packages.append({"path": f"{parent}/Package_{index:03d}", "position": position, "edge": round(edge, 4),
                         "color": name, "rgb": list(rgb)})
    summary = {"parent": parent, "count": count, "physics": bool(physics), "seed": seed,
               "colors": sorted({p["color"] for p in packages}), "packages": packages[:12],
               "more": max(0, count - 12)}
    if dry_run is not False:
        return {"ok": True, "dry_run": True, "written": False, "would_create": summary,
                "next_step": "Dry run: nothing created. Repeat with dry_run=False (or through scene_safe_edit).",
                "python": _python(parent, count, low, high, size_range, physics)}
    # No Sdf.ChangeBlock here: USD refuses UsdStage Define calls inside one (seen in tests: "Failed to define").
    UsdGeom.Xform.Define(stage, parent)
    for package in packages:
        cube = UsdGeom.Cube.Define(stage, package["path"])
        cube.CreateSizeAttr(1.0)
        xform = UsdGeom.XformCommonAPI(cube)
        xform.SetTranslate(Gf.Vec3d(*package["position"]))
        xform.SetScale(Gf.Vec3f(package["edge"], package["edge"], package["edge"]))
        cube.CreateDisplayColorAttr([Gf.Vec3f(*package["rgb"])])
        cube.GetPrim().CreateAttribute("package:color", Sdf.ValueTypeNames.Token, custom=True).Set(package["color"])
        if physics:
            UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())
            UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    return {"ok": True, "dry_run": False, "written": True, "created": summary,
            "remove": f"Delete {parent} to remove the whole batch (scene_delete).",
            "note": "Each package also carries a package:color token, handy for sorting logic (class session 13)."}


def _palette(colors: Any) -> list[tuple[str, tuple[float, float, float]]]:
    if colors is None:
        return [(name, PALETTE[name]) for name in ("red", "green", "blue")]
    if not isinstance(colors, (list, tuple)) or not colors:
        raise ValueError(f"colors must be a list of names ({', '.join(PALETTE)}) or [r, g, b] lists.")
    out = []
    for item in colors:
        if isinstance(item, str) and item.lower() in PALETTE:
            out.append((item.lower(), PALETTE[item.lower()]))
        elif isinstance(item, (list, tuple)) and len(item) == 3 and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1 for v in item):
            out.append(("custom", tuple(float(v) for v in item)))
        else:
            raise ValueError(f"Unknown colour {item!r}: use {', '.join(PALETTE)} or [r, g, b] with values 0-1.")
    return out


def _python(parent: str, count: int, low: tuple, high: tuple, size_range: Any, physics: bool) -> str:
    """The same idea as a beginner script for the Script Editor (class session 8's 'random package generator')."""
    lines = [
        "import random",
        "import omni.usd",
        "from pxr import Gf, UsdGeom, UsdPhysics",
        "",
        "stage = omni.usd.get_context().get_stage()   # the scene open in Isaac Sim",
        f"UsdGeom.Xform.Define(stage, {parent!r})       # one group for the whole batch",
        f"for index in range({count}):",
        f"    cube = UsdGeom.Cube.Define(stage, f'{parent}/Package_{{index:03d}}')",
        "    cube.CreateSizeAttr(1.0)",
        f"    edge = random.uniform({float(size_range[0])}, {float(size_range[1])})",
        f"    x = random.uniform({low[0]}, {high[0]}); y = random.uniform({low[1]}, {high[1]}); "
        f"z = random.uniform({low[2]}, {high[2]})",
        "    UsdGeom.XformCommonAPI(cube).SetTranslate(Gf.Vec3d(x, y, z))",
        "    UsdGeom.XformCommonAPI(cube).SetScale(Gf.Vec3f(edge, edge, edge))",
        "    cube.CreateDisplayColorAttr([Gf.Vec3f(random.random(), random.random(), random.random())])",
    ]
    if physics:
        lines += ["    UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())   # falls with gravity",
                  "    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())   # bumps into things"]
    lines.append("# Note: plain random positions can overlap; the MCP tool uses a grid of cells so they never do.")
    return "\n".join(lines) + "\n"
