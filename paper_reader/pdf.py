"""MuPDF operations. A single child process owns the open document."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile
import unicodedata

import pymupdf as mu

from .analysis_rules import DEFAULT_RULES, classify, page_evidence
from .artifacts import artifact_glyphs
from .search import find_page
from .selection import Glyph, Line, bounds, edge_band, furniture_key, reading_regions, select


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


def _signature(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


class PdfEngine:
    def __init__(self, working_directory=None):
        self.working_directory = working_directory
        self.path: Path | None = None
        self.doc = None
        self.temp: Path | None = None
        self.password = ""
        self.signature = None
        self.backed_up = False
        self.text_cache: OrderedDict = OrderedDict()
        self.edge_cache = {}
        self.display_lists = OrderedDict()
        self.options = DEFAULT_RULES.copy()
        self.dirty = False

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
            self.close()
            self.path, self.doc, self.password, self.signature = path, doc, password, stamp
            self.temp = snapshot
            self.backed_up = self.dirty = False
            self.text_cache.clear()
            self.edge_cache.clear()
            return {"pages": pages, "outline": outline, "name": path.name, "signature": stamp}
        finally:
            if self.temp != snapshot:
                if doc is not None:
                    doc.close()
                snapshot.unlink(missing_ok=True)

    def close(self):
        self.display_lists.clear()
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
        self.edge_cache.clear()

    def content_bounds(self, index):
        page = self.doc[index]
        entries = [tuple(mu.Rect(rect) * page.rotation_matrix) for kind, rect in page.get_bboxlog()
                   if kind in ("fill-text", "stroke-text", "fill-image", "stroke-path") and not mu.Rect(rect).is_empty]
        crop = mu.Rect(bounds(entries)) & page.rect if entries else page.rect
        return tuple(crop)

    def page_links(self, index):
        result = []
        for link in self.doc[index].get_links():
            if link["kind"] == mu.LINK_GOTO and isinstance(link.get("to"), (mu.Point, tuple, list)):
                result.append({"box": tuple(link["from"]), "page": link["page"], "point": tuple(link["to"])})
            if link["kind"] == mu.LINK_URI:
                result.append({"box": tuple(link["from"]), "uri": link.get("uri", "")})
        return result

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

    def _read_lines(self, index: int, mark_artifacts=False):
        page = self.doc[index]
        text = page.get_text("rawdict", flags=mu.TEXTFLAGS_RAWDICT & ~mu.TEXT_PRESERVE_IMAGES)
        artifacts = artifact_glyphs(page) if mark_artifacts else set()
        lines: list[Line] = []
        for block_index, block in enumerate(text["blocks"]):
            for raw in block.get("lines", ()):
                glyphs = []
                sizes = []
                for span in raw["spans"]:
                    for char in span["chars"]:
                        x, y = char["origin"]
                        glyphs.append(Glyph(
                            char["c"], tuple(char["bbox"]),
                            tuple(tuple(point) for point in mu.recover_char_quad(raw["dir"], span, char)),
                            (round(x, 1), round(y, 1), char["c"]) in artifacts,
                        ))
                        sizes.append(span["size"])
                if glyphs and any(glyph.text.strip() for glyph in glyphs):
                    horizontal = raw["wmode"] == 0 and abs(raw["dir"][1]) < 0.15
                    composed = _compose_accents(glyphs) if horizontal else tuple(glyphs)
                    lines.append(Line(composed, bounds(g.box for g in composed),
                                      sum(sizes) / len(sizes), horizontal, block_index))
        return lines

    def _edge_keys(self, index):
        if index not in self.edge_cache:
            page = self.doc[index]
            height = page.rect.width if page.rotation in (90, 270) else page.rect.height
            limit = self.options["edge_band"] / 100
            keys = set()
            for block in page.get_text("dict", flags=mu.TEXTFLAGS_DICT & ~mu.TEXT_PRESERVE_IMAGES)["blocks"]:
                for line in block.get("lines", ()):
                    _, top, _, bottom = line["bbox"]
                    side = -1 if bottom <= height * limit else (1 if top >= height * (1 - limit) else 0)
                    if side:
                        keys.add((side, furniture_key("".join(span["text"] for span in line["spans"])), top / height))
            self.edge_cache[index] = keys
        return self.edge_cache[index]

    def _display_lines(self, lines, page):
        if not page.rotation:
            return tuple(lines)
        matrix = page.rotation_matrix
        return tuple(replace(line, box=tuple(mu.Rect(line.box) * matrix),
                             glyphs=tuple(replace(g, box=tuple(mu.Rect(g.box) * matrix)) for g in line.glyphs))
                     for line in lines)

    def text(self, index):
        if index in self.text_cache:
            self.text_cache.move_to_end(index)
            return self.text_cache[index]
        page = self.doc[index]
        width, height = page.rect.width, page.rect.height
        if page.rotation in (90, 270):
            width, height = height, width
        lines = self._read_lines(index, self.options.get("artifact", True))
        band = self.options["edge_band"] / 100
        neighbours = set()
        if any(edge_band(line, height, band) for line in lines):
            for near in range(max(0, index - 3), min(self.doc.page_count, index + 4)):
                if near != index:
                    neighbours.update(self._edge_keys(near))
        regions = reading_regions(lines, width)
        ordered = tuple(line for region in regions for line in region)
        classified = classify(ordered, width, height, neighbours, page_evidence(page), self.options, regions)
        leading, content, trailing = [], [], []
        horizontal_body = sum(len(line.glyphs) for line in classified if line.horizontal) > sum(
            len(line.glyphs) for line in classified if not line.horizontal)
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
        self.text_cache[index] = result
        if len(self.text_cache) > 12:
            self.text_cache.popitem(last=False)
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

    def add_highlight(self, quads: dict, color: str, comment: str):
        added = []
        try:
            for index, points in quads.items():
                page = self.doc[index]
                annot = page.add_highlight_annot([mu.Quad(*quad) for quad in points])
                added.append((index, annot.xref))
                self._set_annotation(annot, color, comment)
        except Exception:
            for index, xref in added:
                page = self.doc[index]
                annot = page.load_annot(xref)
                if annot is not None:
                    page.delete_annot(annot)
            raise
        self.dirty = bool(added) or self.dirty
        return added

    def modify_highlight(self, targets: list, color: str | None = None, comment: str | None = None,
                         delete: bool = False):
        for index, xref in targets:
            page = self.doc[index]
            annot = page.load_annot(xref)
            if delete:
                page.delete_annot(annot)
            else:
                self._set_annotation(annot, color, comment)
            self.dirty = True

    def resize_highlight(self, index, xref, first, last):
        page = self.doc[index]
        annot = page.load_annot(xref)
        if annot is None:
            raise ValueError("注釈が見つかりません。")
        lines = self.text(index)
        chosen = select({index: lines}, (index, *first), (index, *last), include_skipped=True)
        if not chosen.quads:
            return xref
        color, opacity, info, flags = annot.colors["stroke"], annot.opacity, annot.info, annot.flags
        blend = annot.blendmode
        new = page.add_highlight_annot([mu.Quad(*quad) for quad in chosen.quads[index]])
        try:
            if color:
                new.set_colors(stroke=color)
            new.set_info({key: value for key, value in info.items()
                          if key in ("content", "title", "subject", "creationDate", "modDate")})
            new.set_flags(flags)
            new.update(opacity=opacity, blend_mode=blend)
        except Exception:
            page.delete_annot(new)
            raise
        new_xref = new.xref
        page.delete_annot(annot)
        self.dirty = True
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

    def save(self, backup_directory: str, keep: int, destination: str | None = None):
        target = Path(destination).resolve() if destination else self.path
        overwrite = target == self.path
        if overwrite:
            if not self.dirty:
                return self.signature
            self._check_original()
            self._backup(Path(backup_directory), keep)
        with NamedTemporaryFile(suffix=".pdf", dir=target.parent, delete=False) as handle:
            draft = Path(handle.name)
        try:
            self.doc.save(draft, encryption=mu.PDF_ENCRYPT_KEEP)
            if overwrite:
                self._check_original()
            os.replace(draft, target)
            if overwrite:
                self.signature = _signature(self.path)
                self.dirty = False
                return self.signature
        finally:
            draft.unlink(missing_ok=True)

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
        from .selection import select

        self.open(str(source))
        pages = {index: self.text(index) for index in range(3)}
        assert all(page[0].kind for page in pages.values()), 'Running headers were not detected.'
        assert pages[0][5].text.startswith("Left column") and pages[0][6].text.startswith("Right column"), "Column order differs."
        selection = select(pages, (0, 1, 0), (2, 10, 18))
        assert set(selection.quads) == {0, 1, 2} and "Journal Header" not in selection.source, "Three-page selection differs."
        comment = "日本語の注釈"
        self.add_highlight(selection.quads, "#ffd400", comment)
        self.save(str(root / "backups"), 1)
        self.open(str(source))
        assert [item["comment"] for item in self.annotation_list()] == [comment] * 3, "Annotations did not survive reopen."
        assert len(tuple((root / "backups").glob("*.pdf"))) == 1, "The original PDF was not backed up."
        return self.render(0, 1.0, (0, 0, 500, 500))


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
