#!/usr/bin/env python3
"""Integration test: drives mcp_server.py over real JSON-RPC stdio.

This is deliberately end to end. It spawns the MCP server as a subprocess,
speaks the protocol to it exactly as an MCP client would, and drives a live
Krita through the bridge -- so a pass means the whole chain works, not just
the pieces.

    python test_mcp.py            # run everything
    python test_mcp.py -v         # also print each tool result

Krita must be running with the MCP Bridge plugin enabled.
"""

import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
PROTOCOL = "2025-06-18"

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv

PASS, FAIL = [], []


def check(name, condition, detail=""):
    if condition:
        PASS.append(name)
        print("  [ok]   {0}".format(name))
    else:
        FAIL.append((name, detail))
        print("  [FAIL] {0}  {1}".format(name, detail))
    return bool(condition)


# Bisection aid: KRITA_MCP_TEST_UNTIL=<section> stops just before that
# section and leaves the test document open, so the next step can be probed
# from outside.
UNTIL = os.environ.get("KRITA_MCP_TEST_UNTIL")


class StopTest(Exception):
    pass


def section(title):
    if UNTIL and title == UNTIL:
        raise StopTest(title)
    print("\n=== {0} ===".format(title))


class Client:
    """Minimal MCP client speaking newline-delimited JSON-RPC over pipes."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
        )
        self._inbox = queue.Queue()
        self.stderr_lines = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._next_id = 0

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self._inbox.put(line)
        self._inbox.put(None)

    def _read_stderr(self):
        for line in self.proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def send_raw(self, text):
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=120.0):
        self._next_id += 1
        request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send_raw(json.dumps(message))
        return self.read(timeout, expect_id=request_id)

    def notify(self, method, params=None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send_raw(json.dumps(message))

    def read(self, timeout=120.0, expect_id=None):
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise AssertionError(
                    "timed out waiting for a reply (id={0})".format(expect_id))
            try:
                line = self._inbox.get(timeout=remaining)
            except queue.Empty:
                raise AssertionError("timed out (id={0})".format(expect_id))
            if line is None:
                raise AssertionError(
                    "server exited. stderr:\n" + "\n".join(self.stderr_lines))
            message = json.loads(line)
            if expect_id is None or message.get("id") == expect_id:
                return message

    def call(self, name, arguments=None, timeout=120.0):
        reply = self.request("tools/call",
                             {"name": name, "arguments": arguments or {}},
                             timeout=timeout)
        return reply.get("result", reply)

    def ok(self, name, arguments=None, timeout=120.0):
        """Call a tool, assert it succeeded, return the parsed payload."""
        result = self.call(name, arguments, timeout)
        if result.get("isError"):
            raise AssertionError("{0} failed: {1}".format(
                name, _text_of(result)))
        payload = _text_of(result)
        if VERBOSE:
            print("    {0} -> {1}".format(name, payload[:400]))
        try:
            return json.loads(payload), result
        except ValueError:
            return payload, result

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _text_of(result):
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return ""


def _image_of(result):
    for block in result.get("content", []):
        if block.get("type") == "image":
            return block
    return None


def main():
    tmpdir = tempfile.mkdtemp(prefix="krita-mcp-test-")
    client = Client()
    doc_name = "mcp integration test"

    try:
        # ---------------------------------------------------------------
        section("protocol handshake")
        reply = client.request("initialize", {
            "protocolVersion": PROTOCOL,
            "capabilities": {},
            "clientInfo": {"name": "test_mcp", "version": "1.0"},
        }, timeout=30)
        init = reply.get("result", {})
        check("initialize returns a result", "result" in reply, str(reply)[:300])
        check("protocol version echoed",
              init.get("protocolVersion") == PROTOCOL,
              str(init.get("protocolVersion")))
        check("declares tools capability",
              "tools" in init.get("capabilities", {}), str(init.get("capabilities")))
        check("serverInfo present",
              init.get("serverInfo", {}).get("name") == "krita",
              str(init.get("serverInfo")))

        client.notify("notifications/initialized")

        reply = client.request("ping", {}, timeout=15)
        check("ping answered", reply.get("result") == {}, str(reply)[:200])

        reply = client.request("tools/list", {}, timeout=15)
        tools = reply.get("result", {}).get("tools", [])
        check("tools/list returns tools", len(tools) >= 20, str(len(tools)))
        check("every tool has a schema",
              all(t.get("inputSchema", {}).get("type") == "object" for t in tools))
        check("no private keys leak",
              not any(k.startswith("_") for t in tools for k in t))
        names = {t["name"] for t in tools}

        reply = client.request("nonexistent/method", {}, timeout=15)
        check("unknown method -> -32601",
              reply.get("error", {}).get("code") == -32601, str(reply)[:200])

        client.send_raw("{not json at all")
        reply = client.read(timeout=15)
        check("malformed JSON -> parse error",
              reply.get("error", {}).get("code") == -32700, str(reply)[:200])

        client.notify("notifications/cancelled", {"requestId": 999})
        reply = client.request("ping", {}, timeout=15)
        check("notifications produce no reply and do not desync",
              reply.get("result") == {}, str(reply)[:200])

        # ---------------------------------------------------------------
        section("connection and status")
        status, _ = client.ok("status")
        check("status reports Krita version",
              str(status.get("krita_version", "")).startswith(("5.", "6.")),
              str(status.get("krita_version")))

        selftest, _ = client.ok("self_test")
        check("self_test all green", selftest.get("all_ok"),
              json.dumps(selftest.get("checks"))[:400])

        # An earlier aborted run may have left its document open, which would
        # make every lookup by name ambiguous.
        removed = 0
        for _ in range(10):
            status, _ = client.ok("status")
            stale = [i for i, d in enumerate(status.get("documents", []))
                     if d.get("name") == doc_name]
            if not stale:
                break
            client.ok("close_document", {"document": stale[0],
                                         "discard_changes": True})
            removed += 1
        if removed:
            print("    (closed {0} leftover document(s))".format(removed))

        # ---------------------------------------------------------------
        section("document lifecycle")
        doc, _ = client.ok("create_document", {
            "width": 400, "height": 300, "name": doc_name,
            "background": "#202830", "resolution_dpi": 96,
        })
        check("document created 400x300",
              doc.get("width") == 400 and doc.get("height") == 300, str(doc))
        check("document has a view", doc.get("view_added") is True, str(doc))

        info, _ = client.ok("inspect_document", {"document": doc_name})
        check("inspect_document lists layers",
              len(info.get("layers", [])) >= 1,
              json.dumps(info.get("layers"))[:300])

        px, _ = client.ok("get_pixel", {"document": doc_name, "x": 5, "y": 5})
        check("background colour applied", px.get("hex") == "#202830ff",
              str(px))

        # ---------------------------------------------------------------
        section("layers")
        client.ok("create_layer", {"document": doc_name, "name": "Art",
                                   "type": "paintlayer"})
        client.ok("create_layer", {"document": doc_name, "name": "Group",
                                   "type": "grouplayer"})
        client.ok("create_layer", {"document": doc_name, "name": "Nested",
                                   "type": "paintlayer", "parent": "Group"})

        info, _ = client.ok("inspect_document", {"document": doc_name})
        paths = _paths(info["layers"])
        check("nested layer addressable by path", "Group/Nested" in paths,
              str(paths))
        check("new layers land on top", info["layers"][0]["name"] == "Group",
              str(paths))

        client.ok("set_layer", {"document": doc_name, "layer": "Art",
                                "opacity": 200, "blending_mode": "multiply",
                                "visible": True, "select": True})
        info, _ = client.ok("inspect_document", {"document": doc_name})
        art = _find(info["layers"], "Art")
        check("layer opacity set", art and art["opacity"] == 200, str(art))
        check("blending mode set", art and art["blending_mode"] == "multiply",
              str(art))
        check("layer became active", info.get("active_layer") == "Art",
              str(info.get("active_layer")))

        client.ok("duplicate_layer", {"document": doc_name, "layer": "Art",
                                      "name": "Art copy"})
        info, _ = client.ok("inspect_document", {"document": doc_name})
        check("duplicate created", "Art copy" in _paths(info["layers"]),
              str(_paths(info["layers"])))

        client.ok("move_layer", {"document": doc_name, "layer": "Art copy",
                                 "parent": "Group", "index": 0})
        info, _ = client.ok("inspect_document", {"document": doc_name})
        check("layer reparented into group",
              "Group/Art copy" in _paths(info["layers"]),
              str(_paths(info["layers"])))

        client.ok("delete_layer", {"document": doc_name,
                                   "layer": "Group/Art copy"})
        info, _ = client.ok("inspect_document", {"document": doc_name})
        check("layer deleted",
              "Group/Art copy" not in _paths(info["layers"]),
              str(_paths(info["layers"])))

        # ---------------------------------------------------------------
        section("drawing")
        drawn, _ = client.ok("draw", {
            "document": doc_name, "layer": "Art",
            "commands": [
                {"type": "fill_rect", "x": 0, "y": 0, "w": 400, "h": 300,
                 "color": "#ffffff"},
                {"type": "rect", "x": 20, "y": 20, "w": 120, "h": 80,
                 "fill": "#e94f37", "color": "#1b1b1e", "stroke_width": 4,
                 "radius": 10},
                {"type": "ellipse", "x": 170, "y": 20, "w": 100, "h": 80,
                 "fill": "#3f88c5"},
                {"type": "circle", "cx": 330, "cy": 60, "radius": 40,
                 "fill": "#44bba4", "color": "#000000", "stroke_width": 2},
                {"type": "line", "x1": 20, "y1": 130, "x2": 380, "y2": 130,
                 "color": "#1b1b1e", "stroke_width": 3, "cap": "round"},
                {"type": "polygon",
                 "points": [[40, 160], [110, 160], [75, 230]],
                 "fill": "#f6ae2d", "color": "#1b1b1e", "stroke_width": 2},
                {"type": "polyline",
                 "points": [[140, 230], [180, 170], [220, 220], [260, 165]],
                 "color": "#e94f37", "stroke_width": 3},
                {"type": "linear_gradient", "x": 280, "y": 160, "w": 100,
                 "h": 70, "direction": "diagonal",
                 "stops": [[0, "#3f88c5"], [1, "#f6ae2d"]]},
                {"type": "radial_gradient", "x": 20, "y": 240, "w": 60,
                 "h": 50, "stops": [[0, "#ffffff"], [1, "#44bba4"]]},
                {"type": "text", "x": 100, "y": 250, "text": "Krita MCP\nline two",
                 "size": 20, "color": "#1b1b1e", "bold": True},
                {"type": "clear", "x": 360, "y": 260, "w": 30, "h": 30},
            ],
        })
        check("all 11 draw commands ran", drawn.get("drawn") == 11, str(drawn))

        px, _ = client.ok("get_pixel", {"document": doc_name, "layer": "Art",
                                        "x": 80, "y": 60})
        check("rect fill colour is exact", px.get("hex") == "#e94f37ff", str(px))

        px, _ = client.ok("get_pixel", {"document": doc_name, "layer": "Art",
                                        "x": 220, "y": 60})
        check("ellipse fill colour is exact", px.get("hex") == "#3f88c5ff",
              str(px))

        px, _ = client.ok("get_pixel", {"document": doc_name, "layer": "Art",
                                        "x": 375, "y": 275})
        check("clear left transparency", px.get("rgba", [1, 1, 1, 1])[3] == 0,
              str(px))

        # base64 image paste round-trip
        tiny = base64.b64encode(_tiny_png()).decode("ascii")
        client.ok("draw", {"document": doc_name, "layer": "Art",
                           "commands": [{"type": "image", "x": 300, "y": 250,
                                         "w": 20, "h": 20, "data": tiny}]})
        px, _ = client.ok("get_pixel", {"document": doc_name, "layer": "Art",
                                        "x": 310, "y": 260})
        check("pasted image pixels landed", px.get("hex") == "#ff00ffff", str(px))

        # ---------------------------------------------------------------
        section("reading the canvas")
        payload, result = client.ok("get_image", {"document": doc_name,
                                                  "max_size": 300})
        image = _image_of(result)
        check("get_image returns an image block", image is not None)
        check("image is png", image and image.get("mimeType") == "image/png")
        if image:
            raw = base64.b64decode(image["data"])
            check("png magic bytes", raw[:8] == b"\x89PNG\r\n\x1a\n",
                  repr(raw[:8]))
            check("image downscaled to max_size",
                  payload["returned_size"]["width"] == 300,
                  str(payload.get("returned_size")))
        check("text block accompanies the image",
              payload.get("source") == "merged", str(payload.get("source")))

        payload, result = client.ok("get_image", {
            "document": doc_name, "layer": "Art",
            "region": {"x": 10, "y": 10, "width": 100, "height": 100}})
        check("region read honoured",
              payload["region"] == {"x": 10, "y": 10, "width": 100,
                                    "height": 100},
              str(payload.get("region")))

        # ---------------------------------------------------------------
        section("filters and selection")
        caps, _ = client.ok("list_capabilities", {"kind": "filters"})
        check("filters listed", caps.get("count", 0) > 10, str(caps.get("count")))
        filters = set(caps.get("filters", []))

        caps, _ = client.ok("list_capabilities", {"kind": "blending_modes"})
        check("blending modes listed", caps.get("count", 0) > 10,
              str(caps.get("count")))

        if "invert" in filters:
            client.ok("apply_filter", {"document": doc_name, "layer": "Art",
                                       "filter": "invert"})
            px, _ = client.ok("get_pixel", {"document": doc_name,
                                            "layer": "Art", "x": 80, "y": 60})
            check("invert filter changed pixels",
                  px.get("hex") == "#16b0c8ff", str(px))
            client.ok("apply_filter", {"document": doc_name, "layer": "Art",
                                       "filter": "invert"})

        if "invert" in filters:
            # A regional filter must leave everything outside the rect alone.
            before_in, _ = client.ok("get_pixel", {"document": doc_name,
                                                   "layer": "Art",
                                                   "x": 80, "y": 60})
            before_out, _ = client.ok("get_pixel", {"document": doc_name,
                                                    "layer": "Art",
                                                    "x": 220, "y": 60})
            client.ok("apply_filter", {
                "document": doc_name, "layer": "Art", "filter": "invert",
                "region": {"x": 0, "y": 0, "width": 150, "height": 300}})
            after_in, _ = client.ok("get_pixel", {"document": doc_name,
                                                  "layer": "Art",
                                                  "x": 80, "y": 60})
            after_out, _ = client.ok("get_pixel", {"document": doc_name,
                                                   "layer": "Art",
                                                   "x": 220, "y": 60})
            check("regional filter changed pixels inside the region",
                  after_in["hex"] != before_in["hex"],
                  "{0} -> {1}".format(before_in["hex"], after_in["hex"]))
            check("regional filter left pixels outside untouched",
                  after_out["hex"] == before_out["hex"],
                  "{0} -> {1}".format(before_out["hex"], after_out["hex"]))
            client.ok("apply_filter", {
                "document": doc_name, "layer": "Art", "filter": "invert",
                "region": {"x": 0, "y": 0, "width": 150, "height": 300}})

            got, _ = client.ok("apply_filter", {"document": doc_name,
                                                "layer": "Art",
                                                "filter": "invert"})
            check("whole-layer filter goes through undo history",
                  got.get("undoable") is True, str(got))
            client.ok("apply_filter", {"document": doc_name, "layer": "Art",
                                       "filter": "invert"})

        if "blur" in filters:
            got, _ = client.ok("apply_filter", {
                "document": doc_name, "layer": "Art", "filter": "blur",
                "region": {"x": 0, "y": 0, "width": 200, "height": 150}})
            check("regional filter flagged as not undoable",
                  got.get("undoable") is False, str(got))

        sel, _ = client.ok("set_selection", {"document": doc_name,
                                             "mode": "rect", "x": 10, "y": 10,
                                             "width": 50, "height": 50})
        check("rect selection made",
              sel.get("selection", {}).get("width") == 50, str(sel))
        sel, _ = client.ok("set_selection", {"document": doc_name,
                                             "mode": "none"})
        check("selection cleared", sel.get("selection") is None, str(sel))

        # ---------------------------------------------------------------
        section("escape hatches")
        got, _ = client.ok("run_python", {
            "code": "print('hello from krita')\n"
                    "result = {'docs': len(krita.documents())}"})
        check("run_python executed", got.get("ok") is True, str(got)[:300])
        check("run_python captured stdout",
              "hello from krita" in got.get("stdout", ""), str(got.get("stdout")))
        check("run_python returned a value",
              isinstance(got.get("result", {}).get("docs"), int), str(got))

        got, _ = client.ok("run_python", {"code": "raise ValueError('boom')"})
        check("run_python reports errors without killing the bridge",
              got.get("ok") is False and "boom" in got.get("exception", ""),
              str(got)[:200])

        client.ok("set_selection", {"document": doc_name, "mode": "rect",
                                    "x": 0, "y": 0, "width": 20, "height": 20})
        got, _ = client.ok("trigger_action", {"name": "deselect"})
        check("trigger_action fired", got.get("triggered") == "deselect",
              str(got))
        info, _ = client.ok("inspect_document", {"document": doc_name})
        check("triggered action took effect",
              info.get("selection") is None, str(info.get("selection")))

        result = client.call("trigger_action", {"name": "no_such_action_id"})
        check("unknown action rejected", result.get("isError") is True,
              _text_of(result)[:120])

        # ---------------------------------------------------------------
        section("geometry")
        got, _ = client.ok("transform_image", {"document": doc_name,
                                               "action": "crop", "x": 0,
                                               "y": 0, "width": 300,
                                               "height": 200})
        check("crop applied", got["after"]["width"] == 300, str(got.get("after")))

        got, _ = client.ok("transform_image", {"document": doc_name,
                                               "action": "scale",
                                               "width": 150, "height": 100})
        check("scale applied", got["after"]["width"] == 150,
              str(got.get("after")))

        got, _ = client.ok("transform_image", {"document": doc_name,
                                               "action": "resize_canvas",
                                               "x": 0, "y": 0, "width": 200,
                                               "height": 150})
        check("canvas resized", got["after"]["width"] == 200,
              str(got.get("after")))

        got, _ = client.ok("transform_image", {"document": doc_name,
                                               "action": "rotate",
                                               "degrees": 90})
        check("rotate applied", got["after"]["height"] == 200,
              str(got.get("after")))

        got, _ = client.ok("transform_image", {"document": doc_name,
                                               "action": "flatten"})
        check("flatten collapsed the stack", len(got.get("layers", [])) == 1,
              str(got.get("layers")))

        # ---------------------------------------------------------------
        section("saving")
        png_path = os.path.join(tmpdir, "nested", "export.png")
        got, _ = client.ok("export_document", {"document": doc_name,
                                               "path": png_path})
        check("export wrote a file", os.path.isfile(png_path) and
              got.get("bytes", 0) > 0, str(got))
        check("export created missing directories",
              got.get("created_directory") is True, str(got))

        section("saving.jpeg")
        jpg_path = os.path.join(tmpdir, "export.jpg")
        client.ok("export_document", {"document": doc_name, "path": jpg_path,
                                      "options": {"quality": 85}})
        check("jpeg export honoured options", os.path.isfile(jpg_path),
              jpg_path)

        section("saving.kra")
        kra_path = os.path.join(tmpdir, "saved.kra")
        got, _ = client.ok("save_document", {"document": doc_name,
                                             "path": kra_path})
        check("save-as wrote a .kra", os.path.isfile(kra_path), str(got))

        section("saving.reopen")
        got, _ = client.ok("open_document", {"path": png_path})
        check("exported png reopens", got.get("width") == 150, str(got))
        client.ok("close_document", {"document": got.get("name"),
                                     "discard_changes": True})

        # ---------------------------------------------------------------
        section("error handling")
        result = client.call("no_such_tool", {})
        check("unknown tool is an isError result",
              result.get("isError") is True, str(result)[:200])

        result = client.call("draw", {"document": doc_name,
                                      "commands": [{"type": "banana"}]})
        check("unknown draw type rejected", result.get("isError") is True,
              _text_of(result)[:160])
        check("rejection names the valid types",
              "fill_rect" in _text_of(result), _text_of(result)[:160])

        result = client.call("draw", {"document": doc_name,
                                      "commands": [{"type": "rect", "x": 0,
                                                    "y": 0}]})
        check("missing geometry rejected", result.get("isError") is True,
              _text_of(result)[:160])

        result = client.call("get_pixel", {"document": doc_name,
                                           "x": 99999, "y": 99999})
        check("out-of-bounds pixel rejected", result.get("isError") is True,
              _text_of(result)[:160])

        result = client.call("inspect_document", {"document": "does-not-exist"})
        check("unknown document rejected", result.get("isError") is True,
              _text_of(result)[:160])

        result = client.call("set_layer", {"document": doc_name,
                                           "layer": "no such layer",
                                           "opacity": 100})
        check("unknown layer rejected", result.get("isError") is True,
              _text_of(result)[:160])
        check("layer error lists what exists",
              "Layers in this document" in _text_of(result),
              _text_of(result)[:200])

        result = client.call("apply_filter", {"document": doc_name,
                                              "filter": "not-a-filter"})
        check("unknown filter rejected", result.get("isError") is True,
              _text_of(result)[:160])

        # An unrecognised blending mode is applied but flagged: Krita accepts
        # any string and silently renders unknown ids as Normal, so a warning
        # is the only way to make a typo visible.
        got, _ = client.ok("set_layer", {"document": doc_name,
                                         "blending_mode": "nonsense"})
        check("unknown blending mode carries a warning",
              any("nonsense" in w for w in got.get("warnings", [])),
              str(got.get("warnings"))[:200])
        got, _ = client.ok("set_layer", {"document": doc_name,
                                         "blending_mode": "MULTIPLY"})
        check("known blending mode is canonicalised and unwarned",
              "warnings" not in got
              and got["layer"]["blending_mode"] == "multiply",
              str(got.get("layer", {}).get("blending_mode")))

        result = client.call("transform_image", {"document": doc_name,
                                                 "action": "teleport"})
        check("bad transform action rejected", result.get("isError") is True,
              _text_of(result)[:160])

        # the bridge must still be healthy after all those failures
        status, _ = client.ok("status")
        check("bridge survives every error path",
              status.get("open_document_count", 0) >= 1,
              str(status.get("open_document_count")))

        # ---------------------------------------------------------------
        section("cleanup")
        cleanup_section(client, doc_name)

    except StopTest as exc:
        # Still run the cleanup, so a bisected run exercises the same close
        # as a full run -- that close is what the bisect is hunting.
        print("\nSTOPPED before section {0!r}; running cleanup.".format(exc))
        try:
            cleanup_section(client, doc_name)
        except Exception as inner:
            FAIL.append(("cleanup after stop", str(inner)))
            print("  cleanup failed: {0}".format(str(inner)[:300]))
    except AssertionError as exc:
        FAIL.append(("harness", str(exc)))
        print("\nHARNESS FAILURE: {0}".format(exc))
    except Exception as exc:
        import traceback
        FAIL.append(("harness", "{0}: {1}".format(type(exc).__name__, exc)))
        print("\nUNEXPECTED: {0}".format(traceback.format_exc()))
    finally:
        client.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 60)
    print("{0} passed, {1} failed".format(len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print("  FAIL {0}: {1}".format(name, detail))
    if VERBOSE and client.stderr_lines:
        print("\nserver stderr:")
        for line in client.stderr_lines[-40:]:
            print("  " + line)
    return 1 if FAIL else 0


def cleanup_section(client, doc_name):
    result = client.call("close_document", {"document": doc_name})
    check("close refuses to discard unsaved work silently",
          result.get("isError") is True, _text_of(result)[:160])
    if os.environ.get("KRITA_MCP_TEST_SAVE_BEFORE_CLOSE") == "1":
        # Does saving first avoid the teardown crash? Measured, not assumed.
        client.ok("close_document", {"document": doc_name, "save": True})
    else:
        client.ok("close_document", {"document": doc_name,
                                     "discard_changes": True})
    status, _ = client.ok("status")
    check("test document closed",
          not any(d.get("name") == doc_name
                  for d in status.get("documents", [])),
          str([d.get("name") for d in status.get("documents", [])]))


def _paths(layers, prefix=""):
    out = []
    for entry in layers:
        out.append(entry["path"])
        out.extend(_paths(entry.get("children", [])))
    return out


def _find(layers, name):
    for entry in layers:
        if entry["name"] == name:
            return entry
        found = _find(entry.get("children", []), name)
        if found:
            return found
    return None


def _tiny_png():
    """A 2x2 solid magenta PNG, built without any image library."""
    import struct
    import zlib

    width = height = 2
    raw = b""
    for _ in range(height):
        raw += b"\x00" + b"\xff\x00\xff\xff" * width

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


if __name__ == "__main__":
    sys.exit(main())
