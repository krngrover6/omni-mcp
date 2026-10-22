"""Offline Kit / Isaac Sim / OmniUI docs: synthetic doc trees for exact checks, plus one smoke test on the real install."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Iterator

from omniverse_scene_mcp.offline_docs_knowledge import (
    DOMAINS,
    ENV_ROOT,
    OfflineDocsKnowledge,
    OfflineDocsKnowledgeBackend,
    _LocalBM25,
    _local_snippet,
    _local_tokenize,
    _parse_toml_fallback,
    _sections,
    _stub_classes,
    classify_extension,
    clear_cache,
    normalize_domain,
    resolve_roots,
)

REAL_ROOT = Path(__file__).resolve().parents[2] / "isaacsim-6.1.0"
NOTE = "Offline answer from the installed Isaac Sim 6.1.0 / Kit extension docs; the {} docs MCP is not running."

OVERVIEW = """# Overview

The experimental prims extension wraps USD prims for fast batched access.

## Articulation

Use the Articulation class to set joint position targets on a robot.
Gains are configured with set_dof_gains before the simulation starts.

```python
# not a heading inside a fence
robot.set_dof_position_targets(targets)
```
"""

API_RST = """Python API
==========

.. automodule:: isaacsim.core.experimental.prims

Controllers
-----------

.. autoclass:: isaacsim.core.experimental.prims.ArticulationController
    :members:
    :undoc-members:

See :class:`Articulation <isaacsim.core.experimental.prims.Articulation>` for joint control of robots.
"""

STUB = '''import typing

class Stack(Container, Widget):
    """
    The Stack class lines up child widgets horizontally, vertically or sorted in a Z-order.
    """
    def __init__(self, direction, **kwargs) -> None:
        """
        Constructor.
        """
    @property
    def spacing(self) -> float:
        """
        Sets a non-stretchable space in pixels between child items of this layout.
        """
    def _hidden(self) -> None: ...

class VStack(Stack, Container, Widget):
    """
    Shortcut for Stack{eTopToBottom}. The widgets are placed in a column, with suitable sizes.
    """
    def __init__(self, **kwargs) -> None:
        """
        Construct a stack with the widgets placed in a column from top to bottom.

            `spacing : `
                Sets a non-stretchable space in pixels between child items of this layout.
        """

class HStack(Stack, Container, Widget):
    """Shortcut for Stack{eLeftToRight}. The widgets are placed in a row."""

class _Private:
    """zzprivatestub"""
'''


def _write(root: Path, rel: str, data: str | bytes) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")
    return path


def _toml(name: str, title: str, version: str, description: str, keywords: str = '"isaac"', extra: str = "") -> str:
    return (
        f'[package]\ntitle = "{title}"\nversion = "{version}"\ndescription = "{description}"\n'
        f'keywords = [{keywords}]\ncategory = "Simulation"\nchangelog = "docs/CHANGELOG.md"\nreadme = "docs/Overview.md"\n'
        f'{extra}\n[dependencies]\n"omni.kit.uiapp" = {{}}\n\n[settings]\nexts."{name}".enabled = true\n\n'
        f'[[python.module]]\nname = "{name}"\n'
    )


def _build_tree(root: Path) -> None:
    _write(root, "VERSION", "6.1.0-rc.26+release.49347.2d230af4.gl\n")
    prims = "exts/isaacsim.core.experimental.prims"
    _write(root, f"{prims}/config/extension.toml", _toml(
        "isaacsim.core.experimental.prims", "Isaac Sim Core Experimental Prims", "0.9.1",
        "Batched prim wrappers for articulations and rigid bodies.", '"isaac", "prims"'))
    _write(root, f"{prims}/docs/Overview.md", OVERVIEW)
    old_entries = "".join(f"## [0.{i}.0] - 2025-01-{i % 28 + 1:02d}\n### Changed\n- Routine maintenance entry {i} " + "x" * 160 + "\n\n" for i in range(8, 0, -1))
    _write(root, f"{prims}/docs/CHANGELOG.md", (
        "# Changelog\n\n## [0.9.1] - 2026-05-01\n### Fixed\n- Fixed articulation gains on reset.\n\n"
        "## [0.9.0] - 2026-01-01\n### Added\n- Initial release.\n\n" + old_entries * 6
        + "## [0.0.1] - 2024-01-01\n### Added\n- zzancientword appears only in a very old entry.\n"))
    _write(root, f"{prims}/docs/api.rst", API_RST)
    _write(root, f"{prims}/docs/image.png", b"\x89PNG\r\n\x1a\nzzpngword")
    _write(root, f"{prims}/docs/Third_Party_Notices.md", "# Notices\n\nzzthirdpartyword license text for bundled code.\n")
    _write(root, f"{prims}/docs/binary.md", b"\x00\x01\x02zzbinaryword in a binary file\n")
    _write(root, f"{prims}/docs/big.md", "# Big Page\n\nzzheadword is near the start of a large page.\n\n"
           + "filler line about nothing in particular at all\n" * 4000 + "zztailword is past the size cap.\n")

    api = "extsDeprecated/isaacsim.core.api"
    _write(root, f"{api}/config/extension.toml", _toml(
        "isaacsim.core.api", "Isaac Sim Core", "5.3.2", "Core simulation APIs.",
        extra='\n[deprecation]\nwarning = "Extension deprecated since Isaac Sim 6.0.0 in favor of isaacsim.core.experimental.*"\n'))
    _write(root, f"{api}/docs/Overview.md", "# Overview\n\n## Controllers\n\nThe ArticulationController applies articulation actions to a robot.\n")

    _write(root, "extsInternal/isaacsim.util.debug_draw/config/extension.toml", _toml(
        "isaacsim.util.debug_draw", "Debug Draw (internal)", "3.2.5", "Older internal copy."))
    _write(root, "extsInternal/isaacsim.util.debug_draw/docs/Overview.md", "# Debug Draw\n\nOlder internal copy of the debug draw docs.\n")
    cached_draw = "extscache/isaacsim.util.debug_draw-3.2.6+110.3.0.wx64.r.cp312"
    _write(root, f"{cached_draw}/config/extension.toml", _toml(
        "isaacsim.util.debug_draw", "Debug Draw", "3.2.6", "Draw lines and points in the viewport."))
    _write(root, f"{cached_draw}/docs/Overview.md", "# Debug Draw\n\nDraw zzdebuglines and points in the viewport for debugging.\n")
    _write(root, "extscache/omni.isaac.core_archive-5.4.0/config/extension.toml", _toml(
        "omni.isaac.core_archive", "Isaac Sim Core Pip Archive", "5.4.0", "Pip packages needed by isaac sim extensions."))

    ui = "extscache/omni.ui-3.3.1+00c488ae.wx64.r.cp312"
    _write(root, f"{ui}/config/extension.toml", (
        "[package]\ntitle = \"UI Framework\"\nversion = \"3.3.1\"\ndescription = \"The Omniverse UI Framework\"\n"
        "keywords = ['ui', 'core']\nchangelog = \"docs/CHANGELOG.md\"\nreadme = \"docs/Overview.md\"\n\n"
        "[[python.module]]\nname = \"omni.ui\"\n"))
    _write(root, f"{ui}/docs/Overview.md", "# Overview\n\nThe Omniverse UI Framework builds windows and widgets with Python.\n")
    _write(root, f"{ui}/docs/CHANGELOG.md", "# Changelog\n\n## [3.3.1] - 2026-07-13\n### Fixed\n- Fixed a crash on full shutdown.\n")
    _write(root, f"{ui}/omni/ui/_ui.pyi", STUB)
    _write(root, f"{ui}/omni/ui/tests/test_stub.pyi", 'class ZzTestOnly:\n    """zzteststubword"""\n')
    stage = "extscache/omni.kit.widget.stage-4.6.5+00c488ae"
    _write(root, f"{stage}/config/extension.toml", _toml("omni.kit.widget.stage", "Stage Widget", "4.6.5", "Stage tree widget."))
    _write(root, f"{stage}/docs/USAGE_PYTHON.md", "# Usage Examples\n\n## Build a stage window\n\n```python\nwith ui.VStack():\n    StageWidget()\n```\n")
    _write(root, "extscache/omni.ui_query-1.1.12+00c488ae/config/extension.toml", _toml("omni.ui_query", "UI Query", "1.1.12", "Find widgets by path."))
    _write(root, "extscache/omni.kit.widgets.custom-1.1.3+107.3/config/extension.toml", _toml("omni.kit.widgets.custom", "Custom Widgets", "1.1.3", "Extra widgets."))

    commands = "extscache/omni.kit.commands-1.5.0+00c488ae.wx64.r.cp312"
    _write(root, f"{commands}/config/extension.toml", _toml("omni.kit.commands", "Commands", "1.5.0", "Command system with undo."))
    _write(root, f"{commands}/docs/Overview.md", "# Overview\n\n## Executing commands\n\nUse omni.kit.commands.execute to run a registered command with undo support.\n")
    _write(root, "extscache/carb.audio-0.1.0+00c488ae.wx64.r.cp312/config/extension.toml", _toml("carb.audio", "Carbonite Audio", "0.1.0", "Audio playback plugins."))
    _write(root, "extscache/carb.audio-0.1.0+00c488ae.wx64.r.cp312/docs/Settings.md", "# Settings\n\nAudio device settings for the carb audio plugins.\n")


@contextmanager
def _synthetic() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "isaacsim-6.1.0"
        _build_tree(root)
        try:
            yield root
        finally:
            clear_cache()


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


# --- classification, roots and catalog ---------------------------------------------------------------------------


def test_classify_extension_by_namespace() -> None:
    expected = {
        "isaacsim.core.api": "isaac_sim",
        "isaacsim.sensors.rtx": "isaac_sim",
        "omni.isaac.core_archive": "isaac_sim",
        "omni.ui": "omni_ui",
        "omni.ui.scene": "omni_ui",
        "omni.ui_query": "omni_ui",
        "omni.kit.widget.stage": "omni_ui",
        "omni.kit.widgets.custom": "omni_ui",
        "omni.kit.commands": "kit",
        "omni.kit.window.property": "kit",
        "omni.usd": "kit",
        "carb.audio": "kit",
        "usdrt.scenegraph": "kit",
    }
    assert {name: classify_extension(name) for name in expected} == expected


def test_normalize_domain_accepts_aliases_and_rejects_unknown() -> None:
    assert [normalize_domain(d) for d in ("isaac", "IsaacSim", "omni.ui", "kit")] == ["isaac_sim", "isaac_sim", "omni_ui", "kit"]
    assert set(DOMAINS) == {"kit", "isaac_sim", "omni_ui"}
    try:
        OfflineDocsKnowledge("blender")
    except ValueError as exc:
        assert "blender" in str(exc)
    else:
        raise AssertionError("unknown domain accepted")


def test_resolve_roots_expands_an_install_folder_in_priority_order() -> None:
    with _synthetic() as root:
        containers, install_root = resolve_roots(root)
        assert install_root == root
        assert [p.relative_to(root).as_posix() for p in containers] == ["exts", "extsInternal", "extsDeprecated", "extscache"]
        plain, none = resolve_roots([root / "extscache", root / "missing"])
        assert plain == [root / "extscache"] and none is None


def test_catalog_lists_each_domain_with_prefix_and_limit() -> None:
    with _synthetic() as root:
        def names(domain: str, **kwargs: object) -> list[str]:
            return [item["name"] for item in OfflineDocsKnowledge(domain, roots=root).list_extensions(**kwargs)["extensions"]]

        assert names("isaac_sim") == ["isaacsim.core.api", "isaacsim.core.experimental.prims", "isaacsim.util.debug_draw", "omni.isaac.core_archive"]
        assert names("omni_ui") == ["omni.kit.widget.stage", "omni.kit.widgets.custom", "omni.ui", "omni.ui_query"]
        assert names("kit") == ["carb.audio", "omni.kit.commands"]
        isaac = OfflineDocsKnowledge("isaac_sim", roots=root)
        assert names("isaac_sim", prefix="isaacsim.core") == ["isaacsim.core.api", "isaacsim.core.experimental.prims"]
        limited = isaac.list_extensions(prefix="isaacsim.core.*", limit=1)
        assert limited["count"] == 2 and limited["truncated"] is True and len(limited["extensions"]) == 1
        deprecated = {item["name"]: item["deprecated"] for item in isaac.list_extensions()["extensions"]}
        assert deprecated["isaacsim.core.api"] is True and deprecated["isaacsim.core.experimental.prims"] is False


def test_duplicate_extension_keeps_the_newest_version() -> None:
    with _synthetic() as root:
        info = OfflineDocsKnowledge("isaac_sim", roots=root).extension_info("isaacsim.util.debug_draw")
        assert info["version"] == "3.2.6" and info["title"] == "Debug Draw"
        assert info["path"].endswith("extscache/isaacsim.util.debug_draw-3.2.6+110.3.0.wx64.r.cp312")
        assert len(info["other_locations"]) == 1 and "extsInternal/isaacsim.util.debug_draw (3.2.5)" in info["other_locations"][0]


# --- search -----------------------------------------------------------------------------------------------------------


def test_search_returns_cited_sections_with_line_numbers() -> None:
    with _synthetic() as root:
        hits = OfflineDocsKnowledge("isaac_sim", roots=root).search("joint position targets")
        top = hits[0]
        assert set(top) >= {"title", "extension", "version", "kind", "source_path", "snippet", "score", "deprecated"}
        assert top["title"] == "isaacsim.core.experimental.prims: Overview > Articulation"
        assert (top["extension"], top["version"], top["kind"], top["deprecated"]) == ("isaacsim.core.experimental.prims", "0.9.1", "doc", False)
        path, _, line = top["source_path"].rpartition(":")
        assert path.endswith("exts/isaacsim.core.experimental.prims/docs/Overview.md")
        lines = (root / "exts/isaacsim.core.experimental.prims/docs/Overview.md").read_text(encoding="utf-8").splitlines()
        assert lines[int(line) - 1] == "## Articulation"
        assert "set joint position targets" in top["snippet"] and top["score"] > 0
        assert not any("not a heading" in hit["title"] for hit in hits)  # a "# comment" inside a code fence


def test_search_is_scoped_to_the_domain() -> None:
    with _synthetic() as root:
        kit = OfflineDocsKnowledge("kit", roots=root)
        assert all(not hit["extension"].startswith("isaacsim.") for hit in kit.search("articulation robot joint"))
        assert kit.search("execute a registered command with undo")[0]["extension"] == "omni.kit.commands"
        assert kit.search("audio device settings")[0]["extension"] == "carb.audio"


def test_rst_sections_drop_directive_syntax_and_flag_deprecated_hits() -> None:
    with _synthetic() as root:
        hits = OfflineDocsKnowledge("isaac_sim", roots=root).search("ArticulationController")
        rst = next(hit for hit in hits if hit["source_path"].endswith("api.rst:6"))
        assert rst["title"] == "isaacsim.core.experimental.prims: Python API > Controllers"
        assert "isaacsim.core.experimental.prims.ArticulationController" in rst["snippet"]
        assert "See Articulation for joint control" in rst["snippet"]
        assert ".. autoclass" not in rst["snippet"] and ":members:" not in rst["snippet"] and ":class:" not in rst["snippet"]
        old = next(hit for hit in hits if hit["extension"] == "isaacsim.core.api")
        assert old["deprecated"] is True and "deprecated since Isaac Sim 6.0.0" in old["deprecation_warning"]


def test_caps_skip_binaries_notices_and_truncate_large_files() -> None:
    with _synthetic() as root:
        isaac = OfflineDocsKnowledge("isaac_sim", roots=root)
        assert isaac.search("zzheadword")[0]["title"] == "isaacsim.core.experimental.prims: Big Page"
        for word in ("zztailword", "zzancientword", "zzbinaryword", "zzthirdpartyword", "zzpngword"):
            assert isaac.search(word) == [], word
        assert isaac.search("fixed articulation gains reset")[0]["kind"] == "changelog"  # the newest entry is kept
        assert all(len(hit["snippet"]) <= 600 for hit in isaac.search("filler line about nothing", top_k=25))
        assert len(isaac.search("isaac", top_k=500)) <= 25
        status = isaac.status()
        assert status["index_loaded"] and status["truncated_files"] >= 2 and status["chunks"] > 0
        assert isaac.search("?") == [] and isaac.search("   ") == []


# --- extension_info -----------------------------------------------------------------------------------------------------


def test_extension_info_reports_toml_facts_docs_and_latest_change() -> None:
    with _synthetic() as root:
        isaac = OfflineDocsKnowledge("isaac_sim", roots=root)
        info = isaac.extension_info("isaacsim.core.experimental.prims")
        assert info["found"] is True and info["name"] == "isaacsim.core.experimental.prims"
        assert (info["version"], info["title"], info["domain"]) == ("0.9.1", "Isaac Sim Core Experimental Prims", "isaac_sim")
        assert info["description"].startswith("Batched prim wrappers") and info["keywords"] == ["isaac", "prims"]
        assert info["python_modules"] == ["isaacsim.core.experimental.prims"] and info["dependencies"] == ["omni.kit.uiapp"]
        assert info["deprecated"] is False and info["deprecation_warning"] is None
        names = [doc.rpartition("/")[2] for doc in info["docs"]]
        assert names[0] == "Overview.md" and names[-1] == "CHANGELOG.md" and set(names) == {"Overview.md", "api.rst", "big.md", "CHANGELOG.md"}
        assert info["readme"].endswith("docs/Overview.md") and info["source_path"].endswith("config/extension.toml:1")
        assert info["changelog_latest"]["heading"] == "[0.9.1] - 2026-05-01"
        assert "Fixed articulation gains on reset" in info["changelog_latest"]["text"]
        assert info["changelog_latest"]["source_path"].endswith("docs/CHANGELOG.md:3")
        json.dumps(info)


def test_extension_info_resolves_module_paths_folder_names_and_other_domains() -> None:
    with _synthetic() as root:
        isaac = OfflineDocsKnowledge("isaac_sim", roots=root)
        module = isaac.extension_info("isaacsim.core.experimental.prims.Articulation")
        assert module["name"] == "isaacsim.core.experimental.prims" and module["resolved_from"].endswith(".Articulation")
        assert isaac.extension_info("omni.ui-3.3.1+00c488ae.wx64.r.cp312")["name"] == "omni.ui"
        assert isaac.extension_info("omni.ui")["domain"] == "omni_ui"
        old = isaac.extension_info("isaacsim.core.api")
        assert old["deprecated"] is True and old["deprecation_warning"].startswith("Extension deprecated since Isaac Sim 6.0.0")


def test_extension_info_for_an_old_omni_isaac_name_suggests_isaacsim_extensions() -> None:
    with _synthetic() as root:
        info = OfflineDocsKnowledge("isaac_sim", roots=root).extension_info("omni.isaac.core")
        assert info["found"] is False
        assert {"isaacsim.core.api", "isaacsim.core.experimental.prims"} <= set(info["suggestions"])
        assert "Isaac Sim 6.1.0" in info["message"] and "isaacsim.*" in info["message"]


# --- OmniUI API stubs -----------------------------------------------------------------------------------------------------


def test_stub_parser_extracts_class_and_init_docstrings_and_public_members() -> None:
    classes = {name: (bases, line, text) for name, bases, line, text in _stub_classes(STUB)}
    assert list(classes) == ["Stack", "VStack", "HStack"]
    bases, line, text = classes["VStack"]
    assert bases == "Stack, Container, Widget" and STUB.splitlines()[line - 1].startswith("class VStack(")
    assert "placed in a column, with suitable sizes" in text and "from top to bottom" in text
    assert "Members: spacing" in classes["Stack"][2] and "_hidden" not in classes["Stack"][2]
    assert "placed in a row" in classes["HStack"][2]


def test_omni_ui_stub_classes_are_searchable_only_in_the_omni_ui_domain() -> None:
    with _synthetic() as root:
        ui = OfflineDocsKnowledge("omni_ui", roots=root)
        top = ui.search("VStack")[0]
        assert (top["title"], top["kind"], top["extension"]) == ("omni.ui.VStack (class)", "api", "omni.ui")
        vstack_line = next(i for i, line in enumerate(STUB.splitlines(), 1) if line.startswith("class VStack("))
        assert top["source_path"].endswith(f"omni/ui/_ui.pyi:{vstack_line}") and "placed in a column" in top["snippet"]
        assert ui.search("zzprivatestub") == [] and ui.search("zzteststubword") == []
        assert any(hit["extension"] == "omni.kit.widget.stage" for hit in ui.search("VStack stage window"))
        assert all(hit["kind"] != "api" for hit in OfflineDocsKnowledge("omni_ui", roots=root, include_stubs=False).search("VStack"))
        assert all(hit["kind"] != "api" for hit in OfflineDocsKnowledge("kit", roots=root).search("VStack column"))


# --- section splitting and fallbacks ------------------------------------------------------------------------------------


def test_markdown_and_rst_sections_have_titles_and_first_lines() -> None:
    md = [(title, line) for title, line, _text in _sections(OVERVIEW, "md")]
    assert md == [("Overview", 1), ("Overview > Articulation", 5)]
    rst = (
        "=========\nAPI Title\n=========\n\nIntro paragraph long enough to be kept as a chunk of text here.\n\n"
        "Section One\n-----------\n\nBody of section one with enough words to stay over the minimum size.\n\n"
        "Sub Section\n^^^^^^^^^^^\n\nDeeper body text that should be titled with three parts here.\n"
    )
    assert [(t, n) for t, n, _ in _sections(rst, "rst")] == [
        ("API Title", 2), ("API Title > Section One", 7), ("API Title > Section One > Sub Section", 12)
    ]
    long_text = "# Long\n\n" + "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(40))
    chunks = _sections(long_text, "md")
    assert len(chunks) > 3 and all(title == "Long" for title, _, _ in chunks)
    assert all(len(text) < 2 * 2400 + 400 for _, _, text in chunks)


def test_toml_fallback_parser_reads_the_keys_this_module_needs() -> None:
    data = _parse_toml_fallback(
        '[package]\ntitle = "Isaac Sim Core"\nversion = "5.3.2"  # comment\nkeywords = ["isaac", \'core\']\n'
        'description = "Has \\"quotes\\"."\n\n[deprecation]\nwarning = "Deprecated since 6.0.0"\n\n'
        '[dependencies]\n"omni.usd" = {}\n"omni.kit.uiapp" = {optional = true}\n\n[settings]\nexts."x".title = "ignored"\n\n'
        '[[python.module]]\nname = "isaacsim.core.api"\n\n[[python.module]]\nname = "isaacsim.core.api.tests"\n'
    )
    assert data["package"]["title"] == "Isaac Sim Core" and data["package"]["version"] == "5.3.2"
    assert data["package"]["keywords"] == ["isaac", "core"] and data["package"]["description"] == 'Has "quotes".'
    assert data["deprecation"]["warning"] == "Deprecated since 6.0.0"
    assert list(data["dependencies"]) == ["omni.usd", "omni.kit.uiapp"]
    assert [m["name"] for m in data["python"]["module"]] == ["isaacsim.core.api", "isaacsim.core.api.tests"]


def test_local_bm25_fallback_ranks_the_matching_doc_first() -> None:
    index = _LocalBM25()
    for title, body in (("buttons", "Create a Button with a clicked_fn callback"), ("stacks", "A VStack lays out widgets in a column")):
        index.add({"title": title}, _local_tokenize(body))
    index.finish()
    scores = index.scores(_local_tokenize("vstack column"))
    assert index.docs[max(scores, key=scores.get)]["title"] == "stacks"
    assert "stack" in _local_tokenize("VStack")
    assert len(_local_snippet("word " * 400 + "needle here", {"needle"}, 100)) <= 100


# --- backend --------------------------------------------------------------------------------------------------------------


def _backend(domain: str, root: Path) -> OfflineDocsKnowledgeBackend:
    return OfflineDocsKnowledgeBackend(domain, OfflineDocsKnowledge(domain, roots=root))


def test_backend_routes_an_extension_id_to_extension_info() -> None:
    with _synthetic() as root:
        answer = asyncio.run(_backend("isaac_sim", root).ask("what is isaacsim.core.api?"))
        assert answer["ok"] is True and answer["tool"] == "extension_info"
        assert (answer["backend"], answer["source"], answer["query"]) == ("isaac_sim", "offline_docs", "what is isaacsim.core.api?")
        assert answer["note"] == NOTE.format("isaac_sim")
        assert answer["results"][0]["name"] == "isaacsim.core.api"
        assert "DEPRECATED: Extension deprecated since Isaac Sim 6.0.0" in answer["content"]
        assert "Source: " + answer["results"][0]["source_path"] in answer["content"]
        json.dumps(answer)
        ui = asyncio.run(_backend("omni_ui", root).ask("omni.ui.VStack spacing"))
        assert ui["tool"] == "extension_info" and ui["results"][0]["name"] == "omni.ui"
        assert ui["related"][0]["title"] == "omni.ui.VStack (class)" and "Related docs" in ui["content"]
        assert ui["note"] == NOTE.format("omni_ui")


def test_backend_search_cites_sources_and_flags_names_that_are_not_installed() -> None:
    with _synthetic() as root:
        backend = _backend("isaac_sim", root)
        answer = backend.answer("joint position targets")
        assert answer["ok"] is True and answer["tool"] == "search" and answer["results"]
        assert f"Source: {answer['results'][0]['source_path']}" in answer["content"]
        assert answer["content"].startswith("Offline Isaac Sim docs (installed Isaac Sim 6.1.0 extensions) for: joint position targets")
        renamed = backend.answer("how do I step a World in omni.isaac.core")
        assert renamed["tool"] == "search" and renamed["not_installed"][0]["name"] == "omni.isaac.core"
        assert "omni.isaac.core is not an installed extension in Isaac Sim 6.1.0" in renamed["content"]
        assert "isaacsim.core.api" in renamed["not_installed"][0]["suggestions"]
        quiet = _backend("kit", root).answer("how does omni.kit.app work with commands")
        assert "not_installed" not in quiet  # real Kit modules are not reported as missing extensions


def test_backend_lists_extensions_but_searches_when_a_topic_follows() -> None:
    with _synthetic() as root:
        backend = _backend("isaac_sim", root)
        listing = backend.answer("list isaacsim.core extensions")
        assert listing["tool"] == "list_extensions" and listing["ok"] is True
        assert [item["name"] for item in listing["results"]] == ["isaacsim.core.api", "isaacsim.core.experimental.prims"]
        assert "isaacsim.core.api 5.3.2 - Isaac Sim Core [deprecated]" in listing["content"]
        topic = backend.answer("show me extensions for zzdebuglines")
        assert topic["tool"] == "search" and topic["results"][0]["extension"] == "isaacsim.util.debug_draw"


def test_backend_without_docs_explains_how_to_point_at_the_install() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            missing = _backend("kit", Path(tmp) / "no-isaac-here").answer("stage events")
            assert missing["ok"] is False and ENV_ROOT in missing["message"] and missing["results"] == []
            assert missing["source"] == "offline_docs" and missing["backend"] == "kit"
            empty = _backend("kit", Path(tmp)).answer("stage events")
            assert empty["ok"] is False and empty["message"] == "No offline Kit docs matched this query."
        finally:
            clear_cache()


def test_backend_rejects_knowledge_for_another_domain() -> None:
    try:
        OfflineDocsKnowledgeBackend("kit", OfflineDocsKnowledge("omni_ui", roots=[]))
    except ValueError as exc:
        assert "omni_ui" in str(exc)
    else:
        raise AssertionError("domain mismatch accepted")


# --- environment, caching and read-only behaviour ---------------------------------------------------------------------------


def test_env_root_override_points_every_domain_at_another_install() -> None:
    with _synthetic() as root:
        previous = os.environ.get(ENV_ROOT)
        os.environ[ENV_ROOT] = str(root)
        try:
            knowledge = OfflineDocsKnowledge("kit")
            assert knowledge.install_root == root and knowledge.isaac_sim_version == "6.1.0"
            assert knowledge.search("execute a registered command")[0]["extension"] == "omni.kit.commands"
        finally:
            if previous is None:
                os.environ.pop(ENV_ROOT, None)
            else:
                os.environ[ENV_ROOT] = previous


def test_indexes_are_cached_per_process_and_cleared_on_request() -> None:
    with _synthetic() as root:
        first, second = OfflineDocsKnowledge("omni_ui", roots=root), OfflineDocsKnowledge("omni_ui", roots=str(root))
        assert first.status()["index_loaded"] is False
        index = first._get_index()
        assert second._get_index() is index and second.status()["index_loaded"] is True
        assert first.catalog() is OfflineDocsKnowledge("kit", roots=root).catalog()
        clear_cache()
        assert first._get_index() is not index


def test_building_and_answering_never_writes_inside_the_install() -> None:
    with _synthetic() as root:
        before = _snapshot(root)
        for domain in DOMAINS:
            backend = _backend(domain, root)
            for query in ("what is isaacsim.core.api", "VStack", "list extensions", "joint position targets"):
                backend.answer(query)
        assert _snapshot(root) == before
        assert not any(part == "__pycache__" for path in root.rglob("*") for part in path.parts)


# --- real install smoke test ------------------------------------------------------------------------------------------------


def test_real_isaac_sim_61_docs_answer_articulation_and_vstack_quickly() -> None:
    if not (REAL_ROOT / "exts").is_dir() or not (REAL_ROOT / "extscache").is_dir():
        return  # Isaac Sim 6.1.0 is not installed next to omniverse_scene_mcp
    start = time.perf_counter()
    try:
        isaac = OfflineDocsKnowledge("isaac_sim", roots=REAL_ROOT)
        hits = isaac.search("articulation controller")
        assert hits and all(classify_extension(hit["extension"]) == "isaac_sim" for hit in hits)
        text = " ".join(f"{hit['title']} {hit['snippet']}" for hit in hits).lower()
        assert "articulation" in text and "controller" in text
        assert all(len(hit["snippet"]) <= 600 and hit["source_path"].rpartition(":")[2].isdigit() for hit in hits)
        assert isaac.isaac_sim_version == "6.1.0"
        assert isaac.extension_info("isaacsim.core.api")["found"] is True

        ui = OfflineDocsKnowledge("omni_ui", roots=REAL_ROOT)
        ui_hits = ui.search("VStack")
        assert ui_hits and classify_extension(ui_hits[0]["extension"]) == "omni_ui"
        assert any("VStack" in hit["title"] or "VStack" in hit["snippet"] for hit in ui_hits)
        answer = OfflineDocsKnowledgeBackend("omni_ui", ui).answer("VStack")
        assert answer["ok"] is True and answer["note"] == NOTE.format("omni_ui")
        elapsed = time.perf_counter() - start
        assert elapsed < 10, f"real-data index build and queries took {elapsed:.1f}s"
    finally:
        clear_cache()


# --- review-12 regressions ---------------------------------------------------------------------------------------------


def test_isaac_sim_search_ignores_the_domain_name_words_that_route_the_question() -> None:
    with _synthetic() as root:
        isaac = OfflineDocsKnowledge("isaac_sim", roots=root)
        for topic in ("articulation controller", "joint position targets"):
            plain = isaac.search(topic)
            for suffix in (" in isaac sim", " in Isaac Sim?", " with isaacsim"):
                named = isaac.search(topic + suffix)
                assert [(hit["source_path"], hit["score"]) for hit in named] == [
                    (hit["source_path"], hit["score"]) for hit in plain], (topic, suffix)
        assert isaac.search("isaac sim")  # only domain words: they are still searched
        # Other domains keep their words ('kit file', 'ui window' are real content).
        kit = OfflineDocsKnowledge("kit", roots=root)
        assert kit.search("execute a registered command in kit") != [] and kit.search("kit")


def test_a_namespace_of_installed_extensions_is_not_reported_as_not_installed() -> None:
    with _synthetic() as root:
        isaac = OfflineDocsKnowledge("isaac_sim", roots=root)
        info = isaac.extension_info("isaacsim.core.experimental")
        assert info["found"] is False and info["namespace"] is True and info["count"] == 1
        assert info["suggestions"] == ["isaacsim.core.experimental.prims"]
        assert "is a namespace, not one extension" in info["message"] and "not an installed" not in info["message"]
        core = isaac.extension_info("isaacsim.core.*")
        assert core["namespace"] is True and core["suggestions"] == ["isaacsim.core.api", "isaacsim.core.experimental.prims"]
        missing = isaac.extension_info("isaacsim.nothing.here")
        assert missing["found"] is False and "namespace" not in missing and "not an installed" in missing["message"]
        json.dumps(info)
        answer = _backend("isaac_sim", root).answer("what is isaacsim.core.experimental?")
        assert "not_installed" not in answer, answer.get("not_installed")
        assert "isaacsim.core.experimental is a namespace" in answer["content"]
        renamed = _backend("isaac_sim", root).answer("how do I step a World in omni.isaac.core")
        assert renamed["not_installed"][0]["name"] == "omni.isaac.core"  # a real miss is still reported
