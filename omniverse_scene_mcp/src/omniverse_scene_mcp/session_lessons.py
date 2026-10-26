"""Turn a teacher's class transcript (.srt) into short lessons, a quiz and accuracy flags.

Read-only and offline: transcripts are found in the Downloads folder and the class folder, parsed with
class_notes.parse_srt, cut into ~15 minute blocks at natural pauses, and described for a beginner:
key USD/Isaac terms with plain meanings, the main points with timestamps, "do this in the app" steps,
check questions, matching exam-bank questions and flags where a sentence conflicts with USD facts.
Auto-generated subtitles mishear words, so every flag says "check this", never "the teacher is wrong".
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable

from .class_notes import Cue, format_timestamp, parse_srt

DEFAULT_MINUTES = 15
MIN_MINUTES, MAX_MINUTES = 5, 60
ENV_DIRS = "OMNIVERSE_CLASS_TRANSCRIPTS"
MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024
MAX_SCAN_DEPTH = 3
MAX_POINTS = 6
MAX_STEPS = 6
MAX_SENTENCE = 260
EXCERPT_CHARS = 1500
CUT_SEARCH_SECONDS = 120.0  # after the target length, look this long for a natural pause
PAUSE_SECONDS = 1.5

_SESSION = re.compile(r"session[\s_\-]*0*(\d{1,3})(?!\d)", re.I)
_TITLE = re.compile(r"\(([^()]{3,80})\)")
_SENTENCE_END = re.compile(r"[.!?]$")

# Subtitle mishearings, fixed before anything else reads the text (all patterns are short and linear).
_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:x|ex)[\s\-]?forms?\b", re.I), "Xform"),
    (re.compile(r"\bground[\s\-]?planes?\b", re.I), "ground plane"),
    (re.compile(r"\bU\.?S\.?D\.?\s?A\b"), "USDA"),
    (re.compile(r"\bU\.?S\.?D\.?\s?C\b"), "USDC"),
    (re.compile(r"\bU\.?S\.?D\.?\s?Z\b"), "USDZ"),
    # "USD a file" / "usd c format": a lone letter after USD, followed by a format word, is the file extension.
    (re.compile(r"\busd\s([acz])\b(?=\s+(?:files?|formats?|extensions?|is|are|was)\b)", re.I), None),
    (re.compile(r"\busd([acz])\b", re.I), None),
    (re.compile(r"\bprems?\b", re.I), "prim"),
    (re.compile(r"\bisaac\s?sim\b|\bisaacsim\b|\bisaac seem\b", re.I), "Isaac Sim"),
    (re.compile(r"\bomni\s?verse\b", re.I), "Omniverse"),
    (re.compile(r"\bnew\s?cleus\b|\bnucleous\b", re.I), "Nucleus"),
    (re.compile(r"\bsim\s?ready\b", re.I), "SimReady"),
)

# term: (pattern, plain meaning, exam domain or None, weight). Generic words get a low weight for titles.
_TERMS: dict[str, tuple[str, str, str | None, float]] = {
    "stage": (r"\bstages?\b", "The whole composed scene you see: every layer combined into one result.", "Data Modeling", 0.4),
    "prim": (r"\bprims?\b", "One object in the scene tree (a cube, a light, a group). Its address is a path like /World/Box.", "Data Modeling", 0.5),
    "layer": (r"\blayers?\b", "One USD file of opinions. A stage stacks layers; stronger layers win.", "Composition", 1.0),
    "sublayer": (r"\bsub[\s\-]?layers?\b", "A layer stacked under another layer. Earlier (higher) layers in the list are stronger.", "Composition", 1.2),
    "reference": (r"\breferenc(?:e|es|ed|ing)\b", "Brings another file's prims into yours. You can still override them locally.", "Composition", 1.3),
    "payload": (r"\bpayloads?\b", "A reference that can be loaded or unloaded, so heavy assets only load when needed.", "Composition", 1.4),
    "variant": (r"\bvariants?\b|\bvariant sets?\b", "Switchable versions of a prim (e.g. colour Red/Blue). One variant per set is selected.", "Composition", 1.3),
    "opinion": (r"\bopinions?\b|\boverrid(?:e|es|ing|den)\b", "A value written in a layer. When layers disagree, the strongest opinion wins.", "Composition", 1.2),
    "LIVRPS": (r"\blivrps\b|\bstrength order(?:ing)?\b", "Strength order: Local, Inherits, Variants, References, Payloads, Specializes.", "Composition", 1.5),
    "Xform": (r"\bXforms?\b", "A transform prim: it moves, rotates and scales itself and everything under it.", "Data Modeling", 1.0),
    "transform": (r"\btranslat(?:e|ion)\b|\brotat(?:e|ion)\b|\bscal(?:e|ing)\b", "Move (translate), turn (rotate) and resize (scale) values on an Xform.", "Data Modeling", 0.7),
    "mesh": (r"\bmesh(?:es)?\b", "Geometry made of points and faces.", "Data Modeling", 0.8),
    "attribute": (r"\battributes?\b|\bpropert(?:y|ies)\b", "A named value on a prim, such as size or color.", "Data Modeling", 0.8),
    "material": (r"\bmaterials?\b|\bshaders?\b", "Describes how a surface looks (colour, roughness). It is bound to prims.", "Visualization", 1.0),
    "MDL": (r"\bMDL\b|\bOmniPBR\b", "NVIDIA's material language (OmniPBR etc.), used by Omniverse renderers.", "Visualization", 1.0),
    "light": (r"\b(?:dome|rect|distant|sphere|disk)\s?lights?\b|\blights?\b|\blighting\b", "Lights (dome, rect, distant, sphere) make the scene visible.", "Visualization", 0.8),
    "camera": (r"\bcameras?\b", "A viewpoint prim with lens settings; you render or look through it.", "Visualization", 0.8),
    "asset": (r"\bassets?\b", "A reusable USD file (a chair, a robot) you bring into scenes.", "Content Aggregation", 0.9),
    "asset structure": (r"\bdefault prim\b|\bdefaultPrim\b|\bkind\b|\bcomponent\b|\bassembly\b", "How an asset file is organised: a default prim, model kinds, clean hierarchy.", "Content Aggregation", 1.2),
    "instancing": (r"\binstanc(?:e|es|ing|eable)\b", "Many copies share one prototype, saving memory. You cannot edit inside an instance.", "Content Aggregation", 1.3),
    "file formats": (r"\bUSD[ACZ]\b|\.usd[acz]?\b", "USDA = text, USDC = binary (fast), USDZ = zipped package.", "Data Exchange", 1.2),
    "units": (r"\bmeters? per unit\b|\bmetersPerUnit\b|\bup[\s\-]?axis\b|\bunits?\b", "Scene scale and orientation (metersPerUnit, upAxis). Isaac uses metres and Z-up.", "Data Exchange", 1.1),
    "physics": (r"\bphysics\b|\bphysx\b|\bsimulat(?:e|ion|ing)\b", "Simulated motion and contact, computed by PhysX when you press Play.", "Customizing USD", 0.9),
    "rigid body": (r"\brigid[\s\-]?bod(?:y|ies)\b", "Makes a prim move under physics (gravity, forces).", "Customizing USD", 1.2),
    "collider": (r"\bcollid(?:er|ers|e)\b|\bcollisions?\b", "The shape used for contact. Without it, objects pass through each other.", "Customizing USD", 1.1),
    "mass": (r"\bmass\b|\bdensity\b", "How heavy a rigid body is; PhysX can compute it from density and size.", "Customizing USD", 0.9),
    "ground plane": (r"\bground plane\b", "An infinite flat collider for objects to rest on.", "Customizing USD", 1.0),
    "joint": (r"\bjoints?\b|\barticulations?\b", "Connects two bodies (hinge, slider). An articulation is a jointed robot.", "Customizing USD", 1.1),
    "API schema": (r"\bapi schemas?\b|\bapplied schemas?\b|\bschemas?\b", "Extra behaviour added to a prim, like RigidBodyAPI or CollisionAPI.", "Customizing USD", 1.2),
    "environment": (r"\benvironments?\b|\bwarehouse\b|\bscene setup\b", "The world around your subject: floor, walls, lights, props.", None, 0.7),
    "Nucleus": (r"\bNucleus\b|\bcontent browser\b", "Omniverse's asset server / browser where shared assets live.", None, 0.8),
    "SimReady": (r"\bSimReady\b", "Assets prepared with physics, semantics and correct scale for simulation.", None, 1.1),
    "semantic label": (r"\bsemantic(?:s| labels?)?\b", "Class names on prims so synthetic data knows what each pixel is.", None, 1.0),
    "Action Graph": (r"\baction graphs?\b|\bomni\s?graph\b", "Visual node graphs that run logic (keys, events, ticks) in Kit.", None, 1.0),
    "flatten": (r"\bflatten(?:ed|ing)?\b", "Bake all layers into one, losing the separate composition arcs.", "Composition", 1.2),
    "timeline": (r"\btimeline\b|\bpress(?:ing)? play\b|\btime ?samples?\b|\bkeyframes?\b", "Time in the scene: Play runs physics; time samples animate values.", "Data Modeling", 0.8),
}
_TERM_RE = {name: re.compile(spec[0], re.I) for name, spec in _TERMS.items()}

_ACTION = re.compile(
    r"\b(?:click|right[\s\-]?click|go to|select|drag|drop|create|add|open|enable|press|choose|set|type|save|import|"
    r"rename|move|delete|apply)\b", re.I)
_UI = re.compile(
    r"\b(?:menu|window|panel|button|viewport|stage|property|properties|content|toolbar|tab|create|file|"
    r"layer|layers|search|browser|option|options|preset|edit)\b", re.I)

# Accuracy checks. "likely" = the sentence states something USD does not do; "check" = a topic beginners mix up.
# "unless": a sentence that also matches this is saying the correct thing ("scaling does NOT change the points").
_NEGATION = r"\b(?:not|never|no longer|without)\b|n't\b"
_CLAIMS: tuple[dict[str, Any], ...] = (
    {
        "id": "payload_only_editable",
        "pattern": r"\bonly\b[^.]{0,60}\bpayloads?\b[^.]{0,60}\b(?:edit|chang|modif|overrid)|"
                   r"\breferenc\w*\b[^.]{0,60}\b(?:can ?not|can't|cannot|not able to|unable to)\b[^.]{0,30}\b(?:edit|chang|modif|overrid)",
        "level": "likely",
        "topic": "references vs payloads",
        "correction": "References and payloads can both be overridden: your local opinions win over the referenced "
                      "content (L is stronger than R and P in LIVRPS). A payload is a reference you can load or unload.",
        "domain": "Composition",
    },
    {
        "id": "usda_usdc_swapped",
        # the gap may not contain the other format's name ("USDA is text and USDC is binary" is correct)
        "pattern": r"\bUSDC\b(?:(?!USD[AZ])[^.]){0,40}\b(?:text|humans?|human[\s\-]?readable|ascii|readable)\b|"
                   r"\bUSDA\b(?:(?!USD[CZ])[^.]){0,40}\b(?:binary|crate|compressed|machine)\b",
        # subtitles often split "The USDC. / is text format": read each sentence together with the one before
        "window": 2,
        "level": "likely",
        "topic": "file formats",
        "correction": "USDA is the text format, USDC is the binary 'crate' format, USDZ is a zip package. "
                      "(Subtitles often swap these words.)",
        "domain": "Data Exchange",
    },
    {
        "id": "scale_changes_points",
        "pattern": r"\bscal\w*\b[^.]{0,60}\b(?:changes?|modif\w*|moves?)\b[^.]{0,30}\b(?:points|vertices|mesh data)\b",
        "unless": _NEGATION,
        "level": "likely",
        "topic": "transforms",
        "correction": "Scaling an Xform changes its transform, not the mesh's stored points; the points stay the same "
                      "and the transform is applied when the scene is drawn.",
        "domain": "Data Modeling",
    },
    {
        "id": "rigid_body_adds_collision",
        "pattern": r"\brigid[\s\-]?bod\w*\b[^.]{0,60}\b(?:automatically|also|already)\b[^.]{0,30}\bcollid\w*|"
                   r"\brigid[\s\-]?bod\w*\b[^.]{0,30}\b(?:means|is the same as)\b[^.]{0,20}\bcollid",
        "unless": _NEGATION,
        "level": "likely",
        "topic": "physics",
        "correction": "RigidBodyAPI makes a prim move; CollisionAPI makes it touch things. They are separate. "
                      "Isaac's 'Rigid Body with Colliders Preset' just adds both at once.",
        "domain": "Customizing USD",
    },
    {
        "id": "prim_is_a_file",
        "pattern": r"\bprims?\b[^.]{0,30}\b(?:is|are)\b[^.]{0,8}\b(?:a )?files?\b",
        "unless": _NEGATION,
        "level": "likely",
        "topic": "prims vs files",
        "correction": "A prim is an object in the scene tree, addressed by a path like /World/Box. Files are layers.",
        "domain": "Data Modeling",
    },
    {
        "id": "units_default",
        "pattern": r"\bmeters? per unit\b|\bmetersPerUnit\b",
        "level": "check",
        "topic": "units",
        "correction": "If metersPerUnit is not authored, USD assumes 0.01 (centimetres). Isaac Sim stages author 1.0 "
                      "(metres). Check the stage's value rather than assuming.",
        "domain": "Data Exchange",
    },
    {
        "id": "up_axis",
        "pattern": r"\b[yz][\s\-]?(?:axis\s)?(?:is\s)?up\b|\bup[\s\-]?axis\b",
        "level": "check",
        "topic": "up axis",
        "correction": "Isaac Sim stages are Z-up; many other Kit and DCC files are Y-up. The stage's upAxis metadata "
                      "says which; mixing them makes assets lie on their side.",
        "domain": "Data Exchange",
    },
    {
        "id": "instance_editing",
        "pattern": r"\binstanc\w*\b[^.]{0,80}\b(?:edit|chang|modif)",
        "level": "check",
        "topic": "instancing",
        "correction": "Prims inside an instance (instance proxies) cannot be edited. Edit the source asset, override "
                      "on the instance root, or switch instanceable off for that copy.",
        "domain": "Content Aggregation",
    },
    {
        "id": "sublayer_strength",
        "pattern": r"\bsub[\s\-]?layers?\b[^.]{0,60}\b(?:strong|weak|overrid|wins?|priority|top|bottom)",
        "level": "check",
        "topic": "layer strength",
        "correction": "In a layer stack, the root layer's own opinions are strongest, then its sublayers in list "
                      "order: the first listed (top in the Layer window) is stronger than the ones below.",
        "domain": "Composition",
    },
    {
        "id": "flatten_loses_arcs",
        "pattern": r"\bflatten\w*\b",
        "level": "check",
        "topic": "flattening",
        "correction": "Flattening bakes every layer into one: references, payloads and unselected variants are gone, "
                      "so you can no longer switch or update them.",
        "domain": "Composition",
    },
    {
        "id": "default_prim",
        "pattern": r"\bdefault prim\b|\bdefaultPrim\b",
        "level": "check",
        "topic": "default prim",
        "correction": "A file's defaultPrim is what a reference brings in when no prim path is given. Assets meant "
                      "for referencing should set it.",
        "domain": "Content Aggregation",
    },
)
_CLAIM_RE = {claim["id"]: re.compile(claim["pattern"], re.I) for claim in _CLAIMS}
_UNLESS_RE = {claim["id"]: re.compile(claim["unless"], re.I) for claim in _CLAIMS if claim.get("unless")}


# --------------------------------------------------------------------------------------------------- discovery
def default_dirs(environ: dict[str, str] | None = None) -> list[Path]:
    """Folders searched for transcripts: $OMNIVERSE_CLASS_TRANSCRIPTS, else Downloads and the class folder."""
    env = os.environ if environ is None else environ
    override = env.get(ENV_DIRS)
    if override:
        return [Path(part).expanduser() for part in override.split(os.pathsep) if part.strip()]
    downloads = Path.home() / "Downloads"
    return [downloads, downloads / "Issac Sim Classes"]


def _home_short(path: Path) -> str:
    text = str(path).replace("\\", "/")
    home = str(Path.home()).replace("\\", "/")
    return "~" + text[len(home):] if text.lower().startswith(home.lower()) else text


def _session_number(name: str) -> int | None:
    match = _SESSION.search(name)
    return int(match.group(1)) if match else None


def _title_from_name(name: str) -> str | None:
    match = _TITLE.search(name)
    return match.group(1).strip() if match else None


def _scan(root: Path, depth: int = 0) -> Iterable[Path]:
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir() and depth < MAX_SCAN_DEPTH and not entry.name.startswith("."):
                yield from _scan(entry, depth + 1)
            elif entry.is_file() and entry.suffix.lower() == ".srt":
                yield entry
        except OSError:
            continue


def find_transcripts(search_dirs: list[str | Path] | None = None) -> dict[str, Any]:
    """Read-only: class transcripts (.srt) in Downloads and the class folder, one per session (duplicate
    copies such as "name (1).srt" with identical size are skipped). Nothing is opened for writing."""
    dirs = [Path(d) for d in search_dirs] if search_dirs else default_dirs()
    found: dict[Path, dict[str, Any]] = {}
    for root in dirs:
        if not root.is_dir():
            continue
        for path in _scan(root):
            real = path.resolve()
            if real in found:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            found[real] = {"session": _session_number(path.name), "title": _title_from_name(path.name),
                           "name": path.name, "path": str(path), "shown_path": _home_short(path), "size": size}
    items = sorted(found.values(), key=lambda t: (t["session"] is None, t["session"] or 0, len(t["name"]), t["name"]))
    kept: list[dict[str, Any]] = []
    for item in items:
        twin = next((k for k in kept if k["session"] == item["session"] and k["size"] == item["size"]), None)
        if twin is not None:
            twin.setdefault("duplicates", []).append(item["name"])
            continue
        kept.append(item)
    for item in kept:
        item["has_other_copies"] = len([k for k in kept if k["session"] == item["session"]]) > 1
    return {
        "ok": True,
        "count": len(kept),
        "transcripts": kept,
        "searched": [_home_short(d) for d in dirs],
        "note": "Transcripts are only read. Use class_session_plan(session=N) to turn one into lessons.",
    }


def _resolve(session: int | str | None, path: str | None, search_dirs: list[str | Path] | None) -> Path:
    dirs = [Path(d) for d in search_dirs] if search_dirs else default_dirs()
    if path:
        candidate = Path(path).expanduser()
        if candidate.suffix.lower() != ".srt":
            raise ValueError("Only .srt subtitle files can be turned into lessons")
        real = candidate.resolve()
        if not any(_is_within(real, d.resolve()) for d in dirs if d.exists()):
            raise ValueError("That file is outside the transcript folders (Downloads and the class folder). "
                             f"Move it there or set {ENV_DIRS}.")
        if not real.is_file():
            raise ValueError(f"Transcript not found: {_home_short(candidate)}")
        return real
    if session is None:
        raise ValueError("Give session (e.g. 3) or path to a .srt transcript; class_transcripts lists them")
    number = _session_number(f"session {session}") if not isinstance(session, int) else session
    if number is None or isinstance(session, bool):
        raise ValueError(f"Unknown session {session!r}; use a number such as 3")
    matches = [t for t in find_transcripts(dirs)["transcripts"] if t["session"] == number]
    if not matches:
        raise ValueError(f"No transcript for session {number} found in " + ", ".join(_home_short(d) for d in dirs))
    return Path(matches[0]["path"]).resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ----------------------------------------------------------------------------------------------- text helpers
def clean_text(text: str) -> str:
    """Fix common subtitle mishearings (x form -> Xform, U S D A -> USDA, prem -> prim)."""
    for pattern, replacement in _FIXES:
        if replacement is None:
            text = pattern.sub(lambda m: "USD" + m.group(1).upper(), text)
        else:
            text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text).strip()


def _load_cues(path: Path) -> list[Cue]:
    size = path.stat().st_size
    if size > MAX_TRANSCRIPT_BYTES:
        raise ValueError(f"Transcript is {size // 1024} KB; the limit is {MAX_TRANSCRIPT_BYTES // 1024} KB")
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    cues = [Cue(c.start, c.end, clean_text(c.text)) for c in parse_srt(raw)]
    return [c for c in cues if c.text]


def _sentences(cues: list[Cue]) -> list[dict[str, Any]]:
    """Join cues into sentences; each keeps the start time of its first cue."""
    out: list[dict[str, Any]] = []
    buffer, start = "", None
    for cue in cues:
        for piece in re.split(r"(?<=[.!?])\s+", cue.text):
            if not piece:
                continue
            if start is None:
                start = cue.start
            buffer = f"{buffer} {piece}".strip()
            if _SENTENCE_END.search(piece) or len(buffer) > 400:
                out.append({"text": buffer[:400], "start": start})
                buffer, start = "", None
    if buffer:
        out.append({"text": buffer[:400], "start": start})
    return out


def _blocks(cues: list[Cue], minutes: int) -> list[list[Cue]]:
    """Cut cues into ~minutes-long blocks, preferring a pause or a sentence end shortly after the target."""
    if not cues:
        return []
    target = minutes * 60.0
    blocks, current = [], [cues[0]]
    block_start = cues[0].start
    for prev, cue in zip(cues, cues[1:]):
        elapsed = prev.end - block_start
        if elapsed >= target:
            gap = cue.start - prev.end
            natural = gap >= PAUSE_SECONDS or bool(_SENTENCE_END.search(prev.text))
            if natural or elapsed >= target + CUT_SEARCH_SECONDS:
                blocks.append(current)
                current, block_start = [], cue.start
        current.append(cue)
    if current:
        # a very short tail (under a third of a block) joins the previous block
        if blocks and (current[-1].end - current[0].start) < target / 3:
            blocks[-1].extend(current)
        else:
            blocks.append(current)
    return blocks


def _terms(text: str) -> list[dict[str, Any]]:
    found = []
    for name, regex in _TERM_RE.items():
        count = len(regex.findall(text))
        if count:
            _, meaning, domain, weight = _TERMS[name]
            found.append({"term": name, "count": count, "meaning": meaning, "exam_domain": domain,
                          "_score": count * weight})
    found.sort(key=lambda t: (-t["_score"], t["term"]))
    for item in found:
        item.pop("_score")
    return found


def _clip(text: str, limit: int = MAX_SENTENCE) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _points(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = []
    for index, sentence in enumerate(sentences):
        text = sentence["text"]
        if len(text) < 25:
            continue
        distinct = sum(1 for regex in _TERM_RE.values() if regex.search(text))
        if not distinct:
            continue
        scored.append((distinct + (0.5 if _ACTION.search(text) else 0.0), index))
    best = sorted(sorted(scored, reverse=True)[:MAX_POINTS], key=lambda s: s[1])
    return [{"at": format_timestamp(sentences[i]["start"]), "point": _clip(sentences[i]["text"])} for _, i in best]


def _steps(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps = []
    for sentence in sentences:
        text = sentence["text"]
        if _ACTION.search(text) and _UI.search(text) and len(text) >= 20:
            steps.append({"at": format_timestamp(sentence["start"]), "step": _clip(text)})
        if len(steps) >= MAX_STEPS:
            break
    return steps


def _flags(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags, seen_check, flagged = [], set(), set()
    for index, current in enumerate(sentences):
        for claim in _CLAIMS:
            sentence = current
            if not _CLAIM_RE[claim["id"]].search(current["text"]):
                if claim.get("window", 1) < 2 or index == 0:
                    continue
                previous = sentences[index - 1]
                joined = {"text": f"{previous['text']} {current['text']}".replace(". ", " ", 1),
                          "start": previous["start"]}
                if not _CLAIM_RE[claim["id"]].search(joined["text"]):
                    continue
                sentence = joined
            unless = _UNLESS_RE.get(claim["id"])
            if unless is not None and unless.search(sentence["text"]):
                continue
            key = (claim["id"], sentence["start"])
            if key in flagged:
                continue
            flagged.add(key)
            if claim["level"] == "check":
                if claim["id"] in seen_check:
                    continue  # one reminder per topic per block is enough
                seen_check.add(claim["id"])
            flags.append({
                "at": format_timestamp(sentence["start"]),
                "level": claim["level"],
                "topic": claim["topic"],
                "said": _clip(sentence["text"]),
                "correct_idea": claim["correction"],
                "exam_domain": claim["domain"],
                "note": ("This sentence conflicts with how USD works; check the recording (subtitles can mishear)."
                         if claim["level"] == "likely" else "A topic beginners often mix up; make sure you remember this."),
            })
    return flags


def _check_questions(terms: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"question": f"In your own words: what is {'a ' if t['term'][0].islower() else ''}{t['term']}?",
             "check_against": t["meaning"]} for t in terms[:3]]


def _exam_questions(terms: list[dict[str, Any]], seed: int, count: int = 2) -> list[dict[str, Any]]:
    domains = [t["exam_domain"] for t in terms if t.get("exam_domain")]
    if not domains:
        return []
    try:
        from .exam import bank

        picked = bank.quiz(domain=domains[0], count=count, seed=seed)
        return picked.get("questions", [])
    except Exception:  # noqa: BLE001 - the quiz is extra; a lesson works without it
        return []


def _analyse(path: Path, minutes: int) -> dict[str, Any]:
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise ValueError(f"minutes must be a whole number from {MIN_MINUTES} to {MAX_MINUTES}")
    cues = _load_cues(path)
    if not cues:
        raise ValueError("The transcript has no readable subtitle lines")
    lessons = []
    for number, block in enumerate(_blocks(cues, minutes), start=1):
        sentences = _sentences(block)
        text = " ".join(c.text for c in block)
        terms = _terms(text)
        title = ", ".join(t["term"] for t in terms[:3]) or "Introduction and setup"
        lessons.append({
            "number": number,
            "start": format_timestamp(block[0].start),
            "end": format_timestamp(block[-1].end),
            "minutes": round((block[-1].end - block[0].start) / 60.0, 1),
            "title": title,
            "terms": terms,
            "points": _points(sentences),
            "do_in_app": _steps(sentences),
            "flags": _flags(sentences),
            "_text": text,
        })
    return {
        "session": _session_number(path.name),
        "title": _title_from_name(path.name),
        "transcript": _home_short(path),
        "duration": format_timestamp(cues[-1].end),
        "lessons": lessons,
    }


# --------------------------------------------------------------------------------------------------- public
def session_plan(session: int | str | None = None, path: str | None = None, minutes: int = DEFAULT_MINUTES,
                 search_dirs: list[str | Path] | None = None) -> dict[str, Any]:
    """Read-only: split one class transcript into ~minutes-long lessons in the teacher's order, with a title,
    time range and key terms each, plus how many accuracy flags each lesson has."""
    try:
        analysis = _analyse(_resolve(session, path, search_dirs), minutes)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    lessons = [{
        "number": item["number"], "start": item["start"], "end": item["end"], "minutes": item["minutes"],
        "title": item["title"], "key_terms": [t["term"] for t in item["terms"][:6]],
        "flags": len(item["flags"]),
        "likely_mistakes": sum(1 for f in item["flags"] if f["level"] == "likely"),
    } for item in analysis["lessons"]]
    return {
        "ok": True,
        "session": analysis["session"],
        "title": analysis["title"],
        "transcript": analysis["transcript"],
        "duration": analysis["duration"],
        "minutes_per_lesson": minutes,
        "lesson_count": len(lessons),
        "lessons": lessons,
        "how_to_use": "Study one lesson at a time: class_lesson(session=..., number=1). Watch that part of the "
                      "recording, do the in-app steps, then answer the questions.",
        "note": "Times are from the subtitle file and may differ from other notes of the same class.",
    }


def lesson(number: int, session: int | str | None = None, path: str | None = None, minutes: int = DEFAULT_MINUTES,
           search_dirs: list[str | Path] | None = None) -> dict[str, Any]:
    """Read-only: one lesson from a class transcript: key terms with plain meanings, main points and in-app steps
    with timestamps, accuracy flags, check questions, matching exam-bank questions and a short excerpt."""
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        return {"ok": False, "error": "number must be a whole number starting at 1"}
    try:
        analysis = _analyse(_resolve(session, path, search_dirs), minutes)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    lessons = analysis["lessons"]
    if number > len(lessons):
        return {"ok": False, "error": f"This session has {len(lessons)} lessons of about {minutes} minutes"}
    item = lessons[number - 1]
    text = item.pop("_text")
    terms = item["terms"][:8]
    return {
        "ok": True,
        "session": analysis["session"],
        "session_title": analysis["title"],
        "lesson": number,
        "of": len(lessons),
        "watch": f"{item['start']} to {item['end']} in the recording ({item['minutes']} min)",
        "title": item["title"],
        "key_terms": terms,
        "main_points": item["points"],
        "do_in_app": item["do_in_app"],
        "accuracy_flags": item["flags"],
        "check_questions": _check_questions(terms),
        "exam_questions": _exam_questions(terms, seed=(analysis["session"] or 0) * 100 + number),
        "answer_exam_questions_with": "learning_exam_answer(question_id=..., chosen=[option index])",
        "excerpt": _clip(text, EXCERPT_CHARS),
        "next": (f"class_lesson(session={analysis['session']}, number={number + 1})" if number < len(lessons)
                 else "Session finished: try learning_drill_start(focus='weak') or the homework checker."),
        "note": "Subtitles are auto-generated; words were cleaned (e.g. 'x form' -> Xform) but may still be wrong.",
    }


def check_claims(session: int | str | None = None, path: str | None = None, include_reminders: bool = False,
                 search_dirs: list[str | Path] | None = None) -> dict[str, Any]:
    """Read-only: sentences in a class transcript that conflict with USD facts (likely mistakes), with the
    correct idea and timestamp. include_reminders=True also lists topics beginners often mix up."""
    try:
        analysis = _analyse(_resolve(session, path, search_dirs), DEFAULT_MINUTES)
    except (ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    flags = [dict(flag, lesson=item["number"]) for item in analysis["lessons"] for flag in item["flags"]
             if include_reminders or flag["level"] == "likely"]
    return {
        "ok": True,
        "session": analysis["session"],
        "title": analysis["title"],
        "count": len(flags),
        "flags": flags,
        "note": "A flag means 'check this', not 'the teacher is wrong': subtitles mishear words, and teachers "
                "often simplify. Rewatch the timestamp and compare with the correct idea.",
    }


def claim_checks() -> list[dict[str, str]]:
    """The accuracy checks this module knows (id, level, topic, correct idea)."""
    return [{"id": c["id"], "level": c["level"], "topic": c["topic"], "correct_idea": c["correction"]} for c in _CLAIMS]
