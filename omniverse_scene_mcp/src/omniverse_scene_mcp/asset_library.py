"""Local Isaac Sim asset library (read-only index and search) plus a dry-run-by-default "add asset by reference" planner.

AssetLibrary walks the offline asset pack (default <workspace>/isaacsim-assets-6.1.0, or $OMNIVERSE_ISAAC_ASSETS_ROOT)
once per process with os.scandir: file names, sizes and folder names only. Nothing in the pack is written, imported or
executed; USD headers are read by info()/add_reference() through Sdf.Layer.OpenAsAnonymous(path, metadataOnly=True),
an in-memory copy that is never saved.

add_reference plans (dry_run=True, the default) or authors an Xform prim that references an asset file, with optional
translate / rotateXYZ / scale ops and instanceable, and warns when the asset's upAxis or metersPerUnit differ from the
stage's (Isaac assets are Z-up meters). Imports only pxr, the standard library and this package's pure-pxr helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import numbers
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator

from .authoring import (
    _check_edit_target,
    _check_stage,
    _creation_target,
    _dry,
    _edit_target_info,
    _finish,
    _prim_path,
    _Refusal,
    _require_pxr,
)

try:
    from pxr import Gf, Sdf, UsdGeom
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Gf = Sdf = UsdGeom = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

ENV_ROOT = "OMNIVERSE_ISAAC_ASSETS_ROOT"
DEFAULT_PACK_DIR = "isaacsim-assets-6.1.0"
USD_EXTENSIONS = frozenset({".usd", ".usda", ".usdc", ".usdz"})
MATERIAL_EXTENSIONS = frozenset({".mdl"})
LIBRARIES = ("Isaac", "NVIDIA")
KNOWN_CATEGORIES = frozenset(
    {"robots", "environments", "props", "sensors", "materials", "people", "samples", "isaaclab", "assets", "characters"}
)
MAX_ASSETS = 100_000
MAX_ENTRIES = 500_000  # directory entries visited (files + folders)
MAX_DIR_ENTRIES = 20_000  # entries read from one folder before the rest is skipped
MAX_DEPTH = 16
MAX_TOP_K = 200
HEADER_MAX_BYTES = 512 * 1024 * 1024  # metadataOnly opens stay cheap well past this; bigger files are skipped
ROOT_PRIMS_MAX_BYTES = 16 * 1024 * 1024  # a full anonymous open (to list root prims) only for small files
MAX_ROOT_PRIMS = 20
_UNIT_TOLERANCE = 1e-6
_SKIP_DIRS = frozenset({"__pycache__", "node_modules"})
# Folders that hold the pieces of a bigger asset (robot links, physics/config sublayers, material libraries).
_PART_DIRS = frozenset(
    {"configuration", "configurations", "payloads", "payload", "physics", "materials", "textures", "source_assets",
     "dependencies", "legacy", "neutralized", "half__res", "meshes", "parts"}
)
_ROBOT_CATEGORIES = frozenset({"robots", "robots_multiphysics"})
# Training, sample and alternate-physics copies: ranked a little below the canonical Robots/Props/... assets.
_VARIANT_CATEGORIES = frozenset({"IsaacLab", "Samples", "Robots_Multiphysics"})
_PART_NAME = re.compile(r"(_robot_schema|_physics|_sensor_schema|_base_schema|instanceable_meshes|collision_meshes)$", re.I)
_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_VERSION_DIR = re.compile(r"^\d+(\.\d+)+$")
_QUERY_STOP = frozenset("a an and asset assets file files for in of on the to usd with model models".split())
_CATEGORY_ALIASES = {
    "robot": ("robots", "robots_multiphysics"),
    "env": ("environments",),
    "environment": ("environments",),
    "scene": ("environments", "samples"),
    "prop": ("props",),
    "object": ("props",),
    "sensor": ("sensors",),
    "camera": ("sensors",),
    "lidar": ("sensors",),
    "material": ("materials",),
    "mdl": ("materials",),
    "person": ("people", "characters"),
    "people": ("people", "characters"),
    "human": ("people", "characters"),
    "character": ("people", "characters"),
}

_LOCK = threading.Lock()
_INDEXES: dict[tuple[Any, ...], "_Index"] = {}


# --- roots and text helpers ----------------------------------------------------------------------------------------


def workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def default_root() -> Path:
    """$OMNIVERSE_ISAAC_ASSETS_ROOT, else <workspace>/isaacsim-assets-6.1.0."""
    configured = os.environ.get(ENV_ROOT, "").strip()
    return Path(configured) if configured else workspace_root() / DEFAULT_PACK_DIR


def _fwd(path: Any) -> str:
    return str(path).replace("\\", "/")


def _singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ves"):
        return word[:-3] + "f"  # shelves -> shelf
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    """Lowercased, singular words plus their camelCase pieces: 'SM_RackShelf_01' -> {sm, rackshelf, rack, shelf, 01}."""
    out: set[str] = set()
    for word in _WORD.findall(text):
        out.add(_singular(word.lower()))
        if not (word.islower() or word.isupper()):
            out.update(_singular(part.lower()) for part in _CAMEL.findall(word))
    return out


def _norm_category(name: str) -> str:
    return "_".join(_singular(part) for part in name.strip().lower().replace("-", "_").replace(" ", "_").split("_") if part)


def _scandir(path: str) -> Iterator[os.DirEntry]:
    """os.scandir that retries Windows paths over MAX_PATH with the \\\\?\\ prefix; yields nothing if unreadable."""
    try:
        handle = os.scandir(path)
    except OSError:
        if os.name != "nt" or path.startswith("\\\\?\\"):
            return
        try:
            handle = os.scandir("\\\\?\\" + os.path.abspath(path))
        except OSError:
            return
    with handle:
        try:
            yield from handle
        except OSError:
            return


def _find_content_root(root: Path) -> Path:
    """The folder holding Isaac/ and NVIDIA/ (e.g. <pack>/Assets/Isaac/6.1); the root itself when none is found."""
    frontier = [root]
    for _ in range(4):
        found: list[Path] = []
        nxt: list[Path] = []
        for folder in frontier:
            for library in LIBRARIES:
                lib = folder / library
                if lib.is_dir() and any(
                    entry.is_dir() and entry.name.lower() in KNOWN_CATEGORIES for entry in _scandir(str(lib))
                ):
                    found.append(folder)
                    break
            nxt.extend(Path(entry.path) for entry in _scandir(str(folder)) if entry.is_dir() and not entry.name.startswith("."))
        if found:  # several version folders: prefer the newest-looking name
            return sorted(found, key=lambda p: _version_key(p.name), reverse=True)[0]
        frontier = nxt[:200]
    return root


def _version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", name)) if _VERSION_DIR.match(name) else (-1,)


def _classify(parts: tuple[str, ...]) -> tuple[str, str | None, str | None]:
    """(category, library, group) from folder parts relative to the content root (file name excluded)."""
    if not parts:
        return "Uncategorized", None, None
    library = parts[0] if parts[0] in LIBRARIES else None
    rest = parts[1:] if library else parts
    if library == "NVIDIA" and len(rest) >= 2 and rest[0] == "Assets":
        rest = rest[1:]  # NVIDIA/Assets/Characters/... -> Characters
    if not rest:
        return (library or "Uncategorized"), library, None
    return rest[0], library, (rest[1] if len(rest) > 1 else None)


# --- index ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Asset:
    rel: str
    path: str
    name: str
    stem: str
    ext: str
    size: int
    category: str
    library: str | None
    group: str | None
    depth: int
    thumbnail: str | None
    part: bool
    name_tokens: frozenset[str]
    folder_tokens: frozenset[str]
    category_key: str

    @property
    def kind(self) -> str:
        return "mdl" if self.ext in MATERIAL_EXTENSIONS else "usd"

    def public(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "relative_path": self.rel,
            "name": self.name,
            "category": self.category,
            "library": self.library,
            "group": self.group,
            "extension": self.ext,
            "kind": self.kind,
            "referenceable": self.kind == "usd",
            "size": self.size,
            "thumbnail": self.thumbnail,
        }


class _Index:
    """One walk of a content root: assets, category counts and walk statistics."""

    def __init__(self, content_root: Path, max_assets: int, max_entries: int) -> None:
        started = time.perf_counter()
        self.assets: list[_Asset] = []
        self.entries = 0
        self.folders = 0
        self.unreadable_folders = 0
        self.truncated: list[str] = []
        base = str(content_root)
        stack: list[tuple[str, tuple[str, ...]]] = [(base, ())]
        while stack:
            folder, parts = stack.pop()
            self.folders += 1
            files: list[os.DirEntry] = []
            thumbs = False
            readable = False
            count = 0
            for entry in _scandir(folder):
                readable = True
                count += 1
                self.entries += 1
                if count > MAX_DIR_ENTRIES:
                    self.truncated.append(f"folder over {MAX_DIR_ENTRIES} entries: {_fwd('/'.join(parts)) or '.'}")
                    break
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    if entry.name == ".thumbs":
                        thumbs = True
                    elif not entry.name.startswith(".") and entry.name.lower() not in _SKIP_DIRS and len(parts) < MAX_DEPTH:
                        stack.append((entry.path, parts + (entry.name,)))
                elif os.path.splitext(entry.name)[1].lower() in USD_EXTENSIONS | MATERIAL_EXTENSIONS:
                    files.append(entry)
            if not readable and parts:
                self.unreadable_folders += 1
            thumb_names = (
                {e.name.lower() for e in _scandir(os.path.join(folder, ".thumbs", "256x256"))} if thumbs and files else set()
            )
            for entry in files:
                if len(self.assets) >= max_assets:
                    break
                self._add(content_root, parts, entry, thumb_names)
            if len(self.assets) >= max_assets:
                self.truncated.append(f"stopped at max_assets={max_assets}")
                break
            if self.entries >= max_entries:
                self.truncated.append(f"stopped at max_entries={max_entries}")
                break
        self.assets.sort(key=lambda asset: asset.rel.lower())
        self.categories: dict[str, int] = {}
        for asset in self.assets:
            self.categories[asset.category] = self.categories.get(asset.category, 0) + 1
        self.index_seconds = round(time.perf_counter() - started, 3)

    def _add(self, content_root: Path, parts: tuple[str, ...], entry: os.DirEntry, thumb_names: set[str]) -> None:
        try:
            size = int(entry.stat().st_size)
        except OSError:
            size = -1
        name = entry.name
        stem, ext = os.path.splitext(name)
        category, library, group = _classify(parts)
        rel = "/".join(parts + (name,))
        absolute = _fwd(content_root / rel)
        lowered = {part.lower() for part in parts}
        part = bool(lowered & _PART_DIRS) or bool(_PART_NAME.search(stem))
        if category.lower() in _ROBOT_CATEGORIES and "props" in lowered:
            part = True  # robot link meshes (panda_link0.usd ...)
        folder_tokens: set[str] = set()
        for folder in parts:
            folder_tokens |= _tokens(folder)
        self.assets.append(
            _Asset(
                rel=rel,
                path=absolute,
                name=name,
                stem=stem.lower(),
                ext=ext.lower(),
                size=size,
                category=category,
                library=library,
                group=group,
                depth=len(parts),
                thumbnail=_fwd(Path(content_root, *parts, ".thumbs", "256x256", name + ".png"))
                if (name.lower() + ".png") in thumb_names
                else None,
                part=part,
                name_tokens=frozenset(_tokens(stem)),
                folder_tokens=frozenset(folder_tokens),
                category_key=_norm_category(category),
            )
        )


def clear_cache() -> None:
    """Forget every cached index (tests, or after the asset pack changed)."""
    with _LOCK:
        _INDEXES.clear()


def _score_term(term: str, asset: _Asset) -> float:
    best = 0.0
    for token in asset.name_tokens:
        if token == term:
            return 3.0
        if len(term) >= 3 and token.startswith(term):
            best = max(best, 2.0)
        elif len(term) >= 4 and term in token:
            best = max(best, 1.5)
    if len(term) >= 3 and term in asset.stem:
        best = max(best, 1.5)
    for token in asset.folder_tokens:
        if token == term:
            best = max(best, 1.5)
        elif len(term) >= 3 and token.startswith(term):
            best = max(best, 1.0)
        elif len(term) >= 4 and term in token:
            best = max(best, 0.75)
    if term == asset.category_key or asset.category_key.startswith(term + "_"):
        best = max(best, 1.2)
    return best


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in _WORD.findall(query or ""):
        lowered = _singular(word.lower())
        if lowered not in _QUERY_STOP and (len(lowered) >= 2 or lowered.isdigit()) and lowered not in terms:
            terms.append(lowered)
    return terms


class AssetLibrary:
    """Read-only index of a local Isaac Sim asset pack: categories, ranked search and cheap per-file info.

    root is the pack folder (default $OMNIVERSE_ISAAC_ASSETS_ROOT or <workspace>/isaacsim-assets-6.1.0); the folder
    that holds Isaac/ and NVIDIA/ (e.g. Assets/Isaac/6.1) is found inside it. The walk happens on first use and is
    cached per process for every instance with the same root and caps.
    """

    def __init__(self, root: str | Path | None = None, *, max_assets: int = MAX_ASSETS, max_entries: int = MAX_ENTRIES) -> None:
        self.root = Path(root) if root is not None else default_root()
        self.max_assets = max(1, int(max_assets))
        self.max_entries = max(1, int(max_entries))
        self._content_root: Path | None = None

    @property
    def available(self) -> bool:
        return self.root.is_dir()

    @property
    def content_root(self) -> Path:
        if self._content_root is None:
            self._content_root = _find_content_root(self.root) if self.available else self.root
        return self._content_root

    @property
    def pack_version(self) -> str | None:
        name = self.content_root.name
        return name if self.content_root != self.root and _VERSION_DIR.match(name) else None

    def _index(self) -> _Index:
        key = (os.path.normcase(str(self.content_root.resolve())), self.max_assets, self.max_entries)
        with _LOCK:
            index = _INDEXES.get(key)
            if index is None:
                index = _INDEXES[key] = _Index(self.content_root, self.max_assets, self.max_entries)
            return index

    def status(self) -> dict[str, Any]:
        """Builds the index if needed; counts, categories and how long the walk took."""
        if not self.available:
            return {"available": False, "root": _fwd(self.root), "error": f"Asset pack folder not found: {_fwd(self.root)}",
                    "hint": f"Set {ENV_ROOT} to the folder that holds the Isaac Sim assets."}
        index = self._index()
        return {
            "available": True,
            "root": _fwd(self.root),
            "content_root": _fwd(self.content_root),
            "pack_version": self.pack_version,
            "asset_count": len(index.assets),
            "usd_count": sum(1 for asset in index.assets if asset.kind == "usd"),
            "mdl_count": sum(1 for asset in index.assets if asset.kind == "mdl"),
            "categories": dict(sorted(index.categories.items())),
            "folders_scanned": index.folders,
            "entries_scanned": index.entries,
            "unreadable_folders": index.unreadable_folders,
            "truncated": index.truncated[:20],
            "index_seconds": index.index_seconds,
        }

    def categories(self) -> dict[str, int]:
        return dict(sorted(self._index().categories.items())) if self.available else {}

    def _match_categories(self, category: str) -> list[str]:
        key = _norm_category(category)
        names = self._index().categories
        aliases = set(_CATEGORY_ALIASES.get(key, ()))
        return [
            name for name in names
            if _norm_category(name) == key or _norm_category(name).startswith(key + "_") or name.lower() in aliases
        ]

    def search(self, query: str, top_k: int = 10, category: str | None = None) -> dict[str, Any]:
        """Rank assets by file-name and folder-name matches ('franka', 'warehouse shelf', 'carter', 'cube')."""
        top_k = max(1, min(int(top_k), MAX_TOP_K))
        result: dict[str, Any] = {"query": query, "category": category, "results": []}
        if not self.available:
            result.update(ok=False, error=f"Asset pack folder not found: {_fwd(self.root)}")
            return result
        index = self._index()
        result["index_seconds"] = index.index_seconds
        terms = _query_terms(query)
        if not terms and category is None:
            result.update(ok=False, error="Empty query: give a word such as 'franka', 'shelf' or 'cube'.")
            return result
        allowed: set[str] | None = None
        if category is not None:
            matched = self._match_categories(category)
            if not matched:
                result.update(ok=False, error=f"Unknown category {category!r}.", categories=sorted(index.categories))
                return result
            allowed = set(matched)
            result["matched_categories"] = matched
        started = time.perf_counter()
        joined = {"_".join(terms), "".join(terms)}
        scored: list[tuple[float, _Asset]] = []
        materials_wanted = allowed is not None and any(name.lower() == "materials" for name in allowed)
        for asset in index.assets:
            if allowed is not None and asset.category not in allowed:
                continue
            if terms:
                term_scores = [_score_term(term, asset) for term in terms]
                covered = sum(1 for score in term_scores if score > 0)
                if not covered:
                    continue
                score = sum(term_scores) * (covered / len(terms)) ** 2
                if asset.stem.replace("-", "_") in joined:
                    score *= 1.25  # the file is named exactly after the query: franka.usd for 'franka'
                score += 0.25 * sum(
                    1 for term in terms
                    if term in asset.name_tokens and any(term in token for token in asset.folder_tokens)
                )
            else:
                score = 1.0
            if asset.category in _VARIANT_CATEGORIES:
                score *= 0.85
            if asset.thumbnail:
                score *= 1.3
            if asset.part:
                score *= 0.55
            if asset.kind == "mdl" and not materials_wanted:
                score *= 0.7
            score /= 1.0 + 0.04 * max(0, asset.depth - 3)
            scored.append((score, asset))
        scored.sort(key=lambda item: (-item[0], len(item[1].rel), item[1].rel.lower()))
        result.update(
            ok=True,
            terms=terms,
            total_matches=len(scored),
            search_seconds=round(time.perf_counter() - started, 4),
            results=[{**asset.public(), "score": round(score, 4)} for score, asset in scored[:top_k]],
        )
        return result

    def resolve(self, asset_path: str | Path) -> Path | None:
        """An existing file: absolute, or relative to the content root or the pack root."""
        text = str(asset_path or "").strip().strip('"')
        if not text:
            return None
        path = Path(text)
        candidates = [path] if path.is_absolute() else [self.content_root / path, self.root / path]
        return next((candidate for candidate in candidates if candidate.is_file()), None)

    def info(self, asset_path: str | Path, *, header_max_bytes: int = HEADER_MAX_BYTES) -> dict[str, Any]:
        """Path, name, category, size and (when cheap) the layer header: defaultPrim, upAxis, metersPerUnit."""
        found = self.resolve(asset_path)
        if found is None:
            out: dict[str, Any] = {"ok": False, "error": f"Asset file not found: {_fwd(asset_path)}"}
            stem = Path(str(asset_path)).stem
            if self.available and stem:
                out["suggestions"] = [hit["relative_path"] for hit in self.search(stem, top_k=5).get("results", [])]
            return out
        found = Path(os.path.abspath(found))
        ext = found.suffix.lower()
        size = found.stat().st_size
        rel = _relative_to(found, self.content_root)
        category, library, group = _classify(tuple(Path(rel).parts[:-1])) if rel else ("Outside library", None, None)
        thumb = found.parent / ".thumbs" / "256x256" / (found.name + ".png")
        out = {
            "ok": True,
            "path": _fwd(found),
            "relative_path": rel,
            "in_library": rel is not None,
            "name": found.name,
            "category": category,
            "library": library,
            "group": group,
            "extension": ext,
            "kind": "mdl" if ext in MATERIAL_EXTENSIONS else "usd" if ext in USD_EXTENSIONS else "other",
            "size": size,
            "thumbnail": _fwd(thumb) if thumb.is_file() else None,
        }
        if ext in USD_EXTENSIONS:
            out.update(read_header(found, size=size, max_bytes=header_max_bytes))
        return out


def _relative_to(path: Path, base: Path) -> str | None:
    try:
        return _fwd(os.path.relpath(path, base)) if os.path.commonpath([os.path.normcase(str(path)), os.path.normcase(str(base))]) == os.path.normcase(str(base)) else None
    except ValueError:  # different drives
        return None


# --- USD header ----------------------------------------------------------------------------------------------------


def read_header(path: str | Path, *, size: int | None = None, max_bytes: int = HEADER_MAX_BYTES) -> dict[str, Any]:
    """Layer metadata via a metadata-only anonymous open (never saved): defaultPrim, upAxis, metersPerUnit.

    up_axis / meters_per_unit are None when the layer does not author them (USD then falls back to Y and 0.01).
    """
    out: dict[str, Any] = {"header_read": False, "default_prim": None, "up_axis": None, "meters_per_unit": None}
    if _PXR_IMPORT_ERROR is not None:
        out["header_skipped"] = f"USD Python bindings (pxr) are not available: {_PXR_IMPORT_ERROR}"
        return out
    size = os.path.getsize(path) if size is None else size
    if size > max_bytes:
        out["header_skipped"] = f"file is {size} bytes, over the {max_bytes}-byte header limit"
        return out
    try:
        layer = Sdf.Layer.OpenAsAnonymous(str(path), True)
    except Exception as exc:  # Tf errors for unreadable or malformed files
        out["header_error"] = str(exc)[:400]
        return out
    if layer is None:
        out["header_error"] = "Sdf could not open the layer"
        return out
    root = layer.pseudoRoot
    out["header_read"] = True
    out["default_prim"] = str(layer.defaultPrim) or None
    if root.HasInfo("upAxis"):
        out["up_axis"] = str(root.GetInfo("upAxis"))
    if root.HasInfo("metersPerUnit"):
        out["meters_per_unit"] = float(root.GetInfo("metersPerUnit"))
    try:
        out["sublayer_count"] = len(layer.subLayerPaths)
    except Exception:
        pass
    return out


def root_prim_names(path: str | Path, *, max_bytes: int = ROOT_PRIMS_MAX_BYTES) -> list[str] | None:
    """Root prim names from a full anonymous open (never saved), only for small files; None when skipped."""
    if _PXR_IMPORT_ERROR is not None or os.path.getsize(path) > max_bytes:
        return None
    try:
        layer = Sdf.Layer.OpenAsAnonymous(str(path))
    except Exception:
        return None
    if layer is None:
        return None
    return [str(spec.name) for spec in layer.rootPrims if spec.specifier == Sdf.SpecifierDef][:MAX_ROOT_PRIMS]


# --- add_reference -------------------------------------------------------------------------------------------------


def add_reference(
    stage: Any,
    prim_path: str,
    asset_path: str,
    *,
    translation: Any = None,
    rotation: Any = None,
    scale: Any = None,
    instanceable: bool | None = None,
    asset_prim_path: str | None = None,
    correct_units: bool = False,
    library: AssetLibrary | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or author an Xform at prim_path that references asset_path.

    asset_path is absolute or relative to the asset library; it is authored as an absolute forward-slash path.
    rotation is rotateXYZ in degrees; scale is one number or (x, y, z). asset_prim_path picks a prim in the asset
    when it has no defaultPrim. correct_units=True fills an unspecified rotation / scale with the suggested up-axis
    and metersPerUnit correction.
    """
    action = "add_reference"
    dry = _dry(dry_run)
    warnings: list[str] = []
    try:
        _require_pxr()
        _check_stage(stage)
        _check_edit_target(stage, warnings)
        sdf_path, parent = _creation_target(stage, prim_path, warnings)
        asset = _asset_file(asset_path, library)
        asset_text = _fwd(asset)
        _check_not_in_layer_stack(stage, asset)
        header = read_header(asset)
        if not header["header_read"]:
            warnings.append(
                "The asset header was not read (" + str(header.get("header_skipped") or header.get("header_error")) +
                "), so its defaultPrim and units were not checked."
            )
        target_prim = _reference_prim(asset, header, asset_prim_path, warnings)

        units = _unit_check(stage, header, warnings)
        if correct_units and units["suggested"]:
            suggested = units["suggested"]
            if "rotation" in suggested:
                if rotation is None:
                    rotation = suggested["rotation"]
                else:
                    warnings.append("correct_units did not change rotation because one was given; fold the up-axis turn into it.")
            if "scale" in suggested:
                if scale is None:
                    scale = suggested["scale"]
                else:
                    warnings.append("correct_units did not change scale because one was given; multiply it by the suggested factor.")
        ops = _plan_ops(translation, rotation, scale, warnings)
        if ops["ops"]:
            warnings.append(
                "These xform ops are authored on the referencing prim, so they replace the asset root prim's own "
                "xformOpOrder (any transform authored on the asset's default prim stops applying)."
            )
        if instanceable is not None:
            instanceable = _bool(instanceable, "instanceable")
            if instanceable:
                warnings.append(
                    "instanceable=True shares the asset's prototype: descendants become read-only instance proxies "
                    "(material or physics overrides below this prim are ignored)."
                )
        layer_info = _edit_target_info(stage)
        plan: dict[str, Any] = {
            "path": str(sdf_path),
            "parent": str(parent.GetPath()),
            "type": "Xform",
            "reference": {"asset_path": asset_text, "prim_path": target_prim},
            "asset": {
                "name": asset.name,
                "size": asset.stat().st_size,
                "default_prim": header["default_prim"],
                "up_axis": header["up_axis"],
                "meters_per_unit": header["meters_per_unit"],
                "header_read": header["header_read"],
            },
            "stage_units": {"up_axis": units["stage_up_axis"], "meters_per_unit": units["stage_meters_per_unit"]},
            "unit_correction": units["suggested"] or None,
            "xform_ops": ops["ops"],
            "translation": ops["translation"],
            "rotation": ops["rotation"],
            "scale": ops["scale"],
            "instanceable": instanceable,
            "path_note": "The reference uses the absolute forward-slash path, so it only resolves on machines with the "
            "asset pack at the same location.",
            "relative_path_hint": _relative_hint(asset, layer_info),
        }
    except _Refusal as refusal:
        return _finish(action, dry, stage, warnings, error=str(refusal))
    except Exception as exc:  # planning only reads the stage and the asset header, so nothing was written
        return _finish(action, dry, stage, warnings, error=f"Unexpected error while planning (nothing was written): {exc}")

    if dry:
        return _finish(action, True, stage, warnings, key="would_create", plan=plan)
    return _author(stage, sdf_path, plan, ops, instanceable, warnings)


def _asset_file(asset_path: Any, library: AssetLibrary | None) -> Path:
    if not isinstance(asset_path, (str, Path)) or not str(asset_path).strip():
        raise _Refusal("asset_path must be a USD file path (absolute, or relative to the asset library).")
    lib = library or AssetLibrary()
    found = lib.resolve(asset_path)
    if found is None:
        raise _Refusal(f"Asset file not found: {_fwd(asset_path)}. Use the asset library search to find a path.")
    found = Path(os.path.abspath(found))
    ext = found.suffix.lower()
    if ext in MATERIAL_EXTENSIONS:
        raise _Refusal(f"{found.name} is an MDL material, not a USD layer; bind it with a material instead of a reference.")
    if ext not in USD_EXTENSIONS:
        raise _Refusal(f"{found.name} is not a USD file (expected one of {', '.join(sorted(USD_EXTENSIONS))}).")
    return found


def _check_not_in_layer_stack(stage: Any, asset: Path) -> None:
    wanted = os.path.normcase(os.path.abspath(asset))
    for layer in stage.GetLayerStack(True):
        real = getattr(layer, "realPath", "") or ""
        if real and os.path.normcase(os.path.abspath(real)) == wanted:
            raise _Refusal(f"{asset.name} is already a layer of this stage ({layer.identifier}); referencing it would be a cycle.")


def _reference_prim(asset: Path, header: dict[str, Any], asset_prim_path: Any, warnings: list[str]) -> str | None:
    """None means 'the asset's defaultPrim'; otherwise an explicit absolute prim path inside the asset."""
    if asset_prim_path is not None:
        return str(_prim_path(asset_prim_path, "asset_prim_path"))
    if header["default_prim"] or not header["header_read"]:
        return None
    roots = root_prim_names(asset)
    if roots is None:
        warnings.append("The asset has no defaultPrim and is too large to list its root prims; the reference may not resolve.")
        return None
    if len(roots) == 1:
        warnings.append(f"The asset has no defaultPrim, so the reference names its only root prim /{roots[0]} explicitly.")
        return "/" + roots[0]
    if not roots:
        raise _Refusal(f"{asset.name} has no defaultPrim and no root prim to reference.")
    raise _Refusal(
        f"{asset.name} has no defaultPrim and several root prims ({', '.join(roots)}); pass asset_prim_path to pick one."
    )


def _unit_check(stage: Any, header: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    stage_up = str(UsdGeom.GetStageUpAxis(stage))
    stage_mpu = float(UsdGeom.GetStageMetersPerUnit(stage))
    suggested: dict[str, Any] = {}
    if header["header_read"]:
        asset_up = header["up_axis"]
        asset_mpu = header["meters_per_unit"]
        if asset_up is None:
            warnings.append("The asset does not author upAxis (USD falls back to Y); check it stands upright.")
        elif asset_up != stage_up and {asset_up, stage_up} == {"Y", "Z"}:
            turn = [-90.0, 0.0, 0.0] if asset_up == "Z" else [90.0, 0.0, 0.0]
            suggested["rotation"] = turn
            warnings.append(
                f"The asset is {asset_up}-up but the stage is {stage_up}-up: pass rotation={tuple(turn)} "
                "(rotateXYZ degrees) or correct_units=True so it stands upright."
            )
        if asset_mpu is None:
            warnings.append("The asset does not author metersPerUnit (USD falls back to 0.01, centimeters); check its size.")
        elif stage_mpu > 0 and abs(asset_mpu - stage_mpu) > _UNIT_TOLERANCE * max(asset_mpu, stage_mpu):
            factor = round(asset_mpu / stage_mpu, 9)
            suggested["scale"] = [factor, factor, factor]
            warnings.append(
                f"The asset uses metersPerUnit {asset_mpu:g} but the stage uses {stage_mpu:g}: pass scale={factor:g} "
                "or correct_units=True so it keeps its real-world size."
            )
    return {"stage_up_axis": stage_up, "stage_meters_per_unit": stage_mpu, "suggested": suggested}


def _plan_ops(translation: Any, rotation: Any, scale: Any, warnings: list[str]) -> dict[str, Any]:
    t = _vec3(translation, "translation") if translation is not None else None
    r = _vec3(rotation, "rotation") if rotation is not None else None
    s = None
    if scale is not None:
        s = [_finite(scale, "scale")] * 3 if isinstance(scale, numbers.Real) and not isinstance(scale, bool) else _vec3(scale, "scale")
        if any(value == 0.0 for value in s):
            raise _Refusal("scale components must not be 0 (the asset would collapse).")
        if any(value < 0.0 for value in s):
            warnings.append("A negative scale mirrors the asset and flips its face orientation.")
    ops = [name for name, value in (("xformOp:translate", t), ("xformOp:rotateXYZ", r), ("xformOp:scale", s)) if value is not None]
    return {"ops": ops, "translation": t, "rotation": r, "scale": s}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real) or not math.isfinite(float(value)):
        raise _Refusal(f"{name} must contain finite numbers, got {value!r}")
    return float(value)


def _vec3(value: Any, name: str) -> list[float]:
    if isinstance(value, (str, bytes)) or not hasattr(value, "__len__") or len(value) != 3:
        raise _Refusal(f"{name} must be three numbers (x, y, z), got {value!r}")
    return [_finite(item, name) for item in value]


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0", "yes", "no"):
        return value.strip().lower() in ("true", "1", "yes")
    raise _Refusal(f"{name} must be true or false, got {value!r}")


def _relative_hint(asset: Path, layer_info: dict[str, Any] | None) -> str | None:
    if not layer_info or layer_info.get("anonymous") or layer_info.get("session_layer"):
        return None
    layer_path = layer_info.get("identifier") or ""
    if not os.path.isabs(layer_path):
        return None
    try:
        rel = _fwd(os.path.relpath(asset, os.path.dirname(layer_path)))
    except ValueError:  # different drives
        return None
    return rel if rel.startswith("../") else "./" + rel


def _authored_references(spec: Any) -> list[dict[str, Any]]:
    if not spec:
        return []
    ops = spec.referenceList
    items = list(ops.explicitItems) + list(ops.prependedItems) + list(ops.appendedItems)
    return [{"asset_path": str(item.assetPath), "prim_path": str(item.primPath) or None} for item in items]


def _author(
    stage: Any, sdf_path: Any, plan: dict[str, Any], ops: dict[str, Any], instanceable: bool | None, warnings: list[str]
) -> dict[str, Any]:
    """Author the planned prim; on failure remove the specs it added to the edit target layer."""
    action = "add_reference"
    edit_target = stage.GetEditTarget()
    layer = edit_target.GetLayer()
    spec_path = edit_target.MapToSpecPath(sdf_path)
    first_new = next((prefix for prefix in spec_path.GetPrefixes() if not layer.GetPrimAtPath(prefix)), None)
    reference = plan["reference"]
    try:
        prim = UsdGeom.Xform.Define(stage, sdf_path).GetPrim()
        ref = Sdf.Reference(reference["asset_path"], Sdf.Path(reference["prim_path"]) if reference["prim_path"] else Sdf.Path())
        if not prim.GetReferences().AddReference(ref):
            raise ValueError("AddReference returned False")
        xformable = UsdGeom.Xformable(prim)
        if ops["translation"] is not None:
            xformable.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble).Set(Gf.Vec3d(*ops["translation"]))
        if ops["rotation"] is not None:
            xformable.AddRotateXYZOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Vec3f(*ops["rotation"]))
        if ops["scale"] is not None:
            xformable.AddScaleOp(UsdGeom.XformOp.PrecisionFloat).Set(Gf.Vec3f(*ops["scale"]))
        if instanceable is not None:
            prim.SetInstanceable(instanceable)
    except Exception as exc:
        rolled_back = False
        if first_new is not None and layer.GetPrimAtPath(first_new):
            edit = Sdf.BatchNamespaceEdit()
            edit.Add(Sdf.NamespaceEdit.Remove(first_new))
            rolled_back = bool(layer.Apply(edit))
        note = "the new prim spec was removed again" if rolled_back else "the edit target may hold a partial prim spec"
        return _finish(action, False, stage, warnings, key="would_create", plan=plan,
                       error=f"Authoring failed ({note}): {exc}", written=not rolled_back)
    resolved = len(prim.GetPrimStack()) > 1
    if not resolved:
        warnings.append("The reference did not bring in any prim spec; check the asset path and its defaultPrim.")
    if ops["ops"]:
        order_stack = prim.GetAttribute("xformOpOrder").GetPropertyStack()
        if len(order_stack) > 1:
            warnings.append("The asset's root prim authors its own xformOpOrder; the local ops above replace it.")
    spec = layer.GetPrimAtPath(spec_path)
    created = {
        "path": str(prim.GetPath()),
        "type": prim.GetTypeName(),
        "references": _authored_references(spec),
        "xform_op_order": [str(name) for name in (UsdGeom.Xformable(prim).GetXformOpOrderAttr().Get() or [])],
        "instanceable": bool(prim.IsInstanceable()),
        "reference_resolved": resolved,
        "child_count": len(prim.GetChildren()),
    }
    return _finish(action, False, stage, warnings, key="would_create", plan=plan, extra={"created": created}, written=True)
