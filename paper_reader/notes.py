"""Annotation list, card virtualization, and bulk actions."""

from bisect import bisect_left, bisect_right

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QPlainTextEdit, QPushButton, QScrollArea, QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from .annotation_card import COLORS, Card, add_color_actions, color_menu
from .font_ui import AreaMenu
from .icons import icon, swatch
from .sidebar import ButtonGrid, WrappedLabel


class Notes(QWidget):
    ESTIMATED_HEIGHT = 115
    ROW_SPACING = 4

    def __init__(self, pane):
        super().__init__(pane)
        self.pane = pane
        self.cards, self.colors = {}, {}
        self.pending_comments, self.drafts = {}, {}
        self.filters = set()
        self.keys, self.rows, self.tops = [], {}, [0]
        self.heights = {}
        self.dirty, self.membership = set(), set()
        self.reindex = True
        self.width_used = self.height_used = 0
        self.selected = None
        self.multiple = set()
        self.selection_anchor = None
        self.layout_timer = QTimer(self, singleShot=True, interval=0)
        self.layout_timer.timeout.connect(self.layout_cards)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.title = WrappedLabel()
        layout.addWidget(self.title)
        self.bulk_color = QToolButton()
        self.bulk_color.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.bulk_color.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        colors = AreaMenu(self.pane.fonts, self.bulk_color)
        colors.aboutToShow.connect(lambda: self.show_bulk_colors(colors))
        self.bulk_color.setMenu(colors)
        self.bulk_translate, self.bulk_delete = QPushButton(), QPushButton()
        self.bulk_translate.clicked.connect(lambda: self.pane.translate_notes(self.multiple))
        self.bulk_delete.clicked.connect(self.delete_multiple)
        self.bulk_actions = ButtonGrid([self.bulk_color, self.bulk_translate, self.bulk_delete])
        layout.addWidget(self.bulk_actions)
        self.bulk_actions.hide()
        self.scroll = QScrollArea()
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport = self.scroll.viewport()
        self.contents = QWidget()
        self.scroll.setWidget(self.contents)
        self.scroll.verticalScrollBar().valueChanged.connect(lambda *_: self.layout_timer.start())
        self.viewport.installEventFilter(self)
        layout.addWidget(self.scroll, 1)
        for color in COLORS:
            button = QToolButton()
            button.setCheckable(True)
            button.setFixedSize(25, 25)
            button.setIcon(swatch(QColor(color)))
            button.toggled.connect(lambda on, shade=color: self.set_filter(shade, on))
            self.colors[color] = button
        layout.addWidget(ButtonGrid(list(self.colors.values())))
        select_all = QShortcut(QKeySequence.StandardKey.SelectAll, self)
        select_all.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        select_all.activated.connect(self.select_all_cards)
        self.relabel()

    def relabel(self):
        t = self.pane.t
        self.update_selection_label()
        self.title.setToolTip(t("Ctrl+クリックで選択を切り替え、Shift+クリックで範囲を選択",
                               "Ctrl-click to toggle a selection; Shift-click to select a range"))
        for button, labels in ((self.bulk_color, ("色を変更", "Change color")),
                               (self.bulk_translate, ("翻訳", "Translate")),
                               (self.bulk_delete, ("削除", "Delete"))):
            button.setText(t(*labels))
            button.setAccessibleName(t(*labels))
        self.bulk_color.setIcon(swatch(QColor(next(iter(COLORS)))))
        self.bulk_translate.setIcon(icon(self, "translate"))
        self.bulk_delete.setIcon(icon(self, "delete"))
        self.bulk_actions.flow()
        for color, button in self.colors.items():
            name = t(*COLORS[color])
            button.setToolTip(name)
            button.setAccessibleName(name)
        for card in self.cards.values():
            card.relabel()
        self.heights.clear()
        self.rebuild_offsets()
        self.refresh()

    def update_selection_label(self):
        count = len(self.multiple)
        self.title.setText(self.pane.t(f"注釈（{count}件選択中）", f"Annotations ({count} selected)") if count
                           else self.pane.t("注釈", "Annotations"))
        self.bulk_actions.setVisible(bool(count))

    def show_bulk_colors(self, menu):
        menu.clear()
        color_menu(menu, self.pane)

    def delete_multiple(self):
        self.pane.delete_notes(self.multiple)

    def select_all_cards(self):
        focused = QApplication.focusWidget()
        if isinstance(focused, QPlainTextEdit):
            focused.selectAll()
            return
        self.sync_membership()
        if self.keys:
            self.commit_selected()
            self.pane.view.selected_note = None
            self.pane.view.viewport().update()
            self.set_selection(None)
            self.selection_anchor = self.keys[0]
            self.set_multiple(set(self.keys))

    def select_card(self, key, modifiers=Qt.KeyboardModifier.NoModifier):
        extend = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        toggle = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        if key not in self.rows:
            return
        if not (extend or toggle):
            self.pane.pick_note(key, jump=True, focus_card=True)
            return
        previous = self.selection_anchor
        keys = self.multiple or ({self.selected} if self.selected in self.rows else set())
        if extend:
            anchor = previous if previous in self.rows else key
            first, last = sorted((self.rows[anchor], self.rows[key]))
            span = set(self.keys[first:last + 1])
            keys = (keys | span) if toggle else span
        else:
            keys = keys ^ {key}
            anchor = key
        if self.selected is not None:
            self.commit_selected()
            self.pane.view.selected_note = None
            self.pane.view.viewport().update()
            self.set_selection(None)
        self.selection_anchor = anchor
        self.set_multiple(keys)
        self.reveal(key, focus_card=True)

    def set_multiple(self, keys):
        if self.multiple == keys:
            return
        changed = self.multiple ^ keys
        self.multiple = keys
        self.update_selection_label()
        for key in changed & self.cards.keys():
            self.cards[key].update()

    def commit_selected(self):
        key = self.pane.view.selected_note
        card = self.cards.get(key)
        if card:
            card.commit_comment()
        elif key in self.drafts:
            self.save_comment(key, self.drafts.pop(key))

    def has_drafts(self):
        return bool(self.drafts) or any(card.editing and card.editor.document().isModified()
                                        for card in self.cards.values())

    def comment_drafts(self):
        return self.drafts | {key: card.editor.toPlainText() for key, card in self.cards.items()
                              if card.editing and card.editor.document().isModified()}

    def commit_drafts(self):
        for key, value in self.comment_drafts().items():
            if card := self.cards.get(key):
                if card.editing and card.editor.document().isModified():
                    card.commit_comment()
                    continue
            self.save_comment(key, value)

    def save_comment(self, key, value):
        doc = self.pane.document
        if not doc.get_note(key):
            return
        self.drafts.pop(key, None)
        self.pending_comments[key] = value
        generation = doc.generation

        def failed():
            # The error was already reported through doc.input_error; the tab may be gone by now.
            if not isValid(self):
                return
            if doc.generation == generation and self.pending_comments.get(key) == value:
                self.pending_comments.pop(key, None)
                self.drafts.setdefault(key, value)
                self.update_cards({key})
                doc.drafts_changed.emit()

        targets = doc.targets(key)
        doc.detach_job(targets, cancel=True)
        doc.modify(targets, comment=value, on_error=failed)

    def reset_filter(self):
        self.filters.clear()
        for button in self.colors.values():
            blocked = button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(blocked)
        self.refresh()

    def set_filter(self, color, enabled):
        self.commit_selected()
        if enabled:
            self.filters.add(color)
        else:
            self.filters.discard(color)
        self.refresh()

    def eventFilter(self, watched, event):
        if watched is self.viewport and event.type() == QEvent.Type.Resize:
            self.layout_timer.start()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.layout_timer.start()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "layout_timer"):
            # Forget the measured card heights; layout_cards measures them again and keeps the scroll anchor.
            side = max(25, self.fontMetrics().height() + 5)
            for button in self.colors.values():
                button.setFixedSize(side, side)
            self.width_used = 0
            self.layout_timer.start()

    def reset_document(self):
        for key in list(self.cards):
            self.remove_card(key)
        self.drafts.clear()
        self.pending_comments.clear()
        self.heights.clear()
        self.membership.clear()
        self.dirty.clear()
        self.keys.clear()
        self.selected = None
        self.selection_anchor = None
        self.set_multiple(set())
        self.rebuild_offsets()
        self.refresh()

    def refresh(self, keys=None):
        if keys is None:
            self.reindex = True
            self.dirty.update(self.cards)
        else:
            self.membership.update(keys)
            self.dirty.update(keys)
        if self.isVisible():
            self.layout_cards()

    def update_cards(self, keys):
        self.dirty.update(keys)
        if self.isVisible():
            self.layout_cards()

    def set_selection(self, key):
        self.set_multiple(set())
        self.selection_anchor = key
        previous, self.selected = self.selected, key
        if previous != key:
            self.dirty.update(target for target in (previous, key) if target is not None)
            self.layout_timer.start()

    def rebuild_offsets(self):
        self.rows = {key: index for index, key in enumerate(self.keys)}
        self.tops = [0]
        for key in self.keys:
            height = self.heights.get(key, self.ESTIMATED_HEIGHT)
            self.tops.append(self.tops[-1] + height + self.ROW_SPACING)

    def sync_membership(self):
        if not self.reindex and not self.membership:
            return
        doc = self.pane.document
        candidates = self.membership
        if self.reindex:
            candidates = self.rows.keys() | {key for page in doc.annotations.values() for key in page}
        visible = set(self.keys)
        for key in candidates:
            note = doc.get_note(key)
            if note is None:
                self.pending_comments.pop(key, None)
                self.drafts.pop(key, None)
                self.heights.pop(key, None)
            elif self.pending_comments.get(key) == note["comment"]:
                self.pending_comments.pop(key, None)
            if note and (not self.filters or note["color"] in self.filters):
                visible.add(key)
            else:
                visible.discard(key)
        self.reindex = False
        self.membership.clear()
        if visible != self.rows.keys():
            self.keys = sorted(visible)
            self.rebuild_offsets()
            self.set_multiple(self.multiple & visible)

    def remove_card(self, key):
        card = self.cards.pop(key)
        if card.editing and card.editor.document().isModified() and self.pane.document.get_note(key):
            self.drafts[key] = card.editor.toPlainText()
        card.hide()
        card.deleteLater()

    def layout_cards(self, reveal=None):
        if not self.isVisible():
            return
        self.layout_timer.stop()
        bar = self.scroll.verticalScrollBar()
        offset = bar.value()
        at_end = reveal is None and bar.maximum() > 0 and offset == bar.maximum()
        index = bisect_right(self.tops, offset) - 1
        anchor = self.keys[index] if 0 <= index < len(self.keys) else None
        within = offset - self.tops[index] if anchor is not None else 0
        self.sync_membership()
        viewport = self.viewport
        width, height = max(1, viewport.width()), max(1, viewport.height())
        if self.width_used != width:
            self.width_used = width
            self.heights.clear()
            self.dirty.update(self.cards)
            self.rebuild_offsets()
        elif self.height_used != height:
            # The comment editor's height limit depends on the visible height.
            self.dirty.update(key for key, card in self.cards.items() if card.editing)
        self.height_used = height
        forgotten_height = False
        for key in self.dirty - self.cards.keys():
            forgotten_height |= self.heights.pop(key, self.ESTIMATED_HEIGHT) != self.ESTIMATED_HEIGHT
        if forgotten_height:
            self.rebuild_offsets()
        # First measure enough rows to cover the viewport and roughly one screen on either side.
        while True:
            offset = self.scroll_offset(anchor, within, offset, reveal, height, at_end)
            start = max(0, bisect_right(self.tops, max(0, offset - height)) - 1)
            stop = min(len(self.keys), bisect_left(self.tops, offset + 2 * height))
            visible = self.keys[start:stop]
            measured = False
            for key in visible:
                card = self.cards.get(key)
                if card is None:
                    card = self.cards[key] = Card(self, key)
                    self.dirty.add(key)
                if key in self.dirty or key not in self.heights:
                    card.update_card()
                    size = max(1, card.measure(width))
                    measured |= size != self.heights.get(key, self.ESTIMATED_HEIGHT)
                    self.heights[key] = size
                    self.dirty.discard(key)
            if not measured:
                break
            self.rebuild_offsets()
        offset = self.scroll_offset(anchor, within, offset, reveal, height, at_end)
        # Keep a second buffer so small height changes while editing do
        # not repeatedly destroy and recreate cards at the viewport edge.
        keep_start = max(0, bisect_right(self.tops, max(0, offset - 2 * height)) - 1)
        keep_stop = min(len(self.keys), bisect_left(self.tops, offset + 3 * height))
        # Tall cards can consume several screen heights by themselves.
        # Keep nearby rows by count as well as by pixel distance.
        keep_start = max(0, min(keep_start, start - 4))
        keep_stop = min(len(self.keys), max(keep_stop, stop + 4))
        keep = set(self.keys[keep_start:keep_stop])
        offscreen = self.cards.keys() - set(visible)
        for key in offscreen:
            if key in keep:
                self.cards[key].hide()
            else:
                self.remove_card(key)
        self.dirty.intersection_update(self.cards)
        self.contents.resize(width, max(height, self.tops[-1]))
        bar.setValue(offset)
        for key in visible:
            row = self.rows[key]
            card = self.cards[key]
            card.setGeometry(0, self.tops[row], width, self.heights[key])
            card.show()
            if self.tops[row + 1] > offset and self.tops[row] < offset + height:
                self.pane.document.load_note_source(key)

    def scroll_offset(self, anchor, within, fallback, reveal, height, at_end):
        if at_end:
            return max(0, self.tops[-1] - height)
        offset = fallback
        if anchor in self.rows:
            row = self.rows[anchor]
            offset = self.tops[row] + min(within, self.tops[row + 1] - self.tops[row] - 1)
        if reveal in self.rows:
            row = self.rows[reveal]
            top, bottom = self.tops[row:row + 2]
            if top < offset or top >= offset + height:
                offset = top
            elif bottom > offset + height and bottom - top <= height:
                offset = bottom - height
        return max(0, min(offset, self.tops[-1] - height))

    def reveal(self, key, edit=False, focus_card=False):
        self.layout_cards(key)
        if card := self.cards.get(key):
            if edit:
                card.editor.setFocus()
            elif focus_card:
                card.setFocus()
