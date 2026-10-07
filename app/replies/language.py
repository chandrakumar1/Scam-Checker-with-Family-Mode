"""Choosing the reply language (English or Hindi).

Only two inputs are used: the script of the text the user sent, and the
language code Telegram reports for their app. Nothing is stored.
"""

from typing import Literal

Language = Literal["en", "hi"]

# Share of letters that must be Devanagari for the text to count as Hindi.
DEVANAGARI_SHARE_THRESHOLD = 0.3

_DEVANAGARI_START, _DEVANAGARI_END = 0x0900, 0x097F
# Danda marks and Devanagari digits are in the block but are not letters.
_NOT_LETTERS = {0x0964, 0x0965, *range(0x0966, 0x0970)}


def normalize_language(language: str | None) -> Language:
    return "hi" if language == "hi" else "en"


def _devanagari_share(text: str) -> float | None:
    """Share of Devanagari among letter-like characters, or None if there are none."""
    devanagari = other = 0
    for ch in text:
        code = ord(ch)
        if _DEVANAGARI_START <= code <= _DEVANAGARI_END:
            if code not in _NOT_LETTERS:
                devanagari += 1  # includes vowel signs, which str.isalpha() rejects
        elif ch.isalpha():
            other += 1
    total = devanagari + other
    return devanagari / total if total else None


def choose_language(text: str | None, language_code: str | None) -> Language:
    """Hindi if the text is mostly Devanagari, else Hindi if the user's Telegram
    language is Hindi, else English. The message the user is looking at wins
    over their app language."""
    if text:
        share = _devanagari_share(text)
        if share is not None and share >= DEVANAGARI_SHARE_THRESHOLD:
            return "hi"

    if language_code:
        primary = language_code.lower().replace("_", "-").split("-")[0]
        if primary == "hi":
            return "hi"

    return "en"
