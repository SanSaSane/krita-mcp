#!/usr/bin/env python3
"""Stress one close strategy until Krita dies (or the run completes).

Each iteration builds a document with a history like the integration suite's
-- draw, filter, geometry, save, export -- then closes it with the chosen
strategy and immediately asks for status, which is what exposed the crash.

    python stress_close.py document_close [iterations]
    python stress_close.py file_close_action
    python stress_close.py close_views_first
    python stress_close.py deferred

Exit code 0 means it survived every iteration.
"""

import os
import sys
import tempfile
import time

from mcp_server import BRIDGE, BridgeError, BridgeUnavailable

NAME = "stress doc"

STRATEGIES = {
    # The current implementation: setModified(False) then Document.close().
    "document_close": """
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
doc.close()
result = 'closed'
""",

    # Close through Krita's own menu action, the path Ctrl+W uses.
    "file_close_action": """
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
krita.setActiveDocument(doc)
a = krita.action('file_close')
if a is None:
    raise RuntimeError('no file_close action')
a.trigger()
result = 'triggered'
""",

    # Drop the views through the window first, then retire the document.
    "close_views_first": """
from PyQt5.QtCore import QCoreApplication, QEvent, QEventLoop
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
closed = 0
for w in krita.windows():
    for v in w.views():
        if v.document() is not None and v.document().fileName() == doc.fileName() \\
                and v.document().name() == doc.name():
            v.close()
            closed += 1
QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
QCoreApplication.instance().processEvents(QEventLoop.ExcludeUserInputEvents, 50)
doc.close()
result = 'views closed: %d' % closed
""",

    # Hand the close to the event loop instead of running it in this stack.
    "deferred": """
from PyQt5.QtCore import QTimer
doc = _target
doc.setBatchmode(True)
doc.setModified(False)
QTimer.singleShot(0, doc.close)
result = 'scheduled'
""",
}

PREAMBLE = """
from krita import Krita
krita = Krita.instance()
_target = None
for d in krita.documents():
    if d.name() == %r:
        _target = d
        break
if _target is None:
    raise RuntimeError('target document not found')
"""


GROUPS = os.environ.get("STRESS_GROUPS", "layers,restack,errors,reads").split(",")


def build(tmpdir, index):
    if "restack" in GROUPS or "layers" in GROUPS:
        pass
    return _build(tmpdir, index)


def _restack(name):
    """The layer surgery the integration suite performs."""
    BRIDGE.call("duplicate_layer", {"document": name, "layer": "Art",
                                    "name": "Art copy"}, timeout=60)
    BRIDGE.call("move_layer", {"document": name, "layer": "Art copy",
                               "parent": "G", "index": 0}, timeout=60)
    BRIDGE.call("delete_layer", {"document": name, "layer": "G/Art copy"},
                timeout=60)


def _errors(name):
    """Operations that fail, to leave failed-call debris behind."""
    for params, op in (
        ({"document": name, "commands": [{"type": "banana"}]}, "draw"),
        ({"document": name, "commands": [{"type": "rect", "x": 0, "y": 0}]},
         "draw"),
        ({"document": name, "x": 99999, "y": 99999}, "get_pixel"),
        ({"document": "does-not-exist"}, "document_info"),
        ({"document": name, "layer": "no such layer", "opacity": 100},
         "set_layer"),
        ({"document": name, "filter": "not-a-filter"}, "apply_filter"),
    ):
        try:
            BRIDGE.call(op, params, timeout=30)
        except BridgeError:
            pass


def _reads(name):
    BRIDGE.call("get_image", {"document": name, "max_size": 200})
    BRIDGE.call("get_image", {"document": name, "layer": "Art",
                              "region": {"x": 0, "y": 0, "width": 60,
                                         "height": 60}})
    BRIDGE.call("get_pixel", {"document": name, "x": 5, "y": 5})
    BRIDGE.call("run_python", {"code": "result = len(krita.documents())"})
    try:
        BRIDGE.call("run_python", {"code": "raise ValueError('boom')"})
    except BridgeError:
        pass


def _build(tmpdir, index):
    BRIDGE.call("create_document", {"width": 300, "height": 220,
                                    "name": NAME, "background": "#203040"})
    BRIDGE.call("create_layer", {"document": NAME, "name": "Art"})
    BRIDGE.call("create_layer", {"document": NAME, "name": "G",
                                 "type": "grouplayer"})
    BRIDGE.call("create_layer", {"document": NAME, "name": "Nested",
                                 "parent": "G"})
    BRIDGE.call("draw", {"document": NAME, "layer": "Art", "commands": [
        {"type": "fill_rect", "x": 0, "y": 0, "w": 300, "h": 220,
         "color": "#ffffff"},
        {"type": "rect", "x": 10, "y": 10, "w": 90, "h": 60, "fill": "#e94f37",
         "color": "#111111", "stroke_width": 3},
        {"type": "ellipse", "x": 120, "y": 10, "w": 80, "h": 60,
         "fill": "#3f88c5"},
        {"type": "text", "x": 12, "y": 150, "text": "stress", "size": 22,
         "color": "#111111"},
    ]})
    if "restack" in GROUPS:
        _restack(NAME)
    if "reads" in GROUPS:
        _reads(NAME)
    BRIDGE.call("apply_filter", {"document": NAME, "layer": "Art",
                                 "filter": "invert"}, timeout=60)
    BRIDGE.call("apply_filter", {"document": NAME, "layer": "Art",
                                 "filter": "blur",
                                 "region": {"x": 0, "y": 0, "width": 150,
                                            "height": 120}}, timeout=60)
    BRIDGE.call("set_selection", {"document": NAME, "mode": "rect", "x": 5,
                                  "y": 5, "width": 40, "height": 40})
    BRIDGE.call("set_selection", {"document": NAME, "mode": "none"})
    BRIDGE.call("crop_image", {"document": NAME, "x": 0, "y": 0,
                               "width": 220, "height": 160}, timeout=60)
    BRIDGE.call("scale_image", {"document": NAME, "width": 150,
                                "height": 110}, timeout=90)
    BRIDGE.call("rotate_image", {"document": NAME, "degrees": 90},
                timeout=60)
    BRIDGE.call("flatten_image", {"document": NAME}, timeout=60)

    png = os.path.join(tmpdir, "s{0}.png".format(index))
    kra = os.path.join(tmpdir, "s{0}.kra".format(index))
    BRIDGE.call("export_document", {"document": NAME, "path": png},
                timeout=90)
    BRIDGE.call("save_document", {"document": NAME, "path": kra}, timeout=90)
    opened = BRIDGE.call("open_document", {"path": png}, timeout=90)
    BRIDGE.call("close_document", {"document": opened["name"],
                                   "discard_changes": True}, timeout=60)
    if "errors" in GROUPS:
        _errors(NAME)
    # dirty it again, exactly as the suite does before its final close
    BRIDGE.call("set_layer", {"document": NAME, "blending_mode": "multiply"})


def main():
    strategy = sys.argv[1] if len(sys.argv) > 1 else "document_close"
    iterations = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    if strategy != "op" and strategy not in STRATEGIES:
        print("unknown strategy; pick one of: " + ", ".join(STRATEGIES))
        return 1

    code = PREAMBLE % NAME + STRATEGIES.get(strategy, "result = None")
    tmpdir = tempfile.mkdtemp(prefix="krita-stress-")
    print("strategy: {0}, {1} iterations, groups: {2}".format(
        strategy, iterations, ",".join(GROUPS)))

    for i in range(1, iterations + 1):
        try:
            build(tmpdir, i)
            if strategy == "op":
                BRIDGE.call("close_document", {"document": NAME,
                                               "discard_changes": True},
                            timeout=90)
            else:
                BRIDGE.call("run_python", {"code": code}, timeout=90)
            # the call that used to read freed memory
            status = BRIDGE.call("status", {}, timeout=15)
            still = [d for d in status["documents"] if d.get("name") == NAME]
            if still:
                # the deferred strategy needs a beat before it takes effect
                time.sleep(0.5)
                status = BRIDGE.call("status", {}, timeout=15)
                still = [d for d in status["documents"]
                         if d.get("name") == NAME]
            print("  iteration {0}: ok ({1} docs open, target closed={2})"
                  .format(i, status["open_document_count"], not still))
        except BridgeUnavailable:
            print("  iteration {0}: KRITA DIED".format(i))
            return 2
        except BridgeError as exc:
            print("  iteration {0}: error {1}: {2}".format(i, exc.kind,
                                                           str(exc)[:200]))
            return 3

    print("survived all {0} iterations".format(iterations))
    return 0


if __name__ == "__main__":
    sys.exit(main())
