from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except TypeError:
            return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _flatten_tool_content(result: Any) -> str:
    content = getattr(result, "content", None)
    if not content:
        if hasattr(result, "model_dump"):
            dumped = result.model_dump()
            return str(dumped)
        return str(result)

    parts: list[str] = []
    for item in content:
        if hasattr(item, "text"):
            parts.append(str(item.text))
        else:
            parts.append(str(_jsonable(item)))
    return "\n".join(parts).strip()


@dataclass(slots=True)
class ToolSelection:
    tool_name: str
    arguments: dict[str, Any]
    explanation: str


class StreamableMCPKnowledgeBackend:
    """Async adapter that calls a real upstream MCP server over streamable HTTP."""

    def __init__(
        self,
        name: str,
        endpoint: str,
        selector: Callable[[str], ToolSelection],
        headers: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.endpoint = endpoint
        self.selector = selector
        self.headers = headers or {}
        self.enabled = enabled

    async def ask(self, query: str) -> dict[str, Any]:
        if not self.enabled:
            return {
                "ok": False,
                "backend": self.name,
                "endpoint": self.endpoint,
                "query": query,
                "message": "Knowledge backend is disabled or not configured.",
            }

        selection = self.selector(query)
        try:
            async with streamablehttp_client(self.endpoint, headers=self.headers) as (
                read_stream,
                write_stream,
                _get_session_id,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tool_result = await session.call_tool(selection.tool_name, selection.arguments)
                    return {
                        "ok": not getattr(tool_result, "isError", False),
                        "backend": self.name,
                        "endpoint": self.endpoint,
                        "query": query,
                        "tool": selection.tool_name,
                        "tool_arguments": selection.arguments,
                        "selection_reason": selection.explanation,
                        "content": _flatten_tool_content(tool_result),
                        "raw": _jsonable(tool_result),
                    }
        except Exception as exc:
            return {
                "ok": False,
                "backend": self.name,
                "endpoint": self.endpoint,
                "query": query,
                "tool": selection.tool_name,
                "tool_arguments": selection.arguments,
                "selection_reason": selection.explanation,
                "message": f"Failed to query upstream MCP backend: {exc}",
            }


def _extract_name(query: str) -> str:
    candidates = [
        r"module\s+([A-Za-z0-9_.]+)",
        r"class\s+([A-Za-z0-9_.]+)",
        r"method\s+([A-Za-z0-9_.]+)",
        r"for\s+([A-Za-z0-9_.]+)",
        r"about\s+([A-Za-z0-9_.]+)",
    ]
    for pattern in candidates:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


_USD_NAME = re.compile(
    r"\b(?:pxr\.)?((?:Usd|Sdf|Gf|Tf|Vt|Pcp|Ar|Kind|Sdr|Ndr)(?:[A-Z][A-Za-z0-9_]*)?(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b"
)


def _extract_usd_name(query: str) -> str:
    """Return a pxr module or class name such as UsdGeom, Sdf.Layer or UsdGeom.Xform, or "" if none is named.

    Qualified names win over bare module words, and the last bare word wins, so an English "Kind of ..." or a
    leading "Usd" does not hide the name the user is asking about.
    """
    names = [match.group(1) for match in _USD_NAME.finditer(query)]
    bare = {"Usd", "Sdf", "Gf", "Tf", "Vt", "Pcp", "Ar", "Kind", "Sdr", "Ndr"}
    qualified = [name for name in names if name not in bare]
    if qualified:
        return qualified[0]
    return names[-1] if names else ""


_METHOD_FILLER = {"detail", "details", "docs", "doc", "signature", "signatures", "reference", "info", "for", "of", "on"}
_METHOD_VERB = re.compile(
    r"\b((?:Get|Set|Create|Add|Remove|Has|Is|Compute|Clear|Apply|Define|Open|Find|Traverse|Bind|Make)[A-Z][A-Za-z0-9_]*)\b"
)


def _extract_usd_method(query: str) -> tuple[str, str]:
    """Return (class_name, method_name), e.g. ("Usd.Stage", "Open") for "method detail for Usd.Stage.Open".

    Filler words after "method" ("method detail for ...") are never taken as the method name.
    """
    name = _extract_usd_name(query)
    parts = name.split(".") if name else []
    if len(parts) >= 3:
        return ".".join(parts[:-1]), parts[-1]
    match = re.search(r"\b(?:method|function)s?\s+(?:named\s+|called\s+)?([A-Za-z_][A-Za-z0-9_]*)", query, re.IGNORECASE)
    if match and match.group(1).lower() not in _METHOD_FILLER and match.group(1) != name:
        return name, match.group(1)
    for candidate in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", query) + _METHOD_VERB.findall(query):
        if candidate not in name.split("."):
            return name, candidate
    return name, ""


def _extract_instruction_sets(query: str) -> str | list[str] | None:
    lowered = query.lower()
    for keyword in ("kit_system", "omni_ui_system", "isaacsim_system", "extensions", "testing", "ui"):
        if keyword in lowered:
            return keyword
    return None


def _extract_after_prefix(query: str, prefixes: tuple[str, ...]) -> str:
    stripped = query.strip()
    lowered = stripped.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return stripped[len(prefix):].strip().strip('"').strip("'")
    return ""


def _top_k_for_query(query: str, default: int = 10) -> int:
    lowered = query.lower()
    if any(token in lowered for token in ("best", "top", "most relevant", "quick")):
        return 5
    return default


def build_usd_backend(endpoint: str | None = None) -> StreamableMCPKnowledgeBackend:
    endpoint = endpoint or os.environ.get("OMNIVERSE_USD_MCP_ENDPOINT", "http://127.0.0.1:9903/mcp")

    def select(query: str) -> ToolSelection:
        lowered = query.strip().lower()
        if any(token in lowered for token in ("list modules", "modules available", "browse modules")):
            return ToolSelection("list_usd_modules", {}, "User asked for a module listing")
        if any(token in lowered for token in ("list classes", "classes in", "browse classes")):
            return ToolSelection("list_usd_classes", {}, "User asked for class browsing")
        if any(token in lowered for token in ("method detail", "method", "signature", "arguments")):
            class_name, method_name = _extract_usd_method(query)
            if not method_name and class_name:
                return ToolSelection(
                    "get_usd_class_detail", {"class_names": class_name}, "No method named; class detail lists its methods"
                )
            payload = {"method_names": method_name or query}
            if class_name:
                payload["class_name"] = class_name
            return ToolSelection("get_usd_method_detail", payload, "User asked for a method signature or docs")
        if any(token in lowered for token in ("class detail", "class docs", "class reference")):
            class_name = _extract_usd_name(query) or query
            return ToolSelection("get_usd_class_detail", {"class_names": class_name}, "User asked for class details")
        if any(token in lowered for token in ("module detail", "module docs", "module reference")):
            module_name = _extract_usd_name(query) or query
            return ToolSelection("get_usd_module_detail", {"module_names": module_name}, "User asked for module details")
        if any(token in lowered for token in ("example", "code", "snippet", "how to", "script", "create", "build")):
            return ToolSelection("search_usd_code_examples", {"request": query}, "User asked for implementation examples")
        return ToolSelection("search_usd_knowledge", {"request": query}, "Defaulted to conceptual USD knowledge search")

    return StreamableMCPKnowledgeBackend("usd", endpoint, select)


def build_kit_backend(endpoint: str | None = None) -> StreamableMCPKnowledgeBackend:
    endpoint = endpoint or os.environ.get("OMNIVERSE_KIT_MCP_ENDPOINT", "http://127.0.0.1:9902/mcp")

    def select(query: str) -> ToolSelection:
        lowered = query.strip().lower()
        if any(token in lowered for token in ("instruction", "guide", "docs", "documentation", "system")):
            instruction_sets = _extract_instruction_sets(query)
            payload: dict[str, Any] = {}
            if instruction_sets is not None:
                payload["instruction_sets"] = instruction_sets
            return ToolSelection("get_kit_instructions", payload, "User asked for Kit guidance or instructions")
        return ToolSelection(
            "search_kit_extensions",
            {"query": query, "top_k": _top_k_for_query(query)},
            "User asked for Kit extension discovery",
        )

    return StreamableMCPKnowledgeBackend("kit", endpoint, select)


def build_omni_ui_backend(endpoint: str | None = None) -> StreamableMCPKnowledgeBackend:
    endpoint = endpoint or os.environ.get("OMNIVERSE_OMNI_UI_MCP_ENDPOINT", "http://127.0.0.1:9901/mcp")

    def select(query: str) -> ToolSelection:
        lowered = query.strip().lower()
        if any(token in lowered for token in ("instruction", "guide", "docs", "documentation", "style", "class", "method")):
            payload: dict[str, Any] = {}
            name = _extract_name(query)
            if name:
                payload["name"] = name
            return ToolSelection("get_ui_instructions", payload, "User asked for OmniUI instructions or references")
        return ToolSelection(
            "search_ui_code_examples",
            {"query": query, "top_k": _top_k_for_query(query)},
            "User asked for OmniUI code examples",
        )

    return StreamableMCPKnowledgeBackend("omni_ui", endpoint, select)


def build_isaac_sim_backend(endpoint: str | None = None) -> StreamableMCPKnowledgeBackend:
    endpoint = endpoint or os.environ.get("OMNIVERSE_ISAAC_SIM_MCP_ENDPOINT", "http://127.0.0.1:9904/mcp")

    def select(query: str) -> ToolSelection:
        lowered = query.strip().lower()
        if any(token in lowered for token in ("instruction", "guide", "docs", "documentation", "system")):
            instruction_sets = _extract_instruction_sets(query)
            payload: dict[str, Any] = {}
            if instruction_sets is not None:
                payload["instruction_sets"] = instruction_sets
            return ToolSelection("get_isaac_sim_instructions", payload, "User asked for Isaac Sim guidance or instructions")
        return ToolSelection(
            "search_isaac_sim_extensions",
            {"query": query, "top_k": _top_k_for_query(query)},
            "User asked for Isaac Sim extension discovery",
        )

    return StreamableMCPKnowledgeBackend("isaac_sim", endpoint, select)


def build_bowerbot_backend(endpoint: str | None = None) -> StreamableMCPKnowledgeBackend:
    endpoint = endpoint or os.environ.get("OMNIVERSE_BOWERBOT_MCP_ENDPOINT", "http://127.0.0.1:8181/mcp")

    def select(query: str) -> ToolSelection:
        lowered = query.strip().lower()

        if any(token in lowered for token in ("list projects", "projects", "project list")):
            return ToolSelection("list_projects", {}, "User asked for BowerBot projects")

        if any(token in lowered for token in ("current project", "focused project", "which project")):
            return ToolSelection("get_current_project", {}, "User asked which BowerBot project is focused")

        if any(token in lowered for token in ("open project", "resume project", "switch project")):
            name = _extract_after_prefix(
                query,
                ("open project ", "resume project ", "switch project "),
            ) or _extract_name(query) or query.strip()
            return ToolSelection("open_project", {"name": name}, "User asked to open an existing BowerBot project")

        if any(token in lowered for token in ("new project", "create project", "start project")):
            name = _extract_after_prefix(
                query,
                ("new project ", "create project ", "start project "),
            ) or _extract_name(query) or query.strip()
            return ToolSelection(
                "create_project",
                {"name": name, "up_axis": "Z", "meters_per_unit": 1.0},
                "User asked to start a new BowerBot project",
            )

        if any(token in lowered for token in ("validate", "check scene", "verify", "usd checker")):
            return ToolSelection("validate_scene", {}, "User asked to validate a BowerBot scene")

        if any(token in lowered for token in ("package", "export", "usdz", "publish")):
            return ToolSelection("package_scene", {}, "User asked to package a BowerBot scene")

        if any(token in lowered for token in ("scene", "layout", "structure", "show scene", "what's in the scene")):
            return ToolSelection("list_scene", {}, "User asked to inspect the BowerBot scene")

        if any(token in lowered for token in ("assets", "asset list", "project assets")):
            return ToolSelection("list_project_assets", {}, "User asked to inspect BowerBot project assets")

        if any(token in lowered for token in ("build", "assemble", "place asset", "scene assembly", "scene build")):
            return ToolSelection("list_scene", {}, "Best-effort scene assembly handoff to BowerBot")

        return ToolSelection("list_scene", {}, "Defaulted to BowerBot scene inspection")

    return StreamableMCPKnowledgeBackend("bowerbot", endpoint, select)


def build_placeholder_backend(name: str, endpoint: str | None = None) -> StreamableMCPKnowledgeBackend:
    endpoint = endpoint or ""

    def select(query: str) -> ToolSelection:
        return ToolSelection("search", {"query": query}, "Placeholder backend; no selector configured")

    return StreamableMCPKnowledgeBackend(name, endpoint, select, enabled=False)
