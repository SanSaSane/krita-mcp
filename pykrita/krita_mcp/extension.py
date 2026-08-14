"""Krita extension entry point: owns the bridge's lifetime."""

import traceback

from PyQt5.QtWidgets import QMessageBox

from krita import Extension, Krita

from . import ops
from .httpserver import DEFAULT_PORT, Bridge, info_file_path
from .mainthread import MainThreadInvoker

SETTINGS_GROUP = "krita_mcp"


def _log(text):
    # Goes to Tools > Scripts > Scripter output and to the terminal if Krita
    # was started from one.
    print(text)


class KritaMcpExtension(Extension):

    def __init__(self, parent):
        super(KritaMcpExtension, self).__init__(parent)
        self._invoker = None
        self._bridge = None
        self._start_error = None
        self._toggle_action = None

    # -- Krita hooks ------------------------------------------------------
    def setup(self):
        # Runs on the GUI thread at startup, so the invoker binds to it.
        self._invoker = MainThreadInvoker()
        krita = Krita.instance()

        notifier = krita.notifier()
        notifier.setActive(True)
        try:
            notifier.applicationClosing.connect(self._on_closing)
        except Exception:
            pass

        if self._read_bool("autostart", True):
            self._start(quiet=True)

    def createActions(self, window):
        self._toggle_action = window.createAction(
            "krita_mcp_toggle", "Toggle MCP Bridge", "tools/scripts")
        self._toggle_action.triggered.connect(self._on_toggle)

        status = window.createAction(
            "krita_mcp_status", "MCP Bridge Status...", "tools/scripts")
        status.triggered.connect(self._on_status)

        self._refresh_action_text()

    # -- settings ---------------------------------------------------------
    def _read_bool(self, key, default):
        raw = Krita.instance().readSetting(
            SETTINGS_GROUP, key, "true" if default else "false")
        return str(raw).strip().lower() in ("true", "1", "yes")

    def _read_port(self):
        raw = Krita.instance().readSetting(SETTINGS_GROUP, "port", str(DEFAULT_PORT))
        try:
            port = int(str(raw).strip())
        except ValueError:
            return DEFAULT_PORT
        return port if 1024 <= port <= 65535 else DEFAULT_PORT

    # -- bridge -----------------------------------------------------------
    def _start(self, quiet=False):
        if self._bridge is not None and self._bridge.running:
            return True
        krita = Krita.instance()
        try:
            # Everything is looked up through the module rather than bound
            # here, so `importlib.reload(krita_mcp.ops)` from run_python picks
            # up edited operations without restarting Krita.
            self._bridge = Bridge(
                invoker=self._invoker,
                dispatch=lambda name, params: ops.dispatch(name, params),
                timeout_for=lambda name: ops.op_timeout(name),
                plugin_version=ops.PLUGIN_VERSION,
                krita_version=krita.version(),
                operations=lambda: list(ops.OPS),
                logger=_log,
            )
            self._bridge.start(self._read_port())
            self._start_error = None
            self._refresh_action_text()
            return True
        except Exception as exc:
            self._start_error = "{0}: {1}".format(type(exc).__name__, exc)
            _log("[krita-mcp] failed to start:\n" + traceback.format_exc())
            self._bridge = None
            self._refresh_action_text()
            if not quiet:
                self._message("Could not start the MCP bridge", self._start_error)
            return False

    def _stop(self):
        if self._bridge is not None:
            self._bridge.stop()
            self._bridge = None
        self._refresh_action_text()

    def _on_closing(self):
        self._stop()

    def _on_toggle(self):
        if self._bridge is not None and self._bridge.running:
            self._stop()
            self._message("MCP bridge stopped",
                          "Krita is no longer reachable from the MCP server.")
        elif self._start():
            self._message(
                "MCP bridge started",
                "Listening on http://127.0.0.1:{0}".format(self._bridge.port))

    def _on_status(self):
        if self._bridge is not None and self._bridge.running:
            body = (
                "Running on http://127.0.0.1:{0}\n"
                "Operations: {1}\n"
                "Plugin version: {2}\n\n"
                "Connection details for the MCP server are in:\n{3}"
            ).format(self._bridge.port, len(ops.OPS), ops.PLUGIN_VERSION,
                     info_file_path())
        else:
            body = "Not running."
            if self._start_error:
                body += "\n\nLast error:\n" + self._start_error
        self._message("Krita MCP bridge", body)

    def _refresh_action_text(self):
        if self._toggle_action is None:
            return
        running = self._bridge is not None and self._bridge.running
        self._toggle_action.setText(
            "Stop MCP Bridge (port {0})".format(self._bridge.port) if running
            else "Start MCP Bridge")

    def _message(self, title, body):
        try:
            box = QMessageBox(Krita.instance().activeWindow().qwindow()
                              if Krita.instance().activeWindow() else None)
            box.setWindowTitle(title)
            box.setText(body)
            box.exec_()
        except Exception:
            _log("[krita-mcp] {0}: {1}".format(title, body))
