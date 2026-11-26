"""One document view: navigation, search, selection, and sidebars."""

from __future__ import annotations

from copy import deepcopy
from urllib.parse import urlencode

from PySide6.QtCore import QElapsedTimer, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QLineEdit, QMenu, QMessageBox, QSizePolicy, QSplitter,
    QToolButton, QTreeWidgetItem, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .dialogs import TranslationDialog
from .font_ui import AreaMenu
from .icons import icon, swatch
from .notes import COLORS, Notes, add_color_actions, color_menu
from .selection import bounds
from .sidebar import WrappingOutline
from .translation import chat_request
from .view import PaperView


ROLE = Qt.ItemDataRole.UserRole
HOVER_PRIORITY = 12


class DocumentPane(QWidget):
    message = Signal(str)
    location_changed = Signal()

    def __init__(self, window, document):
        super().__init__()
        self.window, self.document = window, document
        self.fonts = window.fonts
        self.preferences = window.preferences
        self.history = []
        self.history_index = -1
        self.navigation_target = None
        self.lookup = None
        self.lookup_menu = None
        self.lookup_clock = QElapsedTimer()
        self.lookup_timer = QTimer(self, interval=30)
        self.lookup_timer.timeout.connect(self.update_lookup)
        self.last_color = next(iter(COLORS))
        self.rules_generation = document.rules_generation
        self.links = {}
        self.link_reads = {}
        self.hover_point = None
        self.hover_revision = 0
        self.hover_request = None
        self.hover_timer = QTimer(self, singleShot=True, interval=100)
        self.hover_timer.timeout.connect(self.probe_hover)
        self.search_generation = 0
        self.search_pending = False
        self.annotation_dialog = None
        self.pending_note = None
        self.search_timer = QTimer(self, singleShot=True, interval=175)
        self.search_timer.timeout.connect(self.run_search)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.build_search()
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.splitter, 1)
        self.sidebar = QSplitter(Qt.Orientation.Vertical)
        self.sidebar.setChildrenCollapsible(False)
        self.sidebar.setMinimumWidth(145)
        self.splitter.addWidget(self.sidebar)
        self.outline = WrappingOutline()
        self.outline.itemClicked.connect(self.outline_clicked)
        self.sidebar.addWidget(self.outline)
        self.view = PaperView(document, self, document.store.cache, document.identifier)
        self.splitter.addWidget(self.view)
        self.notes = Notes(self)
        self.sidebar.addWidget(self.notes)
        self.sidebar.setStretchFactor(0, 3)
        self.sidebar.setStretchFactor(1, 7)
        self.sidebar.setSizes([300, 700])
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([270, 850])
        self.view.location_changed.connect(self.location_changed)
        self.view.message.connect(self.message)
        self.lookup_notice = QLabel(self.view.viewport())
        self.lookup_notice.setStyleSheet("background: palette(window); color: palette(window-text); padding: 5px;")
        self.lookup_notice.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.lookup_notice.move(8, 8)
        self.lookup_notice.hide()
        self.view.clicked.connect(self.clicked)
        self.view.hover_position.connect(self.track_hover)
        self.view.menu_requested.connect(self.context_menu)
        self.view.note_delete_requested.connect(self.delete_note)
        self.view.note_resize_requested.connect(self.resize_note)
        self.view.resizing_notes = document.resizing_notes
        context = QShortcut(QKeySequence("Shift+F10"), self.view)
        context.activated.connect(
            lambda: self.context_menu(self.view.viewport().mapToGlobal(self.view.viewport().rect().center())))
        for key, callback in (("Ctrl+A", self.view.select_all), ("Ctrl+C", self.copy_selection)):
            shortcut = QShortcut(QKeySequence(key), self.view)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(callback)
        document.loaded.connect(self.on_loaded)
        document.changed.connect(self.on_document_changed)
        document.note_changed.connect(self.on_notes)
        document.history_changed.connect(self.on_annotation_history)
        document.note_jobs_changed.connect(self.on_note_jobs)
        document.input_error.connect(self.message)
        self.relabel()
        if document.info:
            self.on_loaded(False)

    def t(self, japanese, english):
        return self.preferences.tr_text(japanese, english)

    def build_search(self):
        panel = self.search_panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(3)
        self.query = QLineEdit()
        self.query.setMinimumWidth(76)
        self.query.setMaximumWidth(230)
        self.query.textChanged.connect(self.new_query)
        self.query.returnPressed.connect(lambda: self.move_match(1))
        row.addWidget(self.query, 1)
        self.prev_match, self.next_match = QToolButton(), QToolButton()
        self.prev_match.clicked.connect(lambda: self.move_match(-1))
        self.next_match.clicked.connect(lambda: self.move_match(1))
        row.addWidget(self.prev_match)
        row.addWidget(self.next_match)
        self.match_count = QLabel()
        self.match_count.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        row.addWidget(self.match_count)
        self.search_options = QToolButton()
        self.search_options.setText("Aa")
        self.search_options.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = AreaMenu(self.fonts, self.search_options)
        self.search_options.setMenu(menu)
        self.options = [QAction(menu, checkable=True) for _ in range(4)]
        for option in self.options:
            menu.addAction(option)
        row.addWidget(self.search_options)
        for option in self.options[:3]:
            option.toggled.connect(self.new_query)
        self.options[3].toggled.connect(self.show_matches)
        escape = QShortcut(QKeySequence("Escape"), panel)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        escape.activated.connect(self.close_search)
        return panel

    def apply_fonts(self):
        self.outline.setFont(self.fonts.font("outline"))
        self.notes.setFont(self.fonts.font("notes"))

    def relabel(self):
        self.query.setPlaceholderText(self.t("文書内で検索", "Find in document"))
        self.query.setAccessibleName(self.query.placeholderText())
        for flag, labels in zip(self.options, (("大文字と小文字を区別", "Match case"),
                                                ("単語全体", "Whole words"),
                                                ("アクセント記号を区別", "Match diacritics"),
                                                ("全件を強調", "Highlight all"))):
            flag.setText(self.t(*labels))
        for button, symbol, labels in (
            (self.prev_match, "previous", ("前の一致", "Previous match")),
            (self.next_match, "next", ("次の一致", "Next match")),
            (self.search_options, None, ("検索オプション", "Find options")),
        ):
            if symbol:
                button.setIcon(icon(self, symbol))
            button.setToolTip(self.t(*labels))
            button.setAccessibleName(button.toolTip())
        self.outline.setAccessibleName(self.t("目次", "Outline"))
        self.notes.relabel()
        self.view.opacity = self.preferences.data["opacity"] / 100
        self.view.viewport().update()

    def on_document_changed(self):
        self.notes.setEnabled(not self.document.loading)
        if self.document.loading:
            self.track_hover(None)
            self.end_lookup()
            self.view.cancel_click_series()
            self.view.end_selection_gesture()
        if self.rules_generation != self.document.rules_generation:
            self.rules_generation = self.document.rules_generation
            self.track_hover(None)
            self.end_lookup()
            # Callbacks already in flight belonged to the previous rules.
            self.view.text_cache.clear()
            self.view.text_pending.clear()
            self.view.clear_selection()
        self.location_changed.emit()

    def on_loaded(self, preserve):
        self.end_lookup()
        self.navigation_target = None
        self.links.clear()
        self.link_reads.clear()
        self.track_hover(None)
        self.hover_request = None
        self.history.clear()
        self.history_index = -1
        self.pending_note = self.view.selected_note = None
        self.view.set_document(self.document.info["pages"], self.document.generation, preserve)
        self.outline.clear()
        self.notes.reset_document()
        parents = []
        for depth, title, page, point in self.document.info["outline"]:
            while parents and parents[-1][0] >= depth:
                parents.pop()
            parent = parents[-1][1] if parents else self.outline
            item = QTreeWidgetItem(parent, [title])
            item.setData(0, ROLE, (page - 1, point))
            parents.append((depth, item))
        self.outline.expandToDepth(0)
        if self.query.text():
            self.new_query()
        self.on_notes()

    def on_notes(self, keys=None):
        if self.view.selected_note and not self.document.get_note(self.view.selected_note):
            self.view.selected_note = None
            self.notes.set_selection(None)
        self.view.notes = self.document.annotations
        self.view.viewport().update()
        self.notes.refresh(keys)
        if self.pending_note and self.document.get_note(self.pending_note[0]):
            key, edit = self.pending_note
            self.pending_note = None
            self.pick_note(key, edit=edit)

    def on_note_jobs(self, keys, state_changed):
        self.notes.update_cards(keys)
        if state_changed:
            self.view.running_notes = {key for targets, job in self.document.jobs.items()
                                       if job.state in ("waiting", "running") for key in targets}
            if self.view.highlight_drag and self.view.highlight_drag.key in self.view.running_notes:
                self.view.clear_selection()
            self.view.viewport().update()
            if self.isVisible():
                self.location_changed.emit()

    def on_annotation_history(self, keys):
        for key in keys:
            self.notes.pending_comments.pop(key, None)
        if self.pending_note and self.pending_note[0] in keys:
            self.pending_note = None
        if self.view.highlight_drag and self.view.highlight_drag.key in keys:
            self.view.clear_selection()

    def navigate(self, page, x=None, y=0, *, target=None):
        if not self.document.info or not 0 <= page < len(self.view.pages):
            return
        self.end_lookup()
        before = self.view.location(), self.navigation_target
        self.navigation_target = target
        self.view.go_to(page, y, x)
        self.view.show_navigation_target(target)
        after = self.view.location(), target
        if before == after:
            return
        self.history = self.history[:self.history_index + 1]
        if not self.history or self.history[-1] != before:
            self.history.append(before)
        self.history.append(after)
        self.history_index = len(self.history) - 1
        self.location_changed.emit()

    def back_forward(self, delta):
        index = self.history_index + delta
        if 0 <= index < len(self.history):
            self.end_lookup()
            self.view.input_revision += 1
            self.view.cancel_click_series()
            if delta < 0 and self.history_index == len(self.history) - 1:
                self.history[self.history_index] = self.view.location(), self.navigation_target
            self.history_index = index
            position, self.navigation_target = self.history[index]
            self.view.restore(position)
            self.view.show_navigation_target(self.navigation_target)
            self.location_changed.emit()

    def visit_reference(self, entry):
        box = entry.box
        if entry.object_box:
            combined = bounds((box, entry.object_box))
            transform, _, _ = self.view._transform(entry.page)
            projected = transform.mapRect(QRectF(*self.view._rect_args(combined)))
            viewport = self.view.viewport()
            if (projected.width() * self.view.zoom <= viewport.width() - 24
                    and projected.height() * self.view.zoom <= viewport.height() - 24):
                box = combined
        transform, _, _ = self.view._transform(entry.page)
        corner = transform.mapRect(QRectF(*self.view._rect_args(box))).topLeft()
        first = transform.inverted()[0].map(corner)
        self.navigate(entry.page, first.x(), first.y(), target=entry)

    def outline_clicked(self, item, column):
        index, point = item.data(0, ROLE)
        self.navigate(index, *(point or (None, 0)))

    def open_find(self, text=None):
        self.window.chrome.reveal_search()
        if text is not None:
            self.query.setText(text)
        if self.query.text() and not self.view.search_results:
            self.new_query()
        self.query.selectAll()
        self.query.setFocus()

    def close_search(self):
        self.search_generation += 1
        self.search_pending = False
        self.search_timer.stop()
        self.view.search_results.clear()
        self.view.current_match = None
        self.view.viewport().update()
        self.match_count.clear()
        self.query.clear()
        self.view.setFocus()

    def new_query(self, *args):
        self.search_generation += 1
        self.search_pending = False
        self.view.search_results.clear()
        self.view.current_match = None
        self.view.viewport().update()
        self.match_count.clear()
        self.search_timer.start()

    def show_matches(self, enabled):
        self.view.show_all_matches = enabled
        self.view.viewport().update()

    def matches(self):
        return [(page, index, boxes) for page, hits in sorted(self.view.search_results.items())
                for index, boxes in enumerate(hits)]

    def run_search(self):
        query = self.query.text().strip()
        if not query or not self.view.pages:
            return
        self.search_pending = True
        generation = self.search_generation
        anchor = self.view.current_page()
        order = iter([*range(anchor, len(self.view.pages)), *range(anchor)])
        options = tuple(control.isChecked() for control in self.options[:3])
        self.match_count.setText(self.t("検索中…", "Searching…"))

        def advance():
            if generation != self.search_generation:
                return
            page = next(order, None)
            if page is None:
                self.search_pending = False
                self.update_count()
                return

            def ready(matches, error):
                if generation != self.search_generation:
                    return
                if matches:
                    self.view.search_results[page] = matches
                    if self.view.current_match is None:
                        self.move_match(1, from_page=anchor)
                elif error:
                    self.message.emit(error)
                self.update_count(pending=True)
                self.view.viewport().update()
                QTimer.singleShot(0, advance)

            self.document.request("search", (page, query, *options), ready, priority=6,
                                  needed=lambda: generation == self.search_generation)

        advance()

    def update_count(self, pending=False):
        hit_list = self.matches()
        if hit_list:
            index = next((i + 1 for i, hit in enumerate(hit_list) if hit[:2] == self.view.current_match), 0)
            self.match_count.setText(f"{index} / {len(hit_list)}" + ("…" if pending else ""))
        elif not pending:
            self.match_count.setText(self.t("一致なし", "No matches"))

    def move_match(self, step, from_page=None):
        hits = self.matches()
        if not hits:
            if self.query.text() and not (self.search_pending or self.search_timer.isActive()):
                self.new_query()
            return
        current = next((i for i, hit in enumerate(hits) if hit[:2] == self.view.current_match), None)
        if current is None or from_page is not None:
            anchor = self.view.current_page() if from_page is None else from_page
            current = next((i for i, hit in enumerate(hits) if hit[0] >= anchor), 0)
            if step < 0:
                current = (current - 1) % len(hits)
        else:
            current = (current + step) % len(hits)
        page, ordinal, boxes = hits[current]
        self.view.current_match = page, ordinal
        self.navigate(page, boxes[0][0], boxes[0][1])
        self.update_count()
        self.view.viewport().update()

    @staticmethod
    def check_hit(box, x, y):
        return box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def hit_annotation(self, position):
        index, x, y = position
        for key, note in reversed(tuple(self.document.annotations.get(index, {}).items())):
            for quad in note["quads"]:
                xs, ys = zip(*quad)
                if self.check_hit((min(xs), min(ys), max(xs), max(ys)), x, y):
                    return key
        return None

    def link_at(self, position, navigable):
        page, x, y = position
        return next((link for link in self.links.get(page, ()) if self.check_hit(link["box"], x, y)
                     and (not navigable or "page" in link or QUrl(link["uri"]).scheme() in ("http", "https"))),
                    None)

    def with_link(self, position, callback, *, needed=None, failed=None, cancelled=None,
                  navigable=False, priority=-1):
        page = position[0]

        def deliver(error=None):
            if needed is not None and not needed():
                if cancelled is not None:
                    cancelled()
                return
            if error and failed is not None:
                failed(error)
                return
            callback(self.link_at(position, navigable))

        if page in self.links:
            deliver()
            return
        request_key = self.document.identifier, "page_links", self, page
        if page in self.link_reads:
            self.link_reads[page].append((needed, deliver))
            self.document.promote(request_key, priority)
            return
        subscribers = self.link_reads[page] = [(needed, deliver)]

        def loaded(value, error):
            self.link_reads.pop(page, None)
            if value is not None:
                self.links[page] = value
            for _, ready in subscribers:
                ready(error)

        self.document.request("page_links", (page,), loaded, priority=priority, key=request_key,
                              needed=lambda: any(test is None or test() for test, _ in subscribers))

    def track_hover(self, position):
        if position is None and self.hover_point is None:
            return
        if position is not None:
            if self.document.loading or not self.isVisible() or self.lookup is not None:
                return
            page, x, y = position
            mark = self.view.link_hover
            if mark and mark[0] == page and self.check_hit(mark[1], x, y):
                native = self.link_at(position, True)
                if (mark[2] and native and native["box"] == mark[1]) or (not mark[2] and not native):
                    return
            if position == self.hover_point:
                return
        self.hover_revision += 1
        self.hover_point = position
        self.view.show_link_hover(None)
        self.hover_timer.stop()
        if position is not None:
            self.hover_timer.start()

    def probe_hover(self):
        position = self.hover_point
        if self.hover_request is not None or position is None:
            return
        request = self.hover_request = object()
        revision = self.hover_revision

        def current():
            return (isValid(self) and self.isVisible() and not self.document.loading
                    and not self.document.disposed and self.lookup is None and revision == self.hover_revision)

        def finish(box=None, native=False):
            if not isValid(self) or self.hover_request is not request:
                return
            self.hover_request = None
            if current():
                if box is not None:
                    tooltip = "" if native else self.t("クリックして移動先を確認", "Click to look up the destination")
                    self.view.show_link_hover((position[0], box, native), tooltip)
            elif self.hover_point is not None and not self.hover_timer.isActive():
                self.hover_timer.start()

        def link_ready(link):
            if not current():
                finish()
            elif link:
                finish(link["box"], native=True)
            else:
                self.document.request("citation_at", position,
                                      lambda citation, error: finish(citation.box if citation else None),
                                      priority=HOVER_PRIORITY, needed=current)

        self.with_link(position, link_ready, needed=current, failed=lambda error: finish(), cancelled=finish,
                       navigable=True, priority=HOVER_PRIORITY)

    def hideEvent(self, event):
        self.track_hover(None)
        self.end_lookup()
        super().hideEvent(event)

    def end_lookup(self):
        self.lookup = None
        self.lookup_timer.stop()
        self.lookup_clock.invalidate()
        self.lookup_notice.hide()
        if self.lookup_menu is not None:
            menu, self.lookup_menu = self.lookup_menu, None
            menu.close()

    def update_lookup(self):
        if self.lookup is None or not self.lookup():
            self.end_lookup()
        elif self.lookup_clock.isValid():
            elapsed = self.lookup_clock.elapsed()
            if elapsed >= 10_000:
                self.end_lookup()
                self.message.emit(self.t("移動先の確認に時間がかかっています。もう一度クリックすると続きから確認します。",
                                         "Looking up the destination timed out. Click again to resume."))
            elif elapsed >= 150 and self.lookup_notice.isHidden():
                self.lookup_notice.setText(self.t("移動先を確認しています…", "Looking up the destination…"))
                self.lookup_notice.adjustSize()
                self.lookup_notice.show()
                self.lookup_notice.raise_()

    def clicked(self, index, x, y):
        self.end_lookup()
        self.track_hover(None)
        if self.document.loading:
            return
        position = index, x, y
        key = self.hit_annotation(position)
        selected = self.view.selected_note
        revision = self.view.input_revision
        viewport_revision = self.view.viewport_revision
        generation = self.document.generation
        rules_generation = self.document.rules_generation
        location = self.view.location()

        def still_current():
            return (isValid(self) and self.lookup is still_current and self.isVisible()
                    and not self.document.loading and not self.document.disposed
                    and self.document.generation == generation and self.view.input_revision == revision
                    and self.document.rules_generation == rules_generation and self.view.selected_note == selected
                    and self.view.viewport_revision == viewport_revision and self.view.location() == location)

        def clear_note():
            self.notes.commit_selected()
            self.view.selected_note = None
            self.notes.set_selection(None)
            self.view.viewport().update()

        def failed(error):
            if still_current():
                self.end_lookup()
                self.message.emit(error)

        def no_target():
            self.end_lookup()
            if key is None:
                clear_note()
            elif self.document.get_note(key) and self.hit_annotation(position) == key:
                self.pick_note(key)

        def named_target(value, error, page, point):
            if still_current():
                clear_note()
                if value:
                    self.visit_reference(value)
                else:
                    self.navigate(page, *point)

        def citation_ready(citation, targets, error):
            if not still_current():
                return
            if error:
                failed(error)
            elif any(targets):
                self.open_citation(position, citation, targets, still_current, clear_note)
            else:
                no_target()

        def open_link(link):
            if not still_current():
                return
            if link is None:
                self.document.resolve_citation(position, citation_ready, still_current)
            elif "page" in link:
                page, point = link["page"], link["point"]
                if link.get("page_only"):
                    self.document.request("named_figure_target", (position, page),
                                          lambda value, error: named_target(value, error, page, point),
                                          priority=2, needed=still_current)
                else:
                    named_target(None, None, page, point)
            else:
                self.end_lookup()
                clear_note()
                if not QDesktopServices.openUrl(QUrl(link["uri"])):
                    self.message.emit(self.t("ブラウザーを開けませんでした。", "Could not open the browser."))

        self.lookup = still_current
        self.lookup_clock.start()
        self.lookup_timer.start()
        self.with_link(position, open_link, needed=still_current, failed=failed, navigable=True)

    def open_citation(self, position, citation, targets, still_current, clear_note):
        self.lookup_clock.invalidate()
        self.lookup_notice.hide()

        def visit(entry):
            if still_current():
                clear_note()
                self.visit_reference(entry)

        if len(citation.keys) == 1:
            visit(targets[0])
            return
        menu = self.lookup_menu = AreaMenu(self.fonts, self)
        menu.setAccessibleName(self.t("移動先を選択", "Choose a destination"))
        for key, entry in zip(citation.keys, targets):
            if entry:
                menu.addAction(entry.label.replace("&", "&&"), lambda target=entry: visit(target))
            else:
                label = str(key) if isinstance(key, int) else " ".join(key)
                item = menu.addAction(self.t(f"{label} — 照合できません", f"{label} — No unique match"))
                item.setEnabled(False)

        def hidden():
            if self.lookup_menu is menu:
                self.lookup_menu = None
                # Qt can hide a menu before delivering the selected action.
                QTimer.singleShot(0, self, lambda: self.end_lookup() if self.lookup is still_current else None)
            menu.deleteLater()

        menu.aboutToHide.connect(hidden)
        page, x, y = position
        menu.popup(self.view.viewport().mapToGlobal(self.view._project(page, (x, y)).toPoint()))

    def context_menu(self, point, position=None):
        # The menu depends on page coordinates and the selection, not on scrolling, so only input and
        # document changes make a pending lookup stale.
        revision = self.view.input_revision, self.document.generation

        def current():
            return (isValid(self) and self.isVisible() and not self.document.loading and not self.document.disposed
                    and revision == (self.view.input_revision, self.document.generation))

        def show(link):
            if not current():
                return
            t = self.t
            menu = AreaMenu(self.fonts, self)
            if self.view.selection_range:
                menu.addAction(t("選択範囲をコピー", "Copy selection"), self.copy_selection)
                for symbol, label, callback in (
                    ("highlight", t("ハイライトを作成", "Create highlight"), self.highlight_selection),
                    ("translate", t("翻訳…", "Translate…"), lambda color: self.translate_selection(point, color)),
                    ("translate", t("翻訳して注釈を追加", "Translate and add annotation"), self.translate_to_note),
                ):
                    colors = menu.addMenu(icon(self, symbol), label)
                    add_color_actions(colors, t, callback)
                menu.addAction(icon(self, "find"), t("文書内で検索", "Find in document"), self.find_selection)
                menu.addAction(t("Google で検索", "Search Google"), self.google_selection)
                menu.addSeparator()
            if link and link.get("uri"):
                menu.addAction(t("リンク先をコピー", "Copy link address"),
                               lambda: QApplication.clipboard().setText(link["uri"]))
                menu.addSeparator()
            if position and (key := self.hit_annotation(position)):
                self.add_note_actions(menu, key)
                menu.addSeparator()
            if self.document.info:
                menu.addAction(t("注釈を保存", "Save annotations"), self.window.save)
                menu.addAction(t("文書のプロパティ", "Document properties"), self.window.properties)
            if not menu.isEmpty():
                menu.exec(point)
            menu.deleteLater()

        if position and self.document.info:
            self.with_link(position, show, needed=current)
        else:
            show(None)

    def with_selection(self, callback):
        def apply(choices):
            if choices[1].source:
                callback(choices)
            else:
                self.message.emit(self.t("選択範囲に文字情報がありません。", "No text in the selection."))
        self.view.resolve_selection(apply)

    def copy_selection(self):
        self.with_selection(lambda choices: QApplication.clipboard().setText(choices[1].source))

    def find_selection(self):
        self.with_selection(lambda choices: self.open_find(choices[1].source))

    def google_selection(self):
        self.with_selection(lambda choices: QDesktopServices.openUrl(
            QUrl("https://www.google.com/search?" + urlencode({"q": choices[1].source}))))

    def translate_selection(self, point=None, color=None):
        chosen = color or self.last_color
        self.with_selection(lambda choices: self.open_translation(choices=choices, point=point, color=chosen))

    def highlight_selection(self, color=None):
        chosen = color or self.last_color

        def created(keys):
            if self in self.window.controller.panes():
                self.last_color = chosen
                self.new_comment(keys)

        def create(choices):
            selection = choices[1]
            if selection.quads:
                self.document.add_highlight(selection.quads, chosen, on_success=created)
        self.with_selection(create)

    def translate_to_note(self, color=None):
        doc = self.document
        generation = doc.generation
        selected_color = color or self.last_color
        settings = self.preferences.data
        profile = settings["profiles"][settings["activeProfile"]]
        revision = self.view.selection_revision

        def translate(choices):
            selection = choices[0]
            if not selection.source.strip() or not selection.quads:
                self.message.emit(self.t("翻訳する文章が残っていません。「翻訳…」で選択範囲と除外した箇所を確認してください。",
                                         "The filtered selection is empty. Review it with Translate…."))
                return
            try:
                chat_request(profile, selection.source, settings["langFrom"], settings["langTo"])
            except (TypeError, ValueError):
                self.message.emit(self.t("設定で翻訳サービスの接続先を確認してください。",
                                         "Check the translation service URL in Settings."))
                return
            if not profile["model"].strip():
                self.message.emit(self.t("設定で翻訳モデルを選んでください。",
                                         "Choose a translation model in Settings."))
                return
            manager = doc.store.translations
            job = manager.create(profile, selection.source, settings)

            def created(keys):
                if doc.disposed or doc.generation != generation:
                    return
                doc.attach_job(keys, job)
                doc.schedule_save()
                manager.enqueue(job)
                if self in self.window.controller.panes():
                    if self.view.selection_revision == revision:
                        self.view.clear_selection()
                    self.pick_note(keys[0])

            doc.add_highlight(selection.quads, selected_color, on_success=created)

        self.with_selection(translate)

    def show_sidebar(self, name, shown):
        child = getattr(self, name)
        if shown == (not child.isHidden()):
            return
        if name == "notes" and not shown:
            self.notes.commit_selected()
        previous = not self.sidebar.isHidden()
        panel_visible = shown or not (self.outline if name == "notes" else self.notes).isHidden()
        if previous != panel_visible and self.view.pages:
            if location := self.view.location():
                self.view.preserve_content_anchor(location)
        child.setVisible(shown)
        self.sidebar.setVisible(panel_visible)
        if shown and name == "notes":
            self.notes.layout_cards()
        self.location_changed.emit()

    def new_comment(self, keys):
        self.view.clear_selection()
        self.pick_note(keys[0], edit=True)

    def pick_note(self, key, jump=False, focus_card=False, edit=False):
        note = self.document.get_note(key)
        if not note:
            self.pending_note = key, edit
            self.show_sidebar("notes", True)
            return
        if self.view.selected_note != key:
            self.notes.commit_selected()
        self.view.selected_note = key
        self.notes.set_selection(key)
        if self.notes.filters and note["color"] not in self.notes.filters:
            self.notes.reset_filter()
        self.show_sidebar("notes", True)
        self.view.viewport().update()
        if jump:
            self.navigate(key[0], note["box"][0], note["box"][1] - 10)
        self.notes.reveal(key, edit, focus_card)

    def open_translation(self, **kwargs):
        if self.annotation_dialog is not None:
            old = self.annotation_dialog
            if not old.confirm_close():
                old.raise_()
                old.activateWindow()
                return
            old.dismiss()
        dialog = TranslationDialog(self, **kwargs)
        self.annotation_dialog = dialog
        dialog.finished.connect(lambda: setattr(self, "annotation_dialog", None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        dialog.source.setFocus()

    def translate_note(self, key):
        self.notes.commit_selected()

        def ready(source, error):
            if error:
                self.message.emit(error)
            elif note := self.document.get_note(key):
                self.open_translation(key=key, source=source or "", comment=note["comment"])
        note = self.document.get_note(key)
        if note and "source" in note:
            ready(note["source"], None)
        else:
            self.document.request("highlight_text", key, ready, priority=1)

    def annotation_groups(self, keys):
        doc = self.document
        if doc.loading:
            return ()
        groups = dict.fromkeys(doc.targets(key) for key in sorted(keys) if doc.get_note(key))
        unavailable = doc.deleting_notes | doc.resizing_notes
        return tuple(group for group in groups if unavailable.isdisjoint(group))

    def translate_notes(self, keys):
        groups = self.annotation_groups(keys)
        if not groups:
            return
        self.notes.commit_selected()
        settings = self.preferences.data
        profile = deepcopy(settings["profiles"][settings["activeProfile"]])
        languages = {key: settings[key] for key in ("langFrom", "langTo")}
        try:
            chat_request(profile, "", languages["langFrom"], languages["langTo"])
        except (TypeError, ValueError):
            self.message.emit(self.t("設定で翻訳サービスの接続先を確認してください。",
                                     "Check the translation service URL in Settings."))
            return
        if not profile["model"].strip():
            self.message.emit(self.t("設定で翻訳モデルを選んでください。",
                                     "Choose a translation model in Settings."))
            return
        empty = self.t("選択した注釈の一部に、翻訳できるテキストがありません。",
                       "No translatable text was found in some selected annotations.")
        for targets in groups:
            self.document.translate_highlights(targets, profile, languages, empty)

    def add_note_actions(self, menu, key):
        t = self.t
        menu.addAction(t("コメントを編集", "Edit comment"), lambda: self.pick_note(key, edit=True))
        menu.addAction(icon(self, "translate"), t("翻訳…", "Translate…"), lambda: self.translate_note(key))
        colors = menu.addMenu(t("色", "Color"))
        if note := self.document.get_note(key):
            colors.setIcon(swatch(QColor(note["color"])))
            colors.menuAction().setIconVisibleInMenu(True)
        color_menu(colors, self, key)
        menu.addAction(icon(self, "delete"), t("注釈を削除", "Delete annotation"), lambda: self.delete_note(key))

    def delete_note(self, key):
        if key is None:
            return
        self.delete_notes((key,), confirm=False)

    def delete_notes(self, keys, confirm=True):
        groups = self.annotation_groups(keys)
        if not groups:
            return
        count = len(groups)
        if confirm and QMessageBox.question(
            self, self.t("注釈を削除", "Delete annotations"),
            self.t(f"選択した注釈（{count}件）を削除しますか？", f"Delete the selected annotations ({count})?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        targets = tuple(target for group in groups for target in group)
        if self.view.selected_note in targets:
            self.view.selected_note = None
            self.notes.set_selection(None)
        self.document.modify(targets, delete=True)
        self.view.viewport().update()

    def resize_note(self, key, first, last):
        if not self.document.can_resize(key):
            return
        self.notes.commit_selected()

        def moved(updated):
            if self in self.window.controller.panes() and self.view.selected_note == key:
                self.pick_note(updated)

        def failed():
            if self in self.window.controller.panes():
                self.view.viewport().update()

        self.document.resized(key, first, last, moved, failed)
        self.view.viewport().update()

    def recolor_notes(self, keys, color):
        groups = self.annotation_groups(keys)
        if not groups:
            return
        self.notes.commit_selected()
        self.last_color = color
        self.document.modify(tuple(target for group in groups for target in group), color=color)
