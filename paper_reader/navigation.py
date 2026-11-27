"""Per-tab navigation history and asynchronous destination lookup."""

from PySide6.QtCore import QUrl


class NavigationHistory:
    def __init__(self):
        self.clear()

    def clear(self):
        self.entries = []
        self.index = -1
        self.target = None

    def record(self, before, after):
        if before == after:
            return False
        del self.entries[self.index + 1:]
        if not self.entries or self.entries[-1] != before:
            self.entries.append(before)
        self.entries.append(after)
        self.index = len(self.entries) - 1
        return True

    def can_move(self, delta):
        return 0 <= self.index + delta < len(self.entries)

    def move(self, delta, current_location):
        if delta < 0 and self.index == len(self.entries) - 1:
            self.entries[self.index] = current_location(), self.target
        self.index += delta
        position, self.target = self.entries[self.index]
        return position


def check_hit(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


class PageLinks:
    def __init__(self, document):
        self.document = document
        self.cache = {}
        self.pending = {}

    def clear(self):
        self.cache.clear()
        self.pending.clear()

    def at(self, position, navigable):
        page, x, y = position
        return next((link for link in self.cache.get(page, ()) if check_hit(link["box"], x, y)
                     and (not navigable or "page" in link or QUrl(link["uri"]).scheme() in ("http", "https"))),
                    None)

    def read_at(self, position, callback, *, needed=None, failed=None, cancelled=None,
                navigable=False, priority=-1):
        page = position[0]

        def deliver(error=None):
            if needed is not None and not needed():
                if cancelled is not None:
                    cancelled()
                return
            if error and failed is not None:
                failed(error)
                return
            callback(self.at(position, navigable))

        if page in self.cache:
            deliver()
            return
        request_key = self.document.identifier, "page_links", self, page
        if page in self.pending:
            self.pending[page].append((needed, deliver))
            self.document.promote(request_key, priority)
            return
        subscribers = self.pending[page] = [(needed, deliver)]

        def loaded(value, error):
            self.pending.pop(page, None)
            if value is not None:
                self.cache[page] = value
            for _, ready in subscribers:
                ready(error)

        self.document.request("page_links", (page,), loaded, priority=priority, key=request_key,
                              needed=lambda: any(test is None or test() for test, _ in subscribers))

    def resolve_destination(self, position, *, needed, failed, citation, location, external):
        def link_ready(link):
            if not needed():
                return
            if link is None:
                self.document.resolve_citation(position, citation, needed)
            elif "page" not in link:
                external(link["uri"])
            else:
                page, point = link["page"], link["point"]
                if link.get("page_only"):
                    self.document.request("named_figure_target", (position, page),
                                          lambda value, error: location(value, page, point),
                                          priority=2, needed=needed)
                else:
                    location(None, page, point)

        self.read_at(position, link_ready, needed=needed, failed=failed, navigable=True)
