# Throwaway spike, run inside Krita through run_python. Not part of the plugin.
#
# Answers, on a scratch document it creates itself:
#   1. Does Node.move() shift pixels, and does edit_undo revert it?
#   2. Which pivot do rotateNode() / scaleNode() use, and are they undoable?
#   3. Do Selection.cut/copy/paste work without window focus?
from krita import Krita, Selection
from PyQt5.QtCore import QPointF, QByteArray
import math

app = Krita.instance()
out = {}


def bounds(node):
    r = node.bounds()
    return [r.x(), r.y(), r.width(), r.height()]


def settle(d):
    d.waitForDone()
    d.refreshProjection()
    d.waitForDone()


d = app.createDocument(400, 300, "mcp-spike", "RGBA", "U8", "", 72.0)
app.activeWindow().addView(d)
d.setBatchmode(True)

layer = d.createNode("piece", "paintlayer")
d.rootNode().addChildNode(layer, None)
# opaque red 40x20 block at (100, 50)
w, h = 40, 20
layer.setPixelData(QByteArray(bytes([0, 0, 255, 255]) * (w * h)), 100, 50, w, h)
settle(d)
out["start"] = bounds(layer)


def undo_check(label, before):
    app.action("edit_undo").trigger()
    settle(d)
    out[label + "_after_undo"] = bounds(layer)
    out[label + "_undoable"] = bounds(layer) == before
    if bounds(layer) != before:
        return
    app.action("edit_redo").trigger()
    settle(d)


# 1. move
before = bounds(layer)
layer.move(30, 10)
settle(d)
out["move"] = {"bounds": bounds(layer), "position": [layer.position().x(), layer.position().y()]}
undo_check("move", before)

# 2a. rotate 90 degrees
before = bounds(layer)
try:
    layer.rotateNode(math.pi / 2)
    settle(d)
    out["rotate"] = bounds(layer)
    undo_check("rotate", before)
except Exception as exc:
    out["rotate_error"] = repr(exc)

# 2b. scale to double size around an explicit origin
before = bounds(layer)
try:
    layer.scaleNode(QPointF(before[0], before[1]), before[2] * 2, before[3] * 2, "Bicubic")
    settle(d)
    out["scale"] = bounds(layer)
    undo_check("scale", before)
except Exception as exc:
    out["scale_error"] = repr(exc)

# 3. selection cut / paste into a new layer
try:
    b = bounds(layer)
    sel = Selection()
    sel.select(b[0], b[1], b[2] // 2, b[3], 255)
    target = d.createNode("cut-out", "paintlayer")
    d.rootNode().addChildNode(target, None)
    sel.cut(layer)
    settle(d)
    out["after_cut_source"] = bounds(layer)
    sel.paste(target, b[0], b[1])
    settle(d)
    out["after_paste_target"] = bounds(target)
except Exception as exc:
    out["cut_paste_error"] = repr(exc)

out["node_methods"] = sorted(m for m in dir(layer) if any(
    k in m.lower() for k in ("move", "scale", "rotate", "shear", "crop", "transform", "position")))
d.setBatchmode(False)
result = out
