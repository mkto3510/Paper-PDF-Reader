"""Run the Qt and MuPDF release regression checks on Windows."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from multiprocessing import freeze_support
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import sleep
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class LocalTranslation(BaseHTTPRequestHandler):
    requests = []
    fail_next = False

    def do_GET(self):
        assert self.path == "/v1/models"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[{"id":"test-model"}]}')

    def do_POST(self):
        assert self.path == "/v1/chat/completions"
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append(payload)
        if LocalTranslation.fail_next:
            LocalTranslation.fail_next = False
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"Fixture failed"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for part in ("試験用の", "翻訳"):
                stream = {"choices": [{"delta": {"content": part}}]}
                self.wfile.write(("data: " + json.dumps(stream, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                sleep(.45)
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass


def make_pdf(path):
    import pymupdf as mu

    with mu.open() as doc:
        for page_no in range(6):
            page = doc.new_page(width=595, height=842)
            page.insert_text((46, 36), "Journal of testing")
            page.insert_text((46, 150), f"café  {page_no + 1}  Included body")
            page.insert_text((46, 170), "DOCUMENT TEXT")
            page.insert_text((46, 196), "Marked artifact - omit from translations")
            stream = page.get_contents()[-1]
            doc.update_stream(stream, b"/Artifact BMC\n" + doc.xref_stream(stream) + b"\nEMC\n")
            page.insert_text((46, 220), "Text after artifact")
            page.insert_text((46, 260), "inter-")
            page.insert_text((46, 275), "national research and short-")
            page.insert_text((46, 290), "wavelength search")
        doc.set_toc([(1, "First page", 1), (2, "Third page", 3)])
        page = doc[0]
        page.insert_link({"kind": mu.LINK_GOTO, "from": mu.Rect(42, 135, 98, 155), "page": 4, "to": mu.Point(0, 130)})
        doc.save(path)


def replace_external_file(source, target, wait):
    def attempt():
        try:
            os.replace(source, target)
        except PermissionError as error:
            if getattr(error, "winerror", None) not in (5, 32, 33):
                raise
            return False
        return True

    wait(attempt, f"The external file {target.name} remained locked")


def main():
    import pymupdf as mu
    from PySide6.QtCore import QLockFile, QPoint, QPointF, QTimer, Qt
    from PySide6.QtGui import QTextCursor, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QStatusBar

    from paper_reader.application import Application
    from paper_reader.pdf import PdfEngine

    app = QApplication([])
    failures = []
    sys.excepthook = lambda kind, error, trace: failures.append(error)

    def wait(predicate, description):
        for _ in range(250):
            if failures:
                raise failures[0]
            if predicate():
                return
            QTest.qWait(30)
        raise AssertionError(description)

    server = ThreadingHTTPServer(("127.0.0.1", 0), LocalTranslation)
    Thread(target=server.serve_forever, daemon=True).start()
    with TemporaryDirectory() as folder:
        root = Path(folder).resolve()
        path = root / "試験用.pdf"
        make_pdf(path)
        engine = PdfEngine()
        try:
            engine.open(str(path))
            result, full = engine.selection_page(0, (0, 0, 0), (0, 100, 100))
            assert "Marked artifact" in full.source and "Marked artifact" not in result.source
            assert "artifact" in result.skipped
            assert engine.search(0, "CAFE", False, True, False)
            assert not engine.search(0, "cafe", True, True, True)
            assert not engine.search(0, "text", True, False, False)
            assert len(engine.search(0, "international", False, True, False)[0]) == 2
            assert len(engine.search(0, "short-wavelength", False, True, False)[0]) == 2
            assert engine.search(0, "Marked artifact", False, False, False)
            rotated = engine.render(0, 1.0, (0, 0, 500, 500), 90, True)
            assert rotated[:2] == (500, 500)
            assert engine.fonts(0)
        finally:
            engine.close()

        old = {"endpoint": f"http://127.0.0.1:{server.server_port}", "model": "test-model",
               "apiKey": "local-test", "systemPrompt": "", "autoSave": True}
        (root / "settings.json").write_text(json.dumps(old), encoding="utf-8")
        controller = Application(app, root)
        try:
            # A settings file from before the font sizes loads with the new defaults and is not rewritten.
            assert [controller.preferences.data[key] for key in ("fontOutline", "fontNotes", "fontTranslation",
                                                                  "fontMenu", "fontDialog")] == [130, 130, 130, 100, 100]
            assert "fontNotes" not in json.loads((root / "settings.json").read_text(encoding="utf-8"))
            window = controller.new_window()
            window.open_paths([str(path)])
            wait(lambda: window.pane and window.pane.view.cache.tiles, "A visible tile was not painted")
            pane = window.pane
            view = pane.view
            doc = pane.document
            profile = controller.preferences.data["profiles"]["default"]
            assert profile["model"] == "test-model"
            models = []
            controller.documents.translations.models(profile, lambda names, error: models.append((names, error)))
            wait(lambda: models, "The translation model list did not load")
            assert models == [(["test-model"], None)]
            assert window.chrome.page.text() == "1" and window.chrome.total.text() == "/ 6"
            assert window.findChild(QStatusBar) is None
            assert window.chrome.notice.isHidden() and window.chrome.external.isHidden()
            window.show_message("A fixture message")
            assert window.chrome.notice.isVisible()
            window.chrome.clear_notice()
            assert window.chrome.notice.isHidden()
            assert pane.outline.topLevelItemCount() == 1

            def wheel(pixels, angle, modifiers=Qt.KeyboardModifier.NoModifier):
                event = QWheelEvent(QPointF(100, 250), QPointF(100, 250), pixels, angle,
                                    Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.NoScrollPhase, False)
                QApplication.sendEvent(view.viewport(), event)

            vertical = view.verticalScrollBar()
            vertical.setValue(200)
            wheel(QPoint(), QPoint(0, -120))
            assert vertical.value() - 200 == 32 * app.wheelScrollLines(), vertical.value()
            start = vertical.value()
            wheel(QPoint(0, -11), QPoint())
            assert vertical.value() - start == 11
            zoom = view.zoom
            wheel(QPoint(), QPoint(0, 120), Qt.KeyboardModifier.ControlModifier)
            assert view.zoom > zoom

            pane.open_find("cafe")
            wait(lambda: len(pane.matches()) == 6 and pane.match_count.text().endswith("/ 6"), "Search did not return six matches")
            pane.options[2].setChecked(True)
            wait(lambda: pane.match_count.text() == "一致なし", "Diacritic matching did not take effect")
            pane.close_search()
            view.set_layout("book", rtl=True, rotation=90)
            pane.navigate(3)
            assert view.rotation == 90 and pane.history_index > 0
            pane.back_forward(-1)
            view.set_layout("single", rtl=False, rotation=0)

            view.go_to(0)
            wheel_position = view._project(0, (70, 145)).toPoint()
            QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=wheel_position)
            wait(lambda: view.current_page() == 4, "Clicking a PDF link did not navigate")
            pane.back_forward(-1)
            assert view.current_page() == 0
            view.setFocus()
            QTest.keyClick(view, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
            assert pane.annotation_dialog is None
            assert view.selection_range[1][0] == 5
            QTest.keyClick(view, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
            wait(lambda: "6 Included body" in app.clipboard().text(), "Ctrl+C did not copy all six pages")

            pane.translate_selection()
            wait(lambda: pane.annotation_dialog, "Explicit translation dialog did not open")
            dialog = pane.annotation_dialog
            assert "Marked artifact" not in dialog.source.toPlainText()
            assert QTest.qWaitForWindowActive(dialog)
            dialog.source.setFocus()
            QTest.keyClick(dialog.source, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            wait(lambda: dialog.job and dialog.job.state == "running" and dialog.job.output,
                 f"Stream did not start (focus: {app.focusWidget()}, job: {dialog.job})")
            dialog.output.setFocus()
            QTest.keyClick(dialog.output, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
            wait(lambda: len(LocalTranslation.requests) == 1 and len(doc.annotations) == 6
                 and doc.saved_revision >= 2 and not doc.busy, "Translation did not finish or save")
            assert len(LocalTranslation.requests) == 1, "The translation request was sent again."
            with mu.open(path) as saved:
                assert {page.first_annot.info["content"] for page in saved} == {"試験用の翻訳"}

            key = next(iter(doc.annotations[0]))
            pane.pick_note(key)
            pane.notes.set_filter("#ff6666", True)
            assert not pane.notes.cards
            pane.notes.reset_filter()
            assert key in pane.notes.cards
            assert view.selected_note == key
            controller.preferences.save(controller.preferences.data | {"language": "en", "opacity": 35})
            assert window.chrome.menus.actions()[0].text() == "File" and abs(view.opacity - .35) < .001

            window.duplicate(True)
            second = controller.windows[-1].pane
            assert second.document is doc and second.view.cache is view.cache
            QTest.qWait(150)
            second.view.go_to(4)
            assert second.view.location() != view.location()
            previous_location = second.view.location()
            generation = doc.generation
            replaced_path = root / "replacement.pdf"
            with mu.open(path) as external:
                external.new_page()
                external.save(replaced_path)
            replace_external_file(replaced_path, path, wait)
            wait(lambda: doc.generation > generation, "External replacement did not reload")
            assert second.view.generation == view.generation and len(view.pages) == 7
            restored = second.view.location()
            assert restored[0] == previous_location[0] and abs(restored[2] - previous_location[2]) < 2, (previous_location, restored, second.view.zoom)

            socket_name = "PaperReaderRegression-" + uuid4().hex
            lock = QLockFile(str(root / "instance.lock"))
            assert lock.tryLock(0)
            controller.listen(socket_name, lock)
            client_code = (
                "import sys; from PySide6.QtCore import QCoreApplication; "
                "from paper_reader.application import forward_to_other; "
                "app = QCoreApplication([]); raise SystemExit(not forward_to_other(sys.argv[1], sys.argv[2:]))"
            )
            with subprocess.Popen([sys.executable, "-B", "-c", client_code, socket_name, str(path)],
                                  cwd=Path(__file__).resolve().parents[1]) as client:
                wait(lambda: client.poll() is not None, "A second launch did not exit")
                assert client.returncode == 0, "A second launch did not receive an acknowledgement"
                assert len(controller.panes()) == 2

            controller.preferences.save(controller.preferences.data | {"autoSave": False})
            wait(lambda: doc.annotations.get(0), "Annotations were not reloaded")
            revised = next(iter(doc.annotations[0]))
            pane.pick_note(revised)
            card = pane.notes.cards[revised]
            card.edit_comment()
            card.editor.selectAll()
            card.editor.insertPlainText("Local edit")
            card.commit_comment()
            wait(lambda: doc.dirty and not doc.busy, "Local annotation edit was not retained")
            generation = doc.generation
            with mu.open(path) as external:
                external.set_metadata(external.metadata | {"title": "Externally updated PDF"})
                external.save(replaced_path)
            replace_external_file(replaced_path, path, wait)
            wait(lambda: doc.external, "Edited document did not report an external update")
            assert doc.generation == generation and doc.dirty
            copy = root / "external-update-copy.pdf"
            result = []
            doc.save(str(copy), result.append)
            wait(lambda: result, "Save a copy did not complete")
            assert result == [True]
            with mu.open(copy) as local, mu.open(path) as original:
                assert local[0].first_annot.info["content"] == "Local edit"
                assert original[0].first_annot.info["content"] == "試験用の翻訳"
                assert original.metadata["title"] == "Externally updated PDF"
            folder = root / "first-folder"
            folder.mkdir()
            named = folder / "同名の長いファイル名でタブ表示を検証する論文.pdf"
            make_pdf(named)
            with patch.object(window, "confirm_switch", return_value=True):
                window.open_paths([str(named)])
                wait(lambda: window.pane.document.path == named and window.pane.view.pages, "Quick translate fixture was not opened")
            quick = window.pane
            quick_doc = quick.document
            quick.view.select_all()
            sent = len(LocalTranslation.requests)
            LocalTranslation.fail_next = True

            def click_direct():
                popup = app.activePopupWidget()
                action = next((item for item in popup.actions() if item.text() == "Translate and add annotation"), None) if popup else None
                menu = action.menu() if action else None
                color = next((item for item in menu.actions() if item.text() == "Blue"), None) if menu else None
                if popup:
                    popup.close()
                if color is None:
                    failures.append(AssertionError("The direct translation color menu is missing"))
                else:
                    color.trigger()

            QTimer.singleShot(30, click_direct)
            QTimer.singleShot(800, lambda: app.activePopupWidget().close() if app.activePopupWidget() else None)
            quick.context_menu(QPoint(120, 190))
            wait(lambda: len(LocalTranslation.requests) == sent + 1 and quick_doc.jobs and
                 next(iter(quick_doc.jobs.values())).state == "failed" and len(quick_doc.annotations) == 6,
                 "Direct translation did not appear on its highlight")
            key = next(iter(quick_doc.groups))
            assert quick.annotation_dialog is None and len(quick_doc.annotations) == 6
            assert quick_doc.get_note(key)["comment"] == ""
            assert quick_doc.get_note(key)["color"] == "#2ea8e5"
            job = next(iter(quick_doc.jobs.values()))
            job.retry()
            wait(lambda: not quick_doc.jobs and quick_doc.get_note(key)["comment"] == "試験用の翻訳",
                 "Retry did not update the original annotation")
            assert len(LocalTranslation.requests) == sent + 2 and len(quick_doc.annotations) == 6

            quick.pick_note(key, focus_card=True)
            card = quick.notes.cards[key]
            assert card.editing and card.editor.isVisible()
            card.editor.setFocus()
            card.editor.moveCursor(QTextCursor.MoveOperation.Start)
            QTest.keyClick(card.editor, Qt.Key.Key_Delete)
            assert card.editor.toPlainText() == "験用の翻訳" and quick_doc.get_note(key)
            card.cancel_edit()
            card.setFocus()
            QTest.keyClick(card, Qt.Key.Key_Delete)
            wait(lambda: not quick_doc.annotations, "Delete on the annotation card did not remove the highlight")

            quick.view.select_all()
            quick.translate_to_note()
            wait(lambda: quick_doc.jobs and next(iter(quick_doc.jobs.values())).state == "running" and quick_doc.annotations,
                 "The direct translation retry fixture did not start")
            key = next(iter(quick_doc.annotations[0]))
            quick.pick_note(key)
            quick.view.setFocus()
            QTest.keyClick(quick.view, Qt.Key.Key_Delete)
            wait(lambda: not quick_doc.jobs and not quick_doc.annotations, "Deleting a translating highlight did not cancel it")

            original_settings = controller.preferences.data
            saved_profile = original_settings["profiles"]["default"]
            controller.preferences.save(original_settings | {"profiles": {"default": saved_profile | {"model": ""}}})
            quick.view.select_all()
            sent = len(LocalTranslation.requests)
            notices = []
            quick.message.connect(notices.append)
            quick.translate_to_note()
            wait(lambda: notices, "Invalid service settings did not produce feedback")
            assert not quick_doc.annotations and len(LocalTranslation.requests) == sent
            controller.preferences.save(original_settings)

            quick.view.select_all()
            quick.translate_to_note()
            wait(lambda: quick_doc.jobs and next(iter(quick_doc.jobs.values())).state == "running"
                 and len(quick_doc.annotations) == 6, "Cancel fixture did not translate")
            next(iter(quick_doc.jobs.values())).cancel()
            assert not quick_doc.jobs and len(quick_doc.annotations) == 6
            assert all(not row["comment"] for notes in quick_doc.annotations.values() for row in notes.values())

            quick.view.select_all()
            quick.translate_to_note()
            wait(lambda: quick_doc.jobs and next(iter(quick_doc.jobs.values())).state == "running",
                 "Reload fixture did not translate")
            before = quick_doc.generation
            quick_doc.load(reload=True)
            wait(lambda: quick_doc.generation > before and not quick_doc.jobs,
                 "Reload did not remove the old direct translation")
            assert not quick_doc.annotations

            folder = root / "second-folder"
            folder.mkdir()
            same_name = folder / named.name
            make_pdf(same_name)
            window.open_paths([str(same_name)])
            wait(lambda: window.pane.document.path == same_name and window.pane.view.pages, "Same-name PDF was not opened")
            target_pane = window.pane
            window.resize(550, 670)
            while window.tabs.count() < 50:
                window.add_tab(quick_doc)
            QTest.qWait(140)
            tab_bar = window.tabs.tabBar()
            assert tab_bar.usesScrollButtons() and tab_bar.count() == 50
            assert 120 <= tab_bar.tabRect(0).width() <= 220
            tab_bar.moveTab(window.tabs.indexOf(target_pane), 1)
            window.tab_list.open_list()
            window.tab_list.query.setText("second-folder")
            assert window.tab_list.matches == [target_pane]
            QTest.keyClick(window.tab_list.query, Qt.Key.Key_Return)
            assert window.pane is target_pane
            assert tab_bar.rect().intersects(tab_bar.tabRect(window.tabs.currentIndex()))
            assert window.chrome.page.isVisible() and window.chrome.zoom.isVisible()
            target_pane.open_find("cafe")
            adjacent = window.tabs.widget(window.tabs.currentIndex() + 1)
            window.showNormal()
            window.activateWindow()
            assert QTest.qWaitForWindowActive(window)
            target_pane.query.setFocus()
            QTest.qWait(100)
            QTest.keyClick(target_pane.query, Qt.Key.Key_Tab, Qt.KeyboardModifier.ControlModifier)
            wait(lambda: window.pane is adjacent, f"Ctrl+Tab did not switch tabs, current {window.tabs.currentIndex()} expected {window.tabs.indexOf(adjacent)}, focus {app.focusWidget()}, active {app.activeWindow()} action {window.actions['next_tab'][0].shortcut().toString()}")
            assert window.chrome.search.currentWidget() is adjacent.search_panel
            assert not adjacent.query.text() and target_pane.query.text() == "cafe"
            window.tabs.setCurrentWidget(target_pane)
            assert window.chrome.search.currentWidget() is target_pane.search_panel
            assert window.close_tabs([window.tabs.indexOf(target_pane)])
            assert window.tabs.count() == 49

            if os.environ.get("PAPER_READER_QA_SCREEN"):
                window.grab().save(os.environ["PAPER_READER_QA_SCREEN"])
            print("PASS direct translation, retry, annotation keyboard deletion and 50-tab picker")
            print("PASS wheel, touchpad, zoom, PDF links, Unicode search, selection, Artifact, shared tabs, model listing, translation shortcuts and adoption, annotations, preferences, IPC, reload and dirty-file protection")
        finally:
            controller.shutdown()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    freeze_support()
    import traceback
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
