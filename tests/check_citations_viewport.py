"""Regression checks for citation navigation and a stable PDF viewport."""

from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pymupdf as mu
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from check_chrome_notes import wait
from paper_reader import APP_NAME
from paper_reader.application import Application
from paper_reader.citations import citation_spans, normalize
from paper_reader.pdf import PdfEngine


def make_pdf(path):
    with mu.open() as pdf:
        pdf.new_page(width=600, height=700)
        pdf.new_page(width=600, height=700)
        pdf.new_page(width=600, height=700)
        pdf.new_page(width=600, height=700)
        body = pdf[0]
        for y, text in (
            (65, "A comparison [12] with the earlier results."),
            (86, "See [3, 5] and [3-5] for more."),
            (106, "Prior work (Smith, 2024a) and Smith et al. (2024b) disagree."),
            (126, "Prior work (Jones, 2021) remains ambiguous."),
            (146, "This citation [99] has no matching entry."),
            (166, "The original PDF link [12] comes first."),
        ):
            body.insert_text((60, y), text)
        native = body.search_for("[12]")[1]
        body.insert_link({"kind": mu.LINK_GOTO, "from": native, "page": 1, "to": mu.Point(60, 0)})
        reference = pdf[2]
        reference.set_rotation(90)
        for y, text in (
            (65, "References"),
            (105, "[3] Lee, K. (2022). The first paper."),
            (125, "[4] Rao, T. (2023). The second paper."),
            (145, "[5] Ito, M. (2024). The third paper."),
            (165, "[12] Smith, J. (2024a). The fourth paper."),
            (205, "Jones, S. (2021). Duplicate surname and year."),
        ):
            reference.insert_text((60, y), text)
        for y, text in (
            (105, "Smith, J. (2024b). The fifth paper."),
            (125, "Jones, T. (2021). Same name and year."),
            (145, "Kim, H. (2025). The sixth paper."),
        ):
            reference.insert_text((320, y), text)
        appendix = pdf[3]
        appendix.insert_text((60, 65), "Appendix A")
        appendix.insert_text((60, 85), "[99] Entry-shaped text from the appendix.")
        pdf.save(path)


def point(pdf, text, occurrence=0):
    box = pdf[0].search_for(text)[occurrence]
    return box.tl + (box.br - box.tl) / 2


def check_pdf(path):
    assert next(c.keys for _, c in citation_spans(normalize("See ［３，５–６］."))) == (3, 5, 6)
    assert next(c.keys for _, c in citation_spans(normalize("(García-López, 2022; Smith, 2024a)"))) == (
        ("garcía-lópez", "2022"), ("smith", "2024a"))
    assert list(citation_spans("[50–3] and [1–99]")) == []
    engine = PdfEngine(path.parent)
    with mu.open(path) as fixture:
        try:
            engine.open(str(path))
            find = lambda text: (0, *point(fixture, text))
            citation = engine.citation_at(*find("[12]"))
            assert citation.keys == (12,) and engine.reference_targets(find("[12]"), citation.keys) == ()
            assert engine.reference_step() is False
            while not engine.reference_step():
                pass
            entry, = engine.reference_targets(find("[12]"), citation.keys)
            assert entry.page == 2 and entry.label.startswith("[12]")
            for phrase, expected in (
                ("[3-5]", ("[3]", "[4]", "[5]")),
                ("(Smith, 2024a)", ("[12]",)),
                ("et al. (2024b)", ("Smith",)),
                ("(Jones, 2021)", ()),
                ("[99]", ()),
            ):
                position = find(phrase)
                keys = engine.citation_at(*position).keys
                assert tuple(item.label.split()[0].rstrip(",") for item in engine.reference_targets(position, keys)) == expected
        finally:
            engine.close()


def check_window(app, root, path):
    control = Application(app, root)
    control.preferences.data.update(language="en", autoSave=False)
    window = control.new_window()
    try:
        window.open_paths([str(path)])
        wait(lambda: window.pane and window.pane.view.pages, "PDF did not open")
        pane = window.pane
        view, doc = pane.view, pane.document
        window.resize(950, 710)
        view.set_layout(continuous=False)
        view.set_zoom("fit")
        QTest.qWait(80)

        def geometry():
            return view.viewport().size(), view.zoom, view.location()

        for mode in ("fit", "width", "content", "custom"):
            view.set_zoom(mode, 1.3 if mode == "custom" else None)
            if mode == "content":
                wait(lambda: view.content_box, "Content bounds did not load")
            QTest.qWait(30)
            original = geometry()
            assert window.windowTitle() == APP_NAME
            window.show_message("Translation failed. " * 20)
            app.processEvents()
            assert window.chrome.notice.isVisible() and window.chrome.notice.height() > 0
            assert geometry() == original, mode
            window.chrome.clear_notice()
            for revision, saving, label in ((1, 0, "Unsaved changes"), (1, 1, "Saving…"), (0, 0, "")):
                doc.revision, doc.saving = revision, saving
                doc.changed.emit()
                app.processEvents()
                assert geometry() == original, mode
                assert window.windowTitle() == (f"{APP_NAME} — {label}" if label else APP_NAME)

        view.restore((0, 300, 350))
        width = view.viewport().width()
        pane.show_sidebar("outline", False)
        pane.show_sidebar("notes", False)
        wait(lambda: view.viewport().width() > width, "Sidebar did not close")
        QTest.qWait(35)
        before, width = view.location(), view.viewport().width()
        pane.show_sidebar("notes", True)
        wait(lambda: view.viewport().width() < width, "Sidebar did not open")
        QTest.qWait(35)
        assert view.zoom == 1.3 and view.location()[0] == before[0]
        assert all(abs(a - b) <= 2 for a, b in zip(view.location()[1:], before[1:]))
        view.set_zoom("fit")
        view.go_to(0)
        QTest.qWait(30)

        with mu.open(path) as fixture:
            cite_point = view._project(0, tuple(point(fixture, "[12]"))).toPoint()
            link_point = view._project(0, tuple(point(fixture, "[12]", 1))).toPoint()
            group_point = view._project(0, tuple(point(fixture, "[3-5]"))).toPoint()

        request = doc.request
        delayed = []

        def intercept(method, args, callback, **kwargs):
            if method == "reference_step":
                assert kwargs["priority"] > 7
                delayed.append((method, args, callback, kwargs))
            else:
                request(method, args, callback, **kwargs)

        with patch.object(doc, "request", side_effect=intercept):
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=cite_point)
            wait(lambda: delayed, "Citation did not begin scanning")
            view.go_to(1)
        method, args, callback, kwargs = delayed.pop()
        request(method, args, callback, **kwargs)
        wait(lambda: doc.reference_scan is None, "Old citation scan did not settle")
        assert view.current_page() == 1, "A delayed lookup changed the page"

        view.go_to(0)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=cite_point)
        wait(lambda: view.current_page() == 2, "Citation did not reach the bibliography")
        pane.back_forward(-1)
        assert view.current_page() == 0
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=group_point)
        wait(lambda: app.activePopupWidget(), "A multiple citation did not offer reference choices")
        menu = app.activePopupWidget()
        assert [action.text().split()[0] for action in menu.actions()] == ["[3]", "[4]", "[5]"]
        menu.actions()[1].trigger()
        menu.close()
        assert view.current_page() == 2
        pane.back_forward(-1)
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=link_point)
        wait(lambda: view.current_page() == 1, "Citation overrode an embedded PDF link")
    finally:
        control.shutdown()


if __name__ == "__main__":
    freeze_support()
    app = QApplication([])
    with TemporaryDirectory() as folder:
        root = Path(folder)
        path = root / "citations.pdf"
        make_pdf(path)
        check_pdf(path)
        check_window(app, root, path)
    print("PASS citations, stale lookup, navigation history, and stable PDF viewport")
