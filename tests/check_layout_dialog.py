"""Regression coverage for stream ordering, highlight geometry, and editing workflows."""

from dataclasses import replace
from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pymupdf as mu
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, QTimer, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox, QPushButton

from check_chrome_notes import wait
from paper_reader.analysis_rules import DEFAULT_RULES, classify
from paper_reader.application import Application
from paper_reader.dialogs import SettingsDialog, dialog_corner
from paper_reader.selection import Glyph, Line, correct_stream_order, select
from paper_reader.translation import Job


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
        restorer = next(box for box in dialog.findChildren(QCheckBox) if "Include excluded" in box.text())
        label = restorer.text()
        dialog.source.setPlainText("Edited filtered text")
        restorer.setChecked(True)
        assert restorer.text() == label, "The checkbox label must not change with its state"
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
        assert window.windowTitle() == f"{a.document.path.name} — Paper PDF Viewer — Unsaved changes"
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
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert not window.findChildren(QMessageBox), "Confirmation boxes were not released"

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


def check_tabs(app, root):
    """Tab menus act on the clicked tab without switching, names show "&", and windows come back."""
    first, second = root / "Smith&Jones.pdf", root / "other.pdf"
    for path in (first, second):
        make_pdf(path)
    control = Application(app, root)
    control.preferences.data.update(language="en", autoSave=False)
    try:
        window = control.new_window()
        window.open_paths([str(first), str(second)])
        wait(lambda: window.tabs.count() == 2, "Sample tabs did not load")
        panes = {pane.document.path.name: pane for pane in (window.tabs.widget(i) for i in range(2))}
        a, b = panes["Smith&Jones.pdf"], panes["other.pdf"]
        wait(lambda: a.document.annotations.get(0), "Highlight did not load")
        assert window.tabs.tabText(window.tabs.indexOf(a)) == "Smith&&Jones.pdf"
        window.update_status()
        assert window.tabs.tabText(window.tabs.indexOf(a)) == "Smith&&Jones.pdf"

        # The menu duplicates the tab that was clicked, and never asks to save because it never switches.
        window.tabs.setCurrentWidget(a)
        key = next(iter(a.document.annotations[0]))
        a.pick_note(key, edit=True)
        card = a.notes.cards[key]
        card.editor.selectAll()
        card.editor.insertPlainText("Unsaved draft")
        assert a.notes.has_drafts()
        bar = window.tabs.tabBar()
        point = bar.tabRect(window.tabs.indexOf(b)).center()
        picked = []
        with patch.object(window, "confirm_switch", side_effect=AssertionError("The tab menu switched tabs")):
            QTimer.singleShot(80, lambda: picked.append(QApplication.activePopupWidget() is not None))
            QTimer.singleShot(160, lambda: QTest.keyClick(QApplication.activePopupWidget(), Qt.Key.Key_Escape))
            window.tab_menu(point)
        assert picked == [True], "The tab menu did not open"
        assert window.pane is a
        count = window.tabs.count()
        with patch.object(window, "confirm_switch", return_value=True):
            window.duplicate(False, window.tabs.indexOf(b))
        assert window.tabs.count() == count + 1 and window.pane.document is b.document

        # A window that is closed while a document is still loading says so instead of ignoring the click.
        shown = []
        window.opening.add(b.document)
        with patch.object(window, "show_message", shown.append):
            window.close()
        assert shown == ["A document is still loading."] and window in control.windows
        window.opening.clear()

        # A PDF sent to a minimized window restores it.
        window.showMinimized()
        QApplication.processEvents()
        assert window.isMinimized(), "The test platform cannot minimize windows"
        control.receive_paths([str(second)])
        QApplication.processEvents()
        assert not window.isMinimized() and not (window.windowState() & Qt.WindowState.WindowMinimized)
        # A maximized window that was minimized comes back maximized, not as a normal window.
        window.showMaximized()
        QApplication.processEvents()
        assert window.isMaximized()
        window.showMinimized()
        QApplication.processEvents()
        window.present()
        QApplication.processEvents()
        assert not window.isMinimized() and window.isMaximized(), window.windowState()
    finally:
        control.shutdown()


def check_data_protection(app, root):
    """Unsaved translations, drafts on close, copies, and the settings profile are never lost silently."""
    path = root / "protect.pdf"
    make_pdf(path)
    control = Application(app, root)
    control.preferences.data.update(language="en", autoSave=False)
    try:
        window = control.new_window()
        window.open_paths([str(path)])
        wait(lambda: window.pane and window.pane.document.annotations.get(0), "Sample did not load")
        a = window.pane
        doc = a.document
        key = next(iter(doc.annotations[0]))

        # A translation dialog with unsaved text asks before every way of discarding it.
        a.view.select_all()
        area = window.screen().availableGeometry()
        a.translate_selection(area.bottomRight())
        wait(lambda: a.annotation_dialog is not None, "Translation did not open")
        dialog = a.annotation_dialog
        assert dialog.confirm_close() and not dialog.unsaved()
        dialog.output.setPlainText("An edited translation")
        assert dialog.unsaved()
        QTimer.singleShot(60, lambda: choose("Keep editing"))
        dialog.reject()
        assert a.annotation_dialog is dialog and dialog.isVisible()
        # The real Esc key and the title-bar close button take the same guarded path.
        QTimer.singleShot(60, lambda: choose("Keep editing"))
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert a.annotation_dialog is dialog and dialog.isVisible()
        QTimer.singleShot(60, lambda: choose("Keep editing"))
        dialog.close()
        assert a.annotation_dialog is dialog and dialog.isVisible()
        QTimer.singleShot(60, lambda: choose("Keep editing"))
        assert not window.close_tabs([0]) and window.tabs.count() == 1 and a.annotation_dialog is dialog
        QTimer.singleShot(60, lambda: choose("Keep editing"))
        a.open_translation(source="Another", comment="")
        assert a.annotation_dialog is dialog
        # A running translation counts as unsaved and locks the text box until it ends.
        dialog.output.setPlainText(dialog.baseline)
        assert not dialog.unsaved()
        job = Job(None, None)
        dialog.job = job
        job.changed.connect(dialog.update_job)
        job.set_state("running", "partial")
        assert dialog.unsaved() and dialog.output.isReadOnly()
        job.set_state("canceled")
        assert not dialog.output.isReadOnly()
        dialog.output.setPlainText("Typed again")
        QTimer.singleShot(60, lambda: choose("Discard"))
        dialog.reject()
        wait(lambda: a.annotation_dialog is None, "Discarding did not close the translation window")

        # A tab that leaves its document open elsewhere saves every draft, not only the selected card's.
        window.duplicate(False)
        clone = window.pane
        assert clone is not a and clone.document is doc
        clone.notes.drafts[key] = "Parked draft"
        assert window.close_tabs([window.tabs.indexOf(clone)])
        wait(lambda: doc.get_note(key)["comment"] == "Parked draft", "The parked draft was lost on close")

        # Save as writes a copy and says so; the open document and its marker do not change.
        shown = []
        copy = root / "copy.pdf"
        with patch.object(window, "show_message", shown.append):
            wait(lambda: not doc.busy, "The document stayed busy")
            assert window.save_document(doc, str(copy))
            assert copy.exists() and len(shown) == 1 and doc.path.resolve() == path.resolve()
            assert window.save_document(doc, str(path)) and len(shown) == 1, "Saving over the original is not a copy"

        # Editing another profile never changes the profile used for translation.
        settings = SettingsDialog(window)
        original = control.preferences.data["activeProfile"]
        assert settings.active_profile == original and "(in use)" in settings.profiles.currentText()
        settings.add_profile(False)
        new_key = settings.profile_key
        assert new_key != original and settings.use_profile.isEnabled() and not settings.use_profile.isChecked()
        settings.commit()
        assert control.preferences.data["activeProfile"] == original
        settings = SettingsDialog(window)
        settings.select_profile(new_key)
        settings.use_profile.setChecked(True)
        assert not settings.use_profile.isEnabled() and "(in use)" in settings.profiles.currentText()
        QTimer.singleShot(60, lambda: choose("Delete"))
        settings.remove_profile()
        assert settings.active_profile == next(iter(settings.data["profiles"])) != new_key
        # Validation selects the failing profile, names it, and blocks empty languages.
        good_url = control.preferences.data["profiles"][original]["endpoint"]
        settings.add_profile(True)
        broken = settings.profile_key
        settings.fields["endpoint"].setText("not a url")
        settings.select_profile(settings.active_profile)
        assert settings.profile_key != broken
        with patch("paper_reader.settings_dialog.warn") as warning:
            settings.commit()
            assert warning.call_count == 1 and settings.profile_key == broken
            assert settings.data["profiles"][broken]["name"] in warning.call_args.args[2]
            assert "Invalid translation service URL." in warning.call_args.args[2]
            control.preferences.data["language"] = "ja"
            settings.commit()
            assert "URL が正しくありません" in warning.call_args.args[2], warning.call_args.args[2]
            assert "Invalid" not in warning.call_args.args[2]
            control.preferences.data["language"] = "en"
            settings.fields["endpoint"].setText(good_url)
            settings.languages["langTo"].setText("  ")
            settings.commit()
            assert warning.call_count == 3 and settings.tabs.currentIndex() == 0
        settings.close()
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
        check_window(app, Path(work).resolve())
    with TemporaryDirectory() as work:
        check_data_protection(app, Path(work).resolve())
    with TemporaryDirectory() as work:
        check_tabs(app, Path(work).resolve())
    print("PASS stream order, geometry, translated sources, toolbar and guarded saving")
