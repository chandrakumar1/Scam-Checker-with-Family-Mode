import re
import string

import pytest

from app.db.models import LinkStatus
from app.family.types import HelperInfo, PersonInfo
from app.replies import family_formatter as ff
from app.replies import family_templates as FT
from app.replies import templates as T
from app.replies.family_formatter import (
    REQUIRED_FIELDS,
    button_label,
    family_error_message,
    family_menu,
    family_message,
    popup,
)
from app.replies.formatter import TELEGRAM_MAX_CHARS

LANGUAGES = ["en", "hi"]
TOKEN = "AbCdEfGhIjKlMnOpQrStUv"  # 22 url-safe characters
LINK = f"https://t.me/ruko_dev_bot?start=fam_{TOKEN}"

# Words that would claim a message is safe or authentic (also the Hindi ones).
FORBIDDEN = re.compile(
    r"(?i)\b(?:safe|genuine|legit|legitimate|authentic|verified|trustworthy)\b|सुरक्षित|भरोसेमंद|विश्वसनीय|प्रामाणिक"
)

SAMPLE = {"name": "Priya", "handle": "@priya_s", "n": 3, "hours": 48, "link": LINK}


def values_for(key: str) -> dict:
    return {field: SAMPLE[field] for field in REQUIRED_FIELDS[key]}


ALL_KEYS = sorted(FT.MESSAGES)


# --- the template tables are complete and consistent ---------------------------------------------------------


def test_every_message_has_both_languages_and_nothing_else():
    for key, by_lang in FT.MESSAGES.items():
        assert set(by_lang) == {"en", "hi"}, key
        assert all(text.strip() for text in by_lang.values()), key


def test_both_languages_use_exactly_the_same_placeholders():
    for key, by_lang in FT.MESSAGES.items():
        fields = {lang: {n for _, n, _, _ in string.Formatter().parse(text) if n} for lang, text in by_lang.items()}
        assert fields["en"] == fields["hi"] == REQUIRED_FIELDS[key], key


def test_buttons_popups_statuses_have_both_languages():
    for table in (FT.BUTTONS, FT.POPUPS, FT.STATUS_WORDS):
        for key, by_lang in table.items():
            assert set(by_lang) == {"en", "hi"}, key
    assert set(FT.STATUS_WORDS) == {s.value for s in LinkStatus if s.value in ("active", "awaiting_guardian", "awaiting_protected", "invited")}
    assert set(FT.FALLBACK_NAME) == {"en", "hi"} and set(FT.INVITATION_LABEL) == {"en", "hi"}


def test_the_hindi_is_really_hindi():
    devanagari = re.compile(r"[ऀ-ॿ]")
    for key, by_lang in FT.MESSAGES.items():
        assert devanagari.search(by_lang["hi"]), key
    for key, by_lang in FT.BUTTONS.items():
        assert devanagari.search(by_lang["hi"]), key


# --- every message, both languages ---------------------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("key", ALL_KEYS)
def test_every_message_renders_with_the_footer_and_within_telegram_limits(key, language):
    text = family_message(key, language, **values_for(key))
    assert text.endswith(T.FOOTER[language]) and "1930" in text
    assert len(text) <= TELEGRAM_MAX_CHARS
    assert "{" not in text and "}" not in text  # no unfilled placeholder


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("key", ALL_KEYS)
def test_no_message_claims_anything_is_safe(key, language):
    assert not FORBIDDEN.search(family_message(key, language, **values_for(key)))


@pytest.mark.parametrize("language", LANGUAGES)
def test_buttons_are_short_enough_to_tap(language):
    for key, by_lang in FT.BUTTONS.items():
        label = button_label(key, language, **({"name": "Priya"} if "{name}" in by_lang[language] else {}))
        assert 0 < len(label) <= 48, key
    assert len(button_label("remove", language, name="N" * 100)) <= 64  # a long name cannot blow the label up


def test_unknown_language_falls_back_to_english():
    assert family_message("error", "fr") == family_message("error", "en")
    assert family_message("error", None) == family_message("error", "en")
    assert popup("stale", "xx") == FT.POPUPS["stale"]["en"]


def test_error_message_helper():
    assert family_error_message("hi") == family_message("error", "hi")


# --- placeholders are enforced -------------------------------------------------------------------------------------------


def test_missing_or_extra_placeholders_are_programming_errors():
    with pytest.raises(TypeError):
        family_message("helper_prompt", "en")  # needs name
    with pytest.raises(TypeError):
        family_message("helper_prompt", "en", name="A", n=1)  # n is not part of it
    with pytest.raises(TypeError):
        family_message("error", "en", name="A")
    with pytest.raises(ValueError):
        family_message("no_such_message", "en")
    with pytest.raises(TypeError):
        button_label("accept", "en", name="A")  # takes no name
    with pytest.raises(TypeError):
        button_label("remove", "en")  # needs one


# --- names are attacker-controlled: cleaned before use -------------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize(
    ("hostile", "must_not_appear"),
    [
        ("Priya http://evil.example.com/pay", "evil.example"),
        ("Bank help bit.ly/3xYz12", "bit.ly"),
        ("www.sbi-kyc.in", "sbi-kyc"),
        ("t.me/scamchannel", "t.me/scam"),
        ("Priya‮​\x00\x1b", "‮"),
        ("Priya\nCall 98765 now", "\n98765"),
    ],
)
def test_hostile_names_are_cleaned_everywhere_they_are_shown(hostile, must_not_appear, language):
    for key in ("helper_prompt", "confirm_prompt", "confirmed_protected", "left_notice_protected", "blocked_notice"):
        assert must_not_appear not in family_message(key, language, **{**values_for(key), "name": hostile})
    assert must_not_appear not in button_label("remove", language, name=hostile)
    helper = HelperInfo("a" * 16, LinkStatus.ACTIVE, PersonInfo(1, hostile, "en"))
    assert must_not_appear not in family_menu([helper], [helper], language)


def test_long_names_are_capped():
    text = family_message("helper_prompt", "en", name="N" * 500)
    assert "N" * 41 not in text and len(text) <= TELEGRAM_MAX_CHARS


@pytest.mark.parametrize("empty", [None, "", "   ", "​", "http://only.a.link", "www.a.com b.org", "\x00\x01"])
def test_an_empty_name_gets_a_neutral_fallback(empty):
    for language in LANGUAGES:
        text = family_message("helper_declined_notice", language, name=empty)
        assert FT.FALLBACK_NAME[language] in text
        assert "[link removed]" not in text and "[लिंक हटाया गया]" not in text


def test_a_url_inside_a_name_is_dropped_silently():
    text = family_message("helper_declined_notice", "en", name="Priya http://evil.example/x Sharma")
    assert "“Priya Sharma”" in text and "link removed" not in text


def test_names_are_shown_in_quotes_and_cannot_break_out_of_them():
    text = family_message("helper_prompt", "en", name='x" injected "y')
    assert '“x" injected "y”' in text  # the clean_text step keeps the characters; the quotes are typographic


def test_hindi_names_and_emoji_survive():
    text = family_message("confirmed_protected", "hi", name="प्रिया शर्मा 👩")
    assert "“प्रिया शर्मा 👩”" in text


# --- the @username on the confirmation prompt -------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "shown"), [("@rahul_s", " (@rahul_s)"), ("rahul_s", " (@rahul_s)"), ("R2_d2", " (@R2_d2)")])
def test_a_valid_username_is_shown(raw, shown):
    assert f"“Rahul”{shown} accepted" in family_message("confirm_prompt", "en", name="Rahul", handle=raw)


@pytest.mark.parametrize("raw", [None, "", "@", "has space", "a" * 33, "bad-name", "evil.com", "x‮", "http://x", "@@a"])
def test_a_malformed_username_is_dropped(raw):
    text = family_message("confirm_prompt", "en", name="Rahul", handle=raw)
    assert "“Rahul” accepted" in text and "(@" not in text


# --- numbers and the invitation link -----------------------------------------------------------------------------------------------


def test_the_invite_message_carries_the_link_once_and_the_lifetime():
    for language in LANGUAGES:
        text = family_message("invite_created", language, link=LINK, hours=48)
        assert text.count(LINK) == 1
        assert "48" in text and TOKEN in text


@pytest.mark.parametrize(("hours", "shown"), [(48, "48"), (48.0, "48"), (12.5, "12.5"), (1, "1")])
def test_hours_are_formatted_plainly(hours, shown):
    assert f"expires in {shown} hours" in family_message("invite_created", "en", link=LINK, hours=hours)


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/?start=fam_" + TOKEN,
        f"http://t.me/ruko_dev_bot?start=fam_{TOKEN}",  # not https
        f"https://t.me/ruko_dev_bot?start=fam_{TOKEN}&x=1",
        f"https://t.me/ruko_dev_bot?start=fam_{TOKEN} http://evil.example",
        "https://t.me/ruko_dev_bot?start=fam_short",
        f"https://t.me/ru?start=fam_{TOKEN}",  # username too short
        f"https://t.me/ruko_dev_bot?start=other_{TOKEN}",
        f"https://t.me.evil.com/ruko_dev_bot?start=fam_{TOKEN}",
        "",
        None,
        5,
    ],
)
def test_only_a_well_formed_invitation_link_can_be_embedded(bad):
    with pytest.raises(ValueError):
        family_message("invite_created", "en", link=bad, hours=48)


def test_limit_messages_show_the_number():
    assert "3 invitations" in family_message("invite_limit_pending", "en", n=3)
    assert "(5)" in family_message("invite_limit_helpers", "en", n=5)


# --- the /family menu ----------------------------------------------------------------------------------------------------------------


def helper(status, name="Rahul", ref="a" * 16):
    return HelperInfo(ref, status, PersonInfo(1, name, "en") if name else None)


@pytest.mark.parametrize("language", LANGUAGES)
def test_menu_with_nobody_says_so_and_still_explains_what_helpers_are(language):
    text = family_menu([], [], language)
    assert FT.MESSAGES["menu_intro"][language] in text
    assert FT.MESSAGES["menu_no_helpers"][language] in text
    assert FT.MESSAGES["menu_helping_title"][language] not in text
    assert text.endswith(T.FOOTER[language])


def test_menu_lists_helpers_with_their_status_in_words():
    text = family_menu(
        [helper(LinkStatus.ACTIVE, "Rahul"), helper(LinkStatus.AWAITING_GUARDIAN, "Anita"),
         helper(LinkStatus.AWAITING_PROTECTED, "Meera"), helper(LinkStatus.INVITED, None)],
        [],
        "en",
    )  # fmt: skip
    assert "• “Rahul” — active" in text
    assert "• “Anita” — waiting for them to accept" in text
    assert "• “Meera” — waiting for YOUR confirmation" in text
    assert "• Invitation — invitation not opened yet" in text
    assert FT.MESSAGES["menu_no_helpers"]["en"] not in text


def test_menu_lists_the_people_you_help_and_how_to_stop():
    text = family_menu([], [helper(LinkStatus.ACTIVE, "Priya")], "en")
    assert "You are a family helper for:\n• “Priya”\nTo stop, send /leave." in text


def test_menu_in_hindi():
    text = family_menu([helper(LinkStatus.ACTIVE, "Rahul")], [helper(LinkStatus.ACTIVE, "Priya")], "hi")
    assert "• “Rahul” — चालू" in text and "/leave" in text


def test_menu_never_exceeds_the_telegram_limit_or_loses_the_footer():
    many = [helper(LinkStatus.ACTIVE, "N" * 40, ref=f"{i:016x}") for i in range(60)]
    text = family_menu(many, many, "en")
    assert len(text) <= TELEGRAM_MAX_CHARS and text.endswith(T.FOOTER["en"])


def test_menu_does_not_expose_internal_references_or_ids():
    text = family_menu([helper(LinkStatus.ACTIVE, "Rahul", ref="deadbeefdeadbeef")], [], "en")
    assert "deadbeef" not in text and "1" not in text.replace("1930", "").replace("/family", "")


# --- pop-ups -----------------------------------------------------------------------------------------------------------------------------


def test_the_stale_popup_is_short_and_points_back_to_the_menu():
    for language in LANGUAGES:
        text = popup("stale", language)
        assert len(text) <= 200 and "/family" in text  # Telegram limits pop-up text to 200 characters
        assert T.FOOTER[language] not in text


# --- module hygiene ----------------------------------------------------------------------------------------------------------------------


def test_deep_link_pattern_matches_what_the_family_package_generates():
    from app.family import tokens

    link = f"https://t.me/ruko_dev_bot?start={tokens.start_payload(tokens.new_invite_token())}"
    assert ff._DEEP_LINK.fullmatch(link)
