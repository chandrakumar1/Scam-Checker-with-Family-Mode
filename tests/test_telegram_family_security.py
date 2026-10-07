"""Forged, stale and hostile input to Family Mode through TelegramService; failure
handling; the flag-off regression; log privacy. No network."""

import asyncio
import logging
from datetime import timedelta

import pytest

from app.channels.telegram.family_handler import callback_data
from app.family import tokens
from app.replies import family_templates as FT
from app.replies import templates as T
from app.replies.family_formatter import family_error_message, family_message
from app.replies.formatter import error_message, help_message, start_message
from tests.family_helpers import all_links
from tests.family_telegram_helpers import ANOUK, MEERA, QUENTIN, ZEBRINA, Harness, User
from tests.telegram_fakes import make_result

pytestmark = pytest.mark.anyio

STALE_EN = FT.POPUPS["stale"]["en"]
STALE_HI = FT.POPUPS["stale"]["hi"]


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def snapshot(h):
    return [(link.ref, link.status, link.guardian_id, link.token_hash, link.expires_at, link.ended_by) for link in all_links(h.engine)]


async def awaiting_guardian(h):
    """Zebrina invited, Quentin opened: returns the link ref."""
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    return h.only_link().ref


async def awaiting_protected(h):
    ref = await awaiting_guardian(h)
    await h.tap(QUENTIN, callback_data("acc", ref))
    return ref


# --- malformed button data -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        None, "", "fam", "fam:", "fam:nope", "fam:acc", "fam:acc:", "fam:acc:zzzz", "fam:acc:" + "a" * 16 + ":x",
        "fam:acc:" + "A" * 16, "other:acc:" + "a" * 16, "fam:add:extra", "x" * 200, "fam:acc:" + "é" * 20, "emergency:y:token",
    ],
)
async def test_malformed_button_data_changes_nothing_and_is_acknowledged_once(h, data):
    await awaiting_protected(h)
    delivered, state, presses, edits = len(h.tg.delivered), snapshot(h), len(h.answers()), len(h.removed_buttons())
    await h.tap(ZEBRINA, data)
    assert len(h.tg.delivered) == delivered and snapshot(h) == state
    assert len(h.answers()) == presses + 1 and h.answers()[-1]["text"] == STALE_EN
    assert len(h.removed_buttons()) == edits  # forged data edits no message either


# --- wrong user ---------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["acc", "dec"])
@pytest.mark.parametrize("intruder", [ZEBRINA, ANOUK, MEERA])
async def test_only_the_invited_helper_can_accept_or_decline(h, action, intruder):
    ref = await awaiting_guardian(h)
    delivered, state = len(h.tg.delivered), snapshot(h)
    await h.tap(intruder, callback_data(action, ref))
    assert snapshot(h) == state
    assert h.answers()[-1]["text"] == STALE_EN
    assert len(h.tg.delivered) == delivered  # not even the intruder was messaged back
    assert all(p["chat_id"] != QUENTIN.id for p in h.tg.delivered[delivered:])


@pytest.mark.parametrize("action", ["yes", "no"])
@pytest.mark.parametrize("intruder", [QUENTIN, ANOUK, MEERA])
async def test_only_the_protected_person_can_confirm_or_refuse(h, action, intruder):
    ref = await awaiting_protected(h)
    delivered, state = len(h.tg.delivered), snapshot(h)
    await h.tap(intruder, callback_data(action, ref))
    assert snapshot(h) == state and h.statuses() == ["awaiting_protected"]
    assert len(h.tg.delivered) == delivered
    expected_popup = STALE_HI if intruder is QUENTIN else STALE_EN  # the popup follows the PRESSER's language
    assert h.answers()[-1]["text"] == expected_popup


async def test_the_helper_cannot_confirm_themselves_by_forging_yes(h):
    ref = await awaiting_protected(h)
    await h.tap(QUENTIN, callback_data("yes", ref))
    assert h.statuses() == ["awaiting_protected"]
    assert h.alerts() == []


async def test_remove_button_only_works_for_the_protected_person(h):
    ref = await h.link_up(ZEBRINA, QUENTIN)
    state = snapshot(h)
    for intruder in (QUENTIN, ANOUK):  # the helper forging a "remove" press is not allowed to act as the protected person
        await h.tap(intruder, callback_data("rm", ref))
    assert snapshot(h) == state
    assert [a["text"] for a in h.answers()[-2:]] == [STALE_HI, STALE_EN]


async def test_leave_button_only_works_for_the_helper(h):
    ref = await h.link_up(ZEBRINA, QUENTIN)
    state = snapshot(h)
    for intruder in (ZEBRINA, ANOUK):  # the protected person forging "leave" must not end the link as if the helper left
        await h.tap(intruder, callback_data("lv", ref))
    assert snapshot(h) == state
    assert h.only_link().ended_by is None


async def test_a_stranger_with_a_real_looking_reference_creates_nothing(h):
    await h.tap(ANOUK, callback_data("acc", tokens.new_ref()))
    await h.tap(ANOUK, callback_data("yes", tokens.new_ref()))
    await h.tap(ANOUK, callback_data("rm", tokens.new_ref()))
    assert h.people() == [] and h.statuses() == []
    assert [a["text"] for a in h.answers()] == [STALE_EN] * 3
    assert h.tg.delivered == []


# --- wrong place ---------------------------------------------------------------------------------------------------------------------


async def test_a_press_that_did_not_come_from_the_users_own_private_chat_is_ignored(h):
    ref = await awaiting_guardian(h)
    delivered, state, presses, edits = len(h.tg.delivered), snapshot(h), len(h.answers()), len(h.removed_buttons())
    await h.tap(QUENTIN, callback_data("acc", ref), chat_type="group", chat_id=-100123)  # a group
    await h.tap(QUENTIN, callback_data("acc", ref), chat_id=ZEBRINA.id)  # a private chat, but someone else's
    await h.tap(QUENTIN, callback_data("acc", ref), with_message=False)  # an inline-mode press: no message at all
    assert snapshot(h) == state and len(h.tg.delivered) == delivered
    assert len(h.answers()) == presses + 3  # each press is still acknowledged exactly once...
    assert all(a["text"] == STALE_HI for a in h.answers()[-3:])  # ...with the generic popup
    assert len(h.removed_buttons()) == edits  # and no message anywhere was edited


async def test_a_bot_pressing_a_button_is_ignored_entirely(h):
    bot = User(9, "SomeBot", None, "en", is_bot=True)
    await h.tap(bot, "fam:add")
    assert h.tg.calls == []  # no reply, no acknowledgement, no database access


async def test_family_commands_in_groups_are_ignored(h):
    for command in ("/family", "/leave", "/deleteme", "/start fam_" + "a" * 22):
        await h.say(ZEBRINA, command, chat_type="supergroup", chat_id=-1001)
    assert h.tg.calls == [] and h.people() == []


async def test_a_bot_sending_a_family_command_is_ignored(h):
    bot = User(9, "SomeBot", None, "en", is_bot=True)
    await h.say(bot, "/family")
    assert h.tg.calls == [] and h.people() == []


# --- stale presses (the button was valid once) ----------------------------------------------------------------------------------------------------


async def test_pressing_accept_twice_acknowledges_both_but_acts_once(h):
    ref = await awaiting_guardian(h)
    await h.tap(QUENTIN, callback_data("acc", ref))
    sent = len(h.tg.delivered)
    await h.tap(QUENTIN, callback_data("acc", ref))
    assert len(h.tg.delivered) == sent  # no second confirmation prompt for Zebrina
    assert h.answers()[-1]["text"] == STALE_HI
    assert h.statuses() == ["awaiting_protected"]


async def test_pressing_confirm_twice_acts_once(h):
    await awaiting_protected(h)
    yes = h.data_of(ZEBRINA, "yes")
    await h.tap(ZEBRINA, yes)
    sent = len(h.tg.delivered)
    await h.tap(ZEBRINA, yes)
    assert len(h.tg.delivered) == sent and h.answers()[-1]["text"] == STALE_EN and h.statuses() == ["active"]


async def test_the_old_confirm_button_is_dead_after_the_link_was_removed(h):
    await awaiting_protected(h)
    yes = h.data_of(ZEBRINA, "yes")
    ref = h.only_link().ref
    await h.say(QUENTIN, "/leave")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "lv"))
    assert h.statuses() == ["revoked"]
    await h.tap(ZEBRINA, yes)
    assert h.statuses() == ["revoked"] and h.answers()[-1]["text"] == STALE_EN


async def test_an_expired_consent_step_cannot_be_pressed(h):
    ref = await awaiting_guardian(h)
    h.clock.advance(hours=25)
    await h.tap(QUENTIN, callback_data("acc", ref))
    assert h.statuses() == ["expired"] and h.answers()[-1]["text"] == STALE_HI


async def test_stale_presses_remove_the_dead_buttons(h):
    await awaiting_protected(h)
    yes = h.data_of(ZEBRINA, "yes")
    await h.tap(ZEBRINA, yes)
    await h.tap(ZEBRINA, yes, message_id=555)
    assert h.removed_buttons()[-1]["message_id"] == 555


async def test_popups_use_the_presser_s_language(h):
    await h.tap(QUENTIN, callback_data("acc", tokens.new_ref()))
    assert h.answers()[-1]["text"] == STALE_HI
    await h.tap(ANOUK, callback_data("acc", tokens.new_ref()))
    assert h.answers()[-1]["text"] == STALE_EN


# --- concurrency -------------------------------------------------------------------------------------------------------------------------------------


async def test_two_simultaneous_accepts_act_once(h):
    ref = await awaiting_guardian(h)
    await asyncio.gather(
        h.tap(QUENTIN, callback_data("acc", ref)),
        h.tap(QUENTIN, callback_data("acc", ref)),
    )
    prompts = [p for p in h.sent_to(ZEBRINA) if "accepted your invitation" in p["text"]]
    assert len(prompts) == 1 and h.statuses() == ["awaiting_protected"]
    assert sum(1 for a in h.answers()[-2:] if a.get("text") == STALE_HI) == 1  # exactly one of the two was told "stale"


async def test_two_helpers_racing_for_one_link_have_one_winner(h):
    payload = await h.invite(ZEBRINA)
    await asyncio.gather(h.say(QUENTIN, f"/start {payload}"), h.say(ANOUK, f"/start {payload}"))
    opened = [p for u in (QUENTIN, ANOUK) for p in h.sent_to(u) if p["text"].startswith(("“Zebrina”", "“Zebrina"))]
    assert len(opened) == 1 and h.statuses() == ["awaiting_guardian"]


# --- failures: never raise, never silent, never the wrong message -------------------------------------------------------------------------------------------


async def test_a_failing_command_gets_the_family_error_not_the_scam_check_error(h, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("database exploded with a secret detail")

    monkeypatch.setattr(h.handler._family, "list_helpers", boom)
    await h.say(QUENTIN, "/family")  # must not raise
    assert h.texts_to(QUENTIN) == [family_error_message("hi")]
    assert error_message("hi") not in h.texts_to(QUENTIN)
    assert "secret detail" not in " ".join(h.texts_to(QUENTIN))


async def test_a_failing_button_press_is_still_acknowledged_once_and_gets_the_family_error(h, monkeypatch):
    ref = await awaiting_guardian(h)

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(h.handler._family, "guardian_respond", boom)
    await h.tap(QUENTIN, callback_data("acc", ref))
    assert h.last_to(QUENTIN)["text"] == family_error_message("hi")
    assert len(h.answers()) == 3  # add, addy and this press: one acknowledgement each
    assert h.statuses() == ["awaiting_guardian"]


async def test_a_failing_press_from_a_forged_context_sends_nothing_back(h, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(h.handler._family, "guardian_respond", boom)
    before = len(h.tg.delivered)
    await h.tap(QUENTIN, callback_data("acc", tokens.new_ref()), chat_type="group", chat_id=-5)
    assert len(h.tg.delivered) == before  # a forged context never earns a message


async def test_the_database_going_away_does_not_break_normal_scam_checks(h, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(h.handler._family, "list_helpers", boom)
    h.analyzer.result = make_result("Please share the OTP")
    await h.say(ZEBRINA, "Please share the OTP you received")
    assert len(h.analyzer.calls) == 1 and "SCAM" in h.last_to(ZEBRINA)["text"]


# --- Phase 2 behaviour is untouched ------------------------------------------------------------------------------------------------------------------------------


async def test_with_family_mode_off_the_family_commands_are_just_unknown_commands(tmp_path):
    harness = Harness(tmp_path, with_family=False)
    try:
        for command in ("/family", "/leave", "/deleteme"):
            await harness.say(ZEBRINA, command)
        assert harness.texts_to(ZEBRINA) == [help_message("en")] * 3  # exactly Phase 2's answer
        await harness.say(ZEBRINA, f"/start fam_{'a' * 22}")
        assert harness.last_to(ZEBRINA)["text"] == start_message("en")
        assert harness.people() == [] and harness.statuses() == []
    finally:
        harness.close()


async def test_with_family_mode_off_buttons_and_block_events_are_ignored(tmp_path):
    harness = Harness(tmp_path, with_family=False)
    try:
        await harness.tap(ZEBRINA, "fam:add")
        await harness.member(ZEBRINA, "kicked")
        assert harness.tg.calls == []  # not even an acknowledgement: this build never asked for these updates
    finally:
        harness.close()


async def test_with_family_mode_on_ordinary_messages_are_unchanged(h):
    h.analyzer.result = make_result("Please share the OTP")
    await h.say(ZEBRINA, "Please share the OTP you received")
    assert h.analyzer.calls[0]["text"] == "Please share the OTP you received"
    await h.say(ZEBRINA, "/start")
    await h.say(ZEBRINA, "/help")
    await h.say(ZEBRINA, "/somethingelse")
    texts = h.texts_to(ZEBRINA)
    # With Family Mode on, /help also mentions /family (an unknown command shows the same help).
    assert texts[1:] == [start_message("en"), help_message("en", family=True), help_message("en", family=True)]
    assert h.people() == []  # ordinary use never creates a family record


async def test_ordinary_use_stores_nothing_at_all(h):
    await h.say(ZEBRINA, "Hello, lunch at one?")
    await h.say(ZEBRINA, "/help")
    await h.say(ZEBRINA, "/start")
    assert h.people() == [] and h.statuses() == [] and h.alerts() == []


async def test_duplicate_updates_are_dropped_before_family_handling(h):
    from tests.family_telegram_helpers import message_update
    from tests.telegram_fakes import parse

    update = parse(message_update(ZEBRINA, "/family", update_id=4242, date=h.now_unix()))
    assert h.service.accept(update) is True
    assert h.service.accept(update) is False


# --- privacy of logs ----------------------------------------------------------------------------------------------------------------------------------------------


async def test_nothing_sensitive_reaches_the_logs_through_the_whole_family_lifecycle(tmp_path, caplog):
    harness = Harness(tmp_path)
    p = User(7771001, "Zebrina", "zebrina_priv", "en")
    g = User(7772001, "Quentin", "quentin_priv", "hi")
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="app")
    try:
        payload = await harness.invite(p)
        token = payload.removeprefix("fam_")
        await harness.say(g, f"/start {payload}")
        ref = harness.only_link().ref
        await harness.tap(g, harness.data_of(g, "acc"))
        await harness.tap(p, harness.data_of(p, "yes"))
        await harness.say(p, "/family")
        await harness.say(g, "/leave")
        await harness.tap(g, harness.data_of(g, "lv"))
        await harness.say(g, f"/start {payload}")  # a used-up link
        await harness.tap(p, "fam:add")
        await harness.tap(p, "fam:bogus")
        await harness.member(g, "kicked")
        await harness.say(p, "/deleteme")
        await harness.tap(p, "fam:delc")
        await harness.say(g, "/deleteme")
        await harness.tap(g, "fam:delc")
    finally:
        harness.close()

    log = caplog.text
    assert "family:" in log, "the handler should log its outcome labels"
    for secret in (token, payload, ref, "Zebrina", "Quentin", "zebrina_priv", "quentin_priv", "7771001", "7772001",
                   "fam:acc", "fam:yes", "fam:delc", "start=fam", "t.me/", "ruko_dev_bot?start"):  # fmt: skip
        assert secret not in log, f"{secret!r} leaked into the logs"


async def test_failed_notifications_are_logged_by_class_only(tmp_path, caplog):
    harness = Harness(tmp_path)
    caplog.set_level(logging.DEBUG, logger="app")
    try:
        await harness.link_up(ZEBRINA, QUENTIN)
        harness.tg.blocked_chats.add(QUENTIN.id)
        await harness.say(ZEBRINA, "/family")
        await harness.tap(ZEBRINA, harness.data_of(ZEBRINA, "rm"))
    finally:
        harness.close()
    assert "TelegramAPIError" in caplog.text
    assert "Forbidden" not in caplog.text and "blocked by the user" not in caplog.text
    assert "Quentin" not in caplog.text and str(QUENTIN.id) not in caplog.text


# --- every message to a person is plain text with no accidental links ------------------------------------------------------------------------------------------------


async def test_no_family_message_other_than_the_invitation_contains_a_link(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(ZEBRINA, "/family")
    await h.say(QUENTIN, "/leave")
    await h.say(ZEBRINA, "/deleteme")
    for payload in h.tg.delivered:
        assert "parse_mode" not in payload
        if "t.me/" in payload["text"]:
            assert payload["text"].count("t.me/") == 1 and "Send the message below" in payload["text"]


async def test_the_invitation_is_the_only_message_that_carries_the_token(h):
    payload = await h.invite(ZEBRINA)
    token = payload.removeprefix("fam_")
    await h.say(QUENTIN, f"/start {payload}")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "acc"))
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "yes"))
    carriers = [p for p in h.tg.delivered if token in p["text"] or any(token in b["callback_data"] for b in h.buttons(p))]
    assert len(carriers) == 1 and "Send the message below" in carriers[0]["text"]
