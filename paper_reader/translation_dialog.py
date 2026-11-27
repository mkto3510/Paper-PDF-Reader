"""Translation preview and annotation editing window."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QLabel, QPushButton, QScrollArea,
    QTextEdit, QVBoxLayout, QWidget,
)

from .analysis_rules import LABELS
from .annotation_card import COLORS
from .dialog_helpers import ask, dialog_corner
from .icons import swatch
from .preferences import DEFAULTS
from .sidebar import ButtonGrid


class TranslationDialog(QDialog):
    def __init__(self, pane, choices=None, point=None, key=None, source="", comment="", color=None):
        super().__init__(pane, Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.pane, self.document = pane, pane.document
        self.fonts = pane.fonts
        self.preferences = pane.preferences
        self.translations = self.document.store.translations
        self.choices, self.key = choices, key
        self.job = None
        self.selected = choices[0] if choices else None
        self.source_drafts = {}
        self.source_mode = False
        self.anchor = point
        self.preferred_size = QSize(*self.preferences.data["dialogSize"])
        self.fitting = False
        self.applying_font = False  # set while a font change reflows the window, which is not a user resize
        self.excerpt = None
        self.work_screen = None
        self.screen_monitoring = False
        self.setFont(self.fonts.font("translation"))
        self.resize(self.preferred_size)
        t = pane.t
        self.setWindowTitle(t("翻訳", "Translate"))
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        layout.addWidget(scroll, 1)
        content = QWidget()
        scroll.setWidget(content)
        controls = QVBoxLayout(content)
        controls.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.source = QTextEdit()
        self.source.setPlainText(self.selected.source if choices else source)
        self.output = QTextEdit()
        self.output.setPlainText(comment)
        # Text the output box held when it was last in sync with the annotation; anything else is unsaved.
        self.baseline = comment
        self.dismissed = False
        form.addRow(t("原文", "Source"), self.source)
        form.addRow(t("訳文・コメント", "Translation or comment"), self.output)
        self.profile = QComboBox()
        for identifier, profile in self.preferences.data["profiles"].items():
            self.profile.addItem(profile["name"], identifier)
        self.profile.setCurrentIndex(self.profile.findData(self.preferences.data["activeProfile"]))
        form.addRow(t("翻訳サービス", "Translation service"), self.profile)
        self.color = None
        if choices:
            self.color = QComboBox()
            for shade, labels in COLORS.items():
                self.color.addItem(swatch(QColor(shade)), t(*labels), shade)
            self.color.setCurrentIndex(self.color.findData(color or pane.last_color))
            form.addRow(t("ハイライトの色", "Highlight color"), self.color)
        if self.selected and self.selected.skipped:
            items = {}
            for item in self.selected.exclusions:
                items.setdefault((item.page, item.kind), []).append(item.text)
            headings = {}
            for page, kind in items:
                label = t(*LABELS.get(kind, (kind, kind)))
                headings[page, kind] = t(f"{label}（{page + 1}ページ）", f"{label} (p. {page + 1})")
            summary = QLabel()
            summary.setTextFormat(Qt.TextFormat.PlainText)
            summary.setWordWrap(True)
            summary.setText(t("除外した箇所", "Excluded text") + "\n" + " / ".join(headings.values()))
            form.addRow(summary)
            details = QPushButton(t("除外した箇所を表示", "Show excluded text"))
            details.setCheckable(True)
            excerpt = self.excerpt = QTextEdit()
            excerpt.setReadOnly(True)
            excerpt.setPlainText("\n\n".join(headings[key] + "\n" + "\n".join(parts)
                                             for key, parts in items.items()))
            excerpt.hide()
            details.toggled.connect(excerpt.setVisible)
            form.addRow(details)
            form.addRow(excerpt)
            self.fit_excerpt()
            include = QCheckBox(t("除外した箇所も翻訳に含める", "Include excluded text in translation"))
            include.toggled.connect(self.include)
            form.addRow(include)
        controls.addLayout(form)
        self.status = QLabel()
        self.status.setWordWrap(True)
        controls.addWidget(self.status)
        self.start = QPushButton(t("翻訳", "Translate"))
        self.start.clicked.connect(self.preview)
        self.source.textChanged.connect(self.update_start)
        self.update_start()
        self.save = QPushButton(t("注釈に保存", "Save to annotation"))
        self.save.clicked.connect(self.accept_annotation)
        reset = QPushButton(t("既定のサイズ", "Reset size"))
        reset.clicked.connect(self.reset_size)
        controls.addWidget(ButtonGrid([self.start, self.save, reset]))
        for widget, callback in ((self.source, self.preview), (self.output, self.accept_annotation)):
            shortcut = QShortcut(QKeySequence("Ctrl+Return"), widget)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            enter = QShortcut(QKeySequence("Ctrl+Enter"), widget)
            enter.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            enter.activated.connect(callback)
        QShortcut(QKeySequence("Alt+T"), self, activated=self.preview)
        self.finished.connect(self.cleanup)

    def showEvent(self, event):
        super().showEvent(event)
        if event.spontaneous():
            return
        at = self.anchor
        parent = self.pane.window.windowHandle()
        screen = ((QGuiApplication.screenAt(at) if at is not None else None)
                  or (parent.screen() if parent else None) or QGuiApplication.primaryScreen())
        self.fit_screen(screen, at, use_preferred=True)
        if not self.screen_monitoring:
            self.windowHandle().screenChanged.connect(self.schedule_fit)
            QGuiApplication.instance().screenRemoved.connect(self.schedule_fit)
            self.screen_monitoring = True
        self.schedule_fit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isVisible() and not self.fitting and not self.applying_font and self.work_screen is not None:
            self.preferred_size = event.size()

    def apply_font(self):
        """Take a new translation-area size. The reflow it causes must not replace the saved window size."""
        self.applying_font = True
        try:
            self.setFont(self.fonts.font("translation"))
            self.layout().activate()
            self.fit_excerpt()
        finally:
            self.applying_font = False

    def fit_excerpt(self):
        if self.excerpt is not None:
            self.excerpt.setMaximumHeight(max(110, self.excerpt.fontMetrics().lineSpacing() * 5 + 10))

    def schedule_fit(self, *_):
        QTimer.singleShot(0, self, lambda: self.fit_screen(self.screen()))

    def reset_size(self):
        self.preferred_size = QSize(*DEFAULTS["dialogSize"])
        self.fit_screen(self.screen(), use_preferred=True)

    def fit_screen(self, screen, anchor=None, *, use_preferred=False):
        if screen is None:
            return
        if self.work_screen is not screen:
            if self.work_screen in QGuiApplication.screens():
                self.work_screen.availableGeometryChanged.disconnect(self.schedule_fit)
            self.work_screen = screen
            screen.availableGeometryChanged.connect(self.schedule_fit)
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        margins = self.windowHandle().frameMargins()
        chrome = QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        limit = (area.size() - chrome).expandedTo(QSize(1, 1))
        desired = self.preferred_size if use_preferred else self.size()
        self.fitting = True
        try:
            self.setMaximumSize(limit)
            self.resize(desired.boundedTo(limit))
            size = self.size() + chrome
            if anchor is not None:
                corner = anchor + QPoint(0, 8)
            elif use_preferred:
                corner = area.center() - QPoint(size.width() // 2, size.height() // 2)
            else:
                corner = frame.topLeft()
            position = dialog_corner(area, size, corner)
            if frame.topLeft() != position:
                self.move(position)
        finally:
            self.fitting = False

    def include(self, enabled):
        if self.job and self.job.state in ("running", "waiting"):
            self.job.cancel()
        self.source_drafts[self.source_mode] = self.source.toPlainText()
        self.source_mode = enabled
        self.selected = self.choices[int(enabled)]
        self.source.setPlainText(self.source_drafts.get(enabled, self.selected.source))
        self.output.clear()
        self.baseline = ""
        self.status.clear()

    def unsaved(self):
        """True while the translation is in flight or the text differs from what the annotation holds."""
        running = self.job and self.job.state in ("waiting", "running")
        return bool(running) or self.output.toPlainText() != self.baseline

    def confirm_close(self):
        """Ask before throwing unsaved text away. Returns True when the dialog may close."""
        if self.dismissed or not self.unsaved():
            return True
        t = self.pane.t
        return ask(self, t("翻訳", "Translate"), t("未保存の訳文があります。破棄しますか？",
                                                 "The translation has unsaved text. Discard it?"),
                   t("破棄", "Discard"), t("続ける", "Keep editing"), destructive=True)

    def dismiss(self):
        """Close without asking; callers must have called confirm_close first."""
        self.dismissed = True
        super().reject()

    def reject(self):
        if self.confirm_close():
            super().reject()

    def update_start(self):
        running = self.job and self.job.state in ("waiting", "running")
        self.start.setEnabled(bool(running or self.source.toPlainText().strip()))

    def preview(self):
        if self.job and self.job.state in ("waiting", "running"):
            self.job.cancel()
            return
        source = self.source.toPlainText().strip()
        if not source:
            return
        if self.job:
            self.job.changed.disconnect(self.update_job)
        profile = self.preferences.data["profiles"][self.profile.currentData()]
        self.job = self.translations.create(profile, source, self.preferences.data)
        self.job.changed.connect(self.update_job)
        self.translations.enqueue(self.job)

    def update_job(self):
        t = self.pane.t
        labels = {"waiting": t("待機中", "Waiting"), "running": t("翻訳中…", "Translating…"),
                  "failed": self.preferences.localize(self.job.error), "finished": "", "canceled": t("取り消しました", "Canceled")}
        self.status.setText(labels[self.job.state])
        if self.job.state in ("running", "finished"):
            self.output.setPlainText(self.job.output)
        running = self.job.state in ("running", "waiting")
        self.output.setReadOnly(bool(running))
        self.start.setText(t("取り消す", "Cancel") if running else t("翻訳", "Translate"))
        self.update_start()

    def accept_annotation(self):
        job = self.job
        running = job and job.state in ("running", "waiting")
        if running:
            job.changed.disconnect(self.update_job)
            self.job = None
        comment = self.output.toPlainText()

        def attached(keys):
            if running:
                self.document.attach_job(keys, job)
            else:
                self.document.schedule_save()
            self.pane.pick_note(keys[0])

        if self.key:
            targets = self.document.targets(self.key)
            if running:
                self.document.attach_job(targets, job)
            else:
                self.document.detach_job(targets, cancel=True)
                self.document.modify(targets, comment=comment)
        elif self.selected and self.selected.quads:
            self.document.add_highlight(self.selected.quads, self.color.currentData(), "" if running else comment,
                                        attached, job.cancel if running else None)
        elif running:
            job.cancel()
        self.accept()

    def cleanup(self):
        if self.job:
            self.job.changed.disconnect(self.update_job)
            if self.job.state in ("running", "waiting"):
                self.job.cancel()
        size = [self.preferred_size.width(), self.preferred_size.height()]
        if size != self.preferences.data["dialogSize"]:
            try:
                self.preferences.save(self.preferences.data | {"dialogSize": size})
            except OSError as error:
                self.pane.message.emit(str(error))
