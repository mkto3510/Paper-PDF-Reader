# Third-party software and corresponding source

The release uses the Paper PDF Reader source and build scripts at the matching version.

- [Paper PDF Reader](https://github.com/mkto3510/Paper-PDF-Reader)
- PyMuPDF 1.28.2 and MuPDF, AGPL-3.0. [PyMuPDF source and MuPDF revision](https://github.com/pymupdf/PyMuPDF/tree/1.28.2)
- PySide6 Essentials and Shiboken6 6.12.0, LGPL-3.0/GPL options. [Qt for Python source](https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.12.0)
- Qt 6.12.0 shared libraries and plugins. [Qt source archives and module licenses](https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/)
- Python 3.12.14, PSF license. [CPython source](https://github.com/python/cpython/tree/v3.12.14)
- PyInstaller 6.22.3 bootloader, GPL-2.0-or-later with the PyInstaller exception. [PyInstaller source](https://github.com/pyinstaller/pyinstaller/tree/v6.22.3)
- pypdf 7.1.1, BSD-3-Clause. [pypdf source](https://github.com/py-pdf/pypdf/tree/7.1.1)
- PDF.js 6.5.438 SVG icons, Apache-2.0. [Original viewer icons](https://github.com/mozilla/pdf.js/tree/v6.5.438/web/images)
- Installer only, Inno Setup 7. [Inno Setup source](https://github.com/jrsoftware/issrc)

The bundled Qt libraries are dynamically loaded. You may replace those DLLs
with compatible builds for debugging modifications. The translation model
and server are installed and run independently.
