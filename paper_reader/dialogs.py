"""Reader settings and the explicit translation dialog."""

from __future__ import annotations

from copy import deepcopy
import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .analysis_rules import LABELS, RULES
from .preferences import DEFAULTS
from .translation import DEFAULT_PROFILE, request_url


def spin(low, high, value):
    box = QSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    return box


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
        rules.addRow(t("ページ上下端の探索範囲", "Header/footer detection range"), self.edge_range)
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
    def __init__(self, pane, choices=None, point=None, key=None, source="", comment=""):
        super().__init__(pane, Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.pane, self.document = pane, pane.document
        self.preferences = pane.preferences
        self.translations = self.document.store.translations
        self.choices, self.key = choices, key
        self.job = None
        self.selected = choices[0] if choices else None
        t = pane.t
        self.setWindowTitle(t("翻訳", "Translate"))
        self.resize(*self.preferences.data["dialogSize"])
        layout = QVBoxLayout(self)
        form = QFormLayout()
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
        if self.selected and self.selected.skipped:
            include = QCheckBox(t("除外した箇所を含める", "Include excluded text"))
            include.setToolTip(" / ".join(t(*LABELS[name]) for name in self.selected.skipped if name in LABELS))
            include.toggled.connect(self.include)
            form.addRow(include)
        layout.addLayout(form)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        layout.addLayout(row)
        self.start = QPushButton(t("翻訳", "Translate"))
        self.start.clicked.connect(self.preview)
        self.source.textChanged.connect(self.update_start)
        self.update_start()
        row.addWidget(self.start)
        self.save = QPushButton(t("注釈に保存", "Save to annotation"))
        self.save.clicked.connect(self.accept_annotation)
        row.addWidget(self.save)
        reset = QPushButton(t("既定のサイズ", "Reset size"))
        reset.clicked.connect(lambda: self.resize(*DEFAULTS["dialogSize"]))
        row.addWidget(reset)
        for widget, callback in ((self.source, self.preview), (self.output, self.accept_annotation)):
            shortcut = QShortcut(QKeySequence("Ctrl+Return"), widget)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(callback)
            enter = QShortcut(QKeySequence("Ctrl+Enter"), widget)
            enter.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            enter.activated.connect(callback)
        QShortcut(QKeySequence("Alt+T"), self, activated=self.preview)
        self.finished.connect(self.cleanup)
        if point is not None:
            screen = self.screen().availableGeometry()
            self.move(min(point.x(), screen.right() - self.width()),
                      min(point.y() + 8, screen.bottom() - self.height()))

    def include(self, enabled):
        if self.job and self.job.state in ("running", "waiting"):
            self.job.cancel()
        self.selected = self.choices[int(enabled)]
        self.source.setPlainText(self.selected.source)
        self.output.clear()

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
            self.document.add_highlight(self.selected.quads, self.pane.last_color, "" if running else comment,
                                        attached, job.cancel if running else None)
        elif running:
            job.cancel()
        self.accept()

    def cleanup(self):
        if self.job:
            self.job.changed.disconnect(self.update_job)
            if self.job.state in ("running", "waiting"):
                self.job.cancel()
        size = [self.width(), self.height()]
        if size != self.preferences.data["dialogSize"]:
            try:
                self.preferences.save(self.preferences.data | {"dialogSize": size})
            except OSError as error:
                self.pane.message.emit(str(error))
