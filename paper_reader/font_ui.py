"""Qt side of the per-area font sizes: the base font, the area fonts and the menu classes."""

from PySide6.QtCore import QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QMenu, QMenuBar

from . import fonts


_base_font = None


def process_base_font():
    """The application font as the OS gave it; taken once per process, before any setFont."""
    global _base_font
    if _base_font is None:
        font = QFont(QApplication.font())
        if font.pointSizeF() <= 0:
            font.setPointSizeF(fonts.FALLBACK_BASE_PT)
        _base_font = font
    return QFont(_base_font)


def reset_base_font():
    """Forget the stored base font (for tests that fix their own base)."""
    global _base_font
    _base_font = None


class AreaFonts:
    """The fonts of the eight areas, computed from the base font and the saved percentages."""

    def __init__(self, base, preferences):
        self.base = QFont(base)
        self.preferences = preferences

    def base_font(self):
        font = QFont(self.base)
        font.setPointSizeF(self.base.pointSizeF())  # always an explicit size, so later app-font changes are not inherited
        return font

    def percent(self, area):
        return self.preferences.data[fonts.AREA_BY_ID[area].key]

    def pt(self, area, percent=None):
        spec = fonts.AREA_BY_ID[area]
        return fonts.effective_pt(self.base.pointSizeF(), self.percent(area) if percent is None else percent,
                                  spec.floored)

    def font(self, area, percent=None):
        font = QFont(self.base)
        font.setPointSizeF(self.pt(area, percent))
        return font

    def edge(self):
        return fonts.icon_edge(self.percent("toolbar"))


class AreaMenu(QMenu):
    """A menu that carries the menu area's font (popups do not inherit one from their parent)."""

    def __init__(self, fonts_, parent):
        super().__init__(parent)
        self.fonts = fonts_
        self.setFont(fonts_.font("menu"))

    def addMenu(self, *args):
        return add_area_menu(self, *args)


class AreaMenuBar(QMenuBar):
    def __init__(self, fonts_, parent):
        super().__init__(parent)
        self.fonts = fonts_
        self.setFont(fonts_.font("menu"))

    def addMenu(self, *args):
        return add_area_menu(self, *args)


def fit_dialog_font(dialog, fonts_, parent):
    """Lower the dialog's font from the dialog area's size until its smallest size fits the screen.

    The floor is 7 pt. The dialog is also resized to the available space, so it can always be reached
    and saved even at 300%. Decided once, when the dialog opens.
    """
    available = parent.screen().availableGeometry()
    frame, inner = parent.frameGeometry(), parent.geometry()
    limit = QSize(available.width() - (frame.width() - inner.width()),
                  available.height() - (frame.height() - inner.height()))
    pt = fonts_.pt("dialog")
    while True:
        font = fonts_.base_font()
        font.setPointSizeF(pt)
        dialog.setFont(font)
        dialog.layout().activate()
        hint = dialog.minimumSizeHint()
        if (hint.width() <= limit.width() and hint.height() <= limit.height()) or pt <= fonts.FLOOR_PT:
            break
        pt = max(fonts.FLOOR_PT, pt - .5)
    dialog.resize(min(dialog.width(), limit.width()), min(dialog.height(), limit.height()))
    return pt


def add_area_menu(owner, *args):
    """addMenu(title) and addMenu(icon, title) create an AreaMenu; addMenu(menu) passes through."""
    if args and isinstance(args[0], QMenu):
        return QMenu.addMenu(owner, args[0]) if isinstance(owner, QMenu) else QMenuBar.addMenu(owner, args[0])
    menu = AreaMenu(owner.fonts, owner)
    if len(args) == 1:
        menu.setTitle(args[0])
    else:
        menu.setIcon(args[0])
        menu.setTitle(args[1])
    (QMenu if isinstance(owner, QMenu) else QMenuBar).addMenu(owner, menu)
    return menu
