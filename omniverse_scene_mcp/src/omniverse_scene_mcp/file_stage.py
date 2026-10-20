"""Resolve scene files inside the workspace and open them strictly read-only."""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

try:
    from pxr import Usd
except ImportError as exc:  # pragma: no cover - depends on the local environment
    Usd = None  # type: ignore[assignment]
    _PXR_IMPORT_ERROR: ImportError | None = exc
else:
    _PXR_IMPORT_ERROR = None

USD_EXTENSIONS = frozenset({".usd", ".usda", ".usdc", ".usdz"})
_DRIVE = re.compile(r"^/?[A-Za-z]:[/\\]")


class SceneFileError(ValueError):
    """Raised when a scene file is missing, not USD, outside the workspace, or unreadable."""


class ReadOnlyViolation(AssertionError):
    """Raised when a read-only session dirtied a layer or changed a file on disk."""


def default_workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def resolve_scene_file(
    path: str | os.PathLike[str] | None = None,
    stage_name: str | None = None,
    workspace_root: str | os.PathLike[str] | None = None,
    allow_outside_workspace: bool = False,
) -> str:
    """Return the absolute path of an existing USD file, accepting plain paths or 'file:/C:/...' stage names."""
    raw = os.fspath(path) if path is not None else stage_name
    if raw is None or not str(raw).strip():
        raise SceneFileError("Provide a scene file path or a stage name.")
    text = str(raw).strip().split(":SDF_FORMAT_ARGS:", 1)[0]
    lowered = text.lower()
    if lowered.startswith("anon:"):
        raise SceneFileError(f"Stage {text!r} is an anonymous in-memory layer with no file; inspect it in live mode.")
    if re.match(r"^[a-z][a-z0-9+.-]+://", lowered) and not lowered.startswith("file:"):
        raise SceneFileError(f"Only local files are supported in file mode, not {text!r}.")
    if lowered.startswith("file:"):
        text = _file_uri_to_path(text)

    root = Path(workspace_root if workspace_root is not None else default_workspace_root()).resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.suffix.lower() not in USD_EXTENSIONS:
        raise SceneFileError(f"Not a USD file (expected {', '.join(sorted(USD_EXTENSIONS))}): {candidate}")
    if not allow_outside_workspace:
        # Lexical check first (os.path.abspath does no I/O): never touch UNC/SMB shares or reveal whether
        # files outside the workspace exist. The resolved check below still catches links that leave it.
        lexical = Path(os.path.abspath(candidate))
        unresolved_root = Path(os.path.abspath(workspace_root if workspace_root is not None else default_workspace_root()))
        unc = lexical.drive.startswith("\\\\") and not (
            root.drive.startswith("\\\\") or unresolved_root.drive.startswith("\\\\")
        )
        if unc or not (_is_within(lexical, root) or _is_within(lexical, unresolved_root)):
            raise SceneFileError(f"Scene file is outside the workspace {root}: {lexical}")
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise SceneFileError(f"Scene file does not exist: {candidate}") from exc
    if not resolved.is_file():
        raise SceneFileError(f"Scene path is not a file: {resolved}")
    if resolved.suffix.lower() not in USD_EXTENSIONS:
        raise SceneFileError(f"Not a USD file after resolving links: {resolved}")
    if not allow_outside_workspace and not _is_within(resolved, root):
        raise SceneFileError(f"Scene file is outside the workspace {root}: {resolved}")
    return str(resolved)


def open_readonly(path: str | os.PathLike[str]) -> Any:
    """Open a stage with Usd.Stage.Open (all payloads loaded). Callers must never Save or Export it."""
    if Usd is None:
        raise ImportError("USD Python bindings (pxr) are required to open scene files.") from _PXR_IMPORT_ERROR
    file_path = os.fspath(path)
    try:
        stage = Usd.Stage.Open(file_path, Usd.Stage.LoadAll)
    except Exception as exc:
        raise SceneFileError(f"Could not open USD stage {file_path}: {exc}") from exc
    if stage is None:
        raise SceneFileError(f"Could not open USD stage {file_path}")
    return stage


@contextlib.contextmanager
def readonly_stage(path: str | os.PathLike[str]) -> Iterator[Any]:
    """Yield a read-only stage; on exit raise ReadOnlyViolation if a layer became dirty or a file changed."""
    file_path = os.fspath(path)
    root_state = _file_state(file_path)
    stage = open_readonly(file_path)
    states = {real: _file_state(real) for real in _layer_files(stage)}
    states[_normalize(file_path)] = root_state
    already_dirty = {layer.identifier for layer in _stage_layers(stage) if layer.dirty}

    yield stage

    newly_dirty = sorted(
        layer.identifier for layer in _stage_layers(stage) if layer.dirty and layer.identifier not in already_dirty
    )
    changed = sorted(real for real, state in states.items() if _file_state(real) != state)
    if newly_dirty or changed:
        raise ReadOnlyViolation(f"Read-only session modified the scene: dirty layers={newly_dirty}, changed files={changed}")


def _file_uri_to_path(uri: str) -> str:
    rest = unquote(uri[5:])
    if rest.startswith("//"):
        rest = rest[2:]
        if not _DRIVE.match(rest) and not rest.startswith("/"):
            host, _, tail = rest.partition("/")
            rest = "/" + tail if host.lower() in ("", "localhost") else f"//{host}/{tail}"
    if _DRIVE.match(rest) and rest.startswith("/"):
        rest = rest[1:]
    return rest


def _is_within(path: Path, root: Path) -> bool:
    child, parent = os.path.normcase(str(path)), os.path.normcase(str(root))
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _normalize(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _file_state(path: str) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _stage_layers(stage: Any) -> list[Any]:
    layers = list(stage.GetUsedLayers(True))
    for extra in (stage.GetRootLayer(), stage.GetSessionLayer()):
        if extra is not None and all(extra.identifier != layer.identifier for layer in layers):
            layers.append(extra)
    return layers


def _layer_files(stage: Any) -> set[str]:
    files = set()
    for layer in _stage_layers(stage):
        real = layer.realPath
        if layer.anonymous or not real:
            continue
        files.add(_normalize(real.split("[", 1)[0]))
    return files
