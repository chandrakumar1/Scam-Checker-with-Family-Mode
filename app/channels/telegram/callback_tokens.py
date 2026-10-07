"""Signed, stateless tokens for the Emergency buttons.

An emergency button carries `em:<action>:<token>`. The token proves, without
storing anything, that Ruko issued this button to THIS user, when, and which
bounded AlertSummary (if any) it belongs to. Users who never link a family
helper therefore leave no stored trace.

Wire format (29 bytes -> 39 URL-safe characters, so the whole callback data is
~44 bytes, under Telegram's 64-byte limit):

    version(1) | user_id(8) | issued_at(4) | flags(1) | verdict(1) | category(1) | source(1) | mac(12)

`mac` is the first 12 bytes of HMAC-SHA256 over everything before it, keyed
with a key derived from the bot token (no extra secret to manage; rotating the
bot token simply invalidates old buttons, which last 24 hours anyway).

Verification order: format, signature (constant-time), version, owner, age.
The summary codes are explicit tables here, not enum order, so reordering an
enum can never silently change what an old token means.
"""

import base64
import hashlib
import hmac
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import SecretStr

from app.family.types import AlertSummary, AlertVerdict, ScamCategory, SourceKind

TOKEN_VERSION = 1
MAC_BYTES = 12
DEFAULT_TTL = timedelta(hours=24)
CLOCK_SKEW = timedelta(seconds=60)

_KEY_LABEL = b"ruko-emergency-callback-v1"
_PAYLOAD = struct.Struct(">BQIBBBB")  # version, user_id, issued_at, flags, verdict, category, source
_RAW_LENGTH = _PAYLOAD.size + MAC_BYTES  # 29
_TOKEN_LENGTH = 39  # base64url of 29 bytes, no padding
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{39}")

_HAS_SUMMARY = 0x01

# Explicit, stable codes. 0 is reserved for "no summary".
_VERDICT_CODES = {AlertVerdict.LIKELY_SCAM: 1, AlertVerdict.SUSPICIOUS: 2}
_CATEGORY_CODES = {
    ScamCategory.OTP_FRAUD: 1, ScamCategory.REMOTE_ACCESS: 2, ScamCategory.MALICIOUS_APK: 3,
    ScamCategory.DIGITAL_ARREST: 4, ScamCategory.IMPERSONATION: 5, ScamCategory.KYC_UPDATE: 6,
    ScamCategory.ELECTRICITY_BILL: 7, ScamCategory.PARCEL_CUSTOMS: 8, ScamCategory.LOTTERY_PRIZE: 9,
    ScamCategory.TASK_JOB: 10, ScamCategory.REFUND: 11, ScamCategory.INVESTMENT: 12, ScamCategory.LOAN: 13,
    ScamCategory.SEXTORTION: 14, ScamCategory.OTHER: 15,
}  # fmt: skip
_SOURCE_CODES = {SourceKind.TEXT: 1, SourceKind.IMAGE: 2}
_VERDICT_BY_CODE = {code: item for item, code in _VERDICT_CODES.items()}
_CATEGORY_BY_CODE = {code: item for item, code in _CATEGORY_CODES.items()}
_SOURCE_BY_CODE = {code: item for item, code in _SOURCE_CODES.items()}


class TokenStatus(StrEnum):
    OK = "ok"
    INVALID = "invalid"  # malformed, tampered, unknown version/codes, or from the future
    WRONG_USER = "wrong_user"  # a genuine token, presented by someone it was not issued to
    EXPIRED = "expired"  # genuine, theirs, but older than the allowed age


@dataclass(frozen=True, slots=True)
class TokenCheck:
    status: TokenStatus
    summary: AlertSummary | None = None  # only meaningful when status is OK
    issued_at: datetime | None = None


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class CallbackTokenCodec:
    def __init__(self, bot_token: SecretStr | str, ttl: timedelta = DEFAULT_TTL) -> None:
        secret = bot_token.get_secret_value() if isinstance(bot_token, SecretStr) else bot_token
        if not secret:
            raise ValueError("a bot token is required to sign callback tokens")
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self._key = hmac.new(secret.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()
        self._ttl = ttl

    def __repr__(self) -> str:
        return f"CallbackTokenCodec(ttl={self._ttl!r}, key=<redacted>)"

    @property
    def ttl(self) -> timedelta:
        return self._ttl

    def _mac(self, payload: bytes) -> bytes:
        return hmac.new(self._key, payload, hashlib.sha256).digest()[:MAC_BYTES]

    def issue(self, user_id: int, summary: AlertSummary | None, now: datetime) -> str:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or not 0 < user_id < 2**63:
            raise ValueError("user_id must be a positive integer")
        if summary is not None and not isinstance(summary, AlertSummary):
            raise TypeError("summary must be an AlertSummary or None")
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        if summary is None:
            flags, verdict, category, source = 0, 0, 0, 0
        else:
            flags = _HAS_SUMMARY
            verdict = _VERDICT_CODES[summary.verdict]
            category = _CATEGORY_CODES[summary.scam_type]
            source = _SOURCE_CODES[summary.source_kind]
        payload = _PAYLOAD.pack(TOKEN_VERSION, user_id, int(now.timestamp()), flags, verdict, category, source)
        return _b64encode(payload + self._mac(payload))

    def verify(self, token: object, user_id: int, now: datetime) -> TokenCheck:
        invalid = TokenCheck(TokenStatus.INVALID)
        # 1. format
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return invalid
        try:
            raw = _b64decode(token)
        except ValueError:
            return invalid
        if len(raw) != _RAW_LENGTH:
            return invalid

        # 2. signature, in constant time, before any field is trusted
        payload, mac = raw[: _PAYLOAD.size], raw[_PAYLOAD.size :]
        if not hmac.compare_digest(self._mac(payload), mac):
            return invalid

        # 3. version and contents
        version, owner, issued, flags, verdict, category, source = _PAYLOAD.unpack(payload)
        if version != TOKEN_VERSION or flags not in (0, _HAS_SUMMARY):
            return invalid
        summary: AlertSummary | None = None
        if flags == _HAS_SUMMARY:
            if verdict not in _VERDICT_BY_CODE or category not in _CATEGORY_BY_CODE or source not in _SOURCE_BY_CODE:
                return invalid
            summary = AlertSummary(_VERDICT_BY_CODE[verdict], _CATEGORY_BY_CODE[category], _SOURCE_BY_CODE[source])
        elif (verdict, category, source) != (0, 0, 0):
            return invalid

        # 4. owner
        if owner != user_id:
            return TokenCheck(TokenStatus.WRONG_USER)

        # 5. age
        issued_at = datetime.fromtimestamp(issued, UTC)
        age = now - issued_at
        if age < -CLOCK_SKEW:
            return invalid  # issued in the future: cannot be genuine
        if age > self._ttl:
            return TokenCheck(TokenStatus.EXPIRED, issued_at=issued_at)
        return TokenCheck(TokenStatus.OK, summary, issued_at)
