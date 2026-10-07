"""Database tables for Family Mode.

Portability notes (SQLite now, Postgres later):
- Telegram ids are BigInteger (they can exceed 32 bits).
- Enum-like columns are plain bounded strings; validation happens in Python.
- Datetimes are timezone-aware UTC via `UTCDateTime`.
- The partial unique index declares both `sqlite_where` and `postgresql_where`.

Privacy: there is deliberately no free-text column in the alert tables. Every
column is a number, a timestamp, a flag, or a short enum string.
"""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel


class UTCDateTime(TypeDecorator):
    """Always stores UTC and always returns timezone-aware UTC (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class LinkStatus(StrEnum):
    INVITED = "invited"
    AWAITING_GUARDIAN = "awaiting_guardian"
    AWAITING_PROTECTED = "awaiting_protected"
    ACTIVE = "active"
    DECLINED = "declined"
    REVOKED = "revoked"
    EXPIRED = "expired"


# Links that still count towards limits and still block a duplicate pair.
LIVE_LINK_STATUSES = (LinkStatus.AWAITING_GUARDIAN, LinkStatus.AWAITING_PROTECTED, LinkStatus.ACTIVE)
# Links that can still be cancelled or can expire.
OPEN_LINK_STATUSES = (LinkStatus.INVITED, *LIVE_LINK_STATUSES)


class EndedBy(StrEnum):
    PROTECTED = "protected"
    GUARDIAN = "guardian"
    SYSTEM = "system"


class AlertKind(StrEnum):
    NORMAL = "normal"
    EMERGENCY = "emergency"


class SuppressedReason(StrEnum):
    THROTTLED = "throttled"
    DUPLICATE = "duplicate"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED_BLOCKED = "skipped_blocked"
    SKIPPED_REVOKED = "skipped_revoked"


def _in_list(values) -> str:
    return ", ".join(f"'{v.value}'" for v in values)


class Person(SQLModel, table=True):
    __tablename__ = "person"

    id: int | None = Field(default=None, primary_key=True)
    telegram_user_id: int = Field(sa_column=Column(BigInteger, nullable=False, unique=True))
    display_name: str = Field(sa_column=Column(String(40), nullable=False))
    language: str = Field(default="en", sa_column=Column(String(2), nullable=False))
    bot_blocked: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False))
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))


class FamilyLink(SQLModel, table=True):
    __tablename__ = "family_link"
    __table_args__ = (
        CheckConstraint("guardian_id IS NULL OR protected_id != guardian_id", name="ck_family_link_distinct_people"),
        # One live link per pair; ended links (declined/revoked/expired) may repeat.
        Index(
            "uq_family_link_live_pair",
            "protected_id",
            "guardian_id",
            unique=True,
            sqlite_where=text(f"status IN ({_in_list(LIVE_LINK_STATUSES)})"),
            postgresql_where=text(f"status IN ({_in_list(LIVE_LINK_STATUSES)})"),
        ),
        Index("ix_family_link_protected_status", "protected_id", "status"),
        Index("ix_family_link_guardian_status", "guardian_id", "status"),
    )

    id: int | None = Field(default=None, primary_key=True)
    # Random reference used in Telegram button data, so ids are never guessable.
    ref: str = Field(sa_column=Column(String(16), nullable=False, unique=True))
    protected_id: int = Field(sa_column=Column(Integer, ForeignKey("person.id", ondelete="CASCADE"), nullable=False))
    guardian_id: int | None = Field(
        default=None, sa_column=Column(Integer, ForeignKey("person.id", ondelete="CASCADE"), nullable=True)
    )
    status: str = Field(sa_column=Column(String(24), nullable=False))
    # SHA-256 of the invite token; the token itself is never stored. Null once used.
    token_hash: str | None = Field(default=None, sa_column=Column(String(64), nullable=True, unique=True))
    expires_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    guardian_consented_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))
    protected_confirmed_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))
    ended_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))
    ended_by: str | None = Field(default=None, sa_column=Column(String(12), nullable=True))


class FamilyAlert(SQLModel, table=True):
    __tablename__ = "family_alert"
    __table_args__ = (
        UniqueConstraint("protected_id", "source_update_id", name="uq_family_alert_update"),
        Index("ix_family_alert_window", "protected_id", "alert_kind", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True)
    protected_id: int = Field(sa_column=Column(Integer, ForeignKey("person.id", ondelete="CASCADE"), nullable=False))
    created_at: datetime = Field(sa_column=Column(UTCDateTime, nullable=False))
    # Set by the FamilyService operation that created the alert.
    alert_kind: str = Field(sa_column=Column(String(16), nullable=False))
    # The bounded AlertSummary; all null for an emergency raised with /emergency and no check.
    verdict: str | None = Field(default=None, sa_column=Column(String(16), nullable=True))
    scam_type: str | None = Field(default=None, sa_column=Column(String(32), nullable=True))
    source_kind: str | None = Field(default=None, sa_column=Column(String(8), nullable=True))
    source_update_id: int = Field(sa_column=Column(BigInteger, nullable=False))
    suppressed_reason: str | None = Field(default=None, sa_column=Column(String(16), nullable=True))


class FamilyAlertDelivery(SQLModel, table=True):
    __tablename__ = "family_alert_delivery"
    __table_args__ = (UniqueConstraint("alert_id", "guardian_id", name="uq_family_delivery_pair"),)

    id: int | None = Field(default=None, primary_key=True)
    alert_id: int = Field(sa_column=Column(Integer, ForeignKey("family_alert.id", ondelete="CASCADE"), nullable=False))
    guardian_id: int = Field(sa_column=Column(Integer, ForeignKey("person.id", ondelete="CASCADE"), nullable=False))
    status: str = Field(sa_column=Column(String(16), nullable=False))
    error_code: int | None = Field(default=None, sa_column=Column(Integer, nullable=True))
    attempted_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))
    sent_at: datetime | None = Field(default=None, sa_column=Column(UTCDateTime, nullable=True))
