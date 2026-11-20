"""Regression checks for the Windows chrome and large annotation lists."""

from math import ceil
from multiprocessing import freeze_support
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

import pymupdf as mu
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QLabel, QStatusBar, QTabWidget, QTextBrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_reader.application import Application
from paper_reader import VERSION
from paper_reader.analysis_rules import RULES
from paper_reader.dialogs import AboutDialog, SettingsDialog
from paper_reader.notes import Card
from paper_reader.translation import Job


def wait(predicate, description):
    for _ in range(240):
        if predicate():
            return
        QTest.qWait(25)
    raise AssertionError(description)


def make_annotated_pdf(path, count):
    with mu.open() as document:
        for start in range(0, count, 25):
            page = document.new_page()
            for index in range(start, min(start + 25, count)):
                y = 55 + (index - start) * 29
                page.insert_text((55, y), f"Paragraph {index}. A test excerpt for the annotation.")
                highlight = page.add_highlight_annot(mu.Rect(55, y - 12, 247, y + 2))
                highlight.set_colors(stroke=(1, 0, 0) if index % 2 else (1, .83, 0))
                highlight.set_info(content="This comment wraps when the sidebar is narrow. " * 12)
                highlight.update()
        document.save(path)


def check_menu(window, save_settings=False):
    QApplication.processEvents()
    menu_bar = window.chrome.menus
    for key in ("open", "about"):
        item = window.actions[key][0]
        assert item.isEnabled()
        action = next(action for action in menu_bar.actions() if action.menu() and item in action.menu().actions())
        menu = action.menu()
        QTest.mouseClick(menu_bar, Qt.MouseButton.LeftButton, pos=menu_bar.actionGeometry(action).center())
        assert menu.isVisible(), (key, item.text(), menu_bar.rect(), menu_bar.actionGeometry(action),
                                  menu_bar.activeAction(), QApplication.activePopupWidget())
        QTest.keyClick(menu, Qt.Key.Key_Escape)
        assert not menu.isVisible()
    settings = window.actions["preferences"][0]
    assert settings in menu_bar.actions() and settings.isEnabled()
    QTest.mouseClick(menu_bar, Qt.MouseButton.LeftButton, pos=menu_bar.actionGeometry(settings).center())
    dialog = window.findChild(SettingsDialog)
    assert dialog and dialog.isVisible()
    assert tuple(dialog.checks) == RULES
    if save_settings:
        field = dialog.checks["caption"]
        field.setChecked(not field.isChecked())
        expected = field.isChecked()
        buttons = dialog.findChild(QDialogButtonBox)
        QTest.mouseClick(buttons.button(QDialogButtonBox.StandardButton.Save), Qt.MouseButton.LeftButton)
        assert window.preferences.data["rules"]["caption"] == expected
    else:
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    wait(lambda: window.findChild(SettingsDialog) is None, "Settings did not close")


def check_about(window, root):
    def inspect():
        dialog = QApplication.activeModalWidget()
        tabs = dialog.findChild(QTabWidget)
        labels = [child.text() for child in dialog.findChildren(QLabel)]
        description = tabs.widget(0)
        dialog.notice.setCurrentIndex(1)
        seen.append((tabs.tabText(0), tabs.tabText(1), labels, description.toHtml(), dialog.notice_body.toHtml()))
        if window.language == "en":
            dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close).click()
        else:
            QTest.keyClick(dialog, Qt.Key.Key_Escape)

    seen = []
    QTimer.singleShot(0, inspect)
    window.actions["about"][0].trigger()
    overview, licenses, labels, description, notices = seen[0]
    assert (overview, licenses) == (window.t("概要", "Overview"), window.t("ライセンス", "Licenses"))
    assert window.t(f"バージョン {VERSION}", f"Version {VERSION}") in labels
    assert "https://github.com/mkto3510/Paper-PDF-Reader" in description
    assert "https://github.com/pymupdf/PyMuPDF" in notices

    bundle = root / "bundle"
    folder = bundle / "LICENSES"
    component = folder / "Component"
    component.mkdir(parents=True, exist_ok=True)
    (component / "LICENSE.txt").write_text("Third-party license", encoding="utf-8")
    (bundle / "LICENSE.txt").write_text("Application license", encoding="utf-8")
    with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(bundle / "reader.exe")):
        dialog = AboutDialog(window)
    assert "Application license" == dialog.notice_body.toPlainText()
    dialog.notice.setCurrentIndex(1)
    assert "THIRD_PARTY.md" in dialog.notice_body.toPlainText()
    dialog.notice.setCurrentIndex(2)
    assert "Third-party license" == dialog.notice_body.toPlainText()
    assert dialog.findChild(QTextBrowser).openExternalLinks()
    dialog.deleteLater()


def check_native_frame(window):
    if QApplication.platformName() != "windows":
        return
    from ctypes import windll, wintypes, c_int

    style = windll.user32.GetWindowLongW
    style.argtypes = (wintypes.HWND, c_int)
    native_style = style(int(window.winId()), -16)
    for flag in (0x00C00000, 0x00040000, 0x00010000):
        assert native_style & flag == flag
    assert window.windowHandle().frameMargins().top() > 0


def check_chrome(window):
    assert window.findChild(QStatusBar) is None
    check_menu(window)
    pane = window.pane
    pane.match_count.setText("10 / 148")
    for width in (530, 760, 1180, 1920):
        window.resize(width, 710)
        QTest.qWait(40)
        menu = window.chrome.menus
        assert menu.rect().contains(menu.actionGeometry(menu.actions()[-1]))
        widgets = (pane.query, pane.prev_match, pane.next_match, pane.match_count, pane.search_options)
        for left, right in zip(widgets, widgets[1:]):
            assert 0 <= right.x() - (left.x() + left.width()) <= 5
            assert right.isVisible()
        assert window.chrome.page.isVisible() and window.chrome.zoom.isVisible()
    assert window.chrome.more.isHidden()
    window.resize(530, 710)
    QTest.qWait(40)
    assert window.chrome.more.isVisible()
    window.showMaximized()
    assert window.isMaximized()
    check_menu(window)
    window.showNormal()
    assert not window.isMaximized()


def exercise(app, folder, count):
    path = folder / f"{count}-annotations.pdf"
    make_annotated_pdf(path, count)
    control = Application(app, folder)
    control.preferences.data["autoSave"] = False
    try:
        window = control.new_window()
        check_native_frame(window)
        check_menu(window, save_settings=True)
        check_about(window, folder)
        window.open_paths([str(path)])
        wait(lambda: window.pane and sum(map(len, window.pane.document.annotations.values())) == count,
             "The annotations did not load")
        pane = window.pane
        doc = pane.document
        check_chrome(window)
        doc.loading = True
        window.update_status()
        check_menu(window)
        assert not window.actions["duplicate_tab"][0].isEnabled()
        doc.loading = False
        control.preferences.save(control.preferences.data | {"language": "en"})
        assert window.chrome.menus.actions()[0].text() == "File"
        check_chrome(window)
        check_about(window, folder)
        window.resize(1050, 810)
        pane.show_sidebar("notes", True)
        pane.splitter.setSizes([256, 800])
        panel = pane.notes
        first, second, *_, last = panel.keys
        pane.pick_note(first)
        wait(lambda: not doc.source_pending, "The first viewport did not settle")
        pane.pick_note(second)
        wait(lambda: not doc.source_pending, "The second viewport did not settle")
        QTest.qWait(75)
        assert 0 < len(panel.cards) < 60

        updated = []
        update_card = Card.update_card

        def tracked(card):
            updated.append((card.pane, card.key))
            update_card(card)

        with patch.object(Card, "update_card", tracked):
            samples = []
            for index in range(32):
                start = perf_counter()
                pane.pick_note((first, second)[index % 2])
                app.processEvents()
                samples.append((perf_counter() - start) * 1000)
            p95 = sorted(samples)[ceil(.95 * len(samples)) - 1]
            print(f"{count} annotations, selection p95 {p95:.1f} ms, {len(panel.cards)} mounted cards")
            if count == 200:
                assert p95 <= 100
            assert {key for _, key in updated} <= {first, second}, updated

            window.duplicate(False)
            clone = window.pane
            assert clone is not pane and clone.document is doc
            clone.pick_note(first)
            QTest.qWait(40)
            job = Job(None, None)
            doc.attach_job((first,), job)
            job.set_state("running", "started")
            updated.clear()
            for index in range(50):
                job.set_state("running", f"partial {index}")
            assert not updated
            wait(lambda: updated, "The coalesced translation update did not arrive")
            assert updated == [(clone, first)]
            assert "partial 49" in clone.notes.cards[first].state.text()
            window.tabs.setCurrentWidget(pane)
            wait(lambda: first in panel.cards and "partial 49" in panel.cards[first].state.text(),
                 "The hidden tab did not synchronize on activation")
            job.set_state("failed", error="test failure")
            assert "test failure" in panel.cards[first].state.text()
            job.set_state("canceled")

        pane.pick_note(first)
        first_card = panel.cards[first]
        first_card.editor.setFocus()
        first_card.editor.selectAll()
        first_card.editor.insertPlainText("Draft to preserve")
        panel.scroll.verticalScrollBar().setValue(panel.scroll.verticalScrollBar().maximum())
        wait(lambda: last in panel.cards and first not in panel.cards, "Scrolling did not replace cards")
        panel.reveal(first)
        assert panel.cards[first].editor.toPlainText() == "Draft to preserve"
        panel.cards[first].commit_comment()
        wait(lambda: doc.get_note(first)["comment"] == "Draft to preserve", "The comment was not saved")

        active_filter = doc.get_note(first)["color"]
        panel.set_filter(active_filter, True)
        assert first in panel.keys and second not in panel.keys
        panel.reset_filter()
        assert len(panel.keys) == count
        pane.pick_note(first, focus_card=True)
        QTest.keyClick(panel.cards[first], Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert len(panel.multiple) == count and len(panel.cards) < 60

        pane.pick_note(first, focus_card=True)
        QTest.keyClick(panel.cards[first], Qt.Key.Key_Delete)
        wait(lambda: doc.get_note(first) is None, "Delete from the sidebar did not remove the highlight")
        pane.pick_note(second)
        pane.view.setFocus()
        QTest.keyClick(pane.view, Qt.Key.Key_Delete)
        wait(lambda: doc.get_note(second) is None, "Delete from the PDF view did not remove the highlight")
        assert clone.document.get_note(first) is None and clone.document.get_note(second) is None
        result = []
        doc.save(callback=result.append)
        wait(lambda: result, "The edited test PDF was not saved")
        assert result == [True]
        pane.show_sidebar("notes", False)
        failed = Job(None, None)
        failed.set_state("failed", error="test failure")
        doc.attach_job((last,), failed)
        assert window.view_menu.title() == "View ●"
        assert window.close_tabs(range(window.tabs.count()))
        assert window.view_menu.title() == "View"
        check_menu(window)
    finally:
        control.shutdown()


def main():
    app = QApplication([])
    app.setApplicationName("Paper PDF Reader")
    if "--light" in sys.argv:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    for count in (200, 500):
        with TemporaryDirectory() as folder:
            exercise(app, Path(folder), count)
    print("PASS chrome and annotation regressions")


if __name__ == "__main__":
    freeze_support()
    main()
