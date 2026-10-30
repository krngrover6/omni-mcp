"""Class quiz: fill-in-the-blank questions made from YOUR teacher's own sentences (class transcripts, .srt).

Each question is a real sentence from the recording with one key term blanked out ("A ____ can be unloaded so
heavy assets only load when needed"), four choices (the right term plus three from the same exam topic) and the
time in the recording to rewatch. Answers are saved to your exam progress (source "class_quiz"), so wrong ones
come back in spaced repetition and appear in the mistake notebook. Question ids encode the transcript sentence,
so nothing is stored to remember the quiz.

Auto-generated subtitles mishear words, so a question that reads oddly can be skipped: the meaning shown after
answering is the reliable part.
"""

from __future__ import annotations

import random
import re
import string
from typing import Any

from .. import session_lessons as sl
from ..class_notes import format_timestamp

MIN_CHARS, MAX_CHARS = 40, 260
MAX_COUNT = 15
SOURCE = "class_quiz"
_ID = re.compile(r"^class:(\d{1,3}):(\d{1,5}):([a-z][a-z \-]*)$")
_EXPLAINS = re.compile(r"\b(?:is|are|means|lets|allows|can|will|use|uses|used|called|helps|gives|makes)\b", re.I)
_CAPITAL = re.compile(r"(?<![.!?]\s)(?<!^)\b[A-Z][a-z]+\b")


MIN_WORDS = 8
REJECT = -100
# Words that carry no meaning after the blank: "the ____ which are over here" says nothing about the term.
_VAGUE = frozenset("here there this that these those one ones thing things stuff everything all and so on over "
                   "which who what is are was were be it its they them the a an of in to just right now okay ok "
                   "like you we guys etc whatever i think guess as well too also first directly".split())


def _vague_after_blank(text: str, term: str) -> bool:
    """True when the words after the term are only filler ("over here", "this one", "and everything")."""
    regex = sl._TERM_RE.get(term)
    match = regex.search(text) if regex is not None else None
    if match is None:
        return False
    tail = re.findall(r"[a-z']+", text[match.end():].lower())
    return bool(tail) and all(word in _VAGUE for word in tail)


def _quality(text: str, term: str) -> int:
    """Higher for clear, explaining sentences; lower for misheard subtitles (stray capitalised words).

    Seen live in Composer: "Do not try to use the ____ which are over here." made a meaningless question, so a
    sentence needs MIN_WORDS words, an explaining verb, and something other than filler after the blank.
    """
    if len(text.split()) < MIN_WORDS or not _EXPLAINS.search(text) or _vague_after_blank(text, term):
        return REJECT
    score = 2
    score += 1 if 60 <= len(text) <= 200 else 0
    stray = [w for w in _CAPITAL.findall(text) if w.lower() not in term.lower() and w not in ("I", "USD", "Isaac")]
    return score - len(stray)


def _sentences(session: int) -> tuple[list[dict[str, Any]], str]:
    path = sl._resolve(session, None, None)
    return sl._sentences(sl._load_cues(path)), path.name


def _blank(text: str, term: str) -> str | None:
    regex = sl._TERM_RE[term]
    match = regex.search(text)
    # Some term patterns also catch everyday words (e.g. 'kind' for a term): blank only real mentions of the term
    if match is None or not match.group().lower().replace(' ', '')[:4] == term.lower().replace(' ', '')[:4]:
        return None
    return (text[:match.start()] + "____" + text[match.end():]).strip()


def _options(term: str, qid: str) -> list[str]:
    domain = sl._TERMS[term][2]
    pool = sorted(t for t, spec in sl._TERMS.items() if t != term and spec[2] == domain)
    pool += sorted(t for t in sl._TERMS if t != term and t not in pool)
    rng = random.Random(qid)  # same id -> same choices, in the same order
    options = rng.sample(pool[:8], 3) + [term]
    rng.shuffle(options)
    return options


def _question(session: int, index: int, term: str, sentences: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not 0 <= index < len(sentences) or term not in sl._TERMS:
        return None
    sentence = sentences[index]
    stem = _blank(sl.clean_text(sentence["text"]), term)
    if stem is None:
        return None
    qid = f"class:{session}:{index}:{term}"
    options = _options(term, qid)
    return {"id": qid, "session": session, "question": stem,
            "options": [f"{string.ascii_uppercase[i]}. {o}" for i, o in enumerate(options)],
            "rewatch_at": format_timestamp(sentence.get("start")), "_answer": options.index(term),
            "_term": term, "_sentence": sentence["text"]}


def class_quiz(session: int, count: int = 5, seed: int | None = None) -> dict[str, Any]:
    """Read-only: `count` (1-15) fill-in-the-blank questions from a class session's transcript. Answer each with
    learning_class_quiz_answer(id, choice)."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_COUNT:
        return {"ok": False, "error": f"count must be a whole number from 1 to {MAX_COUNT}."}
    try:
        sentences, name = _sentences(session)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    candidates = []
    for index, sentence in enumerate(sentences):
        text = sl.clean_text(sentence["text"])
        if not MIN_CHARS <= len(text) <= MAX_CHARS:
            continue
        found = [t for t, regex in sl._TERM_RE.items() if regex.search(text)]
        if len(found) == 1 and _quality(text, found[0]) >= 2 and _blank(text, found[0]):  # one clear term
            candidates.append((index, found[0]))
    if not candidates:
        return {"ok": False, "error": f"Session {session}'s transcript has no sentences with a key USD/Isaac term."}
    rng = random.Random(seed)
    by_term: dict[str, list[int]] = {}
    for index, term in candidates:
        by_term.setdefault(term, []).append(index)
    terms = list(by_term)
    rng.shuffle(terms)
    picked = []
    while len(picked) < count and terms:  # spread the questions over different terms first
        for term in list(terms):
            pool = by_term[term]
            if not pool:
                terms.remove(term)
                continue
            picked.append((pool.pop(rng.randrange(len(pool))), term))
            if len(picked) == count:
                break
    questions = []
    for index, term in picked:
        question = _question(int(session), index, term, sentences)
        if question:
            questions.append({k: v for k, v in question.items() if not k.startswith("_")})
    return {"ok": True, "read_only": True, "session": session, "transcript": name, "count": len(questions),
            "questions": questions,
            "how_to_answer": "learning_class_quiz_answer(id='<id>', choice='B'). Answers are saved to your exam "
                             "progress; wrong ones come back later and show in learning_exam_mistakes.",
            "note": "Sentences come from auto-generated subtitles: if one reads oddly, skip it."}


def rebuild(qid: str) -> dict[str, Any] | None:
    """The full question (answer included) for a class quiz id, or None."""
    match = _ID.match(qid or "")
    if not match:
        return None
    session, index, term = int(match[1]), int(match[2]), match[3]
    try:
        sentences, _ = _sentences(session)
    except (ValueError, OSError):
        return None
    return _question(session, index, term, sentences)


def class_quiz_answer(question_id: str, choice: str, store: Any = None) -> dict[str, Any]:
    """Grade one class quiz answer (choice: a letter, A-D) and save it to your exam progress."""
    question = rebuild(question_id)
    if question is None:
        return {"ok": False, "error": "Unknown class quiz id; get questions from learning_class_quiz(session)."}
    letter = str(choice).strip().upper()[:1]
    if letter not in "ABCD" or not letter:
        return {"ok": False, "error": "choice must be a letter A-D."}
    chosen = string.ascii_uppercase.index(letter)
    right = chosen == question["_answer"]
    term = question["_term"]
    _, meaning, domain, _ = sl._TERMS[term]
    if store is None:
        from .progress import ProgressStore

        store = ProgressStore()
    saved = store.record_answer(question_id, domain=domain or "Class", objective_ids=[], chosen=letter,
                                correct=string.ascii_uppercase[question["_answer"]], is_correct=right,
                                source=SOURCE)
    return {"ok": True, "id": question_id, "correct": right,
            "right_answer": question["options"][question["_answer"]],
            "you_chose": question["options"][chosen],
            "meaning": f"{term}: {meaning}",
            "teacher_said": sl.clean_text(question["_sentence"]),
            "rewatch_at": question["rewatch_at"],
            "progress": {"saved": bool(saved), "next_review": (saved or {}).get("card", {}).get("due_at")
                         if isinstance(saved, dict) else None},
            "message": "Right!" if right else f"Not quite: the blank is '{term}'. Rewatch the class at "
                                              f"{question['rewatch_at']} and read the meaning above."}
