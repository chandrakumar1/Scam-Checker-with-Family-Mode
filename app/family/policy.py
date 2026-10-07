"""Limits, windows and input hygiene for Family Mode.

Values arrive as constructor arguments (from settings, in a later stage), so
this package never imports app.config.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

MAX_DISPLAY_NAME_CHARS = 40
FALLBACK_DISPLAY_NAME = "Family member"

# C0/C1 control characters become a space (they separate words).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
# Invisible and bidirectional-control characters are deleted outright. The
# zero-width joiner and non-joiner (U+200C, U+200D) are deliberately KEPT: they
# are part of correct spelling in Devanagari and other Indic scripts, and of
# emoji sequences.
_INVISIBLE = re.compile(r"[​‎‏‪-‮⁠-⁤⁦-⁩﻿]+")
_NOT_VISIBLE_CATEGORIES = {"Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp", "Zs"}
_URL = re.compile(
    r"(?i)(?:https?://|ftp://|www\.|t\.me/|tg://)\S+"
    r"|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?"
)
_QUOTES = str.maketrans({'"': "'", "“": "'", "”": "'", "„": "'"})


@dataclass(frozen=True, slots=True)
class FamilyPolicy:
    invite_ttl: timedelta = timedelta(hours=48)
    consent_ttl: timedelta = timedelta(hours=24)
    max_helpers: int = 5
    max_pending_invites: int = 3
    alert_max_per_hour: int = 5
    alert_dedupe_window: timedelta = timedelta(minutes=30)
    alert_retention: timedelta = timedelta(days=30)
    emergency_dedupe_window: timedelta = timedelta(seconds=120)

    def __post_init__(self) -> None:
        for name in ("invite_ttl", "consent_ttl", "alert_dedupe_window", "alert_retention", "emergency_dedupe_window"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        for name in ("max_helpers", "max_pending_invites", "alert_max_per_hour"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


DEFAULT_POLICY = FamilyPolicy()

ONE_HOUR = timedelta(hours=1)


def normalize_language(language: str | None) -> str:
    """Only English and Hindi are supported; anything else is English."""
    if language:
        primary = language.lower().replace("_", "-").split("-")[0]
        if primary == "hi":
            return "hi"
    return "en"


def sanitize_display_name(raw: str | None) -> str:
    """A name safe to show to another person.

    Names come from Telegram profiles, so they are attacker-controlled: strip
    control and bidi characters, remove anything that looks like a URL, turn
    double quotes into single ones (names are shown inside quotes), collapse
    whitespace and cap the length.
    """
    text = _CONTROL.sub(" ", raw or "")
    text = _INVISIBLE.sub("", text)
    text = _URL.sub(" ", text)
    text = text.translate(_QUOTES)
    text = re.sub(r"\s+", " ", text).strip()
    if not any(unicodedata.category(ch) not in _NOT_VISIBLE_CATEGORIES for ch in text):
        return FALLBACK_DISPLAY_NAME  # empty, or only joiners/invisible characters
    if len(text) > MAX_DISPLAY_NAME_CHARS:
        text = text[: MAX_DISPLAY_NAME_CHARS - 1].rstrip() + "…"
    return text
