"""Small layouts shared by the outline and annotation sidebar."""

from PySide6.QtCore import QEvent, QPointF, QSize, QTimer
from PySide6.QtGui import QAbstractTextDocumentLayout, QPainter, QPalette, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QGridLayout, QLabel, QScrollArea, QSizePolicy, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTreeWidget, QWidget,
)


def wrapped_document(text, font, width):
    doc = QTextDocument()
    doc.setDefaultFont(font)
    doc.setDocumentMargin(0)
    option = doc.defaultTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    doc.setDefaultTextOption(option)
    doc.setPlainText(text)
    doc.setTextWidth(max(1, width))
    return doc


def draw_document(doc, painter, point, palette, selected=False):
    painter.save()
    painter.translate(point)
    context = QAbstractTextDocumentLayout.PaintContext()
    context.palette = QPalette(palette)
    if selected:
        context.palette.setColor(QPalette.ColorRole.Text, palette.highlightedText().color())
    doc.documentLayout().draw(painter, context)
    painter.restore()


class WrappedLabel(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.documents = {}
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self):
        return True

    def setText(self, text):
        if text != self.text():
            self.documents.clear()
            super().setText(text)

    def document_at_width(self, width):
        font = self.font()
        key = max(1, width), font.key()
        if key not in self.documents:
            if len(self.documents) >= 2:
                self.documents.pop(next(iter(self.documents)))
            self.documents[key] = wrapped_document(self.text(), font, width)
        return self.documents[key]

    def heightForWidth(self, width):
        return round(self.document_at_width(width).size().height() + .5)

    def sizeHint(self):
        return QSize(1, self.heightForWidth(max(1, self.width())))

    def minimumSizeHint(self):
        return QSize(1, self.fontMetrics().height())

    def paintEvent(self, event):
        painter = QPainter(self)
        draw_document(self.document_at_width(self.width()), painter, QPointF(), self.palette())
        painter.end()


class WrappingOutline(QTreeWidget):
    def __init__(self):
        super().__init__()
        from PySide6.QtCore import Qt
        self.setHeaderHidden(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setUniformRowHeights(False)
        self.setIndentation(14)
        self.setItemDelegate(OutlineDelegate(self))
        self.relayout = QTimer(self, singleShot=True, interval=0)
        self.relayout.timeout.connect(self.doItemsLayout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.relayout.start()


class OutlineDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        tree = self.parent()
        depth = 1
        parent = index.parent()
        while parent.isValid():
            depth += 1
            parent = parent.parent()
        width = max(25, tree.viewport().width() - depth * tree.indentation() - 7)
        doc = wrapped_document(index.data() or "", option.font, width)
        return QSize(1, round(doc.size().height() + 5))

    def paint(self, painter, option, index):
        item = QStyleOptionViewItem(option)
        self.initStyleOption(item, index)
        text, item.text = item.text, ""
        tree = self.parent()
        tree.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, item, painter, tree)
        rectangle = item.rect.adjusted(3, 2, -4, -2)
        doc = wrapped_document(text, item.font, max(25, rectangle.width()))
        painter.save()
        painter.setClipRect(item.rect)
        draw_document(doc, painter, rectangle.topLeft(), item.palette,
                      bool(item.state & QStyle.StateFlag.State_Selected))
        painter.restore()


class ButtonGrid(QWidget):
    """Lay out short rows of controls according to their size hints and actual width."""

    def __init__(self, buttons):
        super().__init__()
        self.buttons = buttons
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.columns = 0
        self.flow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.flow()

    def flow(self, width=None):
        if not self.buttons:
            return
        button_width = max(b.sizeHint().width() for b in self.buttons)
        width = self.width() if width is None else width
        columns = max(1, min(len(self.buttons), (width + 4) // (button_width + 4)))
        if columns == self.columns and self.grid.count() == len(self.buttons):
            return
        self.columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index, button in enumerate(self.buttons):
            self.grid.addWidget(button, index // columns, index % columns)
        self.setFixedHeight(self.grid.sizeHint().height())
