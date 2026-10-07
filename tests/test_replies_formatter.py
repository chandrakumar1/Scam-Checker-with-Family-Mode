import re

import pytest

from app.analyzer.combine import Verdict, combine
from app.analyzer.llm import LLMAnalysis
from app.analyzer.rules import check_rules
from app.replies import formatter, templates as T
from app.replies.formatter import (
    TELEGRAM_MAX_CHARS,
    clean_text,
    error_message,
    format_analysis,
    help_message,
    image_too_large_message,
    image_unreadable_message,
    start_message,
    unsupported_input_message,
)

LANGUAGES = ["en", "hi"]
VERDICTS = ["likely_scam", "suspicious", "no_obvious_red_flags"]

# Words that would claim a message is safe or authentic. Our own text must
# never contain them (the LLM's text is filtered separately).
FORBIDDEN = re.compile(
    r"(?i)\b(?:safe|genuine|legit|legitimate|authentic|verified|trustworthy)\b|सुरक्षित|भरोसेमंद|विश्वसनीय|प्रामाणिक"
)


def llm(verdict="suspicious", explanation="Short explanation.", what_to_do=None, red_flags=None) -> LLMAnalysis:
    return LLMAnalysis(
        verdict=verdict,
        scam_type="other",
        red_flags=red_flags or [],
        explanation=explanation,
        what_to_do=["Do step one."] if what_to_do is None else what_to_do,
    )


def result(text="", llm_result=None):
    return combine(check_rules(text), llm_result)


def all_static_messages(language):
    return [
        start_message(language), help_message(language), unsupported_input_message(language),
        image_too_large_message(language), image_unreadable_message(language), error_message(language),
    ]  # fmt: skip


# --- headlines and structure ----------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("verdict", VERDICTS)
def test_every_verdict_in_every_language(verdict, language):
    text = format_analysis(result("", llm(verdict, "Because reasons.", ["Do this."])), language)
    assert T.HEADLINE[verdict][language] in text
    assert "Because reasons." in text
    assert "Do this." in text
    assert T.SECTION_WHY[language] in text and T.SECTION_TODO[language] in text
    assert len(text) <= TELEGRAM_MAX_CHARS


@pytest.mark.parametrize("language", LANGUAGES)
def test_clear_verdict_always_carries_the_not_a_guarantee_note(language):
    text = format_analysis(result("", llm("no_obvious_red_flags", "Looks like a normal chat.")), language)
    assert T.CLEAR_NOTE[language] in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_scam_verdicts_do_not_carry_the_clear_note(language):
    text = format_analysis(result("", llm("likely_scam")), language)
    assert T.CLEAR_NOTE[language] not in text


# --- the 1930 footer is on every reply ---------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_footer_on_every_user_facing_reply(language):
    replies = all_static_messages(language)
    replies += [format_analysis(result("", llm(v)), language) for v in VERDICTS]
    replies += [format_analysis(result("Please share the OTP", None), language)]
    for reply in replies:
        assert reply.endswith(T.FOOTER[language])
        assert "1930" in reply


# --- never claims a message is safe --------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_our_own_text_never_claims_safety(language):
    replies = all_static_messages(language)
    replies += [format_analysis(result("", llm(v, "x", [])), language) for v in VERDICTS]
    replies += [format_analysis(result("Please share the OTP", None), language)]
    for reply in replies:
        assert not FORBIDDEN.search(reply), reply


@pytest.mark.parametrize(
    "claim",
    ["This message looks safe.", "It appears to be genuine.", "A legitimate bank notice.", "यह संदेश सुरक्षित है।"],
)
def test_llm_explanation_claiming_safety_is_replaced(claim):
    text = format_analysis(result("", llm("no_obvious_red_flags", claim)), "en")
    assert claim not in text
    assert T.HEADLINE["no_obvious_red_flags"]["en"] in text
    assert T.GENERIC_STEPS["no_obvious_red_flags"]["en"][0] in text  # falls back to our steps


def test_safety_filter_only_applies_to_the_clear_verdict():
    # "not genuine" is a legitimate thing to say about a scam.
    text = format_analysis(result("", llm("likely_scam", "This is not a genuine bank message.")), "en")
    assert "not a genuine bank message" in text


# --- when the LLM's words are used, and when not --------------------------------------------------


def test_llm_failure_gives_suspicious_with_generic_text():
    out = format_analysis(result("Hello there", None), "en")
    assert T.HEADLINE["suspicious"]["en"] in out
    assert T.LLM_UNAVAILABLE["en"] in out
    for step in T.GENERIC_STEPS["suspicious"]["en"]:
        assert step in out


def test_llm_failure_with_critical_rule_uses_rule_reason_not_unavailable_note():
    out = format_analysis(result("Please share the OTP you received", None), "en")
    assert T.HEADLINE["likely_scam"]["en"] in out
    assert T.RULE_REASONS["otp_request"]["en"] in out
    assert T.LLM_UNAVAILABLE["en"] not in out


@pytest.mark.parametrize("language", LANGUAGES)
def test_rules_raising_the_verdict_hide_a_contradicting_llm_explanation(language):
    fooled = llm("no_obvious_red_flags", "Looks fine to me.", ["Nothing to do."])
    res = result("Please share the OTP you received", fooled)
    assert res.verdict is Verdict.LIKELY_SCAM and res.raised_by_rules
    out = format_analysis(res, language)
    assert "Looks fine to me." not in out
    assert "Nothing to do." not in out
    assert T.RULE_REASONS["otp_request"][language] in out
    assert T.GENERIC_STEPS["likely_scam"][language][0] in out


def test_llm_text_is_used_when_it_agrees_with_the_final_verdict():
    res = result("Please share the OTP you received", llm("likely_scam", "It asks for your OTP.", ["Call 1930."]))
    out = format_analysis(res, "en")
    assert "It asks for your OTP." in out and "Call 1930." in out
    assert T.RULE_REASONS["otp_request"]["en"] not in out


def test_at_most_three_rule_reasons():
    text = "URGENT: KYC expired. Click bit.ly/x, share the OTP, call 9876543210 immediately"
    res = result(text, llm("no_obvious_red_flags", "fine"))
    assert len(res.rule_hits) == 4
    out = format_analysis(res, "en")
    assert sum(reason["en"] in out for reason in T.RULE_REASONS.values()) == 3


def test_every_rule_has_text_in_both_languages():
    from app.analyzer.rules import RULES

    for rule in RULES:
        assert set(T.RULE_REASONS[rule.name]) == {"en", "hi"}, rule.name


def test_unknown_language_falls_back_to_english():
    assert format_analysis(result("", llm()), "fr") == format_analysis(result("", llm()), "en")
    assert start_message(None) == start_message("en")


# --- untrusted LLM text is cleaned ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_absent"),
    [
        ("Click http://evil.example.com/pay now", "evil.example.com"),
        ("Go to https://bit.ly/3xYz12.", "bit.ly"),
        ("Visit www.sbi-kyc-update.in today", "sbi-kyc-update"),
        ("Open t.me/scamchannel", "t.me"),
        ("Visit evil-bank.xyz/login to fix it", "evil-bank"),
        ("tg://resolve?domain=scam", "tg://"),
    ],
)
def test_urls_in_llm_text_are_removed(raw, expected_absent):
    out = format_analysis(result("", llm("likely_scam", raw, [raw])), "en")
    assert expected_absent not in out
    assert T.LINK_REMOVED["en"] in out


def test_link_removed_marker_is_localised():
    out = format_analysis(result("", llm("likely_scam", "See http://evil.test/x")), "hi")
    assert T.LINK_REMOVED["hi"] in out


def test_our_own_helpline_site_is_kept():
    out = format_analysis(result("", llm("likely_scam", "Report at cybercrime.gov.in now.")), "en")
    assert "Report at cybercrime.gov.in now." in out


def test_trailing_punctuation_survives_url_removal():
    assert clean_text("See http://evil.test/x.", 100) == "See [link removed]."


def test_control_and_bidi_characters_are_stripped():
    cleaned = clean_text("a\x00b‮c​d\ne", 100)
    assert cleaned == "a b c d e"


def test_long_llm_text_is_truncated():
    out = format_analysis(result("", llm("suspicious", "word " * 500, ["step " * 200])), "en")
    assert "…" in out
    assert len(out) < 1500


def test_no_more_than_three_steps_and_blank_steps_dropped():
    res = result("", llm("suspicious", "x", ["one", "two", "three", "four", "   "]))
    out = format_analysis(res, "en")
    assert "• three" in out and "four" not in out


def test_empty_llm_steps_fall_back_to_generic():
    out = format_analysis(result("", llm("suspicious", "x", [])), "en")
    assert T.GENERIC_STEPS["suspicious"]["en"][0] in out


# --- limits, no echo, formatting -------------------------------------------------------------------------


def test_reply_never_exceeds_telegram_limit(monkeypatch):
    monkeypatch.setattr(formatter, "MAX_EXPLANATION_CHARS", 10_000)
    out = format_analysis(result("", llm("suspicious", "word " * 5_000)), "en")
    assert len(out) <= TELEGRAM_MAX_CHARS
    assert out.endswith(T.FOOTER["en"])  # the safety footer is what gets protected


def test_reply_does_not_echo_the_original_message():
    scam = "Dear customer, claim your refund at the secret-link-xyz portal and share the OTP"
    res = result(scam, llm("likely_scam", "It asks for your OTP."))
    out = format_analysis(res, "en")
    assert "secret-link-xyz" not in out
    assert scam not in out


def test_replies_are_plain_text_without_markup():
    out = format_analysis(result("", llm("likely_scam", "Use <b>this</b> & *that*")), "en")
    assert "<b>" in out  # shown literally; we send without parse_mode so it cannot render


def test_static_messages_describe_privacy_accurately():
    for language in LANGUAGES:
        help_text = help_message(language)
        assert "AI" in help_text
        # No absolute "never stored anywhere" claim.
        assert not re.search(r"(?i)never stored|not stored anywhere|कभी सहेजा नहीं", help_text)
    assert "does not intentionally store" in help_message("en")
    assert "जानबूझकर सहेजता नहीं" in help_message("hi")


def test_static_messages_are_non_empty_and_distinct():
    for language in LANGUAGES:
        messages = all_static_messages(language)
        assert all(len(m) > 50 for m in messages)
        assert len(set(messages)) == len(messages)
    assert start_message("en") != start_message("hi")


def test_max_image_size_is_stated_consistently():
    assert "5 MB" in T.IMAGE_TOO_LARGE["en"] and "5 MB" in T.IMAGE_TOO_LARGE["hi"]
    assert "5 MB" in T.HELP["en"] and "5 MB" in T.HELP["hi"]
