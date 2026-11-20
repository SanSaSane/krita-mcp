#!/usr/bin/env python3
"""End-to-end checks for the editing tools, over real JSON-RPC.

Needs a running Krita with the bridge enabled, like test_mcp.py, whose client
and check helpers it reuses. Works on a scratch document it creates and closes.

    python3 test_edit.py
    python3 test_edit.py -v
"""

import json
import os
import shutil
import sys
import tempfile

import test_mcp
from test_mcp import Client, check, section, _text_of

DOC = "mcp edit test"
RED = "#ff0000ff"
BLUE = "#0000ffff"
CLEAR = "#00000000"


def bounds_of(rect):
    return None if rect is None else (rect["x"], rect["y"],
                                      rect["width"], rect["height"])


def main():
    tmpdir = tempfile.mkdtemp(prefix="krita-mcp-edit-")
    client = Client()

    def pixel(layer, x, y):
        got, _ = client.ok("get_pixel", {"document": DOC, "layer": layer,
                                         "x": x, "y": y})
        return got["hex"]

    def layer_bounds(layer):
        got, _ = client.ok("measure", {"document": DOC, "layers": [layer]})
        return bounds_of(got["layers"][0]["bounds"])

    def fresh_piece(name="piece"):
        """A 40x20 red block at 100,50 with a 4px blue stripe on its left."""
        client.ok("create_layer", {"document": DOC, "name": name})
        client.ok("draw", {"document": DOC, "layer": name, "commands": [
            {"type": "fill_rect", "x": 100, "y": 50, "w": 40, "h": 20,
             "color": "#ff0000"},
            {"type": "fill_rect", "x": 100, "y": 50, "w": 4, "h": 20,
             "color": "#0000ff"}]})

    try:
        client.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test_edit", "version": "1"}})
        client.notify("notifications/initialized")
        client.ok("create_document", {"width": 300, "height": 200,
                                      "name": DOC, "background": "#ffffff"})

        # ---------------------------------------------------------------
        section("edit: measure")
        fresh_piece()
        check("measure reports opaque bounds",
              layer_bounds("piece") == (100, 50, 40, 20),
              str(layer_bounds("piece")))

        # ---------------------------------------------------------------
        section("edit: move")
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "translate": [30, 10]})
        move_snapshot = got.get("snapshot")
        check("whole-pixel move is lossless", got.get("method") == "move",
              str(got))
        check("move shifted the bounds",
              bounds_of(got["after"]) == (130, 60, 40, 20), str(got["after"]))
        check("pixels moved with it",
              pixel("piece", 130, 60) == BLUE and pixel("piece", 169, 79) == RED
              and pixel("piece", 129, 60) == CLEAR,
              "{0} {1} {2}".format(pixel("piece", 130, 60),
                                   pixel("piece", 169, 79),
                                   pixel("piece", 129, 60)))
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "move_to": [10, 10]})
        check("move_to places the top-left corner",
              bounds_of(got["after"]) == (10, 10, 40, 20), str(got["after"]))

        # ---------------------------------------------------------------
        section("edit: snapshots")
        listed, _ = client.ok("snapshot", {"action": "list"})
        check("snapshots are listed",
              any(s["id"] == move_snapshot for s in listed["snapshots"]),
              str(listed)[:300])
        client.ok("snapshot", {"action": "restore", "id": move_snapshot})
        check("restore puts the layer back",
              layer_bounds("piece") == (100, 50, 40, 20)
              and pixel("piece", 100, 50) == BLUE,
              str(layer_bounds("piece")))
        result = client.call("snapshot", {"action": "restore", "id": 999999})
        check("unknown snapshot is an error", result.get("isError") is True,
              _text_of(result)[:120])

        # ---------------------------------------------------------------
        section("edit: flip")
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "flip": "horizontal"})
        check("flip keeps the bounds",
              bounds_of(got["after"]) == (100, 50, 40, 20), str(got))
        check("flip mirrors exactly",
              pixel("piece", 139, 55) == BLUE and pixel("piece", 136, 55) == BLUE
              and pixel("piece", 135, 55) == RED
              and pixel("piece", 100, 55) == RED,
              "{0} {1}".format(pixel("piece", 139, 55), pixel("piece", 100, 55)))

        # ---------------------------------------------------------------
        section("edit: rotate")
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "rotate": 90})
        check("90 degrees about the centre swaps width and height",
              bounds_of(got["after"]) == (110, 40, 20, 40), str(got["after"]))
        # after the flip the stripe was on the right; clockwise puts it below
        check("clockwise is positive",
              pixel("piece", 120, 78) == BLUE and pixel("piece", 120, 42) == RED,
              "{0} {1}".format(pixel("piece", 120, 78), pixel("piece", 120, 42)))
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "rotate": 90,
            "pivot": [110, 40]})
        check("rotation honours an explicit pivot",
              bounds_of(got["after"]) == (70, 40, 40, 20), str(got["after"]))
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "rotate": 30})
        after = bounds_of(got["after"])
        check("a free angle resamples and grows the bounds",
              got.get("method") == "resample" and after[2] > 40 and after[3] > 20,
              str(got["after"]))

        # ---------------------------------------------------------------
        section("edit: scale")
        client.ok("delete_layer", {"document": DOC, "layer": "piece"})
        fresh_piece()
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "scale": 2,
            "pivot": "top_left", "smooth": False})
        check("scale 2 about the top-left doubles the size",
              bounds_of(got["after"]) == (100, 50, 80, 40), str(got["after"]))
        check("scaled content is in place",
              pixel("piece", 107, 89) == BLUE and pixel("piece", 108, 89) == RED,
              "{0} {1}".format(pixel("piece", 107, 89), pixel("piece", 108, 89)))
        got, _ = client.ok("transform_layer", {
            "document": DOC, "layer": "piece", "scale": [0.5, 0.5],
            "pivot": "top_left", "translate": [5, 5]})
        check("scale and translate combine",
              bounds_of(got["after"])[:2] == (105, 55)
              and abs(bounds_of(got["after"])[2] - 40) <= 2,
              str(got["after"]))
        got, _ = client.ok("transform_layer", {"document": DOC,
                                               "layer": "piece"})
        check("no parameters changes nothing", got.get("changed") is False,
              str(got))

        # ---------------------------------------------------------------
        section("edit: selections")
        client.ok("delete_layer", {"document": DOC, "layer": "piece"})
        fresh_piece()
        got, _ = client.ok("set_selection", {
            "document": DOC, "mode": "color", "layer": "piece",
            "color": "#ff0000"})
        check("colour selection finds every matching pixel",
              got.get("pixels_matched") == 36 * 20
              and bounds_of(got["selection"]) == (104, 50, 36, 20), str(got))
        got, _ = client.ok("set_selection", {
            "document": DOC, "mode": "color", "layer": "piece",
            "x": 101, "y": 60, "contiguous": True})
        check("contiguous selection stops at the colour edge",
              got.get("pixels_matched") == 4 * 20
              and bounds_of(got["selection"]) == (100, 50, 4, 20), str(got))
        got, _ = client.ok("set_selection", {
            "document": DOC, "mode": "layer_alpha", "layer": "piece"})
        check("layer_alpha selects the opaque area",
              got.get("pixels_matched") == 800
              and bounds_of(got["selection"]) == (100, 50, 40, 20), str(got))
        got, _ = client.ok("set_selection", {
            "document": DOC, "mode": "polygon", "antialias": False,
            "points": [[10, 10], [50, 10], [50, 40], [10, 40]]})
        check("polygon selection covers its area",
              got.get("pixels_matched") == 40 * 30
              and bounds_of(got["selection"]) == (10, 10, 40, 30), str(got))
        got, _ = client.ok("set_selection", {
            "document": DOC, "mode": "ellipse", "x": 30, "y": 10,
            "width": 40, "height": 30, "combine": "add"})
        check("ellipse adds to the selection",
              bounds_of(got["selection"]) == (10, 10, 60, 30), str(got))
        got, _ = client.ok("set_selection", {
            "document": DOC, "mode": "rect", "x": 0, "y": 0,
            "width": 20, "height": 20})
        check("upstream modes still work",
              bounds_of(got["selection"]) == (0, 0, 20, 20), str(got))

        # ---------------------------------------------------------------
        section("edit: cut, copy, clear, fill")
        client.ok("set_selection", {"document": DOC, "mode": "rect",
                                    "x": 120, "y": 0, "width": 100,
                                    "height": 200})
        got, _ = client.ok("edit_selection", {
            "document": DOC, "layer": "piece", "action": "cut_to_layer",
            "name": "right half"})
        cut_snapshot = got.get("snapshot")
        check("cut leaves the rest behind",
              bounds_of(got["layer_bounds"]) == (100, 50, 20, 20), str(got))
        check("cut pixels land on a new layer in place",
              bounds_of(got["new_layer"]["bounds"]) == (120, 50, 20, 20)
              and pixel("right half", 125, 55) == RED, str(got["new_layer"]))
        tree, _ = client.ok("inspect_document", {"document": DOC})
        names = [layer["name"] for layer in tree["layers"]]
        check("the new layer sits directly above its source",
              names.index("right half") == names.index("piece") - 1, str(names))
        client.ok("snapshot", {"action": "restore", "id": cut_snapshot})
        check("restoring the snapshot undoes the cut",
              layer_bounds("piece") == (100, 50, 40, 20),
              str(layer_bounds("piece")))

        got, _ = client.ok("edit_selection", {
            "document": DOC, "layer": "piece", "action": "copy_to_layer",
            "name": "copy", "x": 200, "y": 150})
        check("copy can be placed elsewhere",
              bounds_of(got["new_layer"]["bounds"]) == (200, 150, 20, 20)
              and layer_bounds("piece") == (100, 50, 40, 20), str(got))

        got, _ = client.ok("edit_selection", {
            "document": DOC, "layer": "piece", "action": "fill",
            "color": "#00ff00"})
        check("fill replaces the selected pixels",
              pixel("piece", 125, 55) == "#00ff00ff"
              and pixel("piece", 110, 55) == RED, pixel("piece", 125, 55))
        got, _ = client.ok("edit_selection", {
            "document": DOC, "layer": "piece", "action": "clear"})
        check("clear erases the selected pixels",
              bounds_of(got["layer_bounds"]) == (100, 50, 20, 20), str(got))
        client.ok("set_selection", {"document": DOC, "mode": "none"})
        result = client.call("edit_selection", {
            "document": DOC, "layer": "piece", "action": "clear"})
        check("acting without a selection is an error",
              result.get("isError") is True, _text_of(result)[:120])

        # ---------------------------------------------------------------
        section("edit: replace colour")
        got, _ = client.ok("replace_color", {
            "document": DOC, "layer": "piece", "from": "#ff0000",
            "to": "#8fa55a"})
        check("replace_color counts what it changed",
              got.get("pixels_changed") == 16 * 20, str(got))
        check("only the matching colour changed",
              pixel("piece", 110, 55) == "#8fa55aff"
              and pixel("piece", 101, 55) == BLUE,
              "{0} {1}".format(pixel("piece", 110, 55), pixel("piece", 101, 55)))
        got, _ = client.ok("replace_color", {
            "document": DOC, "layer": "piece", "from": "#0000fa",
            "to": "#000000", "tolerance": 8})
        check("tolerance catches a near colour",
              got.get("pixels_changed") == 4 * 20
              and pixel("piece", 101, 55) == "#000000ff", str(got))
        got, _ = client.ok("replace_color", {
            "document": DOC, "layer": "piece", "from": "#123456",
            "to": "#000000"})
        check("no match changes nothing and takes no snapshot",
              got.get("pixels_changed") == 0 and "snapshot" not in got,
              str(got))

        # ---------------------------------------------------------------
        section("edit: measure against a reference")
        got, _ = client.ok("measure", {"document": DOC,
                                       "layers": ["piece", "right half"],
                                       "reference": "piece"})
        rows = {row["name"]: row for row in got["layers"]}
        check("a layer matches itself exactly",
              rows["piece"]["vs_reference"]["iou"] == 1.0,
              str(rows["piece"]))
        check("a disjoint layer has no overlap",
              rows["right half"]["vs_reference"]["iou"] == 0.0
              and rows["right half"]["vs_reference"]["center_offset"]
              == [20.0, 0.0], str(rows["right half"]))

        # ---------------------------------------------------------------
        section("edit: export and import")
        out = os.path.join(tmpdir, "layers")
        got, _ = client.ok("export_layers", {
            "document": DOC, "directory": out,
            "layers": ["piece", "right half", "copy"]})
        check("one file per layer",
              got.get("exported") == 3
              and all(os.path.isfile(os.path.join(out, f))
                      for f in got["files"]), str(got))
        with open(got["manifest"], "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        entry = next(e for e in manifest["layers"] if e["name"] == "copy")
        check("manifest records trimmed positions",
              (entry["x"], entry["y"], entry["width"], entry["height"])
              == (200, 150, 20, 20) and manifest["canvas"]["width"] == 300,
              str(entry))
        got, _ = client.ok("import_layer", {
            "document": DOC, "path": os.path.join(out, "right_half.png"),
            "name": "imported", "x": 7, "y": 9})
        check("import places the image as a new layer",
              bounds_of(got["layer"]["bounds"]) == (7, 9, 20, 20)
              and pixel("imported", 8, 10) == RED, str(got))
        result = client.call("import_layer", {
            "document": DOC, "path": os.path.join(out, "missing.png")})
        check("a missing file is an error", result.get("isError") is True,
              _text_of(result)[:120])

        # ---------------------------------------------------------------
        section("edit: refusals")
        client.ok("create_layer", {"document": DOC, "name": "empty"})
        result = client.call("transform_layer", {
            "document": DOC, "layer": "empty", "rotate": 10})
        check("an empty layer cannot be transformed",
              result.get("isError") is True, _text_of(result)[:120])
        client.ok("create_layer", {"document": DOC, "name": "grp",
                                   "type": "grouplayer"})
        result = client.call("transform_layer", {
            "document": DOC, "layer": "grp", "rotate": 10})
        check("a group layer is refused with a reason",
              result.get("isError") is True
              and "paint layer" in _text_of(result), _text_of(result)[:160])

    except AssertionError as exc:
        test_mcp.FAIL.append(("harness", str(exc)))
        print("\nHARNESS FAILURE: {0}".format(exc))
    finally:
        try:
            client.call("close_document", {"document": DOC,
                                           "discard_changes": True})
        except Exception:
            pass
        client.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 60)
    print("{0} passed, {1} failed".format(len(test_mcp.PASS),
                                          len(test_mcp.FAIL)))
    for name, detail in test_mcp.FAIL:
        print("  FAIL {0}: {1}".format(name, detail))
    return 1 if test_mcp.FAIL else 0


if __name__ == "__main__":
    if "-v" in sys.argv:
        test_mcp.VERBOSE = True
    sys.exit(main())
