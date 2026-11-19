"""Scrollable Qt canvas backed by a bounded PDF tile cache."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from math import ceil

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QAbstractScrollArea

from .selection import nearest, select


TILE = 768
CACHE_BYTES = 64 * 1024 * 1024
MARGIN = 16


def tile_image(result):
    width, height, stride, x, y, pixels = result
    return x, y, QImage(pixels, width, height, stride, QImage.Format.Format_RGB888).copy()


class PaperView(QAbstractScrollArea):
    chosen = Signal(object, object)
    clicked = Signal(int, float, float)
    message = Signal(str)

    def __init__(self, session, parent):
        super().__init__(parent)
        self.session = session
        self.pages = []
        self.offsets = []
        self.zoom = 1.25
        self.auto_fit = True
        self.generation = 0
        self.tile_cache = OrderedDict()
        self.bytes = 0
        self.pending = set()
        self.text_pending = set()
        self.text_cache = OrderedDict()
        self.page_revision = {}
        self.anchor = None
        self.pointer = None
        self.press_point = None
        self.waiting_for_selection = False
        self.selection = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)

    def set_document(self, pages):
        self.generation += 1
        self.pages = pages
        self.auto_fit = True
        self.tile_cache.clear()
        self.bytes = 0
        self.pending.clear()
        self.text_pending.clear()
        self.text_cache.clear()
        self.page_revision.clear()
        self.clear_selection()
        self._layout()
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)
        self.viewport().update()

    def _layout(self):
        width = self.viewport().width()
        height = self.viewport().height()
        widest = max((page_width for page_width, _ in self.pages), default=1)
        if self.auto_fit and self.pages:
            self.zoom = max(0.2, min(4, (width - 2 * MARGIN - 2) / widest))
        self.content_width = ceil(widest * self.zoom) + MARGIN * 2
        top = MARGIN
        self.offsets = []
        for _, page_height in self.pages:
            self.offsets.append(top)
            top += ceil(page_height * self.zoom) + MARGIN
        self.horizontalScrollBar().setPageStep(width)
        self.verticalScrollBar().setPageStep(height)
        self.horizontalScrollBar().setRange(0, max(0, self.content_width - width) if self.pages else 0)
        self.verticalScrollBar().setRange(0, max(0, top - height) if self.pages else 0)

    def _origin_x(self, index):
        width, _ = self.pages[index]
        canvas = max(self.content_width, self.viewport().width())
        return round((canvas - width * self.zoom) / 2) - self.horizontalScrollBar().value()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout()

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def change_zoom(self, factor=None):
        if not self.pages:
            return
        middle = self.verticalScrollBar().value() + self.viewport().height() / 2
        index = max(0, bisect_right(self.offsets, middle) - 1)
        y = (middle - self.offsets[index]) / self.zoom
        self.auto_fit = factor is None
        if factor is not None:
            self.zoom = max(0.2, min(5, self.zoom * factor))
        self._layout()
        self.verticalScrollBar().setValue(round(self.offsets[index] + y * self.zoom - self.viewport().height() / 2))
        self.viewport().update()

    def go_to(self, index, y=0):
        if 0 <= index < len(self.pages):
            self.verticalScrollBar().setValue(round(self.offsets[index] - MARGIN + y * self.zoom))

    def invalidate(self, indices):
        for index in indices:
            self.page_revision[index] = self.page_revision.get(index, 0) + 1
        for key in list(self.tile_cache):
            if key[1] in indices:
                self.bytes -= self.tile_cache.pop(key)[2].sizeInBytes()
        self.viewport().update()

    def _tile(self, index, tx, ty, width, height):
        scale = self.zoom
        generation = self.generation
        revision = self.page_revision.get(index, 0)
        key = generation, index, scale, revision, tx, ty
        if key in self.tile_cache:
            self.tile_cache.move_to_end(key)
            return self.tile_cache[key]
        if key in self.pending:
            return None
        self.pending.add(key)

        def current():
            return (generation == self.generation and scale == self.zoom
                    and revision == self.page_revision.get(index, 0))

        def visible():
            if not current():
                return False
            x = self._origin_x(index) + tx
            y = self.offsets[index] - self.verticalScrollBar().value() + ty
            return x < self.viewport().width() and y < self.viewport().height() and x + TILE > 0 and y + TILE > 0

        def received(result, error):
            self.pending.discard(key)
            if not current() or result is None:
                if error and current():
                    self.message.emit(error)
                return
            value = tile_image(result)
            if value[2].isNull():
                return
            self.tile_cache[key] = value
            self.bytes += value[2].sizeInBytes()
            while self.bytes > CACHE_BYTES:
                self.bytes -= self.tile_cache.popitem(last=False)[1][2].sizeInBytes()
            self.viewport().update()

        self.session.request("render", (index, scale, (tx, ty, min(tx + TILE, width), min(ty + TILE, height))),
                             received, priority=5, needed=visible)
        return None

    def _load_text(self, index, priority=10):
        if index in self.text_cache or index in self.text_pending:
            return
        self.text_pending.add(index)
        document = self.generation

        def received(result, error):
            if document != self.generation:
                return
            self.text_pending.discard(index)
            if error:
                self.message.emit(error)
                self.waiting_for_selection = False
            if result is not None:
                self.text_cache[index] = result
                if len(self.text_cache) > 12:
                    self.text_cache.popitem(last=False)
                if self.anchor and self.pointer:
                    self._update_selection()

        self.session.request("text", (index,), received, priority=priority,
                             needed=lambda: document == self.generation and (
                                 index in self._visible_pages() or self._in_selection(index)
                             ))

    def _in_selection(self, index):
        if self.anchor is None or self.pointer is None:
            return False
        left, right = sorted((self.anchor[0], self.pointer[0]))
        return left <= index <= right

    def _visible_pages(self):
        if not self.pages:
            return range(0)
        first_y = self.verticalScrollBar().value()
        first = max(0, bisect_right(self.offsets, first_y) - 1)
        last = bisect_right(self.offsets, first_y + self.viewport().height())
        return range(first, last)

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(self.viewport().rect(), self.palette().mid())
        offset = self.verticalScrollBar().value()
        for index in self._visible_pages():
            width, height = (ceil(value * self.zoom) for value in self.pages[index])
            left, top = self._origin_x(index), self.offsets[index] - offset
            painter.fillRect(QRectF(left, top, width, height), Qt.GlobalColor.white)
            min_x = max(0, -left) // TILE * TILE
            min_y = max(0, -top) // TILE * TILE
            for ty in range(min_y, min(height, self.viewport().height() - top), TILE):
                for tx in range(min_x, min(width, self.viewport().width() - left), TILE):
                    tile = self._tile(index, tx, ty, width, height)
                    if tile:
                        x, y, image = tile
                        painter.drawImage(QPoint(left + x, top + y), image)
            self._load_text(index)
            if self.selection:
                for x0, y0, x1, y1 in self.selection.boxes.get(index, ()):
                    rectangle = QRectF(left + x0 * self.zoom, top + y0 * self.zoom,
                                       (x1 - x0) * self.zoom, (y1 - y0) * self.zoom)
                    painter.fillRect(rectangle, QColor(55, 133, 238, 85))
        painter.end()

    def _position(self, point, clamp=False):
        if not self.pages:
            return None
        absolute_y = self.verticalScrollBar().value() + point.y()
        index = min(len(self.pages) - 1, max(0, bisect_right(self.offsets, absolute_y) - 1))
        width, height = self.pages[index]
        x = (point.x() - self._origin_x(index)) / self.zoom
        y = (absolute_y - self.offsets[index]) / self.zoom
        if clamp:
            return index, max(0, min(x, width)), max(0, min(y, height))
        if 0 <= x <= width and 0 <= y <= height:
            return index, x, y
        return None

    def clear_selection(self):
        self.anchor = self.pointer = self.selection = None
        self.waiting_for_selection = False
        self.viewport().update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clear_selection()
            self.anchor = self.pointer = self._position(event.position())
            self.press_point = event.position().toPoint()
            if self.anchor:
                self._load_text(self.anchor[0], priority=1)

    def mouseMoveEvent(self, event):
        if self.anchor is None or not event.buttons() & Qt.MouseButton.LeftButton:
            return
        y = event.position().y()
        scrollbar = self.verticalScrollBar()
        if y < 0 or y >= self.viewport().height():
            scrollbar.setValue(scrollbar.value() + (-24 if y < 0 else 24))
        self.pointer = self._position(event.position(), clamp=True)
        self._update_selection()

    def mouseReleaseEvent(self, event):
        if self.anchor is None or event.button() != Qt.MouseButton.LeftButton:
            return
        if (event.position().toPoint() - self.press_point).manhattanLength() < 4:
            page, x, y = self.anchor
            self.clear_selection()
            self.clicked.emit(page, x, y)
            return
        self.popup_point = event.globalPosition().toPoint()
        self.pointer = self._position(event.position(), clamp=True)
        self.waiting_for_selection = True
        self._update_selection()

    def _update_selection(self):
        if not self.anchor or not self.pointer:
            return
        start, end = sorted((self.anchor[0], self.pointer[0]))
        if end - start >= 3:
            self.selection = None
            self.viewport().update()
            if self.waiting_for_selection:
                self.clear_selection()
                self.message.emit("選択範囲は3ページ以内にしてください。")
            return
        pages = {index: self.text_cache[index] for index in range(start, end + 1) if index in self.text_cache}
        if len(pages) != end - start + 1:
            for index in range(start, end + 1):
                self._load_text(index, priority=1)
            if self.waiting_for_selection:
                self.message.emit("選択したページの文字を読み込んでいます。")
            return

        def hit(position):
            index, x, y = position
            glyph = nearest(pages[index], x, y)
            return (index, *glyph) if glyph is not None else None

        first, last = hit(self.anchor), hit(self.pointer)
        if first is None or last is None:
            if self.waiting_for_selection:
                self.clear_selection()
                self.message.emit("選択範囲に文字情報がありません。")
            return
        self.selection = select(pages, first, last)
        self.viewport().update()
        if self.waiting_for_selection:
            self.waiting_for_selection = False
            self.anchor = self.pointer = None
            if self.selection.source:
                self.chosen.emit((self.selection, select(pages, first, last, include_skipped=True)), self.popup_point)
