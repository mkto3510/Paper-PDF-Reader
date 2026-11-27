"""Application information dialog and the established dialog imports."""

from __future__ import annotations

from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QTabWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from . import APP_NAME, VERSION
from .dialog_helpers import ask, dialog_corner
from .settings_dialog import SettingsDialog
from .translation_dialog import TranslationDialog


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
            self.notice_body.setPlainText(self.t(f"{path.name} を読み込めませんでした。",
                                                  f"Could not read {path.name}."))
            return
        if path.name == "THIRD_PARTY.md":
            self.notice_body.setMarkdown(text)
        else:
            self.notice_body.setPlainText(text)
