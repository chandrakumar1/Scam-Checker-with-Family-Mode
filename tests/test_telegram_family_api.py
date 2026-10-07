"""Telegram models and client additions needed by Family Mode (buttons, block events)."""

import pytest

from app.channels.telegram.client import (
    DEFAULT_ALLOWED_UPDATES,
    FAMILY_ALLOWED_UPDATES,
    TelegramClient,
    telegram_allowed_updates,
)
from app.channels.telegram.models import Update
from tests.telegram_fakes import TOKEN, FakeTelegram, text_update

pytestmark = pytest.mark.anyio


def callback_payload(data="fam:acc:0123456789abcdef", **overrides):
    payload = {
        "update_id": 5,
        "callback_query": {
            "id": "cbq-1",
            "from": {"id": 42, "is_bot": False, "first_name": "Rahul", "last_name": "S", "username": "rahul_s", "language_code": "hi"},
            "message": {"message_id": 77, "date": 1_700_000_000, "chat": {"id": 42, "type": "private"}},
            "chat_instance": "x",
            "data": data,
        },
    }
    payload["callback_query"].update(overrides)
    return payload


# --- models -----------------------------------------------------------------------------------------------


def test_callback_query_is_parsed():
    update = Update.model_validate(callback_payload())
    query = update.callback_query
    assert update.message is None and update.my_chat_member is None
    assert (query.id, query.data) == ("cbq-1", "fam:acc:0123456789abcdef")
    assert (query.from_.id, query.from_.first_name, query.from_.last_name, query.from_.username) == (42, "Rahul", "S", "rahul_s")
    assert query.from_.language_code == "hi"
    assert (query.message.message_id, query.message.chat.id, query.message.chat.type, query.message.date) == (77, 42, "private", 1_700_000_000)


def test_callback_query_without_message_or_data_still_parses():
    update = Update.model_validate({"update_id": 1, "callback_query": {"id": "x", "from": {"id": 1}, "inline_message_id": "abc"}})
    assert update.callback_query.message is None and update.callback_query.data is None


def test_my_chat_member_is_parsed():
    update = Update.model_validate(
        {
            "update_id": 9,
            "my_chat_member": {
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False},
                "date": 1_700_000_000,
                "old_chat_member": {"user": {"id": 99, "is_bot": True}, "status": "member"},
                "new_chat_member": {"user": {"id": 99, "is_bot": True}, "status": "kicked", "until_date": 0},
            },
        }
    )
    event = update.my_chat_member
    assert (event.chat.id, event.chat.type, event.from_.id) == (42, "private", 42)
    assert (event.old_chat_member.status, event.new_chat_member.status) == ("member", "kicked")


def test_user_profile_fields_are_optional():
    from tests.telegram_fakes import parse

    user = parse(text_update("hi")).message.from_
    assert user.first_name == "Test" and user.last_name is None and user.username is None


def test_message_date_is_optional_and_parsed():
    from tests.telegram_fakes import parse

    assert parse(text_update("hi")).message.date == 1_700_000_000
    payload = text_update("hi")
    del payload["message"]["date"]
    assert parse(payload).message.date is None


def test_unknown_fields_in_new_update_types_are_ignored():
    payload = callback_payload()
    payload["callback_query"]["brand_new_field"] = {"a": 1}
    payload["callback_query"]["from"]["is_premium"] = True
    assert Update.model_validate(payload).callback_query.data == "fam:acc:0123456789abcdef"


@pytest.mark.parametrize("bad", [{"update_id": 1, "callback_query": {"from": {"id": 1}}}, {"update_id": 1, "callback_query": "x"},
                                 {"update_id": 1, "my_chat_member": {"chat": {"id": 1, "type": "private"}}}])  # fmt: skip
def test_malformed_new_update_types_are_rejected_not_half_parsed(bad):
    with pytest.raises(Exception):  # noqa: B017 (pydantic.ValidationError; any failure to parse is the point)
        Update.model_validate(bad)


# --- allowed updates --------------------------------------------------------------------------------------------


def test_button_presses_are_always_requested_but_block_events_only_with_family_mode():
    # Emergency buttons are always on, so callback_query is always needed; my_chat_member is Family Mode only.
    assert telegram_allowed_updates(False) == ["message", "callback_query"]
    assert telegram_allowed_updates(True) == ["message", "callback_query", "my_chat_member"] == list(FAMILY_ALLOWED_UPDATES)
    assert list(DEFAULT_ALLOWED_UPDATES) == ["message"]  # a bare client still asks for messages only, as in Phase 2


def test_allowed_updates_helper_returns_a_fresh_list():
    first = telegram_allowed_updates(True)
    first.append("x")
    assert telegram_allowed_updates(True) == ["message", "callback_query", "my_chat_member"]


async def test_client_default_allowed_updates_are_unchanged_from_phase_2():
    tg = FakeTelegram()
    client = tg.client()
    await client.get_updates(timeout=1)
    await client.set_webhook("https://x.example/webhook/telegram", "s")
    assert [p["allowed_updates"] for p in tg.method_calls("getUpdates")] == [["message"]]
    assert [p["allowed_updates"] for p in tg.method_calls("setWebhook")] == [["message"]]


async def test_client_can_be_given_the_family_update_types():
    tg = FakeTelegram()
    client = tg.client(allowed_updates=telegram_allowed_updates(True))
    await client.get_updates(timeout=1)
    await client.set_webhook("https://x.example/webhook/telegram", "s")
    expected = ["message", "callback_query", "my_chat_member"]
    assert tg.method_calls("getUpdates")[0]["allowed_updates"] == expected
    assert tg.method_calls("setWebhook")[0]["allowed_updates"] == expected


async def test_an_explicit_argument_still_wins():
    tg = FakeTelegram()
    client = tg.client(allowed_updates=["message", "callback_query"])
    await client.get_updates(timeout=1, allowed_updates=["message"])
    assert tg.method_calls("getUpdates")[0]["allowed_updates"] == ["message"]


# --- buttons -----------------------------------------------------------------------------------------------------------


async def test_send_message_can_carry_an_inline_keyboard():
    tg = FakeTelegram()
    keyboard = {"inline_keyboard": [[{"text": "Yes", "callback_data": "fam:yes:0123456789abcdef"}]]}
    await tg.client().send_message(5, "hello", reply_markup=keyboard)
    assert tg.sent_messages[0]["reply_markup"] == keyboard


async def test_send_message_without_a_keyboard_is_unchanged():
    tg = FakeTelegram()
    await tg.client().send_message(5, "hello")
    assert "reply_markup" not in tg.sent_messages[0]


async def test_answer_callback_query_without_text():
    tg = FakeTelegram()
    await tg.client().answer_callback_query("cbq-1")
    assert tg.method_calls("answerCallbackQuery") == [{"callback_query_id": "cbq-1"}]


async def test_answer_callback_query_with_text_and_alert_flag():
    tg = FakeTelegram()
    await tg.client().answer_callback_query("cbq-1", "Expired", show_alert=True)
    assert tg.method_calls("answerCallbackQuery") == [{"callback_query_id": "cbq-1", "text": "Expired", "show_alert": True}]


async def test_remove_buttons_sends_an_empty_keyboard():
    tg = FakeTelegram()
    await tg.client().remove_buttons(42, 77)
    assert tg.method_calls("editMessageReplyMarkup") == [
        {"chat_id": 42, "message_id": 77, "reply_markup": {"inline_keyboard": []}}
    ]


async def test_the_token_is_not_in_any_new_request_body():
    tg = FakeTelegram()
    client: TelegramClient = tg.client()
    await client.send_message(1, "x", reply_markup={"inline_keyboard": []})
    await client.answer_callback_query("c", "t")
    await client.remove_buttons(1, 2)
    assert all(TOKEN not in request.content.decode() for request in tg.requests)
