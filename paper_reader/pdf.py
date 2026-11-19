"""MuPDF operations. A single child process owns the open document."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile

import pymupdf as mu

from .selection import Glyph, Line, bounds, classify, edge_band, furniture_key, reading_order


def _signature(path: Path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


class PdfEngine:
    def __init__(self):
        self.path: Path | None = None
        self.doc = None
        self.temp: Path | None = None
        self.password = ""
        self.signature = None
        self.backed_up = False
        self.text_cache: OrderedDict = OrderedDict()
        self.edge_cache: dict = {}
        self.dirty = False

    def open(self, filename: str, password: str = ""):
        path = Path(filename).resolve(strict=True)
        doc = mu.open(path)
        try:
            if not doc.is_pdf or not doc.page_count:
                raise ValueError("ページのある PDF を指定してください。")
            if doc.needs_pass and not doc.authenticate(password):
                doc.close()
                return {"password": True}
            signature = _signature(path)
            pages = [tuple(doc[index].rect)[2:] for index in range(doc.page_count)]
            data = {"pages": pages, "outline": doc.get_toc(), "name": path.name}
        except Exception:
            doc.close()
            raise
        self.close()
        self.path, self.doc, self.password, self.signature = path, doc, password, signature
        self.backed_up = self.dirty = False
        self.text_cache.clear()
        self.edge_cache.clear()
        return data

    def close(self):
        if self.doc is not None:
            self.doc.close()
            self.doc = None
        if self.temp is not None:
            self.temp.unlink(missing_ok=True)
            self.temp = None

    def render(self, page: int, scale: float, tile: tuple[int, int, int, int]):
        pixmap = self.doc[page].get_pixmap(
            matrix=mu.Matrix(scale, scale),
            clip=mu.Rect(*(edge / scale for edge in tile)),
            colorspace=mu.csRGB, alpha=False,
        )
        return pixmap.width, pixmap.height, pixmap.stride, pixmap.x, pixmap.y, pixmap.samples

    def _read_lines(self, index: int):
        page = self.doc[index]
        text = page.get_text("rawdict", flags=mu.TEXTFLAGS_RAWDICT & ~mu.TEXT_PRESERVE_IMAGES)
        lines: list[Line] = []
        for block_index, block in enumerate(text["blocks"]):
            for raw in block.get("lines", ()):
                glyphs = []
                sizes = []
                for span in raw["spans"]:
                    for char in span["chars"]:
                        glyphs.append(Glyph(
                            char["c"], tuple(char["bbox"]),
                            tuple(tuple(point) for point in mu.recover_char_quad(raw["dir"], span, char)),
                        ))
                        sizes.append(span["size"])
                if glyphs and any(glyph.text.strip() for glyph in glyphs):
                    horizontal = raw["wmode"] == 0 and abs(raw["dir"][1]) < 0.15
                    lines.append(Line(tuple(glyphs), bounds(g.box for g in glyphs),
                                      sum(sizes) / len(sizes), horizontal, block_index))
        return lines

    def _edge_keys(self, index: int):
        if index not in self.edge_cache:
            page = self.doc[index]
            height = page.rect.width if page.rotation in (90, 270) else page.rect.height
            keys = set()
            # A block can include several lines, so preserve line-level keys.
            for block in page.get_text("dict", flags=mu.TEXTFLAGS_DICT & ~mu.TEXT_PRESERVE_IMAGES)["blocks"]:
                for line in block.get("lines", ()):
                    _, top, _, bottom = line["bbox"]
                    side = -1 if bottom <= height * 0.12 else (1 if top >= height * 0.88 else 0)
                    if side:
                        keys.add((side, furniture_key("".join(span["text"] for span in line["spans"]))))
            self.edge_cache[index] = keys
        return self.edge_cache[index]

    def text(self, index: int):
        if index in self.text_cache:
            self.text_cache.move_to_end(index)
            return self.text_cache[index]
        page = self.doc[index]
        width, height = page.rect.width, page.rect.height
        if page.rotation in (90, 270):
            width, height = height, width
        lines = self._read_lines(index)
        edge_keys = {(edge_band(line, height), furniture_key(line.text))
                     for line in lines if edge_band(line, height)}
        repeated = set()
        if edge_keys:
            for neighbour in range(max(0, index - 8), min(self.doc.page_count, index + 9)):
                if neighbour != index:
                    repeated.update(edge_keys.intersection(self._edge_keys(neighbour)))
        classified = classify(tuple(lines), width, height, repeated)
        headers, body, footers = [], [], []
        for line in classified:
            if line.kind == "ヘッダー・フッター・ページ番号":
                (headers if edge_band(line, height) < 0 else footers).append(line)
            else:
                body.append(line)
        result = (*sorted(headers, key=lambda line: line.box[1]), *reading_order(body, width),
                  *sorted(footers, key=lambda line: line.box[1]))
        if page.rotation:
            matrix = page.rotation_matrix
            result = tuple(replace(
                line, box=tuple(mu.Rect(line.box) * matrix),
                glyphs=tuple(replace(glyph, box=tuple(mu.Rect(glyph.box) * matrix)) for glyph in line.glyphs),
            ) for line in result)
        self.text_cache[index] = result
        if len(self.text_cache) > 12:
            self.text_cache.popitem(last=False)
        return result

    def annotation_list(self, index: int | None = None):
        indices = range(self.doc.page_count) if index is None else (index,)
        result = []
        for page_index in indices:
            page = self.doc[page_index]
            for annot in page.annots(types=(mu.PDF_ANNOT_HIGHLIGHT,)):
                color = annot.colors["stroke"]
                if len(color) == 1:
                    color = color * 3
                if len(color) != 3:
                    color = (1, 0.83, 0)
                hex_color = "#" + "".join(f"{round(255 * component):02x}" for component in color)
                result.append({"page": page_index, "xref": annot.xref, "comment": annot.info["content"],
                               "color": hex_color, "box": tuple(annot.rect * page.rotation_matrix)})
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

    def _own_file(self, path: Path):
        doc = mu.open(path)
        if doc.needs_pass and not doc.authenticate(self.password):
            doc.close()
            raise ValueError("保存した PDF を開き直せませんでした。")
        return doc

    def save(self, backup_directory: str, keep: int, destination: str | None = None):
        target = Path(destination).resolve() if destination else self.path
        overwrite = target == self.path
        if overwrite:
            if not self.dirty:
                return
            self._check_original()
            self._backup(Path(backup_directory), keep)
            self._check_original()
            if self.temp is None and self.doc.can_save_incrementally():
                self.doc.saveIncr()
                self.signature = _signature(self.path)
                self.dirty = False
                return
        with NamedTemporaryFile(suffix=".pdf", dir=target.parent, delete=False) as handle:
            draft = Path(handle.name)
        try:
            self.doc.save(draft, encryption=mu.PDF_ENCRYPT_KEEP)
            if not overwrite:
                os.replace(draft, target)
                return
            self._check_original()
            self.doc.close()
            try:
                self._check_original()
                os.replace(draft, target)
            except (OSError, ValueError):
                prior = self.temp
                self.doc = self._own_file(draft)
                self.temp = draft
                if prior is not None:
                    prior.unlink(missing_ok=True)
                raise
            prior, self.temp = self.temp, None
            self.doc = self._own_file(target)
            if prior is not None:
                prior.unlink(missing_ok=True)
            self.signature = _signature(self.path)
            self.dirty = False
        finally:
            if draft != self.temp:
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


def worker(pipe):
    engine = PdfEngine()
    try:
        while True:
            name, arguments = pipe.recv()
            try:
                pipe.send((getattr(engine, name)(*arguments), None))
            except Exception as error:
                pipe.send((None, str(error) or type(error).__name__))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        engine.close()
        pipe.close()
