"""Check that a page's text is usable before its classification is done, and that nothing is lost afterwards."""

from multiprocessing import freeze_support
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pymupdf as mu
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_chrome_notes import wait
from paper_reader.application import Application
from paper_reader.selection import Glyph, Line


def fixture(path):
    with mu.open() as pdf:
        page = pdf.new_page(width=600, height=700)
        # The file lists the lower line first, so the classified lines (in file order) and the quick lines
        # (in reading order) number the two lines differently.
        page.insert_text((60, 300), "Lower line comes first in the file.")
        page.insert_text((60, 200), "Upper line comes second in the file.")
        pdf.new_page(width=600, height=700).insert_text((60, 100), "A light second page.")
        pdf.save(path)


def remap_by_position(view):
    """Endpoints move to the glyphs at the same place in the classified lines."""
    def line(text, y, kind=""):
        box = (10, y, 10 + 8 * len(text), y + 10)
        return Line(tuple(Glyph(c, (10 + 8 * i, y, 18 + 8 * i, y + 10), ()) for i, c in enumerate(text)),
                    box, 10, True, 0, kind)

    quick = [line("upper", 10), line("lower", 40)]
    final = [line("lower", 40, "caption"), line("upper", 10, "furniture")]
    view.selection_all = False
    view.selection_range = (0, 0, 1), (0, 1, 3)
    view.unit_anchor = (0, 1, 3), (0, 1, 3), ""
    view.origin_spot = (0, 10 + 8 * 3 + 4, 45)
    view._remap_selection(0, quick, final)
    assert view.selection_range == ((0, 0, 3), (0, 1, 1)), view.selection_range
    assert view.unit_anchor == ((0, 0, 3), (0, 0, 3), "caption"), view.unit_anchor
    assert view.selection_origin == "caption", view.selection_origin
    view.selection_range = (0, 0, 0), (1, 2**31, 2**31)
    view.selection_all = True  # "select all" has no glyph positions to carry over
    view._remap_selection(0, quick, final)
    assert view.selection_range == ((0, 0, 0), (1, 2**31, 2**31))
    view.clear_selection()


def main():
    app = QApplication([])
    with TemporaryDirectory() as folder:
        root = Path(folder)
        path = root / "quick.pdf"
        fixture(path)
        control = Application(app, root)
        control.preferences.data["autoSave"] = False
        try:
            window = control.new_window()
            window.open_paths([str(path)])
            wait(lambda: window.pane and window.pane.document.info, "The PDF did not open")
            pane = window.pane
            view = pane.view
            remap_by_position(view)

            doc = pane.document
            warm = []
            doc.quick_request("quick_text", (1,), lambda value, error: warm.append(error))
            wait(lambda: warm, "The quick worker did not start")
            assert warm == [None], warm

            # Hold back the classified lines, as a page with a heavy figure would, so that only the quick ones
            # can arrive.
            held, real = [], doc.request

            def request(name, args, callback, **options):
                if name == "text":
                    held.append((name, args, callback, options))
                else:
                    real(name, args, callback, **options)

            doc.request = request
            view.reset_text()
            view.load_text(0, lambda _: None, interactive=True)
            wait(lambda: 0 in view.text_cache, "No text arrived")
            assert 0 in view.provisional and 0 in view.text_pending, "The quick lines should come first"
            assert all(line.kind == "" for line in view.text_cache[0])
            quick = view.text_cache[0]
            row = next(i for i, line in enumerate(quick) if line.text.startswith("Upper"))
            view.selection_range = (0, row, 0), (0, row, len(quick[row].glyphs) - 1)
            assert view.selection_pending()
            choices = []
            view.resolve_selection(choices.extend)
            QTest.qWait(150)
            assert not choices and 0 in view.provisional, "The copy must wait for the classified lines"
            doc.request = real
            for name, args, callback, options in held:
                real(name, args, callback, **options)
            wait(lambda: choices, "The selection did not resolve")
            assert 0 not in view.provisional and not view.selection_pending()
            final_row = next(i for i, line in enumerate(view.text_cache[0]) if line.text.startswith("Upper"))
            assert final_row != row, "The fixture should number the lines differently"
            assert view.selection_range[0][1] == final_row, view.selection_range
            assert [choice.source for choice in choices] == ["Upper line comes second in the file."] * 2
            print("PASS quick lines before classification, selection carried over, copy waits for the final lines")
        finally:
            control.shutdown()
            app.processEvents()


if __name__ == "__main__":
    freeze_support()
    main()
