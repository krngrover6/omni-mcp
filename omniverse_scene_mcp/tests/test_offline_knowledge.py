from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import time

from omniverse_scene_mcp.knowledge import build_usd_backend
from omniverse_scene_mcp.offline_knowledge import (
    OFFLINE_NOTE,
    FallbackKnowledgeBackend,
    OfflineUSDKnowledge,
    OfflineUSDKnowledgeBackend,
    _markdown_sections,
    _tokenize,
    build_usd_backend_with_offline_fallback,
    default_data_dir,
    tcp_probe,
)


def _arg(name: str, owner: str, **extra: object) -> dict[str, object]:
    return {"name": name, "parent_method": owner, **extra}


ATLAS = {
    "modules": {
        ".": {"name": "", "full_name": ".", "file_path": "__init__.pyi"},
        "Usd": {"name": "Usd", "full_name": "Usd", "file_path": "Usd/__init__.pyi", "class_names": ["Prim", "Stage"]},
        "UsdGeom": {
            "name": "UsdGeom",
            "full_name": "UsdGeom",
            "file_path": "UsdGeom/__init__.pyi",
            "class_names": ["Xform", "Xformable"],
        },
    },
    "classes": {
        "Usd.Prim": {
            "name": "Prim",
            "full_name": "Usd.Prim",
            "line_number": 40,
            "module_name": "Usd",
            "methods": ["GetAttribute"],
            "docstring": "Prim is the primary container object in scene description.",
        },
        "Usd.Stage": {
            "name": "Stage",
            "full_name": "Usd.Stage",
            "line_number": 10,
            "module_name": "Usd",
            "methods": ["Open", "Open_1", "GetPrimAtPath"],
            "docstring": "The outermost container for scene description, which owns and presents composed prims.",
        },
        "UsdGeom.Xform": {
            "name": "Xform",
            "full_name": "UsdGeom.Xform",
            "line_number": 90,
            "module_name": "UsdGeom",
            "methods": ["Define"],
            "docstring": "Concrete prim schema for a transform, which implements Xformable.",
            "parent_classes": ["Xformable"],
        },
        "UsdGeom.Xformable": {
            "name": "Xformable",
            "full_name": "UsdGeom.Xformable",
            "line_number": 70,
            "module_name": "UsdGeom",
            "methods": ["GetLocalTransformation"],
            "docstring": "Base class for all transformable prims.",
        },
    },
    "methods": {
        "Usd.GetVersion": {
            "name": "GetVersion",
            "full_name": "Usd.GetVersion",
            "line_number": 5,
            "module_name": "Usd",
            "arguments": [],
            "return_type": "tuple",
            "docstring": "Return the USD version as a tuple.",
        },
        "Usd.Prim.GetAttribute": {
            "name": "GetAttribute",
            "full_name": "Usd.Prim.GetAttribute",
            "line_number": 44,
            "module_name": "Usd",
            "arguments": [_arg("self", "Usd.Prim.GetAttribute"), _arg("attrName", "Usd.Prim.GetAttribute")],
            "return_type": "Attribute",
            "docstring": "Return a UsdAttribute with the name C{attrName}.",
            "source_code": "...",
        },
        "Usd.Stage.GetPrimAtPath": {
            "name": "GetPrimAtPath",
            "full_name": "Usd.Stage.GetPrimAtPath",
            "line_number": 30,
            "module_name": "Usd",
            "arguments": [_arg("self", "Usd.Stage.GetPrimAtPath"), _arg("path", "Usd.Stage.GetPrimAtPath")],
            "return_type": "Prim",
            "docstring": "Return the prim at C{path}, or an invalid prim if none exists.",
            "source_code": "...",
        },
        "Usd.Stage.Open": {
            "name": "Open",
            "full_name": "Usd.Stage.Open",
            "line_number": 12,
            "module_name": "Usd",
            "arguments": [
                _arg("filePath", "Usd.Stage.Open"),
                _arg("load", "Usd.Stage.Open", type_annotation="Stage.InitialLoadSet", default_value="..."),
            ],
            "return_type": "Stage",
            "is_static_method": True,
            "decorators": ["overload", "staticmethod"],
            "docstring": "Open a stage rooted at C{filePath}, composing every layer it references.",
            "source_code": "Open a stage rooted at C{filePath}...",
        },
        "Usd.Stage.Open_1": {
            "name": "Open",
            "full_name": "Usd.Stage.Open_1",
            "line_number": 20,
            "module_name": "Usd",
            "arguments": [_arg("rootLayer", "Usd.Stage.Open_1", type_annotation="pxr.Sdf.Layer")],
            "return_type": "Stage",
            "is_static_method": True,
            "docstring": "This is an overloaded member function, provided for convenience.",
        },
        "UsdGeom.Xform.Define": {
            "name": "Define",
            "full_name": "UsdGeom.Xform.Define",
            "line_number": 95,
            "module_name": "UsdGeom",
            "arguments": [
                _arg("stage", "UsdGeom.Xform.Define", type_annotation="pxr.Usd.Stage"),
                _arg("path", "UsdGeom.Xform.Define"),
            ],
            "return_type": "Xform",
            "is_static_method": True,
            "docstring": "Attempt to ensure a UsdPrim adhering to this schema at C{path} is defined.",
        },
        "UsdGeom.Xformable.GetLocalTransformation": {
            "name": "GetLocalTransformation",
            "full_name": "UsdGeom.Xformable.GetLocalTransformation",
            "line_number": 75,
            "module_name": "UsdGeom",
            "arguments": [_arg("self", "x"), _arg("*args", "x", is_variadic=True)],
            "docstring": "Compute the fully-combined, local-to-parent transformation for this prim.",
        },
    },
    "used_classes": {},
}

LIVRPS_TEXT = (
    "LIVRPS is the strength ordering used when opinions compose: Local, Inherits, VariantSets, References, "
    "Payloads, Specializes. Local opinions are strongest and specializes are weakest."
)

KNOWLEDGE_INDEX = {
    "format_version": 1,
    "docstore": {
        "a1": {
            "page_content": LIVRPS_TEXT,
            "metadata": {
                "index_text": "USD Glossary - LIVRPS Strength Ordering - Explains the composition strength order.",
                "index_text_tokens": 12,
                "content_tokens": 30,
                "score": None,
                "method_name": None,
                "url": "https://openusd.org/release/glossary.html#livrps-strength-ordering",
                "usd_version": 25.11,
            },
        },
        "a2": {
            "page_content": LIVRPS_TEXT,
            "metadata": {
                "index_text": "USD Glossary - LIVRPS Strength Ordering - Duplicate chunk.",
                "url": "https://openusd.org/release/glossary.html#livrps-strength-ordering",
                "usd_version": 25.11,
            },
        },
        "b1": {
            "page_content": (
                "A payload is like a reference, but payloads can be loaded or unloaded on demand. "
                "Use a reference when the data should always be composed."
            ),
            "metadata": {
                "index_text": "Composition Arcs - Payloads vs References - Compares the two arcs.",
                "url": "https://openusd.org/release/glossary.html#usdglossary-payload",
                "usd_version": 25.11,
            },
        },
        "c1": {
            "page_content": (
                "Adding a reference in Python:\n\n```python\nfrom pxr import Usd\n"
                "stage = Usd.Stage.CreateInMemory()\nprim = stage.DefinePrim('/World/Asset')\n"
                "prim.GetReferences().AddReference('./asset.usda')\n```\n"
            ),
            "metadata": {
                "index_text": "References - Adding a reference with Python - Shows AddReference.",
                "url": "https://openusd.org/release/api/class_usd_references.html",
                "usd_version": 25.11,
            },
        },
        "d1": {
            "page_content": ".",
            "metadata": {
                "index_text": "USD Terms and Concepts - Relocates - Relocates remap prim paths introduced across composition arcs.",
                "url": "https://openusd.org/release/glossary.html#relocates",
                "usd_version": 25.11,
            },
        },
    },
    "index_to_docstore_id": {"0": "a1", "1": "a2", "2": "b1", "3": "c1", "4": "d1"},
}

GLOSSARY_MD = """# OpenUSD Glossary

:::{glossary}
:sorted:

LIVERPS Strength Ordering

    LIVERPS is the mnemonic for the composition strength ordering of {term}`opinions <Opinions>`.

    It runs from local opinions down to specializes.

    **Also Known As:** *strength ordering, LIVRPS*
    **Further Reading**: [Strength Ordering](<./composition/strength.md>)

Payload

    A payload is a composition arc like a reference that can be loaded on demand.

    **Further Reading**: [Payloads](<./composition/payloads.md>)

Sublayer

    A sublayer combines layers into an ordered layer stack.

Variant Set

    A variant set holds switchable variants for a prim.

    **Also Known As:** *VariantSet*

:::
"""

SUBLAYERS_MD = """---
# SPDX-License-Identifier: Apache-2.0
jupytext:
  text_representation:
    extension: .md
---
# Sublayers

Sublayers stack layers so that stronger layers override weaker ones in the layer stack.

## Working With Python

```{code-cell}
:tags: [remove-input]
# This comment is not a heading
from pxr import Sdf
layer = Sdf.Layer.CreateAnonymous()
layer.subLayerPaths.append("./base.usda")
```

```{raw} html
<div>interactive widget sublayer markup</div>
```

## What Are Variant Sets?

Variant sets let one prim switch between variants without duplicating data.
"""

NOTES_MD = """# Workspace Notes

## References and Payloads

References graft another layer into a prim. Payloads are references you can unload to save memory.
"""


def _make_fixture(root: Path) -> dict[str, Path]:
    data = root / "data"
    (data / "knowledge_rag").mkdir(parents=True)
    (data / "usd_atlas_v25_11.json").write_text(json.dumps(ATLAS), encoding="utf-8")
    (data / "knowledge_rag" / "index.json").write_text(json.dumps(KNOWLEDGE_INDEX), encoding="utf-8")
    docs = root / "learn" / "docs"
    (docs / "composition").mkdir(parents=True)
    (docs / "_static").mkdir()
    (docs / "glossary.md").write_text(GLOSSARY_MD, encoding="utf-8")
    (docs / "composition" / "sublayers.md").write_text(SUBLAYERS_MD, encoding="utf-8")
    (docs / "_static" / "ignored.md").write_text("# Ignored\n\nThis static page about sublayer must be skipped.\n", encoding="utf-8")
    notes = root / "OPENUSD_TEST_ONE_BY_ONE.md"
    notes.write_text(NOTES_MD, encoding="utf-8")
    return {"data": data, "docs": docs, "notes": notes}


def _knowledge(root: Path) -> OfflineUSDKnowledge:
    paths = _make_fixture(root)
    return OfflineUSDKnowledge(paths["data"], {"learn_openusd": [paths["docs"]], "workspace_notes": [paths["notes"]]})


def test_tokenizer_applies_synonyms_and_light_stemming():
    assert _tokenize("LIVRPS") == _tokenize("LIVERPS") == ["liverps"]
    assert _tokenize("payloads") == _tokenize("Payload") == ["payload"]
    assert "sublayer" in _tokenize("subLayers") and _tokenize("sublayers") == ["sublayer"]
    assert "variant" in _tokenize("variantSet") and _tokenize("variants") == ["variant"]
    assert "geom" in _tokenize("UsdGeom.Xform") and "xform" in _tokenize("UsdGeom.Xform")
    assert _tokenize("what is the") == []


def test_atlas_lists_modules_and_classes():
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(Path(tmp))
        modules = knowledge.list_modules()
        assert [m["name"] for m in modules["modules"]] == ["Usd", "UsdGeom"]
        assert modules["modules"][0] == {"name": "Usd", "class_count": 2, "function_count": 1}
        assert knowledge.list_classes()["count"] == 4
        assert knowledge.list_classes("usdgeom")["classes"] == ["UsdGeom.Xform", "UsdGeom.Xformable"]
        assert knowledge.list_classes("Nope")["found"] is False
        detail = knowledge.module_detail("UsdGeom, pxr.Usd, Bogus")
        assert [m["name"] for m in detail["modules"]] == ["UsdGeom", "Usd"]
        assert detail["modules"][1]["functions"] == ["GetVersion"]
        assert detail["not_found"] == ["Bogus"]
        json.dumps(detail)


def test_class_detail_resolves_python_cpp_and_bare_names():
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(Path(tmp))
        for name in ("UsdGeom.Xform", "pxr.UsdGeom.Xform", "UsdGeomXform", "Xform", "usdgeom.xform"):
            detail = knowledge.class_detail(name)
            assert [c["full_name"] for c in detail["classes"]] == ["UsdGeom.Xform"], name
        info = knowledge.class_detail(["UsdGeom.Xform"])["classes"][0]
        assert info["parent_classes"] == ["Xformable"]
        assert info["methods"] == ["Define"]
        assert info["url_or_path"] == "usd_atlas_v25_11.json#UsdGeom.Xform"
        stage = knowledge.class_detail("UsdStage")["classes"][0]
        assert stage["methods"] == ["Open", "GetPrimAtPath"]
        assert knowledge.class_detail("class docs for the Stage please")["classes"][0]["full_name"] == "Usd.Stage"
        assert knowledge.class_detail("Bogus.Thing")["not_found"] == ["Bogus.Thing"]


def test_method_detail_groups_overloads_and_walks_parent_classes():
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(Path(tmp))
        detail = knowledge.method_detail("Open", "Usd.Stage")
        info = detail["methods"][0]
        assert info["full_name"] == "Usd.Stage.Open"
        assert info["signature"] == "Open(filePath, load: Stage.InitialLoadSet = ...) -> Stage"
        assert info["overloads"] == ["Open(rootLayer: pxr.Sdf.Layer) -> Stage"]
        assert info["is_static"] is True
        assert info["docstring"].startswith("Open a stage rooted at filePath,")
        assert knowledge.method_detail("Usd.Stage.Open")["methods"][0]["full_name"] == "Usd.Stage.Open"
        assert knowledge.method_detail("GetAttribute", "UsdPrim")["methods"][0]["signature"] == "GetAttribute(attrName) -> Attribute"
        inherited = knowledge.method_detail("GetLocalTransformation", "UsdGeom.Xform")["methods"][0]
        assert inherited["full_name"] == "UsdGeom.Xformable.GetLocalTransformation"
        assert inherited["signature"] == "GetLocalTransformation(*args)"
        function = knowledge.method_detail("Usd.GetVersion")["methods"][0]
        assert function["owner_kind"] == "module" and function["signature"] == "GetVersion() -> tuple"
        by_name = knowledge.method_detail("getprimatpath")
        assert [m["full_name"] for m in by_name["methods"]] == ["Usd.Stage.GetPrimAtPath"]
        assert knowledge.method_detail("NoSuchMethod")["not_found"] == ["NoSuchMethod"]
        json.dumps(detail)


def test_bm25_search_ranks_cites_and_filters_sources():
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(Path(tmp))
        results = knowledge.search("LIVRPS strength ordering", top_k=5)
        assert results and {r["source"] for r in results} <= {"atlas", "usd_docs", "learn_openusd", "workspace_notes"}
        assert any(r["title"] == "Glossary: LIVERPS Strength Ordering" for r in results)
        assert results[0]["title"] in ("USD Glossary - LIVRPS Strength Ordering", "Glossary: LIVERPS Strength Ordering")
        livrps_urls = [r["url_or_path"] for r in results if r["source"] == "usd_docs" and "livrps" in r["url_or_path"]]
        assert len(livrps_urls) == 1, "duplicate chunks are collapsed"
        assert [r["score"] for r in results] == sorted((r["score"] for r in results), reverse=True)
        assert all(set(r) >= {"title", "source", "url_or_path", "snippet", "score"} for r in results)
        assert all(len(r["snippet"]) <= 600 for r in results)

        compare = knowledge.search("payload vs reference", top_k=3)
        assert "payload" in compare[0]["snippet"].lower() and "reference" in compare[0]["snippet"].lower()

        notes = knowledge.search("payloads unload memory", sources=["workspace_notes"])
        assert notes and notes[0]["source"] == "workspace_notes"
        assert notes[0]["url_or_path"].endswith("OPENUSD_TEST_ONE_BY_ONE.md:3")

        atlas_hits = knowledge.search("GetPrimAtPath", sources=("atlas",))
        assert atlas_hits[0]["title"] == "Usd.Stage.GetPrimAtPath"
        assert atlas_hits[0]["snippet"].startswith("GetPrimAtPath(path) -> Prim.")
        assert all("_static" not in r["url_or_path"] for r in knowledge.search("sublayer", top_k=25))
        assert knowledge.search("") == []
        relocates = knowledge.search("relocates")[0]
        assert relocates["title"] == "USD Terms and Concepts - Relocates"
        assert "Relocates remap prim paths" in relocates["snippet"], "empty chunks fall back to their summary"
        json.dumps(results)


def test_markdown_sections_skip_front_matter_raw_html_and_fenced_comments():
    with tempfile.TemporaryDirectory() as tmp:
        paths = _make_fixture(Path(tmp))
        sections = _markdown_sections(paths["docs"] / "composition" / "sublayers.md")
        titles = [title for title, _line, _text in sections]
        assert titles == ["Sublayers", "Sublayers > Working With Python", "Sublayers > What Are Variant Sets?"]
        lines = {title: line for title, line, _text in sections}
        assert lines["Sublayers"] == 7 and lines["Sublayers > Working With Python"] == 11
        python = sections[1][2]
        assert "# This comment is not a heading" in python and "interactive widget" not in python
        assert all("SPDX" not in text for _title, _line, text in sections)
        heading_only = Path(tmp) / "heading-only.md"
        heading_only.write_text("# Composition Arcs And Strength Ordering Overview\n\n## Next\n", encoding="utf-8")
        assert _markdown_sections(heading_only) == []

        knowledge = OfflineUSDKnowledge(paths["data"], [paths["docs"]])
        hit = knowledge.search("variant sets switch prim", sources=["learn_openusd"])[0]
        assert hit["title"] == "Sublayers > What Are Variant Sets?"
        assert hit["url_or_path"].endswith("composition/sublayers.md:25")


def test_glossary_matches_terms_aliases_and_synonyms():
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(Path(tmp))
        listing = knowledge.glossary()
        assert listing["terms"] == ["LIVERPS Strength Ordering", "Payload", "Sublayer", "Variant Set"]
        livrps = knowledge.glossary("LIVRPS")
        assert livrps["found"] and livrps["term"] == "LIVERPS Strength Ordering"
        assert livrps["also_known_as"] == ["strength ordering", "LIVRPS"]
        assert livrps["further_reading"] == ["Strength Ordering"]
        assert livrps["definition"].startswith("LIVERPS is the mnemonic for the composition strength ordering of opinions.")
        assert livrps["url_or_path"].endswith("glossary.md:6")
        assert knowledge.glossary("payloads")["term"] == "Payload"
        assert knowledge.glossary("subLayers")["term"] == "Sublayer"
        assert knowledge.glossary("VariantSet")["term"] == "Variant Set"
        missing = knowledge.glossary("zebra crossing")
        assert missing["found"] is False
        json.dumps(livrps)


def test_code_examples_return_code_blocks_then_fill_with_snippets():
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(Path(tmp))
        results = knowledge.code_examples("python example to add a reference", top_k=3)
        assert "AddReference" in results[0]["code"]
        assert results[0]["url_or_path"] == "https://openusd.org/release/api/class_usd_references.html"
        assert len(results) == 3
        sublayer = knowledge.code_examples("sublayer paths python")
        assert any("subLayerPaths" in r.get("code", "") and ":tags:" not in r["code"] for r in sublayer)


def test_loading_is_lazy_and_missing_data_is_reported():
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _knowledge(Path(tmp))
        assert knowledge.status()["atlas_loaded"] is False
        knowledge.list_modules()
        assert knowledge.status()["atlas_loaded"] is True and knowledge.status()["search_index_loaded"] is False
        knowledge.search("payload")
        assert knowledge.status()["search_index_loaded"] is True

        empty = OfflineUSDKnowledge(Path(tmp) / "missing", {"learn_openusd": [Path(tmp) / "missing-docs"]})
        assert empty.status()["atlas"] is None and empty.status()["knowledge_index"] is None
        assert empty.list_modules()["modules"] == []
        assert empty.class_detail("UsdGeom.Xform")["not_found"] == ["UsdGeom.Xform"]
        assert empty.search("payload") == []
        assert empty.glossary("payload")["found"] is False
        backend = OfflineUSDKnowledgeBackend(empty)
        result = asyncio.run(backend.ask("what is a payload"))
        assert result["ok"] is False and result["note"] == OFFLINE_NOTE


def test_data_dir_env_override():
    previous = os.environ.get("OMNIVERSE_USD_KNOWLEDGE_DATA")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            os.environ["OMNIVERSE_USD_KNOWLEDGE_DATA"] = tmp
            assert default_data_dir() == Path(tmp)
            assert OfflineUSDKnowledge(doc_paths=[]).data_dir == Path(tmp)
        finally:
            if previous is None:
                os.environ.pop("OMNIVERSE_USD_KNOWLEDGE_DATA", None)
            else:
                os.environ["OMNIVERSE_USD_KNOWLEDGE_DATA"] = previous


def test_offline_backend_answers_with_the_usd_selector():
    with tempfile.TemporaryDirectory() as tmp:
        backend = OfflineUSDKnowledgeBackend(_knowledge(Path(tmp)))
        assert backend.name == "usd" and backend.enabled is True
        reference = build_usd_backend("http://127.0.0.1:1/mcp").selector
        cases = {
            "list modules": ("list_usd_modules", "offline_atlas"),
            "list classes in UsdGeom": ("list_usd_classes", "offline_atlas"),
            "class detail for UsdGeom.Xform": ("get_usd_class_detail", "offline_atlas"),
            "module docs for UsdGeom": ("get_usd_module_detail", "offline_atlas"),
            "method detail for Usd.Stage.Open": ("get_usd_method_detail", "offline_atlas"),
            "show me a code example to add a reference": ("search_usd_code_examples", "offline_bm25"),
            "explain LIVRPS strength ordering": ("search_usd_knowledge", "offline_bm25"),
        }
        for query, (tool, source) in cases.items():
            assert reference(query).tool_name == tool
            result = asyncio.run(backend.ask(query))
            assert result["ok"] is True, query
            assert (result["tool"], result["source"], result["backend"]) == (tool, source, "usd"), query
            assert result["note"] == OFFLINE_NOTE and result["query"] == query
            assert result["content"] and result["results"]
            json.dumps(result)

        classes = asyncio.run(backend.ask("list classes in UsdGeom"))
        assert classes["results"]["classes"] == ["UsdGeom.Xform", "UsdGeom.Xformable"]
        method = asyncio.run(backend.ask("method detail for Usd.Stage.Open"))
        assert "Open(filePath, load: Stage.InitialLoadSet = ...) -> Stage" in method["content"]
        assert "Source: usd_atlas_v25_11.json#Usd.Stage.Open" in method["content"]
        search = asyncio.run(backend.ask("explain LIVRPS strength ordering"))
        assert "Source: https://openusd.org/release/glossary.html#livrps-strength-ordering" in search["content"]
        code = asyncio.run(backend.ask("show me a code example to add a reference"))
        assert "AddReference" in code["content"]

        definition = asyncio.run(backend.ask("What is a payload?"))
        assert definition["glossary"]["term"] == "Payload"
        assert definition["content"].startswith("Definition - Payload (glossary,")

        unknown = asyncio.run(backend.ask("class detail for Bogus.Thing payload"))
        assert unknown["selected_tool"] == "get_usd_class_detail"
        assert unknown["tool"] == "search_usd_knowledge" and unknown["source"] == "offline_bm25"


class _FakePrimary:
    def __init__(self, endpoint: str, result: dict[str, object], enabled: bool = True) -> None:
        self.name = "usd"
        self.endpoint = endpoint
        self.enabled = enabled
        self.selector = lambda query: None
        self.result = result
        self.calls: list[str] = []

    async def ask(self, query: str) -> dict[str, object]:
        self.calls.append(query)
        return dict(self.result)


class _FakeOffline:
    name = "usd"
    endpoint = "offline"
    enabled = True

    async def ask(self, query: str) -> dict[str, object]:
        return {"ok": True, "backend": "usd", "source": "offline_bm25", "query": query, "note": OFFLINE_NOTE}


def _probe(reason: str | None, calls: list[tuple[str, int, float]]):
    async def probe(host: str, port: int, timeout: float) -> str | None:
        calls.append((host, port, timeout))
        return reason

    return probe


def test_fallback_answers_offline_when_local_endpoint_is_closed():
    calls: list[tuple[str, int, float]] = []
    primary = _FakePrimary("http://127.0.0.1:9903/mcp", {"ok": True, "content": "upstream"})
    backend = FallbackKnowledgeBackend(primary, _FakeOffline(), probe=_probe("connection refused", calls))
    assert (backend.name, backend.endpoint, backend.enabled) == ("usd", "http://127.0.0.1:9903/mcp", True)
    assert backend.selector is primary.selector
    result = asyncio.run(backend.ask("what is a payload"))
    assert result["source"] == "offline_bm25" and result["ok"] is True
    assert result["primary_endpoint"] == "http://127.0.0.1:9903/mcp"
    assert "127.0.0.1:9903 is not accepting connections (connection refused)" == result["primary_error"]
    assert calls == [("127.0.0.1", 9903, 0.3)]
    assert primary.calls == []


def test_fallback_uses_primary_when_it_is_up_or_fails_for_other_reasons():
    calls: list[tuple[str, int, float]] = []
    primary = _FakePrimary("http://localhost:9903/mcp", {"ok": True, "content": "upstream"})
    backend = FallbackKnowledgeBackend(primary, _FakeOffline(), probe=_probe(None, calls))
    assert asyncio.run(backend.ask("q")) == {"ok": True, "content": "upstream"}
    assert calls == [("127.0.0.1", 9903, 0.3)]

    tool_error = _FakePrimary("http://127.0.0.1:9903/mcp", {"ok": False, "content": "bad arguments"})
    backend = FallbackKnowledgeBackend(tool_error, _FakeOffline(), probe=_probe(None, []))
    assert asyncio.run(backend.ask("q")) == {"ok": False, "content": "bad arguments"}

    transport = _FakePrimary(
        "http://127.0.0.1:9903/mcp",
        {"ok": False, "message": "Failed to query upstream MCP backend: unhandled errors in a TaskGroup (1 sub-exception)"},
    )
    backend = FallbackKnowledgeBackend(transport, _FakeOffline(), probe=_probe(None, []))
    result = asyncio.run(backend.ask("q"))
    assert result["source"] == "offline_bm25" and "TaskGroup" in result["primary_error"]


def test_fallback_skips_probe_for_remote_hosts_and_handles_disabled_primary():
    async def forbidden(host: str, port: int, timeout: float) -> str | None:
        raise AssertionError("remote endpoints must not be probed")

    remote = _FakePrimary("https://docs.example.com/mcp", {"ok": True, "content": "remote"})
    assert asyncio.run(FallbackKnowledgeBackend(remote, _FakeOffline(), probe=forbidden).ask("q"))["content"] == "remote"

    disabled = _FakePrimary("", {"ok": False, "message": "Knowledge backend is disabled or not configured."}, enabled=False)
    result = asyncio.run(FallbackKnowledgeBackend(disabled, _FakeOffline(), probe=forbidden).ask("q"))
    assert result["source"] == "offline_bm25" and disabled.calls == []


def test_tcp_probe_against_a_listener_owned_by_the_test():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert asyncio.run(tcp_probe("127.0.0.1", port, 1.0)) is None
    finally:
        listener.close()
    assert isinstance(asyncio.run(tcp_probe("127.0.0.1", port, 0.3)), str)


def test_build_usd_backend_with_offline_fallback_shares_the_selector():
    with tempfile.TemporaryDirectory() as tmp:
        backend = build_usd_backend_with_offline_fallback("http://127.0.0.1:1/mcp", _knowledge(Path(tmp)))
        assert backend.endpoint == "http://127.0.0.1:1/mcp" and backend.name == "usd"
        assert backend.offline.selector is backend.primary.selector
        assert backend.probe_timeout == 0.3


def _method(full_name: str, docstring: str = "", **extra: object) -> dict[str, object]:
    owner, _, name = full_name.rpartition(".")
    return {
        "name": name,
        "full_name": full_name,
        "line_number": 1,
        "module_name": owner.split(".")[0],
        "arguments": [],
        "docstring": docstring,
        **extra,
    }


def _class(full_name: str, methods: list[str], docstring: str = "") -> dict[str, object]:
    module, _, name = full_name.partition(".")
    return {"name": name, "full_name": full_name, "line_number": 1, "module_name": module, "methods": methods, "docstring": docstring}


EXTRA_GLOSSARY_MD = """# OpenUSD Glossary

:::{glossary}
:sorted:

API Schema

    An API schema adds properties and behavior to a prim without changing its type.

Composition Arcs

    Composition arcs combine layers and prims into one composed scene.

Payload

    A payload is a composition arc like a reference that can be loaded on demand.

Prim

    A prim is the primary container object in USD.

Reference

    A reference composes another layer's prim into this namespace.

    **Also Known As:** *Reference Arc*

Time Code

    A time code represents a point in time for querying time-varying data.

:::
"""

DEFAULT_PRIM_MD = """# Default Prim

## What Is a Default Prim?

The default prim is the root prim a reference targets when it names no prim path. Set defaultPrim in the layer metadata.
"""

FOUNDATIONS_MD = """# OpenUSD Foundations, One By One

## 15. Default Prim

### Memory hook

OpenUSD notes: think of the front door of an OpenUSD layer; OpenUSD readers walk in there first.

### Simple meaning

OpenUSD notes: when OpenUSD opens a layer it starts from this prim. OpenUSD, OpenUSD, OpenUSD.
"""


def _extended_knowledge(root: Path) -> OfflineUSDKnowledge:
    """The base fixture plus the cases behind the offline-search review findings (AQ-1..AQ-9)."""
    paths = _make_fixture(root)
    atlas = json.loads(json.dumps(ATLAS))
    atlas["modules"]["Sdf"] = {"name": "Sdf", "full_name": "Sdf", "file_path": "Sdf/__init__.pyi"}
    # Insertion order puts the classes learners rarely mean first (as in the real atlas).
    methods = {
        "Sdf.CrateInfo.Open": _method("Sdf.CrateInfo.Open", "Open a crate file for inspection."),
        "Sdf.ZipFile.Open": _method("Sdf.ZipFile.Open", "Open a zip file."),
        "UsdGeom.BasisCurves.Define": _method("UsdGeom.BasisCurves.Define", "Define a BasisCurves prim."),
        "Sdf.ChangeBlock.__init__": _method("Sdf.ChangeBlock.__init__", "Create a change block."),
        "Sdf.ChangeBlock.__enter__": _method("Sdf.ChangeBlock.__enter__", "ChangeBlock __enter__()."),
        "Sdf.ChangeBlock.__exit__": _method("Sdf.ChangeBlock.__exit__", "ChangeBlock __exit__(type, value, traceback)."),
        "UsdGeom.Xform.__init__": _method("UsdGeom.Xform.__init__"),
        "UsdGeom.Xform._GetStaticTfType": _method("UsdGeom.Xform._GetStaticTfType"),
        "UsdGeom.Xform.__bool__": _method("UsdGeom.Xform.__bool__"),
        **atlas["methods"],
    }
    for name in ("GetAllGeomSubsets", "SetFamilyType", "GetFamilyNameAttr", "CreateFamilyNameAttr", "GetIndicesAttr"):
        methods[f"UsdGeom.Subset.{name}"] = _method(f"UsdGeom.Subset.{name}", f"{name} for a GeomSubset subset of faces.")
    atlas["methods"] = methods
    atlas["classes"].update({
        "Sdf.CrateInfo": _class("Sdf.CrateInfo", ["Open"]),
        "Sdf.ZipFile": _class("Sdf.ZipFile", ["Open"]),
        "UsdGeom.BasisCurves": _class("UsdGeom.BasisCurves", ["Define"]),
        "Sdf.ChangeBlock": _class(
            "Sdf.ChangeBlock", ["__init__", "__enter__", "__exit__"],
            "Group many Sdf ChangeBlock edits so change processing runs once when the block closes.",
        ),
        "UsdGeom.Subset": _class("UsdGeom.Subset", [], "Encodes a GeomSubset: a subset of a geometric prim's faces."),
    })
    atlas["classes"]["UsdGeom.Xform"]["methods"] = ["__init__", "Define", "_GetStaticTfType", "__bool__"]
    (paths["data"] / "usd_atlas_v25_11.json").write_text(json.dumps(atlas), encoding="utf-8")

    index = json.loads(json.dumps(KNOWLEDGE_INDEX))
    mirror = "https://github.com/NVIDIA-Omniverse/LearnOpenUSD/blob/main/docs/"
    extra = {
        # A GitHub copy of a lesson that is also indexed from the local docs folder, and one that is not.
        "m1": (mirror + "composition/sublayers.md#sublayers", "Sublayers - Mirror copy - Stack layers.",
               "Sublayers stack layers so that stronger layers override weaker ones (mirror copy of the lesson)."),
        "m2": (mirror + "other/elsewhere.md", "Elsewhere - Mirror only - Not local.",
               "Mirror-only lesson about sublayer muting that exists nowhere else."),
        # Three different C++ reference pages that share a title and the bare API URL.
        "x1": ("https://openusd.org/release/api/", "USD C++ API Reference - Applied schema details.",
               "Auto applied API schemas\n\nApplied API schema details, part one."),
        "x2": ("https://openusd.org/release/api/", "USD C++ API Reference - Applied schema details.",
               "Built-in applied API schemas\n\nApplied API schema details, part two."),
        "x3": ("https://openusd.org/release/api/", "USD C++ API Reference - Applied schema details.",
               "Schema registry notes\n\nApplied API schema details, part three."),
    }
    for key, (url, summary, content) in extra.items():
        index["docstore"][key] = {"page_content": content, "metadata": {"index_text": summary, "url": url}}
    (paths["data"] / "knowledge_rag" / "index.json").write_text(json.dumps(index), encoding="utf-8")

    (paths["docs"] / "glossary.md").write_text(EXTRA_GLOSSARY_MD, encoding="utf-8")
    (paths["docs"] / "default-prim.md").write_text(DEFAULT_PRIM_MD, encoding="utf-8")
    foundations = root / "OPENUSD_FOUNDATIONS_ONE_BY_ONE.md"
    foundations.write_text(FOUNDATIONS_MD, encoding="utf-8")
    return OfflineUSDKnowledge(
        paths["data"], {"learn_openusd": [paths["docs"]], "workspace_notes": [paths["notes"], foundations]}
    )


def test_definition_questions_reach_the_glossary_and_qualified_names_reach_the_atlas():
    """AQ-1 / AQ-2: 'time code' is a term, not a code request; Usd.Stage.Open is answered from the atlas."""
    with tempfile.TemporaryDirectory() as tmp:
        backend = OfflineUSDKnowledgeBackend(_extended_knowledge(Path(tmp)), build_usd_backend("http://127.0.0.1:1/mcp").selector)
        for query in ("what is a timecode", "what is a time code"):
            answer = backend.answer(query)
            assert answer["tool"] == "search_usd_knowledge", query
            assert answer["glossary"]["term"] == "Time Code", query
        method = backend.answer("what does Usd.Stage.Open do")
        assert (method["tool"], method["source"]) == ("get_usd_method_detail", "offline_atlas")
        assert "Open(filePath, load: Stage.InitialLoadSet = ...) -> Stage" in method["content"]


def test_github_mirror_of_a_local_lesson_is_not_indexed_twice():
    """AQ-3"""
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _extended_knowledge(Path(tmp))
        urls = [r["url_or_path"] for r in knowledge.search("sublayer stack layers stronger weaker", top_k=25)]
        assert not any("composition/sublayers.md#" in url and "github.com" in url for url in urls), urls
        assert any(url.endswith("composition/sublayers.md:7") for url in urls), urls
        mirror_only = knowledge.search("mirror-only lesson sublayer muting", top_k=5)
        assert mirror_only[0]["url_or_path"].endswith("other/elsewhere.md"), "a mirror with no local copy stays"


def test_private_and_dunder_methods_are_not_search_results_or_class_listings():
    """AQ-4"""
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _extended_knowledge(Path(tmp))
        for query in ("what does SdfChangeBlock do", "what is Sdf.ChangeBlock"):
            results = knowledge.search(query, top_k=5)
            assert results[0]["title"] == "Sdf.ChangeBlock", (query, [r["title"] for r in results])
            assert not any(r["title"].rpartition(".")[2].startswith("_") for r in results), query
        assert knowledge.class_detail("UsdGeom.Xform")["classes"][0]["methods"] == ["Define"]
        info = knowledge.class_detail("Sdf.ChangeBlock")["classes"][0]
        assert info["methods"] == [] and info["method_count"] == 0
        # Still reachable by name.
        assert knowledge.method_detail("__enter__", "Sdf.ChangeBlock")["methods"][0]["full_name"] == "Sdf.ChangeBlock.__enter__"


def test_results_are_capped_per_page_and_per_atlas_class():
    """AQ-5 / AQ-6: no more than two hits from one page or one class's methods; the class itself is not crowded out."""
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _extended_knowledge(Path(tmp))
        subset = knowledge.search("what is a GeomSubset", top_k=5)
        titles = [r["title"] for r in subset]
        assert "UsdGeom.Subset" in titles, titles
        assert sum(t.startswith("UsdGeom.Subset.") for t in titles) <= 2, titles
        api = knowledge.search("applied API schema details", top_k=5)
        cpp = [r for r in api if r["url_or_path"] == "https://openusd.org/release/api/"]
        assert len(cpp) == 2, [r["title"] for r in api]
        assert {r["title"] for r in cpp} <= {
            "USD C++ API Reference: Auto applied API schemas",
            "USD C++ API Reference: Built-in applied API schemas",
            "USD C++ API Reference: Schema registry notes",
        }


def test_glossary_matches_a_unique_subset_term_and_explain_phrasing():
    """AQ-7"""
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _extended_knowledge(Path(tmp))
        applied = knowledge.glossary("applied API schema")
        assert applied["found"] is True and applied["term"] == "API Schema" and applied["match"] == "subset"
        assert "match" not in knowledge.glossary("API schema")
        # 'Composition Arcs' and the 'Reference Arc' alias tie, so neither is guessed.
        assert knowledge.glossary("reference composition arc")["found"] is False
        # 'Prim' covers only half of 'default prim', which is not enough.
        assert knowledge.glossary("default prim")["found"] is False
        backend = OfflineUSDKnowledgeBackend(knowledge, build_usd_backend("http://127.0.0.1:1/mcp").selector)
        for query in ("explain what a payload is", "explain payloads", "what is an applied API schema"):
            assert backend._definition(query) is not None, query
        assert backend._definition("explain what a payload is")["term"] == "Payload"
        assert backend._definition("explain how references work") is None


def test_bare_method_names_list_the_classes_learners_mean_first():
    """AQ-8"""
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _extended_knowledge(Path(tmp))
        assert knowledge.method_detail("Open")["methods"][0]["full_name"] == "Usd.Stage.Open"
        assert knowledge.method_detail("Define")["methods"][0]["full_name"] == "UsdGeom.Xform.Define"
        names = [m["full_name"] for m in knowledge.method_detail("Open")["methods"]]
        assert set(names) == {"Usd.Stage.Open", "Sdf.CrateInfo.Open", "Sdf.ZipFile.Open"}


def test_openusd_in_a_query_does_not_pull_in_every_note_titled_openusd():
    """AQ-9"""
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = _extended_knowledge(Path(tmp))
        plain = knowledge.search("what is the default prim", top_k=3)
        branded = knowledge.search("what is the default prim in OpenUSD", top_k=3)
        assert branded[0]["title"] == plain[0]["title"] == "Default Prim > What Is a Default Prim?"
        terms = knowledge._rank("what is the default prim in OpenUSD", 5)[1]
        assert not terms & {"openusd", "open", "usd"}, terms
        assert "usd" in knowledge._rank("what is a .usd file", 5)[1], "a bare 'usd' is kept"
        notes = knowledge.search("memory hook front door", sources=["workspace_notes"], top_k=1)
        assert notes[0]["title"] == "OpenUSD Foundations, One By One > 15. Default Prim > Memory hook"


def test_real_bundled_data_smoke():
    knowledge = OfflineUSDKnowledge()
    if knowledge.atlas_path is None or knowledge.knowledge_index_path is None:
        return
    started = time.perf_counter()
    livrps = knowledge.search("LIVRPS strength ordering", top_k=5)
    assert any("livrps" in (r["title"] + r["snippet"]).lower() or "liverps" in (r["title"] + r["snippet"]).lower() for r in livrps)
    compare = knowledge.search("payload vs reference", top_k=5)
    assert any("payload" in r["snippet"].lower() and "reference" in r["snippet"].lower() for r in compare)
    xform = knowledge.class_detail("UsdGeom.Xform")["classes"][0]
    assert xform["full_name"] == "UsdGeom.Xform" and "Xformable" in xform["parent_classes"]
    assert knowledge.method_detail("Open", "Usd.Stage")["methods"][0]["full_name"] == "Usd.Stage.Open"
    json.dumps([livrps, compare, xform])
    assert time.perf_counter() - started < 15
