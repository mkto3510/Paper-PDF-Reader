"""The reader, translation popup, and portable self-test entry point."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QDockWidget, QFileDialog,
    QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QTextEdit,
    QToolBar, QVBoxLayout,
)

from . import VERSION
from .paths import data_dir, write_json
from .session import PdfSession
from .translation import DEFAULTS, Translations
from .view import PaperView, tile_image


COLORS = {
    "黄": "#ffd400", "赤": "#ff6666", "緑": "#5fb236", "青": "#2ea8e5",
    "紫": "#a28ae5", "赤紫": "#e56eee", "橙": "#f19837", "灰": "#aaaaaa",
}
VALUE = Qt.ItemDataRole.UserRole


class Reader(QMainWindow):
    def __init__(self, folder=None):
        super().__init__()
        self.resize(1130, 820)
        self.setAcceptDrops(True)
        self.folder = folder if folder is not None else data_dir()
        self.settings = DEFAULTS.copy()
        try:
            saved = json.loads((self.folder / "settings.json").read_text(encoding="utf-8"))
            for key, default in DEFAULTS.items():
                if type(saved.get(key)) is type(default):
                    self.settings[key] = saved[key]
        except (OSError, AttributeError, ValueError):
            pass
        self.settings["backupCount"] = min(200, max(0, self.settings["backupCount"]))
        self.settings["concurrency"] = min(8, max(1, self.settings["concurrency"]))
        self.path = None
        self.opening = False
        self.revision = self.saved_revision = 0
        self.modifying = 0
        self.saving = 0
        self.popup = None
        self.annotations = {}
        self.groups = {}
        self.jobs = {}
        self.failed = {}
        self.last_color = next(iter(COLORS.values()))
        self.session = PdfSession(self)
        self.translator = Translations(self, self.settings["concurrency"])
        self.auto_save = QTimer(self, singleShot=True, interval=240)
        self.auto_save.timeout.connect(self.save)
        self.view = PaperView(self.session, self)
        self.view.chosen.connect(self.show_selection)
        self.view.clicked.connect(self.show_at)
        self.view.message.connect(self.notify)
        self.setCentralWidget(self.view)
        bar = QToolBar("PDF", self)
        bar.setMovable(False)
        self.addToolBar(bar)
        self.add_action(bar, "開く", "Ctrl+O", self.browse)
        self.add_action(bar, "保存", "Ctrl+S", self.save)
        self.add_action(bar, "名前を付けて保存", "Ctrl+Shift+S", self.save_as)
        bar.addSeparator()
        self.add_action(bar, "縮小", "Ctrl+-", lambda: self.view.change_zoom(1 / 1.2))
        self.add_action(bar, "拡大", "Ctrl++", lambda: self.view.change_zoom(1.2))
        self.add_action(bar, "幅に合わせる", "Ctrl+0", self.view.change_zoom)
        self.add_action(bar, "ページへ移動", "Ctrl+G", self.go_to_page)
        self.add_action(bar, "目次", "", self.show_outline)
        self.add_action(bar, "注釈", "", self.toggle_annotations)
        self.add_action(bar, "翻訳設定", "", self.configure)
        self.setWindowTitle("Paper PDF Reader")
        self.outline = []
        self.note_panel = QDockWidget("ハイライトと注釈", self)
        self.note_list = QListWidget(self.note_panel)
        self.note_list.itemActivated.connect(self.open_note)
        self.note_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.note_list.customContextMenuRequested.connect(self.note_actions)
        self.note_panel.setWidget(self.note_list)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.note_panel)
        self.note_panel.hide()

    def add_action(self, bar, text, key, callback):
        action = QAction(text, self)
        if key:
            action.setShortcut(QKeySequence(key))
        action.triggered.connect(lambda: callback())
        bar.addAction(action)

    def notify(self, text):
        self.statusBar().showMessage(text, 6500)

    def ask(self, title, text):
        return QMessageBox.question(
            self, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes

    def allow_leave(self):
        if self.saving or self.modifying:
            self.notify("PDF を更新しています。処理が終わってから操作してください。")
            return False
        if self.revision != self.saved_revision or self.jobs:
            return self.ask("PDF を閉じる", "保存していない変更や翻訳中の注釈を破棄しますか？")
        return True

    def browse(self):
        path, _ = QFileDialog.getOpenFileName(self, "PDF を開く", "", "PDF (*.pdf)")
        if path:
            self.open_pdf(path)

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf"):
                self.open_pdf(url.toLocalFile())
                event.acceptProposedAction()
                break

    def open_pdf(self, filename, password="", confirmed=False):
        if self.opening or (not confirmed and not self.allow_leave()):
            return
        self.opening = True
        self.dismiss_popup()
        self.auto_save.stop()
        # Freeze annotation jobs before issuing an open on the same PDF worker.
        for job in tuple(self.jobs.values()):
            self.translator.cancel(job)
        self.jobs.clear()

        def opened(result, error):
            self.opening = False
            if error:
                QMessageBox.warning(self, "PDF を開けませんでした", error)
            elif result.get("password"):
                value, accepted = QInputDialog.getText(self, "PDF のパスワード", "パスワード", QLineEdit.EchoMode.Password)
                if accepted:
                    self.open_pdf(filename, value, confirmed=True)
            else:
                self.path = Path(filename).resolve()
                self.outline = result["outline"]
                self.annotations.clear()
                self.groups.clear()
                self.failed.clear()
                self.revision = self.saved_revision = 0
                self.view.set_document(result["pages"])
                self.update_title()
                self.reload_annotations()

        self.session.request("open", (filename, password), opened)

    def update_title(self):
        filename = self.path.name if self.path else ""
        mark = " *" if self.revision != self.saved_revision else ""
        self.setWindowTitle(f"{filename}{mark} — Paper PDF Reader" if filename else "Paper PDF Reader")

    def go_to_page(self):
        if not self.view.pages:
            return
        page, accepted = QInputDialog.getInt(self, "ページへ移動", "ページ番号", 1, 1, len(self.view.pages))
        if accepted:
            self.view.go_to(page - 1)

    def show_outline(self):
        if not self.outline:
            self.notify("目次がありません。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("目次")
        dialog.resize(420, 470)
        layout = QVBoxLayout(dialog)
        listing = QListWidget(dialog)
        layout.addWidget(listing)
        for level, title, page in self.outline:
            item = QListWidgetItem("  " * (level - 1) + title, listing)
            item.setData(VALUE, page - 1)
        listing.itemActivated.connect(lambda item: (self.view.go_to(item.data(VALUE)), dialog.accept()))
        dialog.exec()

    def toggle_annotations(self):
        self.note_panel.setVisible(not self.note_panel.isVisible())

    def reload_annotations(self, indices=None):
        doc = self.view.generation
        indices = tuple(indices) if indices is not None else (None,)

        def request(index):
            def ready(notes, error):
                if doc != self.view.generation or self.opening:
                    return
                if error:
                    self.notify(error)
                    return
                if index is None:
                    self.annotations.clear()
                else:
                    for key in [key for key in self.annotations if key[0] == index]:
                        self.annotations.pop(key)
                for note in notes:
                    self.annotations[note["page"], note["xref"]] = note
                self.refresh_notes()
            self.session.request("annotation_list", (index,), ready, priority=12,
                                 needed=lambda: doc == self.view.generation and not self.opening)

        for index in indices:
            request(index)

    def targets(self, key):
        return self.groups.get(key, (key,))

    def refresh_notes(self):
        self.note_list.clear()
        for key, note in sorted(self.annotations.items()):
            targets = self.targets(key)
            status = "翻訳中" if targets in self.jobs else ("翻訳失敗" if targets in self.failed else "")
            comment = " ".join(note["comment"].splitlines())
            label = f"{note['page'] + 1}ページ　{status or comment[:68] or 'ハイライト'}"
            item = QListWidgetItem(label, self.note_list)
            item.setData(VALUE, key)
            item.setToolTip(self.failed[targets][1] if targets in self.failed else note["comment"])

    def open_note(self, item):
        key = tuple(item.data(VALUE))
        if note := self.annotations.get(key):
            self.view.go_to(note["page"], max(0, note["box"][1] - 32))
            self.edit_annotation(key)

    def show_at(self, index, x, y):
        self.dismiss_popup()
        if self.opening:
            return
        for key, note in reversed(tuple(self.annotations.items())):
            x0, y0, x1, y1 = note["box"]
            if index == note["page"] and x0 <= x <= x1 and y0 <= y <= y1:
                self.edit_annotation(key)
                break

    def note_actions(self, point):
        item = self.note_list.itemAt(point)
        if item is None:
            return
        key = tuple(item.data(VALUE))
        targets = self.targets(key)
        menu = QMenu(self)
        menu.addAction("編集", lambda: self.open_note(item))
        if targets in self.jobs:
            menu.addAction("翻訳を取り消す", lambda: self.cancel_job(targets))
        if targets in self.failed:
            menu.addAction("翻訳を再試行する", lambda: self.translate_note(targets, self.failed[targets][0]))
        menu.exec(self.note_list.mapToGlobal(point))

    def dismiss_popup(self):
        if self.popup is not None:
            self.popup.reject()

    def note_dialog(self, title, source, translation="", point=None, selection=False):
        self.dismiss_popup()
        dialog = QDialog(self, Qt.WindowType.Tool)
        self.popup = dialog
        dialog.setWindowTitle(title)
        dialog.resize(486, 356)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        source_edit = QTextEdit()
        source_edit.setPlainText(source)
        translated = QTextEdit()
        translated.setPlainText(translation)
        form.addRow("原文", source_edit)
        form.addRow("訳文", translated)
        layout.addLayout(form)
        status = QLabel()
        status.setWordWrap(True)
        status.hide()
        layout.addWidget(status)
        colors = QHBoxLayout()
        buttons = {}
        for name, color in COLORS.items():
            button = QPushButton(name)
            button.setAccessibleName(f"{name}のハイライト")
            button.setStyleSheet(f"background-color: {color}; color: black;")
            colors.addWidget(button)
            buttons[color] = button
        layout.addLayout(colors)
        footer = QHBoxLayout()
        layout.addLayout(footer)
        translate = QPushButton("翻訳")
        footer.addWidget(translate)
        active_job = None
        preview_ready = False

        def show_status(text):
            status.setText(text)
            status.setVisible(bool(text))

        def preview():
            nonlocal active_job, preview_ready
            text = source_edit.toPlainText().strip()
            if not text:
                return
            translate.setEnabled(False)
            preview_ready = False
            show_status("翻訳中…")

            def completed(result, error):
                nonlocal active_job, preview_ready
                active_job = None
                translate.setEnabled(True)
                show_status(error or "")
                if not error:
                    preview_ready = True
                    translated.setPlainText(result)

            try:
                active_job = self.translator.submit(self.settings, text, translated.setPlainText, completed)
            except ValueError as error:
                completed("", str(error))

        def closed():
            if active_job is not None:
                self.translator.cancel(active_job)
            if self.popup is dialog:
                self.popup = None
            if selection:
                self.view.clear_selection()
            dialog.deleteLater()

        translate.clicked.connect(preview)
        QShortcut(QKeySequence("Alt+T"), dialog, activated=preview)
        dialog.finished.connect(closed)
        if point:
            screen = QApplication.screenAt(point)
            if screen:
                area = screen.availableGeometry()
                x = min(max(point.x(), area.left()), max(area.left(), area.right() - dialog.width()))
                y = point.y() + 12
                if y + dialog.height() > area.bottom():
                    y = max(area.top(), point.y() - dialog.height() - 12)
                dialog.move(QPoint(x, y))
        return dialog, layout, footer, source_edit, translated, buttons, lambda: preview_ready

    def show_selection(self, choices, point):
        if self.opening:
            return
        original, restored = choices
        dialog, layout, footer, source, translated, colors, ready = self.note_dialog("選択した本文", original.source, point=point, selection=True)
        chosen = original
        if original.skipped:
            include = QCheckBox("除外した箇所を含める")
            include.setToolTip("、".join(original.skipped))
            layout.insertWidget(1, include)

            def include_changed(enabled):
                nonlocal chosen
                chosen = restored if enabled else original
                self.view.selection = chosen
                self.view.viewport().update()
                source.setPlainText(chosen.source)
                translated.clear()

            include.toggled.connect(include_changed)

        def add(color, request_translation):
            original_text = source.toPlainText().strip()
            comment = translated.toPlainText().strip()
            if request_translation and not ready():
                comment = ""
            dialog.accept()
            self.last_color = color

            def added(targets):
                group = tuple(targets)
                for key in group:
                    self.groups[key] = group
                if request_translation and not comment and original_text:
                    self.translate_note(group, original_text)
                elif self.settings["autoSave"]:
                    self.auto_save.start()

            self.mutate("add_highlight", (chosen.quads, color, comment), set(chosen.quads), added)

        for color, button in colors.items():
            button.clicked.connect(lambda checked=False, value=color: add(value, True))
        annotate = QPushButton("ハイライトを保存")
        annotate.clicked.connect(lambda: add(self.last_color, False))
        footer.addWidget(annotate)
        QShortcut(QKeySequence("Alt+Shift+T"), dialog, activated=lambda: add(self.last_color, True))
        dialog.show()

    def edit_annotation(self, key):
        if self.opening or key not in self.annotations:
            return
        doc = self.view.generation

        def ready(original, error):
            if doc != self.view.generation or key not in self.annotations:
                return
            if error:
                self.notify(error)
                return
            note = self.annotations[key]
            targets = self.targets(key)
            dialog, _, footer, source, translated, colors, _ = self.note_dialog(
                "ハイライトの注釈", original, note["comment"],
            )
            indices = {page for page, _ in targets}

            def modify(color=None, comment=None, delete=False):
                self.cancel_job(targets)
                self.failed.pop(targets, None)
                dialog.accept()
                self.mutate("modify_highlight", (list(targets), color, comment, delete), indices,
                            lambda _: self.auto_save.start() if self.settings["autoSave"] else None)

            for color, button in colors.items():
                button.clicked.connect(lambda checked=False, value=color: modify(color=value))
            save = QPushButton("コメントを保存")
            save.clicked.connect(lambda: modify(comment=translated.toPlainText()))
            footer.addWidget(save)
            again = QPushButton("翻訳して保存")

            def queue():
                text = source.toPlainText().strip()
                if text:
                    dialog.accept()
                    self.translate_note(targets, text)

            again.clicked.connect(queue)
            footer.addWidget(again)
            delete = QPushButton("削除")
            delete.clicked.connect(lambda: modify(delete=True) if self.ask("ハイライトの削除", "このハイライトを削除しますか？") else None)
            footer.addWidget(delete)
            dialog.show()

        self.session.request("highlight_text", key, ready)

    def cancel_job(self, targets):
        if (job := self.jobs.pop(targets, None)) is not None:
            self.translator.cancel(job)
            self.refresh_notes()

    def translate_note(self, targets, source):
        self.cancel_job(targets)
        self.failed.pop(targets, None)

        def finished(result, error):
            self.jobs.pop(targets, None)
            if error:
                self.failed[targets] = (source, error)
                self.notify(error)
                self.note_panel.show()
            else:
                self.mutate("modify_highlight", (list(targets), None, result),
                            {page for page, _ in targets}, lambda _: self.auto_save.start() if self.settings["autoSave"] else None)
            self.refresh_notes()

        try:
            self.jobs[targets] = self.translator.submit(self.settings, source, lambda _: None, finished)
            self.notify("翻訳を開始しました。")
            self.refresh_notes()
        except ValueError as error:
            finished("", str(error))

    def mutate(self, command, args, indices, on_success):
        self.modifying += 1

        def done(result, error):
            self.modifying -= 1
            if error:
                self.notify(error)
                return
            self.revision += 1
            self.update_title()
            self.view.invalidate(indices)
            self.reload_annotations(indices)
            on_success(result)

        self.session.request(command, args, done)

    def save(self, destination=None):
        if self.path is None or self.opening:
            return
        if destination is None and self.revision == self.saved_revision:
            return
        self.saving += 1
        revision = self.revision
        overwrite = destination is None or Path(destination).resolve() == self.path

        def saved(_, error):
            self.saving -= 1
            if error:
                QMessageBox.warning(self, "PDF を保存できませんでした", error)
                return
            if overwrite:
                self.saved_revision = revision
                self.update_title()
            self.notify("PDF を保存しました。")

        self.session.request("save", (str(self.folder / "backups"), self.settings["backupCount"], destination), saved)

    def save_as(self):
        if self.path:
            name, _ = QFileDialog.getSaveFileName(self, "名前を付けて保存", str(self.path.with_stem(self.path.stem + "-notes")), "PDF (*.pdf)")
            if name:
                self.save(name if name.lower().endswith(".pdf") else name + ".pdf")

    def configure(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("翻訳設定")
        dialog.resize(520, 360)
        form = QFormLayout(dialog)
        fields = {
            key: QLineEdit(str(self.settings[key])) for key in ("endpoint", "model", "apiKey", "langFrom", "langTo")
        }
        fields["apiKey"].setEchoMode(QLineEdit.EchoMode.Password)
        for key, label in (("endpoint", "接続先 URL"), ("model", "モデル"), ("apiKey", "API キー"),
                           ("langFrom", "原文の言語"), ("langTo", "翻訳先の言語")):
            form.addRow(label, fields[key])
        prompt = QTextEdit()
        prompt.setPlainText(self.settings["systemPrompt"])
        form.addRow("システムプロンプト", prompt)
        concurrency = QSpinBox()
        concurrency.setRange(1, 8)
        concurrency.setValue(self.settings["concurrency"])
        form.addRow("同時翻訳数", concurrency)
        backups = QSpinBox()
        backups.setRange(0, 200)
        backups.setValue(self.settings["backupCount"])
        form.addRow("バックアップの保存数", backups)
        autosave = QCheckBox("訳文をコメントに入れた後、自動で保存する")
        autosave.setChecked(self.settings["autoSave"])
        form.addRow(autosave)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            settings = {key: field.text() for key, field in fields.items()}
            settings.update(systemPrompt=prompt.toPlainText(), concurrency=concurrency.value(),
                            backupCount=backups.value(), autoSave=autosave.isChecked())
            try:
                write_json(self.folder / "settings.json", settings)
            except OSError as error:
                self.notify(str(error))
                return
            self.settings = settings
            self.translator.limit = settings["concurrency"]

    def closeEvent(self, event):
        if self.opening or not self.allow_leave():
            event.ignore()
        else:
            self.dismiss_popup()
            self.session.shutdown()
            event.accept()


def smoke_test(app, report):
    with TemporaryDirectory(prefix="paper-reader-smoke-") as temporary:
        try:
            window = Reader(Path(temporary))
        except Exception as error:
            report.write_text(f"Reader initialization failed: {error}", encoding="utf-8")
            return 1
        try:
            def finish(result, error):
                try:
                    if error:
                        raise RuntimeError(error)
                    _, _, image = tile_image(result)
                    if image.isNull() or image.width() != 500:
                        raise ValueError("Qt could not read the MuPDF raster.")
                except (RuntimeError, TypeError, ValueError) as failure:
                    report.write_text(str(failure), encoding="utf-8")
                    app.exit(1)
                else:
                    report.write_text(f"PASS Paper PDF Reader {VERSION}\n", encoding="utf-8")
                    app.exit(0)

            QTimer.singleShot(0, lambda: window.session.request("smoke", (temporary,), finish))
            QTimer.singleShot(30_000, lambda: finish(None, "PDF process timed out."))
            return app.exec()
        finally:
            window.session.shutdown()


def main():
    parser = argparse.ArgumentParser(prog="PaperPdfReader")
    parser.add_argument("pdf", nargs="?")
    parser.add_argument("--smoke-test", type=Path)
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setApplicationName("Paper PDF Reader")
    app.setApplicationVersion(VERSION)
    if args.smoke_test is not None:
        return smoke_test(app, args.smoke_test)
    window = Reader()
    window.show()
    if args.pdf:
        QTimer.singleShot(0, lambda: window.open_pdf(args.pdf))
    return app.exec()
