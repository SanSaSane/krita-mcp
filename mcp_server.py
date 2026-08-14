#!/usr/bin/env python3
"""MCP stdio server that drives Krita through the in-app HTTP bridge.

Standard library only: nothing to install, nothing to fail at startup.

Transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, so stdout is
reserved exclusively for protocol messages -- all logging goes to stderr.

Run `python mcp_server.py --selftest` to exercise the bridge from a shell.
"""

import argparse
import base64
import http.client
import json
import os
import socket
import sys
import threading
import time

SERVER_NAME = "krita"
SERVER_VERSION = "1.0.0"

SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
PREFERRED_PROTOCOL = "2025-06-18"

HEALTH_TIMEOUT = 3.0
CALL_TIMEOUT = 315.0  # backstop only; the bridge enforces its own per-op limit


def log(message):
    sys.stderr.write("[krita-mcp] {0}\n".format(message))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# bridge client
# ---------------------------------------------------------------------------

class BridgeUnavailable(Exception):
    pass


class BridgeError(Exception):
    """The bridge answered, but the operation failed."""

    def __init__(self, kind, message, detail=None):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def state_dir():
    """Krita's per-user data directory -- must match the plugin's copy."""
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
    override = os.environ.get("KRITA_MCP_INFO_FILE")
    if override:
        return override
    return os.path.join(state_dir(), "krita_mcp_bridge.json")


NOT_RUNNING_HELP = (
    "Could not reach the Krita MCP bridge.\n"
    "  1. Is Krita running?\n"
    "  2. Is the bridge plugin enabled? Settings > Configure Krita > Python "
    "Plugin Manager > 'MCP Bridge', then restart Krita.\n"
    "  3. Check Tools > Scripts > 'MCP Bridge Status...' inside Krita.\n"
    "Connection details are read from: {path}"
)


class BridgeClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._info = None

    # -- discovery --------------------------------------------------------
    def _load_info(self, force=False):
        if self._info is not None and not force:
            return self._info

        host = os.environ.get("KRITA_MCP_HOST", "127.0.0.1")
        port = os.environ.get("KRITA_MCP_PORT")
        token = os.environ.get("KRITA_MCP_TOKEN")
        if port and token:
            self._info = {"host": host, "port": int(port), "token": token}
            return self._info

        path = info_file_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            raise BridgeUnavailable(NOT_RUNNING_HELP.format(path=path))
        except (OSError, ValueError) as exc:
            raise BridgeUnavailable(
                "The bridge file at {0} could not be read ({1}). Restart Krita "
                "to rewrite it.".format(path, exc))

        if not data.get("port") or not data.get("token"):
            raise BridgeUnavailable(
                "The bridge file at {0} is missing the port or token. Restart "
                "Krita.".format(path))
        self._info = {"host": host, "port": int(data["port"]),
                      "token": data["token"]}
        return self._info

    # -- requests ---------------------------------------------------------
    def _request(self, method, path, body, timeout, info):
        conn = http.client.HTTPConnection(info["host"], info["port"],
                                          timeout=timeout)
        try:
            headers = {"Connection": "close"}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
                headers["Content-Length"] = str(len(payload))
                headers["X-Krita-MCP-Token"] = info["token"]
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        finally:
            try:
                conn.close()
            except Exception:
                pass

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BridgeError(
                "bad_response",
                "The bridge returned HTTP {0} with a body that is not JSON."
                .format(status),
                raw[:500].decode("utf-8", "replace"))
        return status, parsed

    def call(self, op, params=None, timeout=CALL_TIMEOUT):
        body = {"op": op, "params": params or {}}
        last_exc = None

        # Two passes: if the first fails at the socket or auth layer, Krita has
        # probably restarted onto a new port/token, so re-read the info file.
        for attempt in (0, 1):
            with self._lock:
                info = self._load_info(force=(attempt == 1))
            try:
                status, parsed = self._request("POST", "/rpc", body, timeout, info)
            except OSError as exc:  # covers refused, reset and timed out
                last_exc = exc
                continue

            if status == 401 and attempt == 0:
                with self._lock:
                    self._info = None
                continue

            if parsed.get("ok"):
                return parsed.get("result")
            error = parsed.get("error") or {}
            raise BridgeError(error.get("type", "error"),
                              error.get("message", "The operation failed."),
                              error.get("detail"))

        raise BridgeUnavailable(
            "{0}\n\nUnderlying error: {1}: {2}".format(
                NOT_RUNNING_HELP.format(path=info_file_path()),
                type(last_exc).__name__, last_exc))

    def health(self):
        with self._lock:
            info = self._load_info(force=True)
        try:
            status, parsed = self._request("GET", "/health", None,
                                           HEALTH_TIMEOUT, info)
        except OSError as exc:
            raise BridgeUnavailable(
                "{0}\n\nUnderlying error: {1}: {2}".format(
                    NOT_RUNNING_HELP.format(path=info_file_path()),
                    type(exc).__name__, exc))
        if status != 200 or not parsed.get("ok"):
            raise BridgeUnavailable(
                "The bridge answered HTTP {0}: {1}".format(status, parsed))
        return parsed


BRIDGE = BridgeClient()


# ---------------------------------------------------------------------------
# tool definitions
# ---------------------------------------------------------------------------

DOCUMENT_PROP = {
    "type": ["string", "integer"],
    "description": ("Which open document: its index from `status`, part of its "
                    "name or file name, or omit for the active one."),
}

LAYER_PROP = {
    "type": "string",
    "description": ("Which layer: a name (\"Sky\"), a path through groups "
                    "(\"Background/Sky\"), a top-first index path (\"#0/#1\"), "
                    "\"uuid:<id>\", or omit for the active layer."),
}

REGION_PROP = {
    "type": "object",
    "description": "Pixel rectangle on the canvas.",
    "properties": {
        "x": {"type": "integer", "default": 0},
        "y": {"type": "integer", "default": 0},
        "width": {"type": "integer"},
        "height": {"type": "integer"},
    },
    "required": ["width", "height"],
}

DRAW_COMMAND = {
    "type": "object",
    "description": (
        "One drawing primitive. `type` decides which other fields apply:\n"
        "- fill_rect: x, y, w, h, color\n"
        "- rect: x, y, w, h, plus optional fill, color (outline), "
        "stroke_width, radius (rounded corners)\n"
        "- ellipse: x, y, w, h (bounding box), fill, color, stroke_width\n"
        "- circle: cx, cy, radius, fill, color, stroke_width\n"
        "- line: x1, y1, x2, y2, color, stroke_width, cap (flat|square|round)\n"
        "- polyline / polygon: points [[x,y],...], color, fill, stroke_width, "
        "close\n"
        "- text: x, y, text, size (pixels), color, font, bold, italic, "
        "anchor (top-left|baseline|center); \\n starts a new line\n"
        "- linear_gradient: x, y, w, h, stops [[0,\"#000\"],[1,\"#fff\"]], "
        "direction (horizontal|vertical|diagonal)\n"
        "- radial_gradient: x, y, w, h, stops\n"
        "- image: x, y, w, h, data (base64 PNG/JPEG) to paste a bitmap\n"
        "- clear: x, y, w, h to erase back to transparency\n"
        "Colours accept #rrggbb, #rrggbbaa, SVG names, or [r,g,b,a]."
    ),
    "properties": {
        "type": {
            "type": "string",
            "enum": ["fill_rect", "rect", "ellipse", "circle", "line",
                     "polyline", "polygon", "text", "linear_gradient",
                     "radial_gradient", "image", "clear"],
        },
        "x": {"type": "number"}, "y": {"type": "number"},
        "w": {"type": "number"}, "h": {"type": "number"},
        "x1": {"type": "number"}, "y1": {"type": "number"},
        "x2": {"type": "number"}, "y2": {"type": "number"},
        "cx": {"type": "number"}, "cy": {"type": "number"},
        "radius": {"type": "number"},
        "color": {"type": ["string", "array"]},
        "fill": {"type": ["string", "array"]},
        "stroke_width": {"type": "number", "default": 1},
        "cap": {"type": "string", "enum": ["flat", "square", "round"]},
        "join": {"type": "string", "enum": ["miter", "bevel", "round"]},
        "close": {"type": "boolean"},
        "points": {"type": "array", "items": {
            "type": "array", "items": {"type": "number"},
            "minItems": 2, "maxItems": 2}},
        "text": {"type": "string"},
        "size": {"type": "integer"},
        "font": {"type": "string"},
        "bold": {"type": "boolean"},
        "italic": {"type": "boolean"},
        "anchor": {"type": "string",
                   "enum": ["top-left", "baseline", "center"]},
        "direction": {"type": "string",
                      "enum": ["horizontal", "vertical", "diagonal"]},
        "stops": {"type": "array", "items": {"type": "array"}},
        "data": {"type": "string", "description": "base64 image bytes"},
    },
    "required": ["type"],
}


def tool(name, description, properties, required=None, op=None, image=False,
         transform=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        "_op": op or name,
        "_image": image,
        "_transform": transform,
    }


def _transform_image(args):
    action = args.pop("action")
    mapping = {
        "resize_canvas": "resize_canvas",
        "scale": "scale_image",
        "rotate": "rotate_image",
        "crop": "crop_image",
        "flatten": "flatten_image",
    }
    if action not in mapping:
        raise BridgeError("invalid_request",
                          "action must be one of: " + ", ".join(mapping))
    return mapping[action], args


def _list_capabilities(args):
    kind = args.pop("kind", "filters")
    if kind == "filters":
        return "list_filters", args
    if kind == "blending_modes":
        return "list_blending_modes", args
    raise BridgeError("invalid_request",
                      "kind must be 'filters' or 'blending_modes'")


TOOLS = [
    tool("status",
         "Krita's version plus every open document (index, size, colour "
         "space, unsaved state). Start here to see what you are working with.",
         {}),

    tool("inspect_document",
         "Full detail for one document: canvas size, resolution, colour "
         "space, the whole layer tree in docker order (top first), and the "
         "current selection.",
         {"document": DOCUMENT_PROP,
          "max_depth": {"type": "integer", "default": -1,
                        "description": "Group nesting to descend; -1 for all."}},
         op="document_info"),

    tool("create_document",
         "Create a new document and open it in a Krita view.",
         {"width": {"type": "integer"},
          "height": {"type": "integer"},
          "name": {"type": "string", "default": "Untitled"},
          "resolution_dpi": {"type": "number", "default": 300},
          "color_model": {"type": "string", "default": "RGBA",
                          "description": "RGBA, GRAYA, CMYKA, LABA, XYZA, YCbCrA"},
          "color_depth": {"type": "string", "default": "U8",
                          "description": "U8, U16, F16 or F32. Drawing needs U8."},
          "color_profile": {"type": "string", "default": "",
                            "description": "Empty for Krita's default."},
          "background": {"type": ["string", "array"],
                         "description": "Optional fill colour, e.g. #ffffff."}},
         required=["width", "height"]),

    tool("open_document",
         "Open an image file from disk in Krita.",
         {"path": {"type": "string", "description": "Absolute path."}},
         required=["path"]),

    tool("save_document",
         "Save a document. Give `path` to save-as (the format follows the "
         "extension); omit it to save over the existing file.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string"}}),

    tool("export_document",
         "Export a flattened copy to any format Krita can write (.png, .jpg, "
         ".webp, .tif, ...) without changing what the document is bound to.",
         {"document": DOCUMENT_PROP,
          "path": {"type": "string", "description": "Absolute output path."},
          "options": {"type": "object",
                      "description": "Exporter settings, e.g. {\"quality\": 90}."}},
         required=["path"]),

    tool("close_document",
         "Close a document. Refuses to discard unsaved work unless you say "
         "so. CAUTION: Krita 5.3.3 has a bug where tearing down a "
         "heavily-edited document sometimes crashes Krita itself (roughly one "
         "close in five after a long editing session; it happens through "
         "Krita's own File > Close too, and saving first does not help). "
         "Prefer leaving documents open and letting the user close them.",
         {"document": DOCUMENT_PROP,
          "save": {"type": "boolean", "default": False},
          "discard_changes": {"type": "boolean", "default": False}}),

    tool("transform_image",
         "Whole-image geometry. `action` picks the operation: resize_canvas "
         "(change the canvas, keeping layer pixels where they are), scale "
         "(resample everything), rotate (by degrees, clockwise), crop, or "
         "flatten (merge all layers into one).",
         {"document": DOCUMENT_PROP,
          "action": {"type": "string",
                     "enum": ["resize_canvas", "scale", "rotate", "crop",
                              "flatten"]},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "width": {"type": "integer"}, "height": {"type": "integer"},
          "degrees": {"type": "number"},
          "strategy": {"type": "string", "default": "Bicubic",
                       "description": "Scaling filter: Bicubic, Bilinear, "
                                      "NearestNeighbor, Lanczos3, Box."}},
         required=["action"], transform=_transform_image),

    tool("create_layer",
         "Add a layer. New layers go to the top of their parent unless you "
         "pass `index` (0 is topmost, negative counts from the bottom).",
         {"document": DOCUMENT_PROP,
          "name": {"type": "string", "default": "Layer"},
          "type": {"type": "string", "default": "paintlayer",
                   "enum": ["paintlayer", "grouplayer", "filterlayer",
                            "filllayer", "filelayer", "clonelayer",
                            "vectorlayer", "transparencymask", "filtermask",
                            "transformmask", "selectionmask", "colorizemask"]},
          "parent": {"type": "string",
                     "description": "A group layer to nest inside; omit for "
                                    "the top level."},
          "index": {"type": "integer"},
          "select": {"type": "boolean", "default": True},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string"},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"}}),

    tool("set_layer",
         "Change a layer's properties: rename it, set opacity (0-255, or 0-1), "
         "blending mode, visibility, lock, and optionally make it active.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "new_name": {"type": "string"},
          "opacity": {"type": "number"},
          "blending_mode": {"type": "string",
                            "description": "See list_capabilities."},
          "visible": {"type": "boolean"},
          "locked": {"type": "boolean"},
          "select": {"type": "boolean", "default": False,
                     "description": "Also make this the active layer."}}),

    tool("delete_layer", "Delete a layer and everything inside it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP},
         required=["layer"]),

    tool("duplicate_layer", "Copy a layer, placing the copy just above it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "name": {"type": "string"},
          "select": {"type": "boolean", "default": True}}),

    tool("move_layer",
         "Restack a layer, optionally into a different group. `index` is "
         "top-first within the new parent.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "parent": {"type": "string",
                     "description": "Target group; omit to stay put."},
          "index": {"type": "integer", "default": 0}},
         required=["layer"]),

    tool("merge_layer_down", "Merge a layer into the one directly beneath it.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP}),

    tool("get_image",
         "Render the canvas and return it as a PNG you can actually look at. "
         "Use this to check your work. Defaults to the merged image; pass "
         "`layer` for one layer in isolation.",
         {"document": DOCUMENT_PROP,
          "layer": {"type": "string",
                    "description": "Omit or \"merged\" for the composite."},
          "region": REGION_PROP,
          "max_size": {"type": "integer", "default": 1024,
                       "description": "Longest edge of the returned image; "
                                      "the canvas is never upscaled."}},
         image=True),

    tool("get_pixel", "Read the exact colour at one pixel.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "x": {"type": "integer"}, "y": {"type": "integer"}},
         required=["x", "y"]),

    tool("draw",
         "Paint shapes, text, gradients and bitmaps onto an RGBA/8-bit paint "
         "layer. Commands are drawn in order, so later ones sit on top. "
         "Note: this writes pixels directly and does not enter Krita's undo "
         "history, so draw onto a layer you can delete.",
         {"document": DOCUMENT_PROP,
          "layer": LAYER_PROP,
          "antialias": {"type": "boolean", "default": True},
          "commands": {"type": "array", "items": DRAW_COMMAND,
                       "minItems": 1}},
         required=["commands"]),

    tool("apply_filter",
         "Run one of Krita's filters over a layer, optionally limited to a "
         "region. Call list_capabilities first to see names and parameters.",
         {"document": DOCUMENT_PROP, "layer": LAYER_PROP,
          "filter": {"type": "string", "description": "e.g. blur, gaussianblur, "
                                                      "invert, desaturate."},
          "settings": {"type": "object",
                       "description": "Filter parameters; must match the "
                                      "filter's own names."},
          "region": REGION_PROP},
         required=["filter"]),

    tool("set_selection",
         "Change the active selection, which constrains filters and painting.",
         {"document": DOCUMENT_PROP,
          "mode": {"type": "string", "default": "rect",
                   "enum": ["rect", "all", "none", "invert", "grow", "shrink",
                            "feather"]},
          "x": {"type": "integer"}, "y": {"type": "integer"},
          "width": {"type": "integer"}, "height": {"type": "integer"},
          "value": {"type": "integer", "default": 255,
                    "description": "Selection strength 0-255."},
          "add": {"type": "boolean", "default": False,
                  "description": "Union with the existing selection."},
          "amount": {"type": "integer", "default": 1,
                     "description": "Pixels, for grow/shrink/feather."}}),

    tool("list_capabilities",
         "List what this Krita build supports: filter names (with their "
         "parameters) or blending mode names.",
         {"kind": {"type": "string", "default": "filters",
                   "enum": ["filters", "blending_modes"]},
          "include_parameters": {"type": "boolean", "default": False}},
         transform=_list_capabilities),

    tool("trigger_action",
         "Fire a Krita menu action by its internal id -- the escape hatch for "
         "anything with no dedicated tool. Useful ids: edit_undo, edit_redo, "
         "deselect, select_all, invert_selection.",
         {"name": {"type": "string"}},
         required=["name"]),

    tool("run_python",
         "Execute Python inside Krita with the full libkis API, for anything "
         "the other tools do not cover. `Krita`, `krita` (the instance) and "
         "`doc` (the active document) are predefined; assign to `result` to "
         "return a value. Runs on the UI thread, so keep it quick.",
         {"code": {"type": "string"}},
         required=["code"]),

    tool("self_test",
         "Verify the bridge end to end: document creation, pixel round-trip, "
         "channel order, drawing, PNG encoding and layer handling. Run this "
         "first if something looks wrong.",
         {}),
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def public_tools():
    return [{k: v for k, v in t.items() if not k.startswith("_")} for t in TOOLS]


# ---------------------------------------------------------------------------
# tool execution
# ---------------------------------------------------------------------------

def _summarise(result):
    return json.dumps(result, indent=2, ensure_ascii=False, default=str)


def call_tool(name, arguments):
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        raise BridgeError("unknown_tool",
                          "No tool named {0!r}. Available: {1}".format(
                              name, ", ".join(sorted(TOOLS_BY_NAME))))

    args = dict(arguments or {})
    op = spec["_op"]
    if spec["_transform"] is not None:
        op, args = spec["_transform"](args)

    result = BRIDGE.call(op, args)

    if spec["_image"] and isinstance(result, dict) and result.get("png_base64"):
        png = result.pop("png_base64")
        return [
            {"type": "text", "text": _summarise(result)},
            {"type": "image", "data": png, "mimeType": "image/png"},
        ]
    return [{"type": "text", "text": _summarise(result)}]


# ---------------------------------------------------------------------------
# JSON-RPC / MCP plumbing
# ---------------------------------------------------------------------------

def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


class Server:
    def __init__(self):
        self.initialized = False
        self.protocol = PREFERRED_PROTOCOL

    def handle(self, message):
        """Return a response dict, or None for notifications."""
        if not isinstance(message, dict):
            return _error(None, -32600, "Request must be a JSON object.")

        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message

        if method is None:
            # A response to something we never sent; ignore it.
            return None

        try:
            if method == "initialize":
                payload = self._initialize(message.get("params") or {})
            elif method == "notifications/initialized":
                self.initialized = True
                return None
            elif method in ("notifications/cancelled", "notifications/progress",
                            "notifications/roots/list_changed"):
                return None
            elif method == "ping":
                payload = {}
            elif method == "tools/list":
                payload = {"tools": public_tools()}
            elif method == "tools/call":
                payload = self._call(message.get("params") or {})
            elif method in ("resources/list", "resources/templates/list"):
                payload = {"resources": [], "resourceTemplates": []}
            elif method == "prompts/list":
                payload = {"prompts": []}
            elif method == "logging/setLevel":
                payload = {}
            else:
                if is_notification:
                    return None
                return _error(request_id, -32601,
                              "Method not found: {0}".format(method))
        except BridgeError as exc:
            if is_notification:
                return None
            return _error(request_id, -32603, str(exc))
        except Exception as exc:
            log("internal error handling {0}: {1!r}".format(method, exc))
            if is_notification:
                return None
            return _error(request_id, -32603,
                          "{0}: {1}".format(type(exc).__name__, exc))

        if is_notification:
            return None
        return _result(request_id, payload)

    def _initialize(self, params):
        requested = params.get("protocolVersion")
        self.protocol = (requested if requested in SUPPORTED_PROTOCOLS
                         else PREFERRED_PROTOCOL)
        return {
            "protocolVersion": self.protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Drives a running Krita instance. Call `status` to see open "
                "documents, `inspect_document` for the layer tree, `draw` to "
                "paint onto a paint layer, and `get_image` to look at the "
                "result. If nothing responds, run `self_test`."
            ),
        }

    def _call(self, params):
        name = params.get("name")
        if not isinstance(name, str):
            raise BridgeError("invalid_request", "tools/call needs a `name`.")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise BridgeError("invalid_request", "`arguments` must be an object.")

        started = time.time()
        try:
            content = call_tool(name, arguments)
        except BridgeUnavailable as exc:
            return {"content": [{"type": "text", "text": str(exc)}],
                    "isError": True}
        except BridgeError as exc:
            text = "{0}: {1}".format(exc.kind, exc)
            if exc.detail:
                text += "\n\n" + exc.detail
            return {"content": [{"type": "text", "text": text}],
                    "isError": True}
        except Exception as exc:
            log("tool {0} blew up: {1!r}".format(name, exc))
            return {"content": [{"type": "text", "text": "{0}: {1}".format(
                type(exc).__name__, exc)}], "isError": True}

        log("{0} ok in {1:.0f}ms".format(name, (time.time() - started) * 1000))
        return {"content": content, "isError": False}


def serve():
    stdin = sys.stdin
    stdout = sys.stdout
    try:  # never let the console codepage mangle protocol bytes
        stdin.reconfigure(encoding="utf-8", errors="replace")
        stdout.reconfigure(encoding="utf-8", newline="\n")
    except AttributeError:
        pass

    server = Server()
    log("ready (pid {0})".format(os.getpid()))

    while True:
        try:
            line = stdin.readline()
        except (KeyboardInterrupt, ValueError):
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except ValueError as exc:
            response = _error(None, -32700, "Parse error: {0}".format(exc))
        else:
            if isinstance(message, list):  # JSON-RPC batch
                responses = [r for r in (server.handle(m) for m in message)
                             if r is not None]
                response = responses or None
            else:
                response = server.handle(message)

        if response is None:
            continue
        try:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()
        except (BrokenPipeError, OSError):
            break

    log("stdin closed, exiting")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_selftest():
    print("bridge file: {0}".format(info_file_path()))
    try:
        health = BRIDGE.health()
    except BridgeUnavailable as exc:
        print("UNAVAILABLE\n{0}".format(exc))
        return 1
    print("health: Krita {0}, plugin {1}, {2} operations".format(
        health.get("krita_version"), health.get("plugin_version"),
        len(health.get("operations", []))))

    result = BRIDGE.call("self_test", {})
    for check in result.get("checks", []):
        print("  [{0}] {1:<22} {2}".format(
            "ok" if check["ok"] else "FAIL", check["check"], check["detail"]))
    print("{0}/{1} checks passed".format(result.get("passed"),
                                         result.get("total")))
    return 0 if result.get("all_ok") else 2


def cli_call(op, params_json, params_file=None):
    if params_file:
        try:
            # utf-8-sig: PowerShell's Set-Content writes a BOM.
            with open(params_file, "r", encoding="utf-8-sig") as handle:
                params_json = handle.read()
        except OSError as exc:
            print("could not read {0}: {1}".format(params_file, exc))
            return 1
    try:
        params = json.loads(params_json) if params_json.strip() else {}
    except ValueError as exc:
        print("params is not valid JSON: {0}".format(exc))
        return 1
    try:
        result = BRIDGE.call(op, params)
    except (BridgeError, BridgeUnavailable) as exc:
        print("ERROR: {0}".format(exc))
        return 1
    if isinstance(result, dict) and "png_base64" in result:
        data = result.pop("png_base64")
        out = os.path.abspath("krita_mcp_output.png")
        with open(out, "wb") as handle:
            handle.write(base64.b64decode(data))
        result["png_written_to"] = out
    print(json.dumps(result, indent=2, default=str))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true",
                        help="check the bridge and run its self test")
    parser.add_argument("--call", metavar="OP",
                        help="invoke one bridge operation and print the result")
    parser.add_argument("--params", metavar="JSON", default="",
                        help="JSON parameters for --call")
    parser.add_argument("--params-file", metavar="PATH",
                        help="read --call parameters from a JSON file "
                             "(avoids shell quoting)")
    parser.add_argument("--list-tools", action="store_true",
                        help="print the MCP tool names and exit")
    args = parser.parse_args()

    if args.list_tools:
        for spec in TOOLS:
            print("{0:<20} -> op {1}".format(spec["name"], spec["_op"]))
        return 0
    if args.selftest:
        return cli_selftest()
    if args.call:
        return cli_call(args.call, args.params, args.params_file)
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
