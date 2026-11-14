from omniverse_scene_mcp import doc_sources, features

# Shape of a real USD Code MCP answer (seen live 2026-11-14 on port 9903).
ANSWER = (
    "Title 'Rigid Body Physics in USD Proposal - Overall Design Concerns - USD Implementation - Joints - long "
    "summary', URL 'https://openusd.org/release/wp_rigid_body_physics.html#joint-limits-and-drives'\n---\n"
    "## Joint limits and drives\n\nLimits restrict the [joint](#joint) motion. ```py\ncode()\n```\n---\n\n\n"
    "Title 'USD C++ API Reference - Joints', URL 'https://openusd.org/release/api/'\n---\nAPI text one.\n---\n\n\n"
    "Title 'USD C++ API Reference - Joint paths', URL 'https://openusd.org/release/api/'\n---\nAPI text two.\n---\n"
    "Title 'What Is LIVRPS?', URL 'https://github.com/NVIDIA-Omniverse/LearnOpenUSD/blob/main/docs/x.md'\n---\nThird.\n---"
)


def _result(*codes):
    return {"ok": True, "findings": [{"code": c, "cause": f"Because of {c}.", "severity": "warning"} for c in codes]}


def test_parse_sources_keeps_title_link_and_a_clean_quote_once_per_page():
    sources = doc_sources.parse_sources(ANSWER, limit=3)
    assert [s["url"] for s in sources] == [
        "https://openusd.org/release/wp_rigid_body_physics.html#joint-limits-and-drives",
        "https://openusd.org/release/api/",  # second hit on the same page is skipped
        "https://github.com/NVIDIA-Omniverse/LearnOpenUSD/blob/main/docs/x.md",
    ]
    first = sources[0]
    assert first["title"] == "Rigid Body Physics in USD Proposal - Overall Design Concerns - USD Implementation"
    assert first["quote"] == "Joint limits and drives Limits restrict the joint motion."  # no markdown, no code
    assert doc_sources.parse_sources("no hits here") == []
    # Seen live: a chunk cut inside a link, and a hit whose text is only code.
    cut = "Title 'API', URL 'https://a/'\n---\nJoint Limits and Drives]( usdPhysics_limits_drives) - Articulations + [\n---"
    assert doc_sources.parse_sources(cut)[0]["quote"] == "Joint Limits and Drives - Articulations"
    code_only = "Title 'Box on Box', URL 'https://b/'\n---\n```usda\ndef Cube {}\n```\n---"
    assert doc_sources.parse_sources(code_only)[0]["quote"] == "(code example only: open the link)"


def test_attach_asks_once_per_code_and_adds_sources_to_every_matching_finding():
    asked = []

    def ask(queries):
        asked.extend(queries)
        return [{"ok": True, "content": ANSWER} for _ in queries]

    result = doc_sources.attach(_result("drive_target_outside_limits", "drive_target_outside_limits", "brand_new"), ask)
    assert asked == [doc_sources.QUERIES["drive_target_outside_limits"], "brand new: Because of brand_new"]
    assert all(len(f["sources"]) == 2 for f in result["findings"])
    assert result["docs"]["ok"] and result["docs"]["with_sources"] == 2


def test_attach_never_breaks_the_findings_when_the_doc_server_is_off():
    def down(queries):
        raise ConnectionError("refused")

    result = doc_sources.attach(_result("no_collider"), down)
    assert result["findings"][0]["code"] == "no_collider" and "sources" not in result["findings"][0]
    assert not result["docs"]["ok"] and "docker start usd-code-mcp" in result["docs"]["note"]

    failed = doc_sources.attach(_result("no_collider"), lambda q: [{"ok": False, "message": "Failed to query"}])
    assert not failed["docs"]["ok"] and "Failed to query" in failed["docs"]["note"]


def test_attach_limits_the_lookups_and_leaves_empty_or_failed_results_alone():
    codes = [f"code_{i}" for i in range(7)]
    result = doc_sources.attach(_result(*codes), lambda q: [{"ok": True, "content": ANSWER} for _ in q])
    assert result["docs"]["checked_codes"] == codes[:doc_sources.MAX_CODES] and "skipped" in result["docs"]
    assert "sources" not in result["findings"][-1]
    assert doc_sources.attach({"ok": True, "findings": []}) == {"ok": True, "findings": []}
    assert doc_sources.attach({"ok": False, "error": "x"}) == {"ok": False, "error": "x"}


def test_every_scene_diagnose_code_has_a_written_question():
    import re
    from pathlib import Path

    source = Path(features.function("scene_diagnose").__code__.co_filename).read_text(encoding="utf-8")
    codes = set(re.findall(r'_finding\("[a-z]+", "([a-z_]+)"', source))
    assert codes and codes <= set(doc_sources.QUERIES), sorted(codes - set(doc_sources.QUERIES))


def test_scene_diagnose_tool_has_a_docs_switch_that_runs_after_the_call():
    saved = features.invoke, doc_sources._default_ask
    try:
        _check_docs_switch()
    finally:
        features.invoke, doc_sources._default_ask = saved


def _check_docs_switch():
    tools = {}

    class App:
        def tool(self, name):
            def deco(fn):
                tools[name] = fn
                return fn
            return deco

    seen = {}
    def fake_invoke(name, arguments, **kw):
        seen[name] = dict(arguments)
        return {"ok": True, "findings": [{"code": "no_collider"}]}

    features.invoke = fake_invoke
    doc_sources._default_ask = lambda q: [{"ok": True, "content": ANSWER} for _ in q]
    features.register(App(), router=None, file_mode=lambda router, scene_file, fn: fn(object()))
    call = tools["scene_diagnose"]
    assert "docs" in call.__signature__.parameters and "docs" not in tools["graph_inspect"].__signature__.parameters
    plain = call(scene_file="x.usda", focus="physics")
    assert "sources" not in plain["findings"][0] and seen["scene_diagnose"] == {"focus": "physics"}  # docs not passed on
    with_docs = call(scene_file="x.usda", focus="physics", docs=True)
    assert with_docs["findings"][0]["sources"][0]["url"].startswith("https://openusd.org/")
