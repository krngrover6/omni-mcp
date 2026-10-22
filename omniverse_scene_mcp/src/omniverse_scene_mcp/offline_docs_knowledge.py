"""Offline Kit, Isaac Sim and OmniUI knowledge from the extension docs installed with Isaac Sim, searched with BM25.

The install is only ever read: files are opened read-only, nothing there is imported, executed or written (so no
``__pycache__`` appears inside it). Each extension contributes its ``config/extension.toml`` (title, description,
keywords, version, deprecation warning) and the Markdown/reStructuredText/example files under its ``docs`` folder;
OmniUI extensions also contribute the class docstrings of their ``.pyi`` API stubs (``omni.ui.VStack`` ...).
"""

from __future__ import annotations

from array import array
import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import difflib
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterable, Iterator, Sequence

try:  # Python 3.11+ (Kit 110 / Isaac Sim 6.1 ship 3.12); a small regex reader covers older interpreters.
    import tomllib as _toml
except ImportError:  # pragma: no cover - depends on the interpreter
    _toml = None  # type: ignore[assignment]

DOMAINS = ("kit", "isaac_sim", "omni_ui")
DEFAULT_INSTALL_DIR = "isaacsim-6.1.0"
DEFAULT_ISAAC_SIM_VERSION = "6.1.0"
SNIPPET_CHARS = 600
ENV_ROOT = "OMNIVERSE_KIT_DOCS_ROOT"

_DOMAIN_ALIASES = {
    "kit": "kit",
    "omni.kit": "kit",
    "isaac_sim": "isaac_sim",
    "isaac": "isaac_sim",
    "isaacsim": "isaac_sim",
    "isaac-sim": "isaac_sim",
    "isaac sim": "isaac_sim",
    "omni_ui": "omni_ui",
    "omniui": "omni_ui",
    "omni.ui": "omni_ui",
    "omni-ui": "omni_ui",
    "ui": "omni_ui",
}
_DOMAIN_LABEL = {"kit": "Kit", "isaac_sim": "Isaac Sim", "omni_ui": "OmniUI"}
# The isaac_sim domain's own name words, dropped from its search terms (see OfflineDocsKnowledge.search).
_ISAAC_DOMAIN_TERMS = frozenset({"isaac", "sim", "isaacsim"})
# Extension folders inside an Isaac Sim install, in priority order for duplicate names at the same version.
_CONTAINERS = (("exts",), ("extsInternal",), ("extsDeprecated",), ("kit", "extscore"), ("kit", "exts"), ("extscache",))
_DOC_FORMATS = {".md": "md", ".rst": "rst", ".py": "py"}
_DOC_BYTES = 160_000  # per Markdown/RST page; only third-party notices (skipped anyway) are bigger
_CHANGELOG_BYTES = 8_000  # Kit changelogs are newest-first, so the head holds the installed version's notes
_EXAMPLE_BYTES = 32_000
_STUB_BYTES = 1_000_000
_TOML_BYTES = 256_000
_MAX_DOC_FILES = 80
_MAX_STUBS = 4
_MAX_DEPTH = 5
_CHUNK_CHARS = 2400
_CONTENT_CHARS = 9000
_STUB_DOC_CHARS = 1500
_LIST_LIMIT = 500
_INFO_LIST_CAP = 40
_PER_FILE = 2
_PER_EXTENSION = 3
_SKIP_DOC = re.compile(r"third[_ -]?party|licen[cs]e|notices|^terms\b", re.I)
_SKIP_DIRS = frozenset({"_build", "_static", "_templates", ".git", "node_modules", "__pycache__"})
_STUB_SKIP_DIRS = frozenset(
    {"tests", "test", "docs", "data", "bin", "config", "PACKAGE-LICENSES", "site-packages", "__pycache__"}
)
_STUB_DOMAINS = frozenset({"omni_ui"})
_KIND_WEIGHT = {"doc": 1.0, "api": 1.0, "extension": 0.9, "example": 0.85, "changelog": 0.55}
_DEPRECATED_WEIGHT = 0.9
_TITLE_NAME_BOOST = 1.5
_FOLDER_VERSION = re.compile(r"^(.+?)-(\d+\.\d+[\w.+\-]*)$")
_VERSION_LINE = re.compile(r"^\s*(\d+\.\d+\.\d+)")
_INFO_FILLER = frozenset(
    "extension extensions ext info information detail details docs doc documentation version versions describe "
    "tell overview summary installed install".split()
)


# --- shared text helpers: reuse the USD offline index when importable, else a small stdlib copy ------------------


class _LocalBM25:
    """Minimal BM25 with the same interface as offline_knowledge._BM25Index (fallback only)."""

    def __init__(self, k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs: list[dict[str, Any]] = []
        self._lengths = array("I")
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._avg = 1.0

    def add(self, doc: dict[str, Any], tokens: list[str]) -> None:
        if not tokens:
            return
        doc_id = len(self.docs)
        self.docs.append(doc)
        self._lengths.append(len(tokens))
        for term, tf in Counter(tokens).items():
            self._postings[term].append((doc_id, tf))

    def finish(self) -> None:
        self._avg = (sum(self._lengths) / len(self._lengths)) if self._lengths else 1.0

    def scores(self, terms: Iterable[str]) -> dict[int, float]:
        total = len(self.docs)
        out: dict[int, float] = defaultdict(float)
        for term in set(terms):
            postings = self._postings.get(term)
            if not postings:
                continue
            idf = math.log(1 + (total - len(postings) + 0.5) / (len(postings) + 0.5))
            for doc_id, tf in postings:
                norm = 1 - self.b + self.b * self._lengths[doc_id] / self._avg
                out[doc_id] += idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        return out


_LOCAL_WORD = re.compile(r"[A-Za-z0-9]+")
_LOCAL_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_LOCAL_STOP = frozenset("a an and are as at be by can do does for from how i in is it of on or the this to what with".split())


def _local_tokenize(text: str, cache: dict[str, tuple[str, ...]] | None = None) -> list[str]:
    out: list[str] = []
    for word in _LOCAL_WORD.findall(text):
        lowered = word.lower()
        if len(lowered) < 2 or lowered in _LOCAL_STOP:
            continue
        out.append(lowered)
        if not (word.islower() or word.isupper() or word.istitle()):
            out.extend(p.lower() for p in _LOCAL_CAMEL.findall(word) if len(p) > 1 and p.lower() != lowered)
    return out


def _local_clean(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\{[A-Za-z:_-]+\}`([^`<]*?)\s*(?:<[^>`]*>)?`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return re.sub(r"</?[A-Za-z][^>]*>", "", text)


def _local_flat(text: str) -> str:
    text = re.sub(r"(?m)^[ \t]*(?:#{1,6}|>|`{3,}[^\n]*|~{3,}[^\n]*)[ \t]?", "", _local_clean(text))
    return re.sub(r"\s+", " ", text.replace("**", "")).strip()


def _local_trim(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _local_snippet(text: str, terms: set[str], limit: int = SNIPPET_CHARS) -> str:
    flat = _local_flat(text)
    if len(flat) <= limit:
        return flat
    lowered = flat.lower()
    hits = [pos for term in terms if (pos := lowered.find(term)) >= 0]
    start = max(0, min(hits) - 60) if hits else 0
    prefix = "..." if start else ""
    return prefix + _local_trim(flat[start:], limit - len(prefix))


def _local_workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


try:
    from .offline_knowledge import (
        _BM25Index as _Index,
        _clean,
        _flat,
        _snippet,
        _tokenize,
        _trim,
        workspace_root,
    )

    HELPERS = "offline_knowledge"
except ImportError:  # pragma: no cover - only if offline_knowledge drops these helpers
    _Index = _LocalBM25  # type: ignore[assignment,misc]
    _clean, _flat, _snippet = _local_clean, _local_flat, _local_snippet
    _tokenize, _trim, workspace_root = _local_tokenize, _local_trim, _local_workspace_root
    HELPERS = "local"


# --- domains, roots and reading -------------------------------------------------------------------------------------


def normalize_domain(domain: str) -> str:
    """'isaac', 'isaacsim', 'omni.ui' ... -> one of DOMAINS; ValueError otherwise."""
    key = str(domain or "").strip().lower()
    if key not in _DOMAIN_ALIASES:
        raise ValueError(f"Unknown docs domain {domain!r}; expected one of {', '.join(DOMAINS)}.")
    return _DOMAIN_ALIASES[key]


def classify_extension(name: str) -> str:
    """isaacsim.* / omni.isaac.* -> isaac_sim; omni.ui* / omni.kit.widget* -> omni_ui; everything else -> kit."""
    lowered = name.strip().lower()
    if lowered.startswith(("isaacsim.", "omni.isaac.")) or lowered == "isaacsim":
        return "isaac_sim"
    if lowered.startswith(("omni.ui", "omni.kit.widget")):
        return "omni_ui"
    return "kit"


def default_install_root() -> Path:
    return workspace_root() / DEFAULT_INSTALL_DIR


def _candidate_roots(roots: str | Path | Iterable[str | Path] | None) -> list[Path]:
    if roots is None:
        configured = os.environ.get(ENV_ROOT, "").strip()
        if configured:
            return [Path(part) for part in configured.split(os.pathsep) if part.strip()]
        return [default_install_root()]
    if isinstance(roots, (str, Path)):
        return [Path(roots)]
    return [Path(root) for root in roots]


def resolve_roots(roots: str | Path | Iterable[str | Path] | None = None) -> tuple[list[Path], Path | None]:
    """Expand install roots (folders holding exts/extscache/...) into extension containers, in priority order.

    A path without any known sub-folder is used as a container itself (a folder whose children are extensions).
    Returns (containers, first install root or None).
    """
    containers: list[Path] = []
    install_root: Path | None = None
    for root in _candidate_roots(roots):
        found = [root.joinpath(*parts) for parts in _CONTAINERS if root.joinpath(*parts).is_dir()]
        if found:
            containers.extend(found)
            install_root = install_root or root
        elif root.is_dir():
            containers.append(root)
    return list(dict.fromkeys(containers)), install_root


def _read_text(path: Path, limit: int) -> tuple[str, bool] | None:
    """First ``limit`` bytes of a text file (cut at a line end), or None for unreadable or binary files."""
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    truncated = len(data) > limit
    if truncated:
        data = data[:limit]
        cut = data.rfind(b"\n")
        if cut > 0:
            data = data[:cut]
    return data.decode("utf-8", errors="replace").lstrip("﻿"), truncated


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", re.split(r"[+\-]", version, 1)[0])[:4])


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_root().resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


# --- extension.toml ----------------------------------------------------------------------------------------------------

_TOML_TABLE = re.compile(r"^\[(\[)?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")
_TOML_KEY = re.compile(r"""^("?)([A-Za-z0-9_.\-]+)\1\s*=\s*(.*)$""")
_TOML_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'')


def _toml_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith("["):
        return [a if a else b for a, b in _TOML_STRING.findall(raw)]
    match = _TOML_STRING.match(raw)
    if match:
        value = match.group(1) if match.group(1) is not None and raw.startswith('"') else match.group(2)
        return (value or "").replace('\\"', '"')
    return raw.split("#", 1)[0].strip()


def _parse_toml_fallback(text: str) -> dict[str, Any]:
    """Regex reader for the extension.toml keys used here: [package], [deprecation], [dependencies], [[python.module]]."""
    data: dict[str, Any] = {"package": {}, "deprecation": {}, "dependencies": {}, "python": {"module": []}}
    table: dict[str, Any] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        header = _TOML_TABLE.match(stripped)
        if header:
            name = header.group(2).strip().strip('"')
            if header.group(1) and name == "python.module":
                table = {}
                data["python"]["module"].append(table)
            elif not header.group(1) and name in ("package", "deprecation", "dependencies"):
                table = data[name]
            else:
                table = None
            continue
        if table is None:
            continue
        key = _TOML_KEY.match(stripped)
        if key:
            table[key.group(2)] = _toml_value(key.group(3))
    return data


def _parse_toml(text: str) -> dict[str, Any]:
    if _toml is not None:
        try:
            return _toml.loads(text)
        except (ValueError, TypeError):  # tomllib.TOMLDecodeError subclasses ValueError
            pass
    return _parse_toml_fallback(text)


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, (str, int, float)) and str(item).strip())


# --- catalog ----------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Extension:
    name: str
    version: str
    domain: str
    path: Path
    display: str
    title: str = ""
    description: str = ""
    category: str = ""
    keywords: tuple[str, ...] = ()
    deprecation: str = ""
    python_modules: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    toml_path: Path | None = None
    docs: tuple[Path, ...] = ()
    readme: Path | None = None
    changelog: Path | None = None
    other_locations: list[str] = field(default_factory=list)

    def rel(self, path: Path) -> str:
        try:
            return f"{self.display}/{path.relative_to(self.path).as_posix()}"
        except ValueError:
            return _display(path)


def _walk_files(top: Path, skip_dirs: frozenset[str], max_depth: int = _MAX_DEPTH) -> Iterator[Path]:
    base_depth = len(top.parts)
    for current, dirs, files in os.walk(top, followlinks=False):
        here = Path(current)
        if len(here.parts) - base_depth >= max_depth:
            dirs[:] = []
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs and "prebundle" not in d.lower())
        for name in sorted(files):
            yield here / name


def _doc_files(docs_dir: Path) -> tuple[Path, ...]:
    if not docs_dir.is_dir():
        return ()
    found = [
        path for path in _walk_files(docs_dir, _SKIP_DIRS)
        if path.suffix.lower() in _DOC_FORMATS and not _SKIP_DOC.search(path.name)
    ]
    order = {"overview": 0, "readme": 1, "index": 2, "usage_python": 3, "changelog": 9}
    found.sort(key=lambda p: (order.get(p.stem.lower(), 5), len(p.parts), p.as_posix().lower()))
    return tuple(found[:_MAX_DOC_FILES])


def _load_extension(folder: Path, container_display: str) -> _Extension | None:
    toml_path = folder / "config" / "extension.toml"
    has_toml = toml_path.is_file()
    if not has_toml and not (folder / "docs").is_dir():
        return None
    versioned = _FOLDER_VERSION.match(folder.name)  # extscache folders are "<name>-<version>+<build>"
    name, folder_version = versioned.groups() if versioned else (folder.name, "")
    data: dict[str, Any] = {}
    if has_toml:
        read = _read_text(toml_path, _TOML_BYTES)
        if read is not None:
            data = _parse_toml(read[0])
    package = data.get("package") if isinstance(data.get("package"), dict) else {}
    deprecation = data.get("deprecation") if isinstance(data.get("deprecation"), dict) else {}
    dependencies = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
    python = data.get("python") if isinstance(data.get("python"), dict) else {}
    modules = python.get("module") if isinstance(python.get("module"), list) else []
    version = _as_text(package.get("version")) or re.split(r"\+", folder_version, 1)[0]
    ext = _Extension(
        name=name,
        version=version,
        domain=classify_extension(name),
        path=folder,
        display=f"{container_display}/{folder.name}",
        title=_as_text(package.get("title")),
        description=_as_text(package.get("description")),
        category=_as_text(package.get("category")),
        keywords=_as_names(package.get("keywords")),
        deprecation=_as_text(deprecation.get("warning")) or ("Deprecated." if deprecation else ""),
        python_modules=tuple(str(m.get("name")) for m in modules if isinstance(m, dict) and m.get("name"))[:20],
        dependencies=tuple(str(key) for key in dependencies),
        toml_path=toml_path if has_toml else None,
        docs=_doc_files(folder / "docs"),
    )
    for attr, key, stem in (("readme", "readme", "overview"), ("changelog", "changelog", "changelog")):
        declared = _as_text(package.get(key))
        path = folder / declared if declared else None
        if path is None or not path.is_file():
            path = next((doc for doc in ext.docs if doc.stem.lower() == stem), None)
            if path is None and attr == "readme":
                path = next((doc for doc in ext.docs if doc.stem.lower() == "readme"), None)
        setattr(ext, attr, path)
    return ext


class _Catalog:
    """Every extension found in the containers, newest version first for duplicate names."""

    def __init__(self, containers: Sequence[Path]) -> None:
        start = time.perf_counter()
        self.extensions: dict[str, _Extension] = {}
        self.folder_count = 0
        for container in containers:
            container_display = _display(container)
            try:
                with os.scandir(container) as entries:
                    folders = sorted(Path(e.path) for e in entries if e.is_dir(follow_symlinks=False))
            except OSError:
                continue
            for folder in folders:
                ext = _load_extension(folder, container_display)
                if ext is None:
                    continue
                self.folder_count += 1
                existing = self.extensions.get(ext.name)
                if existing is None:
                    self.extensions[ext.name] = ext
                elif _version_key(ext.version) > _version_key(existing.version):
                    ext.other_locations = [*existing.other_locations, f"{existing.display} ({existing.version})"]
                    self.extensions[ext.name] = ext
                else:
                    existing.other_locations.append(f"{ext.display} ({ext.version})")
        self._lower = {name.lower(): name for name in self.extensions}
        self.build_seconds = time.perf_counter() - start

    def in_domain(self, domain: str) -> list[_Extension]:
        return [ext for name, ext in sorted(self.extensions.items()) if ext.domain == domain]

    def resolve(self, name: str) -> str | None:
        """Exact, case-insensitive, versioned folder name, or the longest dotted prefix (module path -> extension)."""
        cleaned = str(name).strip().strip("`'\"()[],;:").rstrip(".*")
        match = _FOLDER_VERSION.match(cleaned)
        if match:
            cleaned = match.group(1)
        if cleaned in self.extensions:
            return cleaned
        parts = cleaned.lower().split(".")
        for end in range(len(parts), 1, -1):
            found = self._lower.get(".".join(parts[:end]))
            if found:
                return found
        return None

    def suggest(self, name: str, limit: int = 8) -> list[str]:
        lowered = str(name).strip().lower().rstrip(".*")
        candidates = [lowered]
        if lowered.startswith("omni.isaac."):
            candidates.append("isaacsim." + lowered[len("omni.isaac."):])
        names = sorted(self.extensions)
        out: list[str] = []
        for candidate in candidates:
            out.extend(n for n in names if n.lower().startswith(candidate + "."))
        for candidate in candidates:
            close = difflib.get_close_matches(candidate, list(self._lower), n=limit, cutoff=0.6)
            out.extend(self._lower[c] for c in close)
        return list(dict.fromkeys(out))[:limit]


# --- splitting docs into sections --------------------------------------------------------------------------------------

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_OPEN = re.compile(r"^(`{3,}|~{3,})(.*)$")
_RST_ADORN = re.compile(r"""^([=\-~^"'`*#+_.:<>!$%&,;?@|/\\])\1{2,}\s*$""")
_RST_DIRECTIVE = re.compile(r"^(\s*)\.\.\s+([A-Za-z0-9_:-]+)::\s*(.*)$")
_RST_OPTION = re.compile(r"^\s+:[A-Za-z][\w -]*:(?:\s.*)?$")
_RST_ROLE = re.compile(r":(?:[A-Za-z0-9_-]+:)*[A-Za-z0-9_-]+:`~?([^`<]*?)\s*(?:<([^>`]*)>)?`")
_RST_KEEP_ARG = frozenset(
    "note warning tip important attention caution hint danger error seealso deprecated versionadded versionchanged "
    "rubric topic admonition title".split()
)
_RST_DROP_ARG = frozenset(
    "code-block code sourcecode literalinclude image figure toctree include raw only highlight contents autosummary "
    "currentmodule module py:module py:currentmodule tabularcolumns csv-table list-table table mermaid graphviz".split()
)


_RST_LITERAL = re.compile(r"``([^`]+)``")
_RST_NAMED_LINK = re.compile(r"`([^`<]+?)\s*<[^>]+>`_{1,2}")
_RST_LINK = re.compile(r"`([^`]+)`_{1,2}")
_RST_TILDE = re.compile(r"(?<![\w.])~(?=[A-Za-z_])")
_RST_COMMENT = re.compile(r"^\s*\.\.(?:\s|$)")
_ADORN_CHARS = frozenset("=-~^\"'`*#+_.:<>!$%&,;?@|/\\")


def _rst_inline(text: str) -> str:
    if "`" in text:
        text = _RST_ROLE.sub(lambda m: m.group(1) or m.group(2) or "", text)
        text = _RST_NAMED_LINK.sub(r"\1", _RST_LITERAL.sub(r"\1", text))
        text = _RST_LINK.sub(r"\1", text)
    return _RST_TILDE.sub("", text) if "~" in text else text


def _is_adorn(line: str) -> bool:
    return line[:1] in _ADORN_CHARS and bool(_RST_ADORN.match(line))


def _clean_rst(text: str) -> str:
    """Drop directive syntax, options, comments, targets and underlines; keep autodoc object names and prose."""
    out: list[str] = []
    in_options = False
    for line in text.splitlines():
        if _is_adorn(line):
            continue
        if ".." in line:
            directive = _RST_DIRECTIVE.match(line)
            if directive:
                indent, name, arg = directive.group(1), directive.group(2).lower(), directive.group(3).strip()
                in_options = True
                if arg and (name.startswith(("auto", "py:")) or name in _RST_KEEP_ARG or name not in _RST_DROP_ARG):
                    out.append(indent + arg)
                continue
            if _RST_COMMENT.match(line):
                in_options = False
                continue  # comments, link targets (.. _label:) and substitution definitions
        if in_options and ":" in line and _RST_OPTION.match(line):
            continue
        in_options = False
        out.append(line)
    return _rst_inline("\n".join(out))


def _md_heading_map(lines: list[str]) -> tuple[dict[int, tuple[int, str]], set[int]]:
    heads: dict[int, tuple[int, str]] = {}
    skip: set[int] = set()
    if lines and lines[0].strip() == "---":
        for end in range(1, min(len(lines), 200)):
            if lines[end].strip() == "---":
                skip.update(range(end + 1))
                break
    fence = ""
    for index, line in enumerate(lines):
        first = line[:1]
        if first not in ("#", "`", "~", " ", "\t") or index in skip:
            continue  # cheap pre-filter: only headings and (possibly indented) fences matter here
        stripped = line.strip()
        if fence:
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
            continue
        if stripped[:1] in ("`", "~"):
            opened = _FENCE_OPEN.match(stripped)
            if opened:
                fence = opened.group(1)
                continue
        if first == "#":
            heading = _MD_HEADING.match(line)
            if heading:
                heads[index] = (len(heading.group(1)), _flat(heading.group(2)))
    return heads, skip


def _rst_heading_map(lines: list[str]) -> tuple[dict[int, tuple[int, str]], set[int]]:
    heads: dict[int, tuple[int, str]] = {}
    skip: set[int] = set()
    levels: dict[tuple[str, bool], int] = {}
    index = 0
    while index < len(lines) - 1:
        text, under = lines[index], lines[index + 1]
        stripped = text.strip()
        if (
            stripped
            and _is_adorn(under)
            and not text[0].isspace()
            and not stripped.startswith("..")
            and not _is_adorn(text)
            and len(under.strip()) >= max(3, len(stripped) - 2)
        ):
            char = under.strip()[0]
            over = index > 0 and _is_adorn(lines[index - 1]) and lines[index - 1].strip()[0] == char
            level = levels.setdefault((char, over), len(levels) + 1)
            heads[index] = (level, _flat(_rst_inline(stripped)))
            skip.add(index + 1)
            if over:
                skip.add(index - 1)
            index += 2
            continue
        index += 1
    return heads, skip


def _heading_title(stack: list[tuple[int, str]]) -> str:
    texts = [text for _level, text in stack if text]
    if len(texts) <= 2:
        return " > ".join(texts)
    return f"{texts[0]} > {texts[-2]} > {texts[-1]}"


def _sections(text: str, fmt: str, chunk_chars: int = _CHUNK_CHARS) -> list[tuple[str, int, str]]:
    """Split a page into (heading title, first line number, text) chunks of about ``chunk_chars``."""
    lines = text.splitlines()
    if fmt == "md":
        heads, skip = _md_heading_map(lines)
    elif fmt == "rst":
        heads, skip = _rst_heading_map(lines)
    else:
        heads, skip = {}, set()
    out: list[tuple[str, int, str]] = []
    stack: list[tuple[int, str]] = []
    title = ""
    piece: list[tuple[int, str]] = []
    piece_title = ""
    size = 0

    def emit() -> None:
        nonlocal piece, size
        body, piece, size = piece, [], 0
        while body and not body[0][1].strip():
            body.pop(0)
        while body and not body[-1][1].strip():
            body.pop()
        if not body:
            return
        if sum(len(line.strip()) for index, line in body if index not in heads) > 25:
            out.append((piece_title, body[0][0] + 1, "\n".join(line for _index, line in body)))

    for index, line in enumerate(lines):
        if index in skip:
            continue
        if index in heads:
            emit()
            level, heading = heads[index]
            stack = [item for item in stack if item[0] < level] + [(level, heading)]
            title = _heading_title(stack)
        elif size >= chunk_chars and (not line.strip() or size >= 2 * chunk_chars):
            emit()
        if not piece:
            piece_title = title
        piece.append((index, line))
        size += len(line) + 1
    emit()
    return out


# --- API stubs (.pyi) --------------------------------------------------------------------------------------------------

_STUB_CLASS = re.compile(r"^class\s+([A-Za-z]\w*)\s*(?:\(([^)]*)\))?\s*:")
_STUB_DEF = re.compile(r"^    def\s+([A-Za-z_]\w*)\s*\(")


def _docstring_after(block: list[str], start: int) -> tuple[str, int]:
    """The triple-quoted string starting on the first non-blank line at or after ``start`` (dedented), and its end."""
    index = start
    while index < len(block) and not block[index].strip():
        index += 1
    if index >= len(block):
        return "", index
    first = block[index].strip()
    quote = next((q for q in ('"""', "'''") if first.lstrip("rRuU").startswith(q)), None)
    if quote is None:
        return "", index
    body = first.lstrip("rRuU")[3:]
    if quote in body:
        return body.split(quote, 1)[0].strip(), index + 1
    collected = [body]
    index += 1
    while index < len(block):
        line = block[index]
        if quote in line:
            collected.append(line.split(quote, 1)[0])
            break
        collected.append(line)
        index += 1
    lines = [line.strip() for line in collected]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip(), index + 1


def _stub_classes(text: str) -> list[tuple[str, str, int, str]]:
    """Top-level public classes of a .pyi stub: (name, bases, line number, doc text with __init__ doc and members)."""
    lines = text.splitlines()
    out: list[tuple[str, str, int, str]] = []
    index = 0
    while index < len(lines):
        match = _STUB_CLASS.match(lines[index])
        if not match:
            index += 1
            continue
        end = index + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
            end += 1
        block = lines[index + 1 : end]
        class_doc, _after = _docstring_after(block, 0)
        init_doc = ""
        members: list[str] = []
        for offset, line in enumerate(block):
            definition = _STUB_DEF.match(line)
            if not definition:
                continue
            if definition.group(1) == "__init__" or definition.group(1).startswith("_"):
                if definition.group(1) == "__init__" and not init_doc:
                    init_doc, _after = _docstring_after(block, offset + 1)
                continue
            members.append(definition.group(1))
        name, bases = match.group(1), (match.group(2) or "").strip()
        parts = [f"class {name}({bases})" if bases else f"class {name}"]
        if class_doc:
            parts.append(_trim(class_doc, _STUB_DOC_CHARS))
        if init_doc:
            parts.append(_trim(init_doc, _STUB_DOC_CHARS))
        members = list(dict.fromkeys(members))
        if members:
            parts.append("Members: " + ", ".join(members[:40]))
        out.append((name, bases, index + 1, _trim("\n\n".join(parts), _CHUNK_CHARS)))
        index = end
    return out


def _stub_files(ext: _Extension) -> list[Path]:
    found = [path for path in _walk_files(ext.path, _STUB_SKIP_DIRS, 4) if path.suffix == ".pyi"]
    return found[:_MAX_STUBS]


# --- per-domain index --------------------------------------------------------------------------------------------------


def _kind_of(path: Path) -> str:
    if path.stem.lower().startswith("changelog"):
        return "changelog"
    return "example" if path.suffix.lower() == ".py" else "doc"


class _DomainIndex:
    """BM25 over one domain's extension cards, doc sections and (OmniUI) stub classes."""

    def __init__(self, catalog: _Catalog, domain: str, include_stubs: bool) -> None:
        start = time.perf_counter()
        self.domain = domain
        self.bm25 = _Index()
        self.files = 0
        self.truncated_files = 0
        self.extensions = 0
        cache: dict[str, tuple[str, ...]] = {}
        for ext in catalog.in_domain(domain):
            self.extensions += 1
            self._add_card(ext, cache)
            for path in ext.docs:
                self._add_file(ext, path, cache)
            if include_stubs and domain in _STUB_DOMAINS:
                for path in _stub_files(ext):
                    self._add_stub(ext, path, cache)
        self.bm25.finish()
        self.build_seconds = time.perf_counter() - start

    @staticmethod
    def _doc(ext: _Extension, title: str, kind: str, path: str, line: int, text: str) -> dict[str, Any]:
        return {
            "title": title,
            "extension": ext.name,
            "version": ext.version,
            "kind": kind,
            "path": path,
            "line": line,
            "text": text,
            "deprecation": ext.deprecation,
        }

    def _add_card(self, ext: _Extension, cache: dict[str, tuple[str, ...]]) -> None:
        parts = [f"{ext.name} {ext.version}: {ext.title or ext.name}."]
        if ext.description:
            parts.append(ext.description.rstrip(".") + ".")
        if ext.category:
            parts.append(f"Category: {ext.category}.")
        if ext.keywords:
            parts.append(f"Keywords: {', '.join(ext.keywords)}.")
        if ext.python_modules:
            parts.append(f"Python modules: {', '.join(ext.python_modules)}.")
        if ext.deprecation:
            parts.append(f"Deprecated: {ext.deprecation}")
        text = " ".join(parts)
        path = ext.rel(ext.toml_path) if ext.toml_path else ext.display
        title = f"{ext.name}: {ext.title}" if ext.title else ext.name
        tokens = (
            _tokenize(ext.name, cache) * 3
            + _tokenize(ext.title, cache) * 2
            + _tokenize(" ".join(ext.keywords), cache) * 2
            + _tokenize(f"{ext.description} {' '.join(ext.python_modules)} {ext.deprecation}", cache)
        )
        self.bm25.add(self._doc(ext, title, "extension", path, 1, text), tokens)

    def _add_file(self, ext: _Extension, path: Path, cache: dict[str, tuple[str, ...]]) -> None:
        kind = _kind_of(path)
        fmt = _DOC_FORMATS[path.suffix.lower()]
        limit = {"changelog": _CHANGELOG_BYTES, "example": _EXAMPLE_BYTES}.get(kind, _DOC_BYTES)
        read = _read_text(path, limit)
        if read is None:
            return
        text, truncated = read
        self.files += 1
        self.truncated_files += int(truncated)
        display = ext.rel(path)
        name_tokens = _tokenize(ext.name, cache)
        for heading, line, body in _sections(text, fmt):
            if fmt == "rst":
                body = _clean_rst(body)
            label = heading or (path.stem if fmt != "py" else f"example {path.name}")
            title = _trim(f"{ext.name}: {label}", 160)
            tokens = _tokenize(label, cache) * 2 + name_tokens + _tokenize(_clean(body), cache)
            self.bm25.add(self._doc(ext, title, kind, display, line, body), tokens)

    def _add_stub(self, ext: _Extension, path: Path, cache: dict[str, tuple[str, ...]]) -> None:
        read = _read_text(path, _STUB_BYTES)
        if read is None:
            return
        self.files += 1
        self.truncated_files += int(read[1])
        display = ext.rel(path)
        for name, _bases, line, text in _stub_classes(read[0]):
            qualified = f"{ext.name}.{name}"
            tokens = _tokenize(qualified, cache) * 2 + _tokenize(text, cache)
            self.bm25.add(self._doc(ext, f"{qualified} (class)", "api", display, line, text), tokens)

    @property
    def chunk_count(self) -> int:
        return len(self.bm25.docs)


_LOCK = threading.RLock()
_CATALOGS: dict[tuple[str, ...], _Catalog] = {}
_INDEXES: dict[tuple[tuple[str, ...], str, bool], _DomainIndex] = {}


def clear_cache() -> None:
    """Forget every cached catalog and index (tests, or after reinstalling Isaac Sim)."""
    with _LOCK:
        _CATALOGS.clear()
        _INDEXES.clear()


# --- public knowledge API ------------------------------------------------------------------------------------------------

_DOTTED = re.compile(r"(?<![\w./\\-])([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_*]+)+)")
_FILE_SUFFIX = re.compile(r"\.(?:md|rst|py|pyi|toml|json|usd|usda|usdc|usdz|txt|kit|bat|ps1|sh|png|jpg)$", re.I)
_ISAAC_NAMESPACES = ("isaacsim.", "omni.isaac.")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _identifiers(query: str) -> set[str]:
    """CamelCase API names in a query (VStack, ArticulationController), lowercased."""
    return {w.lower() for w in _IDENTIFIER.findall(query) if len(w) >= 3 and re.search(r"[a-z][A-Z]|^[A-Z][A-Z][a-z]", w)}


class OfflineDocsKnowledge:
    """BM25 search and extension facts for one domain ('kit', 'isaac_sim' or 'omni_ui') of the installed docs.

    ``roots`` are Isaac Sim install folders (holding exts/, extscache/ ...) or folders of extensions; the default is
    $OMNIVERSE_KIT_DOCS_ROOT (os.pathsep-separated) or <workspace>/isaacsim-6.1.0. Catalogs and indexes are built on
    first use and cached per process, shared by every instance with the same roots.
    """

    def __init__(
        self,
        domain: str = "kit",
        roots: str | Path | Iterable[str | Path] | None = None,
        include_stubs: bool = True,
    ) -> None:
        self.domain = normalize_domain(domain)
        self.requested_roots = _candidate_roots(roots)
        self.containers, self.install_root = resolve_roots(self.requested_roots)
        self.include_stubs = include_stubs
        self._key = tuple(str(path) for path in self.containers)
        self._version: str | None = None

    # loading

    @property
    def available(self) -> bool:
        return bool(self.containers)

    @property
    def isaac_sim_version(self) -> str:
        if self._version is None:
            version = ""
            candidates = [self.install_root] if self.install_root else [c.parent for c in self.containers]
            for root in candidates:
                read = _read_text(root / "VERSION", 400) if root else None
                match = _VERSION_LINE.match(read[0]) if read else None
                if match:
                    version = match.group(1)
                    break
            self._version = version or DEFAULT_ISAAC_SIM_VERSION
        return self._version

    def catalog(self) -> _Catalog:
        with _LOCK:
            catalog = _CATALOGS.get(self._key)
            if catalog is None:
                catalog = _CATALOGS[self._key] = _Catalog(self.containers)
            return catalog

    def _get_index(self) -> _DomainIndex:
        key = (self._key, self.domain, self.include_stubs)
        with _LOCK:
            index = _INDEXES.get(key)
            if index is None:
                index = _INDEXES[key] = _DomainIndex(self.catalog(), self.domain, self.include_stubs)
            return index

    def status(self) -> dict[str, Any]:
        with _LOCK:
            catalog = _CATALOGS.get(self._key)
            index = _INDEXES.get((self._key, self.domain, self.include_stubs))
        status: dict[str, Any] = {
            "domain": self.domain,
            "install_root": str(self.install_root) if self.install_root else None,
            "containers": [str(path) for path in self.containers],
            "isaac_sim_version": self.isaac_sim_version,
            "helpers": HELPERS,
            "catalog_loaded": catalog is not None,
            "index_loaded": index is not None,
        }
        if catalog is not None:
            status["extension_folders"] = catalog.folder_count
            status["extensions"] = len(catalog.extensions)
            status["domain_extensions"] = len(catalog.in_domain(self.domain))
            status["catalog_seconds"] = round(catalog.build_seconds, 3)
        if index is not None:
            status.update(
                indexed_files=index.files,
                truncated_files=index.truncated_files,
                chunks=index.chunk_count,
                index_seconds=round(index.build_seconds, 3),
            )
        return status

    # extensions

    def resolve_extension(self, name: str) -> str | None:
        return self.catalog().resolve(name)

    def find_extension_ids(self, query: str) -> tuple[list[str], list[str]]:
        """Installed extensions named in a query (module paths map to their extension), plus unknown dotted ids."""
        catalog = self.catalog()
        resolved: list[str] = []
        unknown: list[str] = []
        for match in _DOTTED.finditer(query):
            token = match.group(1).rstrip(".*")
            if not token or _FILE_SUFFIX.search(token):
                continue
            name = catalog.resolve(token)
            if name:
                if name not in resolved:
                    resolved.append(name)
            elif token.lower().startswith(_ISAAC_NAMESPACES) and token not in unknown:
                unknown.append(token)  # omni.kit.app / carb.settings are real modules, not extensions; stay quiet
        return resolved[:3], unknown[:3]

    def extension_info(self, name: str) -> dict[str, Any]:
        """Facts from extension.toml plus the docs files, for an installed extension (any domain)."""
        catalog = self.catalog()
        resolved = catalog.resolve(name)
        if resolved is None:
            prefix = str(name).strip().rstrip(".*").lower() + "."
            children = sorted(ext for ext in catalog.extensions if ext.lower().startswith(prefix))
            if prefix != "." and children:
                # isaacsim.core.experimental is not one extension but the namespace of several installed ones.
                return {
                    "found": False,
                    "namespace": True,
                    "name": name,
                    "suggestions": children[:8],
                    "count": len(children),
                    "message": (
                        f"{name} is a namespace, not one extension: {len(children)} installed extensions in Isaac Sim "
                        f"{self.isaac_sim_version} start with it."
                    ),
                }
            suggestions = catalog.suggest(name)
            message = f"{name} is not an installed extension in Isaac Sim {self.isaac_sim_version}."
            if str(name).lower().startswith("omni.isaac."):
                message += " Isaac Sim 4.5 renamed the omni.isaac.* extensions to isaacsim.*."
            return {"found": False, "name": name, "suggestions": suggestions, "message": message}
        ext = catalog.extensions[resolved]
        docs = [ext.rel(path) for path in ext.docs if _read_text(path, 2048) is not None]  # skip binaries named .md
        info: dict[str, Any] = {
            "found": True,
            "name": ext.name,
            "version": ext.version,
            "title": ext.title,
            "description": ext.description,
            "keywords": list(ext.keywords),
            "category": ext.category,
            "domain": ext.domain,
            "deprecated": bool(ext.deprecation),
            "deprecation_warning": ext.deprecation or None,
            "python_modules": list(ext.python_modules),
            "dependencies": list(ext.dependencies[:_INFO_LIST_CAP]),
            "dependency_count": len(ext.dependencies),
            "docs": docs[:_INFO_LIST_CAP],
            "docs_count": len(docs),
            "readme": ext.rel(ext.readme) if ext.readme else None,
            "changelog": ext.rel(ext.changelog) if ext.changelog else None,
            "changelog_latest": self._latest_change(ext),
            "path": ext.display,
            "source_path": f"{ext.rel(ext.toml_path)}:1" if ext.toml_path else ext.display,
            "isaac_sim_version": self.isaac_sim_version,
        }
        if ext.other_locations:
            info["other_locations"] = list(ext.other_locations[:5])
        if resolved != str(name).strip():
            info["resolved_from"] = name
        return info

    @staticmethod
    def _latest_change(ext: _Extension) -> dict[str, Any] | None:
        if ext.changelog is None:
            return None
        read = _read_text(ext.changelog, _CHANGELOG_BYTES)
        if read is None:
            return None
        lines = read[0].splitlines()
        heading = re.compile(r"^\s*#{1,3}\s*\[?v?\d+\.\d+")
        start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
        if start is None:
            return None
        end = next((i for i in range(start + 1, len(lines)) if heading.match(lines[i])), len(lines))
        body = "\n".join(lines[start + 1 : end])
        return {
            "heading": _flat(lines[start]),
            "text": _trim(_flat(body), SNIPPET_CHARS),
            "source_path": f"{ext.rel(ext.changelog)}:{start + 1}",
        }

    def list_extensions(self, prefix: str | None = None, limit: int = 50) -> dict[str, Any]:
        """This domain's installed extensions, optionally those whose name starts with ``prefix``."""
        limit = max(1, min(int(limit), _LIST_LIMIT))
        wanted = (prefix or "").strip().lower().rstrip(".*")
        items = [
            {"name": ext.name, "version": ext.version, "title": ext.title, "deprecated": bool(ext.deprecation)}
            for ext in self.catalog().in_domain(self.domain)
            if ext.name.lower().startswith(wanted)
        ]
        return {
            "domain": self.domain,
            "prefix": prefix or None,
            "count": len(items),
            "extensions": items[:limit],
            "truncated": len(items) > limit,
            "isaac_sim_version": self.isaac_sim_version,
        }

    # search

    def search(self, query: str, top_k: int = 5, extension: str | None = None) -> list[dict[str, Any]]:
        """Top doc sections for a query: [{title, extension, version, kind, source_path, snippet, score, ...}]."""
        top_k = max(1, min(int(top_k), 25))
        terms = _tokenize(query)
        if self.domain == "isaac_sim":
            # "... in isaac sim?" is how questions reach this domain, and those words match nearly every Overview
            # page; drop them unless nothing else is left. Kit/OmniUI words ('kit file', 'ui window') stay.
            terms = [term for term in terms if term not in _ISAAC_DOMAIN_TERMS] or terms
        if not terms:
            return []
        index = self._get_index()
        docs = index.bm25.docs
        names = _identifiers(query)
        ranked: list[tuple[float, int]] = []
        for doc_id, score in index.bm25.scores(terms).items():
            doc = docs[doc_id]
            if extension and doc["extension"] != extension:
                continue
            score *= _KIND_WEIGHT.get(doc["kind"], 1.0)
            if doc["deprecation"]:
                score *= _DEPRECATED_WEIGHT
            if names and names.intersection(w.lower() for w in _IDENTIFIER.findall(doc["title"])):
                score *= _TITLE_NAME_BOOST
            ranked.append((score, doc_id))
        ranked.sort(key=lambda item: (-item[0], docs[item[1]]["title"], item[1]))
        term_set = set(terms)
        picked: list[dict[str, Any]] = []
        per_file: Counter[str] = Counter()
        per_ext: Counter[str] = Counter()
        seen: set[str] = set()
        for score, doc_id in ranked:
            doc = docs[doc_id]
            if per_file[doc["path"]] >= _PER_FILE or (not extension and per_ext[doc["extension"]] >= _PER_EXTENSION):
                continue
            fingerprint = _flat(doc["text"])[:160]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            per_file[doc["path"]] += 1
            per_ext[doc["extension"]] += 1
            picked.append(self._result(doc, score, term_set))
            if len(picked) >= top_k:
                break
        return picked

    @staticmethod
    def _result(doc: dict[str, Any], score: float, terms: set[str]) -> dict[str, Any]:
        result = {
            "title": doc["title"],
            "extension": doc["extension"],
            "version": doc["version"],
            "kind": doc["kind"],
            "source_path": f"{doc['path']}:{doc['line']}",
            "snippet": _trim(_snippet(doc["text"], terms, SNIPPET_CHARS), SNIPPET_CHARS),
            "score": round(score, 3),
            "deprecated": bool(doc["deprecation"]),
        }
        if doc["deprecation"]:
            result["deprecation_warning"] = doc["deprecation"]
        return result


# --- knowledge backend ----------------------------------------------------------------------------------------------------

_LIST_QUERY = re.compile(
    r"^\s*(?:please\s+)?(?:list|browse|show(?:\s+me)?)\s+(?:all\s+|the\s+|installed\s+|available\s+)*"
    r"(?:[\w.*-]+\s+){0,3}extensions\b"
    r"|\b(?:what|which)\s+(?:[\w.*-]+\s+){0,3}extensions\s+(?:are\s+)?(?:installed|available|there)\b",
    re.I,
)
_LIST_FILLER = frozenset("installed available there isaac sim kit omniui ui omni domain version".split())


def _format_results(heading: str, query: str, results: list[dict[str, Any]]) -> list[str]:
    lines = [f"{heading} for: {query}", ""]
    warned: set[str] = set()
    for number, item in enumerate(results, 1):
        flag = ", deprecated" if item["deprecated"] else ""
        lines.append(f"{number}. {item['title']} [{item['extension']} {item['version']}{flag}]")
        lines.append(f"   {item['snippet']}")
        lines.append(f"   Source: {item['source_path']}")
        if item["deprecated"] and item["extension"] not in warned:
            warned.add(item["extension"])
            lines.append(f"   Deprecated: {item['deprecation_warning']}")
    return lines


def _format_info(info: dict[str, Any]) -> list[str]:
    title = f" - {info['title']}" if info["title"] else ""
    lines = [f"{info['name']} {info['version']}{title} ({info['domain']} extension)"]
    if info["deprecated"]:
        lines.append(f"DEPRECATED: {info['deprecation_warning']}")
    if info["description"]:
        lines.append(info["description"])
    if info["keywords"]:
        lines.append(f"Keywords: {', '.join(info['keywords'])}")
    if info["python_modules"]:
        lines.append(f"Python modules: {', '.join(info['python_modules'])}")
    if info["dependency_count"]:
        hidden = info["dependency_count"] - len(info["dependencies"])
        more = f" (+{hidden} more)" if hidden > 0 else ""
        lines.append(f"Depends on ({info['dependency_count']}): {', '.join(info['dependencies'])}{more}")
    if info["docs"]:
        lines.append(f"Docs ({info['docs_count']}): {', '.join(info['docs'][:12])}")
    latest = info.get("changelog_latest")
    if latest:
        lines.append(f"Latest changelog entry {latest['heading']}: {latest['text']} (Source: {latest['source_path']})")
    if info.get("other_locations"):
        lines.append(f"Also installed: {', '.join(info['other_locations'])}")
    lines.append(f"Source: {info['source_path']}")
    return lines


class OfflineDocsKnowledgeBackend:
    """Knowledge backend with the ``ask`` contract of the Kit / Isaac Sim / OmniUI docs MCPs, answered offline."""

    def __init__(self, domain: str = "kit", knowledge: OfflineDocsKnowledge | None = None) -> None:
        self.domain = normalize_domain(domain)
        if knowledge is not None and knowledge.domain != self.domain:
            raise ValueError(f"knowledge is for {knowledge.domain}, backend is for {self.domain}")
        self.knowledge = knowledge or OfflineDocsKnowledge(self.domain)
        self.name = self.domain
        self.endpoint = "offline"
        self.enabled = True

    @property
    def note(self) -> str:
        version = self.knowledge.isaac_sim_version
        return (
            f"Offline answer from the installed Isaac Sim {version} / Kit extension docs; "
            f"the {self.domain} docs MCP is not running."
        )

    async def ask(self, query: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, query)

    def answer(self, query: str) -> dict[str, Any]:
        base = {"backend": self.domain, "source": "offline_docs", "endpoint": "offline", "query": query, "note": self.note}
        try:
            answer = self._answer(str(query or ""))
        except Exception as exc:  # never raise into the router
            answer = {
                "ok": False,
                "tool": "search",
                "content": "",
                "results": [],
                "message": f"Offline {self.domain} docs failed: {type(exc).__name__}: {exc}",
            }
        if len(answer.get("content", "")) > _CONTENT_CHARS:
            answer["content"] = _trim(answer["content"], _CONTENT_CHARS)
        return {**base, **answer}

    def _answer(self, query: str) -> dict[str, Any]:
        knowledge = self.knowledge
        if not knowledge.available:
            roots = ", ".join(str(p) for p in knowledge.requested_roots)
            return {
                "ok": False,
                "tool": "search",
                "content": "",
                "results": [],
                "message": (
                    f"No Kit/Isaac Sim extension docs found (looked in {roots}); "
                    f"set {ENV_ROOT} to the Isaac Sim install folder."
                ),
            }
        listing = _LIST_QUERY.search(query)
        # "show me extensions for lidar" is a search; "list isaacsim.sensors extensions" is a listing.
        if listing and not [t for t in _tokenize(query[listing.end() :]) if t not in _LIST_FILLER]:
            return self._list_answer(query)
        resolved, unknown = knowledge.find_extension_ids(query)
        if resolved:
            return self._info_answer(query, resolved, unknown)
        return self._search_answer(query, unknown)

    def _unknown_lines(self, unknown: list[str]) -> tuple[list[str], list[dict[str, Any]]]:
        lines: list[str] = []
        missing: list[dict[str, Any]] = []
        for name in unknown:
            info = self.knowledge.extension_info(name)
            if not info.get("namespace"):  # a namespace of installed extensions is not "not installed"
                missing.append({"name": name, "suggestions": info["suggestions"]})
            label = "Installed extensions in it" if info.get("namespace") else "Possible matches"
            hint = f" {label}: {', '.join(info['suggestions'])}." if info["suggestions"] else ""
            lines.append(f"Note: {info['message']}{hint}")
        return lines, missing

    def _list_answer(self, query: str) -> dict[str, Any]:
        prefix = next((m.group(1).rstrip(".*") for m in _DOTTED.finditer(query)), None)
        data = self.knowledge.list_extensions(prefix, limit=100)
        label = _DOMAIN_LABEL[self.domain]
        scope = f" starting with {prefix}" if prefix else ""
        if not data["extensions"]:
            message = f"No installed {label} extensions{scope}."
            return {"ok": False, "tool": "list_extensions", "content": "", "results": [], "message": message}
        lines = [f"Installed {label} extensions{scope} (Isaac Sim {data['isaac_sim_version']}): {data['count']}"]
        for item in data["extensions"]:
            flag = " [deprecated]" if item["deprecated"] else ""
            title = f" - {item['title']}" if item["title"] else ""
            lines.append(f"- {item['name']} {item['version']}{title}{flag}")
        if data["truncated"]:
            hidden = data["count"] - len(data["extensions"])
            lines.append(f"... {hidden} more; name a prefix such as isaacsim.sensors to narrow it.")
        content = "\n".join(lines)
        return {"ok": True, "tool": "list_extensions", "content": content, "results": data["extensions"], "listing": data}

    def _info_answer(self, query: str, names: list[str], unknown: list[str]) -> dict[str, Any]:
        knowledge = self.knowledge
        infos = [knowledge.extension_info(name) for name in names]
        lines: list[str] = []
        for info in infos:
            lines.extend(_format_info(info))
            lines.append("")
        rest = query
        for match in sorted(_DOTTED.finditer(query), key=lambda m: -m.start()):
            # Keep the class part of a module path ("omni.ui.VStack" -> "VStack") for the related search.
            name = knowledge.resolve_extension(match.group(1).rstrip(".*"))
            tail = match.group(1)[len(name) :].strip(".") if name and match.group(1).lower().startswith(name.lower()) else ""
            rest = rest[: match.start()] + " " + tail.replace(".", " ") + " " + rest[match.end() :]
        # Original words, not lowercased terms, so CamelCase names (VStack) keep their title boost.
        rest_query = " ".join(w for w in re.findall(r"[\w.]+", rest) if w.lower() not in _INFO_FILLER)
        related: list[dict[str, Any]] = []
        if [term for term in _tokenize(rest_query) if term not in _INFO_FILLER]:
            for info in infos:
                if info["domain"] == self.domain:
                    related.extend(knowledge.search(rest_query, top_k=3, extension=info["name"]))
            if not related:
                related = knowledge.search(rest_query, top_k=3)
        if related:
            lines.extend(_format_results("Related docs", rest_query, related))
            lines.append("")
        notes, missing = self._unknown_lines(unknown)
        lines.extend(notes)
        answer: dict[str, Any] = {
            "ok": True,
            "tool": "extension_info",
            "content": "\n".join(lines).strip(),
            "results": infos,
            "related": related,
        }
        if missing:
            answer["not_installed"] = missing
        return answer

    def _search_answer(self, query: str, unknown: list[str]) -> dict[str, Any]:
        knowledge = self.knowledge
        results = knowledge.search(query)
        notes, missing = self._unknown_lines(unknown)
        label = _DOMAIN_LABEL[self.domain]
        answer: dict[str, Any] = {"ok": bool(results), "tool": "search", "results": results}
        if missing:
            answer["not_installed"] = missing
        if not results:
            answer["content"] = "\n".join(notes)
            answer["message"] = f"No offline {label} docs matched this query."
            return answer
        heading = f"Offline {label} docs (installed Isaac Sim {knowledge.isaac_sim_version} extensions)"
        lines = _format_results(heading, query, results)
        if notes:
            lines.extend(["", *notes])
        answer["content"] = "\n".join(lines).strip()
        return answer


__all__ = [
    "DOMAINS",
    "ENV_ROOT",
    "OfflineDocsKnowledge",
    "OfflineDocsKnowledgeBackend",
    "classify_extension",
    "clear_cache",
    "default_install_root",
    "normalize_domain",
    "resolve_roots",
]
