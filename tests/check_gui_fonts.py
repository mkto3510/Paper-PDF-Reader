"""Regression coverage for the per-area GUI font sizes."""

import json
from bisect import bisect_right
from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PySide6.QtCore import QRect
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from check_chrome_notes import make_annotated_pdf, wait
from check_layout_dialog import make_pdf
from paper_reader import font_ui, fonts
from paper_reader.application import Application
from paper_reader.dialogs import SettingsDialog
from paper_reader.preferences import Preferences, load_preferences

BASE = 9.0
KEYS = {area.id: area.key for area in fonts.AREAS}


def fixed_base(app, point_size=BASE):
    """Give every check the same base font, whatever the machine's default is."""
    font = QFont(app.font())
    font.setPointSizeF(point_size)
    app.setFont(font)
    font_ui.reset_base_font()


def check_preferences(root):
    path = root / "settings.json"
    defaults = {area.key: area.default for area in fonts.AREAS}
    assert [area.id for area in fonts.AREAS] == ["menu", "toolbar", "tab", "outline", "notes", "translation",
                                                  "dialog", "notice"]
    assert defaults == {"fontMenu": 100, "fontToolbar": 100, "fontTab": 100, "fontOutline": 130,
                        "fontNotes": 130, "fontTranslation": 130, "fontDialog": 100, "fontNotice": 100}
    for raw, expected in (
        ("not json", defaults), ("[]", defaults), ("{}", defaults),
        ('{"fontNotes": 150}', defaults | {"fontNotes": 150}),
        ('{"fontNotes": true}', defaults), ('{"fontNotes": 150.5}', defaults), ('{"fontNotes": "150"}', defaults),
        ('{"fontNotes": null}', defaults), ('{"fontNotes": [150]}', defaults), ('{"fontNotes": NaN}', defaults),
        ('{"fontNotes": 0}', defaults | {"fontNotes": 50}), ('{"fontNotes": -20}', defaults | {"fontNotes": 50}),
        ('{"fontNotes": 301}', defaults | {"fontNotes": 300}), ('{"fontNotes": 10000000000000}',
                                                               defaults | {"fontNotes": 300}),
        ('{"fontMystery": 150}', defaults), ('{"fontMenu": 80, "fontDialog": 120}',
                                             defaults | {"fontMenu": 80, "fontDialog": 120}),
    ):
        path.write_text(raw, encoding="utf-8")
        loaded = load_preferences(path)
        assert {key: loaded[key] for key in defaults} == expected, (raw, loaded)
        assert "fontMystery" not in loaded
    # A settings file from before the font sizes loads as it always did.
    path.write_text(json.dumps({"language": "en", "opacity": 70}), encoding="utf-8")
    loaded = load_preferences(path)
    assert loaded["language"] == "en" and loaded["opacity"] == 70 and loaded["fontOutline"] == 130


def check_arithmetic():
    table = ((9.0, 50, False, 4.5), (9.0, 50, True, 7.0), (9.0, 100, False, 9.0), (9.0, 100, True, 9.0),
             (9.0, 130, False, 11.5), (9.0, 125, False, 11.5), (9.0, 124, False, 11.0), (9.0, 131, False, 12.0), (9.0, 150, False, 13.5),
             (9.0, 225, False, 20.5), (9.0, 300, False, 27.0), (12.0, 50, True, 7.0), (12.0, 78, True, 9.5),
             (6.0, 100, True, 7.0))
    for base, percent, floored, expected in table:
        assert fonts.effective_pt(base, percent, floored) == expected, (base, percent, floored)
    edges = {50: 16, 100: 16, 101: 16, 125: 20, 130: 20, 150: 24, 175: 28, 200: 32, 225: 32, 250: 40, 275: 40,
             300: 48}
    for percent, edge in edges.items():
        assert fonts.icon_edge(percent) == edge, percent


def check_save_and_signals(app, root):
    fixed_base(app)
    preferences = Preferences(root)
    events = []
    preferences.changed.connect(lambda: events.append("changed"))
    preferences.fonts_changed.connect(lambda: events.append("fonts"))
    # A value equal to its default is not written; the in-memory data keeps all eight keys.
    preferences.save(preferences.data | {"fontNotes": 150, "fontOutline": 130, "opacity": 55})
    written = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert written["fontNotes"] == 150 and "fontOutline" not in written and "fontMenu" not in written
    assert preferences.data["fontOutline"] == 130 and preferences.data["fontMenu"] == 100
    assert events == ["changed", "fonts"], events
    # Saving something else does not announce a font change.
    events.clear()
    preferences.save(preferences.data | {"dialogSize": [500, 400]})
    assert events == ["changed"], events
    # Going back to the default drops the key from the file and announces the change.
    events.clear()
    preferences.save(preferences.data | {"fontNotes": 130})
    assert "fontNotes" not in json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert events == ["changed", "fonts"], events
    # A failed write leaves the data and the signals alone.
    events.clear()
    before = dict(preferences.data)
    with patch("paper_reader.preferences.write_json", side_effect=OSError("read-only")):
        try:
            preferences.save(preferences.data | {"fontNotes": 200})
        except OSError:
            pass
        else:
            raise AssertionError("The failed write was swallowed")
    assert preferences.data == before and not events


def check_startup_dialog_font(app, root):
    """A saved dialog size is applied before any window exists, and shutdown puts the base font back."""
    fixed_base(app)
    (root / "settings.json").write_text(json.dumps({"fontDialog": 150}), encoding="utf-8")
    control = Application(app, root)
    try:
        assert app.font().pointSizeF() == 13.5, app.font().pointSizeF()
        control.preferences.save(control.preferences.data | {"fontDialog": 200})
        assert app.font().pointSizeF() == 18.0
        control.preferences.save(control.preferences.data | {"fontDialog": 50})
        assert app.font().pointSizeF() == 7.0, "The dialog area has a 7 pt floor"
    finally:
        control.shutdown()
    assert app.font().pointSizeF() == BASE
    control.shutdown()  # shutdown can run more than once
    assert app.font().pointSizeF() == BASE


def check_base_font_taken_once(app):
    fixed_base(app, 9.0)
    assert font_ui.process_base_font().pointSizeF() == 9.0
    big = QFont(app.font())
    big.setPointSizeF(20)
    app.setFont(big)
    assert font_ui.process_base_font().pointSizeF() == 9.0, "The base font must be read once per process"
    fixed_base(app)


def check_settings_dialog(app, root):
    fixed_base(app)
    control = Application(app, root)
    try:
        window = control.new_window()
        shortcut = window.actions["preferences"][0].shortcut().toString()
        assert shortcut == "Ctrl+,", shortcut
        events = []
        control.preferences.fonts_changed.connect(lambda: events.append("fonts"))
        dialog = SettingsDialog(window)
        assert dialog.tabs.count() == 4 and dialog.tabs.tabText(3) == "文字サイズ"
        for area in fonts.AREAS:
            box = dialog.font_rows[area.id][0]
            assert (box.minimum(), box.maximum(), box.value()) == (50, 300, area.default), area.id
        assert dialog.font_rows["notes"][1].text() == "11.5 pt"
        dialog.font_rows["dialog"][0].setValue(50)
        assert dialog.font_rows["dialog"][1].text() == "7 pt（下限）"
        dialog.font_rows["dialog"][0].setValue(60)
        assert dialog.font_rows["dialog"][1].text() == "7 pt（下限）", "60% of 9 pt is 5.4 pt, below the floor"
        dialog.font_rows["notes"][0].setValue(150)
        assert dialog.font_rows["notes"][2].font().pointSizeF() == 13.5
        assert control.preferences.data["fontNotes"] == 130, "Nothing changes before Save"
        # The buttons only fill in the boxes; a reset button names the value it goes back to.
        buttons = {button.text(): button for button in dialog.findChildren(QPushButton)}
        buttons["初期値 130%"].click()
        dialog.font_rows["menu"][0].setValue(80)
        buttons["すべて初期値へ戻す"].click()
        assert {area.id: dialog.font_rows[area.id][0].value() for area in fonts.AREAS} == \
            {area.id: area.default for area in fonts.AREAS}
        dialog.close()

        # Two dialogs open at once: each Save writes only its own rows, so neither undoes the other.
        first, second = SettingsDialog(window), SettingsDialog(window)
        first.font_rows["notes"][0].setValue(150)
        first.commit()
        second.font_rows["outline"][0].setValue(200)
        second.commit()
        assert control.preferences.data["fontNotes"] == 150 and control.preferences.data["fontOutline"] == 200
        assert events == ["fonts", "fonts"], events
        written = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        assert written["fontNotes"] == 150 and written["fontOutline"] == 200 and "fontMenu" not in written

        # A translation setting with an error blocks the whole Save, font sizes included.
        events.clear()
        broken = SettingsDialog(window)
        broken.font_rows["tab"][0].setValue(200)
        broken.fields["endpoint"].setText("not a url")
        with patch("paper_reader.dialogs.warn") as warning:
            broken.commit()
            assert warning.call_count == 1
        assert control.preferences.data["fontTab"] == 100 and not events
        broken.close()

        # A write that fails warns, keeps the dialog open and applies nothing.
        failing = SettingsDialog(window)
        failing.font_rows["tab"][0].setValue(200)
        with patch("paper_reader.dialogs.QMessageBox.warning") as warning, \
                patch("paper_reader.preferences.write_json", side_effect=OSError("read-only")):
            failing.commit()
            assert warning.call_count == 1
        assert control.preferences.data["fontTab"] == 100 and not events
        failing.close()
    finally:
        control.shutdown()


def check_settings_dialog_fits_screen(app, root):
    """At 300% the Settings window lowers its own font until it fits; the rest of the app is untouched."""
    fixed_base(app)
    (root / "settings.json").write_text(json.dumps({"fontDialog": 300}), encoding="utf-8")
    control = Application(app, root)
    try:
        window = control.new_window()
        # The offscreen screen is 800 px wide, narrower than the dialog's own smallest width at any size,
        # so the check uses a 1366x768 screen (work area 1366x738) through a stand-in parent.
        class Screen:
            def availableGeometry(self):
                return QRect(0, 0, 1366, 738)

        class Parent:
            def screen(self):
                return Screen()

            def frameGeometry(self):
                return QRect(0, 0, 1000, 800)

            def geometry(self):
                return QRect(0, 0, 1000, 800)

        dialog = SettingsDialog(window)
        pt = font_ui.fit_dialog_font(dialog, control.fonts, Parent())
        hint = dialog.minimumSizeHint()
        assert hint.width() <= 1366 and hint.height() <= 738, hint
        assert pt < 27.0 and dialog.font().pointSizeF() == pt, "The Settings font must come down from 300%"
        assert dialog.width() <= 1366 and dialog.height() <= 738
        dialog.close()
        # At the default size nothing is lowered.
        control.preferences.save(control.preferences.data | {"fontDialog": 100})
        dialog = SettingsDialog(window)
        assert font_ui.fit_dialog_font(dialog, control.fonts, Parent()) == 9.0
        dialog.close()
    finally:
        control.shutdown()


def audit(window):
    """The point size of every widget in the window's own tree (top-level windows inside it are skipped)."""
    sizes = {}
    for widget in window.findChildren(QWidget):
        node, inside = widget, True
        while node is not window:
            if node.isWindow():
                inside = False
                break
            node = node.parentWidget()
        if inside:
            sizes[widget] = widget.font().pointSizeF()
    return sizes


def check_reader_fonts(app, root):
    """The ground, the notes and outline areas, and the re-measuring of cards."""
    fixed_base(app)
    pdf = root / "notes.pdf"
    other = root / "other.pdf"
    make_annotated_pdf(pdf, 12)
    make_annotated_pdf(other, 3)
    control = Application(app, root)
    try:
        window = control.new_window()
        window.resize(1100, 800)
        window.open_paths([str(pdf), str(other)])
        wait(lambda: window.tabs.count() == 2 and all(window.tabs.widget(i).document.annotations
                                                       for i in range(2)), "The documents did not load")
        panes = [window.tabs.widget(i) for i in range(2)]
        pane, hidden = panes[0], panes[1]
        window.tabs.setCurrentWidget(pane)
        pane.show_sidebar("notes", True)
        pane.splitter.setSizes([300, 800])
        panel = pane.notes
        QTest.qWait(200)
        assert window.font().pointSizeF() == BASE
        assert pane.outline.font().pointSizeF() == 11.5 and panel.font().pointSizeF() == 11.5, "130% defaults"
        key = panel.keys[0]  # stays on screen when the cards grow, so its focus can be checked
        window.activateWindow()
        QTest.qWaitForWindowActive(window)
        pane.pick_note(key, focus_card=True)
        wait(lambda: key in panel.cards and panel.cards[key].editor.isVisible(), "The card editor did not open")
        card = panel.cards[key]
        editor = card.editor
        editor.setFocus()
        editor.selectAll()
        editor.insertPlainText("first edit. ")
        editor.insertPlainText("second edit. " * 3)
        cursor = editor.textCursor()
        cursor.setPosition(10)
        editor.setTextCursor(cursor)
        QTest.qWait(300)
        bar = panel.scroll.verticalScrollBar()
        before = card.editor_minimum

        # Notes at 150%, outline at 160%, dialog at 180%: three different sizes, all kept apart.
        control.preferences.save(control.preferences.data | {"fontNotes": 150, "fontOutline": 160,
                                                              "fontDialog": 180})
        def settled(notes):
            # Only the cards on screen are placed; the hidden ones keep their old geometry until they scroll in.
            return all(notes.heights.get(number) == shown.geometry().height()
                       for number, shown in notes.cards.items() if not shown.isHidden())

        wait(lambda: settled(panel), "The card heights did not settle")
        QTest.qWait(300)
        assert (pane.outline.font().pointSizeF(), panel.font().pointSizeF()) == (14.5, 13.5)
        assert app.font().pointSizeF() == 16.0 and window.font().pointSizeF() == BASE
        assert panel.cards[key] is card and card.editor is editor, "Cards must not be rebuilt"
        assert editor.hasFocus() and editor.textCursor().position() == 10, (editor.hasFocus(), editor.textCursor().position(), card.isHidden(), window.isActiveWindow(), app.focusWidget())
        assert editor.document().isUndoAvailable()
        assert editor.font().pointSizeF() == 13.5 and card.editor_minimum > before
        assert editor.height() >= card.editor_minimum, "The comment box must follow its font"
        assert settled(panel) and any(not shown.isHidden() for shown in panel.cards.values())
        for number, shown in panel.cards.items():
            if not shown.isHidden():
                assert shown.actions.height() == shown.actions.grid.sizeHint().height(), "Button group height"
        side = max(25, panel.fontMetrics().height() + 5)
        assert all((b.width(), b.height()) == (side, side) for b in panel.colors.values())
        # Nothing in the window follows the dialog size; every size comes from the ground or one area.
        sizes = audit(window)
        assert set(sizes.values()) <= {BASE, 14.5, 13.5}, {s for s in sizes.values()}
        assert 16.0 not in sizes.values()
        # The cards on screen after a size change are the ones that were on screen before it.
        bar.setValue(bar.maximum() // 2)
        QTest.qWait(300)
        anchor = panel.keys[bisect_right(panel.tops, bar.value()) - 1]
        control.preferences.save(control.preferences.data | {"fontNotes": 120})
        wait(lambda: settled(panel), "The card heights did not settle again")
        QTest.qWait(300)
        assert panel.keys[bisect_right(panel.tops, bar.value()) - 1] == anchor, "The scroll position was lost"
        # A tab that was hidden while the sizes changed lays itself out when it is shown.
        card.cancel_edit()  # an unsaved draft would make leaving this tab ask a question
        window.tabs.setCurrentWidget(hidden)
        hidden.show_sidebar("notes", True)
        QTest.qWait(400)
        hidden_panel = hidden.notes
        assert hidden_panel.font().pointSizeF() == 11.0
        assert settled(hidden_panel) and hidden_panel.cards
        # Going back makes everything default again.
        control.preferences.save(control.preferences.data | {"fontNotes": 130, "fontOutline": 130,
                                                              "fontDialog": 100})
        QTest.qWait(300)
        assert panel.font().pointSizeF() == 11.5 and app.font().pointSizeF() == BASE
    finally:
        control.shutdown()


def check_translation_window_font(app, root):
    """An open translation window follows its area, and the reflow does not change the saved size."""
    fixed_base(app)
    path = root / "translate.pdf"
    make_pdf(path)
    control = Application(app, root)
    control.preferences.data.update(language="en", autoSave=False)
    try:
        window = control.new_window()
        window.open_paths([str(path)])
        wait(lambda: window.pane and window.pane.document.annotations.get(0), "Sample did not load")
        pane = window.pane
        pane.view.select_all()
        pane.translate_selection(window.screen().availableGeometry().bottomRight())
        wait(lambda: pane.annotation_dialog is not None, "Translation did not open")
        dialog = pane.annotation_dialog
        QTest.qWait(200)
        assert dialog.font().pointSizeF() == 11.5, "The translation window starts at its 130% default"
        saved = control.preferences.data["dialogSize"]
        preferred = dialog.preferred_size
        control.preferences.save(control.preferences.data | {"fontTranslation": 300})
        QTest.qWait(300)
        assert dialog.font().pointSizeF() == 27.0 and dialog.output.font().pointSizeF() == 27.0
        assert not dialog.applying_font
        assert dialog.preferred_size == preferred, "A font change must not replace the size to be saved"
        assert control.preferences.data["dialogSize"] == saved
        # A window opened after the change starts at the new size.
        dialog.reject()
        wait(lambda: pane.annotation_dialog is None, "The translation window did not close")
        pane.translate_selection(window.screen().availableGeometry().bottomRight())
        wait(lambda: pane.annotation_dialog is not None, "Translation did not reopen")
        assert pane.annotation_dialog.font().pointSizeF() == 27.0
        pane.annotation_dialog.reject()
    finally:
        control.shutdown()


if __name__ == "__main__":
    freeze_support()
    app = QApplication([])
    fixed_base(app)
    check_arithmetic()
    with TemporaryDirectory() as work:
        check_preferences(Path(work).resolve())
    with TemporaryDirectory() as work:
        check_save_and_signals(app, Path(work).resolve())
    with TemporaryDirectory() as work:
        check_startup_dialog_font(app, Path(work).resolve())
    check_base_font_taken_once(app)
    with TemporaryDirectory() as work:
        check_settings_dialog(app, Path(work).resolve())
    with TemporaryDirectory() as work:
        check_settings_dialog_fits_screen(app, Path(work).resolve())
    with TemporaryDirectory() as work:
        check_reader_fonts(app, Path(work).resolve())
    with TemporaryDirectory() as work:
        check_translation_window_font(app, Path(work).resolve())
    print("PASS font sizes: preferences, arithmetic, saving, startup, settings, reader")
