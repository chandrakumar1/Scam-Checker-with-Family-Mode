import dataclasses
from datetime import timedelta

import pytest

from app.family.policy import (
    DEFAULT_POLICY,
    FALLBACK_DISPLAY_NAME,
    MAX_DISPLAY_NAME_CHARS,
    ONE_HOUR,
    FamilyPolicy,
    normalize_language,
    sanitize_display_name,
)

# --- policy values ---------------------------------------------------------------------------------------------


def test_defaults_match_the_approved_plan():
    policy = DEFAULT_POLICY
    assert policy.invite_ttl == timedelta(hours=48)
    assert policy.consent_ttl == timedelta(hours=24)
    assert policy.max_helpers == 5
    assert policy.max_pending_invites == 3
    assert policy.alert_max_per_hour == 5
    assert policy.alert_dedupe_window == timedelta(minutes=30)
    assert policy.alert_retention == timedelta(days=30)
    assert policy.emergency_dedupe_window == timedelta(seconds=120)
    assert ONE_HOUR == timedelta(hours=1)


def test_policy_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_POLICY.max_helpers = 99  # type: ignore[misc]


def test_policy_accepts_overrides():
    policy = FamilyPolicy(max_helpers=2, emergency_dedupe_window=timedelta(seconds=30))
    assert policy.max_helpers == 2 and policy.emergency_dedupe_window == timedelta(seconds=30)
    assert policy.max_pending_invites == 3  # untouched


@pytest.mark.parametrize(
    "field",
    ["invite_ttl", "consent_ttl", "alert_dedupe_window", "alert_retention", "emergency_dedupe_window"],
)
@pytest.mark.parametrize("bad", [timedelta(0), timedelta(seconds=-1)])
def test_durations_must_be_positive(field, bad):
    with pytest.raises(ValueError):
        FamilyPolicy(**{field: bad})


@pytest.mark.parametrize("field", ["max_helpers", "max_pending_invites", "alert_max_per_hour"])
@pytest.mark.parametrize("bad", [0, -3])
def test_limits_must_be_at_least_one(field, bad):
    with pytest.raises(ValueError):
        FamilyPolicy(**{field: bad})


# --- language -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("hi", "hi"), ("HI", "hi"), ("hi-IN", "hi"), ("hi_IN", "hi"), ("en", "en"), ("en-GB", "en"),
     ("fr", "en"), ("hin", "en"), ("", "en"), (None, "en")],
)  # fmt: skip
def test_normalize_language(raw, expected):
    assert normalize_language(raw) == expected


# --- display names (attacker-controlled input) ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Priya", "Priya"),
        ("  Priya   Sharma  ", "Priya Sharma"),
        ("प्रिया शर्मा", "प्रिया शर्मा"),
        ("Rahul 👨‍👩‍👧", "Rahul 👨‍👩‍👧"),  # emoji joined by zero-width joiners stay intact
        ("क्‍ष त्रिपाठी", "क्‍ष त्रिपाठी"),  # ZWJ is part of correct Devanagari spelling
        ("क्‌ष", "क्‌ष"),  # and so is ZWNJ
        ("Priya\nSharma", "Priya Sharma"),
        ("Pri\x00ya", "Pri ya"),
        ('Say "hello"', "Say 'hello'"),
        ("“Quoted” name", "'Quoted' name"),
    ],
)
def test_sanitize_keeps_normal_names(raw, expected):
    assert sanitize_display_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("Click http://evil.example.com/pay now", "evil"),
        ("Bank https://bit.ly/3xYz12 help", "bit.ly"),
        ("Visit www.sbi-kyc.in", "sbi-kyc"),
        ("Join t.me/scamchannel", "t.me"),
        ("evil-bank.xyz/login", "evil-bank"),
        ("tg://resolve?domain=x", "tg://"),
        ("Name‮evil‬", "‮"),  # right-to-left override
        ("Name‮evil‬", "‬"),
        ("a​b", "​"),  # zero-width space
        ("a‎b‏", "‏"),  # left/right marks
        ("x⁦y⁩", "⁦"),  # bidi isolates
        ("﻿name", "﻿"),
        ("na\x1bme", "\x1b"),
    ],
)
def test_sanitize_removes_urls_and_invisible_or_bidi_characters(raw, must_not_contain):
    assert must_not_contain not in sanitize_display_name(raw)


def test_sanitize_caps_the_length():
    out = sanitize_display_name("A" * 500)
    assert len(out) == MAX_DISPLAY_NAME_CHARS == 40
    assert out.endswith("…")
    assert sanitize_display_name("B" * 40) == "B" * 40  # exactly at the limit is untouched
    assert len(sanitize_display_name("B" * 41)) == 40


def test_sanitize_length_fits_the_database_column():
    from app.db.models import Person

    assert len(sanitize_display_name("Z" * 1000)) <= Person.__table__.c.display_name.type.length


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "\n\t", "\x00\x01", "​​", "‍‌", " ‍ ", "http://evil.example", "www.a.com b.org"],
)
def test_sanitize_falls_back_when_nothing_usable_is_left(raw):
    assert sanitize_display_name(raw) == FALLBACK_DISPLAY_NAME


def test_sanitize_output_never_contains_double_quotes_or_newlines():
    out = sanitize_display_name('He said "call\n98765 now" ')
    assert '"' not in out and "\n" not in out


def test_sanitize_is_idempotent():
    for raw in ("Priya", "  A  B  ", 'x "y"', "A" * 100, "evil.com name", "ok‮"):
        once = sanitize_display_name(raw)
        assert sanitize_display_name(once) == once
