"""Application settings and translation service profiles."""

from __future__ import annotations

from copy import deepcopy
import json

from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import font_ui, fonts
from .analysis_rules import LABELS, RULES
from .dialog_helpers import ask, spin, warn
from .translation import DEFAULT_PROFILE, request_url


AREA_TEXT = {
    "menu": (("メニュー", "メニューバーと、開いたメニュー"), ("Menus", "the menu bar and opened menus")),
    "toolbar": (("ツールバー", "ボタンの行、ページ欄、検索の一致数"),
                ("Toolbar", "the button row, page box and match count")),
    "tab": (("タブ", "タブの並びと、開いている PDF の一覧"), ("Tabs", "the tab strip and the list of open PDFs")),
    "outline": (("目次", "左の目次"), ("Outline", "the table of contents")),
    "notes": (("注釈", "注釈のパネルとカード"), ("Annotations", "the annotation panel and cards")),
    "translation": (("翻訳ウィンドウ", "翻訳の結果の窓"), ("Translation window", "the translation result window")),
    "dialog": (("ダイアログ", "設定、プロパティ、確認の窓、ツールチップなど（メインウィンドウの本体は変わりません）"),
               ("Dialogs", "Settings, Properties, message boxes, tooltips and the rest "
                           "(the main window itself is not affected)")),
    "notice": (("お知らせ", "一時的な通知"), ("Notices", "temporary messages")),
}


class SettingsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.fonts = window.fonts
        self.setFont(self.fonts.font("dialog"))
        self.preferences = window.preferences
        self.translate = self.preferences.tr_text
        self.translations = window.controller.documents.translations
        self.data = deepcopy(self.preferences.data)
        # The sizes as they were when this dialog opened; Save writes only the rows that differ.
        self.font_opened = {area.key: self.data[area.key] for area in fonts.AREAS}
        self.profile_key = None
        self.stop_test_handle = None
        self.finished.connect(self.stop_test)
        # The profile used for translation. Editing another profile never changes it.
        self.active_profile = self.data["activeProfile"]
        t = self.translate
        self.resize(620, 650)
        self.setWindowTitle(t("設定", "Settings"))
        layout = QVBoxLayout(self)
        self.tabs = tabs = QTabWidget()
        layout.addWidget(tabs)
        general = QWidget()
        form = QFormLayout(general)
        self.language = QComboBox()
        self.language.addItem("日本語", "ja")
        self.language.addItem("English", "en")
        self.language.setCurrentIndex(self.language.findData(self.data["language"]))
        form.addRow(t("表示言語", "Display language"), self.language)
        self.languages = {key: QLineEdit(self.data[key]) for key in ("langFrom", "langTo")}
        self.languages["langFrom"].setPlaceholderText(t("例: en", "e.g. en"))
        self.languages["langTo"].setPlaceholderText(t("例: ja", "e.g. ja"))
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
        tabs.addTab(self.font_tab(), t("文字サイズ", "Font size"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(t("保存", "Save"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(t("キャンセル", "Cancel"))
        buttons.accepted.connect(self.commit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        font_ui.fit_dialog_font(self, self.fonts, window)

    def font_tab(self):
        t = self.translate
        page = QWidget()
        outer = QVBoxLayout(page)
        inner = QWidget()
        grid = QGridLayout(inner)
        self.font_rows = {}
        language = 1 if self.data["language"] == "en" else 0
        for row, area in enumerate(fonts.AREAS):
            name, note = AREA_TEXT[area.id][language]
            label = QLabel(f"{name}: {note}")
            label.setWordWrap(True)
            box = spin(fonts.MINIMUM_PERCENT, fonts.MAXIMUM_PERCENT, self.data[area.key])
            box.setSuffix("%")
            size = QLabel()
            sample = QLabel(t("見本 Aa あ 123", "Sample Aa 123"))
            reset = QPushButton(t(f"初期値 {area.default}%", f"Default {area.default}%"))
            box.valueChanged.connect(lambda _=0, area=area: self.show_font_row(area))
            reset.clicked.connect(lambda _=False, area=area: self.font_rows[area.id][0].setValue(area.default))
            for column, widget in enumerate((label, box, size, sample, reset)):
                grid.addWidget(widget, row, column)
            grid.setColumnStretch(0, 1)
            self.font_rows[area.id] = (box, size, sample)
            self.show_font_row(area)
        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setWidget(inner)
        outer.addWidget(scroller, 1)
        everything = QPushButton(t("すべて初期値へ戻す", "Reset all to defaults"))
        everything.clicked.connect(lambda: [self.font_rows[area.id][0].setValue(area.default)
                                            for area in fonts.AREAS])
        outer.addWidget(everything, 0, Qt.AlignmentFlag.AlignLeft)
        exceptions = QLabel(t("次は、文字サイズを変えられません: ファイルを選ぶ窓、ウィンドウの題名、起動時の失敗を知らせる窓。"
                              "設定画面は、画面に収まる大きさに自動で縮みます。",
                              "These cannot change size: file dialogs, window titles, and startup error messages. "
                              "The Settings window shrinks automatically to fit the screen."))
        exceptions.setWordWrap(True)
        outer.addWidget(exceptions)
        return page

    def show_font_row(self, area):
        box, size, sample = self.font_rows[area.id]
        pt = self.fonts.pt(area.id, box.value())
        at_floor = area.floored and pt == fonts.FLOOR_PT and box.value() * self.fonts.base.pointSizeF() / 100 < pt
        size.setText(self.translate(f"{pt:g} pt（下限）", f"{pt:g} pt (minimum)") if at_floor else f"{pt:g} pt")
        sample.setFont(self.fonts.font(area.id, box.value()))

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
        self.use_profile = QCheckBox(t("このプロファイルを翻訳に使う", "Use this profile for translation"))
        self.use_profile.toggled.connect(self.use_profile_toggled)
        form.addRow(self.use_profile)
        self.fields = {key: QLineEdit() for key in ("name", "endpoint", "apiKey")}
        self.fields["name"].textChanged.connect(self.name_edited)
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
        form.addRow(t("温度 (Temperature)", "Temperature"), self.temperature)
        self.streaming = QCheckBox(t("応答をストリーミング", "Stream the response"))
        form.addRow(self.streaming)
        self.prompts = {key: QTextEdit() for key in ("systemPrompt", "prompt", "extra")}
        for key, ja, en in (("systemPrompt", "システムプロンプト", "System prompt"),
                            ("prompt", "ユーザープロンプト", "User prompt"), ("extra", "追加 JSON", "Extra JSON")):
            form.addRow(t(ja, en), self.prompts[key])
        self.fit_prompt_heights()
        variables = QLabel(t("プロンプトでは ${sourceText}（原文）・${langFrom}（原文の言語）・${langTo}（翻訳先の言語）を使えます。",
                             "Prompts can use ${sourceText}, ${langFrom} (source language) and ${langTo} (target language)."))
        variables.setWordWrap(True)
        form.addRow(variables)
        self.profiles.currentIndexChanged.connect(self.profile_selected)
        self.reset_profiles(self.data["activeProfile"])
        return widget

    def fit_prompt_heights(self):
        """The three prompt boxes show about three lines: 64 px at 9 pt, taller for a larger font."""
        height = max(64, self.fontMetrics().lineSpacing() * 3 + 10)
        for editor in self.prompts.values():
            editor.setFixedHeight(height)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and hasattr(self, "prompts"):
            self.fit_prompt_heights()

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
        active = key == self.active_profile
        blocked = self.use_profile.blockSignals(True)
        self.use_profile.setChecked(active)
        self.use_profile.blockSignals(blocked)
        # Exactly one profile is always in use, so the box can only be turned on.
        self.use_profile.setEnabled(not active)

    def profile_label(self, key, name):
        mark = self.translate(" （使用中）", " (in use)") if key == self.active_profile else ""
        return name + mark

    def name_edited(self, text):
        index = self.profiles.currentIndex()
        if index >= 0 and self.profile_key is not None:
            self.profiles.setItemText(index, self.profile_label(self.profile_key, text))

    def use_profile_toggled(self, checked):
        if checked and self.profile_key is not None:
            self.active_profile = self.profile_key
            self.use_profile.setEnabled(False)
            for index in range(self.profiles.count()):
                key = self.profiles.itemData(index)
                name = self.fields["name"].text() if key == self.profile_key else self.data["profiles"][key]["name"]
                self.profiles.setItemText(index, self.profile_label(key, name))

    def reset_profiles(self, current):
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for key, profile in self.data["profiles"].items():
            self.profiles.addItem(self.profile_label(key, profile["name"]), key)
        self.profiles.setCurrentIndex(self.profiles.findData(current))
        self.profiles.blockSignals(False)
        self.show_profile(current)

    def select_profile(self, key):
        index = self.profiles.findData(key)
        if index >= 0 and index != self.profiles.currentIndex():
            self.profiles.setCurrentIndex(index)

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
        t = self.translate
        if len(self.data["profiles"]) <= 1:
            self.service_status.setText(t("最後のプロファイルは削除できません。", "The last profile cannot be deleted."))
            return
        name = self.fields["name"].text()
        if not ask(self, t("プロファイルの削除", "Delete profile"), t(f"「{name}」を削除しますか？", f'Delete "{name}"?'),
                   t("削除", "Delete"), t("キャンセル", "Cancel"), destructive=True):
            return
        self.data["profiles"].pop(self.profile_key)
        remaining = next(iter(self.data["profiles"]))
        if self.profile_key == self.active_profile:
            self.active_profile = remaining
        self.profile_key = None
        self.reset_profiles(remaining)

    def test_connection(self):
        t = self.translate
        self.store_profile()
        key = self.profile_key
        profile = self.data["profiles"][key]
        self.service_status.setText(t("接続を確認中…", "Checking connection…"))
        self.stop_test()

        def received(models, error):
            self.stop_test_handle = None
            if self.profile_key != key:
                return
            if error:
                self.service_status.setText(self.preferences.localize(error))
                return
            model = self.model.currentText()
            self.model.clear()
            self.model.addItems(models)
            self.model.setEditText(model or (models[0] if models else ""))
            self.service_status.setText(t(f"接続成功。モデル {len(models)} 件", f"Connected. {len(models)} models"))

        self.stop_test_handle = self.translations.models(profile, received)

    def stop_test(self):
        """Abandon a connection test that is still waiting, so nothing calls back into a closed dialog."""
        handle, self.stop_test_handle = self.stop_test_handle, None
        if handle:
            handle()

    def commit(self):
        self.store_profile()
        t = self.translate
        data = deepcopy(self.data)
        if not all(field.text().strip() for field in self.languages.values()):
            self.tabs.setCurrentIndex(0)
            empty = next(field for field in self.languages.values() if not field.text().strip())
            empty.setFocus()
            warn(self, t("翻訳設定", "Translation settings"),
                 t("原文の言語と翻訳先の言語を入力してください。", "Enter the source and target languages."))
            return
        for key, profile in data["profiles"].items():
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
                self.tabs.setCurrentIndex(1)
                self.select_profile(key)
                name = self.data["profiles"][key]["name"].strip() or t("（名前なし）", "(unnamed)")
                reason = self.preferences.localize(str(error))
                warn(self, t("翻訳設定", "Translation settings"), t(f"「{name}」: {reason}", f'"{name}": {reason}'))
                return
        data.update(activeProfile=self.active_profile, language=self.language.currentData(),
                    concurrency=self.concurrent.value(), backupCount=self.backups.value(),
                    autoSave=self.auto_save.isChecked(), opacity=self.opacity.value())
        data.update({key: field.text().strip() for key, field in self.languages.items()})
        data["rules"] = {key: field.isChecked() for key, field in self.checks.items()}
        data["rules"].update(edge_band=self.edge_range.value(), small_text=self.small_size.value())
        # Font sizes: a row changed here is written; any other row keeps the latest saved value, so a
        # save from another window is not undone.
        for area in fonts.AREAS:
            value = self.font_rows[area.id][0].value()
            data[area.key] = value if value != self.font_opened[area.key] else self.preferences.data[area.key]
        try:
            self.preferences.save(data)
        except OSError as error:
            QMessageBox.warning(self, t("設定を保存できませんでした", "Could not save settings"), str(error))
            return
        self.accept()
