"""Plain-English phrases for assets, physics, screenshots, simulation controls and the exam dashboard.

Pure string parsing for router.py: nothing here reads the scene, the asset pack or any file. Every parser takes
the request with politeness already stripped ("can you ... please") and returns a small spec dict, or None when the
text is not that kind of request. Prim targets come back as raw text; the router resolves them (path, selection or
name) and decides whether the request previews or applies.
"""

from __future__ import annotations

import re
from typing import Any

_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_COUNT_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "twenty": 20, "thirty": 30, "sixty": 60, "hundred": 100,
}
_COUNT = r"(?:\d{1,6}|" + "|".join(sorted(_COUNT_WORDS, key=len, reverse=True)) + r")"
_SIM_WORD = r"(?:simulation|sim|physics)"
_SIM = rf"(?:the\s+)?(?:physics\s+)?{_SIM_WORD}"

# --- simulation controls (simulation state, never authored scene data) ------------------------------------------
_SIM_PLAY = re.compile(rf"^(?:play|start|run|resume|unpause|continue)\s+{_SIM}(?:\s+again)?$")
_SIM_PAUSE = re.compile(rf"^(?:pause|freeze)\s+{_SIM}$")
_SIM_STOP = re.compile(rf"^(?:stop|reset|end)\s+{_SIM}$")
_SIM_STATUS = re.compile(
    rf"^(?:{_SIM_WORD}\s+(?:status|state)"
    rf"|is\s+{_SIM}\s+(?:running|playing|paused|stopped)"
    rf"|(?:show|check|get|what(?:'s|\s+is))\s+(?:me\s+)?{_SIM}\s+(?:status|state))$"
)
_SIM_STEP = re.compile(
    rf"^(?:step|advance)(?P<sim>\s+{_SIM})?(?:\s+(?:forward|ahead))?"
    rf"(?:\s+(?:by\s+)?(?P<count>{_COUNT})\s+frames?)?(?:\s+(?:forward|ahead))?$"
)

# --- viewport screenshots (write a PNG, never change the scene) -------------------------------------------------
_SHOT_NOUN = r"(?:screenshot|screen\s*shot|screen\s*grab|screengrab|snapshot|picture|image|capture)"
_SHOT_OF = r"(?:\s+of\s+(?:the\s+|my\s+)?(?:viewport|scene|screen|view|stage|current\s+view))?"
_SHOT_SIZE = r"(?:\s+(?:at|in|of)\s+(?P<width>\d{1,5})\s*(?:x|by|\*)\s*(?P<height>\d{1,5})(?:\s*(?:px|pixels))?)?"
_SCREENSHOT = re.compile(
    rf"^(?:(?:take|grab|capture|make|save|get)\s+(?:a\s+|an\s+|the\s+|me\s+a\s+)?(?:viewport\s+)?{_SHOT_NOUN}{_SHOT_OF}"
    rf"|screenshot(?:\s+(?:the\s+)?(?:viewport|scene))?"
    rf"|capture\s+(?:the\s+)?(?:viewport|screen|view))"
    rf"{_SHOT_SIZE}$"
)
_SCREENSHOT_STATUS = re.compile(
    r"^(?:is\s+(?:the|my)\s+(?:screenshot|capture)\s+(?:ready|done|saved|finished|written)"
    r"|(?:screenshot|capture)\s+status|(?:check|show)\s+(?:the\s+|my\s+)?(?:screenshot|capture)(?:\s+status)?)$"
)

# --- physics ---------------------------------------------------------------------------------------------------
_MASS = rf"(?:\s+(?:with|and)\s+(?:a\s+)?mass\s+(?:of\s+)?(?P<mass>{_NUMBER})\s*(?:kg|kilograms?)?)?"
_RIGID_NOUN = r"(?:rigid\s*bod(?:y|ies)|rigidbody|physics\s+(?:object|body)|dynamic\s+body)"
_RIGID_MAKE = re.compile(
    rf"^(?:make|turn|set|convert)\s+(?P<target>.+?)\s+(?:into\s+|to\s+|as\s+)?(?:a\s+|an\s+)?"
    rf"(?P<kind>kinematic\s+|dynamic\s+|physics\s+)?(?:{_RIGID_NOUN}|rigid){_MASS}$"
)
_RIGID_ADD = re.compile(
    rf"^(?:add|apply|give|enable)\s+(?:a\s+|an\s+)?(?P<kind>kinematic\s+)?(?:{_RIGID_NOUN}|rigid\s*body\s+physics|physics)"
    rf"\s+(?:to|on|for)\s+(?P<target>.+?){_MASS}$"
)
_APPROXIMATIONS = {
    "convex hull": "convexHull", "convexhull": "convexHull", "hull": "convexHull",
    "convex decomposition": "convexDecomposition", "convexdecomposition": "convexDecomposition",
    "decomposition": "convexDecomposition",
    "bounding box": "boundingCube", "bounding cube": "boundingCube", "box": "boundingCube", "cube": "boundingCube",
    "boundingcube": "boundingCube",
    "bounding sphere": "boundingSphere", "sphere": "boundingSphere", "boundingsphere": "boundingSphere",
    "mesh simplification": "meshSimplification", "simplified mesh": "meshSimplification",
    "meshsimplification": "meshSimplification",
    "triangle mesh": "none", "exact mesh": "none", "exact shape": "none", "none": "none", "no approximation": "none",
}
_COLLIDER_NOUN = r"(?:collision|collisions|collider|colliders|collision\s+shapes?|physics\s+collision)"
_COLLIDER_ADD = re.compile(
    rf"^(?:add|apply|give|enable|put)\s+(?:a\s+|an\s+)?{_COLLIDER_NOUN}\s+(?:to|on|for)\s+(?P<target>.+?)"
    r"(?:\s+(?:with|using|as)\s+(?:a\s+|an\s+)?(?P<approx>[\w\s-]+?)(?:\s+approximation)?)?$"
)
_COLLIDER_MAKE = re.compile(r"^make\s+(?P<target>.+?)\s+(?:collide|collidable|solid|a\s+collider)$")
_REMOVE_PHYSICS = re.compile(
    r"^(?:remove|delete|clear|strip|take)\s+(?:the\s+|all\s+)?(?:physics(?:\s+apis?)?|rigid\s*bod(?:y|ies)|"
    r"collisions?|colliders?)\s+(?:off\s+|away\s+)?(?:from|on|of)\s+(?P<target>.+?)"
    r"(?P<all>\s+and\s+(?:all\s+)?(?:its\s+|their\s+)?(?:children|descendants|everything\s+below))?$"
)
_GRAVITY = rf"(?P<gravity>{_NUMBER}(?:[,\s]+{_NUMBER}[,\s]+{_NUMBER})?)"
_PHYSICS_SCENE = re.compile(
    rf"^(?:add|create|make|set\s+up)\s+(?:a\s+|an\s+|the\s+)?physics\s*scene(?:\s+(?:at\s+)?(?P<path>/\S+))?"
    rf"(?:\s+with\s+(?:a\s+)?gravity\s+(?:of\s+)?{_GRAVITY}(?:\s*m/s\^?2)?)?$"
)
_PHYSICS_REPORT = re.compile(
    r"^(?:(?:show|give|get|run)\s+(?:me\s+)?(?:the\s+|a\s+|my\s+)?)?physics\s+(?:report|setup|summary|overview|status|check)"
    r"(?:\s+(?:for|of|on|under)\s+(?P<target>.+))?$"
    r"|^(?:check|inspect|report|show)\s+(?:the\s+|my\s+)?physics(?:\s+(?:for|of|on|under)\s+(?P<target2>.+))?$"
    r"|^(?:what|which\s+prims?)\s+(?:has|have)\s+physics$"
)

# --- assets ----------------------------------------------------------------------------------------------------
_LIBRARY = r"(?:\s+(?:in|from)\s+(?:the\s+|my\s+)?(?:local\s+)?(?:asset\s+(?:library|pack)|library|isaac(?:\s+sim)?\s+assets?))"
_ASSET_FIND = (
    re.compile(
        rf"^(?:find|search\s+for|look\s+for|locate|search)\s+(?:me\s+)?(?:an?|some|any)\s+(?P<query>.+?)"
        rf"(?:\s+(?:assets?|models?|usd\s+files?))?{_LIBRARY}?$"
    ),
    re.compile(rf"^(?:find|search(?:\s+for)?|look\s+for|list)\s+(?P<query>.+?)\s+(?:assets?|models?){_LIBRARY}?$"),
    re.compile(rf"^(?:find|search(?:\s+for)?|look\s+for)\s+(?P<query>.+?){_LIBRARY}$"),
    re.compile(r"^search\s+(?:the\s+)?(?:assets?|asset\s+(?:library|pack)|library)\s+for\s+(?P<query>.+)$"),
)
_ASSET_ADD = re.compile(
    r"^(?:add|place|spawn|insert|import|load|drop|bring\s+in)\s+(?:an?|another|some|one)\s+(?P<desc>.+)$"
)
# Head nouns that stay plain prims (router's create), lights, cameras, materials or physics, never an asset search.
_NOT_ASSET_WORDS = frozenset(
    "cube cubes sphere spheres ball balls box boxes cone cones cylinder cylinders capsule capsules plane planes mesh "
    "meshes scope scopes xform xforms group groups prim prims empty null transform node folder light lights lamp "
    "camera cameras cam material materials shader physics collider colliders collision rigid body scene stage layer "
    "reference references variant "
    # Words that name a light on their own ('add a sun' is a distant light, 'add a sky' a dome light).
    "sun sunlight spotlight spotlights skylight sky dome hdri".split()
)
_SPATIAL = re.compile(
    r"\s(?:above|over|below|beneath|near|beside|behind|next\s+to|on|onto|by|like|for|with|aimed|pointing|looking|"
    r"named|called|as)\s"
)
_POSITION = re.compile(rf"\s+at\s+\(?\s*(?P<x>{_NUMBER})[,\s]+(?P<y>{_NUMBER})[,\s]+(?P<z>{_NUMBER})\s*\)?(?=\s|$)")
_PARENT = re.compile(r"\s+(?:under|in|into|inside)\s+(?P<parent>/\S+)(?=\s|$)")
_ASSET_FILLER = frozenset("a an the some new another my our of to from in into".split())
# Words that make 'find a ...' a docs or learning request rather than an asset search.
_NOT_ASSET_SEARCH_WORDS = frozenset(
    "tutorial tutorials doc docs documentation example examples api lesson lessons guide guides way how explanation "
    "video videos course quiz question questions answer bug error fix solution".split()
)

# --- exam dashboard --------------------------------------------------------------------------------------------
_DASHBOARD = re.compile(
    r"^(?:(?:show|open|give|get|build|make|update|refresh|create)\s+(?:me\s+)?(?:my\s+|the\s+|a\s+)?)?"
    r"(?:(?:exam|progress|study|ncp-?ousd|learning)\s+)?(?:progress\s+)?dashboard(?:\s+page)?$"
    r"|^(?:my|the)\s+(?:(?:exam|progress|study)\s+)?dashboard$"
)


def _count(text: str | None) -> int | None:
    if text is None:
        return None
    return int(text) if text.isdigit() else _COUNT_WORDS.get(text)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().rstrip("?.! ")).strip()


def parse_simulation(text: str) -> dict[str, Any] | None:
    """{"op": play|pause|stop|status|step, "frames": n} for an explicit simulation phrase, else None."""
    core = _clean(text).lower()
    if _SIM_PLAY.match(core):
        return {"op": "play"}
    if _SIM_PAUSE.match(core):
        return {"op": "pause"}
    if _SIM_STOP.match(core):
        return {"op": "stop"}
    if _SIM_STATUS.match(core):
        return {"op": "status"}
    step = _SIM_STEP.match(core)
    if step and (step.group("sim") or step.group("count")):
        return {"op": "step", "frames": _count(step.group("count")) or 1}
    return None


def parse_screenshot(text: str) -> dict[str, Any] | None:
    """{"op": "capture", width, height} or {"op": "status"} for a screenshot phrase, else None."""
    core = _clean(text).lower()
    if _SCREENSHOT_STATUS.match(core):
        return {"op": "status"}
    match = _SCREENSHOT.match(core)
    if not match:
        return None
    width, height = match.group("width"), match.group("height")
    return {"op": "capture", "width": int(width) if width else None, "height": int(height) if height else None}


def approximation_name(text: str | None) -> str | None:
    """The UsdPhysics approximation token for words such as 'convex hull'; None when not recognized."""
    if text is None:
        return None
    return _APPROXIMATIONS.get(re.sub(r"\s+", " ", text.strip().lower()))


def parse_physics(text: str) -> dict[str, Any] | None:
    """A physics request: rigid_body, collider, remove_physics, physics_scene or physics_report; else None.

    Targets keep their original case (prim paths are case-sensitive).
    """
    raw = _clean(text)
    core = raw.lower()

    def target(match: re.Match[str], name: str = "target") -> str | None:
        start, end = match.span(name)
        return raw[start:end].strip() if start >= 0 else None

    match = _PHYSICS_SCENE.match(core)
    if match:
        gravity_text = match.group("gravity")
        gravity: Any = None
        if gravity_text:
            values = [float(value) for value in re.findall(_NUMBER, gravity_text)]
            gravity = values[0] if len(values) == 1 else tuple(values)
        return {"kind": "physics_scene", "path": target(match, "path"), "gravity": gravity}
    match = _PHYSICS_REPORT.match(core)
    if match:
        found = target(match, "target") if match.group("target") else (target(match, "target2") if match.group("target2") else None)
        return {"kind": "physics_report", "target": found}
    match = _REMOVE_PHYSICS.match(core)
    if match:
        return {"kind": "remove_physics", "target": target(match), "include_descendants": bool(match.group("all"))}
    for pattern in (_COLLIDER_ADD, _COLLIDER_MAKE):
        match = pattern.match(core)
        if match:
            approx_text = match.groupdict().get("approx")
            spec: dict[str, Any] = {"kind": "collider", "target": target(match), "approximation": None}
            if approx_text:
                spec["approximation"] = approximation_name(approx_text)
                spec["approximation_text"] = approx_text.strip()
            return spec
    for pattern in (_RIGID_MAKE, _RIGID_ADD):
        match = pattern.match(core)
        if match:
            kind = (match.group("kind") or "").strip()
            mass = match.group("mass")
            return {
                "kind": "rigid_body",
                "target": target(match),
                "kinematic": kind == "kinematic",
                "mass": float(mass) if mass else None,
            }
    return None


def parse_asset_find(text: str) -> dict[str, Any] | None:
    """{"query": words} for 'find a carter robot', 'search the asset library for shelves'; else None.

    Bare 'find cube' (no article, no 'asset'/'library') stays the router's scene search for prims, and so does
    'find a mesh' / 'find a prim named Cube' / 'find a light': a plain prim, light, camera, material or physics word
    is an asset search only when the request says assets, models or the library ('find cube assets').
    """
    core = _clean(text).lower()
    if "/" in core:
        return None
    for index, pattern in enumerate(_ASSET_FIND):
        match = pattern.match(core)
        if match:
            query = _asset_query(match.group("query"))
            words = set(query.split())
            if not query or words & _NOT_ASSET_SEARCH_WORDS:
                return None  # 'find a tutorial on lights' is a learning request, not an asset search
            # Only the first pattern can match without naming assets/models/the library (an empty tail).
            explicit = index > 0 or match.end("query") < len(core)
            if not explicit and words & _NOT_ASSET_WORDS:
                return None  # 'find a mesh': the router's scene search for prims, as before
            return {"query": query}
    return None


def parse_asset_add(text: str) -> dict[str, Any] | None:
    """'add a franka robot [at x y z] [under /World/Robots]' -> {query, translation, parent}; else None.

    None when the head noun is a plain prim, light, camera, material or physics word, when a spatial phrase
    ('above the box', 'named X') follows, or when a prim path names the new prim: the router's create handles those.
    """
    raw = _clean(text)
    match = _ASSET_ADD.match(raw.lower())
    if not match:
        return None
    description = raw[match.start("desc"):]
    translation = None
    found = _POSITION.search(" " + description)
    if found:
        translation = (float(found.group("x")), float(found.group("y")), float(found.group("z")))
        description = (" " + description)[: found.start()] + (" " + description)[found.end():]
    parent = None
    found = _PARENT.search(" " + description)
    if found:
        parent = found.group("parent")
        description = (" " + description)[: found.start()] + (" " + description)[found.end():]
    description = re.sub(r"\s+", " ", description).strip()
    lowered = f" {description.lower()} "
    if not description or "/" in description or _SPATIAL.search(lowered) or re.search(rf"\s{_NUMBER}\s", lowered):
        return None
    for phrase in ("to the scene", "to the stage", "to my scene", "in the scene", "into the scene"):
        lowered = lowered.replace(f" {phrase} ", " ")
    words = [word for word in re.findall(r"[a-z0-9][a-z0-9_-]*", lowered) if word not in _ASSET_FILLER]
    if not words or any(word in _NOT_ASSET_WORDS for word in words):
        return None
    return {"query": " ".join(words), "translation": translation, "parent": parent}


def is_dashboard_request(text: str) -> bool:
    """'show my dashboard', 'open the progress dashboard', 'dashboard'."""
    return bool(_DASHBOARD.match(_clean(text).lower()))


def is_tier4_scene_request(text: str) -> bool:
    """Whether the text is one of this module's scene phrases (the router then routes it to the scene)."""
    return any(
        parser(text) is not None for parser in (parse_simulation, parse_screenshot, parse_physics, parse_asset_find)
    )


def _asset_query(text: str) -> str:
    words = [word for word in re.findall(r"[a-z0-9][a-z0-9_-]*", text.lower()) if word not in _ASSET_FILLER]
    return " ".join(words)
