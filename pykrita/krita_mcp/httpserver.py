"""Loopback HTTP bridge that fronts the Krita operations.

Requests arrive on worker threads and are handed to the GUI thread by the
invoker.  The server binds 127.0.0.1 only and requires a shared token that is
written to a file readable just by this user, so a random page in a browser
cannot drive Krita.
"""

import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 9797
PORT_SCAN = 24
MAX_BODY = 64 * 1024 * 1024  # generous: `draw` can carry an embedded image
MAX_TIMEOUT = 300.0

_LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def state_dir():
    """Where the bridge leaves its discovery file.

    Krita's per-user data directory, so the MCP server can find it without
    being told. KRITA_MCP_STATE_DIR overrides it -- both halves read the same
    variable, which is how you point them at each other in an unusual setup.
    """
    override = os.environ.get("KRITA_MCP_STATE_DIR")
    if override:
        return override
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "krita")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/krita")
    base = (os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~/.local/share"))
    return os.path.join(base, "krita")


def info_file_path():
    return os.path.join(state_dir(), "krita_mcp_bridge.json")


def trace_file_path():
    return os.path.join(state_dir(), "krita_mcp_trace.log")


class Trace(object):
    """Append-only op log, flushed per line.

    Krita is a GUI process with no console, so when it dies there is otherwise
    no record of what it was doing. Each op writes a line before it starts and
    another when it finishes, which makes a crash show up as a start with no
    matching end.
    """

    MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, path, enabled):
        self.path = path
        self.enabled = enabled
        if not enabled:
            return
        try:
            if os.path.exists(path) and os.path.getsize(path) > self.MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass

    def write(self, text):
        if not self.enabled:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write("{0:.3f} {1}\n".format(time.time(), text))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KritaMCP/1.0"
    sys_version = ""

    # Krita's console is not a log sink; stay quiet unless something breaks.
    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        self.server.bridge.log("http: " + (fmt % args))

    # -- plumbing ---------------------------------------------------------
    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _fail(self, status, kind, message, detail=None):
        error = {"type": kind, "message": message}
        if detail:
            error["detail"] = detail
        self._send(status, {"ok": False, "error": error})

    def _is_local(self):
        return self.client_address and self.client_address[0] in _LOOPBACK

    def _authorized(self):
        supplied = self.headers.get("X-Krita-MCP-Token", "")
        return secrets.compare_digest(supplied, self.server.bridge.token)

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        if not self._is_local():
            return self._fail(403, "forbidden", "Only loopback clients are served.")
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/", "/health"):
            bridge = self.server.bridge
            return self._send(200, {
                "ok": True,
                "service": "krita-mcp",
                "plugin_version": bridge.plugin_version,
                "krita_version": bridge.krita_version,
                "pid": os.getpid(),
                "operations": sorted(bridge.operations()),
            })
        return self._fail(404, "not_found", "No route {0}".format(path))

    def do_POST(self):
        if not self._is_local():
            return self._fail(403, "forbidden", "Only loopback clients are served.")
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/rpc":
            return self._fail(404, "not_found", "No route {0}".format(path))
        if not self._authorized():
            return self._fail(
                401, "unauthorized",
                "Missing or wrong X-Krita-MCP-Token header. The token lives in "
                + info_file_path())

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail(400, "bad_request", "Content-Length is not a number.")
        if length <= 0:
            return self._fail(400, "bad_request", "Empty request body.")
        if length > MAX_BODY:
            return self._fail(413, "too_large", "Request body exceeds {0} bytes."
                              .format(MAX_BODY))

        try:
            raw = self.rfile.read(length)
        except Exception as exc:
            return self._fail(400, "bad_request", "Could not read the body: {0}"
                              .format(exc))

        try:
            message = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return self._fail(400, "bad_json", "Body is not valid JSON: {0}"
                              .format(exc))
        if not isinstance(message, dict):
            return self._fail(400, "bad_request", "Body must be a JSON object.")

        name = message.get("op")
        if not isinstance(name, str) or not name:
            return self._fail(400, "bad_request", "`op` is required.")
        params = message.get("params") or {}

        timeout = message.get("timeout")
        try:
            timeout = (self.server.bridge.timeout_for(name) if timeout is None
                       else min(MAX_TIMEOUT, max(1.0, float(timeout))))
        except (TypeError, ValueError):
            return self._fail(400, "bad_request", "`timeout` must be a number.")

        status, payload = self.server.bridge.run(name, params, timeout)
        self._send(status, payload)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # do not silently steal another bridge's port

    def __init__(self, address, handler, bridge):
        self.bridge = bridge
        super(_Server, self).__init__(address, handler)

    def handle_error(self, request, client_address):
        self.bridge.log("connection error from {0}:\n{1}".format(
            client_address, traceback.format_exc()))


class Bridge(object):
    """Owns the socket, the token, and the discovery file."""

    def __init__(self, invoker, dispatch, timeout_for, plugin_version,
                 krita_version, operations, logger=None):
        self._invoker = invoker
        self._dispatch = dispatch
        self._timeout_for = timeout_for
        self.plugin_version = plugin_version
        self.krita_version = krita_version
        self.operations = operations
        self.token = secrets.token_urlsafe(32)
        self._logger = logger or (lambda text: None)
        self._server = None
        self._thread = None
        self.port = None
        self.trace = Trace(trace_file_path(),
                           os.environ.get("KRITA_MCP_TRACE") == "1")

    # -- lifecycle --------------------------------------------------------
    def start(self, preferred_port=DEFAULT_PORT):
        if self._server is not None:
            return self.port

        last = None
        for offset in range(PORT_SCAN):
            port = preferred_port + offset
            try:
                self._server = _Server(("127.0.0.1", port), _Handler, self)
                self.port = port
                break
            except OSError as exc:
                last = exc
                continue
        if self._server is None:
            raise RuntimeError(
                "No free port in {0}-{1} for the Krita MCP bridge ({2})."
                .format(preferred_port, preferred_port + PORT_SCAN - 1, last))

        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.2},
            name="krita-mcp-http", daemon=True)
        self._thread.start()
        self._write_info_file()
        self.log("listening on http://127.0.0.1:{0}".format(self.port))
        return self.port

    def stop(self):
        if self._server is None:
            return
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None
        self._remove_info_file()
        self.log("stopped")

    @property
    def running(self):
        return self._server is not None

    # -- request handling -------------------------------------------------
    def timeout_for(self, name):
        try:
            return min(MAX_TIMEOUT, max(1.0, float(self._timeout_for(name))))
        except Exception:
            return 30.0

    def run(self, name, params, timeout):
        """Execute one operation. Always returns (status, json-able payload)."""
        from .mainthread import MainThreadError, MainThreadTimeout

        self.trace.write("--> {0} {1}".format(
            name, json.dumps(params, default=str)[:400]))
        try:
            result = self._invoker.call(
                lambda: self._dispatch(name, params), timeout=timeout)
            self.trace.write("<-- {0} ok".format(name))
            return 200, {"ok": True, "op": name, "result": result}

        except MainThreadTimeout as exc:
            self.trace.write("<-- {0} TIMEOUT".format(name))
            return 504, {"ok": False, "op": name, "error": {
                "type": "krita_busy", "message": str(exc)}}

        except MainThreadError as exc:
            self.trace.write("<-- {0} error {1}".format(name, exc.type_name))
            if exc.kind is not None:  # an OpError: the caller can act on it
                return 400, {"ok": False, "op": name, "error": {
                    "type": exc.kind, "message": exc.message}}
            self.log("op {0!r} raised:\n{1}".format(name, exc.formatted))
            return 500, {"ok": False, "op": name, "error": {
                "type": exc.type_name,
                "message": exc.message or exc.type_name,
                "detail": _tail(exc.formatted),
            }}

        except Exception as exc:  # bridge-level fault, never expected
            formatted = traceback.format_exc()
            self.log("bridge fault on {0!r}:\n{1}".format(name, formatted))
            return 500, {"ok": False, "op": name, "error": {
                "type": "bridge_error", "message": str(exc),
                "detail": _tail(formatted)}}

    # -- discovery file ---------------------------------------------------
    def _write_info_file(self):
        path = info_file_path()
        payload = {
            "url": "http://127.0.0.1:{0}".format(self.port),
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "plugin_version": self.plugin_version,
            "krita_version": self.krita_version,
            "host": socket.gethostname(),
        }
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, path)
        try:  # best effort: keep the token off other accounts on this box
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _remove_info_file(self):
        """Delete the discovery file, but only if it still describes us.

        Two Krita instances share one discovery file, and the second one to
        start overwrites it with its own port and token. If that instance then
        exits it must not delete the entry, or the instance still running
        becomes undiscoverable even though its socket is fine.
        """
        path = info_file_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                current = json.load(handle)
        except (OSError, ValueError):
            return
        if current.get("pid") != os.getpid() or current.get("port") != self.port:
            self.log("leaving the discovery file alone; it belongs to pid {0}"
                     .format(current.get("pid")))
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def log(self, text):
        try:
            self._logger("[krita-mcp] " + text)
        except Exception:
            pass


def _tail(formatted, limit=2400):
    return formatted[-limit:] if len(formatted) > limit else formatted
