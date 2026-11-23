"""Regression checks for drafts, manual notes, and malformed translation replies."""

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import freeze_support
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import sys

import pymupdf as mu
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_chrome_notes import wait
from check_reader import replace_external_file
from paper_reader.application import Application
from paper_reader.dialogs import TranslationDialog


class Replies(BaseHTTPRequestHandler):
    queue = deque()

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = self.queue.popleft()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def make_pdf(path, title):
    with mu.open() as pdf:
        page = pdf.new_page()
        page.insert_text((55, 140), "Original highlight with an editable comment.")
        highlight = page.add_highlight_annot(page.search_for("Original highlight"))
        highlight.set_info(content="Original comment")
        highlight.update()
        pdf.set_metadata({"title": title})
        pdf.save(path)


def check_responses(manager, profile, settings):
    manager.limit = 1
    good = b'{"choices":[{"message":{"content":"next job completed"}}]}'
    for malformed in (b'{"choices":[{"message":null}]}', b'{"choices":[null]}'):
        Replies.queue.extend((malformed, good))
        first = manager.create(profile, "First", settings)
        second = manager.create(profile, "Second", settings)
        manager.enqueue(first)
        manager.enqueue(second)
        assert second.state == "waiting"
        wait(lambda: second.state == "finished", "Malformed reply stopped the translation queue")
        assert first.state == "failed" and first.error
        assert second.output == "next job completed"


def check_comments(control, root, profile):
    source = root / "original.pdf"
    make_pdf(source, "Original")
    window = control.new_window()
    window.open_paths([str(source)])
    wait(lambda: window.pane and window.pane.document.annotations.get(0), "Original annotation did not load")
    pane = window.pane
    doc = pane.document
    key = next(iter(doc.annotations[0]))
    manager, settings = control.documents.translations, control.preferences.data
    existing = manager.create(profile, "Prior translation", settings)
    existing.set_state("running")
    doc.attach_job((key,), existing)
    dialog = TranslationDialog(pane, key=key, source="Text", comment="Original comment")
    dialog.output.setPlainText("Manual comment")
    dialog.accept_annotation()
    assert existing.state == "canceled" and not doc.jobs
    existing.set_state("finished", "Late translation")
    wait(lambda: doc.get_note(key)["comment"] == "Manual comment", "Manual comment was overwritten")
    saved = []
    doc.save(callback=saved.append)
    wait(lambda: saved, "The fixture comment was not saved")
    assert saved == [True] and not doc.dirty

    window.duplicate(True)
    second = control.windows[-1].pane
    second.pick_note(key)
    wait(lambda: key in second.notes.cards, "Duplicate window did not show the selected note")
    editor = second.notes.cards[key].editor
    editor.selectAll()
    editor.insertPlainText("Uncommitted draft in another window")
    assert not doc.dirty and second.notes.has_drafts()
    replacement = root / "replacement.pdf"
    make_pdf(replacement, "External change")
    generation = doc.generation
    replace_external_file(replacement, source, wait)
    wait(lambda: doc.external, "External changes did not protect the editor draft")
    assert doc.generation == generation and not doc.loading
    assert editor.toPlainText() == "Uncommitted draft in another window"


def main():
    app = QApplication([])
    server = ThreadingHTTPServer(("127.0.0.1", 0), Replies)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            control = Application(app, root)
            control.preferences.data["autoSave"] = False
            profile = control.preferences.data["profiles"]["default"] | {
                "endpoint": f"http://127.0.0.1:{server.server_port}", "stream": False}
            try:
                check_responses(control.documents.translations, profile, control.preferences.data)
                check_comments(control, root, profile)
            finally:
                control.shutdown()
    finally:
        server.shutdown()
        server.server_close()
    print("PASS malformed-response queue recovery, translated-comment replacement, and cross-window draft protection")


if __name__ == "__main__":
    freeze_support()
    main()
