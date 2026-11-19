"""Shared open documents, filesystem tracking, and annotation job state."""

from __future__ import annotations

from itertools import count
import os
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from .session import PdfSession
from .translation import Translations
from .view import TileCache


def signature(path):
    try:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ino
    except OSError:
        return None


class Documents(QObject):
    def __init__(self, preferences, has_comment_drafts):
        super().__init__()
        self.preferences = preferences
        self.has_comment_drafts = has_comment_drafts
        self.session = PdfSession(self)
        self.translations = Translations(self, preferences.data["concurrency"])
        self.cache = TileCache()
        self.items = {}
        self.ids = count(1)
        self.watcher = QFileSystemWatcher(self)
        self.debounce = QTimer(self, singleShot=True, interval=250)
        self.watcher.fileChanged.connect(lambda _: self.debounce.start())
        self.watcher.directoryChanged.connect(lambda _: self.debounce.start())
        self.debounce.timeout.connect(self._changed)
        preferences.changed.connect(self._preferences_changed)

    def for_path(self, filename):
        path = Path(filename).resolve()
        key = os.path.normcase(str(path))
        if key not in self.items:
            self.items[key] = Document(self, next(self.ids), path, key)
        return self.items[key]

    def watch(self, path):
        target = str(path)
        if target not in self.watcher.files() and path.is_file():
            self.watcher.addPath(target)
        directory = str(path.parent)
        if directory not in self.watcher.directories():
            self.watcher.addPath(directory)

    def _changed(self):
        for doc in tuple(self.items.values()):
            if not doc.info or doc.loading or doc.saving:
                continue
            self.watch(doc.path)
            stamp = signature(doc.path)
            if stamp == doc.stamp:
                continue
            if (stamp and not doc.busy and not doc.dirty and not doc.has_active_jobs
                    and not self.has_comment_drafts(doc)):
                doc.load(reload=True, automatic=True)
            else:
                doc.external = True
                doc.changed.emit()

    def _preferences_changed(self):
        self.translations.limit = self.preferences.data["concurrency"]
        self.translations.pump()
        for doc in tuple(self.items.values()):
            if doc.info:
                doc.update_rules()

    def remove(self, document):
        if self.items.pop(document.key, None) is None:
            return
        self.cache.discard(document.identifier)
        if str(document.path) in self.watcher.files():
            self.watcher.removePath(str(document.path))
        if all(other.path.parent != document.path.parent for other in self.items.values()):
            if str(document.path.parent) in self.watcher.directories():
                self.watcher.removePath(str(document.path.parent))
        document.dispose()


class Document(QObject):
    loaded = Signal(bool)
    changed = Signal()
    note_changed = Signal(object)
    note_jobs_changed = Signal(object, bool)
    input_error = Signal(str)

    def __init__(self, store, identifier, path, key):
        super().__init__(store)
        self.store, self.identifier, self.path, self.key = store, identifier, path, key
        self.info = None
        self.generation = 0
        self.rules_generation = 0
        self.rules = None
        self.loading = False
        self.disposed = False
        self.external = False
        self.external_message = ""
        self.stamp = None
        self.revision = self.saved_revision = 0
        self.mutating = self.saving = 0
        self.annotations = {}
        self.groups = {}
        self.jobs = {}
        self.job_handlers = {}
        self.annotation_reads = {}
        self.resizing_notes = set()
        self.source_pending = {}
        self.streaming_keys = set()
        self.streaming_timer = QTimer(self, singleShot=True, interval=34)
        self.streaming_timer.timeout.connect(self.flush_job_updates)
        self.auto_save = QTimer(self, singleShot=True, interval=240)
        self.auto_save.timeout.connect(self.save)

    @property
    def dirty(self):
        return self.revision != self.saved_revision

    @property
    def busy(self):
        return bool(self.loading or self.mutating or self.saving)

    @property
    def has_active_jobs(self):
        return any(job.state in ("waiting", "running") for job in self.jobs.values())

    def promote(self, key, priority):
        self.store.session.promote(key, priority)

    def request(self, name, args, callback, *, priority=0, needed=None, key=None):
        generation = self.generation
        life_command = name in ("open", "close", "reload")

        def current():
            return life_command or (not self.disposed and generation == self.generation and not self.loading
                                    and (needed is None or needed()))

        def done(value, error):
            if life_command or (not self.disposed and generation == self.generation):
                callback(value, error)

        self.store.session.request("document", (self.identifier, name, args), done, priority=priority, key=key, needed=current)

    def load(self, callback=None, *, reload=False, password=None, automatic=False):
        if self.loading or self.disposed:
            return
        self.loading = True
        self.changed.emit()
        command = "reload" if reload else "open"
        args = (password,) if reload else (str(self.path), password or "")

        def ready(info, error):
            self.loading = False
            if self.disposed:
                return
            if error or (info and info.get("password")):
                if automatic:
                    self.external = True
                    self.external_message = error or "Password required."
                self.changed.emit()
                if callback:
                    callback(info, error)
                return
            previous = self.info is not None
            self.generation += 1
            self.info = info
            self.stamp = info["signature"]
            self.external = False
            self.external_message = ""
            self.revision = self.saved_revision = 0
            self.annotations.clear()
            self.groups.clear()
            self.annotation_reads.clear()
            self.resizing_notes.clear()
            self.source_pending.clear()
            self.detach_all_jobs(cancel=True)
            self.store.cache.discard(self.identifier)
            self.store.watch(self.path)
            self.update_rules()
            self.loaded.emit(previous)
            self.changed.emit()
            self.load_notes()
            if callback:
                callback(info, None)

        self.request(command, args, ready, priority=-10)

    def update_rules(self):
        latest = self.store.preferences.data["rules"]
        if latest != self.rules:
            self.rules = latest.copy()
            self.rules_generation += 1
            self.request("update_rules", (self.rules,), lambda *_: None, priority=-9)
            self.changed.emit()

    def load_notes(self, indices=None):
        pages = iter(range(len(self.info["pages"])) if indices is None else sorted(indices))
        generation = self.generation

        def advance():
            if self.disposed or self.loading or generation != self.generation:
                return
            page = next(pages, None)
            if page is None:
                return
            revision = self.annotation_reads.get(page, 0) + 1
            self.annotation_reads[page] = revision

            def received(notes, error):
                if notes is not None and revision == self.annotation_reads.get(page, 0):
                    old = self.annotations.get(page, {})
                    fresh = {}
                    for note in notes:
                        key = page, note["xref"]
                        if (key in old and "source" in old[key]
                                and old[key]["quads"] == note["quads"]):
                            note["source"] = old[key]["source"]
                        fresh[key] = note
                    if fresh:
                        self.annotations[page] = fresh
                    else:
                        self.annotations.pop(page, None)
                    changed = {key for key in old.keys() | fresh.keys() if old.get(key) != fresh.get(key)}
                    if changed:
                        self.note_changed.emit(changed)
                QTimer.singleShot(0, advance)

            self.request("annotation_list", (page,), received, priority=10 if indices is None else 2)

        advance()

    def get_note(self, key):
        return self.annotations.get(key[0], {}).get(key)

    def targets(self, key):
        return self.groups.get(key, (key,))

    def load_note_source(self, key):
        note = self.get_note(key)
        if note is None or "source" in note:
            return
        geometry = note["quads"]
        if self.source_pending.get(key) == geometry:
            return
        self.source_pending[key] = geometry

        def loaded(source, error):
            if self.source_pending.get(key) is not geometry:
                return
            self.source_pending.pop(key, None)
            current = self.get_note(key)
            if current and current["quads"] == geometry and not error:
                current["source"] = source or ""
                self.note_changed.emit({key})

        self.request("highlight_text", key, loaded, priority=9)

    def mutate(self, command, args, indices, on_success=None, on_error=None):
        self.mutating += 1
        self.changed.emit()

        def done(result, error):
            self.mutating -= 1
            if error:
                self.input_error.emit(error)
                if on_error:
                    on_error()
            else:
                self.revision += 1
                self.load_notes(indices)
                if on_success:
                    on_success(result)
            self.changed.emit()

        self.request(command, args, done, priority=-4)

    def add_highlight(self, quads, color, comment="", on_success=None, on_error=None):
        def added(keys):
            targets = tuple(keys)
            for key in targets:
                self.groups[key] = targets
            if on_success:
                on_success(targets)

        self.mutate("add_highlight", (quads, color, comment), set(quads), added, on_error)

    def modify(self, targets, color=None, comment=None, delete=False, autosave=True, on_error=None):
        if delete:
            self.detach_job(tuple(targets), cancel=True)
            for target in targets:
                self.groups.pop(target, None)
        self.mutate("modify_highlight", (list(targets), color, comment, delete),
                    {page for page, _ in targets}, lambda _: self.schedule_save() if autosave else None,
                    on_error)

    def can_resize(self, key):
        job = self.jobs.get(self.targets(key))
        return (not self.loading and not self.disposed and key not in self.resizing_notes
                and self.get_note(key) is not None and (job is None or job.state not in ("waiting", "running")))

    def resized(self, key, first, last, selected, on_error):
        if not self.can_resize(key):
            return
        self.resizing_notes.add(key)
        targets = self.targets(key)
        self.detach_job(targets, cancel=True)
        page, xref = key

        def moved(new_xref):
            self.resizing_notes.discard(key)
            new = page, new_xref
            if new != key:
                self.groups.pop(key, None)
                targets_new = tuple(new if target == key else target for target in targets)
                for target in targets_new:
                    self.groups[target] = targets_new
            selected(new)
            self.schedule_save()

        def failed():
            self.resizing_notes.discard(key)
            on_error()

        self.mutate("resize_highlight", (page, xref, first, last), {page}, moved, failed)

    def attach_job(self, targets, job):
        self.detach_job(targets, cancel=True)
        self.jobs[targets] = job
        previous = None

        def changed():
            nonlocal previous
            if self.disposed or self.jobs.get(targets) is not job:
                return
            if job.state == "finished":
                self.detach_job(targets)
                self.modify(targets, comment=job.output)
            elif job.state == "canceled":
                self.detach_job(targets)
            elif job.state == previous == "running":
                self.streaming_keys.update(targets)
                if not self.streaming_timer.isActive():
                    self.streaming_timer.start()
            else:
                self.streaming_keys.difference_update(targets)
                self.note_jobs_changed.emit(set(targets), True)
            previous = job.state

        self.job_handlers[targets] = changed
        job.changed.connect(changed)
        changed()

    def flush_job_updates(self):
        keys = self.streaming_keys
        self.streaming_keys = set()
        if keys:
            self.note_jobs_changed.emit(keys, False)

    def detach_job(self, targets, cancel=False):
        job = self.jobs.pop(targets, None)
        handler = self.job_handlers.pop(targets, None)
        if job:
            if handler:
                job.changed.disconnect(handler)
            if cancel and job.state in ("waiting", "running"):
                job.cancel()
            self.streaming_keys.difference_update(targets)
            self.note_jobs_changed.emit(set(targets), True)

    def detach_all_jobs(self, cancel):
        for targets in tuple(self.jobs):
            self.detach_job(targets, cancel)

    def schedule_save(self):
        if self.store.preferences.data["autoSave"] and not self.external:
            self.auto_save.start()

    def save(self, destination=None, callback=None):
        if self.loading or self.disposed:
            return
        overwrite = destination is None or Path(destination).resolve() == self.path
        if overwrite and (not self.dirty or self.external):
            if callback:
                callback(not self.dirty)
            return
        self.saving += 1
        revision = self.revision
        settings = self.store.preferences
        self.changed.emit()

        def saved(stamp, error):
            self.saving -= 1
            if error:
                self.input_error.emit(error)
            elif overwrite:
                self.stamp, self.saved_revision = stamp, revision
                self.store.watch(self.path)
            if not self.saving:
                self.store.debounce.start()
            self.changed.emit()
            if callback:
                callback(not error)

        self.request("save", (str(settings.directory / "backups"), settings.data["backupCount"], destination),
                     saved, priority=-4)

    def dispose(self):
        self.disposed = True
        self.auto_save.stop()
        self.detach_all_jobs(cancel=True)
        self.request("close", (), lambda *_: None, priority=-10)
