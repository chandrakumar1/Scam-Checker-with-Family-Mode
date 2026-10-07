"""Emergency button security: forged, tampered, wrong-user, expired and out-of-context
presses; acknowledgement discipline; the 24-hour boundary; and log privacy."""

import logging
import re
from datetime import timedelta

import pytest

from app.channels.telegram.emergency import parse_emergency_data
from app.config import Settings
from app.logging_config import configure_logging
from app.replies import emergency_templates as ET
from app.replies.emergency_formatter import emergency_message, guidance_message, popup, question_message
from tests.family_telegram_helpers import ANOUK, MEERA, QUENTIN, ZEBRINA, Harness, User, callback_update
from tests.telegram_fakes import TOKEN, WEBHOOK_SECRET
from tests.test_emergency_flow import Boom, emergency_alerts, scam_reply

pytestmark = pytest.mark.anyio

INVALID_EN = popup("invalid", "en")
INVALID_HI = popup("invalid", "hi")
FAMILY_STALE_EN = __import__("app.replies.family_templates", fromlist=["POPUPS"]).POPUPS["stale"]["en"]
EXPIRED_EN = popup("expired", "en")
GUIDANCE_EN = guidance_message("en")


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


async def question(h, user):
    """/emergency -> returns (yes_data, no_data) from the question."""
    await h.say(user, "/emergency")
    return h.em_data(user, "y"), h.em_data(user, "n")


def flip(token: str, position: int = 10) -> str:
    return token[:position] + ("A" if token[position] != "A" else "B") + token[position + 1 :]


def snapshot(h):
    """Everything an invalid press must leave untouched."""
    return (len(h.tg.delivered), len(h.removed_buttons()), len(h.alerts()), len(h.people()), len(h.tg.method_calls("getFile")))


async def assert_rejected(h, user, data, popup_text, **tap_kwargs):
    before_state, before_acks = snapshot(h), len(h.answers())
    await h.tap(user, data, **tap_kwargs)
    assert snapshot(h) == before_state, "a rejected press changed something"
    assert len(h.answers()) == before_acks + 1, "a press must be acknowledged exactly once"
    assert h.answers()[-1]["text"] == popup_text


# --- tampered and malformed data ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["o", "y", "n"])
async def test_a_tampered_token_changes_nothing(h, action):
    await h.link_up(ZEBRINA, QUENTIN)
    yes, no = await question(h, ZEBRINA)
    _, token = parse_emergency_data(yes)
    await assert_rejected(h, ZEBRINA, f"em:{action}:{flip(token)}", INVALID_EN)
    await assert_rejected(h, ZEBRINA, f"em:{action}:{flip(token, 0)}", INVALID_EN)
    await assert_rejected(h, ZEBRINA, f"em:{action}:{flip(token, 20)}", INVALID_EN)
    assert emergency_alerts(h) == []


async def test_a_tampered_press_sends_no_guidance_and_alerts_nobody(h):
    await h.link_up(ZEBRINA, QUENTIN)
    yes, _ = await question(h, ZEBRINA)
    quentin_before = len(h.sent_to(QUENTIN))
    await h.tap(ZEBRINA, "em:y:" + flip(parse_emergency_data(yes)[1]))
    assert GUIDANCE_EN not in [p["text"] for p in h.sent_to(ZEBRINA)]
    assert len(h.sent_to(QUENTIN)) == quentin_before


@pytest.mark.parametrize(
    "suffix",
    ["", ":", ":short", ":" + "a" * 38, ":" + "a" * 40, ":" + "!" * 39, ":" + "é" * 39, ":" + "a" * 39 + ":x", ":" + "a" * 39 + "\n"],
)
@pytest.mark.parametrize("prefix", ["em:y", "em:n", "em:o", "em:z", "em:Y", "em:", "em"])
async def test_malformed_emergency_data_is_rejected(h, prefix, suffix):
    data = prefix + suffix
    assert parse_emergency_data(data) is None  # every combination here is malformed (well-formed-but-unsigned is tested below)
    # Data that does not even start with "em:" is not an emergency button: the Family handler answers it with its own popup.
    expected = INVALID_EN if data.startswith("em:") else FAMILY_STALE_EN
    await assert_rejected(h, ZEBRINA, data, expected)


async def test_well_formed_but_unsigned_data_is_rejected(h):
    forged = "em:y:" + "A" * 39  # right shape, no valid signature
    await assert_rejected(h, ZEBRINA, forged, INVALID_EN)
    assert emergency_alerts(h) == []


async def test_oversized_data_is_rejected(h):
    await assert_rejected(h, ZEBRINA, "em:y:" + "a" * 100, INVALID_EN)


async def test_a_token_made_for_another_bot_is_rejected(h):
    from app.channels.telegram.callback_tokens import CallbackTokenCodec

    other = CallbackTokenCodec("987654321:AAOtherBotTokenOnly_zyxwvutsrqponmlkjih")
    token = other.issue(ZEBRINA.id, None, h.clock())
    await assert_rejected(h, ZEBRINA, f"em:y:{token}", INVALID_EN)


# --- wrong user / replay by another user --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["y", "n", "o"])
@pytest.mark.parametrize("intruder", [ANOUK, QUENTIN, MEERA])
async def test_another_user_cannot_use_someone_elses_button(h, action, intruder):
    await h.link_up(ZEBRINA, QUENTIN)
    yes, _ = await question(h, ZEBRINA)
    token = parse_emergency_data(yes)[1]
    quentin_before = len(h.sent_to(QUENTIN))
    popup_text = INVALID_HI if intruder is QUENTIN else INVALID_EN  # the popup follows the PRESSER's language
    await assert_rejected(h, intruder, f"em:{action}:{token}", popup_text)
    assert emergency_alerts(h) == []
    assert GUIDANCE_EN not in [p["text"] for p in h.sent_to(intruder)]  # the intruder is told nothing about an emergency
    assert len(h.sent_to(QUENTIN)) == quentin_before


async def test_a_genuine_token_replayed_by_another_user_is_rejected_even_if_the_owner_already_used_it(h):
    yes, _ = await question(h, ZEBRINA)
    await h.tap(ZEBRINA, yes)  # the owner uses it
    await assert_rejected(h, ANOUK, yes, INVALID_EN)


async def test_the_wrong_user_gets_the_same_answer_as_a_tampered_press_so_nothing_is_revealed(h):
    yes, _ = await question(h, ZEBRINA)
    token = parse_emergency_data(yes)[1]
    await h.tap(ANOUK, f"em:y:{token}")
    await h.tap(ANOUK, f"em:y:{flip(token)}")
    assert h.answers()[-1] == {**h.answers()[-2], "callback_query_id": h.answers()[-1]["callback_query_id"]}  # identical text


# --- expiry: 24 hours --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "valid"),
    [
        (timedelta(hours=23, minutes=59), True),
        (timedelta(hours=24), True),
        (timedelta(hours=24, minutes=1), False),
        (timedelta(days=3), False),
    ],
)
async def test_the_question_buttons_work_for_24_hours_and_not_after(h, age, valid):
    await h.link_up(ZEBRINA, QUENTIN)
    yes, _ = await question(h, ZEBRINA)
    h.clock.advance(seconds=age.total_seconds())
    if valid:
        before = len(h.sent_to(ZEBRINA))
        await h.tap(ZEBRINA, yes)
        assert h.sent_to(ZEBRINA)[before]["text"] == GUIDANCE_EN  # it worked: the guidance goes out
        assert len(emergency_alerts(h)) == 1
    else:
        edits_before = len(h.removed_buttons())
        await assert_rejected_expired(h, ZEBRINA, yes, edits_before)
        assert emergency_alerts(h) == []


async def assert_rejected_expired(h, user, data, edits_before):
    delivered, acks = len(h.tg.delivered), len(h.answers())
    await h.tap(user, data, message_id=640)
    assert len(h.tg.delivered) == delivered  # no guidance, no alert, no message at all
    assert len(h.answers()) == acks + 1 and h.answers()[-1]["text"] == EXPIRED_EN
    assert len(h.removed_buttons()) == edits_before + 1 and h.removed_buttons()[-1]["message_id"] == 640  # dead buttons removed


@pytest.mark.parametrize(
    ("age", "valid"),
    [(timedelta(hours=23, minutes=59), True), (timedelta(hours=24, minutes=1), False)],
)
async def test_the_button_on_a_scam_reply_also_lasts_24_hours(h, age, valid):
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP you received", "likely_scam")
    data = h.buttons(reply)[0]["callback_data"]
    h.clock.advance(seconds=age.total_seconds())
    before = len(h.sent_to(ZEBRINA))
    await h.tap(ZEBRINA, data)
    if valid:
        assert h.sent_to(ZEBRINA)[before]["text"] == question_message("en")
        assert h.answers()[-1].get("text") is None
    else:
        assert len(h.sent_to(ZEBRINA)) == before and h.answers()[-1]["text"] == EXPIRED_EN


async def test_the_question_opened_late_gets_fresh_buttons_that_do_not_expire_immediately(h):
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP", "likely_scam")
    h.clock.advance(hours=23, minutes=59)
    await h.tap(ZEBRINA, h.buttons(reply)[0]["callback_data"])  # opened just inside the limit
    h.clock.advance(hours=1)  # an hour later: the reply's own token is long dead...
    await h.tap(ZEBRINA, h.em_data(ZEBRINA, "y"))  # ...but the question's fresh one is not
    assert h.sent_to(ZEBRINA)[-2]["text"] == GUIDANCE_EN


async def test_an_expired_press_by_a_stranger_is_just_invalid_not_expired(h):
    yes, _ = await question(h, ZEBRINA)
    h.clock.advance(days=2)
    await assert_rejected(h, ANOUK, yes, INVALID_EN)  # no hint that the token was genuine


async def test_after_expiry_the_command_still_works(h):
    yes, _ = await question(h, ZEBRINA)
    h.clock.advance(days=2)
    await h.tap(ZEBRINA, yes)
    await h.say(ZEBRINA, "/emergency")  # the popup told them to do exactly this
    await h.tap(ZEBRINA, h.em_data(ZEBRINA, "y"))
    assert GUIDANCE_EN in [p["text"] for p in h.sent_to(ZEBRINA)]


async def test_a_token_from_the_future_is_rejected(h):
    from app.channels.telegram.emergency import _data

    token = h.codec.issue(ZEBRINA.id, None, h.clock.now + timedelta(days=1))
    await assert_rejected(h, ZEBRINA, _data("y", token), INVALID_EN)


# --- context: private chat and the message it is attached to ----------------------------------------------------------------------------------


async def test_a_valid_token_pressed_in_a_group_is_rejected(h):
    yes, _ = await question(h, ZEBRINA)
    await assert_rejected(h, ZEBRINA, yes, INVALID_EN, chat_type="supergroup", chat_id=-100555)
    assert emergency_alerts(h) == [] and GUIDANCE_EN not in [p["text"] for p in h.tg.delivered]


async def test_a_valid_token_pressed_from_someone_elses_private_chat_is_rejected(h):
    yes, _ = await question(h, ZEBRINA)
    await assert_rejected(h, ZEBRINA, yes, INVALID_EN, chat_id=ANOUK.id)


async def test_a_press_with_no_message_attached_is_rejected(h):
    yes, _ = await question(h, ZEBRINA)
    await assert_rejected(h, ZEBRINA, yes, INVALID_EN, with_message=False)


async def test_a_bot_pressing_is_ignored_entirely(h):
    bot = User(9, "SomeBot", None, "en", is_bot=True)
    before = len(h.tg.calls)
    await h.tap(bot, "em:y:" + "A" * 39)
    assert len(h.tg.calls) == before


async def test_forged_context_presses_get_only_a_generic_popup_no_message_and_no_edit(h):
    yes, _ = await question(h, ZEBRINA)
    delivered, edits = len(h.tg.delivered), len(h.removed_buttons())
    await h.tap(ZEBRINA, yes, chat_type="group", chat_id=-1)
    await h.tap(ZEBRINA, yes, with_message=False)
    assert len(h.tg.delivered) == delivered and len(h.removed_buttons()) == edits


# --- acknowledgement discipline --------------------------------------------------------------------------------------------------------------------


async def test_every_kind_of_press_is_acknowledged_exactly_once(h):
    await h.link_up(ZEBRINA, QUENTIN)
    yes, no = await question(h, ZEBRINA)
    token = parse_emergency_data(yes)[1]
    cases = [
        ("em:o:" + token, {}),
        (no, {}),
        (yes, {}),  # a real Yes
        (yes, {}),  # a repeated Yes
        ("em:y:" + flip(token), {}),  # tampered
        (yes, {"chat_type": "group", "chat_id": -9}),  # wrong place
        (yes, {"with_message": False}),
    ]
    for data, kwargs in cases:
        acks = len(h.answers())
        await h.tap(ZEBRINA, data, **kwargs)
        assert len(h.answers()) == acks + 1, (data[:8], kwargs)
    ids = [a["callback_query_id"] for a in h.answers()]
    assert len(ids) == len(set(ids))  # a distinct acknowledgement per press


async def test_even_a_crash_while_processing_acknowledges_once(h, monkeypatch):
    yes, _ = await question(h, ZEBRINA)
    monkeypatch.setattr(h.emergency._codec, "verify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    acks = len(h.answers())
    await h.tap(ZEBRINA, yes)
    assert len(h.answers()) == acks + 1
    assert GUIDANCE_EN in [p["text"] for p in h.sent_to(ZEBRINA)]  # and the person still got the 1930 steps


async def test_a_failing_acknowledgement_does_not_stop_the_emergency(h):
    from tests.telegram_fakes import api_error

    await h.link_up(ZEBRINA, QUENTIN)
    yes, _ = await question(h, ZEBRINA)
    h.tg.queue("answerCallbackQuery", api_error(400, "query is too old"))
    quentin_before = len(h.sent_to(QUENTIN))
    await h.tap(ZEBRINA, yes)
    assert len(h.sent_to(QUENTIN)) == quentin_before + 1  # the alert still went out


# --- state is never changed by an invalid press -------------------------------------------------------------------------------------------------------


async def test_a_flood_of_invalid_presses_creates_nothing(h):
    await h.link_up(ZEBRINA, QUENTIN)
    yes, _ = await question(h, ZEBRINA)
    token = parse_emergency_data(yes)[1]
    state = snapshot(h)
    for data in [f"em:y:{flip(token, i)}" for i in range(0, 30, 3)] + ["em:y:" + "A" * 39] * 5:
        await h.tap(ANOUK, data)
        await h.tap(ZEBRINA, data)
    assert snapshot(h)[1:] == state[1:] and len(h.alerts()) == 0


async def test_invalid_presses_never_touch_the_database(h):
    h.alert_sender._family = Boom()  # any database access would raise, loudly
    yes, _ = await question(h, ZEBRINA)
    await h.tap(ANOUK, yes)
    await h.tap(ZEBRINA, "em:y:" + "A" * 39)
    assert [a["text"] for a in h.answers()[-2:]] == [INVALID_EN, INVALID_EN]


# --- the languages -----------------------------------------------------------------------------------------------------------------------------------


async def test_popups_are_in_the_pressers_language(h):
    await h.tap(QUENTIN, "em:y:" + "A" * 39)
    await h.tap(ANOUK, "em:y:" + "A" * 39)
    assert [a["text"] for a in h.answers()] == [ET.POPUPS["invalid"]["hi"], ET.POPUPS["invalid"]["en"]]
    assert all("/emergency" in a["text"] for a in h.answers())


# --- privacy of logs --------------------------------------------------------------------------------------------------------------------------------------


async def test_nothing_sensitive_reaches_the_logs_through_every_emergency_path(tmp_path, caplog):
    harness = Harness(tmp_path)
    p = User(7771001, "Zebrina", "zebrina_priv", "en")
    g = User(7772001, "Quentin", "quentin_priv", "hi")
    other = User(7773001, "Intruder", "intruder_priv", "en")
    secret_text = "SENTINEL-LOG-CHECK-9981 please share the OTP http://evil.example/x"
    # Production logging setup (httpx quietened, token-redaction filter on); conftest restores it after the test.
    configure_logging(Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_webhook_secret=WEBHOOK_SECRET))
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="app")
    seen: list[str] = []
    try:
        await harness.link_up(p, g)
        reply = await scam_reply(harness, p, secret_text, "likely_scam", explanation=secret_text)
        seen.append(harness.buttons(reply)[0]["callback_data"])
        await harness.tap(p, seen[-1])
        yes, no = harness.em_data(p, "y"), harness.em_data(p, "n")
        seen += [yes, no]
        await harness.tap(p, no)
        await harness.tap(p, yes)
        await harness.tap(p, yes)  # repeated: deduped
        await harness.tap(other, yes)  # wrong user
        await harness.tap(p, "em:y:" + flip(parse_emergency_data(yes)[1]))  # tampered
        await harness.tap(p, yes, chat_type="group", chat_id=-3)  # wrong context
        harness.clock.advance(days=2)
        await harness.tap(p, yes)  # expired
        harness.tg.blocked_chats.add(g.id)
        harness.clock.advance(minutes=5)
        await harness.say(p, "/emergency")
        await harness.tap(p, harness.em_data(p, "y"))  # a failed delivery
        harness.alert_sender._family = Boom()
        harness.clock.advance(minutes=5)
        await harness.tap(p, harness.em_data(p, "y"))  # a database failure
    finally:
        harness.close()

    log = caplog.text
    assert "emergency:" in log  # outcome labels are logged
    tokens = [parse_emergency_data(data)[1] for data in seen]
    for secret in (*seen, *tokens, "em:y:", "em:o:", "em:n:", "SENTINEL-LOG-CHECK", "evil.example", "Zebrina", "Quentin",
                   "Intruder", "zebrina_priv", "quentin_priv", "intruder_priv", "7771001", "7772001", "7773001",
                   "123456789:AAE", "test-webhook-secret"):  # fmt: skip
        assert secret not in log, f"{secret!r} leaked into the logs"
    assert not re.search(r"bot\d{5,}:", log) and "api.telegram.org" not in log


async def test_failures_are_logged_by_class_and_status_only(tmp_path, caplog):
    from tests.telegram_fakes import api_error

    harness = Harness(tmp_path)
    configure_logging(Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_webhook_secret=WEBHOOK_SECRET))
    caplog.set_level(logging.DEBUG, logger="app")
    try:
        await harness.link_up(ZEBRINA, QUENTIN)
        await harness.say(ZEBRINA, "/emergency")
        harness.tg.queue("sendMessage", api_error(500, "Internal error mentioning SECRET-BODY"))
        harness.tg.blocked_chats.add(QUENTIN.id)
        await harness.tap(ZEBRINA, harness.em_data(ZEBRINA, "y"))
    finally:
        harness.close()
    assert "TelegramAPIError" in caplog.text and "status=" in caplog.text
    assert "SECRET-BODY" not in caplog.text and "Forbidden" not in caplog.text and "blocked by the user" not in caplog.text
    assert "Quentin" not in caplog.text and str(QUENTIN.id) not in caplog.text


# --- how the two kinds of buttons coexist ---------------------------------------------------------------------------------------------------------------


async def test_family_buttons_still_work_with_the_emergency_handler_wired(h):
    await h.say(ZEBRINA, "/family")
    await h.tap(ZEBRINA, "fam:add")
    assert "Before I make an invitation link" in h.sent_to(ZEBRINA)[-1]["text"]


async def test_an_emergency_press_is_never_handled_by_the_family_handler(h):
    yes, _ = await question(h, ZEBRINA)
    await h.tap(ZEBRINA, yes)
    assert GUIDANCE_EN in [p["text"] for p in h.sent_to(ZEBRINA)]
    assert h.people() == []  # the family handler never saw it (it would have created a person row)


async def test_an_emergency_press_without_any_emergency_handler_is_harmless(tmp_path):
    harness = Harness(tmp_path, with_emergency=False)
    try:
        await harness.tap(ZEBRINA, "em:y:" + "A" * 39)
        assert harness.sent_to(ZEBRINA) == [] and harness.alerts() == []
    finally:
        harness.close()


async def test_a_message_that_merely_contains_em_is_not_an_emergency(h):
    h.analyzer.result = __import__("tests.family_telegram_helpers", fromlist=["x"]).result_for("Hello there", "no_obvious_red_flags")
    await h.say(ZEBRINA, "em:y:" + "A" * 39)  # text, not a button press
    assert h.analyzer.calls  # it went through the ordinary analysis path
