"""Offline OpenUSD knowledge: the bundled USD 25.11 API atlas, doc chunks and local Markdown notes, searched with BM25."""

from __future__ import annotations

from array import array
import asyncio
from collections import Counter, defaultdict
import difflib
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Awaitable, Callable, Iterable, Mapping
from urllib.parse import urlsplit

OFFLINE_NOTE = "Offline answer from bundled USD 25.11 docs; the USD docs MCP is not running."
SNIPPET_CHARS = 600
SOURCES = ("atlas", "usd_docs", "learn_openusd", "workspace_notes")

_DATA_SUBPATH = ("kit-usd-agents", "source", "aiq", "usd_code_fns", "src", "omni_aiq_usd_code", "data", "v25.11")
_SKIP_DIRS = {"_static", "_templates", "_build", ".git", "node_modules"}
_DOC_CHARS = 1500
_LIST_LIMIT = 500
_MEMBER_LIMIT = 150
_MATCH_LIMIT = 10
_CHUNK_CHARS = 2400
_CONTENT_CHARS = 9000
_SOURCE_WEIGHT = {"atlas": 0.8}
_OVERLOAD_DOC = "This is an overloaded member function"
_PER_GROUP = 2  # at most this many results from one page or one atlas class's methods
# "OpenUSD" / "in USD" match almost every document, so in a query they only add noise.
_BRAND = re.compile(r"\bopen\s*usd\b|\b(?:in|with|using)\s+(?:usd|pxr)\b", re.I)
_LEARN_MIRROR = "github.com/NVIDIA-Omniverse/LearnOpenUSD/blob/main/docs/"
# Owners a bare method name most likely means, strongest first (see _Atlas.resolve_method).
_PREFERRED_OWNERS = (
    "Usd.Stage", "Usd.Prim", "Usd.Attribute", "Usd.Property", "Usd.Relationship", "Sdf.Layer", "Sdf.Path",
    "Usd.References", "Usd.Payloads", "Usd.VariantSet", "Usd.VariantSets", "UsdGeom.Xform", "UsdGeom.Xformable",
    "UsdGeom.Imageable", "UsdGeom.Mesh", "UsdGeom.Scope", "UsdShade.Material", "UsdShade.Shader",
    "UsdShade.MaterialBindingAPI",
)


def workspace_root() -> Path:
    configured = os.environ.get("OMNIVERSE_ROOT", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[3]


def default_data_dir() -> Path:
    configured = os.environ.get("OMNIVERSE_USD_KNOWLEDGE_DATA", "").strip()
    return Path(configured) if configured else workspace_root().joinpath(*_DATA_SUBPATH)


def default_doc_paths() -> dict[str, list[Path]]:
    root = workspace_root()
    return {
        "learn_openusd": [root / "LearnOpenUSD-main" / "docs"],
        "workspace_notes": sorted(root.glob("OPENUSD_*_ONE_BY_ONE.md")),
    }


# --- tokenizing -----------------------------------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOPWORDS = frozenset(
    "a an and are as at be by can could do does for from has have how i if in into is it its me my of on or our "
    "please should show so tell than that the their them then there these they this those to us was we were what "
    "when where which who why will with would you your about explain vs versus".split()
)
_SYNONYMS = {
    "livrps": "liverps",
    "liverps": "liverps",
    "liverp": "liverps",
    "payloads": "payload",
    "sublayers": "sublayer",
    "variants": "variant",
    "variantset": "variant",
    "variantsets": "variant",
}


def _normalize(word: str) -> str:
    if word in _SYNONYMS:
        return _SYNONYMS[word]
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return _SYNONYMS.get(word, word)


def _word_terms(word: str) -> tuple[str, ...]:
    lowered = word.lower()
    if len(lowered) < 2 or lowered in _STOPWORDS:
        return ()
    terms = [_normalize(lowered)]
    if not (word.islower() or word.isupper() or word.istitle()):
        for part in _CAMEL.findall(word):
            part = part.lower()
            if len(part) > 1 and part not in _STOPWORDS:
                terms.append(_normalize(part))
    return tuple(dict.fromkeys(terms))


def _tokenize(text: str, cache: dict[str, tuple[str, ...]] | None = None) -> list[str]:
    out: list[str] = []
    for word in _WORD.findall(text):
        if cache is None:
            out.extend(_word_terms(word))
            continue
        terms = cache.get(word)
        if terms is None:
            terms = cache[word] = _word_terms(word)
        out.extend(terms)
    return out


# --- text cleanup and snippets --------------------------------------------------------------------------------------

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_ROLE = re.compile(r"\{[A-Za-z:_-]+\}`([^`<]*?)\s*(?:<[^>`]*>)?`")
_LINK = re.compile(r"\[([^\]]+)\]\((?:<[^>]*>|[^)\s]*)(?:\s+\"[^\"]*\")?\)")
_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_EPYDOC = re.compile(r"\b[CLIBMU]\{([^{}]*)\}")
_FENCE = re.compile(r"(`{3,}|~{3,})(.*)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_HEADING_LINE = re.compile(r"(?m)^#{1,6}\s+.*$")
_CODE_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,})([^\n]*)\n(.*?)\n[ \t]*\1[ \t]*$", re.S | re.M)


def _clean(text: str) -> str:
    text = _COMMENT.sub("", text)
    text = _IMAGE.sub("", text)
    text = _ROLE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _HTML.sub("", text)
    text = _EPYDOC.sub(r"\1", text)
    return text


def _flat(text: str) -> str:
    text = re.sub(r"(?m)^[ \t]*(?:#{1,6}|>|`{3,}[^\n]*|~{3,}[^\n]*|:::[^\n]*|:[a-z][a-z-]*:[^\n]*)[ \t]?", "", _clean(text))
    text = text.replace("**", "").replace("�", "-")
    return re.sub(r"\s+", " ", text).strip()


def _trim(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _clean_doc(text: str | None) -> str:
    if not text:
        return ""
    text = _EPYDOC.sub(r"\1", text).replace('"""', "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _snippet(text: str, terms: set[str], limit: int = SNIPPET_CHARS) -> str:
    flat = _flat(text)
    if len(flat) <= limit:
        return flat
    stems = [term for term in terms if len(term) >= 5]
    hits: list[tuple[int, set[str]]] = []
    for match in _WORD.finditer(flat):
        word_terms = _word_terms(match.group(0))
        found = terms.intersection(word_terms)
        found.update(stem for stem in stems for term in word_terms if term.startswith(stem))
        if found:
            hits.append((match.start(), found))
            if len(hits) >= 400:
                break
    start = 0
    if hits:
        best = -1
        for i, (pos, _found) in enumerate(hits):
            seen: set[str] = set()
            weight = 0
            for other, found in hits[i:]:
                if other - pos > limit - 120:
                    break
                weight += 1
                seen |= found
            score = len(seen) * 100 + weight
            if score > best:
                best, start = score, pos
        start = min(start, max(0, len(flat) - limit + 40))
        boundary = max(flat.rfind(". ", max(0, start - 160), start), flat.rfind(": ", max(0, start - 160), start))
        start = boundary + 2 if boundary >= 0 else max(0, start - 40)
    prefix = "..." if start > 0 else ""
    body = flat[start:]
    if len(prefix) + len(body) > limit:
        body = body[: limit - len(prefix) - 3].rstrip() + "..."
    return prefix + body


def _best_code_block(text: str, terms: set[str], limit: int = 900) -> str | None:
    best: tuple[int, str] | None = None
    for match in _CODE_BLOCK.finditer(text):
        lang = match.group(2).strip().lower()
        if lang.startswith(("admonish", "mermaid", "html", "{raw}")) or (lang.startswith("{") and not lang.startswith("{code")):
            continue
        lines = match.group(3).splitlines()
        while lines and (not lines[0].strip() or re.match(r"\s*:[a-z][a-z-]*:", lines[0])):
            lines.pop(0)
        code = "\n".join(lines).rstrip()
        if len(code.strip()) < 15:
            continue
        score = len(terms.intersection(_tokenize(code))) * 3 + (4 if "pxr" in code else 0) + (2 if "Usd" in code else 0)
        if best is None or score > best[0]:
            best = (score, code)
    if best is None or best[0] <= 0:
        return None
    return _trim(best[1], limit)


def _display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(workspace_root().resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


# --- BM25 --------------------------------------------------------------------------------------------------------------


class _BM25Index:
    """Inverted index with compact posting arrays; stdlib only."""

    def __init__(self, k1: float = 1.4, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.docs: list[dict[str, Any]] = []
        self._lengths = array("I")
        self._ids: dict[str, array] = {}
        self._tfs: dict[str, array] = {}
        self._avg = 1.0

    def add(self, doc: dict[str, Any], tokens: list[str]) -> None:
        if not tokens:
            return
        doc_id = len(self.docs)
        self.docs.append(doc)
        self._lengths.append(len(tokens))
        for term, tf in Counter(tokens).items():
            ids = self._ids.get(term)
            if ids is None:
                ids = self._ids[term] = array("I")
                self._tfs[term] = array("H")
            ids.append(doc_id)
            self._tfs[term].append(min(tf, 65535))

    def finish(self) -> None:
        self._avg = (sum(self._lengths) / len(self._lengths)) if self._lengths else 1.0

    def scores(self, terms: Iterable[str]) -> dict[int, float]:
        total = len(self.docs)
        out: dict[int, float] = defaultdict(float)
        k1, b, avg, lengths = self.k1, self.b, self._avg, self._lengths
        for term in set(terms):
            ids = self._ids.get(term)
            if not ids:
                continue
            idf = math.log(1 + (total - len(ids) + 0.5) / (len(ids) + 0.5))
            for doc_id, tf in zip(ids, self._tfs[term]):
                out[doc_id] += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * lengths[doc_id] / avg))
        return out


# --- API atlas ---------------------------------------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    name = name.strip().strip("`'\"").strip()
    name = re.sub(r"\(.*\)$", "", name).strip()
    if name.startswith("pxr."):
        name = name[4:]
    return name.replace("::", ".")


def _signature(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for arg in record.get("arguments") or ():
        name = str(arg.get("name", ""))
        if name in ("self", "cls"):
            continue
        text = name
        if arg.get("type_annotation"):
            text += f": {arg['type_annotation']}"
        if "default_value" in arg:
            text += f" = {arg['default_value']}"
        parts.append(text)
    signature = f"{record.get('name', '')}({', '.join(parts)})"
    if record.get("return_type"):
        signature += f" -> {record['return_type']}"
    return signature


class _Atlas:
    """Lookups over usd_atlas_v25_11.json (modules, classes, methods); source code bodies are dropped on load."""

    def __init__(self, path: Path) -> None:
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.path = path
        self.modules: dict[str, dict[str, Any]] = {k: v for k, v in (raw.get("modules") or {}).items() if k != "."}
        self.classes: dict[str, dict[str, Any]] = raw.get("classes") or {}
        self.methods: dict[str, dict[str, Any]] = raw.get("methods") or {}
        del raw
        self.groups: dict[str, list[str]] = {}
        self.members: dict[str, list[str]] = defaultdict(list)
        self.by_method_name: dict[str, list[str]] = defaultdict(list)
        for key, record in self.methods.items():
            record.pop("source_code", None)
            for arg in record.get("arguments") or ():
                arg.pop("parent_method", None)
            owner, _, local = key.rpartition(".")
            base_name = str(record.get("name") or local)
            base = f"{owner}.{base_name}" if owner else base_name
            group = self.groups.get(base)
            if group is None:
                self.groups[base] = [key]
                self.members[owner].append(base_name)
                self.by_method_name[base_name.lower()].append(base)
            else:
                group.append(key)
        for base, group in self.groups.items():
            group.sort(key=lambda k, b=base: (k != b, len(k), k))
        self._module_lower = {key.lower(): key for key in self.modules}
        self._class_alias: tuple[dict[str, list[str]], ...] = (defaultdict(list), defaultdict(list), defaultdict(list))
        for key, record in self.classes.items():
            module = str(record.get("module_name") or key.split(".", 1)[0])
            local = key[len(module) + 1 :] if key.startswith(module + ".") else key
            aliases = (
                (key.lower(), (module + local.replace(".", "")).lower()),
                (local.lower(),),
                (str(record.get("name", "")).lower(),),
            )
            for table, names in zip(self._class_alias, aliases):
                for alias in names:
                    if alias and key not in table[alias]:
                        table[alias].append(key)

    def cite(self, full_name: str) -> str:
        return f"{self.path.name}#{full_name}"

    def resolve_module(self, name: str) -> str | None:
        cleaned = _clean_name(name)
        return cleaned if cleaned in self.modules else self._module_lower.get(cleaned.lower())

    def resolve_class(self, name: str) -> list[str]:
        cleaned = _clean_name(name)
        if cleaned in self.classes:
            return [cleaned]
        for table in self._class_alias:
            found = table.get(cleaned.lower())
            if found:
                return list(found)
        return []

    def class_methods(self, key: str) -> list[str]:
        names: list[str] = []
        for raw_name in self.classes[key].get("methods") or ():
            record = self.methods.get(f"{key}.{raw_name}")
            names.append(str(record.get("name", raw_name)) if record else raw_name)
        names.extend(self.members.get(key, ()))
        # Private and dunder names (_GetStaticTfType, __init__, __bool__) are noise in a class listing; they stay
        # reachable through method_detail.
        return [n for n in dict.fromkeys(names) if not n.startswith("_")]

    def _find_member(self, owner: str, method: str, depth: int = 0) -> str | None:
        base = f"{owner}.{method}"
        if base in self.groups:
            return base
        lowered = method.lower()
        for name in self.members.get(owner, ()):
            if name.lower() == lowered:
                return f"{owner}.{name}"
        record = self.classes.get(owner)
        if record is None or depth > 8:
            return None
        module = str(record.get("module_name", ""))
        for parent in record.get("parent_classes") or ():
            candidates = [f"{module}.{parent}"] if f"{module}.{parent}" in self.classes else self.resolve_class(parent)[:1]
            for candidate in candidates:
                found = self._find_member(candidate, method, depth + 1)
                if found:
                    return found
        return None

    def _owners(self, name: str) -> list[str]:
        module = self.resolve_module(name)
        return self.resolve_class(name)[:5] or ([module] if module else [])

    def resolve_method(self, name: str, class_name: str | None = None) -> tuple[list[str], str]:
        cleaned = _clean_name(name)
        if not cleaned or " " in cleaned:
            return [], ""
        if class_name:
            found = [base for owner in self._owners(class_name) if (base := self._find_member(owner, cleaned))]
            if found:
                return list(dict.fromkeys(found)), ""
        if "." in cleaned:
            if cleaned in self.groups:
                return [cleaned], ""
            owner, _, method = cleaned.rpartition(".")
            found = [base for candidate in self._owners(owner) if (base := self._find_member(candidate, method))]
            if found:
                return list(dict.fromkeys(found)), ""
            cleaned = method
        # Sort before slicing, so the classes learners mean (Usd.Prim.GetAttribute, UsdGeom.Xform.Define) come first.
        matches = sorted(self.by_method_name.get(cleaned.lower(), []), key=lambda b: self._method_rank(b, cleaned))[
            :_MATCH_LIMIT
        ]
        note = f"{cleaned} was not found on {class_name}; showing other classes with that method." if class_name and matches else ""
        return list(matches), note

    def _method_rank(self, base: str, wanted: str) -> tuple[Any, ...]:
        owner, _, name = base.rpartition(".")
        module = owner.split(".")[0]
        return (
            "_" in owner or module == "Usdviewq",
            _PREFERRED_OWNERS.index(owner) if owner in _PREFERRED_OWNERS else len(_PREFERRED_OWNERS),
            name != wanted,
            {"Usd": 0, "Sdf": 1, "UsdGeom": 2, "UsdShade": 3}.get(module, 4 if module.startswith("Usd") else 5),
            not self.methods[self.groups[base][0]].get("docstring"),
            base,
        )

    def method_info(self, base: str) -> dict[str, Any]:
        keys = self.groups[base]
        primary = self.methods[keys[0]]
        owner = base.rpartition(".")[0]
        docstring = next(
            (str(self.methods[k].get("docstring")) for k in keys
             if self.methods[k].get("docstring") and not str(self.methods[k]["docstring"]).startswith(_OVERLOAD_DOC)),
            "",
        )
        return {
            "full_name": base,
            "name": primary.get("name"),
            "owner": owner,
            "owner_kind": "class" if owner in self.classes else "module",
            "module": primary.get("module_name"),
            "signature": _signature(primary),
            "overloads": [_signature(self.methods[k]) for k in keys[1:13]],
            "overload_count": len(keys) - 1,
            "is_static": bool(primary.get("is_static_method")),
            "is_class_method": bool(primary.get("is_class_method")),
            "return_type": primary.get("return_type"),
            "docstring": _trim(_clean_doc(docstring), _DOC_CHARS),
            "line_number": primary.get("line_number"),
            "url_or_path": self.cite(base),
        }

    def class_info(self, key: str) -> dict[str, Any]:
        record = self.classes[key]
        methods = self.class_methods(key)
        return {
            "full_name": key,
            "name": record.get("name"),
            "module": record.get("module_name"),
            "parent_classes": list(record.get("parent_classes") or ()),
            "docstring": _trim(_clean_doc(record.get("docstring")), _DOC_CHARS),
            "methods": methods[:_MEMBER_LIMIT],
            "method_count": len(methods),
            "line_number": record.get("line_number"),
            "url_or_path": self.cite(key),
        }

    def module_info(self, key: str) -> dict[str, Any]:
        record = self.modules[key]
        classes = [name for name in self.classes if self.classes[name].get("module_name") == key]
        functions = list(self.members.get(key, ()))
        return {
            "name": key,
            "file_path": record.get("file_path"),
            "classes": classes[:_MEMBER_LIMIT],
            "class_count": len(classes),
            "functions": functions[:_MEMBER_LIMIT],
            "function_count": len(functions),
            "url_or_path": self.cite(key),
        }

    def search_docs(self) -> Iterable[tuple[dict[str, Any], str, str]]:
        """Yield (doc, title_text, body_text) for every module, class and method group."""
        for key in self.modules:
            yield {"title": key, "source": "atlas", "kind": "module", "key": key, "url_or_path": self.cite(key)}, key, ""
        for key, record in self.classes.items():
            body = f"{' '.join(record.get('parent_classes') or ())} {record.get('docstring') or ''}"
            yield {"title": key, "source": "atlas", "kind": "class", "key": key, "url_or_path": self.cite(key)}, key, body
        for base, keys in self.groups.items():
            if base.rpartition(".")[2].startswith("_"):
                continue  # __enter__/_GetStaticTfType stay reachable through method_detail, not search
            body = " ".join(
                str(self.methods[k].get("docstring") or "") for k in keys[:1]
                if not str(self.methods[k].get("docstring") or "").startswith(_OVERLOAD_DOC)
            )
            yield {"title": base, "source": "atlas", "kind": "method", "key": base, "url_or_path": self.cite(base)}, base, body

    def doc_text(self, doc: dict[str, Any]) -> str:
        key, kind = doc["key"], doc["kind"]
        if kind == "method" and key in self.groups:
            info = self.method_info(key)
            return f"{info['signature']}. {info['docstring']}"
        if kind == "class" and key in self.classes:
            info = self.class_info(key)
            parents = f"({', '.join(info['parent_classes'])})" if info["parent_classes"] else ""
            return f"class {key}{parents}: {info['docstring'] or 'no docstring'}. Methods: {', '.join(info['methods'][:40])}"
        if kind == "module" and key in self.modules:
            info = self.module_info(key)
            return f"module {key}: {info['class_count']} classes ({', '.join(info['classes'][:40])})"
        return key


# --- Markdown chunking -------------------------------------------------------------------------------------------------


def _markdown_sections(path: Path, chunk_chars: int = _CHUNK_CHARS) -> list[tuple[str, int, str]]:
    """Split a Markdown/MyST file into (title, first line number, text) chunks at headings outside code fences."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    doc_title = ""
    heading = ""
    heading_level = 0
    section = ""  # the last H2, so generic H3s ("Memory hook") say which topic they belong to
    buffer: list[tuple[int, str, bool]] = []
    sections: list[tuple[str, int, str]] = []

    def emit(title: str, piece: list[tuple[int, str, bool]]) -> None:
        body = [(number, line) for number, line, _ in piece]
        while body and not body[0][1].strip():
            body.pop(0)
        text = "\n".join(line for _, line in body).strip()
        if body and len(re.sub(r"\s+", "", _clean(_HEADING_LINE.sub("", text)))) > 25:
            sections.append((title, body[0][0], text))

    def flush() -> None:
        title = doc_title or path.stem.replace("-", " ").replace("_", " ").title()
        if heading and heading != doc_title:
            if heading_level >= 3 and section and section != heading:
                title = f"{title} > {section} > {heading}"
            else:
                title = f"{title} > {heading}"
        piece: list[tuple[int, str, bool]] = []
        size = 0
        for entry in buffer:
            if size >= chunk_chars and entry[2]:
                emit(title, piece)
                piece, size = [], 0
                continue
            piece.append(entry)
            size += len(entry[1]) + 1
        emit(title, piece)
        buffer.clear()

    fence = ""
    skipping = False
    for index in range(start, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if fence:
            if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
                fence = ""
                if skipping:
                    skipping = False
                    continue
            if not skipping:
                buffer.append((index + 1, line, False))
            continue
        match = _FENCE.match(stripped)
        if match:
            fence = match.group(1)
            skipping = match.group(2).strip().startswith("{raw}")
            if not skipping:
                buffer.append((index + 1, line, False))
            continue
        heading_match = _HEADING.match(line)
        if heading_match:
            flush()
            heading = _flat(heading_match.group(2))
            heading_level = len(heading_match.group(1))
            if heading_level == 1 and not doc_title:
                doc_title = heading
            if heading_level <= 2:
                section = heading if heading_level == 2 else ""
        buffer.append((index + 1, line, not stripped))
    flush()
    return sections


def _parse_glossary(path: Path) -> list[dict[str, Any]]:
    """Parse a MyST ``{glossary}`` block: unindented term lines followed by indented definitions."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    inside = False
    current: dict[str, Any] | None = None
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not inside:
            inside = stripped.startswith(":::{glossary}") or stripped.startswith("```{glossary}")
            continue
        if not line[:1].isspace() and stripped.startswith((":::", "```")):
            break
        if current is None and stripped.startswith(":"):
            continue
        if stripped and not line[0].isspace():
            current = {"term": stripped, "line": number, "lines": []}
            entries.append(current)
        elif current is not None:
            current["lines"].append(stripped)
    display = _display_path(path)
    parsed: list[dict[str, Any]] = []
    for entry in entries:
        definition: list[str] = []
        aliases: list[str] = []
        further: list[str] = []
        for line in entry["lines"]:
            aka = re.match(r"\*\*Also Known As:?\*\*:?\s*(.*)", line)
            reading = re.match(r"\*\*Further Reading:?\*\*:?\s*(.*)", line)
            if aka:
                aliases = [a.strip(" *_") for a in aka.group(1).split(",") if a.strip(" *_")]
            elif reading:
                further = [_flat(t) for t in re.findall(r"\[([^\]]+)\]", reading.group(1))]
                further += [_flat(t) for t in re.findall(r"\{[a-z]+\}`([^`]+)`", reading.group(1))]
            else:
                definition.append(line)
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(definition)).strip()
        text = "\n\n".join(_flat(paragraph) for paragraph in text.split("\n\n") if paragraph.strip())
        parsed.append(
            {
                "term": entry["term"],
                "definition": _trim(text, 2000),
                "also_known_as": aliases,
                "further_reading": further,
                "source": "learn_openusd",
                "url_or_path": f"{display}:{entry['line']}",
                "_key": frozenset(_tokenize(entry["term"])),
                "_alias_keys": [frozenset(_tokenize(alias)) for alias in aliases],
            }
        )
    return parsed


def _public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def _split_names(names: str | Iterable[str]) -> list[str]:
    items = [names] if isinstance(names, str) else list(names)
    out: list[str] = []
    for item in items:
        out.extend(part.strip() for part in re.split(r"[,;]", str(item)) if part.strip())
    return out


_NAME_IN_TEXT = re.compile(r"\b(?:pxr\.)?[A-Z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _result_group(doc: dict[str, Any]) -> str:
    """What counts as 'the same place' for the per-group result cap: one atlas class (its own entry and its methods)
    or one page (URL without #anchor or :line)."""
    if doc["source"] == "atlas":
        return "atlas:" + (doc["key"].rpartition(".")[0] if doc.get("kind") == "method" else doc["key"])
    url = str(doc["url_or_path"]).split("#", 1)[0]
    return url if doc["source"] == "usd_docs" else re.sub(r":\d+$", "", url)


# --- public knowledge API ------------------------------------------------------------------------------------------------


class OfflineUSDKnowledge:
    """Offline USD API atlas + BM25 doc search. Everything loads lazily on first use.

    ``doc_paths`` maps a source name ("learn_openusd" or "workspace_notes") to Markdown files or directories; a plain
    list of paths is treated as "learn_openusd". Defaults: LearnOpenUSD-main/docs and OPENUSD_*_ONE_BY_ONE.md notes.
    """

    def __init__(
        self,
        data_dir: str | Path | None = None,
        doc_paths: Mapping[str, Iterable[str | Path]] | Iterable[str | Path] | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        if doc_paths is None:
            self.doc_paths = default_doc_paths()
        elif isinstance(doc_paths, Mapping):
            self.doc_paths = {str(source): [Path(p) for p in paths] for source, paths in doc_paths.items()}
        else:
            self.doc_paths = {"learn_openusd": [Path(p) for p in doc_paths]}
        self._lock = threading.RLock()
        self._atlas: _Atlas | None = None
        self._atlas_error: str | None = None
        self._atlas_loaded = False
        self._index: _BM25Index | None = None
        self._glossary: list[dict[str, Any]] | None = None

    # paths and loading

    @property
    def atlas_path(self) -> Path | None:
        preferred = self.data_dir / "usd_atlas_v25_11.json"
        if preferred.is_file():
            return preferred
        found = sorted(self.data_dir.glob("usd_atlas*.json")) if self.data_dir.is_dir() else []
        return found[-1] if found else None

    @property
    def knowledge_index_path(self) -> Path | None:
        path = self.data_dir / "knowledge_rag" / "index.json"
        return path if path.is_file() else None

    def _markdown_files(self) -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = []
        for source, paths in self.doc_paths.items():
            for path in paths:
                if path.is_file() and path.suffix.lower() == ".md":
                    files.append((source, path))
                elif path.is_dir():
                    for found in sorted(path.rglob("*.md")):
                        if not _SKIP_DIRS.intersection(found.relative_to(path).parts[:-1]):
                            files.append((source, found))
        return list(dict.fromkeys(files))

    def status(self) -> dict[str, Any]:
        atlas, index = self.atlas_path, self.knowledge_index_path
        return {
            "data_dir": str(self.data_dir),
            "atlas": str(atlas) if atlas else None,
            "knowledge_index": str(index) if index else None,
            "doc_paths": {source: [str(p) for p in paths] for source, paths in self.doc_paths.items()},
            "atlas_loaded": self._atlas is not None,
            "search_index_loaded": self._index is not None,
            "atlas_error": self._atlas_error,
        }

    def _get_atlas(self) -> _Atlas | None:
        with self._lock:
            if not self._atlas_loaded:
                self._atlas_loaded = True
                path = self.atlas_path
                if path is not None:
                    try:
                        self._atlas = _Atlas(path)
                    except (OSError, ValueError) as exc:
                        self._atlas_error = f"{type(exc).__name__}: {exc}"
            return self._atlas

    def _glossary_entries(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._glossary is None:
                entries: list[dict[str, Any]] = []
                for source, path in self._markdown_files():
                    if path.name.lower() == "glossary.md":
                        for entry in _parse_glossary(path):
                            entries.append({**entry, "source": source})
                self._glossary = entries
            return self._glossary

    def _knowledge_chunks(self) -> Iterable[tuple[str, str, str]]:
        path = self.knowledge_index_path
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        seen: set[tuple[str, str]] = set()
        # LearnOpenUSD lessons mirrored on GitHub are skipped when the same file is indexed locally.
        roots = [path for path in self.doc_paths.get("learn_openusd", []) if path.is_dir()]
        local_copy: dict[str, bool] = {}
        for record in (data.get("docstore") or {}).values():
            content = str(record.get("page_content") or "")
            metadata = record.get("metadata") or {}
            url = str(metadata.get("url") or "")
            key = (url, content[:200])
            if not content.strip() or key in seen:
                continue
            seen.add(key)
            if roots and _LEARN_MIRROR in url:
                relative = url.split(_LEARN_MIRROR, 1)[1].split("#", 1)[0]
                if relative not in local_copy:
                    local_copy[relative] = any((root / relative).is_file() for root in roots)
                if local_copy[relative]:
                    continue
            yield url, str(metadata.get("index_text") or ""), content

    def _get_index(self) -> _BM25Index:
        with self._lock:
            if self._index is not None:
                return self._index
            index = _BM25Index()
            cache: dict[str, tuple[str, ...]] = {}
            atlas = self._get_atlas()
            if atlas is not None:
                for doc, title, body in atlas.search_docs():
                    index.add(doc, _tokenize(title, cache) * 2 + _tokenize(body, cache))
            for url, summary, content in self._knowledge_chunks():
                parts = summary.split(" - ")
                title = " - ".join(parts[:-1]) if len(parts) > 1 else (summary or url)
                if title == "USD C++ API Reference":
                    # Hundreds of chunks share this title and the bare API URL; name the page they come from.
                    first = next((line for line in content.splitlines() if line.strip()), "")
                    label = _trim(_flat(first).lstrip("*-• ").strip(), 90)
                    if label:
                        title = f"{title}: {label}"
                doc = {
                    "title": _trim(title, 140),
                    "source": "usd_docs",
                    "kind": "doc",
                    "url_or_path": url,
                    "text": content,
                    "summary": summary,
                }
                index.add(doc, _tokenize(title, cache) * 2 + _tokenize(summary, cache) + _tokenize(_clean(content), cache))
            glossary_files = {entry["url_or_path"].rpartition(":")[0] for entry in self._glossary_entries()}
            for entry in self._glossary_entries():
                doc = {
                    "title": f"Glossary: {entry['term']}",
                    "source": entry["source"],
                    "kind": "glossary",
                    "url_or_path": entry["url_or_path"],
                    "text": f"{entry['term']}: {entry['definition']}",
                }
                title_tokens = _tokenize(" ".join([entry["term"], *entry["also_known_as"]]), cache)
                index.add(doc, title_tokens * 2 + _tokenize(entry["definition"], cache))
            for source, path in self._markdown_files():
                display = _display_path(path)
                if display in glossary_files:
                    continue
                try:
                    sections = _markdown_sections(path)
                except OSError:
                    continue
                for title, line, text in sections:
                    doc = {"title": title, "source": source, "kind": "doc", "url_or_path": f"{display}:{line}", "text": text}
                    index.add(doc, _tokenize(title, cache) * 2 + _tokenize(_clean(text), cache))
            index.finish()
            self._index = index
            return index

    # atlas API

    def list_modules(self) -> dict[str, Any]:
        atlas = self._get_atlas()
        if atlas is None:
            return {"modules": [], "count": 0, "message": "USD atlas data not found.", "data_dir": str(self.data_dir)}
        counts = Counter(str(record.get("module_name")) for record in atlas.classes.values())
        modules = [
            {"name": key, "class_count": counts.get(key, 0), "function_count": len(atlas.members.get(key, ()))}
            for key in sorted(atlas.modules)
        ]
        return {"modules": modules, "count": len(modules), "url_or_path": atlas.path.name}

    def list_classes(self, module: str | None = None, limit: int = _LIST_LIMIT) -> dict[str, Any]:
        atlas = self._get_atlas()
        if atlas is None:
            return {"module": module, "classes": [], "count": 0, "message": "USD atlas data not found."}
        resolved = atlas.resolve_module(module) if module else None
        if module and resolved is None:
            return {"module": module, "found": False, "classes": [], "count": 0, "message": f"No USD module named {module}."}
        names = sorted(k for k, v in atlas.classes.items() if resolved is None or v.get("module_name") == resolved)
        return {
            "module": resolved,
            "classes": names[:limit],
            "count": len(names),
            "truncated": len(names) > limit,
            "url_or_path": atlas.path.name,
        }

    def module_detail(self, names: str | Iterable[str]) -> dict[str, Any]:
        atlas = self._get_atlas()
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        for name in _split_names(names):
            key = atlas.resolve_module(name) if atlas else None
            if key is None and atlas is not None and " " in name.strip():
                key = next((m for m in (atlas.resolve_module(c) for c in _NAME_IN_TEXT.findall(name)) if m), None)
            if key is None:
                missing.append(name)
            elif all(item["name"] != key for item in found):
                found.append(atlas.module_info(key))
        return {"modules": found, "not_found": missing}

    def _resolve_classes_in(self, atlas: _Atlas, name: str) -> list[str]:
        keys = atlas.resolve_class(name)
        if keys or " " not in name.strip():
            return keys
        resolved: list[str] = []
        for candidate in _NAME_IN_TEXT.findall(name):
            hits = atlas.resolve_class(candidate)
            if len(hits) == 1:
                resolved.append(hits[0])
        return resolved

    def class_detail(self, names: str | Iterable[str]) -> dict[str, Any]:
        atlas = self._get_atlas()
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        ambiguous: dict[str, list[str]] = {}
        for name in _split_names(names):
            keys = self._resolve_classes_in(atlas, name) if atlas else []
            if not keys:
                missing.append(name)
                continue
            if len(keys) > 1:
                ambiguous[name] = keys[:_MATCH_LIMIT]
            for key in keys[:3]:
                if all(item["full_name"] != key for item in found):
                    found.append(atlas.class_info(key))
        return {"classes": found, "not_found": missing, "ambiguous": ambiguous}

    def method_detail(self, method_names: str | Iterable[str], class_name: str | None = None) -> dict[str, Any]:
        atlas = self._get_atlas()
        found: list[dict[str, Any]] = []
        missing: list[str] = []
        notes: list[str] = []
        for name in _split_names(method_names):
            bases, note = atlas.resolve_method(name, class_name) if atlas else ([], "")
            if not bases:
                missing.append(name)
                continue
            if note:
                notes.append(note)
            for base in bases:
                if len(found) < 2 * _MATCH_LIMIT and all(item["full_name"] != base for item in found):
                    found.append(atlas.method_info(base))
        result: dict[str, Any] = {"methods": found, "not_found": missing, "class_name": class_name}
        if notes:
            result["notes"] = notes
        return result

    # search API

    def _rank(
        self, query: str, top_k: int, sources: Iterable[str] | None = None, code_boost: bool = False
    ) -> tuple[list[tuple[float, dict[str, Any]]], set[str]]:
        # Strip "OpenUSD" and "in/with/using USD|pxr" before camel-splitting, so they do not pull in every note
        # titled "OpenUSD ..."; a bare "usd" is kept, since it matters for ".usd file" questions.
        terms = _tokenize(_BRAND.sub(" ", query)) or _tokenize(query)
        if not terms:
            return [], set()
        index = self._get_index()
        allowed = set(sources) if sources else None
        ranked: list[tuple[float, dict[str, Any]]] = []
        for doc_id, score in index.scores(terms).items():
            doc = index.docs[doc_id]
            if allowed is not None and doc["source"] not in allowed:
                continue
            score *= _SOURCE_WEIGHT.get(doc["source"], 1.0)
            if code_boost and "text" in doc and ("```" in doc["text"] or "from pxr import" in doc["text"]):
                score *= 1.3
            ranked.append((score, doc))
        ranked.sort(key=lambda item: (-item[0], item[1]["title"]))
        picked: list[tuple[float, dict[str, Any]]] = []
        seen: set[str] = set()
        per_group: dict[str, int] = {}
        for score, doc in ranked:
            fingerprint = _flat(doc.get("text") or doc["title"])[:160]
            if fingerprint in seen:
                continue
            group = _result_group(doc)
            # Class and module entries are never capped out by their own methods, but they still count.
            exempt = doc["source"] == "atlas" and doc.get("kind") in ("class", "module")
            if per_group.get(group, 0) >= _PER_GROUP and not exempt:
                continue
            seen.add(fingerprint)
            per_group[group] = per_group.get(group, 0) + 1
            picked.append((score, doc))
            if len(picked) >= top_k:
                break
        return picked, set(terms)

    def _result(self, doc: dict[str, Any], score: float, terms: set[str]) -> dict[str, Any]:
        if doc["source"] == "atlas":
            atlas = self._get_atlas()
            text = atlas.doc_text(doc) if atlas else doc["title"]
        else:
            text = doc["text"]
        snippet = _snippet(text, terms)
        if len(snippet) < 80 and doc.get("summary"):
            snippet = _trim(f"{snippet} {doc['summary']}".strip(" ."), SNIPPET_CHARS)
        return {
            "title": doc["title"],
            "source": doc["source"],
            "kind": doc["kind"],
            "url_or_path": doc["url_or_path"],
            "snippet": snippet,
            "score": round(score, 3),
        }

    def search(self, query: str, top_k: int = 5, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """BM25 over atlas entries, USD doc chunks, LearnOpenUSD pages/glossary and workspace notes."""
        top_k = max(1, min(int(top_k), 25))
        ranked, terms = self._rank(query, top_k, sources)
        return [self._result(doc, score, terms) for score, doc in ranked]

    def code_examples(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Doc chunks with code blocks first (each with a ``code`` field), then atlas/doc snippets to fill top_k."""
        top_k = max(1, min(int(top_k), 25))
        ranked, terms = self._rank(query, top_k * 4, ("usd_docs", "learn_openusd", "workspace_notes"), code_boost=True)
        results: list[dict[str, Any]] = []
        for score, doc in ranked:
            code = _best_code_block(doc.get("text", ""), terms)
            if code:
                results.append({**self._result(doc, score, terms), "code": code})
            if len(results) >= top_k:
                return results
        seen = {item["url_or_path"] for item in results}
        for item in self.search(query, top_k=top_k * 2):
            if len(results) >= top_k:
                break
            if item["url_or_path"] not in seen:
                seen.add(item["url_or_path"])
                results.append(item)
        return results

    def glossary(self, term: str | None = None) -> dict[str, Any]:
        entries = self._glossary_entries()
        if term is None or not str(term).strip():
            return {
                "count": len(entries),
                "terms": [entry["term"] for entry in entries],
                "url_or_path": entries[0]["url_or_path"].rpartition(":")[0] if entries else None,
            }
        key = frozenset(_tokenize(term))
        exact = [e for e in entries if key and (key == e["_key"] or key in e["_alias_keys"])]
        wider = sorted((e for e in entries if key and key < e["_key"]), key=lambda e: len(e["term"]))
        names: dict[str, dict[str, Any]] = {}
        for entry in entries:
            names.setdefault(entry["term"].lower(), entry)
            for alias in entry["also_known_as"]:
                names.setdefault(alias.lower(), entry)
        close = [names[n] for n in difflib.get_close_matches(str(term).lower(), list(names), n=4, cutoff=0.75)]
        ordered = list({id(e): e for e in exact + wider + close}.values())
        match = None
        if not ordered and key:
            # "applied API schema" -> "API Schema": the largest term (or alias) whose words are a proper subset of
            # the query's and cover at least two thirds of them, so "default prim" does not fall to "Prim". A tie
            # between different entries ("reference composition arc") is ambiguous and falls through to not found.
            subsets = [
                (len(k), e) for e in entries for k in (e["_key"], *e["_alias_keys"])
                if k and k < key and 3 * len(k) >= 2 * len(key)
            ]
            if subsets:
                longest = max(size for size, _entry in subsets)
                best = list({id(e): e for size, e in subsets if size == longest}.values())
                if len(best) == 1:
                    ordered, match = best, "subset"
        if ordered:
            result = {
                "found": True,
                **_public_entry(ordered[0]),
                "related": [e["term"] for e in ordered[1:6]],
                "query": term,
            }
            if match:
                result["match"] = match
            return result
        suggestions = difflib.get_close_matches(str(term).lower(), list(names), n=5, cutoff=0.5)
        usd_docs = [r for r in self.search(term, top_k=6, sources=("usd_docs",)) if "glossary" in r["url_or_path"].lower()]
        return {
            "found": False,
            "query": term,
            "suggestions": list(dict.fromkeys(names[s]["term"] for s in suggestions)),
            "usd_docs": usd_docs[:3],
        }


# --- knowledge backends --------------------------------------------------------------------------------------------------


def _format_results(query: str, results: list[dict[str, Any]], heading: str) -> str:
    lines = [f"{heading} for: {query}", ""]
    for number, item in enumerate(results, 1):
        lines.append(f"{number}. {item['title']} [{item['source']}]")
        lines.append(f"   {item['snippet']}")
        if item.get("code"):
            lines.append("   Example:")
            lines.extend(f"      {line}" for line in item["code"].splitlines())
        lines.append(f"   Source: {item['url_or_path']}")
    return "\n".join(lines).strip()


def _format_methods(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for info in data["methods"]:
        flags = " [static]" if info["is_static"] else (" [classmethod]" if info["is_class_method"] else "")
        lines.append(f"{info['full_name']}{flags}  ({info['owner_kind']} {info['owner']})")
        lines.append(f"  {info['signature']}")
        if info["overload_count"]:
            lines.append(f"  {info['overload_count']} overload(s):")
            lines.extend(f"    {signature}" for signature in info["overloads"])
        if info["docstring"]:
            lines.append("  " + info["docstring"].replace("\n", "\n  "))
        lines.append(f"  Source: {info['url_or_path']}")
        lines.append("")
    lines.extend(data.get("notes", []))
    if data["not_found"]:
        lines.append(f"Not found in the USD 25.11 atlas: {', '.join(data['not_found'])}")
    return "\n".join(lines).strip()


def _format_classes(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for info in data["classes"]:
        parents = f"; parents: {', '.join(info['parent_classes'])}" if info["parent_classes"] else ""
        lines.append(f"{info['full_name']}  (module {info['module']}{parents})")
        if info["docstring"]:
            lines.append("  " + info["docstring"].replace("\n", "\n  "))
        lines.append(f"  Methods ({info['method_count']}): {', '.join(info['methods'])}")
        lines.append(f"  Source: {info['url_or_path']}")
        lines.append("")
    for name, keys in data.get("ambiguous", {}).items():
        lines.append(f"'{name}' matches several classes: {', '.join(keys)}")
    if data["not_found"]:
        lines.append(f"Not found in the USD 25.11 atlas: {', '.join(data['not_found'])}")
    return "\n".join(lines).strip()


def _format_modules(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for info in data["modules"]:
        lines.append(f"Module {info['name']} ({info['file_path']}): {info['class_count']} classes, {info['function_count']} functions")
        lines.append(f"  Classes: {', '.join(info['classes'])}")
        if info["functions"]:
            lines.append(f"  Functions: {', '.join(info['functions'])}")
        lines.append(f"  Source: {info['url_or_path']}")
        lines.append("")
    if data["not_found"]:
        lines.append(f"Not found in the USD 25.11 atlas: {', '.join(data['not_found'])}")
    return "\n".join(lines).strip()


_DEFINITION_QUESTION = re.compile(
    r"^\s*(?:what\s+(?:is|are)|what's|define|definition\s+of|meaning\s+of"
    r"|explain(?:\s+what)?(?!\s+(?:how|why|when|where)\b))"
    r"\s+(?:an?\s+|the\s+)?(.+?)(?:\s+(?:is|are))?[\s?.!]*$",
    re.I,
)


class OfflineUSDKnowledgeBackend:
    """Knowledge backend with the same ``ask`` contract as the USD MCP backend, answered from local data."""

    def __init__(
        self,
        knowledge: OfflineUSDKnowledge | None = None,
        selector: Callable[[str], Any] | None = None,
        name: str = "usd",
    ) -> None:
        self.name = name
        self.endpoint = "offline"
        self.enabled = True
        self.knowledge = knowledge or OfflineUSDKnowledge()
        if selector is None:
            from .knowledge import build_usd_backend

            selector = build_usd_backend().selector
        self.selector = selector

    async def ask(self, query: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.answer, query)

    def answer(self, query: str) -> dict[str, Any]:
        selection = self.selector(query)
        base = {
            "backend": self.name,
            "endpoint": self.endpoint,
            "query": query,
            "selected_tool": selection.tool_name,
            "tool_arguments": selection.arguments,
            "selection_reason": selection.explanation,
            "note": OFFLINE_NOTE,
        }
        try:
            answer = self._run(selection.tool_name, dict(selection.arguments), query)
        except Exception as exc:
            answer = {
                "ok": False,
                "source": "offline_bm25",
                "tool": selection.tool_name,
                "content": "",
                "results": [],
                "message": f"Offline USD knowledge failed: {type(exc).__name__}: {exc}",
            }
        if len(answer.get("content", "")) > _CONTENT_CHARS:
            answer["content"] = _trim(answer["content"], _CONTENT_CHARS)
        return {**base, **answer}

    def _atlas_answer(self, tool: str, content: str, results: Any) -> dict[str, Any]:
        return {"ok": True, "source": "offline_atlas", "tool": tool, "content": content, "results": results}

    def _definition(self, query: str) -> dict[str, Any] | None:
        match = _DEFINITION_QUESTION.match(query)
        if not match:
            return None
        term = re.sub(r"\s+in\s+(?:open)?usd$", "", match.group(1).strip(), flags=re.I)
        entry = self.knowledge.glossary(term)
        return entry if entry.get("found") else None

    def _search_answer(self, query: str, tool: str = "search_usd_knowledge") -> dict[str, Any]:
        definition = None
        if tool == "search_usd_code_examples":
            results = self.knowledge.code_examples(query)
            heading = "Offline USD code examples (bundled USD 25.11 docs)"
        else:
            results = self.knowledge.search(query)
            heading = "Offline USD knowledge (bundled USD 25.11 docs)"
            definition = self._definition(query)
        if not results and definition is None:
            return {
                "ok": False,
                "source": "offline_bm25",
                "tool": tool,
                "content": "",
                "results": [],
                "message": "No offline USD matches for this query.",
            }
        content = _format_results(query, results, heading)
        answer: dict[str, Any] = {"ok": True, "source": "offline_bm25", "tool": tool, "results": results}
        if definition is not None:
            answer["glossary"] = definition
            content = (
                f"Definition - {definition['term']} (glossary, {definition['url_or_path']}):\n"
                f"{definition['definition']}\n\n{content}"
            )
        answer["content"] = content
        return answer

    def _module_hint(self, query: str) -> str | None:
        from .knowledge import _extract_usd_name

        name = _extract_usd_name(query)
        if not name:
            return None
        atlas = self.knowledge._get_atlas()
        return atlas.resolve_module(name) if atlas else None

    def _run(self, tool: str, args: dict[str, Any], query: str) -> dict[str, Any]:
        knowledge = self.knowledge
        if tool == "list_usd_modules":
            data = knowledge.list_modules()
            if data["modules"]:
                names = ", ".join(item["name"] for item in data["modules"])
                return self._atlas_answer(tool, f"USD 25.11 Python modules ({data['count']}): {names}\nSource: {data['url_or_path']}", data)
        elif tool == "list_usd_classes":
            data = knowledge.list_classes(args.get("module") or self._module_hint(query))
            if data["classes"]:
                scope = f"in {data['module']}" if data["module"] else "in USD 25.11"
                more = " (truncated; name a module to narrow it)" if data.get("truncated") else ""
                content = f"Classes {scope} ({data['count']}){more}: {', '.join(data['classes'])}\nSource: {data['url_or_path']}"
                return self._atlas_answer(tool, content, data)
        elif tool == "get_usd_module_detail":
            data = knowledge.module_detail(args.get("module_names") or query)
            if data["modules"]:
                return self._atlas_answer(tool, _format_modules(data), data)
        elif tool == "get_usd_class_detail":
            data = knowledge.class_detail(args.get("class_names") or query)
            if data["classes"]:
                return self._atlas_answer(tool, _format_classes(data), data)
        elif tool == "get_usd_method_detail":
            data = knowledge.method_detail(args.get("method_names") or query, args.get("class_name"))
            if data["methods"]:
                return self._atlas_answer(tool, _format_methods(data), data)
        elif tool == "search_usd_code_examples":
            return self._search_answer(str(args.get("request") or query), tool)
        return self._search_answer(str(args.get("request") or query))


_LOCAL_HOSTS = {"127.0.0.1": ("127.0.0.1",), "localhost": ("127.0.0.1", "::1"), "::1": ("::1",)}
_CONNECTION_MARKERS = (
    "connect",
    "refused",
    "timed out",
    "timeout",
    "unreachable",
    "taskgroup",
    "reset by peer",
    "getaddrinfo",
    "name resolution",
    "not connected",
    "disabled or not configured",
    "bad gateway",
    "service unavailable",
)


async def tcp_probe(host: str, port: int, timeout: float) -> str | None:
    """Return None if host:port accepts a TCP connection within timeout, else a short reason."""
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except asyncio.TimeoutError:
        return f"no answer within {timeout:g}s"
    except OSError as exc:
        return exc.strerror or str(exc) or type(exc).__name__
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return None


def _is_connection_failure(result: dict[str, Any]) -> bool:
    message = str(result.get("message") or "").lower()
    return bool(message) and any(marker in message for marker in _CONNECTION_MARKERS)


class FallbackKnowledgeBackend:
    """Use ``primary`` when its local endpoint is up; otherwise answer from ``offline`` and say why."""

    def __init__(
        self,
        primary: Any,
        offline: Any,
        probe_timeout: float = 0.3,
        probe: Callable[[str, int, float], Awaitable[str | None]] | None = None,
    ) -> None:
        self.primary = primary
        self.offline = offline
        self.probe_timeout = probe_timeout
        self.name = getattr(primary, "name", getattr(offline, "name", "usd"))
        self.endpoint = getattr(primary, "endpoint", None)
        self.enabled = bool(getattr(primary, "enabled", True) or getattr(offline, "enabled", True))
        if hasattr(primary, "selector"):
            self.selector = primary.selector
        self._probe = probe or tcp_probe

    async def _primary_down(self) -> str | None:
        if not getattr(self.primary, "enabled", True):
            return "Primary knowledge backend is disabled or not configured."
        try:
            parts = urlsplit(str(self.endpoint or ""))
            host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return None
        if not host or host.lower() not in _LOCAL_HOSTS:
            return None
        reason = None
        for address in _LOCAL_HOSTS[host.lower()]:
            reason = await self._probe(address, port, self.probe_timeout)
            if reason is None:
                return None
        return f"{host}:{port} is not accepting connections ({reason})"

    async def ask(self, query: str) -> dict[str, Any]:
        error = await self._primary_down()
        if error is None:
            result = await self.primary.ask(query)
            if result.get("ok") or not _is_connection_failure(result):
                return result
            error = str(result.get("message"))
        answer = await self.offline.ask(query)
        return {**answer, "primary_error": error, "primary_endpoint": self.endpoint}


def build_usd_backend_with_offline_fallback(
    endpoint: str | None = None, knowledge: OfflineUSDKnowledge | None = None
) -> FallbackKnowledgeBackend:
    """The USD MCP backend wrapped so questions fall back to the bundled offline data when it is not running."""
    from .knowledge import build_usd_backend

    primary = build_usd_backend(endpoint)
    return FallbackKnowledgeBackend(primary, OfflineUSDKnowledgeBackend(knowledge, selector=primary.selector))
