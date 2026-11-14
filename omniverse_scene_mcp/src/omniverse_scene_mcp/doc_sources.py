"""Attach NVIDIA doc sources to scene_diagnose findings (router-local, read-only, optional).

The local NVIDIA USD Code MCP (port 9903) indexes openusd.org, LearnOpenUSD and the USD physics docs; its
answers list each hit as "Title '...', URL '...'" followed by the text. For every distinct finding code we ask
one short question and attach the top sources, so a warning can say where the rule comes from. When the doc
server is off, findings stay as they are and a note says so. (Seen live 2026-11-14: the Isaac Sim doc MCP
only lists extensions for these questions, so it is not used here.)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import re
from typing import Any, Callable

MAX_CODES = 5
MAX_SOURCES = 2
QUOTE_CHARS = 220
TIMEOUT_S = 40.0

# One question per finding code, worded the way the docs talk about it.
QUERIES: dict[str, str] = {
    "drive_target_outside_limits": "joint drive target position outside the joint limits, limits and drives",
    "drive_moves_on_play": "joint drive target position stiffness damping, drive moves the joint",
    "drive_undamped": "joint drive damping zero stiffness oscillation",
    "drive_does_nothing": "joint drive stiffness and damping zero, drive has no effect",
    "joint_starts_outside_limits": "joint lower limit upper limit, joint starts outside its limits",
    "joint_starts_slid": "joint localPos0 localPos1 joint frames prismatic joint position",
    "joint_frames_misaligned": "joint localPos0 localPos1 localRot0 localRot1 joint frames must line up",
    "joint_no_bodies": "physics joint body0 body1 relationships",
    "limits_reversed": "joint lower limit greater than upper limit",
    "no_articulation_root": "articulation root API on a joint tree",
    "nested_rigid_body": "rigid body nested under another rigid body hierarchy",
    "no_collider": "rigid body without collision API falls through the ground",
    "no_physics_scene": "physics scene prim required for simulation gravity",
    "kinematic": "kinematic rigid body kinematicEnabled",
    "kinematic_velocity": "kinematic body velocity is ignored, move kinematic bodies with transforms",
    "negative_mass": "mass API mass density must be positive",
    "overlap": "rigid bodies overlapping at start collision",
    "units": "metersPerUnit stage units physics scale",
    "invisible": "visibility invisible imageable prim",
    "missing_asset": "asset path does not resolve, unresolved reference",
    "wrong_prim_path": "prim path does not exist, relationship target path",
}

_HIT = re.compile(r"Title '(.*?)', URL '([^']+)'\s*\n---\n(.*?)(?=\n---|\Z)", re.S)


def question(finding: dict[str, Any]) -> str:
    code = str(finding.get("code") or "")
    if code in QUERIES:
        return QUERIES[code]
    cause = str(finding.get("cause") or "").split(".")[0]
    return f"{code.replace('_', ' ')}: {cause}"[:200]


def _clean(text: str) -> str:
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\]\([^)]*\)", " ", text)  # half links cut by the doc chunker (seen live)
    text = re.sub(r"[#>*`|\[\]+]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_sources(content: str, limit: int = MAX_SOURCES) -> list[dict[str, str]]:
    """Top hits of a USD Code MCP answer as {title, url, quote}; one per URL page."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for title, url, body in _HIT.findall(content or ""):
        if url in seen:  # the same page often comes back several times (e.g. openusd.org/release/api/)
            continue
        seen.add(url)
        short_title = title.split(" - ")
        name = " - ".join(short_title[:3]) if len(short_title) > 1 else title
        quote = _clean(body) or "(code example only: open the link)"
        if len(quote) > QUOTE_CHARS:
            quote = quote[:QUOTE_CHARS].rsplit(" ", 1)[0] + " ..."
        out.append({"title": name[:120], "url": url, "quote": quote})
        if len(out) >= limit:
            break
    return out


def _default_ask(queries: list[str]) -> list[dict[str, Any]]:
    from .knowledge import build_usd_backend

    async def run() -> list[dict[str, Any]]:
        backend = build_usd_backend()
        return list(await asyncio.gather(*(backend.ask(q) for q in queries)))

    # The router's tool runs inside an event loop, so the lookups get their own loop in a worker thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, run()).result(timeout=TIMEOUT_S)


def attach(result: dict[str, Any], ask: Callable[[list[str]], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Add 'sources' to each finding (first MAX_CODES distinct codes). Never raises; never removes a finding."""
    findings = result.get("findings") if isinstance(result, dict) else None
    if not result.get("ok") or not isinstance(findings, list) or not findings:
        return result
    codes: list[str] = []
    for item in findings:
        code = item.get("code")
        if isinstance(code, str) and code not in codes:
            codes.append(code)
    codes = codes[:MAX_CODES]
    first = {code: next(f for f in findings if f.get("code") == code) for code in codes}
    queries = [question(first[code]) for code in codes]
    try:
        answers = (ask or _default_ask)(queries)
    except Exception as exc:  # timeout, server off, network: the scene findings still stand
        result["docs"] = {"ok": False, "note": f"NVIDIA doc lookup failed ({type(exc).__name__}); findings unchanged. "
                                                "Start the doc servers: docker start usd-code-mcp"}
        return result
    found = 0
    by_code: dict[str, list[dict[str, str]]] = {}
    for code, answer in zip(codes, answers):
        sources = parse_sources(answer.get("content", "")) if isinstance(answer, dict) and answer.get("ok") else []
        by_code[code] = sources
        found += bool(sources)
    for item in findings:
        if by_code.get(item.get("code")):
            item["sources"] = by_code[item["code"]]
    errors = [a.get("message") for a in answers if isinstance(a, dict) and not a.get("ok") and a.get("message")]
    result["docs"] = {
        "ok": found > 0,
        "checked_codes": codes,
        "with_sources": found,
        "note": ("Sources are the top NVIDIA USD doc hits for each problem: read them to confirm the rule, they are "
                 "search results, not proof." if found else
                 "No doc sources found" + (f": {errors[0]}" if errors else ".") +
                 " Is the doc server running? docker start usd-code-mcp"),
    }
    if len(codes) < len({f.get('code') for f in findings}):
        result["docs"]["skipped"] = f"Sources only for the first {MAX_CODES} kinds of problem."
    return result
