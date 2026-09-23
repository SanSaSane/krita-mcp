"""Compatibility layer for PyQt5 and PyQt6 (e.g. Krita 5 vs Krita 6)."""

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
    from PyQt6.QtCore import (
        QBuffer,
        QByteArray,
        QCoreApplication,
        QEvent,
        QEventLoop,
        QIODevice,
        QIODeviceBase,
        QLineF,
        QObject,
        QPointF,
        QRect,
        QRectF,
        Qt,
        QTimer,
        pyqtSignal,
    )
    from PyQt6.QtGui import (
        QBrush,
        QColor,
        QFont,
        QFontMetrics,
        QFontMetricsF,
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
        QTransform,
    )
    from PyQt6.QtWidgets import QMessageBox

    if hasattr(QEventLoop, "ProcessEventsFlag"):
        for attr in (
            "AllEvents",
            "ExcludeUserInputEvents",
            "ExcludeSocketNotifiers",
            "WaitForMoreEvents",
        ):
            if hasattr(QEventLoop.ProcessEventsFlag, attr):
                setattr(QEventLoop, attr, getattr(QEventLoop.ProcessEventsFlag, attr))

    if hasattr(QEvent, "Type"):
        for attr in ("DeferredDelete",):
            if hasattr(QEvent.Type, attr):
                setattr(QEvent, attr, getattr(QEvent.Type, attr))

    if hasattr(QIODeviceBase, "OpenModeFlag"):
        for attr in dir(QIODeviceBase.OpenModeFlag):
            if not attr.startswith("_"):
                val = getattr(QIODeviceBase.OpenModeFlag, attr)
                setattr(QBuffer, attr, val)
                setattr(QIODevice, attr, val)

    if hasattr(QImage, "Format"):
        for attr in (
            "Format_ARGB32",
            "Format_ARGB32_Premultiplied",
            "Format_RGB32",
            "Format_RGBA8888",
            "Format_Grayscale8",
        ):
            if hasattr(QImage.Format, attr):
                setattr(QImage, attr, getattr(QImage.Format, attr))

    if hasattr(QPainter, "RenderHint"):
        for attr in (
            "Antialiasing",
            "TextAntialiasing",
            "SmoothPixmapTransform",
            "LosslessImageRendering",
        ):
            if hasattr(QPainter.RenderHint, attr):
                setattr(QPainter, attr, getattr(QPainter.RenderHint, attr))

    if hasattr(QPainter, "CompositionMode"):
        for attr in dir(QPainter.CompositionMode):
            if not attr.startswith("_"):
                setattr(QPainter, attr, getattr(QPainter.CompositionMode, attr))

    class _QtCompat:
        def __getattr__(self, name):
            if hasattr(Qt, name):
                return getattr(Qt, name)
            for enum_cls in (
                getattr(Qt, "AspectRatioMode", None),
                getattr(Qt, "TransformationMode", None),
                getattr(Qt, "ConnectionType", None),
                getattr(Qt, "PenStyle", None),
                getattr(Qt, "BrushStyle", None),
                getattr(Qt, "GlobalColor", None),
                getattr(Qt, "PenCapStyle", None),
                getattr(Qt, "PenJoinStyle", None),
            ):
                if enum_cls and hasattr(enum_cls, name):
                    return getattr(enum_cls, name)
            raise AttributeError(f"Qt has no attribute {name}")

    QtCompat = _QtCompat()

except ImportError:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import (
        QBuffer,
        QByteArray,
        QCoreApplication,
        QEvent,
        QEventLoop,
        QLineF,
        QObject,
        QPointF,
        QRect,
        QRectF,
        Qt,
        QTimer,
        pyqtSignal,
    )
    from PyQt5.QtGui import (
        QBrush,
        QColor,
        QFont,
        QFontMetrics,
        QFontMetricsF,
        QImage,
        QLinearGradient,
        QPainter,
        QPainterPath,
        QPen,
        QPolygonF,
        QRadialGradient,
        QTransform,
    )
    from PyQt5.QtWidgets import QMessageBox

    QtCompat = Qt
