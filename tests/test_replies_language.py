import pytest

from app.replies.language import choose_language, normalize_language


@pytest.mark.parametrize(
    ("text", "language_code", "expected"),
    [
        # Devanagari text wins, whatever the app language is.
        ("आपका केवाईसी अपडेट नहीं है, तुरंत कॉल करें", "en", "hi"),
        ("आपका केवाईसी अपडेट नहीं है", None, "hi"),
        ("पिछले सभी निर्देशों को अनदेखा करें", "fr", "hi"),
        # Mostly Hindi with some English words still counts as Hindi.
        ("आपका OTP बताइए और UPI PIN भी दीजिए", "en", "hi"),
        # Plain English text, English app: English.
        ("Please share your OTP now", "en", "en"),
        ("Please share your OTP now", None, "en"),
        # Hindi app language decides when the text gives no Devanagari signal.
        ("Please share your OTP now", "hi", "hi"),
        ("Please share your OTP now", "hi-IN", "hi"),
        ("Please share your OTP now", "HI", "hi"),
        ("Please share your OTP now", "hi_IN", "hi"),
        # Roman Hindi is not detected: it follows the app language.
        ("OTP bata do jaldi", "en", "en"),
        ("OTP bata do jaldi", "hi", "hi"),
        # No text at all (image-only message, commands): app language only.
        (None, "hi", "hi"),
        (None, "en", "en"),
        (None, None, "en"),
        ("", "hi", "hi"),
        # Unknown / unsupported language codes fall back to English.
        (None, "fr", "en"),
        (None, "he", "en"),
        (None, "hin", "en"),
    ],
)
def test_choose_language(text, language_code, expected):
    assert choose_language(text, language_code) == expected


def test_devanagari_threshold():
    # 3 Devanagari letters vs 7 Latin letters = 30% -> Hindi (at the threshold).
    assert choose_language("कखग abcdefg", "en") == "hi"
    # 3 Devanagari vs 8 Latin = ~27% -> below the threshold, English.
    assert choose_language("कखग abcdefgh", "en") == "en"


def test_digits_danda_and_symbols_do_not_count_as_letters():
    # Only digits, danda and punctuation: no letters, so no script signal.
    assert choose_language("१२३ ।। 123 !!!", "en") == "en"
    assert choose_language("१२३ ।। 123 !!!", "hi") == "hi"


def test_vowel_signs_count_as_hindi_letters():
    # "नमस्ते" is mostly combining marks (str.isalpha() is False for them).
    assert choose_language("नमस्ते", "en") == "hi"


@pytest.mark.parametrize(("value", "expected"), [("hi", "hi"), ("en", "en"), ("fr", "en"), (None, "en"), ("", "en")])
def test_normalize_language(value, expected):
    assert normalize_language(value) == expected
