import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import inspect

import app.family.repository as repository
from app.db.engine import make_engine
from app.db.models import AlertKind, LinkStatus
from app.family import tokens
from app.family.policy import FamilyPolicy
from app.family.service import AsyncFamilyService, FamilyDisabledError, FamilyService
from app.family.types import AlertOutcome, ConsentOutcome, InviteResult, LinkResult, PersonRef
from tests.family_helpers import (
    ANITA,
    MEERA,
    PRIYA,
    RAHUL,
    START,
    all_alerts,
    all_deliveries,
    all_links,
    all_people,
    link_pair,
    link_row,
    make_service,
    open_invite_for,
    person,
    summary,
)

INVALID = LinkResult(ConsentOutcome.INVALID)


def snapshot(engine):
    return [(link.ref, link.status, link.guardian_id, link.token_hash, link.expires_at, link.ended_by) for link in all_links(engine)]


def run_concurrently(*calls):
    barrier = threading.Barrier(len(calls))

    def wrapped(call):
        barrier.wait()
        return call()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        return list(pool.map(wrapped, calls))


# --- disabled ----------------------------------------------------------------------------------------------------------------


def test_consent_operations_refuse_when_family_mode_is_off():
    svc = FamilyService(make_engine("sqlite://"))  # default: disabled; no tables exist, so any DB access would fail
    assert not svc.enabled
    for call in (
        lambda: svc.create_invite(PRIYA),
        lambda: svc.open_invite(RAHUL, tokens.new_invite_token()),
        lambda: svc.guardian_respond(1, tokens.new_ref(), True),
        lambda: svc.protected_confirm(1, tokens.new_ref(), True),
        lambda: svc.revoke(1, tokens.new_ref()),
        lambda: svc.delete_person(1),
        lambda: svc.list_helpers(1),
        lambda: svc.list_protected(1),
        lambda: svc.mark_blocked(1, True),
        lambda: svc.upsert_person(PRIYA),
    ):
        with pytest.raises(FamilyDisabledError):
            call()


def test_family_mode_defaults_to_off():
    assert FamilyService(make_engine("sqlite://")).enabled is False


def test_alert_requests_return_disabled_without_touching_the_database():
    svc = FamilyService(make_engine("sqlite://"), enabled=False)  # no tables: any query would raise
    assert svc.request_normal_alert(PRIYA, summary(), 1).outcome is AlertOutcome.DISABLED
    assert svc.request_emergency_alert(PRIYA, None, 2).outcome is AlertOutcome.DISABLED


# --- create_invite ---------------------------------------------------------------------------------------------------------------


def test_create_invite_returns_a_token_and_stores_only_its_hash():
    svc, engine, clock = make_service()
    result = svc.create_invite(PRIYA)
    assert result.outcome is ConsentOutcome.OK
    assert tokens.is_valid_token(result.token) and tokens.is_valid_ref(result.link_ref)
    assert result.expires_at == START + timedelta(hours=48)

    [link] = all_links(engine)
    assert link.status == LinkStatus.INVITED.value and link.guardian_id is None
    assert link.token_hash == tokens.hash_token(result.token)
    assert link.expires_at == START + timedelta(hours=48)

    # The plaintext token appears nowhere in the database.
    with engine.connect() as connection:
        for table in inspect(engine).get_table_names():
            for row in connection.exec_driver_sql(f"SELECT * FROM {table}").fetchall():  # noqa: S608 (fixed table names)
                assert result.token not in " ".join(str(value) for value in row)


def test_invite_creates_the_protected_person_with_a_clean_name_and_language():
    svc, engine, _ = make_service()
    svc.create_invite(PersonRef(77, "Priya http://evil.example ‮", "hi-IN"))
    [row] = all_people(engine)
    assert (row.telegram_user_id, row.display_name, row.language, row.bot_blocked) == (77, "Priya", "hi", False)


def test_each_invite_is_independent():
    svc, engine, _ = make_service()
    a, b = svc.create_invite(PRIYA), svc.create_invite(PRIYA)
    assert a.token != b.token and a.link_ref != b.link_ref
    assert len(all_links(engine)) == 2


def test_at_most_three_pending_invites():
    svc, _, clock = make_service()
    for _ in range(3):
        assert svc.create_invite(PRIYA).outcome is ConsentOutcome.OK
    blocked = svc.create_invite(PRIYA)
    assert blocked == InviteResult(ConsentOutcome.TOO_MANY_PENDING)
    assert svc.create_invite(ANITA).outcome is ConsentOutcome.OK  # the limit is per person

    clock.advance(hours=49)  # the old invites expired, freeing capacity
    assert svc.create_invite(PRIYA).outcome is ConsentOutcome.OK


def test_an_opened_invite_no_longer_counts_as_pending():
    svc, _, _ = make_service()
    first = svc.create_invite(PRIYA)
    svc.create_invite(PRIYA)
    svc.create_invite(PRIYA)
    assert svc.create_invite(PRIYA).outcome is ConsentOutcome.TOO_MANY_PENDING
    svc.open_invite(RAHUL, first.token)
    assert svc.create_invite(PRIYA).outcome is ConsentOutcome.OK


def test_helper_limit_counts_live_links_including_unconfirmed_ones():
    svc, _, _ = make_service(policy=FamilyPolicy(max_helpers=2))
    link_pair(svc, PRIYA, RAHUL)
    open_invite_for(svc, PRIYA, ANITA)  # awaiting_guardian still occupies a slot
    assert svc.create_invite(PRIYA) == InviteResult(ConsentOutcome.TOO_MANY_HELPERS)


def test_revoking_frees_a_helper_slot():
    svc, _, _ = make_service(policy=FamilyPolicy(max_helpers=1))
    ref = link_pair(svc, PRIYA, RAHUL)
    assert svc.create_invite(PRIYA).outcome is ConsentOutcome.TOO_MANY_HELPERS
    svc.revoke(PRIYA.telegram_user_id, ref)
    assert svc.create_invite(PRIYA).outcome is ConsentOutcome.OK


# --- open_invite -------------------------------------------------------------------------------------------------------------------


def test_opening_an_invite_moves_to_awaiting_guardian_and_consumes_the_token():
    svc, engine, clock = make_service()
    invite = svc.create_invite(PRIYA)
    result = svc.open_invite(RAHUL, invite.token)

    assert result.outcome is ConsentOutcome.OK and result.status is LinkStatus.AWAITING_GUARDIAN
    assert result.link_ref == invite.link_ref
    assert (result.protected.display_name, result.guardian.display_name, result.guardian.language) == ("Priya", "Rahul", "hi")

    link = link_row(engine, invite.link_ref)
    assert link.status == "awaiting_guardian" and link.token_hash is None
    assert link.guardian_id is not None
    assert link.expires_at == START + timedelta(hours=24)  # a fresh consent window


def test_a_token_works_only_once():
    svc, _, _ = make_service()
    invite = svc.create_invite(PRIYA)
    assert svc.open_invite(RAHUL, invite.token).outcome is ConsentOutcome.OK
    assert svc.open_invite(ANITA, invite.token) == INVALID
    assert svc.open_invite(RAHUL, invite.token) == INVALID


def test_every_kind_of_bad_invite_gets_the_same_answer():
    """Nothing about *why* a link failed may leak to whoever opened it."""
    svc, _, clock = make_service()
    expired = svc.create_invite(PRIYA)
    cancelled = svc.create_invite(PRIYA)
    used = svc.create_invite(PRIYA)
    svc.open_invite(ANITA, used.token)
    svc.revoke(PRIYA.telegram_user_id, cancelled.link_ref)
    clock.advance(hours=49)

    attempts = [
        "",
        "not-a-token",
        "a" * 22,  # well-formed but unknown
        tokens.new_invite_token(),
        expired.token,
        cancelled.token,
        used.token,
        None,
    ]
    assert {svc.open_invite(RAHUL, token) for token in attempts} == {INVALID}


def test_failed_opens_create_no_person_rows():
    svc, engine, _ = make_service()
    svc.open_invite(RAHUL, tokens.new_invite_token())
    svc.open_invite(RAHUL, "garbage")
    assert all_people(engine) == []


def test_invite_expires_exactly_at_its_deadline():
    svc, _, clock = make_service()
    early, late = svc.create_invite(PRIYA), svc.create_invite(PRIYA)
    clock.advance(hours=47, minutes=59)
    assert svc.open_invite(RAHUL, early.token).outcome is ConsentOutcome.OK
    clock.advance(minutes=1)  # exactly 48h after creation
    assert svc.open_invite(ANITA, late.token) == INVALID


def test_you_cannot_accept_your_own_invite_and_the_token_survives():
    svc, engine, _ = make_service()
    invite = svc.create_invite(PRIYA)
    assert svc.open_invite(PRIYA, invite.token).outcome is ConsentOutcome.SELF_INVITE
    assert link_row(engine, invite.link_ref).status == "invited"
    assert svc.open_invite(RAHUL, invite.token).outcome is ConsentOutcome.OK  # still usable by someone else


def test_an_existing_helper_cannot_be_invited_again():
    svc, engine, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    invite = svc.create_invite(PRIYA)
    assert svc.open_invite(RAHUL, invite.token).outcome is ConsentOutcome.ALREADY_LINKED
    assert link_row(engine, invite.link_ref).status == "invited"  # not consumed


def test_a_lost_race_for_the_same_pair_is_reported_as_already_linked(monkeypatch):
    """If another request links the pair between our check and our update, the
    database's unique index stops us, and the invite is left untouched."""
    svc, engine, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    invite = svc.create_invite(PRIYA)
    monkeypatch.setattr(repository, "live_pair_exists", lambda *args, **kwargs: False)  # the check "misses"
    assert svc.open_invite(RAHUL, invite.token).outcome is ConsentOutcome.ALREADY_LINKED
    link = link_row(engine, invite.link_ref)
    assert link.status == "invited" and link.token_hash == tokens.hash_token(invite.token) and link.guardian_id is None


def test_the_same_person_can_help_more_than_one_person_and_be_helped():
    svc, _, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    link_pair(svc, ANITA, RAHUL)
    link_pair(svc, RAHUL, PRIYA)  # Rahul is also protected by Priya: a different pair


# --- guardian_respond --------------------------------------------------------------------------------------------------------------


def test_helper_accepts_and_the_protected_person_must_still_confirm():
    svc, engine, clock = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    clock.advance(hours=1)
    result = svc.guardian_respond(RAHUL.telegram_user_id, ref, True)
    assert result.outcome is ConsentOutcome.OK and result.status is LinkStatus.AWAITING_PROTECTED
    link = link_row(engine, ref)
    assert link.status == "awaiting_protected"  # NOT active yet
    assert link.guardian_consented_at == START + timedelta(hours=1)
    assert link.expires_at == START + timedelta(hours=25)  # a fresh 24h for the next step
    assert link.protected_confirmed_at is None
    assert svc.request_normal_alert(PRIYA, summary(), 1).outcome is AlertOutcome.NO_HELPERS


def test_helper_declines():
    svc, engine, _ = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    result = svc.guardian_respond(RAHUL.telegram_user_id, ref, False)
    assert result.outcome is ConsentOutcome.OK and result.status is LinkStatus.DECLINED
    link = link_row(engine, ref)
    assert (link.status, link.ended_by, link.ended_at) == ("declined", "guardian", START)


def test_only_the_invited_helper_can_respond():
    svc, engine, _ = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    before = snapshot(engine)
    for intruder in (ANITA.telegram_user_id, PRIYA.telegram_user_id, 999_999):  # a stranger, the protected person, an unknown id
        assert svc.guardian_respond(intruder, ref, True) == INVALID
        assert svc.guardian_respond(intruder, ref, False) == INVALID
    assert snapshot(engine) == before


def test_helper_response_with_a_malformed_or_unknown_reference_is_invalid():
    svc, engine, _ = make_service()
    open_invite_for(svc, PRIYA, RAHUL)
    before = snapshot(engine)
    for ref in ("", "x", "A" * 16, tokens.new_ref(), None):
        assert svc.guardian_respond(RAHUL.telegram_user_id, ref, True) == INVALID
    assert snapshot(engine) == before


def test_pressing_accept_twice_is_harmless():
    svc, engine, _ = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    assert svc.guardian_respond(RAHUL.telegram_user_id, ref, True).outcome is ConsentOutcome.OK
    after_first = snapshot(engine)
    assert svc.guardian_respond(RAHUL.telegram_user_id, ref, True) == INVALID
    assert svc.guardian_respond(RAHUL.telegram_user_id, ref, False) == INVALID
    assert snapshot(engine) == after_first


def test_helper_response_window_closes_exactly_on_time():
    svc, engine, clock = make_service()
    on_time = open_invite_for(svc, PRIYA, RAHUL)
    clock.advance(hours=23, minutes=59)
    assert svc.guardian_respond(RAHUL.telegram_user_id, on_time, True).outcome is ConsentOutcome.OK

    late = open_invite_for(svc, ANITA, RAHUL)
    clock.advance(hours=24)  # exactly at the deadline
    assert svc.guardian_respond(RAHUL.telegram_user_id, late, True) == INVALID
    assert link_row(engine, late).status == "expired"


# --- protected_confirm --------------------------------------------------------------------------------------------------------------


def test_protected_person_confirms_and_the_link_goes_active():
    svc, engine, clock = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    svc.guardian_respond(RAHUL.telegram_user_id, ref, True)
    clock.advance(hours=2)
    result = svc.protected_confirm(PRIYA.telegram_user_id, ref, True)
    assert result.outcome is ConsentOutcome.OK and result.status is LinkStatus.ACTIVE
    assert (result.protected.display_name, result.guardian.display_name) == ("Priya", "Rahul")
    link = link_row(engine, ref)
    assert link.status == "active" and link.expires_at is None
    assert link.protected_confirmed_at == START + timedelta(hours=2)
    assert link.guardian_consented_at == START  # both consents on record


def test_protected_person_refuses():
    svc, engine, _ = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    svc.guardian_respond(RAHUL.telegram_user_id, ref, True)
    result = svc.protected_confirm(PRIYA.telegram_user_id, ref, False)
    assert result.outcome is ConsentOutcome.OK and result.status is LinkStatus.DECLINED
    link = link_row(engine, ref)
    assert (link.status, link.ended_by) == ("declined", "protected")
    assert svc.request_normal_alert(PRIYA, summary(), 1).outcome is AlertOutcome.NO_HELPERS


def test_only_the_protected_person_can_confirm():
    svc, engine, _ = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    svc.guardian_respond(RAHUL.telegram_user_id, ref, True)
    before = snapshot(engine)
    for intruder in (RAHUL.telegram_user_id, ANITA.telegram_user_id, 999_999):  # the helper cannot confirm themselves
        assert svc.protected_confirm(intruder, ref, True) == INVALID
        assert svc.protected_confirm(intruder, ref, False) == INVALID
    assert snapshot(engine) == before


def test_confirmation_requires_the_helper_to_have_accepted_first():
    svc, engine, _ = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)  # still awaiting_guardian
    assert svc.protected_confirm(PRIYA.telegram_user_id, ref, True) == INVALID
    assert link_row(engine, ref).status == "awaiting_guardian"


def test_confirming_twice_is_harmless():
    svc, engine, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    after_first = snapshot(engine)
    assert svc.protected_confirm(PRIYA.telegram_user_id, ref, True) == INVALID
    assert svc.protected_confirm(PRIYA.telegram_user_id, ref, False) == INVALID
    assert snapshot(engine) == after_first


def test_confirmation_window_closes_exactly_on_time():
    svc, engine, clock = make_service()
    ref = open_invite_for(svc, PRIYA, RAHUL)
    svc.guardian_respond(RAHUL.telegram_user_id, ref, True)
    clock.advance(hours=23, minutes=59)
    other = open_invite_for(svc, ANITA, RAHUL)
    svc.guardian_respond(RAHUL.telegram_user_id, other, True)
    assert svc.protected_confirm(PRIYA.telegram_user_id, ref, True).outcome is ConsentOutcome.OK

    clock.advance(hours=24)  # exactly the deadline for Anita's pending confirmation
    assert svc.protected_confirm(ANITA.telegram_user_id, other, True) == INVALID
    assert link_row(engine, other).status == "expired"


def test_confirm_rechecks_the_helper_limit_even_if_it_was_fine_when_invited():
    """Two links can both be pending while one slot is free; whichever is
    confirmed second must be refused, and stay open rather than vanish."""
    svc, engine, _ = make_service(policy=FamilyPolicy(max_helpers=2))
    ref_a = open_invite_for(svc, PRIYA, RAHUL)
    svc.guardian_respond(RAHUL.telegram_user_id, ref_a, True)
    ref_b = open_invite_for(svc, PRIYA, ANITA)
    svc.guardian_respond(ANITA.telegram_user_id, ref_b, True)
    assert svc.protected_confirm(PRIYA.telegram_user_id, ref_a, True).outcome is ConsentOutcome.OK
    assert svc.protected_confirm(PRIYA.telegram_user_id, ref_b, True).outcome is ConsentOutcome.OK
    # Two active (the maximum). Smuggle in a third awaiting link, bypassing the invite-time check.
    ref_c = open_invite_for(FamilyService(engine, enabled=True, policy=FamilyPolicy(max_helpers=3), clock=svc._clock), PRIYA, MEERA)
    svc.guardian_respond(MEERA.telegram_user_id, ref_c, True)
    assert svc.protected_confirm(PRIYA.telegram_user_id, ref_c, True).outcome is ConsentOutcome.TOO_MANY_HELPERS
    assert link_row(engine, ref_c).status == "awaiting_protected"


# --- the whole state machine ----------------------------------------------------------------------------------------------------------------


def build(svc, engine, clock, status: str) -> str:
    """Create a link in the given status between PRIYA (protected) and RAHUL (helper)."""
    if status == "invited":
        return svc.create_invite(PRIYA).link_ref
    if status == "expired":
        ref = svc.create_invite(PRIYA).link_ref
        clock.advance(hours=49)
        svc.maintenance()
        return ref
    if status == "awaiting_guardian":
        return open_invite_for(svc, PRIYA, RAHUL)
    ref = open_invite_for(svc, PRIYA, RAHUL)
    if status == "declined":
        svc.guardian_respond(RAHUL.telegram_user_id, ref, False)
        return ref
    svc.guardian_respond(RAHUL.telegram_user_id, ref, True)
    if status == "awaiting_protected":
        return ref
    svc.protected_confirm(PRIYA.telegram_user_id, ref, True)
    if status == "active":
        return ref
    assert status == "revoked"
    svc.revoke(PRIYA.telegram_user_id, ref)
    return ref


ALL_STATUSES = ["invited", "awaiting_guardian", "awaiting_protected", "active", "declined", "revoked", "expired"]
TRANSITIONS = {
    # operation -> statuses from which it is allowed
    "guardian_respond": {"awaiting_guardian"},
    "protected_confirm": {"awaiting_protected"},
    "revoke_by_protected": {"invited", "awaiting_guardian", "awaiting_protected", "active"},
    "revoke_by_guardian": {"awaiting_guardian", "awaiting_protected", "active"},
}


def attempt(svc, op: str, ref: str, accept: bool = True) -> LinkResult:
    if op == "guardian_respond":
        return svc.guardian_respond(RAHUL.telegram_user_id, ref, accept)
    if op == "protected_confirm":
        return svc.protected_confirm(PRIYA.telegram_user_id, ref, accept)
    if op == "revoke_by_protected":
        return svc.revoke(PRIYA.telegram_user_id, ref)
    return svc.revoke(RAHUL.telegram_user_id, ref)


@pytest.mark.parametrize("op", sorted(TRANSITIONS))
@pytest.mark.parametrize("status", ALL_STATUSES)
def test_every_operation_is_allowed_only_from_its_legal_states(op, status):
    svc, engine, clock = make_service()
    ref = build(svc, engine, clock, status)
    assert link_row(engine, ref).status == status
    before = snapshot(engine)
    result = attempt(svc, op, ref)
    if status in TRANSITIONS[op]:
        assert result.outcome is ConsentOutcome.OK
    else:
        assert result == INVALID
        assert snapshot(engine) == before  # an illegal attempt changes nothing


@pytest.mark.parametrize("status", ["declined", "revoked", "expired"])
def test_ended_links_stay_ended(status):
    svc, engine, clock = make_service()
    ref = build(svc, engine, clock, status)
    for op in TRANSITIONS:
        assert attempt(svc, op, ref) == INVALID
        assert attempt(svc, op, ref, accept=False) == INVALID
    assert link_row(engine, ref).status == status


# --- revoke ----------------------------------------------------------------------------------------------------------------------------------


def test_protected_person_removes_a_helper():
    svc, engine, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    result = svc.revoke(PRIYA.telegram_user_id, ref)
    assert result.outcome is ConsentOutcome.OK and result.status is LinkStatus.REVOKED
    assert result.guardian.display_name == "Rahul" and result.protected.display_name == "Priya"
    link = link_row(engine, ref)
    assert (link.status, link.ended_by, link.ended_at, link.expires_at) == ("revoked", "protected", START, None)


def test_helper_leaves():
    svc, engine, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    result = svc.revoke(RAHUL.telegram_user_id, ref)
    assert result.outcome is ConsentOutcome.OK and result.protected.display_name == "Priya"
    assert link_row(engine, ref).ended_by == "guardian"


def test_protected_person_can_cancel_an_unopened_invite_and_its_token_dies():
    svc, engine, _ = make_service()
    invite = svc.create_invite(PRIYA)
    result = svc.revoke(PRIYA.telegram_user_id, invite.link_ref)
    assert result.outcome is ConsentOutcome.OK and result.guardian is None
    assert link_row(engine, invite.link_ref).token_hash is None
    assert svc.open_invite(RAHUL, invite.token) == INVALID


def test_a_third_party_cannot_revoke_anything():
    svc, engine, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    svc.upsert_person(ANITA)
    before = snapshot(engine)
    for actor in (ANITA.telegram_user_id, 999_999):
        assert svc.revoke(actor, ref) == INVALID
    assert snapshot(engine) == before


def test_revoke_with_a_bad_reference_is_invalid():
    svc, _, _ = make_service()
    svc.upsert_person(PRIYA)
    for ref in ("", "zzz", tokens.new_ref(), None):
        assert svc.revoke(PRIYA.telegram_user_id, ref) == INVALID


def test_revoking_twice_is_harmless():
    svc, _, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    assert svc.revoke(PRIYA.telegram_user_id, ref).outcome is ConsentOutcome.OK
    assert svc.revoke(PRIYA.telegram_user_id, ref) == INVALID
    assert svc.revoke(RAHUL.telegram_user_id, ref) == INVALID


def test_a_revoked_pair_can_link_again_with_a_fresh_invite():
    svc, engine, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    svc.revoke(RAHUL.telegram_user_id, ref)
    new_ref = link_pair(svc, PRIYA, RAHUL)
    assert new_ref != ref
    assert sorted(link.status for link in all_links(engine)) == ["active", "revoked"]


def test_revocation_takes_effect_immediately_for_alerts():
    svc, _, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    assert svc.request_normal_alert(PRIYA, summary(), 1).outcome is AlertOutcome.READY
    svc.revoke(PRIYA.telegram_user_id, ref)
    assert svc.request_emergency_alert(PRIYA, None, 2).outcome is AlertOutcome.NO_HELPERS


# --- expiry and maintenance ------------------------------------------------------------------------------------------------------------------


def test_maintenance_expires_stale_links_and_clears_their_tokens():
    svc, engine, clock = make_service()
    invite = svc.create_invite(PRIYA)
    awaiting = open_invite_for(svc, ANITA, RAHUL)
    active = link_pair(svc, MEERA, RAHUL)
    clock.advance(hours=49)
    expired, purged = svc.maintenance()
    assert (expired, purged) == (2, 0)
    assert link_row(engine, invite.link_ref).status == "expired"
    assert link_row(engine, invite.link_ref).token_hash is None
    assert link_row(engine, invite.link_ref).ended_by == "system"
    assert link_row(engine, awaiting).status == "expired"
    assert link_row(engine, active).status == "active"  # active links never expire
    assert svc.maintenance() == (0, 0)  # idempotent


def test_expired_invites_do_not_show_in_the_family_menu():
    svc, _, clock = make_service()
    svc.create_invite(PRIYA)
    assert len(svc.list_helpers(PRIYA.telegram_user_id)) == 1
    clock.advance(hours=49)
    assert svc.list_helpers(PRIYA.telegram_user_id) == ()


# --- listing ---------------------------------------------------------------------------------------------------------------------------------------------


def test_list_helpers_shows_open_links_in_order_with_names_and_status():
    svc, _, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    awaiting = open_invite_for(svc, PRIYA, ANITA)
    invite = svc.create_invite(PRIYA)
    declined = open_invite_for(svc, PRIYA, MEERA)
    svc.guardian_respond(MEERA.telegram_user_id, declined, False)

    listed = svc.list_helpers(PRIYA.telegram_user_id)
    assert [(h.person.display_name if h.person else None, h.status.value) for h in listed] == [
        ("Rahul", "active"),
        ("Anita", "awaiting_guardian"),
        (None, "invited"),
    ]
    assert listed[2].link_ref == invite.link_ref and listed[1].link_ref == awaiting
    assert all(tokens.is_valid_ref(h.link_ref) for h in listed)


def test_list_protected_shows_who_a_person_helps():
    svc, _, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    open_invite_for(svc, ANITA, RAHUL)
    svc.create_invite(MEERA)  # an unopened invite is not Rahul's business
    listed = svc.list_protected(RAHUL.telegram_user_id)
    assert [(h.person.display_name, h.status.value) for h in listed] == [("Priya", "active"), ("Anita", "awaiting_guardian")]


def test_listing_for_unknown_people_is_empty():
    svc, _, _ = make_service()
    assert svc.list_helpers(555) == () and svc.list_protected(555) == ()


# --- people ----------------------------------------------------------------------------------------------------------------------------------------------------


def test_upsert_refreshes_name_and_language_but_keeps_one_row():
    svc, engine, _ = make_service()
    info = svc.upsert_person(PersonRef(9, "Old", "en"))
    assert (info.display_name, info.language) == ("Old", "en")
    info = svc.upsert_person(PersonRef(9, "New http://x.example", "hi"))
    assert (info.display_name, info.language) == ("New", "hi")
    assert len(all_people(engine)) == 1


def test_alert_requests_never_create_people():
    svc, engine, _ = make_service()
    svc.request_normal_alert(PRIYA, summary(), 1)
    svc.request_emergency_alert(PRIYA, None, 2)
    assert all_people(engine) == [] and all_alerts(engine) == []


# --- blocking -------------------------------------------------------------------------------------------------------------------------------------------------------


def test_blocking_a_helper_tells_the_people_they_protect():
    svc, engine, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    link_pair(svc, ANITA, RAHUL)
    open_invite_for(svc, MEERA, RAHUL)  # not active: nobody to warn
    result = svc.mark_blocked(RAHUL.telegram_user_id, True)
    assert result.changed
    assert sorted(p.display_name for p in result.notify_protected) == ["Anita", "Priya"]
    assert next(p for p in all_people(engine) if p.display_name == "Rahul").bot_blocked is True


def test_blocking_is_reported_once_and_unblocking_is_quiet():
    svc, _, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    assert svc.mark_blocked(RAHUL.telegram_user_id, True).changed
    again = svc.mark_blocked(RAHUL.telegram_user_id, True)
    assert not again.changed and again.notify_protected == ()
    back = svc.mark_blocked(RAHUL.telegram_user_id, False)
    assert back.changed and back.notify_protected == ()
    assert not svc.mark_blocked(RAHUL.telegram_user_id, False).changed


def test_blocking_an_unknown_person_changes_nothing():
    svc, engine, _ = make_service()
    assert not svc.mark_blocked(12345, True).changed
    assert all_people(engine) == []


def test_a_person_who_messages_again_is_no_longer_marked_blocked():
    svc, engine, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    svc.mark_blocked(RAHUL.telegram_user_id, True)
    svc.upsert_person(RAHUL)  # they just talked to the bot, so it can reach them
    assert next(p for p in all_people(engine) if p.display_name == "Rahul").bot_blocked is False


# --- /deleteme -------------------------------------------------------------------------------------------------------------------------------------------------------


def test_deleting_a_protected_person_removes_everything_about_them_and_names_who_to_tell():
    svc, engine, clock = make_service()
    link_pair(svc, PRIYA, RAHUL)
    link_pair(svc, PRIYA, ANITA)
    link_pair(svc, MEERA, RAHUL)
    svc.request_normal_alert(PRIYA, summary(), 1)
    svc.request_emergency_alert(MEERA, None, 2)

    result = svc.delete_person(PRIYA.telegram_user_id)
    assert result.deleted
    assert sorted(p.display_name for p in result.notify_helpers) == ["Anita", "Rahul"]
    assert result.notify_protected == ()

    names = {p.display_name for p in all_people(engine)}
    assert "Priya" not in names and {"Rahul", "Anita", "Meera"} <= names
    assert all(link.protected_id != 1 for link in all_links(engine)) and len(all_links(engine)) == 1  # only Meera-Rahul remains
    assert len(all_alerts(engine)) == 1  # Meera's alert survives
    assert len(all_deliveries(engine)) == 1


def test_deleting_a_helper_removes_their_links_and_deliveries_but_keeps_the_alerts_of_others():
    svc, engine, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    link_pair(svc, PRIYA, ANITA)
    svc.request_normal_alert(PRIYA, summary(), 1)
    assert len(all_deliveries(engine)) == 2

    result = svc.delete_person(RAHUL.telegram_user_id)
    assert result.deleted and [p.display_name for p in result.notify_protected] == ["Priya"]
    assert result.notify_helpers == ()
    assert len(all_deliveries(engine)) == 1  # only Anita's remains
    assert len(all_alerts(engine)) == 1
    assert [h.person.display_name for h in svc.list_helpers(PRIYA.telegram_user_id)] == ["Anita"]


def test_deleting_someone_who_never_used_family_mode_is_a_no_op():
    svc, _, _ = make_service()
    assert svc.delete_person(424242).deleted is False


def test_deletion_only_notifies_people_with_open_relationships():
    svc, _, _ = make_service()
    ref = link_pair(svc, PRIYA, RAHUL)
    svc.revoke(PRIYA.telegram_user_id, ref)  # already ended
    declined = open_invite_for(svc, PRIYA, ANITA)
    svc.guardian_respond(ANITA.telegram_user_id, declined, False)
    assert svc.delete_person(PRIYA.telegram_user_id).notify_helpers == ()


def test_a_deleted_person_can_start_afresh():
    svc, engine, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    svc.delete_person(PRIYA.telegram_user_id)
    link_pair(svc, PRIYA, RAHUL)
    assert [h.status.value for h in svc.list_helpers(PRIYA.telegram_user_id)] == ["active"]


def test_deletion_removes_every_dependent_row_itself_without_relying_on_cascade():
    """The database would cascade anyway; this proves the code does not depend on
    it (e.g. on a database or setting where foreign keys are not enforced)."""
    svc, engine, _ = make_service()
    link_pair(svc, PRIYA, RAHUL)
    link_pair(svc, PRIYA, ANITA)
    svc.request_normal_alert(PRIYA, summary(), 1)
    svc.request_emergency_alert(PRIYA, None, 2)
    assert len(all_alerts(engine)) == 2 and len(all_deliveries(engine)) == 4

    raw = engine.raw_connection()
    try:
        raw.driver_connection.execute("PRAGMA foreign_keys=OFF")
        assert raw.driver_connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0  # the premise holds
    finally:
        raw.close()

    assert svc.delete_person(PRIYA.telegram_user_id).deleted
    assert all_alerts(engine) == []
    assert all_deliveries(engine) == []
    assert all_links(engine) == []
    assert sorted(p.display_name for p in all_people(engine)) == ["Anita", "Rahul"]


# --- concurrency (real threads, file database) --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def file_service(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")
    svc, engine, clock = make_service(engine=engine)
    yield svc, engine, clock
    engine.dispose()


def test_two_simultaneous_accepts_have_one_winner(file_service):
    svc, engine, _ = file_service
    ref = open_invite_for(svc, PRIYA, RAHUL)
    results = run_concurrently(*[lambda: svc.guardian_respond(RAHUL.telegram_user_id, ref, True)] * 6)
    assert sorted(r.outcome.value for r in results) == ["invalid"] * 5 + ["ok"]
    assert link_row(engine, ref).status == "awaiting_protected"


def test_accept_and_decline_racing_leave_exactly_one_result(file_service):
    svc, engine, _ = file_service
    ref = open_invite_for(svc, PRIYA, RAHUL)
    results = run_concurrently(
        lambda: svc.guardian_respond(RAHUL.telegram_user_id, ref, True),
        lambda: svc.guardian_respond(RAHUL.telegram_user_id, ref, False),
    )
    assert sorted(r.outcome.value for r in results) == ["invalid", "ok"]
    assert link_row(engine, ref).status in {"awaiting_protected", "declined"}


def test_one_invite_opened_by_several_people_at_once_has_one_winner(file_service):
    svc, engine, _ = file_service
    invite = svc.create_invite(PRIYA)
    helpers = [person(3000 + i, f"Helper{i}") for i in range(6)]
    results = run_concurrently(*[lambda h=h: svc.open_invite(h, invite.token) for h in helpers])
    assert sorted(r.outcome.value for r in results) == ["invalid"] * 5 + ["ok"]
    [link] = all_links(engine)
    assert link.status == "awaiting_guardian" and link.token_hash is None


def test_simultaneous_confirm_and_revoke_leave_a_consistent_state(file_service):
    svc, engine, _ = file_service
    ref = open_invite_for(svc, PRIYA, RAHUL)
    svc.guardian_respond(RAHUL.telegram_user_id, ref, True)
    results = run_concurrently(
        lambda: svc.protected_confirm(PRIYA.telegram_user_id, ref, True),
        lambda: svc.revoke(RAHUL.telegram_user_id, ref),
    )
    final = link_row(engine, ref).status
    assert final in {"active", "revoked"}
    if final == "revoked":  # the revoke won, or came after the confirm; either way nothing is left half-done
        assert link_row(engine, ref).ended_by == "guardian"
    assert all(r.outcome in (ConsentOutcome.OK, ConsentOutcome.INVALID) for r in results)


# --- the async facade ------------------------------------------------------------------------------------------------------------------------------------------


def test_async_facade_offers_every_public_operation_of_the_service():
    public = {name for name in dir(FamilyService) if not name.startswith("_") and callable(getattr(FamilyService, name))}
    wrapped = {name for name in dir(AsyncFamilyService) if not name.startswith("_") and callable(getattr(AsyncFamilyService, name))}
    assert public == wrapped


@pytest.mark.anyio
async def test_async_facade_runs_the_full_flow(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'async.db'}")
    svc, engine, _ = make_service(engine=engine)
    facade = AsyncFamilyService(svc)
    assert facade.enabled

    invite = await facade.create_invite(PRIYA)
    opened = await facade.open_invite(RAHUL, invite.token)
    assert (await facade.guardian_respond(RAHUL.telegram_user_id, opened.link_ref, True)).outcome is ConsentOutcome.OK
    assert (await facade.protected_confirm(PRIYA.telegram_user_id, opened.link_ref, True)).outcome is ConsentOutcome.OK
    assert [h.status.value for h in await facade.list_helpers(PRIYA.telegram_user_id)] == ["active"]

    plan = await facade.request_normal_alert(PRIYA, summary(), 1)
    assert plan.outcome is AlertOutcome.READY and plan.alert_kind is AlertKind.NORMAL
    assert await facade.authorize_delivery(plan.deliveries[0].delivery_id)
    assert (await facade.revoke(PRIYA.telegram_user_id, opened.link_ref)).outcome is ConsentOutcome.OK
    assert await facade.maintenance() == (0, 0)
    assert (await facade.delete_person(PRIYA.telegram_user_id)).deleted
    engine.dispose()
