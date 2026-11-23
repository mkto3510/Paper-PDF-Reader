"""Reader information, settings, and translation dialogs."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

from PySide6.QtCore import QPoint, QSize, QTimer, Qt
from PySide6.QtGui import QColor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QTextBrowser, QTextEdit, QVBoxLayout, QWidget,
)

from . import APP_NAME, VERSION
from .analysis_rules import LABELS, RULES
from .icons import swatch
from .notes import COLORS
from .preferences import DEFAULTS
from .sidebar import ButtonGrid
from .translation import DEFAULT_PROFILE, request_url


class AboutDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        t = self.t = parent.t
        self.setWindowTitle(t(f"{APP_NAME} について", f"About {APP_NAME}"))
        self.resize(640, 490)
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        image = QLabel()
        image.setPixmap(self.windowIcon().pixmap(48))
        header.addWidget(image)
        identity = QVBoxLayout()
        name = QLabel(APP_NAME)
        font = name.font()
        font.setPointSizeF(font.pointSizeF() * 1.5)
        font.setBold(True)
        name.setFont(font)
        identity.addWidget(name)
        version = QLabel(t(f"バージョン {VERSION}", f"Version {VERSION}"))
        version.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        identity.addWidget(version)
        header.addLayout(identity, 1)
        layout.addLayout(header)

        tabs = QTabWidget()
        layout.addWidget(tabs)
        overview = QTextBrowser()
        overview.setOpenExternalLinks(True)
        overview.setMarkdown("\n\n".join((
            t("段組のある論文 PDF を読み、選んだ箇所を翻訳できる Windows 向けビューアです。"
              "訳文はハイライトのコメントとして PDF に保存できます。",
              "A Windows viewer for reading research PDFs with multiple columns. Translate a passage "
              "and save the translation as a highlight comment in the PDF."),
            t("PDF を読んだり注釈を付けたりする操作は、オフラインで使えます。翻訳時は、選んだ文章を設定先の翻訳サービスに送ります。",
              "Reading and annotating work offline. When you translate a passage, its text goes to "
              "the translation service you configured."),
            f'[{t("ソースコードを開く", "View source code")}](https://github.com/mkto3510/Paper-PDF-Reader)',
        )))
        tabs.addTab(overview, t("概要", "Overview"))

        licenses = QWidget()
        content = QVBoxLayout(licenses)
        content.addWidget(QLabel(t("アプリのライセンス  AGPL-3.0-or-later", "Application license  AGPL-3.0-or-later")))
        self.notice = QComboBox()
        self.notice.setAccessibleName(t("ライセンス文書", "License document"))
        content.addWidget(self.notice)
        self.notice_body = QTextBrowser()
        self.notice_body.setOpenExternalLinks(True)
        content.addWidget(self.notice_body, 1)
        tabs.addTab(licenses, t("ライセンス", "Licenses"))
        self.load_notices()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText(t("閉じる", "Close"))
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def load_notices(self):
        t = self.t
        packaged = getattr(sys, "frozen", False)
        root = Path(sys.executable).parent if packaged else Path(__file__).resolve().parents[1]
        folder = root / ("LICENSES" if packaged else "licenses")
        license_path = root / ("LICENSE.txt" if packaged else "LICENSE")
        sources_path = (folder if packaged else root) / "THIRD_PARTY.md"
        self.notice.addItem(t(f"{APP_NAME} のライセンス", f"{APP_NAME} license"), license_path)
        self.notice.addItem(t("使用ソフトウェアとソースコード", "Third-party software and source code"), sources_path)
        for path in sorted(folder.rglob("*"), key=lambda file: file.as_posix().casefold()):
            if path != sources_path and path.is_file():
                self.notice.addItem(path.relative_to(folder).as_posix(), path)
        self.notice.currentIndexChanged.connect(self.show_notice)
        self.show_notice()

    def show_notice(self):
        path = self.notice.currentData()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self.notice_body.setPlainText(self.t(f"{path.name} を読み込めませんでした．",
                                                  f"Could not read {path.name}."))
            return
        if path.name == "THIRD_PARTY.md":
            self.notice_body.setMarkdown(text)
        else:
            self.notice_body.setPlainText(text)


def spin(low, high, value):
    box = QSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    return box


def dialog_corner(work_area, frame_size, preferred):
    x = max(work_area.x(), min(preferred.x(), work_area.x() + work_area.width() - frame_size.width()))
    y = max(work_area.y(), min(preferred.y(), work_area.y() + work_area.height() - frame_size.height()))
    return QPoint(x, y)


class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.preferences = window.preferences
        self.translate = self.preferences.tr_text
        self.translations = window.controller.documents.translations
        self.data = deepcopy(self.preferences.data)
        self.profile_key = None
        t = self.translate
        self.resize(620, 650)
        self.setWindowTitle(t("設定", "Settings"))
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        general = QWidget()
        form = QFormLayout(general)
        self.language = QComboBox()
        self.language.addItem("日本語", "ja")
        self.language.addItem("English", "en")
        self.language.setCurrentIndex(self.language.findData(self.data["language"]))
        form.addRow(t("表示言語", "Display language"), self.language)
        self.languages = {key: QLineEdit(self.data[key]) for key in ("langFrom", "langTo")}
        form.addRow(t("原文の言語", "Source language"), self.languages["langFrom"])
        form.addRow(t("翻訳先の言語", "Target language"), self.languages["langTo"])
        self.concurrent = spin(1, 8, self.data["concurrency"])
        self.backups = spin(0, 200, self.data["backupCount"])
        self.opacity = spin(0, 100, self.data["opacity"])
        self.opacity.setSuffix("%")
        self.auto_save = QCheckBox(t("コメントに訳文を入れた後に保存", "Save after writing translated comments"))
        self.auto_save.setChecked(self.data["autoSave"])
        form.addRow(t("同時翻訳数", "Concurrent requests"), self.concurrent)
        form.addRow(t("バックアップ保存数", "Backups to keep"), self.backups)
        form.addRow(t("ハイライトの不透明度", "Highlight opacity"), self.opacity)
        form.addRow(self.auto_save)
        tabs.addTab(general, t("全般", "General"))
        tabs.addTab(self.service_tab(), t("翻訳サービス", "Translation services"))

        rules_page = QWidget()
        rules = QFormLayout(rules_page)
        self.checks = {}
        for key in RULES:
            item = QCheckBox(t(*LABELS[key]))
            item.setChecked(self.data["rules"][key])
            rules.addRow(item)
            self.checks[key] = item
        self.edge_range = spin(3, 25, self.data["rules"]["edge_band"])
        self.small_size = spin(2, 30, self.data["rules"]["small_text"])
        self.edge_range.setSuffix("%")
        self.small_size.setSuffix("%")
        rules.addRow(t("ヘッダー・フッターを探す範囲", "Header/footer detection range"), self.edge_range)
        rules.addRow(t("本文より小さい文字の差", "Small-text size difference"), self.small_size)
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setWidget(rules_page)
        tabs.addTab(scroller, t("本文の除外", "Text exclusions"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(t("保存", "Save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("キャンセル", "Cancel"))
        buttons.accepted.connect(self.commit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def service_tab(self):
        t = self.translate
        widget = QWidget()
        form = QFormLayout(widget)
        top = QHBoxLayout()
        self.profiles = QComboBox()
        top.addWidget(self.profiles, 1)
        for ja, en, callback in (("追加", "Add", lambda: self.add_profile(False)),
                                  ("複製", "Duplicate", lambda: self.add_profile(True)),
                                  ("削除", "Delete", self.remove_profile)):
            button = QPushButton(t(ja, en))
            button.clicked.connect(callback)
            top.addWidget(button)
        form.addRow(top)
        self.fields = {key: QLineEdit() for key in ("name", "endpoint", "apiKey")}
        self.fields["apiKey"].setEchoMode(QLineEdit.EchoMode.Password)
        self.model = QComboBox()
        self.model.setEditable(True)
        for key, ja, en in (("name", "名前", "Name"), ("endpoint", "接続先 URL", "Endpoint URL"),
                            ("apiKey", "API キー", "API key")):
            form.addRow(t(ja, en), self.fields[key])
        models = QHBoxLayout()
        models.addWidget(self.model, 1)
        test_button = QPushButton(t("接続をテスト", "Test connection"))
        test_button.clicked.connect(self.test_connection)
        models.addWidget(test_button)
        form.addRow(t("モデル", "Model"), models)
        self.service_status = QLabel()
        self.service_status.setWordWrap(True)
        form.addRow(self.service_status)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 2)
        self.temperature.setDecimals(2)
        self.temperature.setSingleStep(.1)
        form.addRow("Temperature", self.temperature)
        self.streaming = QCheckBox(t("応答をストリーミング", "Stream the response"))
        form.addRow(self.streaming)
        self.prompts = {key: QTextEdit() for key in ("systemPrompt", "prompt", "extra")}
        for key, ja, en in (("systemPrompt", "システムプロンプト", "System prompt"),
                            ("prompt", "ユーザープロンプト", "User prompt"), ("extra", "追加 JSON", "Extra JSON")):
            self.prompts[key].setFixedHeight(64)
            form.addRow(t(ja, en), self.prompts[key])
        self.profiles.currentIndexChanged.connect(self.profile_selected)
        self.reset_profiles(self.data["activeProfile"])
        return widget

    def store_profile(self):
        if self.profile_key is None:
            return
        profile = self.data["profiles"][self.profile_key]
        profile.update({key: field.text() for key, field in self.fields.items()})
        profile["model"] = self.model.currentText()
        profile["temperature"] = self.temperature.value()
        profile["stream"] = self.streaming.isChecked()
        for key, editor in self.prompts.items():
            profile[key] = editor.toPlainText()

    def show_profile(self, key):
        self.profile_key = key
        profile = self.data["profiles"][key]
        for name, widget in self.fields.items():
            widget.setText(profile[name])
        self.model.clear()
        self.model.setEditText(profile["model"])
        self.temperature.setValue(profile["temperature"])
        self.streaming.setChecked(profile["stream"])
        self.prompts["systemPrompt"].setPlainText(profile["systemPrompt"])
        self.prompts["prompt"].setPlainText(profile["prompt"])
        extra = profile["extra"]
        self.prompts["extra"].setPlainText(extra if isinstance(extra, str) else json.dumps(extra, ensure_ascii=False, indent=2))
        self.service_status.clear()

    def reset_profiles(self, current):
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for key, profile in self.data["profiles"].items():
            self.profiles.addItem(profile["name"], key)
        self.profiles.setCurrentIndex(self.profiles.findData(current))
        self.profiles.blockSignals(False)
        self.show_profile(current)

    def profile_selected(self, index):
        if index >= 0:
            self.store_profile()
            self.show_profile(self.profiles.itemData(index))

    def add_profile(self, duplicate):
        self.store_profile()
        profile = deepcopy(self.data["profiles"][self.profile_key] if duplicate else DEFAULT_PROFILE)
        profile["name"] += self.translate(" のコピー", " copy") if duplicate else self.translate(" 新規", " (new)")
        key = self.preferences.profile_id()
        self.data["profiles"][key] = profile
        self.reset_profiles(key)

    def remove_profile(self):
        if len(self.data["profiles"]) > 1:
            self.data["profiles"].pop(self.profile_key)
            self.reset_profiles(next(iter(self.data["profiles"])))

    def test_connection(self):
        t = self.translate
        self.store_profile()
        key = self.profile_key
        profile = self.data["profiles"][key]
        self.service_status.setText(t("接続を確認中…", "Checking connection…"))

        def received(models, error):
            if self.profile_key != key or not self.isVisible():
                return
            if error:
                self.service_status.setText(error)
                return
            model = self.model.currentText()
            self.model.clear()
            self.model.addItems(models)
            self.model.setEditText(model or (models[0] if models else ""))
            self.service_status.setText(t(f"接続成功。モデル {len(models)} 件", f"Connected. {len(models)} models"))

        self.translations.models(profile, received)

    def commit(self):
        self.store_profile()
        t = self.translate
        data = deepcopy(self.data)
        for profile in data["profiles"].values():
            try:
                request_url(profile["endpoint"])
                if not profile["name"].strip() or not profile["model"].strip():
                    raise ValueError(t("名前とモデルを入力してください。", "Enter a name and model."))
                if "${sourceText}" not in profile["prompt"]:
                    raise ValueError(t("ユーザープロンプトに ${sourceText} を入れてください。",
                                       "Include ${sourceText} in the user prompt."))
                if isinstance(profile["extra"], str):
                    profile["extra"] = json.loads(profile["extra"] or "{}")
                if not isinstance(profile["extra"], dict):
                    raise ValueError(t("追加 JSON はオブジェクトを指定してください。", "Extra JSON must be an object."))
                json.dumps(profile["extra"], allow_nan=False)
            except (TypeError, ValueError) as error:
                QMessageBox.warning(self, t("翻訳設定", "Translation settings"), str(error))
                return
        data.update(activeProfile=self.profile_key, language=self.language.currentData(),
                    concurrency=self.concurrent.value(), backupCount=self.backups.value(),
                    autoSave=self.auto_save.isChecked(), opacity=self.opacity.value())
        data.update({key: field.text().strip() for key, field in self.languages.items()})
        data["rules"] = {key: field.isChecked() for key, field in self.checks.items()}
        data["rules"].update(edge_band=self.edge_range.value(), small_text=self.small_size.value())
        try:
            self.preferences.save(data)
        except OSError as error:
            QMessageBox.warning(self, t("設定を保存できませんでした", "Could not save settings"), str(error))
            return
        self.accept()


class TranslationDialog(QDialog):
    def __init__(self, pane, choices=None, point=None, key=None, source="", comment="", color=None):
        super().__init__(pane, Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.pane, self.document = pane, pane.document
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
        self.work_screen = None
        self.screen_monitoring = False
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
            excerpt = QTextEdit()
            excerpt.setReadOnly(True)
            excerpt.setPlainText("\n\n".join(headings[key] + "\n" + "\n".join(parts)
                                             for key, parts in items.items()))
            excerpt.setMaximumHeight(110)
            excerpt.hide()
            details.toggled.connect(excerpt.setVisible)
            form.addRow(details)
            form.addRow(excerpt)
            include = QCheckBox(t("除外した箇所を翻訳に含める", "Restore excluded text"))
            include.toggled.connect(self.include)
            include.toggled.connect(lambda restored: include.setText(
                t("除外した箇所も翻訳します", "Uncheck to reapply exclusions") if restored
                else t("除外した箇所を翻訳に含める", "Restore excluded text")))
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
        if self.isVisible() and not self.fitting and self.work_screen is not None:
            self.preferred_size = event.size()

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
        self.status.clear()

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
                  "failed": self.job.error, "finished": "", "canceled": t("取り消しました", "Canceled")}
        self.status.setText(labels[self.job.state])
        if self.job.state in ("running", "finished"):
            self.output.setPlainText(self.job.output)
        running = self.job.state in ("running", "waiting")
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
