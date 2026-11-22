"""Names that Kit / Isaac Sim Python modules (omni.*, carb.*, isaacsim.*) really have, read from the local install.

Used by usd_code_verify to catch invented or outdated Kit calls (e.g. the removed omni.isaac.core) without starting
Kit. Each module's files (__init__.py, .py, .pyi stubs) are parsed with ast, never imported or run. Only what the
files prove is used: module-level names (functions, classes, submodules, star imports followed) and enum values
(classes whose stub lists __members__). Methods on objects are NOT checked: seen 2026-11-22, the omni.usd stub has
no UsdContext.get_stage although the method exists, so a member check would raise false alarms. A module that
defines __getattr__ or ships a compiled .pyd without a stub is 'open': nothing in it is flagged.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KIT_TOPS = ("omni", "carb", "isaacsim")
_EXT_FOLDERS = ("kit/exts", "kit/extscore", "kit/extsPhysics", "exts", "extscache", "extsDeprecated")
_FLAT_BASES = ("kit/kernel/py", "python_packages", "kit/python/Lib/site-packages")
MAX_STAR_DEPTH = 4


def default_root() -> Path:
    env = os.environ.get("OMNIVERSE_ISAAC_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3] / "isaacsim-6.1.0"


@dataclass
class ModuleInfo:
    dotted: str
    names: set[str] = field(default_factory=set)
    classes: dict[str, set[str]] = field(default_factory=dict)  # enum-like classes only: name -> members
    returns: dict[str, str] = field(default_factory=dict)  # function -> returned class name (same module)
    open: bool = False
    files: list[str] = field(default_factory=list)


class KitIndex:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else default_root()
        self._bases: dict[str, list[Path]] | None = None
        self._all: list[Path] = []
        self._cache: dict[str, ModuleInfo | None] = {}

    @property
    def available(self) -> bool:
        return (self.root / "kit").is_dir()

    def _bases_for(self, top: str) -> list[Path]:
        if self._bases is None:
            self._bases = {t: [] for t in KIT_TOPS}
            candidates = [self.root / b for b in _FLAT_BASES]
            for folder in _EXT_FOLDERS:
                path = self.root / folder
                if path.is_dir():
                    for ext in sorted(p for p in path.iterdir() if p.is_dir()):
                        candidates.append(ext)
                        if (ext / "pip_prebundle").is_dir():  # seen: isaacsim.asset.transformer lives there
                            candidates.append(ext / "pip_prebundle")
            self._all = candidates
            for base in candidates:
                for name in KIT_TOPS:
                    if (base / name).is_dir():
                        self._bases[name].append(base)
        return self._bases.get(top, [])

    def has_pxr_module(self, name: str) -> bool:
        """A USD schema module that Kit ships beside plain USD (e.g. pxr.PhysxSchema in omni.usd.schema.physx)."""
        self._bases_for("omni")
        return any((base / "pxr" / name).is_dir() for base in self._all)

    def module(self, dotted: str) -> ModuleInfo | None:
        """What the module defines, or None when no installed folder or file has that module path."""
        if dotted in self._cache:
            return self._cache[dotted]
        self._cache[dotted] = None  # guards star-import cycles
        info = self._load(dotted)
        self._cache[dotted] = info
        return info

    def _load(self, dotted: str, depth: int = 0) -> ModuleInfo | None:
        parts = dotted.split(".")
        if parts[0] not in KIT_TOPS:
            return None
        rel = Path(*parts)
        files: list[Path] = []
        found = False
        info = ModuleInfo(dotted)
        for base in self._bases_for(parts[0]):
            folder = base / rel
            if folder.is_dir():
                found = True
                for child in folder.iterdir():
                    stem = child.name.split(".")[0]
                    if child.is_dir() and not child.name.startswith(("_", ".")) and child.name != "tests":
                        info.names.add(child.name)
                    elif child.suffix in (".py", ".pyi", ".pyd") and stem != "__init__":
                        info.names.add(stem)
                for init in ("__init__.pyi", "__init__.py"):
                    if (folder / init).is_file():
                        files.append(folder / init)
            parent = base / rel.parent
            for suffix in (".pyi", ".py"):
                single = parent / f"{parts[-1]}{suffix}"
                if single.is_file():
                    found = True
                    files.append(single)
            if parent.is_dir() and any(p.name.startswith(parts[-1] + ".") and p.suffix == ".pyd"
                                       for p in parent.iterdir()) and not (parent / f"{parts[-1]}.pyi").is_file():
                found, info.open = True, True  # compiled module without a stub: cannot list its names
        if not found:
            return None
        if not files and not info.names:
            info.open = True
        for path in files:
            self._read(path, info, depth)
        info.files = [str(p) for p in files][:6]
        return info

    def _read(self, path: Path, info: ModuleInfo, depth: int) -> None:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError, ValueError):
            info.open = True
            return
        package = info.dotted if path.stem == "__init__" else info.dotted.rsplit(".", 1)[0]
        for node in tree.body:
            self._statement(node, info, package, depth)

    def _statement(self, node: ast.AST, info: ModuleInfo, package: str, depth: int) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.names.add(node.name)
            if node.name == "__getattr__":
                info.open = True
            returned = _annotation_name(node.returns)
            if returned:
                info.returns[node.name] = returned
        elif isinstance(node, ast.ClassDef):
            info.names.add(node.name)
            members = _class_members(node)
            if "__members__" in members:
                info.classes[node.name] = members
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        info.names.add(name.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            info.names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                info.names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            source = _absolute(node, package)
            for alias in node.names:
                if alias.name == "*":
                    self._star(source, info, depth)
                else:
                    info.names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.If, ast.Try)):
            for child in getattr(node, "body", []) + getattr(node, "orelse", []) + \
                    [s for h in getattr(node, "handlers", []) for s in h.body]:
                self._statement(child, info, package, depth)

    def _star(self, source: str | None, info: ModuleInfo, depth: int) -> None:
        if not source or depth >= MAX_STAR_DEPTH or source.split(".")[0] not in KIT_TOPS:
            info.open = True  # names come from somewhere we do not read (e.g. pxr, a C module)
            return
        other = self._cache.get(source) if source in self._cache else self._load(source, depth + 1)
        if other is None:
            info.open = True
            return
        info.names |= other.names
        info.classes.update(other.classes)
        info.returns.update(other.returns)
        info.open = info.open or other.open


def _absolute(node: ast.ImportFrom, package: str) -> str | None:
    if not node.level:
        return node.module
    parts = package.split(".")
    if node.level - 1 >= len(parts):
        return None
    base = parts[: len(parts) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _annotation_name(node: Any) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.rsplit(".", 1)[-1].strip("'\" ") or None
    return None


def _class_members(node: ast.ClassDef) -> set[str]:
    members: set[str] = set()
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            members.add(item.name)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            members.add(item.target.id)
        elif isinstance(item, ast.Assign):
            members |= {t.id for t in item.targets if isinstance(t, ast.Name)}
    members |= {"name", "value"}  # every pybind enum value has these
    return members


_SHARED: KitIndex | None = None


def shared_index() -> KitIndex:
    global _SHARED
    if _SHARED is None:
        _SHARED = KitIndex()
    return _SHARED
