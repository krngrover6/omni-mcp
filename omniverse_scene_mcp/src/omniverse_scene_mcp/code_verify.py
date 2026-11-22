"""usd_code_verify: check USD Python code before teaching it or running it in a real scene (router-local, read-only).

Three steps, each optional after the first:
1. API check (always): parse the code, follow which variable holds which USD object (a stage, a prim, an attribute,
   a schema such as UsdGeom.Xform or UsdPhysics.DriveAPI) and look up every `pxr` name and method on the real USD
   classes of this Python. A name that does not exist is reported with its line and the closest real names, so an
   invented call (`prim.GetAttr`, `cube.SetSize`, `Usd.Stage.Create`) is caught before anyone runs it.
2. Trial run (run=True): the code runs in a separate process on a throwaway practice scene (python_trial_run), so
   the real traceback and prints are seen; the user's scenes are never touched.
3. Doc sources (docs=True): the local NVIDIA USD Code doc MCP (port 9903) is asked about the main calls, and the top
   pages (title, link, short quote) are attached so the code can be checked against the docs.
Only what can be known statically is checked: values of unknown type (function parameters, Kit objects) are skipped
and counted, never guessed.
"""
from __future__ import annotations

import ast
import difflib
import importlib
from typing import Any, Callable

from .kit_index import KIT_TOPS, shared_index

try:
    from pxr import Usd
except ImportError:  # pragma: no cover
    Usd = None

# Old Isaac Sim module names (before 4.5) and where they live now. Seen in many tutorials and generated code.
_KIT_RENAMES = (
    ("omni.isaac.core.utils", "isaacsim.core.utils"),
    ("omni.isaac.core", "isaacsim.core.api"),
    ("omni.isaac.kit", "isaacsim.simulation_app"),
    ("omni.isaac.core_nodes", "isaacsim.core.nodes"),
    ("omni.isaac.wheeled_robots", "isaacsim.robot.wheeled_robots"),
    ("omni.isaac.franka", "isaacsim.robot.manipulators.examples.franka"),
    ("omni.isaac.sensor", "isaacsim.sensors.physics"),
    ("omni.isaac.nucleus", "isaacsim.storage.native"),
)

MAX_CODE_CHARS = 20000
MAX_PROBLEMS = 20
MAX_DOC_QUERIES = 3

# Methods whose return type is known by name, whatever object they are called on.
_PRIM_METHODS = {"GetPrimAtPath", "DefinePrim", "OverridePrim", "CreateClassPrim", "GetPrim", "GetParent",
                 "GetDefaultPrim", "GetPseudoRoot", "GetChild", "GetNextSibling"}
_PRIM_LISTS = {"Traverse", "TraverseAll", "GetChildren", "GetAllChildren", "GetFilteredChildren"}
_ATTR_METHODS = {"GetAttribute", "CreateAttribute"}
_REL_METHODS = {"GetRelationship", "CreateRelationship"}
_STAGE_FACTORIES = {"Open", "CreateNew", "CreateInMemory"}
_SCHEMA_FACTORIES = {"Define", "Get", "Apply"}


class _Obj:
    """A known value: kind 'module' / 'class' (the thing itself) or 'inst' (an instance of cls), or 'list' of cls."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: Any) -> None:
        self.kind, self.value = kind, value

    def label(self) -> str:
        value = self.value
        name = getattr(value, "__name__", str(value))
        module = getattr(value, "__module__", "") or ""
        if self.kind == "module":
            return name.replace("pxr.", "")
        prefix = module.replace("pxr.", "").split("._")[0]
        return f"{prefix}.{name}" if prefix and prefix != "builtins" else name


def _public(names: list[str]) -> list[str]:
    return [n for n in names if not n.startswith("_")]


class _Checker(ast.NodeVisitor):
    def __init__(self, kit: Any = None) -> None:
        self.kit = kit
        self.kit_checked = 0
        self.env: dict[str, _Obj] = {}
        self.memo: dict[int, _Obj | None] = {}
        self.problems: dict[tuple[int, str], dict[str, Any]] = {}
        self.checked: dict[str, int] = {}
        self.unchecked = 0
        self.kit_imports: list[str] = []
        self.pxr_modules: list[str] = []
        self.calls: list[str] = []

    # ---------------------------------------------------------------- imports
    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "pxr":
            for alias in node.names:
                self._bind_pxr(alias.name, alias.asname or alias.name, node.lineno)
        elif module.startswith("pxr."):
            for alias in node.names:
                self._problem(node.lineno, f"from {module} import {alias.name}", "bad_import",
                              "Import USD modules as `from pxr import Usd, UsdGeom` (pxr has modules, not submodule "
                              "members).", [])
        elif module.split(".")[0] in KIT_TOPS:
            self.kit_imports.append(module)
            if self._kit_module(module, node.lineno) is None:
                return
            for alias in node.names:
                if alias.name != "*":
                    found = self._kit_member(module, alias.name, node.lineno)
                    self._bind(ast.Name(alias.asname or alias.name), found)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.startswith("pxr."):
                short = alias.name.split(".", 1)[1]
                self._bind_pxr(short, alias.asname or alias.name, node.lineno, dotted=alias.asname is None)
            elif alias.name.split(".")[0] in KIT_TOPS:
                self.kit_imports.append(alias.name)
                if self._kit_module(alias.name, node.lineno) is None:
                    continue
                if alias.asname:
                    self.env[alias.asname] = _Obj("kmod", alias.name)
                else:
                    top = alias.name.split(".")[0]
                    self.env[top] = _Obj("kmod", top)

    # ---------------------------------------------------------------- Kit / Isaac modules (names only)
    def _kit_module(self, dotted: str, line: int) -> Any:
        """The module's info, True when it cannot be checked here, or None (after reporting) when it is not installed."""
        if self.kit is None or not self.kit.available:
            return True
        info = self.kit.module(dotted)
        if info is not None:
            self.kit_checked += 1
            return info
        parent, _, last = dotted.rpartition(".")
        options = []
        above = self.kit.module(parent) if parent else None
        if above is not None and last in above.names:
            self.unchecked += 1  # registered at run time (seen: sys.modules["omni.replicator.core.backends"] = ...)
            return True
        if above is not None:
            options = difflib.get_close_matches(last, sorted(above.names), n=3, cutoff=0.5)
            options = [f"{parent}.{o}" for o in options]
        for old, new in _KIT_RENAMES:
            if dotted == old or dotted.startswith(old + "."):
                moved = new + dotted[len(old):]
                if self.kit.module(moved) is not None:  # suggest only what is really installed
                    options = [moved] + options
                break
        self._problem(line, dotted, "unknown_kit_module",
                      f"No module {dotted!r} in the installed Isaac Sim 6.1 (checked its exts and extscache).",
                      options[:3], attr=dotted.split(".")[-1])
        return None

    def _kit_member(self, dotted: str, name: str, line: int) -> _Obj | None:
        if self.kit is None or not self.kit.available:
            return None
        if self.kit.module(f"{dotted}.{name}") is not None:
            self.kit_checked += 1
            return _Obj("kmod", f"{dotted}.{name}")
        info = self.kit.module(dotted)
        if info is None or (name not in info.names and info.open):
            self.unchecked += 1
            return None
        if name not in info.names:
            self._problem(line, f"{dotted}.{name}", "unknown_kit_name",
                          f"{dotted} has no {name!r} in the installed Isaac Sim 6.1.",
                          difflib.get_close_matches(name, _public(sorted(info.names)), n=3, cutoff=0.5), attr=name)
            return None
        self.kit_checked += 1
        self.checked[f"{dotted}.{name}"] = self.checked.get(f"{dotted}.{name}", 0) + 1
        if name in info.classes:
            return _Obj("kenum", (dotted, name, info.classes[name]))
        return None

    def _bind_pxr(self, name: str, bind: str, line: int, dotted: bool = False) -> None:
        try:
            module = importlib.import_module("pxr." + name)
        except ImportError:
            import pxr

            if self.kit is not None and self.kit.available and self.kit.has_pxr_module(name):
                self.kit_imports.append(f"pxr.{name}")  # Kit-only schema (PhysxSchema...): names not checked here
                self.unchecked += 1
                return
            known = [m for m in dir(pxr) if not m.startswith("_")]
            self._problem(line, f"pxr.{name}", "unknown_module", f"pxr has no module {name!r} in this USD.",
                          difflib.get_close_matches(name, known, n=3))
            return
        self.pxr_modules.append(name)
        if not dotted:
            self.env[bind] = _Obj("module", module)

    # ---------------------------------------------------------------- statements that bind names
    def visit_Assign(self, node: ast.Assign) -> None:
        value = self._type(node.value)
        for target in node.targets:
            self._bind(target, value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._bind(node.target, self._type(node.value))
            self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        source = self._type(node.iter)
        item = _Obj("inst", source.value) if source is not None and source.kind == "list" else None
        self._bind(node.target, item)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            value = self._type(item.context_expr)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, value)
        self.generic_visit(node)

    def _bind(self, target: ast.AST, value: _Obj | None) -> None:
        if isinstance(target, ast.Name):
            if value is None:
                self.env.pop(target.id, None)
            else:
                self.env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element, None)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        saved = dict(self.env)
        for arg in node.args.args + node.args.kwonlyargs:
            self.env.pop(arg.arg, None)
        self.generic_visit(node)
        self.env = saved

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    # ---------------------------------------------------------------- expressions
    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._type(node)

    def visit_Call(self, node: ast.Call) -> None:
        self._type(node)
        for arg in node.args:
            self.visit(arg)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def _type(self, node: ast.AST) -> _Obj | None:
        key = id(node)
        if key not in self.memo:
            self.memo[key] = None
            self.memo[key] = self._compute(node)
        return self.memo[key]

    def _compute(self, node: ast.AST) -> _Obj | None:
        if isinstance(node, ast.Name):
            return self.env.get(node.id)
        if isinstance(node, ast.Attribute):
            return self._attribute(node)
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            first = self._type(node.elts[0])
            for element in node.elts[1:]:
                self._type(element)
            return _Obj("list", first.value) if first is not None and first.kind == "inst" else None
        return None

    def _attribute(self, node: ast.Attribute) -> _Obj | None:
        base = self._type(node.value)
        name = node.attr
        if base is not None and base.kind == "kmod":
            return self._kit_member(base.value, name, node.lineno)
        if base is not None and base.kind == "kenum":
            dotted, cls, members = base.value
            if name in members or name.startswith("__"):
                self.checked[f"{dotted}.{cls}.{name}"] = 1
            else:
                self._problem(node.lineno, f"{dotted}.{cls}.{name}", "unknown_kit_name",
                              f"{dotted}.{cls} has no value {name!r} in the installed Isaac Sim 6.1.",
                              difflib.get_close_matches(name, _public(sorted(members)), n=3, cutoff=0.5))
            return None
        if base is None:
            if isinstance(node.value, (ast.Name, ast.Attribute, ast.Call)):
                self.unchecked += 1
            return None
        if base.kind in ("list", "method", "func"):
            self.unchecked += 1  # e.g. layer.subLayerPaths.insert: a property's value, type unknown here
            return None
        holder = base.value
        if not hasattr(holder, name):
            if base.kind == "inst" and name.startswith("__"):
                return None
            options = _public(dir(holder))
            where = base.label()
            what = "module" if base.kind == "module" else "class" if base.kind == "class" else "object"
            self._problem(node.lineno, f"{where}.{name}", "unknown_name",
                          f"{where} ({what}) has no {name!r} in USD {_usd_version()}.",
                          difflib.get_close_matches(name, options, n=3, cutoff=0.5), base=where, attr=name)
            return None
        full = f"{base.label()}.{name}"
        self.checked[full] = self.checked.get(full, 0) + 1
        value = getattr(holder, name)
        if base.kind == "module":
            return _Obj("class", value) if isinstance(value, type) else _Obj("module", value) if \
                type(value).__name__ == "module" else _Obj("func", value)
        if base.kind == "class" and isinstance(value, type):
            return _Obj("class", value)  # nested class, e.g. Usd.Stage.InitialLoadSet
        return _Obj("method", (base, name))

    def _call(self, node: ast.Call) -> _Obj | None:
        func = node.func
        callee = self._type(func)
        if isinstance(func, ast.Attribute) and not (callee is not None and callee.kind == "class"):
            method = func.attr
            if method == "get_stage":  # omni.usd.get_context().get_stage()
                return _Obj("inst", Usd.Stage)
            if callee is not None and callee.kind == "method":
                base, _ = callee.value
                if base.kind == "class":
                    self.calls.append(f"{base.label()}.{method}")
                    if base.value is Usd.Stage and method in _STAGE_FACTORIES:
                        return _Obj("inst", Usd.Stage)
                    if method in _SCHEMA_FACTORIES:
                        return _Obj("inst", base.value)
                elif base.kind == "inst":
                    self.calls.append(f"{base.label()}.{method}")
            return _by_method_name(method)
        if callee is not None and callee.kind == "class":
            self.calls.append(callee.label())
            return _Obj("inst", callee.value)  # a schema constructor: UsdPhysics.DriveAPI(prim, "angular")
        return None

    # ---------------------------------------------------------------- report
    def _problem(self, line: int, name: str, code: str, message: str, suggestions: list[str], **extra: Any) -> None:
        key = (line, name)
        if key in self.problems or len(self.problems) >= MAX_PROBLEMS:
            return
        hint = ""
        if suggestions:
            hint = " Did you mean " + " or ".join(f"`{s}`" for s in suggestions) + "?"
        tip = _TIPS.get(extra.get("attr", ""))
        self.problems[key] = {"line": line, "name": name, "code": code, "message": message + hint,
                              "did_you_mean": suggestions, **({"tip": tip} if tip else {})}


def _by_method_name(method: str) -> _Obj | None:
    if Usd is None:
        return None
    if method in _PRIM_METHODS:
        return _Obj("inst", Usd.Prim)
    if method in _PRIM_LISTS:
        return _Obj("list", Usd.Prim)
    if method in _ATTR_METHODS or (method.endswith("Attr") and method.startswith(("Get", "Create"))):
        return _Obj("inst", Usd.Attribute)
    if method in _REL_METHODS or (method.endswith("Rel") and method.startswith(("Get", "Create"))):
        return _Obj("inst", Usd.Relationship)
    if method == "GetStage":
        return _Obj("inst", Usd.Stage)
    if method in ("GetRootLayer", "GetSessionLayer"):
        from pxr import Sdf
        return _Obj("inst", Sdf.Layer)
    return None


# Plain-language fixes for invented names seen often in generated code.
_TIPS = {
    "SetSize": "Schema attributes are set through their attribute: cube.GetSizeAttr().Set(50).",
    "SetRadius": "Schema attributes are set through their attribute: sphere.GetRadiusAttr().Set(30).",
    "GetAttr": "Use prim.GetAttribute('name') (full word), or the schema's GetXxxAttr().",
    "SetValue": "An attribute's value is set with attr.Set(value) (and read with attr.Get()).",
    "GetValue": "An attribute's value is read with attr.Get() (attr.Get(time) for a time sample).",
    "Create": "A new stage is Usd.Stage.CreateNew('file.usda') or Usd.Stage.CreateInMemory().",
    "SetTranslation": "XformCommonAPI uses SetTranslate((x, y, z)); an Xformable uses AddTranslateOp().Set(...).",
    "SetTranslate": "Only XformCommonAPI has SetTranslate; on an Xform use AddTranslateOp().Set((x, y, z)).",
}


def _usd_version() -> str:
    return ".".join(str(v) for v in Usd.GetVersion()) if Usd is not None else "?"


def _docs(calls: list[str], problems: list[dict[str, Any]], ask: Callable[[list[str]], list[dict[str, Any]]] | None
          ) -> dict[str, Any]:
    from .doc_sources import _default_ask, parse_sources

    wanted: list[str] = []
    for item in problems:
        base = item["name"].rsplit(".", 1)[0]
        if base not in wanted:
            wanted.append(base)
    for call in calls:
        if call not in wanted:
            wanted.append(call)
    wanted = wanted[:MAX_DOC_QUERIES]
    if not wanted:
        return {"ok": False, "note": "No USD calls to look up."}
    queries = [f"{name} OpenUSD Python API usage example" for name in wanted]
    try:
        answers = (ask or _default_ask)(queries)
    except Exception as exc:  # server off, timeout: the API check still stands
        return {"ok": False, "note": f"NVIDIA doc lookup failed ({type(exc).__name__}). Start it with: "
                                     "docker start usd-code-mcp"}
    pages = []
    for name, answer in zip(wanted, answers):
        sources = parse_sources(answer.get("content", "")) if isinstance(answer, dict) and answer.get("ok") else []
        pages.append({"about": name, "sources": sources})
    found = sum(bool(p["sources"]) for p in pages)
    return {"ok": found > 0, "pages": pages,
            "note": "Top NVIDIA USD doc hits for the main calls: open them to confirm the code matches the docs."
            if found else "The doc server answered without sources; check it is running (docker start usd-code-mcp)."}


def verify_code(code: str, run: Any = True, docs: Any = False, timeout_s: Any = 20.0,
                runner: Callable[[list[str]], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """Check USD Python code before using it: every pxr name and method is looked up on the real USD classes (an
    invented call is reported with its line and the closest real names), then run=True runs it on a throwaway practice
    scene (never your real scene) and docs=True attaches NVIDIA doc pages for the main calls. Read-only.

    verdict is 'ok' (no problem found and, when run, it ran), 'problems' or 'syntax_error'. Values of unknown type
    (function parameters, Kit objects) are skipped and counted in 'unchecked', never guessed."""
    if Usd is None:
        return {"ok": False, "error": "USD Python bindings (pxr) are not available in the router's Python"}
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "code must be a non-empty string of Python"}
    if len(code) > MAX_CODE_CHARS:
        return {"ok": False, "error": f"code is longer than {MAX_CODE_CHARS} characters"}
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return {"ok": True, "verdict": "syntax_error", "usd_version": _usd_version(),
                "problems": [{"line": exc.lineno, "code": "syntax_error", "name": (exc.text or "").strip(),
                              "message": f"{exc.msg} (line {exc.lineno}, column {exc.offset}). Nothing else was "
                                         "checked or run."}],
                "next_step": f"Fix line {exc.lineno} first: {exc.msg}."}
    checker = _Checker(shared_index())
    checker.visit(tree)
    problems = sorted(checker.problems.values(), key=lambda p: p["line"])
    result: dict[str, Any] = {
        "ok": True, "usd_version": _usd_version(), "problems": problems,
        "checked_names": len(checker.checked), "checked": sorted(checker.checked)[:40],
        "unchecked": checker.unchecked, "pxr_modules": sorted(set(checker.pxr_modules)),
    }
    if checker.kit_imports:
        result["kit_imports"] = sorted(set(checker.kit_imports))
        if checker.kit.available:
            result["kit_checked_names"] = checker.kit_checked
            result["kit_note"] = ("Kit/Isaac modules, functions and enum values were looked up in the installed Isaac "
                                  "Sim 6.1 files; methods on Kit objects are not checked (the stubs miss some). "
                                  "Composer's Kit may differ slightly: ask kit_docs when unsure.")
        else:
            result["kit_note"] = (f"Isaac Sim install not found at {checker.kit.root}, so Kit/Isaac names were not "
                                  "checked; ask the kit_docs or isaac_sim_docs MCP about them.")
    ran_ok = None
    if run is True:
        from .exam.python_practice import trial_run

        try:
            timeout = float(timeout_s)
        except (TypeError, ValueError):
            timeout = 20.0
        trial = trial_run(code, timeout_s=timeout)
        result["run"] = trial
        ran_ok = bool(trial.get("ok") and trial.get("ran"))
    if docs is True:
        result["docs"] = _docs(checker.calls, problems, runner)
    bad = bool(problems) or ran_ok is False
    result["verdict"] = "problems" if bad else "ok"
    if problems:
        first = problems[0]
        result["next_step"] = f"Line {first['line']}: {first['message']}" + (f" {first['tip']}" if first.get("tip") else "")
    elif ran_ok is False:
        error = (result["run"].get("error") or {})
        result["next_step"] = f"The names exist, but the trial run failed: {error.get('explanation') or error}"
    else:
        result["next_step"] = ("Every USD name checked exists" + (" and the code ran on the practice scene" if ran_ok
                               else "") + ". Physics and Kit-only behaviour still need a live check.")
    return result
