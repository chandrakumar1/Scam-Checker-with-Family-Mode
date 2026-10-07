"""Minimal Telegram Bot API client on httpx (no bot library).

Privacy rules this module enforces:
- The bot token is part of every Bot API and file-download URL. The URL is
  built at call time and never stored, logged or put in an exception.
- httpx errors (whose text can include the URL) are converted into
  TelegramError subclasses carrying only the method name / status code, and
  re-raised `from None` so no chained traceback leaks the URL either.
- Status codes are checked by hand; `raise_for_status()` is never used because
  its message contains the URL.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.telegram.org"
DOWNLOAD_CHUNK_BYTES = 64 * 1024
MAX_RETRY_AFTER_SECONDS = 10.0


class TelegramError(Exception):
    """Base class. Messages never contain URLs, tokens or message content."""


class TelegramNetworkError(TelegramError):
    def __init__(self, what: str, cause: str) -> None:
        super().__init__(f"{what}: {cause}")
        self.what = what
        self.cause = cause


class TelegramAPIError(TelegramError):
    def __init__(
        self,
        method: str,
        status: int,
        error_code: int | None = None,
        description: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Telegram {method} failed: HTTP {status}, error_code={error_code}")
        self.method = method
        self.status = status
        self.error_code = error_code if error_code is not None else status
        self.description = description
        self.retry_after = retry_after


class FileTooLargeError(TelegramError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"file exceeds the {limit}-byte limit")
        self.limit = limit


class TelegramFile(BaseModel):
    file_id: str
    file_size: int | None = None
    file_path: str | None = None


def make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))


DEFAULT_ALLOWED_UPDATES = ("message",)  # what a bare TelegramClient asks for (unchanged from Phase 2)
# The Emergency button is always on, so button presses are always needed.
EMERGENCY_ALLOWED_UPDATES = ("message", "callback_query")
# Family Mode also needs "the user blocked the bot" events.
FAMILY_ALLOWED_UPDATES = ("message", "callback_query", "my_chat_member")


def telegram_allowed_updates(family_mode: bool) -> list[str]:
    """The update types the bot asks Telegram for. Button presses are always
    included (Emergency); block events only with Family Mode.
    Changing this requires re-registering the webhook (scripts.set_webhook)."""
    return list(FAMILY_ALLOWED_UPDATES if family_mode else EMERGENCY_ALLOWED_UPDATES)


class TelegramClient:
    def __init__(
        self,
        token: SecretStr | str,
        http: httpx.AsyncClient,
        *,
        base_url: str = DEFAULT_BASE_URL,
        download_timeout: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        allowed_updates: Sequence[str] | None = None,
    ) -> None:
        self._token = token.get_secret_value() if isinstance(token, SecretStr) else token
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._download_timeout = download_timeout
        self._sleep = sleep
        self._allowed_updates = list(allowed_updates) if allowed_updates else list(DEFAULT_ALLOWED_UPDATES)

    def __repr__(self) -> str:
        return f"TelegramClient(base_url={self._base_url!r}, token=<redacted>)"

    # -- plumbing ---------------------------------------------------------------

    def _api_url(self, method: str) -> str:
        return f"{self._base_url}/bot{self._token}/{method}"

    def _file_url(self, file_path: str) -> str:
        return f"{self._base_url}/file/bot{self._token}/{file_path}"

    async def _call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        extra: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        try:
            response = await self._http.post(self._api_url(method), json=payload or {}, **extra)
        except httpx.HTTPError as exc:
            raise TelegramNetworkError(method, type(exc).__name__) from None

        try:
            body = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise TelegramAPIError(method, response.status_code)

        if response.status_code != 200 or not body.get("ok"):
            params = body.get("parameters") or {}
            raise TelegramAPIError(
                method,
                response.status_code,
                error_code=body.get("error_code"),
                description=body.get("description"),
                retry_after=params.get("retry_after"),
            )
        return body.get("result")

    # -- Bot API methods ----------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {
                "message_id": reply_to_message_id,
                "allow_sending_without_reply": True,
            }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        # One retry only: after a network blip, or after Telegram's flood limit.
        try:
            await self._call("sendMessage", payload)
        except TelegramNetworkError:
            await self._sleep(0.5)
            await self._call("sendMessage", payload)
        except TelegramAPIError as exc:
            if exc.error_code != 429:
                raise
            await self._sleep(min(exc.retry_after or 1.0, MAX_RETRY_AFTER_SECONDS))
            await self._call("sendMessage", payload)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action})

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        """Stop the button's loading spinner, optionally with a short popup."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = show_alert
        await self._call("answerCallbackQuery", payload)

    async def remove_buttons(self, chat_id: int, message_id: int) -> None:
        """Take the inline buttons off a message (so a finished or stale choice cannot be pressed again)."""
        await self._call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
        )

    async def get_file(self, file_id: str) -> TelegramFile:
        result = await self._call("getFile", {"file_id": file_id})
        return TelegramFile.model_validate(result)

    async def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": allowed_updates or self._allowed_updates}
        if offset is not None:
            payload["offset"] = offset
        # The HTTP read must outlast the long poll.
        return await self._call("getUpdates", payload, timeout=timeout + 10.0)

    async def set_webhook(
        self,
        url: str,
        secret_token: str,
        *,
        allowed_updates: list[str] | None = None,
        drop_pending_updates: bool = False,
    ) -> None:
        await self._call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": allowed_updates or self._allowed_updates,
                "drop_pending_updates": drop_pending_updates,
            },
        )

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> None:
        await self._call("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    async def get_webhook_info(self) -> dict[str, Any]:
        return await self._call("getWebhookInfo")

    # -- file download --------------------------------------------------------------

    async def download_file(self, file_path: str, max_bytes: int) -> bytes:
        """Download a file, never holding more than `max_bytes` (+ one chunk).

        Raises FileTooLargeError as soon as the limit is exceeded, judged by the
        Content-Length header first and then by bytes actually received, since a
        declared size cannot be trusted.
        """
        if (
            file_path.startswith("/")
            or ".." in file_path
            or any(ch in file_path for ch in "?#\\")
        ):
            raise TelegramAPIError("download", 400, description="unexpected file path")

        try:
            async with asyncio.timeout(self._download_timeout):
                async with self._http.stream("GET", self._file_url(file_path)) as response:
                    if response.status_code != 200:
                        raise TelegramAPIError("download", response.status_code)

                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FileTooLargeError(max_bytes)

                    data = bytearray()
                    async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise FileTooLargeError(max_bytes)
                    return bytes(data)
        except TimeoutError:
            raise TelegramNetworkError("download", "timed out") from None
        except httpx.HTTPError as exc:
            raise TelegramNetworkError("download", type(exc).__name__) from None
