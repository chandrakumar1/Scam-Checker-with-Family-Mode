"""Shared helpers for the Family Mode tests. No network, no Telegram."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlmodel import select

from app.db.engine import init_db, make_engine, session_scope
from app.db.models import FamilyAlert, FamilyAlertDelivery, FamilyLink, Person
from app.family.policy import DEFAULT_POLICY, FamilyPolicy
from app.family.service import FamilyService
from app.family.types import (
    AlertSummary,
    AlertVerdict,
    ConsentOutcome,
    PersonRef,
    ScamCategory,
    SourceKind,
)

START = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def person(telegram_user_id: int, name: str = "Priya", language: str | None = "en") -> PersonRef:
    return PersonRef(telegram_user_id, name, language)


PRIYA = person(1001, "Priya", "en")
RAHUL = person(2001, "Rahul", "hi")
ANITA = person(2002, "Anita", "en")
MEERA = person(2003, "Meera", "en")


def make_service(
    *,
    enabled: bool = True,
    policy: FamilyPolicy = DEFAULT_POLICY,
    clock: Clock | None = None,
    engine: Engine | None = None,
) -> tuple[FamilyService, Engine, Clock]:
    engine = engine or make_engine("sqlite://")
    init_db(engine)
    clock = clock or Clock()
    return FamilyService(engine, enabled=enabled, policy=policy, clock=clock), engine, clock


def link_pair(service: FamilyService, protected: PersonRef, guardian: PersonRef) -> str:
    """Run the whole consent flow and return the active link's ref."""
    invite = service.create_invite(protected)
    assert invite.outcome is ConsentOutcome.OK, invite.outcome
    opened = service.open_invite(guardian, invite.token)
    assert opened.outcome is ConsentOutcome.OK, opened.outcome
    accepted = service.guardian_respond(guardian.telegram_user_id, opened.link_ref, True)
    assert accepted.outcome is ConsentOutcome.OK, accepted.outcome
    confirmed = service.protected_confirm(protected.telegram_user_id, opened.link_ref, True)
    assert confirmed.outcome is ConsentOutcome.OK, confirmed.outcome
    return opened.link_ref


def open_invite_for(service: FamilyService, protected: PersonRef, guardian: PersonRef) -> str:
    """Get a link as far as awaiting_guardian and return its ref."""
    invite = service.create_invite(protected)
    opened = service.open_invite(guardian, invite.token)
    assert opened.outcome is ConsentOutcome.OK
    return opened.link_ref


def summary(
    category: ScamCategory = ScamCategory.OTP_FRAUD,
    verdict: AlertVerdict = AlertVerdict.LIKELY_SCAM,
    source: SourceKind = SourceKind.TEXT,
) -> AlertSummary:
    return AlertSummary(verdict, category, source)


# -- direct database inspection (for asserting what is, and is not, stored) -----------------------------


def link_row(engine: Engine, ref: str) -> FamilyLink:
    with session_scope(engine) as s:
        link = s.exec(select(FamilyLink).where(FamilyLink.ref == ref)).one()
        s.expunge(link)
        return link


def all_links(engine: Engine) -> list[FamilyLink]:
    with session_scope(engine) as s:
        rows = list(s.exec(select(FamilyLink)).all())
        for row in rows:
            s.expunge(row)
        return rows


def all_alerts(engine: Engine) -> list[FamilyAlert]:
    with session_scope(engine) as s:
        rows = list(s.exec(select(FamilyAlert).order_by(FamilyAlert.id)).all())
        for row in rows:
            s.expunge(row)
        return rows


def all_deliveries(engine: Engine) -> list[FamilyAlertDelivery]:
    with session_scope(engine) as s:
        rows = list(s.exec(select(FamilyAlertDelivery).order_by(FamilyAlertDelivery.id)).all())
        for row in rows:
            s.expunge(row)
        return rows


def all_people(engine: Engine) -> list[Person]:
    with session_scope(engine) as s:
        rows = list(s.exec(select(Person).order_by(Person.id)).all())
        for row in rows:
            s.expunge(row)
        return rows


def delivery_by_id(engine: Engine, delivery_id: int) -> FamilyAlertDelivery:
    return next(d for d in all_deliveries(engine) if d.id == delivery_id)
