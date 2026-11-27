"""Window-side document saving and prompts for leaving a document."""

from pathlib import Path

from PySide6.QtCore import QEventLoop
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .dialog_helpers import ask


class DocumentSave:
    def __init__(self, window):
        self.window = window

    def confirm_discard(self, doc, reload=False):
        if doc.loading:
            self.window.show_message(self.window.t("PDF を更新しています。", "The PDF is being updated."))
            return False
        unsaved = self.window.controller.has_unsaved_changes(doc)
        if not unsaved and not doc.has_active_jobs and not doc.saving:
            return True
        box = QMessageBox(self.window)
        box.setWindowTitle(doc.path.name)
        box.setText(self.window.t("未保存の変更または翻訳中の注釈があります。", "There are unsaved changes or active translations."))
        discard = box.addButton(self.window.t("破棄して再読み込み", "Discard and reload") if reload else
                                 self.window.t("破棄して閉じる", "Discard and close"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(self.window.t("キャンセル", "Cancel"), QMessageBox.ButtonRole.RejectRole)
        save = box.addButton(self.window.t("保存して続行", "Save and continue"), QMessageBox.ButtonRole.AcceptRole)
        save.setEnabled((unsaved or bool(doc.saving)) and not doc.has_active_jobs)
        box.setDefaultButton(cancel)
        box.exec()
        chosen = box.clickedButton()
        box.deleteLater()
        if chosen is discard:
            return True
        if chosen is not save:
            return False
        return self.window.save_document(doc)

    def confirm_switch(self, doc):
        box = QMessageBox(self.window)
        box.setWindowTitle(doc.path.name)
        box.setText(self.window.t("未保存の変更があります。", "This document has unsaved changes."))
        save = box.addButton(self.window.t("保存して切り替え", "Save and switch"), QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton(self.window.t("未保存のまま切り替え", "Switch without saving"), QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(self.window.t("キャンセル", "Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        chosen = box.clickedButton()
        box.deleteLater()
        return chosen is keep or (chosen is save and self.window.save_document(doc))

    def save_document(self, doc, destination=None):
        if doc.loading or doc.disposed:
            return False
        if doc.external and destination is None:
            destination = self.window.save_destination(doc)
            if not destination:
                return False
        panels = [pane.notes for pane in self.window.controller.panes() if pane.document is doc]
        drafts = {}
        for panel in panels:
            for key, value in panel.comment_drafts().items():
                if key in drafts and drafts[key] != value:
                    self.window.show_message(self.window.t("同じ注釈に異なる下書きがあります。各タブでコメントを確認してください。",
                                             "Tabs have different drafts for the same annotation. Review their comments."))
                    return False
                drafts[key] = value
        for panel in panels:
            panel.commit_drafts()

        wait = QEventLoop()

        def settled():
            if not doc.busy:
                wait.quit()

        if doc.busy:
            doc.changed.connect(settled)
            try:
                wait.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            finally:
                doc.changed.disconnect(settled)
        if self.window.controller.has_comment_drafts(doc):
            return False
        doc.auto_save.stop()

        def write(allow_signature_loss=False):
            result = []

            def ready(ok):
                result.append(ok)
                wait.quit()

            if destination:
                doc.save(destination, ready, allow_signature_loss=allow_signature_loss,
                         on_signature_loss=lambda: ready(None))
            else:
                doc.save(destination, ready)
            if not result:
                wait.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            return result[0] if result else False

        saved = write()
        if saved is None:
            confirmed = ask(
                self.window, self.window.t("電子署名付き PDF", "Digitally signed PDF"),
                self.window.t("別のファイルに保存すると、電子署名の検証に影響する可能性があります。保存しますか？",
                       "Saving this copy may affect validation of the digital signature. Save a copy?"),
                self.window.t("保存", "Save"), self.window.t("キャンセル", "Cancel"),
            )
            saved = confirmed and write(True)
        if not saved:
            return False
        if destination is not None and Path(destination).resolve() != Path(doc.path).resolve():
            # A copy was written; the open document and its unsaved marker are unchanged.
            self.window.show_message(self.window.t("コピーを保存しました。開いているのは元のファイルです。",
                                     "Saved a copy. The open file is still the original."))
        return destination is not None or not self.window.controller.has_unsaved_changes(doc)

    def save_destination(self, doc):
        path, _ = QFileDialog.getSaveFileName(self.window, self.window.t("名前を付けて保存", "Save as"),
                                              str(doc.path.with_stem(doc.path.stem + "-notes")), "PDF (*.pdf)")
        return path if not path or path.lower().endswith(".pdf") else path + ".pdf"
