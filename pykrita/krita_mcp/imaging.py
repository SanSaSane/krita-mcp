"""QImage <-> Krita pixel plumbing, plus colour parsing.

Krita stores RGBA8 as BGRA bytes, which on a little-endian machine is exactly
QImage.Format_ARGB32 (unpremultiplied) -- so the conversion is a reinterpret,
not a shuffle.  ``verify_channel_order`` checks that assumption at runtime
instead of trusting it.
"""

import base64
import sys

from .compat import QBuffer, QByteArray, QRect, QtCompat as Qt, QColor, QImage

RGBA8 = ("RGBA", "U8")


class ImagingError(Exception):
    pass


def _image_byte_count(img):
    try:
        return img.sizeInBytes()
    except AttributeError:  # Qt < 5.10
        return img.byteCount()


def supports_pixel_ops(node):
    return node.colorModel() == "RGBA" and node.colorDepth() == "U8"


def require_pixel_ops(node):
    if not supports_pixel_ops(node):
        raise ImagingError(
            "Layer {0!r} is {1}/{2}; direct pixel access needs RGBA/U8. "
            "Convert the document with Image > Convert Image Color Space "
            "(8-bit RGBA) first.".format(
                node.name(), node.colorModel(), node.colorDepth()
            )
        )


def clamp_rect(rect, bounds):
    """Intersect ``rect`` with ``bounds``; raise if nothing is left."""
    out = rect.intersected(bounds)
    if out.width() <= 0 or out.height() <= 0:
        raise ImagingError(
            "Region {0},{1} {2}x{3} lies outside the canvas ({4}x{5}).".format(
                rect.x(), rect.y(), rect.width(), rect.height(),
                bounds.width(), bounds.height(),
            )
        )
    return out


def read_node_image(node, rect):
    """Read a rect of a node's own pixels into a detached ARGB32 QImage."""
    require_pixel_ops(node)
    w, h = rect.width(), rect.height()
    raw = bytes(node.pixelData(rect.x(), rect.y(), w, h))
    expected = w * h * 4
    if len(raw) != expected:
        raise ImagingError(
            "Krita returned {0} bytes for a {1}x{2} RGBA region, expected "
            "{3}.".format(len(raw), w, h, expected)
        )
    # QImage does not take ownership of `raw`; copy() detaches before it dies.
    return QImage(raw, w, h, w * 4, QImage.Format_ARGB32).copy()


def write_node_image(node, rect, img):
    """Write an ARGB32-compatible QImage back into a node."""
    require_pixel_ops(node)
    w, h = rect.width(), rect.height()
    if img.width() != w or img.height() != h:
        raise ImagingError(
            "Image is {0}x{1} but the target region is {2}x{3}.".format(
                img.width(), img.height(), w, h
            )
        )
    if img.format() != QImage.Format_ARGB32:
        img = img.convertToFormat(QImage.Format_ARGB32)

    stride = img.bytesPerLine()
    ptr = img.constBits()
    ptr.setsize(_image_byte_count(img))
    raw = bytes(ptr)
    if stride != w * 4:  # repack to a tight buffer
        rows = [raw[y * stride:y * stride + w * 4] for y in range(h)]
        raw = b"".join(rows)

    node.setPixelData(QByteArray(raw), rect.x(), rect.y(), w, h)


def qimage_to_png_bytes(img):
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    ok = img.save(buf, "PNG")
    buf.close()
    if not ok:
        raise ImagingError("Qt failed to encode the image as PNG.")
    return bytes(ba)


def qimage_to_png_b64(img):
    return base64.b64encode(qimage_to_png_bytes(img)).decode("ascii")


def png_b64_to_qimage(data):
    try:
        raw = base64.b64decode(data, validate=True)
    except Exception as exc:
        raise ImagingError("image data is not valid base64: {0}".format(exc))
    img = QImage()
    if not img.loadFromData(QByteArray(raw)):
        raise ImagingError(
            "Could not decode the supplied image data (PNG/JPEG expected)."
        )
    return img.convertToFormat(QImage.Format_ARGB32)


def scale_to_fit(img, max_size):
    """Downscale so the longest edge is at most ``max_size``. Never upscales."""
    if max_size <= 0:
        return img, 1.0
    longest = max(img.width(), img.height())
    if longest <= max_size:
        return img, 1.0
    factor = float(max_size) / float(longest)
    scaled = img.scaled(
        max(1, int(round(img.width() * factor))),
        max(1, int(round(img.height() * factor))),
        Qt.KeepAspectRatio,
        Qt.SmoothTransformation,
    )
    return scaled, factor


def parse_color(value, default=None):
    """Accept #rgb, #rrggbb, #rrggbbaa, SVG names, or [r,g,b(,a)]."""
    if value is None:
        if default is None:
            raise ImagingError("a colour is required here")
        value = default

    if isinstance(value, (list, tuple)):
        parts = list(value)
        if len(parts) not in (3, 4):
            raise ImagingError(
                "colour arrays need 3 or 4 numbers, got {0}".format(len(parts))
            )
        vals = []
        for p in parts:
            try:
                iv = int(round(float(p)))
            except (TypeError, ValueError):
                raise ImagingError("colour component {0!r} is not a number".format(p))
            vals.append(max(0, min(255, iv)))
        if len(vals) == 3:
            vals.append(255)
        return QColor(vals[0], vals[1], vals[2], vals[3])

    if not isinstance(value, str):
        raise ImagingError("colour must be a string or array, got {0}".format(
            type(value).__name__))

    text = value.strip()
    if text.startswith("#") and len(text) == 9:  # #RRGGBBAA - Qt cannot parse it
        try:
            r = int(text[1:3], 16)
            g = int(text[3:5], 16)
            b = int(text[5:7], 16)
            a = int(text[7:9], 16)
        except ValueError:
            raise ImagingError("{0!r} is not a valid #RRGGBBAA colour".format(value))
        return QColor(r, g, b, a)

    color = QColor(text)
    if not color.isValid():
        raise ImagingError(
            "{0!r} is not a recognised colour. Use #rrggbb, #rrggbbaa, an SVG "
            "colour name, or [r,g,b,a].".format(value)
        )
    return color


def verify_channel_order(node, doc):
    """Round-trip one opaque red pixel to confirm the BGRA/ARGB32 assumption.

    Returns (ok, detail). Restores whatever was there before.
    """
    if not supports_pixel_ops(node):
        return False, "node is not RGBA/U8"
    rect = QRect(0, 0, 1, 1)
    try:
        before = bytes(node.pixelData(0, 0, 1, 1))
        node.setPixelData(QByteArray(bytes((0, 0, 255, 255))), 0, 0, 1, 1)
        img = read_node_image(node, rect)
        color = QColor(img.pixel(0, 0))
        node.setPixelData(QByteArray(before), 0, 0, 1, 1)
        ok = (color.red(), color.green(), color.blue()) == (255, 0, 0)
        return ok, "read back r={0} g={1} b={2} on a {3}-endian host".format(
            color.red(), color.green(), color.blue(), sys.byteorder
        )
    except Exception as exc:
        return False, "probe failed: {0}".format(exc)
