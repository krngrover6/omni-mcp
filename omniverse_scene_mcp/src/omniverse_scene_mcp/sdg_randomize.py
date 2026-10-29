"""Domain randomization for SDG jobs: change positions, turns, colours and light brightness on every frame.

Why: a model trained on pictures where everything always looks the same fails when the light or the layout
changes. Randomizing each frame teaches it what matters (the object) and what does not (where it sits, its shade).

How (and why it is safe): every change is authored in the stage's SESSION layer, a scratch layer on top of the
file that is never saved, and restore() puts each touched value back exactly as it was when the job ends. The
same seed gives the same sequence of changes. Plain USD, so it is tested without Kit.

spec = {
  "prims": ["/World/Packages/Package_001", ...],           # what to move / recolour
  "position": {"min": [x, y, z], "max": [x, y, z]},        # optional: random world box for each prim's translate
  "rotate_z": true,                                         # optional: random turn about Z (needs a rotateXYZ op)
  "colors": [[1, 0, 0], [0, 0, 1]] or "any",               # optional: pick one per frame (displayColor)
  "lights": ["/World/DomeLight"], "intensity": [500, 2000]  # optional: random light brightness
}
"""

from __future__ import annotations

import random
from typing import Any

try:
    from pxr import Gf, Sdf, Usd, UsdGeom
except ImportError:  # pragma: no cover
    Gf = Sdf = Usd = UsdGeom = None

MAX_PRIMS = 200
_SPEC_KEYS = {"prims", "position", "rotate_z", "colors", "lights", "intensity"}


def _three(value: Any, label: str) -> list[float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 3 or
            not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
        raise ValueError(f"{label} must be three numbers [x, y, z]")
    return [float(v) for v in value]


def validate(spec: Any) -> dict[str, Any] | None:
    """Check a randomize spec's shape (no stage needed); returns the cleaned spec, or None for no randomization."""
    if spec is None:
        return None
    if not isinstance(spec, dict) or not spec:
        raise ValueError("randomize must be an object such as {'prims': [...], 'colors': 'any'}")
    unknown = set(spec) - _SPEC_KEYS
    if unknown:
        raise ValueError(f"randomize does not take {sorted(unknown)}; use {sorted(_SPEC_KEYS)}")
    clean: dict[str, Any] = {"prims": [], "lights": []}
    for key in ("prims", "lights"):
        paths = spec.get(key) or []
        if not isinstance(paths, list) or len(paths) > MAX_PRIMS or not all(
                isinstance(p, str) and p.startswith("/") for p in paths):
            raise ValueError(f"randomize.{key} must be a list of up to {MAX_PRIMS} absolute prim paths")
        clean[key] = list(dict.fromkeys(paths))
    if "position" in spec:
        box = spec["position"]
        if not isinstance(box, dict):
            raise ValueError("randomize.position must be {'min': [x, y, z], 'max': [x, y, z]}")
        low, high = _three(box.get("min"), "position.min"), _three(box.get("max"), "position.max")
        if any(a > b for a, b in zip(low, high)):
            raise ValueError("position.max must be at least position.min on every axis")
        clean["position"] = (low, high)
    clean["rotate_z"] = bool(spec.get("rotate_z"))
    colors = spec.get("colors")
    if colors is not None:
        if colors != "any" and (not isinstance(colors, list) or not colors or
                                not all(isinstance(c, (list, tuple)) and len(c) == 3 for c in colors)):
            raise ValueError("randomize.colors must be 'any' or a list of [r, g, b] colours (0-1)")
        clean["colors"] = colors if colors == "any" else [[float(v) for v in c] for c in colors]
    if "intensity" in spec:
        pair = spec["intensity"]
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2 or
                not all(isinstance(v, (int, float)) and v >= 0 for v in pair) or pair[0] > pair[1]):
            raise ValueError("randomize.intensity must be [low, high] with 0 <= low <= high")
        clean["intensity"] = (float(pair[0]), float(pair[1]))
    if clean["lights"] and "intensity" not in clean:
        raise ValueError("randomize.lights needs an intensity range [low, high]")
    if clean["prims"] and not ({"position", "colors"} & set(clean) or clean["rotate_z"]):
        raise ValueError("randomize.prims needs something to change: position, rotate_z or colors")
    if not clean["prims"] and not clean["lights"]:
        raise ValueError("randomize needs prims and/or lights")
    return clean


class Randomizer:
    """Apply one random variation per frame in the session layer; restore() undoes all of it exactly."""

    def __init__(self, stage: Any, spec: dict[str, Any], seed: int) -> None:
        self.stage, self.spec, self.rng = stage, spec, random.Random(seed)
        self.layer = stage.GetSessionLayer()
        self._saved: dict[str, Any] = {}  # attribute path -> (existed, had value, value) before we touched it
        self._new_prims: set[str] = set()  # session-layer prim specs that did not exist before
        # Seen live: while Replicator stepped, PhysX wrote rigid bodies' poses into the EDIT TARGET (the file's
        # layer), so restoring the session layer alone left the packages at a random spot. Keep those values too.
        self.target = stage.GetEditTarget().GetLayer()
        self._target_saved: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.plan: list[tuple[Any, str]] = []
        for path in spec["prims"]:
            prim = stage.GetPrimAtPath(path)
            if not prim:
                raise ValueError(f"randomize: no prim at {path}")
            ops = {op.GetOpName(): op for op in UsdGeom.Xformable(prim).GetOrderedXformOps()} \
                if prim.IsA(UsdGeom.Xformable) else {}
            if "position" in spec:
                if "xformOp:translate" not in ops:
                    raise ValueError(f"randomize: {path} has no translate op to move (move it once in Isaac first)")
                self.plan.append((prim, "position"))
            if spec["rotate_z"]:
                if "xformOp:rotateXYZ" in ops:
                    self.plan.append((prim, "rotate"))
                else:
                    self.warnings.append(f"{path} has no rotateXYZ op, so it is not turned")
            if "colors" in spec:
                if prim.IsA(UsdGeom.Gprim):
                    self.plan.append((prim, "color"))
                    if prim.GetRelationship("material:binding") and prim.GetRelationship("material:binding").GetTargets():
                        self.warnings.append(f"{path} has a material bound: the new displayColor will not show")
                else:
                    self.warnings.append(f"{path} is not a shape, so its colour is not changed")
        for path in spec["lights"]:
            light = stage.GetPrimAtPath(path)
            if not light or not light.GetAttribute("inputs:intensity"):
                raise ValueError(f"randomize: {path} is not a light with inputs:intensity")
            self.plan.append((light, "intensity"))

    def _set(self, attribute: Any, value: Any) -> None:
        key = str(attribute.GetPath())
        if key not in self._saved:
            spec = self.layer.GetAttributeAtPath(attribute.GetPath())
            self._saved[key] = (spec is not None, spec is not None and spec.HasDefaultValue(),
                                spec.default if spec else None)
            if self.target is not None and self.target != self.layer:
                spec_t = self.target.GetAttributeAtPath(attribute.GetPath())
                self._target_saved[key] = (spec_t is not None and spec_t.HasDefaultValue(),
                                           spec_t.default if spec_t is not None else None)
            prim_path = attribute.GetPath().GetPrimPath()
            while prim_path != Sdf.Path.absoluteRootPath and not self.layer.GetPrimAtPath(prim_path):
                self._new_prims.add(str(prim_path))
                prim_path = prim_path.GetParentPath()
        with Usd.EditContext(self.stage, self.layer):
            attribute.Set(value)

    def apply(self) -> list[dict[str, Any]]:
        """One frame's random changes; returns what was set (for the dataset manifest)."""
        changes = []
        for prim, what in self.plan:
            if what == "position":
                low, high = self.spec["position"]
                parent = UsdGeom.Xformable(prim.GetParent()).ComputeLocalToWorldTransform(Usd.TimeCode.Default()) \
                    if prim.GetParent().IsA(UsdGeom.Xformable) else Gf.Matrix4d(1)
                world = Gf.Vec3d(*(self.rng.uniform(a, b) for a, b in zip(low, high)))
                local = parent.GetInverse().Transform(world)
                attr = prim.GetAttribute("xformOp:translate")
                value = type(attr.Get())(local) if attr.Get() is not None else Gf.Vec3d(local)
                self._set(attr, value)
                changes.append({"prim": str(prim.GetPath()), "position": [round(v, 3) for v in world]})
            elif what == "rotate":
                attr = prim.GetAttribute("xformOp:rotateXYZ")
                old = attr.Get() or Gf.Vec3f(0, 0, 0)
                angle = self.rng.uniform(0.0, 360.0)
                self._set(attr, type(old)(old[0], old[1], angle))
                changes.append({"prim": str(prim.GetPath()), "rotate_z": round(angle, 1)})
            elif what == "color":
                colors = self.spec["colors"]
                rgb = ([self.rng.random() for _ in range(3)] if colors == "any" else self.rng.choice(colors))
                self._set(UsdGeom.Gprim(prim).GetDisplayColorAttr(), [Gf.Vec3f(*rgb)])
                changes.append({"prim": str(prim.GetPath()), "color": [round(v, 3) for v in rgb]})
            elif what == "intensity":
                low, high = self.spec["intensity"]
                value = self.rng.uniform(low, high)
                self._set(prim.GetAttribute("inputs:intensity"), value)
                changes.append({"prim": str(prim.GetPath()), "intensity": round(value, 1)})
        return changes

    def restore(self) -> int:
        """Put every touched session-layer value back; returns how many were restored."""
        count = 0
        for key, (existed, had_value, old) in self._saved.items():
            path = Sdf.Path(key)
            spec = self.layer.GetAttributeAtPath(path)
            if spec is None:
                continue
            if not existed:  # we created this attribute spec: remove it whole
                self.layer.GetPrimAtPath(path.GetPrimPath()).RemoveProperty(spec)
            elif had_value:
                spec.default = old
            else:
                spec.ClearDefaultValue()
            count += 1
        for prim_path in sorted(self._new_prims, key=lambda p: -p.count("/")):  # deepest first
            spec = self.layer.GetPrimAtPath(prim_path)
            # Remove it only if it is still an empty "over" (nothing else was authored there meanwhile).
            if (spec is not None and spec.specifier == Sdf.SpecifierOver and not spec.typeName
                    and not spec.properties and not spec.nameChildren):
                edit = Sdf.BatchNamespaceEdit()
                edit.Add(Sdf.NamespaceEdit.Remove(Sdf.Path(prim_path)))
                self.layer.Apply(edit)
        for key, (had_value, old) in self._target_saved.items():
            spec = self.target.GetAttributeAtPath(Sdf.Path(key))
            if spec is None:
                continue
            now = spec.default if spec.HasDefaultValue() else None
            if had_value and now != old:
                spec.default = old  # physics wrote a frame's pose here: put the file's value back
            elif not had_value and spec.HasDefaultValue():
                spec.ClearDefaultValue()
        self._saved.clear()
        self._new_prims.clear()
        self._target_saved.clear()
        return count
