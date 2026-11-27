"""Exercise annotation handles and cross-page text in the reader."""

from multiprocessing import freeze_support
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pymupdf as mu
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_chrome_notes import wait
from paper_reader.application import Application
from paper_reader.pdf_analysis import _compose_accents
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
        image_page = pdf.new_page(width=600, height=700)
        image = mu.Pixmap(mu.csRGB, mu.IRect(0, 0, 16, 16), False)
        image.clear_with(180)
        image_page.insert_image(mu.Rect(60, 60, 180, 180), pixmap=image)
        japanese = pdf.new_page(width=600, height=700)
        writer = mu.TextWriter(japanese.rect)
        writer.append((60, 120), "カタカナ テスト", font=mu.Font("cjk"))
        writer.write_text(japanese)
        first = pdf[0]
        first.insert_link({"kind": mu.LINK_GOTO, "from": first.search_for("first")[0],
                           "page": 1, "to": mu.Point(60, 0)})
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

    doc.move_history()
    wait(lambda: doc.get_note(key) and not doc.get_note(new), "Undo did not restore the original range")
    doc.move_history(redo=True)
    wait(lambda: doc.get_note(new) and not doc.get_note(key), "Redo did not restore the new range")


def test_click_selection(pane):
    view, doc = pane.view, pane.document
    button = Qt.MouseButton.LeftButton

    def text_point(page, word):
        view.go_to(page)
        view.load_text(page, lambda _: None)
        wait(lambda: page in view.text_cache, "Click-selection text did not load")
        line = next(line for line in view.text_cache[page] if word in line.text)
        box = line.glyphs[line.text.index(word)].box
        return view._project(page, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)).toPoint(), line.text.strip()

    def click(point, count):
        view.clear_selection()
        QTest.mouseClick(view.viewport(), button, pos=point)
        QTest.mouseDClick(view.viewport(), button, pos=point)
        QTest.mouseRelease(view.viewport(), button, pos=point)
        if count == 3:
            QTest.mouseClick(view.viewport(), button, pos=point)

    def selected():
        choices = []
        wait(lambda: view.selection_range, "Click selection did not set a range")
        view.resolve_selection(choices.extend)
        wait(lambda: choices, "Click selection did not return text")
        return choices[1].source

    before, _ = text_point(0, "The")
    after, _ = text_point(0, "has")
    point, line = text_point(0, "first")
    view.clear_selection()
    QTest.mouseClick(view.viewport(), button, pos=point)
    QTest.mouseDClick(view.viewport(), button, pos=point)
    assert view.selection_range, "The word was not selected on the second press"
    first_word = view.selection_range
    QTest.mouseMove(view.viewport(), after)
    wait(lambda: view.selection_range[1] > first_word[1], "Word drag did not extend")
    QTest.mouseMove(view.viewport(), before)
    wait(lambda: view.selection_range[0] < first_word[0] and view.selection_range[1] == first_word[1],
         "Word drag did not reverse")
    QTest.mouseRelease(view.viewport(), button, pos=point)
    assert selected() == "first"
    QTest.qWait(QApplication.doubleClickInterval() + 25)
    assert view.current_page() == 0, "A double click opened a link"
    QTest.mouseClick(view.viewport(), button, pos=point)
    QTest.mouseDClick(view.viewport(), button, pos=point)
    QTest.mouseRelease(view.viewport(), button, pos=point)
    QTest.mousePress(view.viewport(), button, pos=point)
    assert selected() == line, "The line was not selected on the third press"
    QTest.mouseRelease(view.viewport(), button, pos=point)

    choices = []

    def choose_color():
        menu = QApplication.activePopupWidget()
        colors = next(item.menu() for item in menu.actions() if item.text() == pane.t("ハイライトを作成", "Create highlight"))
        choices.extend(action.text() for action in colors.actions())
        action = next(action for action in colors.actions() if action.text() == pane.t("青", "Blue"))
        menu.close()
        action.trigger()

    QTimer.singleShot(0, choose_color)
    pane.context_menu(view.viewport().mapToGlobal(point))
    wait(lambda: any(note["color"] == "#2ea8e5" for note in doc.annotations[0].values()), "Blue highlight was not created")
    assert len(choices) == 8 and pane.last_color == "#2ea8e5"
    doc.move_history()
    wait(lambda: len(doc.annotations[0]) == 1, "The created highlight was not undone")

    point, _ = text_point(4, "カタカナ")
    click(point, 2)
    assert selected() == "カタカナ"
    point, line = text_point(2, "Right column")
    click(point, 3)
    assert selected() == line
    view.go_to(3)
    click(view._project(3, (100, 100)).toPoint(), 3)
    wait(lambda: 3 in view.text_cache, "Image-only page was not processed")
    assert view.selection_range is None

    view.set_layout(continuous=True)
    old_mode, old_zoom = view.zoom_mode, view.zoom
    view.set_zoom("custom", .8)
    view.restore((0, 100, 685))
    for page in (0, 1):
        view.load_text(page, lambda _lines: None)
    wait(lambda: all(page in view.text_cache for page in (0, 1)), "Cross-page selection text did not load")

    def projected(page, word):
        item = next(line for line in view.text_cache[page] if word in line.text)
        box = item.glyphs[item.text.index(word)].box
        return view._project(page, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)).toPoint()

    start, end = projected(0, "reports"), projected(1, "results")
    assert view.viewport().rect().contains(start) and view.viewport().rect().contains(end)
    view.clear_selection()
    QTest.mouseClick(view.viewport(), button, pos=start)
    QTest.mouseDClick(view.viewport(), button, pos=start)
    QTest.mouseMove(view.viewport(), end)
    wait(lambda: view.selection_range[1][0] == 1, "Word drag did not cross the page boundary")
    QTest.mouseRelease(view.viewport(), button, pos=end)
    assert selected().startswith("reports") and selected().endswith("results")

    view.set_zoom(old_mode, old_zoom if old_mode == "custom" else None)
    point, _ = text_point(0, "first")
    after, _ = text_point(0, "has")
    request, delayed = doc.request, []

    def intercept(name, args, callback, **options):
        if name == "text" and args == (0,):
            delayed.append((name, args, callback, options))
        else:
            request(name, args, callback, **options)

    view.clear_selection()
    view.text_cache.pop(0)
    with patch.object(doc, "request", side_effect=intercept):
        QTest.mouseClick(view.viewport(), button, pos=point)
        QTest.mouseDClick(view.viewport(), button, pos=point)
        QTest.mouseMove(view.viewport(), after)
        QTest.mouseRelease(view.viewport(), button, pos=after)
        assert view.selection_range is None and len(delayed) == 1
    name, args, callback, options = delayed.pop()
    request(name, args, callback, **options)
    assert selected() == "first editable highlight has"

    view.clear_selection()
    QTest.mouseClick(view.viewport(), button, pos=point)
    wait(lambda: view.current_page() == 1, "A single click did not follow the link")


def test_shared_history(window):
    pane = window.pane
    doc = pane.document
    keys = []
    quads = {page: [pane.view.text_cache[page][0].glyphs[0].quad] for page in (0, 1)}
    doc.add_highlight(quads, "#2ea8e5", "Grouped note", on_success=keys.extend)
    wait(lambda: len(keys) == 2 and all(doc.get_note(key) for key in keys), "The grouped note did not load")
    window.duplicate(False)
    wait(lambda: window.pane is not pane, "The duplicate tab did not open")
    sibling = window.pane
    assert sibling.document is doc
    doc.modify(keys, delete=True)
    wait(lambda: not any(doc.get_note(key) for key in keys), "The grouped note was not deleted")
    saved = []
    doc.save(callback=saved.append)
    wait(lambda: saved, "Deleting annotations could not be saved")
    assert saved == [True] and not doc.dirty

    window.activateWindow()
    sibling.view.setFocus()
    wait(window.isActiveWindow, "The reader window did not become active")
    assert window.actions["undo"][0].isEnabled() and sibling.view.hasFocus()
    QTest.keyClick(sibling.view, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    wait(lambda: all(doc.get_note(key) for key in keys), "Ctrl+Z did not restore the group in both tabs")
    assert doc.dirty and doc.targets(keys[0]) == tuple(keys) and doc.targets(keys[1]) == tuple(keys)
    assert all(window.tabs.tabText(index).endswith("*") for index in range(window.tabs.count()))
    assert all(pane.view.notes[key[0]][key]["comment"] == "Grouped note" for key in keys)

    sibling.open_find("")
    QTest.keyClicks(sibling.query, "abc")
    revision = doc.revision
    QTest.keyClick(sibling.query, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert not sibling.query.text() and doc.revision == revision
    QTest.keyClick(sibling.query, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert sibling.query.text() == "abc" and doc.revision == revision
    sibling.close_search()
    sibling.view.setFocus()
    QTest.keyClick(sibling.view, Qt.Key.Key_Z,
                   Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    wait(lambda: not any(doc.get_note(key) for key in keys), "Ctrl+Shift+Z did not delete the group again")
    assert not doc.dirty and all(not window.tabs.tabText(index).endswith("*") for index in range(window.tabs.count()))

    doc.store.preferences.data["autoSave"] = True
    window.actions["undo"][0].trigger()
    wait(lambda: all(doc.get_note(key) for key in keys) and not doc.dirty and not doc.busy,
         "Undo was not automatically saved")
    with mu.open(doc.path) as pdf:
        for page in pdf.pages(0, 2):
            assert any(annot.info["content"] == "Grouped note" for annot in page.annots())
    doc.load(reload=True)
    wait(lambda: not doc.loading, "Reload did not finish")
    assert not doc.can_undo and not doc.can_redo


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
            test_click_selection(pane)
            test_resize(pane, 0)
            test_resize(pane, 1)
            window.rotate(90)
            test_resize(pane, 2)
            result = []
            doc.save(callback=result.append)
            wait(lambda: result, "The resized PDF was not saved")
            assert result == [True]
            with mu.open(path) as pdf:
                for number, page in enumerate(pdf.pages(0, 3)):
                    note = page.first_annot
                    assert note.info["content"] == f"Existing comment {number}"
                    assert abs(note.opacity - .37) < .001
            test_shared_history(window)
        finally:
            control.shutdown()
    print("PASS cross-page text, range editing, cancellation, translation lock, rotation and saved notes")


if __name__ == "__main__":
    freeze_support()
    main()
