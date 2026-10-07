"""Turn analyzer results into Telegram-ready reply text.

Replies are plain text (no parse_mode), so there is nothing to escape. Every
reply ends with the 1930 safety footer. LLM output is derived from untrusted
messages, so it is cleaned before use: control characters removed, length
capped, and URLs replaced (Telegram would make them tappable).
"""

import re

from app.analyzer.combine import AnalysisResult, Verdict
from app.replies import templates as T
from app.replies.language import Language, normalize_language

TELEGRAM_MAX_CHARS = 4096
MAX_EXPLANATION_CHARS = 300
MAX_STEP_CHARS = 160
MAX_STEPS = 3
MAX_RULE_REASONS = 3

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁠﻿]+")
_URL = re.compile(
    r"(?i)(?:https?://|ftp://|www\.|t\.me/|tg://)\S+"
    r"|\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?"
)
_TRAILING_PUNCT = ".,;:!?)]}\"'"
# Our own helpline website may appear in LLM text and is safe to keep.
_ALLOWED_DOMAINS = {"cybercrime.gov.in"}

# An explanation for the "no obvious red flags" verdict must not claim safety.
_SAFETY_CLAIM = re.compile(
    r"(?i)\b(?:safe|genuine|legit|legitimate|authentic|verified|trustworthy)\b"
    r"|सुरक्षित|भरोसेमंद|विश्वसनीय|प्रामाणिक|असली"
)


def clean_text(text: str | None, max_chars: int, language: Language = "en") -> str:
    """Make untrusted text safe to show: no control chars, no URLs, bounded length."""
    if not text:
        return ""

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        stripped = raw.rstrip(_TRAILING_PUNCT)
        trailing = raw[len(stripped):]
        if stripped.lower() in _ALLOWED_DOMAINS:
            return raw
        return T.LINK_REMOVED[language] + trailing

    text = _CONTROL_CHARS.sub(" ", text)
    text = _URL.sub(replace_url, text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _bullets(lines: list[str]) -> str:
    return "\n".join(f"• {line}" for line in lines)


def _assemble(parts: list[str], language: Language) -> str:
    footer = T.FOOTER[language]
    body = "\n\n".join(part for part in parts if part)
    room = TELEGRAM_MAX_CHARS - len(footer) - 2
    if len(body) > room:
        body = body[: room - 1].rstrip() + "…"
    return f"{body}\n\n{footer}"


def _reasons_and_steps(result: AnalysisResult, language: Language) -> tuple[list[str], list[str]]:
    verdict = result.verdict

    # The LLM's words are only shown when they match the final verdict.
    # Otherwise (LLM failed, or the rules raised the verdict) they could
    # contradict the headline, e.g. "looks fine" under "this is a SCAM".
    llm_agrees = not result.llm_failed and result.llm_verdict == verdict
    explanation = clean_text(result.explanation, MAX_EXPLANATION_CHARS, language) if llm_agrees else ""
    if verdict is Verdict.NO_OBVIOUS_RED_FLAGS and _SAFETY_CLAIM.search(explanation):
        explanation = ""

    if explanation:
        reasons = [explanation]
        steps = [clean_text(step, MAX_STEP_CHARS, language) for step in result.what_to_do[:MAX_STEPS]]
        steps = [step for step in steps if step]
    else:
        reasons = [
            T.RULE_REASONS[hit.rule][language]
            for hit in result.rule_hits[:MAX_RULE_REASONS]
            if hit.rule in T.RULE_REASONS
        ]
        if not reasons and result.llm_failed:
            reasons = [T.LLM_UNAVAILABLE[language]]
        steps = []

    return reasons, steps or list(T.GENERIC_STEPS[verdict.value][language])


def format_analysis(result: AnalysisResult, language: str | None = "en", extra: str | None = None) -> str:
    """The reply for a checked message. `extra` is one optional fixed line (for
    example "I am also telling your family helper: ...") placed before the footer."""
    lang = normalize_language(language)
    verdict = result.verdict
    reasons, steps = _reasons_and_steps(result, lang)

    parts = [T.HEADLINE[verdict.value][lang]]
    if verdict is Verdict.NO_OBVIOUS_RED_FLAGS:
        parts.append(T.CLEAR_NOTE[lang])
    if reasons:
        parts.append(f"{T.SECTION_WHY[lang]}\n{_bullets(reasons)}")
    parts.append(f"{T.SECTION_TODO[lang]}\n{_bullets(steps)}")
    if extra:
        parts.append(extra)
    return _assemble(parts, lang)


def start_message(language: str | None = "en") -> str:
    lang = normalize_language(language)
    return _assemble([T.START[lang]], lang)


def help_message(language: str | None = "en", *, family: bool = False) -> str:
    """/help. It always mentions /emergency; it mentions /family only when Family Mode is available."""
    lang = normalize_language(language)
    parts = [T.HELP[lang], T.HELP_EMERGENCY[lang]]
    if family:
        parts.append(T.HELP_FAMILY[lang])
    return _assemble(parts, lang)


def unsupported_input_message(language: str | None = "en") -> str:
    lang = normalize_language(language)
    return _assemble([T.UNSUPPORTED[lang]], lang)


def image_too_large_message(language: str | None = "en") -> str:
    lang = normalize_language(language)
    return _assemble([T.IMAGE_TOO_LARGE[lang]], lang)


def image_unreadable_message(language: str | None = "en") -> str:
    lang = normalize_language(language)
    return _assemble([T.IMAGE_UNREADABLE[lang]], lang)


def error_message(language: str | None = "en") -> str:
    lang = normalize_language(language)
    return _assemble([T.ERROR[lang]], lang)
