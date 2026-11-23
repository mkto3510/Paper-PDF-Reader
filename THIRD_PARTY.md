# 同梱ソフトウェアとソースコード

配布版のビルドには、同じバージョンの Paper PDF Viewer のソースコードとビルドスクリプトを使います。

- [Paper PDF Viewer](https://github.com/mkto3510/Paper-PDF-Reader)
- PyMuPDF 1.28.2 と MuPDF（AGPL-3.0）。[PyMuPDF のソースコードと MuPDF のリビジョン](https://github.com/pymupdf/PyMuPDF/tree/1.28.2)
- PySide6 Essentials と Shiboken6 6.12.0（LGPL-3.0 または GPL）。[Qt for Python のソースコード](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0)
- Qt 6.12.0 の共有ライブラリとプラグイン。[Qt のソースコードとモジュールごとのライセンス](https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/)
- Python 3.12.14（PSF ライセンス）。[CPython のソースコード](https://github.com/python/cpython/tree/v3.12.14)
- PyInstaller 6.22.3 のブートローダー（GPL-2.0-or-later、PyInstaller の例外付き）。[PyInstaller のソースコード](https://github.com/pyinstaller/pyinstaller/tree/v6.22.3)
- pypdf 7.1.1（BSD-3-Clause）。[pypdf のソースコード](https://github.com/py-pdf/pypdf/tree/7.1.1)
- PDF.js 6.5.438 の SVG アイコン（Apache-2.0）。[ビューアの元のアイコン](https://github.com/mozilla/pdf.js/tree/v6.5.438/web/images)
- Inno Setup 7（インストーラーのみ）。[Inno Setup のソースコード](https://github.com/jrsoftware/issrc)

同梱の Qt ライブラリは実行時に読み込まれます。変更内容をデバッグするときは、互換性のある DLL に差し替えられます。翻訳モデルと翻訳サーバーは別途インストールして起動します。
