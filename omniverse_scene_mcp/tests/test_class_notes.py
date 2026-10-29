from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

from omniverse_scene_mcp.class_notes import (
    ENV_ROOT,
    PASSAGE_CHARS,
    SRT_CAVEAT,
    TIMELINE_NOTE,
    ClassNotes,
    Cue,
    SearchHits,
    default_roots,
    format_timestamp,
    merge_cues,
    parse_gemini,
    parse_srt,
)

SRT = """1
00:00:01,000 --> 00:00:03,500
Teacher: Today we build a warehouse.

2
00:00:04,000 --> 00:00:06,000
First create an x form
called Environment.

3
00:00:06,500 --> 00:00:09,000
Then add a ground plane under it.

4
00:01:00,000 --> 00:01:02,000
After the break: lights and cameras.

5
00:01:02,500 --> 00:01:04,000
Use a rect light for the shelves.
"""

GEMINI = """﻿Please rate the new Quick notes tab by taking a short survey.
Mentorship-Session-03


Oct 15, 2026


Warehouse lighting basics
* Rect lights brighten shelves without harsh shadows.
* Distant light should be hidden for indoor scenes.


Summary
Lights and cameras were configured for the warehouse.


Details
Ground Plane Setup: yatish explained adding a physics ground plane under the Environment Xform (00:07:30).
________________


You should review Gemini's notes to make sure they're accurate.
Oct 15, 2026
Mentorship-Session-03 - Transcript
00:00:05


yatish kumavat: Good morning everyone.
lakshay sharma: Morning.


00:07:20


yatish kumavat: Now create an Xform named Environment and then a ground plane inside it.
lakshay sharma: Do we use the physics ground plane?
yatish kumavat: Yes, the physics ground plane gives collision.


Transcription ended after 00:09:40


This editable transcript was computer generated and might contain errors.
"""

REVISION = """# Session 03 - Lights

## 1. Rect lights

Rect lights light the shelves evenly. Compare with the subtitles at 00:01:02.

## 2. Ground plane

The physics ground plane adds collision; a plain plane is only visual geometry.
"""

CONTEXT = """# Course

## Session 03 sources

The Session 03 recording and the Gemini export are kept in Downloads.

## Course-wide tips

Always save the stage before closing the application window.
"""

GEMINI_NAME = "(Gemini)Mentorship-Session-03 - 2026_10_15 11_00 IST - Notes by Gemini.txt"


def _write(path: Path, text: str, newline: str = "\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def _fixture(tmp: str) -> tuple[Path, Path]:
    classes = Path(tmp) / "classes"
    mentor = Path(tmp) / "mentorship"
    session = classes / "Session 3"
    _write(session / "session-3(Lights)_compressed.srt", SRT, newline="\r\n")
    _write(session / GEMINI_NAME, GEMINI)
    (session / "notes.pdf").write_bytes(b"%PDF-1.4 binary")
    (session / "lecture.mp4").write_bytes(b"\x00\x00")
    (classes / "Session 4").mkdir(parents=True)
    _write(mentor / "session03" / "REVISION_NOTES.md", REVISION)
    _write(mentor / "PROJECT_CONTEXT.md", CONTEXT)
    return classes, mentor


# --- parsing -------------------------------------------------------------------------------------------------------------


def test_parse_srt_cues_bom_crlf_and_multiline() -> None:
    cues = parse_srt("﻿" + SRT.replace("\n", "\r\n"))
    assert len(cues) == 5
    assert cues[0] == Cue(1.0, 3.5, "Teacher: Today we build a warehouse.")
    assert cues[1] == Cue(4.0, 6.0, "First create an x form called Environment.")
    assert cues[4].start == 62.5 and cues[4].end == 64.0
    assert parse_srt("") == []
    # An index line is optional and empty cues are dropped.
    assert parse_srt("00:00:01,5 --> 00:00:02,000\nHi\n\n2\n00:00:03,000 --> 00:00:04,000\n\n") == [Cue(1.5, 2.0, "Hi")]


def test_format_timestamp() -> None:
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(3725.9) == "01:02:05"
    assert format_timestamp(None) is None


def test_merge_cues_stops_at_gaps_and_size_limit() -> None:
    cues = parse_srt(SRT)
    passages = merge_cues(cues)
    assert [(p["first"], p["last"]) for p in passages] == [(0, 2), (3, 4)]
    assert passages[0]["timestamp"] == "00:00:01" and passages[0]["end_timestamp"] == "00:00:09"
    assert passages[0]["text"] == (
        "Teacher: Today we build a warehouse. First create an x form called Environment. Then add a ground plane under it."
    )
    assert passages[1]["timestamp"] == "00:01:00"
    assert passages[1]["text"] == "After the break: lights and cameras. Use a rect light for the shelves."
    # 36 + 1 + 42 > 60, so every cue becomes its own passage.
    assert [(p["first"], p["last"]) for p in merge_cues(cues[:3], limit=60)] == [(0, 0), (1, 1), (2, 2)]
    # A tighter silence limit splits passage one at the 0.5 s gaps.
    assert [(p["first"], p["last"]) for p in merge_cues(cues[:3], max_gap=0.4)] == [(0, 0), (1, 1), (2, 2)]


def test_merge_cues_long_recording_stays_under_limit() -> None:
    cues = [Cue(i * 2.0, i * 2.0 + 1.5, f"cue number {i} talks about lights and cameras") for i in range(200)]
    passages = merge_cues(cues)
    assert all(len(p["text"]) <= PASSAGE_CHARS for p in passages)
    assert passages[0]["first"] == 0 and passages[-1]["last"] == 199
    assert all(b["first"] == a["last"] + 1 for a, b in zip(passages, passages[1:]))
    assert len(passages) < 30  # merged, not one passage per cue


def test_parse_gemini_notes_and_transcript() -> None:
    parsed = parse_gemini(GEMINI)
    assert parsed["date"] == "2026-10-15"
    assert parsed["duration"] == 580.0
    assert [(n["part"], n["timestamp"]) for n in parsed["notes"]] == [
        ("quick_notes", None), ("summary", None), ("details", "00:07:30"),
    ]
    assert parsed["notes"][0]["text"].startswith("Warehouse lighting basics * Rect lights brighten shelves")
    assert all("Please rate" not in n["text"] and "review Gemini" not in n["text"] for n in parsed["notes"])
    assert [(t["timestamp"], len(t["lines"])) for t in parsed["transcript"]] == [("00:00:05", 2), ("00:07:20", 3)]
    assert parsed["transcript"][1]["lines"][2] == "yatish kumavat: Yes, the physics ground plane gives collision."


def test_parse_gemini_without_footer_uses_last_marker() -> None:
    parsed = parse_gemini("Notes\n\n00:01:00\nA: hello there\n00:02:30\nB: bye now\n")
    assert parsed["duration"] == 150.0
    assert parsed["date"] is None
    assert [t["timestamp"] for t in parsed["transcript"]] == ["00:01:00", "00:02:30"]


# --- index -----------------------------------------------------------------------------------------------------------------


def test_sessions_sources_durations_dates_and_skips() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        classes, mentor = _fixture(tmp)
        notes = ClassNotes([classes, mentor])
        sessions = notes.sessions()
        assert [s["session"] for s in sessions] == [3, 4]
        three, four = sessions
        assert three["sources"] == ["subtitles", "gemini_notes", "revision_notes"]
        assert three["duration"] == {"subtitles": "00:01:04", "gemini_notes": "00:09:40"}
        assert three["date"] == "2026-10-15"
        assert three["skipped"] == ["notes.pdf"]
        assert {f["file"] for f in three["files"]} == {
            "session-3(Lights)_compressed.srt", GEMINI_NAME, "REVISION_NOTES.md", "PROJECT_CONTEXT.md",
        }
        assert four == {"session": 4, "label": "Session 4", "sources": [], "files": [], "duration": {}, "date": None,
                        "skipped": []}
        stats = notes.stats()
        assert stats["files"] == {"subtitles": 1, "gemini_notes": 1, "revision_notes": 2}
        assert stats["skipped"][0]["file"] == "notes.pdf" and stats["skipped"][0]["reason"].startswith("PDF")
        assert stats["index_ms"] >= 0 and stats["passages"] > 0
        json.dumps(stats)


def test_search_ground_plane_returns_both_timelines_with_note() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        notes = ClassNotes(_fixture(tmp))
        hits = notes.search("ground plane", top_k=10)
        assert isinstance(hits, SearchHits) and isinstance(hits, list)
        assert hits.timeline_note == TIMELINE_NOTE
        by_source = {}
        for hit in hits:
            by_source.setdefault(hit["source"], []).append(hit)
        assert set(by_source) == {"subtitles", "gemini_notes", "revision_notes"}
        assert [(h["timestamp"], h["timeline"]) for h in by_source["subtitles"]] == [("00:00:01", "compressed_srt")]
        assert by_source["subtitles"][0]["text"].endswith("Then add a ground plane under it.")
        gemini = {(h["section"], h["timestamp"]) for h in by_source["gemini_notes"]}
        assert gemini == {("transcript", "00:07:20"), ("details", "00:07:30")}
        assert all(h["timeline"] == "gemini_transcript" for h in by_source["gemini_notes"])
        assert all(h["timestamp"] is None and h["timeline"] is None for h in by_source["revision_notes"])
        assert all(len(h["text"]) <= PASSAGE_CHARS for h in hits)
        assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
        assert hits.as_dict()["timeline_note"] == TIMELINE_NOTE
        json.dumps(hits.as_dict())


def test_xform_spellings_are_unified() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        notes = ClassNotes(_fixture(tmp))
        hits = notes.search("Xform", top_k=10)
        sources = {(h["source"], h["timestamp"]) for h in hits}
        assert ("subtitles", "00:00:01") in sources  # the SRT says "x form"
        assert ("gemini_notes", "00:07:20") in sources


def test_single_source_hits_have_no_timeline_note() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        notes = ClassNotes(_fixture(tmp))
        hits = notes.search("break")
        assert [(h["source"], h["timestamp"]) for h in hits] == [("subtitles", "00:01:00")]
        assert hits.timeline_note is None
        assert notes.search("   ") == [] and notes.search("the of and") == []


def test_session_filter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        notes = ClassNotes(_fixture(tmp))
        assert notes.search("ground plane", session=4) == []
        for value in (3, "3", "Session 3", "session03"):
            hits = notes.search("ground plane", session=value)
            assert hits and all(h["session"] == 3 for h in hits)
        # Course-wide PROJECT_CONTEXT sections have no session and are excluded by a session filter.
        assert [h["session"] for h in notes.search("save stage closing application")] == [None]
        assert notes.search("save stage closing application", session=3) == []
        # A "Session 03 ..." heading inside PROJECT_CONTEXT.md belongs to that session.
        hit = notes.search("Downloads export recording")[0]
        assert (hit["session"], hit["file"], hit["section"]) == (3, "PROJECT_CONTEXT.md", "Course > Session 03 sources")


def test_ranking_prefers_phrase_and_term_frequency() -> None:
    srt = (
        "1\n00:00:01,000 --> 00:00:03,000\nWe add a plane for the floor.\n\n"
        "2\n00:01:00,000 --> 00:01:03,000\nThe ground plane, the physics ground plane, adds collision.\n\n"
        "3\n00:02:00,000 --> 00:02:03,000\nNow we talk about cameras.\n\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp) / "Session 1" / "s1.srt", srt)
        hits = ClassNotes([tmp]).search("ground plane")
        assert [h["timestamp"] for h in hits] == ["00:01:00", "00:00:01"]
        assert hits[0]["score"] > hits[1]["score"] > 0
        assert hits.timeline_note is None


def test_overlapping_windows_are_not_returned_twice() -> None:
    cues = "".join(
        f"{i + 1}\n00:00:{i * 2:02d},000 --> 00:00:{i * 2 + 1:02d},000\nsentence {i} mentions the rect light and lots of padding words here\n\n"
        for i in range(25)
    )
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp) / "Session 1" / "s1.srt", cues)
        hits = ClassNotes([tmp]).search("rect light", top_k=50)
        assert len(hits) >= 2
        # Each cue appears in at most one returned passage.
        seen = [w for h in hits for w in h["text"].split() if w.isdigit()]
        assert len(seen) == len(set(seen)) == 25


def test_known_srt_mistranscription_caveats() -> None:
    srt = (
        "1\n00:42:11,000 --> 00:42:16,000\nUSDC is the readable text format and USDA is binary.\n\n"
        "2\n01:12:29,000 --> 01:12:32,000\nWe only play a simulation, so everything will be standard.\n\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        _write(Path(tmp) / "Session 2" / "s2.srt", srt)
        _write(Path(tmp) / "Session 5" / "s5.srt", srt)
        notes = ClassNotes([tmp])
        usd = notes.search("USDA USDC", session=2)[0]
        assert "swap USDA and USDC" in usd["caveat"]
        play = notes.search("play simulation", session=2)[0]
        assert "not to play the simulation" in play["caveat"]
        assert all("caveat" not in h for h in notes.search("USDA play simulation", session=5))


def test_ask_groups_by_session_and_hints() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        classes, mentor = _fixture(tmp)
        _write(classes / "Session 4" / "s4.srt", "1\n00:10:00,000 --> 00:10:04,000\nThe ground plane again in session four.\n")
        notes = ClassNotes([classes, mentor])
        answer = notes.ask("how do I add a physics ground plane", top_k=8)
        assert answer["ok"] is True
        groups = answer["answer_passages"]
        assert {g["session"] for g in groups} == {3, 4}
        assert all(p["session"] == g["session"] for g in groups for p in g["passages"])
        assert sum(len(g["passages"]) for g in groups) <= 8
        best = groups[0]["passages"][0]
        assert answer["summary_hint"].startswith(f"Start with Session {best['session']}'s") or "revision notes" in answer["summary_hint"]
        assert answer["summary_hint"].count(".") >= 1 and "\n" not in answer["summary_hint"]
        assert TIMELINE_NOTE in answer["note"] and SRT_CAVEAT in answer["note"] and "PDF" in answer["note"]
        json.dumps(answer)
        only_srt = notes.ask("again in session four")
        assert only_srt["summary_hint"] == "Start with Session 4's subtitles at 00:10:00 (compressed-recording timeline)."
        assert TIMELINE_NOTE not in (only_srt["note"] or "")
        gemini = notes.ask("good morning everyone")
        assert gemini["summary_hint"] == "Start with Session 3's Gemini transcript at 00:00:05 (Gemini timeline)."
        nothing = notes.ask("quaternion spline")
        assert nothing["ok"] is True and nothing["answer_passages"] == []
        assert notes.ask("")["ok"] is False


def test_missing_roots_and_non_gemini_txt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = ClassNotes([Path(tmp) / "nope"])
        assert missing.sessions() == [] and missing.search("ground plane") == []
        answer = missing.ask("ground plane")
        assert answer["ok"] is False and "No class sources" in answer["error"]
        _write(Path(tmp) / "Session 7" / "todo.txt", "buy milk and eggs for the ground plane party")
        notes = ClassNotes([tmp])
        assert notes.search("ground plane") == []
        assert notes.stats()["skipped"] == [{"file": "todo.txt", "reason": "not a Gemini notes export"}]


def test_env_root_override_and_lazy_refresh() -> None:
    old = os.environ.get(ENV_ROOT)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[ENV_ROOT] = tmp
            roots = default_roots()
            assert roots[0] == Path(tmp) and roots[-1].name == "isaacsim-mentorship"
            notes = ClassNotes([tmp])
            assert notes.search("ground plane") == []
            _write(Path(tmp) / "Session 1" / "s1.srt", "1\n00:00:01,000 --> 00:00:02,000\nground plane\n")
            assert notes.search("ground plane") == []  # index is built once, lazily
            assert notes.refresh()["files"]["subtitles"] == 1
            assert notes.search("ground plane")[0]["timestamp"] == "00:00:01"
    finally:
        if old is None:
            os.environ.pop(ENV_ROOT, None)
        else:
            os.environ[ENV_ROOT] = old


def test_sources_are_never_modified() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        classes, mentor = _fixture(tmp)
        before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in Path(tmp).rglob("*") if p.is_file()}
        notes = ClassNotes([classes, mentor])
        notes.ask("ground plane")
        notes.sessions()
        after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in Path(tmp).rglob("*") if p.is_file()}
        assert before == after


def test_real_class_folder_smoke() -> None:
    real = Path.home() / "Downloads" / "Issac Sim Classes"
    if not real.is_dir():
        return
    notes = ClassNotes([real])
    stats = notes.stats()
    assert stats["files"]["subtitles"] >= 1 and stats["files"]["gemini_notes"] >= 1
    assert stats["index_ms"] < 5000
    for query in ("Xform", "ground plane"):
        hits = notes.search(query, top_k=8)
        assert hits, query
        assert any(h["timestamp"] and len(h["timestamp"]) == 8 for h in hits), query
        assert all(len(h["text"]) <= PASSAGE_CHARS for h in hits)
        if {h["source"] for h in hits} >= {"subtitles", "gemini_notes"}:
            assert hits.timeline_note == TIMELINE_NOTE
    assert notes.ask("ground plane")["ok"] is True


def test_downloads_is_read_flat_session_named_files_only() -> None:
    # Seen 2026-10-29: Sessions 3-6 were saved straight into Downloads, so class_ask could not see them.
    with tempfile.TemporaryDirectory() as tmp:
        downloads = Path(tmp) / "Downloads"
        (downloads / "unrelated" / "Session 9").mkdir(parents=True)
        (downloads / "Session6(Action Graph Fundamentals)_zoomed_HD.srt").write_text(SRT, encoding="utf-8")
        (downloads / "Session6(Action Graph Fundamentals)_zoomed_HD (1).srt").write_text(SRT, encoding="utf-8")
        (downloads / "Session6(Action Graph Fundamentals) - Notes by Gemini.txt").write_text(GEMINI, encoding="utf-8")
        (downloads / "holiday.srt").write_text(SRT, encoding="utf-8")
        (downloads / "unrelated" / "Session 9" / "deep.srt").write_text(SRT, encoding="utf-8")
        notes = ClassNotes([downloads])
        files = {f["file"] for s in notes.sessions() for f in s["files"]}
        assert files == {"Session6(Action Graph Fundamentals)_zoomed_HD.srt",
                         "Session6(Action Graph Fundamentals) - Notes by Gemini.txt"}, files
        assert [s["session"] for s in notes.sessions()] == [6]
