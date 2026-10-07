"""Nothing sensitive may reach the logs: no token, no webhook secret, no message
text, no image bytes, no chat id, and no URL that contains the bot token."""

import logging
import re
from pathlib import Path

import httpx
import pytest

from app.channels.telegram.client import TelegramClient
from app.channels.telegram.service import TelegramService
from app.config import Settings
from app.logging_config import NOISY_LOGGERS, RedactSecretsFilter, configure_logging, redact
from tests.telegram_fakes import (
    PNG_BYTES,
    TOKEN,
    WEBHOOK_SECRET,
    FakeAnalyzer,
    FakeTelegram,
    api_error,
    document_update,
    parse,
    photo_update,
    text_update,
)

pytestmark = pytest.mark.anyio

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRET_TEXT = "ZEBRA-MESSAGE-BODY-5551234 please share the OTP"
SECRET_CAPTION = "QUOKKA-CAPTION-9876543 look at this"
CHAT_ID = 918273645


def settings() -> Settings:
    return Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_webhook_secret=WEBHOOK_SECRET)


@pytest.fixture
def restore_logging():
    """configure_logging changes global logger state; put it back afterwards."""
    names = ["app", *NOISY_LOGGERS]
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    for handler in logging.getLogger().handlers:
        for existing in [f for f in handler.filters if isinstance(f, RedactSecretsFilter)]:
            handler.removeFilter(existing)


@pytest.fixture
def tg():
    return FakeTelegram()


def assert_clean(log_text: str, *extra_forbidden: str) -> None:
    forbidden = [TOKEN, TOKEN.split(":")[1], WEBHOOK_SECRET, SECRET_TEXT, "ZEBRA-MESSAGE", "QUOKKA-CAPTION",
                 str(CHAT_ID), "PNG", *extra_forbidden]  # fmt: skip
    for item in forbidden:
        assert item not in log_text, f"{item!r} leaked into the logs"
    assert not re.search(r"bot\d{5,}:", log_text)
    assert "api.telegram.org" not in log_text


# --- redaction helpers ---------------------------------------------------------------------------------


def test_redact_removes_tokens_in_urls():
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert TOKEN not in redact(f"HTTP Request: POST {url} 200 OK")
    assert "bot<redacted>" in redact(url)
    assert redact(f"file: https://api.telegram.org/file/bot{TOKEN}/photos/a.jpg") .count(TOKEN) == 0


def test_redact_removes_configured_secrets_but_not_short_ones():
    assert redact(f"secret={WEBHOOK_SECRET}!", [WEBHOOK_SECRET]) == "secret=<redacted>!"
    assert redact("a short value: abc", ["abc"]) == "a short value: abc"  # too short to be a real secret


def test_filter_rewrites_log_records(caplog):
    configure = RedactSecretsFilter([WEBHOOK_SECRET])
    for handler in logging.getLogger().handlers:
        handler.addFilter(configure)
    try:
        with caplog.at_level(logging.INFO):
            logging.getLogger("x").info("calling bot%s/getMe with %s", TOKEN, WEBHOOK_SECRET)
    finally:
        for handler in logging.getLogger().handlers:
            handler.removeFilter(configure)
    assert TOKEN not in caplog.text and WEBHOOK_SECRET not in caplog.text
    assert "<redacted>" in caplog.text


# --- the httpx INFO-log problem and its fix ---------------------------------------------------------------------


async def test_httpx_logs_the_token_by_default_and_configure_logging_stops_it(caplog, restore_logging):
    async def one_request():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": True}))) as http:
            await TelegramClient(TOKEN, http).get_me()

    # Without our configuration httpx records the full URL, token included.
    logging.getLogger("httpx").setLevel(logging.INFO)
    with caplog.at_level(logging.INFO):
        await one_request()
    assert TOKEN in caplog.text, "premise of this test: httpx leaks the token at INFO"

    # After configure_logging, the same request leaves no trace.
    caplog.clear()
    configure_logging(settings())
    with caplog.at_level(logging.INFO):
        await one_request()
    assert TOKEN not in caplog.text
    assert not re.search(r"bot\d{5,}:", caplog.text)


def test_configure_logging_raises_http_library_loggers_to_warning(restore_logging):
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    configure_logging(settings())
    for name in NOISY_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


# --- end to end: nothing sensitive in any log line -----------------------------------------------------------------


def capture_everything(caplog):
    """Capture every record our own code can emit. The httpx loggers are left
    as configure_logging set them (WARNING), as in a real deployment."""
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="app")


async def test_redaction_filter_is_a_second_line_of_defence(tg, caplog, restore_logging):
    """Even if someone turns httpx INFO logging back on, the token never shows."""
    configure_logging(settings())
    logging.getLogger("httpx").setLevel(logging.INFO)  # undo the first line of defence
    capture_everything(caplog)
    caplog.set_level(logging.INFO, logger="httpx")

    service = TelegramService(tg.client(), analyze=FakeAnalyzer())
    await service.handle_update(parse(text_update(SECRET_TEXT, chat_id=CHAT_ID)))

    assert "HTTP Request" in caplog.text  # httpx really did log the request...
    assert "bot<redacted>/sendMessage" in caplog.text  # ...with the token rewritten
    assert TOKEN not in caplog.text and TOKEN.split(":")[1] not in caplog.text
    assert not re.search(r"bot\d{5,}:", caplog.text)


async def test_text_flow_logs_nothing_sensitive(tg, caplog, restore_logging):
    configure_logging(settings())
    capture_everything(caplog)
    service = TelegramService(tg.client(), analyze=FakeAnalyzer())
    await service.handle_update(parse(text_update(SECRET_TEXT, chat_id=CHAT_ID, update_id=77)))
    assert len(tg.sent_messages) == 1
    assert "update 77" in caplog.text  # we do log the update id and outcome...
    assert_clean(caplog.text)  # ...and nothing else of interest


async def test_photo_flow_logs_nothing_sensitive(tg, caplog, restore_logging):
    configure_logging(settings())
    capture_everything(caplog)
    tg.add_file("p", PNG_BYTES, file_path="photos/secret-path-name.png")
    service = TelegramService(tg.client(), analyze=FakeAnalyzer())
    await service.handle_update(parse(photo_update("p", caption=SECRET_CAPTION, chat_id=CHAT_ID, sizes=[(100, 100, len(PNG_BYTES))])))
    await service.handle_update(parse(document_update("p", mime_type="image/png", file_size=len(PNG_BYTES), chat_id=CHAT_ID, update_id=2)))
    assert len(tg.sent_messages) == 2
    assert_clean(caplog.text, "secret-path-name")


async def test_error_paths_log_only_exception_types(tg, caplog, restore_logging):
    configure_logging(settings())
    capture_everything(caplog)

    # Analyzer blows up with an error message that contains the user's text.
    boom = FakeAnalyzer(error=RuntimeError(f"failed on: {SECRET_TEXT} via bot{TOKEN}"))
    await TelegramService(tg.client(), analyze=boom).handle_update(parse(text_update(SECRET_TEXT, chat_id=CHAT_ID)))

    # Telegram API errors, rate limits and a failed download.
    tg.queue("sendMessage", api_error(403, f"Forbidden: {SECRET_TEXT}"), api_error(403, "Forbidden"))
    await TelegramService(tg.client(), analyze=FakeAnalyzer()).handle_update(parse(text_update(SECRET_TEXT, chat_id=CHAT_ID, update_id=2)))
    tg.file_infos["gone"] = {"file_id": "gone", "file_path": "photos/gone.jpg", "file_size": 5}
    await TelegramService(tg.client(), analyze=FakeAnalyzer()).handle_update(
        parse(photo_update("gone", caption=SECRET_CAPTION, chat_id=CHAT_ID, sizes=[(10, 10, 5)], update_id=3))
    )

    assert "RuntimeError" in caplog.text  # the type is logged
    assert "failed on" not in caplog.text  # the message is not
    assert_clean(caplog.text)


async def test_rejected_inputs_log_nothing_sensitive(tg, caplog, restore_logging):
    configure_logging(settings())
    capture_everything(caplog)
    service = TelegramService(tg.client(), analyze=FakeAnalyzer())
    await service.handle_update(parse(photo_update("big", caption=SECRET_CAPTION, chat_id=CHAT_ID, sizes=[(4000, 4000, 9_000_000)])))
    await service.handle_update(parse(text_update("/start", chat_id=CHAT_ID, update_id=2)))
    assert_clean(caplog.text)


# --- static guard against logging habits that would leak -------------------------------------------------------------


def source_files():
    return [*(PROJECT_ROOT / "app" / "channels").rglob("*.py"), *(PROJECT_ROOT / "scripts").rglob("*.py")]


@pytest.mark.parametrize("path", source_files(), ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_no_exception_logging_that_would_print_tracebacks_or_messages(path):
    # Tracebacks chain exception messages, which can contain URLs (and the token).
    source = path.read_text(encoding="utf-8")
    assert "logger.exception" not in source
    assert "exc_info" not in source
    assert ".raise_for_status(" not in source  # its message contains the request URL


def test_no_log_call_formats_message_content():
    pattern = re.compile(r"logger\.\w+\(.*\b(?:\.text|\.caption|image_bytes|\.data)\b")
    for path in source_files():
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            assert not pattern.search(line), f"{path.name}:{line_number} logs message content"
