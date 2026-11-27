"""Menu construction and action state for one reader window."""

from PySide6.QtGui import QAction, QActionGroup, QKeySequence

from . import APP_NAME
from .icons import icon
from .viewport_layout import ACTUAL_SIZE


class ReaderMenus:
    def __init__(self, window):
        self.window = window
        self.actions = {}
        self.action_groups = []
        self.view_menu = None

    def _add_action(self, menu, key, names, callback, shortcut="", required=True, check=False):
        window = self.window
        action = QAction(window.t(*names), window)
        symbols = {"open": "open", "back": "back", "page_previous": "page-up", "page_next": "page-down",
                   "single": "single", "spread": "spread",
                   "width": "width", "fit": "fit", "left": "left", "right": "right",
                   "bigger": "zoom-in", "smaller": "zoom-out", "find": "find",
                   "find_previous": "previous", "find_next": "next", "outline": "outline",
                   "notes": "notes", "save": "save", "save_as": "save", "properties": "properties"}
        if key in symbols:
            action.setIcon(icon(window, symbols[key], window.icon_edge))
            action.setIconVisibleInMenu(True)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(check)
        action.triggered.connect(lambda checked=False: callback())
        if menu is not None:
            menu.addAction(action)
        window.addAction(action)
        self.actions[key] = action, required
        return action

    def rebuild(self):
        window = self.window
        for action, _ in self.actions.values():
            window.removeAction(action)
            action.deleteLater()
        self.actions.clear()
        for group in self.action_groups:
            group.deleteLater()
        self.action_groups.clear()
        menu = window.chrome.menus
        old_menus = [action.menu() for action in menu.actions()]
        menu.clear()
        for old in old_menus:
            if old:
                old.deleteLater()
        file, edit, view, go, zoom, selection, help_ = [menu.addMenu(window.t(*names)) for names in (
            ("ファイル", "File"), ("編集", "Edit"), ("表示", "View"), ("移動", "Go to"), ("ズーム", "Zoom"),
            ("選択", "Selection"), ("ヘルプ", "Help"),
        )]
        self.view_menu = view
        self._add_action(file, "new", ("新しいウィンドウ", "New window"), window.controller.new_window, "Ctrl+N", False)
        self._add_action(file, "open", ("開く…", "Open…"), window.browse, "Ctrl+O", False)
        self._add_action(file, "close", ("文書を閉じる", "Close document"), lambda: window.close_tabs([window.tabs.currentIndex()]), "Ctrl+W")
        self._add_action(file, "reload", ("再読み込み", "Reload"), window.reload, "Ctrl+R")
        self._add_action(file, "duplicate_tab", ("タブを複製", "Duplicate tab"), lambda: window.duplicate(False))
        self._add_action(file, "duplicate_window", ("別ウィンドウに複製", "Duplicate in new window"), lambda: window.duplicate(True))
        file.addSeparator()
        self._add_action(file, "save_as", ("名前を付けて保存…", "Save as…"), window.save_as, "Ctrl+Shift+S")
        self._add_action(file, "save", ("注釈を保存", "Save annotations"), window.save, "Ctrl+S")
        self._add_action(file, "properties", ("文書のプロパティ", "Document properties"), window.properties, "Ctrl+D")
        self._add_action(file, "exit", ("終了", "Exit"), window.close, required=False)

        self._add_action(edit, "undo", ("注釈を元に戻す", "Undo annotation edit"),
                    window.move_annotation_history, "Ctrl+Z")
        redo = self._add_action(edit, "redo", ("注釈をやり直す", "Redo annotation edit"),
                           lambda: window.move_annotation_history(True))
        redo.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])

        modes = QActionGroup(window)
        modes.setExclusive(True)
        self.action_groups.append(modes)
        for mode, names in (("single", ("単ページ", "Single page")), ("spread", ("見開き", "Facing pages")),
                            ("book", ("表紙付き見開き", "Book view"))):
            modes.addAction(self._add_action(view, mode, names,
                                       lambda value=mode: window.dispatch("view.set_layout", value), check=True))
        self._add_action(view, "continuous", ("連続表示", "Continuous"), window.toggle_continuous, check=True)
        self._add_action(view, "rtl", ("右開き", "Right to left"), window.toggle_rtl, check=True)
        self._add_action(view, "left", ("左に90度回転", "Rotate left"), lambda: window.rotate(-90))
        self._add_action(view, "right", ("右に90度回転", "Rotate right"), lambda: window.rotate(90))
        self._add_action(view, "outline", ("目次", "Outline"), lambda: window.toggle_sidebar("outline"), check=True)
        self._add_action(view, "notes", ("注釈一覧", "Annotations"), lambda: window.toggle_sidebar("notes"), check=True)

        self._add_action(go, "page_previous", ("前のページ", "Previous page"), lambda: window.step_page(-1))
        self._add_action(go, "page_next", ("次のページ", "Next page"), lambda: window.step_page(1))
        self._add_action(go, "first", ("最初のページ", "First page"), lambda: window.dispatch("navigate", 0), "Ctrl+Home")
        self._add_action(go, "last", ("最後のページ", "Last page"), lambda: window.dispatch("navigate", len(window.pane.view.pages) - 1), "Ctrl+End")
        self._add_action(go, "page", ("ページへ移動…", "Go to page…"), window.go_page, "Ctrl+G")
        self._add_action(go, "back", ("戻る", "Back"), lambda: window.dispatch("back_forward", -1), "Alt+Left")
        self._add_action(go, "forward", ("進む", "Forward"), lambda: window.dispatch("back_forward", 1), "Alt+Right")
        self._add_action(go, "find", ("文書内で検索…", "Find in document…"), lambda: window.dispatch("open_find"), "Ctrl+F")
        self._add_action(go, "find_next", ("次の一致", "Next match"), lambda: window.dispatch("move_match", 1), "F3")
        self._add_action(go, "find_previous", ("前の一致", "Previous match"), lambda: window.dispatch("move_match", -1), "Shift+F3")

        self._add_action(zoom, "fit", ("ページ全体に合わせる", "Fit page"), lambda: window.dispatch("view.set_zoom", "fit"))
        self._add_action(zoom, "actual", ("実寸", "Actual size"), lambda: window.dispatch("view.set_zoom", "custom", ACTUAL_SIZE))
        self._add_action(zoom, "width", ("幅に合わせる", "Fit width"), lambda: window.dispatch("view.set_zoom", "width"), "Ctrl+0")
        self._add_action(zoom, "content", ("内容に合わせる", "Fit content"), lambda: window.dispatch("view.set_zoom", "content"))
        self._add_action(zoom, "bigger", ("拡大", "Zoom in"), lambda: window.dispatch("view.change_zoom", 1.2), "Ctrl++")
        self._add_action(zoom, "smaller", ("縮小", "Zoom out"), lambda: window.dispatch("view.change_zoom", 1 / 1.2), "Ctrl+-")
        self._add_action(zoom, "cycle", ("倍率を切り替える", "Cycle fit modes"), window.cycle_zoom, "Ctrl+Shift+0")
        zoom.addSeparator()
        for percent in (6400, 3200, 1600, 800, 400, 200, 150, 125, 100, 50, 25, 12.5, 8.33):
            name = f"{percent:g}%"
            self._add_action(zoom, name, (name, name),
                        lambda value=percent: window.dispatch("view.set_zoom", "custom", ACTUAL_SIZE * value / 100))
        self._add_action(zoom, "custom", ("倍率を指定…", "Custom zoom…"), window.custom_zoom)

        for key, names, command in (
            ("copy", ("選択範囲をコピー", "Copy selection"), "copy_selection"),
            ("translate", ("翻訳…", "Translate…"), "translate_selection"),
            ("find_selection", ("文書内で検索", "Find in document"), "find_selection"),
            ("google", ("Google で検索", "Search Google"), "google_selection"),
            ("all", ("すべて選択", "Select all"), "view.select_all"),
        ):
            self._add_action(selection, key, names, lambda method=command: window.dispatch(method))
        settings = self._add_action(None, "preferences", ("設定", "Settings"), window.open_settings, "Ctrl+,", False)
        menu.insertAction(help_.menuAction(), settings)
        self._add_action(help_, "keys", ("ショートカット", "Keyboard shortcuts"), window.shortcuts, required=False)
        self._add_action(help_, "about", (f"{APP_NAME} について", f"About {APP_NAME}"), window.about, required=False)
        self._add_action(None, "next_tab", ("次のタブ", "Next tab"), lambda: window.cycle_tab(1), "Ctrl+Tab", False)
        self._add_action(None, "previous_tab", ("前のタブ", "Previous tab"), lambda: window.cycle_tab(-1), "Ctrl+Shift+Tab", False)
        window.chrome.bind(self.actions, zoom)

    def update_availability(self, pane, ready, failed):
        self.view_menu.setTitle(self.window.t("表示", "View") + (" ●" if failed and pane.notes.isHidden() else ""))
        for key, (action, required) in self.actions.items():
            action.setEnabled(not required or ready)
            if ready and key in ("copy", "translate", "find_selection", "google"):
                action.setEnabled(bool(pane.view.selection_range))

    def clear_document_actions(self):
        self.actions["notes"][0].setText(self.window.t("注釈一覧", "Annotations"))

    def update_document_actions(self, pane, unsaved, failed):
        doc = pane.document
        for key, active in ((pane.view.layout_mode, True), ("continuous", pane.view.continuous),
                            ("rtl", pane.view.rtl), ("outline", not pane.outline.isHidden()),
                            ("notes", not pane.notes.isHidden())):
            self.actions[key][0].setChecked(active)
        self.actions["notes"][0].setText(self.window.t("注釈一覧", "Annotations") + (" ●" if failed else ""))
        self.actions["save"][0].setEnabled(unsaved and not doc.saving)
        self.actions["undo"][0].setEnabled(doc.can_undo and not doc.busy)
        self.actions["redo"][0].setEnabled(doc.can_redo and not doc.busy)
        self.actions["back"][0].setEnabled(pane.navigation.can_move(-1))
        self.actions["forward"][0].setEnabled(pane.navigation.can_move(1))
        current = pane.view.current_page()
        self.actions["page_previous"][0].setEnabled(current > 0)
        self.actions["page_next"][0].setEnabled(current + 1 < len(pane.view.pages))
