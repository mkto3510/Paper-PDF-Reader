"""Exercise annotation handles and cross-page text in the reader."""

from multiprocessing import freeze_support
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pymupdf as mu
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_chrome_notes import wait
from paper_reader.application import Application
from paper_reader.pdf import _compose_accents
from paper_reader.selection import Glyph, Line, Selection, join_selections, nearest, reading_order
from paper_reader.translation import Job


def fixture(path):
    with mu.open() as pdf:
        for number in range(3):
            page = pdf.new_page(width=600, height=700)
            if number == 0:
                page.insert_text((60, 140), "The first editable highlight has extra text.")
                page.insert_text((60, 655), "The paper reports inter-")
            else:
                page.insert_text((60, 55), "national results for this study." if number == 1 else "Another page.")
                page.insert_text((60, 150), "The first editable highlight has extra text.")
                for y in (95, 108, 123, 138, 153, 168):
                    page.insert_text((325, y), "Right column, independent content.")
                if number == 2:
                    page.set_rotation(90)
            quad = page.search_for("editable highlight", quads=True)[0]
            note = page.add_highlight_annot(quad)
            note.set_colors(stroke=(.2, .6, .8))
            note.set_info(content=f"Existing comment {number}")
            note.update(opacity=.37)
        pdf.save(path)


def test_resize(pane, page):
    view, doc = pane.view, pane.document
    key = next(iter(doc.annotations[page]))
    pane.pick_note(key, jump=True, focus_card=True)
    view.load_text(page, lambda _: None)
    wait(lambda: page in view.text_cache, "The page text did not load")
    line = next(item for item in view.text_cache[page] if "first editable" in item.text)
    target = line.glyphs[line.text.index("has") + 2].box
    first, last = view.highlight_handles(page)
    start = view._project(page, last).toPoint()
    end = view._project(page, ((target[0] + target[2]) / 2, (target[1] + target[3]) / 2)).toPoint()
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end)
    assert view.highlight_drag and view.highlight_drag.preview
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    assert view.highlight_drag is None and doc.get_note(key)

    job = Job(None, None)
    doc.attach_job((key,), job)
    job.set_state("running")
    assert not view.highlight_handles(page)
    job.set_state("canceled")
    assert view.highlight_handles(page) == (first, last)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(view.viewport(), end)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
    wait(lambda: doc.get_note(key) is None and view.selected_note is not None and doc.get_note(view.selected_note),
         "The resized note was not selected")
    new = view.selected_note
    result = []
    doc.request("highlight_text", new, lambda value, error: result.append((value, error)))
    wait(lambda: result, "The resized note source did not load")
    assert result[0] == ("editable highlight has", None), result[0]
    assert doc.get_note(new)["comment"] == f"Existing comment {page}"
    assert doc.get_note(new)["color"] == "#3399cc"
    assert abs(doc.get_note(new)["opacity"] - .37) < .001


def main():
    app = QApplication([])
    def fragment(raw, starts=False, compounds=frozenset()):
        return Selection(raw, {}, {}, (), raw, compounds, starts)

    compound = fragment("element", compounds=frozenset({"finite-element"}))
    assert join_selections([fragment("finite-"), compound]).source == "finite-element"
    assert join_selections([fragment("inter\u00ad"), fragment("national")]).source == "international"
    paragraphs = [fragment("First paragraph."), fragment("Second paragraph.", True)]
    assert join_selections(paragraphs).source == "First paragraph.\n\nSecond paragraph."
    boxes = [(0, 0, 12, 16), (4, 0, 16, 16)]
    lines = tuple(Line((Glyph("x", box, ()),), box, 12, True, i) for i, box in enumerate(boxes))
    assert nearest(lines, 10, 8) == (1, 0)
    mark, letter = Glyph("´", (10, 1, 13, 5), ()), Glyph("e", (9, 4, 14, 12), ())
    assert [glyph.text for glyph in _compose_accents([mark, letter])] == ["é"]
    separate = Glyph("e", (19, 4, 24, 12), ())
    assert [glyph.text for glyph in _compose_accents([mark, separate])] == ["´", "e"]
    text_objects = [
        ("the range =", (20, 100, 97, 113)),
        (" and then", (160, 100, 240, 113)),
        ("i", (91, 101, 94, 114)),
        ("1,2,", (100, 101, 120, 114)),
        ("…", (121, 101, 135, 112)),
        (",6", (136, 101, 150, 114)),
        ("next line", (20, 113, 240, 126)),
    ]
    lines = tuple(Line((Glyph(text, box, ()),), box, 10, True, i)
                  for i, (text, box) in enumerate(text_objects))
    assert [line.text for line in reading_order(lines, 300)] == [
        "the range =", "i", "1,2,", "…", ",6", " and then", "next line"]
    with TemporaryDirectory() as folder:
        root = Path(folder)
        path = root / "ranges.pdf"
        fixture(path)
        control = Application(app, root)
        control.preferences.data["autoSave"] = False
        try:
            window = control.new_window()
            window.open_paths([str(path)])
            wait(lambda: window.pane and len(window.pane.document.annotations) == 3, "The PDF did not open")
            pane = window.pane
            doc, view = pane.document, pane.view
            for page in (0, 1):
                view.load_text(page, lambda _: None)
            wait(lambda: all(page in view.text_cache for page in (0, 1)), "The selection text did not load")
            first = next(i for i, line in enumerate(view.text_cache[0]) if line.text.endswith("inter-"))
            last, line = next((i, line) for i, line in enumerate(view.text_cache[1]) if line.text.startswith("national"))
            view.selection_range = (0, first, 0), (1, last, len(line.glyphs) - 1)
            choices = []
            view.resolve_selection(choices.extend)
            wait(lambda: choices, "The cross-page selection did not finish")
            sources = [choice.source for choice in choices]
            assert sources == ["The paper reports international results for this study."] * 2, sources
            view.clear_selection()
            test_resize(pane, 0)
            test_resize(pane, 1)
            window.rotate(90)
            test_resize(pane, 2)
            result = []
            doc.save(callback=result.append)
            wait(lambda: result, "The resized PDF was not saved")
            assert result == [True]
            with mu.open(path) as pdf:
                for number, page in enumerate(pdf):
                    note = page.first_annot
                    assert note.info["content"] == f"Existing comment {number}"
                    assert abs(note.opacity - .37) < .001
        finally:
            control.shutdown()
    print("PASS cross-page text, range editing, cancellation, translation lock, rotation and saved notes")


if __name__ == "__main__":
    freeze_support()
    main()
