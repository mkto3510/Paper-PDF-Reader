"""Text geometry and reading order for PDF selections."""

from __future__ import annotations

from dataclasses import dataclass
import re
from statistics import median
import unicodedata


Point = tuple[float, float]
Box = tuple[float, float, float, float]
Quad = tuple[Point, Point, Point, Point]


@dataclass(frozen=True, slots=True)
class Glyph:
    text: str
    box: Box
    quad: Quad
    artifact: bool = False


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
class Exclusion:
    page: int
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class Selection:
    source: str
    quads: dict[int, list[Quad]]
    boxes: dict[int, list[Box]]
    skipped: tuple[str, ...]
    raw_source: str = ""
    hyphenated: frozenset[str] = frozenset()
    starts_paragraph: bool = False
    exclusions: tuple[Exclusion, ...] = ()


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

    distances = [distance(line.box) for line in lines]
    closest = min(distances)

    def glyph_distance(position):
        index, glyph = position
        box = lines[index].glyphs[glyph].box
        # Two lines or glyphs can have overlapping boxes, especially near subscripts.
        center_distance = (x - (box[0] + box[2]) / 2) ** 2 + (y - (box[1] + box[3]) / 2) ** 2
        return distance(box), center_distance

    return min(((index, glyph) for index, line in enumerate(lines) if distances[index] == closest
                for glyph in range(len(line.glyphs))), key=glyph_distance, default=None)


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


def _horizontal_order(lines):
    rows = []
    for line in sorted(lines, key=lambda item: (item.box[1], item.box[0])):
        row = rows[-1] if rows else None
        # Superscripts and inline formulas can be separate text objects whose
        # bounding boxes start above or below their printed line.
        if row and line.horizontal and all(
            other.horizontal
            and min(line.box[3], other.box[3]) - max(line.box[1], other.box[1])
            >= min(line.box[3] - line.box[1], other.box[3] - other.box[1]) * .35
            and (min(line.box[2], other.box[2]) - max(line.box[0], other.box[0]) <= 2
                 or min(line.box[2] - line.box[0], other.box[2] - other.box[0]) < 1.5 * min(line.size, other.size))
            and abs(line.box[1] + line.box[3] - other.box[1] - other.box[3])
            <= 1.1 * max(line.size, other.size)
            for other in row
        ):
            row.append(line)
        else:
            rows.append([line])
    return [line for row in rows for line in sorted(row, key=lambda item: item.box[0])]


def reading_regions(lines, width):
    """Return ordered horizontal regions and columns using geometry alone."""
    if len(lines) < 2:
        return [list(lines)] if lines else []
    horizontal = [line for line in lines if line.horizontal]
    vertical = [line for line in lines if not line.horizontal]
    if sum(len(line.glyphs) for line in vertical) > sum(len(line.glyphs) for line in horizontal):
        return [sorted(column, key=lambda line: (-line.box[2], line.box[1]))
                for column in reversed(_bands(lines, 0))]
    if not horizontal:
        return [sorted(lines, key=lambda line: (line.box[1], line.box[0]))]
    typical = median(line.size for line in horizontal)
    # Estimate a gutter with evidence from rows on both sides. Spanning material
    # stays in its own band so headings and equations keep their vertical place.
    best = None
    for step in range(36, 65, 2):
        center = width * step / 100
        gutter = max(typical * 0.6, width * 0.012)
        left = [line for line in horizontal if line.box[2] < center - gutter / 2]
        right = [line for line in horizontal if line.box[0] > center + gutter / 2]
        if len(left) < 2 or len(right) < 2:
            continue
        # Separate text objects on a single printed row do not establish columns.
        if any(max(l.box[1] for l in side) - min(l.box[1] for l in side) < typical * .65
               for side in (left, right)):
            continue
        overlap = min(max(l.box[3] for l in left), max(l.box[3] for l in right))
        overlap -= max(min(l.box[1] for l in left), min(l.box[1] for l in right))
        if overlap < typical:
            continue
        score = len(left) + len(right), overlap, -abs(step - 50)
        if best is None or score > best[0]:
            best = score, center, gutter
    if best is None:
        return [_horizontal_order(lines)]

    _, center, gutter = best
    span_margin = max(typical * 2, width * .055)
    spanning = [line for line in horizontal if line.box[0] < center - span_margin
                and line.box[2] > center + span_margin
                and line.box[3] - line.box[1] < typical * 2.1]
    spanning_ids = {id(line) for line in spanning}
    side = [line for line in lines if id(line) not in spanning_ids]
    regions = []

    def emit_columns(group):
        left, right = [], []
        for line in group:
            (right if line.box[0] > center else left).append(line)
        regions.extend(_horizontal_order(column) for column in (left, right) if column)

    # Consecutive full-width lines (e.g. an abstract) form one region.
    bands = []
    for line in sorted(spanning, key=lambda item: item.box[1]):
        if not bands or line.box[1] - max(item.box[3] for item in bands[-1]) > typical * .75:
            bands.append([])
        bands[-1].append(line)
    for band in bands:
        top, bottom = min(l.box[1] for l in band), max(l.box[3] for l in band)
        emit_columns(line for line in side if line.box[1] < top)
        within = [line for line in side if top <= line.box[1] < bottom and line.horizontal]
        regions.append(_horizontal_order([*band, *within]))
        claimed = {id(line) for line in within}
        side = [line for line in side if line.box[1] >= top and id(line) not in claimed]
    emit_columns(side)
    return regions


def reading_order(lines, width):
    return tuple(line for region in reading_regions(lines, width) for line in region)


def correct_stream_order(lines, width, graphics=()):
    """Repair repeated column crossings while preserving the PDF's order elsewhere."""
    main = [line for line in lines if line.horizontal and not line.kind]
    if len(main) < 6:
        return lines
    typical = median(line.size for line in main)
    gutter = max(typical * .75, width * .012)
    evidence = [line for line in main if line.box[2] - line.box[0] >= width * .13
                and not any(g[0] <= line.box[0] and line.box[2] <= g[2]
                            and g[1] <= line.box[1] and line.box[3] <= g[3] for g in graphics)]
    rows = []
    for line in sorted(evidence, key=lambda item: item.box[1] + item.box[3]):
        y = (line.box[1] + line.box[3]) / 2
        if not rows or y - rows[-1][0] > typical * .65:
            rows.append((y, []))
        rows[-1][1].append(line)
    candidates = set()
    for _, row in rows:
        right = None
        for line in sorted(row, key=lambda item: item.box[0]):
            left = line.box[0]
            if right is not None and left - right >= gutter:
                candidates.add(round((left + right) / 2, 1))
            right = max(line.box[2], right if right is not None else left)

    # A gutter can disappear behind a figure, leaving only one occupied side.
    # A body line crossing the gutter, in contrast, bounds the repair vertically.
    bands = []
    for x in sorted(candidates):
        top, pairs = float("-inf"), 0
        for y, row in (*rows, (float("inf"), ())):
            left = any(line.box[2] <= x - gutter / 2 for line in row)
            right = any(line.box[0] >= x + gutter / 2 for line in row)
            barrier = not row or any(line.box[0] < x < line.box[2] for line in row)
            if barrier:
                if pairs >= 3:
                    bands.append((pairs, top, y, x))
                top, pairs = y, 0
            elif left and right:
                pairs += 1
    if not bands:
        return lines

    ordered = list(lines)
    stable_ids = {id(line) for line in evidence}
    # The strongest split first. Further splits can repair a third column.
    for _, top, bottom, x in sorted(bands, key=lambda band: (-band[0], abs(band[3] - width / 2))):
        slots, sides = [], []
        for index, line in enumerate(ordered):
            y = (line.box[1] + line.box[3]) / 2
            if line.kind or not line.horizontal or not top < y < bottom:
                continue
            if line.box[2] <= x or line.box[0] >= x:
                slots.append(index)
                sides.append(line.box[0] >= x)
        sequence = [right for index, right in zip(slots, sides) if id(ordered[index]) in stable_ids]
        if sum(a != b for a, b in zip(sequence, sequence[1:])) < 3:
            continue
        left_column = [ordered[index] for index, right in zip(slots, sides) if not right]
        right_column = [ordered[index] for index, right in zip(slots, sides) if right]
        for index, line in zip(slots, (*left_column, *right_column)):
            ordered[index] = line
    return tuple(ordered)


def furniture_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text)).strip()


def edge_band(line: Line, height: float, fraction: float = .12) -> int:
    return -1 if line.box[3] <= height * fraction else (1 if line.box[1] >= height * (1 - fraction) else 0)


_LIGATURES = str.maketrans({"ﬁ": "fi", "ﬀ": "ff", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "\u00ad": ""})
_CJK = r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]"


def clean_source(text: str, hyphenated: set[str] | None = None) -> str:
    text = re.sub(r"\u00ad[ \t]*\n[ \t]*(?![ \t]*\n)", "", text)
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


def join_selections(parts: list[Selection]) -> Selection:
    if len(parts) == 1:
        return parts[0]
    fragments = []
    hyphenated = set()
    for part in parts:
        hyphenated.update(part.hyphenated)
        text = part.raw_source or part.source
        if text:
            if fragments and part.starts_paragraph and not fragments[-1].endswith(("-", "\u00ad")):
                fragments.append("")
            fragments.append(text)
    raw = "\n".join(fragments)
    return Selection(clean_source(raw, hyphenated),
                     {key: value for part in parts for key, value in part.quads.items()},
                     {key: value for part in parts for key, value in part.boxes.items()},
                     tuple(sorted({kind for part in parts for kind in part.skipped})),
                     exclusions=tuple(item for part in parts for item in part.exclusions))


def select(pages: dict[int, tuple[Line, ...]], first: tuple[int, int, int],
           last: tuple[int, int, int], include_skipped: bool = False, *, origin="legacy", rules=None) -> Selection:
    start, end = sorted((first, last))
    text: list[str] = []
    skipped: set[str] = set()
    exclusions: list[Exclusion] = []
    quads: dict[int, list[Quad]] = {}
    boxes: dict[int, list[Box]] = {}
    previous = None
    starts_paragraph = False
    hyphenated: set[str] = set()
    last_run = None

    def exclude(page, kind, glyphs):
        nonlocal last_run
        value = "".join(glyph.text for glyph in glyphs).strip()
        if value:
            skipped.add(kind)
            exclusions.append(Exclusion(page, kind, value))
        last_run = None

    for page in sorted(p for p in pages if start[0] <= p <= end[0]):
        lines = pages[page]
        for index, line in enumerate(lines):
            hyphenated.update(word.casefold() for word in re.findall(r"\w+-\w+", line.text))
            position = page, index
            if position < start[:2] or position > end[:2]:
                continue
            edge = position in (start[:2], end[:2])
            kind = line.kind
            left = start[2] if position == start[:2] else 0
            right = end[2] + 1 if position == end[:2] else len(line.glyphs)
            selected = list(line.glyphs[left:right])
            while selected and not selected[0].text.strip():
                selected.pop(0)
            while selected and not selected[-1].text.strip():
                selected.pop()
            if not selected:
                continue
            if not include_skipped:
                enabled = bool(kind and (rules is None or rules.get(kind, True)))
                if origin == "caption":
                    omit = kind != "caption" and (not kind or enabled)
                elif not origin:
                    omit = enabled
                else:
                    omit = enabled and (not edge or kind in ("furniture", "ruby", "line_number"))
                if omit:
                    exclude(page, kind or "body", selected)
                    continue
            if previous is not None and page == previous[0] and line.paragraph != previous[1]:
                # A PDF often starts new blocks at columns. Keep the separator
                # for an indented paragraph or a clear gap within one column.
                moved_right = 3 < line.box[0] - previous[2][0] < line.size * 3
                same_column = abs(line.box[0] - previous[2][0]) < line.size * 3
                if moved_right or (same_column and line.box[1] - previous[2][3] > line.size * 0.65):
                    text.append("")
            runs = []
            exclude_artifacts = not include_skipped and (rules is None or rules.get("artifact", True))
            for offset, glyph in enumerate(selected):
                omitted = exclude_artifacts and glyph.artifact
                if omitted and kind == "caption" and glyph.text in ".,:;!?":
                    # Some PDF producers mark linked caption punctuation as an artifact.
                    # Keep it when it touches the visible text of that same caption.
                    neighbours = selected[max(0, offset - 1):offset] + selected[offset + 1:offset + 2]
                    omitted = not any(not char.artifact and char.text.isalnum() for char in neighbours)
                if not runs or runs[-1][0] != omitted:
                    runs.append((omitted, []))
                runs[-1][1].append(glyph)
            if any(not omitted for omitted, _ in runs):
                if not text and len(lines) > index + 1 and left == 0:
                    following = lines[index + 1]
                    gap = following.box[1] - line.box[1]
                    indent = line.box[0] - following.box[0]
                    overlap = min(line.box[2], following.box[2]) - max(line.box[0], following.box[0])
                    starts_paragraph = (line.horizontal and following.horizontal and overlap > line.size * 3
                                        and 0 < gap < line.size * 2.2 and 3 < indent < line.size * 3)
                previous = page, line.paragraph, line.box
                text.append("".join(glyph.text for omitted, run in runs if not omitted for glyph in run))
            for omitted, run in runs:
                if omitted:
                    exclude(page, "artifact", run)
                    continue
                a, b = run[0].quad, run[-1].quad
                box = bounds(glyph.box for glyph in run)
                if not line.horizontal:
                    quads.setdefault(page, []).append((a[0], b[1], a[2], b[3]))
                    boxes.setdefault(page, []).append(box)
                    last_run = None
                    continue
                # Glyph quads are in the unrotated coordinate system required by PDF annotations.
                geometry = bounds((x, y, x, y) for glyph in run for x, y in glyph.quad)
                merge = False
                if last_run and last_run[0] == page and last_run[1] != index:
                    _, _, previous_geometry, previous_size, previous_kind = last_run
                    px0, py0, px1, py1 = previous_geometry
                    x0, y0, x1, y1 = geometry
                    size = max(line.size, previous_size)
                    merge = (kind == previous_kind and px0 <= x0 and -size * .45 <= x0 - px1 <= size * .85
                             and min(py1, y1) > max(py0, y0)
                             and abs(py0 + py1 - y0 - y1) <= size * 1.1)
                    if merge:
                        geometry = bounds((previous_geometry, geometry))
                        box = bounds((boxes[page][-1], box))
                x0, y0, x1, y1 = geometry
                quad = ((x0, y0), (x1, y0), (x0, y1), (x1, y1))
                if merge:
                    quads[page][-1] = quad
                    boxes[page][-1] = box
                else:
                    quads.setdefault(page, []).append(quad)
                    boxes.setdefault(page, []).append(box)
                last_run = page, index, geometry, line.size, kind
    raw = "\n".join(text)
    return Selection(clean_source(raw, hyphenated), quads, boxes, tuple(sorted(skipped)),
                     raw, frozenset(hyphenated), starts_paragraph, tuple(exclusions))
