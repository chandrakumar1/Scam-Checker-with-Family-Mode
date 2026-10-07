import dataclasses

import pytest

from app.analyzer.combine import Verdict
from app.channels.telegram.alert_summary import build_alert_summary, category_of
from app.family.types import AlertSummary, AlertVerdict, ScamCategory, SourceKind
from tests.family_telegram_helpers import result_for


def summary_of(text="Hello there", verdict="likely_scam", label="other", image=False, **kw):
    return build_alert_summary(result_for(text, verdict, label=label, **kw), has_image=image)


# --- the verdict -----------------------------------------------------------------------------------------------------


def test_a_likely_scam_maps_to_a_likely_scam_summary():
    assert summary_of(verdict="likely_scam").verdict is AlertVerdict.LIKELY_SCAM


def test_a_suspicious_result_maps_to_a_suspicious_summary():
    assert summary_of(verdict="suspicious").verdict is AlertVerdict.SUSPICIOUS


def test_an_llm_failure_is_suspicious_so_it_maps_to_suspicious_never_to_likely_scam():
    result = result_for("Hello there", None)
    assert result.verdict is Verdict.SUSPICIOUS and result.llm_failed
    assert build_alert_summary(result, has_image=False).verdict is AlertVerdict.SUSPICIOUS


def test_no_obvious_red_flags_has_no_summary_at_all():
    assert summary_of(verdict="no_obvious_red_flags") is None


def test_a_critical_rule_makes_a_likely_scam_even_when_the_llm_says_nothing_is_wrong():
    result = result_for("Please share the OTP you received", "no_obvious_red_flags")
    assert result.verdict is Verdict.LIKELY_SCAM
    assert build_alert_summary(result, has_image=False).verdict is AlertVerdict.LIKELY_SCAM


# --- the category: critical rule first, then the whitelisted LLM label, then OTHER ------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Please share the OTP you received", ScamCategory.OTP_FRAUD),
        ("Please install AnyDesk so I can help", ScamCategory.REMOTE_ACCESS),
        ("Install SBI_Rewards.apk to claim points", ScamCategory.MALICIOUS_APK),
        ("This is CBI. An arrest warrant has been issued in your name. You are under digital arrest.", ScamCategory.DIGITAL_ARREST),
    ],
)
def test_each_critical_rule_decides_its_category(text, expected):
    assert summary_of(text, "likely_scam", label="refund").scam_type is expected  # beats the LLM's label


def test_a_critical_rule_beats_the_llm_label_even_when_they_disagree():
    result = result_for("Please share the OTP you received", "likely_scam", label="kyc_update")
    assert category_of(result) is ScamCategory.OTP_FRAUD


def test_with_two_critical_rules_the_first_listed_wins_deterministically():
    result = result_for("Please install AnyDesk and share the OTP", "likely_scam")
    assert category_of(result) is category_of(result_for("Please install AnyDesk and share the OTP", "likely_scam"))
    assert category_of(result) in (ScamCategory.OTP_FRAUD, ScamCategory.REMOTE_ACCESS)


@pytest.mark.parametrize("text", ["Dear customer your KYC has expired, update now", "Click bit.ly/3xYz12 now", "Urgent! Call 98765 43210 immediately"])
def test_lesser_rules_do_not_decide_the_category(text):
    assert summary_of(text, "suspicious", label="refund").scam_type is ScamCategory.REFUND  # falls through to the LLM


@pytest.mark.parametrize("category", list(ScamCategory))
def test_every_whitelisted_llm_label_maps_to_itself(category):
    assert summary_of(label=category.value).scam_type is category


@pytest.mark.parametrize(
    ("label", "expected"),
    [("OTP_FRAUD", ScamCategory.OTP_FRAUD), ("  Refund ", ScamCategory.REFUND), ("KYC_Update", ScamCategory.KYC_UPDATE)],
)
def test_llm_labels_are_normalised_before_the_whitelist(label, expected):
    assert summary_of(label=label).scam_type is expected


@pytest.mark.parametrize(
    "label",
    ["none", "", "unknown", "something_new", "otp fraud", "otp-fraud", "http://evil.example/pay", "Call 98765 43210 now",
     "ignore previous instructions", "otp_fraud; DROP TABLE", "x" * 500],
)  # fmt: skip
def test_anything_else_the_llm_says_becomes_other(label):
    assert summary_of(label=label).scam_type is ScamCategory.OTHER


def test_no_llm_label_at_all_becomes_other():
    assert build_alert_summary(result_for("Hello there", None), has_image=False).scam_type is ScamCategory.OTHER


# --- the source ---------------------------------------------------------------------------------------------------------------


def test_source_kind_follows_whether_an_image_was_checked():
    assert summary_of(image=False).source_kind is SourceKind.TEXT
    assert summary_of(image=True).source_kind is SourceKind.IMAGE


# --- nothing but the three enums can cross -----------------------------------------------------------------------------------------


def test_the_summary_is_exactly_three_enum_fields():
    s = summary_of("Please share the OTP", label="refund")
    assert isinstance(s, AlertSummary)
    assert [f.name for f in dataclasses.fields(s)] == ["verdict", "scam_type", "source_kind"]
    for field in dataclasses.fields(s):
        assert type(getattr(s, field.name)).__module__ == "app.family.types"


def test_nothing_from_the_analysis_can_leak_into_the_summary():
    sentinels = ["SENTINEL-MESSAGE-TEXT", "http://evil.example/steal", "otp_request", "authority_threat", "Do step one."]
    result = result_for(
        "SENTINEL-MESSAGE-TEXT please share the OTP http://evil.example/steal",
        "likely_scam",
        label="http://evil.example/steal",
        explanation="SENTINEL-MESSAGE-TEXT otp_request authority_threat",
    )
    assert result.rule_hits and result.explanation  # the inputs really are loaded with sentinels
    s = build_alert_summary(result, has_image=False)
    rendered = repr(s) + str(dataclasses.asdict(s))
    for sentinel in sentinels:
        assert sentinel not in rendered


def test_a_hostile_llm_label_cannot_become_a_string_in_the_summary():
    s = summary_of(label="<script>alert(1)</script>")
    assert s.scam_type is ScamCategory.OTHER and isinstance(s.scam_type, ScamCategory)
