from datetime import UTC, datetime, timedelta

import pytest

from app.channels.telegram.family_handler import (
    CONFIRM_FRESHNESS,
    FRESH_ACTIONS,
    MAX_CALLBACK_DATA_BYTES,
    PLAIN_ACTIONS,
    REF_ACTIONS,
    FamilyTelegramHandler,
    callback_data,
    parse_callback_data,
    parse_command,
)
from app.channels.telegram.models import Message
from app.family import tokens

REF = "0123456789abcdef"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class _Noop:
    """Stands in for the client and family service: parsing never touches them."""


def handler(**kwargs) -> FamilyTelegramHandler:
    return FamilyTelegramHandler(_Noop(), _Noop(), clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


def message(text: str | None, **extra) -> Message:
    return Message.model_validate({"message_id": 1, "chat": {"id": 5, "type": "private"}, "text": text, **extra})


# --- commands ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/family", ("family", "")),
        ("/FAMILY", ("family", "")),
        ("/family@ruko_dev_bot", ("family", "")),
        ("/family please", ("family", "please")),
        ("/start fam_abc", ("start", "fam_abc")),
        ("/start   fam_abc  ", ("start", "fam_abc")),
        ("/start@ruko_dev_bot fam_abc", ("start", "fam_abc")),
        ("/leave", ("leave", "")),
        ("/deleteme", ("deleteme", "")),
    ],
)
def test_parse_command(text, expected):
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", [None, "", "family", " /family", "hello /family", "/", "/ family", "/fam-ily", "//family", "/family!"])
def test_parse_command_rejects_non_commands(text):
    assert parse_command(text) is None


def test_matches_only_the_family_commands_and_the_invite_deep_link():
    h = handler()
    for text in ("/family", "/leave", "/deleteme", "/family@ruko_dev_bot", "/Leave", f"/start fam_{'a' * 22}", "/start fam_", "/start fam_junk"):
        assert h.matches(message(text)), text
    for text in ("/start", "/start other_payload", "/start famous", "/help", "/emergency", "hello", "/familyx", None, "/start@ruko_dev_bot"):
        assert not h.matches(message(text)), text


def test_matches_ignores_captions_and_media():
    assert not handler().matches(Message.model_validate({"message_id": 1, "chat": {"id": 5, "type": "private"}, "caption": "/family"}))


def test_matching_is_synchronous_and_touches_nothing():
    # The service calls matches() before doing anything else, so it must be pure.
    h = handler()
    assert h.matches(message("/family")) is True  # returns a bool, not a coroutine


# --- button data ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(PLAIN_ACTIONS))
def test_plain_actions_parse(action):
    assert parse_callback_data(f"fam:{action}") == (action, None)


@pytest.mark.parametrize("action", sorted(REF_ACTIONS))
def test_ref_actions_parse(action):
    assert parse_callback_data(f"fam:{action}:{REF}") == (action, REF)


@pytest.mark.parametrize(
    "data",
    [
        None, 5, b"fam:add", "", "fam", "fam:", "fam:add:", "fam:add:extra", "fam:nope", "fam:acc", "fam:acc:", "fam:acc:short",
        f"fam:acc:{REF}:more", f"fam:acc:{REF.upper()}", f"fam:acc:{REF[:-1]}g", f"fam: acc:{REF}", f"FAM:acc:{REF}",
        f"other:acc:{REF}", f"fam:add:{REF}", f"fam:addy:{REF}", f"fam:rm:{REF}\n", "fam:rm:" + "a" * 100, "x" * 100,
        f"fam:acc:{REF}" + "é" * 40,
    ],
)
def test_malformed_button_data_is_rejected(data):
    assert parse_callback_data(data) is None


def test_every_action_is_either_plain_or_ref_never_both():
    assert not (PLAIN_ACTIONS & REF_ACTIONS)
    assert FRESH_ACTIONS <= PLAIN_ACTIONS  # the actions that need a recent message carry no ref


def test_built_button_data_round_trips_and_fits_telegrams_limit():
    for action in PLAIN_ACTIONS:
        data = callback_data(action)
        assert parse_callback_data(data) == (action, None)
    for action in REF_ACTIONS:
        ref = tokens.new_ref()
        data = callback_data(action, ref)
        assert parse_callback_data(data) == (action, ref)
        assert len(data.encode()) <= MAX_CALLBACK_DATA_BYTES == 64


def test_button_data_carries_no_names_ids_or_tokens():
    data = callback_data("acc", tokens.new_ref())
    assert data.count(":") == 2 and data.startswith("fam:acc:")
    assert len(data) == len("fam:acc:") + 16


# --- freshness of confirmation messages ---------------------------------------------------------------------------------------


def fresh(age_seconds: float | None, h: FamilyTelegramHandler | None = None) -> bool:
    h = h or handler()
    date = None if age_seconds is None else int((NOW - timedelta(seconds=age_seconds)).timestamp())
    return h._is_fresh(Message.model_validate({"message_id": 1, "chat": {"id": 5, "type": "private"}, "date": date}))


def test_a_confirmation_message_is_fresh_for_thirty_minutes():
    assert CONFIRM_FRESHNESS == timedelta(minutes=30)
    assert fresh(0) and fresh(60) and fresh(29 * 60 + 59) and fresh(30 * 60)
    assert not fresh(30 * 60 + 1) and not fresh(3600) and not fresh(86400)


def test_a_message_without_a_date_is_never_fresh():
    assert not fresh(None)


def test_small_clock_skew_into_the_future_is_tolerated_but_not_much():
    assert fresh(-30) and fresh(-60)
    assert not fresh(-61) and not fresh(-3600)


def test_freshness_window_is_configurable():
    short = handler(confirm_freshness=timedelta(minutes=5))
    assert fresh(299, short) and not fresh(301, short)
