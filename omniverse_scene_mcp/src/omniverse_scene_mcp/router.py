from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any, Protocol

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

# Questions about the open stage keep their scene-aware routing instead of going to a docs backend.
_LIVE_SCENE = re.compile(r"(?:^|\s)/\w|\bselect(?:ed|ion)\b|\bmy\b|\b(?:this|current) (?:scene|stage)\b|\bhow many\b")

# Whole-word lesson topics for the fallback, so "remove" is not "move" and "lightweight" is not "light".
_FALLBACK_TOPICS = (
    ("schemas", re.compile(r"\b(?:schemas?|subtypes?)\b")),
    ("prims", re.compile(r"\b(?:prims?|stages?|scene graph|hierarchy)\b")),
    ("selection", re.compile(r"\b(?:select(?:ed|ion)?|focus|highlight)\b")),
    ("transforms", re.compile(r"\b(?:move|moving|rotat(?:e|ion|ing)|scal(?:e|ing)|transforms?)\b")),
    ("materials", re.compile(r"\b(?:materials?|shade|shaders?)\b")),
    ("lights", re.compile(r"\b(?:lights?|lighting)\b")),
)


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
)


def _detect_named_knowledge_domain(request: str) -> RouteTarget:
    text = request.replace("usd composer", "composer")
    return next((target for target, pattern in _NAMED_DOMAINS if pattern.search(text)), RouteTarget.UNKNOWN)


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
        # Checked before learning topics, which also match words such as "move", "select" and "prim".
        if self._looks_like_targeted_scene_command(normalized):
            return RoutingDecision(RouteTarget.SCENE, 0.97, "Scene command verb with a target detected", normalized)
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
        if parsed is None:
            return {
                "ok": False,
                "message": "Scene routing selected, but I could not parse the command.",
                "hint": "Try phrases like 'inspect /World', 'create /World/NewPrim', 'select /World/Cube', 'rename /World/Cube to HeroCube', or 'find cube'.",
            }

        action = parsed["action"]
        if action == "summary":
            return {"ok": True, "target": "scene", "result": self._scene.scene_summary()}
        if action == "ping":
            return {"ok": True, "target": "scene", "result": self._scene.ping()}
        if action == "select":
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

    def _dispatch_learning(self, request: str) -> dict[str, Any]:
        topic = self._detect_learning_topic(request) or "schemas"
        style = self._detect_learning_style(request)
        self._record_learning_event("request", topic=topic, style=style, request=request)
        guide = self._build_learning_guide(topic)
        if style == "practice":
            return self.start_practice(topic)
        if style == "quiz":
            return {"ok": True, "target": "learn", "topic": topic, "style": style, "quiz": self._build_scene_aware_quiz(topic)}
        if style == "study":
            return self._build_study_mode(topic)
        if style == "translate":
            return self.translate_command(request)
        if style == "cheatsheet":
            return {
                "ok": True,
                "target": "learn",
                "topic": topic,
                "style": style,
                "title": guide["title"],
                "cheatsheet": guide["cheatsheet"],
                "scene_context": self.scene_summary(),
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
            "scene_context": self.scene_summary(),
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
            "scene_context": self.scene_summary(),
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
        scene = self.scene_summary()
        commands = {
            "scene": [
                "inspect /World/Props/Cube",
                "children /Environment",
                "select /Environment/Cube",
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
            "scene_context": self.scene_summary(),
        }

    def _record_learning_event(self, event: str, **details: Any) -> None:
        entry = {
            "event": event,
            "details": details,
            "scene": self.scene_summary(),
        }
        self._learning_history.append(entry)
        self._learning_history = self._learning_history[-100:]

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
        if any(word in lower for word in ("bigger", "scale up", "10x", "ten times", "larger")):
            return {"kind": "scene_action", "action": "move", "arguments": {"scale": [10.0, 10.0, 10.0]}, "message": "Try scaling the selected prim up."}
        if any(word in lower for word in ("red", "paint", "color it red")):
            return {"kind": "scene_action", "action": "material", "arguments": {"material_path": "/World/Looks/RedMaterial"}, "message": "Try binding the red material."}
        if any(word in lower for word in ("up", "higher", "raise")):
            return {"kind": "scene_action", "action": "move", "arguments": {"translation": [0.0, 2.0, 0.0]}, "message": "Try moving the selected prim upward."}
        if any(word in lower for word in ("rename", "call it", "name it")):
            return {"kind": "scene_action", "action": "rename", "arguments": {"new_name": "PracticeTarget"}, "message": "Try renaming the selected prim."}
        if any(word in lower for word in ("under environment", "move under", "parent it", "nest it")):
            return {"kind": "scene_action", "action": "reparent", "arguments": {"new_parent": "/Environment"}, "message": "Try reparenting the selected prim under /Environment."}
        return {
            "kind": "explain",
            "message": "I could not map that to one exact scene command yet. Try saying it with the prim path or selection.",
            "examples": ["move this 0 2 0", "make it red", "rename this to HeroCube"],
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
        """An imperative scene verb aimed at a path, the selection or numbers, or 'add a <thing>'."""
        prefix = r"^(?:scene\s+)?(?:can you\s+)?(?:please\s+)?"
        if re.match(prefix + r"(?:add|create|spawn|place)\s+(?:a|an)\s+\w", request):
            return True
        verbs = r"(?:inspect|children|delete|remove|duplicate|reparent|move|shift|nudge|material|select|focus|highlight|rename|save)"
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
        if any(word in request for word in usd_words) or _USD_WORD.search(request):
            return RouteTarget.USD
        return RouteTarget.UNKNOWN

    @staticmethod
    def _detect_learning_topic(request: str) -> str | None:
        lowered = request.lower()
        if any(word in lowered for word in ("schema", "schemas", "applied schema", "typed schema", "subtype")):
            return "schemas"
        if any(word in lowered for word in ("prim", "prims", "stage", "scene graph", "hierarchy")):
            return "prims"
        if any(word in lowered for word in ("select", "selection", "focus", "highlight")):
            return "selection"
        if any(word in lowered for word in ("move", "rotate", "scale", "transform")):
            return "transforms"
        if any(word in lowered for word in ("material", "materials", "shade", "shader")):
            return "materials"
        if any(word in lowered for word in ("light", "lighting", "dome light", "distant light")):
            return "lights"
        if any(word in lowered for word in ("workflow", "cheat sheet", "cheatsheet", "quickstart", "commands", "how do i start")):
            return "workflow"
        return None

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
                    "Open the stage tree and compare /World, /Environment, and a leaf prim like a Cube.",
                    "Use scene_children on a parent prim to see the hierarchy.",
                ],
                "cheatsheet": [
                    "Path = the prim's address.",
                    "Parent = where it lives in the hierarchy.",
                    "Children = what it contains.",
                ],
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
                    "select /World/Props/Cube",
                    "inspect this",
                    "move it 1 2 3",
                ],
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
                    "move /World/Props/Cube 1 2 3",
                    "make it 10x bigger",
                    "rotate it 90 degrees",
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
                    "material /World/Looks/RedMaterial to /Environment/Cube",
                    "make it red",
                    "inspect /Environment/Cube",
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
                    "Inspect the Environment prim and note the Sky and DistantLight entries.",
                    "Compare the scene with stage lights on and off if your workflow supports it.",
                ],
                "cheatsheet": [
                    "DomeLight",
                    "DistantLight",
                    "AreaLight",
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
                    "inspect /Environment/Cube",
                    "move /Environment/Cube 0 2 0",
                    "rename /Environment/Cube to HeroCube",
                ],
                "cheatsheet": [
                    "inspect",
                    "children",
                    "select",
                    "rename",
                    "find",
                    "move",
                    "material",
                ],
            },
        }
        return guides.get(topic, guides["schemas"])

    @staticmethod
    def _build_quiz(topic: str) -> dict[str, Any]:
        quizzes: dict[str, dict[str, Any]] = {
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
        return quizzes.get(topic, quizzes["schemas"])

    def _build_scene_aware_quiz(self, topic: str) -> dict[str, Any]:
        scene = self.scene_summary()
        selection = scene.get("selection", [])
        selected_path = selection[0] if len(selection) == 1 else None
        selected_prim = self._scene.inspect_prim(selected_path) if selected_path else None
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
            return {
                "topic": topic,
                "scene_context": scene,
                "selected_prim": selected_prim,
                "questions": questions,
                "answers": answers,
            }

        base_quiz = self._build_quiz(topic)
        base_quiz["scene_context"] = scene
        base_quiz["hint"] = "Select a prim first if you want a quiz that uses the live scene."
        return base_quiz

    def _build_study_mode(self, topic: str | None = None) -> dict[str, Any]:
        scene = self.scene_summary()
        selection = scene.get("selection", [])
        selected_path = selection[0] if len(selection) == 1 else None
        selected_prim = self._scene.inspect_prim(selected_path) if selected_path else None
        topic = topic or self._infer_study_topic(scene, selected_prim)
        suggestion = self._study_suggestion(topic, scene, selected_prim)
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

    def start_practice(self, topic: str = "schemas") -> dict[str, Any]:
        session = self._make_practice_session(topic)
        self._practice_session = session
        self._record_learning_event("practice_start", topic=topic, session_id=session["session_id"])
        self._learning_progress["topics"][topic] = self._learning_progress["topics"].get(topic, 0) + 1
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
            "ok": True,
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
        scene = self.scene_summary()
        current_selection = scene.get("selection", [])
        selected = current_selection[0] if len(current_selection) == 1 else None
        default_target = selected or ("/Environment/Cube" if topic in {"transforms", "materials", "selection"} else "/World")
        templates = {
            "schemas": {
                "title": "Interactive schema drill",
                "steps": [
                    "Inspect a prim and read its Type column.",
                    "Create or find a Cube and compare its type to the inspected prim.",
                    "Rename the Cube and confirm the type does not change.",
                ],
                "commands": [
                    f"inspect {selected or '/Environment/Cube'}",
                    "add a cube",
                    "rename this to PracticeCube",
                ],
            },
            "prims": {
                "title": "Interactive prim drill",
                "steps": [
                    "Inspect /World and /Environment.",
                    "List the children of /Environment.",
                    "Find a Cube and inspect it.",
                ],
                "commands": [
                    "inspect /World",
                    "children /Environment",
                    "find cube",
                ],
            },
            "selection": {
                "title": "Interactive selection drill",
                "steps": [
                    "Select one prim.",
                    "Use a natural-language command that says 'this' or 'it'.",
                    "Change the selection and repeat.",
                ],
                "commands": [
                    f"select {selected or '/Environment/Cube'}",
                    "inspect this",
                    "move it 0 2 0",
                ],
            },
            "transforms": {
                "title": "Interactive transform drill",
                "steps": [
                    "Inspect the target prim.",
                    "Move it upward by 2 units.",
                    "Make it 10x bigger.",
                ],
                "commands": [
                    f"inspect {default_target}",
                    f"move {default_target} 0 2 0",
                    f"scale {default_target} 10 10 10",
                ],
            },
            "materials": {
                "title": "Interactive material drill",
                "steps": [
                    "Inspect the target prim.",
                    "Bind a red material.",
                    "Re-inspect and verify the binding.",
                ],
                "commands": [
                    f"make {default_target} red",
                    f"inspect {default_target}",
                ],
            },
            "workflow": {
                "title": "Interactive USD workflow drill",
                "steps": [
                    "Inspect the scene.",
                    "Change one thing.",
                    "Verify the result.",
                    "Make a second change.",
                ],
                "commands": [
                    "scene summary",
                    "inspect /Environment/Cube",
                    "move /Environment/Cube 0 2 0",
                ],
            },
        }
        template = templates.get(topic, templates["schemas"])
        session = {
            "session_id": f"practice-{topic}",
            "topic": topic,
            "step_index": 0,
            "steps": template["steps"],
            "commands": template["commands"],
            "title": template["title"],
            "scene_context": scene,
        }
        return session

    def _practice_state_payload(self, session: dict[str, Any], started: bool) -> dict[str, Any]:
        index = session["step_index"]
        steps = session["steps"]
        commands = session["commands"]
        current_step = steps[index]
        current_command = commands[index] if index < len(commands) else None
        next_step = steps[index + 1] if index + 1 < len(steps) else None
        next_command = commands[index + 1] if index + 1 < len(commands) else None
        return {
            "ok": True,
            "target": "learn",
            "style": "practice",
            "status": "active",
            "started": started,
            "session_id": session["session_id"],
            "topic": session["topic"],
            "title": session["title"],
            "commands": steps,
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
    def _study_suggestion(topic: str, scene: dict[str, Any], selected_prim: dict[str, Any] | None) -> dict[str, Any]:
        if selected_prim is None:
            return {
                "recommendation": "Select a prim in the stage tree first.",
                "why": "The best next lesson depends on what is currently active in the scene.",
                "try_next": ["select /Environment/Cube", "inspect this"],
                "what_to_look_for": ["path", "type", "children", "material"],
            }
        if topic == "materials":
            return {
                "recommendation": f"Practice materials on {selected_prim['path']}.",
                "why": "It is a visible prim with no material bound yet, so the change will be obvious.",
                "try_next": [f"make {selected_prim['path']} red", f"inspect {selected_prim['path']}"],
                "what_to_look_for": ["material binding", "viewport color", "type staying the same"],
            }
        if topic == "prims":
            return {
                "recommendation": f"Study the hierarchy around {selected_prim['path']}.",
                "why": "Prims make more sense when you inspect parents and children together.",
                "try_next": [f"children {selected_prim['path']}", f"inspect {selected_prim['path']}"],
                "what_to_look_for": ["parent path", "children", "how the scene is organized"],
            }
        if topic == "transforms":
            return {
                "recommendation": f"Practice transforms on {selected_prim['path']}.",
                "why": "Transforms are easiest to learn on a visible prim with a clear before/after.",
                "try_next": [f"move {selected_prim['path']} 0 2 0", f"scale {selected_prim['path']} 10 10 10"],
                "what_to_look_for": ["position", "size", "orientation"],
            }
        return {
            "recommendation": f"Use the current prim {selected_prim['path']} as your study target.",
            "why": f"This prim is a good anchor for learning {topic}.",
            "try_next": [f"inspect {selected_prim['path']}", f"rename {selected_prim['path']} to PracticeTarget"],
            "what_to_look_for": ["type", "path", "selection", "what changes versus what stays the same"],
        }

    def _build_practice_session(self, topic: str) -> dict[str, Any]:
        scene = self.scene_summary()
        current_selection = scene.get("selection", [])
        selected = current_selection[0] if len(current_selection) == 1 else None
        default_target = selected or "/Environment/Cube" if topic in {"transforms", "materials", "selection"} else "/World"

        sessions: dict[str, dict[str, Any]] = {
            "schemas": {
                "title": "Practical schema drill",
                "goal": "Learn how schema/type shows up in the live stage.",
                "steps": [
                    "Select a prim in the stage tree.",
                    "Inspect it and look at the Type field.",
                    "Create a new Cube and compare its Type to the selected prim.",
                    "Rename the Cube and notice that the Type stays the same.",
                ],
                "commands": [
                    f"inspect {selected or '/Environment/Cube'}",
                    "add a cube",
                    "rename this to PracticeCube",
                ],
                "what_to_notice": [
                    "The Type column tells you what kind of prim it is.",
                    "Renaming changes the path/name, not the type.",
                ],
            },
            "prims": {
                "title": "Practical prim drill",
                "goal": "Learn prims by navigating the live stage graph.",
                "steps": [
                    "Inspect /World and /Environment.",
                    "List their children.",
                    "Select a leaf prim and rename it.",
                    "Find every Cube in the stage.",
                ],
                "commands": [
                    "inspect /World",
                    "children /Environment",
                    "find cube",
                ],
                "what_to_notice": [
                    "Prims are scene nodes with paths, parents, and children.",
                    "Hierarchy is what lets you organize content.",
                ],
            },
            "selection": {
                "title": "Practical selection drill",
                "goal": "Learn how selection controls natural language commands.",
                "steps": [
                    "Select one prim.",
                    "Say 'inspect this' and 'move it 0 2 0'.",
                    "Select another prim and repeat.",
                ],
                "commands": [
                    f"select {selected or '/Environment/Cube'}",
                    "inspect this",
                    "move this 0 2 0",
                ],
                "what_to_notice": [
                    "This, it, and selected point to the current selection.",
                    "Selection makes natural language much faster.",
                ],
            },
            "transforms": {
                "title": "Practical transform drill",
                "goal": "Learn translation, rotation, and scale by changing a real prim.",
                "steps": [
                    "Inspect the target prim.",
                    "Move it upward.",
                    "Make it bigger.",
                    "Try a dry run before a more aggressive change.",
                ],
                "commands": [
                    f"inspect {default_target}",
                    f"move {default_target} 0 2 0",
                    f"scale {default_target} 10 10 10",
                ],
                "what_to_notice": [
                    "Translation changes position.",
                    "Scale changes size.",
                    "Rotation changes orientation.",
                ],
            },
            "materials": {
                "title": "Practical material drill",
                "goal": "Learn how to bind and verify materials in the viewport.",
                "steps": [
                    "Inspect the cube or selected prim.",
                    "Bind a red material.",
                    "Check the stage tree and viewport for the change.",
                ],
                "commands": [
                    f"make {default_target} red",
                    f"inspect {default_target}",
                ],
                "what_to_notice": [
                    "Materials affect appearance, not hierarchy.",
                    "Material bindings can be inspected after the change.",
                ],
            },
            "lights": {
                "title": "Practical lighting drill",
                "goal": "Learn how lights affect the scene.",
                "steps": [
                    "Inspect the Environment prim.",
                    "Look at the existing lights.",
                    "Create or adjust a light if needed and compare the result.",
                ],
                "commands": [
                    "inspect /Environment",
                    "children /Environment",
                ],
                "what_to_notice": [
                    "Lighting changes how everything is rendered.",
                    "Light prims are scene objects like everything else.",
                ],
            },
            "workflow": {
                "title": "Practical USD workflow drill",
                "goal": "Learn the edit-check-repeat loop you’ll use daily.",
                "steps": [
                    "Inspect the scene.",
                    "Change one thing.",
                    "Verify the result.",
                    "Repeat with a second change.",
                ],
                "commands": [
                    "scene summary",
                    "inspect /Environment/Cube",
                    "move /Environment/Cube 0 2 0",
                    "rename /Environment/Cube to PracticeCube",
                ],
                "what_to_notice": [
                    "Practical learning means making a tiny change and observing the effect.",
                    "Use the scene tree as your feedback loop.",
                ],
            },
        }
        session = sessions.get(topic, sessions["schemas"])
        return {
            "ok": True,
            "target": "learn",
            "topic": topic,
            "style": "practice",
            "title": session["title"],
            "goal": session["goal"],
            "steps": session["steps"],
            "commands": session["commands"],
            "what_to_notice": session["what_to_notice"],
            "scene_context": scene,
            "next_prompt": "Do the first command, then tell me what changed and I’ll guide the next step.",
        }

    def _parse_scene_command(self, request: str) -> dict[str, Any] | None:
        text = request.strip()
        lower = text.lower()
        if lower in {"ping", "scene ping", "status"}:
            return {"action": "ping"}
        if lower in {"summary", "scene summary", "scene status"}:
            return {"action": "summary"}
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
        selection_hint = bool(re.search(r"\b(selected|this|that|it)\b", lower))
        selected_path = self._single_selected_path()
        target_path = path or (selected_path if selection_hint else None)
        create_parent = self._extract_create_parent(text) or "/World"

        select_match = re.match(r"^(?:scene\s+)?select\s+(.+)$", text, re.IGNORECASE)
        if select_match:
            paths = self._extract_paths(select_match.group(1))
            if paths:
                return {"action": "select", "paths": paths}
        if any(word in lower for word in ("select", "focus", "highlight")) and target_path is not None:
            return {"action": "select", "paths": [target_path]}

        create_match = re.match(r"^(?:scene\s+)?create\s+(\S+)(?:\s+type\s+(\S+))?$", text, re.IGNORECASE)
        if create_match:
            return {"action": "create", "path": create_match.group(1), "type_name": create_match.group(2) or "Xform"}
        size_natural = re.match(
            r"^(?:can you\s+)?(?:please\s+)?(?:make|set|turn)\s+(.+?)\s+(?:bigger|larger|smaller|tiny|small|huge|bigger\s+please)$",
            text,
            re.IGNORECASE,
        )
        if size_natural:
            path_candidate = self._extract_usd_path(size_natural.group(1)) or target_path
            if path_candidate is not None:
                scale_value = 10.0 if "smaller" not in lower else 0.1
                return {
                    "action": "move",
                    "path": path_candidate,
                    "scale": (scale_value, scale_value, scale_value),
                }
        create_natural = re.match(
            r"^(?:can you\s+)?(?:please\s+)?(?:make|create|add|spawn|place)\s+(?:a|an|the)?\s*(.+)$",
            text,
            re.IGNORECASE,
        )
        if create_natural:
            description = create_natural.group(1)
            type_name = self._guess_type_name(description)
            base_name = self._guess_base_name(description, type_name)
            parent_path = self._normalize_parent_path(create_parent)
            if target_path is None:
                target_path = self._scene.unique_child_path(parent_path, base_name)
            return {"action": "create", "path": target_path, "type_name": type_name}

        delete_match = re.match(r"^(?:scene\s+)?delete\s+(\S+)$", text, re.IGNORECASE)
        if delete_match:
            return {"action": "delete", "path": delete_match.group(1)}
        if any(word in lower for word in ("delete", "remove", "erase")) and target_path is not None:
            return {"action": "delete", "path": target_path}

        inspect_match = re.match(r"^(?:scene\s+)?inspect\s+(\S+)$", text, re.IGNORECASE)
        if inspect_match:
            return {"action": "inspect", "path": inspect_match.group(1)}
        if any(word in lower for word in ("inspect", "look at", "tell me about", "describe")) and target_path is not None:
            return {"action": "inspect", "path": target_path}

        children_match = re.match(r"^(?:scene\s+)?children\s+(\S+)$", text, re.IGNORECASE)
        if children_match:
            return {"action": "children", "path": children_match.group(1)}
        if any(phrase in lower for phrase in ("what's under", "what is under", "children of", "inside", "under")) and target_path is not None:
            return {"action": "children", "path": target_path}

        duplicate_match = re.match(r"^(?:scene\s+)?duplicate\s+(\S+)\s+to\s+(\S+)$", text, re.IGNORECASE)
        if duplicate_match:
            return {
                "action": "duplicate",
                "source_path": duplicate_match.group(1),
                "target_path": duplicate_match.group(2),
            }
        duplicate_natural = re.match(r"^(?:can you\s+)?(?:please\s+)?(?:duplicate|copy)\s+(.+?)\s+(?:to|as)\s+(\S+)$", text, re.IGNORECASE)
        if duplicate_natural:
            source_path = self._extract_usd_path(duplicate_natural.group(1)) or target_path
            if source_path is not None:
                return {
                    "action": "duplicate",
                    "source_path": source_path,
                    "target_path": duplicate_natural.group(2),
                }

        reparent_match = re.match(r"^(?:scene\s+)?reparent\s+(\S+)\s+to\s+(\S+)$", text, re.IGNORECASE)
        if reparent_match:
            return {"action": "reparent", "path": reparent_match.group(1), "new_parent": reparent_match.group(2)}
        reparent_natural = re.match(
            r"^(?:can you\s+)?(?:please\s+)?(?:move|put|nest|parent)\s+(.+?)\s+(?:under|into|inside|to)\s+(\S+)$",
            text,
            re.IGNORECASE,
        )
        if reparent_natural:
            child_path = self._extract_usd_path(reparent_natural.group(1)) or target_path
            if child_path is not None:
                return {"action": "reparent", "path": child_path, "new_parent": reparent_natural.group(2)}

        move_match = re.match(
            r"^(?:scene\s+)?move\s+(/\S*)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)$",
            text,
            re.IGNORECASE,
        )
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
        move_natural = re.match(
            r"^(?:can you\s+)?(?:please\s+)?(?:move|shift|nudge|translate)\s+(.+?)\s+(?:by|to)?\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)$",
            text,
            re.IGNORECASE,
        )
        if move_natural:
            move_path = self._extract_usd_path(move_natural.group(1)) or target_path
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

        material_match = re.match(r"^(?:scene\s+)?material\s+(\S+)\s+to\s+(\S+)$", text, re.IGNORECASE)
        if material_match:
            return {
                "action": "material",
                "path": material_match.group(1),
                "material_path": material_match.group(2),
            }
        material_natural = re.match(
            r"^(?:can you\s+)?(?:please\s+)?(?:bind|assign|set|give|paint)\s+(.+?)\s+(?:to|with|using)\s+(\S+)$",
            text,
            re.IGNORECASE,
        )
        if material_natural:
            material_path = self._extract_usd_path(material_natural.group(1))
            path_candidate = target_path or self._extract_usd_path(material_natural.group(2))
            if path_candidate is not None and material_path is not None:
                return {
                    "action": "material",
                    "path": path_candidate,
                    "material_path": material_path,
                }

        rename_match = re.match(r"^(?:scene\s+)?rename\s+(.+?)\s+to\s+(\S+)$", text, re.IGNORECASE)
        if rename_match:
            path_candidate = self._extract_usd_path(rename_match.group(1)) or target_path
            if path_candidate is not None:
                return {"action": "rename", "path": path_candidate, "new_name": rename_match.group(2)}
        rename_natural = re.match(
            r"^(?:can you\s+)?(?:please\s+)?(?:rename|call|name)\s+(.+?)\s+(?:to|as)\s+(\S+)$",
            text,
            re.IGNORECASE,
        )
        if rename_natural:
            path_candidate = self._extract_usd_path(rename_natural.group(1)) or target_path
            if path_candidate is not None:
                return {"action": "rename", "path": path_candidate, "new_name": rename_natural.group(2)}

        find_match = re.match(r"^(?:scene\s+)?(?:find|search\s+for)\s+(.+)$", text, re.IGNORECASE)
        if find_match:
            query_text = find_match.group(1).strip()
            query, scope_path = self._split_query_and_scope(query_text)
            return {"action": "find", "query": query, "scope_path": scope_path}

        save_match = re.match(r"^(?:scene\s+)?save(?:\s+as)?\s+(.+)$", text, re.IGNORECASE)
        if save_match:
            return {"action": "save", "path": save_match.group(1).strip()}
        save_natural = re.match(r"^(?:can you\s+)?(?:please\s+)?save(?:\s+this)?\s+scene(?:\s+as)?\s+(.+)$", text, re.IGNORECASE)
        if save_natural:
            return {"action": "save", "path": save_natural.group(1).strip()}

        return None

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
