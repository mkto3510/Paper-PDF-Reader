"""A tiled PDF canvas with document-space navigation and selection."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from math import ceil

from PySide6.QtCore import QEvent, QPointF, QRectF, QTextBoundaryFinder, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QAbstractScrollArea, QApplication
from shiboken6 import isValid

from .selection import join_selections, nearest, select

TILE = 768
CACHE_BYTES = 64 * 1024 * 1024
MARGIN = 16
ACTUAL_SIZE = 96 / 72
MIN_SCALE = ACTUAL_SIZE * 0.0833
MAX_SCALE = ACTUAL_SIZE * 64
DRAG_DISTANCE = 4


def tile_image(result):
    width, height, stride, x, y, pixels = result
    return x, y, QImage(pixels, width, height, stride, QImage.Format.Format_RGB888).copy()


def word_range(line, index):
    # Qt's offsets count UTF-16 units, while each PDF glyph can carry several characters.
    offsets = [0]
    for glyph in line.glyphs:
        offsets.append(offsets[-1] + sum(2 if ord(char) > 0xFFFF else 1 for char in glyph.text))
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Word, line.text)
    finder.setPosition(offsets[index])
    first = offsets[index] if finder.isAtBoundary() else finder.toPreviousBoundary()
    finder.setPosition(offsets[index])
    last = finder.toNextBoundary()
    return bisect_right(offsets, first) - 1, bisect_left(offsets, last) - 1


@dataclass
class HighlightDrag:
    key: tuple
    moving: int
    press: QPointF
    point: QPointF
    original: tuple | None = None
    ends: tuple | None = None
    preview: tuple = ()
    handle: tuple | None = None
    released: bool = False


class TileCache:
    def __init__(self):
        self.tiles = OrderedDict()
        self.bytes = 0

    def get(self, key):
        tile = self.tiles.get(key)
        if tile is not None:
            self.tiles.move_to_end(key)
        return tile

    def put(self, key, tile):
        previous = self.tiles.pop(key, None)
        if previous:
            self.bytes -= previous[2].sizeInBytes()
        self.tiles[key] = tile
        self.bytes += tile[2].sizeInBytes()
        while self.bytes > CACHE_BYTES:
            self.bytes -= self.tiles.popitem(last=False)[1][2].sizeInBytes()

    def discard(self, document):
        for key in [key for key in self.tiles if key[0] == document]:
            self.bytes -= self.tiles.pop(key)[2].sizeInBytes()


class PaperView(QAbstractScrollArea):
    clicked = Signal(int, float, float)
    hover_position = Signal(object)
    menu_requested = Signal(object, object)
    location_changed = Signal()
    message = Signal(str)
    note_delete_requested = Signal(object)
    note_resize_requested = Signal(object, object, object)

    def __init__(self, session, parent, cache=None, document=None):
        super().__init__(parent)
        self.session = session
        self.cache = cache if cache is not None else TileCache()
        self.document = document if document is not None else id(self)
        self.pages = []
        self.positions = {}
        self.row_tops = []
        self.rows = []
        self.row_for = {}
        self.content_width = 0
        self.zoom = ACTUAL_SIZE
        self.zoom_mode = "width"
        self.rotation = 0
        self.layout_mode = "single"
        self.continuous = True
        self.rtl = False
        self.page = 0
        self._arrow_page = None
        self.direction = (0, 1)
        self.generation = 0
        self.pending = set()
        self.text_pending = {}
        self.text_cache = OrderedDict()
        self.notes = {}
        self.selected_note = None
        self.highlight_drag = None
        self.resizing_notes = set()
        self.running_notes = set()
        self.opacity = 0.4
        self.search_results = {}
        self.current_match = None
        self.show_all_matches = False
        self.navigation_target = None
        self.link_hover = None
        self.navigation_timer = QTimer(self, singleShot=True, interval=3000)
        self.navigation_timer.timeout.connect(lambda: self.show_navigation_target(None))
        self.content_box = None
        self.content_anchor = None
        self.anchor = self.pointer = None
        self.selection_range = None
        self.selection_origin = ""
        self.waiting = False
        self.press = self.pan_start = None
        self.dragging_selection = False
        self.press_clicks = 1
        self.click_series = None
        self.input_revision = 0
        self.viewport_revision = 0
        self.unit_anchor = None
        self.drag_point = None
        self.drag_scroll = QTimer(self, interval=35)
        self.drag_scroll.timeout.connect(self.scroll_selection)
        self.click_timer = QTimer(self, singleShot=True, timerType=Qt.TimerType.PreciseTimer)
        self.click_timer.timeout.connect(self.finish_click_series)
        self.panning = False
        self.wheel_remainder = QPointF()
        self.selection_revision = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.verticalScrollBar().setSingleStep(32)
        self.horizontalScrollBar().setSingleStep(32)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.viewport().setMouseTracking(True)
        self.viewport().grabGesture(Qt.GestureType.PinchGesture)

    def set_document(self, pages, generation=0, preserve=False):
        location = self.location() if preserve and self.pages else None
        self.show_navigation_target(None)
        self.pages, self.generation = pages, generation
        self.page = min(self.page, len(pages) - 1) if pages and preserve else 0
        self.text_pending.clear()
        self.text_cache.clear()
        self.search_results = {}
        self.current_match = None
        self.content_box = None
        self.clear_selection()
        self._layout()
        if location:
            self.restore(location)
        else:
            self.verticalScrollBar().setValue(0)
            self.horizontalScrollBar().setValue(0)
        self.viewport().update()

    def _transform(self, index):
        width, height = self.pages[index]
        rotation = QTransform().rotate(self.rotation)
        box = rotation.mapRect(QRectF(0, 0, width, height))
        rotation *= QTransform.fromTranslate(-box.x(), -box.y())
        return rotation, box.width(), box.height()

    def _layout(self):
        self._invalidate_pointer_position()
        grouped = []
        index = 0
        while index < len(self.pages):
            count = 1 if self.layout_mode == "single" or (self.layout_mode == "book" and index == 0) else 2
            row = list(range(index, min(index + count, len(self.pages))))
            grouped.append(row[::-1] if self.rtl else row)
            index += count
        self.row_for = {page: row for row, pages in enumerate(grouped) for page in pages}
        shown = grouped if self.continuous or not grouped else [grouped[self.row_for[self.page]]]
        sizes = {p: self._transform(p)[1:] for p in range(len(self.pages))}
        width, height = self.viewport().width(), self.viewport().height()
        if shown and self.zoom_mode != "custom":
            active = grouped[self.row_for[self.page]]
            room_width = max(1, width - (len(active) + 1) * MARGIN - 2)
            room_height = max(1, height - 2 * MARGIN - 2)
            width_fit = room_width / sum(sizes[p][0] for p in active)
            height_fit = room_height / max(sizes[p][1] for p in active)
            if self.zoom_mode == "content" and self.content_box:
                box = self._transform(self.page)[0].mapRect(QRectF(*self._rect_args(self.content_box)))
                fit = min(room_width / max(box.width(), 1), room_height / max(box.height(), 1))
            else:
                fit = width_fit if self.zoom_mode == "width" else min(width_fit, height_fit)
            self.zoom = max(MIN_SCALE, min(MAX_SCALE, fit))
        widths = [sum(ceil(sizes[p][0] * self.zoom) for p in row) + (len(row) + 1) * MARGIN for row in shown]
        self.content_width = max([width, *widths])
        self.positions.clear()
        self.rows, self.row_tops = shown, []
        top = MARGIN
        for row, row_width in zip(shown, widths):
            self.row_tops.append(top)
            x = (self.content_width - row_width) // 2 + MARGIN
            for page in row:
                pw, ph = (ceil(value * self.zoom) for value in sizes[page])
                self.positions[page] = x, top, pw, ph
                x += pw + MARGIN
            top += max(self.positions[p][3] for p in row) + MARGIN
        for bar, extent, viewport in ((self.verticalScrollBar(), top, height),
                                      (self.horizontalScrollBar(), self.content_width, width)):
            bar.setPageStep(viewport)
            bar.setRange(0, max(0, extent - viewport) if shown else 0)

    def resizeEvent(self, event):
        old = event.oldSize()
        # The viewport already reports its new size during this event. Use
        # the previous center to anchor each resize, including scrollbar changes.
        center = QPointF((old.width() - 1) // 2, (old.height() - 1) // 2)
        anchor = self.location(center) if old.isValid() and self.pages else None
        super().resizeEvent(event)
        self._layout()
        self.restore(anchor)

    def _invalidate_pointer_position(self):
        self._arrow_page = None
        self.viewport_revision += 1
        self.cancel_click_series()
        self.hover_position.emit(None)

    def scrollContentsBy(self, dx, dy):
        if dx or dy:
            self._invalidate_pointer_position()
        if dy:
            self.direction = (0, 1 if dy < 0 else -1)
        elif dx:
            self.direction = (1 if dx < 0 else -1, 0)
        self.viewport().update()
        self.location_changed.emit()

    def wheelEvent(self, event):
        pixels, angle = QPointF(event.pixelDelta()), event.angleDelta()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            amount = angle.y() / 120 if angle.y() else pixels.y() / 96
            if amount:
                self.change_zoom(1.2 ** amount, event.position())
            event.accept()
            return
        if pixels.isNull() and not angle.isNull():
            step = 32 * QApplication.wheelScrollLines() / 120
            pixels = QPointF(angle.x() * step, angle.y() * step)
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and not pixels.x():
            pixels = QPointF(pixels.y(), 0)
        self.wheel_remainder += pixels
        x, y = int(self.wheel_remainder.x()), int(self.wheel_remainder.y())
        self.wheel_remainder -= QPointF(x, y)
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - x)
        if y:
            self.scroll_by(-y)
        event.accept()

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self.hover_position.emit(None)
        if event.type() == QEvent.Type.Gesture:
            pinch = event.gesture(Qt.GestureType.PinchGesture)
            if pinch and pinch.lastScaleFactor():
                self.change_zoom(pinch.scaleFactor() / pinch.lastScaleFactor(), pinch.centerPoint())
                event.accept(pinch)
                return True
        if event.type() == QEvent.Type.NativeGesture and event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            self.change_zoom(1 + event.value(), event.position())
            event.accept()
            return True
        return super().viewportEvent(event)

    def scroll_by(self, amount):
        bar = self.verticalScrollBar()
        at_edge = bar.value() == (bar.maximum() if amount > 0 else bar.minimum())
        if self.pages and not self.continuous and at_edge:
            row = self.row_for[self.page] + (1 if amount > 0 else -1)
            members = sorted(page for page, index in self.row_for.items() if index == row)
            if members:
                self.go_to(members[0])
                if amount < 0:
                    bar.setValue(bar.maximum())
            return
        bar.setValue(bar.value() + round(amount))

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        command_modifiers = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
                             | Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.MetaModifier)
        if key == Qt.Key.Key_Delete and self.selected_note and not self.selection_range and not self.highlight_drag:
            if not event.isAutoRepeat():
                self.note_delete_requested.emit(self.selected_note)
            event.accept()
        elif key == Qt.Key.Key_Escape:
            self.clear_selection()
        elif key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp, Qt.Key.Key_Space):
            back = key == Qt.Key.Key_PageUp or (key == Qt.Key.Key_Space and modifiers & Qt.KeyboardModifier.ShiftModifier)
            self.scroll_by(self.viewport().height() * (-1 if back else 1))
        elif key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and modifiers & Qt.KeyboardModifier.ShiftModifier:
            self.scroll_by(self.viewport().height() * (0.5 if key == Qt.Key.Key_Down else -0.5))
        elif (self.pages and not modifiers & command_modifiers
              and key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)):
            horizontal = key in (Qt.Key.Key_Left, Qt.Key.Key_Right)
            bar = self.horizontalScrollBar() if horizontal else self.verticalScrollBar()
            if self.zoom_mode == "fit" or bar.minimum() == bar.maximum():
                self._turn_page_with_arrow(-1 if key in (Qt.Key.Key_Left, Qt.Key.Key_Up) else 1)
                event.accept()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def _turn_page_with_arrow(self, step):
        viewport = QRectF(self.viewport().rect())
        page = self._arrow_page
        if page is None:
            visible = {}
            for index in self._visible_pages():
                rect = self._page_rectangle(index)
                overlap = viewport.intersected(rect)
                visible[index] = int(100 * overlap.width() * overlap.height() / (rect.width() * rect.height()))
            if self.layout_mode == "single" and visible.get(self.page) == 100:
                page = self.page
            elif visible:
                page = max(visible, key=lambda index: (visible[index], -index))
            else:
                page = self.current_page()

        target = page + step
        if target in self.positions and self.row_for[target] == self.row_for[page]:
            rect = self._page_rectangle(target)
            overlap = viewport.intersected(rect)
            if overlap.width() == rect.width() and overlap.height() * 100 >= rect.height():
                target += step
        target = max(0, min(target, len(self.pages) - 1))
        if target != page:
            self.go_to(target, arrow=True)

    def current_page(self):
        if self._arrow_page is not None:
            return self._arrow_page
        if not self.pages or not self.continuous:
            return self.page
        center = self.viewport().rect().center()
        pos = self._position(center, clamp=True)
        return pos[0] if pos else self.page

    def location(self, point=None):
        point = QPointF(point) if point is not None else QPointF(self.viewport().rect().center())
        return self._position(point, clamp=True)

    def restore(self, location, point=None):
        if not location or not self.pages:
            return
        self._arrow_page = None
        index, x, y = location
        index = min(index, len(self.pages) - 1)
        if index not in self.positions:
            self.page = index
            self._layout()
        world = self._transform(index)[0].map(QPointF(x, y)) * self.zoom
        point = QPointF(point) if point is not None else QPointF(self.viewport().rect().center())
        left, top, _, _ = self.positions[index]
        self.horizontalScrollBar().setValue(round(left + world.x() - point.x()))
        self.verticalScrollBar().setValue(round(top + world.y() - point.y()))
        self.location_changed.emit()
        self.viewport().update()

    def change_zoom(self, factor, point=None):
        if self.pages and factor > 0:
            self.set_zoom("custom", self.zoom * factor, point)

    def set_zoom(self, mode, scale=None, point=None):
        self.cancel_click_series()
        if not self.pages:
            return
        location = self.location(point)
        self.page = location[0]
        self.zoom_mode = mode
        self.input_revision += 1
        self.content_anchor = None
        if scale is not None:
            self.zoom = max(MIN_SCALE, min(MAX_SCALE, scale))
        self.content_box = None
        self._layout()
        self.restore(location, point)
        if mode == "content":
            generation, index = self.generation, self.page

            def ready(box, error):
                if not isValid(self):
                    return
                if not error and generation == self.generation and self.zoom_mode == mode and index == self.page:
                    current = self.location()
                    self.content_box = box
                    self._layout()
                    if self.content_anchor is not None:
                        revision, anchor = self.content_anchor
                        self.content_anchor = None
                        self.restore(anchor if revision == self.input_revision else current)
                    elif box:
                        x0, y0, x1, y1 = box
                        self.restore((index, (x0 + x1) / 2, (y0 + y1) / 2))
                    else:
                        self.restore(location, point)

            self.session.request("content_bounds", (index,), ready, priority=4)

    def preserve_content_anchor(self, location):
        if self.zoom_mode == "content" and self.content_box is None:
            self.content_anchor = self.input_revision, location

    def set_layout(self, mode=None, continuous=None, rtl=None, rotation=None):
        self.input_revision += 1
        self.cancel_click_series()
        location = self.location()
        if location:
            self.page = location[0]
        if mode is not None:
            self.layout_mode = mode
        if continuous is not None:
            self.continuous = continuous
        if rtl is not None:
            self.rtl = rtl
        if rotation is not None:
            self.rotation = rotation % 360
        self._layout()
        self.restore(location)

    def go_to(self, index, y=0, x=None, *, arrow=False):
        if not 0 <= index < len(self.pages):
            return
        self._arrow_page = None
        self.input_revision += 1
        self.cancel_click_series()
        self.page = index
        if not self.continuous or index not in self.positions:
            self._layout()
        transform, _, _ = self._transform(index)
        point = transform.map(QPointF(x or 0, y))
        left, top, width, _ = self.positions[index]
        if not x and not y:
            point = QPointF(0, 0)
        bar = self.horizontalScrollBar()
        if arrow:
            # Keep visible pages horizontally in place. Bring a clipped target's left edge into view.
            if left < bar.value() or left + width > bar.value() + self.viewport().width():
                bar.setValue(left - MARGIN)
        else:
            horizontal = (left + point.x() * self.zoom - MARGIN if x is not None
                          else left + width / 2 - self.viewport().width() / 2)
            bar.setValue(round(horizontal))
        self.verticalScrollBar().setValue(round(top + point.y() * self.zoom - MARGIN))
        if arrow:
            # A short page or a page in the same spread may share a scroll position with another page.
            self._arrow_page = index
        self.location_changed.emit()
        self.viewport().update()

    def _page_rectangle(self, index):
        left, top, width, height = self.positions[index]
        return QRectF(left - self.horizontalScrollBar().value(), top - self.verticalScrollBar().value(), width, height)

    def _visible_pages(self, area=None):
        if not self.rows:
            return ()
        area = self.viewport().rect() if area is None else area
        offset = self.verticalScrollBar().value()
        start = max(0, bisect_right(self.row_tops, area.top() + offset) - 1)
        stop = bisect_right(self.row_tops, area.bottom() + offset)
        return tuple(p for row in self.rows[start:stop] for p in row if self._page_rectangle(p).intersects(QRectF(area)))

    def _position(self, point, clamp=False):
        if not self.rows:
            return None
        point = QPointF(point)
        y = point.y() + self.verticalScrollBar().value()
        index = max(0, bisect_right(self.row_tops, y) - 1)
        candidates = [p for row in self.rows[max(0, index - 1):index + 2] for p in row]
        best = None
        for page in candidates:
            rect = self._page_rectangle(page)
            local = point - rect.topLeft()
            bounded = QPointF(min(max(local.x(), 0), rect.width()), min(max(local.y(), 0), rect.height()))
            distance = (local - bounded).manhattanLength()
            if best is None or distance < best[0]:
                best = distance, page, bounded
        distance, page, bounded = best
        if distance and not clamp:
            return None
        inverse, _ = self._transform(page)[0].inverted()
        position = inverse.map(bounded / self.zoom)
        return page, position.x(), position.y()

    def _tile(self, page, tx, ty, width, height, prefetch):
        scale, rotation, generation = self.zoom, self.rotation, self.generation
        key = self.document, generation, page, scale, rotation, tx, ty
        if tile := self.cache.get(key):
            return tile
        if self.session is None:
            return None
        request_key = id(self), key
        if key in self.pending:
            if not prefetch:
                self.session.promote(request_key, 0)
            return None
        self.pending.add(key)

        def current():
            return (generation, scale, rotation) == (self.generation, self.zoom, self.rotation) and page in self.positions

        def needed():
            # The view may have been deleted while this request was queued (its tab was closed).
            if not isValid(self) or not current() or not self.isVisible():
                return False
            tile = QRectF(tx, ty, TILE, TILE).translated(self._page_rectangle(page).topLeft())
            visible = tile.intersects(QRectF(self.viewport().rect()))
            return visible or (prefetch and tile.intersects(self._prefetch_area()))

        def received(result, error):
            if not isValid(self):
                return
            self.pending.discard(key)
            if result and generation == self.generation:
                value = tile_image(result)
                if not value[2].isNull():
                    self.cache.put(key, value)
                    if current():
                        self.viewport().update()
            elif error and current():
                self.message.emit(error)

        self.session.request("render", (page, scale, (tx, ty, min(tx + TILE, width), min(ty + TILE, height)), rotation, True),
                             received, priority=4 if prefetch else 0, key=request_key, needed=needed)
        return None

    def _prefetch_area(self):
        area = QRectF(self.viewport().rect())
        dx, dy = self.direction
        return area.translated(dx * area.width(), dy * area.height())

    def _paint_tiles(self, painter, area, prefetch=False):
        complete = True
        for page in self._visible_pages(area):
            rect = self._page_rectangle(page)
            region = rect.intersected(QRectF(area)).translated(-rect.topLeft())
            for ty in range(max(0, int(region.top())) // TILE * TILE, ceil(region.bottom()), TILE):
                for tx in range(max(0, int(region.left())) // TILE * TILE, ceil(region.right()), TILE):
                    tile = self._tile(page, tx, ty, int(rect.width()), int(rect.height()), prefetch)
                    if painter is not None and tile:
                        x, y, image = tile
                        painter.drawImage(rect.topLeft() + QPointF(x, y), image)
                    complete &= tile is not None
        return complete

    @staticmethod
    def _rect_args(box):
        x0, y0, x1, y1 = box
        return x0, y0, x1 - x0, y1 - y0

    def _project(self, page, point):
        return self._page_rectangle(page).topLeft() + self._transform(page)[0].map(QPointF(*point)) * self.zoom

    def highlight_handles(self, page):
        key = self.selected_note
        if not key or key[0] != page or key in self.running_notes or key in self.resizing_notes:
            return ()
        note = self.notes.get(page, {}).get(key)
        if not note or not note["quads"]:
            return ()
        first, last = note["quads"][0], note["quads"][-1]
        return tuple(tuple((a + b) / 2 for a, b in zip(top, bottom))
                     for top, bottom in ((first[0], first[2]), (last[1], last[3])))

    def _highlight_handle_at(self, point):
        if self.selected_note is not None:
            page = self.selected_note[0]
            if page in self.positions:
                handles = self.highlight_handles(page)
                if handles:
                    distances = [(self._project(page, handle) - point).manhattanLength() for handle in handles]
                    moving = min(range(len(handles)), key=distances.__getitem__)
                    if distances[moving] <= 11:
                        return page, handles, moving
        return None

    def _start_highlight_drag(self, point):
        hit = self._highlight_handle_at(point)
        if hit is None:
            return False
        page, handles, moving = hit
        self.clear_selection()
        drag = self.highlight_drag = HighlightDrag(self.selected_note, moving, point, point)

        def ready(lines):
            if self.highlight_drag is not drag or self.selected_note != drag.key:
                return
            note = self.notes.get(page, {}).get(drag.key)
            if not lines or not note:
                self.clear_selection()
                return
            first, last = note["quads"][0], note["quads"][-1]
            points = []
            for handle, quad in zip(handles, (first, last)):
                center = tuple(sum(p[axis] for p in quad) / 4 for axis in (0, 1))
                # Move just inside the mark to distinguish two touching glyphs.
                points.append(tuple(edge + (inside - edge) * .001 for edge, inside in zip(handle, center)))
            drag.original = drag.ends = tuple(sorted(nearest(lines, *point) for point in points))
            self._preview_highlight(lines)
            if drag.released and self.highlight_drag is drag:
                self._finish_highlight_drag()

        self.load_text(page, ready)
        return True

    def _preview_highlight(self, lines):
        drag = self.highlight_drag
        if drag.original is None:
            return
        page = drag.key[0]
        if page not in self.positions or self.selected_note != drag.key:
            self.clear_selection()
            return
        if (drag.point - drag.press).manhattanLength() < 3:
            drag.ends, drag.preview, drag.handle = drag.original, (), None
            self.viewport().update()
            return
        rect = self._page_rectangle(page)
        local = drag.point - rect.topLeft()
        bounded = QPointF(min(max(local.x(), 0), rect.width()), min(max(local.y(), 0), rect.height()))
        inverse, _ = self._transform(page)[0].inverted()
        document_point = inverse.map(bounded / self.zoom)
        hit = nearest(lines, document_point.x(), document_point.y())
        first, last = drag.original
        ends = (min(hit, last), last) if drag.moving == 0 else (first, max(hit, first))
        if ends != drag.ends:
            drag.ends = ends
            if ends == drag.original:
                drag.preview, drag.handle = (), None
                self.viewport().update()
                return
            drag.preview = select({page: lines}, (page, *ends[0]), (page, *ends[1]),
                                  include_skipped=True).boxes.get(page, ())
            line, glyph = ends[drag.moving]
            x0, y0, x1, y1 = lines[line].glyphs[glyph].box
            drag.handle = (x0 + x1) / 2, (y0 + y1) / 2
            self.viewport().update()

    def _finish_highlight_drag(self):
        drag, self.highlight_drag = self.highlight_drag, None
        self.viewport().update()
        if drag.key == self.selected_note and drag.key not in self.running_notes and drag.ends != drag.original:
            self.note_resize_requested.emit(drag.key, *drag.ends)

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        viewport = self.viewport().rect()
        painter.fillRect(viewport, self.palette().mid())
        visible = self._visible_pages()
        for page in visible:
            painter.fillRect(self._page_rectangle(page), Qt.GlobalColor.white)
        complete = self._paint_tiles(painter, viewport)
        for page in visible:
            self._paint_overlays(painter, page)
        painter.end()
        if complete and visible:
            self._paint_tiles(None, self._prefetch_area(), prefetch=True)
        if self.selection_range:
            first, last = self.selection_range
            for page in visible:
                if first[0] <= page <= last[0]:
                    self.load_text(page)
        if complete:
            self.location_changed.emit()

    def show_navigation_target(self, target):
        self.navigation_target = target
        self.navigation_timer.stop()
        if target:
            self.navigation_timer.start()
        self.viewport().update()

    def _paint_overlays(self, painter, page):
        painter.save()
        painter.setClipRect(self._page_rectangle(page))
        drag = self.highlight_drag
        for key, note in self.notes.get(page, {}).items():
            if drag and drag.key == key and drag.preview:
                continue
            color = QColor(note["color"])
            outline = QColor(color)
            ca = note.get("opacity", 1)
            fill = QColor(*(round((1 - ca) * 255 + ca * channel)
                            for channel in (color.red(), color.green(), color.blue())))
            fill.setAlphaF(self.opacity)
            for quad in note["quads"]:
                polygon = QPolygonF([self._project(page, quad[i]) for i in (0, 1, 3, 2)])
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(fill)
                painter.drawPolygon(polygon)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                if key == self.selected_note or key in self.running_notes:
                    style = Qt.PenStyle.SolidLine if key == self.selected_note else Qt.PenStyle.DashLine
                    painter.setPen(QPen(outline, 1.5, style))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPolygon(polygon)
        transform = self._transform(page)[0]
        origin = self._page_rectangle(page).topLeft()
        if self.link_hover and self.link_hover[0] == page:
            _, box, native = self.link_hover
            rect = transform.mapRect(QRectF(*self._rect_args(box)))
            projected = QRectF(rect.topLeft() * self.zoom + origin, rect.size() * self.zoom)
            color = self.palette().link().color()
            if native:
                fill = QColor(color)
                fill.setAlpha(38)
                painter.fillRect(projected, fill)
            painter.setPen(QPen(color, 1.25, Qt.PenStyle.SolidLine if native else Qt.PenStyle.DotLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(projected)
        if handles := self.highlight_handles(page):
            color = QColor(self.notes[page][self.selected_note]["color"])
            if drag and drag.key == self.selected_note and drag.preview:
                fill = QColor(color)
                fill.setAlphaF(self.opacity)
                painter.setPen(QPen(color, 1.5))
                painter.setBrush(fill)
                for box in drag.preview:
                    rect = transform.mapRect(QRectF(*self._rect_args(box)))
                    painter.drawRect(QRectF(rect.topLeft() * self.zoom + origin, rect.size() * self.zoom))
                handles = list(handles)
                handles[drag.moving] = drag.handle
            painter.setPen(QPen(self.palette().base().color(), 1.2))
            painter.setBrush(color)
            for handle in handles:
                painter.drawEllipse(self._project(page, handle), 4.5, 4.5)
        selections = self.selection_range
        if selections and page in self.text_cache and selections[0][0] <= page <= selections[1][0]:
            selection = select({page: self.text_cache[page]}, *selections, include_skipped=True)
            for box in selection.boxes.get(page, ()):
                rect = transform.mapRect(QRectF(*self._rect_args(box)))
                painter.fillRect(QRectF(rect.topLeft() * self.zoom + origin, rect.size() * self.zoom), QColor(55, 133, 238, 85))
        for ordinal, boxes in enumerate(self.search_results.get(page, ())):
            active = self.current_match == (page, ordinal)
            if active or self.show_all_matches:
                for box in boxes:
                    rect = transform.mapRect(QRectF(*self._rect_args(box)))
                    painter.fillRect(QRectF(rect.topLeft() * self.zoom + origin, rect.size() * self.zoom),
                                     QColor(255, 143 if active else 220, 40, 145))
        target = self.navigation_target
        if target and target.page == page:
            rect = transform.mapRect(QRectF(*self._rect_args(target.box)))
            painter.setPen(QPen(QColor(40, 107, 202), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(rect.topLeft() * self.zoom + origin, rect.size() * self.zoom)
                             .adjusted(-3, -2, 3, 2))
        painter.restore()

    def load_text(self, index, callback=None, *, interactive=False):
        if index in self.text_cache:
            self.text_cache.move_to_end(index)
            if callback:
                callback(self.text_cache[index])
            return
        key = id(self), "text", self.generation, index
        if index in self.text_pending:
            if callback:
                self.text_pending[index].append(callback)
            if interactive or callback:
                self.session.promote(key, 5)
            return
        if self.session is None:
            return
        callbacks = self.text_pending[index] = [callback] if callback else []
        generation = self.generation

        def received(lines, error):
            if not isValid(self) or generation != self.generation or self.text_pending.get(index) is not callbacks:
                return
            self.text_pending.pop(index)
            if lines is not None:
                self.text_cache[index] = lines
                # Keep every visible page: a selection over more than 12 pages would otherwise evict
                # pages it still needs, and the highlight would flicker as they are read again.
                while len(self.text_cache) > min(64, max(12, len(self._visible_pages()) + 4)):
                    self.text_cache.popitem(last=False)
                self._select_endpoints()
                self.viewport().update()
            if error:
                self.message.emit(error)
            for ready in callbacks:
                ready(lines)

        self.session.request("text", (index,), received, priority=5 if interactive or callback else 7, key=key,
                             needed=lambda: isValid(self) and generation == self.generation and (
                                 index in self._visible_pages() or bool(callbacks) or
                                 index in tuple(p[0] for p in (self.anchor, self.pointer) if p)))

    def clear_selection(self):
        self.input_revision += 1
        self.cancel_click_series()
        self.selection_revision += 1
        self.end_selection_gesture()
        self.selection_range = None
        self.selection_origin = ""
        self.waiting = False
        self.highlight_drag = None
        self.viewport().update()

    def select_all(self):
        if self.pages:
            self.clear_selection()
            self.selection_range = (0, 0, 0), (len(self.pages) - 1, 2**31, 2**31)
            self.viewport().update()

    def resolve_selection(self, callback):
        if not self.selection_range:
            return
        bounds_ = self.selection_range
        origin = self.selection_origin
        revision, generation = self.selection_revision, self.generation
        pending = iter(range(bounds_[0][0], bounds_[1][0] + 1))
        collected = [[], []]

        def advance():
            if not isValid(self) or revision != self.selection_revision or generation != self.generation:
                return
            index = next(pending, None)
            if index is None:
                callback(tuple(join_selections(parts) for parts in collected))
                return

            def received(result, error):
                if not isValid(self) or revision != self.selection_revision or generation != self.generation:
                    return
                if error:
                    self.message.emit(error)
                    return
                if result is None:
                    return
                for bucket, part in zip(collected, result):
                    bucket.append(part)
                advance()

            self.session.request("selection_page", (index, *bounds_, origin), received, priority=6,
                                 needed=lambda: (isValid(self) and revision == self.selection_revision
                                                 and generation == self.generation))

        advance()

    def show_link_hover(self, mark, tooltip=""):
        if mark == self.link_hover and tooltip == self.viewport().toolTip():
            return
        self.link_hover = mark
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if mark and mark[2]
                                  else Qt.CursorShape.IBeamCursor)
        self.viewport().setToolTip(tooltip)
        self.viewport().update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self.hover_position.emit(None)
            self.input_revision += 1
            self.cancel_click_series()
            self.end_selection_gesture()
            self.pan_start = event.position()
            self.panning = False
        elif event.button() == Qt.MouseButton.LeftButton:
            self.start_left_press(event.position())

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_left_press(event.position(), double=True)
        else:
            self.mousePressEvent(event)

    def start_left_press(self, point, double=False):
        self.hover_position.emit(None)
        self.setFocus()
        if self._start_highlight_drag(point):
            return
        position = self._position(point)
        clicks = 2 if double else 1
        if self.click_series and self.click_timer.isActive() and position:
            previous, pixel, count = self.click_series
            distance = max(abs(pixel.x() - point.x()), abs(pixel.y() - point.y()))
            if position[0] == previous[0] and (double or distance <= QApplication.styleHints().mouseDoubleClickDistance()):
                clicks = count + 1
        self.clear_selection()
        self.press, self.press_clicks, self.dragging_selection = point, clicks, False
        self.anchor = self.pointer = position
        if position:
            if clicks > 1:
                self._select_endpoints()
            else:
                self.load_text(position[0], interactive=True)

    def cancel_click_series(self):
        self.click_timer.stop()
        self.click_series = None

    def finish_click_series(self):
        self.click_timer.stop()
        series, self.click_series = self.click_series, None
        if series and series[2] == 1:
            self.clicked.emit(*series[0])

    def end_selection_gesture(self):
        self.anchor = self.pointer = self.unit_anchor = self.drag_point = None
        self.waiting = False
        self.drag_scroll.stop()

    def hideEvent(self, event):
        self.input_revision += 1
        self._invalidate_pointer_position()
        self.end_selection_gesture()
        super().hideEvent(event)

    def selection_span(self, position, *, exact=False):
        page, x, y = position
        lines = self.text_cache[page]
        hit = nearest(lines, x, y)
        if hit is None:
            return None
        row, index = hit
        line = lines[row]
        glyph = line.glyphs[index]
        if exact:
            left, top, right, bottom = glyph.box
            if not (left <= x <= right and top <= y <= bottom):
                return None
        if self.press_clicks == 1:
            first = last = index
        elif self.press_clicks == 2:
            if not glyph.text.strip():
                if exact:
                    return None
                index = min((i for i, item in enumerate(line.glyphs) if item.text.strip()),
                            key=lambda i: abs(i - index), default=None)
                if index is None:
                    return None
            first, last = word_range(line, index)
        else:
            first, last = 0, len(line.glyphs) - 1
        return (page, row, first), (page, row, last), line.kind

    def scroll_selection(self):
        point = self.drag_point
        if point is None or not self.anchor:
            self.drag_scroll.stop()
            return
        self.scroll_by(-32 if point.y() < 0 else 32)
        self.pointer = self._position(point, clamp=True)
        self._select_endpoints()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.NoButton:
            point = event.position()
            self.hover_position.emit(None if self._highlight_handle_at(point) else self._position(point))
        elif self.highlight_drag and event.buttons() & Qt.MouseButton.LeftButton:
            drag = self.highlight_drag
            drag.point = event.position()
            if lines := self.text_cache.get(drag.key[0]):
                self._preview_highlight(lines)
        elif self.pan_start is not None and event.buttons() & Qt.MouseButton.RightButton:
            change = event.position() - self.pan_start
            if self.panning or change.manhattanLength() >= 5:
                self.panning = True
                self.pan_start = event.position()
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - round(change.x()))
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - round(change.y()))
        elif self.anchor and event.buttons() & Qt.MouseButton.LeftButton:
            self.dragging_selection |= (event.position() - self.press).manhattanLength() >= DRAG_DISTANCE
            if not self.dragging_selection:
                return
            point = event.position()
            if point.y() < 0 or point.y() >= self.viewport().height():
                self.drag_point = point
                if not self.drag_scroll.isActive():
                    self.drag_scroll.start()
                    self.scroll_selection()
            else:
                self.drag_point = None
                self.drag_scroll.stop()
                self.pointer = self._position(point, clamp=True)
                self._select_endpoints()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            if not self.panning:
                self.menu_requested.emit(event.globalPosition().toPoint(), self._position(event.position()))
            self.pan_start = None
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if drag := self.highlight_drag:
            drag.point, drag.released = event.position(), True
            if lines := self.text_cache.get(drag.key[0]):
                self._preview_highlight(lines)
                if self.highlight_drag is drag:
                    self._finish_highlight_drag()
            return
        self.drag_scroll.stop()
        self.drag_point = None
        if not self.anchor:
            return
        point = event.position()
        self.dragging_selection |= (point - self.press).manhattanLength() >= DRAG_DISTANCE
        if not self.dragging_selection:
            clicks = self.press_clicks
            if clicks < 3:
                self.click_series = self.anchor, self.press, clicks
                self.click_timer.start(QApplication.doubleClickInterval())
            if clicks == 1:
                self.end_selection_gesture()
                return
        else:
            self.pointer = self._position(point, clamp=True)
        self.waiting = True
        self._select_endpoints()

    def _select_endpoints(self):
        if not self.anchor or not self.pointer or (self.press_clicks == 1 and not self.dragging_selection):
            return
        if self.unit_anchor is None:
            page = self.anchor[0]
            if page not in self.text_cache:
                self.load_text(page, interactive=True)
                return
            self.unit_anchor = self.selection_span(self.anchor, exact=self.press_clicks > 1)
            if self.unit_anchor is None:
                if self.waiting:
                    self.end_selection_gesture()
                return
        first, last, origin = self.unit_anchor
        if self.dragging_selection:
            page = self.pointer[0]
            if page not in self.text_cache:
                self.load_text(page, interactive=True)
                return
            if target := self.selection_span(self.pointer):
                first, last = min(first, target[0]), max(last, target[1])
        selection = first, last
        if self.waiting:
            self.end_selection_gesture()
        if self.selection_range != selection:
            self.selection_revision += 1
            self.selection_origin = origin
            self.selection_range = selection
            self.viewport().update()
            self.location_changed.emit()
