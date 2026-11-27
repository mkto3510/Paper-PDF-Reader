"""Regression checks for citation navigation and a stable PDF viewport."""

from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pymupdf as mu
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from check_chrome_notes import wait
from paper_reader import APP_NAME
from paper_reader.application import Application
from paper_reader.font_ui import AreaMenu
from paper_reader.citations import Citation, citation_spans, normalize
from paper_reader.pdf import PdfEngine
from paper_reader.pane import HOVER_PRIORITY


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
            (189, "An external link goes to a website."),
            (211, "Ordinary highlighted prose."),
        ):
            body.insert_text((60, y), text)
        native = body.search_for("[12]")[1]
        body.insert_link({"kind": mu.LINK_GOTO, "from": native, "page": 1, "to": mu.Point(60, 0)})
        ext = body.search_for("external link")[0]
        body.insert_link({"kind": mu.LINK_URI, "from": ext, "uri": "https://example.org/reference"})
        for phrase in ("[12]", "[3-5]", "[99]", "external link", "Ordinary highlighted prose."):
            for rect in body.search_for(phrase):
                body.add_highlight_annot(rect)
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
            (105, "Smith, J., and Lee, K. (2024b). The fifth paper."),
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
            title = f"{doc.path.name} — {APP_NAME}"
            assert window.windowTitle() == title
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
                assert window.windowTitle() == (f"{title} — {label}" if label else title)

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
        view.set_zoom("custom", 1.3)
        view.go_to(0)
        QTest.qWait(30)

        with mu.open(path) as fixture:
            positions = [tuple(point(fixture, "[12]")), tuple(point(fixture, "[12]", 1)),
                         tuple(point(fixture, "[3-5]"))]
        cite_point = view._project(0, positions[0]).toPoint()
        wait(lambda: len(doc.annotations.get(0, {})) == 6, "Highlight fixtures did not load")
        QTest.mouseMove(view.viewport(), cite_point)
        wait(lambda: view.link_hover is not None, "No hover mark on inferred citation")
        assert view.link_hover[0] == 0 and not view.link_hover[2]
        assert view.viewport().cursor().shape() == Qt.CursorShape.IBeamCursor
        assert "look up" in view.viewport().toolTip()
        assert not doc.reference_scans and not doc.references_ready

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
            active = delayed[-1][3]["needed"]
            scrollbar = view.verticalScrollBar()
            original_value = scrollbar.value()
            assert active() and scrollbar.maximum() > original_value
            scrollbar.setValue(original_value + 20)
            scrollbar.setValue(original_value)
            assert not active(), "Scrolling away and back revived a pending citation"
            view.go_to(1)
        method, args, callback, kwargs = delayed.pop()
        request(method, args, callback, **kwargs)
        wait(lambda: doc.reference_scan is None, "Old citation scan did not settle")
        assert view.current_page() == 1, "A delayed lookup changed the page"

        view.set_zoom("fit")
        view.go_to(0)
        QTest.qWait(80)
        cite_point, link_point, group_point = [view._project(0, item).toPoint() for item in positions]
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=cite_point)
        try:
            wait(lambda: view.current_page() == 2, "Citation did not reach the bibliography")
        except AssertionError:
            print("citation diagnostics", view.current_page(), pane.lookup, doc.references_ready, view.viewport_revision,
                  view._position(cite_point), view.selected_note, view.click_series)
            raise
        assert view.selected_note is None and window.chrome.buttons["back"].isEnabled()
        QTest.mouseClick(window.chrome.buttons["back"], Qt.MouseButton.LeftButton)
        assert view.current_page() == 0 and not window.chrome.buttons["back"].isEnabled()
        QTest.keyClick(view, Qt.Key.Key_Right, Qt.KeyboardModifier.AltModifier)
        wait(lambda: view.current_page() == 2, "Forward shortcut did not restore the target")
        QTest.keyClick(view, Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
        wait(lambda: view.current_page() == 0, "Back shortcut did not restore the source")
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=group_point)
        wait(lambda: app.activePopupWidget(), "A multiple citation did not offer reference choices")
        menu = app.activePopupWidget()
        assert [action.text().split()[0] for action in menu.actions()] == ["[3]", "[4]", "[5]"]
        menu.actions()[1].trigger()
        menu.close()
        assert view.current_page() == 2
        pane.back_forward(-1)
        QTest.mouseMove(view.viewport(), link_point)
        wait(lambda: view.link_hover is not None and view.link_hover[2], "No hover mark on native link")
        assert view.viewport().cursor().shape() == Qt.CursorShape.PointingHandCursor
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=link_point)
        wait(lambda: view.current_page() == 1, "Citation overrode a highlighted embedded PDF link")
        assert view.link_hover is None
        window.chrome.buttons["back"].click()
        with mu.open(path) as fixture:
            external, missing, prose = [tuple(point(fixture, text)) for text in
                                        ("external link", "[99]", "Ordinary highlighted prose.")]
        with patch("paper_reader.pane.QDesktopServices.openUrl", return_value=True) as open_url:
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view._project(0, external).toPoint())
            wait(lambda: open_url.called, "Highlighted HTTP link did not open")
            assert open_url.call_args.args[0].toString() == "https://example.org/reference"
        assert view.selected_note is None
        missing_key = pane.hit_annotation((0, *missing))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view._project(0, missing).toPoint())
        wait(lambda: view.selected_note == missing_key, "Unmatched citation blocked annotation selection")
        prose_key = pane.hit_annotation((0, *prose))
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view._project(0, prose).toPoint())
        wait(lambda: view.selected_note == prose_key, "Plain highlight could not be selected")
        assert missing_key and prose_key and missing_key != prose_key

        # A context menu or hover that waited for the page links must not outlive the input that asked for it.
        page_links = []

        def delay_links(method, args, callback, **kwargs):
            if method == "page_links":
                page_links.append(callback)
            else:
                request(method, args, callback, **kwargs)

        menus = []

        class RecordingMenu(AreaMenu):
            def exec(self, *args):
                menus.append(args)

        prose_point = view._project(0, prose).toPoint()
        with patch.object(doc, "request", side_effect=delay_links), \
                patch("paper_reader.pane.AreaMenu", RecordingMenu):
            pane.page_links.cache.pop(0, None)
            pane.context_menu(view.viewport().mapToGlobal(prose_point), (0, *prose))
            assert len(page_links) == 1 and not menus
            view.input_revision += 1
            page_links.pop()([], None)
            assert not menus, "A stale link lookup still opened the context menu"
            pane.page_links.cache.pop(0, None)
            pane.context_menu(view.viewport().mapToGlobal(prose_point), (0, *prose))
            assert len(page_links) == 1
            page_links.pop()([], None)
            assert len(menus) == 1, "The current link lookup did not open the context menu"

            # Scrolling alone must not discard a right-click that is waiting for the links.
            pane.page_links.cache.pop(0, None)
            pane.context_menu(view.viewport().mapToGlobal(prose_point), (0, *prose))
            scrollbar = view.verticalScrollBar()
            scrollbar.setValue(scrollbar.value() + 20)
            scrollbar.setValue(scrollbar.value() - 20)
            page_links.pop()([], None)
            assert len(menus) == 2, "Scrolling discarded a pending context menu"

            # A stale hover and a current right-click share one link read: the hover is cancelled, the menu is not.
            pane.page_links.cache.pop(0, None)
            QTest.mouseMove(view.viewport(), prose_point)
            wait(lambda: page_links, "Hover did not request the page links")
            assert pane.hover_request is not None
            pane.context_menu(view.viewport().mapToGlobal(prose_point), (0, *prose))
            assert len(page_links) == 1, "A second subscriber started another link read"
            app.sendEvent(view.viewport(), QEvent(QEvent.Type.Leave))
            with patch.object(pane.page_links, "at", wraps=pane.page_links.at) as link_at:
                page_links.pop()([], None)
                assert pane.hover_request is None, "A cancelled link lookup left the hover request pending"
                assert len(menus) == 3, "A stale sibling blocked the current context menu"
                assert link_at.call_count == 1, "A stale hover still received the link result"
        pane.page_links.cache.pop(0, None)

        delayed_hover = []

        def delay_hover(method, args, callback, **kwargs):
            if method == "citation_at" and kwargs["priority"] == HOVER_PRIORITY:
                delayed_hover.append((callback, kwargs["needed"]))
            else:
                request(method, args, callback, **kwargs)

        with patch.object(doc, "request", side_effect=delay_hover):
            cite_point = view._project(0, positions[0]).toPoint()
            QTest.mouseMove(view.viewport(), cite_point)
            wait(lambda: len(delayed_hover) == 1, "Hover lookup did not start")
            for shift in range(1, 20):
                QTest.mouseMove(view.viewport(), cite_point + type(cite_point)(shift, 1))
            QTest.qWait(160)
            assert len(delayed_hover) == 1 and not delayed_hover[0][1]()
            app.sendEvent(view.viewport(), QEvent(QEvent.Type.Leave))
            delayed_hover[0][0](Citation("[12]", (12,), box=(1, 1, 10, 10)), None)
            assert view.link_hover is None

        view.set_layout("spread")
        view.set_zoom("custom", .85)
        window.rotate(90)
        view.go_to(0)
        QTest.mouseMove(view.viewport(), view._project(0, positions[1]).toPoint())
        wait(lambda: view.link_hover is not None and view.link_hover[2], "Native hover was displaced by rotation")
        window.duplicate(False)
        wait(lambda: window.pane is not pane, "A separate same-document tab did not open")
        assert view.link_hover is None and not window.chrome.buttons["back"].isEnabled()

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
