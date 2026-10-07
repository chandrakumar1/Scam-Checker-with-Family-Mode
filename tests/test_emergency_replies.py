import re
import string

import pytest

from app.family.types import AlertSummary, AlertVerdict, ScamCategory, SourceKind
from app.replies import emergency_templates as ET
from app.replies import templates as T
from app.replies.emergency_formatter import (
    button_label,
    category_phrase,
    emergency_alert_message,
    emergency_message,
    guidance_message,
    normal_alert_message,
    popup,
    question_message,
    told_helpers_notice,
)
from app.replies.family_formatter import clean_name
from app.replies.formatter import TELEGRAM_MAX_CHARS, format_analysis, help_message
from tests.family_telegram_helpers import result_for

LANGUAGES = ["en", "hi"]
# Words that would claim a MESSAGE is safe or authentic. ("भरोसेमंद व्यक्ति" = "a trusted person", used
# legitimately in "call someone you trust", so it is not on this list as it is in the family one.)
FORBIDDEN = re.compile(r"(?i)\b(?:safe|genuine|legit|legitimate|authentic|verified|trustworthy)\b|सुरक्षित|विश्वसनीय|प्रामाणिक")
HOSTILE_NAMES = [
    "Priya http://evil.example.com/pay",
    "Bank help bit.ly/3xYz12",
    "www.sbi-kyc.in",
    "Priya‮​\x00\x1b",
    "N" * 500,
    "Priya\nCall 98765 43210 now",
    "",
    None,
]


def summary(category=ScamCategory.OTP_FRAUD, verdict=AlertVerdict.LIKELY_SCAM, source=SourceKind.TEXT):
    return AlertSummary(verdict, category, source)


def fields_of(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


# --- the exact question ---------------------------------------------------------------------------------------------------


def test_the_english_question_is_exactly_the_approved_wording():
    assert ET.QUESTION["en"] == "Did you already send money, OTP, PIN, password, or card details?"


def test_the_question_message_starts_with_the_question_and_carries_the_footer():
    for language in LANGUAGES:
        text = question_message(language)
        assert text.startswith(ET.QUESTION[language]) and text.endswith(T.FOOTER[language])


def test_the_hindi_question_is_hindi():
    assert re.search(r"[ऀ-ॿ]", ET.QUESTION["hi"]) and "OTP" in ET.QUESTION["hi"]


def test_yes_and_no_button_labels_exist_in_both_languages():
    assert (button_label("yes", "en"), button_label("no", "en")) == ("Yes", "No")
    assert button_label("yes", "hi") and button_label("no", "hi")
    assert button_label("emergency", "en").startswith("🆘") and button_label("emergency", "hi").startswith("🆘")
    for key in ET.BUTTONS:
        for language in LANGUAGES:
            assert 0 < len(button_label(key, language)) <= 40


# --- the static guidance -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_guidance_has_all_four_steps(language):
    text = guidance_message(language)
    assert "1930" in text  # call 1930
    if language == "en":
        assert "Call 1930" in text and "right now" in text
        assert "bank" in text and "printed on your card" in text and "block your card and UPI" in text
        assert "Do not send any more money" in text
        assert text.rstrip().endswith("Call someone you trust now.")
    else:
        assert "बैंक" in text and "कार्ड" in text and "UPI" in text and "पैसे न भेजें" in text and "भरोसेमंद" in text


@pytest.mark.parametrize("language", LANGUAGES)
def test_guidance_is_static_fixed_text_with_no_placeholders_and_no_footer(language):
    template = ET.MESSAGES["guidance"][language]
    assert fields_of(template) == set()
    assert guidance_message(language) == template  # identical every time, nothing filled in
    assert not guidance_message(language).endswith(T.FOOTER[language])  # it IS the 1930 content


def test_guidance_is_the_same_whatever_else_is_going_on():
    assert guidance_message("en") == guidance_message("en") == guidance_message(None) == guidance_message("fr")


# --- every message ----------------------------------------------------------------------------------------------------------------


def values_for(key: str) -> dict:
    return {f: ["Rahul", "Anita"] for f in fields_of(ET.MESSAGES[key]["en"]) if f != "name" and f != "phrase"}


USER_KEYS = [k for k in ET.MESSAGES if not k.startswith(("normal_alert", "emergency_alert"))]


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("key", USER_KEYS)
def test_messages_to_the_person_render_within_limits(key, language):
    text = emergency_message(key, language, **values_for(key))
    assert 0 < len(text) <= TELEGRAM_MAX_CHARS and "{" not in text and "}" not in text
    if key in ET.NO_FOOTER:
        assert not text.endswith(T.FOOTER[language])
    else:
        assert text.endswith(T.FOOTER[language]) and "1930" in text


def test_exactly_the_planned_messages_go_without_a_footer():
    assert ET.NO_FOOTER == {"guidance", "emergency_alert", "emergency_alert_typed", "told_notice"}
    assert ET.NO_FOOTER <= set(ET.MESSAGES)


@pytest.mark.parametrize("language", LANGUAGES)
def test_no_message_claims_anything_is_safe(language):
    for key in ET.MESSAGES:
        template = ET.MESSAGES[key][language]
        assert not FORBIDDEN.search(template), key
    for phrase in ET.CATEGORY_PHRASES.values():
        assert not FORBIDDEN.search(phrase[language])


def test_both_languages_use_the_same_placeholders():
    for key, by_lang in ET.MESSAGES.items():
        assert set(by_lang) == {"en", "hi"} and fields_of(by_lang["en"]) == fields_of(by_lang["hi"]), key


def test_unknown_keys_and_placeholders_are_programming_errors():
    with pytest.raises(ValueError):
        emergency_message("nope", "en")
    with pytest.raises(TypeError):
        emergency_message("told_all", "en", names=["A"], extra="x")
    with pytest.raises(TypeError):
        emergency_message("told_all", "en", name="A")  # `names` is the placeholder


def test_the_outcome_messages_name_the_right_people():
    all_ = emergency_message("told_all", "en", names=["Rahul", "Anita"])
    assert "“Rahul”, “Anita”" in all_ and "1930" in all_
    some = emergency_message("told_some", "en", reached=["Rahul"], failed=["Anita"])
    assert "I told “Rahul”, but could not reach “Anita”" in some
    assert "call someone you trust now" in emergency_message("no_helpers", "en").lower()
    assert "/family" in emergency_message("no_helpers", "en")
    assert "/family" not in emergency_message("family_off", "en")  # /family does not exist when Family Mode is off
    assert "call someone you trust now" in emergency_message("family_off", "en").lower()
    assert "call someone you trust now" in emergency_message("error", "en").lower()
    assert "call someone you trust now" in emergency_message("told_none", "en").lower()


def test_every_no_helper_outcome_gives_the_trusted_person_guidance():
    for key in ("no_helpers", "family_off", "error", "told_none"):
        for language in LANGUAGES:
            text = emergency_message(key, language)
            assert ("trust" in text) if language == "en" else ("भरोसेमंद" in text), key


def test_the_deduped_message_still_points_at_1930():
    assert "1930" in emergency_message("deduped", "en") and "1930" in emergency_message("deduped", "hi")


def test_the_no_branch_is_calm_and_offers_the_command_again():
    for language in LANGUAGES:
        text = emergency_message("no_response", language)
        assert "/emergency" in text and text.endswith(T.FOOTER[language])
        assert "ACT NOW" not in text and "1930" in text  # only via the footer: no alarm


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_names_in_outcome_messages_are_cleaned(name):
    for language in LANGUAGES:
        text = emergency_message("told_all", language, names=[name])
        for bad in ("evil.example", "bit.ly", "sbi-kyc", "‮", "​", "\x00", "\x1b", "N" * 41, "[link removed]"):
            assert bad not in text


def test_at_most_five_names_are_shown():
    text = emergency_message("told_all", "en", names=[f"Name{i}" for i in range(9)])
    assert "Name4" in text and "Name5" not in text


def test_the_reply_notice_names_the_helpers_and_has_no_footer():
    notice = told_helpers_notice(["Rahul", "Anita"], "en")
    assert notice == "I am also telling your family helper(s): “Rahul”, “Anita”."
    assert not notice.endswith(T.FOOTER["en"])


def test_popups_are_short_distinct_and_point_to_the_command():
    for language in LANGUAGES:
        invalid, expired = popup("invalid", language), popup("expired", language)
        assert invalid != expired
        for text in (invalid, expired):
            assert "/emergency" in text and len(text) <= 200  # Telegram's popup limit
            assert T.FOOTER[language] not in text
    assert popup("invalid", "en") == "This button does not work. Send /emergency if you need help."
    assert popup("expired", "en") == "This button has expired. Send /emergency if you need help."


# --- what an alert to a helper may contain: fixed wording + a clean name + one category phrase ----------------------------------------


def test_category_phrases_cover_exactly_the_whitelist_in_both_languages():
    assert set(ET.CATEGORY_PHRASES) == {c.value for c in ScamCategory}
    for category in ScamCategory:
        for language in LANGUAGES:
            phrase = category_phrase(summary(category), language)
            assert phrase and not re.search(r"\d|https?:|www\.|\.com|@", phrase)


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("category", list(ScamCategory))
@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_an_emergency_alert_is_only_fixed_wording_a_clean_name_and_one_phrase(language, category, name):
    s = summary(category)
    expected = ET.MESSAGES["emergency_alert_typed"][language].format(
        name=clean_name(name, language), phrase=ET.CATEGORY_PHRASES[category.value][language]
    )
    assert emergency_alert_message(name, s, language) == expected  # nothing else can be in it


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_an_emergency_alert_with_no_summary_says_nothing_about_the_scam(language, name):
    expected = ET.MESSAGES["emergency_alert"][language].format(name=clean_name(name, language))
    assert emergency_alert_message(name, None, language) == expected
    assert "{phrase}" not in expected and "It looked like" not in expected


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_normal_alert_is_only_fixed_wording_a_clean_name_and_one_phrase_plus_the_footer(language, name):
    s = summary(ScamCategory.REFUND)
    body = ET.MESSAGES["normal_alert"][language].format(
        name=clean_name(name, language), phrase=ET.CATEGORY_PHRASES["refund"][language]
    )
    assert normal_alert_message(name, s, language) == f"{body}\n\n{T.FOOTER[language]}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_alerts_never_contain_links_digits_or_markup_beyond_the_fixed_text(language):
    for text in (
        emergency_alert_message("Priya", summary(), language),
        emergency_alert_message("Priya", None, language),
        normal_alert_message("Priya", summary(), language),
    ):
        assert not re.search(r"https?://|www\.|t\.me|\.com|bit\.ly|<|>|\[|\]", text)
        assert set(re.findall(r"\d+", text)) <= {"1930"}  # the helpline is the only number


def test_the_emergency_alert_carries_the_explicit_1930_steps_instead_of_the_footer():
    for language in LANGUAGES:
        for text in (emergency_alert_message("Priya", None, language), emergency_alert_message("Priya", summary(), language)):
            assert "1930" in text and "UPI" in text and not text.endswith(T.FOOTER[language])


def test_the_normal_alert_has_the_1930_footer():
    for language in LANGUAGES:
        assert normal_alert_message("Priya", summary(), language).endswith(T.FOOTER[language])


def test_alert_functions_have_no_parameter_for_free_text():
    import inspect

    for function in (emergency_alert_message, normal_alert_message):
        assert list(inspect.signature(function).parameters) == ["protected_name", "summary", "language"]
    with pytest.raises(TypeError):
        emergency_alert_message("Priya", None, "en", explanation="free text")  # type: ignore[call-arg]


def test_alerts_use_neutral_wording():
    en = emergency_alert_message("Priya", None, "en")
    assert re.search(r"\b(?:she|he|her|his|him)\b", en) is None and "they" in en


def test_alerts_stay_within_telegrams_limit_even_with_enormous_names():
    for text in (emergency_alert_message("N" * 10_000, summary(), "en"), normal_alert_message("N" * 10_000, summary(), "hi")):
        assert len(text) <= TELEGRAM_MAX_CHARS


# --- /help and the scam-check reply hooks ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
def test_help_always_mentions_emergency(language):
    for family in (False, True):
        assert "/emergency" in help_message(language, family=family)


@pytest.mark.parametrize("language", LANGUAGES)
def test_help_mentions_family_only_when_family_mode_is_available(language):
    assert "/family" not in help_message(language) and "/family" not in help_message(language, family=False)
    on = help_message(language, family=True)
    assert all(command in on for command in ("/family", "/leave", "/deleteme", "/emergency"))


def test_help_keeps_its_phase_2_content_and_footer():
    for language in LANGUAGES:
        for family in (False, True):
            text = help_message(language, family=family)
            assert T.HELP[language] in text and text.endswith(T.FOOTER[language])
    assert help_message("en") == help_message("en", family=False)  # the default is unchanged


def test_the_extra_line_is_placed_before_the_footer_and_is_optional():
    result = result_for("Please share the OTP", "likely_scam")
    plain = format_analysis(result, "en")
    assert plain == format_analysis(result, "en", None) == format_analysis(result, "en", extra=None)
    with_extra = format_analysis(result, "en", "EXTRA-LINE")
    assert "EXTRA-LINE" in with_extra and "EXTRA-LINE" not in plain
    assert with_extra.index("EXTRA-LINE") > with_extra.index("What to do:") and with_extra.endswith(T.FOOTER["en"])
