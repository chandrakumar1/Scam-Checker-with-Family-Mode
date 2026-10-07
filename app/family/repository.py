"""Queries and atomic transitions for Family Mode.

Plain functions that take a Session, so FamilyService decides where each
transaction starts and ends. No raw SQL: everything is expressed through
SQLModel/SQLAlchemy so it ports from SQLite to Postgres unchanged.

State changes on links are single `UPDATE ... WHERE <expected state>`
statements whose row count is checked, so two racing requests have exactly one
winner. Callers must treat any ORM objects loaded before such an update as
stale.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, or_, update
from sqlmodel import Session, col, func, select

from app.db.models import (
    LIVE_LINK_STATUSES,
    AlertKind,
    DeliveryStatus,
    EndedBy,
    FamilyAlert,
    FamilyAlertDelivery,
    FamilyLink,
    LinkStatus,
    Person,
)

# --- people ----------------------------------------------------------------------------------------


def person_by_telegram_id(s: Session, telegram_user_id: int, *, lock: bool = False) -> Person | None:
    statement = select(Person).where(Person.telegram_user_id == telegram_user_id)
    if lock:
        # Row lock on Postgres; SQLite ignores it (BEGIN IMMEDIATE serialises writers there).
        statement = statement.with_for_update()
    return s.exec(statement).first()


def person_by_id(s: Session, person_id: int) -> Person | None:
    return s.get(Person, person_id)


def add_person(s: Session, *, telegram_user_id: int, display_name: str, language: str, now: datetime) -> Person:
    # On Postgres a concurrent first-time insert would need INSERT .. ON CONFLICT;
    # on SQLite writers are serialised, so check-then-insert is safe.
    person = Person(
        telegram_user_id=telegram_user_id, display_name=display_name, language=language, bot_blocked=False, created_at=now
    )
    s.add(person)
    s.flush()
    return person


# --- links -------------------------------------------------------------------------------------------


def link_by_ref(s: Session, ref: str) -> FamilyLink | None:
    return s.exec(select(FamilyLink).where(FamilyLink.ref == ref)).first()


def link_by_token_hash(s: Session, token_hash: str) -> FamilyLink | None:
    return s.exec(select(FamilyLink).where(FamilyLink.token_hash == token_hash)).first()


def add_link(
    s: Session,
    *,
    ref: str,
    protected_id: int,
    token_hash: str,
    expires_at: datetime,
    now: datetime,
) -> FamilyLink:
    link = FamilyLink(
        ref=ref,
        protected_id=protected_id,
        guardian_id=None,
        status=LinkStatus.INVITED.value,
        token_hash=token_hash,
        expires_at=expires_at,
        created_at=now,
    )
    s.add(link)
    s.flush()
    return link


def count_links(
    s: Session,
    *,
    protected_id: int,
    statuses: Sequence[LinkStatus],
    unexpired_at: datetime | None = None,
) -> int:
    statement = (
        select(func.count())
        .select_from(FamilyLink)
        .where(FamilyLink.protected_id == protected_id, col(FamilyLink.status).in_([x.value for x in statuses]))
    )
    if unexpired_at is not None:
        statement = statement.where(col(FamilyLink.expires_at) > unexpired_at)
    return s.exec(statement).one()


def live_pair_exists(s: Session, protected_id: int, guardian_id: int) -> bool:
    statement = select(func.count()).select_from(FamilyLink).where(
        FamilyLink.protected_id == protected_id,
        FamilyLink.guardian_id == guardian_id,
        col(FamilyLink.status).in_([x.value for x in LIVE_LINK_STATUSES]),
    )
    return s.exec(statement).one() > 0


def active_link_exists(s: Session, protected_id: int, guardian_id: int) -> bool:
    statement = select(func.count()).select_from(FamilyLink).where(
        FamilyLink.protected_id == protected_id,
        FamilyLink.guardian_id == guardian_id,
        FamilyLink.status == LinkStatus.ACTIVE.value,
    )
    return s.exec(statement).one() > 0


def transition_link(
    s: Session,
    *,
    ref: str,
    from_statuses: Sequence[LinkStatus],
    to_status: LinkStatus,
    values: dict[str, Any] | None = None,
    extra_where: Sequence[Any] = (),
) -> bool:
    """Atomically move a link between states. True only if exactly one row matched."""
    statement = (
        update(FamilyLink)
        .where(
            FamilyLink.ref == ref,
            col(FamilyLink.status).in_([x.value for x in from_statuses]),
            *extra_where,
        )
        .values(status=to_status.value, **(values or {}))
        .execution_options(synchronize_session=False)
    )
    matched = s.exec(statement).rowcount == 1
    s.expire_all()  # ORM copies loaded before the UPDATE are now stale
    return matched


def links_for_protected(
    s: Session, protected_id: int, statuses: Sequence[LinkStatus], *, now: datetime
) -> list[tuple[FamilyLink, Person | None]]:
    """Links where this person is the protected one, with the helper (if any).
    An invite nobody has opened only counts while it has not expired."""
    statement = (
        select(FamilyLink, Person)
        .join(Person, Person.id == FamilyLink.guardian_id, isouter=True)
        .where(
            FamilyLink.protected_id == protected_id,
            col(FamilyLink.status).in_([x.value for x in statuses]),
            or_(FamilyLink.status != LinkStatus.INVITED.value, col(FamilyLink.expires_at) > now),
        )
        .order_by(col(FamilyLink.created_at), col(FamilyLink.id))
    )
    return list(s.exec(statement).all())


def links_for_guardian(
    s: Session, guardian_id: int, statuses: Sequence[LinkStatus]
) -> list[tuple[FamilyLink, Person]]:
    """Links where this person is the helper, with the protected person."""
    statement = (
        select(FamilyLink, Person)
        .join(Person, Person.id == FamilyLink.protected_id)
        .where(FamilyLink.guardian_id == guardian_id, col(FamilyLink.status).in_([x.value for x in statuses]))
        .order_by(col(FamilyLink.created_at), col(FamilyLink.id))
    )
    return list(s.exec(statement).all())


def active_helpers(s: Session, protected_id: int) -> list[tuple[FamilyLink, Person]]:
    statement = (
        select(FamilyLink, Person)
        .join(Person, Person.id == FamilyLink.guardian_id)
        .where(FamilyLink.protected_id == protected_id, FamilyLink.status == LinkStatus.ACTIVE.value)
        .order_by(col(FamilyLink.id))
    )
    return list(s.exec(statement).all())


# --- alerts ---------------------------------------------------------------------------------------------


def alert_for_update(s: Session, protected_id: int, source_update_id: int) -> FamilyAlert | None:
    return s.exec(
        select(FamilyAlert).where(
            FamilyAlert.protected_id == protected_id, FamilyAlert.source_update_id == source_update_id
        )
    ).first()


def recent_alert(
    s: Session,
    protected_id: int,
    kind: AlertKind,
    since: datetime,
    *,
    scam_type: str | None = None,
) -> FamilyAlert | None:
    """The most recent alert that really went out (not a suppressed one) since `since`."""
    statement = select(FamilyAlert).where(
        FamilyAlert.protected_id == protected_id,
        FamilyAlert.alert_kind == kind.value,
        col(FamilyAlert.suppressed_reason).is_(None),
        col(FamilyAlert.created_at) > since,
    )
    if scam_type is not None:
        statement = statement.where(FamilyAlert.scam_type == scam_type)
    return s.exec(statement.order_by(col(FamilyAlert.created_at).desc())).first()


def count_alerts(s: Session, protected_id: int, kind: AlertKind, since: datetime) -> int:
    statement = (
        select(func.count())
        .select_from(FamilyAlert)
        .where(
            FamilyAlert.protected_id == protected_id,
            FamilyAlert.alert_kind == kind.value,
            col(FamilyAlert.suppressed_reason).is_(None),
            col(FamilyAlert.created_at) > since,
        )
    )
    return s.exec(statement).one()


def add_alert(
    s: Session,
    *,
    protected_id: int,
    now: datetime,
    kind: AlertKind,
    verdict: str | None,
    scam_type: str | None,
    source_kind: str | None,
    source_update_id: int,
    suppressed_reason: str | None,
) -> FamilyAlert:
    alert = FamilyAlert(
        protected_id=protected_id,
        created_at=now,
        alert_kind=kind.value,
        verdict=verdict,
        scam_type=scam_type,
        source_kind=source_kind,
        source_update_id=source_update_id,
        suppressed_reason=suppressed_reason,
    )
    s.add(alert)
    s.flush()
    return alert


def add_delivery(s: Session, *, alert_id: int, guardian_id: int, status: DeliveryStatus) -> FamilyAlertDelivery:
    delivery = FamilyAlertDelivery(alert_id=alert_id, guardian_id=guardian_id, status=status.value)
    s.add(delivery)
    s.flush()
    return delivery


def delivery_context(s: Session, delivery_id: int) -> tuple[FamilyAlertDelivery, FamilyAlert, Person] | None:
    statement = (
        select(FamilyAlertDelivery, FamilyAlert, Person)
        .join(FamilyAlert, FamilyAlert.id == FamilyAlertDelivery.alert_id)
        .join(Person, Person.id == FamilyAlertDelivery.guardian_id)
        .where(FamilyAlertDelivery.id == delivery_id)
    )
    return s.exec(statement).first()


def finish_delivery(
    s: Session, *, delivery_id: int, status: DeliveryStatus, error_code: int | None, now: datetime
) -> bool:
    """Record the outcome of a send. Only a still-pending delivery can be finished."""
    statement = (
        update(FamilyAlertDelivery)
        .where(FamilyAlertDelivery.id == delivery_id, FamilyAlertDelivery.status == DeliveryStatus.PENDING.value)
        .values(
            status=status.value,
            error_code=error_code,
            attempted_at=now,
            sent_at=now if status is DeliveryStatus.SENT else None,
        )
        .execution_options(synchronize_session=False)
    )
    matched = s.exec(statement).rowcount == 1
    s.expire_all()
    return matched


# --- maintenance and deletion -------------------------------------------------------------------------------


def expire_due(s: Session, now: datetime) -> int:
    """Expire invites and consent steps whose time has run out."""
    statement = (
        update(FamilyLink)
        .where(
            col(FamilyLink.status).in_(
                [LinkStatus.INVITED.value, LinkStatus.AWAITING_GUARDIAN.value, LinkStatus.AWAITING_PROTECTED.value]
            ),
            col(FamilyLink.expires_at) <= now,
        )
        .values(status=LinkStatus.EXPIRED.value, ended_at=now, ended_by=EndedBy.SYSTEM.value, token_hash=None)
        .execution_options(synchronize_session=False)
    )
    expired = s.exec(statement).rowcount
    s.expire_all()
    return expired


def purge_alerts_before(s: Session, cutoff: datetime) -> int:
    old_alerts = select(FamilyAlert.id).where(col(FamilyAlert.created_at) < cutoff)
    s.exec(delete(FamilyAlertDelivery).where(col(FamilyAlertDelivery.alert_id).in_(old_alerts)))
    purged = s.exec(delete(FamilyAlert).where(col(FamilyAlert.created_at) < cutoff)).rowcount
    s.expire_all()
    return purged


def delete_person_data(s: Session, person_id: int) -> None:
    """Remove a person and everything that refers to them. Done explicitly, so
    it does not depend on the database enforcing ON DELETE CASCADE."""
    own_alerts = select(FamilyAlert.id).where(FamilyAlert.protected_id == person_id)
    s.exec(
        delete(FamilyAlertDelivery).where(
            or_(col(FamilyAlertDelivery.alert_id).in_(own_alerts), FamilyAlertDelivery.guardian_id == person_id)
        )
    )
    s.exec(delete(FamilyAlert).where(FamilyAlert.protected_id == person_id))
    s.exec(delete(FamilyLink).where(or_(FamilyLink.protected_id == person_id, FamilyLink.guardian_id == person_id)))
    s.exec(delete(Person).where(Person.id == person_id))
    s.expire_all()
