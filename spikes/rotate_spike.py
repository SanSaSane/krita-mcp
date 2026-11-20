from krita import Krita, Selection
from PyQt5.QtCore import QByteArray, QCoreApplication, QEventLoop
import math, time
app = Krita.instance()
d = [x for x in app.documents() if x.name() == "mcp-spike"][0]
out = {}
def b(n):
    r = n.bounds(); return [r.x(), r.y(), r.width(), r.height()]
def pump(t=0.4):
    end = time.time() + t
    while time.time() < end:
        QCoreApplication.instance().processEvents(QEventLoop.ExcludeUserInputEvents, 20); time.sleep(0.01)
def settle():
    d.waitForDone(); d.refreshProjection(); d.waitForDone(); pump()
layer = d.createNode("rot", "paintlayer")
d.rootNode().addChildNode(layer, None)
layer.setPixelData(QByteArray(bytes([0, 255, 0, 255]) * (60 * 20)), 200, 100, 60, 20)
settle(); out["start"] = b(layer)
d.setActiveNode(layer); settle()
for label, ang in (("r90", math.pi/2), ("r30", math.radians(30))):
    layer.rotateNode(ang); settle(); out[label] = b(layer)
app.action("edit_undo").trigger(); settle(); out["undo1"] = b(layer)
layer.shearNode(20, 0); settle(); out["shear"] = b(layer)
# paste variants
src = d.nodeByName("piece")
sel = Selection(); bb = b(src); sel.select(bb[0], bb[1], bb[2], bb[3], 255)
sel.copy(src); settle()
tgt = d.createNode("pasted", "paintlayer"); d.rootNode().addChildNode(tgt, None); settle()
sel.paste(tgt, 10, 10); settle(); out["paste_sel"] = b(tgt)
d.setSelection(sel); d.setActiveNode(src); settle()
app.action("copy_selection_to_new_layer").trigger(); settle()
out["top_after_action"] = [d.topLevelNodes()[-1].name(), b(d.topLevelNodes()[-1])]
out["layers"] = [n.name() for n in d.topLevelNodes()]
result = out
