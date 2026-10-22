"""Plain-English phrases for round 7: timed exam drills, today's study brief, a daily reminder, the change journal,
smart layout previews, the robot inspector and the Docker docs helper.

Pure string parsing for router.py: nothing here reads the scene, a file or the progress store. parse(text) takes the
request as typed (case kept: prim paths are case-sensitive) and returns a small spec {"kind": ..., ...} or None.

Kinds and what the router does with them:

* drill_start / drill_focuses (learning_drill_start / learning_drill_focuses): starts a timed drill (it only saves the
  drill session; answers are saved later by learning_drill_answer) or lists the focuses.
* today (learning_today) and reminder_setup (learning_reminder_setup): read-only; the reminder only RETURNS text.
* journal / journal_summary (scene_journal / scene_journal_summary): read-only.
* layout (scene_layout_*): ALWAYS a dry-run preview from a sentence, even for exact paths; the payload names the
  tool call (with dry_run=False) that applies it. Layout moves many prims at once, so a sentence never applies it.
* robot_inspect (robot_inspect): read-only.
* docker_status / docker_setup_plan / docker_explain (docs_docker_*): read-only; nothing is ever run in Docker.

Every pattern is anchored, has no nested unbounded repeats and only runs on text of at most MAX_PHRASE_CHARS
characters, so parse() stays linear on long input (it runs on every router request).
"""

from __future__ import annotations

import re
from typing import Any

_FLAGS = re.IGNORECASE
MAX_PHRASE_CHARS = 600
_POLITE = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)[,!\s]+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|kindly\s+|"
    r"i\s+(?:want|would\s+like|need)\s+(?:you\s+)?to\s+)?",
    _FLAGS,
)
_TRAILING_CHARS = " \t\n\r\f\v?.!"
_PRIM_PATH = re.compile(r"(?<![\w/])(/[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*)(?![\w/])")
_USD_FILE = re.compile(r"(?P<file>(?:[A-Za-z]:)?[^\s\"'<>|?*]*\.usd[acz]?)(?=[\s\"',;)]|$)", _FLAGS)
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_TRIPLE = re.compile(rf"(?P<x>{_NUMBER})[,\s]+(?P<y>{_NUMBER})[,\s]+(?P<z>{_NUMBER})")

# Kinds the router answers as scene requests (the rest are learning requests).
SCENE_KINDS = frozenset({"journal", "journal_summary", "layout", "robot_inspect"})

# --- exam drills ---------------------------------------------------------------------------------------------------
# Domain words -> the official NCP-OUSD domain names (exam/bank.py DOMAINS); longest first when matching.
_DOMAIN_WORDS = {
    "composition": "Composition",
    "composition arcs": "Composition",
    "content aggregation": "Content Aggregation",
    "aggregation": "Content Aggregation",
    "customizing usd": "Customizing USD",
    "customising usd": "Customizing USD",
    "customizing": "Customizing USD",
    "customization": "Customizing USD",
    "customisation": "Customizing USD",
    "data exchange": "Data Exchange",
    "exchange": "Data Exchange",
    "data modeling": "Data Modeling",
    "data modelling": "Data Modeling",
    "modeling": "Data Modeling",
    "modelling": "Data Modeling",
    "debugging and troubleshooting": "Debugging and Troubleshooting",
    "debugging": "Debugging and Troubleshooting",
    "troubleshooting": "Debugging and Troubleshooting",
    "pipeline development": "Pipeline Development",
    "pipeline": "Pipeline Development",
    "pipelines": "Pipeline Development",
    "visualization": "Visualization",
    "visualisation": "Visualization",
}
_DOMAIN_KEYS = tuple(sorted(_DOMAIN_WORDS, key=len, reverse=True))
_DRILL_VERB = (
    r"(?:start|begin|run|do|take|launch|give\s+me|get\s+me|let'?s\s+(?:do|start|have)|i\s+want(?:\s+to\s+do)?|"
    r"can\s+i\s+(?:do|have|get|take))"
)
_DRILL_START = re.compile(
    rf"^{_DRILL_VERB}\s+(?:me\s+)?(?:a|an|another|one|my|the|some|new)?\s*(?P<pre>[\w\s.-]{{0,60}}?)\s*\bdrills?\b"
    r"(?P<rest>(?:\s+.{0,200})?)$",
    _FLAGS,
)
_DRILL_ADJECTIVE = re.compile(
    r"^(?:a\s+|an\s+)?(?P<pre>(?:quick|short|timed|mixed|random|flash|weak[\w\s-]{0,20}?|mistakes?|exam|"
    r"\d{1,2}\s*-?\s*questions?)\s+[\w\s.-]{0,40}?)\s*\bdrill\b(?P<rest>(?:\s+.{0,200})?)$",
    _FLAGS,
)
_DRILL_ME = re.compile(r"^drill\s+me(?:\s+(?:on|in|about|with|for)\s+(?P<topic>.{1,200}))?$", _FLAGS)
_FLASH = re.compile(
    r"^(?:(?:start|begin|give\s+me|do|run|take|let'?s\s+do|i\s+want)\s+(?:me\s+)?(?:a|an|some|the|my)?\s*)?"
    r"(?:quick\s+|short\s+|timed\s+)?flash\s*-?\s*(?:cards?|quiz(?:zes)?|drill|round|questions?)"
    r"(?:\s+(?:on|about|for|in)\s+(?P<topic>.{1,200}))?$",
    _FLAGS,
)
_DRILL_FOCUSES = re.compile(
    r"^(?:what|which)\s+(?:kinds?\s+of\s+|types?\s+of\s+)?drills\s+(?:can\s+i\s+do|are\s+there|do\s+you\s+have)$"
    r"|^(?:list|show)\s+(?:me\s+)?(?:the\s+)?drill\s+(?:focuses|types|kinds|options)$|^drill\s+(?:focuses|types|options)$",
    _FLAGS,
)
_COUNT = re.compile(r"\b(?P<n>\d{1,2})\s*-?\s*(?:questions?|qs?)\b", _FLAGS)
_OBJECTIVE = re.compile(r"(?<![\d.])(?P<o>\d{1,2}\.\d{1,2})(?![\d.])")
_WEAK = re.compile(r"\bweak|\bstruggl|\bworst\b|\bneed\s+(?:to\s+)?(?:work|practice)", _FLAGS)
_MISTAKES = re.compile(r"\bmistakes?\b|\bwrong\b|\bmissed\b|\berrors?\b", _FLAGS)
_MIXED = re.compile(r"\bmixed\b|\brandom\b|\beverything\b|\ball\s+(?:topics|domains|areas)\b|\bwhole\s+(?:exam|bank)\b|"
                    r"\bfull\s+exam\b", _FLAGS)
_FILLER = re.compile(
    r"\b(?:a|an|the|my|me|on|in|about|for|of|with|areas?|spots?|topics?|domains?|objectives?|objective|quick|short|"
    r"timed|exam|ncp-?ousd|usd|openusd|question|questions|qs?|q|drill|drills|session|round|please|now|today|some|"
    r"new|another|one|focus|focused|focusing|again|domain|section)\b|\d{1,2}\s*-?\s*(?=questions?|qs?\b)",
    _FLAGS,
)

# --- today's brief and the reminder ------------------------------------------------------------------------------
_MINUTES = r"(?:[,\s]+(?:i\s+have|i'?ve\s+got|i\s+got|in|with|for|within)\s+(?P<n>\d{1,3})\s*(?P<unit>m|mins?|minutes?|h|hrs?|hours?))?"
_TODAY = (
    re.compile(
        r"^what\s+(?:should|shall|do|can|could|must)\s+i\s+(?:do|study|work\s+on|practi[cs]e|learn|revise|review|focus\s+on)"
        rf"\s+(?:today|now|tonight|this\s+(?:morning|afternoon|evening)){_MINUTES}$",
        _FLAGS,
    ),
    re.compile(rf"^(?:what(?:'s|\s+is)\s+)?(?:my\s+)?(?:study\s+)?plan\s+for\s+(?:today|tonight){_MINUTES}$", _FLAGS),
    re.compile(rf"^(?:show\s+(?:me\s+)?)?(?:today'?s|my\s+daily|my\s+today'?s|the\s+daily)\s+(?:study\s+)?(?:plan|brief|briefing){_MINUTES}$",
               _FLAGS),
    re.compile(rf"^(?:plan|organi[sz]e)\s+my\s+(?:study\s+)?(?:day|session|evening|study\s+time){_MINUTES}$", _FLAGS),
)
_REMINDER = (
    re.compile(
        r"^(?:set\s*up|setup|create|add|make|schedule|configure)\s+(?:a|an|my|the)?\s*(?P<adj>[\w\s-]{0,30}?)\s*\breminders?\b"
        r"(?P<rest>(?:\s+.{0,120})?)$",
        _FLAGS,
    ),
    re.compile(r"^remind\s+me\s+(?P<rest>.{1,120})$", _FLAGS),
)
_REMINDER_TIME = re.compile(
    r"\b(?:at\s+)?(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)?(?![\w:.])", _FLAGS
)
_REMINDER_WORDS = frozenset(
    "a an the my me to for of at in on study studying daily every each day days evening evenings morning mornings "
    "night tonight exam ncp ousd ncp-ousd usd reminder reminders notification popup pop-up windows task scheduled "
    "practice practise revise review o'clock oclock everyday".split()
)
_REMIND_NEEDS = re.compile(r"\b(?:every\s*day|daily|each\s+day|every\s+(?:evening|morning|night)|to\s+study|study)\b",
                           _FLAGS)

# --- change journal ----------------------------------------------------------------------------------------------
_WHEN = r"(?P<when>today|yesterday|this\s+(?:morning|afternoon|evening)|on\s+\d{4}-\d{2}-\d{2}|\d{4}-\d{2}-\d{2})"
_JOURNAL_SUMMARY = (
    re.compile(
        r"^what\s+(?:did|have)\s+(?:you|i|we)\s+(?:change|changed|edit|edited|modify|modified|do|done)\s+"
        rf"(?:(?:in|to)\s+(?:my|the)\s+scene\s+)?{_WHEN}$",
        _FLAGS,
    ),
    re.compile(rf"^what\s+(?:changed|got\s+changed|was\s+changed)\s+(?:in\s+(?:my|the)\s+scene\s+)?{_WHEN}$", _FLAGS),
    re.compile(rf"^(?:summari[sz]e|recap)\s+(?:my|the|today'?s)\s+(?:scene\s+)?(?:changes|edits)(?:\s+(?:for\s+)?{_WHEN})?$",
               _FLAGS),
    re.compile(rf"^(?:journal|change\s+journal|scene\s+journal)\s+summary(?:\s+(?:for\s+)?{_WHEN})?$", _FLAGS),
)
_JOURNAL = (
    re.compile(
        r"^(?:show|open|read|list|give|display|print)\s+(?:me\s+)?(?:the|my)?\s*(?:scene\s+)?(?:change\s+)?"
        r"(?:journal|change\s*log|changelog|edit\s+log)(?:\s+(?:for|of|on)\s+(?P<path>/\S+))?"
        rf"(?:\s+(?:for\s+)?{_WHEN})?$",
        _FLAGS,
    ),
    re.compile(r"^(?:the\s+|my\s+)?(?:scene\s+|change\s+)?journal(?:\s+(?:for|of|on)\s+(?P<path>/\S+))?$", _FLAGS),
    re.compile(
        r"^what\s+(?:did|have)\s+(?:you|i|we)\s+(?:change|changed|edit|edited|do)\s+(?:on|to|in|with)\s+(?P<path>/\S+)"
        rf"(?:\s+{_WHEN})?$",
        _FLAGS,
    ),
    re.compile(r"^(?:history|change\s+history|journal)\s+(?:of|for)\s+(?P<path>/\S+)$", _FLAGS),
)

# --- smart layout (previews only) ------------------------------------------------------------------------------
_SELECTION_WORDS = re.compile(r"^(?:these|those|this|them|it|that|the\s+selection|my\s+selection|selection|selected|"
                              r"the\s+selected(?:\s+\w+)?|selected\s+\w+|these\s+\w+|those\s+\w+)$", _FLAGS)
_NAME_TARGET = re.compile(
    r"^(?:all\s+(?:the\s+|my\s+)?|every\s+|the\s+|my\s+|these\s+|those\s+)?(?:(?P<count>\d{1,3})\s+)?(?P<name>[A-Za-z][\w-]{1,40})"
    r"(?:\s+(?:under|in|inside|within|below)\s+(?P<scope>/\S+))?$",
    _FLAGS,
)
_SNAP = re.compile(
    r"^(?P<verb>snap|drop|settle|rest|lower|ground|put|place|set|move)\s+(?P<targets>.{1,400}?)\s+(?:back\s+|down\s+)?"
    r"(?:on\s*to|onto|on|to|against|on\s+top\s+of)\s+(?:the\s+)?(?P<surface>floor|ground(?:\s+plane)?|/\S+)$",
    _FLAGS,
)
_STACK = re.compile(r"^(?:stack(?:\s+up)?|pile(?:\s+up)?)\s+(?P<body>.{1,400})$", _FLAGS)
_ROWS = re.compile(r"(?:^|\s)(?P<rows>\d{1,3}(?:(?:\s*[,/-]\s*|\s+)\d{1,3}){1,40})(?=\s|$)")
_ANCHOR_PATH = re.compile(r"\s+(?:on\s+top\s+of|on|onto|atop|at)\s+(?:the\s+)?(?P<anchor>/\S+)")
_ANCHOR_POINT = re.compile(rf"\s+(?:at|around|centered\s+at|centred\s+at)\s+(?P<point>{_NUMBER}[,\s]+{_NUMBER}[,\s]+{_NUMBER})")
_GAP = re.compile(rf"\s+(?:with\s+(?:a\s+)?)?(?:gap|gaps|spacing)\s+(?:of\s+)?(?P<gap>{_NUMBER})\b|\s+(?P<apart>{_NUMBER})\s*(?:m|units?)?\s+apart\b",
                  _FLAGS)
_FACING = re.compile(r"\s+facing\s+(?P<facing>[+-]?[xyz])\b", _FLAGS)
_ROW_WORDS = re.compile(r"\s+(?:in|into|as)\s+(?:a\s+)?(?:rows?|pyramid|wall|stack)(?:\s+of)?\b|\s+rows?(?:\s+of)?\b", _FLAGS)
_DISTRIBUTE = re.compile(
    r"^(?P<verb>distribute|spread(?:\s+out)?|put|place|set|arrange|lay\s+out|line\s+up)\s+(?P<targets>.{1,400}?)\s+"
    r"(?P<evenly>(?:evenly|equally|neatly)\s+)?(?:along|across|on|onto|over|on\s+top\s+of)\s+(?:the\s+)?(?P<surface>/\S+)"
    rf"(?P<after>\s+(?:evenly|equally))?(?:\s+with\s+(?:a\s+)?(?:margin|gap)\s+(?:of\s+)?(?P<margin>{_NUMBER}))?$",
    _FLAGS,
)
_SPACE = re.compile(
    r"^(?:space(?:\s+out)?|spread\s+out|even\s+out)\s+(?P<targets>.{1,400}?)(?:\s+(?:evenly|equally))?"
    r"(?:\s+(?:along|on|in|across)\s+(?:the\s+)?(?P<axis>[xyz])(?:[\s-]*axis)?)?"
    rf"(?:\s+(?:with|by|at)\s+(?P<spacing>{_NUMBER})\s*(?:m|meters?|metres?|units?)?(?:\s+(?:gaps?|apart|spacing))?)?$",
    _FLAGS,
)
_ALIGN = re.compile(
    r"^(?:align|line\s+up)\s+(?P<targets>.{1,400}?)(?:\s+(?:on|along|in|by)\s+(?:the\s+)?(?P<axis>[xyz])(?:[\s-]*axis)?)?"
    r"(?:\s+(?:by|to|on|at)?\s*(?:their|the)?\s*(?P<mode>min(?:imum)?|max(?:imum)?|cent(?:er|re)s?|middles?|left|right|"
    r"bottoms?|tops?|low(?:est)?|high(?:est)?)(?:\s+(?:sides?|edges?))?)?$",
    _FLAGS,
)
_MODE_WORDS = {"min": "min", "minimum": "min", "left": "min", "bottom": "min", "bottoms": "min", "low": "min",
               "lowest": "min", "max": "max", "maximum": "max", "right": "max", "top": "max", "tops": "max",
               "high": "max", "highest": "max"}
_NOT_TARGETS = re.compile(r"\s(?:to|with|at|and\s+then|then|by)\s", _FLAGS)

# --- robot inspector ---------------------------------------------------------------------------------------------
_ROBOT_VERB = r"(?:inspect|check|describe|explain|analy[sz]e|examine|review|report\s+on|tell\s+me\s+about|look\s+at|show\s+me)"
_ROBOT = (
    re.compile(
        rf"^{_ROBOT_VERB}\s+(?:my|the|this|our)\s+robots?(?:'s\s+joints)?(?:\s+(?:at|in|under|called|named)?\s*(?P<path>/\S+))?"
        r"(?:\s+(?:in|from)\s+(?:the\s+)?(?:file\s+)?(?P<file>\S+\.usd[acz]?))?$",
        _FLAGS,
    ),
    re.compile(rf"^{_ROBOT_VERB}\s+(?:the\s+)?robot\s+(?:at\s+|in\s+|under\s+)?(?P<path>/\S+)$", _FLAGS),
    re.compile(r"^robots?\s+(?:report|inspection|inspector|check|summary|joints|details)(?:\s+(?:for|of|on)\s+(?P<path>/\S+))?$",
               _FLAGS),
    re.compile(r"^(?:what|which|how\s+many)\s+joints\s+(?:does|do|has|have)\s+(?:my|the|this)\s+robot(?:\s+(?:have|got))?"
               r"(?:\s+(?:at|in|under)\s+(?P<path>/\S+))?$", _FLAGS),
    re.compile(r"^is\s+my\s+robot\s+(?:set\s+up|setup|configured|rigged)\s+(?:right|correctly|properly|ok)$", _FLAGS),
)
_ROBOT_PATH = re.compile(r"^(?:inspect|examine)\s+(?P<path>/\S+)$", _FLAGS)
_ROBOT_NAME = re.compile(
    r"robot|franka|panda|ur\d{1,2}e?$|ur_\d|kinova|jaco|jetbot|carter|leatherback|anymal|humanoid|manipulator|kuka|"
    r"iiwa|fanuc|yaskawa|unitree|allegro|cobotta|dofbot|^spot$|^h1$|^g1$|^go[12]$",
    _FLAGS,
)

# --- Docker docs ------------------------------------------------------------------------------------------------
_DOCKER_SERVICE = re.compile(r"\b(?P<service>usd|kit|omni\.?ui|omniui|isaac(?:\s*sim)?|isaacsim)\b", _FLAGS)
_DOCKER_EXPLAIN = (
    re.compile(r"^(?:is|would|will)\s+(?:using\s+|running\s+)?(?:the\s+)?docker(?:\s+(?:docs?|mcp|version|container))?\s+"
               r"(?:be\s+)?(?:worth\s+it|worth\s+(?:it\s+)?(?:for\s+.{1,60})?|necessary|needed|required|useful|helpful|"
               r"a\s+good\s+idea|better|any\s+better|faster)$", _FLAGS),
    re.compile(r"^(?:should|do|must)\s+i\s+(?:really\s+)?(?:use|need|install|set\s+up|bother\s+with|try|run)\s+(?:the\s+)?"
               r"docker(?:\s+.{1,80})?$", _FLAGS),
    re.compile(r"^(?:docker|the\s+docker\s+docs?)\s+(?:vs\.?|versus|or)\s+(?:the\s+)?(?:offline|local|built-in)(?:\s+.{1,60})?$",
               _FLAGS),
    re.compile(r"^(?:why|when)\s+(?:would|should)\s+i\s+(?:use|want|need)\s+docker(?:\s+.{1,80})?$", _FLAGS),
    re.compile(r"^(?:explain|compare)\s+(?:the\s+)?docker(?:\s+(?:choice|option|docs?|mcp|vs\.?\s+offline|setup))?$",
               _FLAGS),
    re.compile(r"^(?:what\s+does|what\s+would)\s+docker\s+(?:add|give\s+me|change)(?:\s+.{1,60})?$", _FLAGS),
)
_DOCKER_STATUS = (
    re.compile(r"^(?:the\s+)?docker\s+(?:status|state|check|health)(?:\s+(?:for|of)\s+.{1,40})?$", _FLAGS),
    re.compile(r"^(?:is\s+)?(?:the\s+)?docker(?:\s+(?:docs?|desktop|engine|daemon|mcp|container|usd\s+docs?))?\s+"
               r"(?:running|installed|up|working|ready|on)(?:\s+(?:for|with)\s+.{1,40})?$", _FLAGS),
    re.compile(r"^(?:check|show)\s+(?:me\s+)?(?:the\s+)?docker(?:\s+(?:status|setup|docs?|mcp|containers?))?$", _FLAGS),
    re.compile(r"^(?:what(?:'s|\s+is)\s+)?(?:the\s+)?status\s+of\s+(?:the\s+)?docker(?:\s+(?:docs?|mcp|container))?$", _FLAGS),
    re.compile(r"^do\s+i\s+have\s+docker(?:\s+installed)?$", _FLAGS),
)
_DOCKER_SETUP = (
    re.compile(r"^(?:how\s+(?:do|can|would|should)\s+i\s+)?(?:set\s*up|setup|install|configure|get|start|run)\s+(?:the\s+)?"
               r"docker(?:\s+(?:usd\s+|kit\s+|isaac\s+)?(?:docs?|documentation|mcp|container|version))*(?:\s+(?:for|with)\s+.{1,40})?$",
               _FLAGS),
    re.compile(r"^(?:the\s+)?docker\s+(?:setup|set\s+up|install(?:ation)?)(?:\s+(?:plan|steps|guide|instructions))?"
               r"(?:\s+(?:for|with)\s+.{1,40})?$", _FLAGS),
    re.compile(r"^(?:show|give)\s+(?:me\s+)?(?:the\s+|a\s+)?docker\s+(?:setup\s+)?(?:plan|steps|guide|instructions)$", _FLAGS),
)


# --------------------------------------------------------------------------------------------------------- helpers


def _strip(text: str) -> str:
    core = _POLITE.sub("", text.strip(), count=1).rstrip(_TRAILING_CHARS)
    if core[-6:].lower() == "please" and len(core) > 6 and core[-7].isspace():
        core = core[:-6].rstrip(_TRAILING_CHARS)
    return core.strip()


def _paths(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(1) for match in _PRIM_PATH.finditer(text)))


def _minutes(match: re.Match[str]) -> int | None:
    number = match.groupdict().get("n")
    if not number:
        return None
    unit = (match.group("unit") or "m").lower()
    return int(number) * (60 if unit.startswith("h") else 1)


def _day(text: str | None) -> str:
    if not text:
        return "today"
    lowered = text.lower().strip()
    if lowered.startswith("on "):
        lowered = lowered[3:].strip()
    if lowered.startswith("this "):
        return "today"
    return lowered


def _drill_spec(words: str) -> dict[str, Any] | None:
    """The focus a drill sentence asks for; None when it names a topic the exam drills do not know."""
    text = " ".join(words.split())
    lowered = text.lower()
    count_match = _COUNT.search(lowered)
    spec: dict[str, Any] = {"kind": "drill_start", "focus": "weak", "objective": None, "domain": None,
                            "count": int(count_match.group("n")) if count_match else None}
    if "flash" in lowered:
        spec["focus"] = "flash"
    objective = _OBJECTIVE.search(lowered)
    domain = next((_DOMAIN_WORDS[key] for key in _DOMAIN_KEYS if re.search(rf"\b{re.escape(key)}\b", lowered)), None)
    if objective:
        spec["objective"] = objective.group("o")
        if spec["focus"] != "flash":
            spec["focus"] = "objective"
    elif domain:
        spec["domain"] = domain
        if spec["focus"] != "flash":
            spec["focus"] = "domain"
    elif spec["focus"] != "flash":
        if _MISTAKES.search(lowered):
            spec["focus"] = "mistakes"
        elif _MIXED.search(lowered):
            spec["focus"] = "mixed"
        elif _WEAK.search(lowered):
            spec["focus"] = "weak"
        else:
            leftover = _FILLER.sub(" ", lowered)
            leftover = re.sub(r"[^\w\s]", " ", leftover)
            if leftover.split():
                return None  # 'drill me on prims': a lesson topic, not an exam domain; the learning tutor answers it
    return spec


def _reminder_time(rest: str) -> tuple[str | None, str]:
    """('19:00', rest without the time) for '... at 7pm'; (None, rest) when no time is given."""
    match = _REMINDER_TIME.search(rest)
    if not match:
        return None, rest
    hour = int(match.group("h"))
    minute = int(match.group("m") or 0)
    ampm = (match.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None, rest
    return f"{hour:02d}:{minute:02d}", rest[: match.start()] + " " + rest[match.end():]


def _only_reminder_words(text: str) -> bool:
    words = re.findall(r"[\w'-]+", text.lower())
    return all(word in _REMINDER_WORDS for word in words)


def _targets(text: str, exclude: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """Which prims a layout sentence names: exact paths, the selection ('these'), or a name ('the 10 boxes')."""
    clean = text.strip().strip(",")
    paths = [path for path in _paths(clean) if path not in exclude]
    if paths:
        rest = _PRIM_PATH.sub(" ", clean)
        if re.search(r"[A-Za-z]", re.sub(r"\b(?:and|the|these|those|prims?|boxes|cubes|objects|items)\b|,", " ", rest, flags=_FLAGS)):
            return None  # words besides the paths: not a plain list of prims
        return {"paths": paths, "selection": False, "name": None, "count": None, "scope": None}
    if _SELECTION_WORDS.match(clean):
        return {"paths": [], "selection": True, "name": None, "count": None, "scope": None}
    count_first = re.match(r"^(?:these|those|the|my)\s+(?P<count>\d{1,3})\s+(?P<noun>[A-Za-z][\w-]{1,40})$", clean, _FLAGS)
    if count_first and clean.lower().startswith(("these", "those")):
        return {"paths": [], "selection": True, "name": count_first.group("noun"), "count": int(count_first.group("count")),
                "scope": None}
    named = _NAME_TARGET.match(clean)
    if named and named.group("name").lower() not in {"it", "them", "this", "that", "these", "those", "selection"}:
        count = named.group("count")
        return {"paths": [], "selection": False, "name": named.group("name"), "count": int(count) if count else None,
                "scope": named.group("scope")}
    return None


def _layout(op: str, targets: dict[str, Any], **options: Any) -> dict[str, Any]:
    return {"kind": "layout", "op": op, **targets, "options": {k: v for k, v in options.items() if v is not None}}


def _number(text: str | None) -> float | None:
    return float(text) if text else None


def _parse_layout(core: str) -> dict[str, Any] | None:
    stack = _STACK.match(core)
    if stack:
        body = stack.group("body")
        options: dict[str, Any] = {}
        point = _ANCHOR_POINT.search(body)
        if point:
            options["anchor"] = [float(v) for v in re.split(r"[,\s]+", point.group("point").strip())]
            body = body[: point.start()] + body[point.end():]
        anchor = _ANCHOR_PATH.search(body)
        anchor_path = None
        if anchor:
            anchor_path = anchor.group("anchor")
            options["anchor"] = anchor_path
            body = body[: anchor.start()] + body[anchor.end():]
        gap = _GAP.search(body)
        if gap:
            options["gap"] = float(gap.group("gap") or gap.group("apart"))
            body = body[: gap.start()] + body[gap.end():]
        facing = _FACING.search(body)
        if facing:
            word = facing.group("facing").upper()
            options["facing"] = word if word[0] in "+-" else "+" + word
            body = body[: facing.start()] + body[facing.end():]
        rows = _ROWS.search(body)
        if rows:
            options["rows"] = [int(v) for v in re.findall(r"\d{1,3}", rows.group("rows"))]
            body = body[: rows.start()] + " " + body[rows.end():]
        body = _ROW_WORDS.sub(" ", body)
        found = _targets(" ".join(body.split()), exclude=(anchor_path,) if anchor_path else ())
        if found is None:
            return None
        return _layout("stack", found, **options)

    distribute = _DISTRIBUTE.match(core)
    if distribute:
        verb = distribute.group("verb").lower()
        evenly = bool(distribute.group("evenly") or distribute.group("after"))
        if evenly or verb.startswith(("distribute", "spread")):
            surface = distribute.group("surface")
            found = _targets(distribute.group("targets"), exclude=(surface,))
            if found is not None:
                return _layout("distribute", found, surface_path=surface, margin=_number(distribute.group("margin")))

    snap = _SNAP.match(core)
    if snap:
        verb = snap.group("verb").lower()
        surface = snap.group("surface")
        floor = not surface.startswith("/")
        if verb in ("put", "place", "set", "move") and not floor:
            return None  # 'put /World/A on /World/B' is not a layout request (and 'move ... to /X' reparents)
        found = _targets(snap.group("targets"), exclude=() if floor else (surface,))
        if found is None:
            return None
        return _layout("snap", found, surface=None if floor else surface)

    space = _SPACE.match(core)
    if space:
        found = _targets(space.group("targets"))
        if found is None or _NOT_TARGETS.search(f" {space.group('targets')} "):
            return None
        if not (space.group("axis") or re.search(r"\b(?:evenly|equally)\b", core, _FLAGS) or space.group("spacing")):
            return None
        return _layout("space", found, axis=(space.group("axis") or "x").lower(), spacing=_number(space.group("spacing")))

    align = _ALIGN.match(core)
    if align:
        if _NOT_TARGETS.search(f" {align.group('targets')} "):
            return None
        found = _targets(align.group("targets"))
        if found is None:
            return None
        if found["paths"] and len(found["paths"]) < 2:
            return None
        mode_word = (align.group("mode") or "min").lower()
        mode = "center" if mode_word.startswith(("cent", "middle")) else _MODE_WORDS.get(mode_word, "min")
        return _layout("align", found, axis=(align.group("axis") or "x").lower(), mode=mode)
    return None


def _parse_docker(core: str) -> dict[str, Any] | None:
    if "docker" not in core.lower():
        return None
    service_match = _DOCKER_SERVICE.search(core.lower().replace("docker", " "))
    service = service_match.group("service").replace(" ", "") if service_match else "usd"
    if any(pattern.match(core) for pattern in _DOCKER_EXPLAIN):
        return {"kind": "docker_explain"}
    if any(pattern.match(core) for pattern in _DOCKER_STATUS):
        return {"kind": "docker_status", "service": service}
    if any(pattern.match(core) for pattern in _DOCKER_SETUP):
        return {"kind": "docker_setup_plan", "service": service}
    return None


def _parse_robot(core: str) -> dict[str, Any] | None:
    for pattern in _ROBOT:
        match = pattern.match(core)
        if match:
            groups = match.groupdict()
            path = groups.get("path")
            return {"kind": "robot_inspect", "root_path": path.rstrip(".,;:") if path else None,
                    "scene_file": groups.get("file")}
    named = _ROBOT_PATH.match(core)
    if named:
        path = named.group("path").rstrip(".,;:")
        if _PRIM_PATH.fullmatch(path) and _ROBOT_NAME.search(path.rsplit("/", 1)[-1]):
            return {"kind": "robot_inspect", "root_path": path, "scene_file": None}
    return None


# --------------------------------------------------------------------------------------------------------- parse


def parse(text: str) -> dict[str, Any] | None:
    """{"kind": ..., ...} for a round-7 request, else None. Never reads anything."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_PHRASE_CHARS:
        return None
    core = _strip(text)
    if not core:
        return None
    core = re.sub(r"\s+", " ", core)

    if _DRILL_FOCUSES.match(core):
        return {"kind": "drill_focuses"}
    flash = _FLASH.match(core)
    if flash:
        spec = _drill_spec("flash " + (flash.group("topic") or ""))
        if spec is not None:
            return spec
    for pattern in (_DRILL_ME, _DRILL_START, _DRILL_ADJECTIVE):
        match = pattern.match(core)
        if match:
            groups = match.groupdict()
            words = " ".join(part for part in (groups.get("pre"), groups.get("rest"), groups.get("topic")) if part)
            spec = _drill_spec(words)
            if spec is not None:
                return spec
            break

    for pattern in _TODAY:
        match = pattern.match(core)
        if match:
            return {"kind": "today", "minutes": _minutes(match)}

    for index, pattern in enumerate(_REMINDER):
        match = pattern.match(core)
        if match:
            rest = match.group("rest") or ""
            adjective = (match.groupdict().get("adj") or "")
            at, leftover = _reminder_time(rest)
            if index == 1 and not _REMIND_NEEDS.search(rest):
                break  # 'remind me what a payload is' is a question, not a daily reminder
            if not _only_reminder_words(adjective + " " + leftover):
                break
            return {"kind": "reminder_setup", "at": at or "19:00"}

    for pattern in _JOURNAL_SUMMARY:
        match = pattern.match(core)
        if match:
            return {"kind": "journal_summary", "day": _day(match.groupdict().get("when"))}
    for pattern in _JOURNAL:
        match = pattern.match(core)
        if match:
            groups = match.groupdict()
            when = groups.get("when")
            return {"kind": "journal", "prim": groups.get("path"), "since": _day(when) if when else None}

    layout = _parse_layout(core)
    if layout is not None:
        return layout
    robot = _parse_robot(core)
    if robot is not None:
        return robot
    return _parse_docker(core)
