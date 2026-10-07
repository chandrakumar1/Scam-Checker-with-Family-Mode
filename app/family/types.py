"""Types for Family Mode.

`AlertSummary` is the ONLY thing that crosses from analysis into this package.
It is bounded by construction: three enum fields, nothing else. No message
text, hash, image, URL, rule name or LLM text can be represented in it.

Normal alerts and emergencies are separate FamilyService operations. The kind
of alert is decided by which operation is called, never by the summary, and an
emergency raised with /emergency passes no summary at all.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.db.models import AlertKind, LinkStatus


class AlertVerdict(StrEnum):
    LIKELY_SCAM = "likely_scam"
    SUSPICIOUS = "suspicious"


class ScamCategory(StrEnum):
    OTP_FRAUD = "otp_fraud"
    REMOTE_ACCESS = "remote_access"
    MALICIOUS_APK = "malicious_apk"
    DIGITAL_ARREST = "digital_arrest"
    IMPERSONATION = "impersonation"
    KYC_UPDATE = "kyc_update"
    ELECTRICITY_BILL = "electricity_bill"
    PARCEL_CUSTOMS = "parcel_customs"
    LOTTERY_PRIZE = "lottery_prize"
    TASK_JOB = "task_job"
    REFUND = "refund"
    INVESTMENT = "investment"
    LOAN = "loan"
    SEXTORTION = "sextortion"
    OTHER = "other"


class SourceKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"


@dataclass(frozen=True, slots=True)
class AlertSummary:
    verdict: AlertVerdict
    scam_type: ScamCategory
    source_kind: SourceKind

    def __post_init__(self) -> None:
        # Real enum members only: a raw string (or rule name, or message text)
        # cannot be smuggled through.
        for name, enum_type in (("verdict", AlertVerdict), ("scam_type", ScamCategory), ("source_kind", SourceKind)):
            if not isinstance(getattr(self, name), enum_type):
                raise TypeError(f"AlertSummary.{name} must be a {enum_type.__name__}")


@dataclass(frozen=True, slots=True)
class PersonRef:
    """Who is acting, as the channel layer knows them. Identity only, no content."""

    telegram_user_id: int
    display_name: str
    language: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.telegram_user_id, bool) or not isinstance(self.telegram_user_id, int) or self.telegram_user_id <= 0:
            raise ValueError("telegram_user_id must be a positive integer")
        if not isinstance(self.display_name, str):
            raise TypeError("display_name must be a string")


@dataclass(frozen=True, slots=True)
class PersonInfo:
    telegram_user_id: int
    display_name: str
    language: str


# --- consent operations -------------------------------------------------------------------------


class ConsentOutcome(StrEnum):
    OK = "ok"
    # Not found, expired, used, wrong user, or wrong state. Deliberately one
    # outcome so callers cannot tell the user (or an attacker) which it was.
    INVALID = "invalid"
    SELF_INVITE = "self_invite"
    ALREADY_LINKED = "already_linked"
    TOO_MANY_PENDING = "too_many_pending"
    TOO_MANY_HELPERS = "too_many_helpers"


@dataclass(frozen=True, slots=True)
class InviteResult:
    outcome: ConsentOutcome
    token: str | None = None  # the plaintext token, returned once and never stored
    link_ref: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class LinkResult:
    outcome: ConsentOutcome
    link_ref: str | None = None
    status: LinkStatus | None = None
    protected: PersonInfo | None = None
    guardian: PersonInfo | None = None


@dataclass(frozen=True, slots=True)
class HelperInfo:
    link_ref: str
    status: LinkStatus
    person: PersonInfo | None  # the other party; None for an invite nobody has opened yet
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DeletionResult:
    deleted: bool
    notify_helpers: tuple[PersonInfo, ...] = ()  # helpers of the deleted person
    notify_protected: tuple[PersonInfo, ...] = ()  # people the deleted person was helping


@dataclass(frozen=True, slots=True)
class BlockResult:
    changed: bool
    notify_protected: tuple[PersonInfo, ...] = ()  # people who lost a reachable helper


# --- alert operations ---------------------------------------------------------------------------


class AlertOutcome(StrEnum):
    READY = "ready"
    NO_HELPERS = "no_helpers"
    DEDUPED = "deduped"
    THROTTLED = "throttled"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    delivery_id: int
    guardian_telegram_user_id: int
    guardian_name: str
    guardian_language: str


@dataclass(frozen=True, slots=True)
class AlertPlan:
    outcome: AlertOutcome
    alert_kind: AlertKind
    alert_id: int | None = None
    protected_name: str = ""
    summary: AlertSummary | None = None
    deliveries: tuple[DeliveryPlan, ...] = ()
    # Helpers skipped at planning time because the bot cannot reach them.
    unreachable_names: tuple[str, ...] = ()
