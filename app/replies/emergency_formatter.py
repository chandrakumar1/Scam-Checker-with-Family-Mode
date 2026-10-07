"""Pure text builders for the Emergency flow and for family alerts.

No Telegram, no database. The functions that build a message TO A HELPER
accept only a name and a bounded AlertSummary (or None): there is no parameter
through which message text, a rule name, LLM text or a URL could enter. The
name is cleaned here before use.
"""

from collections.abc import Sequence

from app.family.types import AlertSummary
from app.replies import emergency_templates as ET
from app.replies import templates as T
from app.replies.family_formatter import clean_name
from app.replies.formatter import TELEGRAM_MAX_CHARS
from app.replies.language import Language, normalize_language

MAX_NAMES_SHOWN = 5  # a person can have at most this many helpers anyway


def _with_footer(body: str, language: Language) -> str:
    footer = T.FOOTER[language]
    room = TELEGRAM_MAX_CHARS - len(footer) - 2
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return f"{body}\n\n{footer}"


def _cap(body: str) -> str:
    return body if len(body) <= TELEGRAM_MAX_CHARS else body[: TELEGRAM_MAX_CHARS - 1].rstrip() + "…"


def _quoted(names: Sequence[str], language: Language) -> str:
    return ", ".join(f"“{clean_name(name, language)}”" for name in names[:MAX_NAMES_SHOWN])


def emergency_message(key: str, language: str | None = "en", **values: object) -> str:
    """A message to the protected person. `names`, `reached` and `failed` take
    lists of display names; they are cleaned and quoted here."""
    lang = normalize_language(language)
    if key not in ET.MESSAGES:
        raise ValueError(f"unknown emergency message {key!r}")
    text = ET.MESSAGES[key][lang]
    filled: dict[str, str] = {}
    for field, raw in values.items():
        if field not in ("names", "reached", "failed"):
            raise TypeError(f"unknown placeholder {field!r}")
        filled[field] = _quoted(list(raw), lang)  # type: ignore[call-overload]
    text = text.format(**filled)
    return _cap(text) if key in ET.NO_FOOTER else _with_footer(text, lang)


def guidance_message(language: str | None = "en") -> str:
    """The static 1930 steps: sent first, needs nothing but Telegram."""
    return emergency_message("guidance", language)


def question_message(language: str | None = "en") -> str:
    lang = normalize_language(language)
    return _with_footer(ET.QUESTION[lang], lang)


def button_label(key: str, language: str | None = "en") -> str:
    return ET.BUTTONS[key][normalize_language(language)]


def popup(key: str, language: str | None = "en") -> str:
    return ET.POPUPS[key][normalize_language(language)]


def category_phrase(summary: AlertSummary, language: str | None = "en") -> str:
    return ET.CATEGORY_PHRASES[summary.scam_type.value][normalize_language(language)]


def told_helpers_notice(names: Sequence[str], language: str | None = "en") -> str:
    """The line added to a scam-check reply when helpers are being alerted."""
    return emergency_message("told_notice", language, names=list(names))


def normal_alert_message(protected_name: str, summary: AlertSummary, language: str | None = "en") -> str:
    """Normal alert to a helper: fixed wording, the person's name, one category phrase."""
    lang = normalize_language(language)
    text = ET.MESSAGES["normal_alert"][lang].format(
        name=clean_name(protected_name, lang), phrase=category_phrase(summary, lang)
    )
    return _with_footer(text, lang)


def emergency_alert_message(protected_name: str, summary: AlertSummary | None, language: str | None = "en") -> str:
    """Emergency alert to a helper. With no summary (/emergency with no check
    behind it) it says nothing at all about the kind of scam."""
    lang = normalize_language(language)
    name = clean_name(protected_name, lang)
    if summary is None:
        return _cap(ET.MESSAGES["emergency_alert"][lang].format(name=name))
    return _cap(ET.MESSAGES["emergency_alert_typed"][lang].format(name=name, phrase=category_phrase(summary, lang)))
