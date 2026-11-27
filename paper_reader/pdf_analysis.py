"""Text, page layout, and citation and figure indexes for one PDF."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from time import perf_counter
import unicodedata

import pymupdf as mu

from .analysis_rules import DEFAULT_RULES, classify, page_evidence
from .artifacts import artifact_glyphs
from .captions import caption_heading
from .citations import END, HEADING, Reference, ReferenceIndex, citation_at
from .layout import PageLayout
from .paragraphs import enumerate_paragraphs
from .search import find_page
from .selection import (Glyph, Line, bounds, correct_stream_order, edge_band, furniture_key, reading_order,
                        reading_regions, select)


# Pages on each side of a figure mention that figure_nearby reads before the full scan is needed.
FIGURE_WINDOW = 6
# Pages whose index lines, reading regions and caption decisions are kept for the scans.
PAGE_LAYOUTS = 128

_SPACING_ACCENTS = {"´": "\u0301", "ˆ": "\u0302", "˜": "\u0303",
                    "¨": "\u0308", "¸": "\u0327", "ˇ": "\u030c", "˚": "\u030a"}


def _compose_accents(glyphs):
    result = []
    for glyph in glyphs:
        if result and (accent := _SPACING_ACCENTS.get(result[-1].text)) and glyph.text.isalpha():
            mark = result[-1]
            x = (mark.box[0] + mark.box[2]) / 2
            composed = unicodedata.normalize("NFC", glyph.text + accent)
            if (len(composed) == 1 and glyph.box[0] <= x <= glyph.box[2]
                    and mark.box[1] < glyph.box[3] and glyph.artifact == mark.artifact):
                result[-1] = replace(glyph, text=composed, box=bounds((glyph.box, mark.box)))
                continue
        result.append(glyph)
    return tuple(result)


def _baseline(raw):
    """Return (size, baseline) of the span that holds most characters of a line."""
    spans = [span for span in raw["spans"] if span["chars"]]
    if not spans:
        return None
    main = max(spans, key=lambda span: len(span["chars"]))
    return main["size"], main["chars"][0]["origin"][1], len(main["chars"])


def _is_raised(span, y, base):
    """Tell whether a character is a superscript: small and sitting above the line's main baseline."""
    if base is None or not span["chars"]:
        return False
    size, baseline, count = base
    if count <= len(span["chars"]):
        return False  # the main text of the line is never a superscript
    return bool(span["flags"] & 1) or (span["size"] <= size * .8 and y < baseline - size * .15)


def _continues_word(a, b):
    """Tell whether line b goes on from line a on the same printed line, a word gap to its right."""
    if not (a.horizontal and b.horizontal) or abs(a.size - b.size) > max(a.size, b.size) * .15:
        return False
    size = max(a.size, b.size)
    height_a, height_b = a.box[3] - a.box[1], b.box[3] - b.box[1]
    same_row = (abs(a.box[1] + a.box[3] - b.box[1] - b.box[3]) / 2 <= size * .2
                and abs(height_a - height_b) <= size * .25)
    return same_row and -size * .15 <= b.box[0] - a.box[2] <= size


def _rejoin_word_pieces(lines, single_glyph=False):
    """Join lines that a PDF stores as one text object per word back into printed lines.

    Some publishers write every word as its own text object, and MuPDF then returns each word as a line of its own
    block. Such words look like table cells or repeated headers to the analysis, and selections lose them. Only a
    page where many lines continue the line before them on the same row is changed, so pages MuPDF reads as whole
    lines keep their lines and blocks. The joined lines are grouped into blocks again by their vertical spacing.
    """
    if len(lines) < 20:
        return lines
    joins = sum(_continues_word(a, b) for a, b in zip(lines, lines[1:]))
    if joins < 20 or joins < len(lines) * .3:
        return lines
    chains = []
    for line in lines:
        if chains and _continues_word(chains[-1][-1], line):
            chains[-1].append(line)
        else:
            chains.append([line])
    joined = []
    for chain in chains:
        glyphs = list(chain[0].glyphs)
        for previous, line in zip(chain, chain[1:]):
            gap = line.box[0] - previous.box[2]
            if gap > max(previous.size, line.size) * .15 and glyphs[-1].text.strip() and line.glyphs[0].text.strip():
                box = (previous.box[2], min(previous.box[1], line.box[1]), line.box[0], max(previous.box[3], line.box[3]))
                quad = (((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3]))
                        if glyphs[-1].quad else ())
                glyphs.append(Glyph(" ", box, quad, glyphs[-1].artifact and line.glyphs[0].artifact))
            glyphs.extend(line.glyphs)
        box = bounds(line.box for line in chain)
        if single_glyph:
            glyphs = [Glyph("".join(glyph.text for glyph in glyphs), box, ())]
        count = sum(len(line.glyphs) for line in chain)
        size = sum(line.size * len(line.glyphs) for line in chain) / count
        joined.append(replace(chain[0], glyphs=tuple(glyphs), box=box, size=size))
    block = 0
    for index, line in enumerate(joined):
        if index:
            above = joined[index - 1]
            size = max(above.size, line.size)
            below = (line.horizontal and above.horizontal and -size * .2 <= line.box[1] - above.box[3] <= size * .8
                     and min(line.box[2], above.box[2]) > max(line.box[0], above.box[0])
                     and abs(line.size - above.size) <= size * .1)
            block += not below
        joined[index] = replace(line, paragraph=block)
    return joined


def _join_spaced_headings(lines):
    """Join letter-spaced headings that PDFs store as one line per character.

    Japanese journals often print 文献 or 謝辞 with a wide gap between the characters, and MuPDF returns each
    character as its own line. Only fragments whose joined text is a bibliography heading or an end heading are
    merged, so table cells and other short lines are left alone.
    """
    short = [line for line in lines if line.horizontal and 0 < len(line.text.strip()) <= 3]
    if len(short) < 2:
        return lines
    short.sort(key=lambda line: (round((line.box[1] + line.box[3]) / 2 / max(line.size, 1)), line.box[0]))
    merged, absorbed = [], set()
    chain = []

    def close():
        if len(chain) >= 2:
            text = " ".join(item.text.strip() for item in chain)
            if HEADING.fullmatch(text) or END.fullmatch(text):
                box = bounds(item.box for item in chain)
                first = chain[0]
                merged.append(replace(first, glyphs=(Glyph(text, box, ()),), box=box))
                absorbed.update(id(item) for item in chain)
        chain.clear()

    for line in short:
        if chain:
            previous = chain[-1]
            same_row = abs((line.box[1] + line.box[3]) / 2 - (previous.box[1] + previous.box[3]) / 2) < previous.size * .4
            near = 0 <= line.box[0] - previous.box[2] <= previous.size * 6
            if same_row and near and abs(line.size - previous.size) <= previous.size * .15:
                chain.append(line)
                continue
            close()
        chain.append(line)
    close()
    if not absorbed:
        return lines
    return [line for line in lines if id(line) not in absorbed] + merged


def _close_spaced_letters(lines):
    """Close up headings printed with a space between letters ("R E F E R E N C E S", set in small capitals).

    Only a short line whose letters, closed up, are a bibliography or end heading is changed, and only when most
    of its words are one or two letters long, so ordinary words are never joined.
    """
    result = []
    for line in lines:
        text = line.text.strip()
        words = text.split()
        if line.horizontal and 4 <= len(words) and len(text) <= 40 and sum(len(word) <= 2 for word in words) >= len(words) * .7:
            closed = "".join(words)
            if (HEADING.fullmatch(closed) or END.fullmatch(closed)) and not (HEADING.fullmatch(text) or END.fullmatch(text)):
                line = replace(line, glyphs=(Glyph(closed, line.box, ()),))
        result.append(line)
    return result


class PdfAnalysis:
    def __init__(self, read_page):
        self.read_page = read_page
        self.doc = None
        self.options = DEFAULT_RULES.copy()
        self.text_cache: OrderedDict = OrderedDict()
        self.citation_cache: OrderedDict = OrderedDict()
        self.evidence_cache: OrderedDict = OrderedDict()
        self.page_layouts: OrderedDict = OrderedDict()
        self.references = None
        self.figure_page = 0
        self.figure_pages = {}
        self.named_figure_pages = {}
        self.in_bibliography = False
        self.chapter_pages = set()

    def attach(self, doc):
        self.doc = doc
        self.text_cache.clear()
        self.page_layouts.clear()

    def detach(self):
        self.doc = None
        self.references = None
        self.figure_page = 0
        self.figure_pages.clear()
        self.named_figure_pages.clear()
        self.in_bibliography = False
        self.chapter_pages = set()
        self.evidence_cache.clear()
        self.page_layouts.clear()
        self.citation_cache.clear()

    def update_rules(self, settings):
        self.options = DEFAULT_RULES | settings
        self.text_cache.clear()
        self.citation_cache.clear()
        self.page_layouts.clear()

    def citation_at(self, page, x, y):
        return citation_at(self.text(page, quads=False), x, y)

    def _page_layout(self, index):
        """Return the page as the index reads it, from the cache when it was read before."""
        cache = self.page_layouts
        if index in cache:
            cache.move_to_end(index)
            return cache[index]
        page = self.read_page(index)
        lines, width, height = self._index_rows(page)
        fraction = self.options["edge_band"] / 100
        edges = frozenset((side, furniture_key(line.text), line.box[1] / height)
                          for line in lines if (side := edge_band(line, height, fraction)))
        cache[index] = layout = PageLayout(lines, width, height, edges)
        if len(cache) > PAGE_LAYOUTS:
            cache.popitem(last=False)
        return layout

    def _index_rows(self, page):
        # Read a single page and avoid instantiating per-glyph quads for the index.
        blocks = page.get_text("dict", flags=mu.TEXTFLAGS_DICT & ~mu.TEXT_PRESERVE_IMAGES)["blocks"]
        lines = []
        for block_index, block in enumerate(blocks):
            for line in block.get("lines", ()):
                spans = line["spans"]
                text = "".join(span["text"] for span in spans).strip()
                if text:
                    box = tuple(line["bbox"])
                    count = sum(len(span["text"]) for span in spans)
                    size = sum(len(span["text"]) * span["size"] for span in spans) / count
                    leading_bold = bool(spans[0]["flags"] & mu.TEXT_FONT_BOLD)
                    lines.append(Line((Glyph(text, box, ()),), box, size,
                                      line["wmode"] == 0 and abs(line["dir"][1]) < .15, block_index,
                                      leading_bold=leading_bold))
        lines = _close_spaced_letters(_join_spaced_headings(_rejoin_word_pieces(lines, single_glyph=True)))
        width, height = page.rect.width, page.rect.height
        if page.rotation in (90, 270):
            width, height = height, width
        return lines, width, height

    def reference_step(self, budget_ms=None):
        """Read pages for the reference index. With a budget, keep reading until that many milliseconds pass,
        so a long scan does not pay a worker round trip per page."""
        if self.references is None:
            outline = [(depth, title, page, None) for depth, title, page in self.doc.get_toc()]
            self.references = ReferenceIndex(self.doc.page_count, outline)
        deadline = perf_counter() + budget_ms / 1000 if budget_ms else 0
        while not self.references.complete:
            index = self.references.next_page
            page = self.read_page(index)
            matrix = page.rotation_matrix
            rows = [(line, tuple(mu.Rect(line.box) * matrix)) for line in self._page_layout(index).reading_order]
            self.references.add_page(index, rows)
            if perf_counter() >= deadline:
                break
        return self.references.complete

    def reference_targets(self, position, keys, ordered=False):
        if self.references and self.references.complete:
            return self.references.targets(position, keys, ordered=ordered)
        return (None,) * len(keys) if ordered else ()

    def _caption_rows(self, index, skip_bibliography=False):
        page = self.read_page(index)
        layout = self._page_layout(index)
        lines = layout.lines
        in_bibliography = self.in_bibliography and index not in self.chapter_pages
        carried = False
        if skip_bibliography and not any(HEADING.fullmatch(line.text.strip()) or END.fullmatch(line.text.strip())
                                         for line in lines):
            # No section heading here, so reading order cannot change which lines are eligible. A bibliography
            # state carried from an earlier page is only trusted while no confirmed caption shows up (a contents
            # line or another article's heading can start it by mistake); see the end of this method.
            carried = in_bibliography
            eligible = {id(line) for line in lines}
        elif skip_bibliography:
            eligible = set()
            for region in layout.regions:
                for line in region:
                    text = line.text.strip()
                    if HEADING.fullmatch(text):
                        in_bibliography = True
                    elif END.fullmatch(text):
                        in_bibliography = False
                    elif not in_bibliography:
                        eligible.add(id(line))
        else:
            eligible = {id(line) for line in lines}
        if not any(id(line) in eligible and caption_heading(line.text) for line in lines):
            return (), in_bibliography
        groups, _, _ = layout.captions(self._page_evidence(index, page))
        if skip_bibliography:
            starts = {line.box for line in lines if id(line) in eligible}
            groups = [group for group in groups if any(
                x0 >= group.box[0] - .1 and x1 <= group.box[2] + .1 and abs(y0 - group.box[1]) < .1
                for x0, y0, x1, _ in starts)]
        matrix = page.rotation_matrix
        found = tuple(
            (group, Reference(index, tuple(mu.Rect(group.box) * matrix), group.text[:110],
                              tuple(mu.Rect(group.object_box) * matrix) if group.object_box else None))
            for group in groups if group.kind != "other" and not group.continued
        )
        if carried:
            # Confirmed captions end a bibliography state that was carried in; without any, it goes on.
            return (found, False) if found else ((), True)
        return found, in_bibliography

    def figure_step(self, budget_ms=None):
        """Read pages for the figure index, like reference_step."""
        deadline = perf_counter() + budget_ms / 1000 if budget_ms else 0
        while self.figure_page < self.doc.page_count:
            index = self.figure_page
            if index == 0:
                self.chapter_pages = {page - 1 for depth, title, page in self.doc.get_toc()
                                      if depth == 1 and not HEADING.fullmatch(title.strip())}
            entries, bibliography = self._caption_rows(index, skip_bibliography=True)
            self.figure_pages[index] = entries
            self.in_bibliography = bibliography
            self.figure_page += 1
            if perf_counter() >= deadline:
                break
        return self.figure_page >= self.doc.page_count

    @staticmethod
    def _figure_targets(keys, entries):
        targets = []
        for kind, label in keys:
            exact = [target for group, target in entries if (group.kind, group.label) == (kind, label)]
            if not exact and len(label) > 1 and label[-2].isdigit() and label[-1].isalpha():
                # "Fig. 3b" is a panel of figure 3. A caption that lists the panel wins when several share the number.
                base = [(group, target) for group, target in entries if (group.kind, group.label) == (kind, label[:-1])]
                exact = ([target for group, target in base if f"({label[-1]})" in group.text.casefold()]
                         or [target for _, target in base])
            targets.append(exact[0] if len(exact) == 1 else None)
        return tuple(targets)

    def figure_targets(self, position, keys, ordered=True):
        if self.figure_page < self.doc.page_count:
            return (None,) * len(keys)
        entries = tuple(entry for page_entries in self.figure_pages.values() for entry in page_entries)
        page, x, y = position
        return tuple(None if target and target.page == page and mu.Point(x, y) in mu.Rect(target.box) else target
                     for target in self._figure_targets(keys, entries))

    def figure_nearby(self, position, keys):
        """Resolve figure mentions from the pages around the mention, without reading the whole document.

        Pages are read in rings of growing distance. A key resolves when the first ring holding its label has
        exactly one caption for it. Keys left as None (no caption nearby, or several in one ring) are for the
        caller to resolve with the full scan.
        """
        page, x, y = position
        found = [None] * len(keys)
        pending = set(range(len(keys)))
        for distance in range(FIGURE_WINDOW + 1):
            ring = {index for index in (page - distance, page + distance) if 0 <= index < self.doc.page_count}
            entries = []
            for index in sorted(ring):
                cached = self.named_figure_pages.get(index)
                if cached is None:
                    cached, _ = self._caption_rows(index)
                    self.named_figure_pages[index] = cached
                entries.extend(cached)
            for number in sorted(pending):
                key = keys[number]
                matching = [target for group, target in entries if (group.kind, group.label) == tuple(key)]
                if len(matching) > 1:
                    pending.discard(number)
                    continue
                target, = self._figure_targets((key,), entries)
                if target is None:
                    continue
                pending.discard(number)
                if not (target.page == page and mu.Point(x, y) in mu.Rect(target.box)):
                    found[number] = target
            if not pending:
                break
        return tuple(found)

    def named_figure_target(self, position, page):
        marker = self.citation_at(*position)
        if marker is None or marker.kind != "figure" or len(marker.keys) != 1:
            return None
        entries = self.named_figure_pages.get(page)
        if entries is None:
            entries, _ = self._caption_rows(page)
            self.named_figure_pages[page] = entries
        target, = self._figure_targets(marker.keys, entries)
        return target

    def _read_lines(self, page, mark_artifacts=False, *, quads=True):
        text = page.get_text("rawdict", flags=mu.TEXTFLAGS_RAWDICT & ~mu.TEXT_PRESERVE_IMAGES, sort=False)
        artifacts = artifact_glyphs(page) if mark_artifacts else set()
        lines: list[Line] = []
        for block_index, block in enumerate(text["blocks"]):
            for raw in block.get("lines", ()):
                glyphs = []
                sizes = []
                base = _baseline(raw)
                for span in raw["spans"]:
                    for char in span["chars"]:
                        x, y = char["origin"]
                        quad = tuple(tuple(point) for point in mu.recover_char_quad(raw["dir"], span, char)) if quads else ()
                        glyphs.append(Glyph(
                            char["c"], tuple(char["bbox"]), quad,
                            (round(x, 1), round(y, 1), char["c"]) in artifacts,
                            _is_raised(span, y, base),
                        ))
                        sizes.append(span["size"])
                if glyphs and any(glyph.text.strip() for glyph in glyphs):
                    horizontal = raw["wmode"] == 0 and abs(raw["dir"][1]) < 0.15
                    composed = _compose_accents(glyphs) if horizontal else tuple(glyphs)
                    leading_bold = bool(raw["spans"][0]["flags"] & mu.TEXT_FONT_BOLD)
                    lines.append(Line(composed, bounds(g.box for g in composed),
                                      sum(sizes) / len(sizes), horizontal, block_index,
                                      leading_bold=leading_bold))
        return _rejoin_word_pieces(lines)

    def _display_lines(self, lines, page):
        if not page.rotation:
            return tuple(lines)
        matrix = page.rotation_matrix
        return tuple(replace(line, box=tuple(mu.Rect(line.box) * matrix),
                             glyphs=tuple(replace(g, box=tuple(mu.Rect(g.box) * matrix)) for g in line.glyphs))
                     for line in lines)

    def _page_evidence(self, index, page):
        if index not in self.evidence_cache:
            self.evidence_cache[index] = page_evidence(page)
            if len(self.evidence_cache) > 24:
                self.evidence_cache.popitem(last=False)
        else:
            self.evidence_cache.move_to_end(index)
        return self.evidence_cache[index]

    def text(self, index, *, quads=True):
        cache = self.text_cache if quads or index in self.text_cache else self.citation_cache
        if index in cache:
            cache.move_to_end(index)
            return cache[index]
        page = self.read_page(index)
        width, height = page.rect.width, page.rect.height
        if page.rotation in (90, 270):
            width, height = height, width
        lines = self._read_lines(page, self.options.get("artifact", True), quads=quads)
        band = self.options["edge_band"] / 100
        neighbours = set()
        if any(edge_band(line, height, band) for line in lines):
            for near in range(max(0, index - 3), min(self.doc.page_count, index + 4)):
                if near != index:
                    neighbours.update(self._page_layout(near).edges)
        regions = reading_regions(lines, width)
        evidence = self._page_evidence(index, page)
        classified = classify(lines, width, height, neighbours, evidence, self.options, regions, first_page=index == 0)
        leading, content, trailing = [], [], []
        horizontal_body = sum(len(line.glyphs) for line in classified if line.horizontal) > sum(
            len(line.glyphs) for line in classified if not line.horizontal)
        if horizontal_body:
            classified = correct_stream_order(classified, width, (*evidence[0], *evidence[3]))
        else:
            ranks = {id(line): rank for rank, line in enumerate(lines)}
            classified = tuple(classified[ranks[id(line)]] for region in regions for line in region)
        for line in classified:
            side_mark = (horizontal_body and not line.horizontal and line.kind in ("margin", "small")
                         and (line.box[2] <= width * .07 or line.box[0] >= width * .93))
            if side_mark:
                trailing.append(line)
            elif line.kind != "furniture":
                content.append(line)
            elif edge_band(line, height, band) < 0:
                leading.append(line)
            else:
                trailing.append(line)
        result = self._display_lines((*leading, *content, *trailing), page)
        cache[index] = result
        if len(cache) > (12 if quads else 6):
            cache.popitem(last=False)
        return result

    def quick_text(self, index):
        """The lines of a page in reading order, before classification.

        This is a provisional answer for hit testing and the blue selection while `text` is still deciding which
        lines are headers, tables or captions: every line has an empty kind, and the list has no furniture moved
        to its ends. Line indices therefore differ from those of `text`; callers match them up by position.
        """
        page = self.read_page(index)
        width = page.rect.width
        if page.rotation in (90, 270):
            width = page.rect.height
        lines = self._read_lines(page, False)
        return self._display_lines(reading_order(lines, width), page)

    def paragraphs(self, first=0, last=None):
        """List the paragraphs of pages first..last that are handed to translation (see paragraphs.py)."""
        last = self.doc.page_count - 1 if last is None else min(last, self.doc.page_count - 1)
        pages = {index: self.text(index, quads=False) for index in range(first, last + 1)}
        return enumerate_paragraphs(pages, self.options)

    def selection_page(self, index, first, last, origin=None):
        page = {index: self.text(index)}
        if origin is None:
            anchor_lines = self.text(first[0])
            origin = anchor_lines[first[1]].kind if first[1] < len(anchor_lines) else ""
        return (select(page, first, last, origin=origin, rules=self.options),
                select(page, first, last, include_skipped=True))

    def search(self, index, query, match_case, whole, match_marks):
        return find_page(self.text(index), query, match_case, whole, match_marks)
