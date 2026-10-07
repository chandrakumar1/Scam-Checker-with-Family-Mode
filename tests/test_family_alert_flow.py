"""Normal family alerts end to end: a checked message becomes (at most) one bounded
alert to the helpers, never blocking the person's reply. Real TelegramService,
FamilyAlertSender and FamilyService on a temporary SQLite file. No network."""

import asyncio
import logging

import pytest
from sqlalchemy import inspect as sa_inspect

from app.config import Settings
from app.family.types import AlertSummary, AlertVerdict, ScamCategory, SourceKind
from app.logging_config import configure_logging
from app.replies import templates as T
from app.replies.emergency_formatter import emergency_message, normal_alert_message, told_helpers_notice
from tests.family_helpers import all_alerts, all_deliveries, all_people
from tests.family_telegram_helpers import ANOUK, QUENTIN, ZEBRINA, Harness, User, message_update, result_for
from tests.telegram_fakes import TOKEN, WEBHOOK_SECRET, api_error, api_ok, photo_update
from tests.test_emergency_flow import Boom

pytestmark = pytest.mark.anyio

LABELS = ["kyc_update", "refund", "loan", "investment", "lottery_prize", "task_job", "other"]


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


async def check(h, user, text="Hello there", verdict="likely_scam", **kwargs):
    """Analyse `text` as the given verdict; returns the person's reply (the first message sent to them)."""
    h.analyzer.result = result_for(text, verdict, **kwargs)
    before = len(h.sent_to(user))
    await h.say(user, text)
    return h.sent_to(user)[before]


def normal_alerts(h):
    return [a for a in h.alerts() if a.alert_kind == "normal"]


def sent_alerts(h):
    return [a for a in normal_alerts(h) if a.suppressed_reason is None]


def summary(category=ScamCategory.OTHER, verdict=AlertVerdict.LIKELY_SCAM, source=SourceKind.TEXT):
    return AlertSummary(verdict, category, source)


# --- only a likely scam alerts anyone ----------------------------------------------------------------------------------------------


async def test_a_likely_scam_alerts_the_helper_with_a_bounded_message(h):
    await h.link_up(ZEBRINA, QUENTIN)
    reply = await check(h, ZEBRINA, "Hello there", "likely_scam", label="refund")
    assert h.sent_to(QUENTIN)[-1]["text"] == normal_alert_message("Zebrina", summary(ScamCategory.REFUND), "hi")
    assert reply["text"].count(told_helpers_notice(["Quentin"], "en")) == 1
    [alert] = normal_alerts(h)
    assert (alert.alert_kind, alert.verdict, alert.scam_type, alert.source_kind, alert.suppressed_reason) == (
        "normal", "likely_scam", "refund", "text", None,
    )  # fmt: skip
    assert [d.status for d in all_deliveries(h.engine)] == ["sent"]


@pytest.mark.parametrize("verdict", ["suspicious", "no_obvious_red_flags", None])
async def test_nothing_else_ever_produces_a_normal_alert(h, verdict):
    await h.link_up(ZEBRINA, QUENTIN)
    helper_before = len(h.sent_to(QUENTIN))
    spy_calls = []
    real = h.alert_sender.request_normal

    async def spy(*args, **kwargs):
        spy_calls.append(1)
        return await real(*args, **kwargs)

    h.alert_sender.request_normal = spy
    reply = await check(h, ZEBRINA, "Hello there", verdict)  # None = the LLM failed (a "suspicious" verdict)
    assert len(h.sent_to(QUENTIN)) == helper_before  # the helper heard nothing
    assert h.alerts() == [] and all_deliveries(h.engine) == []
    assert spy_calls == []  # the family side was not even asked
    assert told_helpers_notice(["Quentin"], "en") not in reply["text"]


async def test_a_suspicious_result_caused_by_a_high_severity_rule_still_never_alerts(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await check(h, ZEBRINA, "Click bit.ly/3xYz12 now", "no_obvious_red_flags")  # a HIGH rule: verdict "suspicious"
    assert h.alerts() == [] and h.sent_to(QUENTIN)[-1]["text"] != ""
    assert not any(p["text"].startswith("⚠️") for p in h.sent_to(QUENTIN))


async def test_a_critical_rule_alerts_even_when_the_llm_says_all_clear(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await check(h, ZEBRINA, "Please share the OTP you received", "no_obvious_red_flags")
    assert h.sent_to(QUENTIN)[-1]["text"] == normal_alert_message("Zebrina", summary(ScamCategory.OTP_FRAUD), "hi")


async def test_without_helpers_nothing_is_stored_or_added_to_the_reply(h):
    reply = await check(h, ANOUK, "Please share the OTP", "likely_scam")
    assert h.alerts() == [] and h.people() == []
    assert "family helper" not in reply["text"]


async def test_family_mode_off_means_no_alerts_but_the_emergency_button_stays(tmp_path):
    harness = Harness(tmp_path, with_family=False)
    try:
        reply = await check(harness, ZEBRINA, "Please share the OTP", "likely_scam")
        assert harness.alerts() == [] and harness.people() == []
        assert harness.has_emergency_button(reply) and "family helper" not in reply["text"]
    finally:
        harness.close()


# --- the reply to the person: first, and honest about who is told ---------------------------------------------------------------------------


async def test_the_persons_reply_is_sent_before_any_helper_is_contacted(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.send_log.clear()
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    order = h.tg.send_log
    reply_at = next(i for i, e in enumerate(order) if e.startswith(f"{ZEBRINA.id}:"))
    alert_at = next(i for i, e in enumerate(order) if e.startswith(f"{QUENTIN.id}:"))
    assert reply_at < alert_at


async def test_the_reply_names_every_helper_actually_being_told_and_keeps_its_button_and_footer(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.link_up(ZEBRINA, ANOUK)
    reply = await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert told_helpers_notice(["Quentin", "Anouk"], "en") in reply["text"]
    assert reply["text"].endswith(T.FOOTER["en"]) and h.has_emergency_button(reply)
    assert reply["text"].index("What to do:") < reply["text"].index("I am also telling")  # placed before the footer


async def test_the_notice_never_claims_a_helper_who_will_not_be_contacted(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.link_up(ZEBRINA, ANOUK)
    h.family.mark_blocked(ANOUK.id, True)  # known unreachable at planning time
    reply = await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert told_helpers_notice(["Quentin"], "en") in reply["text"] and "Anouk" not in reply["text"]
    assert "Anouk" in h.sent_to(ZEBRINA)[-1]["text"]  # but they ARE named in the "could not reach" follow-up


async def test_each_helper_gets_the_alert_in_their_own_language(h):
    await h.link_up(ZEBRINA, QUENTIN)  # Hindi
    await h.link_up(ZEBRINA, ANOUK)  # English
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert h.sent_to(QUENTIN)[-1]["text"] == normal_alert_message("Zebrina", summary(ScamCategory.OTP_FRAUD), "hi")
    assert h.sent_to(ANOUK)[-1]["text"] == normal_alert_message("Zebrina", summary(ScamCategory.OTP_FRAUD), "en")


async def test_a_hostile_display_name_is_cleaned_in_the_alert_and_the_notice(h):
    evil = User(3201, "Zebrina http://evil.example/pay ‮", "z_evil", "en")
    await h.link_up(evil, QUENTIN)
    await check(h, evil, "Please share the OTP", "likely_scam")
    assert "evil.example" not in h.sent_to(QUENTIN)[-1]["text"]
    assert h.sent_to(QUENTIN)[-1]["text"] == normal_alert_message("Zebrina", summary(ScamCategory.OTP_FRAUD), "hi")


# --- what the alert contains ----------------------------------------------------------------------------------------------------------------


async def test_no_message_content_reaches_a_helper_or_the_database(h):
    await h.link_up(ZEBRINA, QUENTIN)
    secret = "SENTINEL-ALERT-BODY-777123 http://evil.example/steal"
    await check(h, ZEBRINA, f"{secret} please share the OTP", "likely_scam", label=secret, explanation=secret)
    helper_text = " ".join(h.texts_to(QUENTIN))
    for forbidden in ("SENTINEL", "777123", "evil.example", "otp_request", "Do step one", "flag", "otp_fraud"):
        assert forbidden not in helper_text
    with h.engine.connect() as connection:  # every value of every alert table
        for table in ("family_alert", "family_alert_delivery"):
            for row in connection.exec_driver_sql(f"SELECT * FROM {table}").fetchall():  # noqa: S608 (fixed names)
                assert "SENTINEL" not in " ".join(str(value) for value in row)


@pytest.mark.parametrize(
    ("text", "label", "expected"),
    [
        ("Please share the OTP you received", "refund", ScamCategory.OTP_FRAUD),  # a critical rule wins over the label
        ("Hello there", "refund", ScamCategory.REFUND),
        ("Hello there", "something_unknown", ScamCategory.OTHER),
        ("Hello there", "http://evil.example", ScamCategory.OTHER),
    ],
)
async def test_the_category_phrase_comes_from_the_whitelist_only(h, text, label, expected):
    await h.link_up(ZEBRINA, QUENTIN)
    await check(h, ZEBRINA, text, "likely_scam", label=label)
    assert h.sent_to(QUENTIN)[-1]["text"] == normal_alert_message("Zebrina", summary(expected), "hi")
    assert sent_alerts(h)[-1].scam_type == expected.value


async def test_a_checked_image_is_recorded_as_an_image(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.analyzer.result = result_for("Hello there", "likely_scam", label="refund")
    h.tg.add_file("img", b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    await h.feed(photo_update("img", sizes=[(100, 100, 108)], chat_id=ZEBRINA.id, update_id=950))
    assert sent_alerts(h)[-1].source_kind == "image"


async def test_the_normal_alert_ends_with_the_1930_footer(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert h.sent_to(QUENTIN)[-1]["text"].endswith(T.FOOTER["hi"])


# --- policy through Telegram: hourly limit, category dedupe, redelivery ---------------------------------------------------------------------------------


async def test_at_most_five_normal_alerts_an_hour(h):
    await h.link_up(ZEBRINA, QUENTIN)
    helper_before = len(h.sent_to(QUENTIN))
    replies = []
    for label in LABELS[:6]:  # six different categories, so only the hourly limit can stop the sixth
        replies.append(await check(h, ZEBRINA, "Hello there", "likely_scam", label=label))
        h.clock.advance(minutes=1)
    assert len(h.sent_to(QUENTIN)) == helper_before + 5
    assert [told_helpers_notice(["Quentin"], "en") in r["text"] for r in replies] == [True] * 5 + [False]
    assert all("What to do:" in r["text"] for r in replies)  # every scam check was still answered in full
    assert [a.suppressed_reason for a in normal_alerts(h)] == [None] * 5 + ["throttled"]


async def test_the_hourly_limit_resets_after_an_hour(h):
    await h.link_up(ZEBRINA, QUENTIN)
    for label in LABELS[:5]:
        await check(h, ZEBRINA, "Hello there", "likely_scam", label=label)
    h.clock.advance(hours=1, seconds=1)
    helper_before = len(h.sent_to(QUENTIN))
    await check(h, ZEBRINA, "Hello there", "likely_scam", label=LABELS[5])
    assert len(h.sent_to(QUENTIN)) == helper_before + 1


async def test_the_same_category_within_thirty_minutes_alerts_once(h):
    await h.link_up(ZEBRINA, QUENTIN)
    first = await check(h, ZEBRINA, "Hello there", "likely_scam", label="refund")
    helper_after_first = len(h.sent_to(QUENTIN))
    h.clock.advance(minutes=29)
    second = await check(h, ZEBRINA, "Hello there", "likely_scam", label="refund")
    assert len(h.sent_to(QUENTIN)) == helper_after_first
    assert told_helpers_notice(["Quentin"], "en") in first["text"] and told_helpers_notice(["Quentin"], "en") not in second["text"]
    assert [a.suppressed_reason for a in normal_alerts(h)] == [None, "duplicate"]
    h.clock.advance(minutes=2)  # 31 minutes after the alert that was sent
    await check(h, ZEBRINA, "Hello there", "likely_scam", label="refund")
    assert len(h.sent_to(QUENTIN)) == helper_after_first + 1


async def test_a_different_category_is_not_a_duplicate(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await check(h, ZEBRINA, "Hello there", "likely_scam", label="refund")
    await check(h, ZEBRINA, "Hello there", "likely_scam", label="loan")
    assert len(sent_alerts(h)) == 2


async def test_a_redelivered_update_alerts_only_once_but_is_still_answered(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.analyzer.result = result_for("Hello there", "likely_scam", label="refund")
    payload = message_update(ZEBRINA, "Hello there", update_id=888_001, date=h.now_unix())
    helper_before = len(h.sent_to(QUENTIN))
    await h.feed(payload)
    await h.feed(payload)  # the very same update again (bypassing the in-memory cache, as after a restart)
    assert len(h.sent_to(QUENTIN)) == helper_before + 1
    assert [a.source_update_id for a in normal_alerts(h)] == [888_001]
    assert len([p for p in h.sent_to(ZEBRINA) if "What to do:" in p["text"]]) == 2


async def test_the_alert_and_its_pending_deliveries_exist_before_anything_is_sent(h):
    await h.link_up(ZEBRINA, QUENTIN)
    seen = []

    def inspect_database(payload):
        if payload["chat_id"] == QUENTIN.id:
            seen.append(([a.alert_kind for a in all_alerts(h.engine)], [d.status for d in all_deliveries(h.engine)]))

    h.tg.on_send = inspect_database
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert seen == [(["normal"], ["pending"])]  # at send time: the alert is written, the delivery still pending
    assert [d.status for d in all_deliveries(h.engine)] == ["sent"]  # and afterwards it is recorded as sent


# --- delivery: authorisation is re-checked at the last moment ---------------------------------------------------------------------------------------------


async def test_a_helper_removed_between_planning_and_sending_receives_nothing(h):
    await h.link_up(ZEBRINA, QUENTIN)
    ref_anouk = await h.link_up(ZEBRINA, ANOUK)
    real = h.alert_sender._family.authorize_delivery
    state = {"done": False}

    async def revoke_then_authorise(delivery_id):
        if not state["done"]:
            state["done"] = True
            h.family.revoke(ZEBRINA.id, ref_anouk)
        return await real(delivery_id)

    h.alert_sender._family.authorize_delivery = revoke_then_authorise
    anouk_before = len(h.sent_to(ANOUK))
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert len(h.sent_to(ANOUK)) == anouk_before  # revoked: no alert
    assert h.sent_to(QUENTIN)[-1]["text"].startswith("⚠️")
    assert sorted(d.status for d in all_deliveries(h.engine)) == ["sent", "skipped_revoked"]
    assert h.sent_to(ZEBRINA)[-1]["text"] == emergency_message("normal_alert_failed", "en", names=["Anouk"])


async def test_a_helper_who_blocks_the_bot_between_planning_and_sending_receives_nothing(h):
    await h.link_up(ZEBRINA, QUENTIN)
    real = h.alert_sender._family.authorize_delivery

    async def block_then_authorise(delivery_id):
        h.family.mark_blocked(QUENTIN.id, True)
        return await real(delivery_id)

    h.alert_sender._family.authorize_delivery = block_then_authorise
    before = len(h.sent_to(QUENTIN))
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert len(h.sent_to(QUENTIN)) == before
    assert [d.status for d in all_deliveries(h.engine)] == ["skipped_blocked"]


# --- delivery failures ------------------------------------------------------------------------------------------------------------------------------------


async def test_a_helper_who_blocked_the_bot_is_recorded_flagged_and_reported(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.link_up(ZEBRINA, ANOUK)
    h.tg.blocked_chats.add(ANOUK.id)
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert sorted((d.status, d.error_code) for d in all_deliveries(h.engine)) == [("failed", 403), ("sent", None)]
    assert next(p for p in all_people(h.engine) if p.display_name == "Anouk").bot_blocked is True
    follow_up = h.sent_to(ZEBRINA)[-1]
    assert follow_up["text"] == emergency_message("normal_alert_failed", "en", names=["Anouk"])
    assert follow_up["text"].endswith(T.FOOTER["en"])  # one follow-up, with the footer


async def test_a_transient_failure_is_recorded_but_does_not_mark_the_helper_blocked(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.error_chats[QUENTIN.id] = 500
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert [(d.status, d.error_code) for d in all_deliveries(h.engine)] == [("failed", 500)]
    assert next(p for p in all_people(h.engine) if p.display_name == "Quentin").bot_blocked is False
    assert h.sent_to(ZEBRINA)[-1]["text"] == emergency_message("normal_alert_failed", "en", names=["Quentin"])


async def test_nothing_is_sent_to_the_person_when_every_delivery_succeeds(h):
    await h.link_up(ZEBRINA, QUENTIN)
    before = len(h.sent_to(ZEBRINA))
    await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert len(h.sent_to(ZEBRINA)) == before + 1  # only the reply itself: no follow-up on success


async def test_when_no_helper_is_reachable_at_planning_time_the_person_is_still_told(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.family.mark_blocked(QUENTIN.id, True)
    attempts_before = len(h.tg.send_log)
    reply = await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert "family helper" not in reply["text"]  # nobody is being told, so the reply says nobody is
    assert h.sent_to(ZEBRINA)[-1]["text"] == emergency_message("normal_alert_failed", "en", names=["Quentin"])
    assert not any(e.startswith(f"{QUENTIN.id}:") for e in h.tg.send_log[attempts_before:])


# --- family problems must never block or spoil the scam-check reply -----------------------------------------------------------------------------------------


async def test_a_failing_family_side_does_not_stop_the_reply(h, caplog):
    await h.link_up(ZEBRINA, QUENTIN)
    h.alert_sender._family = Boom()
    caplog.set_level(logging.WARNING, logger="app")
    reply = await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert "SCAM" in reply["text"] and h.has_emergency_button(reply) and "family helper" not in reply["text"]
    assert not any("could not check" in t.lower() for t in h.texts_to(ZEBRINA))  # and no error message either
    assert "family alert planning failed: RuntimeError" in caplog.text and "database is down" not in caplog.text


async def test_a_slow_family_side_cannot_hold_up_the_reply(h):
    class Hanging:
        async def request_normal_alert(self, *args, **kwargs):
            await asyncio.sleep(30)

    h.alert_sender._family = Hanging()
    h.alert_sender._normal_timeout = 0.3
    h.analyzer.result = result_for("Please share the OTP", "likely_scam")
    await asyncio.wait_for(h.say(ZEBRINA, "Please share the OTP"), 3)
    assert "SCAM" in h.sent_to(ZEBRINA)[-1]["text"]


async def test_an_error_while_delivering_never_becomes_a_could_not_check_message(h, monkeypatch, caplog):
    await h.link_up(ZEBRINA, QUENTIN)

    async def explode(plan):
        raise RuntimeError("delivery exploded with a secret detail")

    monkeypatch.setattr(h.alert_sender, "deliver", explode)
    caplog.set_level(logging.WARNING, logger="app")
    before = len(h.sent_to(ZEBRINA))
    reply = await check(h, ZEBRINA, "Please share the OTP", "likely_scam")
    assert "SCAM" in reply["text"]
    assert len(h.sent_to(ZEBRINA)) == before + 1  # just the reply: no fallback error, no follow-up
    assert not any("could not check" in t.lower() for t in h.texts_to(ZEBRINA))
    assert "delivering a family alert failed: RuntimeError" in caplog.text and "secret detail" not in caplog.text


async def test_a_failing_follow_up_message_is_contained(h, caplog):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.blocked_chats.add(QUENTIN.id)
    caplog.set_level(logging.WARNING, logger="app")
    # sends to the person: the reply succeeds, then the follow-up fails with a 500
    h.tg.queue("sendMessage", api_ok({"message_id": 1}), api_error(500, "boom"))
    reply = await check(h, ZEBRINA, "Please share the OTP", "likely_scam")  # must not raise
    assert "SCAM" in reply["text"]
    assert not any("could not check" in t.lower() for t in h.texts_to(ZEBRINA))
    assert "delivering a family alert failed: TelegramAPIError" in caplog.text


async def test_if_the_reply_itself_fails_the_helpers_are_still_told(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.analyzer.result = result_for("Please share the OTP", "likely_scam")
    h.tg.queue("sendMessage", api_error(500, "the reply could not be sent"))  # the person's reply fails
    quentin_before = len(h.sent_to(QUENTIN))
    await h.say(ZEBRINA, "Please share the OTP")  # must not raise
    assert len(h.sent_to(QUENTIN)) == quentin_before + 1  # the planned alert was not abandoned
    assert [d.status for d in all_deliveries(h.engine)] == ["sent"]


async def test_an_ordinary_message_with_family_mode_on_behaves_like_phase_2(h):
    from app.replies.formatter import format_analysis

    h.analyzer.result = result_for("See you at lunch", "no_obvious_red_flags")
    await h.say(ANOUK, "See you at lunch")
    [reply] = h.sent_to(ANOUK)
    assert reply["text"] == format_analysis(h.analyzer.result, "en") and "reply_markup" not in reply


# --- privacy of logs ---------------------------------------------------------------------------------------------------------------------------------------------


async def test_the_alert_flow_logs_nothing_sensitive(tmp_path, caplog):
    harness = Harness(tmp_path)
    p = User(7781001, "Zebrina", "zebrina_log", "en")
    g1 = User(7782001, "Quentin", "quentin_log", "hi")
    g2 = User(7782002, "Anouk", "anouk_log", "en")
    secret = "SENTINEL-FLOW-LOG-5550123 http://evil.example/x"
    configure_logging(Settings(_env_file=None, telegram_bot_token=TOKEN, telegram_webhook_secret=WEBHOOK_SECRET))
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="app")
    try:
        await harness.link_up(p, g1)
        await harness.link_up(p, g2)
        harness.tg.blocked_chats.add(g2.id)  # one delivery fails
        await check(harness, p, f"{secret} please share the OTP", "likely_scam", label=secret, explanation=secret)
        harness.alert_sender._family = Boom()  # and then the family side breaks
        await check(harness, p, secret, "likely_scam", label="refund", explanation=secret)
    finally:
        harness.close()
    log = caplog.text
    assert "analyzed:likely_scam" in log  # the outcome labels are there
    for forbidden in ("SENTINEL", "evil.example", "Zebrina", "Quentin", "Anouk", "zebrina_log", "quentin_log", "anouk_log",
                      "7781001", "7782001", "7782002", "123456789:AAE", "em:o:", "fam:"):  # fmt: skip
        assert forbidden not in log, f"{forbidden!r} leaked into the logs"
    assert "alert delivery failed: TelegramAPIError status=403" in log


async def test_the_database_schema_still_holds_no_free_text(h):
    inspector = sa_inspect(h.engine)
    for table in ("family_alert", "family_alert_delivery"):
        for column in inspector.get_columns(table):
            assert "TEXT" not in str(column["type"]).upper() and "BLOB" not in str(column["type"]).upper()
