"""Family Mode domain service (channel-agnostic).

Everything is a sync method running in one short database transaction;
`AsyncFamilyService` is a thin wrapper that runs them in a worker thread for
the async Telegram layer. Nothing here talks to Telegram or knows what a
message says: the only analysis-derived input is a bounded `AlertSummary`.

Alerts: `request_normal_alert` and `request_emergency_alert` are separate,
explicit operations. They plan an alert and return who to message; the channel
layer sends, calling `authorize_delivery` immediately before every send (so
revocation is re-checked at the last moment) and `record_delivery` after.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col

from app.db.engine import session_scope
from app.db.models import (
    LIVE_LINK_STATUSES,
    OPEN_LINK_STATUSES,
    AlertKind,
    DeliveryStatus,
    EndedBy,
    FamilyLink,
    LinkStatus,
    Person,
    SuppressedReason,
)
from app.family import repository as repo
from app.family import tokens
from app.family.policy import DEFAULT_POLICY, ONE_HOUR, FamilyPolicy, normalize_language, sanitize_display_name
from app.family.types import (
    AlertOutcome,
    AlertPlan,
    AlertSummary,
    AlertVerdict,
    BlockResult,
    ConsentOutcome,
    DeletionResult,
    DeliveryPlan,
    HelperInfo,
    InviteResult,
    LinkResult,
    PersonInfo,
    PersonRef,
)


class FamilyDisabledError(RuntimeError):
    """A consent operation was called while Family Mode is switched off."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _info(person: Person) -> PersonInfo:
    return PersonInfo(person.telegram_user_id, person.display_name, person.language)


class FamilyService:
    def __init__(
        self,
        engine: Engine,
        *,
        enabled: bool = False,
        policy: FamilyPolicy = DEFAULT_POLICY,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._engine = engine
        self._enabled = enabled
        self._policy = policy
        self._clock = clock

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise FamilyDisabledError("Family Mode is disabled")

    # -- people -----------------------------------------------------------------------------------------

    def _upsert_person(self, s: Session, ref: PersonRef, now: datetime) -> Person:
        """Create or refresh a person. They just interacted with the bot, so they are reachable."""
        name = sanitize_display_name(ref.display_name)
        language = normalize_language(ref.language)
        person = repo.person_by_telegram_id(s, ref.telegram_user_id, lock=True)
        if person is None:
            return repo.add_person(
                s, telegram_user_id=ref.telegram_user_id, display_name=name, language=language, now=now
            )
        person.display_name = name
        person.language = language
        person.bot_blocked = False
        s.add(person)
        s.flush()
        return person

    def upsert_person(self, ref: PersonRef) -> PersonInfo:
        self._require_enabled()
        with session_scope(self._engine) as s:
            return _info(self._upsert_person(s, ref, self._clock()))

    # -- consent: invite -----------------------------------------------------------------------------------

    def create_invite(self, protected: PersonRef) -> InviteResult:
        """The protected person agreed to invite a helper: mint a single-use invite."""
        self._require_enabled()
        now = self._clock()
        with session_scope(self._engine) as s:
            repo.expire_due(s, now)
            person = self._upsert_person(s, protected, now)
            pending = repo.count_links(s, protected_id=person.id, statuses=[LinkStatus.INVITED], unexpired_at=now)
            if pending >= self._policy.max_pending_invites:
                return InviteResult(ConsentOutcome.TOO_MANY_PENDING)
            if repo.count_links(s, protected_id=person.id, statuses=LIVE_LINK_STATUSES) >= self._policy.max_helpers:
                return InviteResult(ConsentOutcome.TOO_MANY_HELPERS)

            token = tokens.new_invite_token()
            ref = tokens.new_ref()
            expires_at = now + self._policy.invite_ttl
            repo.add_link(
                s, ref=ref, protected_id=person.id, token_hash=tokens.hash_token(token), expires_at=expires_at, now=now
            )
            return InviteResult(ConsentOutcome.OK, token=token, link_ref=ref, expires_at=expires_at)

    def open_invite(self, guardian: PersonRef, token: str) -> LinkResult:
        """Someone opened an invite link. Consumes the token and awaits their consent."""
        self._require_enabled()
        if not tokens.is_valid_token(token):
            return LinkResult(ConsentOutcome.INVALID)
        now = self._clock()
        digest = tokens.hash_token(token)
        with session_scope(self._engine) as s:
            repo.expire_due(s, now)
            link = repo.link_by_token_hash(s, digest)
            if (
                link is None
                or link.status != LinkStatus.INVITED.value
                or link.expires_at is None
                or link.expires_at <= now
            ):
                return LinkResult(ConsentOutcome.INVALID)

            protected = repo.person_by_id(s, link.protected_id)
            if protected is None:
                return LinkResult(ConsentOutcome.INVALID)
            if protected.telegram_user_id == guardian.telegram_user_id:
                return LinkResult(ConsentOutcome.SELF_INVITE)

            guardian_person = self._upsert_person(s, guardian, now)
            if repo.live_pair_exists(s, protected.id, guardian_person.id):
                return LinkResult(ConsentOutcome.ALREADY_LINKED)

            try:
                with s.begin_nested():  # a savepoint, so a lost race cannot poison the transaction
                    moved = repo.transition_link(
                        s,
                        ref=link.ref,
                        from_statuses=[LinkStatus.INVITED],
                        to_status=LinkStatus.AWAITING_GUARDIAN,
                        values={
                            "guardian_id": guardian_person.id,
                            "token_hash": None,
                            "expires_at": now + self._policy.consent_ttl,
                        },
                        extra_where=[FamilyLink.token_hash == digest, col(FamilyLink.expires_at) > now],
                    )
            except IntegrityError:  # the same pair became linked a moment ago
                return LinkResult(ConsentOutcome.ALREADY_LINKED)
            if not moved:
                return LinkResult(ConsentOutcome.INVALID)

            return LinkResult(
                ConsentOutcome.OK,
                link_ref=link.ref,
                status=LinkStatus.AWAITING_GUARDIAN,
                protected=_info(protected),
                guardian=_info(guardian_person),
            )

    # -- consent: the two confirmations ------------------------------------------------------------------------

    def guardian_respond(self, guardian_telegram_user_id: int, link_ref: str, accept: bool) -> LinkResult:
        """The helper pressed Accept or Decline. Only that helper, only in the right state."""
        self._require_enabled()
        if not tokens.is_valid_ref(link_ref):
            return LinkResult(ConsentOutcome.INVALID)
        now = self._clock()
        with session_scope(self._engine) as s:
            repo.expire_due(s, now)
            guardian = repo.person_by_telegram_id(s, guardian_telegram_user_id)
            link = repo.link_by_ref(s, link_ref)
            if guardian is None or link is None or link.guardian_id != guardian.id:
                return LinkResult(ConsentOutcome.INVALID)
            protected = repo.person_by_id(s, link.protected_id)
            if protected is None:
                return LinkResult(ConsentOutcome.INVALID)

            guard = [FamilyLink.guardian_id == guardian.id, col(FamilyLink.expires_at) > now]
            if accept:
                to_status = LinkStatus.AWAITING_PROTECTED
                values = {"guardian_consented_at": now, "expires_at": now + self._policy.consent_ttl}
            else:
                to_status = LinkStatus.DECLINED
                values = {"ended_at": now, "ended_by": EndedBy.GUARDIAN.value, "expires_at": None}
            moved = repo.transition_link(
                s,
                ref=link_ref,
                from_statuses=[LinkStatus.AWAITING_GUARDIAN],
                to_status=to_status,
                values=values,
                extra_where=guard,
            )
            if not moved:
                return LinkResult(ConsentOutcome.INVALID)
            return LinkResult(
                ConsentOutcome.OK, link_ref, to_status, protected=_info(protected), guardian=_info(guardian)
            )

    def protected_confirm(self, protected_telegram_user_id: int, link_ref: str, confirm: bool) -> LinkResult:
        """The protected person confirmed (or refused) the helper who accepted. Only that person."""
        self._require_enabled()
        if not tokens.is_valid_ref(link_ref):
            return LinkResult(ConsentOutcome.INVALID)
        now = self._clock()
        with session_scope(self._engine) as s:
            repo.expire_due(s, now)
            protected = repo.person_by_telegram_id(s, protected_telegram_user_id)
            link = repo.link_by_ref(s, link_ref)
            if (
                protected is None
                or link is None
                or link.protected_id != protected.id
                or link.status != LinkStatus.AWAITING_PROTECTED.value
                or link.guardian_id is None
            ):
                return LinkResult(ConsentOutcome.INVALID)
            guardian = repo.person_by_id(s, link.guardian_id)
            if guardian is None:
                return LinkResult(ConsentOutcome.INVALID)

            guard = [FamilyLink.protected_id == protected.id, col(FamilyLink.expires_at) > now]
            if confirm:
                active = repo.count_links(s, protected_id=protected.id, statuses=[LinkStatus.ACTIVE])
                if active >= self._policy.max_helpers:
                    return LinkResult(ConsentOutcome.TOO_MANY_HELPERS)
                to_status = LinkStatus.ACTIVE
                values = {"protected_confirmed_at": now, "expires_at": None}
            else:
                to_status = LinkStatus.DECLINED
                values = {"ended_at": now, "ended_by": EndedBy.PROTECTED.value, "expires_at": None}
            moved = repo.transition_link(
                s,
                ref=link_ref,
                from_statuses=[LinkStatus.AWAITING_PROTECTED],
                to_status=to_status,
                values=values,
                extra_where=guard,
            )
            if not moved:
                return LinkResult(ConsentOutcome.INVALID)
            return LinkResult(
                ConsentOutcome.OK, link_ref, to_status, protected=_info(protected), guardian=_info(guardian)
            )

    # -- consent: ending a link ----------------------------------------------------------------------------------

    def revoke(self, actor_telegram_user_id: int, link_ref: str) -> LinkResult:
        """Either side ends a link, effective immediately.

        The protected person may cancel any open link (including an unopened
        invite); the helper may leave one they are part of."""
        self._require_enabled()
        if not tokens.is_valid_ref(link_ref):
            return LinkResult(ConsentOutcome.INVALID)
        now = self._clock()
        with session_scope(self._engine) as s:
            actor = repo.person_by_telegram_id(s, actor_telegram_user_id)
            link = repo.link_by_ref(s, link_ref)
            if actor is None or link is None:
                return LinkResult(ConsentOutcome.INVALID)

            if link.protected_id == actor.id:
                role, from_statuses = EndedBy.PROTECTED, OPEN_LINK_STATUSES
            elif link.guardian_id == actor.id:
                role, from_statuses = EndedBy.GUARDIAN, LIVE_LINK_STATUSES
            else:
                return LinkResult(ConsentOutcome.INVALID)

            protected = repo.person_by_id(s, link.protected_id)
            guardian = repo.person_by_id(s, link.guardian_id) if link.guardian_id is not None else None
            moved = repo.transition_link(
                s,
                ref=link_ref,
                from_statuses=from_statuses,
                to_status=LinkStatus.REVOKED,
                values={"ended_at": now, "ended_by": role.value, "token_hash": None, "expires_at": None},
            )
            if not moved or protected is None:
                return LinkResult(ConsentOutcome.INVALID)
            return LinkResult(
                ConsentOutcome.OK,
                link_ref,
                LinkStatus.REVOKED,
                protected=_info(protected),
                guardian=_info(guardian) if guardian else None,
            )

    def delete_person(self, telegram_user_id: int) -> DeletionResult:
        """/deleteme: remove the person, their links and alerts. Returns who to tell."""
        self._require_enabled()
        with session_scope(self._engine) as s:
            person = repo.person_by_telegram_id(s, telegram_user_id, lock=True)
            if person is None:
                return DeletionResult(deleted=False)

            open_links = (LinkStatus.AWAITING_GUARDIAN, LinkStatus.AWAITING_PROTECTED, LinkStatus.ACTIVE)
            helpers = tuple(
                _info(guardian)
                for _link, guardian in repo.links_for_protected(s, person.id, open_links, now=self._clock())
                if guardian is not None
            )
            protected = tuple(_info(p) for _link, p in repo.links_for_guardian(s, person.id, open_links))
            repo.delete_person_data(s, person.id)
            return DeletionResult(deleted=True, notify_helpers=helpers, notify_protected=protected)

    # -- consent: reading --------------------------------------------------------------------------------------------

    def list_helpers(self, protected_telegram_user_id: int) -> tuple[HelperInfo, ...]:
        """The protected person's helpers and open invites (for the /family menu)."""
        self._require_enabled()
        now = self._clock()
        with session_scope(self._engine) as s:
            repo.expire_due(s, now)
            person = repo.person_by_telegram_id(s, protected_telegram_user_id)
            if person is None:
                return ()
            rows = repo.links_for_protected(s, person.id, OPEN_LINK_STATUSES, now=now)
            return tuple(
                HelperInfo(link.ref, LinkStatus(link.status), _info(guardian) if guardian else None, link.expires_at)
                for link, guardian in rows
            )

    def list_protected(self, guardian_telegram_user_id: int) -> tuple[HelperInfo, ...]:
        """The people this person helps (for /leave). `person` is the protected person."""
        self._require_enabled()
        now = self._clock()
        with session_scope(self._engine) as s:
            repo.expire_due(s, now)
            person = repo.person_by_telegram_id(s, guardian_telegram_user_id)
            if person is None:
                return ()
            rows = repo.links_for_guardian(s, person.id, LIVE_LINK_STATUSES)
            return tuple(HelperInfo(link.ref, LinkStatus(link.status), _info(p), link.expires_at) for link, p in rows)

    def mark_blocked(self, telegram_user_id: int, blocked: bool) -> BlockResult:
        """Record that the bot was blocked (or unblocked) by this person."""
        self._require_enabled()
        with session_scope(self._engine) as s:
            person = repo.person_by_telegram_id(s, telegram_user_id, lock=True)
            if person is None or person.bot_blocked == blocked:
                return BlockResult(changed=False)
            person.bot_blocked = blocked
            s.add(person)
            s.flush()
            if not blocked:
                return BlockResult(changed=True)
            people = tuple(
                _info(p) for _link, p in repo.links_for_guardian(s, person.id, [LinkStatus.ACTIVE])
            )
            return BlockResult(changed=True, notify_protected=people)

    # -- alerts -----------------------------------------------------------------------------------------------------------

    def request_normal_alert(self, person: PersonRef, summary: AlertSummary, source_update_id: int) -> AlertPlan:
        """A NORMAL alert: only for a likely scam, throttled and de-duplicated."""
        if not isinstance(summary, AlertSummary):
            raise TypeError("summary must be an AlertSummary")
        if summary.verdict is not AlertVerdict.LIKELY_SCAM:
            raise ValueError("normal alerts require a LIKELY_SCAM summary")
        return self._request_alert(person, summary, source_update_id, AlertKind.NORMAL)

    def request_emergency_alert(
        self, person: PersonRef, summary: AlertSummary | None, source_update_id: int
    ) -> AlertPlan:
        """An EMERGENCY alert: bypasses the normal hourly throttle, but at most
        one per person per emergency window. `summary` is None for /emergency
        raised with no check behind it."""
        if summary is not None and not isinstance(summary, AlertSummary):
            raise TypeError("summary must be an AlertSummary or None")
        return self._request_alert(person, summary, source_update_id, AlertKind.EMERGENCY)

    def _request_alert(
        self, ref: PersonRef, summary: AlertSummary | None, source_update_id: int, kind: AlertKind
    ) -> AlertPlan:
        if isinstance(source_update_id, bool) or not isinstance(source_update_id, int):
            raise TypeError("source_update_id must be an integer")
        if not self._enabled:
            return AlertPlan(AlertOutcome.DISABLED, kind, summary=summary)

        now = self._clock()
        with session_scope(self._engine) as s:
            # The row lock (Postgres) / immediate transaction (SQLite) makes the
            # checks below and the insert one indivisible step.
            person = repo.person_by_telegram_id(s, ref.telegram_user_id, lock=True)
            if person is None:
                # Never a family member: nothing to alert and nothing written.
                return AlertPlan(AlertOutcome.NO_HELPERS, kind, summary=summary)
            person.display_name = sanitize_display_name(ref.display_name)
            person.language = normalize_language(ref.language)
            person.bot_blocked = False
            s.add(person)
            name = person.display_name

            existing = repo.alert_for_update(s, person.id, source_update_id)
            if existing is not None:  # the same Telegram update again (e.g. after a restart)
                return AlertPlan(AlertOutcome.DEDUPED, kind, existing.id, name, summary)

            helpers = repo.active_helpers(s, person.id)
            if not helpers:
                return AlertPlan(AlertOutcome.NO_HELPERS, kind, None, name, summary)

            suppressed: SuppressedReason | None = None
            if kind is AlertKind.NORMAL:
                assert summary is not None
                if repo.recent_alert(
                    s, person.id, kind, now - self._policy.alert_dedupe_window, scam_type=summary.scam_type.value
                ):
                    suppressed = SuppressedReason.DUPLICATE
                elif repo.count_alerts(s, person.id, kind, now - ONE_HOUR) >= self._policy.alert_max_per_hour:
                    suppressed = SuppressedReason.THROTTLED
            elif repo.recent_alert(s, person.id, kind, now - self._policy.emergency_dedupe_window):
                suppressed = SuppressedReason.DUPLICATE  # emergencies skip the hourly throttle entirely

            alert = repo.add_alert(
                s,
                protected_id=person.id,
                now=now,
                kind=kind,
                verdict=summary.verdict.value if summary else None,
                scam_type=summary.scam_type.value if summary else None,
                source_kind=summary.source_kind.value if summary else None,
                source_update_id=source_update_id,
                suppressed_reason=suppressed.value if suppressed else None,
            )
            if suppressed is not None:
                outcome = AlertOutcome.DEDUPED if suppressed is SuppressedReason.DUPLICATE else AlertOutcome.THROTTLED
                return AlertPlan(outcome, kind, alert.id, name, summary)

            deliveries: list[DeliveryPlan] = []
            unreachable: list[str] = []
            for _link, guardian in helpers:
                if guardian.bot_blocked:
                    repo.add_delivery(s, alert_id=alert.id, guardian_id=guardian.id, status=DeliveryStatus.SKIPPED_BLOCKED)
                    unreachable.append(guardian.display_name)
                    continue
                delivery = repo.add_delivery(s, alert_id=alert.id, guardian_id=guardian.id, status=DeliveryStatus.PENDING)
                deliveries.append(
                    DeliveryPlan(delivery.id, guardian.telegram_user_id, guardian.display_name, guardian.language)
                )
            return AlertPlan(
                AlertOutcome.READY, kind, alert.id, name, summary, tuple(deliveries), tuple(unreachable)
            )

    def authorize_delivery(self, delivery_id: int) -> bool:
        """Call immediately before sending. Re-checks, right now, that the link
        is still active and the helper still reachable. False means do not send."""
        with session_scope(self._engine) as s:
            context = repo.delivery_context(s, delivery_id)
            if context is None:
                return False
            delivery, alert, guardian = context
            if delivery.status != DeliveryStatus.PENDING.value:
                return False  # already sent, failed or skipped: never send twice
            if not repo.active_link_exists(s, alert.protected_id, guardian.id):
                delivery.status = DeliveryStatus.SKIPPED_REVOKED.value
                s.add(delivery)
                return False
            if guardian.bot_blocked:
                delivery.status = DeliveryStatus.SKIPPED_BLOCKED.value
                s.add(delivery)
                return False
            return True

    def record_delivery(self, delivery_id: int, status: DeliveryStatus, error_code: int | None = None) -> bool:
        """Record the outcome of a send (SENT or FAILED). Only a pending delivery can be finished."""
        if status not in (DeliveryStatus.SENT, DeliveryStatus.FAILED):
            raise ValueError("a send can only be recorded as SENT or FAILED")
        with session_scope(self._engine) as s:
            return repo.finish_delivery(s, delivery_id=delivery_id, status=status, error_code=error_code, now=self._clock())

    # -- maintenance ---------------------------------------------------------------------------------------------------------

    def maintenance(self) -> tuple[int, int]:
        """Expire stale invites and purge old alerts. Returns (expired links, purged alerts)."""
        now = self._clock()
        with session_scope(self._engine) as s:
            expired = repo.expire_due(s, now)
            purged = repo.purge_alerts_before(s, now - self._policy.alert_retention)
            return expired, purged


class AsyncFamilyService:
    """Runs FamilyService operations in a worker thread, for the async Telegram layer."""

    def __init__(self, service: FamilyService) -> None:
        self._service = service

    @property
    def enabled(self) -> bool:
        return self._service.enabled

    async def upsert_person(self, ref: PersonRef) -> PersonInfo:
        return await asyncio.to_thread(self._service.upsert_person, ref)

    async def create_invite(self, protected: PersonRef) -> InviteResult:
        return await asyncio.to_thread(self._service.create_invite, protected)

    async def open_invite(self, guardian: PersonRef, token: str) -> LinkResult:
        return await asyncio.to_thread(self._service.open_invite, guardian, token)

    async def guardian_respond(self, guardian_telegram_user_id: int, link_ref: str, accept: bool) -> LinkResult:
        return await asyncio.to_thread(self._service.guardian_respond, guardian_telegram_user_id, link_ref, accept)

    async def protected_confirm(self, protected_telegram_user_id: int, link_ref: str, confirm: bool) -> LinkResult:
        return await asyncio.to_thread(self._service.protected_confirm, protected_telegram_user_id, link_ref, confirm)

    async def revoke(self, actor_telegram_user_id: int, link_ref: str) -> LinkResult:
        return await asyncio.to_thread(self._service.revoke, actor_telegram_user_id, link_ref)

    async def delete_person(self, telegram_user_id: int) -> DeletionResult:
        return await asyncio.to_thread(self._service.delete_person, telegram_user_id)

    async def list_helpers(self, protected_telegram_user_id: int) -> tuple[HelperInfo, ...]:
        return await asyncio.to_thread(self._service.list_helpers, protected_telegram_user_id)

    async def list_protected(self, guardian_telegram_user_id: int) -> tuple[HelperInfo, ...]:
        return await asyncio.to_thread(self._service.list_protected, guardian_telegram_user_id)

    async def mark_blocked(self, telegram_user_id: int, blocked: bool) -> BlockResult:
        return await asyncio.to_thread(self._service.mark_blocked, telegram_user_id, blocked)

    async def request_normal_alert(self, person: PersonRef, summary: AlertSummary, source_update_id: int) -> AlertPlan:
        return await asyncio.to_thread(self._service.request_normal_alert, person, summary, source_update_id)

    async def request_emergency_alert(
        self, person: PersonRef, summary: AlertSummary | None, source_update_id: int
    ) -> AlertPlan:
        return await asyncio.to_thread(self._service.request_emergency_alert, person, summary, source_update_id)

    async def authorize_delivery(self, delivery_id: int) -> bool:
        return await asyncio.to_thread(self._service.authorize_delivery, delivery_id)

    async def record_delivery(self, delivery_id: int, status: DeliveryStatus, error_code: int | None = None) -> bool:
        return await asyncio.to_thread(self._service.record_delivery, delivery_id, status, error_code)

    async def maintenance(self) -> tuple[int, int]:
        return await asyncio.to_thread(self._service.maintenance)
