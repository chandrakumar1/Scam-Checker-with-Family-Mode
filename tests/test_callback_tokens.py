import hmac
import itertools
import struct
from datetime import UTC, datetime, timedelta

import pytest

from app.channels.telegram import callback_tokens as ct
from app.channels.telegram.callback_tokens import (
    CLOCK_SKEW,
    DEFAULT_TTL,
    CallbackTokenCodec,
    TokenStatus,
)
from app.channels.telegram.emergency import MAX_CALLBACK_DATA_BYTES, parse_emergency_data
from app.family.types import AlertSummary, AlertVerdict, ScamCategory, SourceKind

BOT_TOKEN = "123456789:AAEfakeTokenForTestsOnly_0123456789abc"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
USER = 5_000_000_000  # above 32 bits, like real Telegram ids can be
OTHER = 4_000_000_007
SUMMARY = AlertSummary(AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, SourceKind.TEXT)


@pytest.fixture
def codec():
    return CallbackTokenCodec(BOT_TOKEN)


def signed(codec, *, version=1, user=USER, issued=None, flags=1, verdict=1, category=1, source=1) -> str:
    """A correctly SIGNED token with chosen field values (for testing what verification does with them)."""
    issued = int(NOW.timestamp()) if issued is None else issued
    payload = struct.pack(">BQIBBBB", version, user, issued, flags, verdict, category, source)
    return ct._b64encode(payload + codec._mac(payload))


# --- round trip ---------------------------------------------------------------------------------------------------


def test_a_token_carries_the_owner_the_time_and_the_summary(codec):
    token = codec.issue(USER, SUMMARY, NOW)
    check = codec.verify(token, USER, NOW)
    assert check.status is TokenStatus.OK
    assert check.summary == SUMMARY
    assert check.issued_at == NOW


def test_a_token_without_a_summary_round_trips_as_none(codec):
    check = codec.verify(codec.issue(USER, None, NOW), USER, NOW)
    assert check.status is TokenStatus.OK and check.summary is None


@pytest.mark.parametrize(
    ("verdict", "category", "source"), list(itertools.product(AlertVerdict, ScamCategory, SourceKind))
)
def test_every_possible_summary_round_trips(codec, verdict, category, source):
    summary = AlertSummary(verdict, category, source)
    assert codec.verify(codec.issue(USER, summary, NOW), USER, NOW).summary == summary


def test_token_and_callback_data_fit_telegrams_limit(codec):
    token = codec.issue(USER, SUMMARY, NOW)
    assert len(token) == 39 and token.isascii()
    for action in "oyn":
        data = f"em:{action}:{token}"
        assert len(data.encode()) == 44 <= MAX_CALLBACK_DATA_BYTES == 64
        assert parse_emergency_data(data) == (action, token)


def test_a_token_is_url_safe_and_has_no_padding(codec):
    for user in (1, USER, 2**62):
        token = codec.issue(user, SUMMARY, NOW)
        assert all(c.isalnum() or c in "-_" for c in token) and "=" not in token


def test_the_codes_are_explicit_tables_covering_every_enum_member():
    # If someone adds an enum member without a code, tokens for it would crash: fail here instead.
    assert set(ct._VERDICT_CODES) == set(AlertVerdict)
    assert set(ct._CATEGORY_CODES) == set(ScamCategory)
    assert set(ct._SOURCE_CODES) == set(SourceKind)
    for table in (ct._VERDICT_CODES, ct._CATEGORY_CODES, ct._SOURCE_CODES):
        codes = list(table.values())
        assert len(set(codes)) == len(codes) and 0 not in codes  # unique, and 0 is reserved for "no summary"


def test_the_token_is_not_readable_text(codec):
    token = codec.issue(USER, SUMMARY, NOW)
    for fragment in ("otp", "fraud", "likely", "scam", "text", str(USER)):
        assert fragment not in token.lower()


# --- who may use a token ------------------------------------------------------------------------------------------------


def test_another_user_cannot_use_a_genuine_token(codec):
    check = codec.verify(codec.issue(USER, SUMMARY, NOW), OTHER, NOW)
    assert check.status is TokenStatus.WRONG_USER and check.summary is None


def test_the_owner_check_comes_before_the_age_check(codec):
    old = codec.issue(USER, SUMMARY, NOW - timedelta(days=3))
    assert codec.verify(old, OTHER, NOW).status is TokenStatus.WRONG_USER  # not EXPIRED: no hint to strangers
    assert codec.verify(old, USER, NOW).status is TokenStatus.EXPIRED


# --- expiry ---------------------------------------------------------------------------------------------------------------


def test_default_lifetime_is_24_hours(codec):
    assert DEFAULT_TTL == timedelta(hours=24) and codec.ttl == DEFAULT_TTL


@pytest.mark.parametrize(
    ("age", "status"),
    [
        (timedelta(0), TokenStatus.OK),
        (timedelta(hours=1), TokenStatus.OK),
        (timedelta(hours=23, minutes=59), TokenStatus.OK),
        (timedelta(hours=24), TokenStatus.OK),  # exactly at the limit is still valid
        (timedelta(hours=24, seconds=1), TokenStatus.EXPIRED),
        (timedelta(hours=24, minutes=1), TokenStatus.EXPIRED),
        (timedelta(days=30), TokenStatus.EXPIRED),
    ],
)
def test_age_boundaries(codec, age, status):
    token = codec.issue(USER, SUMMARY, NOW)
    assert codec.verify(token, USER, NOW + age).status is status


def test_an_expired_token_still_reports_when_it_was_issued(codec):
    check = codec.verify(codec.issue(USER, SUMMARY, NOW), USER, NOW + timedelta(days=2))
    assert check.status is TokenStatus.EXPIRED and check.issued_at == NOW and check.summary is None


@pytest.mark.parametrize(
    ("ahead", "status"),
    [(timedelta(seconds=30), TokenStatus.OK), (CLOCK_SKEW, TokenStatus.OK),
     (CLOCK_SKEW + timedelta(seconds=1), TokenStatus.INVALID), (timedelta(days=5), TokenStatus.INVALID)],
)  # fmt: skip
def test_a_token_from_the_future_is_only_tolerated_within_clock_skew(codec, ahead, status):
    token = codec.issue(USER, SUMMARY, NOW + ahead)
    assert codec.verify(token, USER, NOW).status is status


def test_the_lifetime_is_configurable():
    short = CallbackTokenCodec(BOT_TOKEN, ttl=timedelta(hours=1))
    token = short.issue(USER, SUMMARY, NOW)
    assert short.verify(token, USER, NOW + timedelta(minutes=59)).status is TokenStatus.OK
    assert short.verify(token, USER, NOW + timedelta(minutes=61)).status is TokenStatus.EXPIRED


# --- tampering and malformed input --------------------------------------------------------------------------------------------


def test_changing_any_single_character_invalidates_the_token(codec):
    token = codec.issue(USER, SUMMARY, NOW)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    for position in range(len(token)):
        for replacement in alphabet:
            if replacement == token[position]:
                continue
            tampered = token[:position] + replacement + token[position + 1 :]
            check = codec.verify(tampered, USER, NOW)
            # (the last base64 character carries unused bits, so a few alterations decode to the same bytes)
            if check.status is TokenStatus.OK:
                assert ct._b64decode(tampered) == ct._b64decode(token)
            else:
                assert check.status is TokenStatus.INVALID, (position, replacement)


def test_tampering_cannot_change_the_owner_or_the_summary(codec):
    token = codec.issue(USER, SUMMARY, NOW)
    raw = bytearray(ct._b64decode(token))
    for index in range(len(raw) - ct.MAC_BYTES):  # flip every payload bit in turn, keep the old signature
        for bit in range(8):
            mutated = bytearray(raw)
            mutated[index] ^= 1 << bit
            assert codec.verify(ct._b64encode(bytes(mutated)), USER, NOW).status is TokenStatus.INVALID
            assert codec.verify(ct._b64encode(bytes(mutated)), OTHER, NOW).status is TokenStatus.INVALID


def test_a_stripped_or_replaced_signature_is_invalid(codec):
    raw = ct._b64decode(codec.issue(USER, SUMMARY, NOW))
    assert codec.verify(ct._b64encode(raw[: -ct.MAC_BYTES] + b"\x00" * ct.MAC_BYTES), USER, NOW).status is TokenStatus.INVALID
    assert codec.verify(ct._b64encode(raw[: -ct.MAC_BYTES]), USER, NOW).status is TokenStatus.INVALID


@pytest.mark.parametrize(
    "token",
    [None, "", "x", "a" * 38, "a" * 40, "a" * 39, "!" * 39, "é" * 39, " " * 39, 12345, b"a" * 39, ["a" * 39], "A" * 39 + "\n"],
)
def test_malformed_tokens_are_invalid(codec, token):
    assert codec.verify(token, USER, NOW).status is TokenStatus.INVALID


def test_a_token_from_another_bot_is_invalid(codec):
    other_bot = CallbackTokenCodec("987654321:AAOtherBotTokenOnly_zyxwvutsrqponmlkjih")
    assert codec.verify(other_bot.issue(USER, SUMMARY, NOW), USER, NOW).status is TokenStatus.INVALID


def test_two_codecs_with_the_same_bot_token_agree():
    a, b = CallbackTokenCodec(BOT_TOKEN), CallbackTokenCodec(BOT_TOKEN)
    assert b.verify(a.issue(USER, SUMMARY, NOW), USER, NOW).status is TokenStatus.OK


def test_the_key_is_derived_not_the_bot_token_itself(codec):
    assert codec._key != BOT_TOKEN.encode() and len(codec._key) == 32
    assert BOT_TOKEN not in repr(codec) and "redacted" in repr(codec)


# --- correctly signed but nonsensical contents ------------------------------------------------------------------------------


def test_a_signed_token_with_an_unknown_version_is_invalid(codec):
    assert codec.verify(signed(codec, version=1), USER, NOW).status is TokenStatus.OK  # control
    assert codec.verify(signed(codec, version=2), USER, NOW).status is TokenStatus.INVALID
    assert codec.verify(signed(codec, version=0), USER, NOW).status is TokenStatus.INVALID


@pytest.mark.parametrize(
    "fields",
    [
        {"flags": 2}, {"flags": 3}, {"flags": 255},  # unknown flags
        {"verdict": 0}, {"verdict": 3}, {"verdict": 99},  # has-summary but a bad verdict code
        {"category": 0}, {"category": 16}, {"category": 255},
        {"source": 0}, {"source": 3},
        {"flags": 0, "verdict": 1}, {"flags": 0, "category": 1}, {"flags": 0, "source": 1},  # no summary, yet codes set
    ],
)
def test_inconsistent_signed_contents_are_invalid(codec, fields):
    assert codec.verify(signed(codec, **fields), USER, NOW).status is TokenStatus.INVALID


def test_a_signed_token_with_no_summary_and_zero_codes_is_valid(codec):
    check = codec.verify(signed(codec, flags=0, verdict=0, category=0, source=0), USER, NOW)
    assert check.status is TokenStatus.OK and check.summary is None


# --- the signature check really is constant-time ---------------------------------------------------------------------------------


def test_signatures_are_compared_with_compare_digest(codec, monkeypatch):
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(ct.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    codec.verify(codec.issue(USER, SUMMARY, NOW), USER, NOW)
    assert calls == [1]


# --- issuing ------------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad_id", [0, -1, True, "123", 1.5, None, 2**63])
def test_issue_rejects_bad_user_ids(codec, bad_id):
    with pytest.raises(ValueError):
        codec.issue(bad_id, SUMMARY, NOW)


@pytest.mark.parametrize("bad", ["otp_fraud", {"verdict": "likely_scam"}, 5, ("likely_scam",)])
def test_issue_accepts_only_an_alert_summary_or_none(codec, bad):
    with pytest.raises(TypeError):
        codec.issue(USER, bad, NOW)


def test_issue_needs_an_aware_time(codec):
    with pytest.raises(ValueError):
        codec.issue(USER, SUMMARY, datetime(2026, 10, 7, 12, 0))


@pytest.mark.parametrize("bad", ["", None])
def test_a_bot_token_is_required(bad):
    with pytest.raises(ValueError):
        CallbackTokenCodec(bad)


def test_the_lifetime_must_be_positive():
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError):
            CallbackTokenCodec(BOT_TOKEN, ttl=bad)


def test_a_secretstr_bot_token_works():
    from pydantic import SecretStr

    assert CallbackTokenCodec(SecretStr(BOT_TOKEN)).verify(
        CallbackTokenCodec(BOT_TOKEN).issue(USER, SUMMARY, NOW), USER, NOW
    ).status is TokenStatus.OK


# --- the button data parser -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [None, 5, "", "em", "em:", "em:y", "em:y:", "em:z:" + "a" * 39, "em:Y:" + "a" * 39, "EM:y:" + "a" * 39, "fam:y:" + "a" * 39,
     "em:y:" + "a" * 38, "em:y:" + "a" * 40, "em:y:" + "!" * 39, "em:y:" + "a" * 39 + ":x", "em:y:" + "a" * 39 + "\n", "em:y:" + "é" * 39],
)  # fmt: skip
def test_malformed_emergency_data_is_rejected(data):
    assert parse_emergency_data(data) is None
