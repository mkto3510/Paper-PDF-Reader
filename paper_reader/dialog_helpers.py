"""Shared Qt helpers for dialog prompts, fields, and placement."""

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMessageBox, QSpinBox


def spin(low, high, value):
    box = QSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    return box


def ask(parent, title, text, accept, reject, destructive=False, default_accept=False):
    """Two-button question with caller-supplied labels. Returns True when the first button is chosen."""
    box = QMessageBox(QMessageBox.Icon.Question, title, text, QMessageBox.StandardButton.NoButton, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)  # profile names and file names are shown as typed
    yes = box.addButton(accept, QMessageBox.ButtonRole.DestructiveRole if destructive else QMessageBox.ButtonRole.AcceptRole)
    no = box.addButton(reject, QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(yes if default_accept else no)
    box.setEscapeButton(no)
    box.exec()
    chosen = box.clickedButton() is yes
    box.deleteLater()
    return chosen


def warn(parent, title, text):
    """A warning that shows its text as typed (a profile name such as "<b>x" is not rich text)."""
    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.exec()
    box.deleteLater()


def dialog_corner(work_area, frame_size, preferred):
    x = max(work_area.x(), min(preferred.x(), work_area.x() + work_area.width() - frame_size.width()))
    y = max(work_area.y(), min(preferred.y(), work_area.y() + work_area.height() - frame_size.height()))
    return QPoint(x, y)
