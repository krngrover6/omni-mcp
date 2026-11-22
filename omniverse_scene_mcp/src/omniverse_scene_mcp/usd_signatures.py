"""Real call signatures of USD (pxr) functions, read from USD itself.

Both USD builds on this laptop (Kit's 25.11 and usd-core 26.08) ship their Python bindings without help text, so the
signatures cannot be read from __doc__. Instead each function is called once with a Probe object that no C++
parameter accepts: USD (Boost.Python) refuses before running anything and lists every overload in its ArgumentError,
e.g. `Define(TfWeakPtr<UsdStage> stage, SdfPath path)`. Anything other than that ArgumentError means "unknown" and
nothing is checked. Results are cached per function.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_PREFIX = re.compile(r"pxrInternal_v\d+_\d+(?:_\d+)?__pxrReserved__::")


class _Probe:
    """An object of a type no USD parameter accepts."""


@dataclass(frozen=True)
class Overload:
    params: tuple[tuple[str, str, bool], ...]  # (name, simple type, optional)

    @property
    def required(self) -> int:
        return sum(1 for _, _, optional in self.params if not optional)

    @property
    def total(self) -> int:
        return len(self.params)

    def accepts(self, count: int, keywords: tuple[str, ...] = ()) -> bool:
        if not self.required <= count <= self.total:
            return False
        names = {name for name, _, _ in self.params}
        return all(k in names for k in keywords)

    def text(self, name: str, skip_self: bool) -> str:
        shown = self.params[1:] if skip_self else self.params
        parts = []
        for pname, ptype, optional in shown:
            label = f"{pname}: {ptype}" if pname else ptype
            parts.append(f"[{label}]" if optional else label)
        return f"{name}({', '.join(parts)})"


def _split(params: str) -> list[str]:
    out, depth, current = [], 0, []
    for char in params:
        if char in "<(":
            depth += 1
        elif char in ">)":
            depth -= 1
        if char == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        out.append("".join(current).strip())
    return out


def _simple_type(text: str) -> str:
    text = _PREFIX.sub("", text)
    text = re.sub(r"\b(class|struct|enum)\s+", "", text).replace("{lvalue}", "").strip()
    if "basic_string" in text and "vector" not in text:
        return "str"
    if text.startswith("std::vector"):
        return "list"
    weak = re.match(r"TfWeakPtr<(.+)>$", text)
    if weak:
        text = weak.group(1)
    text = text.split("::")[-1] if "<" not in text else text
    return {"_object * __ptr64": "self", "_object *": "self", "object": "any value"}.get(text, text)


def _param(text: str) -> tuple[str, str, bool]:
    optional = "=" in text
    head = text.split("=", 1)[0].strip()
    head = _PREFIX.sub("", head).replace("{lvalue}", "").strip()
    match = re.match(r"(.*?[\s*&>])([A-Za-z_]\w*)$", head)
    if match and not head.endswith(">") and match.group(1).strip() not in ("class", "struct", "enum", ""):
        type_text, name = match.group(1), match.group(2)
        if type_text.strip().endswith("::"):
            type_text, name = head, ""
    else:
        type_text, name = head, ""
    return name, _simple_type(type_text), optional


def parse_error(message: str) -> list[Overload]:
    """Overloads listed in a Boost.Python ArgumentError message."""
    if "did not match C++ signature" not in message:
        return []
    body = message.split("did not match C++ signature:", 1)[1]
    overloads = []
    for line in body.strip().splitlines():
        line = line.strip()
        match = re.match(r"[\w~]+\((.*)\)$", line)
        if not match:
            continue
        params = tuple(_param(p) for p in _split(match.group(1)) if p and p.strip() != "void")  # (void) = none
        overloads.append(Overload(params))
    return overloads


_CACHE: dict[int, list[Overload] | None] = {}


def signatures(func: Any, constructor: bool = False) -> list[Overload] | None:
    """The overloads of a USD function or class constructor, or None when they cannot be read safely."""
    if constructor:
        if not (getattr(func, "__module__", "") or "").startswith("pxr."):
            return None
    elif "Boost.Python.function" not in str(type(func)):
        return None  # plain Python helpers in pxr are left alone
    key = id(func)
    if key in _CACHE:
        return _CACHE[key]
    found: list[Overload] | None = None
    try:
        func(_Probe())
    except BaseException as exc:  # noqa: BLE001 - only an ArgumentError tells us anything
        if type(exc).__name__ == "ArgumentError":
            found = parse_error(str(exc)) or None
    _CACHE[key] = found
    return found
