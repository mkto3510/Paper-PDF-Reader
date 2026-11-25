"""Regression checks for drafts, manual notes, and malformed translation replies."""

from collections import deque
from contextlib import redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from multiprocessing import freeze_support
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Thread
import sys

import pymupdf as mu
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_chrome_notes import wait
from check_reader import replace_external_file
from paper_reader.application import Application
from paper_reader.dialogs import TranslationDialog
from paper_reader.messages import PAIRS, TO_ENGLISH, TO_JAPANESE, localize


class Replies(BaseHTTPRequestHandler):
    """Queued replies: bytes are a 200 JSON body; (status, body, headers) is any other reply."""
    queue = deque()
    methods = []

    def reply(self):
        self.methods.append(self.command)
        item = self.queue.popleft()
        status, body, headers = item if isinstance(item, tuple) else (200, item, {})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.reply()

    def do_GET(self):
        self.reply()

    def log_message(self, *_):
        pass


class Elsewhere(BaseHTTPRequestHandler):
    """Another origin. A redirect must never reach it."""
    hits = []

    def do_POST(self):
        self.hits.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()

    do_GET = do_POST

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


def check_service_errors(manager, profile, settings, other_port):
    """HTTP errors name the service's reason, and redirects stay on the same origin."""
    base = profile["endpoint"]
    profile = profile | {"apiKey": "secret"}
    manager.limit = 1

    def run(*queued):
        Replies.queue.clear()
        Replies.queue.extend(queued)
        job = manager.create(profile, "Source", settings)
        manager.enqueue(job)
        wait(lambda: job.state in ("finished", "failed"), "The translation did not end")
        return job

    job = run((400, b'{"error":{"message":"model not found"}}', {}))
    assert job.state == "failed" and job.error == "HTTP 400: model not found", job.error
    assert run((500, b"oops", {})).error == "HTTP 500"
    assert run((502, b'{"detail":"upstream down"}', {})).error == "HTTP 502: upstream down"
    for status in (301, 302, 303, 307, 308):
        Replies.methods.clear()
        job = run((status, b"", {"Location": "/v1/chat/completions"}),
                  b'{"choices":[{"message":{"content":"followed"}}]}')
        assert job.state == "finished" and job.output == "followed", (status, job.state, job.error)
        assert Replies.methods == ["POST", "POST"], (status, Replies.methods)
    # A redirect loop stops instead of running forever.
    job = run(*[(307, b"", {"Location": "/v1/chat/completions"})] * 6)
    assert job.state == "failed" and job.error == "HTTP 307", (job.state, job.error)
    Elsewhere.hits.clear()
    job = run((307, b"", {"Location": f"http://127.0.0.1:{other_port}/v1/chat/completions"}))
    wait(lambda: job.state == "failed", "A cross-origin redirect was not refused")
    QTest.qWait(150)
    assert not Elsewhere.hits, f"The API key or text reached another origin: {Elsewhere.hits}"
    assert job.error.startswith("HTTP 307"), job.error

    answers = []
    Replies.queue.clear()
    Replies.queue.append((401, b'{"error":"bad key"}', {}))
    manager.models(profile, lambda names, error: answers.append((names, error)))
    wait(lambda: answers, "The model list did not answer")
    assert answers == [(None, "HTTP 401: bad key")], answers
    answers.clear()
    Replies.queue.append(b'{"data":[{"id":"m1"},{"id":"m2"}]}')
    manager.models(profile, lambda names, error: answers.append((names, error)))
    wait(lambda: answers, "The model list did not answer")
    assert answers == [(["m1", "m2"], None)], answers
    answers.clear()
    Replies.queue.append(b'{"data":[]}')
    abandon = manager.models(profile, lambda names, error: answers.append((names, error)))
    abandon()
    QTest.qWait(300)
    assert not answers, "An abandoned model request still called back"
    Replies.queue.clear()


def check_messages():
    """Fixed messages convert both ways, and the code keeps one spelling for shared terms."""
    for japanese, english in PAIRS:
        assert localize("en", japanese) == english == localize("en", english)
        assert localize("ja", english) == japanese == localize("ja", japanese)
        assert all(ord(char) < 0x3000 for char in english), english
    assert localize("en", "HTTP 400: model not found") == "HTTP 400: model not found"
    sources = {path.name: path.read_text(encoding="utf-8")
               for path in (Path(__file__).resolve().parents[1] / "paper_reader").glob("*.py")}
    # Every fixed message raised by the PDF worker, the session and the translation client has a pair.
    known = set(TO_ENGLISH) | set(TO_JAPANESE)
    for name, pattern in (("pdf.py", r'raise ValueError\("([^"]+)"\)'), ("translation.py", r'raise ValueError\("([^"]+)"\)'),
                          ("session.py", r'callback\(None, "([^"]+)"\)'), ("session.py", r'_call\(callback, None, "([^"]+)"\)'),
                          ("session.py", r'message = "([^"]+)"'), ("translation.py", r'error = error or "([^"]+)"')):
        for message in re.findall(pattern, sources[name]):
            assert message in known, f"{name}: {message!r} is missing from messages.PAIRS"
    for name, text in sources.items():
        assert "ウインドウ" not in text, name
        assert "．" not in text, name
        if name not in ("preferences.py", "application.py"):
            assert "Preferences" not in text, name


def check_session_recovery(session):
    """A callback that raises must not stall the requests queued behind it."""
    replies = []

    def broken(*_):
        raise RuntimeError("callback failure")

    errors = StringIO()
    with redirect_stderr(errors):
        session.request("document", (0, "missing", ()), broken)
        session.request("document", (0, "missing", ()), lambda value, error: replies.append((value, error)))
        wait(lambda: replies, "A failing callback stalled the PDF queue")
    assert "callback failure" in errors.getvalue()


def check_text_cache(control, root):
    """Every visible page keeps its text while a selection spans more pages than the old limit."""
    path = root / "pages.pdf"
    with mu.open() as pdf:
        for number in range(60):
            pdf.new_page(width=300, height=300).insert_text((30, 60), f"Page {number} text")
        pdf.save(path)
    window = control.new_window()
    window.open_paths([str(path)])
    wait(lambda: window.pane and window.pane.document.info and not window.pane.document.loading, "Pages did not load")
    window.resize(1100, 1500)
    window.show()
    view = window.pane.view
    view.set_zoom("custom", .08)
    QTest.qWait(200)
    visible = len(view._visible_pages())
    assert visible > 16, visible
    view.select_all()
    wait(lambda: all(page in view.text_cache for page in view._visible_pages()), "Visible pages lost their text")
    QTest.qWait(300)
    assert len(view.text_cache) >= visible
    assert all(page in view.text_cache for page in view._visible_pages())
    window.close_tabs([0])


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
    elsewhere = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    Thread(target=server.serve_forever, daemon=True).start()
    Thread(target=elsewhere.serve_forever, daemon=True).start()
    try:
        with TemporaryDirectory() as folder:
            root = Path(folder)
            control = Application(app, root)
            control.preferences.data["autoSave"] = False
            profile = control.preferences.data["profiles"]["default"] | {
                "endpoint": f"http://127.0.0.1:{server.server_port}", "stream": False}
            try:
                check_responses(control.documents.translations, profile, control.preferences.data)
                check_service_errors(control.documents.translations, profile, control.preferences.data,
                                     elsewhere.server_port)
                check_messages()
                check_session_recovery(control.documents.session)
                check_text_cache(control, root)
                check_comments(control, root, profile)
            finally:
                control.shutdown()
    finally:
        for running in (server, elsewhere):
            running.shutdown()
            running.server_close()
    print("PASS malformed-response queue recovery, service errors, session recovery, text cache, "
          "translated-comment replacement, and cross-window draft protection")


if __name__ == "__main__":
    freeze_support()
    main()
