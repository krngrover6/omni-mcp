from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import os
import re
from typing import Any, Protocol
from urllib.parse import unquote

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
_USD_SUFFIXES = (".usd", ".usda", ".usdc", ".usdz")

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
    ) -> None:
        self._scene = scene_service
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
        if _is_prim_count_question(core):
            return RoutingDecision(RouteTarget.SCENE, 0.95, "Prim count question about the open stage", normalized)
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
                    "or 'find cube'."
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
            return {"ok": False, "target": "scene", "message": parsed["message"], "hint": parsed["hint"]}
        if action == "set_attribute":
            return self._attribute_not_supported(parsed)
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
            return self._wrap_operation_result("scene", result)
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
            ],
            "learning": [
                "practice transforms with me",
                "quiz me on schemas",
                "what should I do next?",
                "translate: make this bigger",
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
        lowered = request.lower()
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
        transform = self._parse_transform(text, selected_path)
        if transform is not None:
            return transform
        # "the/this/my ..." names something that already exists ("make the light brighter", "add my light to the
        # scene"), so it never creates a prim.
        create_natural = re.match(
            r"^(?:make\s+(?:a|an|another|new)\s+|(?:create|add|spawn|place)\s+(?:a\s+|an\s+|another\s+|new\s+)?"
            r"(?!(?:the|this|that|these|those|it|its|my|our|your|selected)\b))(.+)$",
            text,
            re.IGNORECASE,
        )
        if create_natural:
            description = re.split(r"\s+(?:under|in|into|inside|at)\s+", create_natural.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
            type_name = self._guess_type_name(description)
            base_name = self._guess_base_name(description, type_name)
            parent_text = self._extract_create_parent(text)
            parent_path = (
                self._extract_usd_path(parent_text or "")
                or (selected_path if parent_text and _PRONOUN.search(parent_text.lower()) else None)
                or self._normalize_parent_path(parent_text or "/World")
            )
            return {"action": "create", "path": self._scene.unique_child_path(parent_path, base_name), "type_name": type_name}

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
            material_path = self._extract_usd_path(material_text)
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
        """'set intensity of X to 500' is an attribute edit (not supported yet); 'set material of X to /M' binds."""
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
        material_path = self._extract_usd_path(value)
        if attribute.lower() in {"material", "material binding"} and material_path and path:
            parsed: dict[str, Any] = {"action": "material", "path": path, "material_path": material_path}
            if named:
                parsed["resolved_from_name"] = named
            return parsed
        return {"action": "set_attribute", "path": path, "attribute": attribute, "value": value, "candidates": candidates}

    def _fragment_target(self, fragment: str, selected_path: str | None) -> str | None:
        """The prim a phrase points at: a path inside it, or the selection when the whole phrase is this/that/it/
        selected (optionally with 'the' and one noun). Any other phrase returns None, so name resolution only previews."""
        return self._extract_usd_path(fragment) or (
            selected_path if _SELECTION_REF.fullmatch(fragment.strip()) else None
        )

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
    def _guess_type_name(text: str) -> str:
        lower = text.lower()
        if "light" in lower:
            if "distant" in lower:
                return "DistantLight"
            if "sphere" in lower:
                return "SphereLight"
            return "DistantLight"
        if "camera" in lower:
            return "Camera"
        if "cube" in lower:
            return "Cube"
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
