"""Plain-English phrases for the learning helpers: Python snippets, practice exercises, error explanations,
homework checks, the scene tour and the class-notes search.

Pure string parsing for router.py: nothing here reads the scene, a file or the class notes. parse(text) takes the
request as the user typed it (case kept: prim paths and pasted errors are case-sensitive) and returns a small spec
dict {"kind": ..., ...} or None when the text is not one of these requests. Every kind is read-only; the router
runs the matching MCP tool (python_show, python_practice_*, explain_error, assignment_check, scene_explain,
class_ask, ...).
"""

from __future__ import annotations

import re
from typing import Any

_FLAGS = re.IGNORECASE
_POLITE = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)[,!\s]+)?(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?|please\s+|kindly\s+|"
    r"i\s+(?:want|would\s+like|need)\s+(?:you\s+)?to\s+)?",
    _FLAGS,
)
# Trailing punctuation, then an optional ' please', then more punctuation. Stripped with str.rstrip, not a regex:
# r"[\s?.!]*(?:\s+please)?[\s?.!]*$" backtracks cubically on a long run of dots or spaces (seconds at 800 dots).
_TRAILING_CHARS = " \t\n\r\f\v?.!"
# Learning-helper phrases are short; longer text (other than a pasted error) is never one of them. The cap also
# bounds the quadratic number/path/file searches below, since parse() runs on every router request.
MAX_PHRASE_CHARS = 1_000
_PRIM_PATH = re.compile(r"(?<![\w/])(/[A-Za-z_][A-Za-z0-9_]*(?:/[A-Za-z_][A-Za-z0-9_]*)*)(?![\w/.])")
_USD_FILE = re.compile(r"(?P<file>(?:[A-Za-z]:)?[^\s\"'<>|?*]*\.usd[acz]?)(?=[\s\"',;)]|$)", _FLAGS)
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_TRIPLE = re.compile(rf"(?P<x>{_NUMBER})[,\s]+(?P<y>{_NUMBER})[,\s]+(?P<z>{_NUMBER})")

# --- explain an error ---------------------------------------------------------------------------------------------
_ERROR_NOUN = r"(?:error(?:\s+message)?|errors|traceback|exception|stack\s*trace|warning|crash)"
_ERROR_INTRO = re.compile(
    r"^(?:explain|decode|interpret|translate|debug|fix|solve|help\s+(?:me\s+)?(?:with|understand|fix|debug)|"
    r"what\s+does|what(?:'s|\s+is)|what\s+causes|why\s+(?:do|did|am)\s+i\s+(?:get|getting|see|seeing)|"
    r"i\s+(?:got|get|have|see)|i'?m\s+(?:getting|seeing)|i\s+am\s+(?:getting|seeing)|"
    r"what\s+went\s+wrong\s+with)\s+"
    rf"(?P<det>this|the|my|that|an?|these)?\s*(?:python\s+|kit\s+|usd\s+|pxr\s+|isaac(?:\s+sim)?\s+|script\s+editor\s+|"
    rf"composer\s+|console\s+)?{_ERROR_NOUN}"
    r"(?:\s+(?:mean|means|say|says|about))?(?:\s+(?:in|from)\s+(?:isaac(?:\s+sim)?|kit|composer|the\s+script\s+editor))?"
    r"(?P<sep>\s*[:\-]\s*|\s*\n\s*|\s+|$)",
    _FLAGS,
)
_PASTED_ERROR = re.compile(
    r"^\s*(?:Traceback \(most recent call last\)|\[Error\]|\[Warning\]|"
    r"(?:[A-Za-z_][\w.]*\.)?[A-Z]\w*(?:Error|Exception):|Coding Error|Runtime Error|Error in ')"
)

# --- Python snippets ----------------------------------------------------------------------------------------------
_CODE = r"(?:python(?:\s+code|\s+script|\s+snippet)?|pxr\s+code|usd\s+python(?:\s+code)?|openusd\s+python|code|script|snippet)"
_QUALIFIER = r"(?:(?:standalone|stand-alone|plain|simple|short|beginner|basic|usd|openusd|pxr|script\s+editor)\s+)?"
_SHOW_PYTHON = (
    re.compile(
        rf"^(?:show|give|tell|write|teach|send)\s+(?:me\s+)?(?:the\s+|some\s+|a\s+|an\s+|example\s+)?(?:example\s+)?{_QUALIFIER}{_CODE}"
        r"\s+(?:for|to|that|which|of|on|about|behind|doing|showing)\s+(?P<task>.+)$",
        _FLAGS,
    ),
    re.compile(
        rf"^how\s+(?:do|can|would|should|could)\s+i\s+(?:write\s+|use\s+|do\s+|code\s+)?(?:a\s+|some\s+)?{_QUALIFIER}{_CODE}"
        r"\s+(?:to|for|that|which)\s+(?P<task>.+)$",
        _FLAGS,
    ),
    re.compile(
        r"^how\s+(?:do|can|would|should|could)\s+i\s+(?P<task>.+?)\s+(?:in|with|using|from|via)\s+"
        r"(?:python|pxr|the\s+usd\s+api|usd\s+python|a\s+(?:python\s+)?script|code|the\s+script\s+editor)$",
        _FLAGS,
    ),
    re.compile(rf"^(?:what(?:'s|\s+is)\s+the\s+)?{_CODE}\s+(?:for|to)\s+(?P<task>.+)$", _FLAGS),
    re.compile(r"^(?P<task>.+?)\s+in\s+python$", _FLAGS),
)
_PYTHON_TOPICS = re.compile(
    r"^(?:(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?)?python\s+(?:topics|snippets|examples)$"
    r"|^what\s+(?:python|code|snippets)\s+can\s+you\s+(?:show|teach|give)(?:\s+me)?$"
    r"|^(?:show|teach)\s+me\s+(?:the\s+|some\s+)?python$",
    _FLAGS,
)
_STANDALONE = re.compile(
    r"\b(?:standalone|stand-alone|outside\s+(?:of\s+)?(?:isaac|kit|composer|omniverse|the\s+app)|plain\s+python|"
    r"usd-core|usd\s+core|from\s+a\s+(?:\.usda\s+)?file|in\s+a\s+terminal|without\s+(?:isaac|kit|omniverse))\b",
    _FLAGS,
)
# (pattern, action): first match wins, so specific tasks come before generic verbs such as "add" or "make".
_TASKS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, _FLAGS), action)
    for pattern, action in (
        (r"\bphysics\s*scene\b|\bgravity\b", "add_physics_scene"),
        (r"\brigid\s*bod|\bdynamic\b|\bfall(?:s|ing)?\b", "make_rigid_body"),
        (r"\bcollider|\bcollision|\bcollid", "make_collider"),
        (r"\bpayload", "add_payload"),
        (r"\breferenc|\basset\b|\bimport\b|\bbring\s+in\b", "add_reference"),
        (r"\bsub\s*-?\s*layer", "add_sublayer"),
        (r"\bedit\s*target", "set_edit_target"),
        (r"\b(?:select|switch|choose|pick|change|set)\w*\s+(?:a\s+|the\s+)?variant\b|\bvariant\s+selection\b",
         "select_variant"),
        (r"\bvariant", "create_variant_set"),
        (r"\b(?:bind|assign|apply)\w*\b.*\bmaterial|\bmaterial\s+binding", "bind_material"),
        (r"\bmaterial|\bshader|\bcolou?r\b|\bpaint|\bpreviewsurface", "create_material"),
        (r"\bcamera", "create_camera"),
        (r"\blight|\blamp|\bsun\b|\bdome\b|\bhdri\b", "create_light"),
        (r"\btime\s*samples?|\bkey\s*frames?|\banimat", "time_samples"),
        (r"\battribute|\battr\b|\bproperty", "set_attribute"),
        (r"\brenam", "rename"),
        (r"\bduplicat|\bcopy|\bclon", "duplicate"),
        (r"\bdelet|\bremov|\bdeactivat", "delete"),
        (r"\borient|\bquaternion|\bisaac\s+(?:style\s+)?transform", "set_transform_isaac"),
        (r"\brotat|\bspin|\bturn", "rotate"),
        (r"\bscal|\bresiz|\bbigger\b|\bsmaller\b", "scale"),
        (r"\bmov(?:e|es|ed|ing)\b|\btranslat|\bposition|\bplac(?:e|ing)\b", "move"),
        (r"\bexport|\bflatten", "export"),
        (r"\bsav(?:e|es|ing)\b", "save"),
        (r"\bundo", "undo_hint"),
        (r"\bchildren\b|\bchild\s+prims?\b", "list_children"),
        (r"\btravers|\bevery\s+prim|\ball\s+(?:the\s+)?prims|\blist\s+(?:the\s+)?prims|\bwalk|\biterat|\bloop\s+over",
         "traverse"),
        (r"\binspect|\bread\b|\bprint\b|\blook\s+at\b|\bget\s+(?:a\s+)?prim\b|\bfind\s+(?:a\s+)?prim\b", "inspect_prim"),
        (r"\b(?:creat|add|mak|defin|new|spawn)\w*\b", "create_prim"),
        (r"\b(?:cube|box|sphere|ball|cylinder|cone|capsule|plane|mesh|xform|scope)s?\b", "create_prim"),
    )
)
_PRIM_TYPE_WORDS = {
    "cube": "Cube", "box": "Cube", "sphere": "Sphere", "ball": "Sphere", "cylinder": "Cylinder", "cone": "Cone",
    "capsule": "Capsule", "plane": "Plane", "ground plane": "Plane", "floor": "Plane", "mesh": "Mesh",
    "xform": "Xform", "group": "Xform", "scope": "Scope", "folder": "Scope",
}
_LIGHT_WORDS = {
    "rect": "rect", "rectangle": "rect", "area": "rect", "sphere": "sphere", "point": "sphere", "disk": "disk",
    "disc": "disk", "spot": "disk", "distant": "distant", "sun": "distant", "directional": "distant",
    "dome": "dome", "sky": "dome", "hdri": "dome", "environment": "dome", "cylinder": "cylinder", "tube": "cylinder",
}

# --- Python practice exercises ------------------------------------------------------------------------------------
_LEVEL_WORDS = {"beginner": "beginner", "easy": "beginner", "basic": "beginner", "simple": "beginner",
                "intermediate": "intermediate", "medium": "intermediate", "harder": "intermediate",
                "advanced": "intermediate"}
_LEVEL = r"(?:(?P<level>beginner|easy|basic|simple|intermediate|medium|harder|advanced)\s+(?:level\s+)?)?"
_PRACTICE_NOUN = r"(?:exercises?|practice|challenges?|drills?|tasks?|problems?|katas?)"
_PRACTICE_LANG = r"(?:python|usd\s+python|openusd\s+python|pxr|coding|code|scripting)"
_PRACTICE = (
    re.compile(
        rf"^(?:give|show|get|find|set|send|hand)\s+(?:me\s+)?(?:a|an|another|one|some|the\s+next|my\s+next|a\s+new|"
        rf"next)?\s*{_LEVEL}{_PRACTICE_LANG}\s+{_PRACTICE_NOUN}(?:\s+(?:for|to\s+do))?(?:\s+(?:today|now))?$",
        _FLAGS,
    ),
    re.compile(rf"^(?:next|new|another)?\s*{_LEVEL}{_PRACTICE_LANG}\s+{_PRACTICE_NOUN}$", _FLAGS),
    re.compile(rf"^(?:i\s+want\s+to\s+|let'?s\s+|let\s+me\s+)?practi[cs]e\s+(?:my\s+|some\s+)?{_LEVEL}{_PRACTICE_LANG}"
               r"(?:\s+(?:coding|with\s+usd|for\s+usd|with\s+openusd))?$", _FLAGS),
    re.compile(rf"^(?:test|quiz)\s+me\s+(?:on|with)\s+(?:some\s+)?{_LEVEL}{_PRACTICE_LANG}(?:\s+{_PRACTICE_NOUN})?$", _FLAGS),
)
_PRACTICE_LIST = re.compile(
    rf"^(?:(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:the\s+|my\s+)?)?{_LEVEL}{_PRACTICE_LANG}\s+(?:exercises|challenges|drills)"
    r"(?:\s+list)?$|^what\s+python\s+(?:exercises|challenges)\s+(?:are\s+there|do\s+you\s+have|can\s+i\s+do)$",
    _FLAGS,
)

# --- homework -----------------------------------------------------------------------------------------------------
_HW = r"(?:homework|home\s+work|assignment|assignments|hw|class\s+task|warehouse\s+(?:homework|assignment|task|scene))"
_SESSION = r"(?:(?:session|class|lesson)\s*0*(?P<session>\d{1,3})(?:'s)?\s+)?"
_HOMEWORK = (
    re.compile(
        rf"^(?:check|grade|mark|review|score|evaluate|test|verify|look\s+at|assess|rate)\s+(?:my|the|this|our)?\s*"
        rf"{_SESSION}{_HW}(?:\s+(?:scene|file|progress|work))?(?:\s+(?:for|in|on|against|using|with)\s+(?P<rest>.+))?$",
        _FLAGS,
    ),
    re.compile(
        rf"^(?:how(?:'s|\s+is)|is|how\s+am\s+i\s+doing\s+(?:on|with)|how\s+far\s+(?:along\s+)?am\s+i\s+(?:on|with)|"
        rf"am\s+i\s+done\s+with)\s+(?:my|the)\s+{_SESSION}{_HW}(?:\s+(?:going|done|finished|complete|correct|ok|okay|"
        rf"right|good|passing))?(?:\s+yet)?$",
        _FLAGS,
    ),
    re.compile(
        rf"^what(?:'s|\s+is|\s+do\s+i\s+(?:do|need\s+to\s+do|have\s+to\s+do))?\s+(?:next|left|missing|remaining|"
        rf"still\s+(?:missing|left|to\s+do))\s+(?:on|in|for|with)\s+(?:my|the)\s+{_SESSION}{_HW}$",
        _FLAGS,
    ),
    re.compile(
        rf"^what\s+(?:should|do|must)\s+i\s+(?:do|fix|add)\s+next\s+(?:on|for|in|with)\s+(?:my|the)\s+{_SESSION}{_HW}$",
        _FLAGS,
    ),
    re.compile(rf"^(?:my\s+)?{_SESSION}{_HW}\s+(?:status|check|progress|score|next\s+step|feedback)$", _FLAGS),
    re.compile(rf"^(?:did\s+i\s+(?:pass|finish|complete)|have\s+i\s+(?:passed|finished|completed))\s+(?:my|the)\s+{_SESSION}{_HW}$",
               _FLAGS),
)
_HOMEWORK_NEXT = re.compile(r"\b(?:next|left|missing|remaining|still)\b", _FLAGS)
_HOMEWORK_LIST = re.compile(
    rf"^(?:(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?)(?:homeworks?|assignments|homework\s+list|assignment\s+list)$"
    r"|^what\s+(?:homework|assignments?)\s+(?:do\s+i\s+have|are\s+there|is\s+there)$",
    _FLAGS,
)

# --- explain my scene ---------------------------------------------------------------------------------------------
_SCENE_WORD = r"(?:scene|stage|world|level|setup|project\s+scene)"
_SCENE_TOUR = (
    re.compile(
        rf"^(?:explain|describe|summari[sz]e|walk\s+me\s+through|tour|give\s+me\s+a\s+tour\s+of|take\s+me\s+on\s+a\s+tour\s+of|"
        rf"tell\s+me\s+about|break\s+down|go\s+through)\s+(?:my|the|this|our)\s+(?:whole\s+|current\s+|open\s+|entire\s+)?"
        rf"{_SCENE_WORD}(?:\s+(?:under|at|below|from|starting\s+at|for|in)\s+(?P<path>/\S+))?"
        rf"(?:\s+(?:to\s+me|for\s+me|like\s+i'?m\s+(?:a\s+)?(?:beginner|five|new)|simply|in\s+plain\s+(?:english|words)|"
        rf"step\s+by\s+step))*$",
        _FLAGS,
    ),
    re.compile(rf"^(?:(?:a|my|the)\s+)?(?:scene|stage)\s+tour(?:\s+(?:of|for|under)\s+(?P<path>/\S+))?$", _FLAGS),
    re.compile(r"^(?:give\s+me\s+a\s+)?tour\s+(?:of\s+)?(?P<path>/\S+)$", _FLAGS),
    re.compile(r"^(?:explain|describe)\s+(?:what(?:'s|\s+is)\s+)?(?:in|under|inside|below)\s+(?P<path>/\S+)$", _FLAGS),
    re.compile(rf"^what(?:'s|\s+is)\s+in\s+(?:my|this)\s+(?:open\s+|current\s+)?{_SCENE_WORD}"
               r"(?:\s+(?:explained|in\s+plain\s+words|for\s+a\s+beginner))?$", _FLAGS),
)

# --- class notes --------------------------------------------------------------------------------------------------
_TEACHER = r"(?:teacher|mentor|instructor|trainer|tutor|lecturer|professor|coach|sir|ma'?am|presenter)"
_SAY = r"(?:say|said|tell\s+us|tell\s+me|told\s+us|explain|explained|teach|taught|show|showed|mention|mentioned|recommend|suggest)"
_CLASS = (
    re.compile(
        rf"^(?:what|how|when|where|why)\s+did\s+(?:the|my|our)\s+{_TEACHER}\s+{_SAY}\s*(?:us\s+)?(?:about|on|regarding|for|"
        rf"concerning)?\s+(?P<q>.+?)(?:\s+(?:in|during)\s+(?:session|class|lesson)\s*0*(?P<session>\d{{1,3}}))?$",
        _FLAGS,
    ),
    re.compile(
        rf"^did\s+(?:the|my|our)\s+{_TEACHER}\s+{_SAY}\s+(?:anything\s+)?(?:about|on)\s+(?P<q>.+?)"
        rf"(?:\s+(?:in|during)\s+(?:session|class|lesson)\s*0*(?P<session>\d{{1,3}}))?$",
        _FLAGS,
    ),
    re.compile(
        r"^what\s+did\s+(?:we|i)\s+(?:learn|cover|do|see|study|talk\s+about)\s+(?:in|during)\s+(?:the\s+)?"
        r"(?:class|lecture|lesson|session\s*0*(?P<session>\d{1,3})|(?:last|first|latest)\s+(?:class|session|lesson))"
        r"(?:\s+(?:about|on|regarding)\s+(?P<q>.+))?$",
        _FLAGS,
    ),
    re.compile(
        r"^(?:search|check|look\s+(?:in|through)|find\s+(?:in|it\s+in)|grep)\s+(?:my\s+|the\s+)?(?:class|session|lecture|course|"
        r"mentorship|teacher'?s?)\s+(?:notes|transcripts?|recordings?|subtitles|videos?)\s+(?:for|about|on)\s+(?P<q>.+)$",
        _FLAGS,
    ),
    re.compile(
        r"^(?:in|during)\s+(?:the\s+)?(?:class|lecture|session\s*0*(?P<session>\d{1,3})),?\s+(?P<q>(?:what|how|when|why|where|did|"
        r"was|were|which)\b.+)$",
        _FLAGS,
    ),
    re.compile(r"^(?:class|session|lecture|course)\s+notes\s+(?:about|on|for)\s+(?P<q>.+)$", _FLAGS),
)
_CLASS_SESSIONS = re.compile(
    r"^(?:(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?)?(?:class|mentorship|course)\s+sessions$"
    r"|^what\s+(?:class\s+)?sessions\s+(?:do\s+i\s+have|are\s+there|have\s+we\s+had)$"
    r"|^(?:list|show)\s+(?:me\s+)?(?:all\s+)?(?:my\s+|the\s+)?(?:class|lecture)\s+(?:recordings|notes)$",
    _FLAGS,
)


def _strip(text: str) -> str:
    core = _POLITE.sub("", text.strip(), count=1).rstrip(_TRAILING_CHARS)
    if core[-6:].lower() == "please" and len(core) > 6 and core[-7].isspace():
        core = core[:-6].rstrip(_TRAILING_CHARS)
    return core.strip()


def _session(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _path(text: str) -> str | None:
    found = _PRIM_PATH.search(text)
    return found.group(1) if found else None


def _vector(text: str) -> tuple[float, float, float] | None:
    found = _TRIPLE.search(text)
    if not found:
        return None
    return (float(found.group("x")), float(found.group("y")), float(found.group("z")))


def _python_args(action: str, task: str) -> dict[str, Any]:
    """Values the sentence names: the prim path, a prim/light type and an x y z triple; the rest stays default."""
    args: dict[str, Any] = {}
    path = _path(task)
    lowered = task.lower()
    if action == "duplicate" and path:
        args["source_path"] = path
    elif action == "create_material" and path:
        args["bind_to"] = path
    elif action in ("bind_material",) and path:
        args["prim_path"] = path
    elif path and action not in ("traverse", "save", "export", "add_sublayer", "set_edit_target"):
        args["prim_path"] = path
    if action == "create_prim":
        for word in sorted(_PRIM_TYPE_WORDS, key=len, reverse=True):
            if re.search(rf"\b{word}s?\b", lowered):
                args["type_name"] = _PRIM_TYPE_WORDS[word]
                if "prim_path" not in args:
                    args["prim_path"] = f"/World/My{_PRIM_TYPE_WORDS[word]}"
                break
    if action == "create_light":
        for word, light in _LIGHT_WORDS.items():
            if re.search(rf"\b{word}\b", lowered):
                args["light_type"] = light
                break
    triple = _vector(task)
    if triple is not None:
        key = {"move": "translate", "rotate": "rotate", "scale": "scale", "create_camera": "position",
               "create_light": "translate"}.get(action)
        if key:
            args[key] = triple
    elif action == "scale":
        single = re.search(rf"\b(?:by|to|x)\s*(?P<n>{_NUMBER})\b|(?P<m>{_NUMBER})\s*(?:x|times)\b", lowered)
        if single:
            number = float(single.group("n") or single.group("m"))
            args["scale"] = (number, number, number)
    return args


def python_task(task: str) -> dict[str, Any] | None:
    """The python_teacher action and args a task description names ('moving a cube', 'add a dome light'); else None."""
    text = task.strip()
    for pattern, action in _TASKS:
        if pattern.search(text):
            return {"action": action, "args": _python_args(action, text)}
    return None


def parse(text: str) -> dict[str, Any] | None:
    """{"kind": ..., ...} for a learning-helper request, else None. Never reads anything."""
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if _PASTED_ERROR.match(raw):
        return {"kind": "explain_error", "text": raw}
    lead = _POLITE.sub("", raw, count=1)
    intro = _ERROR_INTRO.match(lead)
    if intro:
        rest = lead[intro.end():].strip()
        # 'explain this error' (pointing at a paste) or 'I got an error: <text>' (the text follows a colon or line).
        pointed = (intro.group("det") or "").lower() in ("this", "my", "that", "these")
        separator = intro.group("sep") or ""
        pasted = bool(rest) and (separator.strip() in (":", "-") or "\n" in separator)
        if pointed or pasted:
            return {"kind": "explain_error", "text": rest}
    if len(raw) > MAX_PHRASE_CHARS:
        return None
    core = _strip(raw)
    if not core:
        return None
    one_line = re.sub(r"\s+", " ", core)
    if _PYTHON_TOPICS.match(one_line):
        return {"kind": "python_topics"}
    if _PRACTICE_LIST.match(one_line):
        level = _PRACTICE_LIST.match(one_line).groupdict().get("level")
        return {"kind": "python_practice_list", "level": _LEVEL_WORDS.get((level or "").lower())}
    for pattern in _PRACTICE:
        match = pattern.match(one_line)
        if match:
            level = match.groupdict().get("level")
            return {"kind": "python_exercise", "level": _LEVEL_WORDS.get((level or "").lower())}
    for pattern in _SHOW_PYTHON:
        match = pattern.match(one_line)
        if match:
            task = match.group("task").strip()
            if pattern is _SHOW_PYTHON[-1] and not re.search(r"^(?:how|show|write|make|add|create|move)\b", task, _FLAGS):
                continue  # 'X in python' only when X reads as a task
            found = python_task(task)
            context = "standalone" if _STANDALONE.search(one_line) else "script_editor"
            if found is None:
                return {"kind": "python_topics", "task": task, "context": context}
            return {"kind": "python_show", "task": task, "context": context, **found}
    if _HOMEWORK_LIST.match(one_line):
        return {"kind": "assignment_list"}
    for pattern in _HOMEWORK:
        match = pattern.match(one_line)
        if match:
            scene_file = _USD_FILE.search(core)
            return {
                "kind": "assignment_check",
                "scene_file": scene_file.group("file") if scene_file else None,
                "session": _session(match.groupdict().get("session")),
                "next_step_only": bool(_HOMEWORK_NEXT.search(one_line)),
            }
    for pattern in _SCENE_TOUR:
        match = pattern.match(one_line)
        if match:
            path = match.groupdict().get("path")
            return {"kind": "scene_explain", "focus_path": path.rstrip(".,;:") if path else None}
    if _CLASS_SESSIONS.match(one_line):
        return {"kind": "class_sessions"}
    for pattern in _CLASS:
        match = pattern.match(one_line)
        if match:
            groups = match.groupdict()
            question = (groups.get("q") or "").strip()
            session = _session(groups.get("session"))
            if not question:
                question = "summary of the main points and next steps"
            return {"kind": "class_ask", "question": question, "session": session}
    return None
