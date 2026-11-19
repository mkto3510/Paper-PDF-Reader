"""Regression coverage for stream ordering, highlight geometry, and editing workflows."""

from dataclasses import replace
from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pymupdf as mu
from PySide6.QtCore import QPoint, QRect, QSize, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox, QPushButton

from check_chrome_notes import wait
from paper_reader.analysis_rules import DEFAULT_RULES, classify
from paper_reader.application import Application
from paper_reader.dialogs import dialog_corner
from paper_reader.selection import Glyph, Line, correct_stream_order, select


def glyph(text, x0, y0, x1, y1):
    return Glyph(text, (x0, y0, x1, y1), ((x0, y0), (x1, y0), (x0, y1), (x1, y1)))


def line(name, x, y, width, kind=""):
    char = glyph(name, x, y, x + width, y + 10)
    return Line((char,), char.box, 10, True, 0, kind)


def check_stream():
    columns = [[line(f"{letter}{row}", x, 90 + row * 16, 144) for row in range(7)]
               for letter, x in zip("ABC", (42, 220, 400))]
    columns[0][3] = line("A3", 87, 138, 98)
    fig = line("Figure label", 43, 127, 34, "figure")
    native = (*columns[0][:3], fig, *columns[0][3:], *columns[1], *columns[2])
    assert correct_stream_order(native, 600, ((40, 125, 85, 150),)) == native
    interleaved = tuple(line for row in zip(*columns) for line in row)
    corrected = correct_stream_order(interleaved, 600, ((40, 125, 85, 150),))
    assert corrected == tuple(line for column in columns for line in column)
    title = line("Spanning title", 40, 50, 510)
    assert correct_stream_order((title, *interleaved), 600)[0] == title

    chars = (glyph("H", 20, 16, 29, 28), glyph("2", 29, 12, 33, 21))
    body = Line(chars, (20, 12, 33, 28), 10, True, 0)
    caption = line("Figure 1. Caption", 20, 38, 80, "caption")
    footer = line("Following body", 20, 60, 110)
    chosen = select({0: (body, caption), 1: (footer,)}, (1, 0, 0), (0, 0, 0), origin="")
    assert chosen.quads[0] == [((20, 12), (33, 12), (20, 28), (33, 28))]
    assert [(part.page, part.kind, part.text) for part in chosen.exclusions] == [(0, "caption", caption.text)]

    caption_rows = []
    for y, name in ((120, "Fig. 1. A figure caption with wide font bounds."),
                    (129, "The next caption line overlaps the first line's bounds."),
                    (138, "The final caption line.")):
        char = glyph(name, 40, y, 460, y + 14)
        caption_rows.append(Line((char,), char.box, 8, True, 2))
    classified = classify(caption_rows, 600, 800, set(), ([], [], [], [], False), DEFAULT_RULES)
    assert [row.kind for row in classified] == ["caption"] * 3
    label = (glyph("1", 42, 50, 55, 60), replace(glyph(".", 55, 50, 58, 60), artifact=True))
    linked_caption = Line(label, (42, 50, 58, 60), 10, True, 0, "caption")
    restored = select({0: (linked_caption,)}, (0, 0, 0), (0, 0, 1), origin="caption")
    assert restored.source == "1." and not restored.exclusions


def make_pdf(path):
    with mu.open() as pdf:
        page = pdf.new_page()
        for y, text in ((100, "This is the first paragraph of the paper."),
                        (125, "Fig. 1 | An illustration between two paragraphs."),
                        (150, "This is the following paragraph of the paper.")):
            page.insert_text((60, y), text)
        note = page.add_highlight_annot(page.search_for("the first paragraph"))
        note.set_info(content="Original")
        note.update()
        pdf.save(path)


def choose(message):
    dialog = next(widget for widget in QApplication.topLevelWidgets()
                  if isinstance(widget, QMessageBox) and widget.isVisible())
    next(button for button in dialog.buttons() if message in button.text()).click()


def check_window(app, root):
    first, second = (root / name for name in ("first.pdf", "second.pdf"))
    for path in (first, second):
        make_pdf(path)
    control = Application(app, root)
    control.preferences.data.update(language="en", autoSave=False, dialogSize=[5000, 5000])
    try:
        window = control.new_window()
        window.open_paths([str(first), str(second)])
        wait(lambda: window.tabs.count() == 2, "Sample tabs did not load")
        panes = {window.tabs.widget(i).document.path: window.tabs.widget(i) for i in range(2)}
        a, b = panes[first], panes[second]
        window.tabs.setCurrentWidget(a)
        assert a.notes.isVisible() and a.outline.isVisible()
        assert not window.chrome.buttons["single"].icon().isNull()
        assert not window.chrome.buttons["spread"].icon().isNull()
        window.actions["spread"][0].trigger()
        assert a.view.layout_mode == "spread"
        window.actions["single"][0].trigger()
        assert a.view.layout_mode == "single"
        wait(lambda: a.document.annotations.get(0), "Highlight did not load")

        a.view.select_all()
        area = window.screen().availableGeometry()
        a.translate_selection(area.bottomRight(), "#ff6666")
        wait(lambda: a.annotation_dialog is not None, "Translation did not open")
        dialog = a.annotation_dialog
        wait(lambda: dialog.screen().availableGeometry().contains(dialog.frameGeometry()),
             "Translation window is outside the work area")
        assert dialog.color.currentData() == "#ff6666"
        assert "illustration" not in dialog.source.toPlainText()
        next(button for button in dialog.findChildren(QPushButton) if button.text() == "Show excluded text").click()
        assert area.contains(dialog.frameGeometry())
        restorer = next(box for box in dialog.findChildren(QCheckBox) if "Restore excluded" in box.text())
        dialog.source.setPlainText("Edited filtered text")
        restorer.setChecked(True)
        assert "illustration" in dialog.source.toPlainText()
        dialog.source.setPlainText("Edited complete text")
        restorer.setChecked(False)
        assert dialog.source.toPlainText() == "Edited filtered text"
        restorer.setChecked(True)
        assert dialog.source.toPlainText() == "Edited complete text"
        dialog.reset_size()
        wait(lambda: area.contains(dialog.frameGeometry()), "Reset moved the dialog outside the work area")
        dialog.reject()
        wait(lambda: not a.annotation_dialog, "Translation window did not close")

        key = next(iter(a.document.annotations[0]))
        a.pick_note(key, edit=True)
        card = a.notes.cards[key]
        card.editor.selectAll()
        card.editor.insertPlainText("Draft preserved through switching")
        assert "*" in window.tabs.tabText(window.tabs.indexOf(a))
        assert window.chrome.save_state.text() == "Unsaved changes"
        QTimer.singleShot(60, lambda: choose("Cancel"))
        window.tabs.setCurrentWidget(b)
        assert window.pane is a
        QTimer.singleShot(60, lambda: choose("Switch without saving"))
        window.tabs.setCurrentWidget(b)
        assert window.pane is b and a.notes.has_drafts()
        window.tabs.setCurrentWidget(a)
        with patch.object(a.document, "save", side_effect=lambda _path, callback: callback(False)):
            QTimer.singleShot(60, lambda: choose("Save and switch"))
            window.tabs.setCurrentWidget(b)
        assert window.pane is a and a.document.dirty
        QTimer.singleShot(60, lambda: choose("Save and switch"))
        window.tabs.setCurrentWidget(b)
        assert window.pane is b and not a.document.dirty
        with mu.open(first) as saved:
            assert saved[0].first_annot.info["content"] == "Draft preserved through switching"

        window.tabs.setCurrentWidget(a)
        for corner in (area.topLeft(), area.topRight(), area.bottomLeft()):
            a.view.select_all()
            a.translate_selection(corner, "#ff6666")
            wait(lambda: a.annotation_dialog is not None, "Translation window did not reopen")
            dialog = a.annotation_dialog
            wait(lambda: area.contains(dialog.frameGeometry()), "Translation window crossed the work-area edge")
            if corner == area.bottomLeft():
                dialog.output.setPlainText("Annotation using selected color")
                dialog.accept_annotation()
                wait(lambda: len(a.document.annotations[0]) == 2, "Color-specified annotation was not created")
                annotation = next(note for note in a.document.annotations[0].values()
                                  if note["comment"] == "Annotation using selected color")
                assert annotation["color"] == "#ff6666"
                assert window.save_document(a.document)
            else:
                dialog.reject()
            wait(lambda: a.annotation_dialog is None, "Translation window did not close")
        screens = [(screen.name(), screen.availableGeometry()) for screen in app.screens()]
        for name, second_area in screens:
            if name == window.screen().name():
                continue
            for corner in (second_area.topLeft(), second_area.bottomRight()):
                a.view.select_all()
                a.translate_selection(corner)
                wait(lambda: a.annotation_dialog is not None, "Secondary-monitor translation did not open")
                dialog = a.annotation_dialog
                try:
                    wait(lambda: dialog.screen().name() == name and second_area.contains(dialog.frameGeometry()),
                         "Translation window left the secondary monitor's work area")
                except AssertionError:
                    print("geometry", [(s.name(), s.availableGeometry().getRect()) for s in app.screens()],
                          "anchor", corner, "screenAt", QGuiApplication.screenAt(corner),
                          "target", name, "actual", dialog.screen().name(), dialog.frameGeometry(), flush=True)
                    raise
                dialog.reject()
                wait(lambda: a.annotation_dialog is None, "Secondary-monitor translation did not close")
    finally:
        control.shutdown()


if __name__ == "__main__":
    freeze_support()
    check_stream()
    secondary = QRect(-1280, -240, 1280, 850)
    assert dialog_corner(secondary, QSize(480, 360), QPoint(-3, 604)) == QPoint(-480, 250)
    assert dialog_corner(secondary, QSize(480, 360), QPoint(-1300, -260)) == secondary.topLeft()
    app = QApplication([])
    with TemporaryDirectory() as work:
        check_window(app, Path(work))
    print("PASS stream order, geometry, translated sources, toolbar and guarded saving")
