import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlmodel import select

from app.db.engine import make_engine, session_scope
from app.db.models import AlertKind, DeliveryStatus, FamilyAlert
from app.family.policy import FamilyPolicy
from app.family.service import FamilyService
from app.family.types import (
    AlertOutcome,
    AlertSummary,
    AlertVerdict,
    PersonRef,
    ScamCategory,
    SourceKind,
)
from tests.family_helpers import (
    ANITA,
    MEERA,
    PRIYA,
    RAHUL,
    START,
    Clock,
    all_alerts,
    all_deliveries,
    all_people,
    delivery_by_id,
    link_pair,
    make_service,
    open_invite_for,
    person,
    summary,
)

CATEGORIES = list(ScamCategory)  # 15 different categories, so normal alerts never trip the category dedupe


def run_concurrently(*calls):
    barrier = threading.Barrier(len(calls))

    def wrapped(call):
        barrier.wait()
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(wrapped, calls))


@pytest.fixture
def linked():
    """A service with Priya protected by Rahul."""
    svc, engine, clock = make_service()
    link_pair(svc, PRIYA, RAHUL)
    return svc, engine, clock


@pytest.fixture
def file_linked(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    svc, engine, clock = make_service(engine=engine)
    link_pair(svc, PRIYA, RAHUL)
    yield svc, engine, clock
    engine.dispose()


def normal(svc, update_id, category=ScamCategory.OTP_FRAUD):
    return svc.request_normal_alert(PRIYA, summary(category), update_id)


def emergency(svc, update_id, s=None):
    return svc.request_emergency_alert(PRIYA, s, update_id)


# --- two separate, explicit operations -------------------------------------------------------------------------------


def test_normal_alert_writes_a_normal_alert_with_the_summary_fields(linked):
    svc, engine, _ = linked
    plan = svc.request_normal_alert(PRIYA, summary(ScamCategory.DIGITAL_ARREST, source=SourceKind.IMAGE), 41)
    assert plan.outcome is AlertOutcome.READY and plan.alert_kind is AlertKind.NORMAL
    [alert] = all_alerts(engine)
    assert (alert.alert_kind, alert.verdict, alert.scam_type, alert.source_kind) == (
        "normal", "likely_scam", "digital_arrest", "image",
    )  # fmt: skip
    assert (alert.source_update_id, alert.suppressed_reason, alert.created_at) == (41, None, START)
    assert plan.summary == AlertSummary(AlertVerdict.LIKELY_SCAM, ScamCategory.DIGITAL_ARREST, SourceKind.IMAGE)


@pytest.mark.parametrize("verdict", [AlertVerdict.LIKELY_SCAM, AlertVerdict.SUSPICIOUS])
def test_emergency_alert_accepts_either_verdict_and_records_kind_emergency(linked, verdict):
    svc, engine, _ = linked
    plan = emergency(svc, 7, summary(ScamCategory.REFUND, verdict, SourceKind.TEXT))
    assert plan.outcome is AlertOutcome.READY and plan.alert_kind is AlertKind.EMERGENCY
    [alert] = all_alerts(engine)
    assert (alert.alert_kind, alert.verdict, alert.scam_type, alert.source_kind) == (
        "emergency", verdict.value, "refund", "text",
    )  # fmt: skip


def test_emergency_alert_without_a_check_stores_no_summary_fields(linked):
    svc, engine, _ = linked
    plan = emergency(svc, 7, None)
    assert plan.outcome is AlertOutcome.READY and plan.summary is None
    [alert] = all_alerts(engine)
    assert alert.alert_kind == "emergency"
    assert (alert.verdict, alert.scam_type, alert.source_kind) == (None, None, None)


def test_the_kind_comes_from_the_operation_not_from_the_summary(linked):
    svc, engine, _ = linked
    same = summary(ScamCategory.LOAN)
    assert svc.request_normal_alert(PRIYA, same, 1).alert_kind is AlertKind.NORMAL
    assert svc.request_emergency_alert(PRIYA, same, 2).alert_kind is AlertKind.EMERGENCY
    assert [a.alert_kind for a in all_alerts(engine)] == ["normal", "emergency"]


def test_normal_alert_requires_a_likely_scam_summary(linked):
    svc, engine, _ = linked
    with pytest.raises(ValueError):
        svc.request_normal_alert(PRIYA, summary(verdict=AlertVerdict.SUSPICIOUS), 1)
    assert all_alerts(engine) == []  # nothing was written


@pytest.mark.parametrize("bad", [None, "likely_scam", {"verdict": "likely_scam"}, 5, ("likely_scam", "otp_fraud", "text")])
def test_normal_alert_rejects_anything_but_an_alert_summary(linked, bad):
    svc, engine, _ = linked
    with pytest.raises(TypeError):
        svc.request_normal_alert(PRIYA, bad, 1)  # type: ignore[arg-type]
    assert all_alerts(engine) == []


@pytest.mark.parametrize("bad", ["suspicious", "Please share your OTP", {"scam_type": "otp"}, 5, ["x"]])
def test_emergency_alert_rejects_anything_but_a_summary_or_none(linked, bad):
    svc, engine, _ = linked
    with pytest.raises(TypeError):
        svc.request_emergency_alert(PRIYA, bad, 1)  # type: ignore[arg-type]
    assert all_alerts(engine) == []


@pytest.mark.parametrize("bad_id", ["1", 1.5, None, True])
def test_update_id_must_be_an_integer(linked, bad_id):
    svc, _, _ = linked
    with pytest.raises(TypeError):
        normal(svc, bad_id)
    with pytest.raises(TypeError):
        emergency(svc, bad_id)


# --- who gets planned ------------------------------------------------------------------------------------------------------------


def test_every_active_helper_gets_a_pending_delivery_with_their_language(linked):
    svc, engine, _ = linked
    link_pair(svc, PRIYA, ANITA)
    plan = normal(svc, 1)
    assert [(d.guardian_name, d.guardian_language, d.guardian_telegram_user_id) for d in plan.deliveries] == [
        ("Rahul", "hi", RAHUL.telegram_user_id),
        ("Anita", "en", ANITA.telegram_user_id),
    ]
    assert plan.protected_name == "Priya" and plan.unreachable_names == ()
    assert [d.status for d in all_deliveries(engine)] == ["pending", "pending"]
    assert {d.delivery_id for d in plan.deliveries} == {d.id for d in all_deliveries(engine)}


def test_only_active_links_are_alerted(linked):
    svc, engine, clock = linked
    open_invite_for(svc, PRIYA, ANITA)  # awaiting_guardian
    waiting = open_invite_for(svc, PRIYA, MEERA)
    svc.guardian_respond(MEERA.telegram_user_id, waiting, True)  # awaiting_protected
    declined = open_invite_for(svc, PRIYA, person(2004, "Dev"))
    svc.guardian_respond(2004, declined, False)
    svc.create_invite(PRIYA)  # unopened invite
    assert [d.guardian_name for d in normal(svc, 1).deliveries] == ["Rahul"]


def test_a_revoked_helper_is_not_alerted(linked):
    svc, _, _ = linked
    ref = link_pair(svc, PRIYA, ANITA)
    svc.revoke(PRIYA.telegram_user_id, ref)
    assert [d.guardian_name for d in normal(svc, 1).deliveries] == ["Rahul"]


def test_no_helpers_means_no_alert_and_nothing_written():
    svc, engine, _ = make_service()
    svc.upsert_person(PRIYA)  # known to the service, but without helpers
    for plan in (normal(svc, 1), emergency(svc, 2)):
        assert plan.outcome is AlertOutcome.NO_HELPERS and plan.deliveries == ()
    assert all_alerts(engine) == [] and all_deliveries(engine) == []


def test_a_person_unknown_to_family_mode_gets_no_helpers_and_no_database_rows():
    svc, engine, _ = make_service()
    assert normal(svc, 1).outcome is AlertOutcome.NO_HELPERS
    assert emergency(svc, 2).outcome is AlertOutcome.NO_HELPERS
    assert all_people(engine) == []


def test_blocked_helpers_are_skipped_and_named(linked):
    svc, engine, _ = linked
    link_pair(svc, PRIYA, ANITA)
    svc.mark_blocked(RAHUL.telegram_user_id, True)
    plan = normal(svc, 1)
    assert plan.outcome is AlertOutcome.READY
    assert [d.guardian_name for d in plan.deliveries] == ["Anita"]
    assert plan.unreachable_names == ("Rahul",)
    statuses = {d.guardian_id: d.status for d in all_deliveries(engine)}
    assert sorted(statuses.values()) == ["pending", "skipped_blocked"]


def test_when_every_helper_is_blocked_the_plan_is_ready_but_empty(linked):
    svc, _, _ = linked
    svc.mark_blocked(RAHUL.telegram_user_id, True)
    plan = emergency(svc, 1)
    assert plan.outcome is AlertOutcome.READY
    assert plan.deliveries == () and plan.unreachable_names == ("Rahul",)  # the caller must tell the user


def test_the_protected_persons_name_and_language_are_refreshed_and_cleaned(linked):
    svc, engine, _ = linked
    plan = svc.request_normal_alert(PersonRef(PRIYA.telegram_user_id, "Priya http://evil.example", "hi"), summary(), 1)
    assert plan.protected_name == "Priya"
    assert next(p for p in all_people(engine) if p.telegram_user_id == PRIYA.telegram_user_id).language == "hi"


# --- idempotency -------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("op", ["normal", "emergency"])
def test_the_same_update_never_alerts_twice(linked, op):
    svc, engine, _ = linked
    request = (lambda: normal(svc, 9)) if op == "normal" else (lambda: emergency(svc, 9))
    first, second = request(), request()
    assert first.outcome is AlertOutcome.READY
    assert second.outcome is AlertOutcome.DEDUPED and second.alert_id == first.alert_id and second.deliveries == ()
    assert len(all_alerts(engine)) == 1 and len(all_deliveries(engine)) == 1


def test_idempotency_survives_a_restart(linked):
    svc, engine, clock = linked
    first = normal(svc, 55)
    restarted = FamilyService(engine, enabled=True, clock=clock)  # a new process: no in-memory state at all
    again = restarted.request_normal_alert(PRIYA, summary(), 55)
    assert again.outcome is AlertOutcome.DEDUPED and again.alert_id == first.alert_id
    assert len(all_alerts(engine)) == 1


def test_different_people_may_share_an_update_id(linked):
    svc, _, _ = linked
    link_pair(svc, ANITA, RAHUL)
    assert normal(svc, 1).outcome is AlertOutcome.READY
    assert svc.request_normal_alert(ANITA, summary(), 1).outcome is AlertOutcome.READY


# --- normal alerts: category dedupe (30 min) and hourly throttle (5) ----------------------------------------------------------------------


def test_same_category_within_30_minutes_is_suppressed_and_recorded(linked):
    svc, engine, clock = linked
    assert normal(svc, 1).outcome is AlertOutcome.READY
    clock.advance(minutes=29, seconds=59)
    second = normal(svc, 2)
    assert second.outcome is AlertOutcome.DEDUPED and second.deliveries == ()
    rows = all_alerts(engine)
    assert [(a.source_update_id, a.suppressed_reason) for a in rows] == [(1, None), (2, "duplicate")]
    assert len(all_deliveries(engine)) == 1  # the suppressed one sent nothing


def test_category_dedupe_window_ends_at_30_minutes(linked):
    svc, _, clock = linked
    normal(svc, 1)
    clock.advance(minutes=30)  # exactly at the edge: the old alert no longer counts
    assert normal(svc, 2).outcome is AlertOutcome.READY


def test_a_different_category_is_not_a_duplicate(linked):
    svc, _, _ = linked
    assert normal(svc, 1, ScamCategory.OTP_FRAUD).outcome is AlertOutcome.READY
    assert normal(svc, 2, ScamCategory.KYC_UPDATE).outcome is AlertOutcome.READY


def test_a_suppressed_duplicate_does_not_extend_the_dedupe_window(linked):
    svc, _, clock = linked
    normal(svc, 1)
    clock.advance(minutes=20)
    assert normal(svc, 2).outcome is AlertOutcome.DEDUPED  # suppressed at +20
    clock.advance(minutes=11)  # +31 since the one that was sent
    assert normal(svc, 3).outcome is AlertOutcome.READY


def test_the_sixth_normal_alert_in_an_hour_is_throttled(linked):
    svc, engine, clock = linked
    for i in range(5):
        assert normal(svc, i, CATEGORIES[i]).outcome is AlertOutcome.READY
        clock.advance(minutes=1)
    sixth = normal(svc, 5, CATEGORIES[5])
    assert sixth.outcome is AlertOutcome.THROTTLED and sixth.deliveries == ()
    assert [a.suppressed_reason for a in all_alerts(engine)] == [None] * 5 + ["throttled"]
    assert len(all_deliveries(engine)) == 5


def test_throttled_attempts_do_not_extend_the_throttle(linked):
    svc, _, clock = linked
    for i in range(5):
        normal(svc, i, CATEGORIES[i])
    for i in range(5, 12):  # a flood of attempts, all suppressed
        assert normal(svc, i, CATEGORIES[i]).outcome is AlertOutcome.THROTTLED
    clock.advance(hours=1, seconds=1)  # the five real ones age out; the suppressed ones never counted
    assert normal(svc, 99, CATEGORIES[6]).outcome is AlertOutcome.READY


def test_throttle_is_per_person_and_follows_the_policy(linked):
    svc, engine, clock = make_service(policy=FamilyPolicy(alert_max_per_hour=2))
    link_pair(svc, PRIYA, RAHUL)
    link_pair(svc, ANITA, RAHUL)
    assert normal(svc, 1, CATEGORIES[0]).outcome is AlertOutcome.READY
    assert normal(svc, 2, CATEGORIES[1]).outcome is AlertOutcome.READY
    assert normal(svc, 3, CATEGORIES[2]).outcome is AlertOutcome.THROTTLED
    assert svc.request_normal_alert(ANITA, summary(CATEGORIES[2]), 4).outcome is AlertOutcome.READY  # someone else


# --- emergencies: bypass the throttle, dedupe within 2 minutes ---------------------------------------------------------------------------------


def test_an_emergency_bypasses_the_hourly_throttle(linked):
    svc, engine, _ = linked
    for i in range(5):
        normal(svc, i, CATEGORIES[i])
    assert normal(svc, 5, CATEGORIES[5]).outcome is AlertOutcome.THROTTLED  # normal alerts are now throttled...
    plan = emergency(svc, 6)
    assert plan.outcome is AlertOutcome.READY and len(plan.deliveries) == 1  # ...but an emergency goes out
    assert [a.suppressed_reason for a in all_alerts(engine) if a.alert_kind == "emergency"] == [None]


def test_emergencies_do_not_count_towards_the_normal_throttle(linked):
    svc, _, clock = linked
    for i in range(4):
        assert normal(svc, i, CATEGORIES[i]).outcome is AlertOutcome.READY
    for i in range(10, 14):  # four emergencies, spaced out so none is deduped
        assert emergency(svc, i).outcome is AlertOutcome.READY
        clock.advance(minutes=3)
    assert normal(svc, 20, CATEGORIES[4]).outcome is AlertOutcome.READY  # the fifth normal: still allowed
    assert normal(svc, 21, CATEGORIES[5]).outcome is AlertOutcome.THROTTLED


def test_the_normal_category_dedupe_does_not_apply_to_emergencies(linked):
    svc, _, _ = linked
    assert normal(svc, 1, ScamCategory.OTP_FRAUD).outcome is AlertOutcome.READY
    assert emergency(svc, 2, summary(ScamCategory.OTP_FRAUD)).outcome is AlertOutcome.READY


def test_a_normal_alert_does_not_block_an_emergency_and_vice_versa(linked):
    svc, _, _ = linked
    assert emergency(svc, 1).outcome is AlertOutcome.READY
    assert normal(svc, 2).outcome is AlertOutcome.READY  # emergency windows are separate from normal ones
    assert emergency(svc, 3).outcome is AlertOutcome.DEDUPED


def test_repeated_emergencies_dedupe_to_one_per_two_minutes(linked):
    svc, engine, clock = linked
    first = emergency(svc, 1)
    assert first.outcome is AlertOutcome.READY
    clock.advance(seconds=30)
    for update_id in (2, 3, 4):
        repeat = emergency(svc, update_id)
        assert repeat.outcome is AlertOutcome.DEDUPED and repeat.deliveries == ()
    real = [a for a in all_alerts(engine) if a.suppressed_reason is None]
    assert len(real) == 1  # exactly one alert actually went out
    assert len(all_deliveries(engine)) == 1


def test_emergency_dedupe_boundaries(linked):
    svc, _, clock = linked
    emergency(svc, 1)
    clock.advance(seconds=119)
    assert emergency(svc, 2).outcome is AlertOutcome.DEDUPED
    clock.advance(seconds=1)  # exactly 120 s after the one that was sent
    assert emergency(svc, 3).outcome is AlertOutcome.READY
    clock.advance(seconds=121)
    assert emergency(svc, 4).outcome is AlertOutcome.READY


def test_suppressed_emergencies_do_not_extend_the_window(linked):
    svc, _, clock = linked
    emergency(svc, 1)
    clock.advance(seconds=100)
    assert emergency(svc, 2).outcome is AlertOutcome.DEDUPED
    clock.advance(seconds=21)  # 121 s after the real one, only 21 s after the suppressed one
    assert emergency(svc, 3).outcome is AlertOutcome.READY


def test_emergency_dedupe_is_per_person(linked):
    svc, _, _ = linked
    link_pair(svc, ANITA, RAHUL)
    assert emergency(svc, 1).outcome is AlertOutcome.READY
    assert svc.request_emergency_alert(ANITA, None, 2).outcome is AlertOutcome.READY


def test_dedupe_holds_even_if_the_previous_emergency_could_not_be_delivered(linked):
    """Strict by design: the user is told to call their helpers themselves."""
    svc, _, clock = linked
    first = emergency(svc, 1)
    svc.record_delivery(first.deliveries[0].delivery_id, DeliveryStatus.FAILED, 403)
    clock.advance(seconds=10)
    assert emergency(svc, 2).outcome is AlertOutcome.DEDUPED


def test_emergency_dedupe_window_follows_the_policy():
    svc, _, clock = make_service(policy=FamilyPolicy(emergency_dedupe_window=timedelta(seconds=10)))
    link_pair(svc, PRIYA, RAHUL)
    emergency(svc, 1)
    clock.advance(seconds=11)
    assert emergency(svc, 2).outcome is AlertOutcome.READY


# --- delivery: re-check authorisation right before sending -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["normal", "emergency"])
def test_an_active_helper_is_authorised_to_receive(linked, kind):
    svc, _, _ = linked
    plan = normal(svc, 1) if kind == "normal" else emergency(svc, 1)
    assert svc.authorize_delivery(plan.deliveries[0].delivery_id) is True


@pytest.mark.parametrize("kind", ["normal", "emergency"])
def test_revocation_between_planning_and_sending_stops_the_message(linked, kind):
    svc, engine, _ = linked
    ref = link_pair(svc, PRIYA, ANITA)
    plan = normal(svc, 1) if kind == "normal" else emergency(svc, 1)
    rahul_delivery, anita_delivery = plan.deliveries
    assert svc.authorize_delivery(rahul_delivery.delivery_id) is True

    svc.revoke(PRIYA.telegram_user_id, ref)  # Anita is removed after the plan, before her send
    assert svc.authorize_delivery(anita_delivery.delivery_id) is False
    assert delivery_by_id(engine, anita_delivery.delivery_id).status == "skipped_revoked"
    assert delivery_by_id(engine, rahul_delivery.delivery_id).status == "pending"


def test_a_helper_who_leaves_after_planning_is_not_sent_to(linked):
    svc, engine, _ = linked
    ref = link_pair(svc, PRIYA, ANITA)
    plan = emergency(svc, 1)
    svc.revoke(ANITA.telegram_user_id, ref)
    assert svc.authorize_delivery(plan.deliveries[1].delivery_id) is False


def test_a_helper_who_blocks_the_bot_after_planning_is_not_sent_to(linked):
    svc, engine, _ = linked
    plan = normal(svc, 1)
    svc.mark_blocked(RAHUL.telegram_user_id, True)
    assert svc.authorize_delivery(plan.deliveries[0].delivery_id) is False
    assert delivery_by_id(engine, plan.deliveries[0].delivery_id).status == "skipped_blocked"


def test_a_deleted_person_cannot_be_sent_to(linked):
    svc, _, _ = linked
    plan = normal(svc, 1)
    svc.delete_person(RAHUL.telegram_user_id)
    assert svc.authorize_delivery(plan.deliveries[0].delivery_id) is False


def test_an_unknown_delivery_is_not_authorised(linked):
    svc, _, _ = linked
    assert svc.authorize_delivery(424242) is False


def test_a_delivery_is_never_authorised_twice(linked):
    svc, _, _ = linked
    delivery_id = normal(svc, 1).deliveries[0].delivery_id
    assert svc.record_delivery(delivery_id, DeliveryStatus.SENT) is True
    assert svc.authorize_delivery(delivery_id) is False  # already sent: no duplicate message


def test_authorisation_is_decided_at_send_time_not_plan_time(linked):
    svc, _, _ = linked
    ref = link_pair(svc, PRIYA, ANITA)
    delivery_id = normal(svc, 1).deliveries[1].delivery_id
    svc.revoke(PRIYA.telegram_user_id, ref)
    assert svc.authorize_delivery(delivery_id) is False
    assert svc.authorize_delivery(delivery_id) is False  # and it stays refused


# --- recording the outcome ---------------------------------------------------------------------------------------------------------------------------


def test_record_sent_and_failed(linked):
    svc, engine, clock = linked
    link_pair(svc, PRIYA, ANITA)
    rahul, anita = normal(svc, 1).deliveries
    clock.advance(seconds=5)
    assert svc.record_delivery(rahul.delivery_id, DeliveryStatus.SENT) is True
    assert svc.record_delivery(anita.delivery_id, DeliveryStatus.FAILED, 403) is True
    sent, failed = delivery_by_id(engine, rahul.delivery_id), delivery_by_id(engine, anita.delivery_id)
    assert (sent.status, sent.error_code, sent.attempted_at, sent.sent_at) == ("sent", None, START + timedelta(seconds=5), START + timedelta(seconds=5))
    assert (failed.status, failed.error_code, failed.sent_at) == ("failed", 403, None)


def test_a_delivery_can_only_be_finished_once(linked):
    svc, engine, _ = linked
    delivery_id = normal(svc, 1).deliveries[0].delivery_id
    assert svc.record_delivery(delivery_id, DeliveryStatus.FAILED, 500) is True
    assert svc.record_delivery(delivery_id, DeliveryStatus.SENT) is False
    assert delivery_by_id(engine, delivery_id).status == "failed"


def test_a_skipped_delivery_cannot_be_recorded_as_sent(linked):
    svc, engine, _ = linked
    delivery_id = normal(svc, 1).deliveries[0].delivery_id
    svc.mark_blocked(RAHUL.telegram_user_id, True)
    svc.authorize_delivery(delivery_id)  # marks it skipped_blocked
    assert svc.record_delivery(delivery_id, DeliveryStatus.SENT) is False
    assert delivery_by_id(engine, delivery_id).status == "skipped_blocked"


@pytest.mark.parametrize("status", [DeliveryStatus.PENDING, DeliveryStatus.SKIPPED_BLOCKED, DeliveryStatus.SKIPPED_REVOKED])
def test_only_sent_or_failed_can_be_recorded(linked, status):
    svc, _, _ = linked
    delivery_id = normal(svc, 1).deliveries[0].delivery_id
    with pytest.raises(ValueError):
        svc.record_delivery(delivery_id, status)


def test_recording_an_unknown_delivery_returns_false(linked):
    svc, _, _ = linked
    assert svc.record_delivery(424242, DeliveryStatus.SENT) is False


# --- nothing but bounded values is ever stored -----------------------------------------------------------------------------------------------------------


def test_stored_alerts_contain_only_enum_values_and_numbers(linked):
    svc, engine, _ = linked
    normal(svc, 1, ScamCategory.OTP_FRAUD)
    emergency(svc, 2, summary(ScamCategory.REFUND, AlertVerdict.SUSPICIOUS, SourceKind.IMAGE))
    emergency(svc, 200)
    for alert in all_alerts(engine):
        assert alert.alert_kind in {k.value for k in AlertKind}
        assert alert.verdict in {None, *(v.value for v in AlertVerdict)}
        assert alert.scam_type in {None, *(c.value for c in ScamCategory)}
        assert alert.source_kind in {None, *(s.value for s in SourceKind)}
        assert alert.suppressed_reason in {None, "throttled", "duplicate"}
        assert isinstance(alert.source_update_id, int)


def test_the_service_has_no_way_to_receive_message_content():
    import inspect

    for name in ("request_normal_alert", "request_emergency_alert"):
        params = inspect.signature(getattr(FamilyService, name)).parameters
        assert list(params) == ["self", "person", "summary", "source_update_id"]


# --- retention ------------------------------------------------------------------------------------------------------------------------------------------


def test_old_alerts_and_their_deliveries_are_purged_after_30_days(linked):
    svc, engine, clock = linked
    normal(svc, 1)
    clock.advance(days=29)
    emergency(svc, 2)
    assert svc.maintenance() == (0, 0)  # nothing is old enough yet
    clock.advance(days=2)  # the first is now 31 days old, the second 2 days
    expired, purged = svc.maintenance()
    assert purged == 1
    assert [a.source_update_id for a in all_alerts(engine)] == [2]
    assert len(all_deliveries(engine)) == 1


# --- concurrency (real threads, file database) -------------------------------------------------------------------------------------------------------------


def test_simultaneous_emergency_taps_create_exactly_one_alert(file_linked):
    svc, engine, _ = file_linked
    plans = run_concurrently(*[lambda i=i: svc.request_emergency_alert(PRIYA, None, 100 + i) for i in range(8)])
    assert sorted(p.outcome.value for p in plans) == ["deduped"] * 7 + ["ready"]
    real = [a for a in all_alerts(engine) if a.suppressed_reason is None]
    assert len(real) == 1
    assert len(all_deliveries(engine)) == 1


def test_simultaneous_redeliveries_of_one_update_create_exactly_one_alert(file_linked):
    svc, engine, _ = file_linked
    plans = run_concurrently(*[lambda: svc.request_emergency_alert(PRIYA, None, 777)] * 6)
    assert sorted(p.outcome.value for p in plans) == ["deduped"] * 5 + ["ready"]
    assert len(all_alerts(engine)) == 1


def test_simultaneous_normal_alerts_respect_the_hourly_throttle(file_linked):
    svc, engine, _ = file_linked
    plans = run_concurrently(*[lambda i=i: svc.request_normal_alert(PRIYA, summary(CATEGORIES[i]), 200 + i) for i in range(8)])
    assert sorted(p.outcome.value for p in plans) == ["ready"] * 5 + ["throttled"] * 3
    assert len(all_deliveries(engine)) == 5


def test_simultaneous_same_category_normal_alerts_send_once(file_linked):
    svc, engine, _ = file_linked
    plans = run_concurrently(*[lambda i=i: svc.request_normal_alert(PRIYA, summary(), 300 + i) for i in range(6)])
    assert sorted(p.outcome.value for p in plans) == ["deduped"] * 5 + ["ready"]


def test_authorisation_racing_a_revocation_never_sends_after_the_revoke_completes(file_linked):
    svc, engine, _ = file_linked
    ref = link_pair(svc, PRIYA, ANITA)
    plan = svc.request_emergency_alert(PRIYA, None, 1)
    anita = plan.deliveries[1]
    results = run_concurrently(
        lambda: svc.authorize_delivery(anita.delivery_id),
        lambda: svc.revoke(PRIYA.telegram_user_id, ref),
    )
    # Whatever the interleaving, once revoke has committed a fresh check must refuse.
    assert svc.authorize_delivery(anita.delivery_id) is False
    assert results[1].outcome.value == "ok"


def test_alert_rows_are_written_in_one_transaction_with_their_deliveries(linked, monkeypatch):
    """If planning fails half way, neither the alert nor any delivery remains."""
    svc, engine, _ = linked
    link_pair(svc, PRIYA, ANITA)
    import app.family.repository as repository

    real_add_delivery = repository.add_delivery
    calls = {"n": 0}

    def failing_add_delivery(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real_add_delivery(*args, **kwargs)

    monkeypatch.setattr(repository, "add_delivery", failing_add_delivery)
    with pytest.raises(RuntimeError):
        normal(svc, 1)
    assert all_alerts(engine) == [] and all_deliveries(engine) == []
    with session_scope(engine) as s:
        assert s.exec(select(FamilyAlert)).all() == []
