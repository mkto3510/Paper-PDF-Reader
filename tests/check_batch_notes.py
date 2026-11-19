"""Exercise sidebar multi-selection and all three bulk actions on a saved PDF."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

import pymupdf as mu
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from check_chrome_notes import wait
from paper_reader.application import Application


class Service(BaseHTTPRequestHandler):
    sources = []

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        source = payload["messages"][-1]["content"]
        self.sources.append(source)
        body = json.dumps({"choices": [{"message": {"content": f"Translated {source}"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def exercise(app, root, endpoint):
    path = root / "bulk.pdf"
    with mu.open() as pdf:
        page = pdf.new_page()
        for index in range(5):
            source = f"Paragraph {index}."
            page.insert_text((60, 80 + index * 30), source)
            highlight = page.add_highlight_annot(page.search_for(source))
            highlight.set_info(content=f"Original {index}")
            highlight.update()
        pdf.save(path)

    control = Application(app, root)
    settings = control.preferences.data
    settings["autoSave"] = False
    settings["profiles"][settings["activeProfile"]].update(endpoint=endpoint, stream=False)
    try:
        window = control.new_window()
        window.open_paths([str(path)])
        wait(lambda: window.pane and len(window.pane.document.annotations.get(0, {})) == 5, "Notes did not load")
        pane = window.pane
        pane.show_sidebar("notes", True)
        panel, doc = pane.notes, pane.document
        a, b, c, d, e = panel.keys

        def click(key, modifier=Qt.KeyboardModifier.NoModifier):
            panel.reveal(key)
            QTest.mouseClick(panel.cards[key].heading, Qt.MouseButton.LeftButton, modifier)
            app.processEvents()

        click(a)
        assert panel.cards[a].editing
        panel.cards[a].editor.setPlainText("Draft kept")
        panel.cards[a].editor.document().setModified(True)
        click(c, Qt.KeyboardModifier.ShiftModifier)
        assert panel.multiple == {a, b, c}
        wait(lambda: doc.get_note(a)["comment"] == "Draft kept", "Entering multi-selection lost the draft")
        click(b, Qt.KeyboardModifier.ControlModifier)
        click(e, Qt.KeyboardModifier.ControlModifier)
        assert panel.multiple == {a, c, e} and pane.view.selected_note is None
        assert panel.bulk_actions.isVisible()
        panel.reveal(d)
        assert panel.multiple == {a, c, e}

        red = "#ff6666"
        menu = panel.bulk_color.menu()
        panel.show_bulk_colors(menu)
        next(action for action in menu.actions() if action.text() == pane.t("赤", "Red")).trigger()
        wait(lambda: all(doc.get_note(key)["color"] == red for key in (a, c, e)), "Bulk color did not apply")
        assert all(doc.get_note(key)["color"] != red for key in (b, d))
        panel.set_filter(red, True)
        assert panel.multiple == {a, c, e}
        panel.reset_filter()

        # Two chosen rows belong to one grouped highlight. Translate it only once.
        doc.groups[c] = doc.groups[e] = (c, e)
        panel.bulk_translate.click()
        panel.bulk_translate.click()
        expected = ["Paragraph 0.", "Paragraph 2.\nParagraph 4."]
        wait(lambda: all(doc.get_note(key)["comment"].startswith("Translated") for key in (a, c, e)),
             "Translations did not reach the original annotations")
        assert Counter(Service.sources) == Counter(expected)
        assert doc.get_note(c)["comment"] == doc.get_note(e)["comment"] == f"Translated {expected[1]}"
        assert doc.get_note(b)["comment"] == "Original 1"

        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            panel.bulk_delete.click()
        assert panel.multiple == {a, c, e}
        # Filter changes must deselect hidden annotations before destructive actions.
        panel.set_filter(red, True)
        panel.set_filter(doc.get_note(b)["color"], True)
        panel.set_filter(red, False)
        assert not panel.multiple
        panel.reset_filter()
        click(a)
        click(c, Qt.KeyboardModifier.ControlModifier)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            QTest.keyClick(panel.cards[c], Qt.Key.Key_Delete)
        wait(lambda: not any(doc.get_note(key) for key in (a, c, e)), "Bulk Delete did not remove the selected groups")
        assert not panel.multiple and not doc.jobs
        saved = []
        doc.save(callback=saved.append)
        wait(lambda: saved, "The edited PDF was not saved")
        assert saved == [True]
        with mu.open(path) as result:
            assert [annot.info["content"] for annot in result[0].annots()] == ["Original 1", "Original 3"]
    finally:
        control.shutdown()


if __name__ == "__main__":
    freeze_support()
    app = QApplication([])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Service)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as folder:
            exercise(app, Path(folder), f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
    print("PASS sidebar multi-selection, bulk color, translation, and deletion")
