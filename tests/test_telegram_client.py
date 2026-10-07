import json

import httpx
import pytest

from app.channels.telegram.client import (
    FileTooLargeError,
    TelegramAPIError,
    TelegramClient,
    TelegramNetworkError,
)
from tests.telegram_fakes import MIB, PNG_BYTES, TOKEN, FakeTelegram, api_error, api_ok

pytestmark = pytest.mark.anyio


@pytest.fixture
def tg():
    return FakeTelegram()


# --- request shapes -----------------------------------------------------------------


async def test_send_message_payload(tg):
    await tg.client().send_message(5, "hello", reply_to_message_id=9)
    [payload] = tg.sent_messages
    assert payload["chat_id"] == 5
    assert payload["text"] == "hello"
    assert payload["reply_parameters"] == {"message_id": 9, "allow_sending_without_reply": True}
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert "parse_mode" not in payload  # plain text: nothing to escape


async def test_send_message_without_reply(tg):
    await tg.client().send_message(5, "hello")
    assert "reply_parameters" not in tg.sent_messages[0]


async def test_send_chat_action(tg):
    await tg.client().send_chat_action(5)
    assert tg.method_calls("sendChatAction") == [{"chat_id": 5, "action": "typing"}]


async def test_get_file(tg):
    tg.add_file("f1", b"abc", file_path="photos/a.jpg")
    info = await tg.client().get_file("f1")
    assert (info.file_id, info.file_path, info.file_size) == ("f1", "photos/a.jpg", 3)


async def test_get_updates_payload_and_http_timeout(tg):
    tg.updates_batches.append([{"update_id": 1}])
    result = await tg.client().get_updates(offset=5, timeout=30)
    assert result == [{"update_id": 1}]
    assert tg.method_calls("getUpdates") == [{"timeout": 30, "allowed_updates": ["message"], "offset": 5}]
    # The HTTP read timeout must outlast the long poll.
    assert tg.requests[0].extensions["timeout"]["read"] == 40.0


async def test_set_and_delete_webhook(tg):
    client = tg.client()
    await client.set_webhook("https://x.example/webhook/telegram", "sekret", drop_pending_updates=True)
    assert tg.method_calls("setWebhook") == [
        {
            "url": "https://x.example/webhook/telegram",
            "secret_token": "sekret",
            "allowed_updates": ["message"],
            "drop_pending_updates": True,
        }
    ]
    await client.delete_webhook()
    assert tg.method_calls("deleteWebhook") == [{"drop_pending_updates": False}]


async def test_base_url_is_configurable(tg):
    seen = []

    def handler(request):
        seen.append(str(request.url.host))
        return api_ok(True)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await TelegramClient(TOKEN, http, base_url="https://tg.test/").send_chat_action(1)
    assert seen == ["tg.test"]


# --- errors ----------------------------------------------------------------------------


async def test_api_error_is_reported_without_url(tg):
    tg.queue("sendMessage", api_error(403, "Forbidden: bot was blocked by the user"))
    with pytest.raises(TelegramAPIError) as excinfo:
        await tg.client().send_message(1, "x")
    error = excinfo.value
    assert error.error_code == 403
    assert error.method == "sendMessage"
    assert TOKEN not in str(error)
    assert "api.telegram.org" not in str(error)


async def test_non_json_response_is_an_api_error(tg):
    tg.queue("getMe", httpx.Response(502, text="<html>bad gateway</html>"))
    with pytest.raises(TelegramAPIError) as excinfo:
        await tg.client().get_me()
    assert excinfo.value.status == 502


async def test_network_error_is_sanitized(tg):
    def boom(request):
        raise httpx.ConnectError("connection failed", request=request)

    tg.queue("getMe", boom)
    with pytest.raises(TelegramNetworkError) as excinfo:
        await tg.client().get_me()
    error = excinfo.value
    assert TOKEN not in str(error)
    assert "ConnectError" in str(error)
    assert error.__cause__ is None and error.__suppress_context__  # `from None`


# --- send_message retry --------------------------------------------------------------------


async def test_send_message_retries_once_on_rate_limit(tg):
    tg.queue("sendMessage", api_error(429, "Too Many Requests", retry_after=3))
    await tg.client().send_message(1, "x")
    assert len(tg.sent_messages) == 2
    assert tg.sleeps == [3]


async def test_rate_limit_sleep_is_capped(tg):
    tg.queue("sendMessage", api_error(429, "Too Many Requests", retry_after=500))
    await tg.client().send_message(1, "x")
    assert tg.sleeps == [10.0]


async def test_send_message_gives_up_after_one_retry(tg):
    tg.queue("sendMessage", api_error(429, "x", retry_after=1), api_error(429, "x", retry_after=1))
    with pytest.raises(TelegramAPIError):
        await tg.client().send_message(1, "x")
    assert len(tg.sent_messages) == 2


async def test_send_message_retries_once_on_network_error(tg):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    tg.queue("sendMessage", boom)
    await tg.client().send_message(1, "x")
    assert len(tg.sent_messages) == 2


async def test_other_api_errors_are_not_retried(tg):
    tg.queue("sendMessage", api_error(400, "Bad Request: chat not found"))
    with pytest.raises(TelegramAPIError):
        await tg.client().send_message(1, "x")
    assert len(tg.sent_messages) == 1


# --- download ----------------------------------------------------------------------------------


async def test_download_returns_bytes(tg):
    tg.add_file("f1", PNG_BYTES, file_path="photos/a.png")
    assert await tg.client().download_file("photos/a.png", 5 * MIB) == PNG_BYTES


async def test_download_exactly_at_limit_is_allowed(tg):
    tg.files["photos/limit.png"] = b"x" * 1000
    assert len(await tg.client().download_file("photos/limit.png", 1000)) == 1000


async def test_download_one_byte_over_limit_is_rejected(tg):
    tg.files["photos/big.png"] = b"x" * 1001
    with pytest.raises(FileTooLargeError):
        await tg.client().download_file("photos/big.png", 1000)


async def test_download_rejects_on_content_length_without_reading_body(tg):
    class ExplodingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("body must not be read when Content-Length is over the limit")
            yield b""

    tg.files["photos/huge.png"] = httpx.Response(200, headers={"content-length": str(6 * MIB)}, stream=ExplodingStream())
    with pytest.raises(FileTooLargeError):
        await tg.client().download_file("photos/huge.png", 5 * MIB)


async def test_download_aborts_mid_stream_when_declared_size_lies(tg):
    delivered = []

    class LyingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(200):  # 200 x 64 KiB = 12.5 MiB, no Content-Length sent
                delivered.append(1)
                yield b"x" * 65536

    tg.files["photos/lie.png"] = httpx.Response(200, stream=LyingStream())
    with pytest.raises(FileTooLargeError):
        await tg.client().download_file("photos/lie.png", 5 * MIB)
    assert len(delivered) < 100  # stopped long before reading all 12.5 MiB


async def test_download_http_error(tg):
    with pytest.raises(TelegramAPIError) as excinfo:
        await tg.client().download_file("photos/missing.png", MIB)
    assert excinfo.value.status == 404


async def test_download_timeout_is_a_network_error(tg):
    import asyncio

    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(5)
            yield b"x"

    tg.files["photos/slow.png"] = httpx.Response(200, stream=SlowStream())
    with pytest.raises(TelegramNetworkError):
        await tg.client(download_timeout=0.2).download_file("photos/slow.png", MIB)


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "../secret", "a/../b", "a?x=1", "a#frag", "a\\b"])
async def test_download_rejects_suspicious_paths(tg, bad_path):
    with pytest.raises(TelegramAPIError):
        await tg.client().download_file(bad_path, MIB)
    assert tg.downloads == []


# --- token hygiene -------------------------------------------------------------------------------


def test_repr_hides_the_token(tg):
    client = tg.client()
    assert TOKEN not in repr(client)
    assert "redacted" in repr(client)


async def test_token_is_only_in_the_request_path(tg):
    await tg.client().send_message(1, "x")
    [request] = tg.requests
    assert TOKEN in request.url.path
    assert TOKEN not in request.content.decode()
    assert TOKEN not in json.dumps(dict(request.headers))
