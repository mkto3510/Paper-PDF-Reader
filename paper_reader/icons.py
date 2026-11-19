"""Shared, palette-coloured PDF.js icons for Qt Widgets."""

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


ICON_DIR = Path(__file__).with_name("icons")


@lru_cache(maxsize=240)
def _render(name, color):
    renderer = QSvgRenderer(str(ICON_DIR / (name + ".svg")))
    icon = QIcon()
    if not renderer.isValid():
        return icon
    for ratio in (1, 1.25, 1.5, 2, 3):
        size = round(16 * ratio)
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor.fromRgba(color))
        painter.end()
        pixmap.setDevicePixelRatio(ratio)
        icon.addPixmap(pixmap)
    return icon


def icon(widget, name):
    return _render(name, widget.palette().buttonText().color().rgba())


def swatch(color):
    pixmap = QPixmap(16, 16)
    pixmap.fill(color)
    return QIcon(pixmap)
