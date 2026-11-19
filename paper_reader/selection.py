"""Text geometry and reading order for PDF selections."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import re
from statistics import median


Point = tuple[float, float]
Box = tuple[float, float, float, float]
Quad = tuple[Point, Point, Point, Point]


@dataclass(frozen=True, slots=True)
class Glyph:
    text: str
    box: Box
    quad: Quad


@dataclass(frozen=True, slots=True)
class Line:
    glyphs: tuple[Glyph, ...]
    box: Box
    size: float
    horizontal: bool
    paragraph: int
    kind: str = ""

    @property
    def text(self) -> str:
        return "".join(glyph.text for glyph in self.glyphs)


@dataclass(frozen=True, slots=True)
class Selection:
    source: str
    quads: dict[int, list[Quad]]
    boxes: dict[int, list[Box]]
    skipped: tuple[str, ...]


def bounds(boxes) -> Box:
    boxes = tuple(boxes)
    return (
        min(box[0] for box in boxes), min(box[1] for box in boxes),
        max(box[2] for box in boxes), max(box[3] for box in boxes),
    )


def nearest(lines: tuple[Line, ...], x: float, y: float) -> tuple[int, int] | None:
    if not lines:
        return None

    def distance(box):
        left, top, right, bottom = box
        return max(left - x, 0, x - right) ** 2 + max(top - y, 0, y - bottom) ** 2

    i = min(range(len(lines)), key=lambda index: distance(lines[index].box))
    line = lines[i]
    return i, min(range(len(line.glyphs)), key=lambda index: distance(line.glyphs[index].box))


def _bands(lines: list[Line], axis: int) -> list[list[Line]]:
    ordered = sorted(lines, key=lambda line: line.box[axis])
    groups: list[list[Line]] = []
    edge = float("-inf")
    for line in ordered:
        if line.box[axis] > edge + 1:
            groups.append([])
        groups[-1].append(line)
        edge = max(edge, line.box[axis + 2])
    return groups


def reading_order(lines: list[Line], width: float) -> tuple[Line, ...]:
    if len(lines) < 2:
        return tuple(lines)
    horizontal = [line for line in lines if line.horizontal]
    if not horizontal:
        return tuple(sorted(lines, key=lambda line: (line.box[1], line.box[0])))
    typical = median(line.size for line in horizontal)
    # Estimate a gutter with evidence from rows on both sides. Spanning material
    # stays in its own band so headings and equations keep their vertical place.
    best = None
    for step in range(36, 65, 2):
        center = width * step / 100
        gutter = max(typical * 0.6, width * 0.012)
        left = [line for line in lines if line.box[2] < center - gutter / 2]
        right = [line for line in lines if line.box[0] > center + gutter / 2]
        if len(left) < 2 or len(right) < 2:
            continue
        overlap = min(max(l.box[3] for l in left), max(l.box[3] for l in right))
        overlap -= max(min(l.box[1] for l in left), min(l.box[1] for l in right))
        if overlap < typical:
            continue
        score = len(left) + len(right), overlap, -abs(step - 50)
        if best is None or score > best[0]:
            best = score, center, gutter
    if best is None:
        return tuple(line for band in _bands(lines, 1) for line in sorted(band, key=lambda l: (l.box[1], l.box[0])))

    _, center, gutter = best
    spanning = [line for line in lines if line.box[0] <= center + gutter / 2 and line.box[2] >= center - gutter / 2]
    side = [line for line in lines if line not in spanning]
    ordered: list[Line] = []

    def emit_columns(group):
        ordered.extend(sorted(group, key=lambda l: (l.box[0] > center, l.box[1], l.box[0])))

    for band in _bands(spanning, 1):
        boundary = min(line.box[1] for line in band)
        emit_columns(line for line in side if line.box[1] < boundary)
        side = [line for line in side if line.box[1] >= boundary]
        ordered.extend(sorted(band, key=lambda line: (line.box[1], line.box[0])))
    emit_columns(side)
    return tuple(ordered)


def furniture_key(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.casefold())).strip()


def edge_band(line: Line, height: float) -> int:
    return -1 if line.box[3] <= height * 0.12 else (1 if line.box[1] >= height * 0.88 else 0)


def classify(lines: tuple[Line, ...], width: float, height: float, repeated: set) -> tuple[Line, ...]:
    counts: Counter[float] = Counter()
    for line in lines:
        if line.horizontal and not edge_band(line, height):
            counts[round(line.size, 1)] += len(line.glyphs)
    body_size = counts.most_common(1)[0][0] if counts else 0
    result = []
    for line in lines:
        text = line.text.strip()
        band = edge_band(line, height)
        kind = ""
        if band and ((band, furniture_key(text)) in repeated or re.fullmatch(r"[-–—―\s]*\d+[-–—―\s]*", text)
                     or re.match(r"^(?:https?://doi\.org/|doi\s*:)", text, re.I)):
            kind = "ヘッダー・フッター・ページ番号"
        elif re.match(r"^(?:(?:fig(?:ure)?|table)\.?\s*\d+\s*[:.：．]|\s*第?\s*\d*\s*[図表]\s*\d*[:.：．]?)", text, re.I):
            kind = "図表の見出し"
        elif not line.horizontal or line.size < body_size * 0.94:
            kind = "脚注・小さい文字・回転した文字"
        elif (re.search(r"\s{2,}\(\d+[a-z]?\)\s*$", text)
              or (re.search(r"[=∑∫√≤≥]", text) and len(re.findall(r"[A-Za-z]{3,}", text)) <= 2
                  and line.box[2] - line.box[0] < width * 0.42)):
            kind = "別行立ての数式"
        result.append(replace(line, kind=kind))
    return tuple(result)


_LIGATURES = str.maketrans({"ﬁ": "fi", "ﬀ": "ff", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "\u00ad": ""})
_CJK = r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]"


def clean_source(text: str, hyphenated: set[str] | None = None) -> str:
    text = text.translate(_LIGATURES)
    paragraphs = []
    for paragraph in re.split(r"\n[ \t]*\n", text):
        def wrap_word(match: re.Match) -> str:
            left, right = match.groups()
            word = f"{left}-{right}"
            if word.casefold() in (hyphenated or ()) or right[0].isdigit() or right[0].isupper():
                return word
            return left + right

        paragraph = re.sub(r"(\w+)-\n(\w+)", wrap_word, paragraph)
        paragraph = re.sub(rf"({_CJK})[ \t]*\n[ \t]*|[ \t]*\n[ \t]*(?={_CJK})", r"\1", paragraph)
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if paragraph:
            paragraphs.append(paragraph)
    return "\n\n".join(paragraphs)


def select(pages: dict[int, tuple[Line, ...]], first: tuple[int, int, int],
           last: tuple[int, int, int], include_skipped: bool = False) -> Selection:
    start, end = sorted((first, last))
    if end[0] - start[0] >= 3:
        raise ValueError("選択範囲は3ページ以内にしてください。")
    text: list[str] = []
    skipped: set[str] = set()
    quads: dict[int, list[Quad]] = {}
    boxes: dict[int, list[Box]] = {}
    previous = None
    hyphenated: set[str] = set()
    for page in range(start[0], end[0] + 1):
        for index, line in enumerate(pages.get(page, ())):
            hyphenated.update(word.casefold() for word in re.findall(r"\w+-\w+", line.text))
            position = page, index
            if position < start[:2] or position > end[:2]:
                continue
            edge = position in (start[:2], end[:2])
            if not include_skipped and line.kind and (not edge or line.kind == "ヘッダー・フッター・ページ番号"):
                skipped.add(line.kind)
                continue
            left = start[2] if position == start[:2] else 0
            right = end[2] + 1 if position == end[:2] else len(line.glyphs)
            selected = list(line.glyphs[left:right])
            while selected and not selected[0].text.strip():
                selected.pop(0)
            while selected and not selected[-1].text.strip():
                selected.pop()
            if not selected:
                continue
            if previous is not None and page == previous[0] and line.paragraph != previous[1]:
                # A PDF often starts new blocks at columns. Keep the separator
                # for an indented paragraph or a clear gap within one column.
                moved_right = 3 < line.box[0] - previous[2][0] < line.size * 3
                same_column = abs(line.box[0] - previous[2][0]) < line.size * 3
                if moved_right or (same_column and line.box[1] - previous[2][3] > line.size * 0.65):
                    text.append("")
            previous = page, line.paragraph, line.box
            text.append("".join(glyph.text for glyph in selected))
            a, b = selected[0].quad, selected[-1].quad
            quads.setdefault(page, []).append((a[0], b[1], a[2], b[3]))
            boxes.setdefault(page, []).append(bounds(glyph.box for glyph in selected))
    return Selection(clean_source("\n".join(text), hyphenated), quads, boxes, tuple(sorted(skipped)))
