"""Shared, palette-coloured PDF.js icons for Qt Widgets."""

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


ICON_DIR = Path(__file__).with_name("icons")


@lru_cache(maxsize=240)
def _render(name, color, edge=16):
    """An icon with the 16 px picture the menus use and, when `edge` is larger, a bigger picture for buttons.

    Both go into one QIcon: a menu asks for 16 px and gets that picture, a button asks for its icon size
    and gets the bigger one instead of an enlarged (blurred) 16 px picture.
    """
    renderer = QSvgRenderer(str(ICON_DIR / (name + ".svg")))
    icon = QIcon()
    if not renderer.isValid():
        return icon
    for logical in (16, edge) if edge > 16 else (16,):
        for ratio in (1, 1.25, 1.5, 2, 3):
            size = round(logical * ratio)
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


def icon(widget, name, edge=16):
    return _render(name, widget.palette().buttonText().color().rgba(), edge)


def swatch(color):
    pixmap = QPixmap(16, 16)
    pixmap.fill(color)
    return QIcon(pixmap)
