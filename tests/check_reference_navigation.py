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
from paper_reader.pdf_analysis import PdfAnalysis
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
        assert engine.analysis.figure_page == 0, "The nearby lookup read the whole document"
    finally:
        engine.close()


def check_superscript_citations(folder):
    """Numbers printed as superscripts act as citations. Exponents and unit powers do not."""
    path = folder / "superscript.pdf"
    rows = (  # (baseline, text, raised number, expected keys)
        (100, "The result was confirmed", "3", (3,)),
        (130, "Other work agrees,", "1,2", (1, 2)),
        (160, "The area in km", "2", None),
        (190, "The scale is 10", "3", None),
        (220, "A later point", "4-6", (4, 5, 6)),
        (250, "A rate in cm/year", "2", None),
        (280, "Taro Yamada", "1", None),
    )
    with mu.open() as pdf:
        for _ in range(2):
            pdf.new_page(width=612, height=780)
        for y, text, note, _ in rows:
            pdf[0].insert_text((55, y), text, fontsize=11)
            pdf[0].insert_text((56 + mu.get_text_length(text, fontsize=11), y - 5), note, fontsize=7)
        pdf[1].insert_text((55, 100), "References")
        for number in range(1, 8):
            pdf[1].insert_text((55, 110 + number * 25),
                               f"{number}. A. Author{number}, Title number {number}. Journal of Tests, 7 (1970) 12.")
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        scan_steps(engine)
        for y, text, note, expected in rows:
            point = (0, 58 + mu.get_text_length(text, fontsize=11), y - 7)
            citation = engine.citation_at(*point)
            assert (citation.keys if citation else None) == expected, (text, note, citation)
        point = (0, 58 + mu.get_text_length(rows[0][1], fontsize=11), 93)
        target, = engine.reference_targets(point, (3,))
        assert target.label.startswith("3. A. Author3"), target
    finally:
        engine.close()


def check_bracket_author_year(folder):
    """AGU journals cite as "Hess [1965]" and "[Hanks and Kanamori, 1979]" and number paragraphs "[1]"."""
    path = folder / "agu.pdf"
    with mu.open() as pdf:
        for _ in range(2):
            pdf.new_page(width=612, height=780)
        pdf[0].insert_text((55, 100), "[1] The introduction begins here with a plain sentence.")
        pdf[0].insert_text((55, 130), "Hess [1965] mapped the ridge, as did others.")
        pdf[0].insert_text((55, 160), "Moment is defined elsewhere [Hanks and Kanamori, 1979].")
        pdf[0].insert_text((55, 190), "See also reference [7] for another view.")
        pdf[0].insert_text((55, 220), "The crystal direction [001] is the easy axis.")
        pdf[0].insert_text((55, 250), "As in the friction law (1979) and the event (December 1987) here.")
        pdf[1].insert_text((55, 100), "References")
        pdf[1].insert_text((55, 130), "Hess, H. H. (1965), Mapping the oceanic ridge, Journal of Tests, 12, 1-9.")
        pdf[1].insert_text((55, 160), "Hanks, T. C., and H. Kanamori (1979), A moment magnitude scale, Journal of Tests, 84, 2348.")
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        scan_steps(engine)
        expected = (("[1] The", "[1]", None), ("Hess [1965", "[1965]", (("hess", "1965"),)),
                    ("Kanamori, 1979", "[Hanks", (("hanks", "1979", "kanamori"),)), ("[7]", "[7]", (7,)),
                    ("[001]", "[001]", None), ("law", "law", None), ("December", "December", None))
        for text, search, keys in expected:
            point = (0, *click_point_in(engine, 0, search))
            citation = engine.citation_at(*point)
            assert (citation.keys if citation else None) == keys, (text, citation)
        point = (0, *click_point_in(engine, 0, "[1965]"))
        target, = engine.reference_targets(point, (("hess", "1965"),))
        assert target.label.startswith("Hess, H. H."), target
    finally:
        engine.close()


def check_split_caption(folder):
    """A label on a line of its own ("Figure 1.") with the title beside it still captions the figure above."""
    path = folder / "split_caption.pdf"
    swatch = mu.Pixmap(mu.csRGB, mu.IRect(0, 0, 40, 40))
    swatch.clear_with(180)
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        page.insert_image(mu.Rect(88, 60, 288, 248), pixmap=swatch)
        page.insert_image(mu.Rect(207, 150, 288, 247), pixmap=swatch)  # a panel inside the figure
        page.insert_text((61, 276), "Figure 1.", fontsize=10)
        page.insert_text((110.1, 275.9), "Tectonic setting of the fault", fontsize=10)
        page.insert_text((61, 288), "zone. Topographic relief map is shown.", fontsize=10)
        page.insert_text((61, 400), "Ordinary body text follows the caption here.", fontsize=10)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        rows, _ = engine.analysis._caption_rows(0)
        assert len(rows) == 1, rows
        text = rows[0][0].text
        assert text.startswith("Figure 1.") and "Tectonic setting" in text and "Topographic relief" in text, text
    finally:
        engine.close()


def check_scanned_caption(folder):
    """A scanned page has no figure object; a smaller or bold "Fig. 2. Title" opening a block is still a caption."""
    path = folder / "scanned_caption.pdf"
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        for row in range(6):
            page.insert_text((61, 70 + row * 12), f"Running prose of the article, line {row}, goes on across the column here.",
                             fontsize=10)
        page.insert_text((120, 200), "Depth (km)", fontsize=7)
        page.insert_text((130, 243), "0   50   100", fontsize=7)
        page.insert_text((61, 250), "Fig. 2. The effects of imposing a boundary at depth.", fontsize=8)
        page.insert_text((61, 262), "Curve (a) shows the solution.", fontsize=8)
        for row in range(4):
            page.insert_text((61, 300 + row * 12), f"More prose, line {row}, continues the argument in full sentences.",
                             fontsize=10)
        # In running text the same words at body size, inside a paragraph, are not a caption.
        page.insert_text((61, 380), "as discussed before, the model reproduces the data and", fontsize=10)
        page.insert_text((61, 392), "Fig. 3. Is mentioned in passing at the body size here.", fontsize=10)
        page.insert_text((61, 404), "so the argument continues without any figure nearby.", fontsize=10)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        rows, _ = engine.analysis._caption_rows(0)
        labels = sorted(row[0].label for row in rows)
        assert labels == ["2"], labels
    finally:
        engine.close()


def check_paragraphs(folder):
    """Paragraphs: a heading stands alone, a paragraph continues into the next column, an indented line after a
    finished sentence starts a new one, and a lone symbol is not a paragraph."""
    path = folder / "paragraphs.pdf"
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        page.insert_text((56, 80), "1. Introduction", fontsize=11, fontname="hebo")
        for row in range(20):
            page.insert_text((56, 100 + row * 12), f"Running text of the first paragraph, line {row}, goes on across the column", fontsize=10)
        right = [(320, "continues at the top of the right column and the sentence"),
                 (320, "then ends here. It is finished."),
                 (334, "A new paragraph begins after the indent. Its second line"),
                 (320, "wraps around the column as running text does.")]
        for row, (x, text) in enumerate(right):
            page.insert_text((x, 100 + row * 12), text, fontsize=10)
        page.insert_text((300, 60), "X", fontsize=10)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        texts = [item.text for item in engine.paragraphs()]
    finally:
        engine.close()
    assert "1. Introduction" in texts, texts
    joined = [text for text in texts if text.startswith("Running text")]
    assert len(joined) == 1 and "line 19" in joined[0] and joined[0].endswith("It is finished."), texts
    assert any(text.startswith("A new paragraph begins") and text.endswith("does.") for text in texts), texts
    assert "X" not in texts, texts


def check_paragraph_edges(folder):
    """A bold line ending in a colon is a heading, numbered items and bibliography entries are separate paragraphs,
    and the entries of a bibliography are marked as references."""
    path = folder / "paragraph_edges.pdf"
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        page.insert_text((56, 80), "Acknowledgments:", fontsize=10, fontname="hebo")
        page.insert_text((56, 94), "This study was supported by a grant from the national research fund.", fontsize=10)
        page.insert_text((56, 106), "We thank the reviewers for their careful reading of the manuscript.", fontsize=10)
        page.insert_text((56, 150), "References", fontsize=11, fontname="hebo")
        entries = [("Dieterich JH, Kilgore B. 1996a. Implications of fault constitutive", "properties for earthquake prediction. Proc. Natl. Acad. Sci. 93:3787"),
                   ("Doser DI, Kanamori H. 1986. Depth of seismicity in the Imperial", "Valley region. J. Geophys. Res. 91:675"),
                   ("Edmond JM, Paterson MS. 1972. Volume changes during the deformation", "of rocks at high pressures. Int. J. Rock Mech. 9:161")]
        for row, (first, second) in enumerate(entries):
            page.insert_text((56, 170 + row * 30), first, fontsize=9)
            page.insert_text((68, 181 + row * 30), second, fontsize=9)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        found = engine.paragraphs()
    finally:
        engine.close()
    texts = [(item.kind, item.text) for item in found]
    assert ("body", "Acknowledgments:") in texts, texts
    assert any(kind == "body" and text.startswith("This study was supported") and text.endswith("manuscript.")
               for kind, text in texts), texts
    references = [text for kind, text in texts if kind == "reference"]
    assert len(references) == 3 and references[1].startswith("Doser DI") and references[1].endswith("675"), references


def check_selection_paragraphs(folder):
    """A selection splits paragraphs where a paragraph starts, judged against the usual line gap of the page: wide
    line spacing (Japanese typesetting) does not split a paragraph, an indented line after a finished sentence
    starts a new one, and a page whose every line is marked bold (OCR text) is not read as a list of headings."""
    from paper_reader.selection import select

    path = folder / "selection_paragraphs.pdf"
    with mu.open() as pdf:
        wide = pdf.new_page(width=612, height=792)
        for row in range(8):
            wide.insert_text((56, 100 + row * 24), f"First paragraph, line {row}, runs on without any indent"
                             + ("." if row == 7 else ""), fontsize=10)
        wide.insert_text((70, 100 + 8 * 24), "Second paragraph begins with an indent after a finished sentence.", fontsize=10)
        for row in range(1, 4):
            wide.insert_text((56, 100 + (8 + row) * 24), f"Second paragraph, line {row}, runs on without any indent", fontsize=10)
        bold = pdf.new_page(width=612, height=792)
        for row in range(10):
            bold.insert_text((56, 100 + row * 12), f"Every line {row} of this page is set in bold type as OCR text is marked",
                             fontsize=10, fontname="hebo")
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        for number, expected in ((0, 2), (1, 1)):
            lines = engine.text(number)
            source = select({number: lines}, (number, 0, 0), (number, len(lines) - 1, len(lines[-1].glyphs) - 1),
                            origin="").source
            parts = source.split("\n\n")
            assert len(parts) == expected, (number, len(parts), parts)
        assert parts[0].count("every line") == 0 and len(engine.text(0)) == 12
        first, second = select({0: engine.text(0)}, (0, 0, 0), (0, 11, 5), origin="").source.split("\n\n")
        assert first.startswith("First paragraph") and second.startswith("Second paragraph begins"), (first, second)
    finally:
        engine.close()


def check_old_settings(folder):
    """A settings file saved before the "front" rule existed loads with that rule on and keeps the other choices."""
    import json

    from paper_reader.preferences import load_preferences

    path = folder / "old_settings.json"
    path.write_text(json.dumps({"language": "ja", "rules": {"furniture": True, "caption": False, "edge_band": 10}}),
                    encoding="utf-8")
    rules = load_preferences(path)["rules"]
    assert rules["front"] is True and rules["caption"] is False and rules["edge_band"] == 10, rules


def check_paragraph_pitfalls(folder):
    """Lines that only look like the start of a paragraph do not split one: a line that begins with a word such as
    "results,", a number followed by a multiplication sign or a decimal point, or a name with an initial; and the
    subscript of a chemical formula that sits on a line of its own."""
    from paper_reader.selection import select

    path = folder / "paragraph_pitfalls.pdf"
    rows = ["The advantage of this experiment is that it provides a calibrated phase and the",
            "results, therefore, show coupling effects between magnitude and phase. Indeed",
            "96 × 95/2 = 4560 possible pairs, starting from 96 acquisitions in one day",
            "0.91. When applied from the origin to the point, the lower crustal slope will",
            "produce a deviation, and we would like to thank S. Coulson,",
            "R. Cravey, B. Rosich Tell, N. Walker, and many other people at the institute",
            "for their support of the work."]
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        for row, text in enumerate(rows):
            page.insert_text((56, 100 + row * 12), text, fontsize=10)
        formula = pdf.new_page(width=612, height=792)
        formula.insert_text((56, 100), "where Q is the production rate of H", fontsize=10)
        formula.insert_text((205, 103), "2", fontsize=7)
        formula.insert_text((210, 100), "O at the comet, r is the distance from the", fontsize=10)
        formula.insert_text((56, 112), "center of the comet, and v is the gas velocity of the outflow.", fontsize=10)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        for number in (0, 1):
            lines = engine.text(number)
            source = select({number: lines}, (number, 0, 0), (number, len(lines) - 1, len(lines[-1].glyphs) - 1),
                            origin="").source
            assert "\n\n" not in source, (number, source.split("\n\n"))
    finally:
        engine.close()


def check_symbol_lines():
    """A line of math-font glyphs with no word is a piece of an equation; prose with a symbol in it stays body."""
    from paper_reader.analysis_rules import DEFAULT_RULES, classify, symbols_only
    from paper_reader.selection import Glyph, Line, reading_regions

    assert symbols_only("\x04x1−y1\x052\x03") and symbols_only("log\x04r1\x03x3−y3\x05")
    assert symbols_only("i\x08\x08\x08") and not symbols_only("x\x04")
    assert not symbols_only("(\x0412 MPa K\x031). Aside from the important differences in the")
    assert not symbols_only("例えば久保ほか2017はラプラス事前分布を用いた")

    def line(text, y, x=56, width=250, block=0):
        box = (x, y, x + width, y + 10)
        return Line((Glyph(text, box, ()),), box, 10.0, True, block)

    texts = [(f"Running prose of the article line {row} goes on across the whole column here", 56, 250)
             for row in range(5)]
    texts += [("\x04x1−y1\x052\x03\x04x2−y2\x05", 120, 90), ("\x03 \x01 \x02", 130, 20)]
    texts += [(f"More running prose of the article line {row} goes on across the whole column", 56, 250)
              for row in range(5)]
    lines = [line(text, 100 + number * 12, x, width, number) for number, (text, x, width) in enumerate(texts)]
    result = classify(lines, 612, 792, set(), ([], [], [], [], False), DEFAULT_RULES, reading_regions(lines, 612))
    kinds = [item.kind for item in result]
    assert kinds[5] == kinds[6] == "equation", kinds
    assert all(kind == "" for number, kind in enumerate(kinds) if number not in (5, 6)), kinds


def check_figure_text():
    """Numbers that follow each other above a caption and do not share a left edge are the tick labels of a figure; a
    paragraph, a short list of words and a short list of numbers that share a left edge stay body text, and so do
    short lines with words (they are running text more often than labels)."""
    from paper_reader.analysis_rules import DEFAULT_RULES, classify
    from paper_reader.selection import Glyph, Line, reading_regions

    def line(text, x, y, width, block):
        box = (x, y, x + width, y + 10)
        return Line((Glyph(text, box, ()),), box, 10.0, True, block)

    prose = [line(f"Running prose of the article line {row} goes on across the whole column here", 56, 60 + row * 12,
                  300, row) for row in range(5)]
    ticks = [("0", 90), ("50", 130), ("100", 200), ("150", 140), ("200", 220), ("250", 170)]
    words = [("Moho", 95), ("crust", 135), ("mantle", 205), ("slab", 145), ("trench", 225), ("arc", 175)]
    figure = [line(text, x, 140 + row * 14, 30, 10 + row) for row, (text, x) in enumerate(ticks)]
    labels = [line(text, x, 230 + row * 14, 30, 16 + row) for row, (text, x) in enumerate(words)]
    caption = [line("Figure 3. Cross section of the model with the crust and the mantle drawn to scale", 56, 330, 300, 22)]
    aligned_numbers = ["13", "40", "7", "92", "55", "21"]
    aligned = [line(text, 56, 400 + row * 12, 20, 30 + row) for row, text in enumerate(aligned_numbers)]
    more = [line(f"More running prose of the article line {row} goes on across the whole column", 56, 480 + row * 12,
                 300, 40 + row) for row in range(5)]
    lines = [*prose, *figure, *labels, *caption, *aligned, *more]
    result = classify(lines, 612, 792, set(), ([], [], [], [], False), DEFAULT_RULES, reading_regions(lines, 612))
    kinds = {item.text: item.kind for item in result}
    assert all(kinds[text] == "figure" for text, _ in ticks), kinds
    assert all(kinds[text] == "" for text, _ in words), kinds
    assert all(kinds[text] == "" for text in aligned_numbers), kinds
    assert all(item.kind == "" for item in result if item.text.startswith(("Running", "More"))), kinds


def check_front_matter():
    """On a first page, the author line, the affiliations, the contact line and the dates above the abstract are front
    matter; the title and the abstract are not. Without a line that opens the text, nothing is marked."""
    from paper_reader.analysis_rules import DEFAULT_RULES, classify
    from paper_reader.selection import Glyph, Line, reading_regions

    def line(text, y, size=10.0, block=0, width=300):
        box = (56, y, 56 + width, y + size)
        return Line((Glyph(text, box, ()),), box, size, True, block)

    def kinds(texts, first_page=True):
        lines = [line(text, 60 + number * 16, 18.0 if number == 0 else 10.0, number) for number, text in enumerate(texts)]
        body = [line(f"The body of the article goes on here in running sentences, line {row}.", 400 + row * 12,
                     block=100 + row) for row in range(8)]
        lines += body
        result = classify(lines, 612, 792, set(), ([], [], [], [], False), DEFAULT_RULES, reading_regions(lines, 612),
                          first_page=first_page)
        return [item.kind for item in result[:len(texts)]]

    head = ["Maximum hurricane intensity preceded by increase in lightning",
            "Colin Price1*, Mustafa Asfur2 and Yoav Yair2",
            "Department of Geophysics, Tel Aviv University, Tel Aviv 69978, Israel",
            "Received 12 March 2015; accepted 3 June 2015",
            "E-mail: price@example.org",
            "Abstract",
            "Hurricanes are the most deadly storms of the Earth and cause devastation every year."]
    assert kinds(head) == ["", "front", "front", "front", "front", "", ""], kinds(head)
    assert kinds(head, first_page=False) == [""] * 7
    no_start = [head[0], head[1], head[2], head[3], head[6]]
    assert kinds(no_start) == [""] * 5, kinds(no_start)
    japanese = ["北海道駒ヶ岳火山の噴火履歴", "勝井義雄・篠沢達也・知本康男", "北海道大学大学院理学研究院", "はじめに",
                "北海道には現在18の活火山が存在し，中でも雌阿寒岳，十勝岳が知られている。"]
    assert kinds(japanese) == ["", "front", "front", "", ""], kinds(japanese)
    spaced = ["Equilibrium condensation of a solar composition gas", "Glenn J. MacPherson a,*, Michail I. Petaev b",
              "a Department of Mineral Sciences, National Museum of Natural History", "A B S T R A C T",
              "New full equilibrium condensation calculations are presented in this abstract."]
    assert kinds(spaced) == ["", "front", "front", "", ""], kinds(spaced)

    # The notes at the foot of a first page, and a sentence that only starts with the same word.
    def notes(texts):
        body = [line(f"The body of the article goes on here in running sentences, line {row}.", 100 + row * 12,
                     block=100 + row) for row in range(8)]
        foot = [line(text, 680 + number * 9, 8.0, 200 + number, 240) for number, text in enumerate(texts)]
        lines = body + foot
        result = classify(lines, 612, 792, set(), ([], [], [], [], False), DEFAULT_RULES, reading_regions(lines, 612),
                          first_page=True)
        return [item.kind for item in result[len(body):]]

    assert notes(["Manuscript received March 6, 2015; revised May 18, 2015; accepted June", "5, 2015.",
                  "The authors are with the German Aerospace Center (DLR), Wessling 82230,", "Germany (e-mail: a@b.de)."])         == ["front"] * 4
    assert notes(["Accepted solutions were then compared with the data of the survey."]) == [""]


def check_heading_variants(folder):
    """Letter-spaced and "cited" headings open a bibliography."""
    for number, heading in enumerate(("R E F E R E N C E S", "REFERENCES CITED", "References")):
        path = folder / f"heading{number}.pdf"
        with mu.open() as pdf:
            first = pdf.new_page(width=612, height=792)
            first.insert_text((61, 100), "Body text cites Smith (1990) and Lee [2001] in a sentence.", fontsize=10)
            last = pdf.new_page(width=612, height=792)
            last.insert_text((61, 100), heading, fontsize=9)
            for row, line in enumerate(("Smith, A. B., 1990. A study of things. J. Tests 3, 1-5.",
                                        "Lee, C. D., 2001. Another study of things. J. Tests 4, 6-9.",
                                        "Kim, E. F., 1999. A third study of things. J. Tests 5, 10-14.")):
                last.insert_text((61, 120 + row * 14), line, fontsize=9)
            pdf.save(path)
        engine = PdfEngine(folder)
        try:
            engine.open(str(path))
            while not engine.reference_step():
                pass
            assert [len(region[2]) for region in engine.references.regions] == [3], (heading, engine.references.regions)
        finally:
            engine.close()


def check_label_close_below_figure_text(folder):
    """A label alone on a line is a caption when a sentence follows it, even with figure text right above it."""
    path = folder / "label_below_text.pdf"
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        for row in range(6):
            page.insert_text((56, 70 + row * 12), f"Running prose of the article, line {row}, goes on across the column.", fontsize=10)
        page.insert_text((120, 300), "FeO (wt %)", fontsize=8)
        page.insert_text((56, 312), "Figure 7.", fontsize=9, fontname="hebo")
        page.insert_text((56, 323), "Concentrations of FeO versus Th for different groups of lunar material.", fontsize=9)
        page.insert_text((56, 400), "Sentences as in Figure 7. then go on in the running text of the article.", fontsize=10)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        rows, _ = engine.analysis._caption_rows(0)
        assert [row[0].label for row in rows] == ["7"], rows
        assert rows[0][0].text.startswith("Figure 7.") and "Concentrations" in rows[0][0].text, rows[0][0].text
    finally:
        engine.close()


def check_label_on_band_row(folder):
    """A label printed a little higher than the full-width caption text beside it stays on that row."""
    path = folder / "band_row.pdf"
    swatch = mu.Pixmap(mu.csRGB, mu.IRect(0, 0, 40, 40))
    swatch.clear_with(180)
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        for row in range(8):
            page.insert_text((56, 520 + row * 12), f"Left column prose line {row} of the article text here.", fontsize=10)
            page.insert_text((320, 520 + row * 12), f"Right column prose line {row} of the article text.", fontsize=10)
        page.insert_image(mu.Rect(120, 60, 500, 290), pixmap=swatch)
        page.insert_text((56, 316), "Figure 2.", fontsize=11)
        page.insert_text((150, 316), "Antiplane displacement due to anelastic strain in a rectangular shear zone, black", fontsize=10)
        page.insert_text((56, 328), "dashed box, shown across the full width of the page for this test only.", fontsize=10)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        rows, _ = engine.analysis._caption_rows(0)
        assert [row[0].label for row in rows] == ["2"], rows
    finally:
        engine.close()


def check_page_layout(folder):
    """The scans share one reading of a page; changing the band rule discards it."""
    path = folder / "layout.pdf"
    with mu.open() as pdf:
        page = pdf.new_page(width=612, height=792)
        page.insert_text((61, 40), "Running head of the journal", fontsize=8)
        page.insert_text((61, 200), "Figure 1. A caption of some length for the figure.", fontsize=10)
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        layout = engine.analysis._page_layout(0)
        assert engine.analysis._page_layout(0) is layout
        assert layout.regions is layout.regions and layout.reading_order
        assert any(key[1].startswith("running head") for key in layout.edges), layout.edges
        engine.update_rules({"edge_band": 3})
        assert engine.analysis._page_layout(0) is not layout
        assert not engine.analysis._page_layout(0).edges
    finally:
        engine.close()


def check_spurious_bibliography(folder):
    """A "References" line in a contents list must not hide the captions of every later page."""
    path = folder / "spurious.pdf"
    swatch = mu.Pixmap(mu.csRGB, mu.IRect(0, 0, 40, 40))
    swatch.clear_with(180)
    with mu.open() as pdf:
        for _ in range(4):
            pdf.new_page(width=612, height=792)
        pdf[0].insert_text((61, 100), "Contents", fontsize=14)
        pdf[0].insert_text((61, 130), "References", fontsize=10)
        for number in (1, 2):
            page = pdf[number]
            page.insert_image(mu.Rect(88, 60, 288, 248), pixmap=swatch)
            page.insert_text((61, 270), f"Figure {number}. A caption for figure {number} with a title.", fontsize=10)
        pdf[3].insert_text((61, 100), "References", fontsize=12)
        for number in range(1, 5):
            pdf[3].insert_text((61, 110 + number * 14), f"{number}. A. Author, Title {number}. J Tests 3:1-5 (1990).")
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        while not engine.figure_step():
            pass
        found = sorted(entry[0].label for entries in engine.figure_pages.values() for entry in entries)
        assert found == ["1", "2"], found
    finally:
        engine.close()


def check_year_lists():
    """One author with several years ("1990a,b", "1990, 1992", "1990; 1992") names every year."""
    from paper_reader.citations import citation_spans

    def keys(text):
        return [citation.keys for _, citation in citation_spans(text)]

    assert keys("(Smith, 1990a, b)") == [(("smith", "1990a"), ("smith", "1990b"))]
    assert keys("(Smith, 1990a,b; Lee, 2001)") == [(("smith", "1990a"), ("smith", "1990b"), ("lee", "2001"))]
    assert keys("Smith (1990, 1992)") == [(("smith", "1990"), ("smith", "1992"))]
    assert keys("(Smith et al., 1990, 1992)") == [(("smith", "1990", "*"), ("smith", "1992", "*"))]
    assert keys("(Smith 1990; 1992)") == [(("smith", "1990"), ("smith", "1992"))]
    assert keys("Smith and Lee (1990a,b,c)")[0] == tuple(("smith", f"1990{x}", "lee") for x in "abc")
    # A page number after the year is not another year.
    assert keys("(Smith, 1990, p. 5)") == [(("smith", "1990"),)]
    assert keys("(Smith, 1990, 25-30)") == [(("smith", "1990"),)]
    assert keys("(2010; 2011)") == []


def check_prefixed_labels():
    """Extended data and supplementary figures are separate from the figures of the article."""
    from unicodedata import normalize

    from paper_reader.captions import caption_heading, figure_label, figure_mentions

    def keys(text):
        return [item[2] for item in figure_mentions(text)]

    assert keys("see Extended Data Fig. 3 and Fig. 3a") == [(("figure", "ed3"),), (("figure", "3a"),)]
    assert keys("in Supplementary Figs. 3 and 4 only") == [(("figure", "s3"), ("figure", "s4"))]
    assert keys("Supplementary Table 2 lists") == [(("table", "s2"),)]
    assert keys("Fig. S3 shows") == [(("figure", "s3"),)]
    assert keys("Supplementary Fig. S3 shows") == [(("figure", "s3"),)]
    heading = caption_heading(normalize("NFKC", "Extended Data Fig. 1 | MT data misfit analysis."))
    assert figure_label(heading) == ("figure", "ed1"), figure_label(heading)
    # Plates, schemes and photographs print their own numbers; Abbildung and Tabelle are figure and table.
    assert keys("shown on Plate 1 and Fig. 1") == [(("figure", "pl1"),), (("figure", "1"),)]
    assert keys("Plates 1 and 2") == [(("figure", "pl1"), ("figure", "pl2"))]
    assert keys("Scheme 2 shows") == [(("figure", "sc2"),)]
    assert keys("写真 3 に示す") == [(("figure", "ph3"),)]
    assert keys("Abbildung 4 und Tabelle 2") == [(("figure", "4"),), (("table", "2"),)]
    assert keys("the tectonic plate 3 moves, plate d, scheme i") == []
    assert figure_label(caption_heading("Plate 1. (a) Surface expressions")) == ("figure", "pl1")
    assert caption_heading("plate 3 moves north") is None


def check_panel_mentions():
    """"Fig. 3b" goes to the caption of figure 3, even when that caption does not list its panels."""
    from paper_reader.captions import Caption
    from paper_reader.citations import Reference

    def entry(label, text, page):
        box = (0, 0, 1, 1)
        return Caption("figure", label, text, box, None, False), Reference(page, box, text)

    entries = [entry("3", "Figure 3. Displacement fields.", 2), entry("4", "Figure 4. (a) Map. (b) Section.", 3),
               entry("5", "Figure 5. First case.", 4), entry("5", "Figure 5. Second case (b).", 5)]
    found = PdfAnalysis._figure_targets((("figure", "3b"), ("figure", "4b"), ("figure", "5b"), ("figure", "5c"),
                                       ("figure", "9a")), entries)
    assert [target.page if target else None for target in found] == [2, 3, 5, None, None], found


def check_orphan_tail(folder):
    """A line such as "439:3225-38", the tail of an entry that started elsewhere, must not start an entry."""
    path = folder / "tail.pdf"
    with mu.open() as pdf:
        for _ in range(2):
            pdf.new_page(width=612, height=780)
        pdf[0].insert_text((55, 100), "As shown before (Lavie et al. 2017) the effect is clear.")
        pdf[1].insert_text((55, 100), "References")
        pdf[1].insert_text((55, 130), "Smith J, Jones K. 1990. A first paper on tests. J Tests 3:1-5.")
        pdf[1].insert_text((55, 142), "439:3225-38")
        pdf[1].insert_text((55, 154), "Lavie B, Ehrenreich D, Bourrier V. 2017. A second paper on tests. J Tests 8:1-9.")
        pdf[1].insert_text((55, 166), "Brown A, Green C. 2001. A third paper on tests. J Tests 9:2-4.")
        pdf[1].insert_text((55, 178), "Nozette S, et al., The mission to the Moon, J. Tests, 99, 1835-1850, 1994.")
        pdf.save(path)
    engine = PdfEngine(folder)
    try:
        engine.open(str(path))
        scan_steps(engine)
        point = (0, *click_point_in(engine, 0, "Lavie"))
        citation = engine.citation_at(*point)
        target, = engine.reference_targets(point, citation.keys)
        assert target.label.startswith("Lavie B"), target
        years = {entry[3].label[:6]: entry[2] for region in engine.references.regions for entry in region[2]}
        assert years["Nozett"] == "1994", years  # not the page number 1835
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
        check_superscript_citations(folder)
        check_bracket_author_year(folder)
        check_orphan_tail(folder)
        check_split_caption(folder)
        check_scanned_caption(folder)
        check_label_on_band_row(folder)
        check_label_close_below_figure_text(folder)
        check_heading_variants(folder)
        check_paragraphs(folder)
        check_paragraph_edges(folder)
        check_selection_paragraphs(folder)
        check_paragraph_pitfalls(folder)
        check_old_settings(folder)
        check_symbol_lines()
        check_figure_text()
        check_front_matter()
        check_page_layout(folder)
        check_year_lists()
        check_prefixed_labels()
        check_panel_mentions()
        check_spurious_bibliography(folder)
        check_window(app, folder, path)
    print("PASS figure/table jumps, named links, captions, citation overlap, and color actions")
