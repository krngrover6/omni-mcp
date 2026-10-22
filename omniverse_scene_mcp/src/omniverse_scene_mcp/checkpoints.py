"""Scene checkpoints: file-level snapshots of a scene and its local layers, with a plain-English diff and restore.

Works on the files on disk only, so no Isaac Sim or USD Composer needs to be running. A checkpoint copies the scene's
root layer and every local layer it uses (sublayers, references and payloads, found by reading each layer with
Sdf, read-only) that lives inside the project folder. Files outside the project folder, remote assets and files over
the per-file size cap are listed as "not copied". Textures and other non-layer assets are never copied.

Where checkpoints live::

    <project>/.checkpoints/<scene-stem>/<NNNN>-<YYYYmmdd-HHMMSS>-<slug>/
        manifest.json          relative paths, sha256, sizes, note, created time, source mtimes
        files/<relative path>  the copied layers, at their path relative to the project folder

``$OMNIVERSE_SCENE_CHECKPOINTS`` replaces ``<project>/.checkpoints`` as the base folder. The project folder is
``project_dir`` when given, else the nearest folder above the scene (inside the workspace) that holds
PROJECT_CONTEXT.md or a ``.checkpoints`` folder, else the scene's own folder. The scene must be inside the workspace
(``$OMNIVERSE_ROOT``, same rules as file_stage.resolve_scene_file).

Safety rules: every mutating call is a dry run unless dry_run=False; create and prune write only inside the
checkpoint folder; restore writes only files named in the manifest, only inside the project folder, never through a
link that leaves it, and first takes an automatic safety checkpoint of the current files. Restore refuses when a file
changed after that safety checkpoint was taken (for example the app saved at the same moment). Copies are atomic
(temp file + os.replace) and verified by sha256.

Checkpoints hold what is saved on disk: unsaved changes in Isaac Sim or Composer are not in them, and after a restore
an open app must reload the file (File > Reload, or close and reopen it).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .file_stage import SceneFileError, default_workspace_root, resolve_scene_file

try:
    from pxr import Sdf
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Sdf = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

ENV_BASE = "OMNIVERSE_SCENE_CHECKPOINTS"
FOLDER_NAME = ".checkpoints"
MANIFEST_NAME = "manifest.json"
FILES_DIR = "files"
SCENE_MARKER = "scene.json"
FORMAT_VERSION = 1
PROJECT_MARKERS = ("PROJECT_CONTEXT.md",)
LAYER_EXTENSIONS = frozenset({".usd", ".usda", ".usdc"})
PACKAGE_EXTENSIONS = frozenset({".usdz"})
DEFAULT_KEEP = 20
DEFAULT_MAX_TOTAL_MB = 200
DEFAULT_MAX_FILE_MB = 50
DEFAULT_MAX_LINES = 400
DEFAULT_MAX_CHANGES = 200
MAX_DEP_FILES = 500
MAX_NOTE_CHARS = 300
MAX_VALUE_CHARS = 80
_ID_RE = re.compile(r"^(\d{4,})-\d{8}-\d{6}(?:-[a-z0-9-]+)?$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_LOCK = threading.RLock()

UNSAVED_WARNING = (
    "Checkpoints copy the files saved on disk. Unsaved changes in Isaac Sim or USD Composer are not included: "
    "save in the app first if you want them in the checkpoint."
)
APP_RELOAD_WARNING = (
    "If this scene is open in Isaac Sim or USD Composer, reload it there after the restore (File > Reload, or close "
    "and reopen it). Unsaved changes in the app are not in any checkpoint, and saving from the app afterwards would "
    "overwrite the restored files."
)


class CheckpointError(ValueError):
    """Bad input: a scene outside the workspace, an unknown checkpoint id, an unsafe path or missing USD bindings."""


# ------------------------------------------------------------------------------------------------ paths and scope


def _norm(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.abspath(path))


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(child: str | os.PathLike[str], parent: str | os.PathLike[str]) -> bool:
    child_n, parent_n = os.path.normcase(os.fspath(child)), os.path.normcase(os.fspath(parent))
    try:
        return os.path.commonpath([child_n, parent_n]) == parent_n
    except ValueError:
        return False


def _fwd(path: str | os.PathLike[str]) -> str:
    return os.fspath(path).replace("\\", "/")


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return False


def _safe_name(text: str, fallback: str = "scene", limit: int = 60) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")[:limit]
    return slug or fallback


def _slug(note: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", note.lower()).strip("-")[:40].strip("-")
    return slug or fallback


class _Scene:
    """One scene: its file, project folder, path relative to the project and where its checkpoints live."""

    def __init__(self, scene: Path, project: Path, base: Path, base_is_default: bool) -> None:
        self.scene = scene
        self.project = project
        self.rel = Path(os.path.relpath(scene, project)).as_posix()
        self.base = base
        self.base_is_default = base_is_default
        self.identity = self.rel if base_is_default else _fwd(_norm(scene))
        self.store = self._store_dir()

    def _store_dir(self) -> Path:
        stem = _safe_name(self.scene.stem)
        plain = self.base / stem
        hashed = self.base / f"{stem}-{hashlib.sha1(self.identity.lower().encode('utf-8')).hexdigest()[:8]}"
        owner = _read_marker(plain)
        if owner is None or owner == self.identity:
            if owner is not None or not hashed.exists():
                return plain
        return hashed

    def describe(self) -> dict[str, Any]:
        return {"scene": _fwd(self.scene), "project": _fwd(self.project), "scene_relative": self.rel,
                "checkpoint_folder": _fwd(self.store)}


def _read_marker(folder: Path) -> str | None:
    try:
        data = json.loads((folder / SCENE_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scene = data.get("scene") if isinstance(data, dict) else None
    return scene if isinstance(scene, str) else None


def _resolve(scene_path: Any, project_dir: Any = None, workspace_root: Any = None) -> _Scene:
    try:
        scene = Path(resolve_scene_file(scene_path, workspace_root=workspace_root))
    except SceneFileError as exc:
        raise CheckpointError(str(exc)) from exc
    root = Path(workspace_root if workspace_root is not None else default_workspace_root()).resolve()
    if project_dir is not None:
        candidate = Path(project_dir)
        if not candidate.is_absolute():
            candidate = root / candidate
        project = candidate.resolve()
        if not project.is_dir():
            raise CheckpointError(f"Project folder does not exist: {_fwd(project)}")
        if not _within(project, root):
            raise CheckpointError(f"Project folder is outside the workspace {_fwd(root)}: {_fwd(project)}")
        if not _within(scene, project) or _norm(scene) == _norm(project):
            raise CheckpointError(f"The scene {_fwd(scene)} is not inside the project folder {_fwd(project)}")
    else:
        project = scene.parent
        for folder in [scene.parent, *scene.parent.parents]:
            if not _within(folder, root) or _norm(folder) == _norm(root):
                break
            if any((folder / marker).is_file() for marker in PROJECT_MARKERS) or (folder / FOLDER_NAME).is_dir():
                project = folder
                break
    configured = os.environ.get(ENV_BASE, "").strip()
    if configured:
        base = Path(configured)
        if not base.is_absolute():
            base = root / base
        base = Path(os.path.abspath(base))
        default = False
    else:
        base = project / FOLDER_NAME
        default = True
        if base.exists() and (_is_link(base) or not _within(_real(base), _real(project))):
            raise CheckpointError(f"{_fwd(base)} is a link that leaves the project folder; refusing to use it.")
    return _Scene(scene, project, base, default)


def _require_pxr() -> None:
    if Sdf is None:
        raise CheckpointError(f"USD Python bindings (pxr) are required for this: {_PXR_IMPORT_ERROR}")


# --------------------------------------------------------------------------------------------- dependency discovery


def _open_layer(path: str | os.PathLike[str]) -> Any:
    """The file's content as a fresh anonymous Sdf layer (never the shared registry copy), or None."""
    _require_pxr()
    try:
        return Sdf.Layer.OpenAsAnonymous(os.fspath(path))
    except Exception:  # noqa: BLE001 - Tf parse errors
        return None


def _listop_items(listop: Any) -> list[Any]:
    try:
        return list(listop.GetAddedOrExplicitItems())
    except Exception:  # noqa: BLE001
        return []


def _arc_roles(layer: Any) -> dict[str, str]:
    """asset path -> 'sublayer' | 'reference' | 'payload' for the composition arcs authored in one layer."""
    roles: dict[str, str] = {}
    for sub in layer.subLayerPaths:
        roles.setdefault(str(sub), "sublayer")

    def visit(path: Any) -> None:
        if not path.IsPrimPath():
            return
        spec = layer.GetPrimAtPath(path)
        if spec is None:
            return
        for item in _listop_items(spec.referenceList):
            if item.assetPath:
                roles.setdefault(item.assetPath, "reference")
        for item in _listop_items(spec.payloadList):
            if item.assetPath:
                roles.setdefault(item.assetPath, "payload")

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return roles


def _anchor(asset: str, layer_file: str) -> str | None:
    """Absolute local path for an authored asset path, or None for remote/expression paths."""
    text = asset.split(":SDF_FORMAT_ARGS:", 1)[0].strip()
    if "[" in text:
        text = text.split("[", 1)[0]
    if not text or text.startswith("`"):
        return None
    if text.lower().startswith("file:"):
        rest = text[5:]
        if rest.startswith("//"):
            rest = rest[2:]
            if rest.lower().startswith("localhost/"):
                rest = rest[9:]
            elif not rest.startswith("/"):
                return None  # file://host/share: a network path, never followed
        text = rest[1:] if _DRIVE_RE.match(rest[1:]) and rest.startswith("/") else rest
    elif _SCHEME_RE.match(text) and not _DRIVE_RE.match(text):
        return None
    if not os.path.isabs(text) and not _DRIVE_RE.match(text):
        text = os.path.join(os.path.dirname(layer_file), text)
    return os.path.abspath(text)


def _collect(ctx: _Scene, max_file_bytes: int, extra_rels: tuple[str, ...] = ()) -> dict[str, Any]:
    """Files to copy (root first) and the dependencies that will not be copied, with the reason."""
    project_real = _real(ctx.project)
    base_real = _real(ctx.base)
    files: list[dict[str, Any]] = []
    not_copied: list[dict[str, str]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    queue: list[tuple[str, str]] = [(str(ctx.scene), "root")]

    def add(path: str, role: str) -> bool:
        real = _real(path)
        if real in seen:
            return False
        seen.add(real)
        info = os.stat(path)
        rel = Path(os.path.relpath(os.path.realpath(path), os.path.realpath(ctx.project))).as_posix()
        files.append({"abs": os.path.realpath(path), "path": rel, "role": role, "size": info.st_size,
                      "mtime_ns": info.st_mtime_ns})
        return True

    while queue:
        path, role = queue.pop(0)
        if not add(path, role):
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in LAYER_EXTENSIONS:
            continue
        layer = _open_layer(path)
        if layer is None:
            name = Path(os.path.relpath(path, ctx.project)).as_posix()
            warnings.append(f"{name} could not be read as USD; it is copied, but the layers it uses are not known.")
            continue
        roles = _arc_roles(layer)
        for asset in sorted(set(layer.GetCompositionAssetDependencies()) | set(roles)):
            asset_role = roles.get(asset, "dependency")
            target = _anchor(asset, path)
            if target is None:
                not_copied.append({"path": asset, "reason": "not a local file (remote or expression path)"})
                continue
            shown = _fwd(asset)
            if not _within(_norm(target), _norm(ctx.project)) or not _within(_real(target), project_real):
                not_copied.append({"path": shown, "reason": "outside the project folder"})
                continue
            if _within(_real(target), base_real):
                continue
            if not os.path.isfile(target):
                if _real(target) not in seen:
                    not_copied.append({"path": shown, "reason": "missing on disk"})
                continue
            if _real(target) in seen:
                continue
            size = os.path.getsize(target)
            if size > max_file_bytes:
                reason = f"{_human(size)} is over the {_human(max_file_bytes)} per-file cap"
                not_copied.append({"path": shown, "reason": reason})
                continue
            if len(files) + len(queue) >= MAX_DEP_FILES:
                not_copied.append({"path": shown, "reason": f"more than {MAX_DEP_FILES} files"})
                continue
            queue.append((target, asset_role))
    for rel in extra_rels:
        target = os.path.join(ctx.project, *rel.split("/"))
        if os.path.isfile(target) and _within(_real(target), project_real) and _real(target) not in seen:
            add(target, "restore target")
    return {"files": files, "not_copied": not_copied, "warnings": warnings}


def _mb(size: int | float) -> float:
    return round(size / (1024 * 1024), 3)


def _human(size: int | float) -> str:
    if size < 1024:
        return f"{int(size)} bytes"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


# ------------------------------------------------------------------------------------------------- file helpers


def _sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(src: str | os.PathLike[str], dst: Path) -> tuple[str, int]:
    """Copy src to dst through a temp file in dst's folder; returns (sha256, size) checked on the written file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.{uuid.uuid4().hex[:8]}.tmp"
    digest = hashlib.sha256()
    size = 0
    try:
        with open(src, "rb") as reader, open(tmp, "wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        if _sha256(tmp) != digest.hexdigest():
            raise OSError(f"copy of {_fwd(src)} did not verify")
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            tmp.unlink()
    return digest.hexdigest(), size


def _ignore_in_git(folder: Path) -> None:
    """Keep the default <project>/.checkpoints/ folder out of git: it gets a '.gitignore' of '*' (it ignores itself).

    Only the default folder is touched ($OMNIVERSE_SCENE_CHECKPOINTS gets nothing it did not ask for); an existing
    .gitignore is left alone. Never raises: checkpoints still work in a folder that cannot take the file.
    """
    marker = folder / ".gitignore"
    try:
        if not marker.exists():
            with open(marker, "x", encoding="utf-8", newline="\n") as handle:
                handle.write("# Created by omniverse_scene_mcp.checkpoints: scene checkpoints are local, never committed.\n*\n")
    except OSError:  # FileExistsError from a parallel writer, or a read-only folder
        pass


def _write_json(path: Path, data: Any) -> None:
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _stat(path: str | os.PathLike[str]) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _iso(ns: int | None = None) -> str:
    moment = datetime.fromtimestamp(ns / 1e9) if ns is not None else datetime.now()
    return moment.astimezone().isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------------------ the store


def _entries(ctx: _Scene) -> list[dict[str, Any]]:
    """Checkpoints of this scene, oldest first; damaged ones carry 'damaged': reason."""
    if not ctx.store.is_dir():
        return []
    found = []
    for child in ctx.store.iterdir():
        match = _ID_RE.match(child.name)
        if not match or not child.is_dir() or _is_link(child):
            continue
        entry: dict[str, Any] = {"id": child.name, "number": int(match.group(1)), "folder": child}
        try:
            manifest = json.loads((child / MANIFEST_NAME).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                raise ValueError("manifest has no file list")
            entry["manifest"] = manifest
        except (OSError, ValueError) as exc:
            entry["damaged"] = f"manifest.json unreadable: {exc}"
        found.append(entry)
    found.sort(key=lambda item: (item["number"], item["id"]))
    return found


def _find(ctx: _Scene, ident: Any, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    entries = _entries(ctx) if entries is None else entries
    usable = [entry for entry in entries if "manifest" in entry]
    if not usable:
        raise CheckpointError(f"No checkpoints yet for {ctx.rel}. Create one with create_checkpoint(dry_run=False).")
    text = str(ident).strip().lower() if ident is not None else "latest"
    if text in ("latest", "last", "newest"):
        return usable[-1]
    if text in ("previous", "prev"):
        if len(usable) < 2:
            raise CheckpointError("There is only one checkpoint, so there is no 'previous' one.")
        return usable[-2]
    for entry in usable:
        if entry["id"] == str(ident).strip():
            return entry
    if text.isdigit():
        for entry in usable:
            if entry["number"] == int(text):
                return entry
    prefixed = [entry for entry in usable if entry["id"].startswith(str(ident).strip())]
    if len(prefixed) == 1:
        return prefixed[0]
    known = ", ".join(entry["id"] for entry in usable[-5:])
    raise CheckpointError(f"No checkpoint {ident!r} for {ctx.rel}. Recent ones: {known}")


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    manifest = entry.get("manifest") or {}
    row = {
        "id": entry["id"],
        "number": entry["number"],
        "created": manifest.get("created"),
        "note": manifest.get("note", ""),
        "kind": manifest.get("kind", "manual"),
        "file_count": len(manifest.get("files", [])),
        "total_bytes": manifest.get("total_bytes"),
        "not_copied": len(manifest.get("not_copied", [])),
    }
    if "damaged" in entry:
        row["damaged"] = entry["damaged"]
    return row


# ------------------------------------------------------------------------------------------------ create / list


def create_checkpoint(
    scene_path: Any,
    note: str = "",
    *,
    dry_run: bool = True,
    max_total_mb: float = DEFAULT_MAX_TOTAL_MB,
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
    project_dir: Any = None,
    workspace_root: Any = None,
) -> dict[str, Any]:
    """Plan (dry_run=True, the default) or take a checkpoint of the scene and its local layers."""
    ctx = _resolve(scene_path, project_dir, workspace_root)
    with _LOCK:
        return _create(ctx, note, dry_run=dry_run, max_total_mb=max_total_mb, max_file_mb=max_file_mb)


def _create(
    ctx: _Scene,
    note: str,
    *,
    dry_run: bool,
    max_total_mb: float,
    max_file_mb: float = DEFAULT_MAX_FILE_MB,
    kind: str = "manual",
    extra_rels: tuple[str, ...] = (),
    restoring: str | None = None,
) -> dict[str, Any]:
    _require_pxr()
    note = " ".join(str(note or "").split())[:MAX_NOTE_CHARS]
    max_total = int(float(max_total_mb) * 1024 * 1024)
    max_file = int(float(max_file_mb) * 1024 * 1024)
    if max_total <= 0 or max_file <= 0:
        raise CheckpointError("max_total_mb and max_file_mb must be positive.")
    found = _collect(ctx, max_file, extra_rels)
    files, not_copied = found["files"], found["not_copied"]
    total = sum(item["size"] for item in files)
    entries = _entries(ctx)
    number = (entries[-1]["number"] + 1) if entries else 1
    slug = _slug(note, "safety" if kind == "safety" else "checkpoint")
    planned_id = f"{number:04d}-{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": bool(dry_run),
        "created": False,
        **ctx.describe(),
        "id": planned_id,
        "files": [{"path": item["path"], "role": item["role"], "size": item["size"]} for item in files],
        "not_copied": not_copied,
        "total_bytes": total,
        "total_mb": _mb(total),
        "max_total_mb": max_total_mb,
        "warnings": [*found["warnings"], UNSAVED_WARNING],
    }
    if total > max_total:
        result.update(ok=False, refused="size",
                      error=f"This checkpoint would copy {_human(total)}, more than max_total_mb={max_total_mb}. "
                            "Raise max_total_mb or move big files out of the project folder.")
        return result
    if dry_run:
        result["message"] = (f"Dry run: would copy {len(files)} file(s), {_mb(total)} MB, to "
                             f"{_fwd(ctx.store)}/{planned_id}. Nothing was written; pass dry_run=False to create it.")
        return result

    ctx.store.mkdir(parents=True, exist_ok=True)
    if _is_link(ctx.store) or not _within(_real(ctx.store), _real(ctx.base)):
        raise CheckpointError(f"{_fwd(ctx.store)} is a link that leaves the checkpoint folder; refusing to write.")
    if ctx.base_is_default:
        _ignore_in_git(ctx.base)
    if _read_marker(ctx.store) is None:
        _write_json(ctx.store / SCENE_MARKER, {"scene": ctx.identity, "created": _iso()})
    incoming = ctx.store / f".incoming-{uuid.uuid4().hex[:12]}"
    incoming.mkdir()
    try:
        records = []
        for item in files:
            for _attempt in range(2):
                before = _stat(item["abs"])
                sha, size = _copy_verified(item["abs"], incoming / FILES_DIR / Path(*item["path"].split("/")))
                after = _stat(item["abs"])
                if before == after and before is not None and size == before[1]:
                    break
            else:
                raise CheckpointError(f"{item['path']} kept changing while it was copied; try again in a moment.")
            records.append({"path": item["path"], "role": item["role"], "size": size, "sha256": sha,
                            "source_mtime_ns": after[0], "source_mtime": _iso(after[0])})
        manifest = {
            "format": FORMAT_VERSION,
            "id": None,
            "number": None,
            "kind": kind,
            "note": note,
            "created": _iso(),
            "scene": ctx.rel,
            "scene_name": ctx.scene.name,
            "project_name": ctx.project.name,
            "files": records,
            "not_copied": not_copied,
            "total_bytes": sum(record["size"] for record in records),
        }
        if restoring:
            manifest["taken_before_restoring"] = restoring
        final = None
        for _attempt in range(50):
            final_id = f"{number:04d}-{datetime.now():%Y%m%d-%H%M%S}-{slug}"
            manifest.update(id=final_id, number=number)
            _write_json(incoming / MANIFEST_NAME, manifest)
            try:
                os.rename(incoming, ctx.store / final_id)
            except FileExistsError:
                number += 1
                continue
            except OSError:
                if (ctx.store / final_id).exists():
                    number += 1
                    continue
                raise
            final = final_id
            break
        if final is None:
            raise CheckpointError("Could not pick a free checkpoint number.")
    finally:
        if incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)
    result.update(created=True, id=final, path=_fwd(ctx.store / final), manifest=manifest,
                  message=f"Checkpoint {final} saved: {len(records)} file(s), {_mb(manifest['total_bytes'])} MB.")
    result["files"] = [{key: r[key] for key in ("path", "role", "size", "sha256")} for r in records]
    return result


def list_checkpoints(scene_path: Any, *, project_dir: Any = None, workspace_root: Any = None) -> dict[str, Any]:
    """Checkpoints of one scene, oldest first. Never creates anything."""
    ctx = _resolve(scene_path, project_dir, workspace_root)
    rows = [_summary(entry) for entry in _entries(ctx)]
    return {"ok": True, **ctx.describe(), "count": len(rows), "checkpoints": rows,
            "latest": rows[-1]["id"] if rows else None,
            "message": f"{len(rows)} checkpoint(s) for {ctx.rel}." if rows else
                       f"No checkpoints yet for {ctx.rel}. create_checkpoint(dry_run=False) takes one."}


# ----------------------------------------------------------------------------------------------------- diff


class _Side:
    """One side of a diff: 'current' (files on disk now) or a checkpoint folder."""

    def __init__(self, label: str, files: dict[str, dict[str, Any]], damaged: list[str] | None = None) -> None:
        self.label = label
        self.files = files  # rel -> {"abs": path, "sha256": str}
        self.damaged = damaged or []


def _side(ctx: _Scene, ident: Any, entries: list[dict[str, Any]]) -> _Side:
    if ident is not None and str(ident).strip().lower() == "current":
        found = _collect(ctx, DEFAULT_MAX_FILE_MB * 1024 * 1024)
        files = {item["path"]: {"abs": item["abs"], "sha256": _sha256(item["abs"])} for item in found["files"]}
        return _Side("current", files)
    entry = _find(ctx, ident, entries)
    files, damaged = {}, []
    for record in entry["manifest"]["files"]:
        rel = str(record.get("path", ""))
        copy = entry["folder"] / FILES_DIR / Path(*rel.split("/"))
        if not rel or not _within(_norm(copy), _norm(entry["folder"])) or not copy.is_file():
            damaged.append(rel)
            continue
        files[rel] = {"abs": str(copy), "sha256": record.get("sha256")}
    return _Side(entry["id"], files, damaged)


def _layer_text(path: str) -> str | None:
    """Text of a layer file: .usda as is, crate files exported to text in memory, None for packages/binaries."""
    ext = os.path.splitext(path)[1].lower()
    if ext in PACKAGE_EXTENSIONS:
        return None
    try:
        with open(path, "rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    if head.startswith(b"#usda") or (ext == ".usda"):
        with open(path, "rb") as handle:
            return handle.read().decode("utf-8", "replace")
    if ext in LAYER_EXTENSIONS:
        layer = _open_layer(path)
        if layer is not None:
            try:
                return layer.ExportToString()
            except Exception:  # noqa: BLE001
                return None
    return None


def diff(
    scene_path: Any,
    a: Any = None,
    b: Any = "current",
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_changes: int = DEFAULT_MAX_CHANGES,
    project_dir: Any = None,
    workspace_root: Any = None,
) -> dict[str, Any]:
    """What changed from a to b. a defaults to the latest checkpoint, b to 'current' (the files on disk now).

    Each side is 'current', a checkpoint id, its number, 'latest' or 'previous'. Returns per-file unified diffs
    (text .usda; .usdc exported to text in memory) and a USD-aware plain-English summary.
    """
    _require_pxr()
    ctx = _resolve(scene_path, project_dir, workspace_root)
    max_lines = max(1, int(max_lines))
    max_changes = max(1, int(max_changes))
    entries = _entries(ctx)
    old, new = _side(ctx, a, entries), _side(ctx, b, entries)
    file_rows: list[dict[str, Any]] = []
    sentences: list[str] = []
    changes: list[dict[str, Any]] = []
    lines_left = max_lines
    truncated = False
    total_changes = 0
    for rel in sorted(set(old.files) | set(new.files), key=lambda r: (r != ctx.rel, r)):
        before, after = old.files.get(rel), new.files.get(rel)
        if before and after and before["sha256"] == after["sha256"]:
            continue
        status = "changed" if before and after else ("added" if after else "removed")
        row: dict[str, Any] = {"path": rel, "status": status, "diff": "", "summary": []}
        old_text = _layer_text(before["abs"]) if before else ""
        new_text = _layer_text(after["abs"]) if after else ""
        if old_text is None or new_text is None:
            row["diff"] = "(binary file changed; no line diff)"
        else:
            lines = list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                              fromfile=f"{old.label}/{rel}", tofile=f"{new.label}/{rel}", lineterm=""))
            if len(lines) > lines_left:
                lines = lines[:lines_left] + [f"... diff cut at max_lines={max_lines}"]
                truncated = True
            lines_left = max(0, lines_left - len(lines))
            row["diff"] = "\n".join(lines)
            row["diff_lines"] = len(lines)
        prefix = "" if rel == ctx.rel else f"In {rel}: "
        if status == "added":
            found = [{"kind": "file_added", "path": rel, "text": f"{rel} became part of the scene."}]
        elif status == "removed":
            found = [{"kind": "file_removed", "path": rel, "text": f"{rel} is no longer part of the scene."}]
        else:
            found = []
        if before and after and os.path.splitext(rel)[1].lower() in LAYER_EXTENSIONS:
            old_layer, new_layer = _open_layer(before["abs"]), _open_layer(after["abs"])
            if old_layer is not None and new_layer is not None:
                found.extend(layer_changes(old_layer, new_layer))
            else:
                found.append({"kind": "unreadable", "path": rel,
                              "text": f"{rel} changed but could not be read as USD."})
        for change in found:
            total_changes += 1
            if len(changes) < max_changes:
                row["summary"].append(change["text"])
                changes.append({**change, "file": rel})
                sentences.append(prefix + change["text"])
        file_rows.append(row)
    if total_changes > max_changes:
        truncated = True
        sentences.append(f"...and {total_changes - max_changes} more change(s) (max_changes={max_changes}).")
    warnings = []
    if old.damaged or new.damaged:
        warnings.append("Some checkpoint files are missing: " + ", ".join(old.damaged + new.damaged))
    if "current" in (old.label, new.label):
        warnings.append("'current' means the files saved on disk; unsaved changes in an open app are not compared.")
    same = not file_rows
    return {
        "ok": True,
        **ctx.describe(),
        "a": old.label,
        "b": new.label,
        "same": same,
        "files": file_rows,
        "summary": sentences or ["No differences: the files are identical."],
        "changes": changes,
        "truncated": truncated,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------------------- USD-aware layer diff

_SKIP_PRIM_KEYS = frozenset({"references", "payload", "variantSelection", "specifier", "typeName", "primOrder",
                             "propertyOrder", "variantSetNames"})
_SKIP_LAYER_KEYS = frozenset({"subLayers", "subLayerOffsets", "defaultPrim", "primOrder"})


def _fmt_num(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _fmt(value: Any) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, (bool, int, float)):
        return _fmt_num(value)
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else value[: MAX_VALUE_CHARS - 3] + "..."
    if Sdf is not None and isinstance(value, Sdf.AssetPath):
        return f"@{value.path}@"
    if Sdf is not None and isinstance(value, Sdf.Path):
        return str(value)
    seq = _as_seq(value)
    if seq is not None:
        if len(seq) > 8:
            return f"[{len(seq)} values]"
        return "(" + ", ".join(_fmt(item) for item in seq) + ")"
    text = " ".join(str(value).split())
    return text if len(text) <= MAX_VALUE_CHARS else text[: MAX_VALUE_CHARS - 3] + "..."


def _as_seq(value: Any) -> list[Any] | None:
    if isinstance(value, (str, bytes, dict)):
        return None
    try:
        length = len(value)
    except TypeError:
        return None
    if length > 100000:
        return None
    try:
        return [value[i] for i in range(length)]
    except Exception:  # noqa: BLE001
        return None


def _same(old: Any, new: Any) -> bool:
    return repr(old) == repr(new)


def _article(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _arc_text(item: Any) -> str:
    asset = getattr(item, "assetPath", "") or ""
    prim = str(getattr(item, "primPath", "") or "")
    if asset and prim:
        return f"{asset} ({prim})"
    return asset or prim or "an internal prim"


def _prim_paths(layer: Any) -> dict[str, Any]:
    paths: dict[str, Any] = {}

    def visit(path: Any) -> None:
        if path.IsPrimPath():
            spec = layer.GetPrimAtPath(path)
            if spec is not None:
                paths[str(path)] = spec

    layer.Traverse(Sdf.Path.absoluteRootPath, visit)
    return paths


def _prop_state(prop: Any) -> dict[str, Any]:
    state: dict[str, Any] = {"kind": "relationship" if isinstance(prop, Sdf.RelationshipSpec) else "attribute"}
    if state["kind"] == "relationship":
        state["targets"] = [str(t) for t in _listop_items(prop.targetPathList)]
    else:
        state["type"] = str(prop.typeName)
        state["default"] = prop.default if prop.HasDefaultValue() else None
        state["has_default"] = bool(prop.HasDefaultValue())
        samples = prop.GetInfo("timeSamples") if prop.HasInfo("timeSamples") else None
        state["samples"] = dict(samples) if samples else {}
        state["connections"] = [str(t) for t in _listop_items(prop.connectionPathList)]
    return state


def _signature(spec: Any, depth: int = 0) -> str:
    parts: list[Any] = [spec.typeName, str(spec.specifier)]
    for prop in spec.properties:
        state = _prop_state(prop)
        parts.append((prop.name, repr(sorted((k, repr(v)) for k, v in state.items()))))
    if depth < 20:
        for child in spec.nameChildren:
            parts.append((child.name, _signature(child, depth + 1)))
    return repr(parts)


def _shape(spec: Any) -> tuple[Any, ...]:
    props = tuple(sorted(p.name for p in spec.properties))
    return spec.typeName, props, tuple(sorted(c.name for c in spec.nameChildren))


def _top_level(paths: set[str]) -> list[str]:
    return sorted(p for p in paths if str(Sdf.Path(p).GetParentPath()) not in paths)


def layer_changes(old: Any, new: Any) -> list[dict[str, Any]]:
    """Plain-English changes between two Sdf layers (layer metadata, prims, variants, arcs, properties)."""
    _require_pxr()
    out: list[dict[str, Any]] = []
    _layer_meta_changes(old, new, out)
    old_prims, new_prims = _prim_paths(old), _prim_paths(new)
    removed = _top_level(set(old_prims) - set(new_prims))
    added = _top_level(set(new_prims) - set(old_prims))
    pairs: list[tuple[str, str]] = []
    for matcher in (lambda s: _signature(s), _shape):
        for old_path in list(removed):
            key = matcher(old_prims[old_path])
            candidates = [p for p in added if matcher(new_prims[p]) == key]
            rivals = [p for p in removed if matcher(old_prims[p]) == key]
            if len(candidates) == 1 and (len(rivals) == 1 or matcher is not _shape):
                pairs.append((old_path, candidates[0]))
                removed.remove(old_path)
                added.remove(candidates[0])
    for old_path, new_path in pairs:
        old_sdf, new_sdf = Sdf.Path(old_path), Sdf.Path(new_path)
        if old_sdf.GetParentPath() == new_sdf.GetParentPath():
            text = f"You renamed {old_path} to {new_sdf.name}."
            kind = "prim_renamed"
        elif old_sdf.name == new_sdf.name:
            text = f"You moved {old_path} under {new_sdf.GetParentPath()} (now {new_path})."
            kind = "prim_reparented"
        else:
            text = f"You renamed and moved {old_path} to {new_path}."
            kind = "prim_renamed"
        out.append({"kind": kind, "path": new_path, "old": old_path, "new": new_path, "text": text})
        _prim_changes(new_path, old_prims[old_path], new_prims[new_path], out)
        # Children of a renamed prim keep their relative paths: compare them too.
        for child_old, child_spec in old_prims.items():
            if child_old.startswith(old_path + "/") or child_old.startswith(old_path + "{"):
                child_new = new_path + child_old[len(old_path):]
                if child_new in new_prims:
                    _prim_changes(child_new, child_spec, new_prims[child_new], out)
    for path in removed:
        spec = old_prims[path]
        noun = f" ({_article(spec.typeName)})" if spec.typeName else ""
        count = sum(1 for p in old_prims if p.startswith(path + "/") or p.startswith(path + "{"))
        extra = f" and the {count} prim(s) under it" if count else ""
        out.append({"kind": "prim_removed", "path": path, "text": f"You deleted {path}{noun}{extra}."})
    for path in added:
        spec = new_prims[path]
        noun = f" ({_article(spec.typeName)})" if spec.typeName else ""
        verb = "added an override for" if spec.specifier == Sdf.SpecifierOver else "added"
        out.append({"kind": "prim_added", "path": path, "text": f"You {verb} {path}{noun}."})
    renamed_old = [old for old, _new in pairs]
    for path in sorted(set(old_prims) & set(new_prims)):
        if any(path == r or path.startswith(r + "/") or path.startswith(r + "{") for r in renamed_old):
            continue
        _prim_changes(path, old_prims[path], new_prims[path], out)
    return out


def _layer_meta_changes(old: Any, new: Any, out: list[dict[str, Any]]) -> None:
    old_subs, new_subs = [str(p) for p in old.subLayerPaths], [str(p) for p in new.subLayerPaths]
    for sub in new_subs:
        if sub not in old_subs:
            out.append({"kind": "sublayer_added", "path": sub, "text": f"You added the sublayer {sub}."})
    for sub in old_subs:
        if sub not in new_subs:
            out.append({"kind": "sublayer_removed", "path": sub, "text": f"You removed the sublayer {sub}."})
    common_old = [s for s in old_subs if s in new_subs]
    common_new = [s for s in new_subs if s in old_subs]
    if common_old != common_new:
        out.append({"kind": "sublayer_order", "old": old_subs, "new": new_subs,
                    "text": f"You changed the sublayer order (strongest first) to {', '.join(new_subs)}."})
    if old.defaultPrim != new.defaultPrim:
        out.append({"kind": "default_prim", "old": old.defaultPrim, "new": new.defaultPrim,
                    "text": f"You changed the default prim from {old.defaultPrim or 'nothing'} to "
                            f"{new.defaultPrim or 'nothing'}."})
    old_root, new_root = old.pseudoRoot, new.pseudoRoot
    for key in sorted((set(old_root.ListInfoKeys()) | set(new_root.ListInfoKeys())) - _SKIP_LAYER_KEYS):
        before = old_root.GetInfo(key) if old_root.HasInfo(key) else None
        after = new_root.GetInfo(key) if new_root.HasInfo(key) else None
        if not _same(before, after):
            out.append({"kind": "layer_metadata", "key": key, "old": _fmt(before), "new": _fmt(after),
                        "text": f"You changed the layer's {key} from {_fmt(before)} to {_fmt(after)}."})


def _spec_word(specifier: Any) -> str:
    return str(specifier).replace("Sdf.Specifier", "").lower() or str(specifier)


def _prim_changes(path: str, old: Any, new: Any, out: list[dict[str, Any]]) -> None:
    if old.typeName != new.typeName:
        out.append({"kind": "type_changed", "path": path, "old": old.typeName, "new": new.typeName,
                    "text": f"You changed {path} from {old.typeName or 'no type'} to {new.typeName or 'no type'}."})
    if old.specifier != new.specifier:
        out.append({"kind": "specifier_changed", "path": path, "old": str(old.specifier), "new": str(new.specifier),
                    "text": f"You changed how {path} is defined ({_spec_word(old.specifier)} -> "
                            f"{_spec_word(new.specifier)})."})
    old_sel, new_sel = dict(old.variantSelections), dict(new.variantSelections)
    for name in sorted(set(old_sel) | set(new_sel)):
        before, after = old_sel.get(name), new_sel.get(name)
        if before == after:
            continue
        if before and after:
            text = f"You switched the '{name}' variant on {path} from {before} to {after}."
        elif after:
            text = f"You picked '{after}' for the '{name}' variant on {path}."
        else:
            text = f"You cleared the '{name}' variant selection on {path} (was {before})."
        out.append({"kind": "variant_selection", "path": path, "variant_set": name, "old": before, "new": after,
                    "text": text})
    for field, word in (("referenceList", "reference"), ("payloadList", "payload")):
        before = {_arc_text(i) for i in _listop_items(getattr(old, field))}
        after = {_arc_text(i) for i in _listop_items(getattr(new, field))}
        for item in sorted(after - before):
            out.append({"kind": f"{word}_added", "path": path, "new": item,
                        "text": f"You added {_article(word)} to {item} on {path}."})
        for item in sorted(before - after):
            out.append({"kind": f"{word}_removed", "path": path, "old": item,
                        "text": f"You removed the {word} to {item} from {path}."})
    for key in sorted((set(old.ListInfoKeys()) | set(new.ListInfoKeys())) - _SKIP_PRIM_KEYS):
        before = old.GetInfo(key) if old.HasInfo(key) else None
        after = new.GetInfo(key) if new.HasInfo(key) else None
        if _same(before, after):
            continue
        if key == "active":
            text = f"You deactivated {path}." if after is False else f"You activated {path} again."
        else:
            text = f"You changed {key} on {path} from {_fmt(before)} to {_fmt(after)}."
        out.append({"kind": "prim_metadata", "path": path, "key": key, "old": _fmt(before), "new": _fmt(after),
                    "text": text})
    old_props = {p.name: p for p in old.properties}
    new_props = {p.name: p for p in new.properties}
    for name in sorted(set(old_props) | set(new_props)):
        before = _prop_state(old_props[name]) if name in old_props else None
        after = _prop_state(new_props[name]) if name in new_props else None
        if before is not None and after is not None and _same(before, after):
            continue
        _property_change(path, name, before, after, out)


def _value_change(path: str, name: str, before: Any, after: Any) -> str:
    verb = None
    if name.startswith("xformOp:translate"):
        verb = "moved"
    elif name.startswith("xformOp:rotate") or name.startswith("xformOp:orient"):
        verb = "rotated"
    elif name.startswith("xformOp:scale"):
        verb = "scaled"
    if name == "visibility":
        if after == "invisible":
            return f"You hid {path}."
        if before == "invisible":
            return f"You made {path} visible again."
    if verb:
        detail = "" if name.count(":") <= 1 else f" ({name})"
        if before is None:
            return f"You {verb} {path}{detail} to {_fmt(after)}."
        if after is None:
            return f"You cleared {name} on {path} (was {_fmt(before)})."
        old_seq, new_seq = _as_seq(before), _as_seq(after)
        if old_seq is not None and new_seq is not None and len(old_seq) == len(new_seq) == 3:
            changed = [i for i in range(3) if not _same(old_seq[i], new_seq[i])]
            if len(changed) == 1:
                axis = "xyz"[changed[0]]
                return (f"You {verb} {path}{detail} from {axis}={_fmt(old_seq[changed[0]])} "
                        f"to {axis}={_fmt(new_seq[changed[0]])}.")
        return f"You {verb} {path}{detail} from {_fmt(before)} to {_fmt(after)}."
    if before is None:
        return f"You set {name} on {path} to {_fmt(after)}."
    if after is None:
        return f"You cleared {name} on {path} (was {_fmt(before)})."
    return f"You changed {name} on {path} from {_fmt(before)} to {_fmt(after)}."


def _property_change(
    path: str, name: str, before: dict | None, after: dict | None, out: list[dict[str, Any]]
) -> None:
    full = f"{path}.{name}"
    kind = (after or before or {}).get("kind")
    if kind == "relationship":
        old_t = (before or {}).get("targets", [])
        new_t = (after or {}).get("targets", [])
        if name.startswith("material:binding"):
            if before is None or not old_t:
                text = f"You assigned the material {', '.join(new_t) or 'nothing'} to {path}."
            elif after is None or not new_t:
                text = f"You removed the material binding from {path} (was {', '.join(old_t)})."
            else:
                text = f"You changed the material on {path} from {', '.join(old_t)} to {', '.join(new_t)}."
        elif before is None:
            text = f"You added the relationship {name} on {path} pointing at {', '.join(new_t) or 'nothing'}."
        elif after is None:
            text = f"You removed the relationship {name} from {path}."
        else:
            text = (f"You changed {name} on {path} from {', '.join(old_t) or 'nothing'} to "
                    f"{', '.join(new_t) or 'nothing'}.")
        out.append({"kind": "relationship_changed", "path": full, "old": old_t, "new": new_t, "text": text})
        return
    if before is None:
        value = after.get("default")
        if not after.get("has_default") and after.get("samples"):
            text = f"You animated {name} on {path} ({len(after['samples'])} keyframes)."
        elif not after.get("has_default"):
            text = f"You added the attribute {name} ({after.get('type')}) on {path}."
        else:
            text = _value_change(path, name, None, value)
        out.append({"kind": "attribute_added", "path": full, "new": _fmt(value), "text": text})
        return
    if after is None:
        out.append({"kind": "attribute_removed", "path": full, "old": _fmt(before.get("default")),
                    "text": f"You removed the attribute {name} from {path}."})
        return
    if not _same(before.get("default"), after.get("default")) or before.get("has_default") != after.get("has_default"):
        old_v = before.get("default") if before.get("has_default") else None
        new_v = after.get("default") if after.get("has_default") else None
        out.append({"kind": "attribute_changed", "path": full, "old": _fmt(old_v), "new": _fmt(new_v),
                    "text": _value_change(path, name, old_v, new_v)})
    if not _same(before.get("samples"), after.get("samples")):
        old_n, new_n = len(before.get("samples") or {}), len(after.get("samples") or {})
        out.append({"kind": "animation_changed", "path": full, "old": old_n, "new": new_n,
                    "text": f"You changed the animation of {name} on {path} ({old_n} -> {new_n} keyframes)."})
    if not _same(before.get("connections"), after.get("connections")):
        out.append({"kind": "connection_changed", "path": full, "old": before.get("connections"),
                    "new": after.get("connections"),
                    "text": f"You changed what {name} on {path} is connected to "
                            f"({', '.join(before.get('connections') or []) or 'nothing'} -> "
                            f"{', '.join(after.get('connections') or []) or 'nothing'})."})
    if before.get("type") != after.get("type"):
        out.append({"kind": "attribute_type_changed", "path": full, "old": before.get("type"), "new": after.get("type"),
                    "text": f"You changed the type of {name} on {path} from {before.get('type')} to "
                            f"{after.get('type')}."})


# ---------------------------------------------------------------------------------------------------- restore


def _target(ctx: _Scene, rel: str) -> Path:
    """The project file a manifest path restores to; CheckpointError when it would leave the project folder."""
    if not rel or rel.startswith("/") or _DRIVE_RE.match(rel) or "\\" in rel or ".." in rel.split("/"):
        raise CheckpointError(f"Unsafe path in the checkpoint manifest: {rel!r}")
    target = Path(os.path.abspath(ctx.project / Path(*rel.split("/"))))
    if not _within(_norm(target), _norm(ctx.project)):
        raise CheckpointError(f"{rel} would be written outside the project folder.")
    if _within(_norm(target), _norm(ctx.base)):
        raise CheckpointError(f"{rel} points into the checkpoint folder.")
    if target.exists() and _is_link(target):
        raise CheckpointError(f"{rel} is a link; restore never writes through links.")
    if not _within(_real(target), _real(ctx.project)):
        raise CheckpointError(f"{rel} resolves outside the project folder through a link.")
    return target


def restore_checkpoint(
    scene_path: Any,
    checkpoint: Any,
    *,
    dry_run: bool = True,
    max_total_mb: float = DEFAULT_MAX_TOTAL_MB,
    project_dir: Any = None,
    workspace_root: Any = None,
) -> dict[str, Any]:
    """Put the files of a checkpoint back. dry_run=True (the default) only shows the plan.

    A real restore first saves a safety checkpoint of the current files, then refuses if any target changed after
    it (race), then writes each file atomically and checks its sha256. Only manifest files inside the project are
    written; files that are not in the manifest are left alone.
    """
    _require_pxr()
    ctx = _resolve(scene_path, project_dir, workspace_root)
    with _LOCK:
        entry = _find(ctx, checkpoint)
        manifest = entry["manifest"]
        base = {"ok": True, "dry_run": bool(dry_run), "restored": [], **ctx.describe(), "checkpoint": entry["id"],
                "warnings": [APP_RELOAD_WARNING]}
        if manifest.get("scene") not in (None, ctx.rel):
            return {**base, "ok": False, "refused": "scene",
                    "error": f"Checkpoint {entry['id']} belongs to {manifest.get('scene')}, not {ctx.rel}."}
        plan, damaged = [], []
        for record in manifest["files"]:
            rel = str(record.get("path", ""))
            target = _target(ctx, rel)
            copy = entry["folder"] / FILES_DIR / Path(*rel.split("/"))
            if not copy.is_file() or _sha256(copy) != record.get("sha256"):
                damaged.append(rel)
                continue
            if target.is_file():
                action = "unchanged" if _sha256(target) == record["sha256"] else "restore"
            else:
                action = "recreate"
            plan.append({"path": rel, "action": action, "target": target, "copy": copy, "sha256": record["sha256"],
                         "size": record.get("size")})
        if damaged:
            return {**base, "ok": False, "refused": "damaged",
                    "error": f"Checkpoint {entry['id']} is damaged (missing or changed copies: {', '.join(damaged)}). "
                             "Nothing was restored."}
        current = {item["path"] for item in _collect(ctx, DEFAULT_MAX_FILE_MB * 1024 * 1024)["files"]}
        in_manifest = {item["path"] for item in plan}
        base["plan"] = [{"path": p["path"], "action": p["action"], "size": p["size"]} for p in plan]
        base["left_alone"] = sorted(current - in_manifest)
        writes = [p for p in plan if p["action"] != "unchanged"]
        if not writes:
            return {**base, "message": f"The files already match checkpoint {entry['id']}; nothing to restore."}
        if dry_run:
            return {**base, "message": f"Dry run: would restore {len(writes)} file(s) from {entry['id']} after saving "
                                       "a safety checkpoint of the current files. Nothing was written; pass "
                                       "dry_run=False to restore."}
        existing = tuple(p["path"] for p in writes if p["target"].is_file())
        safety = _create(ctx, f"auto safety before restoring {entry['id']}", dry_run=False,
                         max_total_mb=max(float(max_total_mb), DEFAULT_MAX_TOTAL_MB), max_file_mb=1e6, kind="safety",
                         extra_rels=existing, restoring=entry["id"])
        if not safety.get("created"):
            return {**base, "ok": False, "refused": "safety",
                    "error": "Could not save the safety checkpoint, so nothing was restored: "
                             + str(safety.get("error"))}
        base["safety_checkpoint"] = safety["id"]
        recorded = {r["path"]: (r["source_mtime_ns"], r["size"]) for r in safety["manifest"]["files"]}

        def raced(item: dict[str, Any]) -> bool:
            now = _stat(item["target"])
            return now != recorded.get(item["path"]) if (now is not None or item["path"] in recorded) else False

        racing = [p["path"] for p in writes if raced(p)]
        if racing:
            return {**base, "ok": False, "refused": "race",
                    "error": f"{', '.join(racing)} changed after the safety checkpoint {safety['id']} was taken "
                             "(maybe an app saved just now). Nothing was restored; try again."}
        restored = []
        for item in writes:
            if raced(item):
                return {**base, "ok": False, "refused": "race", "restored": restored,
                        "error": f"{item['path']} changed during the restore; stopped. Restored so far: "
                                 f"{', '.join(restored) or 'nothing'}. Checkpoint {safety['id']} holds the files from "
                                 "before the restore."}
            sha, _size = _copy_verified(item["copy"], item["target"])
            if sha != item["sha256"] or _sha256(item["target"]) != item["sha256"]:
                return {**base, "ok": False, "refused": "verify", "restored": restored,
                        "error": f"{item['path']} did not verify after copying. Checkpoint {safety['id']} holds the "
                                 "files from before the restore."}
            restored.append(item["path"])
        base["restored"] = restored
        base["warnings"] = [APP_RELOAD_WARNING, f"To undo this restore, restore checkpoint {safety['id']}."]
        base["message"] = (f"Restored {len(restored)} file(s) from {entry['id']}. The files from before are in safety "
                           f"checkpoint {safety['id']}.")
        return base


# ------------------------------------------------------------------------------------------------------ prune


def prune(
    scene_path: Any,
    keep: int = DEFAULT_KEEP,
    *,
    dry_run: bool = True,
    project_dir: Any = None,
    workspace_root: Any = None,
) -> dict[str, Any]:
    """Delete all but the newest `keep` checkpoints of this scene (dry_run=True only lists them)."""
    try:
        keep = int(keep)
    except (TypeError, ValueError) as exc:
        raise CheckpointError("keep must be a whole number of at least 1.") from exc
    if keep < 1:
        raise CheckpointError("keep must be at least 1, so the newest checkpoint is never deleted.")
    ctx = _resolve(scene_path, project_dir, workspace_root)
    with _LOCK:
        entries = _entries(ctx)
        doomed = entries[:-keep] if len(entries) > keep else []
        kept = entries[len(doomed):]
        freed = 0
        for entry in doomed:
            for item in entry["folder"].rglob("*"):
                if item.is_file() and not _is_link(item):
                    freed += item.stat().st_size
        result = {"ok": True, "dry_run": bool(dry_run), **ctx.describe(), "keep": keep,
                  "kept": [e["id"] for e in kept], "deleted": [], "would_delete": [e["id"] for e in doomed],
                  "freed_bytes": freed}
        if dry_run or not doomed:
            result["message"] = (f"Dry run: would delete {len(doomed)} checkpoint(s), {_mb(freed)} MB."
                                 if doomed else f"Nothing to prune: {len(entries)} checkpoint(s), keep={keep}.")
            return result
        store_real = _real(ctx.store)
        for entry in doomed:
            folder = entry["folder"]
            if _is_link(folder) or not _within(_real(folder), store_real) or _norm(folder) == _norm(ctx.store):
                raise CheckpointError(f"Refusing to delete {_fwd(folder)}: it is not a plain checkpoint folder.")
            shutil.rmtree(folder)
            result["deleted"].append(entry["id"])
        result["would_delete"] = []
        result["message"] = f"Deleted {len(result['deleted'])} old checkpoint(s), freed {_mb(freed)} MB."
        return result


__all__ = [
    "APP_RELOAD_WARNING",
    "CheckpointError",
    "ENV_BASE",
    "FOLDER_NAME",
    "UNSAVED_WARNING",
    "create_checkpoint",
    "diff",
    "layer_changes",
    "list_checkpoints",
    "prune",
    "restore_checkpoint",
]
