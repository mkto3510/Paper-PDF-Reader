"""Startup, same-user instance reuse, and the packaged smoke test."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME, EXECUTABLE_NAME, VERSION
from .documents import Documents
from .icons import ICON_DIR
from .paths import data_dir
from .preferences import Preferences
from .session import PdfSession
from .view import tile_image
from .window import Reader


class Application:
    def __init__(self, app, directory):
        self.app = app
        self.preferences = Preferences(directory)
        self.windows = []
        self.documents = Documents(self.preferences, self.has_comment_drafts)
        self.server = None
        self.lock = None
        self.connections = {}
        app.aboutToQuit.connect(self.shutdown)

    def new_window(self):
        window = Reader(self)
        self.windows.append(window)
        window.show()
        return window

    def panes(self):
        return [window.tabs.widget(i) for window in self.windows for i in range(window.tabs.count())]

    def has_comment_drafts(self, document):
        return any(pane.document is document and pane.notes.has_drafts() for pane in self.panes())

    def has_unsaved_changes(self, document):
        return bool(document.dirty or document.mutating or self.has_comment_drafts(document))

    def receive_paths(self, paths):
        active = self.app.activeWindow()
        window = active if isinstance(active, Reader) else (self.windows[-1] if self.windows else self.new_window())
        window.open_paths(paths)
        window.raise_()
        window.activateWindow()

    def listen(self, name, lock):
        self.lock = lock
        self.server = QLocalServer(self.app)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(name):
            raise OSError(self.server.errorString())
        self.server.newConnection.connect(self.accept_connection)

    def accept_connection(self):
        while self.server.hasPendingConnections():
            peer = self.server.nextPendingConnection()
            self.connections[peer] = bytearray()
            peer.readyRead.connect(lambda socket=peer: self.receive_connection(socket))
            peer.disconnected.connect(lambda socket=peer: (self.connections.pop(socket, None), socket.deleteLater()))
            self.receive_connection(peer)

    def receive_connection(self, socket):
        buffer = self.connections.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 1024 * 1024:
            socket.disconnectFromServer()
            return
        if b"\n" not in buffer:
            return
        try:
            paths = json.loads(bytes(buffer).split(b"\n", 1)[0])
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise ValueError("Invalid file list.")
        except (UnicodeError, ValueError):
            socket.disconnectFromServer()
            return
        buffer.clear()
        socket.write(b"ok\n")
        socket.flush()
        socket.disconnectFromServer()
        self.receive_paths(paths)

    def shutdown(self):
        if self.server:
            self.server.close()
        for doc in tuple(self.documents.items.values()):
            doc.detach_all_jobs(cancel=True)
        self.documents.session.shutdown()
        if self.lock:
            self.lock.unlock()


def forward_to_other(name, paths):
    socket = QLocalSocket()
    socket.connectToServer(name)
    if not socket.waitForConnected(200):
        return False
    socket.write(json.dumps(paths).encode() + b"\n")
    socket.flush()
    if not socket.bytesAvailable() and not socket.waitForReadyRead(1000):
        return False
    return bytes(socket.readAll()).startswith(b"ok")


def smoke_test(app, report):
    with TemporaryDirectory(prefix="paper-reader-smoke-") as temporary:
        controller = Application(app, Path(temporary))
        window = Reader(controller)
        try:
            def finish(result, error):
                try:
                    if error:
                        raise RuntimeError(error)
                    _, _, image = tile_image(result)
                    if image.isNull() or image.width() != 500:
                        raise ValueError("Qt could not read the MuPDF raster.")
                except (RuntimeError, TypeError, ValueError) as failure:
                    report.write_text(str(failure), encoding="utf-8")
                    app.exit(1)
                else:
                    report.write_text(f"PASS {APP_NAME} {VERSION}\n", encoding="utf-8")
                    app.exit(0)

            QTimer.singleShot(0, lambda: controller.documents.session.request("smoke", (temporary,), finish))
            QTimer.singleShot(30_000, lambda: finish(None, "PDF process timed out."))
            return app.exec()
        finally:
            controller.shutdown()


def main():
    parser = argparse.ArgumentParser(prog=EXECUTABLE_NAME)
    parser.add_argument("pdf", nargs="*")
    parser.add_argument("--smoke-test", type=Path)
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(QIcon(str(ICON_DIR / "app.ico")))
    if args.smoke_test is not None:
        return smoke_test(app, args.smoke_test)
    folder = data_dir()
    filenames = [str(Path(path).resolve()) for path in args.pdf]
    instance = "PaperPdfReader-" + sha256(os.path.normcase(str(folder.resolve())).encode()).hexdigest()[:32]
    lock = QLockFile(str(folder / "instance.lock"))
    if not lock.tryLock(0):
        for _ in range(8):
            if forward_to_other(instance, filenames):
                return 0
        QMessageBox.warning(None, APP_NAME, "Could not contact the running instance.")
        return 1
    controller = Application(app, folder)
    try:
        controller.listen(instance, lock)
    except OSError as error:
        controller.shutdown()
        QMessageBox.warning(None, APP_NAME, str(error))
        return 1
    window = controller.new_window()
    if filenames:
        QTimer.singleShot(0, lambda: window.open_paths(filenames))
    return app.exec()
