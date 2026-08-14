#!/usr/bin/env python3
"""Paint the project banner in Krita, over the MCP protocol.

Same client as example_poster.py, wider crop, tuned for a README header.

    python example_banner.py [output.png]
"""

import os
import random
import sys

from example_poster import Client, bird, ridge

W, H = 1600, 500
HORIZON = 330
DOC = "Krita MCP banner"


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                          else os.path.join(os.path.dirname(
                              os.path.abspath(__file__)), "docs", "banner.png"))
    os.makedirs(os.path.dirname(out), exist_ok=True)

    client = Client()
    try:
        client.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "example_banner", "version": "1.0"}})
        client.notify("notifications/initialized")

        for _ in range(5):
            status, _ = client.call("status")
            stale = [i for i, d in enumerate(status["documents"])
                     if d.get("name") == DOC]
            if not stale:
                break
            client.call("close_document", {"document": stale[0],
                                           "discard_changes": True})

        client.call("create_document", {
            "width": W, "height": H, "name": DOC, "resolution_dpi": 150,
            "background": "#12102a"})
        for name in ("Sky", "Mountains", "Water", "Birds", "Title"):
            client.call("create_layer", {"document": DOC, "name": name})

        sun_x, sun_y = 1180, 236

        client.call("draw", {"document": DOC, "layer": "Sky", "commands": [
            {"type": "linear_gradient", "x": 0, "y": 0, "w": W, "h": HORIZON,
             "direction": "vertical",
             "stops": [[0, "#141033"], [0.34, "#4c2d7a"], [0.66, "#c9512f"],
                       [0.87, "#ee8f4c"], [1, "#f6c177"]]},
            {"type": "radial_gradient", "x": sun_x - 250, "y": sun_y - 250,
             "w": 500, "h": 500,
             "stops": [[0.0, "#ffdca0b4"], [0.30, "#ffc98268"],
                       [0.55, "#ff9f5c33"], [0.78, "#e97a4614"],
                       [1.0, "#e97a4600"]]},
            {"type": "circle", "cx": sun_x, "cy": sun_y, "radius": 74,
             "fill": "#ffe6a8"},
        ]})

        client.call("draw", {"document": DOC, "layer": "Mountains",
                             "commands": [
            ridge([[0, HORIZON], [200, 214], [400, 278], [640, 176],
                   [880, 268], [1120, 198], [1350, 262], [W, 226],
                   [W, HORIZON]], "#5b4483"),
            ridge([[0, HORIZON], [270, 252], [510, 300], [750, 232],
                   [1010, 292], [1300, 244], [W, 282], [W, HORIZON]],
                  "#33264f"),
            {"type": "polygon",
             "points": [[750, 232], [776, 258], [762, 254], [750, 246],
                        [738, 256], [726, 252]], "fill": "#efe9f7"},
            ridge([[0, HORIZON], [340, 300], [700, 318], [1060, 296],
                   [1400, 314], [W, 302], [W, HORIZON]], "#1d1636"),
        ]})

        water = [{"type": "linear_gradient", "x": 0, "y": HORIZON, "w": W,
                  "h": H - HORIZON, "direction": "vertical",
                  "stops": [[0, "#d0603a"], [0.24, "#8a3f6e"],
                            [0.62, "#2e1f4d"], [1, "#120f28"]]}]
        rng = random.Random(11)
        y = float(HORIZON + 3)
        while y < H - 8:
            depth = (y - HORIZON) / float(H - HORIZON)
            half = (120 * (1.0 - depth * 0.58)) * rng.uniform(0.35, 1.3)
            alpha = int(max(0x0C, 0xC4 * (1.0 - depth) ** 1.3))
            water.append({"type": "fill_rect",
                          "x": sun_x - half + rng.uniform(-28, 28) * depth,
                          "y": y, "w": half * 2,
                          "h": rng.choice([2, 3, 3, 4, 6]),
                          "color": "#ffd489{0:02x}".format(alpha)})
            y += rng.uniform(4, 9) + depth * 8
        client.call("draw", {"document": DOC, "layer": "Water",
                             "commands": water})

        client.call("draw", {"document": DOC, "layer": "Birds", "commands": [
            bird(430, 132, 20, "#241b33", 3.0),
            bird(512, 100, 15, "#241b33", 2.6),
            bird(586, 146, 12, "#2d2340", 2.2),
            bird(340, 178, 10, "#2d2340", 2.0),
            bird(654, 92, 9, "#33284a", 1.8),
        ]})

        client.call("draw", {"document": DOC, "layer": "Title", "commands": [
            {"type": "text", "x": 72, "y": 62, "text": "KRITA × MCP",
             "size": 76, "bold": True, "color": "#f7e7ce"},
            {"type": "line", "x1": 78, "y1": 168, "x2": 560, "y2": 168,
             "color": "#f6c177", "stroke_width": 2},
            {"type": "text", "x": 78, "y": 190,
             "text": "let Claude paint in Krita",
             "size": 30, "italic": True, "color": "#e4bfa4"},
        ]})

        client.call("export_document", {"document": DOC, "path": out})
        print("banner -> {0} ({1} bytes)".format(out, os.path.getsize(out)))

        payload, image = client.call("get_image", {"document": DOC,
                                                   "max_size": 1100})
        if image:
            import base64
            preview = os.path.join(os.path.dirname(out), "banner_preview.png")
            with open(preview, "wb") as handle:
                handle.write(base64.b64decode(image))
            print("preview -> {0}".format(preview))
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
