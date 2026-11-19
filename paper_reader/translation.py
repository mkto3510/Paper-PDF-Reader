"""Saved OpenAI-compatible services and transferable translation jobs."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

DEFAULT_PROFILE = {
    "name": "LM Studio",
    "endpoint": "http://localhost:1234",
    "model": "plamo-3-translate-0.8b",
    "apiKey": "",
    "systemPrompt": "[plamo-translate]\ntarget_lang=${langTo}\nform=plain",
    "prompt": "${sourceText}",
    "temperature": 0.0,
    "stream": True,
    "extra": {},
}


def request_url(endpoint, models=False):
    url = QUrl(endpoint.strip())
    if not url.isValid() or url.scheme() not in ("http", "https") or not url.host():
        raise ValueError("Invalid translation service URL.")
    path = url.path().rstrip("/")
    if path.endswith("/chat/completions"):
        base = path.removesuffix("/chat/completions")
    else:
        base = path if path.endswith("/v1") else path + "/v1"
    url.setPath(base + ("/models" if models else "/chat/completions"))
    return url


def api_request(profile, models=False):
    request = QNetworkRequest(request_url(profile["endpoint"], models))
    request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
    request.setTransferTimeout(15_000 if models else 120_000)
    request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                         QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
    if key := profile["apiKey"].strip():
        request.setRawHeader(QByteArray(b"Authorization"), QByteArray(f"Bearer {key}".encode()))
    return request


def chat_request(profile, source, lang_from="en", lang_to="ja"):
    variables = {"${sourceText}": source, "${langFrom}": lang_from, "${langTo}": lang_to}

    def expand(template):
        # A variable-like string in the source itself must not be expanded.
        import re
        return re.sub(r"\$\{(?:sourceText|langFrom|langTo)\}", lambda m: variables[m[0]], template)

    system = expand(profile["systemPrompt"])
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": expand(profile["prompt"])})
    payload = {"model": profile["model"], "messages": messages,
               "temperature": profile["temperature"], "stream": profile["stream"]} | profile["extra"]
    return api_request(profile), json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


@dataclass(frozen=True)
class Request:
    profile: dict
    source: str
    lang_from: str
    lang_to: str


class Job(QObject):
    changed = Signal()

    def __init__(self, manager, request):
        super().__init__()
        self.manager = manager
        self.request = request
        self.state = "waiting"
        self.output = ""
        self.error = ""

    def set_state(self, state, output="", error=""):
        self.state, self.output, self.error = state, output, error
        self.changed.emit()

    def cancel(self):
        self.manager.cancel(self)

    def retry(self):
        self.manager.enqueue(self)


class Translations(QObject):
    def __init__(self, parent, limit=2):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.limit = limit
        self.waiting = deque()
        self.active = {}

    def create(self, profile, source, settings):
        from copy import deepcopy
        source_request = Request(deepcopy(profile), source, settings["langFrom"], settings["langTo"])
        return Job(self, source_request)

    def enqueue(self, job):
        if job in self.active or job in self.waiting:
            return
        job.set_state("waiting")
        self.waiting.append(job)
        self.pump()

    def cancel(self, job):
        if job in self.waiting:
            self.waiting.remove(job)
        reply = self.active.pop(job, None)
        if reply is not None:
            reply.abort()
        job.set_state("canceled")
        self.pump()

    def pump(self):
        while self.waiting and len(self.active) < self.limit:
            job = self.waiting.popleft()
            try:
                request, payload = chat_request(job.request.profile, job.request.source,
                                                job.request.lang_from, job.request.lang_to)
            except (TypeError, ValueError) as error:
                job.set_state("failed", error=str(error))
                continue
            self._start(job, request, payload)

    def models(self, profile, callback):
        try:
            reply = self.network.get(api_request(profile, models=True))
        except ValueError as error:
            callback(None, str(error))
            return

        def done():
            try:
                if reply.error() != QNetworkReply.NetworkError.NoError:
                    raise ValueError(reply.errorString())
                if reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) != 200:
                    raise ValueError("The service could not list models.")
                items = json.loads(bytes(reply.readAll()))["data"]
                names = list(dict.fromkeys(item["id"] for item in items if isinstance(item.get("id"), str)))
                callback(names, None)
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                callback(None, str(error))
            finally:
                reply.deleteLater()

        reply.finished.connect(done)

    def _start(self, job, request, payload):
        reply = self.network.post(request, QByteArray(payload))
        self.active[job] = reply
        buffer = bytearray()
        chunks = []
        service_error = ""
        streamed = False
        job.set_state("running")

        def parse_data(line):
            nonlocal service_error
            if not line.startswith(b"data:") or line[5:].strip() in (b"", b"[DONE]"):
                return
            try:
                packet = json.loads(line[5:])
                if error := packet.get("error"):
                    service_error = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                choices = packet.get("choices") or []
                if choices:
                    item = choices[0]
                    token = item.get("delta", {}).get("content") or item.get("message", {}).get("content") or item.get("text")
                    if isinstance(token, str):
                        chunks.append(token)
            except (AttributeError, IndexError, TypeError, ValueError) as error:
                service_error = str(error)

        def read():
            nonlocal streamed
            if self.active.get(job) is not reply:
                return
            buffer.extend(bytes(reply.readAll()))
            mime = reply.header(QNetworkRequest.KnownHeaders.ContentTypeHeader)
            if "text/event-stream" not in str(mime).lower() and not buffer.lstrip().startswith(b"data:"):
                return
            streamed = True
            count = len(chunks)
            lines = buffer.split(b"\n")
            buffer[:] = lines.pop()
            for line in lines:
                parse_data(line.strip())
            if len(chunks) != count:
                job.set_state("running", "".join(chunks))

        def done():
            if self.active.get(job) is not reply:
                reply.deleteLater()
                return
            read()
            if self.active.get(job) is not reply:
                reply.deleteLater()
                return
            self.active.pop(job)
            if streamed and buffer:
                parse_data(buffer.strip())
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = service_error
            if reply.error() != QNetworkReply.NetworkError.NoError or status != 200:
                error = error or (reply.errorString() if status is None else f"HTTP {status}")
            if not streamed and not error:
                try:
                    item = json.loads(buffer)["choices"][0]
                    chunks.append(item.get("message", {}).get("content") or item.get("text") or "")
                except (AttributeError, KeyError, IndexError, TypeError, ValueError) as failure:
                    error = str(failure)
            text = "".join(chunk for chunk in chunks if isinstance(chunk, str)).strip()
            reply.deleteLater()
            if not text:
                error = error or "Empty translation."
            job.set_state("failed" if error else "finished", text, error)
            self.pump()

        reply.readyRead.connect(read)
        reply.finished.connect(done)
