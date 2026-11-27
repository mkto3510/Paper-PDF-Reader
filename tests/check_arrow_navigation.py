"""Exercise arrow-key navigation through the focused PDF reader window."""

from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory

import pymupdf as mu
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from check_chrome_notes import wait
from paper_reader.application import Application


LEFT, RIGHT, UP, DOWN = Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down


def make_pdf(path, heights):
    with mu.open() as pdf:
        for number, height in enumerate(heights, 1):
            page = pdf.new_page(width=600, height=height)
            page.insert_text((50, 50), f"Page {number}")
        pdf.save(path)


def press(key, modifiers=Qt.KeyboardModifier.NoModifier):
    widget = QApplication.focusWidget()
    assert widget is not None, f"No widget has focus for {key}"
    QTest.keyClick(widget, key, modifiers)
    QApplication.processEvents()


def expect_page(window, number):
    view = window.pane.view
    assert view.current_page() + 1 == number, (number, view.current_page() + 1, view.location())
    assert window.chrome.page.text() == str(number), (number, window.chrome.page.text())


def check_window(app, root):
    uniform = root / "uniform.pdf"
    make_pdf(uniform, [700] * 6)
    control = Application(app, root)
    control.preferences.data.update(language="en", autoSave=False)
    window = control.new_window()
    try:
        window.resize(950, 800)
        window.open_paths([str(uniform)])
        wait(lambda: window.pane and window.pane.view.pages, "PDF did not open")
        pane = window.pane
        view = pane.view
        view.setFocus()
        view.set_layout("single", continuous=True)
        view.set_zoom("fit")
        assert view.verticalScrollBar().maximum() > 0
        for key, page in ((UP, 1), (RIGHT, 2), (DOWN, 3), (LEFT, 2), (UP, 1)):
            press(key)
            expect_page(window, page)

        pane.navigate(2)
        citation_origin = pane.history[0]
        entries = pane.history[:]
        for page in (4, 5, 6, 6):
            press(RIGHT)
            expect_page(window, page)
        assert pane.history == entries
        press(LEFT, Qt.KeyboardModifier.AltModifier)
        assert view.location() == citation_origin[0]
        press(RIGHT)
        assert pane.history_index == 0 and window.actions["forward"][0].isEnabled()
        press(RIGHT, Qt.KeyboardModifier.AltModifier)
        expect_page(window, 6)

        view.set_layout(continuous=False)
        view.set_zoom("custom", 3)
        view.go_to(2)
        app.processEvents()
        horizontal, vertical = view.horizontalScrollBar(), view.verticalScrollBar()
        assert horizontal.maximum() > 0 and vertical.maximum() > 0
        horizontal.setValue(0)
        vertical.setValue(0)
        press(RIGHT)
        assert horizontal.value() > 0 and vertical.value() == 0, (
            horizontal.value(), vertical.value(), view.zoom_mode, QApplication.focusWidget())
        press(DOWN)
        assert vertical.value() > 0
        horizontal.setValue(horizontal.maximum())
        vertical.setValue(vertical.maximum())
        press(RIGHT)
        press(DOWN)
        expect_page(window, 3)
        assert (horizontal.value(), vertical.value()) == (horizontal.maximum(), vertical.maximum())

        view.set_zoom("width")
        app.processEvents()
        assert horizontal.maximum() == 0 and vertical.maximum() > 0
        press(RIGHT)
        expect_page(window, 4)

        for continuous in (False, True):
            for rtl in (False, True):
                view.set_layout("book", continuous=continuous, rtl=rtl)
                if continuous:
                    view.set_zoom("custom", 0.4)
                else:
                    view.set_zoom("fit")
                view.go_to(0)
                app.processEvents()
                for key, page in ((RIGHT, 2), (RIGHT, 4), (LEFT, 3), (LEFT, 1)):
                    press(key)
                    expect_page(window, page)

        window.resize(740, 800)
        view.set_layout("spread", continuous=False, rtl=False)
        view.set_zoom("custom", 0.7)
        app.processEvents()
        view.go_to(2)
        assert horizontal.maximum() > 0 and vertical.maximum() == 0
        press(DOWN)
        expect_page(window, 4)
        assert horizontal.value() > 0
        press(DOWN)
        expect_page(window, 5)

        window.resize(950, 800)
        view.set_layout("spread", continuous=True, rtl=False)
        app.processEvents()
        view.set_zoom("fit")
        view.go_to(4)
        app.processEvents()
        press(RIGHT)
        expect_page(window, 6)
        press(RIGHT)
        expect_page(window, 6)

        page_field = window.chrome.page
        page_field.setFocus()
        page_field.setCursorPosition(1)
        location = view.location()
        press(LEFT)
        assert page_field.cursorPosition() == 0 and view.location() == location
        window.duplicate(False)
        wait(lambda: window.pane is not pane, "Duplicate did not open")
        window.pane.view.setFocus()
        press(LEFT)
        assert view.location() == location, "An inactive view moved"

        mixed = root / "mixed.pdf"
        make_pdf(mixed, [700, 100, 700, 700, 100, 100])
        window.open_paths([str(mixed)])
        wait(lambda: len(window.pane.view.pages) > 1 and window.pane.view.pages[1][1] == 100,
             "Mixed-height PDF did not open")
        view = window.pane.view
        view.setFocus()
        view.set_layout("single", continuous=True)
        view.set_zoom("fit")
        view.go_to(0)
        for key, page in ((DOWN, 2), (DOWN, 3), (DOWN, 4), (DOWN, 5), (DOWN, 6), (UP, 5), (UP, 4)):
            press(key)
            expect_page(window, page)
    finally:
        control.shutdown()


if __name__ == "__main__":
    freeze_support()
    app = QApplication([])
    with TemporaryDirectory() as folder:
        check_window(app, Path(folder))
    print("PASS arrow navigation, overflow, spreads, focus, and history")
