"""extension_check: find the usual beginner mistakes in a Kit extension folder before Kit fails quietly (read-only).

A Kit extension that is wrong often just does nothing: no error in the viewport, the button never appears. This reads
the folder (config/extension.toml and the Python package it names) without running anything, and reports:
- extension.toml that does not parse, has no [package] version, or a version that is not x.y.z,
- a [[python.module]] whose name has no matching folder (name = "my.ext" needs my/ext/__init__.py),
- no class that subclasses omni.ext.IExt, or one that __init__.py never imports (Kit only sees what the module's
  __init__ exposes), on_startup without its ext_id parameter, on_shutdown with extra parameters,
- an omni / isaacsim import that no dependency provides, directly or through the dependencies' own dependencies
  (read from the installed extensions' extension.toml files; Kit's kernel modules such as carb, omni.ext and
  omni.kit.app are always there),
- a dependency that is not installed in the local Isaac Sim (a note: Composer has a different set),
- an omni.ui Window made at startup that on_shutdown never destroys (it stays behind on every hot reload).
"""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

from .kit_index import default_root

MAX_PY_FILES = 200
_SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+].*)?$")
_KERNEL_FALLBACK = {"carb", "omni.core", "omni.experimental", "omni.ext", "omni.kit.app", "omni.log",
                    "omni.platforminfo", "omni.schema", "omni.str", "omni.structuredlog"}
_EXT_FOLDERS = ("kit/exts", "kit/extscore", "kit/extsPhysics", "exts", "extscache", "extsDeprecated")
_INSTALLED: dict[str, dict[str, Any]] | None = None


def _installed(root: Path) -> dict[str, dict[str, Any]]:
    """{extension name: {"deps": [...], "modules": [...]}} from the local install's extension.toml files."""
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    found: dict[str, dict[str, Any]] = {}
    for folder in _EXT_FOLDERS:
        base = root / folder
        if not base.is_dir():
            continue
        for ext in base.iterdir():
            toml = ext / "config" / "extension.toml"
            if not toml.is_file():
                continue
            name = re.sub(r"-\d+\.\d+.*$", "", ext.name)  # omni.usd-1.18.0+... -> omni.usd
            try:
                data = tomllib.loads(toml.read_text(encoding="utf-8", errors="replace"))
            except (tomllib.TOMLDecodeError, OSError):
                continue
            modules = [m.get("name") for m in data.get("python", {}).get("module", []) if m.get("name")]
            found[name] = {"deps": list((data.get("dependencies") or {}).keys()), "modules": modules}
    _INSTALLED = found
    return found


def _kernel(root: Path) -> set[str]:
    py = root / "kit" / "kernel" / "py"
    if not py.is_dir():
        return set(_KERNEL_FALLBACK)
    names = {"carb"} | {f"omni.{p.name}" for p in (py / "omni").iterdir() if p.is_dir() and p.name != "kit"}
    names |= {f"omni.kit.{p.name}" for p in (py / "omni" / "kit").iterdir() if p.is_dir()}
    return names


def _provided(deps: list[str], installed: dict[str, dict[str, Any]]) -> set[str]:
    """Every module name reachable through the dependencies (and their dependencies)."""
    seen: set[str] = set()
    todo = list(deps)
    names: set[str] = set()
    while todo and len(seen) < 2000:
        dep = todo.pop()
        if dep in seen:
            continue
        seen.add(dep)
        names.add(dep)
        info = installed.get(dep)
        if info:
            names.update(info["modules"])
            todo.extend(info["deps"])
    return names


def _covered(module: str, names: set[str]) -> bool:
    return any(module == n or module.startswith(n + ".") or n.startswith(module + ".") for n in names)


def _line_of(text: str, needle: str) -> int | None:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    return None


def check_extension(folder: str) -> dict[str, Any]:
    """Check a Kit extension folder you wrote (the one holding config/extension.toml) for the mistakes that make an
    extension silently do nothing: toml and version, python.module folders, the omni.ext.IExt class and whether
    __init__.py exposes it, on_startup(self, ext_id) / on_shutdown(self), imports without a dependency, windows never
    destroyed. Nothing is run or changed."""
    if not isinstance(folder, str) or not folder.strip():
        return {"ok": False, "error": "folder must be the extension's folder path"}
    ext = Path(folder.strip())
    if ext.name == "extension.toml":
        ext = ext.parent.parent
    elif ext.name == "config":
        ext = ext.parent
    toml_path = ext / "config" / "extension.toml"
    if not toml_path.is_file():
        return {"ok": False, "error": f"No config/extension.toml in {ext}: give the extension's own folder."}
    problems: list[dict[str, Any]] = []

    def add(severity: str, code: str, where: str, line: int | None, message: str, fix: str) -> None:
        problems.append({"severity": severity, "code": code, "file": where, "line": line, "message": message,
                         "fix": fix})

    toml_text = toml_path.read_text(encoding="utf-8", errors="replace")
    try:
        data = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        match = re.search(r"line (\d+)", str(exc))
        add("error", "toml_parse", "config/extension.toml", int(match.group(1)) if match else None,
            f"extension.toml cannot be read: {exc}. Kit will not load the extension at all.",
            "Fix that line (a missing quote or = is the usual cause).")
        return _report(ext, problems, {})
    package = data.get("package") or {}
    version = package.get("version")
    if not version:
        if not re.search(r"-\d+\.\d+", ext.name):  # NVIDIA's cached folders carry it in the name (name-1.2.3)
            add("warning", "no_version", "config/extension.toml", _line_of(toml_text, "[package]"),
                "[package] has no version and the folder name has none either; the Extension Manager shows it as "
                "0.0.0 and other extensions cannot ask for a version of it.", 'Add version = "0.1.0" under [package].')
    elif not _SEMVER.match(str(version)):
        add("info", "bad_version", "config/extension.toml", _line_of(toml_text, "version"),
            f"version {version!r} is not x.y.z.", 'Use three numbers, e.g. version = "1.0.0".')
    deps = list((data.get("dependencies") or {}).keys())
    modules = [m for m in (data.get("python", {}) or {}).get("module", []) if isinstance(m, dict)]
    module_names = [m["name"] for m in modules if m.get("name")]
    if not modules:
        add("info", "no_python_module", "config/extension.toml", None,
            "No [[python.module]]: this extension has no Python code Kit will import.",
            'Add [[python.module]] with name = "<your.module>" if it should run Python.')

    root = default_root()
    installed = _installed(root) if (root / "kit").is_dir() else {}
    kernel = _kernel(root)
    for dep in deps:
        if installed and dep not in installed:
            add("info", "dependency_not_in_isaac", "config/extension.toml", _line_of(toml_text, f'"{dep}"'),
                f"Dependency {dep!r} is not installed in the local Isaac Sim 6.1 (Composer may have it).",
                "Check the name in the Extension Manager (Developer > Extensions in Composer).")
    provided = _provided(deps, installed) | set(module_names)

    py_files: list[Path] = []
    bases = [ext] + [ext / m["path"] for m in modules if isinstance(m.get("path"), str)] + [ext / "pip_prebundle"]
    for name in module_names:
        parts = name.split(".")
        found_dir = next((b / Path(*parts) for b in bases if (b / Path(*parts) / "__init__.py").is_file()), None)
        found_file = next((b / Path(*parts).with_suffix(".py") for b in bases
                           if (b / Path(*parts).with_suffix(".py")).is_file()), None)
        compiled = any((b / Path(*parts[:-1])).is_dir() and any((b / Path(*parts[:-1])).glob(parts[-1] + ".*.pyd"))
                       or (b / Path(*parts)).is_dir() for b in bases)
        if found_dir is not None:
            py_files.extend(sorted(found_dir.rglob("*.py"))[:MAX_PY_FILES])
        elif found_file is not None:
            py_files.append(found_file)
        elif compiled:
            continue  # a compiled module or a namespace folder: nothing to read here
        elif any(other != name and other.replace("_", ".") == name.replace("_", ".") for other in module_names):
            continue  # an alias of a sibling module (seen: omni.ui.scene registered by omni.ui_scene)
        else:
            add("error", "module_folder_missing", "config/extension.toml", _line_of(toml_text, f'"{name}"'),
                f"[[python.module]] name = {name!r}, but there is no {'/'.join(name.split('.'))}/__init__.py in the "
                "extension folder, so Kit cannot import it.",
                "Make the folders match the dotted name (each dot = one folder level), with an __init__.py inside.")

    ext_classes: list[tuple[Path, ast.ClassDef]] = []
    exposed: set[str] = set()
    star_from: set[str] = set()
    imported: set[str] = set()  # last part of every module imported anywhere in the package
    for path in py_files:
        rel = str(path.relative_to(ext)).replace("\\", "/")
        text = path.read_text(encoding="utf-8-sig", errors="replace")  # some files start with a byte-order mark
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            add("error", "python_syntax", rel, exc.lineno, f"Python syntax error: {exc.msg}. Kit stops loading here.",
                "Fix that line first.")
            continue
        if path.name == "__init__.py" and not rel.split("/")[-2:-1] == ["tests"]:
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name == "*":
                            star_from.add((node.module or "").split(".")[-1])
                        else:
                            exposed.add(alias.asname or alias.name)
                elif isinstance(node, ast.ClassDef):
                    exposed.add(node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])  # from .extension import X / from .impl.extension import *
            if isinstance(node, ast.ClassDef) and "/tests/" not in f"/{rel}" and any(
                    (isinstance(b, ast.Attribute) and b.attr == "IExt") or (isinstance(b, ast.Name) and b.id == "IExt")
                    for b in node.bases):
                ext_classes.append((path, node))
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and "/tests/" not in f"/{rel}":
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                    ([node.module] if node.module and not node.level else [])
                for module in names:
                    top = module.split(".")[0]
                    if top not in ("omni", "isaacsim") or not installed:
                        continue
                    if _covered(module, kernel) or _covered(module, provided):
                        continue
                    add("info", "import_without_dependency", rel, node.lineno,
                        f"`{module}` is imported, but no dependency in extension.toml provides it. Kit may load this "
                        "extension before that module exists, and the import fails.",
                        f'Add the extension that provides it under [dependencies], e.g. "{module}" = {{}}.')
    if module_names and py_files and not ext_classes:
        add("info", "no_iext_class", "/".join(module_names[0].split(".")) + "/__init__.py", None,
            "No class subclasses omni.ext.IExt: fine for a library of functions, but nothing runs when the extension "
            "starts (no window, no button).",
            "Add class MyExtension(omni.ext.IExt) with def on_startup(self, ext_id) and def on_shutdown(self).")
    for path, cls in ext_classes:
        rel = str(path.relative_to(ext)).replace("\\", "/")
        module_file = path.stem
        # Kit only finds the class if importing the package runs its file: through __init__.py directly or a chain
        # (impl/__init__.py importing .extension, as NVIDIA's own extensions do).
        dotted = ".".join(path.relative_to(ext).with_suffix("").parts)
        listed = any(dotted.endswith(name) for name in module_names)  # the file is a [[python.module]] itself
        if path.name != "__init__.py" and cls.name not in exposed and module_file not in star_from                 and module_file not in imported and not listed:
            add("warning", "iext_not_exposed", rel, cls.lineno,
                f"class {cls.name} is in {path.name}, but no __init__.py imports that file, so Kit may never find it.",
                f"In the package's __init__.py write: from .{module_file} import {cls.name}")
        methods = {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        start = methods.get("on_startup")
        # Kit calls on_startup(ext_id), and also accepts on_startup(self) (many NVIDIA extensions use it).
        if start is not None and not start.args.vararg and len(start.args.args) > 2:
            add("error", "on_startup_signature", rel, start.lineno,
                f"on_startup takes {len(start.args.args)} parameters; Kit gives it at most the extension id.",
                "Write def on_startup(self, ext_id):")
        stop = methods.get("on_shutdown")
        if stop is not None and not stop.args.vararg and len(stop.args.args) > 1:
            add("error", "on_shutdown_signature", rel, stop.lineno,
                f"on_shutdown takes {len(stop.args.args)} parameter(s); Kit calls it with none.",
                "Write def on_shutdown(self):")
        if start is not None and stop is None:
            add("info", "no_on_shutdown", rel, cls.lineno,
                "There is on_startup but no on_shutdown: what startup made (windows, subscriptions) stays behind when "
                "the extension is switched off or hot-reloaded.", "Add def on_shutdown(self) that destroys/clears it.")
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        makes_window = start is not None and re.search(r"\bui\.Window\(|omni\.ui\.Window\(|Window\(", ast.get_source_segment(
            source, start) or "")
        stop_text = ast.get_source_segment(source, stop) or "" if stop is not None else ""
        if makes_window and stop is not None and not re.search(r"destroy\(|=\s*None", stop_text):
            add("info", "window_not_destroyed", rel, stop.lineno,
                "on_startup makes a window, but on_shutdown neither destroys it nor clears it: after each hot reload "
                "an old copy stays open.", "In on_shutdown: self._window.destroy() then self._window = None.")
    return _report(ext, problems, {"name": ext.name, "version": version, "dependencies": deps,
                                   "python_modules": module_names, "python_files": len(py_files),
                                   "extension_classes": [c.name for _, c in ext_classes]})


def _report(ext: Path, problems: list[dict[str, Any]], facts: dict[str, Any]) -> dict[str, Any]:
    order = {"error": 0, "warning": 1, "info": 2}
    problems.sort(key=lambda p: (order[p["severity"]], p["file"], p["line"] or 0))
    errors = sum(p["severity"] == "error" for p in problems)
    head = problems[0] if problems else None
    return {"ok": True, "folder": str(ext), "verdict": "errors" if errors else ("warnings" if problems else "ok"),
            **facts, "problems": problems,
            "next_step": (f"{head['file']}{':' + str(head['line']) if head['line'] else ''}: {head['message']} "
                          f"Fix: {head['fix']}" if head else
                          "No common mistake found. Enable it in the Extension Manager and watch the Console for "
                          "errors."),
            "note": "Static check: nothing was run. Kit-specific behaviour still needs a live test."}
