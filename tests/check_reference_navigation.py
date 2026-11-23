"""Regression fixtures for figure/table routing, caption filtering and color actions."""

from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as mu
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject, DictionaryObject, NameObject, NumberObject, RectangleObject, TextStringObject,
)

from check_chrome_notes import wait
from paper_reader.application import Application
from paper_reader.captions import classify_captions, figure_mentions
from paper_reader.citations import ReferenceIndex
from paper_reader.notes import COLORS, add_color_actions
from paper_reader.pdf import PdfEngine
from paper_reader.selection import Glyph, Line, select


def fixture(folder):
    original, path = folder / "original.pdf", folder / "references.pdf"
    with mu.open() as pdf:
        for _ in range(3):
            pdf.new_page(width=612, height=780)
        page = pdf[0]
        page.insert_textbox(mu.Rect(55, 54, 500, 125),
                            "Figure 2.6 shows the layout of the radar.\nIts next line remains part of the description.\nThe description then continues.",
                            fontsize=12)
        page.insert_text((55, 156), "Fig. 1 [1] and Table I give details.")
        for phrase in ("Fig. 1", "Table I"):
            page.add_highlight_annot(page.search_for(phrase)[0])
        page.insert_text((55, 185), "Figs. 1, 2 and 3 are described in detail.")
        page.insert_text((55, 210), "Counts are discussed (Smith and Jones, 2024a).")
        page.insert_textbox(mu.Rect(55, 220, 545, 280), "See (García-López,\n2022) for a comparison.", fontsize=11)
        page.insert_text((55, 295), "The equation Eq. [1] is unrelated.")
        page.insert_text((55, 315), "See [1-3] for citations.")
        page.insert_text((55, 365), "Figure 1: The portable radio.")
        page.insert_text((55, 430), "Figure 2: Another independent example [1].")
        page.insert_text((55, 480), "Figure 2.6: A radar drawing.")
        page.insert_text((55, 512), "Figure 2.6 plots temperature across each experimental run.", fontsize=10)
        page.insert_text((55, 550), "A later line of ordinary body text.")
        names = page.search_for("Figure 2.6")[0]
        page = pdf[1]
        page.set_rotation(90)
        page.insert_text((55, 100), "Table I: Measured values.")
        page.insert_text((55, 245), "Table I (continued): Further measurements.")
        page.insert_text((55, 340), "Figure 3: Last example.")
        page = pdf[2]
        page.insert_text((55, 100), "[1] Alice Smith and Mark Jones. 2024a. Published results in Journal A.")
        page.insert_text((55, 125), "[2] García-López, T. (2022). Notes from a conference.")
        page.insert_text((55, 150), "[3] Chen, Z. (2020). Measurements from a laboratory.")
        pdf.save(original)

    writer = PdfWriter(clone_from=PdfReader(original))
    writer.add_named_destination("figure.2.6", 0)
    height = writer.pages[0].mediabox.height
    writer.add_annotation(0, DictionaryObject({
        NameObject("/Subtype"): NameObject("/Link"),
        NameObject("/Rect"): RectangleObject((names.x0, height - names.y1, names.x1, height - names.y0)),
        NameObject("/Dest"): TextStringObject("figure.2.6"),
    }))
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def click_point(pdf, text, number=0):
    rect = pdf[0].search_for(text)[number]
    return rect.tl + (rect.br - rect.tl) / 2


def check_engine(path):
    sample = next(figure_mentions("A comparison of Figs. 2, 4 and 6 with Table II."))
    assert sample[2] == (("figure", "2"), ("figure", "4"), ("figure", "6"))
    assert next(figure_mentions("Figs. 1-3"))[2] == (("figure", "1"), ("figure", "2"), ("figure", "3"))
    assert next(figure_mentions("Figure 1-3"))[2] == (("figure", "1-3"),)
    assert next(figure_mentions("Figures 2.1–2.3"))[2] == (("figure", "2.1"), ("figure", "2.2"), ("figure", "2.3"))
    assert [parts[2] for parts in figure_mentions("図２～４と第３図。")] == [
        (("figure", "2"), ("figure", "3"), ("figure", "4")), (("figure", "3"),)]
    engine = PdfEngine(path.parent)
    try:
        engine.open(str(path))
        with mu.open(path) as pdf:
            def marker(label, number=0):
                point = (0, *click_point(pdf, label, number))
                return point, engine.citation_at(*point)

            position, figure = marker("Fig. 1")
            assert figure.keys == (("figure", "1"),)
            assert marker("[1]")[1].keys == (1,)
            assert marker("[1]", 1)[1] is None
            assert marker("[1]", 2)[1].keys == (1,)
            assert marker("García-López")[1].keys == (("garcía-lópez", "2022"),)
            assert marker("Smith and Jones, 2024a")[1].keys == (("smith", "2024a", "jones"),)
            links = engine.page_links(0)
            assert links and links[0]["page"] == 0
            assert engine.named_figure_target((0, *click_point(pdf, "Figure 2.6")), 0).label.startswith("Figure 2.6:")
            while not engine.figure_step():
                pass
            assert engine.figure_targets(position, figure.keys)[0].label.startswith("Figure 1:")
            position, table = marker("Table I")
            entry, = engine.figure_targets(position, table.keys)
            assert entry.page == 1 and entry.label.startswith("Table I:")
            while not engine.reference_step():
                pass
            position, reference = marker("Smith and Jones, 2024a")
            assert engine.reference_targets(position, reference.keys)[0].label.startswith("[1]")
        lines = engine.text(0)
        first = next(i for i, line in enumerate(lines) if line.text.startswith("Figure 2.6 shows"))
        last = next(i for i, line in enumerate(lines) if line.text.startswith("A later line"))
        result = select({0: lines}, (0, first, 0), (0, last, len(lines[last].glyphs) - 1), origin="")
        assert "Figure 2.6 shows" in result.source and "Its next line" in result.source
        assert "Figure 2.6 plots" in result.source
        assert any("A radar drawing" in x.text for x in result.exclusions)
    finally:
        engine.close()


def check_named_coordinates(folder):
    path = folder / "named-cropped.pdf"
    with mu.open() as pdf:
        pdf.new_page(width=600, height=800)
        pdf.new_page(width=600, height=800)
        pdf.save(path)
    writer = PdfWriter(clone_from=path)
    writer.pages[1].cropbox = RectangleObject((40, 30, 550, 730))
    writer.pages[1].rotate(90)
    destination = ArrayObject([writer.pages[1].indirect_reference, NameObject("/XYZ"),
                               NumberObject(109), NumberObject(702), NumberObject(0)])
    fit = ArrayObject([writer.pages[1].indirect_reference, NameObject("/Fit")])
    zero = ArrayObject([writer.pages[1].indirect_reference, NameObject("/XYZ"),
                        NumberObject(0), NumberObject(0), NumberObject(0)])
    writer.add_named_destination_array(TextStringObject("coordinate"), writer._add_object(destination))
    writer.add_named_destination_array(TextStringObject("fit"), writer._add_object(fit))
    writer.add_named_destination_array(TextStringObject("zero"), writer._add_object(zero))
    for rect, target in (([20, 650, 100, 680], TextStringObject("coordinate")),
                         ([20, 600, 100, 630], destination),
                         ([20, 550, 100, 580], fit),
                         ([20, 500, 100, 530], TextStringObject("fit")),
                         ([20, 450, 100, 480], TextStringObject("zero"))):
        writer.add_annotation(0, DictionaryObject({
            NameObject("/Subtype"): NameObject("/Link"),
            NameObject("/Rect"): RectangleObject(rect),
            NameObject("/Dest"): target,
        }))
    with path.open("wb") as stream:
        writer.write(stream)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        named, direct, direct_fit, named_fit, named_zero = engine.page_links(0)
        assert named["point"] == direct["point"] == (672, 69)
        assert direct_fit.get("page_only") and named_fit.get("page_only")
        assert not named_zero.get("page_only") and named_zero["point"] != (0, 0)
    finally:
        engine.close()


def check_unheaded_bibliography():
    def row(text, x, y, paragraph):
        box = (x, y, x + 150, y + 10)
        return Line((Glyph(text, box, ()),), box, 10, True, paragraph), box

    early = ReferenceIndex(12)
    tail = ReferenceIndex(12)
    references = [row("Acknowledgments", 60, 120, 0),
                  row("[1] Kim, B. (2024). First.", 60, 140, 1),
                  row("[2] A short title.", 60, 160, 2),
                  row("[3] Wiley and Cooper.", 60, 180, 3),
                  row("Journal of Testing.", 74, 191, 3)]
    for page in range(11, -1, -1):
        early.add_page(page, references if page == 6 else [row("Ordinary body text", 60, 120, 0)])
        tail.add_page(page, references if page == 11 else [row("Ordinary body text", 60, 120, 0)])
    assert not early.targets((1, 90, 90), (1,))
    assert len(tail.targets((1, 90, 90), (1, 2, 3))) == 3

    venue_style = ReferenceIndex(1)
    venue_style.add_page(0, [row("References", 60, 120, 0),
                             row("de Vries, A., and Smith, B.: Events in 1950 and", 60, 140, 1),
                             row("their impact on measurement, Proceedings of", 82, 152, 1),
                             row("the International Conference on Research,", 82, 164, 1),
                             row("third edition, 20–32, https://example.com/2023,", 82, 176, 1),
                             row("2024.", 82, 188, 1),
                             row("Jones, C. (2020). A separate reference.", 60, 210, 2)])
    assert not venue_style.targets((0, 80, 80), (("de vries", "1950"),))
    target, = venue_style.targets((0, 80, 80), (("de vries", "2024", "smith"),))
    assert target.label.startswith("de Vries, A.")


def check_two_column_table():
    def row(text, x, y, width, paragraph):
        box = (x, y, x + width, y + 10)
        return Line((Glyph(text, box, ()),), box, 10, True, paragraph)

    lines = [
        row("Other prose", 60, 115, 190, 0),
        row("Table A.2 Measurements by instrument and reference", 60, 164, 240, 1),
        row("observation interval", 87, 177, 100, 7),
        row("Instrument", 67, 193, 61, 2),
        row("Interval", 175, 193, 43, 3),
        row("Instrument B", 67, 221, 60, 4),
        row("6 days", 175, 221, 31, 5),
    ]
    rules = [(start, y, stop, y) for y in (191, 214, 242)
             for start, stop in ((62, 154), (154, 300))]
    groups, caption_rows, _ = classify_captions(lines, (lines,), ([], rules, [], [], False), 600, 700)
    caption, = groups
    assert (caption.kind, caption.label) == ("table", "a.2")
    assert {id(line) for line in lines[1:3]} == caption_rows and caption.object_box


def scan_steps(engine, name="reference_step", budget_ms=None):
    step, count = getattr(engine, name), 1
    while not step(budget_ms):
        count += 1
    return count


def check_scan_order(folder):
    """The reference scan stops early, and figures near a mention resolve without the full scan."""
    def build(name, pages, writer):
        path = folder / name
        with mu.open() as pdf:
            for _ in range(pages):
                pdf.new_page(width=612, height=780)
            writer(pdf)
            pdf.save(path)
        return path

    def bibliography(first, entries=4):
        def write(pdf):
            pdf[0].insert_text((55, 100), "A comparison [2] with the earlier results.")
            for index in range(1, len(pdf) - 1):
                pdf[index].insert_text((55, 100), "Ordinary body text without any structure.")
            pdf[first].insert_text((55, 100), "References")
            for number in range(1, entries + 1):
                pdf[min(first + number // 3, len(pdf) - 1)].insert_text(
                    (55, 130 + number * 25), f"[{number}] Author{number}, A. (2020). Title number {number}. Journal of Tests.")
        return write

    # A heading with entries ends the scan a page before the heading, long before page 0.
    engine = PdfEngine(folder)
    try:
        engine.open(str(build("short.pdf", 12, bibliography(10))))
        assert scan_steps(engine) <= 4 and engine.references.complete
        point = (0, *click_point_in(engine, 0, "[2]"))
        citation = engine.citation_at(*point)
        assert engine.reference_targets(point, citation.keys)[0].label.startswith("[2]")
        # A time budget reads several pages per call and reaches the same result.
        engine.close()
        engine.open(str(build("short.pdf", 12, bibliography(10))))
        assert scan_steps(engine, budget_ms=500) == 1
        assert engine.reference_targets(point, citation.keys)[0].label.startswith("[2]")

        # A heading without entries behind it keeps the scan going to the first page.
        engine.close()
        engine.open(str(build("empty.pdf", 8, lambda pdf: pdf[7].insert_text((55, 100), "References"))))
        assert scan_steps(engine) == 8

        # A long document may hold a bibliography per chapter, so all its pages are read.
        engine.close()
        engine.open(str(build("long.pdf", 64, bibliography(62, 6))))
        assert scan_steps(engine) == 64
        assert len(engine.references.regions) == 1
    finally:
        engine.close()

    # Japanese journals print the heading with a gap between its characters, each stored as a separate line.
    def spaced(pdf):
        pdf[0].insert_text((55, 100), "A comparison [2] with the earlier results.")
        pdf[3].insert_text((300, 100), "文", fontname="japan")
        pdf[3].insert_text((345, 100), "献", fontname="japan")
        for number in range(1, 5):
            pdf[3].insert_text((55, 130 + number * 25),
                               f"[{number}] Author{number}, A. (2020). Title number {number}. Journal of Tests.")
    engine = PdfEngine(folder)
    try:
        engine.open(str(build("spaced.pdf", 4, spaced)))
        assert scan_steps(engine) <= 3
        regions = engine.references.regions
        assert len(regions) == 1 and regions[0][0][2] == "文 献" and len(regions[0][2]) == 4, regions
    finally:
        engine.close()

    # Older journals number entries with a bare numeral. Ordinary lines starting with a number stay prose.
    def bare(pdf):
        pdf[0].insert_text((55, 100), "A comparison [3] with the earlier results.")
        pdf[3].insert_text((55, 100), "References")
        for number in range(1, 6):
            pdf[3].insert_text((55, 110 + number * 25),
                               f"{number} A. Author{number}, Title number {number}. Journal of Tests, 7 (1970) 12.")
        pdf[3].insert_text((55, 300), "76 (1971) 7888 continues the previous entry.")
    engine = PdfEngine(folder)
    try:
        engine.open(str(build("bare.pdf", 4, bare)))
        scan_steps(engine)
        point = (0, *click_point_in(engine, 0, "[3]"))
        keys = engine.citation_at(*point).keys
        target, = engine.reference_targets(point, keys)
        assert keys == (3,) and target.label.startswith("3 A. Author3"), target
    finally:
        engine.close()

    def figures(pdf):
        pdf[1].insert_text((55, 100), "See Figure 7 for details.")
        pdf[2].insert_text((55, 300), "Figure 7: A nearby example.")
        pdf[4].insert_text((55, 100), "See Figure 8 for two cases.")
        pdf[3].insert_text((55, 300), "Figure 8: The first case.")
        pdf[5].insert_text((55, 300), "Figure 8: The second case.")
        pdf[0].insert_text((55, 100), "See Figure 9 for a distant example.")
        pdf[11].insert_text((55, 300), "Figure 9: A distant example.")

    engine = PdfEngine(folder)
    try:
        engine.open(str(build("figures.pdf", 12, figures)))
        for page, label, expected in ((1, "Figure 7", "Figure 7:"), (4, "Figure 8", None), (0, "Figure 9", None)):
            point = (page, *click_point_in(engine, page, label))
            citation = engine.citation_at(*point)
            target, = engine.figure_nearby(point, citation.keys)
            assert (target.label.startswith(expected) and target.page == 2) if expected else target is None, label
        assert engine.figure_page == 0, "The nearby lookup read the whole document"
    finally:
        engine.close()


def click_point_in(engine, page, text):
    with mu.open(engine.temp) as pdf:
        rect = pdf[page].search_for(text)[0]
    return tuple(rect.tl + (rect.br - rect.tl) / 2)


def check_window(app, folder, path):
    menu = QMenu()
    chosen = []
    add_color_actions(menu, lambda jp, en: en, chosen.append)
    assert len(menu.actions()) == len(COLORS)
    assert all(not a.isCheckable() and not a.icon().isNull() and a.text() for a in menu.actions())
    menu.actions()[3].trigger()
    assert chosen == [tuple(COLORS)[3]]

    control = Application(app, folder)
    control.preferences.data.update(language="en", autoSave=False)
    window = control.new_window()
    try:
        window.open_paths([str(path)])
        wait(lambda: window.pane and window.pane.view.pages, "PDF did not open")
        pane = window.pane
        view = pane.view
        view.set_zoom("fit")
        view.set_layout(continuous=False)
        view.go_to(0)
        wait(lambda: len(pane.document.annotations.get(0, {})) == 2, "Figure/table highlights did not load")
        with mu.open(path) as pdf:
            point = view._project(0, tuple(click_point(pdf, "Fig. 1"))).toPoint()
            table = view._project(0, tuple(click_point(pdf, "Table I"))).toPoint()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
        wait(lambda: view.navigation_target is not None, "Figure was not outlined")
        assert view.navigation_target.label.startswith("Figure 1:")
        assert pane.history_index > 0 and view.current_page() == 0
        pane.back_forward(-1)
        assert not view.navigation_target and view.current_page() == 0
        pane.back_forward(1)
        assert view.navigation_target.label.startswith("Figure 1:")
        pane.back_forward(-1)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=table)
        wait(lambda: view.current_page() == 1 and view.navigation_target is not None,
             "Highlighted table reference did not reach its caption")
        assert view.navigation_target.label.startswith("Table I:") and view.selected_note is None
    finally:
        control.shutdown()


if __name__ == "__main__":
    freeze_support()
    app = QApplication([])
    with TemporaryDirectory() as directory:
        folder = Path(directory)
        path = fixture(folder)
        check_engine(path)
        check_named_coordinates(folder)
        check_unheaded_bibliography()
        check_two_column_table()
        check_scan_order(folder)
        check_window(app, folder, path)
    print("PASS figure/table jumps, named links, captions, citation overlap, and color actions")
