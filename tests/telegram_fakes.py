"""Test doubles for the Telegram channel. No real network is ever used."""

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.analyzer.combine import AnalysisResult, combine
from app.analyzer.llm import LLMAnalysis
from app.analyzer.rules import check_rules
from app.channels.telegram.client import TelegramClient
from app.channels.telegram.models import Update

TOKEN = "123456789:AAEfakeTokenForTestsOnly_0123456789abc"
WEBHOOK_SECRET = "test-webhook-secret_123"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"0" * 200
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"0" * 200
MIB = 1024 * 1024


def api_ok(result: Any = True) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def api_error(code: int, description: str, **parameters: Any) -> httpx.Response:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        body["parameters"] = parameters
    return httpx.Response(code, json=body)


class FakeTelegram:
    """An in-memory Telegram Bot API behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (method, JSON payload)
        self.files: dict[str, bytes | httpx.Response] = {}  # file_path -> content
        self.file_infos: dict[str, dict[str, Any]] = {}  # file_id -> getFile result
        self.queued: dict[str, list[httpx.Response | Callable[[httpx.Request], httpx.Response]]] = {}
        self.updates_batches: list[list[dict[str, Any]]] = []
        self.webhook_url = ""
        self.downloads: list[str] = []
        self.sleeps: list[float] = []

    # -- configuration helpers ---------------------------------------------------

    def add_file(self, file_id: str, content: bytes, *, file_path: str | None = None, declare_size: bool = True) -> None:
        file_path = file_path or f"photos/{file_id}.jpg"
        self.files[file_path] = content
        info: dict[str, Any] = {"file_id": file_id, "file_path": file_path}
        if declare_size:
            info["file_size"] = len(content)
        self.file_infos[file_id] = info

    def queue(self, method: str, *responses: Any) -> None:
        self.queued.setdefault(method, []).extend(responses)

    # -- the transport -------------------------------------------------------------

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        file_prefix = f"/file/bot{TOKEN}/"
        if path.startswith(file_prefix):
            file_path = path[len(file_prefix):]
            self.downloads.append(file_path)
            content = self.files.get(file_path)
            if content is None:
                return httpx.Response(404)
            return content if isinstance(content, httpx.Response) else httpx.Response(200, content=content)

        match = re.fullmatch(rf"/bot{re.escape(TOKEN)}/(\w+)", path)
        if not match:
            return api_error(404, "Not Found")
        method = match.group(1)
        payload = json.loads(request.content) if request.content else {}
        self.calls.append((method, payload))

        queue = self.queued.get(method)
        if queue:
            item = queue.pop(0)
            return item(request) if callable(item) else item

        if method == "getFile":
            info = self.file_infos.get(payload.get("file_id"))
            return api_ok(info) if info else api_error(400, "Bad Request: invalid file_id")
        if method == "getUpdates":
            return api_ok(self.updates_batches.pop(0) if self.updates_batches else [])
        if method == "getMe":
            return api_ok({"id": 1, "is_bot": True, "username": "ruko_dev_bot"})
        if method == "getWebhookInfo":
            return api_ok({"url": self.webhook_url, "pending_update_count": 0})
        if method == "setWebhook":
            self.webhook_url = payload["url"]
        if method == "deleteWebhook":
            self.webhook_url = ""
        if method == "sendMessage":
            return api_ok({"message_id": 999})
        return api_ok(True)

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def client(self, **kwargs: Any) -> TelegramClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self._handler))
        kwargs.setdefault("sleep", self._sleep)
        return TelegramClient(TOKEN, http, **kwargs)

    # -- assertions helpers ----------------------------------------------------------

    def method_calls(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    @property
    def sent_messages(self) -> list[dict[str, Any]]:
        return self.method_calls("sendMessage")

    @property
    def sent_texts(self) -> list[str]:
        return [payload["text"] for payload in self.sent_messages]


# -- update builders ------------------------------------------------------------------


def _message(
    *,
    update_id: int,
    chat_id: int,
    chat_type: str,
    language_code: str | None,
    is_bot: bool,
    message_id: int,
    **content: Any,
) -> dict[str, Any]:
    user: dict[str, Any] = {"id": chat_id, "is_bot": is_bot, "first_name": "Test"}
    if language_code is not None:
        user["language_code"] = language_code
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": user,
            "chat": {"id": chat_id, "type": chat_type},
            "date": 1_700_000_000,
            **content,
        },
    }


def text_update(
    text: str,
    *,
    update_id: int = 1,
    chat_id: int = 100,
    chat_type: str = "private",
    language_code: str | None = "en",
    is_bot: bool = False,
    message_id: int = 10,
    entities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {"text": text}
    if entities:
        content["entities"] = entities
    return _message(
        update_id=update_id, chat_id=chat_id, chat_type=chat_type, language_code=language_code,
        is_bot=is_bot, message_id=message_id, **content,
    )  # fmt: skip


def photo_update(
    file_id: str = "photo-large",
    *,
    sizes: list[tuple[int, int, int | None]] | None = None,
    caption: str | None = None,
    caption_entities: list[dict[str, Any]] | None = None,
    update_id: int = 1,
    chat_id: int = 100,
    language_code: str | None = "en",
    message_id: int = 10,
) -> dict[str, Any]:
    """`sizes` is a list of (width, height, file_size); the LAST is given `file_id`."""
    sizes = sizes or [(90, 90, 1_000), (1280, 1280, 50_000)]
    photo = []
    for index, (width, height, size) in enumerate(sizes):
        variant: dict[str, Any] = {
            "file_id": file_id if index == len(sizes) - 1 else f"{file_id}-small-{index}",
            "file_unique_id": f"u{index}",
            "width": width,
            "height": height,
        }
        if size is not None:
            variant["file_size"] = size
        photo.append(variant)
    content: dict[str, Any] = {"photo": photo}
    if caption is not None:
        content["caption"] = caption
    if caption_entities:
        content["caption_entities"] = caption_entities
    return _message(
        update_id=update_id, chat_id=chat_id, chat_type="private", language_code=language_code,
        is_bot=False, message_id=message_id, **content,
    )  # fmt: skip


def document_update(
    file_id: str = "doc-1",
    *,
    mime_type: str | None = "image/png",
    file_size: int | None = 1_000,
    caption: str | None = None,
    update_id: int = 1,
    chat_id: int = 100,
    language_code: str | None = "en",
) -> dict[str, Any]:
    document: dict[str, Any] = {"file_id": file_id, "file_unique_id": "ud", "file_name": "file"}
    if mime_type is not None:
        document["mime_type"] = mime_type
    if file_size is not None:
        document["file_size"] = file_size
    content: dict[str, Any] = {"document": document}
    if caption is not None:
        content["caption"] = caption
    return _message(
        update_id=update_id, chat_id=chat_id, chat_type="private", language_code=language_code,
        is_bot=False, message_id=10, **content,
    )  # fmt: skip


def other_update(update_id: int = 1, *, chat_id: int = 100, language_code: str | None = "en", **content: Any) -> dict[str, Any]:
    return _message(
        update_id=update_id, chat_id=chat_id, chat_type="private", language_code=language_code,
        is_bot=False, message_id=10, **content,
    )  # fmt: skip


def parse(payload: dict[str, Any]) -> Update:
    return Update.model_validate(payload)


# -- analyzer double ----------------------------------------------------------------------


def make_result(text: str = "Please share the OTP", llm_verdict: str | None = "likely_scam") -> AnalysisResult:
    llm = None
    if llm_verdict is not None:
        llm = LLMAnalysis(
            verdict=llm_verdict,
            scam_type="other",
            red_flags=["flag"],
            explanation="This asks for your OTP.",
            what_to_do=["Do not share the OTP."],
        )
    return combine(check_rules(text), llm)


class FakeAnalyzer:
    """Stands in for analyze_message; records calls instead of calling an LLM."""

    def __init__(self, result: AnalysisResult | None = None, error: Exception | None = None) -> None:
        self.result = result if result is not None else make_result()
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> AnalysisResult:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result
