"""The operations the MCP server can invoke.

Everything in here runs on Krita's GUI thread (see mainthread.py), so it may
touch libkis freely, but it must never block for long: the HTTP worker is
waiting on it and the whole UI is frozen meanwhile.

Layer addressing
----------------
Layers are reported in the order the layer docker shows them, top first, and
``index``/``index_path`` follow that same top-first convention.  libkis itself
orders children bottom-first; the conversion is confined to ``_children`` and
``_insert_node`` so the rest of the module can think in docker order.
"""

import base64
import gc
import io
import os
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout

from .compat import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QLinearGradient,
    QLineF,
    QPainter,
    QPainterPath,
    QPen,
    QPointF,
    QPolygonF,
    QRadialGradient,
    QRect,
    QRectF,
    QtCompat as Qt,
    QTransform,
)

from . import imaging
from .imaging import ImagingError, parse_color

PLUGIN_VERSION = "1.0.0"

OPS = {}

# Krita 5.3 exposes no way to enumerate composite ops, and setBlendingMode
# accepts any string -- an unknown id is stored verbatim and silently renders
# as Normal. So we keep a conservative list of ids we are sure about and warn
# (rather than refuse) on anything else, which keeps exotic ids usable while
# making a typo visible instead of silent.
COMMON_BLENDING_MODES = [
    "normal", "dissolve", "behind", "greater", "erase", "copy",
    "alphadarken", "destination-in", "destination-atop",
    "multiply", "screen", "overlay", "darken", "lighten",
    "add", "subtract", "inverse_subtract", "divide",
    "difference", "exclusion", "negation",
    "dodge", "burn", "linear_dodge", "linear_burn",
    "hard light", "soft_light_svg", "vivid_light", "linear light",
    "pin_light", "hard mix", "lighter color", "darker color",
    "color", "hue", "saturation", "luminize",
    "grain_merge", "grain_extract", "geometric_mean", "parallel", "allanon",
    "freeze", "heat", "glow", "reflect",
    "and", "or", "xor",
]
_BLENDING_LOOKUP = {m.lower(): m for m in COMMON_BLENDING_MODES}


class OpError(Exception):
    """A problem the caller can act on. Reported without a traceback."""

    def __init__(self, message, kind="invalid_request"):
        super(OpError, self).__init__(message)
        self.kind = kind


def op(name, timeout=20.0, mutates=False):
    def register(fn):
        fn.op_name = name
        fn.op_timeout = timeout
        fn.op_mutates = mutates
        OPS[name] = fn
        return fn
    return register


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _krita():
    from krita import Krita
    return Krita.instance()


def _arg(params, key, default=None):
    value = params.get(key, default)
    return default if value is None else value


def _req(params, key):
    if key not in params or params[key] is None:
        raise OpError("missing required parameter {0!r}".format(key))
    return params[key]


def _as_int(value, key):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        raise OpError("{0} must be a number, got {1!r}".format(key, value))


def _as_float(value, key):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise OpError("{0} must be a number, got {1!r}".format(key, value))


def _as_bool(value, key):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "yes", "1"):
            return True
        if low in ("false", "no", "0"):
            return False
    raise OpError("{0} must be true or false, got {1!r}".format(key, value))


def _abspath(path, key="path"):
    if not isinstance(path, str) or not path.strip():
        raise OpError("{0} must be a non-empty file path".format(key))
    return os.path.abspath(os.path.expanduser(path.strip()))


def _ensure_parent_dir(path):
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
        return True
    return False


# --------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------

def _documents():
    return list(_krita().documents())


def resolve_document(ref):
    krita = _krita()
    docs = _documents()

    if ref is None or ref == "" or ref == "active":
        doc = krita.activeDocument()
        if doc is not None:
            return doc
        if len(docs) == 1:
            return docs[0]
        if not docs:
            raise OpError(
                "No document is open in Krita. Use create_document or "
                "open_document first.", kind="no_document")
        raise OpError(
            "No document is active and {0} are open. Pass `document` with an "
            "index or a file name.".format(len(docs)), kind="ambiguous")

    if isinstance(ref, bool):
        raise OpError("document must be an index or a name, not a boolean")

    if isinstance(ref, int) or (isinstance(ref, str) and ref.lstrip("-").isdigit()):
        index = int(ref)
        if 0 <= index < len(docs):
            return docs[index]
        raise OpError(
            "Document index {0} is out of range; {1} document(s) are open."
            .format(index, len(docs)), kind="not_found")

    if not isinstance(ref, str):
        raise OpError("document must be an index or a name, got {0}".format(
            type(ref).__name__))

    needle = ref.strip()
    lowered = needle.lower()
    exact, partial = [], []
    for doc in docs:
        name = doc.name() or ""
        filename = doc.fileName() or ""
        base = os.path.basename(filename)
        if needle in (name, filename, base):
            exact.append(doc)
        elif lowered in name.lower() or lowered in filename.lower():
            partial.append(doc)

    hits = exact or partial
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise OpError(
            "No open document matches {0!r}. Open documents: {1}".format(
                ref, _document_labels(docs) or "(none)"), kind="not_found")
    raise OpError(
        "{0!r} matches {1} documents. Use the index instead: {2}".format(
            ref, len(hits), _document_labels(docs)), kind="ambiguous")


def _document_labels(docs):
    return ", ".join(
        "[{0}] {1}".format(i, d.fileName() or d.name() or "untitled")
        for i, d in enumerate(docs))


def _document_index(doc):
    for i, other in enumerate(_documents()):
        # libkis hands out fresh wrappers, so compare identity by file/name.
        if other.fileName() == doc.fileName() and other.name() == doc.name():
            return i
    return -1


def _doc_summary(doc):
    return {
        "index": _document_index(doc),
        "name": doc.name(),
        "file_name": doc.fileName() or None,
        "width": doc.width(),
        "height": doc.height(),
        "color_model": doc.colorModel(),
        "color_depth": doc.colorDepth(),
        "color_profile": doc.colorProfile(),
        "resolution_dpi": round(doc.resolution(), 3),
        "modified": bool(doc.modified()),
    }


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

def _children(node):
    """Children in docker order: topmost first."""
    return list(reversed(list(node.childNodes())))


def _node_uuid(node):
    try:
        raw = node.uniqueId()
    except Exception:
        return None
    try:
        text = raw.toString()
    except AttributeError:
        text = str(raw)
    return text.strip("{}") or None


def _insert_node(parent, child, ui_index):
    """Insert ``child`` so it lands at docker position ``ui_index`` (0 = top)."""
    siblings = list(parent.childNodes())  # bottom-first, libkis order
    count = len(siblings)
    ui_index = max(0, min(count, ui_index))
    below_pos = count - ui_index - 1
    above = siblings[below_pos] if below_pos >= 0 else None
    parent.addChildNode(child, above)


def _node_entry(node, path, index_path, index, active_uuid, active_name):
    uuid = _node_uuid(node)
    entry = {
        "name": node.name(),
        "type": node.type(),
        "path": path,
        "index_path": index_path,
        "index": index,
        "visible": bool(node.visible()),
        "locked": bool(node.locked()),
        "opacity": int(node.opacity()),
        "blending_mode": node.blendingMode(),
        "color_model": node.colorModel(),
        "color_depth": node.colorDepth(),
        "animated": bool(node.animated()),
    }
    if uuid:
        entry["uuid"] = uuid
    try:
        bounds = node.bounds()
        entry["bounds"] = {
            "x": bounds.x(), "y": bounds.y(),
            "width": bounds.width(), "height": bounds.height(),
        }
    except Exception:
        pass
    if uuid and active_uuid and uuid == active_uuid:
        entry["active"] = True
    elif not uuid and active_name is not None and node.name() == active_name:
        entry["active"] = True
    return entry


def _walk_layers(node, prefix, index_prefix, depth, max_depth,
                 active_uuid, active_name):
    out = []
    for i, child in enumerate(_children(node)):
        path = "{0}/{1}".format(prefix, child.name()) if prefix else child.name()
        index_path = "{0}/#{1}".format(index_prefix, i) if index_prefix else "#{0}".format(i)
        entry = _node_entry(child, path, index_path, i, active_uuid, active_name)
        kids = _children(child)
        if kids:
            entry["child_count"] = len(kids)
            if max_depth < 0 or depth + 1 < max_depth:
                entry["children"] = _walk_layers(
                    child, path, index_path, depth + 1, max_depth,
                    active_uuid, active_name)
        out.append(entry)
    return out


def layer_tree(doc, max_depth=-1):
    active = doc.activeNode()
    active_uuid = _node_uuid(active) if active is not None else None
    active_name = active.name() if active is not None and not active_uuid else None
    return _walk_layers(doc.rootNode(), "", "", 0, max_depth,
                        active_uuid, active_name)


def _flatten_tree(node, prefix, index_prefix, out):
    for i, child in enumerate(_children(node)):
        path = "{0}/{1}".format(prefix, child.name()) if prefix else child.name()
        index_path = "{0}/#{1}".format(index_prefix, i) if index_prefix else "#{0}".format(i)
        out.append((path, index_path, child))
        _flatten_tree(child, path, index_path, out)
    return out


def resolve_node(doc, ref, allow_root=False):
    """Resolve a layer reference. See the module docstring for the syntax."""
    if ref is None or ref == "" or ref == "active":
        node = doc.activeNode()
        if node is not None:
            return node
        kids = _children(doc.rootNode())
        if not kids:
            raise OpError("The document has no layers.", kind="not_found")
        return kids[0]

    if not isinstance(ref, str):
        raise OpError("layer must be a string reference, got {0}".format(
            type(ref).__name__))

    needle = ref.strip()
    if needle == "root":
        if not allow_root:
            raise OpError("The root node cannot be used for this operation.")
        return doc.rootNode()

    catalogue = _flatten_tree(doc.rootNode(), "", "", [])

    if needle.lower().startswith("uuid:"):
        wanted = needle[5:].strip().strip("{}").lower()
        for _path, _ip, node in catalogue:
            uuid = _node_uuid(node)
            if uuid and uuid.lower() == wanted:
                return node
        raise OpError("No layer with uuid {0!r}.".format(wanted), kind="not_found")

    for path, index_path, node in catalogue:
        if needle == path or needle == index_path:
            return node

    # walk the path allowing #N components and case-insensitive names
    parts = [p for p in needle.split("/") if p != ""]
    node = doc.rootNode()
    ok = True
    for part in parts:
        kids = _children(node)
        found = None
        if part.startswith("#") and part[1:].isdigit():
            idx = int(part[1:])
            if 0 <= idx < len(kids):
                found = kids[idx]
        if found is None:
            for kid in kids:
                if kid.name() == part:
                    found = kid
                    break
        if found is None:
            for kid in kids:
                if kid.name().lower() == part.lower():
                    found = kid
                    break
        if found is None:
            ok = False
            break
        node = found
    if ok and parts:
        return node

    # last resort: unique name match anywhere in the tree
    matches = [n for path, ip, n in catalogue if n.name().lower() == needle.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        paths = [p for p, ip, n in catalogue if n.name().lower() == needle.lower()]
        raise OpError(
            "{0!r} matches {1} layers ({2}). Use a full path or an index path."
            .format(ref, len(matches), ", ".join(paths)), kind="ambiguous")

    available = ", ".join(p for p, ip, n in catalogue) or "(none)"
    raise OpError(
        "No layer matches {0!r}. Layers in this document: {1}".format(
            ref, available), kind="not_found")


def _parent_of(doc, node):
    try:
        parent = node.parentNode()
    except Exception:
        parent = None
    if parent is not None:
        return parent
    # parentNode() returns None for the root; fall back to a search so a
    # detached wrapper still resolves to something sane.
    target_uuid = _node_uuid(node)
    stack = [doc.rootNode()]
    while stack:
        current = stack.pop()
        for child in current.childNodes():
            same = (_node_uuid(child) == target_uuid if target_uuid
                    else child.name() == node.name())
            if same:
                return current
            stack.append(child)
    return doc.rootNode()


def _refresh(doc):
    doc.refreshProjection()
    doc.waitForDone()


def _trace(text):
    """Write a step marker to the bridge trace log, if tracing is on."""
    try:
        from .httpserver import Trace, trace_file_path
        if os.environ.get("KRITA_MCP_TRACE") != "1":
            return
        Trace(trace_file_path(), True).write("    " + text)
    except Exception:
        pass


def _settle_after_close(budget=0.6):
    """Let Krita finish retiring a document before we answer the client.

    Document.close() only starts the teardown: views are dropped with
    deleteLater() and the document is retired from Krita's part list through
    the event loop. Until that finishes, Krita.documents() still hands back
    the dead document, and the next request -- which arrives a few
    milliseconds later -- reads freed memory and takes the process down.

    Dispatching DeferredDelete alone is not enough; measured over repeated
    runs, the teardown needs real event-loop time. The bridge's own queued
    signal can be delivered while we pump, so MainThreadInvoker defers any
    operation that arrives during this window instead of running it nested.
    """
    from .compat import QCoreApplication, QEvent, QEventLoop

    app = QCoreApplication.instance()
    if app is None:
        return
    deadline = time.time() + budget
    while time.time() < deadline:
        app.processEvents(QEventLoop.ExcludeUserInputEvents, 20)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        time.sleep(0.01)
    gc.collect()


def _describe(doc, node):
    return {
        "document": _doc_summary(doc),
        "layer": _node_entry(node, node.name(), "", -1, _node_uuid(node), None),
    }


# --------------------------------------------------------------------------
# status / discovery
# --------------------------------------------------------------------------

@op("status", timeout=10.0)
def op_status(params):
    krita = _krita()
    docs = _documents()
    window = krita.activeWindow()
    active = krita.activeDocument()
    return {
        "krita_version": krita.version(),
        "plugin_version": PLUGIN_VERSION,
        "python_version": sys.version.split()[0],
        "has_window": window is not None,
        "open_document_count": len(docs),
        "active_document": _doc_summary(active) if active is not None else None,
        "documents": [_doc_summary(d) for d in docs],
        # Krita's ambient setting, not the live one: dispatch turns batch mode
        # on around every operation, so reading it here would always say True.
        "batch_mode": bool(_batch_saved),
    }


@op("list_documents", timeout=10.0)
def op_list_documents(params):
    docs = _documents()
    active = _krita().activeDocument()
    active_file = active.fileName() if active is not None else None
    active_name = active.name() if active is not None else None
    out = []
    for i, doc in enumerate(docs):
        entry = _doc_summary(doc)
        entry["index"] = i
        entry["active"] = (
            active is not None
            and doc.fileName() == active_file
            and doc.name() == active_name
        )
        out.append(entry)
    return {"count": len(out), "documents": out}


@op("document_info", timeout=15.0)
def op_document_info(params):
    doc = resolve_document(params.get("document"))
    max_depth = _as_int(_arg(params, "max_depth", -1), "max_depth")
    info = _doc_summary(doc)
    info["layers"] = layer_tree(doc, max_depth)
    active = doc.activeNode()
    info["active_layer"] = active.name() if active is not None else None
    selection = doc.selection()
    if selection is not None:
        info["selection"] = {
            "x": selection.x(), "y": selection.y(),
            "width": selection.width(), "height": selection.height(),
        }
    else:
        info["selection"] = None
    return info


@op("list_blending_modes", timeout=10.0)
def op_list_blending_modes(params):
    return {
        "count": len(COMMON_BLENDING_MODES),
        "blending_modes": list(COMMON_BLENDING_MODES),
        "note": ("Krita 5.3 has no API for enumerating composite ops, so this "
                 "is the curated set of ids this bridge recognises rather "
                 "than everything Krita supports. Other valid ids still work; "
                 "set_layer just adds a warning for ids it does not know."),
    }


@op("list_filters", timeout=10.0)
def op_list_filters(params):
    krita = _krita()
    names = list(krita.filters())
    detail = _as_bool(_arg(params, "include_parameters", False), "include_parameters")
    if not detail:
        return {"count": len(names), "filters": names}
    out = []
    for name in names:
        entry = {"name": name}
        try:
            flt = krita.filter(name)
            cfg = flt.configuration()
            entry["parameters"] = dict(cfg.properties())
        except Exception as exc:
            entry["parameters_error"] = str(exc)
        out.append(entry)
    return {"count": len(out), "filters": out}


# --------------------------------------------------------------------------
# document lifecycle
# --------------------------------------------------------------------------

@op("create_document", timeout=60.0, mutates=True)
def op_create_document(params):
    krita = _krita()
    width = _as_int(_req(params, "width"), "width")
    height = _as_int(_req(params, "height"), "height")
    if width < 1 or height < 1 or width > 40000 or height > 40000:
        raise OpError("width and height must be between 1 and 40000 pixels.")

    name = str(_arg(params, "name", "Untitled"))
    color_model = str(_arg(params, "color_model", "RGBA"))
    color_depth = str(_arg(params, "color_depth", "U8"))
    profile = str(_arg(params, "color_profile", ""))
    resolution = _as_float(_arg(params, "resolution_dpi", 300.0), "resolution_dpi")

    doc = krita.createDocument(width, height, name, color_model, color_depth,
                               profile, resolution)
    if doc is None:
        raise OpError(
            "Krita refused to create the document. Check that color_model "
            "{0!r} / color_depth {1!r} / profile {2!r} are a valid combination "
            "(see list_color_spaces).".format(color_model, color_depth, profile))
    doc.setBatchmode(True)

    background = params.get("background")
    if background is not None:
        color = parse_color(background)
        node = doc.activeNode() or _children(doc.rootNode())[0]
        if imaging.supports_pixel_ops(node):
            image = QImage(width, height, QImage.Format_ARGB32)
            image.fill(color)
            imaging.write_node_image(node, QRect(0, 0, width, height), image)

    window = krita.activeWindow()
    view_added = False
    if window is not None:
        window.addView(doc)
        view_added = True
    _refresh(doc)

    result = _doc_summary(doc)
    result["view_added"] = view_added
    if not view_added:
        result["note"] = ("No Krita main window was available, so the document "
                          "is open in memory but not shown.")
    return result


@op("open_document", timeout=120.0, mutates=True)
def op_open_document(params):
    krita = _krita()
    path = _abspath(_req(params, "path"))
    if not os.path.isfile(path):
        raise OpError("No file at {0}".format(path), kind="not_found")

    doc = krita.openDocument(path)
    if doc is None:
        raise OpError(
            "Krita could not open {0}. The format may be unsupported or the "
            "file may be damaged.".format(path))
    doc.setBatchmode(True)
    window = krita.activeWindow()
    if window is not None:
        window.addView(doc)
    _refresh(doc)
    return _doc_summary(doc)


@op("save_document", timeout=180.0, mutates=True)
def op_save_document(params):
    doc = resolve_document(params.get("document"))
    path = params.get("path")
    previous = doc.batchmode()
    doc.setBatchmode(True)
    try:
        if path:
            target = _abspath(path)
            created = _ensure_parent_dir(target)
            ok = doc.saveAs(target)
        else:
            target = doc.fileName()
            created = False
            if not target:
                raise OpError(
                    "This document has never been saved, so `path` is "
                    "required.")
            ok = doc.save()
        doc.waitForDone()
    finally:
        doc.setBatchmode(previous)

    if not ok:
        raise OpError(
            "Krita reported a failure saving to {0}. Check the extension is a "
            "format Krita can write (.kra, .png, .jpg, .tif, .psd, ...) and "
            "that the path is writable.".format(target))
    return {"saved": True, "path": target, "created_directory": created,
            "document": _doc_summary(doc)}


@op("export_document", timeout=180.0, mutates=False)
def op_export_document(params):
    from krita import InfoObject

    doc = resolve_document(params.get("document"))
    target = _abspath(_req(params, "path"))
    created = _ensure_parent_dir(target)

    info = InfoObject()
    options = _arg(params, "options", {})
    if not isinstance(options, dict):
        raise OpError("options must be an object of exporter settings")
    for key, value in options.items():
        info.setProperty(key, value)

    previous = doc.batchmode()
    doc.setBatchmode(True)
    try:
        _refresh(doc)
        ok = doc.exportImage(target, info)
        doc.waitForDone()
    finally:
        doc.setBatchmode(previous)

    if not ok or not os.path.isfile(target):
        raise OpError(
            "Export to {0} failed. Krita infers the format from the file "
            "extension; make sure it is one Krita can write.".format(target))
    return {"exported": True, "path": target,
            "bytes": os.path.getsize(target),
            "created_directory": created}


@op("close_document", timeout=60.0, mutates=True)
def op_close_document(params):
    doc = resolve_document(params.get("document"))
    save_first = _as_bool(_arg(params, "save", False), "save")
    summary = _doc_summary(doc)
    if save_first:
        if not doc.fileName():
            raise OpError("Cannot save before closing: the document has no "
                          "file name. Call save_document with a path first.")
        doc.setBatchmode(True)
        if not doc.save():
            raise OpError("Saving failed, so the document was left open.")
        doc.waitForDone()
    elif doc.modified():
        if not _as_bool(_arg(params, "discard_changes", False), "discard_changes"):
            raise OpError(
                "{0} has unsaved changes. Pass save=true to save it first, or "
                "discard_changes=true to throw them away."
                .format(summary["name"]), kind="unsaved_changes")

    _trace("close: about to close {0!r}".format(summary["name"]))
    doc.setBatchmode(True)
    # Document.close() closes the document's views, and a view whose document
    # is still dirty puts up a modal "save changes?" prompt. That dialog runs
    # a nested event loop on the UI thread, which would wedge the whole bridge
    # until someone clicked it by hand. Clearing the flag first means the
    # prompt can never appear -- the decision to discard was already made
    # above.
    try:
        doc.setModified(False)
    except Exception:
        pass
    # Filters, scaling, rotation and flatten all run as asynchronous strokes.
    # Destroying the image while one is still in flight crashes Krita, so make
    # sure the scheduler is idle before anything is torn down. Autosave is a
    # per-document timer that also touches the document behind our back.
    try:
        doc.setAutosave(False)
    except Exception:
        pass
    try:
        doc.waitForDone()
    except Exception:
        pass

    # Force a collection while the document is still valid. Any libkis wrapper
    # left unreachable by an earlier operation is destroyed here, rather than
    # at some arbitrary later point when its C++ object is already gone.
    gc.collect()

    # KNOWN KRITA DEFECT (5.3.3): tearing a document down occasionally faults
    # inside Krita's own C++ teardown, taking the process with it. Measured at
    # roughly one close in five at the end of a long editing session; a clean
    # document closes reliably. It reproduces identically through Krita's own
    # File > Close action, so it is not an artefact of using libkis, and
    # nothing available from a plugin prevents it -- draining the scheduler,
    # disabling autosave, clearing the modified flag, collecting stale
    # wrappers and pumping the event loop were all tried and none of them
    # removes it. Everything above narrows the window; see README.md.
    _trace("close: calling Document.close()")
    doc.close()
    _trace("close: close call returned")
    _settle_after_close()
    _trace("close: settled")
    return {"closed": True, "document": summary}


# --------------------------------------------------------------------------
# canvas geometry
# --------------------------------------------------------------------------

@op("resize_canvas", timeout=120.0, mutates=True)
def op_resize_canvas(params):
    doc = resolve_document(params.get("document"))
    x = _as_int(_arg(params, "x", 0), "x")
    y = _as_int(_arg(params, "y", 0), "y")
    width = _as_int(_req(params, "width"), "width")
    height = _as_int(_req(params, "height"), "height")
    if width < 1 or height < 1:
        raise OpError("width and height must be at least 1")
    before = _doc_summary(doc)
    doc.resizeImage(x, y, width, height)
    _refresh(doc)
    return {"before": before, "after": _doc_summary(doc)}


@op("scale_image", timeout=180.0, mutates=True)
def op_scale_image(params):
    doc = resolve_document(params.get("document"))
    width = _as_int(_req(params, "width"), "width")
    height = _as_int(_req(params, "height"), "height")
    if width < 1 or height < 1:
        raise OpError("width and height must be at least 1")
    strategy = str(_arg(params, "strategy", "Bicubic"))
    xres = _as_float(_arg(params, "x_res", doc.xRes()), "x_res")
    yres = _as_float(_arg(params, "y_res", doc.yRes()), "y_res")
    before = _doc_summary(doc)
    doc.scaleImage(width, height, int(xres), int(yres), strategy)
    _refresh(doc)
    return {"before": before, "after": _doc_summary(doc),
            "strategy": strategy}


@op("rotate_image", timeout=180.0, mutates=True)
def op_rotate_image(params):
    import math
    doc = resolve_document(params.get("document"))
    degrees = _as_float(_req(params, "degrees"), "degrees")
    before = _doc_summary(doc)
    doc.rotateImage(math.radians(degrees))
    _refresh(doc)
    return {"before": before, "after": _doc_summary(doc), "degrees": degrees}


@op("crop_image", timeout=120.0, mutates=True)
def op_crop_image(params):
    doc = resolve_document(params.get("document"))
    x = _as_int(_req(params, "x"), "x")
    y = _as_int(_req(params, "y"), "y")
    width = _as_int(_req(params, "width"), "width")
    height = _as_int(_req(params, "height"), "height")
    if width < 1 or height < 1:
        raise OpError("width and height must be at least 1")
    before = _doc_summary(doc)
    doc.crop(x, y, width, height)
    _refresh(doc)
    return {"before": before, "after": _doc_summary(doc)}


@op("flatten_image", timeout=120.0, mutates=True)
def op_flatten_image(params):
    doc = resolve_document(params.get("document"))
    doc.flatten()
    _refresh(doc)
    return {"flattened": True, "layers": layer_tree(doc, 1)}


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------

@op("list_layers", timeout=15.0)
def op_list_layers(params):
    doc = resolve_document(params.get("document"))
    max_depth = _as_int(_arg(params, "max_depth", -1), "max_depth")
    tree = layer_tree(doc, max_depth)
    return {"document": _doc_summary(doc), "layers": tree}


_LAYER_TYPES = {
    "paintlayer": "paintlayer",
    "paint": "paintlayer",
    "grouplayer": "grouplayer",
    "group": "grouplayer",
    "filelayer": "filelayer",
    "filterlayer": "filterlayer",
    "filllayer": "filllayer",
    "clonelayer": "clonelayer",
    "vectorlayer": "vectorlayer",
    "transparencymask": "transparencymask",
    "filtermask": "filtermask",
    "transformmask": "transformmask",
    "selectionmask": "selectionmask",
    "colorizemask": "colorizemask",
}


@op("create_layer", timeout=60.0, mutates=True)
def op_create_layer(params):
    doc = resolve_document(params.get("document"))
    name = str(_arg(params, "name", "Layer"))
    raw_type = str(_arg(params, "type", "paintlayer")).lower().replace(" ", "")
    if raw_type not in _LAYER_TYPES:
        raise OpError("Unknown layer type {0!r}. Supported: {1}".format(
            raw_type, ", ".join(sorted(set(_LAYER_TYPES.values())))))
    node_type = _LAYER_TYPES[raw_type]

    if node_type == "grouplayer":
        node = doc.createGroupLayer(name)
    else:
        node = doc.createNode(name, node_type)
    if node is None:
        raise OpError("Krita could not create a {0} named {1!r}.".format(
            node_type, name))

    parent_ref = params.get("parent")
    parent = (resolve_node(doc, parent_ref, allow_root=True)
              if parent_ref not in (None, "") else doc.rootNode())
    if parent_ref not in (None, "") and parent.type() != "grouplayer" \
            and parent is not doc.rootNode():
        raise OpError("parent {0!r} is a {1}; layers can only be nested inside "
                      "a group layer.".format(parent_ref, parent.type()))

    sibling_count = len(_children(parent))
    index = params.get("index")
    ui_index = 0 if index is None else _as_int(index, "index")
    if ui_index < 0:
        ui_index = max(0, sibling_count + 1 + ui_index)
    _insert_node(parent, node, ui_index)

    if _as_bool(_arg(params, "select", True), "select"):
        doc.setActiveNode(node)

    _changed, warnings = _apply_layer_properties(node, params)
    _refresh(doc)

    tree = layer_tree(doc, -1)
    result = {"created": True, "layer": _find_in_tree(tree, node) or
              _node_entry(node, node.name(), "", ui_index, _node_uuid(node), None),
              "layers": tree}
    if warnings:
        result["warnings"] = warnings
    return result


def _find_in_tree(tree, node):
    uuid = _node_uuid(node)
    if not uuid:
        return None
    stack = list(tree)
    while stack:
        entry = stack.pop()
        if entry.get("uuid") == uuid:
            return entry
        stack.extend(entry.get("children", []))
    return None


def _apply_layer_properties(node, params):
    """Apply the shared layer properties. Returns (changed, warnings)."""
    changed, warnings = [], []
    if "opacity" in params and params["opacity"] is not None:
        value = _as_float(params["opacity"], "opacity")
        if 0.0 <= value <= 1.0 and not float(value).is_integer():
            value = value * 255.0  # accept 0..1 as a convenience
        opacity = max(0, min(255, int(round(value))))
        node.setOpacity(opacity)
        changed.append("opacity")
    if "blending_mode" in params and params["blending_mode"] is not None:
        mode = str(params["blending_mode"]).strip()
        canonical = _BLENDING_LOOKUP.get(mode.lower())
        if canonical is None:
            warnings.append(
                "{0!r} is not one of the blending mode ids this bridge knows. "
                "It has been set anyway, but if Krita does not recognise it "
                "the layer will render as Normal. Known ids: {1}"
                .format(mode, ", ".join(COMMON_BLENDING_MODES)))
        else:
            mode = canonical
        node.setBlendingMode(mode)
        changed.append("blending_mode")
    if "visible" in params and params["visible"] is not None:
        node.setVisible(_as_bool(params["visible"], "visible"))
        changed.append("visible")
    if "locked" in params and params["locked"] is not None:
        node.setLocked(_as_bool(params["locked"], "locked"))
        changed.append("locked")
    if "new_name" in params and params["new_name"]:
        node.setName(str(params["new_name"]))
        changed.append("name")
    return changed, warnings


@op("set_layer", timeout=30.0, mutates=True)
def op_set_layer(params):
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, params.get("layer"))
    changed, warnings = _apply_layer_properties(node, params)
    if _as_bool(_arg(params, "select", False), "select"):
        doc.setActiveNode(node)
        changed.append("active")
    _refresh(doc)
    result = {"changed": changed,
              "layer": _find_in_tree(layer_tree(doc), node) or
              _node_entry(node, node.name(), "", -1, _node_uuid(node), None)}
    if warnings:
        result["warnings"] = warnings
    return result


@op("select_layer", timeout=15.0, mutates=True)
def op_select_layer(params):
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, _req(params, "layer"))
    doc.setActiveNode(node)
    return {"active_layer": node.name(),
            "layer": _find_in_tree(layer_tree(doc), node)}


@op("delete_layer", timeout=60.0, mutates=True)
def op_delete_layer(params):
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, _req(params, "layer"))
    if len(_flatten_tree(doc.rootNode(), "", "", [])) <= 1:
        raise OpError("A document must keep at least one layer.")
    name = node.name()
    node.remove()
    _refresh(doc)
    return {"deleted": name, "layers": layer_tree(doc)}


@op("duplicate_layer", timeout=90.0, mutates=True)
def op_duplicate_layer(params):
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, params.get("layer"))
    copy = node.duplicate()
    if copy is None:
        raise OpError("Krita could not duplicate {0!r}.".format(node.name()))
    copy.setName(str(_arg(params, "name", node.name() + " copy")))
    parent = _parent_of(doc, node)
    siblings = _children(parent)
    position = 0
    for i, sibling in enumerate(siblings):
        if _node_uuid(sibling) == _node_uuid(node):
            position = i
            break
    _insert_node(parent, copy, position)
    if _as_bool(_arg(params, "select", True), "select"):
        doc.setActiveNode(copy)
    _refresh(doc)
    return {"duplicated": node.name(), "new_layer": copy.name(),
            "layers": layer_tree(doc)}


@op("move_layer", timeout=90.0, mutates=True)
def op_move_layer(params):
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, _req(params, "layer"))
    name = node.name()

    current_parent = _parent_of(doc, node)
    parent_ref = params.get("parent")
    if parent_ref in (None, ""):
        target_parent = current_parent
    else:
        target_parent = resolve_node(doc, parent_ref, allow_root=True)
        if target_parent.type() != "grouplayer" and target_parent is not doc.rootNode():
            raise OpError("parent {0!r} is a {1}; only group layers can hold "
                          "children.".format(parent_ref, target_parent.type()))

    # Refuse to move a group into its own subtree, which would detach the tree.
    descendants = {_node_uuid(n) for _p, _i, n in _flatten_tree(node, "", "", [])}
    if _node_uuid(target_parent) in descendants:
        raise OpError("Cannot move {0!r} into one of its own children.".format(name))

    index = params.get("index")
    ui_index = 0 if index is None else _as_int(index, "index")

    current_parent.removeChildNode(node)
    sibling_count = len(_children(target_parent))
    if ui_index < 0:
        ui_index = max(0, sibling_count + 1 + ui_index)
    _insert_node(target_parent, node, ui_index)
    doc.setActiveNode(node)
    _refresh(doc)
    return {"moved": name, "layers": layer_tree(doc)}


@op("merge_layer_down", timeout=120.0, mutates=True)
def op_merge_layer_down(params):
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, params.get("layer"))
    parent = _parent_of(doc, node)
    siblings = _children(parent)
    position = None
    for i, sibling in enumerate(siblings):
        if _node_uuid(sibling) == _node_uuid(node):
            position = i
            break
    if position is None or position >= len(siblings) - 1:
        raise OpError("{0!r} is the bottom layer of its group; there is "
                      "nothing under it to merge into.".format(node.name()))
    name = node.name()
    node.mergeDown()
    _refresh(doc)
    return {"merged": name, "layers": layer_tree(doc)}


# --------------------------------------------------------------------------
# reading pixels
# --------------------------------------------------------------------------

@op("get_image", timeout=90.0)
def op_get_image(params):
    doc = resolve_document(params.get("document"))
    bounds = QRect(0, 0, doc.width(), doc.height())

    region = params.get("region")
    if region:
        if not isinstance(region, dict):
            raise OpError("region must be an object with x, y, width, height")
        rect = QRect(
            _as_int(region.get("x", 0), "region.x"),
            _as_int(region.get("y", 0), "region.y"),
            _as_int(_req(region, "width"), "region.width"),
            _as_int(_req(region, "height"), "region.height"),
        )
        rect = imaging.clamp_rect(rect, bounds)
    else:
        rect = bounds

    layer_ref = params.get("layer")
    _refresh(doc)
    if layer_ref in (None, "", "merged"):
        source = "merged"
        image = doc.projection(rect.x(), rect.y(), rect.width(), rect.height())
    else:
        node = resolve_node(doc, layer_ref)
        source = node.name()
        raw = node.projectionPixelData(rect.x(), rect.y(),
                                       rect.width(), rect.height())
        data = bytes(raw)
        expected = rect.width() * rect.height() * 4
        if len(data) != expected:
            raise OpError(
                "Layer {0!r} is {1}/{2}; only RGBA/U8 layers can be read "
                "directly. Read the merged image instead."
                .format(node.name(), node.colorModel(), node.colorDepth()))
        image = QImage(data, rect.width(), rect.height(), rect.width() * 4,
                       QImage.Format_ARGB32).copy()

    if image is None or image.isNull():
        raise OpError("Krita returned an empty image for that region.")

    max_size = _as_int(_arg(params, "max_size", 1024), "max_size")
    max_size = max(0, min(4096, max_size))
    scaled, factor = imaging.scale_to_fit(image, max_size)

    result = {
        "source": source,
        "region": {"x": rect.x(), "y": rect.y(),
                   "width": rect.width(), "height": rect.height()},
        "returned_size": {"width": scaled.width(), "height": scaled.height()},
        "scale": round(factor, 6),
        "document": _doc_summary(doc),
    }
    if _as_bool(_arg(params, "include_data", True), "include_data"):
        result["png_base64"] = imaging.qimage_to_png_b64(scaled)
    return result


@op("get_pixel", timeout=20.0)
def op_get_pixel(params):
    doc = resolve_document(params.get("document"))
    x = _as_int(_req(params, "x"), "x")
    y = _as_int(_req(params, "y"), "y")
    rect = imaging.clamp_rect(QRect(x, y, 1, 1),
                              QRect(0, 0, doc.width(), doc.height()))
    _refresh(doc)
    layer_ref = params.get("layer")
    if layer_ref in (None, "", "merged"):
        image = doc.projection(rect.x(), rect.y(), 1, 1)
        source = "merged"
    else:
        node = resolve_node(doc, layer_ref)
        source = node.name()
        image = imaging.read_node_image(node, rect)
    color = QColor(image.pixel(0, 0))
    alpha = QColor.fromRgba(image.pixel(0, 0)).alpha()
    return {
        "source": source, "x": x, "y": y,
        "rgba": [color.red(), color.green(), color.blue(), alpha],
        "hex": "#{0:02x}{1:02x}{2:02x}{3:02x}".format(
            color.red(), color.green(), color.blue(), alpha),
    }


# --------------------------------------------------------------------------
# drawing
# --------------------------------------------------------------------------

_CAPS = {"flat": Qt.FlatCap, "square": Qt.SquareCap, "round": Qt.RoundCap}
_JOINS = {"miter": Qt.MiterJoin, "bevel": Qt.BevelJoin, "round": Qt.RoundJoin}


def _points(cmd, key="points"):
    raw = cmd.get(key)
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        raise OpError("{0} needs at least two [x, y] points".format(key))
    out = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise OpError("each point must be [x, y], got {0!r}".format(item))
        out.append((_as_float(item[0], "x"), _as_float(item[1], "y")))
    return out


def _prepare(cmd, index):
    """Validate one draw command and work out the pixels it can touch."""
    if not isinstance(cmd, dict):
        raise OpError("commands[{0}] must be an object".format(index))
    kind = str(cmd.get("type", "")).lower().strip()
    if not kind:
        raise OpError("commands[{0}] is missing `type`".format(index))

    prep = {"type": kind, "cmd": cmd}
    prep["stroke_width"] = max(0.0, _as_float(cmd.get("stroke_width", 1.0),
                                              "stroke_width"))
    pad = prep["stroke_width"] + 2.0

    def rect_of(x, y, w, h):
        return QRect(int(x - pad) - 1, int(y - pad) - 1,
                     int(w + 2 * pad) + 3, int(h + 2 * pad) + 3)

    def size_of(cmd_):
        """Geometry size, from either `w`/`h` or `width`/`height`."""
        w = cmd_.get("w", cmd_.get("width"))
        h = cmd_.get("h", cmd_.get("height"))
        if w is None or h is None:
            raise OpError(
                "commands[{0}] ({1}) needs `w` and `h` (or `width` and "
                "`height`). Use `stroke_width` for the outline thickness."
                .format(index, kind))
        return _as_float(w, "w"), _as_float(h, "h")

    if kind in ("rect", "rectangle", "fill_rect", "clear", "ellipse", "circle",
                "linear_gradient", "radial_gradient", "image"):
        x = _as_float(cmd.get("x", 0), "x")
        y = _as_float(cmd.get("y", 0), "y")
        if kind == "circle":
            radius = _as_float(_req(cmd, "radius"), "radius")
            cx = _as_float(cmd.get("cx", x), "cx")
            cy = _as_float(cmd.get("cy", y), "cy")
            prep["geom"] = (cx - radius, cy - radius, radius * 2, radius * 2)
        else:
            w, h = size_of(cmd)
            prep["geom"] = (x, y, w, h)
        gx, gy, gw, gh = prep["geom"]
        prep["bbox"] = rect_of(gx, gy, gw, gh)

    elif kind == "line":
        x1 = _as_float(_req(cmd, "x1"), "x1")
        y1 = _as_float(_req(cmd, "y1"), "y1")
        x2 = _as_float(_req(cmd, "x2"), "x2")
        y2 = _as_float(_req(cmd, "y2"), "y2")
        prep["geom"] = (x1, y1, x2, y2)
        prep["bbox"] = rect_of(min(x1, x2), min(y1, y2),
                               abs(x2 - x1), abs(y2 - y1))

    elif kind in ("polyline", "polygon"):
        pts = _points(cmd)
        prep["geom"] = pts
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        prep["bbox"] = rect_of(min(xs), min(ys),
                               max(xs) - min(xs), max(ys) - min(ys))

    elif kind == "text":
        text = cmd.get("text")
        if not isinstance(text, str) or text == "":
            raise OpError("commands[{0}] (text) needs a non-empty `text`"
                          .format(index))
        font = QFont(str(cmd.get("font", "Sans Serif")))
        font.setPixelSize(max(1, _as_int(cmd.get("size", 24), "size")))
        font.setBold(bool(cmd.get("bold", False)))
        font.setItalic(bool(cmd.get("italic", False)))
        prep["font"] = font
        metrics = QFontMetricsF(font)
        bounds = metrics.boundingRect(text)
        x = _as_float(cmd.get("x", 0), "x")
        y = _as_float(cmd.get("y", 0), "y")
        anchor = str(cmd.get("anchor", "top-left")).lower()
        if anchor not in ("top-left", "baseline", "center"):
            raise OpError("anchor must be top-left, baseline or center")
        prep["anchor"] = anchor
        prep["metrics"] = metrics
        prep["geom"] = (x, y, text)
        width = bounds.width() + metrics.averageCharWidth() * 2 + 8
        height = metrics.height() * (text.count("\n") + 1) + 8
        if anchor == "center":
            ox, oy = x - width / 2.0, y - height / 2.0
        elif anchor == "baseline":
            ox, oy = x - 4, y - metrics.ascent() - 4
        else:
            ox, oy = x - 4, y - 4
        prep["bbox"] = rect_of(ox, oy, width, height)

    else:
        raise OpError(
            "commands[{0}]: unknown draw type {1!r}. Supported: rect, "
            "fill_rect, ellipse, circle, line, polyline, polygon, text, "
            "clear, linear_gradient, radial_gradient, image.".format(index, kind))

    return prep


def _pen_for(cmd, prep):
    color = cmd.get("color")
    if color is None and cmd.get("stroke") is None:
        return QPen(Qt.NoPen)
    pen = QPen(parse_color(cmd.get("stroke", color)))
    pen.setWidthF(max(0.0, prep["stroke_width"]))
    pen.setCapStyle(_CAPS.get(str(cmd.get("cap", "round")).lower(), Qt.RoundCap))
    pen.setJoinStyle(_JOINS.get(str(cmd.get("join", "round")).lower(), Qt.RoundJoin))
    return pen


def _brush_for(cmd):
    fill = cmd.get("fill")
    if fill is None:
        return QBrush(Qt.NoBrush)
    return QBrush(parse_color(fill))


def _gradient_stops(cmd):
    stops = cmd.get("stops")
    if not isinstance(stops, (list, tuple)) or len(stops) < 2:
        raise OpError("gradients need at least two stops, e.g. "
                      "[[0, \"#000\"], [1, \"#fff\"]]")
    out = []
    for item in stops:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise OpError("each stop must be [position, colour]")
        out.append((max(0.0, min(1.0, _as_float(item[0], "stop position"))),
                    parse_color(item[1])))
    return out


def _run_command(painter, prep, doc_rect):
    cmd = prep["cmd"]
    kind = prep["type"]

    if kind == "clear":
        x, y, w, h = prep["geom"]
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(QRectF(x, y, w, h), Qt.transparent)
        painter.restore()
        return

    if kind == "fill_rect":
        x, y, w, h = prep["geom"]
        painter.fillRect(QRectF(x, y, w, h),
                         parse_color(cmd.get("color", cmd.get("fill")), "#000000"))
        return

    if kind in ("rect", "rectangle"):
        x, y, w, h = prep["geom"]
        painter.setPen(_pen_for(cmd, prep))
        painter.setBrush(_brush_for(cmd))
        radius = _as_float(cmd.get("radius", 0), "radius")
        if radius > 0:
            painter.drawRoundedRect(QRectF(x, y, w, h), radius, radius)
        else:
            painter.drawRect(QRectF(x, y, w, h))
        return

    if kind in ("ellipse", "circle"):
        x, y, w, h = prep["geom"]
        painter.setPen(_pen_for(cmd, prep))
        painter.setBrush(_brush_for(cmd))
        painter.drawEllipse(QRectF(x, y, w, h))
        return

    if kind == "line":
        x1, y1, x2, y2 = prep["geom"]
        pen = _pen_for(cmd, prep)
        if pen.style() == Qt.NoPen:
            pen = QPen(parse_color(cmd.get("color", "#000000")))
            pen.setWidthF(max(0.0, prep["stroke_width"]))
        painter.setPen(pen)
        painter.drawLine(QLineF(x1, y1, x2, y2))
        return

    if kind in ("polyline", "polygon"):
        pts = [QPointF(px, py) for px, py in prep["geom"]]
        painter.setPen(_pen_for(cmd, prep))
        painter.setBrush(_brush_for(cmd))
        closed = kind == "polygon" or bool(cmd.get("close", False))
        if closed:
            painter.drawPolygon(QPolygonF(pts))
        else:
            painter.setBrush(Qt.NoBrush)
            painter.drawPolyline(QPolygonF(pts))
        return

    if kind == "text":
        x, y, text = prep["geom"]
        metrics = prep["metrics"]
        painter.setFont(prep["font"])
        painter.setPen(QPen(parse_color(cmd.get("color", "#000000"))))
        lines = text.split("\n")
        line_height = metrics.height()
        for i, line in enumerate(lines):
            if prep["anchor"] == "baseline":
                bx, by = x, y + i * line_height
            elif prep["anchor"] == "center":
                total = line_height * len(lines)
                bx = x - metrics.horizontalAdvance(line) / 2.0
                by = y - total / 2.0 + metrics.ascent() + i * line_height
            else:
                bx, by = x, y + metrics.ascent() + i * line_height
            painter.drawText(QPointF(bx, by), line)
        return

    if kind in ("linear_gradient", "radial_gradient"):
        x, y, w, h = prep["geom"]
        stops = _gradient_stops(cmd)
        if kind == "linear_gradient":
            angle = str(cmd.get("direction", "horizontal")).lower()
            if angle == "vertical":
                grad = QLinearGradient(x, y, x, y + h)
            elif angle == "diagonal":
                grad = QLinearGradient(x, y, x + w, y + h)
            else:
                grad = QLinearGradient(x, y, x + w, y)
        else:
            grad = QRadialGradient(x + w / 2.0, y + h / 2.0, max(w, h) / 2.0)
        for position, color in stops:
            grad.setColorAt(position, color)
        painter.fillRect(QRectF(x, y, w, h), QBrush(grad))
        return

    if kind == "image":
        x, y, w, h = prep["geom"]
        source = imaging.png_b64_to_qimage(_req(cmd, "data"))
        if int(w) > 0 and int(h) > 0 and (source.width() != int(w)
                                          or source.height() != int(h)):
            source = source.scaled(int(w), int(h), Qt.IgnoreAspectRatio,
                                   Qt.SmoothTransformation)
        painter.drawImage(int(x), int(y), source)
        return

    raise OpError("unhandled draw type {0!r}".format(kind))


@op("draw", timeout=120.0, mutates=True)
def op_draw(params):
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, params.get("layer"))
    imaging.require_pixel_ops(node)
    if node.locked():
        raise OpError("Layer {0!r} is locked; unlock it with set_layer before "
                      "drawing.".format(node.name()))
    if node.type() != "paintlayer":
        raise OpError(
            "Layer {0!r} is a {1}. Drawing writes raw pixels, so it needs a "
            "paint layer. Create one with create_layer."
            .format(node.name(), node.type()))

    commands = params.get("commands")
    if not isinstance(commands, (list, tuple)) or not commands:
        raise OpError("`commands` must be a non-empty array of draw commands")
    if len(commands) > 2000:
        raise OpError("Too many commands in one call ({0}); split into batches "
                      "of 2000 or fewer.".format(len(commands)))

    preps = [_prepare(cmd, i) for i, cmd in enumerate(commands)]

    doc_rect = QRect(0, 0, doc.width(), doc.height())
    region = QRect()
    for prep in preps:
        region = region.united(prep["bbox"])
    region = region.intersected(doc_rect)
    if region.width() <= 0 or region.height() <= 0:
        raise OpError(
            "Every command falls outside the {0}x{1} canvas, so nothing would "
            "change.".format(doc.width(), doc.height()))

    canvas = imaging.read_node_image(node, region)
    canvas = canvas.convertToFormat(QImage.Format_ARGB32_Premultiplied)

    painter = QPainter()
    if not painter.begin(canvas):
        raise OpError("Could not start painting on the layer buffer.")
    try:
        painter.setRenderHint(
            QPainter.Antialiasing,
            _as_bool(_arg(params, "antialias", True), "antialias"))
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.translate(-region.x(), -region.y())
        for prep in preps:
            _run_command(painter, prep, doc_rect)
    finally:
        painter.end()

    imaging.write_node_image(node, region,
                             canvas.convertToFormat(QImage.Format_ARGB32))
    _refresh(doc)

    return {
        "drawn": len(preps),
        "layer": node.name(),
        "region": {"x": region.x(), "y": region.y(),
                   "width": region.width(), "height": region.height()},
        "note": ("Pixels were written directly, which Krita's undo history "
                 "does not record. Delete the layer to revert."),
    }


# --------------------------------------------------------------------------
# filters and selection
# --------------------------------------------------------------------------

@op("apply_filter", timeout=180.0, mutates=True)
def op_apply_filter(params):
    krita = _krita()
    doc = resolve_document(params.get("document"))
    node = resolve_node(doc, params.get("layer"))
    name = str(_req(params, "filter"))

    flt = krita.filter(name)
    if flt is None:
        available = list(krita.filters())
        match = [f for f in available if f.lower() == name.lower()]
        if match:
            flt = krita.filter(match[0])
            name = match[0]
        else:
            raise OpError(
                "{0!r} is not a Krita filter. Call list_filters to see the "
                "{1} available ones.".format(name, len(available)))

    settings = _arg(params, "settings", {})
    if not isinstance(settings, dict):
        raise OpError("settings must be an object of filter parameters")
    applied_settings = {}
    if settings:
        cfg = flt.configuration()
        known = dict(cfg.properties())
        for key, value in settings.items():
            if key not in known:
                raise OpError(
                    "Filter {0!r} has no parameter {1!r}. Its parameters are: "
                    "{2}".format(name, key, ", ".join(sorted(known)) or "(none)"))
            cfg.setProperty(key, value)
            applied_settings[key] = value
        flt.setConfiguration(cfg)

    canvas = QRect(0, 0, doc.width(), doc.height())
    region = params.get("region")
    if region:
        rect = imaging.clamp_rect(
            QRect(_as_int(region.get("x", 0), "region.x"),
                  _as_int(region.get("y", 0), "region.y"),
                  _as_int(_req(region, "width"), "region.width"),
                  _as_int(_req(region, "height"), "region.height")),
            canvas)
    else:
        rect = canvas

    if node.locked():
        raise OpError("Layer {0!r} is locked; unlock it with set_layer first."
                      .format(node.name()))

    # Two APIs with different trade-offs, verified against 5.3.3:
    #   startFilter() is undoable and returns a real bool, but ignores the
    #                 rectangle and always covers the whole layer.
    #   apply()       honours the rectangle but bypasses undo and returns None.
    # So the region decides which one is correct here.
    whole_layer = rect == canvas
    if whole_layer and hasattr(flt, "startFilter"):
        undoable = True
        ok = flt.startFilter(node, rect.x(), rect.y(),
                             rect.width(), rect.height())
    else:
        undoable = False
        flt.apply(node, rect.x(), rect.y(), rect.width(), rect.height())
        ok = True  # apply() has no usable return value

    doc.waitForDone()
    _refresh(doc)
    if ok is False:
        raise OpError(
            "Krita declined to apply {0!r} to {1!r}. Filters need an unlocked "
            "paint layer with pixel data -- group and vector layers cannot be "
            "filtered directly.".format(name, node.name()))
    result = {"filter": name, "layer": node.name(),
              "settings": applied_settings,
              "undoable": undoable,
              "region": {"x": rect.x(), "y": rect.y(),
                         "width": rect.width(), "height": rect.height()}}
    if not undoable:
        result["note"] = ("Filtering a sub-region writes pixels directly and "
                          "is not recorded in Krita's undo history.")
    return result


@op("set_selection", timeout=30.0, mutates=True)
def op_set_selection(params):
    from krita import Selection

    doc = resolve_document(params.get("document"))
    mode = str(_arg(params, "mode", "rect")).lower()

    if mode in ("none", "deselect", "clear"):
        cleared = False
        action = _krita().action("deselect")
        if action is not None:
            action.trigger()
            cleared = True
        else:
            try:
                doc.setSelection(None)
                cleared = True
            except Exception:
                pass
        if not cleared:
            raise OpError("Could not clear the selection; no Krita window is "
                          "available to run the deselect action.")
        _refresh(doc)
        return {"selection": None}

    selection = doc.selection()
    if mode == "all":
        selection = Selection()
        selection.select(0, 0, doc.width(), doc.height(), 255)
        doc.setSelection(selection)
    elif mode == "rect":
        x = _as_int(_arg(params, "x", 0), "x")
        y = _as_int(_arg(params, "y", 0), "y")
        width = _as_int(_req(params, "width"), "width")
        height = _as_int(_req(params, "height"), "height")
        value = max(0, min(255, _as_int(_arg(params, "value", 255), "value")))
        if not _as_bool(_arg(params, "add", False), "add") or selection is None:
            selection = Selection()
        selection.select(x, y, width, height, value)
        doc.setSelection(selection)
    elif mode in ("invert", "grow", "shrink", "feather"):
        if selection is None:
            raise OpError("There is no selection to {0}.".format(mode))
        if mode == "invert":
            selection.invert()
        elif mode == "grow":
            amount = _as_int(_arg(params, "amount", 1), "amount")
            selection.grow(amount, amount)
        elif mode == "shrink":
            amount = _as_int(_arg(params, "amount", 1), "amount")
            selection.shrink(amount, amount, True)
        else:
            selection.feather(_as_int(_arg(params, "amount", 1), "amount"))
        doc.setSelection(selection)
    else:
        raise OpError("mode must be one of: rect, all, none, invert, grow, "
                      "shrink, feather")

    _refresh(doc)
    current = doc.selection()
    return {"selection": None if current is None else {
        "x": current.x(), "y": current.y(),
        "width": current.width(), "height": current.height()}}


# --------------------------------------------------------------------------
# escape hatches
# --------------------------------------------------------------------------

@op("trigger_action", timeout=60.0, mutates=True)
def op_trigger_action(params):
    name = str(_req(params, "name"))
    action = _krita().action(name)
    if action is None:
        raise OpError(
            "Krita has no action named {0!r}. Action names are the internal "
            "ids from Settings > Configure Krita > Keyboard Shortcuts, for "
            "example edit_undo, edit_redo, deselect, select_all."
            .format(name), kind="not_found")
    # Krita keeps many action enabled-states tied to canvas focus, so they read
    # as disabled whenever its window is in the background. QAction::trigger()
    # emits regardless of that flag, so refusing here would reject calls that
    # actually work -- report the flag instead of gating on it.
    was_enabled = bool(action.isEnabled())
    if not was_enabled:
        action.setEnabled(True)
        try:
            action.trigger()
        finally:
            action.setEnabled(False)
    else:
        action.trigger()
    result = {"triggered": name, "was_enabled": was_enabled}
    if not was_enabled:
        result["note"] = ("Krita had this action marked disabled, usually "
                          "because its window is not focused. It was still "
                          "triggered; check the result to confirm it applied.")
    return result


@op("run_python", timeout=120.0, mutates=True)
def op_run_python(params):
    code = _req(params, "code")
    if not isinstance(code, str) or not code.strip():
        raise OpError("code must be a non-empty Python source string")

    from krita import Krita, InfoObject, Selection

    namespace = {
        "__name__": "krita_mcp_script",
        "Krita": Krita,
        "InfoObject": InfoObject,
        "Selection": Selection,
        "krita": Krita.instance(),
        "app": Krita.instance(),
        "doc": Krita.instance().activeDocument(),
        "QImage": QImage,
        "QColor": QColor,
        "QRect": QRect,
        "result": None,
    }

    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            exec(compile(code, "<krita-mcp>", "exec"), namespace)
    except BaseException as exc:
        return {
            "ok": False,
            "exception": "{0}: {1}".format(type(exc).__name__, exc),
            "traceback": traceback.format_exc(limit=12),
            "stdout": out.getvalue()[-8000:],
            "stderr": err.getvalue()[-4000:],
        }

    value = namespace.get("result")
    return {
        "ok": True,
        "result": _jsonable(value),
        "stdout": out.getvalue()[-8000:],
        "stderr": err.getvalue()[-4000:],
    }


def _jsonable(value, depth=0):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth > 6:
        return repr(value)[:400]
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in list(value)[:500]]
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1)
                for k, v in list(value.items())[:500]}
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)[:65536]).decode("ascii")
    return repr(value)[:400]


@op("self_test", timeout=90.0, mutates=True)
def op_self_test(params):
    """Exercise the bridge end to end and report what worked."""
    krita = _krita()
    checks = []

    def record(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": str(detail)})

    record("krita_instance", krita is not None, krita.version())

    doc = None
    try:
        doc = krita.createDocument(64, 48, "krita-mcp self test", "RGBA", "U8",
                                   "", 120.0)
        doc.setBatchmode(True)
        record("create_document", doc is not None,
               "{0}x{1}".format(doc.width(), doc.height()))

        node = doc.activeNode() or _children(doc.rootNode())[0]
        record("root_layer", node is not None, node.name())

        ok, detail = imaging.verify_channel_order(node, doc)
        record("pixel_channel_order", ok, detail)

        imaging.write_node_image(
            node, QRect(0, 0, 64, 48),
            _solid(64, 48, QColor(0, 128, 255, 255)))
        _refresh(doc)
        back = imaging.read_node_image(node, QRect(0, 0, 1, 1))
        color = QColor(back.pixel(0, 0))
        record("pixel_roundtrip",
               (color.red(), color.green(), color.blue()) == (0, 128, 255),
               "read #{0:02x}{1:02x}{2:02x}".format(
                   color.red(), color.green(), color.blue()))

        # Exercise the real draw path against the throwaway document.
        op_draw({
            "document": "krita-mcp self test",
            "commands": [
                {"type": "fill_rect", "x": 8, "y": 8, "w": 16, "h": 16,
                 "color": "#ff0000"},
                {"type": "text", "x": 4, "y": 30, "text": "ok", "size": 12,
                 "color": "#ffffff"},
            ],
        })
        probe = imaging.read_node_image(node, QRect(10, 10, 1, 1))
        pc = QColor(probe.pixel(0, 0))
        record("draw", (pc.red(), pc.green(), pc.blue()) == (255, 0, 0),
               "pixel at 10,10 is #{0:02x}{1:02x}{2:02x}".format(
                   pc.red(), pc.green(), pc.blue()))

        png = imaging.qimage_to_png_bytes(doc.projection(0, 0, 64, 48))
        record("png_encode", len(png) > 0, "{0} bytes".format(len(png)))

        group = doc.createGroupLayer("self test group")
        doc.rootNode().addChildNode(group, None)
        record("create_group_layer", group is not None, group.type())

        tree = layer_tree(doc)
        record("layer_tree", len(tree) >= 2,
               ", ".join(e["path"] for e in tree))
    except Exception as exc:
        record("exception", False, "{0}: {1}".format(type(exc).__name__, exc))
    finally:
        if doc is not None:
            try:
                doc.setBatchmode(True)
                doc.setModified(False)
                doc.close()
                _settle_after_close()
            except Exception:
                pass

    passed = sum(1 for c in checks if c["ok"])
    return {"passed": passed, "total": len(checks),
            "all_ok": passed == len(checks), "checks": checks}


def _solid(width, height, color):
    image = QImage(width, height, QImage.Format_ARGB32)
    image.fill(color)
    return image


_batch_depth = 0
_batch_saved = False


def dispatch(name, params):
    """Look up and run an operation. Called on the GUI thread."""
    handler = OPS.get(name)
    if handler is None:
        raise OpError(
            "Unknown operation {0!r}. Available: {1}".format(
                name, ", ".join(sorted(OPS))), kind="unknown_op")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise OpError("params must be a JSON object")

    krita = _krita()
    # Batch mode stops scripted actions opening modal dialogs, which would
    # block the UI thread and with it the whole bridge. Nest by depth: a
    # modal dialog can pump the event loop and re-enter dispatch, and a naive
    # save/restore would then latch batch mode on permanently.
    global _batch_depth, _batch_saved
    if _batch_depth == 0:
        _batch_saved = krita.batchmode()
    _batch_depth += 1
    krita.setBatchmode(True)
    started = time.time()
    try:
        result = handler(params)
    finally:
        _batch_depth -= 1
        if _batch_depth <= 0:
            _batch_depth = 0
            krita.setBatchmode(_batch_saved)
    if isinstance(result, dict):
        result.setdefault("elapsed_ms", int((time.time() - started) * 1000))
    return result


def op_timeout(name, default=30.0):
    handler = OPS.get(name)
    return getattr(handler, "op_timeout", default) if handler else default
