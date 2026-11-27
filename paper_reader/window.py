"""Qt windows and actions for the tabbed reader."""

from __future__ import annotations

import traceback

from PySide6.QtCore import QEvent, QSignalBlocker, QTimer, Qt
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QTabWidget, QTextEdit, QToolButton, QVBoxLayout,
)

from . import APP_NAME
from .chrome import MINIMUM_WIDTH, DocumentTabs, Header, TabList
from .dialogs import AboutDialog
from .document_save import DocumentSave
from .font_ui import AreaMenu
from .pane import DocumentPane
from .reader_menus import ReaderMenus
from .settings_dialog import SettingsDialog
from .viewport_layout import ACTUAL_SIZE


def tab_title(name):
    """A file name as tab text; QTabBar would otherwise read a single "&" as a keyboard mnemonic."""
    return name.replace("&", "&&")


class Reader(QMainWindow):
    def present(self):
        """Bring the window to the front, un-minimizing it but keeping a maximized state."""
        state = self.windowState()
        if state & Qt.WindowState.WindowMinimized:
            self.setWindowState(state & ~Qt.WindowState.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.fonts = controller.fonts
        self.setFont(self.fonts.base_font())  # the ground: parts without an area of their own keep this size
        self.preferences = controller.preferences
        self.setMinimumWidth(MINIMUM_WIDTH)
        self.resize(1180, 820)
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.language = None
        self.icon_edge = None
        self.menus = ReaderMenus(self)
        self.document_save = DocumentSave(self)
        self.opening = set()
        self.switching = False
        self.tabs = QTabWidget()
        self.tabs.setTabBar(DocumentTabs())
        self.tabs.setMovable(True)
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(lambda index: self.close_tabs([index]))
        bar = self.tabs.tabBar()
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        bar.customContextMenuRequested.connect(self.tab_menu)
        self.setCentralWidget(self.tabs)
        self.chrome = Header(self)
        self.setMenuWidget(self.chrome)
        self.tabs.currentChanged.connect(self.active_tab_changed)
        self.tab_list_button = QToolButton()
        self.tabs.setCornerWidget(self.tab_list_button, Qt.Corner.TopRightCorner)
        self.tab_list = TabList(self, self.tab_list_button)
        self.tab_list_button.clicked.connect(self.tab_list.open_list)
        bar.tabMoved.connect(lambda *_: self.tab_list.refresh() if self.tab_list.isVisible() else None)
        self.last_pane = None
        self.preferences.changed.connect(self.refresh_settings)
        self.preferences.fonts_changed.connect(self.apply_fonts)
        self.refresh_settings()
        self.apply_fonts()

    def apply_fonts(self):
        """Apply the saved sizes to this window. Each stage runs on its own, so one failure stops no other."""
        for stage in (self.apply_roots, self.apply_menus, self.apply_icon_sizes, self.apply_translation_fonts,
                      self.chrome.timer.start):
            try:
                stage()
            except Exception:
                traceback.print_exc()

    def apply_translation_fonts(self):
        for index in range(self.tabs.count()):
            try:
                dialog = self.tabs.widget(index).annotation_dialog
                if dialog is not None:
                    dialog.apply_font()
            except Exception:
                traceback.print_exc()

    def apply_icon_sizes(self):
        self.chrome.apply_icon_size()
        for index in range(self.tabs.count()):
            try:
                self.tabs.widget(index).apply_icon_size()
            except Exception:
                traceback.print_exc()

    def apply_menus(self):
        for menu in self.findChildren(AreaMenu):
            try:
                menu.setFont(self.fonts.font("menu"))
            except Exception:
                traceback.print_exc()

    def apply_roots(self):
        for widget in (self.tabs.tabBar(), self.tab_list_button):
            widget.setFont(self.fonts.font("tab"))
        try:
            self.chrome.apply_fonts()
        except Exception:
            traceback.print_exc()
        for index in range(self.tabs.count()):
            try:
                self.tabs.widget(index).apply_fonts()
            except Exception:
                traceback.print_exc()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "chrome"):
            self.language = None
            self.refresh_settings()

    def active_tab_changed(self, _):
        previous, target = self.last_pane, self.pane
        if self.switching:
            if previous is not None and self.tabs.indexOf(previous) >= 0:
                with QSignalBlocker(self.tabs):
                    self.tabs.setCurrentWidget(previous)
            return
        if (previous is not None and target is not None and self.tabs.indexOf(previous) >= 0
                and previous.document is not target.document
                and self.controller.has_unsaved_changes(previous.document)):
            focus = previous.focusWidget()
            with QSignalBlocker(self.tabs):
                self.tabs.setCurrentWidget(previous)
            self.switching = True
            try:
                proceed = self.confirm_switch(previous.document)
                if proceed and self.tabs.indexOf(target) >= 0:
                    with QSignalBlocker(self.tabs):
                        self.tabs.setCurrentWidget(target)
                    target.view.setFocus()
                elif focus is not None:
                    focus.setFocus()
            finally:
                self.switching = False
        elif previous and previous is not target and self.tabs.indexOf(previous) >= 0:
            previous.notes.commit_selected()
        self.sync_active_tab()

    def sync_active_tab(self):
        if self.last_pane is not self.pane:
            self.chrome.clear_notice()
        self.last_pane = self.pane
        self.chrome.page.setModified(False)
        self.chrome.select_pane(self.pane)
        if self.tab_list.isVisible():
            self.tab_list.refresh()
        self.update_status()

    def t(self, ja, en):
        return self.preferences.tr_text(ja, en)

    @property
    def pane(self):
        return self.tabs.currentWidget()

    def dispatch(self, name, *args):
        pane = self.pane
        if pane and not pane.document.loading:
            if name.startswith("view."):
                getattr(pane.view, name.removeprefix("view."))(*args)
            else:
                getattr(pane, name)(*args)

    @property
    def actions(self):
        return self.menus.actions

    @property
    def view_menu(self):
        return self.menus.view_menu

    def refresh_settings(self):
        # The actions' icons carry the toolbar's size, so they are made again when the language or that size changes.
        if self.language != self.preferences.data["language"] or self.icon_edge != self.fonts.edge():
            self.language = self.preferences.data["language"]
            self.icon_edge = self.fonts.edge()
            self.menus.rebuild()
        self.tab_list_button.setArrowType(Qt.ArrowType.DownArrow)
        self.tab_list_button.setToolTip(self.t("開いている PDF の一覧", "Open PDFs"))
        self.tab_list_button.setAccessibleName(self.tab_list_button.toolTip())
        self.tab_list.relabel()
        for i in range(self.tabs.count()):
            self.tabs.widget(i).relabel()
        self.update_status()

    def open_settings(self):
        SettingsDialog(self).open()

    def update_status(self, *_):
        drafts = {pane.document for pane in self.controller.panes() if pane.notes.has_drafts()}
        tabs_changed = False
        for i in range(self.tabs.count()):
            doc = self.tabs.widget(i).document
            unsaved = doc.dirty or doc.mutating or doc in drafts
            name = tab_title(doc.path.name) + (" *" if unsaved else "")
            if self.tabs.tabText(i) != name:
                self.tabs.setTabText(i, name)
                tabs_changed = True
            state = self.t("未保存の変更あり", "Unsaved changes") if unsaved else ""
            tip = "\n".join(part for part in (str(doc.path), state) if part)
            if self.tabs.tabToolTip(i) != tip:
                self.tabs.setTabToolTip(i, tip)
        if tabs_changed and self.tab_list.isVisible():
            self.tab_list.refresh()
        pane = self.pane
        ready = bool(pane and pane.document.info and not pane.document.loading)
        failed = ready and any(job.state == "failed" for job in pane.document.jobs.values())
        self.menus.update_availability(pane, ready, failed)
        self.chrome.set_location(pane)
        if not ready:
            self.menus.clear_document_actions()
            self.chrome.set_external("")
            self.set_document_title()
            return
        doc = pane.document
        unsaved = doc.dirty or doc.mutating or doc in drafts
        state = (self.t("保存中…", "Saving…") if doc.saving else
                 self.t("未保存の変更があります", "Unsaved changes") if unsaved else "")
        self.set_document_title(state, doc.path.name)
        self.menus.update_document_actions(pane, unsaved, failed)
        self.chrome.set_external(self.t("元の PDF が変更されました。別のファイルに保存するか、読み込み直してください。",
                                     "The PDF has changed on disk. Save a copy or reload.") if doc.external else "")

    def set_document_title(self, state="", name=""):
        title = " — ".join(part for part in (name, APP_NAME, state) if part)
        if self.windowTitle() != title:
            self.setWindowTitle(title)

    def browse(self):
        files, _ = QFileDialog.getOpenFileNames(self, self.t("PDF を開く", "Open PDFs"), "", "PDF (*.pdf)")
        self.open_paths(files)

    def dragEnterEvent(self, event):
        if any(url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls()
                 if url.isLocalFile() and url.toLocalFile().lower().endswith(".pdf")]
        if paths:
            self.open_paths(paths)
            event.acceptProposedAction()

    def open_paths(self, paths):
        for path in paths:
            doc = self.controller.documents.for_path(path)
            same = next((pane for pane in self.controller.panes() if pane.document is doc), None)
            if same:
                same.window.tabs.setCurrentWidget(same)
                same.window.present()
            elif doc not in self.opening and not doc.loading:
                self.opening.add(doc)
                self.load_document(doc)

    def load_document(self, doc, reload=False, password=None):
        def ready(info, error):
            if error:
                self.opening.discard(doc)
                self.show_error(error)
                if doc.info is None:
                    self.controller.documents.remove(doc)
            elif info.get("password"):
                box = self.input_box(self.t("PDF のパスワード", "PDF password"), self.t("パスワード", "Password"),
                                     QInputDialog.InputMode.TextInput)
                box.setTextEchoMode(QLineEdit.EchoMode.Password)
                ok, value = box.exec(), box.textValue()
                box.deleteLater()
                if ok:
                    self.load_document(doc, reload, value)
                else:
                    self.opening.discard(doc)
                    if doc.info is None:
                        self.controller.documents.remove(doc)
            elif not reload:
                self.opening.discard(doc)
                self.add_tab(doc)
        doc.load(ready, reload=reload, password=password)

    def add_tab(self, doc):
        pane = DocumentPane(self, doc)
        pane.location_changed.connect(self.update_status)
        pane.message.connect(self.show_message)
        doc.drafts_changed.connect(pane.location_changed)
        self.chrome.search.addWidget(pane.search_panel)
        pane.apply_fonts()
        index = self.tabs.addTab(pane, tab_title(doc.path.name))
        self.tabs.setCurrentIndex(index)
        if self.pane is pane:
            pane.view.setFocus()
        self.update_status()
        return pane

    def duplicate(self, new_window, index=None):
        original = self.pane if index is None else self.tabs.widget(index)
        if original is None or original.document.loading:
            return
        window = self.controller.new_window() if new_window else self
        pane = window.add_tab(original.document)
        target, source = pane.view, original.view
        target.zoom_mode, target.zoom = source.zoom_mode, source.zoom
        target.set_layout(source.layout_mode, source.continuous, source.rtl, source.rotation)
        target.restore(source.location())

    def cycle_tab(self, step):
        count = self.tabs.count()
        if count:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % count)

    def tab_menu(self, point):
        bar = self.tabs.tabBar()
        index = bar.tabAt(point)
        if index < 0:
            return
        # The menu works on the tab that was clicked; it does not switch to it (and so never asks to save).
        menu = AreaMenu(self.fonts, self)
        menu.addAction(self.t("タブを複製", "Duplicate tab"), lambda: self.duplicate(False, index))
        menu.addAction(self.t("別ウィンドウに複製", "Duplicate in new window"), lambda: self.duplicate(True, index))
        menu.addSeparator()
        count = self.tabs.count()
        for names, targets in (
            (("このタブを閉じる", "Close tab"), [index]), (("左側のタブを閉じる", "Close tabs to the left"), list(range(index))),
            (("右側のタブを閉じる", "Close tabs to the right"), list(range(index + 1, count))),
            (("他のタブを閉じる", "Close other tabs"), [i for i in range(count) if i != index]),
            (("すべてのタブを閉じる", "Close all tabs"), list(range(count))),
        ):
            menu.addAction(self.t(*names), lambda checked=False, selection=targets: self.close_tabs(selection)).setEnabled(bool(targets))
        menu.exec(bar.mapToGlobal(point))
        menu.deleteLater()

    def error_text(self, message):
        return self.preferences.localize(message)

    def show_message(self, message):
        self.chrome.show_notice(self.error_text(message))

    def show_error(self, message):
        QMessageBox.warning(self, APP_NAME, self.error_text(message))

    def reload(self):
        doc = self.pane.document
        if self.confirm_discard(doc, reload=True):
            self.load_document(doc, reload=True)

    def move_annotation_history(self, redo=False):
        if self.pane:
            self.pane.document.move_history(redo)

    def confirm_discard(self, doc, reload=False):
        return self.document_save.confirm_discard(doc, reload)

    def confirm_switch(self, doc):
        return self.document_save.confirm_switch(doc)

    def save_document(self, doc, destination=None):
        return self.document_save.save_document(doc, destination)

    def close_tabs(self, indices):
        panes = [pane for i in sorted(set(indices)) if (pane := self.tabs.widget(i))]
        surviving = set(self.controller.panes()) - set(panes)
        ending = {pane.document for pane in panes} - {pane.document for pane in surviving}
        # Ask about every open translation first: "Keep editing" must cancel the whole close before
        # "Save and continue" in the next prompt writes anything.
        for pane in panes:
            dialog = pane.annotation_dialog
            if dialog and not dialog.confirm_close():
                dialog.raise_()
                dialog.activateWindow()
                return False
        if any(not self.confirm_discard(doc) for doc in sorted(ending, key=lambda d: d.identifier)):
            return False
        with QSignalBlocker(self.tabs):
            for pane in panes:
                if pane.document not in ending:
                    # The document stays open elsewhere: keep every draft, not only the selected card's.
                    pane.notes.commit_drafts()
                if pane.annotation_dialog:
                    pane.annotation_dialog.dismiss()
                self.tabs.removeTab(self.tabs.indexOf(pane))
                self.chrome.search.removeWidget(pane.search_panel)
                pane.search_panel.deleteLater()
                pane.deleteLater()
        if self.tab_list.isVisible():
            self.tab_list.refresh()
        for doc in ending:
            self.controller.documents.remove(doc)
        self.sync_active_tab()
        return True

    def closeEvent(self, event):
        if self.opening:
            self.show_message(self.t("読み込み中です。", "A document is still loading."))
            event.ignore()
        elif not self.close_tabs(range(self.tabs.count())):
            event.ignore()
        else:
            self.controller.windows.remove(self)
            self.deleteLater()
            event.accept()

    def save(self):
        if self.pane:
            self.save_document(self.pane.document)

    def save_destination(self, doc):
        return self.document_save.save_destination(doc)

    def save_as(self):
        if self.pane:
            doc = self.pane.document
            if path := self.save_destination(doc):
                self.save_document(doc, path)

    def toggle_continuous(self):
        self.pane.view.set_layout(continuous=not self.pane.view.continuous)
        self.update_status()

    def toggle_rtl(self):
        self.pane.view.set_layout(rtl=not self.pane.view.rtl)
        self.update_status()

    def toggle_sidebar(self, name):
        sidebar = getattr(self.pane, name)
        self.pane.show_sidebar(name, sidebar.isHidden())

    def rotate(self, degrees):
        self.pane.view.set_layout(rotation=self.pane.view.rotation + degrees)

    def step_page(self, step):
        pane = self.pane
        pane.navigate(pane.view.current_page() + step)

    def go_page(self):
        view = self.pane.view
        box = self.input_box(self.t("ページへ移動", "Go to page"), self.t("ページ番号", "Page number"),
                             QInputDialog.InputMode.IntInput)
        box.setIntRange(1, len(view.pages))
        box.setIntValue(view.current_page() + 1)
        if box.exec():
            self.pane.navigate(box.intValue() - 1)
        box.deleteLater()

    def cycle_zoom(self):
        view = self.pane.view
        view.set_zoom({"fit": "width", "width": "content"}.get(view.zoom_mode, "fit"))

    def custom_zoom(self):
        view = self.pane.view
        box = self.input_box(self.t("倍率", "Zoom"), "%", QInputDialog.InputMode.DoubleInput)
        box.setDoubleDecimals(2)
        box.setDoubleRange(8.33, 6400)
        box.setDoubleValue(view.zoom / ACTUAL_SIZE * 100)
        if box.exec():
            view.set_zoom("custom", ACTUAL_SIZE * box.doubleValue() / 100)
        box.deleteLater()

    def input_box(self, title, label, mode):
        """An input dialog whose buttons follow the display language."""
        box = QInputDialog(self)
        box.setInputMode(mode)
        box.setWindowTitle(title)
        box.setLabelText(label)
        box.setOkButtonText("OK")
        box.setCancelButtonText(self.t("キャンセル", "Cancel"))
        return box

    def properties(self):
        if not self.pane:
            return
        doc = self.pane.document

        def ready(info, error):
            if error or not info:
                self.show_error(error or "文書のプロパティを読み取れませんでした。")
                return
            dialog = QDialog(self)
            dialog.resize(560, 530)
            dialog.setWindowTitle(self.t("文書のプロパティ", "Document properties"))
            form = QFormLayout(dialog)
            for key, names in (
                ("title", ("タイトル", "Title")), ("author", ("著者", "Author")), ("subject", ("件名", "Subject")),
                ("keywords", ("キーワード", "Keywords")), ("creationDate", ("作成日時", "Created")),
                ("modDate", ("更新日時", "Modified")), ("format", ("形式", "Format")), ("pages", ("ページ数", "Pages")),
                ("filename", ("ファイル", "File")), ("size", ("容量（バイト）", "Size (bytes)")),
            ):
                cell = QLabel(str(info.get(key) or "—"))
                cell.setTextFormat(Qt.TextFormat.PlainText)
                cell.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                cell.setWordWrap(True)
                form.addRow(self.t(*names), cell)
            fonts = QTextEdit()
            fonts.setReadOnly(True)
            fonts.setPlainText(self.t("フォントを読み込んでいます…", "Loading fonts…"))
            form.addRow(self.t("フォント", "Fonts"), fonts)
            pages = iter(range(info["pages"]))
            found = set()
            ended = False

            def stop():
                nonlocal ended
                ended = True
            dialog.finished.connect(stop)

            def advance():
                if ended or doc.disposed:
                    return
                index = next(pages, None)
                if index is None:
                    fonts.setPlainText("\n".join(f"{name} · {kind} · {encoding} · " +
                                                 (self.t("埋め込み", "Embedded") if embedded else
                                                  self.t("埋め込みなし", "Not embedded"))
                                                 for name, kind, encoding, embedded in sorted(found)))
                    return

                def loaded(values, failure):
                    if values:
                        found.update(values)
                    QTimer.singleShot(0, advance)
                doc.request("fonts", (index,), loaded, priority=8, needed=lambda: not ended)

            QTimer.singleShot(0, advance)
            dialog.exec()
            dialog.deleteLater()
        doc.request("properties", (), ready)

    def shortcuts(self):
        lines = [f"{action.text()}    {' / '.join(key.toString() for key in action.shortcuts())}"
                 for action, _ in self.actions.values() if action.shortcuts()]
        lines.extend(f"{self.t(*names)}    {keys}" for names, keys in (
            (("コピー（選択中の本文）", "Copy selected text"), "Ctrl+C"),
            (("すべて選択（注釈一覧では注釈をすべて選択）", "Select all (all annotations in the list)"), "Ctrl+A"),
            (("PDF 本文をスクロール（全体表示またはその方向にスクロール範囲がなければページ送り）",
              "Scroll the PDF (turn pages at Fit page or when that axis has no scroll range)"), "↑ / ↓ / ← / →"),
            (("ページ送り", "Page forward / back"), "PageDown / PageUp"),
            (("選択の解除（注釈の編集中は、編集を保存済みの内容に戻す）",
              "Clear the selection (while editing an annotation: revert to the saved comment)"), "Esc"),
            (("コメントを保存（注釈の編集中・翻訳ウィンドウ）", "Save the comment (annotation editor and translation window)"),
             "Ctrl+Enter"),
        ))
        self.text_dialog(self.t("ショートカット", "Keyboard shortcuts"), "\n".join(lines))

    def about(self):
        dialog = AboutDialog(self)
        dialog.exec()
        dialog.deleteLater()

    def text_dialog(self, title, text):
        dialog = QDialog(self)
        dialog.resize(620, 520)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        body = QTextEdit()
        body.setPlainText(text)
        body.setReadOnly(True)
        layout.addWidget(body)
        dialog.exec()
        dialog.deleteLater()
