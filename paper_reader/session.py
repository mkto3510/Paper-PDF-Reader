"""Schedule PDF operations without waiting for MuPDF on the Qt event loop."""

from __future__ import annotations

from heapq import heapify, heappop, heappush
from itertools import count
from multiprocessing import get_context
from tempfile import TemporaryDirectory
from threading import Thread

from PySide6.QtCore import QObject, QTimer, Signal


def _run_worker(peer, working_directory):
    from .pdf import worker

    worker(peer, working_directory)


class PdfSession(QObject):
    replied = Signal(object, object)
    stopped = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        context = get_context("spawn")
        self.working_directory = TemporaryDirectory(prefix="paper-reader-documents-")
        self.pipe, peer = context.Pipe()
        self.process = context.Process(target=_run_worker, args=(peer, self.working_directory.name), daemon=True)
        self.process.start()
        peer.close()
        self.queue = []
        self.sequence = count()
        self.current = None
        self.closed = False
        self.replied.connect(self._received)
        self.stopped.connect(self._stopped)
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def request(self, name, args, callback, *, priority=0, key=None, needed=None):
        if self.closed:
            callback(None, "PDF 処理を終了しました。")
            return
        heappush(self.queue, (priority, next(self.sequence), key, name, args, callback, needed))
        self._pump()

    def _pump(self):
        while self.queue and self.current is None and not self.closed:
            _, _, key, name, args, callback, needed = heappop(self.queue)
            if needed is not None and not needed():
                callback(None, None)
                continue
            self.current = callback, key
            try:
                self.pipe.send((name, args))
            except (OSError, EOFError):
                self._stopped()

    def _read(self):
        try:
            while True:
                result, error = self.pipe.recv()
                self.replied.emit(result, error)
        except (OSError, EOFError):
            self.stopped.emit()

    def _received(self, result, error):
        if self.current is None:
            return
        callback, _ = self.current
        self.current = None
        callback(result, error)
        QTimer.singleShot(0, self._pump)

    def promote(self, key, priority):
        for index, request in enumerate(self.queue):
            if request[2] == key and request[0] > priority:
                self.queue[index] = (priority, *request[1:])
                heapify(self.queue)
                break

    def discard(self, key):
        keep = []
        while self.queue:
            request = heappop(self.queue)
            if request[2] != key:
                heappush(keep, request)
        self.queue = keep

    def _stopped(self):
        if self.closed:
            return
        message = "PDF 処理が途中で終了しました。アプリを開き直してください。"
        self.closed = True
        if self.current is not None:
            callback, _ = self.current
            self.current = None
            callback(None, message)
        while self.queue:
            heappop(self.queue)[5](None, message)

    def shutdown(self):
        if self.pipe.closed:
            return
        self.closed = True
        self.queue.clear()
        self.current = None
        try:
            self.pipe.send(None)
        except (OSError, EOFError):
            pass
        self.process.join(timeout=0.7)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join()
        self.reader.join(timeout=0.7)
        self.pipe.close()
        self.working_directory.cleanup()
