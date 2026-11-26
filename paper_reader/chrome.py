"""Window menu, document toolbar and open-PDF picker."""

from collections import Counter

from PySide6.QtCore import QEvent, QPoint, QSize, QTimer, Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QMenuBar,
    QSizePolicy, QStackedWidget, QTabBar, QToolButton, QVBoxLayout, QWidget,
)

from .font_ui import AreaMenu, AreaMenuBar
from .icons import icon


MINIMUM_WIDTH = 530


class DocumentTabs(QTabBar):
    def __init__(self):
        super().__init__()
        self.setExpanding(False)
        self.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.setUsesScrollButtons(True)

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        return QSize(max(120, min(220, size.width())), size.height())

    def minimumTabSizeHint(self, index):
        return QSize(120, super().minimumTabSizeHint(index).height())


class TabList(QFrame):
    def __init__(self, window, button):
        super().__init__(window, Qt.WindowType.Popup)
        self.window, self.button = window, button
        self.matches = []
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        self.query = QLineEdit()
        self.query.installEventFilter(self)
        self.query.textChanged.connect(self.refresh)
        self.query.returnPressed.connect(self.activate_current)
        layout.addWidget(self.query)
        self.items = QListWidget()
        self.items.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.items.itemClicked.connect(lambda _: self.activate_current())
        self.items.itemActivated.connect(lambda _: self.activate_current())
        layout.addWidget(self.items)

    def open_list(self):
        self.setFont(self.window.fonts.font("tab"))
        self.query.clear()
        self.refresh()
        self.resize(420, 360)
        position = self.button.mapToGlobal(self.button.rect().bottomRight())
        limit = self.button.screen().availableGeometry()
        self.resize(min(self.width(), limit.width()), min(self.height(), limit.height()))
        x = max(limit.left(), position.x() - self.width() + 1)
        y = min(position.y(), limit.bottom() - self.height() + 1)
        self.move(x, max(limit.top(), y))
        self.show()
        self.query.setFocus()

    def relabel(self):
        label = self.window.t("開いているPDFを検索", "Search open PDFs")
        self.query.setPlaceholderText(label)
        self.query.setAccessibleName(label)

    def refresh(self):
        tabs = self.window.tabs
        selected = self.matches[self.items.currentRow()] if 0 <= self.items.currentRow() < len(self.matches) else None
        panes = [tabs.widget(i) for i in range(tabs.count())]
        duplicate_names = Counter(pane.document.path.name.casefold() for pane in panes)
        identical_paths = Counter(pane.document.key for pane in panes)
        tokens = self.query.text().casefold().split()
        self.items.clear()
        self.matches = []
        for index, pane in enumerate(panes):
            doc = pane.document
            if not all(token in str(doc.path).casefold() for token in tokens):
                continue
            title = doc.path.name
            if duplicate_names[title.casefold()] > 1:
                title += " · " + doc.path.parent.name
            if identical_paths[doc.key] > 1:
                title += " · " + self.window.t(f"タブ {index + 1}", f"Tab {index + 1}")
            if self.window.controller.has_unsaved_changes(doc):
                title += " *"
            item = QListWidgetItem(("✓  " if pane is tabs.currentWidget() else "") + title)
            item.setToolTip(str(doc.path))
            self.items.addItem(item)
            self.matches.append(pane)
        if self.matches:
            target = selected if selected in self.matches else tabs.currentWidget()
            self.items.setCurrentRow(self.matches.index(target) if target in self.matches else 0)

    def activate_current(self):
        row = self.items.currentRow()
        if 0 <= row < len(self.matches):
            pane = self.matches[row]
            if self.window.tabs.indexOf(pane) >= 0:
                self.window.tabs.setCurrentWidget(pane)
                if self.window.pane is pane:
                    self.hide()
                    pane.view.setFocus()

    def eventFilter(self, target, event):
        if target is self.query and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Escape:
                self.hide()
                return True
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down):
                if self.items.count():
                    delta = 1 if event.key() == Qt.Key.Key_Down else -1
                    self.items.setCurrentRow((self.items.currentRow() + delta) % self.items.count())
                return True
        return super().eventFilter(target, event)


class Header(QWidget):
    GROUPS = (("open",), ("back", "page_previous", "page", "total", "page_next"),
              ("single", "spread", "left", "right"), ("width", "fit", "smaller", "bigger", "zoom"), ("search",))
    OVERFLOW_ORDER = (("left", "right"), ("smaller", "bigger"), ("width", "fit"),
                      ("single", "spread"), ("page_previous", "page_next"))

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)
        self.menus = AreaMenuBar(window.fonts, self)
        self.menus.setNativeMenuBar(False)
        layout.addWidget(self.menus)
        row = QWidget(self)
        row.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.installEventFilter(self)
        layout.addWidget(row)
        self.tools = QHBoxLayout(row)
        self.tools.setContentsMargins(6, 2, 6, 3)
        self.tools.setSpacing(3)
        self.page = QLineEdit()
        self.page.setMaximumWidth(54)
        self.page.setMinimumWidth(42)
        self.page.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.page_validator = QIntValidator(1, 1, self.page)
        self.page.setValidator(self.page_validator)
        self.page.returnPressed.connect(self.move_to_page)
        self.page.editingFinished.connect(window.update_status)
        self.total = QLabel()
        self.zoom = QToolButton()
        self.zoom.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.search = QStackedWidget()
        self.empty_search = QWidget()
        self.search.addWidget(self.empty_search)
        self.search.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.search.hide()
        self.buttons = {}
        self.separators = []
        fields = {"page": self.page, "total": self.total, "zoom": self.zoom, "search": self.search}
        self.controls = dict(fields)
        for index, group in enumerate(self.GROUPS):
            if index:
                divider = QFrame(row, frameShape=QFrame.Shape.VLine)
                divider.setFixedSize(1, 17)
                self.tools.addWidget(divider, alignment=Qt.AlignmentFlag.AlignVCenter)
                self.separators.append(divider)
            for name in group:
                if name not in fields:
                    button = QToolButton(row)
                    self.controls[name] = self.buttons[name] = button
                self.tools.addWidget(self.controls[name])
        self.tools.addStretch()
        self.more = QToolButton()
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.overflow = AreaMenu(window.fonts, self.more)
        self.more.setMenu(self.overflow)
        self.tools.addWidget(self.more)
        self.hidden_tools = None
        self.external = QLabel()
        self.notice = QLabel(window)
        self.notice.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.notice.setStyleSheet("background: palette(window); color: palette(window-text); "
                                  "border-bottom: 1px solid palette(mid);")
        for message in (self.external, self.notice):
            message.setContentsMargins(8, 3, 8, 3)
            message.setTextFormat(Qt.TextFormat.PlainText)
            message.setWordWrap(True)
            message.hide()
        layout.addWidget(self.external)
        self.notice_timeout = QTimer(self, singleShot=True, interval=6500)
        self.notice_timeout.timeout.connect(self.clear_notice)
        self.timer = QTimer(self, singleShot=True, interval=0)
        self.timer.timeout.connect(self.fit_tools)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.timer.start()
        self.place_notice()

    def eventFilter(self, target, event):
        if event.type() == QEvent.Type.LayoutRequest and hasattr(self, "timer"):
            self.timer.start()
        return super().eventFilter(target, event)

    def set_external(self, message):
        if self.external.text() != message:
            self.external.setText(message)
            self.external.setVisible(bool(message))

    def place_notice(self):
        if self.notice.isHidden():
            return
        width = self.width()
        top = self.mapTo(self.window, self.rect().bottomLeft()).y() + 1
        self.notice.setGeometry(0, top, width, self.notice.heightForWidth(width))
        self.notice.raise_()

    def show_notice(self, message):
        self.notice.setText(message)
        self.notice.setVisible(bool(message))
        self.place_notice()
        self.notice_timeout.start()

    def clear_notice(self):
        self.notice.clear()
        self.notice.hide()
        self.notice_timeout.stop()

    def move_to_page(self):
        pane = self.window.pane
        if pane and self.page.hasAcceptableInput():
            pane.navigate(int(self.page.text()) - 1)
            pane.view.setFocus()

    def bind(self, actions, zoom_menu):
        self.zoom.setMenu(zoom_menu)
        self.hidden_tools = None
        for name, button in self.buttons.items():
            button.setDefaultAction(actions[name][0])
            button.setAccessibleName(actions[name][0].text())
        self.more.setIcon(icon(self, "more"))
        self.more.setToolTip(self.window.t("その他", "More"))
        self.more.setAccessibleName(self.more.toolTip())
        self.page.setAccessibleName(self.window.t("ページ番号", "Page number"))
        self.zoom.setAccessibleName(self.window.t("倍率", "Zoom"))
        self.zoom.setToolTip(self.zoom.accessibleName())
        self.timer.start()

    def select_pane(self, pane):
        panel = pane.search_panel if pane else self.empty_search
        panel.installEventFilter(self)
        self.search.setCurrentWidget(panel)
        self.search.setVisible(pane is not None)
        self.timer.start()

    def reveal_search(self):
        self.search.show()
        self.timer.start()

    def fit_tools(self):
        if not self.window.actions:
            return
        search_visible = not self.search.isHidden()
        search_width = self.search.currentWidget().minimumSizeHint().width() if search_visible else 0
        self.search.setMinimumWidth(max(0, search_width))
        widths = {name: min(control.maximumWidth(), max(control.minimumWidth(), control.sizeHint().width()))
                  for name, control in self.controls.items()}
        widths["search"] = search_width
        margins = self.tools.contentsMargins()
        spacing = self.tools.spacing()
        visible = [tuple(name for name in group if name != "search" or search_visible) for group in self.GROUPS]

        def required(hidden):
            groups = [tuple(name for name in group if name not in hidden) for group in visible]
            groups = [group for group in groups if group]
            separators = len(groups) - 1
            count = sum(map(len, groups)) + separators + bool(hidden)
            return (margins.left() + margins.right() + separators + spacing * (count - 1)
                    + sum(widths[name] for group in groups for name in group)
                    + (self.more.sizeHint().width() if hidden else 0))

        maximum_hidden = frozenset(name for pair in self.OVERFLOW_ORDER for name in pair)
        menu_width = max(self.menus.sizeHint().width(),
                         max((self.menus.actionGeometry(action).right() + 1 for action in self.menus.actions()),
                             default=0))
        self.window.setMinimumWidth(max(MINIMUM_WIDTH, menu_width, required(maximum_hidden)))
        hidden = set()
        for pair in self.OVERFLOW_ORDER:
            if required(hidden) <= self.width():
                break
            hidden.update(pair)
        focus = QApplication.focusWidget()
        for name, button in self.buttons.items():
            button.setVisible(name not in hidden)
        for divider, group in zip(self.separators, visible[1:]):
            divider.setVisible(any(name not in hidden for name in group))
        self.more.setVisible(bool(hidden))
        if focus is self.more and not hidden:
            (self.buttons["back"] if self.buttons["back"].isEnabled() else self.buttons["open"]).setFocus()
        elif focus in (self.buttons[name] for name in hidden):
            self.more.setFocus()
        if hidden != self.hidden_tools:
            self.hidden_tools = hidden
            self.overflow.clear()
            for group in self.GROUPS:
                actions = [self.window.actions[name][0] for name in group if name in hidden]
                if actions and not self.overflow.isEmpty():
                    self.overflow.addSeparator()
                self.overflow.addActions(actions)

    def set_location(self, pane):
        ready = pane is not None and pane.document.info and not pane.document.loading
        self.page.setEnabled(bool(ready))
        self.zoom.setEnabled(bool(ready))
        if not ready:
            self.page.clear()
            self.total.clear()
            self.zoom.setText("— %")
            return
        view = pane.view
        self.page_validator.setTop(len(view.pages))
        if not self.page.hasFocus() or not self.page.isModified():
            self.page.setText(str(view.current_page() + 1))
        self.total.setText(f"/ {len(view.pages)}")
        from .view import ACTUAL_SIZE
        self.zoom.setText(f"{view.zoom / ACTUAL_SIZE * 100:.0f}%")
