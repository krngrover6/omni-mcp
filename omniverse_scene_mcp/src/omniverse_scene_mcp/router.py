from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
import os
import re
from typing import Any, Protocol
from urllib.parse import unquote

from . import help as tool_guide
from . import nl_tier4, nl_tier5
from .service import OperationResult, SceneMCPService

_LEARNING_PHRASES = (
    "teach me",
    "learn",
    "explain",
    "lesson",
    "tutorial",
    "cheat sheet",
    "cheatsheet",
    "practice",
    "drill",
    "hands on",
    "hands-on",
    "study",
    "next step",
    "what should i do next",
    "suggest",
    "recommend",
)

# Whole words, so "kitchen" or "toolkit" do not match and file names such as scene.usd or app.kit stay paths;
# a bare extension (".usd file") still counts.
_USD_WORD = re.compile(r"(?<!\w)(?<!\w\.)usd\b")
_KIT_WORD = re.compile(r"(?<!\w)(?<!\w\.)kit\b")

# "can you move the cube up in kit?" -> "move the cube up": polite wrappers and app names around a scene request.
_POLITE_PREFIX = re.compile(r"^(?:hey\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:(?:please|pls|plz|kindly)\s+)*")
_POLITE_SUFFIX = re.compile(
    r"(?:\s+(?:in|inside|using)\s+(?:usd composer|composer|omniverse kit|kit|usd|isaac sim|isaac))?"
    r"(?:\s+(?:please|pls|plz|for me))?\s*[?.!]*$"
)
_SCENE_VERB = re.compile(
    r"^(?:move|shift|nudge|rotate|scale|make|select|focus|highlight|inspect|delete|remove|duplicate|"
    r"rename|reparent|put|nest|find|search|add|create|spawn|place|save|bind|assign|paint)\b"
)
_TARGET_STOP_WORDS = {
    "up", "down", "upward", "upwards", "downward", "downwards", "left", "right", "forward", "forwards", "back",
    "backward", "backwards", "by", "to", "as", "in", "on", "under", "with", "into", "onto", "inside", "from",
    "bigger", "larger", "smaller", "twice", "half", "double",
}
_NAME_ARTICLES = {"the", "a", "an", "this", "that", "it", "selected", "my", "our"}
_PRONOUN = re.compile(r"\b(?:selected|this|that|it)\b")
# A whole target phrase that means "the selection" ("this", "it", "the selected light"). Edits use this, not
# _PRONOUN.search, so "the cube that is red" or "the sphere next to it" never retargets the selected prim.
_SELECTION_REF = re.compile(r"(?:the\s+)?(?:selected|this|that|it)(?:\s+[\w-]+)?", re.IGNORECASE)
# A whole target phrase that is exactly one prim path ("/World/Box", "the prim /World/Box"). An edit applies to a
# path only when the phrase is this bare path, never to a path that merely appears inside it ("everything except
# /World/Hero"), the same rule delete already follows.
_BARE_PATH = re.compile(r"(?:the\s+)?(?:prim\s+)?(/(?:[\w\-.]+/)*[\w\-.]+)", re.IGNORECASE)
# The material side of a binding: exactly one material path ("/World/Looks/Red", "the material /World/Looks/Red").
_BARE_MATERIAL_PATH = re.compile(r"(?:the\s+)?(?:material\s+)?(/(?:[\w\-.]+/)*[\w\-.]+)", re.IGNORECASE)
# 'set the position/rotation/scale/name of X to ...' are not attributes to write directly: the working command.
_TRANSFORM_ATTRIBUTE_WORDS = {
    "position": "move", "location": "move", "translate": "move", "translation": "move",
    "rotation": "rotate", "rotate": "rotate", "scale": "scale", "name": "rename",
}

# Questions about the open stage keep their scene-aware routing instead of going to a docs backend.
_LIVE_SCENE = re.compile(r"(?:^|\s)/\w|\bselect(?:ed|ion)\b|\bmy\b|\b(?:this|current) (?:scene|stage)\b|\bhow many\b")

# Whole-word lesson topics, so "remove" is not "move", "lightweight" is not "light" and "primvars" is not "prims".
_FALLBACK_TOPICS = (
    ("schemas", re.compile(r"\b(?:schemas?|subtypes?)\b")),
    ("prims", re.compile(r"\b(?:prims?|stages?|scene graph|hierarch(?:y|ies))\b")),
    ("selection", re.compile(r"\b(?:select(?:s|ed|ing|ion)?|focus|highlight)\b")),
    ("transforms", re.compile(r"\b(?:mov(?:e|es|ed|ing)|rotat(?:e|es|ed|ing|ions?)|scal(?:e|es|ed|ing)|transform(?:s|ations?)?)\b")),
    ("materials", re.compile(r"\b(?:materials?|shade|shaders?|shading)\b")),
    ("lights", re.compile(r"\b(?:lights?|lighting)\b")),
)
_WORKFLOW_TOPIC = re.compile(r"\b(?:workflows?|cheat ?sheets?|quickstart|commands|how do i start)\b")

# NCP-OUSD exam vocabulary: a question using these terms is an OpenUSD concept question. "kind of" is English.
_USD_EXAM_TERMS = re.compile(
    r"\b(?:inherits?|speciali[sz]es|sub-?layers?|variants?|variant ?sets?|payloads?|live?rps|primvars?|"
    r"kinds?(?! of\b)|model hierarch(?:y|ies)|instanceable|instancing|meters ?per ?unit|up ?axis|"
    r"flatten(?:s|ed|ing)?|edit ?targets?|layer ?offsets?|opinion strength|strength order(?:ing)?|value resolution)\b"
)

# "how many prims are in this stage?" asks for the open stage's total; per-type or per-subtree counts do not.
_PRIM_COUNT = re.compile(r"^how many (?:usd )?prims?\b(.*)$")
_NOT_A_TOTAL = re.compile(
    r"/|\b(?:types?|kinds?|of|under|below|inside|within|with|named|called|layers?|can|could|should|max(?:imum)?|limit)\b"
)

# "set intensity of X to 500" / "set X's intensity to 500": attribute edits, not material bindings.
_SET_ATTRIBUTE = (
    re.compile(
        r"^(?:set|change|adjust)\s+(?:the\s+)?(?P<attr>[\w:.]+(?:\s+[\w:.]+)?)\s+of\s+(?P<target>.+?)\s+to\s+(?P<value>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:set|change|adjust)\s+(?P<target>.+?)'s\s+(?P<attr>[\w:.]+(?:\s+[\w:.]+)?)\s+to\s+(?P<value>.+)$",
        re.IGNORECASE,
    ),
)

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_VECTOR = rf"({_NUMBER})[,\s]+({_NUMBER})[,\s]+({_NUMBER})"
_SCALE_WORDS = {"twice": 2.0, "double": 2.0, "thrice": 3.0, "half": 0.5}
_EDIT_ACTIONS = {"move", "rename", "delete", "duplicate", "reparent", "material"}
_NAME_ACTIONS = _EDIT_ACTIONS | {"inspect", "children", "select"}
# Objects after these verbs are new things or search text, never an existing prim to resolve by name.
_NAME_CREATE_VERBS = {"add", "create", "spawn", "place", "find", "search", "save"}
_DEFAULT_TARGET = "/World/Props/Cube"
_PRACTICE_TOPICS = ("schemas", "prims", "selection", "transforms", "materials", "lights", "workflow")

# NCP-OUSD daily lessons: "start day 3", "day one", "lesson for day 2", "next lesson", "today's lesson".
_DAY_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3,
}
_EXAM_VERBS = (
    r"(?:(?:let'?s|i want to|i'd like to)\s+)?"
    r"(?:(?:start|begin|open|show(?:\s+me)?|give\s+me|do|run|load|resume|continue(?:\s+with)?|go\s+to|"
    r"take\s+me\s+to|move\s+on\s+to|on\s+to)\s+)?"
)
_EXAM_DAY = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?" + _EXAM_VERBS + r"(?:(?:the|my)\s+)?(?:lesson\s+for\s+)?(?:(?:exam|study|plan)\s+)?"
    r"day\s+(?P<day>\d{1,4}|" + "|".join(_DAY_WORDS) + r")"
    r"(?:\s+(?:lesson|of\s+(?:the|my)\s+(?:study\s+|exam\s+)?plan|(?:study\s+|exam\s+)?plan))?$"
)
_NEXT_LESSON = re.compile(
    r"^(?:what(?:'s|\s+is)\s+)?" + _EXAM_VERBS + r"(?:(?:the|my)\s+)?(?:next|today'?s)\s+(?:exam\s+|study\s+|daily\s+)?"
    r"(?:lesson|day|study\s+day|session)$"
    r"|^(?:continue|resume)\s+(?:(?:my|the)\s+)?(?:study\s+plan|exam\s+plan|daily\s+lessons?|exam\s+prep)$"
)
# Timed mock exams are never started from a sentence: the router explains how instead.
# 'mock' needs a separator and 'full test' is left out, so prim names (MockTest) and 'full test suite' stay scene/usd.
_MOCK_EXAM = re.compile(
    r"\bmock[\s-]+(?:exams?|tests?)\b|\b(?:full|timed)\s+(?:practice\s+)?exams?\b|\btimed\s+(?:practice\s+)?tests?\b"
    r"|\bpractice\s+exams?\b|\bexam\s+simulation\b"
)
MOCK_EXAM_DEFAULTS = {"count": 65, "minutes": 120}
_USD_SUFFIXES = (".usd", ".usda", ".usdc", ".usdz")

# --- help ("help", "what can you do", "how do I use this", "list tools", "help lights") -------------------------
_HELP_REQUEST = re.compile(
    r"^(?:(?:show|give|get)\s+(?:me\s+)?(?:some\s+)?)?help(?:\s+me)?"
    r"(?:\s+(?:with|on|about|for)\s+(?P<topic>.+)|\s+(?P<bare>[\w.:&/ -]+))?$"
    r"|^/help$|^\?$"
    r"|^what\s+(?:else\s+)?can\s+(?:you|i)\s+do(?:\s+(?:here|with\s+(?:this|you|it|the\s+router)))?$"
    r"|^what\s+(?:do|can)\s+you\s+(?:support|understand)$"
    r"|^how\s+(?:do|can|should)\s+i\s+(?:use|start\s+using)\s+"
    r"(?:this|you|it|the\s+router|this\s+router|this\s+tool|these\s+tools|the\s+tools)$"
    r"|^(?:list|show(?:\s+me)?|what\s+are)\s+(?:all\s+)?(?:(?:the|your|available)\s+)*tools(?:\s+(?:you\s+have|available))?$"
    r"|^(?:what|which)\s+tools\s+(?:do\s+you\s+have|are\s+there|are\s+available|can\s+i\s+use)$"
    r"|^tools?\s+guide$"
)

# --- undo -----------------------------------------------------------------------------------------------------------
_UNDO_REQUEST = re.compile(
    r"^undo(?:\s+(?:that|it|this|(?:the\s+|my\s+)?last(?:\s+(?:change|edit|step|action|one|thing(?:\s+you\s+did)?))?|"
    r"what\s+you\s+(?:just\s+)?did))?$"
)
# Only these apply an undo (and only when the request is not a dry run); plain "undo" previews.
_UNDO_CONFIRM = re.compile(
    r"^(?:confirm(?:ed)?\s+undo|undo\s+(?:it\s+|that\s+)?(?:now|for\s+real)|yes,?\s+undo(?:\s+(?:it|that))?|"
    r"really\s+undo(?:\s+(?:it|that))?|apply\s+(?:the\s+)?undo)$"
)
_UNDO_HISTORY = re.compile(
    r"^(?:what\s+(?:did|have)\s+you\s+(?:just\s+)?(?:change|changed)|what\s+changed|what\s+(?:can|could)\s+i\s+undo|"
    r"(?:show|list|give)(?:\s+me)?\s+(?:the\s+|my\s+|your\s+)?(?:recent\s+|last\s+)?"
    r"(?:changes|edits|undo\s+history|change\s+history|history\s+of\s+changes)|(?:undo|change)\s+history|recent\s+changes)$"
)

# --- shapes, lights, cameras, materials and colors ----------------------------------------------------------------
# New shapes match whole words ("airplane" is not a plane); "cube" keeps its old substring match.
_SHAPE_WORDS = (
    ("capsule", "Capsule"),
    ("cylinder", "Cylinder"),
    ("cone", "Cone"),
    ("sphere", "Sphere"),
    ("ball", "Sphere"),
    ("plane", "Plane"),
    ("box", "Cube"),
)
_LIGHT_KINDS = {
    "rect": "rect", "rectangle": "rect", "rectangular": "rect", "area": "rect",
    "disk": "disk", "disc": "disk",
    "dome": "dome", "sky": "dome", "hdri": "dome",
    "distant": "distant", "sun": "distant", "directional": "distant",
    "sphere": "sphere", "point": "sphere",
    "cylinder": "cylinder", "tube": "cylinder",
}
_LIGHT_TYPE_NAMES = {
    "rect": "RectLight", "disk": "DiskLight", "dome": "DomeLight", "distant": "DistantLight",
    "sphere": "SphereLight", "cylinder": "CylinderLight",
}
_COLOR_WORDS: dict[str, tuple[float, float, float]] = {
    "red": (1.0, 0.0, 0.0), "green": (0.0, 1.0, 0.0), "blue": (0.0, 0.0, 1.0), "yellow": (1.0, 1.0, 0.0),
    "white": (1.0, 1.0, 1.0), "black": (0.0, 0.0, 0.0), "gray": (0.5, 0.5, 0.5), "grey": (0.5, 0.5, 0.5),
    "orange": (1.0, 0.5, 0.0), "purple": (0.5, 0.0, 0.5), "pink": (1.0, 0.4, 0.7), "cyan": (0.0, 1.0, 1.0),
    "magenta": (1.0, 0.0, 1.0), "brown": (0.45, 0.25, 0.1), "gold": (1.0, 0.77, 0.34), "silver": (0.75, 0.75, 0.75),
}
_COLOR_WORD = "(?:" + "|".join(sorted(_COLOR_WORDS, key=len, reverse=True)) + ")"
_VECTOR_TEXT = rf"\(?\s*{_NUMBER}\s*[,\s]\s*{_NUMBER}\s*[,\s]\s*{_NUMBER}\s*\)?"
_CREATE_VERB = r"^(?:add|create|spawn|place|put|make)\s+(?:a|an|another|new)\s+(?:new\s+)?"
_LIGHT_REQUEST = re.compile(
    _CREATE_VERB + rf"(?:(?P<color>{_COLOR_WORD})\s+)?(?:(?P<kind>{'|'.join(_LIGHT_KINDS)})\s+)?light\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_CAMERA_REQUEST = re.compile(_CREATE_VERB + r"camera\b(?P<rest>.*)$", re.IGNORECASE)
# Up to three words before "material": a color word is understood, anything else ("shiny") is refused, never
# turned into an Xform named ShinyMaterial.
_MATERIAL_REQUEST = re.compile(
    r"^(?:make|create|add|define)\s+(?:(?:a|an|new|another)\s+)*(?P<words>(?:[\w-]+\s+){0,3}?)material\b(?P<rest>.*)$",
    re.IGNORECASE,
)
# "paint /World/Box red", "make it blue", "colour the cube 1 0 0"; never "make a ..." (that creates something).
_PAINT_REQUEST = re.compile(
    r"^(?P<verb>paint|colou?r|make|turn)\s+(?!(?:a|an|another|new)\b)(?P<target>.+?)\s+(?:(?:in|with|using)\s+)?"
    rf"(?P<color>{_COLOR_WORD}|{_VECTOR_TEXT})$",
    re.IGNORECASE,
)
_DEFAULT_LOOKS = "/World/Looks"

# Friendly attribute names -> USD names; None means "depends on the prim type" (see _usd_attribute_name).
_ATTRIBUTE_ALIASES: dict[str, str | None] = {
    "intensity": "inputs:intensity",
    "brightness": "inputs:intensity",
    "exposure": "inputs:exposure",
    "color": None,
    "colour": None,
    "display color": "primvars:displayColor",
    "displaycolor": "primvars:displayColor",
    "focal length": "focalLength",
    "focallength": "focalLength",
    "horizontal aperture": "horizontalAperture",
    "size": "size",
    "radius": None,
    "visibility": "visibility",
    "purpose": "purpose",
}
_LIGHT_ONLY_ATTRIBUTES = {"intensity", "brightness", "exposure"}
_CAMERA_ONLY_ATTRIBUTES = {"focal length", "focallength", "horizontal aperture"}
_NUMERIC_ATTRIBUTES = {
    "inputs:intensity", "inputs:exposure", "inputs:radius", "focalLength", "horizontalAperture", "size", "radius",
}
_VISIBILITY_WORDS = {
    "invisible": "invisible", "hidden": "invisible", "hide": "invisible", "off": "invisible", "false": "invisible",
    "no": "invisible", "visible": "inherited", "inherited": "inherited", "show": "inherited", "shown": "inherited",
    "on": "inherited", "true": "inherited", "yes": "inherited",
}
_PURPOSES = ("default", "render", "proxy", "guide")

# Clause words in "add a rect light /World/Key at 0 5 0 aimed at the cube with intensity 5000".
_AIM_WORDS = {"aimed", "aiming", "aim", "pointed", "pointing", "point", "looking", "facing", "directed", "shining"}
_AIM_PREPOSITIONS = {"at", "to", "toward", "towards", "on"}
_CLAUSE_WORDS = _AIM_WORDS | {
    "with", "and", "at", "above", "over", "intensity", "exposure", "color", "colour", "size", "under", "in", "inside",
    "into", "named", "called", "as", "focal", "roughness", "metallic", "metalness", "diffuse",
}
_CLAUSE_NUMBERS = {
    "intensity": "intensity", "exposure": "exposure", "size": "size", "roughness": "roughness",
    "metallic": "metallic", "metalness": "metallic",
}
_CLAUSE_FILLER = {"with", "and", "please", "here", "now", "then"}
_EXTENDED_ACTIONS = {"set_attribute", "create_light", "create_camera", "create_material", "paint", "undo", "undo_history",
                     "rigid_body", "collider", "remove_physics", "physics_scene", "physics_report", "screenshot",
                     "simulation"}
# Asset categories the router passes over when it picks the asset to preview from a sentence (whole sample scenes,
# training copies and alternate-physics variants), unless the request names them.
_ASSET_VARIANT_CATEGORIES = {"samples", "isaaclab", "robots_multiphysics"}
_ASSET_TIE = 0.95  # a second hit scoring at least this share of the first makes the pick ambiguous
_ASSET_PIECE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")  # 'SM_RackShelf_01' -> Rack, Shelf, 01


def _asset_tokens(path: str) -> set[str]:
    """Lowercased words of a library path plus their camelCase pieces."""
    tokens: set[str] = set()
    for word in re.findall(r"[A-Za-z0-9]+", path):
        tokens.add(word.lower())
        tokens.update(piece.lower() for piece in _ASSET_PIECE.findall(word))
    return tokens


def _asset_word_covered(word: str, tokens: set[str]) -> bool:
    """word (or its singular) is a path word or that word plus s/es; five or more letters may sit inside one."""
    forms = {word, word[:-1]} if len(word) > 3 and word.endswith("s") else {word}
    if any(form in tokens or form + "s" in tokens or form + "es" in tokens for form in forms):
        return True
    return len(word) >= 5 and any(form in token for form in forms for token in tokens)

_QUIZZES: dict[str, dict[str, list[str]]] = {
    "schemas": {
        "questions": [
            "What is the difference between a typed schema and an applied schema?",
            "Does renaming a prim change its schema type?",
            "Which stage tree column helps you spot the prim's main type?",
        ],
        "answers": [
            "Typed schema is the prim's main type; applied schema adds extra capability.",
            "No, rename changes the path or name, not the prim's schema type.",
            "The Type column.",
        ],
    },
    "prims": {
        "questions": [
            "What does a prim path represent?",
            "What is the relationship between a parent prim and a child prim?",
            "Name one thing a prim can represent in a USD scene.",
        ],
        "answers": [
            "It is the prim's address in the scene hierarchy.",
            "The parent contains or organizes the child in the scene graph.",
            "A transform, mesh, camera, light, or many other scene concepts.",
        ],
    },
}


class RouteTarget(str, Enum):
    SCENE = "scene"
    LEARN = "learn"
    USD = "usd"
    KIT = "kit"
    OMNI_UI = "omni_ui"
    ISAAC_SIM = "isaac_sim"
    BOWERBOT = "bowerbot"
    UNKNOWN = "unknown"


# Explicit domain names that let a question outrank a lesson topic. Generic words ("layout", "extension",
# "robot") do not count, "USD Composer" is the app rather than the USD API, and Bowerbot is left out
# because its selector can create or package projects.
_NAMED_DOMAINS = (
    (RouteTarget.ISAAC_SIM, re.compile(r"\bisaac|\bphysx\b|\bros ?2\b")),
    (RouteTarget.OMNI_UI, re.compile(r"\bomni\.ui\b")),
    (RouteTarget.KIT, re.compile(r"(?<!\w)(?<!\w\.)kit\b|\bomni\.kit\b")),
    (RouteTarget.USD, re.compile(r"(?<!\w)(?<!\w\.)usd[acz]?\b|\bopenusd\b|\bomni\.usd\b|\bpxr\b|\w\.usd[acz]?\b")),
    (RouteTarget.USD, _USD_EXAM_TERMS),
)


def _detect_named_knowledge_domain(request: str) -> RouteTarget:
    text = request.replace("usd composer", "composer")
    return next((target for target, pattern in _NAMED_DOMAINS if pattern.search(text)), RouteTarget.UNKNOWN)


def _is_prim_count_question(text: str) -> bool:
    match = _PRIM_COUNT.match(text)
    return bool(match) and not _NOT_A_TOTAL.search(match.group(1))


def _name_key(name: str) -> str:
    return re.sub(r"[\s_\-]", "", name.lower())


def _format_number(value: float) -> str:
    text = f"{float(value) + 0.0:.6f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def _local_usd_file(stage_name: str) -> str | None:
    """Local file behind a stage identifier such as 'C:/x/scene.usda' or 'file:/C:/x/scene.usda'."""
    name = stage_name.strip()
    if name.lower().startswith("file:"):
        name = re.sub(r"^/+(?=[A-Za-z]:[\\/])", "", unquote(name[5:]))
        if name.startswith("//"):
            name = "/" + name.lstrip("/")
    if not name.lower().endswith(_USD_SUFFIXES) or not os.path.isabs(name) or not os.path.isfile(name):
        return None
    return name


def _saved_up_axis(stage_name: str) -> dict[str, str] | None:
    """upAxis of the saved root layer, read from its metadata only; nothing is composed or saved.

    Mirrors UsdGeom.GetStageUpAxis for a file: the root layer's authored value, else the UsdGeom fallback.
    None when the stage is not a readable local USD file or pxr is unavailable.
    """
    path = _local_usd_file(stage_name)
    if path is None:
        return None
    try:
        from pxr import Sdf, UsdGeom
    except ImportError:
        return None
    try:
        layer = Sdf.Layer.OpenAsAnonymous(path, True)
        if layer is None:
            return None
        root = layer.pseudoRoot
        authored = bool(root.HasInfo(UsdGeom.Tokens.upAxis))
        axis = str(root.GetInfo(UsdGeom.Tokens.upAxis) if authored else UsdGeom.GetFallbackUpAxis())
    except Exception:  # noqa: BLE001 - an unreadable layer just means the up axis is unknown
        return None
    if axis not in {"Y", "Z"}:
        return None
    return {"axis": axis, "source": "authored" if authored else "fallback", "layer": path}


def _parse_color(text: str) -> tuple[float, float, float] | None:
    """(r, g, b) from a color word ('red') or three numbers ('1 0 0', '(1, 0, 0)'); None otherwise."""
    cleaned = text.strip()
    if cleaned.lower() in _COLOR_WORDS:
        return _COLOR_WORDS[cleaned.lower()]
    if re.fullmatch(_VECTOR_TEXT, cleaned):
        red, green, blue = (float(value) for value in re.findall(_NUMBER, cleaned))
        return (red, green, blue)
    return None


def _attribute_value(name: str, text: str) -> Any:
    """The JSON value to author for attribute `name` from the words after 'to'; ValueError when it cannot be one."""
    raw = text.strip().strip("'\"").strip()
    if name in {"inputs:color", "primvars:displayColor"}:
        color = _parse_color(raw)
        if color is None:
            raise ValueError(f"{name} needs a color word such as red or blue, or three numbers from 0 to 1 such as '1 0 0'.")
        # displayColor is a color3f[] primvar (one color per prim when constant); a light's inputs:color is one color3f.
        return [list(color)] if name == "primvars:displayColor" else list(color)
    if name == "visibility":
        if raw.lower() not in _VISIBILITY_WORDS:
            raise ValueError("visibility is 'invisible' (hidden) or 'inherited' (visible).")
        return _VISIBILITY_WORDS[raw.lower()]
    if name == "purpose":
        if raw.lower() not in _PURPOSES:
            raise ValueError(f"purpose is one of {', '.join(_PURPOSES)}.")
        return raw.lower()
    if re.fullmatch(rf"\(?\s*{_NUMBER}(?:\s*[,\s]\s*{_NUMBER})*\s*\)?", raw):
        values = [float(value) for value in re.findall(_NUMBER, raw)]
        return values[0] if len(values) == 1 else values
    if name in _NUMERIC_ATTRIBUTES:
        raise ValueError(f"{name} needs a number, for example '5000'.")
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    return raw


def _world_bound(result: Any) -> dict[str, Any] | None:
    """The non-empty world_bound record of a live 'bounds' insight result, or None."""
    if not isinstance(result, dict) or result.get("ok") is False:
        return None
    record = result.get("world_bound")
    if record is None and isinstance(result.get("result"), dict):
        record = result["result"].get("world_bound")
    if not isinstance(record, dict) or record.get("empty"):
        return None
    try:
        if any(len([float(value) for value in record[key]]) != 3 for key in ("center", "size", "max")):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return record


def _refusal(message: str, example: str) -> dict[str, Any]:
    """An 'unsupported' parse result: nothing changes and the hint shows a phrasing that works."""
    return {
        "action": "unsupported",
        "path": None,
        "message": f"{message} Nothing was changed.",
        "hint": f"For example '{example}'.",
    }


def _service_lacks(method: str, action: str, goal: str, **extra: Any) -> dict[str, Any]:
    """Result for a scene service that predates `method` (an older router build); nothing is changed."""
    return {
        "ok": False,
        **extra,
        "target": "scene",
        "action": action,
        "applied": False,
        "needs_service_update": True,
        "message": f"This router's scene service has no {method}() yet, so I cannot {goal}. Nothing was changed.",
    }


@dataclass(slots=True)
class RoutingDecision:
    target: RouteTarget
    confidence: float
    reason: str
    normalized_request: str


@dataclass(slots=True)
class RoutedResult:
    decision: RoutingDecision
    payload: dict[str, Any]


class KnowledgeBackend(Protocol):
    async def ask(self, query: str) -> dict[str, Any]: ...


class NullKnowledgeBackend:
    """Fallback backend used when a knowledge MCP is not connected yet."""

    def __init__(self, name: str, endpoint: str | None = None) -> None:
        self.name = name
        self.endpoint = endpoint

    async def ask(self, query: str) -> dict[str, Any]:
        return {
            "ok": False,
            "backend": self.name,
            "endpoint": self.endpoint,
            "query": query,
            "message": "Knowledge backend is not connected yet.",
        }


class OmniverseRouter:
    """Single front door for scene actions and knowledge handoff."""

    def __init__(
        self,
        scene_service: SceneMCPService,
        usd_backend: KnowledgeBackend | None = None,
        kit_backend: KnowledgeBackend | None = None,
        omni_ui_backend: KnowledgeBackend | None = None,
        isaac_sim_backend: KnowledgeBackend | None = None,
        bowerbot_backend: KnowledgeBackend | None = None,
        exam_day: Callable[[int | None], dict[str, Any]] | None = None,
        mock_status: Callable[[], dict[str, Any] | None] | None = None,
        dashboard: Callable[[bool], dict[str, Any]] | None = None,
        learning_helpers: dict[str, Callable[..., dict[str, Any]]] | None = None,
    ) -> None:
        self._scene = scene_service
        # Learning-helper callbacks by kind (server.build_server attaches its python_*, explain_error, assignment_*,
        # scene_explain and class_* tools); without them the pure helpers still answer and the rest name their tool.
        self._learning_helpers: dict[str, Callable[..., dict[str, Any]]] = dict(learning_helpers or {})
        # dashboard(write) builds the exam dashboard (and saves dashboard.html when write is True).
        self._dashboard = dashboard
        self._last_capture: str | None = None  # the newest screenshot path, for 'is my screenshot ready'
        # Optional exam hooks (runtime.build_router wires them to the server's stores): exam_day(day) builds that
        # day's lesson; mock_status() is the open mock's status or None.
        self._exam_day = exam_day
        self._mock_status = mock_status
        self._practice_session: dict[str, Any] | None = None
        self._learning_history: list[dict[str, Any]] = []
        self._learning_progress: dict[str, Any] = {
            "topics": {},
            "completed_sessions": 0,
            "completed_steps": 0,
        }
        self._backends = {
            RouteTarget.USD: usd_backend or NullKnowledgeBackend("usd"),
            RouteTarget.KIT: kit_backend or NullKnowledgeBackend("kit"),
            RouteTarget.OMNI_UI: omni_ui_backend or NullKnowledgeBackend("omni_ui"),
            RouteTarget.ISAAC_SIM: isaac_sim_backend or NullKnowledgeBackend("isaac_sim"),
            RouteTarget.BOWERBOT: bowerbot_backend or NullKnowledgeBackend("bowerbot"),
        }

    def attach_learning_helpers(self, helpers: dict[str, Callable[..., dict[str, Any]]]) -> None:
        """Use these callbacks (kind -> callable) for the learning-helper sentences in nl_tier5.py."""
        self._learning_helpers.update(helpers)

    async def handle(self, request: str, dry_run: bool = False) -> RoutedResult:
        decision = self.route(request)
        payload = await self._dispatch(decision, request, dry_run=dry_run)
        return RoutedResult(decision=decision, payload=payload)

    async def ask_backend(self, target: RouteTarget, query: str) -> dict[str, Any]:
        backend = self._backends.get(target)
        if backend is None:
            return {
                "ok": False,
                "backend": target.value,
                "query": query,
                "message": "Unknown backend target.",
            }
        return await backend.ask(query)

    def route(self, request: str) -> RoutingDecision:
        normalized = self._normalize(request)
        if self._practice_session is not None and normalized in {"done", "next", "continue", "ok", "finished"}:
            return RoutingDecision(RouteTarget.LEARN, 0.99, "Active practice session advance request", normalized)
        core = self._strip_politeness(normalized)
        helper = nl_tier5.parse(request)
        if helper is not None:  # Python snippets, exercises, error explanations, homework, scene tour, class notes
            return RoutingDecision(RouteTarget.LEARN, 0.96, f"Learning helper request ({helper['kind']}, read-only)",
                                   normalized)
        if self._help_request(core) is not None:
            return RoutingDecision(RouteTarget.LEARN, 0.97, "Help request (tool guide)", normalized)
        if nl_tier4.is_dashboard_request(core):
            return RoutingDecision(RouteTarget.LEARN, 0.96, "Exam dashboard request", normalized)
        if nl_tier4.is_tier4_scene_request(core):
            return RoutingDecision(
                RouteTarget.SCENE, 0.96, "Asset search, physics, screenshot or simulation request", normalized
            )
        exam = self._exam_request(normalized)
        if exam is not None:
            if exam[0] == "mock":
                return RoutingDecision(RouteTarget.LEARN, 0.95, "Mock exam request (explained, never auto-started)", normalized)
            return RoutingDecision(RouteTarget.LEARN, 0.96, "Daily exam lesson request", normalized)
        if _is_prim_count_question(core):
            return RoutingDecision(RouteTarget.SCENE, 0.95, "Prim count question about the open stage", normalized)
        if self._is_extended_scene_request(core):
            return RoutingDecision(RouteTarget.SCENE, 0.96, "Undo, light, camera, material or paint request", normalized)
        # Checked before learning topics, which also match words such as "move", "select" and "prim".
        if self._looks_like_targeted_scene_command(normalized) or any(pattern.match(core) for pattern in _SET_ATTRIBUTE):
            return RoutingDecision(RouteTarget.SCENE, 0.97, "Scene command verb with a target detected", normalized)
        if (
            core != normalized
            and _SCENE_VERB.match(core)
            and not any(phrase in core for phrase in _LEARNING_PHRASES)
            and not re.search(r"\b(?:docs?|documentation|examples?|api)\b", core)
        ):
            return RoutingDecision(RouteTarget.SCENE, 0.9, "Polite scene request detected", normalized)
        learning_topic = self._detect_learning_topic(normalized)
        explicit_learning = self._detect_learning_style(normalized) != "lesson" or any(
            phrase in normalized for phrase in _LEARNING_PHRASES
        )
        # Questions that name a knowledge domain go to its MCP even when they mention a lesson topic such as
        # "prim"; _dispatch attaches the built-in lesson if that backend cannot answer.
        if not explicit_learning and self._is_question(normalized) and not _LIVE_SCENE.search(normalized):
            target = _detect_named_knowledge_domain(normalized)
            if target is not RouteTarget.UNKNOWN:
                return RoutingDecision(target, 0.92, f"Question names the {target.value} domain", normalized)

        if learning_topic is not None or explicit_learning:
            return RoutingDecision(RouteTarget.LEARN, 0.91, "Learning keywords detected", normalized)

        if self._is_question(normalized):
            target = self._detect_knowledge_target(normalized)
            if target is not RouteTarget.UNKNOWN:
                return RoutingDecision(target, 0.92, f"Matched {target.value} knowledge keywords", normalized)

        if self._looks_like_explicit_scene_command(normalized):
            return RoutingDecision(RouteTarget.SCENE, 0.97, "Scene keywords or command verb detected", normalized)

        target = self._detect_knowledge_target(normalized)
        if target is not RouteTarget.UNKNOWN:
            return RoutingDecision(target, 0.9, f"Matched {target.value} keywords", normalized)

        if self._looks_like_scene_request(normalized):
            return RoutingDecision(RouteTarget.SCENE, 0.74, "Scene-oriented nouns or verbs detected", normalized)

        return RoutingDecision(RouteTarget.UNKNOWN, 0.35, "No confident keyword match", normalized)

    def scene_summary(self) -> dict[str, Any]:
        return self._scene.scene_summary()

    def backend_status(self) -> dict[str, Any]:
        backends: list[dict[str, Any]] = []
        for target, backend in self._backends.items():
            backends.append(
                {
                    "target": target.value,
                    "backend": getattr(backend, "name", target.value),
                    "endpoint": getattr(backend, "endpoint", None),
                    "enabled": bool(getattr(backend, "enabled", True)),
                }
            )
        return {"backends": backends}

    async def _dispatch(self, decision: RoutingDecision, request: str, dry_run: bool) -> dict[str, Any]:
        if decision.target is RouteTarget.SCENE:
            return self._dispatch_scene(request, dry_run=dry_run)

        if decision.target is RouteTarget.LEARN:
            normalized = self._normalize(request)
            if self._practice_session is not None and normalized in {"done", "next", "continue", "ok", "finished"}:
                return self.next_practice_step()
            helper = nl_tier5.parse(request)
            if helper is not None:
                return await self._run_learning_helper(helper)
            help_request = self._help_request(self._strip_politeness(normalized))
            if help_request is not None:
                return self.tool_help(help_request["topic"])
            if nl_tier4.is_dashboard_request(self._strip_politeness(normalized)):
                return await self._run_dashboard(write=not dry_run)
            exam = self._exam_request(normalized)
            if exam is not None:
                return await self._dispatch_exam(*exam)
            return self._dispatch_learning(request)

        if decision.target is RouteTarget.BOWERBOT and dry_run:
            # Bowerbot's project tools write to disk, so a dry run previews them instead of calling the backend.
            selector = getattr(self._backends.get(RouteTarget.BOWERBOT), "selector", None)
            selection = selector(request) if selector else None
            if selection is not None and selection.tool_name in {"create_project", "open_project", "package_scene"}:
                return {
                    "ok": True,
                    "dry_run": True,
                    "target": "bowerbot",
                    "would_call": selection.tool_name,
                    "arguments": selection.arguments,
                    "message": "Preview only: Bowerbot was not called.",
                }

        if decision.target in self._backends:
            payload = await self.ask_backend(decision.target, request)
            if not payload.get("ok"):
                fallback = self._fallback_lesson(request)
                if fallback is not None:
                    payload = {**payload, "fallback": fallback}
            return payload

        return {
            "ok": False,
            "message": "I could not confidently route this request.",
            "suggested_targets": ["scene", "usd", "kit", "omni_ui", "isaac_sim", "bowerbot"],
        }

    def _dispatch_scene(self, request: str, dry_run: bool) -> dict[str, Any]:
        asset = self._try_asset_request(request)
        if asset is not None:
            return asset
        parsed = self._parse_scene_command(request)
        candidates: list[str] = []
        if parsed is None:
            parsed, candidates = self._resolve_by_name(request)
        if parsed is None:
            failure = {
                "ok": False,
                "message": "Scene routing selected, but I could not parse the command.",
                "hint": (
                    "Try phrases like 'inspect /World', 'create /World/NewPrim', 'select /World/Cube', "
                    "'move /World/Cube by 0 0 1', 'rotate /World/Cube 0 90 0', 'rename /World/Cube to HeroCube', "
                    "or 'find cube'. Say 'help' for everything I understand."
                ),
            }
            if candidates:
                failure["candidates"] = candidates
                failure["hint"] = (
                    "Name the prim by path (or select it and say 'this') and give exact values, "
                    f"for example 'move {candidates[0]} by 0 0 1'. Nothing was changed."
                )
            return failure

        named = parsed.pop("resolved_from_name", None)
        action = parsed["action"]
        if action == "unsupported":
            unsupported = {"ok": False, "target": "scene", "message": parsed["message"], "hint": parsed["hint"]}
            if parsed.get("candidates"):
                unsupported["candidates"] = parsed["candidates"]
            return unsupported
        if action in _EXTENDED_ACTIONS:
            return self._dispatch_extended(parsed, dry_run=dry_run, named=named)
        if action == "move":
            failure = self._make_transform_absolute(parsed)
            if failure is not None:
                return failure
        target_path = parsed.get("path") or parsed.get("source_path")
        if named is not None and action in _EDIT_ACTIONS:
            # A name is not an exact target: show what would change and the explicit command, never apply it.
            command = self._explicit_command(parsed)
            result = self._run_scene_action(parsed, dry_run=True)
            result["command"] = command
            result["message"] = (
                f"'{named}' matched only {target_path}, but a name is not an exact target, so this is a preview "
                f"and nothing was changed. Run '{command}' to apply it."
            )
        else:
            result = self._run_scene_action(parsed, dry_run=dry_run)
        if named is not None:
            result["resolved_from_name"] = {"name": named, "path": target_path}
        if parsed.get("relative"):
            result["relative"] = parsed["relative"]
        return result

    def _run_scene_action(self, parsed: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        action = parsed["action"]
        if action == "summary":
            summary = self._scene.scene_summary()
            payload = {"ok": True, "target": "scene", "result": summary}
            if parsed.get("question") == "prim_count":
                payload["message"] = f"The open stage has {summary.get('prim_count')} prims."
            return payload
        if action == "ping":
            return {"ok": True, "target": "scene", "result": self._scene.ping()}
        if action == "select":
            if dry_run:
                # Selection steers later "this/it" edits, so a preview must not change it.
                return {"ok": True, "dry_run": True, "target": "scene", "would_select": list(parsed["paths"])}
            return {"ok": True, "target": "scene", "result": self._scene.set_selection(parsed["paths"])}
        if action == "inspect":
            return {"ok": True, "target": "scene", "result": self._scene.inspect_prim(parsed["path"])}
        if action == "children":
            return {"ok": True, "target": "scene", "result": self._scene.list_children(parsed["path"])}
        if action == "create":
            result = self._scene.create_prim(parsed["path"], parsed["type_name"], dry_run=dry_run)
            payload = self._wrap_operation_result("scene", result)
            if parsed.get("translation") is not None:
                self._place_created_prim(payload, parsed["path"], parsed["translation"], dry_run)
            return payload
        if action == "delete":
            result = self._scene.delete_prim(parsed["path"], dry_run=dry_run)
            return self._wrap_operation_result("scene", result)
        if action == "duplicate":
            result = self._scene.duplicate_prim(parsed["source_path"], parsed["target_path"], dry_run=dry_run)
            return self._wrap_operation_result("scene", result)
        if action == "reparent":
            result = self._scene.reparent_prim(parsed["path"], parsed["new_parent"], dry_run=dry_run)
            return self._wrap_operation_result("scene", result)
        if action == "move":
            result = self._scene.move_prim(
                parsed["path"],
                translation=parsed.get("translation"),
                rotation=parsed.get("rotation"),
                scale=parsed.get("scale"),
                dry_run=dry_run,
            )
            return self._wrap_operation_result("scene", result)
        if action == "material":
            binder = getattr(self._scene, "bind_material", None)
            if callable(binder):
                payload = self._wrap_result(binder(parsed["path"], parsed["material_path"], dry_run=dry_run))
                inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
                if not payload.get("ok"):  # a refusal (missing prim or material) says why
                    detail = inner.get("error") or inner.get("message") or "The scene bridge could not bind it."
                    payload["message"] = f"{detail} Nothing was changed."
                return payload
            result = self._scene.set_material(parsed["path"], parsed["material_path"], dry_run=dry_run)
            return self._wrap_operation_result("scene", result)
        if action == "rename":
            result = self._scene.rename_prim(parsed["path"], parsed["new_name"], dry_run=dry_run)
            return self._wrap_operation_result("scene", result)
        if action == "find":
            return {
                "ok": True,
                "target": "scene",
                "result": self._scene.find_prims(parsed["query"], scope_path=parsed.get("scope_path")),
            }
        if action == "save":
            if dry_run:
                return {
                    "ok": True,
                    "dry_run": True,
                    "target": "scene",
                    "result": {"path": parsed["path"]},
                    "message": f"Would save the current scene to {parsed['path']}",
                }
            return {"ok": True, "target": "scene", "result": self._scene.save_scene(parsed["path"])}

        return {"ok": False, "message": f"Unsupported scene action: {action}"}

    def _make_transform_absolute(self, parsed: dict[str, Any]) -> dict[str, Any] | None:
        """Turn 'by', 'up' and 'N times' requests into absolute values read from the prim (read-only).

        Returns a failure payload, and changes nothing, when 'up' cannot be resolved to a stage axis.
        """
        if not {"offset", "up_offset", "scale_factor"} & parsed.keys():
            return None
        path = parsed["path"]
        relative: dict[str, Any] = {}
        if "up_offset" in parsed:
            amount = parsed.pop("up_offset")
            up = self._stage_up_axis()
            if up is None:
                value = _format_number(amount)
                return {
                    "ok": False,
                    "target": "scene",
                    "path": path,
                    "message": (
                        "I could not read the stage up axis (upAxis) from a saved USD file, so I will not guess "
                        "which way is up. Nothing was changed."
                    ),
                    "hint": (
                        f"Give an explicit offset: 'move {path} by 0 0 {value}' on a Z-up stage or "
                        f"'move {path} by 0 {value} 0' on a Y-up stage."
                    ),
                }
            offset = [0.0, 0.0, 0.0]
            offset[1 if up["axis"] == "Y" else 2] = amount
            parsed["offset"] = tuple(offset)
            relative["up_axis"] = up
        current = self._scene.inspect_prim(path)
        if "offset" in parsed:
            offset = parsed.pop("offset")
            relative.update(from_translation=list(current["translation"]), offset=list(offset))
            parsed["translation"] = tuple(
                round(float(value) + delta, 9) + 0.0 for value, delta in zip(current["translation"], offset)
            )
        if "scale_factor" in parsed:
            factor = parsed.pop("scale_factor")
            relative.update(from_scale=list(current["scale"]), factor=list(factor))
            parsed["scale"] = tuple(round(float(value) * ratio, 9) + 0.0 for value, ratio in zip(current["scale"], factor))
        parsed["relative"] = relative
        return None

    def _stage_up_axis(self) -> dict[str, str] | None:
        try:
            stage_name = str(self._scene.scene_summary().get("stage_name") or "")
        except Exception:  # noqa: BLE001 - an unreachable scene means the up axis is unknown
            return None
        return _saved_up_axis(stage_name)

    @staticmethod
    def _explicit_command(parsed: dict[str, Any]) -> str:
        """The exact path-based command that performs a parsed edit; it parses back to the same action."""
        action, path = parsed["action"], parsed.get("path")
        if action == "move":
            parts = [
                f"{verb} {path} " + " ".join(_format_number(value) for value in parsed[key])
                for key, verb in (("translation", "move"), ("rotation", "rotate"), ("scale", "scale"))
                if parsed.get(key) is not None
            ]
            return "; ".join(parts)
        if action == "rename":
            return f"rename {path} to {parsed['new_name']}"
        if action == "delete":
            return f"delete {path}"
        if action == "duplicate":
            return f"duplicate {parsed['source_path']} to {parsed['target_path']}"
        if action == "reparent":
            return f"reparent {path} to {parsed['new_parent']}"
        if action == "material":
            return f"bind {parsed['material_path']} to {path}"
        return ""

    def _attribute_not_supported(self, parsed: dict[str, Any]) -> dict[str, Any]:
        path = parsed.get("path")
        payload: dict[str, Any] = {
            "ok": False,
            "target": "scene",
            "action": "set_attribute",
            "path": path,
            "attribute": parsed["attribute"],
            "value": parsed["value"],
            "message": (
                f"Setting '{parsed['attribute']}' is attribute editing, which arrives in a later tier. "
                "Nothing was changed."
            ),
        }
        if path is None:
            payload["candidates"] = parsed.get("candidates", [])
            payload["hint"] = "Name the prim by path to see its current values, for example 'inspect /World/Props/Cube'."
            return payload
        try:
            payload["prim"] = self._scene.inspect_prim(path)
        except ValueError as exc:
            payload["error"] = str(exc)
        return payload

    # --- help ---------------------------------------------------------------------------------------------------

    def tool_help(self, topic: str | None = None) -> dict[str, Any]:
        """The tool guide grouped by task (help.help); reads no scene, so it works with every bridge down."""
        return {"target": "learn", "style": "help", **tool_guide.help(topic)}

    @staticmethod
    def _help_request(core: str) -> dict[str, str | None] | None:
        """{"topic": ...} for 'help', 'what can you do', 'list tools' or 'help with lights'; None otherwise.

        'help me add a light' is a request rather than a topic, so a bare topic must name a group or tool exactly.
        """
        match = _HELP_REQUEST.match(core.strip().rstrip("?.! "))
        if not match:
            return None
        topic, bare = match.group("topic"), match.group("bare")
        if topic is not None:
            return {"topic": topic.strip()} if tool_guide.resolve_topic(topic, search=True) else None
        if bare is not None:
            return {"topic": bare.strip()} if tool_guide.resolve_topic(bare) else None
        return {"topic": None}

    @staticmethod
    def _is_extended_scene_request(core: str) -> bool:
        """Undo, undo history, 'add a rect light ...', 'add a camera ...', 'make a red material' or 'paint X red'."""
        text = core.strip().rstrip("?.! ")
        if _UNDO_REQUEST.match(text) or _UNDO_CONFIRM.match(text) or _UNDO_HISTORY.match(text):
            return True
        if any(phrase in text for phrase in _LEARNING_PHRASES) or "quiz" in text:
            return False
        if _LIGHT_REQUEST.match(text) or _CAMERA_REQUEST.match(text) or _MATERIAL_REQUEST.match(text):
            return True
        paint = _PAINT_REQUEST.match(text)
        return bool(paint) and (paint.group("verb") not in {"make", "turn"} or paint.group("color") in _COLOR_WORDS)

    # --- natural-language parsing: lights, cameras, materials, paint -------------------------------------------

    def _parse_extended_create(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        """Lights, cameras, materials and paint; None for anything else (including a plain 'add a light')."""
        for parser in (self._parse_light, self._parse_camera, self._parse_material, self._parse_paint):
            parsed = parser(text, selected_path)
            if parsed is not None:
                return parsed
        return None

    def _parse_light(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        match = _LIGHT_REQUEST.match(text)
        if not match:
            return None
        clauses = self._parse_clauses(
            match.group("rest"), {"aim", "above", "translation", "intensity", "exposure", "size", "color"}
        )
        kind_word, color_word = match.group("kind"), match.group("color")
        specific = clauses.keys() & {
            "aim", "above", "translation", "intensity", "exposure", "size", "color", "path", "at_path", "at_target",
            "name",
        }
        if kind_word is None and color_word is None and not specific and not clauses.get("unknown"):
            # "add a light" and "add a light under /World/Lights" keep the plain DistantLight create; leftover words
            # ("make a light brighter") are refused by _authoring_request instead of naming a new light.
            return None
        kind = _LIGHT_KINDS[kind_word.lower()] if kind_word else "distant"
        if color_word and "color" not in clauses:
            clauses["color"] = _COLOR_WORDS[color_word.lower()]
        return self._authoring_request(
            "create_light",
            clauses,
            selected_path,
            default_name=_LIGHT_TYPE_NAMES[kind],
            extra={"light_type": kind},
            example=f"add a {kind} light above /World/Props/Cube with intensity 5000",
        )

    def _parse_camera(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        match = _CAMERA_REQUEST.match(text)
        if not match:
            return None
        clauses = self._parse_clauses(match.group("rest"), {"aim", "above", "translation", "focal_length"})
        specific = {"aim", "above", "translation", "focal_length", "path", "at_path", "at_target", "name"}
        if not clauses.keys() & specific and not clauses.get("unknown"):
            return None  # "add a camera" and "add a camera under /World/Cams" keep the plain Camera create
        return self._authoring_request(
            "create_camera",
            clauses,
            selected_path,
            default_name="Camera",
            extra={},
            example="add a camera at 0 2 10 looking at /World/Props/Cube",
        )

    def _parse_material(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        match = _MATERIAL_REQUEST.match(text)
        if not match:
            return None
        clauses = self._parse_clauses(match.group("rest"), {"roughness", "metallic", "color"})
        example = "create material /World/Looks/Steel color 0.8 0.8 0.8 roughness 0.2 metallic 1"
        words = match.group("words").split()
        color_words = [word for word in words if word.lower() in _COLOR_WORDS]
        leftovers = [word for word in words if word.lower() not in _COLOR_WORDS] + list(clauses.get("unknown") or [])
        if len(color_words) > 1:
            leftovers.append(" ".join(color_words))
        if clauses.keys() & {"aim", "above", "translation", "at_target"}:
            leftovers.append("a position or target (materials have neither)")
        if leftovers:
            return _refusal(f"I understood a material request but not '{' '.join(leftovers)}'.", example)
        color_word = color_words[0] if color_words else None
        color = clauses.get("color") or (_COLOR_WORDS[color_word.lower()] if color_word else None)
        for key in ("roughness", "metallic"):
            if clauses.get(key) is not None and not 0.0 <= clauses[key] <= 1.0:
                return _refusal(f"{key} must be between 0 and 1, not {_format_number(clauses[key])}.", example)
        explicit = clauses.get("path") or clauses.get("at_path")
        if explicit:
            if not re.fullmatch(r"(?:/[A-Za-z_]\w*)+", explicit):
                return _refusal(f"'{explicit}' is not a valid prim path.", example)
            if self._safe_inspect(explicit) is not None:
                return _refusal(f"{explicit} already exists; bind it with 'bind {explicit} to <prim path>'.", example)
            path = explicit
        else:
            name = clauses.get("name") or (f"{color_word.capitalize()}Material" if color_word else "Material")
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                return _refusal(f"'{name}' is not a valid prim name.", example)
            path = self._free_child_path(clauses.get("parent") or _DEFAULT_LOOKS, name)
        return {
            "action": "create_material",
            "path": path,
            "explicit": bool(explicit),
            "diffuse_color": color,
            "roughness": clauses.get("roughness"),
            "metallic": clauses.get("metallic"),
        }

    def _parse_paint(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        match = _PAINT_REQUEST.match(text)
        if not match:
            return None
        verb, color_text = match.group("verb").lower(), match.group("color")
        if verb in {"make", "turn"} and color_text.lower() not in _COLOR_WORDS:
            return None
        color = _parse_color(color_text)
        if color is None:
            return None
        resolved = self._resolve_target_text(match.group("target"), selected_path)
        if resolved.get("error"):
            refusal = _refusal(resolved["error"], f"paint /World/Props/Cube {color_text}")
            if resolved.get("candidates"):
                refusal["candidates"] = resolved["candidates"]
            return refusal
        parsed: dict[str, Any] = {"action": "paint", "path": resolved["value"], "color": color, "color_text": color_text}
        if resolved.get("named"):
            parsed["resolved_from_name"] = resolved["named"]
        return parsed

    def _authoring_request(
        self,
        action: str,
        clauses: dict[str, Any],
        selected_path: str | None,
        default_name: str,
        extra: dict[str, Any],
        example: str,
    ) -> dict[str, Any]:
        """A create_light/create_camera action from parsed clauses; 'explicit' is True only for a user-given path."""
        noun = "light" if action == "create_light" else "camera"
        if clauses.get("unknown"):
            return _refusal(f"I understood a {noun} request but not '{' '.join(clauses['unknown'])}'.", example)
        if "at_target" in clauses:
            return _refusal(
                f"'at {clauses['at_target']}' is not a position: say 'at x y z', 'above <prim>' to place it over a "
                "prim, or 'aimed at <prim>'.",
                example,
            )
        parsed: dict[str, Any] = {"action": action, **extra}
        named: list[str] = []
        for key in ("above", "aim"):
            if key not in clauses:
                continue
            if key == "above" and isinstance(clauses[key], tuple):
                return _refusal("'above' needs a prim; for a point use 'at x y z'.", example)
            resolved = self._resolve_target_text(clauses[key], selected_path)
            if resolved.get("error"):
                refusal = _refusal(resolved["error"], example)
                if resolved.get("candidates"):
                    refusal["candidates"] = resolved["candidates"]
                return refusal
            parsed["above" if key == "above" else "aim_at"] = resolved["value"]
            if resolved.get("named"):
                named.append(resolved["named"])
        if "above" in parsed and "aim_at" not in parsed:
            parsed["aim_at"] = parsed["above"]
        if clauses.get("translation") is not None:
            parsed["translation"] = clauses["translation"]
        explicit = clauses.get("path") or clauses.get("at_path")
        if explicit:
            if not re.fullmatch(r"(?:/[A-Za-z_]\w*)+", explicit):
                return _refusal(f"'{explicit}' is not a valid prim path.", example)
            if self._safe_inspect(explicit) is not None:
                return _refusal(
                    f"{explicit} already exists, so it cannot be the new {noun}'s path. To light or frame it, say "
                    f"'above {explicit}' or 'aimed at {explicit}'; or pick a new path.",
                    example,
                )
            parsed["path"] = explicit
        else:
            name = clauses.get("name") or default_name
            if not re.fullmatch(r"[A-Za-z_]\w*", name):
                return _refusal(f"'{name}' is not a valid prim name.", example)
            parsed["path"] = self._free_child_path(clauses.get("parent") or "/World", name)
        parsed["explicit"] = bool(explicit)
        for key in ("intensity", "exposure", "size", "color", "focal_length"):
            if clauses.get(key) is not None:
                parsed[key] = clauses[key]
        if named:
            parsed["resolved_from_name"] = " and ".join(named)
        return parsed

    @staticmethod
    def _parse_clauses(rest: str, allowed: set[str]) -> dict[str, Any]:
        """Split ' /World/Key at 0 5 0 aimed at the cube with intensity 5000' into clauses; leftovers -> 'unknown'.

        Keys: path, at_path, name, parent, translation, aim, above, at_target, intensity, exposure, size, color,
        roughness, metallic, focal_length. Targets are a path, an (x, y, z) tuple or the words naming a prim.
        """
        tokens = re.findall(r"[^\s,()]+", rest)
        count = len(tokens)
        out: dict[str, Any] = {}
        unknown: list[str] = []

        def is_number(index: int) -> bool:
            return index < count and re.fullmatch(_NUMBER, tokens[index]) is not None

        def vector(index: int) -> tuple[float, float, float] | None:
            if all(is_number(index + offset) for offset in range(3)):
                return (float(tokens[index]), float(tokens[index + 1]), float(tokens[index + 2]))
            return None

        def target(index: int) -> tuple[Any, int]:
            if index >= count:
                return None, index
            if tokens[index].startswith("/"):
                return tokens[index], index + 1
            point = vector(index)
            if point is not None:
                return point, index + 3
            end = index
            while (
                end < count
                and tokens[end].lower() not in _CLAUSE_WORDS
                and not tokens[end].startswith("/")
                and not is_number(end)
            ):
                end += 1
            return (" ".join(tokens[index:end]) or None), end

        def skip(index: int, words: set[str]) -> int:
            return index + 1 if index < count and tokens[index].lower() in words else index

        index = 0
        while index < count:
            word = tokens[index].lower()
            following = [token.lower() for token in tokens[index + 1 : index + 3]]
            if word in _CLAUSE_FILLER:
                index += 1
                continue
            if word in {"to", "in", "into", "inside"} and following[:1] in (["scene"], ["stage"]):
                index += 2
                continue
            if (
                word in {"to", "in", "into", "inside"}
                and len(following) == 2
                and following[0] in {"the", "this", "my"}
                and following[1] in {"scene", "stage"}
            ):
                index += 3
                continue
            if word in {"under", "in", "inside", "into"} and following and following[0].startswith("/"):
                out["parent"] = tokens[index + 1]
                index += 2
                continue
            if word in _AIM_WORDS and "aim" in allowed:
                start = skip(index + 1, _AIM_PREPOSITIONS)
                value, end = target(start)
                if value is None:
                    unknown.append(tokens[index])
                    index = max(start, index + 1)
                    continue
                out["aim"] = value
                index = end
                continue
            if word in {"above", "over"} and "above" in allowed:
                value, end = target(index + 1)
                if value is None:
                    unknown.append(tokens[index])
                    index += 1
                    continue
                out["above"] = value
                index = end
                continue
            if word == "at":
                point = vector(index + 1)
                if point is not None and "translation" in allowed:
                    out["translation"] = point
                    index += 4
                    continue
                if following and following[0].startswith("/"):
                    out["at_path"] = tokens[index + 1]
                    index += 2
                    continue
                value, end = target(index + 1)
                out["at_target"] = " ".join(_format_number(v) for v in value) if isinstance(value, tuple) else (value or "")
                index = max(end, index + 1)
                continue
            if word in {"named", "called", "as"} and index + 1 < count:
                name = tokens[index + 1]
                out["path" if name.startswith("/") else "name"] = name
                index += 2
                continue
            if tokens[index].startswith("/") and "path" not in out:
                out["path"] = tokens[index]
                index += 1
                continue
            key = _CLAUSE_NUMBERS.get(word)
            if key is not None and key in allowed:
                start = skip(index + 1, {"of", "=", "to"})
                if is_number(start):
                    out[key] = float(tokens[start])
                    index = start + 1
                    continue
            if word == "focal" and following[:1] == ["length"] and "focal_length" in allowed:
                start = skip(index + 2, {"of", "=", "to"})
                if is_number(start):
                    out["focal_length"] = float(tokens[start])
                    index = start + 1
                    continue
            lens = re.fullmatch(rf"({_NUMBER})mm", word)
            if lens and "focal_length" in allowed:
                out["focal_length"] = float(lens.group(1))
                index = skip(index + 1, {"lens"})
                continue
            if word in {"color", "colour", "diffuse"} and "color" in allowed:
                start = skip(skip(index + 1, {"color", "colour"}), {"of", "=", "to"})
                point = vector(start)
                if point is not None:
                    out["color"] = point
                    index = start + 3
                    continue
                if start < count and tokens[start].lower() in _COLOR_WORDS:
                    out["color"] = _COLOR_WORDS[tokens[start].lower()]
                    index = start + 1
                    continue
            unknown.append(tokens[index])
            index += 1
        out["unknown"] = unknown
        return out

    def _resolve_target_text(self, value: Any, selected_path: str | None) -> dict[str, Any]:
        """A target phrase -> {"value": path or (x, y, z), "named": name if found by name} or {"error": ...}."""
        if isinstance(value, tuple):
            return {"value": value}
        text = str(value).strip()
        path = self._extract_usd_path(text) if "/" in text else None
        if path is not None:
            if self._safe_inspect(path) is None:
                return {"error": f"There is no prim at {path}."}
            return {"value": path}
        if _SELECTION_REF.fullmatch(text):
            if selected_path is None:
                return {"error": f"'{text}' means the selected prim, but exactly one prim must be selected."}
            return {"value": selected_path}
        words = [word for word in re.findall(r"[\w\-]+", text.lower()) if word not in _NAME_ARTICLES]
        if not words:
            return {"error": f"I could not tell which prim '{text}' is."}
        found, candidates = self._unique_prim_named(words)
        if found is None:
            return {"error": f"I could not find exactly one prim named '{' '.join(words)}'.", "candidates": candidates}
        return {"value": found, "named": " ".join(words)}

    # --- running the extended actions --------------------------------------------------------------------------

    def _dispatch_extended(self, parsed: dict[str, Any], dry_run: bool, named: str | None) -> dict[str, Any]:
        action = parsed["action"]
        if action == "undo":
            return self._run_undo(confirm=bool(parsed.get("confirm")), dry_run=dry_run)
        if action == "undo_history":
            return self._run_undo_history()
        if action == "set_attribute":
            return self._run_set_attribute(parsed, dry_run=dry_run, named=named)
        if action in {"create_light", "create_camera"}:
            return self._run_light_or_camera(parsed, dry_run=dry_run, named=named)
        if action == "create_material":
            return self._run_create_material(parsed)
        if action in {"rigid_body", "collider", "remove_physics", "physics_scene"}:
            return self._run_physics_edit(parsed, dry_run=dry_run, named=named)
        if action == "physics_report":
            return self._run_physics_report(parsed)
        if action == "screenshot":
            return self._run_screenshot(parsed, dry_run=dry_run)
        if action == "simulation":
            return self._run_simulation(parsed, dry_run=dry_run)
        return self._run_paint(parsed, named=named)

    def _run_undo(self, confirm: bool, dry_run: bool) -> dict[str, Any]:
        """'undo' previews the last change's undo; only 'confirm undo' without dry_run applies it."""
        undo = getattr(self._scene, "undo_last", None)
        if not callable(undo):
            return _service_lacks("undo_last", "undo", "undo the last change")
        preview = dry_run or not confirm
        try:
            payload = self._wrap_result(undo(dry_run=preview))
        except (ValueError, TypeError, LookupError) as exc:
            return {"ok": False, "target": "scene", "action": "undo", "applied": False,
                    "message": f"{exc} Nothing was undone."}
        payload["action"] = "undo"
        payload["applied"] = bool(payload.get("ok")) and not preview and payload.get("dry_run") is not True
        if preview and payload.get("ok"):
            payload["command"] = "confirm undo"
            payload.setdefault("message", "Preview only: nothing was undone.")
            payload["next_step"] = (
                "Send 'confirm undo' with dry_run=False to undo it, or call scene_undo_last(dry_run=False)."
                if not dry_run or confirm
                else "Send it again with dry_run=False to undo it."
            )
        return payload

    def _run_undo_history(self) -> dict[str, Any]:
        history = getattr(self._scene, "undo_history", None)
        if not callable(history):
            return _service_lacks("undo_history", "undo_history", "list the recent changes")
        payload = self._wrap_result(history())
        payload["action"] = "undo_history"
        return payload

    def _run_set_attribute(self, parsed: dict[str, Any], dry_run: bool, named: str | None) -> dict[str, Any]:
        setter = getattr(self._scene, "set_attribute", None)
        if not callable(setter):
            return self._attribute_not_supported(parsed)
        path, friendly, value_text = parsed.get("path"), parsed["attribute"], parsed["value"]
        base: dict[str, Any] = {
            "ok": False,
            "target": "scene",
            "action": "set_attribute",
            "path": path,
            "attribute": friendly,
            "value": value_text,
            "applied": False,
        }
        if path is None:
            return {
                **base,
                "candidates": parsed.get("candidates", []),
                "message": f"I could not tell which prim's '{friendly}' to set. Nothing was changed.",
                "hint": f"Name the prim by path, for example 'set {friendly} of /World/RectLight to {value_text}'.",
            }
        try:
            prim = self._scene.inspect_prim(path)
        except ValueError as exc:
            return {**base, "error": str(exc), "message": f"{exc}. Nothing was changed."}
        resolved = self._resolve_attribute(friendly, str(prim.get("type_name") or ""), value_text, path)
        if "error" in resolved:
            return {**base, "prim": prim, "message": f"{resolved['error']} Nothing was changed."}
        preview = dry_run or named is not None
        try:
            result = setter(path, resolved["name"], resolved["value"], time=None, dry_run=preview)
        except (ValueError, TypeError) as exc:
            return {**base, "attribute_name": resolved["name"], "prim": prim, "error": str(exc),
                    "message": f"{exc} Nothing was changed."}
        payload = self._wrap_result(result)
        payload.update(action="set_attribute", path=path, attribute=friendly, attribute_name=resolved["name"])
        payload["value"] = resolved["value"]
        payload["applied"] = bool(payload.get("ok")) and not preview and payload.get("dry_run") is not True
        if named is not None:
            command = f"set {friendly} of {path} to {value_text}"
            payload["command"] = command
            payload["resolved_from_name"] = {"name": named, "path": path}
            payload["message"] = (
                f"'{named}' matched only {path}, but a name is not an exact target, so this is a preview and nothing "
                f"was changed. Run '{command}' to apply it."
            )
        return payload

    @staticmethod
    def _resolve_attribute(friendly: str, type_name: str, value_text: str, path: str) -> dict[str, Any]:
        """{"name": USD attribute name, "value": JSON value} for a friendly name on this prim type, or {"error": ...}."""
        key = re.sub(r"\s+", " ", friendly.strip().lower())
        is_light = bool(re.search(r"Light(?:_\d+)?$", type_name))
        kind = type_name or "typeless prim"
        if key in _LIGHT_ONLY_ATTRIBUTES and not is_light:
            return {"error": f"'{friendly}' is a light setting ({_ATTRIBUTE_ALIASES[key]}), but {path} is a {kind}."}
        if key in _CAMERA_ONLY_ATTRIBUTES and type_name != "Camera":
            return {"error": f"'{friendly}' is a camera setting ({_ATTRIBUTE_ALIASES[key]}), but {path} is a {kind}."}
        if key in _ATTRIBUTE_ALIASES:
            name = _ATTRIBUTE_ALIASES[key]
            if name is None and key in {"color", "colour"}:
                name = "inputs:color" if is_light else "primvars:displayColor"
            elif name is None:
                name = "inputs:radius" if is_light else "radius"
        elif key in _TRANSFORM_ATTRIBUTE_WORDS:
            # Not attributes to write by that name (a custom 'position' would change nothing visible); the transform
            # and rename commands do it. 'orientation' is left alone: it is a real Gprim attribute.
            verb = _TRANSFORM_ATTRIBUTE_WORDS[key]
            command = {
                "move": f"move {path} to {value_text}",
                "rotate": f"rotate {path} {value_text}",
                "scale": f"scale {path} {value_text}",
                "rename": f"rename {path} to {value_text}",
            }[verb]
            return {"error": f"'{friendly}' is not an attribute I set directly; to change it, say '{command}'."}
        elif re.fullmatch(r"[A-Za-z_]\w*(?::[A-Za-z_]\w*)*", friendly.strip()):
            name = friendly.strip()
        else:
            return {"error": f"I do not know an attribute called '{friendly}'; use its USD name, such as inputs:intensity."}
        try:
            value = _attribute_value(name, value_text)
        except ValueError as exc:
            return {"error": str(exc)}
        return {"name": name, "value": value}

    def _run_light_or_camera(self, parsed: dict[str, Any], dry_run: bool, named: str | None) -> dict[str, Any]:
        """Preview a light or camera; it is created only for an explicit new path, explicit targets and dry_run=False."""
        action, path = parsed["action"], parsed["path"]
        is_light = action == "create_light"
        noun = f"{parsed['light_type']} light" if is_light else "camera"
        method = getattr(self._scene, action, None)
        if not callable(method):
            return _service_lacks(action, action, f"create a {noun}", path=path)
        notes: list[str] = []
        translation = parsed.get("translation")
        placed_above = False
        if parsed.get("above") is not None and translation is None:
            translation, reason = self._position_above(parsed["above"])
            if translation is None:
                notes.append(
                    f"I could not work out where 'above {parsed['above']}' is ({reason}), so it would stay at its "
                    f"parent's origin, only aimed at {parsed['above']}. Add 'at x y z' to place it."
                )
            else:
                placed_above = True
                notes.append(
                    f"Placed one bounding-box size above {parsed['above']} (world space; it matches the {noun}'s "
                    "parent space when that parent has no transform)."
                )
        keys = ("intensity", "exposure", "color", "size") if is_light else ("focal_length",)
        options: dict[str, Any] = {key: parsed[key] for key in keys if parsed.get(key) is not None}
        if translation is not None:
            options["translation"] = tuple(translation)
        if parsed.get("aim_at") is not None:
            options["aim_at"] = parsed["aim_at"]
        position_known = parsed.get("above") is None or translation is not None
        apply = not dry_run and bool(parsed.get("explicit")) and named is None and position_known
        try:
            if is_light:
                result = method(path, parsed["light_type"], dry_run=not apply, **options)
            else:
                result = method(path, dry_run=not apply, **options)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "target": "scene", "action": action, "path": path, "dry_run": not apply,
                    "applied": False, "message": f"{exc} Nothing was changed."}
        payload = self._wrap_result(result)
        inner = result.payload if isinstance(result, OperationResult) and isinstance(result.payload, dict) else {}
        for key in ("unsupported", "needs_bridge_update", "fake_scene", "warnings", "error"):
            if key in inner:
                payload[key] = inner[key]
        payload["action"], payload["path"] = action, path
        if is_light:
            payload["light_type"] = parsed["light_type"]
        if notes:
            payload["notes"] = notes
        if named is not None:
            payload["resolved_from_name"] = {"name": named, "path": parsed.get("aim_at")}
        command = self._authoring_command(parsed, translation)
        if not payload.get("ok"):
            payload["applied"] = False
            payload["command"] = command
            detail = (
                inner.get("error") or inner.get("message") or payload.get("message")
                or "The scene bridge could not plan this."
            )
            payload["message"] = f"{detail} Nothing was changed."
            if inner.get("needs_bridge_update") or inner.get("unsupported"):
                type_name = _LIGHT_TYPE_NAMES[parsed["light_type"]] if is_light else "Camera"
                payload["fallback_command"] = f"create {path} type {type_name}"
                payload["hint"] = (
                    f"This scene bridge cannot plan a {noun} with settings or aim. '{payload['fallback_command']}' "
                    f"creates a bare {type_name} prim instead, with no {'intensity or aim' if is_light else 'lens or aim'}."
                )
            return payload
        applied = payload.get("dry_run") is False
        payload["applied"] = applied
        if applied:
            payload["message"] = f"Created the {noun} {path} in the open scene; it is not saved yet."
            return payload
        reasons = []
        if dry_run:
            reasons.append("you asked for a dry run")
        if named is not None:
            reasons.append(f"'{named}' is a name, not an exact prim path")
        if not parsed.get("explicit"):
            reasons.append(f"I chose the path {path} for you")
        if not position_known:
            reasons.append("its position is unknown")
        payload["preview"] = True
        payload["command"] = command
        payload["message"] = (
            f"Preview only: nothing was changed ({'; '.join(reasons) or 'preview'}). "
            f"Send '{command}' with router_dispatch(dry_run=False) to create it."
        )
        if placed_above:
            payload["placement"] = {"above": parsed["above"], "translation": list(translation or ())}
        return payload

    @staticmethod
    def _authoring_command(parsed: dict[str, Any], translation: Any) -> str:
        """The explicit sentence that creates this light or camera; it parses back with an explicit path."""
        is_light = parsed["action"] == "create_light"
        parts = [f"add a {parsed['light_type']} light {parsed['path']}" if is_light else f"add a camera {parsed['path']}"]
        if translation is not None:
            parts.append("at " + " ".join(_format_number(value) for value in translation))
        aim = parsed.get("aim_at")
        if aim is not None:
            target = aim if isinstance(aim, str) else " ".join(_format_number(value) for value in aim)
            parts.append(("aimed at " if is_light else "looking at ") + target)
        if is_light:
            parts += [
                f"{key} {_format_number(parsed[key])}"
                for key in ("intensity", "exposure", "size")
                if parsed.get(key) is not None
            ]
            if parsed.get("color") is not None:
                parts.append("color " + " ".join(_format_number(value) for value in parsed["color"]))
        elif parsed.get("focal_length") is not None:
            parts.append(f"focal length {_format_number(parsed['focal_length'])}")
        return " ".join(parts)

    def _position_above(self, path: str) -> tuple[tuple[float, float, float] | None, str]:
        """A world point one bounding-box size above a prim's live bounds, or (None, why not). Read-only."""
        insight = getattr(self._scene, "live_insight", None)
        if not callable(insight):
            return None, "this scene service cannot read live bounds"
        try:
            bounds = insight("bounds", {"prim_path": path})
        except Exception as exc:  # noqa: BLE001 - placement is best effort; the light is still only previewed
            return None, f"{type(exc).__name__}: {exc}"
        record = _world_bound(bounds)
        if record is None:
            message = bounds.get("message") if isinstance(bounds, dict) else None
            return None, str(message or f"no live world bounds for {path}")
        axis = self._placement_up_axis(insight)
        if axis is None:
            return None, "the stage up axis (upAxis) is unknown"
        index = 1 if axis == "Y" else 2
        point = [float(value) for value in record["center"]]
        lift = max(abs(float(value)) for value in record["size"]) or 1.0
        point[index] = float(record["max"][index]) + lift
        return (round(point[0], 6) + 0.0, round(point[1], 6) + 0.0, round(point[2], 6) + 0.0), ""

    def _placement_up_axis(self, insight: Callable[..., Any]) -> str | None:
        """upAxis from the saved root layer, else from the live stage_info query; None when neither says."""
        saved = self._stage_up_axis()
        if saved is not None:
            return saved["axis"]
        try:
            info = insight("stage_info", {})
        except Exception:  # noqa: BLE001 - an unknown axis just means no placement
            return None
        if not isinstance(info, dict) or info.get("ok") is False:
            return None
        axis = info.get("up_axis")
        if axis is None and isinstance(info.get("result"), dict):
            axis = info["result"].get("up_axis")
        return axis if axis in {"Y", "Z"} else None

    def _run_create_material(self, parsed: dict[str, Any]) -> dict[str, Any]:
        """Materials described in a sentence are always previewed; the payload names the exact tool call to apply."""
        path = parsed["path"]
        creator = getattr(self._scene, "create_material", None)
        if not callable(creator):
            return _service_lacks("create_material", "create_material", "create materials", path=path)
        options = {key: parsed[key] for key in ("diffuse_color", "roughness", "metallic") if parsed.get(key) is not None}
        try:
            result = creator(path, dry_run=True, **options)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "target": "scene", "action": "create_material", "path": path, "dry_run": True,
                    "applied": False, "message": f"{exc} Nothing was changed."}
        payload = self._wrap_result(result)
        listed = {key: list(value) if isinstance(value, tuple) else value for key, value in options.items()}
        arguments = {"path": path, **listed}
        payload.update(action="create_material", path=path, applied=False, preview=True)
        payload["apply_with"] = {"tool": "scene_create_material", "arguments": {**arguments, "dry_run": False}}
        chosen = "" if parsed.get("explicit") else f" I chose the path {path} for you."
        payload["message"] = (
            f"Preview only: nothing was created.{chosen} A material described in a sentence is always previewed; "
            f"call scene_create_material with apply_with's arguments to create it, then 'bind {path} to <prim path>'."
        )
        inner = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        if not payload.get("ok"):  # the preview was refused (path taken, bad value): say why first
            detail = inner.get("error") or inner.get("message") or "The scene bridge could not plan this material."
            payload["message"] = f"{detail} Nothing was created."
        return payload

    def _run_paint(self, parsed: dict[str, Any], named: str | None) -> dict[str, Any]:
        """'paint X red': preview creating (or reusing) a color material and binding it; never applied directly."""
        path, color = parsed["path"], tuple(parsed["color"])
        prim = self._safe_inspect(path)
        if prim is None:
            return {"ok": False, "target": "scene", "action": "paint", "path": path, "applied": False,
                    "message": f"There is no prim at {path}. Nothing was changed."}
        if re.search(r"Light(?:_\d+)?$", str(prim.get("type_name") or "")):
            # A light's color is its inputs:color attribute, not a material.
            painted = self._run_set_attribute(
                {"action": "set_attribute", "path": path, "attribute": "color", "value": parsed["color_text"]},
                dry_run=True,
                named=named,
            )
            painted.setdefault("notes", []).append(f"{path} is a light, so its color is inputs:color, not a material.")
            return painted
        material_path, reuse = self._paint_material(parsed["color_text"], color)
        base = {"target": "scene", "action": "paint", "path": path, "material_path": material_path, "color": list(color)}
        changes: list[dict[str, Any]] = []
        ok = True
        detail: list[str] = []
        if reuse:
            binder = getattr(self._scene, "bind_material", None) or self._scene.set_material
            bound = self._wrap_result(binder(path, material_path, dry_run=True))
            changes += bound.get("changes") or []
            ok = bool(bound.get("ok"))
        else:
            creator = getattr(self._scene, "create_material", None)
            if not callable(creator):
                return _service_lacks(
                    "create_material", "paint", f"make a {parsed['color_text']} material",
                    path=path, material_path=material_path, color=list(color),
                )
            try:
                created = self._wrap_result(creator(material_path, diffuse_color=color, dry_run=True))
            except (ValueError, TypeError) as exc:
                return {"ok": False, **base, "applied": False, "message": f"{exc} Nothing was changed."}
            changes += created.get("changes") or []
            ok = bool(created.get("ok"))
            if not ok:
                inner = created.get("payload") if isinstance(created.get("payload"), dict) else {}
                detail.append(str(inner.get("error") or inner.get("message") or created.get("message") or ""))
            changes.append({
                "action": "bind_material",
                "path": path,
                "summary": f"Bind {material_path} to {path}",
                "before": {"material": prim.get("material")},
                "after": {"material": material_path},
            })
        steps = [] if reuse else [{
            "tool": "scene_create_material",
            "arguments": {"path": material_path, "diffuse_color": list(color), "dry_run": False},
        }]
        steps.append({
            "tool": "scene_bind_material",
            "arguments": {"prim_path": path, "material_path": material_path, "dry_run": False},
        })
        command = f"bind {material_path} to {path}"
        payload = {
            "ok": ok,
            **base,
            "dry_run": True,
            "applied": False,
            "preview": True,
            "creates_material": not reuse,
            "changes": changes,
            "steps": steps,
            "command": command,
        }
        made = f"reuse {material_path}" if reuse else f"create {material_path}"
        payload["message"] = (
            f"Preview only: nothing was changed. Painting would {made} and bind it to {path}. "
            + ("Run the steps' tool calls in order to apply it." if not reuse else f"Run '{command}' to apply it.")
        )
        if detail and any(detail):
            payload["message"] = f"{' '.join(part for part in detail if part)} {payload['message']}"
        if named is not None:
            payload["resolved_from_name"] = {"name": named, "path": path}
        return payload

    def _paint_material(self, color_text: str, color: tuple[float, ...]) -> tuple[str, bool]:
        """(material path, reuse?): /World/Looks/<Color>Material, reused only when it is already a Material prim."""
        word = color_text.strip().lower()
        if word in _COLOR_WORDS:
            base = f"{word.capitalize()}Material"
        else:
            base = "Color_" + "".join(f"{max(0, min(255, round(value * 255))):02X}" for value in color[:3])
        candidate = f"{_DEFAULT_LOOKS}/{base}"
        existing = self._safe_inspect(candidate)
        if existing is not None and existing.get("type_name") == "Material":
            return candidate, True
        if existing is None:
            return candidate, False
        return self._free_child_path(_DEFAULT_LOOKS, base), False

    def _place_created_prim(self, payload: dict[str, Any], path: str, translation: Any, dry_run: bool) -> None:
        """Second step of 'add a sphere at 0 0 2': set the new prim's translate (only described in a dry run)."""
        values = tuple(float(value) for value in translation)
        if dry_run:
            payload["changes"].append({
                "action": "transform",
                "path": path,
                "summary": f"Set the translate of {path} to {' '.join(_format_number(value) for value in values)}",
                "before": None,
                "after": {"translation": list(values)},
            })
            if isinstance(payload.get("payload"), dict):
                payload["payload"]["translation"] = list(values)
            return
        try:
            moved = self._scene.move_prim(path, translation=values, dry_run=False)
        except ValueError as exc:
            payload.setdefault("warnings", []).append(f"Created {path} but could not move it: {exc}")
            return
        payload["changes"].extend(asdict(change) for change in moved.changes)
        payload["payload"] = moved.payload
        payload["ok"] = bool(payload["ok"] and moved.ok)
        if moved.ok:
            # The bridges record the create and the move as separate undo entries (and Kit Ctrl+Z steps).
            payload["undo_steps"] = 2
            payload.setdefault("notes", []).append(
                f"Created and then moved {path}: that is two undo steps. The first 'undo' only removes the move "
                "(the prim goes back to its parent's origin); the second removes the prim."
            )

    # --- tier 4: assets, physics, screenshots, simulation and the exam dashboard (phrases in nl_tier4.py) ---------

    def _parse_tier4(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        """Simulation, screenshot and physics phrases -> a parsed action; None for anything else."""
        simulation = nl_tier4.parse_simulation(text)
        if simulation is not None:
            return {"action": "simulation", **simulation}
        screenshot = nl_tier4.parse_screenshot(text)
        if screenshot is not None:
            return {"action": "screenshot", **screenshot}
        physics = nl_tier4.parse_physics(text)
        if physics is None:
            return None
        kind = physics["kind"]
        if kind == "physics_scene":
            path = physics.get("path")
            if path is not None and not re.fullmatch(r"(?:/[A-Za-z_]\w*)+", path):
                return _refusal(f"'{path}' is not a valid prim path.", "add a physics scene /physicsScene")
            return {"action": "physics_scene", "path": path or "/physicsScene", "explicit": path is not None,
                    "gravity": physics.get("gravity")}
        examples = {
            "physics_report": "physics report for /World/Props",
            "rigid_body": "make /World/Props/Cube a rigid body",
            "collider": "add collision to /World/Props/Cube",
            "remove_physics": "remove physics from /World/Props/Cube",
        }
        parsed: dict[str, Any] = {"action": kind}
        if kind == "physics_report" and physics.get("target") is None:
            parsed["path"] = None
            return parsed
        resolved = self._physics_target(physics.get("target") or "", selected_path)
        if "error" in resolved:
            refusal = _refusal(resolved["error"], examples[kind])
            if resolved.get("candidates"):
                refusal["candidates"] = resolved["candidates"]
            return refusal
        parsed["path"] = resolved["path"]
        if resolved.get("named"):
            parsed["resolved_from_name"] = resolved["named"]
        if kind == "rigid_body":
            parsed.update(kinematic=bool(physics.get("kinematic")), mass=physics.get("mass"))
        elif kind == "collider":
            if physics.get("approximation_text") and physics.get("approximation") is None:
                return _refusal(
                    f"I do not know the collision approximation '{physics['approximation_text']}'; use convex hull, "
                    "convex decomposition, bounding box, bounding sphere, mesh simplification or triangle mesh.",
                    "add collision to /World/Props/Cube with convex hull",
                )
            parsed.update(approximation=physics.get("approximation"), approximation_text=physics.get("approximation_text"))
        elif kind == "remove_physics":
            parsed["include_descendants"] = bool(physics.get("include_descendants"))
        return parsed

    def _physics_target(self, fragment: str, selected_path: str | None) -> dict[str, Any]:
        """{"path"} for a bare path or the selection, {"path", "named"} for a unique prim name, else {"error"}."""
        path = self._fragment_target(fragment, selected_path)
        if path is not None:
            return {"path": path}
        if "/" in fragment:
            return {"error": f"'{fragment}' is not exactly one prim path."}
        words = [word for word in re.findall(r"[\w\-]+", fragment.lower()) if word not in _NAME_ARTICLES]
        if not words:
            return {"error": f"I could not tell which prim '{fragment}' is."}
        found, candidates = self._unique_prim_named(words)
        if found is None:
            return {"error": f"I could not find exactly one prim named '{' '.join(words)}'.", "candidates": candidates}
        return {"path": found, "named": " ".join(words)}

    def _run_physics_edit(self, parsed: dict[str, Any], dry_run: bool, named: str | None) -> dict[str, Any]:
        """Preview a physics edit; it applies only for an exact path (or the selection) with dry_run=False."""
        action, path = parsed["action"], parsed["path"]
        method_name = {
            "rigid_body": "make_rigid_body",
            "collider": "make_collider",
            "remove_physics": "remove_physics",
            "physics_scene": "add_physics_scene",
        }[action]
        method = getattr(self._scene, method_name, None)
        if not callable(method):
            return _service_lacks(method_name, action, "set up physics", path=path)
        options: dict[str, Any] = {}
        if action == "rigid_body":
            options = {"kinematic": bool(parsed.get("kinematic")), **({"mass": parsed["mass"]} if parsed.get("mass") else {})}
        elif action == "collider" and parsed.get("approximation"):
            options = {"approximation": parsed["approximation"]}
        elif action == "remove_physics":
            options = {"include_descendants": bool(parsed.get("include_descendants"))}
        elif action == "physics_scene" and parsed.get("gravity") is not None:
            options = {"gravity": parsed["gravity"]}
        explicit = parsed.get("explicit", True) and named is None
        preview = dry_run or not explicit
        command = self._physics_command(parsed)
        try:
            result = method(path, dry_run=preview, **options)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "target": "scene", "action": action, "path": path, "dry_run": True, "applied": False,
                    "command": command, "message": f"{exc} Nothing was changed."}
        payload = self._wrap_result(result)
        inner = result.payload if isinstance(result, OperationResult) and isinstance(result.payload, dict) else {}
        for key in ("unsupported", "needs_bridge_update", "fake_scene", "warnings", "notes", "error", "undoable"):
            if key in inner:
                payload[key] = inner[key]
        payload.update(action=action, path=path, command=command)
        if named is not None:
            payload["resolved_from_name"] = {"name": named, "path": path}
        if not payload.get("ok"):
            payload["applied"] = False
            detail = inner.get("error") or inner.get("message") or "The scene bridge could not plan this."
            payload["message"] = f"{detail} Nothing was changed."
            return payload
        if payload.get("dry_run") is False:
            payload["applied"] = True
            payload["message"] = (
                f"Applied to the open scene (not saved): '{command}'. Say 'undo' to preview undoing it."
            )
            return payload
        reasons = []
        if dry_run:
            reasons.append("you asked for a dry run")
        if named is not None:
            reasons.append(f"'{named}' is a name, not an exact prim path")
        if not parsed.get("explicit", True):
            reasons.append(f"I chose the path {path} for you")
        payload.update(applied=False, preview=True)
        payload["message"] = (
            f"Preview only: nothing was changed ({'; '.join(reasons) or 'preview'}). "
            f"Send '{command}' with router_dispatch(dry_run=False) to apply it."
        )
        return payload

    @staticmethod
    def _physics_command(parsed: dict[str, Any]) -> str:
        """The exact sentence that applies a parsed physics edit; it parses back to the same action."""
        action, path = parsed["action"], parsed["path"]
        if action == "rigid_body":
            text = f"make {path} a {'kinematic ' if parsed.get('kinematic') else ''}rigid body"
            return text + (f" with mass {_format_number(parsed['mass'])}" if parsed.get("mass") else "")
        if action == "collider":
            words = parsed.get("approximation_text")
            return f"add collision to {path}" + (f" with {words}" if words else "")
        if action == "remove_physics":
            return f"remove physics from {path}" + (" and its children" if parsed.get("include_descendants") else "")
        gravity = parsed.get("gravity")
        text = f"add a physics scene {path}"
        if gravity is not None:
            values = gravity if isinstance(gravity, (list, tuple)) else [gravity]
            text += " with gravity " + " ".join(_format_number(value) for value in values)
        return text

    def _run_physics_report(self, parsed: dict[str, Any]) -> dict[str, Any]:
        reporter = getattr(self._scene, "physics_report", None)
        if not callable(reporter):
            return _service_lacks("physics_report", "physics_report", "report the physics setup")
        payload = self._wrap_result(reporter(parsed.get("path")))
        payload["action"] = "physics_report"
        if payload.get("unsupported") or payload.get("needs_bridge_update"):
            payload["hint"] = "scene_physics_report(mode='saved_file') reads the physics setup of the saved file instead."
        return payload

    def _run_screenshot(self, parsed: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        """'take a screenshot' captures the active viewport (no scene change); 'is my screenshot ready' checks it."""
        if parsed["op"] == "status":
            if self._last_capture is None:
                return {"ok": False, "target": "scene", "action": "capture_status",
                        "message": "No screenshot was taken through the router yet; say 'take a screenshot'."}
            payload = self._wrap_result(self._scene.capture_status(self._last_capture))
            payload["action"] = "capture_status"
            return payload
        size = {key: parsed[key] for key in ("width", "height") if parsed.get(key)}
        if dry_run:
            return {"ok": True, "target": "scene", "action": "capture_viewport", "dry_run": True, "applied": False,
                    "preview": True, "would": "capture the active viewport to a new PNG", **size,
                    "message": "Preview only: no screenshot was taken. Send it again with dry_run=False to take it."}
        capture = getattr(self._scene, "capture_viewport", None)
        if not callable(capture):
            return _service_lacks("capture_viewport", "capture_viewport", "take a screenshot")
        payload = self._wrap_result(capture(None, **size))
        payload["action"] = "capture_viewport"
        if payload.get("ok") and isinstance(payload.get("path"), str):
            self._last_capture = payload["path"]
            payload["next_step"] = "Say 'is my screenshot ready' (or call scene_capture_status(path)) in a moment."
        return payload

    def _run_simulation(self, parsed: dict[str, Any], dry_run: bool) -> dict[str, Any]:
        """Simulation controls run only when explicitly requested ('play the simulation'); a dry run previews them."""
        op = parsed["op"]
        method_name = f"simulation_{op}"
        method = getattr(self._scene, method_name, None)
        if not callable(method):
            return _service_lacks(method_name, method_name, f"{op} the simulation")
        if op != "status" and dry_run:
            would = f"step the simulation {parsed.get('frames', 1)} frame(s)" if op == "step" else f"{op} the simulation"
            return {"ok": True, "target": "scene", "action": method_name, "dry_run": True, "applied": False,
                    "preview": True, "would": would,
                    "message": f"Preview only: the simulation was not touched. Send it with dry_run=False to {would}."}
        payload = self._wrap_result(method(parsed["frames"]) if op == "step" else method())
        payload["action"] = method_name
        if op != "status":
            payload["applied"] = bool(payload.get("ok"))
            payload.setdefault(
                "note", "Simulation state only: no authored scene data changed and nothing was saved."
            )
        return payload

    # --- tier 5: learning helpers (phrases in nl_tier5.py; every one is read-only) -----------------------------------

    async def _run_learning_helper(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Run one nl_tier5 request: the attached tool when there is one, else the pure helper or the tool to call."""
        kind = spec["kind"]
        tool, arguments = _learning_helper_call(spec)
        base: dict[str, Any] = {"target": "learn", "style": kind, "tool": tool, "arguments": arguments, "read_only": True}
        self._record_learning_event(f"learning_helper_{kind}")
        helper = self._learning_helpers.get(kind)
        try:
            if helper is not None:
                result = await asyncio.to_thread(helper, **arguments)
            else:
                result = _pure_learning_helper(kind, arguments)
        except Exception as exc:  # noqa: BLE001 - a broken helper is reported, never raised into the client
            return {"ok": False, **base, "executed": True, "message": f"{tool} failed: {type(exc).__name__}: {exc}"}
        if result is None:
            call = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
            return {"ok": True, **base, "executed": False,
                    "message": f"Call {tool}({call}) for this; this router has no {tool} helper attached."}
        payload = {**base, "executed": True, **result}
        payload.setdefault("ok", True)
        if spec.get("next_step_only") and isinstance(result.get("next_step"), (str, dict)):
            payload["answer"] = result["next_step"]
        return payload

    async def _run_dashboard(self, write: bool) -> dict[str, Any]:
        base = {"target": "learn", "style": "dashboard", "tool": "learning_exam_dashboard"}
        self._record_learning_event("exam_dashboard", write=write)
        if self._dashboard is None:
            return {"ok": True, **base, "executed": False,
                    "message": "Call learning_exam_dashboard() for your dashboard; this router has no exam progress "
                    "store attached."}
        try:
            data = await asyncio.to_thread(self._dashboard, write)
        except Exception as exc:  # noqa: BLE001 - a broken store is reported, not raised
            return {"ok": False, **base, "executed": True, "message": f"Could not build the dashboard: {exc}"}
        return {**base, "executed": True, **data}

    def _try_asset_request(self, request: str) -> dict[str, Any] | None:
        """'find a carter robot' / 'add a franka robot [at x y z]': search the asset pack and PREVIEW adding the top hit.

        Always a preview (the asset is chosen by search, not an exact path); the payload names the scene_add_asset
        call that applies it. 'add a <word>' with no confident asset match returns None, so the router's plain
        create runs as before.
        """
        text = re.sub(r"\s+", " ", self._strip_politeness(request.strip(), keep_case=True))
        found = nl_tier4.parse_asset_find(text)
        added = None if found is not None else nl_tier4.parse_asset_add(text)
        spec = found or added
        if spec is None:
            return None
        search = getattr(self._scene, "asset_search", None)
        adder = getattr(self._scene, "add_asset", None)
        if not callable(search) or not callable(adder):
            return None if added else _service_lacks("asset_search", "asset_search", "search the asset library")
        query = spec["query"]
        result = search(query, top_k=10)
        hits = [hit for hit in result.get("results") or [] if isinstance(hit, dict) and hit.get("referenceable", True)]
        confident = [hit for hit in hits if self._asset_covers(query, hit)]
        if added is not None and not confident:
            return None
        base = {"target": "scene", "action": "add_asset" if added else "asset_search", "query": query,
                "applied": False}
        if not hits:
            reason = result.get("message") or result.get("error")
            return {"ok": False, **base, "results": [],
                    "message": f"No asset in the local asset pack matches '{query}'"
                    + (f" ({reason})" if reason else "") + ". Nothing was changed.",
                    "hint": "Try another word (asset_search(query, category='robots') lists matches)."}
        ranked = self._rank_assets(confident or hits, query)
        top = ranked[0]
        ambiguous = len(ranked) > 1 and float(ranked[1].get("score") or 0) >= _ASSET_TIE * float(top.get("score") or 0)
        candidates = [
            {key: hit.get(key) for key in ("path", "relative_path", "name", "category", "score")} for hit in ranked[:5]
        ]
        parent = (added or {}).get("parent") or "/World"
        stem = re.sub(r"\W", "_", os.path.splitext(str(top.get("name") or "Asset"))[0]) or "Asset"
        name = ("_" + stem if stem[0].isdigit() else stem[0].upper() + stem[1:])
        prim_path = self._scene.unique_child_path(parent, name)
        translation = (added or {}).get("translation")
        options: dict[str, Any] = {"correct_units": True}
        if translation is not None:
            options["translation"] = tuple(translation)
        try:
            preview = adder(prim_path, top["path"], dry_run=True, **options)
        except (ValueError, TypeError) as exc:
            return {"ok": False, **base, "path": prim_path, "asset": top, "candidates": candidates,
                    "message": f"{exc} Nothing was changed."}
        payload = self._wrap_result(preview)
        inner = preview.payload if isinstance(preview, OperationResult) and isinstance(preview.payload, dict) else {}
        for key in ("unsupported", "needs_bridge_update", "fake_scene", "warnings", "notes", "error"):
            if key in inner:
                payload[key] = inner[key]
        arguments: dict[str, Any] = {"prim_path": prim_path, "asset_path": top["path"], "correct_units": True}
        if translation is not None:
            arguments["translation"] = list(translation)
        payload.update(
            **base,
            path=prim_path,
            asset={key: top.get(key) for key in ("path", "relative_path", "name", "category", "thumbnail", "score")},
            candidates=candidates,
            ambiguous=ambiguous,
            preview=True,
            apply_with={"tool": "scene_add_asset", "arguments": {**arguments, "dry_run": False}},
        )
        if found is not None:
            payload["results"] = candidates
        pick = f"{top.get('relative_path') or top.get('name')}"
        if not payload.get("ok"):
            detail = inner.get("error") or inner.get("message") or "The scene bridge could not plan this."
            payload["message"] = f"Found {pick}, but adding it could not be planned: {detail} Nothing was changed."
            return payload
        choice = (
            f"Several assets match '{query}' about equally well (see candidates); this previews the first."
            if ambiguous
            else f"The best match for '{query}' is {pick}."
        )
        payload["message"] = (
            f"{choice} Preview only: nothing was changed, because an asset found by search is never added from a "
            f"sentence. Call scene_add_asset with apply_with's arguments to add it at {prim_path}."
        )
        return payload

    @staticmethod
    def _asset_covers(query: str, hit: dict[str, Any]) -> bool:
        """Every word of the query is a word of the hit's library path (folder or file name, camelCase split, plain
        s/es plurals); a word of five or more letters may also sit inside one ('shelf' in 'rackshelf').
        Short words never match inside other words ('tree' is not 'streetlight', 'sun' not 'countersunk')."""
        tokens = _asset_tokens(str(hit.get("relative_path") or hit.get("path") or ""))
        words = [word for word in re.findall(r"[a-z0-9]+", query.lower()) if len(word) > 1]
        return bool(words) and all(_asset_word_covered(word, tokens) for word in words)

    @staticmethod
    def _rank_assets(hits: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
        """Library hits with whole sample scenes and training/alternate-physics copies last (unless asked for)."""
        wanted = query.lower()

        def variant(hit: dict[str, Any]) -> bool:
            category = str(hit.get("category") or "").lower()
            return category in _ASSET_VARIANT_CATEGORIES and category.split("_")[0] not in wanted

        return sorted(hits, key=lambda hit: (variant(hit), -float(hit.get("score") or 0.0)))

    def _wrap_result(self, result: Any) -> dict[str, Any]:
        """Scene payload for an OperationResult, a plain dict result, or any other value."""
        if isinstance(result, OperationResult):
            return self._wrap_operation_result("scene", result)
        if isinstance(result, dict):
            payload = dict(result)
            if "target" in payload and payload["target"] != "scene":
                payload["result_target"] = payload["target"]
            payload["target"] = "scene"
            payload["ok"] = bool(result.get("ok", True))
            return payload
        return {"ok": True, "target": "scene", "result": result}

    def _dispatch_learning(self, request: str) -> dict[str, Any]:
        style = self._detect_learning_style(request)
        topic = (
            self._detect_learning_topic(request)
            or (self._named_topic(request) if style == "quiz" else None)
            or "schemas"
        )
        self._record_learning_event("request", topic=topic, style=style, request=request)
        if style == "practice":
            return self.start_practice(topic)
        if style == "quiz":
            quiz = self._build_scene_aware_quiz(topic)
            payload = {"ok": bool(quiz["questions"]), "target": "learn", "topic": topic, "style": style, "quiz": quiz}
            if "message" in quiz:
                payload["message"] = quiz["message"]
            return payload
        if style == "study":
            return self._build_study_mode(topic)
        if style == "translate":
            return self.translate_command(request)
        guide = self._build_learning_guide(topic)
        if style == "cheatsheet":
            return {
                "ok": True,
                "target": "learn",
                "topic": topic,
                "style": style,
                "title": guide["title"],
                "cheatsheet": guide["cheatsheet"],
                "commands": guide["commands"],
                "scene_context": self._scene_context(),
            }
        return {
            "ok": True,
            "target": "learn",
            "topic": topic,
            "style": style,
            "title": guide["title"],
            "explanation": guide["explanation"],
            "key_points": guide["key_points"],
            "practice": guide["practice"],
            "commands": guide["commands"],
            "scene_context": self._scene_context(),
        }

    def _fallback_lesson(self, request: str) -> dict[str, Any] | None:
        """Built-in lesson for a knowledge question whose backend failed; reads no scene and records no history."""
        normalized = self._normalize(request)
        topic = next((name for name, pattern in _FALLBACK_TOPICS if pattern.search(normalized)), None)
        if topic is None:
            return None
        guide = self._build_learning_guide(topic)
        return {
            "target": "learn",
            "topic": topic,
            "style": "lesson",
            "title": guide["title"],
            "explanation": guide["explanation"],
            "key_points": guide["key_points"],
            "practice": guide["practice"],
            "commands": guide["commands"],
            "note": "Offline built-in lesson; the knowledge backend could not answer.",
        }

    def _exam_request(self, normalized: str) -> tuple[str, int | None] | None:
        """("day", N) or ("day", None) for the next open day, ("mock", None), or None for any other request."""
        if _MOCK_EXAM.search(re.sub(r"/\S*", " ", normalized)):  # never match inside a prim path
            return ("mock", None)
        core = self._strip_politeness(normalized).rstrip("?.! ")
        match = _EXAM_DAY.match(core)
        if match:
            word = match.group("day")
            return ("day", int(word) if word.isdigit() else _DAY_WORDS[word])
        if _NEXT_LESSON.match(core):
            return ("day", None)
        return None

    async def _dispatch_exam(self, kind: str, day: int | None) -> dict[str, Any]:
        """Run the daily lesson through the exam_day hook, or explain how to start a mock (never starts one)."""
        if kind == "mock":
            return self._mock_exam_instructions()
        arguments = {} if day is None else {"day": day}
        call = "learning_exam_day()" if day is None else f"learning_exam_day(day={day})"
        base = {"target": "learn", "style": "exam_day", "tool": "learning_exam_day", "arguments": arguments}
        self._record_learning_event("exam_day", day=day)
        if self._exam_day is None:
            return {
                "ok": True,
                **base,
                "executed": False,
                "message": f"Call {call} for this lesson; this router has no exam progress store attached.",
            }
        try:
            # The first lesson loads the offline docs index (about 1-2 s), so keep it off the event loop.
            lesson = await asyncio.to_thread(self._exam_day, day)
        except Exception as exc:  # noqa: BLE001 - a broken store or plan is reported, not raised
            return {"ok": False, **base, "executed": True, "message": f"Could not build the lesson: {type(exc).__name__}: {exc}"}
        return {**base, "executed": True, **lesson}

    def _mock_exam_instructions(self) -> dict[str, Any]:
        count, minutes = MOCK_EXAM_DEFAULTS["count"], MOCK_EXAM_DEFAULTS["minutes"]
        payload: dict[str, Any] = {
            "ok": True,
            "target": "learn",
            "style": "mock_exam",
            "started": False,
            "tool": "learning_mock_exam_start",
            "arguments": dict(MOCK_EXAM_DEFAULTS),
            "message": (
                f"A mock exam is timed: {count} questions in {minutes} minutes at the official domain weights, and "
                "the clock starts the moment it is created, so the router never starts one from a sentence. When you "
                f"are ready, call learning_mock_exam_start(count={count}, minutes={minutes}); a smaller count and "
                "time give a shorter practice run."
            ),
            "steps": [
                f"learning_mock_exam_start(count={count}, minutes={minutes}) starts the clock and shows the first "
                "page of questions (answers withheld).",
                "learning_mock_exam_questions(session_id, offset=10) shows the next page.",
                "learning_mock_exam_answer(session_id, question_id, chosen) records or changes an answer "
                "(letters or 0-based indices).",
                "learning_mock_exam_flag(session_id, question_id) marks a question to revisit.",
                "learning_mock_exam_status(session_id) shows the time left and what is unanswered or flagged.",
                "learning_mock_exam_finish(session_id) scores it per domain, explains wrong answers and saves the "
                "answered questions to your exam progress.",
                "learning_mock_exam_abandon(session_id) closes it without scoring.",
            ],
            "note": "NVIDIA publishes no passing score; treat a mock score as a readiness check, not a pass mark.",
        }
        if self._mock_status is not None:
            try:
                active = self._mock_status()
            except Exception as exc:  # noqa: BLE001 - an unreadable session file must not break the explanation
                payload["open_mock_error"] = f"{type(exc).__name__}: {exc}"
            else:
                if active:
                    keys = ("session_id", "state", "remaining", "answered", "total", "deadline")
                    payload["open_mock"] = {key: active.get(key) for key in keys}
                    payload["tool"] = "learning_mock_exam_status"
                    payload["arguments"] = {"session_id": active.get("session_id")}
                    payload["message"] = (
                        f"Mock {active.get('session_id')} is already open ({active.get('remaining')} left, "
                        f"{active.get('answered')}/{active.get('total')} answered). Continue it with "
                        "learning_mock_exam_status and learning_mock_exam_answer, or finish or abandon it before "
                        "starting another."
                    )
        self._record_learning_event("mock_exam", started=False)
        return payload

    def translate_command(self, request: str) -> dict[str, Any]:
        parsed = self._parse_scene_command(request)
        route = self.route(request)
        suggestion: dict[str, Any]
        if parsed is not None:
            suggestion = {"kind": "scene_action", "action": parsed["action"], "arguments": parsed}
        else:
            suggestion = self._suggest_command_from_request(request)
        self._record_learning_event("translate", request=request, route=route.target.value, suggestion=suggestion)
        return {
            "ok": True,
            "target": "learn",
            "style": "translate",
            "request": request,
            "route": {
                "target": route.target.value,
                "confidence": route.confidence,
                "reason": route.reason,
            },
            "suggestion": suggestion,
            "scene_context": self._scene_context(),
        }

    def learning_history(self, limit: int = 20) -> dict[str, Any]:
        recent = self._learning_history[-max(1, limit) :]
        return {
            "ok": True,
            "target": "learn",
            "history": recent,
            "progress": self._learning_progress_snapshot(),
            "cheatsheet": self.cheat_sheet(),
        }

    def cheat_sheet(self) -> dict[str, Any]:
        scene = self._scene_context()
        target = self._example_target(scene)
        root = self._example_root(scene)
        commands = {
            "scene": [
                f"inspect {target}",
                f"children {root}",
                f"select {target}",
                "inspect this",
                f"move {target} by 0 0 1",
                "rename this to HeroCube",
                "find cube",
                "add a sphere at 0 0 2",
                f"add a rect light above {target}",
                f"paint {target} red",
                "undo",
                "what did you change",
            ],
            "learning": [
                "help",
                "practice transforms with me",
                "quiz me on schemas",
                "what should I do next?",
                "translate: make this bigger",
                "start day 1",
                "next lesson",
                "mock exam",
            ],
        }
        return {
            "ok": True,
            "target": "learn",
            "scene_context": scene,
            "commands": commands,
            "progress": self._learning_progress_snapshot(),
        }

    def learning_roadmap(self) -> dict[str, Any]:
        progress = self._learning_progress_snapshot()
        completed_topics = set(progress["topics"].keys())
        roadmap = self._build_roadmap(completed_topics)
        return {
            "ok": True,
            "target": "learn",
            "progress": progress,
            "roadmap": roadmap,
            "scene_context": self._scene_context(),
        }

    def _record_learning_event(self, event: str, **details: Any) -> None:
        try:
            summary = self._scene.scene_summary()
            scene = {"stage_name": summary.get("stage_name"), "prim_count": summary.get("prim_count")}
        except Exception:  # noqa: BLE001 - learning history must not depend on the scene bridge
            scene = {"stage_name": None, "prim_count": None}
        self._learning_history.append({"event": event, "details": details, "scene": scene})
        self._learning_history = self._learning_history[-100:]

    def _scene_context(self) -> dict[str, Any]:
        """Scene summary for learning payloads; lessons still work when the scene bridge is unreachable."""
        try:
            return self._scene.scene_summary()
        except Exception as exc:  # noqa: BLE001 - learning content does not need a live scene
            return {"available": False, "error": str(exc), "selection": []}

    def _learning_progress_snapshot(self) -> dict[str, Any]:
        topics = {
            topic: count
            for topic, count in self._learning_progress["topics"].items()
        }
        return {
            "topics": topics,
            "completed_sessions": self._learning_progress["completed_sessions"],
            "completed_steps": self._learning_progress["completed_steps"],
            "recent_events": [entry["event"] for entry in self._learning_history[-10:]],
        }

    def _build_roadmap(self, completed_topics: set[str]) -> list[dict[str, Any]]:
        roadmap = [
            {
                "topic": "prims",
                "status": "done" if "prims" in completed_topics else "next",
                "why": "Prims are the basic building blocks of the stage.",
            },
            {
                "topic": "selection",
                "status": "done" if "selection" in completed_topics else "next",
                "why": "Selection is how you target the right prim for natural language.",
            },
            {
                "topic": "transforms",
                "status": "done" if "transforms" in completed_topics else "next",
                "why": "Transforms teach you how to place assets in the scene.",
            },
            {
                "topic": "materials",
                "status": "done" if "materials" in completed_topics else "next",
                "why": "Materials are the fastest way to see a visible result.",
            },
            {
                "topic": "schemas",
                "status": "done" if "schemas" in completed_topics else "next",
                "why": "Schemas help you understand what a prim is capable of.",
            },
            {
                "topic": "lights",
                "status": "done" if "lights" in completed_topics else "next",
                "why": "Lights are key for understanding the full look of a scene.",
            },
        ]
        return roadmap

    @staticmethod
    def _suggest_command_from_request(request: str) -> dict[str, Any]:
        lower = request.lower()
        if re.search(r"\b(?:bigger|larger|scale up|10x|ten times)\b", lower):
            return {
                "kind": "scene_action",
                "action": "move",
                "command": "make this 10x bigger",
                "arguments": {"scale_factor": [10.0, 10.0, 10.0]},
                "message": "Try scaling the selected prim up; this multiplies its current scale.",
            }
        if re.search(r"\b(?:red|paint|color it red)\b", lower):
            return {
                "kind": "scene_action",
                "action": "material",
                "command": "bind /World/Looks/RedMaterial to this",
                "arguments": {"material_path": "/World/Looks/RedMaterial"},
                "message": "Try binding a red material; it must exist first ('find material' lists yours).",
            }
        if re.search(r"\b(?:up|higher|raise)\b", lower):
            return {
                "kind": "scene_action",
                "action": "move",
                "command": "move this up by 2",
                "arguments": {"up_offset": 2.0},
                "message": "Try moving the selected prim 2 units along the stage up axis (upAxis).",
            }
        if re.search(r"\b(?:rename|call it|name it)\b", lower):
            return {
                "kind": "scene_action",
                "action": "rename",
                "command": "rename this to PracticeTarget",
                "arguments": {"new_name": "PracticeTarget"},
                "message": "Try renaming the selected prim.",
            }
        if re.search(r"\b(?:under environment|move under|parent it|nest it)\b", lower):
            new_parent = "/Environment" if "environment" in lower else "/World"
            return {
                "kind": "scene_action",
                "action": "reparent",
                "command": f"reparent this to {new_parent}",
                "arguments": {"new_parent": new_parent},
                "message": f"Try reparenting the selected prim under {new_parent}.",
            }
        return {
            "kind": "explain",
            "message": "I could not map that to one exact scene command yet. Try saying it with the prim path or selection.",
            "examples": ["move this by 0 0 1", "bind /World/Looks/RedMaterial to this", "rename this to HeroCube"],
        }

    def _wrap_operation_result(self, target: str, result: OperationResult) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "target": target,
            "dry_run": result.dry_run,
            "changes": [asdict(change) for change in result.changes],
            "payload": result.payload,
        }

    def _looks_like_scene_request(self, request: str) -> bool:
        scene_keywords = (
            "stage",
            "scene",
            "prim",
            "camera",
            "light",
            "material",
            "selection",
            "select",
            "hierarchy",
            "parent",
            "child",
            "transform",
            "move",
            "rotate",
            "scale",
            "bigger",
            "larger",
            "smaller",
            "paint",
            "duplicate",
            "delete",
            "create",
            "reparent",
            "rename",
            "find",
            "inspect",
            "summary",
            "show me",
            "what's selected",
            "what is selected",
            "what am i looking at",
            "tell me about",
            "look at",
        )
        if request.startswith(("scene ", "prim ", "stage ", "camera ", "material ")):
            return True
        return any(keyword in request for keyword in scene_keywords)

    def _looks_like_explicit_scene_command(self, request: str) -> bool:
        return bool(
            re.match(
                r"^(?:scene\s+)?(?:ping|status|summary|inspect|children|create|delete|duplicate|reparent|move|material|select|rename|find|save)\b",
                request,
            )
        )

    def _looks_like_targeted_scene_command(self, request: str) -> bool:
        """An imperative scene verb aimed at a path, the selection or numbers, 'add a <thing>' or 'find <word>'."""
        prefix = r"^(?:scene\s+)?(?:can you\s+)?(?:please\s+)?"
        if re.match(prefix + r"(?:add|create|spawn|place)\s+(?:a|an)\s+\w", request) or re.match(prefix + r"create\s+/", request):
            return True
        # "find light" searches the stage; "find out how lights work" or "find docs" do not.
        find = re.match(
            prefix + r"find\s+(?:all\s+|every\s+|the\s+)?[\w-]+(?:\s+(?:in|under|inside|within)\s+/\S*)?$",
            request.rstrip("?.! "),
        )
        if find and not re.search(r"\b(?:docs?|documentation|examples?|api|tutorials?)\b", request):
            return True
        # "make" is ordinary English, so it needs a path, the selection or a size factor and no learning wording.
        if re.match(
            prefix + r"make\b.*(?:/|\b(?:this|that|it|selected)\b|\b(?:twice|half|double|thrice)\b|\d+\s*(?:x|times)\b)",
            request,
        ) and not any(phrase in request for phrase in _LEARNING_PHRASES):
            return True
        verbs = (
            r"(?:inspect|children|delete|remove|duplicate|reparent|move|shift|nudge|rotate|scale|put|nest|material|"
            r"bind|assign|paint|select|focus|highlight|rename|save)"
        )
        return bool(re.match(prefix + verbs + r"\b.*(?:/|\b(?:this|that|it|selected)\b|\d)", request))

    def _is_question(self, request: str) -> bool:
        if request.startswith(("what ", "how ", "why ", "when ", "where ", "which ")):
            return True
        # "find cube in usd?" is still a scene command, despite the question mark.
        return request.endswith("?") and not self._looks_like_explicit_scene_command(request)

    def _detect_knowledge_target(self, request: str) -> RouteTarget:
        kit_words = ("extension", "app template", "window", "ui layout", "omniverse kit", "omni.kit")
        omni_ui_words = ("omni.ui", "widget", "ui class", "ui api", "style docs", "layout")
        isaac_words = ("isaac", "robot", "simulation", "physx", "articulation", "sensor", "ros 2")
        usd_words = (
            "openusd",
            "omni.usd",
            "pxr.usd",
            "prim definition",
            "sdf",
            "usda",
            "usdz",
            "schema",
            "reference",
            "payload",
        )
        bowerbot_words = (
            "bowerbot",
            "asset assembly",
            "scene assembly",
            "build a scene",
            "assemble a scene",
            "new project",
            "open project",
            "list projects",
            "package scene",
            "validate scene",
            "project setup",
        )

        if any(word in request for word in bowerbot_words):
            return RouteTarget.BOWERBOT
        if any(word in request for word in isaac_words):
            return RouteTarget.ISAAC_SIM
        if any(word in request for word in omni_ui_words):
            return RouteTarget.OMNI_UI
        if any(word in request for word in kit_words) or _KIT_WORD.search(request):
            return RouteTarget.KIT
        if any(word in request for word in usd_words) or _USD_WORD.search(request) or _USD_EXAM_TERMS.search(request):
            return RouteTarget.USD
        return RouteTarget.UNKNOWN

    @staticmethod
    def _detect_learning_topic(request: str) -> str | None:
        lowered = request.lower()
        topic = next((name for name, pattern in _FALLBACK_TOPICS if pattern.search(lowered)), None)
        if topic is None and _WORKFLOW_TOPIC.search(lowered):
            return "workflow"
        return topic

    @staticmethod
    def _named_topic(request: str) -> str | None:
        """The subject after 'on' or 'about' in 'quiz me on payloads', for topics without a lesson pattern."""
        match = re.search(r"\b(?:on|about)\s+(.+)$", request.lower())
        if not match:
            return None
        filler = {"the", "a", "an", "usd", "openusd", "my", "this", "it", "me", "some"}
        words = [word for word in re.findall(r"[a-z0-9][\w.-]*", match.group(1)) if word not in filler]
        return " ".join(words) or None

    @staticmethod
    def _detect_learning_style(request: str) -> str:
        # Dotted module names (omni.kit.commands) are API names, not the words "commands" or "study".
        lowered = re.sub(r"\w+(?:\.\w+)+", " ", request.lower())
        if any(word in lowered for word in ("quiz", "test me", "check my understanding")):
            return "quiz"
        if any(word in lowered for word in ("practice", "drill", "hands on", "hands-on", "do it with me", "walk me through", "show me how")):
            return "practice"
        if any(word in lowered for word in ("study", "what should i do next", "next step", "suggest", "recommend", "what next")):
            return "study"
        if any(word in lowered for word in ("translate", "convert", "paraphrase", "what does this mean", "how do i say")):
            return "translate"
        if any(word in lowered for word in ("cheat sheet", "cheatsheet", "quick reference", "commands", "shortcuts")):
            return "cheatsheet"
        return "lesson"

    def _build_learning_guide(self, topic: str) -> dict[str, Any]:
        """Static lesson content; every entry in "commands" is a runnable scene command."""
        guides: dict[str, dict[str, Any]] = {
            "schemas": {
                "title": "USD Schemas in Plain English",
                "explanation": (
                    "In USD, a schema is the definition that gives a prim its behavior and meaning. "
                    "The easiest way to think about it is: the type tells you what the prim is, and "
                    "schemas tell you what properties and behavior it supports."
                ),
                "key_points": [
                    "Typed schemas describe the prim's main type, like Xform, Cube, Mesh, Camera, DomeLight, or DistantLight.",
                    "Applied/API schemas add extra capabilities to a prim without changing its main type.",
                    "Subtypes are more specific specializations inside a schema family.",
                    "The Type column in the stage tree is showing the prim's main typed schema or type name.",
                ],
                "practice": [
                    "Inspect a prim and compare its path, type, parent, and material.",
                    "Create an Xform, then a Cube, and notice how their Type values differ.",
                    "Select a prim and rename it, then observe that the type stays the same.",
                ],
                "cheatsheet": [
                    "Typed schema: the prim's main identity.",
                    "Applied schema: extra capability layered onto the prim.",
                    "Subtype: a more specific variant of a schema family.",
                    "Stage tree Type column: the main type, not every applied schema.",
                ],
                "commands": ["inspect /World/Props/Cube", "find cube"],
            },
            "prims": {
                "title": "What a Prim Is",
                "explanation": (
                    "A prim is a node in the USD stage graph. It can represent a transform, a mesh, a light, "
                    "a camera, or another scene concept. Prims are what you inspect, select, rename, move, and organize."
                ),
                "key_points": [
                    "Every prim has a path like /World/Props/Cube.",
                    "Parents and children define scene hierarchy.",
                    "Transforms, materials, and selection are all attached to prims.",
                ],
                "practice": [
                    "Open the stage tree and compare /World, /World/Props, and the leaf prim /World/Props/Cube.",
                    "List the children of a parent prim to see the hierarchy.",
                ],
                "cheatsheet": [
                    "Path = the prim's address.",
                    "Parent = where it lives in the hierarchy.",
                    "Children = what it contains.",
                ],
                "commands": ["inspect /World", "children /World/Props", "find cube"],
            },
            "selection": {
                "title": "Selection and Focus",
                "explanation": (
                    "Selection is the active prim or set of prims you are working on. "
                    "Many natural-language commands like 'this', 'it', or 'selected' refer to the current selection."
                ),
                "key_points": [
                    "Single selection can drive natural-language commands.",
                    "Multiple selection is useful when you want to compare or operate on several prims at once.",
                    "Selection does not change the prim's type; it only changes what is active.",
                ],
                "practice": [
                    "Select one prim, then ask to inspect this.",
                    "Select several prims and see how the stage tree and scene summary reflect that.",
                ],
                "cheatsheet": [
                    "'this', 'it' and 'selected' mean the single selected prim.",
                    "Selection is app state; it is not authored into a layer.",
                    "Select first, then inspect, then edit.",
                ],
                "commands": ["select /World/Props/Cube", "inspect this", "move it 1 2 3"],
            },
            "transforms": {
                "title": "Transforms",
                "explanation": (
                    "Transforms change where a prim is, how it rotates, and how large it is. "
                    "They are one of the most important daily USD skills because they let you place assets in the scene."
                ),
                "key_points": [
                    "Translation moves the prim.",
                    "Rotation turns the prim.",
                    "Scale changes its size.",
                ],
                "practice": [
                    "Move a Cube up, then make it bigger, then compare the result in the viewport.",
                    "Ask for a dry run first when you're not sure what will change.",
                ],
                "cheatsheet": [
                    "'move X x y z' sets the translate; 'move X by dx dy dz' adds to it.",
                    "'rotate X rx ry rz' sets absolute XYZ angles in degrees.",
                    "'make X twice as big' multiplies the current scale.",
                    "'up' follows the stage upAxis metadata (Y or Z); it is never assumed.",
                ],
                "commands": [
                    "move /World/Props/Cube 1 2 3",
                    "move /World/Props/Cube by 0 0 1",
                    "move /World/Props/Cube up by 1",
                    "rotate /World/Props/Cube 0 0 90",
                    "scale /World/Props/Cube 2 2 2",
                    "make /World/Props/Cube twice as big",
                ],
            },
            "materials": {
                "title": "Materials",
                "explanation": (
                    "A material controls how a prim looks when it is rendered. "
                    "In practice, you bind a material to a prim, then inspect the scene to see the visual result."
                ),
                "key_points": [
                    "Materials affect appearance, not hierarchy.",
                    "Binding a material is different from changing the prim's type.",
                ],
                "practice": [
                    "Bind a red material to a cube and compare before and after.",
                    "Ask what material is currently bound to a prim before changing it.",
                ],
                "cheatsheet": [
                    "A binding is a relationship (material:binding) from the prim to a Material prim.",
                    "The Material prim must exist before you bind it.",
                    "Binding changes appearance, never the prim's type or hierarchy.",
                ],
                "commands": [
                    "inspect /World/Props/Cube",
                    "find material",
                    "bind /World/Looks/RedMaterial to /World/Props/Cube",
                ],
            },
            "lights": {
                "title": "Lights",
                "explanation": (
                    "Lights are prims that affect illumination. Dome lights, distant lights, and area lights are common starting points."
                ),
                "key_points": [
                    "Lights are prims with lighting behavior.",
                    "Lighting changes the look of the whole scene, not just one asset.",
                ],
                "practice": [
                    "Find the light prims in the stage and note each one's type.",
                    "Add a DistantLight and rotate it to change where the light comes from.",
                ],
                "cheatsheet": [
                    "DomeLight",
                    "DistantLight",
                    "AreaLight",
                ],
                "commands": [
                    "find light",
                    "create /World/PracticeLight type DistantLight",
                    "rotate /World/PracticeLight -45 0 0",
                ],
            },
            "workflow": {
                "title": "A Simple Learning Workflow",
                "explanation": (
                    "The fastest way to learn USD is to inspect a prim, understand its type, change one thing, and check the result. "
                    "Repeat with selection, hierarchy, transforms, and materials."
                ),
                "key_points": [
                    "Inspect first.",
                    "Change one thing at a time.",
                    "Use dry runs when possible.",
                    "Use the stage tree to build intuition.",
                ],
                "practice": [
                    "Inspect a prim, change one property, then inspect it again.",
                    "Repeat the loop with selection, hierarchy, transforms, and materials.",
                ],
                "cheatsheet": [
                    "Inspect first.",
                    "Change one thing.",
                    "Preview with dry_run.",
                    "Verify the result.",
                ],
                "commands": [
                    "scene summary",
                    "inspect /World/Props/Cube",
                    "move /World/Props/Cube by 0 0 1",
                    "rename /World/Props/Cube to HeroCube",
                ],
            },
        }
        return guides.get(topic, guides["schemas"])

    @staticmethod
    def _build_quiz(topic: str) -> dict[str, Any] | None:
        quiz = _QUIZZES.get(topic)
        return {key: list(value) for key, value in quiz.items()} if quiz else None

    def _build_scene_aware_quiz(self, topic: str) -> dict[str, Any]:
        scene = self._scene_context()
        selection = scene.get("selection", [])
        selected_path = selection[0] if len(selection) == 1 else None
        selected_prim = self._safe_inspect(selected_path)
        quiz_topics = sorted(_QUIZZES)
        if selected_prim is not None:
            questions = [
                f"What is the prim path of the currently selected object?",
                f"What is the Type of {selected_path}?",
                "If you rename this prim, what changes and what stays the same?",
            ]
            answers = [
                selected_prim["path"],
                selected_prim["type_name"],
                "The name/path changes, but the prim's type stays the same.",
            ]
            if selected_prim.get("material"):
                questions.append("What material is currently bound to the selected prim?")
                answers.append(selected_prim["material"])
            quiz = {
                "topic": topic,
                "scene_context": scene,
                "selected_prim": selected_prim,
                "questions": questions,
                "answers": answers,
            }
            if topic not in _QUIZZES:
                quiz["quiz_topics"] = quiz_topics
                quiz["note"] = (
                    f"There is no built-in {topic} quiz yet, so this quiz uses the selected prim. "
                    f"Topic quizzes: {', '.join(quiz_topics)}."
                )
            return quiz

        base_quiz = self._build_quiz(topic)
        if base_quiz is None:
            return {
                "topic": topic,
                "questions": [],
                "answers": [],
                "quiz_topics": quiz_topics,
                "scene_context": scene,
                "message": (
                    f"There is no built-in quiz for '{topic}' yet. Quizzes exist for: {', '.join(quiz_topics)}. "
                    "Select a prim for a quiz about the live scene."
                ),
            }
        base_quiz["scene_context"] = scene
        base_quiz["hint"] = "Select a prim first if you want a quiz that uses the live scene."
        return base_quiz

    def _build_study_mode(self, topic: str | None = None) -> dict[str, Any]:
        scene = self._scene_context()
        selection = scene.get("selection", [])
        selected_path = selection[0] if len(selection) == 1 else None
        selected_prim = self._safe_inspect(selected_path)
        topic = topic or self._infer_study_topic(scene, selected_prim)
        target = selected_path or self._example_target(scene)
        suggestion = self._study_suggestion(topic, scene, selected_prim, target)
        return {
            "ok": True,
            "target": "learn",
            "style": "study",
            "topic": topic,
            "scene_context": scene,
            "selected_prim": selected_prim,
            "recommendation": suggestion["recommendation"],
            "why": suggestion["why"],
            "try_next": suggestion["try_next"],
            "what_to_look_for": suggestion["what_to_look_for"],
        }

    def _safe_inspect(self, path: str | None) -> dict[str, Any] | None:
        if not path:
            return None
        try:
            return self._scene.inspect_prim(path)
        except Exception:  # noqa: BLE001 - a stale selection just means no prim to teach from
            return None

    def start_practice(self, topic: str = "schemas") -> dict[str, Any]:
        session = self._make_practice_session(topic)
        self._practice_session = session
        self._record_learning_event("practice_start", topic=session["topic"], session_id=session["session_id"])
        self._learning_progress["topics"][session["topic"]] = self._learning_progress["topics"].get(session["topic"], 0) + 1
        return self._practice_state_payload(session, started=True)

    def next_practice_step(self, feedback: str | None = None) -> dict[str, Any]:
        if self._practice_session is None:
            return {
                "ok": False,
                "message": "No active practice session. Start one first with a practice request.",
            }
        if feedback:
            self._practice_session.setdefault("feedback", []).append(feedback)
        self._practice_session["step_index"] += 1
        self._learning_progress["completed_steps"] += 1
        self._record_learning_event(
            "practice_step",
            topic=self._practice_session["topic"],
            feedback=feedback,
            step_index=self._practice_session["step_index"],
        )
        if self._practice_session["step_index"] >= len(self._practice_session["steps"]):
            finished = self._practice_session
            self._practice_session = None
            self._learning_progress["completed_sessions"] += 1
            self._record_learning_event("practice_complete", topic=finished["topic"], session_id=finished["session_id"])
            return {
                "ok": True,
                "target": "learn",
                "style": "practice",
                "status": "complete",
                "topic": finished["topic"],
                "message": "Nice, you finished the drill.",
                "recap": finished["steps"],
            }
        return self._practice_state_payload(self._practice_session, started=False)

    def scene_quiz(self, topic: str = "schemas") -> dict[str, Any]:
        quiz = self._build_scene_aware_quiz(topic)
        self._record_learning_event("quiz", topic=topic)
        return {
            "ok": bool(quiz["questions"]),
            "target": "learn",
            "style": "quiz",
            "topic": topic,
            **quiz,
        }

    def study_mode(self, topic: str | None = None) -> dict[str, Any]:
        result = self._build_study_mode(topic or "workflow")
        self._record_learning_event("study", topic=result["topic"])
        return result

    def _make_practice_session(self, topic: str) -> dict[str, Any]:
        scene = self._scene_context()
        drill_topic = topic if topic in _PRACTICE_TOPICS else "schemas"
        template = self._practice_template(drill_topic, scene)
        session = {
            "session_id": f"practice-{drill_topic}",
            "topic": drill_topic,
            "step_index": 0,
            "steps": template["steps"],
            "commands": template["commands"],
            "title": template["title"],
            "scene_context": scene,
        }
        if drill_topic != topic:
            session["note"] = (
                f"There is no '{topic}' drill yet, so this is the schemas drill. Drills: {', '.join(_PRACTICE_TOPICS)}."
            )
        return session

    def _practice_template(self, topic: str, scene: dict[str, Any]) -> dict[str, Any]:
        """One command per step, with paths taken from the current scene where possible."""
        root = self._example_root(scene)
        if topic == "prims":
            return {
                "title": "Interactive prim drill",
                "steps": [
                    f"Inspect the root prim {root}.",
                    f"List the children of {root}.",
                    "Find every prim whose path or type mentions 'cube'.",
                ],
                "commands": [f"inspect {root}", f"children {root}", "find cube"],
            }
        if topic == "lights":
            light = self._free_child_path(root, "PracticeLight")
            return {
                "title": "Interactive lighting drill",
                "steps": [
                    "List the lights already in the stage.",
                    f"Add a DistantLight at {light}.",
                    "Inspect the new light and read its Type.",
                    "Rotate the light to change the direction its light comes from.",
                ],
                "commands": [
                    "find light",
                    f"create {light} type DistantLight",
                    f"inspect {light}",
                    f"rotate {light} -45 0 0",
                ],
            }
        target = self._example_target(scene)
        if topic == "selection":
            return {
                "title": "Interactive selection drill",
                "steps": [
                    "Select one prim.",
                    "Inspect it through the selection by saying 'this'.",
                    "Move it by an offset through the selection by saying 'it'.",
                ],
                "commands": [f"select {target}", "inspect this", "move it by 1 0 0"],
            }
        if topic == "transforms":
            return {
                "title": "Interactive transform drill",
                "steps": [
                    "Inspect the target prim and note its translation, rotation and scale.",
                    "Move it 2 units along the stage up axis (upAxis).",
                    "Rotate it to 0 0 45 degrees (absolute XYZ angles).",
                    "Make it twice as big (multiplies the current scale).",
                ],
                "commands": [
                    f"inspect {target}",
                    f"move {target} up by 2",
                    f"rotate {target} 0 0 45",
                    f"make {target} twice as big",
                ],
            }
        if topic == "materials":
            material = self._free_child_path(root, "PracticeMaterial")
            return {
                "title": "Interactive material drill",
                "steps": [
                    "Inspect the target prim and note its material.",
                    "Create a Material prim to bind.",
                    "Bind the new material to the target.",
                    "Inspect the target again and verify the binding.",
                ],
                "commands": [
                    f"inspect {target}",
                    f"create {material} type Material",
                    f"bind {material} to {target}",
                    f"inspect {target}",
                ],
            }
        if topic == "workflow":
            return {
                "title": "Interactive USD workflow drill",
                "steps": [
                    "Inspect the scene.",
                    "Inspect one prim.",
                    "Change one thing.",
                    "Verify the result.",
                ],
                "commands": [
                    "scene summary",
                    f"inspect {target}",
                    f"make {target} twice as big",
                    f"inspect {target}",
                ],
            }
        cube = self._free_child_path(root, "SchemaCube")
        return {
            "title": "Interactive schema drill",
            "steps": [
                "Inspect a prim and read its Type.",
                "Create a Cube to compare with it.",
                "Rename the Cube and confirm in the result that its Type is still Cube.",
            ],
            "commands": [f"inspect {target}", f"create {cube} type Cube", f"rename {cube} to RenamedCube"],
        }

    def _example_target(self, scene: dict[str, Any]) -> str:
        """Drill target: the single selected prim, else /World/Props/Cube or the first Cube in the stage."""
        selection = scene.get("selection") or []
        if len(selection) == 1:
            return selection[0]
        if scene.get("available") is False:
            return _DEFAULT_TARGET
        if self._safe_inspect(_DEFAULT_TARGET) is not None:
            return _DEFAULT_TARGET
        try:
            cubes = [prim["path"] for prim in self._scene.find_prims("cube")["prims"] if prim.get("type_name") == "Cube"]
        except Exception:  # noqa: BLE001 - examples fall back to the documented default
            cubes = []
        return cubes[0] if cubes else _DEFAULT_TARGET

    @staticmethod
    def _example_root(scene: dict[str, Any]) -> str:
        root = scene.get("root_path")
        if root and root != "/":
            return root
        top_level = scene.get("top_level_prims") or []
        return top_level[0] if top_level else "/World"

    def _free_child_path(self, parent: str, name: str) -> str:
        try:
            return self._scene.unique_child_path(parent, name)
        except Exception:  # noqa: BLE001 - a suggestion path does not need a live scene
            return f"{parent.rstrip('/')}/{name}"

    def _practice_state_payload(self, session: dict[str, Any], started: bool) -> dict[str, Any]:
        index = session["step_index"]
        steps = session["steps"]
        commands = session["commands"]
        current_step = steps[index]
        current_command = commands[index] if index < len(commands) else None
        next_step = steps[index + 1] if index + 1 < len(steps) else None
        next_command = commands[index + 1] if index + 1 < len(commands) else None
        payload = {
            "ok": True,
            "target": "learn",
            "style": "practice",
            "status": "active",
            "started": started,
            "session_id": session["session_id"],
            "topic": session["topic"],
            "title": session["title"],
            "steps": steps,
            "commands": commands,
            "current_step_index": index + 1,
            "step_count": len(steps),
            "current_step": current_step,
            "current_command": current_command,
            "next_step": next_step,
            "next_command": next_command,
            "scene_context": session["scene_context"],
            "instruction": "Do the current step in USD Composer, then call the next-practice tool or say 'done'.",
            "next_prompt": "Finish the current step, then ask for the next practice step.",
        }
        if "note" in session:
            payload["note"] = session["note"]
        return payload

    @staticmethod
    def _infer_study_topic(scene: dict[str, Any], selected_prim: dict[str, Any] | None) -> str:
        if selected_prim is None:
            return "selection" if not scene.get("selection") else "workflow"
        if selected_prim.get("material") is None and selected_prim.get("type_name") in {"Cube", "Mesh"}:
            return "materials"
        if selected_prim.get("children"):
            return "prims"
        if selected_prim.get("type_name") == "Xform":
            return "transforms"
        return "workflow"

    @staticmethod
    def _study_suggestion(
        topic: str, scene: dict[str, Any], selected_prim: dict[str, Any] | None, target: str
    ) -> dict[str, Any]:
        if selected_prim is None:
            return {
                "recommendation": "Select a prim in the stage tree first.",
                "why": "The best next lesson depends on what is currently active in the scene.",
                "try_next": [f"select {target}", "inspect this"],
                "what_to_look_for": ["path", "type", "children", "material"],
            }
        path = selected_prim["path"]
        if topic == "materials":
            return {
                "recommendation": f"Practice materials on {path}; say 'practice materials' for a guided binding drill.",
                "why": "It is a visible prim with no material bound yet, so the change will be obvious.",
                "try_next": ["find material", f"inspect {path}"],
                "what_to_look_for": ["material binding", "viewport color", "type staying the same"],
            }
        if topic == "prims":
            return {
                "recommendation": f"Study the hierarchy around {path}.",
                "why": "Prims make more sense when you inspect parents and children together.",
                "try_next": [f"children {path}", f"inspect {path}"],
                "what_to_look_for": ["parent path", "children", "how the scene is organized"],
            }
        if topic == "transforms":
            return {
                "recommendation": f"Practice transforms on {path}.",
                "why": "Transforms are easiest to learn on a visible prim with a clear before/after.",
                "try_next": [f"move {path} up by 1", f"make {path} twice as big"],
                "what_to_look_for": ["position", "size", "orientation", "which axis is up"],
            }
        return {
            "recommendation": f"Use the current prim {path} as your study target.",
            "why": f"This prim is a good anchor for learning {topic}.",
            "try_next": [f"inspect {path}", f"rename {path} to PracticeTarget"],
            "what_to_look_for": ["type", "path", "selection", "what changes versus what stays the same"],
        }

    def _parse_scene_command(self, request: str) -> dict[str, Any] | None:
        # Collapse runs of whitespace so "create  the cube" cannot slip past the (?!the) guard below.
        text = re.sub(r"\s+", " ", self._strip_politeness(request.strip(), keep_case=True))
        lower = text.lower()
        if lower in {"ping", "scene ping", "status"}:
            return {"action": "ping"}
        if lower in {"summary", "scene summary", "scene status"}:
            return {"action": "summary"}
        if _UNDO_CONFIRM.match(lower):
            return {"action": "undo", "confirm": True}
        if _UNDO_REQUEST.match(lower):
            return {"action": "undo", "confirm": False}
        if _UNDO_HISTORY.match(lower):
            return {"action": "undo_history"}
        if _is_prim_count_question(lower):
            return {"action": "summary", "question": "prim_count"}
        if any(phrase in lower for phrase in ("what's selected", "what is selected", "current selection", "show selection")):
            return {"action": "summary"}

        natural_summary = (
            "show me the scene",
            "show the scene",
            "what am i looking at",
            "what is in the scene",
            "what's in the scene",
            "scene overview",
            "scene summary",
        )
        if any(phrase in lower for phrase in natural_summary):
            return {"action": "summary"}

        path = self._extract_usd_path(text)
        # Keyword checks ignore words inside prim paths, so /World/Eraser is not "erase" and /World/It is not "it".
        words = re.sub(r"/\S*", " ", lower)
        selected_path = self._single_selected_path()
        target_path = path or (selected_path if _PRONOUN.search(words) else None)

        tier4 = self._parse_tier4(text, selected_path)
        if tier4 is not None:
            return tier4

        attribute = self._parse_set_attribute(text, selected_path)
        if attribute is not None:
            return attribute

        select_match = re.match(r"^(?:scene\s+)?select\s+(.+)$", text, re.IGNORECASE)
        if select_match:
            paths = self._extract_paths(select_match.group(1))
            if paths:
                return {"action": "select", "paths": paths}
        if re.search(r"\b(?:select|focus|highlight)\b", words) and target_path is not None:
            return {"action": "select", "paths": [target_path]}

        create_match = re.match(r"^(?:scene\s+)?create\s+(/\S*)(?:\s+type\s+(\S+))?$", text, re.IGNORECASE)
        if create_match:
            return {"action": "create", "path": create_match.group(1), "type_name": create_match.group(2) or "Xform"}
        extended = self._parse_extended_create(text, selected_path)
        if extended is not None:
            return extended
        transform = self._parse_transform(text, selected_path)
        if transform is not None:
            return transform
        # "the/this/my ..." names something that already exists ("make the light brighter", "add my light to the
        # scene"), so it never creates a prim. "at x y z" is a position, never a parent path.
        create_text, translation = self._split_create_position(text)
        create_natural = re.match(
            r"^(?:make\s+(?:a|an|another|new)\s+|(?:create|add|spawn|place)\s+(?:a\s+|an\s+|another\s+|new\s+)?"
            r"(?!(?:the|this|that|these|those|it|its|my|our|your|selected)\b))(.+)$",
            create_text,
            re.IGNORECASE,
        )
        if create_natural:
            description = re.split(r"\s+(?:under|in|into|inside|at)\s+", create_natural.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
            type_name = self._guess_type_name(description)
            base_name = self._guess_base_name(description, type_name)
            parent_text = self._extract_create_parent(create_text)
            parent_path = (
                self._extract_usd_path(parent_text or "")
                or (selected_path if parent_text and _PRONOUN.search(parent_text.lower()) else None)
                or self._normalize_parent_path(parent_text or "/World")
            )
            created: dict[str, Any] = {
                "action": "create",
                "path": self._scene.unique_child_path(parent_path, base_name),
                "type_name": type_name,
            }
            if translation is not None:
                created["translation"] = translation
            return created

        delete_match = re.match(r"^(?:scene\s+)?(?:delete|remove|erase)\s+(?:the\s+)?(?:prim\s+)?(.+)$", text, re.IGNORECASE)
        if delete_match:
            # Deleting is destructive: only a bare path or the selection, never a path mentioned elsewhere.
            fragment = delete_match.group(1).strip()
            if re.fullmatch(r"/\S*", fragment):
                return {"action": "delete", "path": fragment}
            if selected_path and re.fullmatch(r"(?:this|that|it|selected)(?:\s+prim)?", fragment, re.IGNORECASE):
                return {"action": "delete", "path": selected_path}

        inspect_match = re.match(r"^(?:scene\s+)?inspect\s+(/\S*)$", text, re.IGNORECASE)
        if inspect_match:
            return {"action": "inspect", "path": inspect_match.group(1)}
        if re.search(r"\b(?:inspect|describe|look at|tell me about)\b", words) and target_path is not None:
            return {"action": "inspect", "path": target_path}

        # Before the "under" children check, so "move this under /World" reparents instead of listing children.
        reparent_match = re.match(r"^(?:scene\s+)?reparent\s+(/\S*)\s+to\s+(/\S*)$", text, re.IGNORECASE)
        if reparent_match:
            return {"action": "reparent", "path": reparent_match.group(1), "new_parent": reparent_match.group(2)}
        reparent_natural = re.match(
            r"^(?:move|put|nest|parent|reparent)\s+(.+?)\s+(?:under|into|inside|to)\s+(/\S*)$",
            text,
            re.IGNORECASE,
        )
        if reparent_natural:
            child_path = self._fragment_target(reparent_natural.group(1), selected_path)
            if child_path is not None:
                return {"action": "reparent", "path": child_path, "new_parent": reparent_natural.group(2)}

        children_match = re.match(r"^(?:scene\s+)?children\s+(/\S*)$", text, re.IGNORECASE)
        if children_match:
            return {"action": "children", "path": children_match.group(1)}
        if (
            re.search(r"\b(?:what's under|what is under|children of|inside|under)\b", words)
            and not re.match(r"(?:move|put|nest|parent|reparent|shift|nudge)\b", lower)
            and target_path is not None
        ):
            return {"action": "children", "path": target_path}

        duplicate_match = re.match(r"^(?:scene\s+)?duplicate\s+(/\S*)\s+to\s+(/\S*)$", text, re.IGNORECASE)
        if duplicate_match:
            return {
                "action": "duplicate",
                "source_path": duplicate_match.group(1),
                "target_path": duplicate_match.group(2),
            }
        duplicate_natural = re.match(r"^(?:duplicate|copy)\s+(.+?)\s+(?:to|as)\s+(\S+)$", text, re.IGNORECASE)
        if duplicate_natural:
            source_path = self._fragment_target(duplicate_natural.group(1), selected_path)
            if source_path is not None:
                copy = duplicate_natural.group(2)
                # A bare name ("as HeroCube2") becomes a sibling of the source.
                target = copy if copy.startswith("/") else f"{source_path.rsplit('/', 1)[0]}/{copy}"
                return {"action": "duplicate", "source_path": source_path, "target_path": target}

        move_match = re.match(rf"^(?:scene\s+)?move\s+(/\S*)\s+{_VECTOR}$", text, re.IGNORECASE)
        if move_match:
            return {
                "action": "move",
                "path": move_match.group(1),
                "translation": (
                    float(move_match.group(2)),
                    float(move_match.group(3)),
                    float(move_match.group(4)),
                ),
            }
        move_natural = re.match(rf"^(?:move|shift|nudge|translate)\s+(.+?)\s+(?:to\s+)?{_VECTOR}$", text, re.IGNORECASE)
        if move_natural:
            move_path = self._fragment_target(move_natural.group(1), selected_path)
            if move_path is not None:
                return {
                    "action": "move",
                    "path": move_path,
                    "translation": (
                        float(move_natural.group(2)),
                        float(move_natural.group(3)),
                        float(move_natural.group(4)),
                    ),
                }

        material_match = re.match(r"^(?:scene\s+)?material\s+(/\S*)\s+to\s+(/\S*)$", text, re.IGNORECASE)
        if material_match:
            return {
                "action": "material",
                "path": material_match.group(1),
                "material_path": material_match.group(2),
            }
        material_natural = re.match(
            r"^(bind|assign|set|give|paint)\s+(.+?)\s+(to|with|using)\s+(\S+)$",
            text,
            re.IGNORECASE,
        )
        if material_natural:
            verb, first, connector, second = material_natural.groups()
            # "bind M to P" names the material first; "paint P with M" and "set P to M" name the prim first.
            if verb.lower() in {"bind", "assign"} and connector.lower() == "to":
                material_text, prim_text = first, second
            else:
                prim_text, material_text = first, second
            # Both sides must be exact: "bind anything but /World/Looks/Red to /World/Hero" binds nothing.
            bare_material = _BARE_MATERIAL_PATH.fullmatch(material_text.strip())
            material_path = bare_material.group(1) if bare_material else None
            prim_path = self._fragment_target(prim_text, selected_path)
            if prim_path is not None and material_path is not None:
                return {
                    "action": "material",
                    "path": prim_path,
                    "material_path": material_path,
                }

        rename_match = re.match(r"^(?:scene\s+)?(?:rename|call|name)\s+(.+?)\s+(?:to|as)\s+(\S+)$", text, re.IGNORECASE)
        if rename_match:
            path_candidate = self._fragment_target(rename_match.group(1), selected_path)
            if path_candidate is not None:
                return {"action": "rename", "path": path_candidate, "new_name": rename_match.group(2)}

        find_match = re.match(r"^(?:scene\s+)?(?:find|search\s+for)\s+(.+)$", text, re.IGNORECASE)
        if find_match:
            query_text = find_match.group(1).strip()
            query, scope_path = self._split_query_and_scope(query_text)
            return {"action": "find", "query": query, "scope_path": scope_path}

        # Only a real USD file path is ever saved to; "save the scene" must not export to a file named "the scene".
        save_natural = re.match(
            r"^save\s+(?:(?:this|the|current)\s+)?(?:scene|stage)\s+(?:as|to)\s+(.+)$", text, re.IGNORECASE
        )
        save_match = re.match(r"^(?:scene\s+)?save(?:\s+as)?\s+(.+)$", text, re.IGNORECASE)
        for found in (save_natural, save_match):
            if found:
                candidate = found.group(1).strip()
                if re.fullmatch(r"\S+\.usd[acz]?", candidate, re.IGNORECASE) or re.fullmatch(
                    r"(?:[A-Za-z]:[\\/]|[\\/]).*\.usd[acz]?", candidate, re.IGNORECASE
                ):
                    return {"action": "save", "path": candidate}
        if re.match(r"(?:scene\s+)?save\b", text, re.IGNORECASE):
            return {
                "action": "unsupported",
                "path": None,
                "message": "Saving needs a file path ending in .usd, .usda, .usdc or .usdz. Nothing was saved.",
                "hint": "For example 'save C:/path/to/scene.usda', or scene_save(path, dry_run=True) to preview.",
            }

        return None

    def _parse_transform(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        """Translate/rotate/scale phrasings. Relative values ("by", "up", "twice") are made absolute at dispatch.

        A recognised phrasing whose target is only a name returns None, so name resolution can retry it.
        """

        def target(fragment: str) -> str | None:
            return self._fragment_target(fragment, selected_path)

        def vector(match: re.Match[str], first: int) -> tuple[float, float, float]:
            return (float(match.group(first)), float(match.group(first + 1)), float(match.group(first + 2)))

        def unsupported(fragment: str, message: str, example: str) -> dict[str, Any]:
            path = target(fragment) or "<prim path>"
            return {"action": "unsupported", "path": target(fragment), "message": message, "hint": example.format(path=path)}

        move = r"^(?:move|shift|nudge|translate)\s+(.+?)\s+"
        match = re.match(move + rf"by\s+{_VECTOR}$", text, re.IGNORECASE)
        if match:
            path = target(match.group(1))
            return {"action": "move", "path": path, "offset": vector(match, 2)} if path else None
        match = re.match(move + rf"(up|down)(?:wards?)?\s+(?:by\s+)?({_NUMBER})(?:\s*units?)?$", text, re.IGNORECASE)
        if match:
            path = target(match.group(1))
            amount = float(match.group(3)) * (1 if match.group(2).lower() == "up" else -1)
            return {"action": "move", "path": path, "up_offset": amount} if path else None
        match = re.match(
            move + rf"(?:left|right|forwards?|backwards?|back)(?:\s+(?:by\s+)?{_NUMBER}(?:\s*units?)?)?$", text, re.IGNORECASE
        )
        if match:
            return unsupported(
                match.group(1),
                "USD only defines an up axis (upAxis); left, right, forward and back depend on the camera, "
                "so I will not guess a direction. Nothing was changed.",
                "Give an explicit offset, for example 'move {path} by 1 0 0'.",
            )

        match = re.match(rf"^rotate\s+(.+?)\s+by\s+{_VECTOR}(?:\s*deg(?:rees?)?)?$", text, re.IGNORECASE)
        if match:
            return unsupported(
                match.group(1),
                "Rotation here sets absolute XYZ angles in degrees (xformOp:rotateXYZ), not an offset. Nothing was changed.",
                "Inspect the prim for its current angles, then give the final ones, for example 'rotate {path} 0 0 90'.",
            )
        match = re.match(rf"^rotate\s+(.+?)\s+(?:to\s+)?{_VECTOR}(?:\s*deg(?:rees?)?)?$", text, re.IGNORECASE)
        if match:
            path = target(match.group(1))
            return {"action": "move", "path": path, "rotation": vector(match, 2)} if path else None
        match = re.match(
            rf"^rotate\s+(.+?)\s+(?:by\s+)?{_NUMBER}\s*(?:deg(?:rees?)?)?(?:\s+(?:around|about|on)\s+.+)?$", text, re.IGNORECASE
        )
        if match:
            return unsupported(
                match.group(1),
                "Give all three XYZ angles in degrees; one angle does not say which axis to turn. Nothing was changed.",
                "For example 'rotate {path} 0 0 90'.",
            )

        match = re.match(rf"^scale\s+(.+?)\s+by\s+(?:{_VECTOR}|({_NUMBER})\s*(?:x|times)?)$", text, re.IGNORECASE)
        if match:
            path = target(match.group(1))
            factor = vector(match, 2) if match.group(2) else (float(match.group(5)),) * 3
            return {"action": "move", "path": path, "scale_factor": factor} if path else None
        match = re.match(rf"^scale\s+(.+?)\s+(?:to\s+)?(?:{_VECTOR}|({_NUMBER}))$", text, re.IGNORECASE)
        if match:
            path = target(match.group(1))
            scale = vector(match, 2) if match.group(2) else (float(match.group(5)),) * 3
            return {"action": "move", "path": path, "scale": scale} if path else None
        match = re.match(
            rf"^(?:make|scale)\s+(.+?)\s+(?:(twice|double|thrice|half)|({_NUMBER})\s*(?:x|times))\s+"
            r"(?:as\s+(?:big|large)|bigger|larger)(?:\s+as\s+(?:it\s+is|now|before))?$",
            text,
            re.IGNORECASE,
        )
        if match:
            path = target(match.group(1))
            value = _SCALE_WORDS[match.group(2).lower()] if match.group(2) else float(match.group(3))
            return {"action": "move", "path": path, "scale_factor": (value,) * 3} if path else None
        match = re.match(r"^(?:make|set|turn)\s+(.+?)\s+(bigger|larger|huge|smaller|small|tiny)$", text, re.IGNORECASE)
        if match:
            path = target(match.group(1))
            value = 0.1 if match.group(2).lower() in {"smaller", "small", "tiny"} else 10.0
            return {"action": "move", "path": path, "scale_factor": (value,) * 3} if path else None
        return None

    def _parse_set_attribute(self, text: str, selected_path: str | None) -> dict[str, Any] | None:
        """'set intensity of X to 500' is an attribute edit; 'set material of X to /M' binds exactly one material path.

        The target must be a bare path or the selection (else it is resolved by name, which only previews), and a
        material value that is not exactly one path is refused rather than written as a 'material' attribute.
        """
        match = next((found for found in (pattern.match(text) for pattern in _SET_ATTRIBUTE) if found), None)
        if match is None:
            return None
        attribute, fragment, value = (match.group(name).strip() for name in ("attr", "target", "value"))
        path = self._fragment_target(fragment, selected_path)
        named: str | None = None
        candidates: list[str] = []
        if path is None:
            words = [word for word in re.findall(r"[\w\-]+", fragment.lower()) if word not in _NAME_ARTICLES]
            if words:
                path, candidates = self._unique_prim_named(words)
                named = " ".join(words) if path else None
        if attribute.lower() in {"material", "material binding"}:
            bare_material = _BARE_MATERIAL_PATH.fullmatch(value)
            if bare_material is None:
                refusal = _refusal(
                    f"To bind a material, give exactly one material path, not '{value}' (to color a prim with a new "
                    "material, say 'paint <prim path> red').",
                    f"set the material of {path or '/World/Props/Cube'} to /World/Looks/Red",
                )
                refusal["path"] = path
                if candidates:
                    refusal["candidates"] = candidates
                return refusal
            if path:
                parsed: dict[str, Any] = {"action": "material", "path": path, "material_path": bare_material.group(1)}
                if named:
                    parsed["resolved_from_name"] = named
                return parsed
        parsed = {"action": "set_attribute", "path": path, "attribute": attribute, "value": value, "candidates": candidates}
        if named:
            parsed["resolved_from_name"] = named
        return parsed

    def _fragment_target(self, fragment: str, selected_path: str | None) -> str | None:
        """The prim a phrase points at: a bare path ('/World/Box', 'the prim /World/Box'), or the selection when the
        whole phrase is this/that/it/selected (optionally with 'the' and one noun). Any other phrase returns None,
        including one that only mentions a path ('everything except /World/Hero'), so name resolution refuses with
        candidates or only previews."""
        fragment = fragment.strip()
        bare = _BARE_PATH.fullmatch(fragment)
        if bare:
            return bare.group(1)
        return selected_path if _SELECTION_REF.fullmatch(fragment) else None

    @staticmethod
    def _strip_politeness(text: str, keep_case: bool = False) -> str:
        """Remove "can you / please / kindly" and a trailing "in kit?" so the scene parser sees the plain command."""
        lowered = text.lower()
        prefix = _POLITE_PREFIX.match(lowered)
        start = prefix.end() if prefix else 0
        suffix = _POLITE_SUFFIX.search(lowered, start)
        end = suffix.start() if suffix else len(text)
        core = (text if keep_case else lowered)[start:end].strip()
        return core or (text if keep_case else lowered).strip()

    def _resolve_by_name(self, request: str) -> tuple[dict[str, Any] | None, list[str]]:
        """Parse "move the cube by 0 0 1" when exactly one prim's name is "cube"; the scene is only read.

        Returns (parsed command marked "resolved_from_name", candidate paths). Verbs that create or search never
        resolve, and an unparseable rewrite still returns the candidates.
        """
        text = self._strip_politeness(request.strip(), keep_case=True)
        named = self._named_target(text)
        if named is None:
            return None, []
        verb, words, start, end = named
        path, candidates = self._unique_prim_named(words)
        if path is None or verb in _NAME_CREATE_VERBS:
            return None, candidates
        parsed = self._parse_scene_command(f"{text[:start]}{path}{text[end:]}")
        if parsed is None or parsed["action"] not in _NAME_ACTIONS:
            return None, candidates
        parsed["resolved_from_name"] = " ".join(words)
        return parsed, candidates

    @staticmethod
    def _named_target(text: str) -> tuple[str, list[str], int, int] | None:
        """(verb, name words, start, end) of the object named right after a scene verb: 'the cube' in 'move the cube up'."""
        tokens = list(re.finditer(r"[^\s,]+", text))
        if len(tokens) < 2 or not _SCENE_VERB.match(tokens[0].group().lower()):
            return None
        words: list[str] = []
        start = end = None
        for token in tokens[1:]:
            word = token.group().lower().strip("?!.")
            if word.startswith("/"):
                return None
            if word in _TARGET_STOP_WORDS or re.match(r"[-+]?\.?\d", word):
                break
            start = token.start() if start is None else start
            end = token.end()
            if word not in _NAME_ARTICLES:
                words.append(word)
        if not words or start is None or end is None:
            return None
        return tokens[0].group().lower(), words, start, end

    def _unique_prim_named(self, words: list[str]) -> tuple[str | None, list[str]]:
        """Read-only: the single prim whose last path component equals the name (case-insensitive), and candidates."""
        try:
            prims = self._scene.find_prims(words[-1])["prims"]
        except Exception:  # noqa: BLE001 - name lookup is a convenience; failing it changes nothing
            return None, []
        wanted = _name_key("".join(words))
        exact = [prim["path"] for prim in prims if _name_key(prim["path"].rsplit("/", 1)[-1]) == wanted]
        others = [prim["path"] for prim in prims if prim["path"] not in exact]
        return (exact[0] if len(exact) == 1 else None), (exact + others)[:10]

    def _single_selected_path(self) -> str | None:
        selection = self._scene.current_selection()
        if len(selection) == 1:
            return selection[0]
        return None

    @staticmethod
    def _extract_create_parent(text: str) -> str | None:
        match = re.search(r"\b(?:under|in|into|inside|at)\s+(.+)$", text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    @staticmethod
    def _normalize_parent_path(text: str) -> str:
        cleaned = text.strip().strip(".")
        lowered = cleaned.lower()
        if cleaned.startswith("/"):
            return cleaned
        if lowered in {"world", "the world"}:
            return "/World"
        if lowered in {"environment", "the environment"}:
            return "/Environment"
        if lowered in {"props", "the props"}:
            return "/World/Props"
        if lowered in {"root", "the root"}:
            return "/"
        return f"/{cleaned.lstrip('/')}"

    @staticmethod
    def _extract_usd_path(text: str) -> str | None:
        match = re.search(r"(/(?:[\w\-.]+/)*[\w\-.]+)", text)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _extract_paths(text: str) -> list[str]:
        paths = re.findall(r"(/(?:[\w\-.]+/)*[\w\-.]+)", text)
        if paths:
            return paths
        parts = [part.strip() for part in re.split(r"[,\s]+", text) if part.strip()]
        return [part for part in parts if part.startswith("/")]

    @staticmethod
    def _split_query_and_scope(text: str) -> tuple[str, str | None]:
        scope_match = re.search(r"\b(?:in|under|inside|within)\s+(.+)$", text, re.IGNORECASE)
        if scope_match:
            query = text[: scope_match.start()].strip(" ,.")
            scope_path = OmniverseRouter._extract_usd_path(scope_match.group(1))
            if scope_path is not None:
                return query or scope_path, scope_path
        return text.strip(" ,."), None

    @staticmethod
    def _split_create_position(text: str) -> tuple[str, tuple[float, float, float] | None]:
        """'add a sphere at 0 0 2 under /World' -> ('add a sphere under /World', (0, 0, 2))."""
        found = re.search(rf"\s+at\s+\(?\s*{_VECTOR}\s*\)?(?=\s|$)", text, re.IGNORECASE)
        if not found:
            return text, None
        position = (float(found.group(1)), float(found.group(2)), float(found.group(3)))
        return re.sub(r"\s+", " ", f"{text[: found.start()]} {text[found.end():]}").strip(), position

    @staticmethod
    def _guess_type_name(text: str) -> str:
        # Only the head noun names the type: "a lamp above the box" is not a Cube, "a table next to the sphere" not a
        # Sphere. The reference object after a spatial or other preposition is dropped first.
        text = re.split(
            r"\s+(?:above|over|below|beneath|under|on|onto|near|beside|behind|next\s+to|by|like|for|with)\s+",
            text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]
        lower = text.lower()
        if "light" in lower:
            kind = next((word for word in re.findall(r"[a-z]+", lower) if word in _LIGHT_KINDS), None)
            return _LIGHT_TYPE_NAMES[_LIGHT_KINDS[kind]] if kind else "DistantLight"
        if "camera" in lower:
            return "Camera"
        if "cube" in lower:
            return "Cube"
        shape = next((type_name for word, type_name in _SHAPE_WORDS if re.search(rf"\b{word}s?\b", lower)), None)
        if shape is not None:
            return shape
        if "mesh" in lower:
            return "Mesh"
        if "scope" in lower:
            return "Scope"
        return "Xform"

    @staticmethod
    def _guess_base_name(text: str, type_name: str) -> str:
        lowered = text.lower()
        cleaned = re.sub(r"\b(a|an|the|new|newly|simple|small|big|large|red|blue|green|yellow)\b", "", lowered)
        cleaned = re.sub(r"\b(cube|light|camera|mesh|scope|group|xform|prim|object|thing)\b", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,_-")
        if cleaned:
            parts = [part.capitalize() for part in cleaned.split()]
            return "".join(parts)
        return type_name

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip().lower())


# --- tier 5 learning helpers: which tool a sentence maps to, and the helpers that need no server state ------------

_HELPER_TOOLS = {
    "python_show": "python_show",
    "python_topics": "python_topics",
    "python_exercise": "python_practice_get",
    "python_practice_list": "python_practice_list",
    "explain_error": "explain_error",
    "assignment_check": "assignment_check",
    "assignment_list": "assignment_list",
    "scene_explain": "scene_explain",
    "class_ask": "class_ask",
    "class_sessions": "class_sessions",
}


def _learning_helper_call(spec: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """(tool name, keyword arguments) for one nl_tier5 spec; these are the arguments the helper callbacks take."""
    kind = spec["kind"]
    tool = _HELPER_TOOLS[kind]
    if kind == "python_show":
        return tool, {"action": spec["action"], "args": dict(spec.get("args") or {}), "context": spec["context"]}
    if kind in ("python_exercise", "python_practice_list"):
        return tool, {"level": spec.get("level")}
    if kind == "explain_error":
        return tool, {"text": spec.get("text") or ""}
    if kind == "assignment_check":
        return tool, {"scene_file": spec.get("scene_file")}
    if kind == "scene_explain":
        return tool, {"focus_path": spec.get("focus_path")}
    if kind == "class_ask":
        return tool, {"question": spec["question"], "session": spec.get("session")}
    return tool, {}


def _pure_learning_helper(kind: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """The helpers that need no live scene, progress store or class notes; None for the others."""
    if kind == "python_show":
        from .python_teacher import python_for

        return python_for(arguments["action"], arguments["args"], context=arguments["context"])
    if kind == "python_topics":
        from .python_teacher import list_python_topics

        return list_python_topics()
    if kind == "python_practice_list":
        from .exam.python_practice import list_exercises

        return list_exercises(arguments.get("level"))
    if kind == "python_exercise":
        from .exam.python_practice import get_exercise, list_exercises

        listing = list_exercises(arguments.get("level"))
        if not listing.get("ok") or not listing.get("exercises"):
            return listing
        return get_exercise(listing["exercises"][0]["id"])
    if kind == "explain_error":
        from .error_explainer import explain_error

        return explain_error(arguments.get("text") or "")
    if kind == "assignment_list":
        from .assignments import list_assignments

        assignments = list_assignments()
        return {"ok": True, "count": len(assignments), "assignments": assignments}
    return None
