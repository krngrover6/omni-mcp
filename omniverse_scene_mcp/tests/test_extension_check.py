"""extension_check: one beginner mistake per tiny extension; a correct extension is clean."""
from __future__ import annotations

import tempfile
from pathlib import Path

from omniverse_scene_mcp import features
from omniverse_scene_mcp.extension_check import check_extension

TOML = '''[package]
version = "0.1.0"
title = "Hello"

[dependencies]
"omni.ui" = {}

[[python.module]]
name = "my_company.hello"
'''
EXT = '''import omni.ext
import omni.ui as ui


class HelloExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        self._window = ui.Window("Hello", width=300, height=200)

    def on_shutdown(self):
        self._window.destroy()
        self._window = None
'''


def _make(tmp: str, toml: str = TOML, init: str = "from .extension import HelloExtension\n", ext: str = EXT,
          folder: str = "my_company/hello") -> Path:
    root = Path(tmp) / "my_company.hello"
    (root / "config").mkdir(parents=True)
    (root / "config" / "extension.toml").write_text(toml, encoding="utf-8")
    package = root / Path(folder)
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(init, encoding="utf-8")
    (package / "extension.py").write_text(ext, encoding="utf-8")
    return root


def _codes(result):
    return {p["code"] for p in result["problems"] if p["severity"] != "info"}


def test_a_correct_extension_is_clean():
    with tempfile.TemporaryDirectory() as tmp:
        result = check_extension(str(_make(tmp)))
        assert result["verdict"] in ("ok", "warnings") and _codes(result) == set(), result["problems"]
        assert result["extension_classes"] == ["HelloExtension"] and result["python_modules"] == ["my_company.hello"]
        assert check_extension(str(_make(tmp + "/b") / "config" / "extension.toml"))["ok"]  # the toml path works too


def test_each_beginner_mistake_is_found():
    cases = {
        "toml_parse": dict(toml='[package]\nversion = "0.1.0\n'),
        "no_version": dict(toml=TOML.replace('version = "0.1.0"\n', "")),
        "module_folder_missing": dict(folder="my_company/helo"),
        "iext_not_exposed": dict(init="# nothing imported\n"),
        "on_startup_signature": dict(ext=EXT.replace("def on_startup(self, ext_id)", "def on_startup(self, a, b)")),
        "on_shutdown_signature": dict(ext=EXT.replace("def on_shutdown(self)", "def on_shutdown(self, x)")),
        "python_syntax": dict(ext=EXT.replace("def on_shutdown(self):", "def on_shutdown(self)")),
    }
    for code, kwargs in cases.items():
        with tempfile.TemporaryDirectory() as tmp:
            result = check_extension(str(_make(tmp, **kwargs)))
            assert code in {p["code"] for p in result["problems"]}, (code, result["problems"])
            problem = next(p for p in result["problems"] if p["code"] == code)
            assert problem["fix"] and problem["message"]


def test_soft_hints_and_kit_patterns_that_are_fine():
    with tempfile.TemporaryDirectory() as tmp:
        no_cleanup = EXT.replace("        self._window.destroy()\n        self._window = None\n", "        pass\n")
        hints = {p["code"]: p["severity"] for p in check_extension(str(_make(tmp, ext=no_cleanup)))["problems"]}
        assert hints.get("window_not_destroyed") == "info"
    with tempfile.TemporaryDirectory() as tmp:  # on_startup(self) is accepted by Kit (many NVIDIA extensions)
        result = check_extension(str(_make(tmp, ext=EXT.replace("def on_startup(self, ext_id)", "def on_startup(self)"))))
        assert "on_startup_signature" not in _codes(result)
    with tempfile.TemporaryDirectory() as tmp:  # a chain: __init__ -> impl/__init__ -> extension
        root = _make(tmp, init="from .impl import *\n")
        impl = root / "my_company" / "hello" / "impl"
        impl.mkdir()
        (impl / "__init__.py").write_text("from .extension import *\n", encoding="utf-8")
        (root / "my_company" / "hello" / "extension.py").rename(impl / "extension.py")
        assert "iext_not_exposed" not in _codes(check_extension(str(root)))
    with tempfile.TemporaryDirectory() as tmp:
        assert not check_extension(tmp)["ok"]  # no config/extension.toml
    assert features.FEATURES["extension_check"].mutates is False
