"""LM Studio and compatible chat-completions endpoints."""

from __future__ import annotations

from collections import deque
from itertools import count
import json

from PySide6.QtCore import QByteArray, QObject, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest


DEFAULTS = {
    "endpoint": "http://localhost:1234",
    "model": "plamo-3-translate-0.8b",
    "apiKey": "",
    "langFrom": "en",
    "langTo": "ja",
    "systemPrompt": "[plamo-translate]\ntarget_lang=${langTo}\nform=plain",
    "concurrency": 2,
    "backupCount": 5,
    "autoSave": True,
}


def chat_request(settings, source):
    endpoint = str(settings["endpoint"]).strip().rstrip("/")
    if endpoint.endswith("/v1"):
        endpoint += "/chat/completions"
    elif not endpoint.endswith("/chat/completions"):
        endpoint += "/v1/chat/completions"
    url = QUrl(endpoint)
    if not url.isValid() or url.scheme() not in ("http", "https") or not url.host():
        raise ValueError("翻訳サービスの URL を確認してください。")
    request = QNetworkRequest(url)
    request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
    request.setTransferTimeout(120_000)
    request.setAttribute(
        QNetworkRequest.Attribute.RedirectPolicyAttribute,
        QNetworkRequest.RedirectPolicy.ManualRedirectPolicy,
    )
    api_key = str(settings["apiKey"]).strip()
    if api_key:
        request.setRawHeader(QByteArray(b"Authorization"), QByteArray(f"Bearer {api_key}".encode()))
    system = str(settings["systemPrompt"]).replace("${langFrom}", str(settings["langFrom"]))
    system = system.replace("${langTo}", str(settings["langTo"]))
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": source})
    payload = {
        "model": str(settings["model"]), "messages": messages, "temperature": 0, "stream": True,
    }
    return request, json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Translations(QObject):
    def __init__(self, parent, limit=2):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.limit = limit
        self.waiting = deque()
        self.active = {}
        self.ids = count()

    def submit(self, settings, source, on_progress, on_finished):
        request, payload = chat_request(settings, source)
        job = next(self.ids)
        self.waiting.append((job, request, payload, on_progress, on_finished))
        self._pump()
        return job

    def cancel(self, job):
        self.waiting = deque(item for item in self.waiting if item[0] != job)
        reply = self.active.pop(job, None)
        if reply is not None:
            reply.abort()
        self._pump()

    def _pump(self):
        while self.waiting and len(self.active) < self.limit:
            job, request, payload, progress, finished = self.waiting.popleft()
            self._start(job, request, payload, progress, finished)

    def _start(self, job, request, payload, progress, finished):
        reply = self.network.post(request, QByteArray(payload))
        self.active[job] = reply
        buffer = bytearray()
        chunks = []
        service_error = None
        streamed = False

        def read():
            nonlocal streamed, service_error
            buffer.extend(bytes(reply.readAll()))
            mime = reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader)
            if not mime or "text/event-stream" not in str(mime).lower():
                return
            streamed = True
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer[:] = rest
                if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                    continue
                try:
                    packet = json.loads(line[5:])
                    if error := packet.get("error"):
                        service_error = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    choices = packet.get("choices") or []
                    if choices and (token := choices[0].get("delta", {}).get("content")):
                        chunks.append(token)
                        if job in self.active:
                            progress("".join(chunks))
                except (AttributeError, TypeError, ValueError):
                    service_error = "翻訳サーバーの応答を読み取れませんでした。"

        def done():
            if job not in self.active:
                reply.deleteLater()
                return
            read()
            self.active.pop(job)
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = service_error
            if reply.error() != QNetworkReply.NetworkError.NoError or status != 200:
                error = reply.errorString() if status is None else f"翻訳サーバーが HTTP {status} を返しました。"
            if not streamed and not error:
                try:
                    chunks.append(json.loads(buffer)["choices"][0]["message"]["content"])
                except (KeyError, IndexError, TypeError, ValueError):
                    error = "翻訳サーバーの応答を読み取れませんでした。"
            text = "".join(chunk for chunk in chunks if isinstance(chunk, str)).strip()
            reply.deleteLater()
            finished(text, error or (None if text else "空の訳文が返りました。"))
            self._pump()

        reply.readyRead.connect(read)
        reply.finished.connect(done)
