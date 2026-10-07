"""The Emergency flow end to end: Telegram updates in, messages out, through the
real TelegramService, EmergencyHandler, FamilyAlertSender and FamilyService
(temporary SQLite file). No network, no LLM."""

import asyncio
import logging
from datetime import timedelta

import pytest

from app.channels.telegram.emergency import parse_emergency_data
from app.family.types import AlertSummary, AlertVerdict, ScamCategory, SourceKind
from app.replies import emergency_templates as ET
from app.replies import templates as T
from app.replies.emergency_formatter import (
    emergency_alert_message,
    emergency_message,
    guidance_message,
    popup,
    question_message,
)
from app.replies.formatter import format_analysis, help_message
from tests.family_telegram_helpers import (
    ANOUK,
    MEERA,
    QUENTIN,
    ZEBRINA,
    Harness,
    User,
    callback_update,
    result_for,
)
from tests.telegram_fakes import MIB, FakeAnalyzer, FakeTelegram, api_error, other_update, photo_update

pytestmark = pytest.mark.anyio


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


@pytest.fixture
def h_off(tmp_path):
    harness = Harness(tmp_path, with_family=False)
    yield harness
    harness.close()


GUIDANCE = {lang: guidance_message(lang) for lang in ("en", "hi")}


async def scam_reply(h, user, text="Hello there", verdict="likely_scam", **kwargs):
    """Make the analyzer return the given verdict, send a message, return the reply that carries (or lacks) the button."""
    h.analyzer.result = result_for(text, verdict, **kwargs)
    before = len(h.sent_to(user))
    await h.say(user, text)
    return h.sent_to(user)[before]


async def ask(h, user):
    """/emergency -> the question. Returns the question message."""
    before = len(h.sent_to(user))
    await h.say(user, "/emergency")
    return h.sent_to(user)[before]


async def press_yes(h, user, **kwargs):
    """Press Yes on the latest question; returns the messages the person received because of it."""
    before = len(h.sent_to(user))
    await h.tap(user, h.em_data(user, "y"), **kwargs)
    return h.sent_to(user)[before:]


def texts(payloads):
    return [p["text"] for p in payloads]


def emergency_alerts(h):
    return [a for a in h.alerts() if a.alert_kind == "emergency"]


class Boom:
    """Stands in for the family service when the database is down: every call fails."""

    def __getattr__(self, name):
        async def fail(*args, **kwargs):
            raise RuntimeError("database is down")

        return fail


# --- the Emergency button on scam-check replies ----------------------------------------------------------------------------


async def test_the_button_is_on_a_likely_scam_reply(h):
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP you received", "likely_scam")
    assert h.has_emergency_button(reply)
    [button] = h.buttons(reply)
    assert button["text"] == ET.BUTTONS["emergency"]["en"]
    assert button["callback_data"].startswith("em:o:") and len(button["callback_data"].encode()) <= 64


async def test_the_button_is_on_a_suspicious_reply(h):
    assert h.has_emergency_button(await scam_reply(h, ZEBRINA, "Hello there", "suspicious"))


async def test_the_button_is_on_a_suspicious_reply_caused_by_an_llm_failure(h):
    reply = await scam_reply(h, ZEBRINA, "Hello there", None)  # the LLM failed, so the verdict is "suspicious"
    assert "suspicious" in reply["text"].lower() and h.has_emergency_button(reply)


async def test_the_button_is_not_on_a_no_obvious_red_flags_reply(h):
    reply = await scam_reply(h, ZEBRINA, "See you at lunch", "no_obvious_red_flags")
    assert not h.has_emergency_button(reply) and "reply_markup" not in reply


async def test_the_button_is_in_the_users_language(h):
    reply = await scam_reply(h, QUENTIN, "Please share the OTP", "likely_scam")
    assert h.buttons(reply)[0]["text"] == ET.BUTTONS["emergency"]["hi"]


async def test_the_buttons_token_is_issued_to_the_user_and_carries_the_bounded_summary(h):
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP you received", "suspicious")
    action, token = parse_emergency_data(h.buttons(reply)[0]["callback_data"])
    check = h.codec.verify(token, ZEBRINA.id, h.clock())
    assert action == "o" and check.summary == AlertSummary(AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, SourceKind.TEXT)
    assert h.codec.verify(token, ANOUK.id, h.clock()).status.value == "wrong_user"


async def test_an_image_check_is_marked_as_an_image_in_the_token(h):
    h.analyzer.result = result_for("Hello there", "suspicious")
    h.tg.add_file("img", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    await h.feed(photo_update("img", sizes=[(100, 100, 108)], chat_id=ZEBRINA.id, update_id=900))
    reply = h.sent_to(ZEBRINA)[-1]
    token = parse_emergency_data(h.buttons(reply)[0]["callback_data"])[1]
    assert h.codec.verify(token, ZEBRINA.id, h.clock()).summary.source_kind is SourceKind.IMAGE


@pytest.mark.parametrize("kind", ["too_large", "unreadable", "unsupported", "start", "help", "family", "leave", "deleteme", "unknown"])
async def test_the_button_is_not_on_rejections_unsupported_input_or_commands(h, kind):
    if kind == "too_large":
        await h.feed(photo_update("big", sizes=[(4000, 4000, 6 * MIB)], chat_id=ZEBRINA.id, update_id=901))
    elif kind == "unreadable":
        h.tg.add_file("bad", b"%PDF-1.4 not an image")
        await h.feed(photo_update("bad", sizes=[(10, 10, 30)], chat_id=ZEBRINA.id, update_id=902))
    elif kind == "unsupported":
        await h.feed(other_update(903, chat_id=ZEBRINA.id, voice={"file_id": "v"}))
    else:
        await h.say(ZEBRINA, {"start": "/start", "help": "/help", "family": "/family", "leave": "/leave",
                              "deleteme": "/deleteme", "unknown": "/whatever"}[kind])  # fmt: skip
    assert h.sent_to(ZEBRINA)  # something was answered...
    assert not any(h.has_emergency_button(p) for p in h.sent_to(ZEBRINA))  # ...and none of it carries the button


async def test_the_button_is_not_on_alert_messages_to_helpers(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await scam_reply(h, ZEBRINA, "Please share the OTP you received", "likely_scam")
    assert h.sent_to(QUENTIN)[-1]["text"].startswith("⚠️")  # the alert really went out
    assert not any(h.has_emergency_button(p) for p in h.sent_to(QUENTIN))


async def test_the_button_is_not_on_the_follow_up_or_outcome_messages(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.blocked_chats.add(QUENTIN.id)
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP you received", "likely_scam")
    follow_up = h.sent_to(ZEBRINA)[-1]
    assert follow_up is not reply and not h.has_emergency_button(follow_up)


async def test_without_an_emergency_handler_the_reply_is_exactly_phase_2():
    tg = FakeTelegram()
    from app.channels.telegram.service import TelegramService
    from tests.telegram_fakes import make_result, parse, text_update

    result = make_result("Please share the OTP", "likely_scam")
    service = TelegramService(tg.client(), analyze=FakeAnalyzer(result))
    await service.handle_update(parse(text_update("Please share the OTP")))
    [reply] = tg.sent_messages
    assert reply["text"] == format_analysis(result, "en") and "reply_markup" not in reply


# --- /emergency works at any time -------------------------------------------------------------------------------------------------


async def test_slash_emergency_asks_the_question_with_yes_and_no(h):
    question = await ask(h, ZEBRINA)
    assert question["text"] == question_message("en")
    assert question["text"].startswith("Did you already send money, OTP, PIN, password, or card details?")
    assert [(b["text"], b["callback_data"][:5]) for b in h.buttons(question)] == [("Yes", "em:y:"), ("No", "em:n:")]
    assert all(len(b["callback_data"].encode()) <= 64 for b in h.buttons(question))
    assert question["reply_parameters"]["message_id"] == 10
    assert h.analyzer.calls == [] and h.people() == [] and h.alerts() == []  # no analysis, nothing stored


async def test_the_question_in_hindi(h):
    question = await ask(h, QUENTIN)
    assert question["text"] == question_message("hi") and ET.QUESTION["hi"] in question["text"]
    assert [b["text"] for b in h.buttons(question)] == [ET.BUTTONS["yes"]["hi"], ET.BUTTONS["no"]["hi"]]


@pytest.mark.parametrize("text", ["/emergency", "/EMERGENCY", "/emergency@ruko_dev_bot", "/emergency help me"])
async def test_the_command_forms_all_work(h, text):
    await h.say(ZEBRINA, text)
    assert h.sent_to(ZEBRINA)[-1]["text"] == question_message("en")


async def test_it_works_with_family_mode_off(h_off):
    question = await ask(h_off, ZEBRINA)
    assert question["text"] == question_message("en")
    assert [m for m in texts(await press_yes(h_off, ZEBRINA))] == [
        GUIDANCE["en"], emergency_message("family_off", "en")
    ]


async def test_it_works_with_no_helpers_and_no_previous_check(h):
    await h.say(ZEBRINA, "/family")  # a person with an (empty) family record
    await ask(h, ZEBRINA)
    assert texts(await press_yes(h, ZEBRINA)) == [GUIDANCE["en"], emergency_message("no_helpers", "en")]


async def test_asking_the_question_needs_no_database_at_all(h):
    h.alert_sender._family = Boom()
    h.handler._family = Boom()
    assert (await ask(h, ZEBRINA))["text"] == question_message("en")


async def test_it_works_while_the_analysis_queue_is_completely_full(tmp_path):
    harness = Harness(tmp_path, max_concurrent=1)
    try:
        await harness.link_up(ZEBRINA, QUENTIN)
        started, release = asyncio.Event(), asyncio.Event()

        class Blocking(FakeAnalyzer):
            async def __call__(self, **kwargs):
                started.set()
                await release.wait()
                return await super().__call__(**kwargs)

        harness.service._analyze = Blocking()
        slow = asyncio.create_task(harness.say(ANOUK, "something to analyse"))
        await asyncio.wait_for(started.wait(), 2)

        # The queue really is full: an ordinary message cannot even start.
        stuck = asyncio.create_task(harness.say(MEERA, "another message"))
        await asyncio.sleep(0.3)
        assert not stuck.done() and not slow.done()

        # ...yet the whole emergency flow completes, promptly, around it.
        question = await asyncio.wait_for(ask(harness, ZEBRINA), 2)
        assert question["text"] == question_message("en")
        outcome = texts(await asyncio.wait_for(press_yes(harness, ZEBRINA), 5))
        assert outcome[0] == GUIDANCE["en"] and "I have told “Quentin”" in outcome[1]
        assert not slow.done() and not stuck.done()  # still waiting: emergency did not queue behind them

        release.set()
        await asyncio.gather(slow, stuck)
    finally:
        harness.close()


async def test_emergency_never_takes_a_slot_from_the_analysis_queue(h):
    free_before = h.service._semaphore._value
    await ask(h, ZEBRINA)
    await press_yes(h, ZEBRINA)
    assert h.service._semaphore._value == free_before == 5
    assert h.service._emergency_semaphore._value == 10


async def test_groups_and_bots_cannot_start_it(h):
    await h.say(ZEBRINA, "/emergency", chat_type="supergroup", chat_id=-100200)
    await h.say(User(9, "SomeBot", None, "en", is_bot=True), "/emergency")
    assert h.tg.calls == []


async def test_without_any_emergency_handler_it_is_just_an_unknown_command(tmp_path):
    harness = Harness(tmp_path, with_family=False, with_emergency=False)
    try:
        await harness.say(ZEBRINA, "/emergency")
        assert texts(harness.sent_to(ZEBRINA)) == [help_message("en")]
        assert "/emergency" in help_message("en")  # and /help still points people at it
    finally:
        harness.close()


# --- opening the question from a reply, and answering No ----------------------------------------------------------------------------


async def test_pressing_the_reply_button_asks_the_question_and_changes_nothing(h):
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP you received", "likely_scam")
    await h.tap(ZEBRINA, h.buttons(reply)[0]["callback_data"], message_id=610)
    question = h.sent_to(ZEBRINA)[-1]
    assert question["text"] == question_message("en")
    assert [b["callback_data"][:5] for b in h.buttons(question)] == ["em:y:", "em:n:"]
    assert h.removed_buttons() == []  # the reply keeps its button: nothing has been decided
    assert len(h.answers()) == 1 and h.alerts() == [] and h.people() == []
    # the question's buttons carry the same bounded summary, in fresh tokens
    token = parse_emergency_data(h.buttons(question)[0]["callback_data"])[1]
    assert h.codec.verify(token, ZEBRINA.id, h.clock()).summary.scam_type is ScamCategory.OTP_FRAUD


async def test_no_removes_the_buttons_and_sends_the_calm_response_only(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    before_helper, acks_before = len(h.sent_to(QUENTIN)), len(h.answers())
    await h.tap(ZEBRINA, h.em_data(ZEBRINA, "n"), message_id=611)
    assert h.removed_buttons()[-1] == {"chat_id": ZEBRINA.id, "message_id": 611, "reply_markup": {"inline_keyboard": []}}
    assert h.sent_to(ZEBRINA)[-1]["text"] == emergency_message("no_response", "en")
    assert GUIDANCE["en"] not in texts(h.sent_to(ZEBRINA))  # no alarm for a No
    assert emergency_alerts(h) == [] and h.alerts() == []  # no family alert at all
    assert len(h.sent_to(QUENTIN)) == before_helper  # the helper heard nothing
    assert len(h.answers()) == acks_before + 1  # exactly one acknowledgement for the press


async def test_pressing_no_again_is_safe_and_still_alerts_nobody(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    no = h.em_data(ZEBRINA, "n")
    before_helper, acks_before = len(h.sent_to(QUENTIN)), len(h.answers())
    await h.tap(ZEBRINA, no)
    await h.tap(ZEBRINA, no)
    assert len(h.answers()) == acks_before + 2  # each press acknowledged once
    assert h.alerts() == [] and len(h.sent_to(QUENTIN)) == before_helper


async def test_pressing_the_reply_button_twice_just_asks_twice(h):
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP", "likely_scam")
    data = h.buttons(reply)[0]["callback_data"]
    await h.tap(ZEBRINA, data)
    await h.tap(ZEBRINA, data)
    assert texts(h.sent_to(ZEBRINA))[-2:] == [question_message("en")] * 2 and h.alerts() == []


# --- answering Yes: the 1930 steps come first --------------------------------------------------------------------------------------------


async def test_yes_sends_the_guidance_then_alerts_the_helper_then_reports(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    h.tg.send_log.clear()
    update_id = h._update_id + 1
    mine = await press_yes(h, ZEBRINA, message_id=612)

    assert texts(mine) == [GUIDANCE["en"], emergency_message("told_all", "en", names=["Quentin"])]
    assert mine[1]["text"].endswith(T.FOOTER["en"]) and not mine[0]["text"].endswith(T.FOOTER["en"])
    order = h.tg.send_log  # one entry per send attempt, in order
    guidance_at = next(i for i, e in enumerate(order) if e.startswith(f"{ZEBRINA.id}:🆘 ACT NOW"))
    alert_at = next(i for i, e in enumerate(order) if e.startswith(f"{QUENTIN.id}:🆘 आपातकाल"))
    report_at = next(i for i, e in enumerate(order) if e.startswith(f"{ZEBRINA.id}:I have told"))
    assert guidance_at < alert_at < report_at

    assert h.removed_buttons()[-1]["message_id"] == 612
    [alert] = emergency_alerts(h)
    assert (alert.alert_kind, alert.verdict, alert.scam_type, alert.source_kind) == ("emergency", None, None, None)
    assert alert.source_update_id == update_id
    assert [d.status for d in __import__("tests.family_helpers", fromlist=["x"]).all_deliveries(h.engine)] == ["sent"]


async def test_the_guidance_is_sent_before_any_database_or_family_call(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    events = []
    h.tg.on_send = lambda p: events.append(("send", p["chat_id"], p["text"][:10]))
    real = h.alert_sender._family.request_emergency_alert

    async def spy(*args, **kwargs):
        events.append(("database",))
        return await real(*args, **kwargs)

    h.alert_sender._family.request_emergency_alert = spy
    await press_yes(h, ZEBRINA)
    first_database_call = events.index(("database",))
    guidance_sends = [i for i, e in enumerate(events) if e[0] == "send" and e[1] == ZEBRINA.id and e[2].startswith("🆘 ACT NOW")]
    assert guidance_sends and guidance_sends[0] < first_database_call


async def test_the_guidance_arrives_even_though_the_database_is_down(h):
    await ask(h, ZEBRINA)
    h.alert_sender._family = Boom()
    mine = await press_yes(h, ZEBRINA)
    assert texts(mine) == [GUIDANCE["en"], emergency_message("error", "en")]


async def test_the_guidance_arrives_immediately_even_if_the_database_hangs(h):
    await ask(h, ZEBRINA)

    class Hanging:
        async def request_emergency_alert(self, *args, **kwargs):
            await asyncio.sleep(30)

    h.alert_sender._family = Hanging()
    h.alert_sender._emergency_timeout = 0.3
    guidance_sent_at = []
    h.tg.on_send = lambda p: guidance_sent_at.append(asyncio.get_running_loop().time()) if p["text"].startswith("🆘 ACT NOW") else None
    started = asyncio.get_running_loop().time()
    mine = await asyncio.wait_for(press_yes(h, ZEBRINA), 3)
    assert texts(mine) == [GUIDANCE["en"], emergency_message("error", "en")]
    assert guidance_sent_at and guidance_sent_at[0] - started < 0.2  # not delayed by the hung database
    assert asyncio.get_running_loop().time() - started < 2


async def test_a_rejected_first_guidance_message_is_retried_once(h, caplog):
    await ask(h, ZEBRINA)
    caplog.set_level(logging.DEBUG, logger="app")
    h.tg.queue("sendMessage", api_error(500, "Internal Server Error with SECRET-DETAIL"))  # the first send fails
    mine = await press_yes(h, ZEBRINA)
    assert texts(mine)[0] == GUIDANCE["en"] and texts(mine).count(GUIDANCE["en"]) == 1  # delivered, once
    assert sum(1 for e in h.tg.send_log if f"{ZEBRINA.id}:🆘 ACT NOW" in e) == 2  # attempted twice
    assert "emergency guidance send failed (attempt 1): TelegramAPIError status=500" in caplog.text
    assert "SECRET-DETAIL" not in caplog.text and "Internal Server Error" not in caplog.text


async def test_if_the_guidance_cannot_be_sent_at_all_the_rest_still_happens(h, caplog):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    caplog.set_level(logging.WARNING, logger="app")
    h.tg.queue("sendMessage", api_error(500, "boom"), api_error(502, "boom again"))
    mine = await press_yes(h, ZEBRINA)
    assert GUIDANCE["en"] not in texts(mine)
    assert texts(mine) == [emergency_message("told_all", "en", names=["Quentin"])]  # the alert and report went ahead
    assert h.sent_to(QUENTIN)[-1]["text"].startswith("🆘")
    assert "attempt 1" in caplog.text and "attempt 2" in caplog.text and "status=502" in caplog.text


async def test_yes_in_hindi_gets_the_hindi_guidance_and_outcome(h):
    await h.link_up(ANOUK, QUENTIN)
    await ask(h, QUENTIN)  # Quentin (Hindi) has nobody to alert
    assert texts(await press_yes(h, QUENTIN)) == [GUIDANCE["hi"], emergency_message("no_helpers", "hi")]


# --- every outcome tells the person what happened -----------------------------------------------------------------------------------


async def test_all_helpers_reached(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.link_up(ZEBRINA, ANOUK)
    await ask(h, ZEBRINA)
    mine = texts(await press_yes(h, ZEBRINA))
    assert mine == [GUIDANCE["en"], emergency_message("told_all", "en", names=["Quentin", "Anouk"])]
    assert h.sent_to(QUENTIN)[-1]["text"] == emergency_alert_message("Zebrina", None, "hi")
    assert h.sent_to(ANOUK)[-1]["text"] == emergency_alert_message("Zebrina", None, "en")


async def test_some_helpers_reached(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.link_up(ZEBRINA, ANOUK)
    h.tg.blocked_chats.add(ANOUK.id)
    await ask(h, ZEBRINA)
    mine = texts(await press_yes(h, ZEBRINA))
    assert mine == [GUIDANCE["en"], emergency_message("told_some", "en", reached=["Quentin"], failed=["Anouk"])]
    from tests.family_helpers import all_deliveries

    assert sorted((d.status, d.error_code) for d in all_deliveries(h.engine)) == [("failed", 403), ("sent", None)]
    assert next(p for p in h.people() if p.display_name == "Anouk").bot_blocked is True  # a 403 means they blocked the bot


async def test_no_helper_could_be_reached_at_delivery_time(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.blocked_chats.add(QUENTIN.id)
    await ask(h, ZEBRINA)
    mine = texts(await press_yes(h, ZEBRINA))
    assert mine == [GUIDANCE["en"], emergency_message("told_none", "en")]
    assert "call someone you trust now" in mine[1].lower()


async def test_no_helper_was_reachable_even_at_planning_time(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.family.mark_blocked(QUENTIN.id, True)  # known to have blocked the bot already
    await ask(h, ZEBRINA)
    attempts_before = len(h.tg.send_log)
    mine = texts(await press_yes(h, ZEBRINA))
    assert mine == [GUIDANCE["en"], emergency_message("told_none", "en")]
    assert not any(e.startswith(f"{QUENTIN.id}:") for e in h.tg.send_log[attempts_before:])  # not even attempted


async def test_no_helper_linked(h):
    await h.say(ZEBRINA, "/family")
    await ask(h, ZEBRINA)
    mine = texts(await press_yes(h, ZEBRINA))
    assert mine == [GUIDANCE["en"], emergency_message("no_helpers", "en")]
    assert "Call someone you trust now" in mine[1] and "/family" in mine[1]
    assert emergency_alerts(h) == []


async def test_a_person_unknown_to_family_mode_gets_the_same_and_leaves_no_trace(h):
    await ask(h, ANOUK)
    assert texts(await press_yes(h, ANOUK)) == [GUIDANCE["en"], emergency_message("no_helpers", "en")]
    assert h.people() == [] and h.alerts() == []


async def test_family_mode_off(h_off):
    await ask(h_off, ZEBRINA)
    mine = texts(await press_yes(h_off, ZEBRINA))
    assert mine == [GUIDANCE["en"], emergency_message("family_off", "en")]
    assert "/family" not in mine[1] and "call someone you trust now" in mine[1].lower()
    assert h_off.people() == [] and h_off.alerts() == []


async def test_a_database_failure(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    h.alert_sender._family = Boom()
    mine = await press_yes(h, ZEBRINA)
    assert texts(mine) == [GUIDANCE["en"], emergency_message("error", "en")]
    assert h.sent_to(QUENTIN)[-1]["text"] != emergency_alert_message("Zebrina", None, "hi")  # no alert went out


async def test_the_database_taking_too_long_is_a_failure_not_a_hang(h):
    class Slow:
        async def request_emergency_alert(self, *args, **kwargs):
            await asyncio.sleep(10)

    await ask(h, ZEBRINA)
    h.alert_sender._family = Slow()
    h.alert_sender._emergency_timeout = 0.2
    mine = await asyncio.wait_for(press_yes(h, ZEBRINA), 3)
    assert texts(mine) == [GUIDANCE["en"], emergency_message("error", "en")]


async def test_an_unexpected_failure_while_delivering(h, monkeypatch, caplog):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)

    async def explode(plan):
        raise RuntimeError("something nobody expected, with a secret detail")

    monkeypatch.setattr(h.alert_sender, "deliver", explode)
    caplog.set_level(logging.WARNING, logger="app")
    mine = await press_yes(h, ZEBRINA)
    assert texts(mine) == [GUIDANCE["en"], emergency_message("error", "en")]
    assert "RuntimeError" in caplog.text and "secret detail" not in caplog.text


async def test_if_the_handler_itself_breaks_the_guidance_still_goes_out(h, monkeypatch):
    await ask(h, ZEBRINA)

    def broken(*args, **kwargs):
        raise RuntimeError("verification exploded")

    monkeypatch.setattr(h.emergency._codec, "verify", broken)
    before = len(h.sent_to(ZEBRINA))
    await h.tap(ZEBRINA, h.em_data(ZEBRINA, "y"))
    assert texts(h.sent_to(ZEBRINA)[before:]) == [GUIDANCE["en"]]  # the last-resort path
    assert len(h.answers()) == 1  # and the press was still acknowledged, once


async def test_if_the_command_breaks_the_guidance_still_goes_out(h, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("keyboard exploded")

    monkeypatch.setattr(h.emergency, "_question_keyboard", broken)
    await h.say(ZEBRINA, "/emergency")
    assert texts(h.sent_to(ZEBRINA)) == [GUIDANCE["en"]]


async def test_every_outcome_message_after_the_guidance_carries_the_footer(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    mine = await press_yes(h, ZEBRINA)
    assert mine[1]["text"].endswith(T.FOOTER["en"])
    await h.say(ANOUK, "/emergency")
    assert (await press_yes(h, ANOUK))[1]["text"].endswith(T.FOOTER["en"])


# --- policy: bypass of the normal throttle, 2-minute dedupe, idempotency ----------------------------------------------------------------


NORMAL_LABELS = ["kyc_update", "refund", "loan", "investment", "lottery_prize", "task_job"]  # distinct categories


async def normal_scam(h, user, label):
    h.analyzer.result = result_for("Hello there", "likely_scam", label=label)
    await h.say(user, "Hello there")


async def test_an_emergency_goes_out_when_the_normal_hourly_limit_is_used_up(h):
    await h.link_up(ZEBRINA, QUENTIN)
    for label in NORMAL_LABELS[:5]:
        await normal_scam(h, ZEBRINA, label)
        h.clock.advance(minutes=1)
    helper_before = len(h.sent_to(QUENTIN))
    await normal_scam(h, ZEBRINA, NORMAL_LABELS[5])  # the sixth: throttled
    assert len(h.sent_to(QUENTIN)) == helper_before
    assert [a.suppressed_reason for a in h.alerts()].count("throttled") == 1

    await ask(h, ZEBRINA)
    mine = texts(await press_yes(h, ZEBRINA))
    assert mine[1] == emergency_message("told_all", "en", names=["Quentin"])
    assert h.sent_to(QUENTIN)[-1]["text"] == emergency_alert_message("Zebrina", None, "hi")


async def test_emergencies_do_not_use_up_the_normal_hourly_limit(h):
    await h.link_up(ZEBRINA, QUENTIN)
    for label in NORMAL_LABELS[:4]:
        await normal_scam(h, ZEBRINA, label)
    for _ in range(3):  # three emergencies, spaced out so none is deduplicated
        await ask(h, ZEBRINA)
        await press_yes(h, ZEBRINA)
        h.clock.advance(minutes=3)
    assert len(emergency_alerts(h)) == 3
    helper_before = len(h.sent_to(QUENTIN))
    await normal_scam(h, ZEBRINA, NORMAL_LABELS[4])  # the fifth normal alert: still allowed
    assert len(h.sent_to(QUENTIN)) == helper_before + 1
    await normal_scam(h, ZEBRINA, NORMAL_LABELS[5])  # the sixth: now throttled
    assert len(h.sent_to(QUENTIN)) == helper_before + 1


async def test_an_emergency_ignores_the_normal_thirty_minute_category_dedupe(h):
    await h.link_up(ZEBRINA, QUENTIN)
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP you received", "likely_scam")  # normal OTP alert
    assert h.sent_to(QUENTIN)[-1]["text"].startswith("⚠️")
    await h.tap(ZEBRINA, h.buttons(reply)[0]["callback_data"])
    await press_yes(h, ZEBRINA)  # the same category, moments later
    assert h.sent_to(QUENTIN)[-1]["text"].startswith("🆘")


async def test_the_alert_from_a_checked_message_carries_only_its_category(h):
    await h.link_up(ZEBRINA, QUENTIN)
    reply = await scam_reply(h, ZEBRINA, "Please share the OTP you received", "likely_scam")
    await h.tap(ZEBRINA, h.buttons(reply)[0]["callback_data"])
    await press_yes(h, ZEBRINA)
    summary = AlertSummary(AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, SourceKind.TEXT)
    assert h.sent_to(QUENTIN)[-1]["text"] == emergency_alert_message("Zebrina", summary, "hi")
    [alert] = emergency_alerts(h)
    assert (alert.verdict, alert.scam_type, alert.source_kind) == ("likely_scam", "otp_fraud", "text")


async def test_an_emergency_from_a_suspicious_reply_is_allowed_and_recorded_as_suspicious(h):
    await h.link_up(ZEBRINA, QUENTIN)
    reply = await scam_reply(h, ZEBRINA, "Hello there", "suspicious", label="refund")
    await h.tap(ZEBRINA, h.buttons(reply)[0]["callback_data"])
    await press_yes(h, ZEBRINA)
    [alert] = emergency_alerts(h)
    assert (alert.verdict, alert.scam_type) == ("suspicious", "refund")
    assert h.alerts()[-1].alert_kind == "emergency" and len([a for a in h.alerts() if a.alert_kind == "normal"]) == 0


async def test_repeated_yes_within_two_minutes_alerts_once(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)  # press_yes presses the same Yes button both times
    first = texts(await press_yes(h, ZEBRINA))
    helper_after_first = len(h.sent_to(QUENTIN))
    h.clock.advance(seconds=30)
    again = texts(await press_yes(h, ZEBRINA))
    assert first[1].startswith("I have told") and again == [GUIDANCE["en"], emergency_message("deduped", "en")]
    assert len(h.sent_to(QUENTIN)) == helper_after_first  # the helper was not alerted again
    assert len(emergency_alerts(h)) == 2 and [a.suppressed_reason for a in emergency_alerts(h)] == [None, "duplicate"]


@pytest.mark.parametrize(("gap_seconds", "expect_new_alert"), [(119, False), (121, True)])
async def test_the_two_minute_boundary(h, gap_seconds, expect_new_alert):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    await press_yes(h, ZEBRINA)
    helper_after_first = len(h.sent_to(QUENTIN))
    h.clock.advance(seconds=gap_seconds)
    mine = texts(await press_yes(h, ZEBRINA))
    assert (len(h.sent_to(QUENTIN)) == helper_after_first + 1) is expect_new_alert
    assert (mine[1].startswith("I have told")) is expect_new_alert


async def test_simultaneous_yes_presses_alert_exactly_once(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    yes = h.em_data(ZEBRINA, "y")
    helper_before = len(h.sent_to(QUENTIN))
    await asyncio.gather(*[h.tap(ZEBRINA, yes) for _ in range(6)])
    assert len(h.sent_to(QUENTIN)) == helper_before + 1  # one alert, whatever the interleaving
    real = [a for a in emergency_alerts(h) if a.suppressed_reason is None]
    assert len(real) == 1 and len(emergency_alerts(h)) == 6
    mine = texts(h.sent_to(ZEBRINA))
    assert mine.count(GUIDANCE["en"]) == 6  # every press gets the 1930 steps
    assert sum(1 for t in mine if t.startswith("I have told")) == 1
    assert sum(1 for t in mine if t == emergency_message("deduped", "en")) == 5
    assert len(h.answers()) >= 6


async def test_a_failed_delivery_still_counts_for_the_two_minute_dedupe(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.blocked_chats.add(QUENTIN.id)
    await ask(h, ZEBRINA)
    first = texts(await press_yes(h, ZEBRINA))
    refused_after_first = len(h.tg.refused)
    h.clock.advance(seconds=10)
    again = texts(await press_yes(h, ZEBRINA))
    assert first[1] == emergency_message("told_none", "en")
    assert again[1] == emergency_message("deduped", "en")  # no second attempt, as approved
    assert len(h.tg.refused) == refused_after_first


async def test_a_redelivered_update_is_idempotent(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    payload = callback_update(
        ZEBRINA, h.em_data(ZEBRINA, "y"), update_id=777_001, message_date=h.now_unix(), message_id=620, query_id="redelivered"
    )
    helper_before = len(h.sent_to(QUENTIN))
    await h.feed(payload)
    await h.feed(payload)  # Telegram delivers the very same update again (even bypassing the in-memory cache)
    assert len(h.sent_to(QUENTIN)) == helper_before + 1
    assert [a.source_update_id for a in emergency_alerts(h)] == [777_001]  # one alert, keyed by the update id


async def test_a_helper_removed_between_planning_and_sending_receives_nothing(h):
    await h.link_up(ZEBRINA, QUENTIN)
    ref_anouk = await h.link_up(ZEBRINA, ANOUK)
    await ask(h, ZEBRINA)
    real = h.alert_sender._family.authorize_delivery
    state = {"done": False}

    async def revoke_then_authorise(delivery_id):
        if not state["done"]:
            state["done"] = True
            h.family.revoke(ZEBRINA.id, ref_anouk)  # Zebrina removes Anouk at the very last moment
        return await real(delivery_id)

    h.alert_sender._family.authorize_delivery = revoke_then_authorise
    anouk_before = len(h.sent_to(ANOUK))
    mine = texts(await press_yes(h, ZEBRINA))
    assert len(h.sent_to(ANOUK)) == anouk_before  # revoked, so nothing was sent
    assert h.sent_to(QUENTIN)[-1]["text"].startswith("🆘")
    assert mine[1] == emergency_message("told_some", "en", reached=["Quentin"], failed=["Anouk"])
    from tests.family_helpers import all_deliveries

    assert sorted(d.status for d in all_deliveries(h.engine)) == ["sent", "skipped_revoked"]


async def test_a_helper_who_blocks_the_bot_between_planning_and_sending_receives_nothing(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)
    real = h.alert_sender._family.authorize_delivery

    async def block_then_authorise(delivery_id):
        h.family.mark_blocked(QUENTIN.id, True)
        return await real(delivery_id)

    h.alert_sender._family.authorize_delivery = block_then_authorise
    quentin_before = len(h.sent_to(QUENTIN))
    mine = texts(await press_yes(h, ZEBRINA))
    assert len(h.sent_to(QUENTIN)) == quentin_before and mine[1] == emergency_message("told_none", "en")


async def test_a_failure_to_authorise_means_no_send(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await ask(h, ZEBRINA)

    async def broken(delivery_id):
        raise RuntimeError("cannot check")

    h.alert_sender._family.authorize_delivery = broken
    quentin_before = len(h.sent_to(QUENTIN))
    mine = texts(await press_yes(h, ZEBRINA))
    assert len(h.sent_to(QUENTIN)) == quentin_before  # fail closed
    assert mine[1] == emergency_message("told_none", "en")


async def test_a_hostile_display_name_is_cleaned_in_the_helpers_alert(h):
    evil = User(3101, "Zebrina http://evil.example/pay ‮​", "z_evil", "en")
    await h.link_up(evil, QUENTIN)
    await ask(h, evil)
    await press_yes(h, evil)
    alert_text = h.sent_to(QUENTIN)[-1]["text"]
    assert alert_text == emergency_alert_message("Zebrina", None, "hi")
    assert "evil.example" not in alert_text and "‮" not in alert_text


async def test_the_alert_contains_only_fixed_wording_and_nothing_from_the_message(h):
    await h.link_up(ZEBRINA, QUENTIN)
    secret = "SENTINEL-MESSAGE-BODY-424242 http://evil.example/steal"
    reply = await scam_reply(
        h, ZEBRINA, f"{secret} please share the OTP", "likely_scam", label="http://evil.example/steal", explanation=secret
    )
    await h.tap(ZEBRINA, h.buttons(reply)[0]["callback_data"])
    await press_yes(h, ZEBRINA)
    helper_text = " ".join(texts(h.sent_to(QUENTIN)))
    for forbidden in ("SENTINEL", "424242", "evil.example", "otp_request", "Do step one", "flag"):
        assert forbidden not in helper_text
    from tests.family_helpers import all_alerts

    for alert in all_alerts(h.engine):
        assert "SENTINEL" not in " ".join(str(v) for v in (alert.verdict, alert.scam_type, alert.source_kind))
