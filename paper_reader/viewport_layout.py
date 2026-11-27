"""Page placement and fit calculations for the PDF viewport."""

from dataclasses import dataclass
from math import ceil

from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform


MARGIN = 16
ACTUAL_SIZE = 96 / 72
MIN_SCALE = ACTUAL_SIZE * 0.0833
MAX_SCALE = ACTUAL_SIZE * 64


@dataclass
class ViewportLayout:
    zoom: float
    rows: list[list[int]]
    row_for: dict[int, int]
    row_tops: list[int]
    positions: dict[int, tuple[int, int, int, int]]
    width: int
    height: int


def page_transform(size, degrees):
    width, height = size
    rotation = QTransform().rotate(degrees)
    box = rotation.mapRect(QRectF(0, 0, width, height))
    rotation *= QTransform.fromTranslate(-box.x(), -box.y())
    return rotation, box.width(), box.height()


def layout_pages(pages, viewport_size, *, page, mode, continuous, rtl, rotation, zoom_mode, zoom, content_box):
    grouped = []
    index = 0
    while index < len(pages):
        count = 1 if mode == "single" or (mode == "book" and index == 0) else 2
        row = list(range(index, min(index + count, len(pages))))
        grouped.append(row[::-1] if rtl else row)
        index += count
    row_for = {page_index: row for row, page_indices in enumerate(grouped) for page_index in page_indices}
    shown = grouped if continuous or not grouped else [grouped[row_for[page]]]
    sizes = [page_transform(size, rotation)[1:] for size in pages]
    width, height = viewport_size
    if shown and zoom_mode != "custom":
        active = grouped[row_for[page]]
        room_width = max(1, width - (len(active) + 1) * MARGIN - 2)
        room_height = max(1, height - 2 * MARGIN - 2)
        width_fit = room_width / sum(sizes[p][0] for p in active)
        height_fit = room_height / max(sizes[p][1] for p in active)
        if zoom_mode == "content" and content_box:
            x0, y0, x1, y1 = content_box
            transform, _, _ = page_transform(pages[page], rotation)
            box = transform.mapRect(QRectF(x0, y0, x1 - x0, y1 - y0))
            fit = min(room_width / max(box.width(), 1), room_height / max(box.height(), 1))
        else:
            fit = width_fit if zoom_mode == "width" else min(width_fit, height_fit)
        zoom = max(MIN_SCALE, min(MAX_SCALE, fit))

    widths = [sum(ceil(sizes[p][0] * zoom) for p in row) + (len(row) + 1) * MARGIN for row in shown]
    content_width = max([width, *widths])
    positions, row_tops = {}, []
    top = MARGIN
    for row, row_width in zip(shown, widths):
        row_tops.append(top)
        x = (content_width - row_width) // 2 + MARGIN
        for page_index in row:
            pw, ph = (ceil(value * zoom) for value in sizes[page_index])
            positions[page_index] = x, top, pw, ph
            x += pw + MARGIN
        top += max(positions[p][3] for p in row) + MARGIN
    return ViewportLayout(zoom, shown, row_for, row_tops, positions, content_width, top)
