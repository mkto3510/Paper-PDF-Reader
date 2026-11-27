"""Single annotation card and the shared annotation color actions."""

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QPlainTextEdit, QPushButton, QSizePolicy, QToolButton, QVBoxLayout,
)

from .font_ui import AreaMenu
from .icons import icon, swatch
from .sidebar import ButtonGrid, WrappedLabel, wrapped_document


COLORS = {
    "#ffd400": ("黄", "Yellow"), "#ff6666": ("赤", "Red"), "#5fb236": ("緑", "Green"),
    "#2ea8e5": ("青", "Blue"), "#a28ae5": ("紫", "Purple"), "#e56eee": ("赤紫", "Magenta"),
    "#f19837": ("橙", "Orange"), "#aaaaaa": ("灰", "Gray"),
}


def add_color_actions(menu, translate, apply):
    for shade, labels in COLORS.items():
        action = menu.addAction(swatch(QColor(shade)), translate(*labels))
        action.setIconVisibleInMenu(True)
        action.triggered.connect(lambda _checked=False, color=shade: apply(color))


def color_menu(menu, pane, key=None):
    keys = (key,) if key is not None else tuple(pane.notes.multiple)
    add_color_actions(menu, pane.t, lambda color: pane.recolor_notes(keys, color))


class Card(QFrame):
    def __init__(self, panel, key):
        super().__init__(panel.contents)
        self.panel, self.pane, self.key = panel, panel.pane, key
        self.editing = False
        self.shade = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        header = QHBoxLayout()
        self.heading = QToolButton()
        self.heading.setAutoRaise(True)
        self.heading.installEventFilter(self)
        self.heading.clicked.connect(lambda: self.panel.select_card(self.key))
        header.addWidget(self.heading)
        header.addStretch(1)
        self.color = QToolButton()
        self.color.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        colors = AreaMenu(self.pane.fonts, self.color)
        colors.aboutToShow.connect(lambda: self.show_colors(colors))
        self.color.setMenu(colors)
        header.addWidget(self.color)
        layout.addLayout(header)
        self.source = WrappedLabel()
        layout.addWidget(self.source)
        self.comment = WrappedLabel()
        layout.addWidget(self.comment)
        self.editor = QPlainTextEdit()
        self.editor.setMinimumWidth(0)
        self.editor.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.editor.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.editor.document().modificationChanged.connect(self.pane.document.drafts_changed)
        self.editor.textChanged.connect(self.editor_changed)
        layout.addWidget(self.editor)
        self.editor.setFixedHeight(self.editor_minimum)
        self.commit, self.translate, self.delete = QPushButton(), QPushButton(), QPushButton()
        self.commit.clicked.connect(self.commit_comment)
        self.translate.clicked.connect(lambda: self.pane.translate_note(self.key))
        self.delete.clicked.connect(lambda: self.pane.delete_note(self.key))
        self.actions = ButtonGrid([self.commit, self.translate, self.delete])
        layout.addWidget(self.actions)
        for sequence, action in (("Ctrl+Return", self.commit_comment), ("Ctrl+Enter", self.commit_comment),
                                 ("Escape", self.cancel_edit)):
            shortcut = QShortcut(QKeySequence(sequence), self.editor)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(action)
        self.state = WrappedLabel()
        layout.addWidget(self.state)
        self.job_action = QPushButton()
        self.job_action.clicked.connect(self.job_clicked)
        layout.addWidget(self.job_action)
        self.relabel()

    @property
    def editor_minimum(self):
        # Measured with the editor's own font, so it follows the annotation area's size.
        return max(66, self.editor.fontMetrics().lineSpacing() * 4 + 10)

    def relabel(self):
        t = self.pane.t
        self.heading.setText(t(f"{self.key[0] + 1} ページ", f"Page {self.key[0] + 1}"))
        self.color.setAccessibleName(t("色を変更", "Change color"))
        self.color.setToolTip(self.color.accessibleName())
        for button, labels, symbol in (
            (self.commit, ("保存", "Save"), "save"),
            (self.translate, ("翻訳…", "Translate…"), "translate"),
            (self.delete, ("削除", "Delete"), "delete"),
        ):
            button.setText(t(*labels))
            button.setIcon(icon(self, symbol))
            button.setAccessibleName(t(*labels))
        self.commit.setToolTip(t("コメントを保存 (Ctrl+Enter)", "Save comment (Ctrl+Enter)"))

    def editor_changed(self):
        # Remeasure on the next event-loop turn so typing and programmatic edits cannot re-enter the layout.
        if not self.editor.isHidden() and self.key in self.panel.cards:
            self.panel.dirty.add(self.key)
            self.panel.layout_timer.start()

    def fit_editor(self, width):
        """Grow the comment editor to show its whole text, up to 70% of the list's visible height."""
        editor = self.editor
        margins = self.layout().contentsMargins()
        inner = width - margins.left() - margins.right()
        frame = editor.frameWidth() * 2 + round(editor.document().documentMargin()) * 2
        text = wrapped_document(editor.toPlainText(), editor.font(), max(1, inner - frame))
        needed = round(text.size().height()) + frame
        limit = max(self.editor_minimum, int(self.panel.viewport.height() * .7))
        height = min(max(needed, self.editor_minimum), limit)
        scrolls = needed > limit
        policy = Qt.ScrollBarPolicy.ScrollBarAsNeeded if scrolls else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        if editor.verticalScrollBarPolicy() != policy:
            editor.setVerticalScrollBarPolicy(policy)
        if editor.height() != height:
            editor.setFixedHeight(height)

    def measure(self, width):
        if not self.editor.isHidden():
            self.fit_editor(width)
        self.actions.flow(width - 12)
        layout = self.layout()
        measured = layout.totalHeightForWidth(width)
        return measured if measured >= 0 else layout.sizeHint().height()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        selected = self.editing or self.key in self.panel.multiple
        painter.setPen(palette.highlight().color() if selected else palette.mid().color())
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 3, 3)
        painter.end()

    def show_colors(self, menu):
        menu.clear()
        color_menu(menu, self.pane, None if self.key in self.panel.multiple else self.key)

    def eventFilter(self, watched, event):
        if (watched is self.heading and event.type() == QEvent.Type.MouseButtonRelease and self.heading.isDown()
                and event.button() == Qt.MouseButton.LeftButton
                and self.heading.rect().contains(event.position().toPoint())
                and event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)):
            self.heading.setDown(False)
            self.panel.select_card(self.key, event.modifiers())
            return True
        return super().eventFilter(watched, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.panel.select_card(self.key, event.modifiers())
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Delete and not event.isAutoRepeat():
            if self.panel.multiple:
                self.panel.delete_multiple()
            else:
                self.pane.delete_note(self.pane.view.selected_note)
            event.accept()
        else:
            super().keyPressEvent(event)

    def update_card(self):
        note = self.pane.document.get_note(self.key)
        if note is None:
            return
        t = self.pane.t
        chosen = self.key == self.pane.view.selected_note
        pending = self.panel.pending_comments.get(self.key)
        if pending is not None and pending == note["comment"]:
            self.panel.pending_comments.pop(self.key)
            pending = None
        saved = note["comment"] if pending is None else pending
        if chosen and (not self.editing or not self.editor.document().isModified()):
            if (draft := self.panel.drafts.pop(self.key, None)) is not None:
                self.editor.setPlainText(draft)
                self.editor.document().setModified(True)
            elif self.editor.toPlainText() != saved:
                self.editor.setPlainText(saved)
        self.editing = chosen
        if note["color"] != self.shade:
            self.shade = note["color"]
            self.color.setIcon(swatch(QColor(self.shade)))
        excerpt = note.get("source", "")
        self.source.setText(excerpt)
        self.source.setVisible(bool(excerpt))
        self.comment.setText(saved)
        self.comment.setVisible(not chosen and bool(self.comment.text()))
        self.editor.setVisible(chosen)
        self.actions.setVisible(chosen)
        job = self.pane.document.jobs.get(self.pane.document.targets(self.key))
        self.state.setVisible(job is not None)
        self.job_action.setVisible(job is not None)
        if job:
            label = {"waiting": t("待機中", "Waiting"), "running": t("翻訳中…", "Translating…"),
                     "failed": t("翻訳に失敗しました", "Translation failed")}.get(job.state, "")
            self.state.setText("\n".join(part for part in (label, job.output, self.pane.preferences.localize(job.error))
                                         if part))
            self.job_action.setText(t("再試行", "Retry") if job.state == "failed" else t("取り消す", "Cancel"))
        self.update()

    def edit_comment(self):
        self.pane.pick_note(self.key)
        self.editor.setFocus()

    def commit_comment(self):
        if not self.editing or not self.editor.document().isModified():
            return
        value = self.editor.toPlainText()
        self.editor.document().setModified(False)
        self.panel.save_comment(self.key, value)

    def cancel_edit(self):
        note = self.pane.document.get_note(self.key)
        if note:
            self.panel.drafts.pop(self.key, None)
            saved = self.panel.pending_comments.get(self.key, note["comment"])
            if self.editor.toPlainText() != saved:
                # Replace through the cursor, not setPlainText, so Ctrl+Z can bring the edit back.
                cursor = self.editor.textCursor()
                cursor.beginEditBlock()
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.insertText(saved)
                cursor.endEditBlock()
                self.editor.setTextCursor(cursor)
            self.editor.document().setModified(False)
        self.editor.setFocus()

    def job_clicked(self):
        job = self.pane.document.jobs.get(self.pane.document.targets(self.key))
        if job:
            job.retry() if job.state == "failed" else job.cancel()
