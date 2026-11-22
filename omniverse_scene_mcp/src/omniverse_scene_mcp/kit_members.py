"""kit_object_members: list the real members of a Kit / Isaac object inside the running app (read-only).

usd_code_verify checks Kit module names from the installed files, but methods on Kit objects cannot be checked that
way (seen 2026-11-22: the omni.usd stub has no UsdContext.get_stage although it exists). Inside Kit the object itself
can be asked. The expression is NOT evaluated as Python: it is parsed and only a chain of names, attributes and
zero-argument getter calls is followed, e.g. `omni.usd.get_context()` or
`omni.appwindow.get_default_app_window().get_keyboard()`. Calls are allowed only for get_* / acquire_* / *_interface
names, so nothing that changes the scene (new_stage, play...) can run.
"""
from __future__ import annotations

import ast
import importlib
import re
from typing import Any

from .kit_index import KIT_TOPS

MAX_MEMBERS = 400
_GETTER = re.compile(r"^(get_\w+|acquire_\w+|\w+_interface)$")


def _steps(expression: str) -> list[tuple[str, str]]:
    tree = ast.parse(expression.strip(), mode="eval").body
    steps: list[tuple[str, str]] = []
    node = tree
    while True:
        if isinstance(node, ast.Call):
            if node.args or node.keywords or not isinstance(node.func, (ast.Attribute, ast.Name)):
                raise ValueError("only calls without arguments are allowed")
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            if not _GETTER.match(name):
                raise ValueError(f"{name}() is not a getter (get_*, acquire_*, *_interface); it is not called")
            steps.append(("call", name))
            node = node.func
        elif isinstance(node, ast.Attribute):
            steps.append(("attr", node.attr))
            node = node.value
        elif isinstance(node, ast.Name):
            steps.append(("name", node.id))
            break
        else:
            raise ValueError("only names, attributes and getter calls are allowed")
    steps.reverse()
    if steps[0][1] not in KIT_TOPS:
        raise ValueError(f"the expression must start with one of {', '.join(KIT_TOPS)}")
    return steps


def object_members(expression: str, names: Any = None) -> dict[str, Any]:
    """Inside the running Kit app: the public members of the object an expression names, e.g.
    expression="omni.usd.get_context()", names=["get_stage"]. Only names, attributes and get_*/acquire_*
    calls without arguments are followed, nothing else runs. 'has' answers for each of names."""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 300:
        return {"ok": False, "error": "expression must be a short text like omni.usd.get_context()"}
    if names is not None and (not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
        return {"ok": False, "error": "names must be a list of member names"}
    try:
        steps = _steps(expression)
    except (SyntaxError, ValueError) as exc:
        return {"ok": False, "error": f"cannot follow {expression!r}: {exc}"}
    try:
        obj = importlib.import_module(steps[0][1])
        for kind, name in steps[1:]:
            if kind == "attr":
                if hasattr(obj, name):
                    obj = getattr(obj, name)
                elif type(obj).__name__ == "module":
                    obj = importlib.import_module(f"{obj.__name__}.{name}")
                else:
                    return {"ok": False, "error": f"{name!r} does not exist on {type(obj).__name__}"}
            else:  # the getter was reached by the attr step before it
                obj = obj()
                if obj is None:
                    return {"ok": False, "error": f"{name}() returned None (nothing open or not started?)"}
    except Exception as exc:  # noqa: BLE001 - report, never raise into Kit
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    members = sorted(n for n in dir(obj) if not n.startswith("_"))
    result: dict[str, Any] = {"ok": True, "expression": expression, "type": type(obj).__name__,
                              "members": members[:MAX_MEMBERS], "truncated": len(members) > MAX_MEMBERS}
    if names:
        result["has"] = {n: hasattr(obj, n) for n in names[:50]}
    return result
