"""MuPDF operations. A single child process owns the open document."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
from itertools import count
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
from time import perf_counter, sleep
import unicodedata

import pymupdf as mu

from .analysis_rules import DEFAULT_RULES, classify, page_evidence
from .artifacts import artifact_glyphs
from .captions import caption_heading, classify_captions
from .citations import END, HEADING, Reference, ReferenceIndex, citation_at
from .layout import PageLayout
from .search import find_page
from .selection import Glyph, Line, bounds, correct_stream_order, edge_band, furniture_key, reading_regions, select


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


def _signature(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


class PdfEngine:
    def __init__(self, working_directory=None):
        self.working_directory = working_directory
        self.path: Path | None = None
        self.doc = None
        self.analysis_doc = None
        self.temp: Path | None = None
        self.password = ""
        self.signature = None
        self.backed_up = False
        self.text_cache: OrderedDict = OrderedDict()
        self.citation_cache: OrderedDict = OrderedDict()
        self.evidence_cache: OrderedDict = OrderedDict()
        self.page_layouts: OrderedDict = OrderedDict()
        self.display_lists = OrderedDict()
        self.options = DEFAULT_RULES.copy()
        self.history = []
        self.state_ids = count(1)
        self.saved_revision = 0
        self.references = None
        self.figure_page = 0
        self.figure_pages = {}
        self.named_figure_pages = {}
        self.in_bibliography = False
        self.chapter_pages = set()

    def open(self, filename: str, password: str = ""):
        path = Path(filename).resolve(strict=True)
        stamp = _signature(path)
        # MuPDF's Windows file handles prevent atomic replacement of an open PDF.
        # Keep a private disk snapshot so editors can freely replace the original.
        with NamedTemporaryFile(suffix=".pdf", dir=self.working_directory, delete=False) as handle:
            snapshot = Path(handle.name)
        doc = None
        try:
            shutil.copyfile(path, snapshot)
            if _signature(path) != stamp:
                raise ValueError("PDF changed while it was being opened.")
            doc = mu.open(snapshot)
            if not doc.is_pdf or not doc.page_count:
                raise ValueError("ページのある PDF を指定してください。")
            if doc.needs_pass and not doc.authenticate(password):
                return {"password": True}
            pages = [tuple(doc[index].rect)[2:] for index in range(doc.page_count)]
            outline = [(level, title, number, tuple(dest["to"]) if dest.get("to") else None)
                       for level, title, number, dest in doc.get_toc(simple=False)]
            doc.journal_enable()
            self.close()
            self.path, self.doc, self.password, self.signature = path, doc, password, stamp
            self.temp = snapshot
            self.backed_up = False
            self.history = [(0, ())]
            self.state_ids = count(1)
            self.saved_revision = 0
            self.text_cache.clear()
            self.page_layouts.clear()
            return {"pages": pages, "outline": outline, "name": path.name, "signature": stamp}
        finally:
            if self.temp != snapshot:
                if doc is not None:
                    doc.close()
                snapshot.unlink(missing_ok=True)

    def close(self):
        self.references = None
        self.figure_page = 0
        self.figure_pages.clear()
        self.named_figure_pages.clear()
        self.in_bibliography = False
        self.chapter_pages = set()
        self.evidence_cache.clear()
        self.page_layouts.clear()
        self.citation_cache.clear()
        self.display_lists.clear()
        if self.analysis_doc is not None:
            self.analysis_doc.close()
            self.analysis_doc = None
        if self.doc is not None:
            self.doc.close()
            self.doc = None
        if self.temp is not None:
            self.temp.unlink(missing_ok=True)
            self.temp = None

    def _display_list(self, index, hide_highlights):
        key = index, hide_highlights
        if key in self.display_lists:
            self.display_lists.move_to_end(key)
            return self.display_lists[key]
        page = self.doc[index]
        if not hide_highlights:
            result = page.get_displaylist()
        else:
            ml = mu.mupdf
            internal = ml.pdf_page_from_fz_page(page.this)
            display = ml.fz_new_display_list(ml.FzRect(*page.rect))
            device = ml.fz_new_list_device(display)
            matrix, cookie = ml.FzMatrix(), ml.FzCookie()
            ml.pdf_run_page_contents(internal, device, matrix, cookie)
            annot = ml.pdf_first_annot(internal)
            while annot.m_internal is not None:
                if ml.pdf_annot_type(annot) != ml.PDF_ANNOT_HIGHLIGHT:
                    ml.pdf_run_annot(annot, device, matrix, cookie)
                annot = ml.pdf_next_annot(annot)
            ml.pdf_run_page_widgets(internal, device, matrix, cookie)
            ml.fz_close_device(device)
            result = mu.DisplayList(display)
        self.display_lists[key] = result
        if len(self.display_lists) > 8:
            self.display_lists.popitem(last=False)
        return result

    def render(self, page, scale, tile, rotation=0, hide_highlights=False):
        display = self._display_list(page, hide_highlights)
        matrix = mu.Matrix(scale, scale).prerotate(rotation)
        scaled = display.rect * matrix
        matrix.e -= scaled.x0
        matrix.f -= scaled.y0
        clip = mu.Rect(tile) * ~matrix
        pixmap = display.get_pixmap(matrix=matrix, clip=clip, colorspace=mu.csRGB, alpha=False)
        return pixmap.width, pixmap.height, pixmap.stride, pixmap.x, pixmap.y, pixmap.samples

    def update_rules(self, settings):
        self.options = DEFAULT_RULES | settings
        self.text_cache.clear()
        self.citation_cache.clear()
        self.page_layouts.clear()

    def content_bounds(self, index):
        page = self.doc[index]
        entries = [tuple(mu.Rect(rect) * page.rotation_matrix) for kind, rect in page.get_bboxlog()
                   if kind in ("fill-text", "stroke-text", "fill-image", "stroke-path") and not mu.Rect(rect).is_empty]
        crop = mu.Rect(bounds(entries)) & page.rect if entries else page.rect
        return tuple(crop)

    def page_links(self, index):
        result = []
        for link in self.doc[index].get_links():
            if link["kind"] in (mu.LINK_GOTO, mu.LINK_NAMED):
                page, point = link.get("page"), link.get("to")
                if not isinstance(page, int) or not 0 <= page < self.doc.page_count:
                    continue
                if isinstance(point, (mu.Point, tuple, list)):
                    entry = {"box": tuple(link["from"]), "page": page, "point": tuple(point)}
                    if not any(point) and self._page_only_link(link.get("xref")):
                        entry["page_only"] = True
                    elif link["kind"] == mu.LINK_NAMED:
                        target = self.doc[page]
                        # The page transform includes both crop offset and rotation.
                        matrix = mu.mupdf.FzMatrix()
                        mu.mupdf.pdf_page_transform(target._pdf_page(), mu.mupdf.FzRect(), matrix)
                        entry["point"] = tuple(mu.Point(point) * mu.JM_py_from_matrix(matrix))
                    result.append(entry)
            elif link["kind"] == mu.LINK_URI:
                result.append({"box": tuple(link["from"]), "uri": link.get("uri", "")})
        return result

    def _page_only_link(self, xref):
        if not isinstance(xref, int) or xref < 1:
            return False
        pdf = self._pdf()
        low = mu.mupdf
        annot = low.pdf_load_object(pdf, xref)
        destination = low.pdf_dict_gets(annot, "Dest")
        if low.pdf_is_null(destination):
            action = low.pdf_dict_gets(annot, "A")
            destination = low.pdf_dict_gets(action, "D")
        if low.pdf_is_name(destination) or low.pdf_is_string(destination):
            destination = low.pdf_lookup_dest(pdf, destination)
        if low.pdf_is_dict(destination):
            destination = low.pdf_dict_gets(destination, "D")
        if not low.pdf_is_array(destination) or low.pdf_array_len(destination) < 2:
            return False
        style = low.pdf_to_name(low.pdf_array_get(destination, 1))
        if style in ("Fit", "FitB"):
            return True
        if style == "XYZ":
            return all(low.pdf_is_null(low.pdf_array_get(destination, i)) for i in (2, 3))
        return style in ("FitH", "FitBH", "FitV", "FitBV") and low.pdf_is_null(low.pdf_array_get(destination, 2))

    def citation_at(self, page, x, y):
        return citation_at(self.text(page, quads=False), x, y)

    def _page_layout(self, index):
        """Return the page as the index reads it, from the cache when it was read before."""
        cache = self.page_layouts
        if index in cache:
            cache.move_to_end(index)
            return cache[index]
        page = self._analysis_page(index)
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
        lines = _join_spaced_headings(lines)
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
            page = self._analysis_page(index)
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
        page = self._analysis_page(index)
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

    def properties(self):
        return dict(self.doc.metadata or {}) | {"filename": str(self.path), "size": self.path.stat().st_size,
                                                "pages": self.doc.page_count}

    def fonts(self, index):
        return [(base, face, encoding, xref != 0 and extension not in ("n/a", ""))
                for xref, extension, face, base, _, encoding, *_ in self.doc.get_page_fonts(index, full=True)]

    def reload(self, password=None):
        backed_up, rules = self.backed_up, self.options
        result = self.open(str(self.path), self.password if password is None else password)
        if "pages" in result:
            self.backed_up, self.options = backed_up, rules
        return result

    def _analysis_page(self, index):
        page = self.doc[index]
        if not page.rotation:
            return page
        # PyMuPDF temporarily writes /Rotate during text extraction. Use the
        # immutable snapshot for that work so it doesn't enter the edit history.
        if self.analysis_doc is None:
            doc = mu.open(self.temp)
            if doc.needs_pass and not doc.authenticate(self.password):
                doc.close()
                raise ValueError("PDF を開けませんでした。")
            self.analysis_doc = doc
        return self.analysis_doc[index]

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
        return lines

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
        page = self._analysis_page(index)
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
        classified = classify(lines, width, height, neighbours, evidence, self.options, regions)
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

    def selection_page(self, index, first, last, origin=None):
        page = {index: self.text(index)}
        if origin is None:
            anchor_lines = self.text(first[0])
            origin = anchor_lines[first[1]].kind if first[1] < len(anchor_lines) else ""
        return (select(page, first, last, origin=origin, rules=self.options),
                select(page, first, last, include_skipped=True))

    def search(self, index, query, match_case, whole, match_marks):
        return find_page(self.text(index), query, match_case, whole, match_marks)

    def annotation_list(self, index: int | None = None):
        indices = range(self.doc.page_count) if index is None else (index,)
        result = []
        for page_index in indices:
            page = self.doc[page_index]
            for annot in page.annots(types=(mu.PDF_ANNOT_HIGHLIGHT,)):
                color = annot.colors["stroke"]
                if len(color) == 1:
                    color = color * 3
                if len(color) == 4:
                    cyan, magenta, yellow, black = color
                    color = tuple(1 - min(1, channel + black) for channel in (cyan, magenta, yellow))
                if len(color) != 3:
                    color = (1, 0.83, 0)
                hex_color = "#" + "".join(f"{round(255 * component):02x}" for component in color)
                vertices = annot.vertices or ()
                matrix = page.rotation_matrix
                quads = [tuple(tuple(mu.Point(*p) * matrix) for p in vertices[i:i + 4])
                         for i in range(0, len(vertices) - 3, 4)]
                result.append({"page": page_index, "xref": annot.xref, "comment": annot.info["content"],
                               "color": hex_color, "box": tuple(annot.rect * matrix), "quads": quads,
                               "opacity": annot.opacity if annot.opacity >= 0 else 1.0})
        return result

    def _set_annotation(self, annot, color: str | None, comment: str | None):
        if color is not None:
            annot.set_colors(stroke=tuple(int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)))
        if comment is not None:
            annot.set_info(content=comment)
        annot.update()

    def _pdf(self):
        return mu.mupdf.pdf_document_from_fz_document(self.doc.this)

    def _annotation(self, page, xref):
        try:
            annot = page.load_annot(xref)
        except Exception as error:
            raise ValueError("注釈が見つかりません。") from error
        if annot is None or annot.type[0] != mu.PDF_ANNOT_HIGHLIGHT:
            raise ValueError("注釈が見つかりません。")
        return annot

    def _history_result(self, step, keys, value=None, discarded=()):
        for page, _ in keys:
            self.display_lists.pop((page, False), None)
        return {"value": value, "state": self.history[step][0], "undo": step > 0,
                "redo": step + 1 < len(self.history), "keys": keys, "discarded": discarded}

    def edit(self, command, args):
        step, _ = self.doc.journal_position()
        self.doc.journal_start_op(command)
        try:
            if command == "add_highlight":
                result = self.add_highlight(*args)
                keys = result
            elif command == "modify_highlight":
                result = self.modify_highlight(*args)
                keys = args[0]
            elif command == "resize_highlight":
                result = self.resize_highlight(*args)
                keys = {args[:2], (args[0], result)}
            else:
                raise ValueError(command)
        except Exception:
            mu.mupdf.pdf_abandon_operation(self._pdf())
            raise
        self.doc.journal_stop_op()
        current, _ = self.doc.journal_position()
        if current == step:
            return self._history_result(current, (), result)
        discarded = [state for state, _ in self.history[current:]]
        keys = tuple(keys)
        self.history[current:] = [(next(self.state_ids), keys)]
        return self._history_result(current, keys, result, discarded)

    def move_history(self, redo):
        step, steps = self.doc.journal_position()
        if (redo and step == steps) or (not redo and step == 0):
            return self._history_result(step, ())
        moved, keys = self.history[step + int(redo)]
        if redo:
            self.doc.journal_redo()
        else:
            self.doc.journal_undo()
        return self._history_result(step + (1 if redo else -1), keys, moved)

    def add_highlight(self, quads: dict, color: str, comment: str):
        added = []
        for index, points in quads.items():
            page = self.doc[index]
            annot = page.add_highlight_annot([mu.Quad(*quad) for quad in points])
            added.append((index, annot.xref))
            self._set_annotation(annot, color, comment)
        return added

    def modify_highlight(self, targets: list, color: str | None = None, comment: str | None = None,
                         delete: bool = False):
        page = None
        for index, xref in targets:
            if page is None or page.number != index:
                page = self.doc[index]
            annot = self._annotation(page, xref)
            if delete:
                page.delete_annot(annot)
            else:
                new_comment = comment if comment is not None and comment != annot.info["content"] else None
                new_color = color
                if color is not None:
                    rgb = annot.colors["stroke"]
                    if len(rgb) == 3 and color.lower() == "#" + "".join(f"{round(c * 255):02x}" for c in rgb):
                        new_color = None
                if new_color is not None or new_comment is not None:
                    self._set_annotation(annot, new_color, new_comment)

    def resize_highlight(self, index, xref, first, last):
        page = self.doc[index]
        annot = self._annotation(page, xref)
        lines = self.text(index)
        chosen = select({index: lines}, (index, *first), (index, *last), include_skipped=True)
        if not chosen.quads:
            return xref
        color, opacity, info, flags = annot.colors["stroke"], annot.opacity, annot.info, annot.flags
        blend = annot.blendmode
        new = page.add_highlight_annot([mu.Quad(*quad) for quad in chosen.quads[index]])
        if color:
            new.set_colors(stroke=color)
        new.set_info({key: value for key, value in info.items()
                      if key in ("content", "title", "subject", "creationDate", "modDate")})
        new.set_flags(flags)
        new.update(opacity=opacity, blend_mode=blend)
        new_xref = new.xref
        page.delete_annot(annot)
        return new_xref

    def highlight_text(self, index: int, xref: int):
        page = self.doc[index]
        annot = page.load_annot(xref)
        vertices = annot.vertices or []
        rectangles = [mu.Quad(*vertices[i:i + 4]).rect * page.rotation_matrix
                      for i in range(0, len(vertices) - 3, 4)]
        from .selection import clean_source

        raw = []
        for line in self.text(index):
            selected = []
            for glyph in line.glyphs:
                center = (glyph.box[0] + glyph.box[2]) / 2, (glyph.box[1] + glyph.box[3]) / 2
                if any(center in rectangle for rectangle in rectangles):
                    selected.append(glyph.text)
            if selected:
                raw.append("".join(selected))
        return clean_source("\n".join(raw))

    def _check_original(self):
        if _signature(self.path) != self.signature:
            raise ValueError("別のアプリで PDF が変更されました。名前を付けて保存してください。")

    def _backup(self, folder: Path, keep: int):
        if self.backed_up or not keep:
            return
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = folder / f"{stamp}-{self.path.name}"
        try:
            shutil.copy2(self.path, backup)
        except OSError:
            backup.unlink(missing_ok=True)
            raise
        self.backed_up = True
        for obsolete in sorted(folder.glob("????????-??????-??????-*.pdf"), reverse=True)[keep:]:
            obsolete.unlink(missing_ok=True)

    def _signature_needs_copy(self, incremental):
        ml = mu.mupdf
        root = ml.pdf_dict_gets(ml.pdf_trailer(self._pdf()), "Root")
        certification = ml.pdf_dict_gets(ml.pdf_dict_gets(root, "Perms"), "DocMDP")
        if not ml.pdf_is_null(certification):
            references = ml.pdf_dict_gets(certification, "Reference")
            for index in range(ml.pdf_array_len(references)):
                reference = ml.pdf_array_get(references, index)
                if ml.pdf_to_name(ml.pdf_dict_gets(reference, "TransformMethod")) == "DocMDP":
                    permission = ml.pdf_to_int(ml.pdf_dict_gets(ml.pdf_dict_gets(reference, "TransformParams"), "P"))
                    return not incremental or permission != 3
            return True
        if incremental:
            return False
        flags = self.doc.get_sigflags()
        return (flags >= 0 and bool(flags & 2)) or any(
            widget.is_signed for page in self.doc for widget in page.widgets(types=(mu.PDF_WIDGET_TYPE_SIGNATURE,)))

    def save(self, backup_directory: str, keep: int, destination: str | None = None,
             allow_signature_loss: bool = False):
        target = Path(destination).resolve() if destination else self.path
        overwrite = target == self.path
        step, _ = self.doc.journal_position()
        revision = self.history[step][0]
        if overwrite:
            if revision == self.saved_revision:
                return {"stamp": self.signature, "state": revision}
            self._check_original()
        incremental = self.doc.can_save_incrementally()
        if (overwrite or not allow_signature_loss) and self._signature_needs_copy(incremental):
            if overwrite:
                return {"signature_blocked": True}
            return {"confirm_signature_loss": True}
        if overwrite:
            self._backup(Path(backup_directory), keep)
        with NamedTemporaryFile(suffix=".pdf", dir=target.parent, delete=False) as handle:
            draft = Path(handle.name)
        try:
            self._write_draft(draft, incremental)
            self._replace_saved_file(draft, target, overwrite)
            if overwrite:
                self.signature = _signature(self.path)
                self.saved_revision = revision
            return {"stamp": self.signature, "state": revision}
        finally:
            draft.unlink(missing_ok=True)

    def _replace_saved_file(self, draft, target, overwrite):
        # A Windows file scanner can temporarily prevent atomic replacement.
        # Check the source again after each wait so a concurrent edit is safe.
        for delay in (0, .05, .1, .2, .4):
            if delay:
                sleep(delay)
            if overwrite:
                self._check_original()
            try:
                os.replace(draft, target)
                return
            except PermissionError as error:
                if getattr(error, "winerror", None) not in (5, 32, 33) or delay == .4:
                    raise

    def _write_draft(self, draft, incremental):
        # MuPDF may journal bookkeeping for xref streams during serialization.
        # Roll back that bookkeeping in the live document after writing the draft.
        self.doc.journal_start_op("Save PDF")
        try:
            if incremental:
                self.doc.save_snapshot(draft)
            else:
                # Skip PyMuPDF's pre-save catalog edits outside the annotation journal.
                options = mu.mupdf.PdfWriteOptions()
                options.do_encrypt = mu.PDF_ENCRYPT_KEEP
                options.dont_regenerate_id = 1
                mu.mupdf.pdf_save_document(self._pdf(), str(draft), options)
        finally:
            mu.mupdf.pdf_abandon_operation(self._pdf())

    def smoke(self, folder: str):
        root = Path(folder)
        source = root / "二段組の試験.pdf"
        with mu.open() as sample:
            for index in range(3):
                page = sample.new_page()
                page.insert_text((45, 35), "Journal Header")
                for line in range(5):
                    page.insert_text((45, 120 + line * 20), f"Left column {index + 1} line {line + 1}")
                    page.insert_text((340, 120 + line * 20), f"Right column {index + 1} line {line + 1}")
                page.insert_text((285, 810), str(index + 1))
            sample.save(source)
        self.open(str(source))
        pages = {index: self.text(index) for index in range(3)}
        assert all(page[0].kind for page in pages.values()), 'Running headers were not detected.'
        assert pages[0][5].text.startswith("Left column") and pages[0][6].text.startswith("Right column"), "Column order differs."
        selection = select(pages, (0, 1, 0), (2, 10, 18))
        assert set(selection.quads) == {0, 1, 2} and "Journal Header" not in selection.source, "Three-page selection differs."
        comment = "日本語の注釈"
        self.edit("add_highlight", (selection.quads, "#ffd400", comment))
        self.save(str(root / "backups"), 1)
        self.open(str(source))
        assert [item["comment"] for item in self.annotation_list()] == [comment] * 3, "Annotations did not survive reopen."
        assert len(tuple((root / "backups").glob("*.pdf"))) == 1, "The original PDF was not backed up."
        raster = self.render(0, 1.0, (0, 0, 500, 500))
        fixture = root / "文書内の参照.pdf"
        with mu.open() as sample:
            body = sample.new_page()
            body.insert_text((55, 90), "Figure 2.6 shows one result [1].")
            body.insert_text((55, 120), "The measurements are in Table I.")
            body.insert_text((55, 260), "Figure 2.6: A radar drawing.")
            refs = sample.new_page()
            refs.insert_text((55, 120), "Table I: Measurements.")
            refs.insert_text((55, 320), "References")
            refs.insert_text((55, 370), "[1] Smith, A. (2024). Radar measurements.")
            sample.save(fixture)
        self.open(str(fixture))
        for label, expected in (("Figure 2.6", "Figure 2.6:"), ("Table I", "Table I:"), ("[1]", "[1]")):
            rect = self.doc[0].search_for(label)[0]
            source = 0, (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
            marker = self.citation_at(*source)
            assert marker, f"The reference marker {label} was not detected."
            step = self.figure_step if marker.kind == "figure" else self.reference_step
            while not step():
                pass
            lookup = self.figure_targets if marker.kind == "figure" else self.reference_targets
            targets = lookup(source, marker.keys)
            assert (len(targets) == 1 and targets[0] and targets[0].label.startswith(expected)), label
        return raster


def worker(pipe, working_directory):
    engines = {None: PdfEngine(working_directory)}
    try:
        while True:
            request = pipe.recv()
            if request is None:
                break
            name, arguments = request
            try:
                engine = engines[None]
                if name == "document":
                    identifier, name, arguments = arguments
                    if name == "open":
                        engine = engines.setdefault(identifier, PdfEngine(working_directory))
                    else:
                        engine = engines[identifier]
                    if name == "close":
                        engines.pop(identifier)
                pipe.send((getattr(engine, name)(*arguments), None))
            except Exception as error:
                pipe.send((None, str(error) or type(error).__name__))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        for engine in engines.values():
            engine.close()
        pipe.close()
