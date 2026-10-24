"""Create an inert Kit extension skeleton in a new directory; never install or enable it."""
from __future__ import annotations
from .day5_io import bundle


def extension_create(project_dir: str, name: str = 'study_extension', *, dry_run: bool = True) -> dict:
    """Preview or create a minimal Kit extension with a read-only stage-summary button; no setup or installation."""
    try:
        # bundle validates name before writing. A constant module avoids caller-supplied Python/TOML injection.
        config = '''[package]
version = "0.1.0"
title = "OpenUSD Study Extension"
description = "A minimal read-only learning extension"
category = "Examples"

[dependencies]
"omni.usd" = {}
"omni.ui" = {}

[[python.module]]
name = "study_extension"
'''
        code = '''import omni.ext
import omni.ui as ui
import omni.usd


class StudyExtension(omni.ext.IExt):
    def on_startup(self, ext_id):
        self._window = ui.Window("USD Study", width=360, height=140)
        with self._window.frame:
            with ui.VStack():
                self._label = ui.Label("Press Inspect to read the active stage.")
                ui.Button("Inspect stage", clicked_fn=self._inspect)

    def _inspect(self):
        stage = omni.usd.get_context().get_stage()
        self._label.text = stage.GetRootLayer().identifier if stage else "No active stage"

    def on_shutdown(self):
        if getattr(self, '_window', None) is not None:
            self._window.visible = False
            self._window.destroy()
        self._window = self._label = None
'''
        readme = '''# Study extension

This folder is a new extension, not an installed or enabled extension.
Add its parent folder to Kit's extension search paths only after approval, then enable it in Extension Manager.
Its Inspect button reads the active stage identifier and never edits or saves the scene.

Next lesson: understand `on_startup`, the button callback, and `on_shutdown` before adding scene edits.
'''
        return bundle(project_dir, name, {'config/extension.toml': config,
                      'study_extension/__init__.py': code, 'README.md': readme}, dry_run)
    except (ValueError, OSError, TypeError) as exc:
        return {'ok': False, 'error': str(exc), 'written': False}
