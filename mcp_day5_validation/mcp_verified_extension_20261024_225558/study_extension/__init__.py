import omni.ext
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
