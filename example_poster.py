#!/usr/bin/env python3
"""Draw a poster in Krita over the MCP protocol.

Doubles as a worked example and as proof the whole chain works: it spawns
mcp_server.py and speaks real JSON-RPC to it, exactly as an MCP client does.
Every tool call is checked, and anything that comes back with isError stops
the run.

    python example_poster.py [output.png]
"""

import base64
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")

W, H = 1200, 800
HORIZON = 520
DOC = "Krita MCP poster"


class Client:
    """Minimal MCP client over stdio."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self._inbox = queue.Queue()
        self._id = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            if line.strip():
                self._inbox.put(line.strip())
        self._inbox.put(None)

    def _send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=180):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method,
                    "params": params or {}})
        while True:
            try:
                line = self._inbox.get(timeout=timeout)
            except queue.Empty:
                raise RuntimeError("timed out waiting for {0}".format(method))
            if line is None:
                raise RuntimeError("server exited")
            message = json.loads(line)
            if message.get("id") == self._id:
                return message

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def call(self, name, arguments=None, timeout=180):
        reply = self.request("tools/call",
                             {"name": name, "arguments": arguments or {}},
                             timeout)
        result = reply.get("result")
        if result is None:
            raise RuntimeError("{0}: {1}".format(name, reply.get("error")))
        text = ""
        image = None
        for block in result.get("content", []):
            if block.get("type") == "text":
                text = block["text"]
            elif block.get("type") == "image":
                image = block["data"]
        if result.get("isError"):
            raise RuntimeError("{0} failed:\n{1}".format(name, text))
        print("  ok  {0}".format(name))
        try:
            return json.loads(text), image
        except ValueError:
            return text, image

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def ridge(points, colour):
    return {"type": "polygon", "points": points, "fill": colour}


def bird(x, y, span=15, colour="#241b33", width=2.5):
    return {"type": "polyline",
            "points": [[x - span, y], [x - span / 3.0, y - span * 0.55],
                       [x, y - span * 0.15],
                       [x + span / 3.0, y - span * 0.55], [x + span, y]],
            "color": colour, "stroke_width": width}


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                          else os.path.join(HERE, "poster.png"))
    client = Client()
    try:
        print("handshake")
        client.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "example_poster", "version": "1.0"}})
        client.notify("notifications/initialized")

        status, _ = client.call("status")
        print("  Krita {0}".format(status["krita_version"]))

        # a previous run may have left its document open
        for _ in range(5):
            status, _ = client.call("status")
            stale = [i for i, d in enumerate(status["documents"])
                     if d.get("name") == DOC]
            if not stale:
                break
            client.call("close_document", {"document": stale[0],
                                           "discard_changes": True})

        print("document")
        client.call("create_document", {
            "width": W, "height": H, "name": DOC, "resolution_dpi": 150,
            "background": "#12102a"})

        for name in ("Sky", "Mountains", "Water", "Birds", "Title"):
            client.call("create_layer", {"document": DOC, "name": name})

        print("sky")
        client.call("draw", {"document": DOC, "layer": "Sky", "commands": [
            {"type": "linear_gradient", "x": 0, "y": 0, "w": W, "h": HORIZON,
             "direction": "vertical",
             "stops": [[0, "#141033"], [0.38, "#4c2d7a"],
                       [0.68, "#c9512f"], [0.88, "#ee8f4c"],
                       [1, "#f6c177"]]},
            # Halo as one radial gradient. Stacked translucent circles are the
            # obvious way to do this and it looks wrong -- each circle's edge
            # reads as a hard ring.
            {"type": "radial_gradient", "x": 860 - 230, "y": 402 - 230,
             "w": 460, "h": 460,
             "stops": [[0.0, "#ffdca0b4"], [0.30, "#ffc98268"],
                       [0.55, "#ff9f5c33"], [0.78, "#e97a4614"],
                       [1.0, "#e97a4600"]]},
            {"type": "circle", "cx": 860, "cy": 402, "radius": 88,
             "fill": "#ffe6a8"},
        ]})

        print("mountains")
        client.call("draw", {"document": DOC, "layer": "Mountains",
                             "commands": [
            ridge([[0, HORIZON], [150, 392], [300, 462], [478, 338],
                   [660, 452], [840, 372], [1010, 444], [W, 404],
                   [W, HORIZON]], "#5b4483"),
            ridge([[0, HORIZON], [205, 436], [382, 498], [560, 410],
                   [762, 486], [980, 424], [W, 468], [W, HORIZON]],
                  "#33264f"),
            # snow on the two tallest near peaks
            {"type": "polygon",
             "points": [[560, 410], [588, 442], [572, 438], [560, 428],
                        [546, 440], [532, 436]], "fill": "#efe9f7"},
            {"type": "polygon",
             "points": [[980, 424], [1004, 450], [990, 446], [980, 438],
                        [968, 448], [956, 444]], "fill": "#efe9f7"},
            ridge([[0, HORIZON], [260, 486], [520, 512], [800, 480],
                   [1050, 508], [W, 490], [W, HORIZON]], "#1d1636"),
        ]})

        print("water")
        reflection = [
            {"type": "linear_gradient", "x": 0, "y": HORIZON, "w": W,
             "h": H - HORIZON, "direction": "vertical",
             "stops": [[0, "#d0603a"], [0.22, "#8a3f6e"],
                       [0.6, "#2e1f4d"], [1, "#120f28"]]},
        ]
        # Glare built only from broken streaks. Two things that look obvious
        # and both read wrong: a vertical linear gradient fills its rect
        # uniformly across x, so it leaves a hard-edged box floating in the
        # water; and streaks on a straight taper turn into a tidy funnel that
        # reads as a staircase. Jittered widths, offsets and gaps from a fixed
        # seed give something water-like and still reproducible.
        rng = random.Random(7)
        y = float(HORIZON + 3)
        while y < H - 12:
            depth = (y - HORIZON) / float(H - HORIZON)
            half = (128 * (1.0 - depth * 0.6)) * rng.uniform(0.35, 1.3)
            alpha = int(max(0x0C, 0xC4 * (1.0 - depth) ** 1.35))
            offset = rng.uniform(-30, 30) * depth
            reflection.append({
                "type": "fill_rect", "x": 860 - half + offset, "y": y,
                "w": half * 2, "h": rng.choice([2, 3, 3, 4, 6]),
                "color": "#ffd489{0:02x}".format(alpha)})
            # tight near the horizon, opening up with depth
            y += rng.uniform(4, 9) + depth * 9
        client.call("draw", {"document": DOC, "layer": "Water",
                             "commands": reflection})

        print("birds and title")
        client.call("draw", {"document": DOC, "layer": "Birds", "commands": [
            bird(300, 210, 20, "#241b33", 3.0),
            bird(372, 176, 15, "#241b33", 2.6),
            bird(438, 224, 12, "#2d2340", 2.2),
            bird(210, 268, 10, "#2d2340", 2.0),
            bird(500, 168, 9, "#33284a", 1.8),
        ]})

        client.call("draw", {"document": DOC, "layer": "Title", "commands": [
            {"type": "text", "x": 64, "y": 60, "text": "KRITA × MCP",
             "size": 62, "bold": True, "color": "#f7e7ce"},
            {"type": "text", "x": 68, "y": 138,
             "text": "painted by Claude over the bridge",
             "size": 25, "italic": True, "color": "#e4bfa4"},
            {"type": "line", "x1": 68, "y1": 118, "x2": 470, "y2": 118,
             "color": "#f6c177", "stroke_width": 2},
        ]})

        print("checks")
        px, _ = client.call("get_pixel", {"document": DOC, "layer": "Sky",
                                          "x": 860, "y": 402})
        print("     sun centre reads {0}".format(px["hex"]))
        info, _ = client.call("inspect_document", {"document": DOC})
        print("     layers: {0}".format(
            ", ".join(e["name"] for e in info["layers"])))

        payload, image = client.call("get_image", {"document": DOC,
                                                   "max_size": 1000})
        print("     preview {0}x{1}".format(payload["returned_size"]["width"],
                                            payload["returned_size"]["height"]))

        client.call("export_document", {"document": DOC, "path": out})
        print("\nexported {0} ({1} bytes)".format(out, os.path.getsize(out)))

        preview = os.path.join(HERE, "poster_preview.png")
        if image:
            with open(preview, "wb") as handle:
                handle.write(base64.b64decode(image))
            print("preview  {0}".format(preview))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
