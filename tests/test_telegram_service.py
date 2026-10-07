import asyncio

import httpx
import pytest

from app.analyzer import llm as analyzer_llm
from app.channels.telegram.client import TelegramClient
from app.channels.telegram.service import MAX_IMAGE_BYTES, TelegramService
from app.replies import templates as T
from app.replies.formatter import (
    error_message,
    format_analysis,
    help_message,
    image_too_large_message,
    image_unreadable_message,
    start_message,
    unsupported_input_message,
)
from tests.telegram_fakes import (
    JPEG_BYTES,
    MIB,
    PNG_BYTES,
    FakeAnalyzer,
    FakeTelegram,
    api_error,
    document_update,
    make_result,
    other_update,
    parse,
    photo_update,
    text_update,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def analyzer():
    return FakeAnalyzer()


def make_service(tg: FakeTelegram, analyzer: FakeAnalyzer, **kwargs) -> TelegramService:
    return TelegramService(tg.client(), analyze=analyzer, **kwargs)


async def handle(service: TelegramService, payload: dict) -> None:
    await service.handle_update(parse(payload))


# --- text ----------------------------------------------------------------------------------------


async def test_text_message_is_analyzed_and_answered(tg, analyzer):
    service = make_service(tg, analyzer)
    await handle(service, text_update("Please share the OTP", chat_id=555, message_id=77))

    assert analyzer.calls == [{"text": "Please share the OTP", "image_bytes": None, "language": "en"}]
    [message] = tg.sent_messages
    assert message["chat_id"] == 555
    assert message["reply_parameters"]["message_id"] == 77
    assert message["text"] == format_analysis(analyzer.result, "en")
    assert "parse_mode" not in message


async def test_forwarded_message_is_treated_like_any_other(tg, analyzer):
    payload = text_update("Forwarded scam text")
    payload["message"]["forward_origin"] = {"type": "user", "sender_user": {"id": 1, "is_bot": False}}
    await handle(make_service(tg, analyzer), payload)
    assert analyzer.calls[0]["text"] == "Forwarded scam text"


async def test_devanagari_text_gets_hindi_reply_and_hindi_analysis(tg, analyzer):
    await handle(make_service(tg, analyzer), text_update("आपका ओटीपी बताइए", language_code="en"))
    assert analyzer.calls[0]["language"] == "hi"
    assert tg.sent_texts == [format_analysis(analyzer.result, "hi")]


async def test_hindi_language_code_gets_hindi_reply_for_english_text(tg, analyzer):
    await handle(make_service(tg, analyzer), text_update("Share the OTP", language_code="hi"))
    assert analyzer.calls[0]["language"] == "hi"
    assert tg.sent_texts == [format_analysis(analyzer.result, "hi")]


async def test_hidden_link_urls_reach_the_analyzer(tg, analyzer):
    entities = [
        {"type": "text_link", "offset": 0, "length": 5, "url": "https://bit.ly/claim"},
        {"type": "bold", "offset": 0, "length": 5},
        {"type": "text_link", "offset": 6, "length": 4, "url": "https://bit.ly/claim"},  # duplicate
    ]
    await handle(make_service(tg, analyzer), text_update("Claim now", entities=entities))
    assert analyzer.calls[0]["text"] == "Claim now\n[link: https://bit.ly/claim]"


async def test_hidden_link_is_caught_by_the_real_rules(tg, monkeypatch):
    """End to end through the real analyzer path, with the LLM unavailable."""

    async def no_llm(**kwargs):
        return None

    monkeypatch.setattr(analyzer_llm, "analyze", no_llm)
    entities = [{"type": "text_link", "offset": 0, "length": 5, "url": "https://bit.ly/claim"}]
    service = TelegramService(tg.client())  # default analyzer = the real analyze_message
    await handle(service, text_update("Claim your gift", entities=entities))
    assert T.RULE_REASONS["shortened_link"]["en"] in tg.sent_texts[0]


async def test_typing_indicator_while_analyzing_and_stops_after(tg):
    class SlowAnalyzer(FakeAnalyzer):
        async def __call__(self, **kwargs):
            await asyncio.sleep(0.35)
            return await super().__call__(**kwargs)

    service = make_service(tg, SlowAnalyzer(), typing_interval=0.1)
    await handle(service, text_update("hello"))
    actions = tg.method_calls("sendChatAction")
    assert len(actions) >= 3 and all(a["action"] == "typing" for a in actions)

    settled = len(actions)
    await asyncio.sleep(0.3)
    assert len(tg.method_calls("sendChatAction")) == settled  # the heartbeat was cancelled


async def test_typing_failure_does_not_break_the_reply(tg):
    class SlowAnalyzer(FakeAnalyzer):
        async def __call__(self, **kwargs):
            await asyncio.sleep(0.05)
            return await super().__call__(**kwargs)

    tg.queue("sendChatAction", api_error(400, "nope"), api_error(400, "nope"))
    await handle(make_service(tg, SlowAnalyzer(), typing_interval=0.02), text_update("hello"))
    assert len(tg.sent_messages) == 1


# --- commands ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "language_code", "expected"),
    [
        ("/start", "en", start_message("en")),
        ("/start", "hi", start_message("hi")),
        ("/start@ruko_bot", "en", start_message("en")),
        ("/start some-payload", "en", start_message("en")),
        ("/START", "en", start_message("en")),
        ("/help", "en", help_message("en")),
        ("/help", "hi-IN", help_message("hi")),
        ("/help@ruko_bot", "en", help_message("en")),
        ("/unknowncommand", "en", help_message("en")),
    ],
)
async def test_commands(tg, analyzer, text, language_code, expected):
    await handle(make_service(tg, analyzer), text_update(text, language_code=language_code))
    assert tg.sent_texts == [expected]
    assert analyzer.calls == []
    assert tg.method_calls("sendChatAction") == []


async def test_start_and_help_carry_the_1930_footer(tg, analyzer):
    service = make_service(tg, analyzer)
    await handle(service, text_update("/start"))
    await handle(service, text_update("/help"))
    assert all(text.endswith(T.FOOTER["en"]) for text in tg.sent_texts)


async def test_slash_inside_text_is_not_a_command(tg, analyzer):
    await handle(make_service(tg, analyzer), text_update("Pay now /start immediately"))
    assert analyzer.calls[0]["text"] == "Pay now /start immediately"


# --- who we answer --------------------------------------------------------------------------------------


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_groups_and_channels_are_ignored(tg, analyzer, chat_type):
    await handle(make_service(tg, analyzer), text_update("Share the OTP", chat_type=chat_type))
    await handle(make_service(tg, analyzer), text_update("/start", chat_type=chat_type))
    assert tg.calls == [] and analyzer.calls == []


async def test_bots_are_ignored(tg, analyzer):
    await handle(make_service(tg, analyzer), text_update("hello", is_bot=True))
    assert tg.calls == [] and analyzer.calls == []


async def test_message_without_sender_is_ignored(tg, analyzer):
    payload = text_update("hello")
    del payload["message"]["from"]
    await handle(make_service(tg, analyzer), payload)
    assert tg.calls == [] and analyzer.calls == []


async def test_non_message_updates_are_ignored(tg, analyzer):
    update = parse({"update_id": 3, "edited_message": {"message_id": 1, "text": "x"}})
    await make_service(tg, analyzer).handle_update(update)
    assert tg.calls == [] and analyzer.calls == []


async def test_service_messages_are_ignored(tg, analyzer):
    await handle(make_service(tg, analyzer), other_update(new_chat_title="x"))
    assert tg.calls == [] and analyzer.calls == []


# --- unsupported input -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        {"voice": {"file_id": "v"}},
        {"audio": {"file_id": "a"}},
        {"video": {"file_id": "v"}},
        {"video_note": {"file_id": "v"}},
        {"animation": {"file_id": "g"}},
        {"sticker": {"file_id": "s"}},
        {"location": {"latitude": 1, "longitude": 2}},
        {"contact": {"phone_number": "1"}},
        {"poll": {"id": "p"}},
        {"text": "   "},
    ],
    ids=lambda c: next(iter(c)) + ("-blank" if "text" in c else ""),
)
async def test_unsupported_input_gets_a_polite_reply(tg, analyzer, content):
    await handle(make_service(tg, analyzer), other_update(**content))
    assert tg.sent_texts == [unsupported_input_message("en")]
    assert analyzer.calls == []
    assert tg.method_calls("getFile") == [] and tg.downloads == []


@pytest.mark.parametrize("mime_type", ["application/pdf", "text/plain", "image/svg+xml", "image/heic", None])
async def test_non_image_documents_are_unsupported(tg, analyzer, mime_type):
    await handle(make_service(tg, analyzer), document_update(mime_type=mime_type))
    assert tg.sent_texts == [unsupported_input_message("en")]
    assert analyzer.calls == [] and tg.method_calls("getFile") == []


async def test_unsupported_reply_is_localised(tg, analyzer):
    await handle(make_service(tg, analyzer), other_update(voice={"file_id": "v"}, language_code="hi"))
    assert tg.sent_texts == [unsupported_input_message("hi")]


# --- photos and image documents ---------------------------------------------------------------------------------


async def test_photo_uses_largest_variant_and_caption(tg, analyzer):
    tg.add_file("big", JPEG_BYTES, file_path="photos/big.jpg")
    payload = photo_update("big", caption="Is this real?", sizes=[(90, 90, 900), (320, 320, 5000), (1280, 960, len(JPEG_BYTES))])
    await handle(make_service(tg, analyzer), payload)

    assert tg.method_calls("getFile") == [{"file_id": "big"}]
    assert tg.downloads == ["photos/big.jpg"]
    assert analyzer.calls == [{"text": "Is this real?", "image_bytes": JPEG_BYTES, "language": "en"}]
    assert len(tg.sent_messages) == 1


async def test_photo_without_caption_has_no_text(tg, analyzer):
    tg.add_file("p", PNG_BYTES)
    await handle(make_service(tg, analyzer), photo_update("p", sizes=[(100, 100, len(PNG_BYTES))]))
    assert analyzer.calls[0]["text"] is None
    assert analyzer.calls[0]["image_bytes"] == PNG_BYTES


async def test_photo_hidden_caption_links_reach_the_analyzer(tg, analyzer):
    tg.add_file("p", PNG_BYTES)
    entities = [{"type": "text_link", "offset": 0, "length": 4, "url": "https://tinyurl.com/x"}]
    payload = photo_update("p", sizes=[(100, 100, len(PNG_BYTES))], caption="Look", caption_entities=entities)
    await handle(make_service(tg, analyzer), payload)
    assert analyzer.calls[0]["text"] == "Look\n[link: https://tinyurl.com/x]"


async def test_photo_language_comes_from_caption_script(tg, analyzer):
    tg.add_file("p", PNG_BYTES)
    payload = photo_update("p", sizes=[(100, 100, len(PNG_BYTES))], caption="यह असली है क्या?", language_code="en")
    await handle(make_service(tg, analyzer), payload)
    assert analyzer.calls[0]["language"] == "hi"


async def test_image_only_photo_uses_language_code(tg, analyzer):
    tg.add_file("p", PNG_BYTES)
    await handle(make_service(tg, analyzer), photo_update("p", sizes=[(100, 100, len(PNG_BYTES))], language_code="hi"))
    assert analyzer.calls[0]["language"] == "hi"


@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg", "image/webp", "image/gif", "IMAGE/PNG"])
async def test_image_documents_are_analyzed(tg, analyzer, mime_type):
    tg.add_file("doc-1", PNG_BYTES, file_path="documents/file.png")
    await handle(make_service(tg, analyzer), document_update("doc-1", mime_type=mime_type, file_size=len(PNG_BYTES), caption="hi"))
    assert analyzer.calls == [{"text": "hi", "image_bytes": PNG_BYTES, "language": "en"}]


# --- the 5 MiB limit ------------------------------------------------------------------------------------------------


def test_limit_is_five_mebibytes():
    assert MAX_IMAGE_BYTES == 5 * 1024 * 1024 == 5_242_880


async def assert_rejected_as_too_large(tg, analyzer, language="en"):
    assert tg.sent_texts == [image_too_large_message(language)]
    assert analyzer.calls == []  # never analyzed
    assert tg.downloads == []  # never downloaded


async def test_declared_oversize_is_rejected_before_getfile(tg, analyzer):
    payload = photo_update("big", sizes=[(1280, 1280, MAX_IMAGE_BYTES + 1)])
    await handle(make_service(tg, analyzer), payload)
    assert tg.method_calls("getFile") == []  # not even asked Telegram for the file
    await assert_rejected_as_too_large(tg, analyzer)


async def test_largest_variant_over_limit_is_rejected_not_swapped_for_a_smaller_one(tg, analyzer):
    tg.add_file("small-0", PNG_BYTES)  # a perfectly good smaller variant exists
    payload = photo_update("huge", sizes=[(90, 90, 1_000), (800, 800, 40_000), (4000, 4000, 6 * MIB)])
    await handle(make_service(tg, analyzer), payload)
    assert tg.method_calls("getFile") == []
    await assert_rejected_as_too_large(tg, analyzer)


async def test_oversize_image_document_is_rejected(tg, analyzer):
    await handle(make_service(tg, analyzer), document_update("d", mime_type="image/png", file_size=10 * MIB))
    assert tg.method_calls("getFile") == []
    await assert_rejected_as_too_large(tg, analyzer)


async def test_oversize_reported_only_by_getfile_is_rejected_without_download(tg, analyzer):
    tg.file_infos["p"] = {"file_id": "p", "file_path": "photos/p.jpg", "file_size": MAX_IMAGE_BYTES + 1}
    payload = photo_update("p", sizes=[(100, 100, None)])  # update did not declare a size
    await handle(make_service(tg, analyzer), payload)
    assert len(tg.method_calls("getFile")) == 1
    await assert_rejected_as_too_large(tg, analyzer)


async def test_undeclared_oversize_is_caught_during_download(tg, analyzer):
    tg.add_file("p", PNG_BYTES + b"x" * (MAX_IMAGE_BYTES), declare_size=False)
    await handle(make_service(tg, analyzer), photo_update("p", sizes=[(100, 100, None)]))
    assert tg.sent_texts == [image_too_large_message("en")]
    assert analyzer.calls == []


async def test_image_of_exactly_the_limit_is_accepted(tg, analyzer):
    content = PNG_BYTES + b"x" * (MAX_IMAGE_BYTES - len(PNG_BYTES))
    assert len(content) == MAX_IMAGE_BYTES
    tg.add_file("p", content)
    await handle(make_service(tg, analyzer), photo_update("p", sizes=[(100, 100, MAX_IMAGE_BYTES)]))
    assert len(analyzer.calls) == 1
    assert len(analyzer.calls[0]["image_bytes"]) == MAX_IMAGE_BYTES


async def test_too_large_reply_is_localised(tg, analyzer):
    payload = photo_update("big", sizes=[(1280, 1280, 6 * MIB)], language_code="hi")
    await handle(make_service(tg, analyzer), payload)
    await assert_rejected_as_too_large(tg, analyzer, "hi")


async def test_too_large_reply_has_the_footer(tg, analyzer):
    await handle(make_service(tg, analyzer), photo_update("big", sizes=[(1280, 1280, 6 * MIB)]))
    assert tg.sent_texts[0].endswith(T.FOOTER["en"])


# --- unreadable images -------------------------------------------------------------------------------------------------


async def test_non_image_bytes_are_rejected_after_download(tg, analyzer):
    tg.add_file("p", b"%PDF-1.4 definitely not an image")
    await handle(make_service(tg, analyzer), photo_update("p", sizes=[(100, 100, 30)]))
    assert tg.sent_texts == [image_unreadable_message("en")]
    assert analyzer.calls == []


async def test_getfile_without_a_path_is_unreadable(tg, analyzer):
    tg.file_infos["p"] = {"file_id": "p", "file_size": 100}
    await handle(make_service(tg, analyzer), photo_update("p", sizes=[(100, 100, 100)]))
    assert tg.sent_texts == [image_unreadable_message("en")]
    assert analyzer.calls == []


# --- failures never go silent, never raise -------------------------------------------------------------------------------


async def test_analyzer_exception_gets_a_fallback_reply(tg):
    analyzer = FakeAnalyzer(error=RuntimeError("boom with secret text"))
    await handle(make_service(tg, analyzer), text_update("hello", language_code="hi"))
    assert tg.sent_texts == [error_message("hi")]


async def test_download_failure_gets_a_fallback_reply(tg, analyzer):
    tg.file_infos["p"] = {"file_id": "p", "file_path": "photos/missing.jpg", "file_size": 10}  # download 404s
    await handle(make_service(tg, analyzer), photo_update("p", sizes=[(100, 100, 10)]))
    assert tg.sent_texts == [error_message("en")]
    assert analyzer.calls == []


async def test_getfile_api_error_gets_a_fallback_reply(tg, analyzer):
    await handle(make_service(tg, analyzer), photo_update("unknown-id", sizes=[(100, 100, 10)]))
    assert tg.sent_texts == [error_message("en")]


async def test_failure_to_send_the_reply_does_not_raise(tg, analyzer):
    tg.queue("sendMessage", *[api_error(403, "Forbidden: bot was blocked by the user")] * 3)
    await handle(make_service(tg, analyzer), text_update("hello"))  # must not raise


async def test_cancellation_is_not_swallowed(tg):
    class HangingAnalyzer(FakeAnalyzer):
        async def __call__(self, **kwargs):
            await asyncio.sleep(30)

    service = make_service(tg, HangingAnalyzer())
    task = asyncio.create_task(service.handle_update(parse(text_update("hello"))))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- dedup & concurrency -----------------------------------------------------------------------------------------------------


def test_accept_deduplicates_by_update_id(tg, analyzer):
    service = make_service(tg, analyzer)
    assert service.accept(parse(text_update("a", update_id=1))) is True
    assert service.accept(parse(text_update("a", update_id=1))) is False
    assert service.accept(parse(text_update("a", update_id=2))) is True


async def test_concurrency_is_capped(tg):
    in_flight = 0
    peak = 0

    class CountingAnalyzer(FakeAnalyzer):
        async def __call__(self, **kwargs):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return await super().__call__(**kwargs)

    service = make_service(tg, CountingAnalyzer(), max_concurrent=2)
    await asyncio.gather(*(handle(service, text_update(f"msg {i}", update_id=i)) for i in range(8)))
    assert peak == 2
    assert len(tg.sent_messages) == 8


def test_service_uses_the_given_client(tg, analyzer):
    assert isinstance(tg.client(), TelegramClient)
    assert isinstance(make_service(tg, analyzer), TelegramService)
    assert httpx  # the transport is httpx.MockTransport; no network involved
