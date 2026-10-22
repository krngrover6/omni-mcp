"""pxr is an optional extra ([usd]): every test that needs it must skip, not fail, when it is missing.

Static checks over tests/test_*.py (nothing is imported or run), so they hold with and without pxr or pytest:
module-level guards raise unittest.SkipTest (pytest.skip needs pytest, which the stdlib runner does not), a
module-level pxr import sits inside such a guard, and a test that imports pxr in its body checks first.
"""

from __future__ import annotations

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def _modules() -> list[tuple[str, ast.Module]]:
    return [(path.name, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(TESTS_DIR.glob("test_*.py"))]


def _imports_pxr(node: ast.AST) -> bool:
    if isinstance(node, ast.ImportFrom):
        return (node.module or "").split(".")[0] == "pxr" and not node.level
    if isinstance(node, ast.Import):
        return any(alias.name.split(".")[0] == "pxr" for alias in node.names)
    return False


def _guarded_pxr_try(node: ast.AST) -> bool:
    """A module-level try that imports pxr and handles ImportError."""
    if not isinstance(node, ast.Try) or not any(_imports_pxr(item) for item in node.body):
        return False
    return any(
        handler.type is not None and "ImportError" in ast.unparse(handler.type) for handler in node.handlers
    )


def test_module_level_pxr_guards_skip_with_unittest_not_pytest():
    for name, tree in _modules():
        for node in tree.body:
            if not _guarded_pxr_try(node):
                continue
            handler = next(h for h in node.handlers if "ImportError" in ast.unparse(h.type))
            text = ast.unparse(handler)
            assert "pytest.skip" not in text, f"{name}: use 'raise unittest.SkipTest(...)'; pytest may be missing"


def test_module_level_pxr_imports_are_guarded():
    for name, tree in _modules():
        guarded = False
        for node in tree.body:
            if _guarded_pxr_try(node):
                guarded = True
            elif _imports_pxr(node):
                assert guarded, f"{name}:{node.lineno} imports pxr at module level without an ImportError guard"


def test_tests_that_import_pxr_in_their_body_check_for_it_first():
    for name, tree in _modules():
        if any(_guarded_pxr_try(node) for node in tree.body):
            continue  # the whole module already skips (or degrades) without pxr
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
                continue
            tried = {id(inner) for block in ast.walk(node) if isinstance(block, ast.Try) for inner in ast.walk(block)}
            imports = [inner for inner in ast.walk(node) if _imports_pxr(inner) and id(inner) not in tried]
            if not imports:
                continue
            first = min(inner.lineno for inner in imports)
            checks = [
                inner for inner in ast.walk(node)
                if isinstance(inner, ast.Call) and ast.unparse(inner.func) in {"_require_pxr", "_has_pxr"}
                and inner.lineno < first
            ]
            assert checks, f"{name}::{node.name} imports pxr without _require_pxr() or a _has_pxr() check first"
