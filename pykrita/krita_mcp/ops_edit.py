"""Editing operations: per-layer transforms, richer selections, cut/copy,
colour replacement, snapshots, measuring and per-layer export.

Kept apart from ops.py so upstream changes there stay easy to merge. ops.py
calls ``install(ops_module)`` at import time, which registers everything here
in ``ops.OPS``; helpers from ops.py are reached through ``O``.

How edits are reverted
----------------------
Most of these operations write pixels directly, which Krita's undo history
does not record. Instead every mutating operation first stores a compressed
copy of the layer it is about to change (a *snapshot*) and returns its id;
the ``snapshot`` operation restores it. Snapshots live in memory for the
lifetime of the Krita process.

Speed
-----
Krita's Python has no numpy, and a Python loop over a 2048 x 2048 layer is far
too slow. Per-pixel work is therefore done with QPainter composition, with
``bytes.translate`` and with big-integer bit operations over whole buffers,
all of which run in C.
"""

import json
import math
import os
import re
import time
import zlib

from .compat import (
    QByteArray,
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPointF,
    QPolygonF,
    QRect,
    QRectF,
    QTransform,
)

from . import imaging
from .imaging import parse_color

O = None  # the ops module, set by install()

_REGISTRY = []

MAX_SNAPSHOTS = 40

_NONZERO = bytes([0] + [255] * 255)


def edit_op(name, timeout=60.0, mutates=True):
    def register(fn):
        fn.op_name = name
        fn.op_timeout = timeout
        fn.op_mutates = mutates
        _REGISTRY.append(fn)
        return fn
    return register


def install(ops_module):
    global O, _upstream_set_selection
    O = ops_module
    _upstream_set_selection = ops_module.OPS.get("set_selection")
    for fn in _REGISTRY:
        ops_module.OPS[fn.op_name] = fn


_upstream_set_selection = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _rect_dict(rect):
    return {"x": rect.x(), "y": rect.y(),
            "width": rect.width(), "height": rect.height()}


def _canvas(doc):
    return QRect(0, 0, doc.width(), doc.height())


def _opaque_bounds(node):
    """The rectangle holding every non-transparent pixel, in canvas pixels."""
    rect = node.bounds()
    if rect.width() <= 0 or rect.height() <= 0:
        return QRect()
    return QRect(rect.x(), rect.y(), rect.width(), rect.height())


def _require_content(node):
    rect = _opaque_bounds(node)
    if rect.isEmpty():
        raise O.OpError("Layer {0!r} is empty; there is nothing to edit."
                        .format(node.name()))
    return rect


def _paint_node(doc, ref):
    node = O.resolve_node(doc, ref)
    if node.type() != "paintlayer":
        raise O.OpError(
            "Layer {0!r} is a {1}; this operation needs a paint layer."
            .format(node.name(), node.type()))
    imaging.require_pixel_ops(node)
    if node.locked():
        raise O.OpError("Layer {0!r} is locked.".format(node.name()))
    return node


def _raw(node, rect):
    return bytes(node.pixelData(rect.x(), rect.y(), rect.width(), rect.height()))


def _write_raw(node, rect, raw):
    node.setPixelData(QByteArray(raw), rect.x(), rect.y(),
                      rect.width(), rect.height())


def _clear(node, rect):
    if not rect.isEmpty():
        _write_raw(node, rect, bytes(rect.width() * rect.height() * 4))


def _image_bytes(img):
    """Tightly packed bytes of a 32-bit QImage."""
    if img.format() != QImage.Format_ARGB32:
        img = img.convertToFormat(QImage.Format_ARGB32)
    ptr = img.constBits()
    ptr.setsize(img.width() * img.height() * 4)
    return bytes(ptr)


def _premultiplied(node, rect):
    return imaging.read_node_image(node, rect).convertToFormat(
        QImage.Format_ARGB32_Premultiplied)


def _mask_image(mask, width, height):
    """A black ARGB image whose alpha channel is ``mask`` (one byte a pixel)."""
    buf = bytearray(width * height * 4)
    buf[3::4] = mask
    return QImage(bytes(buf), width, height, width * 4,
                  QImage.Format_ARGB32).copy()


def _selection_mask(doc, rect):
    """Selection strength over ``rect`` as bytes, or None with no selection."""
    selection = doc.selection()
    if selection is None:
        return None
    data = bytes(selection.pixelData(rect.x(), rect.y(),
                                     rect.width(), rect.height()))
    if len(data) != rect.width() * rect.height():
        raise O.OpError("Krita returned a selection mask of the wrong size.")
    return data


def _selection_rect(doc):
    selection = doc.selection()
    if selection is None:
        return None
    rect = QRect(selection.x(), selection.y(),
                 selection.width(), selection.height())
    return None if rect.isEmpty() else rect


def _and(a, b):
    n = len(a)
    return (int.from_bytes(a, "little") & int.from_bytes(b, "little")
            ).to_bytes(n, "little")


def _count(mask):
    return len(mask) - mask.count(0)


def _color_mask(raw, color, tolerance):
    """255 where a BGRA pixel is within ``tolerance`` of ``color`` per channel.

    A fully transparent target matches every fully transparent pixel, whatever
    colour is stored underneath; any other target ignores transparent pixels.
    """
    if color.alpha() == 0:
        return raw[3::4].translate(bytes([255] + [0] * 255))
    out = None
    channels = (color.blue(), color.green(), color.red(), color.alpha())
    for offset, want in enumerate(channels):
        table = bytes(255 if abs(v - want) <= tolerance and (offset < 3 or v > 0)
                      else 0 for v in range(256))
        part = raw[offset::4].translate(table)
        out = part if out is None else _and(out, part)
    return out


def _flood(mask, width, height, sx, sy):
    """The connected part of a 0/255 mask that contains (sx, sy)."""
    out = bytearray(width * height)
    if mask[sy * width + sx] == 0:
        return bytes(out)
    stack = [(sx, sy)]
    while stack:
        x, y = stack.pop()
        row = y * width
        if out[row + x] or not mask[row + x]:
            continue
        left = mask.rfind(b"\x00", row, row + x)
        left = row if left < 0 else left + 1
        right = mask.find(b"\x00", row + x, row + width)
        right = row + width if right < 0 else right
        out[left:right] = b"\xff" * (right - left)
        for ny in (y - 1, y + 1):
            if ny < 0 or ny >= height:
                continue
            shift = (ny - y) * width
            i, end = left + shift, right + shift
            while i < end:
                if mask[i] == 0:
                    i = mask.find(b"\xff", i, end)
                    if i < 0:
                        break
                    continue
                if not out[i]:
                    stack.append((i - ny * width, ny))
                nxt = mask.find(b"\x00", i, end)
                if nxt < 0:
                    break
                i = nxt
    return bytes(out)


def _layer_summary(doc, node):
    rect = _opaque_bounds(node)
    return {"name": node.name(), "uuid": O._node_uuid(node),
            "bounds": None if rect.isEmpty() else _rect_dict(rect)}


def _new_layer_above(doc, node, name):
    parent = O._parent_of(doc, node)
    child = doc.createNode(name, "paintlayer")
    siblings = O._children(parent)
    target_uuid = O._node_uuid(node)
    index = 0
    for i, sibling in enumerate(siblings):
        if O._node_uuid(sibling) == target_uuid:
            index = i
            break
    O._insert_node(parent, child, index)
    return child


# --------------------------------------------------------------------------
# snapshots
# --------------------------------------------------------------------------

def _store():
    # Kept on the package module, which dev reloads never replace.
    import krita_mcp
    return krita_mcp.__dict__.setdefault(
        "_snapshot_store", {"next_id": 1, "items": []})


def _doc_key(doc):
    return O._node_uuid(doc.rootNode())


def take_snapshot(doc, node, label):
    store = _store()
    rect = _opaque_bounds(node)
    data = b"" if rect.isEmpty() else zlib.compress(_raw(node, rect), 1)
    entry = {
        "id": store["next_id"],
        "doc": _doc_key(doc),
        "document": doc.name(),
        "uuid": O._node_uuid(node),
        "layer": node.name(),
        "label": label,
        "time": time.time(),
        "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
        "data": data,
    }
    store["next_id"] += 1
    store["items"].append(entry)
    del store["items"][:-MAX_SNAPSHOTS]
    return entry["id"]


def _snapshot_public(entry):
    return {"id": entry["id"], "document": entry["document"],
            "layer": entry["layer"], "label": entry["label"],
            "age_seconds": int(time.time() - entry["time"]),
            "bounds": dict(zip(("x", "y", "width", "height"), entry["rect"])),
            "compressed_bytes": len(entry["data"])}


@edit_op("snapshot", timeout=60.0)
def op_snapshot(params):
    action = str(O._arg(params, "action", "list")).lower()
    store = _store()

    if action == "list":
        return {"snapshots": [_snapshot_public(e) for e in store["items"]],
                "limit": MAX_SNAPSHOTS}

    if action == "take":
        doc = O.resolve_document(params.get("document"))
        node = _paint_node(doc, params.get("layer"))
        label = str(O._arg(params, "label", "manual"))
        return {"snapshot": take_snapshot(doc, node, label),
                "layer": _layer_summary(doc, node)}

    if action in ("restore", "drop"):
        wanted = O._as_int(O._req(params, "id"), "id")
        entry = next((e for e in store["items"] if e["id"] == wanted), None)
        if entry is None:
            raise O.OpError(
                "No snapshot {0}. Call snapshot with action=list.".format(wanted),
                kind="not_found")
        if action == "drop":
            store["items"].remove(entry)
            return {"dropped": wanted}

        doc = None
        for candidate in O._documents():
            if _doc_key(candidate) == entry["doc"]:
                doc = candidate
                break
        if doc is None:
            raise O.OpError("The document snapshot {0} belongs to is no longer "
                            "open.".format(wanted), kind="not_found")
        node = O.resolve_node(doc, "uuid:" + entry["uuid"])
        # so that a restore can itself be reverted
        redo = take_snapshot(doc, node, "before restore of {0}".format(wanted))
        _clear(node, _opaque_bounds(node))
        rect = QRect(*entry["rect"])
        if not rect.isEmpty():
            _write_raw(node, rect, zlib.decompress(entry["data"]))
        O._refresh(doc)
        return {"restored": wanted, "snapshot": redo,
                "layer": _layer_summary(doc, node)}

    raise O.OpError("action must be one of: list, take, restore, drop")


# --------------------------------------------------------------------------
# transform
# --------------------------------------------------------------------------

_ANCHORS = {
    "center": (0.5, 0.5), "centre": (0.5, 0.5),
    "top_left": (0.0, 0.0), "top": (0.5, 0.0), "top_right": (1.0, 0.0),
    "left": (0.0, 0.5), "right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0), "bottom": (0.5, 1.0), "bottom_right": (1.0, 1.0),
}


def _pair(value, key):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise O.OpError("{0} must be [x, y]".format(key))
    return O._as_float(value[0], key), O._as_float(value[1], key)


def _pivot(value, rect):
    if value is None:
        value = "center"
    if isinstance(value, str):
        anchor = _ANCHORS.get(value.strip().lower())
        if anchor is None:
            raise O.OpError("pivot must be [x, y] or one of: " +
                            ", ".join(sorted(set(_ANCHORS) - {"centre"})))
        return (rect.x() + rect.width() * anchor[0],
                rect.y() + rect.height() * anchor[1])
    return _pair(value, "pivot")


@edit_op("transform_layer", timeout=120.0)
def op_transform_layer(params):
    doc = O.resolve_document(params.get("document"))
    node = _paint_node(doc, params.get("layer"))
    before = _require_content(node)

    dx, dy = _pair(params["translate"], "translate") if params.get(
        "translate") is not None else (0.0, 0.0)
    if params.get("move_to") is not None:
        tx, ty = _pair(params["move_to"], "move_to")
        dx, dy = dx + tx - before.x(), dy + ty - before.y()

    degrees = O._as_float(O._arg(params, "rotate", 0.0), "rotate")
    scale = O._arg(params, "scale", 1.0)
    if isinstance(scale, (list, tuple)):
        sx, sy = _pair(scale, "scale")
    else:
        sx = sy = O._as_float(scale, "scale")
    if sx == 0 or sy == 0:
        raise O.OpError("scale must not be zero")
    flip = str(O._arg(params, "flip", "none")).lower()
    if flip == "horizontal":
        sx = -sx
    elif flip == "vertical":
        sy = -sy
    elif flip == "both":
        sx, sy = -sx, -sy
    elif flip != "none":
        raise O.OpError("flip must be one of: none, horizontal, vertical, both")

    px, py = _pivot(params.get("pivot"), before)
    resample = (degrees % 360.0 != 0.0) or abs(sx) != 1.0 or abs(sy) != 1.0
    mirror_only = not resample and (sx < 0 or sy < 0)
    whole_pixels = dx == int(dx) and dy == int(dy)

    if not resample and not mirror_only and dx == 0 and dy == 0:
        return {"changed": False, "layer": _layer_summary(doc, node)}

    snapshot = take_snapshot(doc, node, "transform_layer")

    if not resample and not mirror_only and whole_pixels:
        # A plain move: lossless, and Krita only shifts the layer's offset.
        pos = node.position()
        node.move(pos.x() + int(dx), pos.y() + int(dy))
        method = "move"
    else:
        smooth = O._as_bool(O._arg(params, "smooth", True), "smooth")
        if mirror_only and whole_pixels and (2 * px) == int(2 * px) \
                and (2 * py) == int(2 * py):
            smooth = False  # an exact mirror needs no filtering
        transform = QTransform()
        transform.translate(px, py)
        transform.rotate(degrees)
        transform.scale(sx, sy)
        transform.translate(-px, -py)
        transform = transform * QTransform.fromTranslate(dx, dy)

        target = transform.mapRect(QRectF(before)).toAlignedRect()
        if smooth:
            target = target.adjusted(-1, -1, 1, 1)
        if target.width() > 16384 or target.height() > 16384:
            raise O.OpError("The transformed layer would be {0}x{1} pixels, "
                            "which is too large.".format(target.width(),
                                                         target.height()))
        source = _premultiplied(node, before)
        out = QImage(target.width(), target.height(),
                     QImage.Format_ARGB32_Premultiplied)
        out.fill(0)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
        painter.setRenderHint(QPainter.Antialiasing, smooth)
        painter.setTransform(
            transform * QTransform.fromTranslate(-target.x(), -target.y()))
        painter.drawImage(QPointF(before.x(), before.y()), source)
        painter.end()

        _clear(node, before)
        imaging.write_node_image(node, target, out)
        method = "mirror" if mirror_only else "resample"

    O._refresh(doc)
    after = _opaque_bounds(node)
    return {
        "changed": True,
        "method": method,
        "layer": node.name(),
        "pivot": [px, py],
        "before": _rect_dict(before),
        "after": None if after.isEmpty() else _rect_dict(after),
        "snapshot": snapshot,
        "undoable": False,
        "note": ("Not in Krita's undo history. Revert with snapshot "
                 "action=restore id={0}.".format(snapshot)),
    }


# --------------------------------------------------------------------------
# selections
# --------------------------------------------------------------------------

def _shape_mask(doc, params, mode):
    canvas = _canvas(doc)
    antialias = O._as_bool(O._arg(params, "antialias", True), "antialias")
    path = QPainterPath()
    if mode == "ellipse":
        path.addEllipse(QRectF(
            O._as_float(O._arg(params, "x", 0), "x"),
            O._as_float(O._arg(params, "y", 0), "y"),
            O._as_float(O._req(params, "width"), "width"),
            O._as_float(O._req(params, "height"), "height")))
    else:
        points = O._req(params, "points")
        if not isinstance(points, list) or len(points) < 3:
            raise O.OpError("points must be a list of at least three [x, y]")
        polygon = QPolygonF()
        for point in points:
            x, y = _pair(point, "points")
            polygon.append(QPointF(x, y))
        path.addPolygon(polygon)
        path.closeSubpath()

    rect = path.boundingRect().toAlignedRect().intersected(canvas)
    if rect.isEmpty():
        raise O.OpError("The shape lies outside the canvas.")
    img = QImage(rect.width(), rect.height(), QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing, antialias)
    painter.translate(-rect.x(), -rect.y())
    painter.fillPath(path, QColor(0, 0, 0, 255))
    painter.end()
    return rect, _image_bytes(img)[3::4]


def _pixels_for_selection(doc, params):
    canvas = _canvas(doc)
    ref = params.get("layer")
    if ref == "merged":
        raw = bytes(doc.pixelData(0, 0, canvas.width(), canvas.height()))
        source = "merged"
    else:
        node = O.resolve_node(doc, ref)
        imaging.require_pixel_ops(node)
        raw = _raw(node, canvas)
        source = node.name()
    if len(raw) != canvas.width() * canvas.height() * 4:
        raise O.OpError("Only RGBA/8-bit pixels can be selected by colour.")
    return canvas, raw, source


def _color_selection_mask(doc, params):
    canvas, raw, source = _pixels_for_selection(doc, params)
    width, height = canvas.width(), canvas.height()
    tolerance = max(0, min(255, O._as_int(O._arg(params, "tolerance", 0),
                                          "tolerance")))
    contiguous = O._as_bool(O._arg(params, "contiguous", False), "contiguous")
    seed = None
    if params.get("x") is not None and params.get("y") is not None:
        seed = (O._as_int(params["x"], "x"), O._as_int(params["y"], "y"))
        if not canvas.contains(seed[0], seed[1]):
            raise O.OpError("The sample point lies outside the canvas.")
    if params.get("color") is not None:
        color = parse_color(params["color"])
    elif seed is not None:
        i = (seed[1] * width + seed[0]) * 4
        color = QColor(raw[i + 2], raw[i + 1], raw[i], raw[i + 3])
    else:
        raise O.OpError("mode=color needs `color`, or a sample point x, y.")
    mask = _color_mask(raw, color, tolerance)
    if contiguous:
        if seed is None:
            raise O.OpError("contiguous=true needs a start point x, y.")
        mask = _flood(mask, width, height, seed[0], seed[1])
    info = {"source": source,
            "color": "#{0:02x}{1:02x}{2:02x}{3:02x}".format(
                color.red(), color.green(), color.blue(), color.alpha()),
            "tolerance": tolerance, "contiguous": contiguous}
    return canvas, mask, info


@edit_op("set_selection", timeout=60.0)
def op_set_selection(params):
    from krita import Selection

    mode = str(O._arg(params, "mode", "rect")).lower()
    if mode not in ("ellipse", "polygon", "color", "layer_alpha"):
        return _upstream_set_selection(params)

    doc = O.resolve_document(params.get("document"))
    info = {}
    if mode in ("ellipse", "polygon"):
        rect, mask = _shape_mask(doc, params, mode)
    elif mode == "color":
        rect, mask, info = _color_selection_mask(doc, params)
    else:
        node = O.resolve_node(doc, params.get("layer"))
        imaging.require_pixel_ops(node)
        rect = _opaque_bounds(node).intersected(_canvas(doc))
        if rect.isEmpty():
            raise O.OpError("Layer {0!r} has no pixels on the canvas."
                            .format(node.name()))
        mask = _raw(node, rect)[3::4]
        if params.get("threshold") is not None:
            threshold = max(1, min(255, O._as_int(params["threshold"],
                                                  "threshold")))
            mask = mask.translate(bytes(255 if v >= threshold else 0
                                        for v in range(256)))
        info = {"source": node.name()}

    selected = _count(mask)
    fresh = Selection()
    fresh.setPixelData(QByteArray(mask), rect.x(), rect.y(),
                       rect.width(), rect.height())

    combine = str(O._arg(params, "combine", "replace")).lower()
    if O._as_bool(O._arg(params, "add", False), "add"):
        combine = "add"
    current = doc.selection()
    if combine == "replace" or current is None:
        if current is None and combine in ("subtract", "intersect"):
            raise O.OpError("There is no selection to {0} with.".format(combine))
        doc.setSelection(fresh)
    elif combine == "add":
        current.add(fresh)
        doc.setSelection(current)
    elif combine == "subtract":
        current.subtract(fresh)
        doc.setSelection(current)
    elif combine == "intersect":
        current.intersect(fresh)
        doc.setSelection(current)
    else:
        raise O.OpError("combine must be one of: replace, add, subtract, "
                        "intersect")

    O._refresh(doc)
    result = {"mode": mode, "combine": combine, "pixels_matched": selected,
              "selection": None}
    result.update(info)
    final = _selection_rect(doc)
    if final is not None:
        result["selection"] = _rect_dict(final)
    if selected == 0:
        result["note"] = "Nothing matched, so the new selection is empty."
    return result


# --------------------------------------------------------------------------
# acting on the selection
# --------------------------------------------------------------------------

@edit_op("edit_selection", timeout=120.0)
def op_edit_selection(params):
    doc = O.resolve_document(params.get("document"))
    node = _paint_node(doc, params.get("layer"))
    action = str(O._req(params, "action")).lower()
    if action not in ("clear", "fill", "copy_to_layer", "cut_to_layer"):
        raise O.OpError("action must be one of: clear, fill, copy_to_layer, "
                        "cut_to_layer")

    selected = _selection_rect(doc)
    if selected is None:
        raise O.OpError("There is no selection. Call set_selection first.")
    bounds = _opaque_bounds(node)
    rect = selected if action == "fill" else selected.intersected(bounds)
    if rect.isEmpty():
        raise O.OpError("The selection does not touch any pixel of layer "
                        "{0!r}.".format(node.name()))
    mask = _mask_image(_selection_mask(doc, rect), rect.width(), rect.height())

    result = {"action": action, "layer": node.name(),
              "region": _rect_dict(rect), "undoable": False}
    image = _premultiplied(node, rect)

    if action == "fill":
        color = parse_color(O._req(params, "color"))
        result["snapshot"] = take_snapshot(doc, node, "edit_selection fill")
        paint = QImage(rect.width(), rect.height(),
                       QImage.Format_ARGB32_Premultiplied)
        paint.fill(color)
        painter = QPainter(paint)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, mask)
        painter.end()
        painter = QPainter(image)
        if O._as_bool(O._arg(params, "replace", True), "replace"):
            # put the colour in place of what was there, not on top of it
            painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
            painter.drawImage(0, 0, mask)
            painter.setCompositionMode(QPainter.CompositionMode_Plus)
        painter.drawImage(0, 0, paint)
        painter.end()
        imaging.write_node_image(node, rect, image)
        O._refresh(doc)
        return result

    piece = None
    if action in ("copy_to_layer", "cut_to_layer"):
        piece = image.copy()
        painter = QPainter(piece)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, mask)
        painter.end()

    if action in ("clear", "cut_to_layer"):
        result["snapshot"] = take_snapshot(doc, node, "edit_selection " + action)
        painter = QPainter(image)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        painter.drawImage(0, 0, mask)
        painter.end()
        imaging.write_node_image(node, rect, image)

    if piece is not None:
        target_doc = doc
        if params.get("target_document") is not None:
            target_doc = O.resolve_document(params.get("target_document"))
        name = str(O._arg(params, "name", node.name() + " piece"))
        if target_doc is doc:
            child = _new_layer_above(doc, node, name)
        else:
            child = target_doc.createNode(name, "paintlayer")
            target_doc.rootNode().addChildNode(child, None)
        at = QRect(rect)
        if params.get("x") is not None or params.get("y") is not None:
            at.moveTo(O._as_int(O._arg(params, "x", rect.x()), "x"),
                      O._as_int(O._arg(params, "y", rect.y()), "y"))
        imaging.write_node_image(child, at, piece)
        O._refresh(target_doc)
        result["new_layer"] = _layer_summary(target_doc, child)
        result["note"] = ("Delete the new layer to revert the copy"
                          + ("; restore the snapshot to put the cut pixels "
                             "back." if action == "cut_to_layer" else "."))

    O._refresh(doc)
    after = _opaque_bounds(node)
    result["layer_bounds"] = None if after.isEmpty() else _rect_dict(after)
    return result


@edit_op("replace_color", timeout=120.0)
def op_replace_color(params):
    doc = O.resolve_document(params.get("document"))
    node = _paint_node(doc, params.get("layer"))
    rect = _require_content(node)
    source = parse_color(O._req(params, "from"))
    target = parse_color(O._req(params, "to"))
    tolerance = max(0, min(255, O._as_int(O._arg(params, "tolerance", 0),
                                          "tolerance")))
    keep_alpha = O._as_bool(O._arg(params, "preserve_alpha", True),
                            "preserve_alpha")
    if O._as_bool(O._arg(params, "selection_only", False), "selection_only"):
        selected = _selection_rect(doc)
        if selected is None:
            raise O.OpError("selection_only=true but there is no selection.")
        rect = rect.intersected(selected)
        if rect.isEmpty():
            raise O.OpError("The selection does not touch layer {0!r}."
                            .format(node.name()))
        limit = _selection_mask(doc, rect).translate(
            bytes(255 if v >= 128 else 0 for v in range(256)))
    else:
        limit = None

    raw = _raw(node, rect)
    if source.alpha() == 255 and keep_alpha:
        # Edge pixels of a flat shape carry its colour at partial opacity;
        # match on colour alone so the anti-aliased rim is recoloured too.
        wanted = (source.blue(), source.green(), source.red())
        mask = None
        for offset, want in enumerate(wanted):
            table = bytes(255 if abs(v - want) <= tolerance else 0
                          for v in range(256))
            part = raw[offset::4].translate(table)
            mask = part if mask is None else _and(mask, part)
        mask = _and(mask, raw[3::4].translate(_NONZERO))
    else:
        mask = _color_mask(raw, source, tolerance)
    if limit is not None:
        mask = _and(mask, limit)

    changed = _count(mask)
    result = {"layer": node.name(), "pixels_changed": changed,
              "region": _rect_dict(rect), "undoable": False}
    if changed == 0:
        result["note"] = ("No pixel matched. Read the exact colour with "
                          "get_pixel, or raise tolerance.")
        return result

    result["snapshot"] = take_snapshot(doc, node, "replace_color")
    count = rect.width() * rect.height()
    wide = bytearray(count * 4)
    wide[0::4] = mask
    wide[1::4] = mask
    wide[2::4] = mask
    if not keep_alpha:
        wide[3::4] = mask
    paint = bytes((target.blue(), target.green(), target.red(),
                   target.alpha())) * count
    r = int.from_bytes(raw, "little")
    c = int.from_bytes(paint, "little")
    m = int.from_bytes(bytes(wide), "little")
    _write_raw(node, rect, (((r ^ c) & m) ^ r).to_bytes(count * 4, "little"))
    O._refresh(doc)
    return result


# --------------------------------------------------------------------------
# bringing images in, measuring, exporting
# --------------------------------------------------------------------------

@edit_op("import_layer", timeout=120.0)
def op_import_layer(params):
    doc = O.resolve_document(params.get("document"))
    path = O._abspath(O._req(params, "path"))
    if not os.path.isfile(path):
        raise O.OpError("No such file: {0}".format(path), kind="not_found")
    image = QImage(path)
    if image.isNull():
        raise O.OpError("Could not read {0} as an image.".format(path))
    image = image.convertToFormat(QImage.Format_ARGB32)
    name = str(O._arg(params, "name",
                      os.path.splitext(os.path.basename(path))[0]))
    if params.get("above") is not None:
        child = _new_layer_above(doc, O.resolve_node(doc, params["above"]), name)
    else:
        child = doc.createNode(name, "paintlayer")
        doc.rootNode().addChildNode(child, None)
    imaging.require_pixel_ops(child)
    rect = QRect(O._as_int(O._arg(params, "x", 0), "x"),
                 O._as_int(O._arg(params, "y", 0), "y"),
                 image.width(), image.height())
    imaging.write_node_image(child, rect, image)
    O._refresh(doc)
    return {"imported": path,
            "image_size": {"width": image.width(), "height": image.height()},
            "layer": _layer_summary(doc, child)}


def _target_nodes(doc, params):
    refs = params.get("layers")
    if refs:
        if not isinstance(refs, list):
            raise O.OpError("layers must be a list of layer references")
        return [O.resolve_node(doc, ref) for ref in refs]
    parent = doc.rootNode()
    if params.get("group") is not None:
        parent = O.resolve_node(doc, params["group"], allow_root=True)
    return O._children(parent)  # top first


def _alpha_mask(node, rect, threshold):
    data = bytes(node.projectionPixelData(rect.x(), rect.y(),
                                          rect.width(), rect.height()))
    if len(data) != rect.width() * rect.height() * 4:
        raise O.OpError("Layer {0!r} is not RGBA/8-bit.".format(node.name()))
    return data[3::4].translate(bytes(255 if v >= threshold else 0
                                      for v in range(256)))


@edit_op("measure", timeout=120.0, mutates=False)
def op_measure(params):
    doc = O.resolve_document(params.get("document"))
    O._refresh(doc)
    threshold = max(1, min(255, O._as_int(O._arg(params, "threshold", 128),
                                          "threshold")))
    reference = None
    if params.get("reference") is not None:
        reference = O.resolve_node(doc, params["reference"])
        ref_rect = _opaque_bounds(reference)
        if ref_rect.isEmpty():
            raise O.OpError("The reference layer is empty.")

    rows = []
    for node in _target_nodes(doc, params):
        rect = _opaque_bounds(node)
        row = {"name": node.name(), "type": node.type(),
               "visible": bool(node.visible())}
        if rect.isEmpty():
            row["bounds"] = None
            rows.append(row)
            continue
        row["bounds"] = _rect_dict(rect)
        row["center"] = [rect.x() + rect.width() / 2.0,
                         rect.y() + rect.height() / 2.0]
        if reference is not None or O._as_bool(
                O._arg(params, "count_pixels", False), "count_pixels"):
            mine = _alpha_mask(node, rect, threshold)
            row["opaque_pixels"] = _count(mine)
        if reference is not None:
            union = rect.united(ref_rect)
            a = int.from_bytes(_alpha_mask(node, union, threshold), "little")
            b = int.from_bytes(_alpha_mask(reference, union, threshold),
                               "little")
            both = (a & b).bit_count() // 8
            either = (a | b).bit_count() // 8
            inside = a.bit_count() // 8
            row["vs_reference"] = {
                "overlap_pixels": both,
                "iou": round(both / either, 4) if either else 0.0,
                "inside_reference": round(both / inside, 4) if inside else 0.0,
                "center_offset": [
                    row["center"][0] - (ref_rect.x() + ref_rect.width() / 2.0),
                    row["center"][1] - (ref_rect.y() + ref_rect.height() / 2.0)],
            }
        rows.append(row)

    result = {"document": {"name": doc.name(), "width": doc.width(),
                           "height": doc.height()},
              "layers": rows}
    if reference is not None:
        result["reference"] = {"name": reference.name(),
                               "bounds": _rect_dict(ref_rect)}
    return result


def _file_stem(name, used):
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "layer"
    candidate, n = stem, 2
    while candidate.lower() in used:
        candidate = "{0}_{1}".format(stem, n)
        n += 1
    used.add(candidate.lower())
    return candidate


@edit_op("export_layers", timeout=300.0, mutates=False)
def op_export_layers(params):
    doc = O.resolve_document(params.get("document"))
    directory = O._abspath(O._req(params, "directory"), "directory")
    os.makedirs(directory, exist_ok=True)
    trim = O._as_bool(O._arg(params, "trim", True), "trim")
    include_hidden = O._as_bool(O._arg(params, "include_hidden", False),
                                "include_hidden")
    O._refresh(doc)
    canvas = _canvas(doc)

    nodes = _target_nodes(doc, params)
    used, entries, skipped = set(), [], []
    # bottom first, which is draw order
    for order, node in enumerate(reversed(nodes)):
        if not include_hidden and not node.visible():
            skipped.append({"name": node.name(), "reason": "hidden"})
            continue
        rect = _opaque_bounds(node) if trim else canvas
        if rect.isEmpty():
            skipped.append({"name": node.name(), "reason": "empty"})
            continue
        data = bytes(node.projectionPixelData(rect.x(), rect.y(),
                                              rect.width(), rect.height()))
        if len(data) != rect.width() * rect.height() * 4:
            skipped.append({"name": node.name(), "reason": "not RGBA/8-bit"})
            continue
        image = QImage(data, rect.width(), rect.height(), rect.width() * 4,
                       QImage.Format_ARGB32).copy()
        file_name = _file_stem(node.name(), used) + ".png"
        if not image.save(os.path.join(directory, file_name), "PNG"):
            raise O.OpError("Could not write {0}".format(
                os.path.join(directory, file_name)))
        entries.append({
            "name": node.name(), "file": file_name, "order": order,
            "x": rect.x(), "y": rect.y(),
            "width": rect.width(), "height": rect.height(),
            "center": [rect.x() + rect.width() / 2.0,
                       rect.y() + rect.height() / 2.0],
            "opacity": int(node.opacity()),
            "visible": bool(node.visible()),
            "blending_mode": node.blendingMode(),
        })

    manifest = {
        "document": doc.name(),
        "source_file": doc.fileName() or None,
        "canvas": {"width": doc.width(), "height": doc.height()},
        "trimmed": trim,
        "layers": entries,
    }
    manifest_name = str(O._arg(params, "manifest", "layers.json"))
    manifest_path = os.path.join(directory, os.path.basename(manifest_name))
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
    return {"directory": directory, "manifest": manifest_path,
            "exported": len(entries), "skipped": skipped,
            "files": [e["file"] for e in entries]}
