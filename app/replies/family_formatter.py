"""Pure text builders for the Family Mode flows.

No Telegram, no database. Every message ends with the standard 1930 footer.
Everything that can come from another person (names, @usernames) is cleaned
here before it is put into a message, and the one URL we ever embed (the
invitation deep link) must match its exact expected shape or it is refused.
"""

import re
import string
from collections.abc import Sequence

from app.family.types import HelperInfo
from app.replies import family_templates as FT
from app.replies import templates as T
from app.replies.formatter import TELEGRAM_MAX_CHARS, clean_text
from app.replies.language import Language, normalize_language

MAX_NAME_CHARS = 40

_USERNAME = re.compile(r"[A-Za-z0-9_]{1,32}")
_DEEP_LINK = re.compile(r"https://t\.me/[A-Za-z0-9_]{5,32}\?start=fam_[A-Za-z0-9_-]{22}")

# The placeholders each message needs, taken from its English template.
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    key: frozenset(name for _, name, _, _ in string.Formatter().parse(by_lang["en"]) if name)
    for key, by_lang in FT.MESSAGES.items()
}


def _with_footer(body: str, language: Language) -> str:
    footer = T.FOOTER[language]
    room = TELEGRAM_MAX_CHARS - len(footer) - 2
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return f"{body}\n\n{footer}"


def clean_name(raw: object, language: Language) -> str:
    """A name safe to show another person. URLs are dropped silently (a name
    should never read "[link removed]"), and an empty result falls back to a neutral name."""
    text = clean_text(None if raw is None else str(raw), 10_000, language)
    text = re.sub(r"\s+", " ", text.replace(T.LINK_REMOVED[language], " ")).strip()
    if len(text) > MAX_NAME_CHARS:
        text = text[: MAX_NAME_CHARS - 1].rstrip() + "…"
    return text or FT.FALLBACK_NAME[language]


_clean_name = clean_name  # the original private name, still used throughout this module


def _format_hours(hours: float) -> str:
    return str(int(hours)) if float(hours).is_integer() else f"{hours:.1f}"


def _clean_values(values: dict[str, object], language: Language) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for field, raw in values.items():
        if field == "name":
            cleaned[field] = _clean_name(raw, language)
        elif field == "handle":
            # An @username is shown only if it is a well-formed Telegram username.
            text = "" if raw is None else str(raw).removeprefix("@")
            cleaned[field] = f" (@{text})" if _USERNAME.fullmatch(text) else ""
        elif field == "n":
            cleaned[field] = str(int(raw))  # type: ignore[call-overload]
        elif field == "hours":
            cleaned[field] = _format_hours(float(raw))  # type: ignore[arg-type]
        elif field == "link":
            if not isinstance(raw, str) or not _DEEP_LINK.fullmatch(raw):
                raise ValueError("link must be a well-formed Ruko invitation link")
            cleaned[field] = raw
        else:
            raise TypeError(f"unknown placeholder {field!r}")
    return cleaned


def family_message(key: str, language: str | None = "en", **values: object) -> str:
    """A complete message, filled in, cleaned, and ending with the 1930 footer."""
    lang = normalize_language(language)
    if key not in FT.MESSAGES:
        raise ValueError(f"unknown message {key!r}")
    if set(values) != REQUIRED_FIELDS[key]:
        raise TypeError(f"{key} needs exactly {sorted(REQUIRED_FIELDS[key])}, got {sorted(values)}")
    return _with_footer(FT.MESSAGES[key][lang].format(**_clean_values(values, lang)), lang)


def popup(key: str, language: str | None = "en") -> str:
    """A short pop-up over a pressed button. No footer: it has to stay tiny."""
    return FT.POPUPS[key][normalize_language(language)]


def button_label(key: str, language: str | None = "en", **values: object) -> str:
    lang = normalize_language(language)
    template = FT.BUTTONS[key][lang]
    needs_name = "{name}" in template
    if needs_name != ("name" in values) or set(values) - {"name"}:
        raise TypeError(f"button {key!r} takes {'only name' if needs_name else 'no values'}")
    return template.format(**_clean_values(values, lang)) if needs_name else template


def family_error_message(language: str | None = "en") -> str:
    return family_message("error", language)


def family_menu(
    helpers: Sequence[HelperInfo], protected: Sequence[HelperInfo], language: str | None = "en"
) -> str:
    """The /family screen: who helps you, and who you help."""
    lang = normalize_language(language)
    parts = [FT.MESSAGES["menu_intro"][lang]]

    if helpers:
        lines = []
        for helper in helpers:
            status = FT.STATUS_WORDS[helper.status.value][lang]
            who = f"“{_clean_name(helper.person.display_name, lang)}”" if helper.person else FT.INVITATION_LABEL[lang]
            lines.append(f"• {who} — {status}")
        parts.append(FT.MESSAGES["menu_helpers_title"][lang] + "\n" + "\n".join(lines))
    else:
        parts.append(FT.MESSAGES["menu_no_helpers"][lang])

    if protected:
        lines = [f"• “{_clean_name(p.person.display_name, lang)}”" for p in protected if p.person]
        parts.append(
            FT.MESSAGES["menu_helping_title"][lang] + "\n" + "\n".join(lines) + "\n" + FT.MESSAGES["menu_helping_hint"][lang]
        )

    return _with_footer("\n\n".join(parts), lang)
