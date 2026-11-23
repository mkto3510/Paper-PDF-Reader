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
    history_changed = Signal(object)
    note_jobs_changed = Signal(object, bool)
    drafts_changed = Signal()
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
        self.can_undo = self.can_redo = False
        self.mutating = self.saving = 0
        self.annotations = {}
        self.groups = {}
        self.group_history = {}
        self.jobs = {}
        self.job_handlers = {}
        self.translation_sources = {}
        self.deleting_notes = set()
        self.annotation_reads = {}
        self.resizing_notes = set()
        self.source_pending = {}
        self.reference_waiters = {}
        self.reference_scans = {}
        self.references_ready = set()
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
        return bool(self.translation_sources) or any(job.state in ("waiting", "running") for job in self.jobs.values())

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
        self.reference_waiters.clear()
        self.reference_scans.clear()
        self.references_ready.clear()
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
            self.can_undo = self.can_redo = False
            self.mutating = self.saving = 0
            self.auto_save.stop()
            self.annotations.clear()
            self.groups.clear()
            self.group_history.clear()
            self.annotation_reads.clear()
            self.resizing_notes.clear()
            self.source_pending.clear()
            self.translation_sources.clear()
            self.deleting_notes.clear()
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

    @property
    def reference_scan(self):
        return self.reference_scans.get("reference")

    def resolve_citation(self, position, callback, needed, started=None):
        def targets(citation):
            self.request(f"{citation.kind}_targets", (position, citation.keys, True),
                         lambda result, error: callback(citation, result or ()) if needed() else None,
                         priority=2, needed=needed)

        def found(citation, error):
            if citation is None or not needed():
                return
            if started is not None:
                started()
            kind = citation.kind
            if kind in self.references_ready:
                targets(citation)
                return
            waiters = self.reference_waiters.setdefault(kind, [])
            waiters[:] = [waiter for waiter in waiters if waiter[2]()]
            waiters.append((citation, targets, needed, callback))
            if kind not in self.reference_scans:
                self.scan_references(kind)

        self.request("citation_at", position, found, priority=2, needed=needed)

    def scan_references(self, kind):
        scan = self.reference_scans[kind] = object()

        def active():
            return (self.reference_scans.get(kind) is scan and not self.disposed and not self.loading
                    and any(needed() for _, _, needed, _ in self.reference_waiters.get(kind, ())))

        def advance():
            if self.reference_scans.get(kind) is not scan:
                return
            if not active():
                completed(None, None)
                return
            self.request(f"{kind}_step", (), completed, priority=8, needed=active)

        def completed(done, error):
            if self.reference_scans.get(kind) is not scan:
                return
            if done is False:
                QTimer.singleShot(0, self, advance)
                return
            self.reference_scans.pop(kind)
            waiters = self.reference_waiters.pop(kind, ())
            if done is True:
                self.references_ready.add(kind)
            for citation, resolve, needed, callback in waiters:
                if needed():
                    if done is True:
                        resolve(citation)
                    else:
                        callback(citation, ())

        advance()

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
                    self.deleting_notes.difference_update(old.keys() - fresh.keys())
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
            if result is None:
                if error:
                    self.input_error.emit(error)
                if on_error:
                    on_error()
            else:
                keys = set(result["keys"])
                group_keys = keys.union(*(self.targets(key) for key in keys))
                groups_before = {key: self.groups.get(key) for key in group_keys}
                self.update_history(result)
                self.load_notes(indices)
                if on_success:
                    on_success(result["value"])
                changes = {key: (before, self.groups.get(key)) for key, before in groups_before.items()
                           if before != self.groups.get(key)}
                if changes:
                    self.group_history[self.revision] = changes
                for state in result["discarded"]:
                    self.group_history.pop(state, None)
            self.changed.emit()

        self.request("edit", (command, args), done, priority=-4)

    def update_history(self, result):
        self.revision = result["state"]
        self.can_undo, self.can_redo = result["undo"], result["redo"]

    def move_history(self, redo=False):
        if self.busy or not (self.can_redo if redo else self.can_undo):
            return
        self.mutating += 1
        self.changed.emit()

        def moved(result, error):
            self.mutating -= 1
            if error:
                self.input_error.emit(error)
            elif result is not None:
                self.update_history(result)
                keys = set(result["keys"])
                groups = self.group_history.get(result["value"], {})
                affected = keys | groups.keys()
                for targets in self.jobs.keys() | self.translation_sources.keys():
                    if not affected.isdisjoint(targets):
                        self.detach_job(targets, cancel=True)
                for key, choices in groups.items():
                    group = choices[int(redo)]
                    if group is None:
                        self.groups.pop(key, None)
                    else:
                        self.groups[key] = group
                for key in keys:
                    self.source_pending.pop(key, None)
                self.deleting_notes.difference_update(keys)
                self.history_changed.emit(keys)
                self.load_notes({page for page, _ in keys})
                self.schedule_save()
            self.changed.emit()

        self.request("move_history", (redo,), moved, priority=-4)

    def add_highlight(self, quads, color, comment="", on_success=None, on_error=None):
        def added(keys):
            targets = tuple(keys)
            for key in targets:
                self.groups[key] = targets
            if on_success:
                on_success(targets)

        self.mutate("add_highlight", (quads, color, comment), set(quads), added, on_error)

    def modify(self, targets, color=None, comment=None, delete=False, autosave=True, on_error=None):
        targets = tuple(target for target in targets if target not in self.deleting_notes)
        if not targets:
            return
        if delete:
            self.deleting_notes.update(targets)
            for group in {self.targets(target) for target in targets}:
                self.detach_job(group, cancel=True)

        def updated(_):
            if delete:
                removed = set(targets)
                for group in {self.targets(target) for target in targets}:
                    survivors = tuple(key for key in group if key not in removed)
                    for key in survivors:
                        self.groups[key] = survivors
                for target in targets:
                    self.groups.pop(target, None)
            if autosave:
                self.schedule_save()

        def failed():
            if delete:
                self.deleting_notes.difference_update(targets)
            if on_error:
                on_error()

        self.mutate("modify_highlight", (list(targets), color, comment, delete),
                    {page for page, _ in targets}, updated, failed)

    def can_resize(self, key):
        job = self.jobs.get(self.targets(key))
        return (not self.loading and not self.disposed and key not in self.resizing_notes
                and key not in self.deleting_notes
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

    def translate_highlights(self, targets, profile, settings, empty_message):
        active = self.jobs.get(targets)
        if (targets in self.translation_sources or (active and active.state in ("waiting", "running"))
                or any(key in self.deleting_notes or key in self.resizing_notes for key in targets)):
            return
        sources = {}
        self.translation_sources[targets] = sources

        def pending():
            return not self.disposed and self.translation_sources.get(targets) is sources

        def received(key, source, error):
            if not pending():
                return
            if error:
                self.translation_sources.pop(targets)
                self.input_error.emit(error)
                return
            sources[key] = source or ""
            if len(sources) != len(targets):
                return
            self.translation_sources.pop(targets)
            if self.loading:
                return
            source = "\n".join(sources[target].strip() for target in targets if sources[target].strip())
            if not source:
                self.input_error.emit(empty_message)
                return
            manager = self.store.translations
            job = manager.create(profile, source, settings)
            self.attach_job(targets, job)
            manager.enqueue(job)

        for key in targets:
            note = self.get_note(key)
            if note is not None and "source" in note:
                received(key, note["source"], None)
            else:
                self.request("highlight_text", key, lambda source, error, target=key: received(target, source, error),
                             priority=3, needed=pending)

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
        if cancel:
            self.translation_sources.pop(targets, None)
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

    def save(self, destination=None, callback=None, *, allow_signature_loss=False, on_signature_loss=None):
        if self.loading or self.disposed:
            if callback:
                callback(False)
            return
        overwrite = destination is None or Path(destination).resolve() == self.path
        if overwrite and ((not self.dirty and not self.mutating) or self.external):
            if callback:
                callback(not self.dirty)
            return
        self.saving += 1
        settings = self.store.preferences
        self.changed.emit()

        def saved(result, error):
            self.saving -= 1
            confirmation = bool(result and result.get("confirm_signature_loss"))
            okay = bool(result and "stamp" in result)
            if error:
                self.input_error.emit(error)
            elif result and result.get("signature_blocked"):
                self.input_error.emit(settings.tr_text(
                    "電子署名を保護するため、この PDF は上書きできません。別のファイルに保存してください。",
                    "To protect the PDF's digital signature, save the changes to a different file."))
            elif confirmation and not on_signature_loss:
                self.input_error.emit(settings.tr_text(
                    "別のファイルに保存すると、電子署名の検証に影響する可能性があります。",
                    "Saving a copy may affect validation of the digital signature."))
            elif overwrite and okay:
                self.stamp, self.saved_revision = result["stamp"], result["state"]
                self.store.watch(self.path)
            if not self.saving:
                self.store.debounce.start()
            self.changed.emit()
            if confirmation and on_signature_loss:
                on_signature_loss()
            elif callback:
                callback(okay)

        self.request("save", (str(settings.directory / "backups"), settings.data["backupCount"], destination,
                              allow_signature_loss),
                     saved, priority=-4)

    def dispose(self):
        self.reference_waiters.clear()
        self.reference_scans.clear()
        self.disposed = True
        self.auto_save.stop()
        self.translation_sources.clear()
        self.detach_all_jobs(cancel=True)
        self.request("close", (), lambda *_: None, priority=-10)
