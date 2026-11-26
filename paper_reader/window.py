"""Qt windows and actions for the tabbed reader."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QEventLoop, QSignalBlocker, QTimer, Qt
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFormLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMenu, QMessageBox, QTabWidget, QTextEdit, QToolButton, QVBoxLayout,
)

from . import APP_NAME
from .chrome import MINIMUM_WIDTH, DocumentTabs, Header, TabList
from .dialogs import AboutDialog, SettingsDialog, ask
from .icons import icon
from .pane import DocumentPane
from .view import ACTUAL_SIZE


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
        self.preferences = controller.preferences
        self.setMinimumWidth(MINIMUM_WIDTH)
        self.resize(1180, 820)
        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.language = None
        self.actions = {}
        self.action_groups = []
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
        self.refresh_settings()

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

    def action(self, menu, key, names, callback, shortcut="", required=True, check=False):
        action = QAction(self.t(*names), self)
        symbols = {"open": "open", "back": "back", "page_previous": "page-up", "page_next": "page-down",
                   "single": "single", "spread": "spread",
                   "width": "width", "fit": "fit", "left": "left", "right": "right",
                   "bigger": "zoom-in", "smaller": "zoom-out", "find": "find",
                   "find_previous": "previous", "find_next": "next", "outline": "outline",
                   "notes": "notes", "save": "save", "save_as": "save", "properties": "properties"}
        if key in symbols:
            action.setIcon(icon(self, symbols[key]))
            action.setIconVisibleInMenu(True)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(check)
        action.triggered.connect(lambda checked=False: callback())
        if menu is not None:
            menu.addAction(action)
        self.addAction(action)
        self.actions[key] = action, required
        return action

    def refresh_settings(self):
        if self.language != self.preferences.data["language"]:
            self.language = self.preferences.data["language"]
            self.build_menu()
        self.tab_list_button.setArrowType(Qt.ArrowType.DownArrow)
        self.tab_list_button.setToolTip(self.t("開いている PDF の一覧", "Open PDFs"))
        self.tab_list_button.setAccessibleName(self.tab_list_button.toolTip())
        self.tab_list.relabel()
        for i in range(self.tabs.count()):
            self.tabs.widget(i).relabel()
        self.update_status()

    def build_menu(self):
        for action, _ in self.actions.values():
            self.removeAction(action)
            action.deleteLater()
        self.actions.clear()
        for group in self.action_groups:
            group.deleteLater()
        self.action_groups.clear()
        menu = self.chrome.menus
        old_menus = [action.menu() for action in menu.actions()]
        menu.clear()
        for old in old_menus:
            if old:
                old.deleteLater()
        file, edit, view, go, zoom, selection, help_ = [menu.addMenu(self.t(*names)) for names in (
            ("ファイル", "File"), ("編集", "Edit"), ("表示", "View"), ("移動", "Go to"), ("ズーム", "Zoom"),
            ("選択", "Selection"), ("ヘルプ", "Help"),
        )]
        self.view_menu = view
        self.action(file, "new", ("新しいウィンドウ", "New window"), self.controller.new_window, "Ctrl+N", False)
        self.action(file, "open", ("開く…", "Open…"), self.browse, "Ctrl+O", False)
        self.action(file, "close", ("文書を閉じる", "Close document"), lambda: self.close_tabs([self.tabs.currentIndex()]), "Ctrl+W")
        self.action(file, "reload", ("再読み込み", "Reload"), self.reload, "Ctrl+R")
        self.action(file, "duplicate_tab", ("タブを複製", "Duplicate tab"), lambda: self.duplicate(False))
        self.action(file, "duplicate_window", ("別ウィンドウに複製", "Duplicate in new window"), lambda: self.duplicate(True))
        file.addSeparator()
        self.action(file, "save_as", ("名前を付けて保存…", "Save as…"), self.save_as, "Ctrl+Shift+S")
        self.action(file, "save", ("注釈を保存", "Save annotations"), self.save, "Ctrl+S")
        self.action(file, "properties", ("文書のプロパティ", "Document properties"), self.properties, "Ctrl+D")
        self.action(file, "exit", ("終了", "Exit"), self.close, required=False)

        self.action(edit, "undo", ("注釈を元に戻す", "Undo annotation edit"),
                    self.move_annotation_history, "Ctrl+Z")
        redo = self.action(edit, "redo", ("注釈をやり直す", "Redo annotation edit"),
                           lambda: self.move_annotation_history(True))
        redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])

        modes = QActionGroup(self)
        modes.setExclusive(True)
        self.action_groups.append(modes)
        for mode, names in (("single", ("単ページ", "Single page")), ("spread", ("見開き", "Facing pages")),
                            ("book", ("表紙付き見開き", "Book view"))):
            modes.addAction(self.action(view, mode, names,
                                       lambda value=mode: self.dispatch("view.set_layout", value), check=True))
        self.action(view, "continuous", ("連続表示", "Continuous"), self.toggle_continuous, check=True)
        self.action(view, "rtl", ("右開き", "Right to left"), self.toggle_rtl, check=True)
        self.action(view, "left", ("左に90度回転", "Rotate left"), lambda: self.rotate(-90))
        self.action(view, "right", ("右に90度回転", "Rotate right"), lambda: self.rotate(90))
        self.action(view, "outline", ("目次", "Outline"), lambda: self.toggle_sidebar("outline"), check=True)
        self.action(view, "notes", ("注釈一覧", "Annotations"), lambda: self.toggle_sidebar("notes"), check=True)

        self.action(go, "page_previous", ("前のページ", "Previous page"), lambda: self.step_page(-1))
        self.action(go, "page_next", ("次のページ", "Next page"), lambda: self.step_page(1))
        self.action(go, "first", ("最初のページ", "First page"), lambda: self.dispatch("navigate", 0), "Ctrl+Home")
        self.action(go, "last", ("最後のページ", "Last page"), lambda: self.dispatch("navigate", len(self.pane.view.pages) - 1), "Ctrl+End")
        self.action(go, "page", ("ページへ移動…", "Go to page…"), self.go_page, "Ctrl+G")
        self.action(go, "back", ("戻る", "Back"), lambda: self.dispatch("back_forward", -1), "Alt+Left")
        self.action(go, "forward", ("進む", "Forward"), lambda: self.dispatch("back_forward", 1), "Alt+Right")
        self.action(go, "find", ("文書内で検索…", "Find in document…"), lambda: self.dispatch("open_find"), "Ctrl+F")
        self.action(go, "find_next", ("次の一致", "Next match"), lambda: self.dispatch("move_match", 1), "F3")
        self.action(go, "find_previous", ("前の一致", "Previous match"), lambda: self.dispatch("move_match", -1), "Shift+F3")

        self.action(zoom, "fit", ("ページ全体に合わせる", "Fit page"), lambda: self.dispatch("view.set_zoom", "fit"))
        self.action(zoom, "actual", ("実寸", "Actual size"), lambda: self.dispatch("view.set_zoom", "custom", ACTUAL_SIZE))
        self.action(zoom, "width", ("幅に合わせる", "Fit width"), lambda: self.dispatch("view.set_zoom", "width"), "Ctrl+0")
        self.action(zoom, "content", ("内容に合わせる", "Fit content"), lambda: self.dispatch("view.set_zoom", "content"))
        self.action(zoom, "bigger", ("拡大", "Zoom in"), lambda: self.dispatch("view.change_zoom", 1.2), "Ctrl++")
        self.action(zoom, "smaller", ("縮小", "Zoom out"), lambda: self.dispatch("view.change_zoom", 1 / 1.2), "Ctrl+-")
        self.action(zoom, "cycle", ("倍率を切り替える", "Cycle fit modes"), self.cycle_zoom, "Ctrl+Shift+0")
        zoom.addSeparator()
        for percent in (6400, 3200, 1600, 800, 400, 200, 150, 125, 100, 50, 25, 12.5, 8.33):
            name = f"{percent:g}%"
            self.action(zoom, name, (name, name),
                        lambda value=percent: self.dispatch("view.set_zoom", "custom", ACTUAL_SIZE * value / 100))
        self.action(zoom, "custom", ("倍率を指定…", "Custom zoom…"), self.custom_zoom)

        for key, names, command in (
            ("copy", ("選択範囲をコピー", "Copy selection"), "copy_selection"),
            ("translate", ("翻訳…", "Translate…"), "translate_selection"),
            ("find_selection", ("文書内で検索", "Find in document"), "find_selection"),
            ("google", ("Google で検索", "Search Google"), "google_selection"),
            ("all", ("すべて選択", "Select all"), "view.select_all"),
        ):
            self.action(selection, key, names, lambda method=command: self.dispatch(method))
        settings = self.action(None, "preferences", ("設定", "Settings"), self.open_settings, "Ctrl+,", False)
        menu.insertAction(help_.menuAction(), settings)
        self.action(help_, "keys", ("ショートカット", "Keyboard shortcuts"), self.shortcuts, required=False)
        self.action(help_, "about", (f"{APP_NAME} について", f"About {APP_NAME}"), self.about, required=False)
        self.action(None, "next_tab", ("次のタブ", "Next tab"), lambda: self.cycle_tab(1), "Ctrl+Tab", False)
        self.action(None, "previous_tab", ("前のタブ", "Previous tab"), lambda: self.cycle_tab(-1), "Ctrl+Shift+Tab", False)
        self.chrome.bind(self.actions, zoom)

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
        self.view_menu.setTitle(self.t("表示", "View") + (" ●" if failed and pane.notes.isHidden() else ""))
        for key, (action, required) in self.actions.items():
            action.setEnabled(not required or ready)
            if ready and key in ("copy", "translate", "find_selection", "google"):
                action.setEnabled(bool(pane.view.selection_range))
        self.chrome.set_location(pane)
        if not ready:
            self.actions["notes"][0].setText(self.t("注釈一覧", "Annotations"))
            self.chrome.set_external("")
            self.set_document_title()
            return
        doc = pane.document
        unsaved = doc.dirty or doc.mutating or doc in drafts
        state = (self.t("保存中…", "Saving…") if doc.saving else
                 self.t("未保存の変更があります", "Unsaved changes") if unsaved else "")
        self.set_document_title(state, doc.path.name)
        for key, active in ((pane.view.layout_mode, True), ("continuous", pane.view.continuous),
                            ("rtl", pane.view.rtl), ("outline", not pane.outline.isHidden()),
                            ("notes", not pane.notes.isHidden())):
            self.actions[key][0].setChecked(active)
        self.actions["notes"][0].setText(self.t("注釈一覧", "Annotations") + (" ●" if failed else ""))
        self.actions["save"][0].setEnabled(unsaved and not doc.saving)
        self.actions["undo"][0].setEnabled(doc.can_undo and not doc.busy)
        self.actions["redo"][0].setEnabled(doc.can_redo and not doc.busy)
        self.actions["back"][0].setEnabled(pane.history_index > 0)
        self.actions["forward"][0].setEnabled(pane.history_index + 1 < len(pane.history))
        current = pane.view.current_page()
        self.actions["page_previous"][0].setEnabled(current > 0)
        self.actions["page_next"][0].setEnabled(current + 1 < len(pane.view.pages))
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
        menu = QMenu(self)
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
        if doc.loading:
            self.show_message(self.t("PDF を更新しています。", "The PDF is being updated."))
            return False
        unsaved = self.controller.has_unsaved_changes(doc)
        if not unsaved and not doc.has_active_jobs and not doc.saving:
            return True
        box = QMessageBox(self)
        box.setWindowTitle(doc.path.name)
        box.setText(self.t("未保存の変更または翻訳中の注釈があります。", "There are unsaved changes or active translations."))
        discard = box.addButton(self.t("破棄して再読み込み", "Discard and reload") if reload else
                                 self.t("破棄して閉じる", "Discard and close"), QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton(self.t("キャンセル", "Cancel"), QMessageBox.ButtonRole.RejectRole)
        save = box.addButton(self.t("保存して続行", "Save and continue"), QMessageBox.ButtonRole.AcceptRole)
        save.setEnabled((unsaved or bool(doc.saving)) and not doc.has_active_jobs)
        box.setDefaultButton(cancel)
        box.exec()
        chosen = box.clickedButton()
        box.deleteLater()
        if chosen is discard:
            return True
        if chosen is not save:
            return False
        return self.save_document(doc)

    def confirm_switch(self, doc):
        box = QMessageBox(self)
        box.setWindowTitle(doc.path.name)
        box.setText(self.t("未保存の変更があります。", "This document has unsaved changes."))
        save = box.addButton(self.t("保存して切り替え", "Save and switch"), QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton(self.t("未保存のまま切り替え", "Switch without saving"), QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton(self.t("キャンセル", "Cancel"), QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        chosen = box.clickedButton()
        box.deleteLater()
        return chosen is keep or (chosen is save and self.save_document(doc))

    def save_document(self, doc, destination=None):
        if doc.loading or doc.disposed:
            return False
        if doc.external and destination is None:
            destination = self.save_destination(doc)
            if not destination:
                return False
        panels = [pane.notes for pane in self.controller.panes() if pane.document is doc]
        drafts = {}
        for panel in panels:
            for key, value in panel.comment_drafts().items():
                if key in drafts and drafts[key] != value:
                    self.show_message(self.t("同じ注釈に異なる下書きがあります。各タブでコメントを確認してください。",
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
        if self.controller.has_comment_drafts(doc):
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
                self, self.t("電子署名付き PDF", "Digitally signed PDF"),
                self.t("別のファイルに保存すると、電子署名の検証に影響する可能性があります。保存しますか？",
                       "Saving this copy may affect validation of the digital signature. Save a copy?"),
                self.t("保存", "Save"), self.t("キャンセル", "Cancel"),
            )
            saved = confirmed and write(True)
        if not saved:
            return False
        if destination is not None and Path(destination).resolve() != Path(doc.path).resolve():
            # A copy was written; the open document and its unsaved marker are unchanged.
            self.show_message(self.t("コピーを保存しました。開いているのは元のファイルです。",
                                     "Saved a copy. The open file is still the original."))
        return destination is not None or not self.controller.has_unsaved_changes(doc)

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
        path, _ = QFileDialog.getSaveFileName(self, self.t("名前を付けて保存", "Save as"),
                                              str(doc.path.with_stem(doc.path.stem + "-notes")), "PDF (*.pdf)")
        return path if not path or path.lower().endswith(".pdf") else path + ".pdf"

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
            (("スクロール", "Scroll"), "↑ / ↓ / ← / →"),
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
