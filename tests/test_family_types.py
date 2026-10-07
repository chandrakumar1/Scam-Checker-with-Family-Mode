import dataclasses

import pytest

from app.analyzer.prompts import SYSTEM_PROMPT
from app.db.models import AlertKind
from app.family.types import (
    AlertOutcome,
    AlertPlan,
    AlertSummary,
    AlertVerdict,
    ConsentOutcome,
    DeliveryPlan,
    PersonInfo,
    PersonRef,
    ScamCategory,
    SourceKind,
)

OK = (AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, SourceKind.TEXT)


# --- AlertSummary: bounded by construction ---------------------------------------------------------------


def test_alert_summary_has_exactly_the_three_planned_fields():
    assert [f.name for f in dataclasses.fields(AlertSummary)] == ["verdict", "scam_type", "source_kind"]


def test_alert_summary_does_not_carry_the_kind_of_alert():
    summary = AlertSummary(*OK)
    assert not hasattr(summary, "alert_kind")
    with pytest.raises(TypeError):
        AlertSummary(*OK, AlertKind.NORMAL)  # type: ignore[call-arg]


def test_every_field_is_an_enum_type():
    types = {f.name: f.type for f in dataclasses.fields(AlertSummary)}
    assert types == {"verdict": AlertVerdict, "scam_type": ScamCategory, "source_kind": SourceKind}


def test_alert_summary_is_immutable_and_has_no_spare_attributes():
    summary = AlertSummary(*OK)
    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.verdict = AlertVerdict.SUSPICIOUS  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        summary.message_text = "free text"  # type: ignore[attr-defined]
    assert not hasattr(summary, "__dict__")


@pytest.mark.parametrize(
    ("verdict", "scam_type", "source_kind"),
    [
        ("likely_scam", ScamCategory.OTP_FRAUD, SourceKind.TEXT),  # raw strings are rejected even if they match a value
        (AlertVerdict.LIKELY_SCAM, "otp_fraud", SourceKind.TEXT),
        (AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, "text"),
        (AlertVerdict.LIKELY_SCAM, "otp_request", SourceKind.TEXT),  # a rule name
        (AlertVerdict.LIKELY_SCAM, "Please share your OTP at http://evil.example", SourceKind.TEXT),  # message text
        (AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, "a" * 64),  # a hash
        (AlertVerdict.LIKELY_SCAM, b"\x89PNG", SourceKind.IMAGE),  # image bytes
        (None, ScamCategory.OTP_FRAUD, SourceKind.TEXT),
        (AlertVerdict.LIKELY_SCAM, None, SourceKind.TEXT),
        (AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, None),
        (ScamCategory.OTP_FRAUD, AlertVerdict.LIKELY_SCAM, SourceKind.TEXT),  # wrong enum in the wrong slot
    ],
)
def test_alert_summary_rejects_anything_that_is_not_the_right_enum(verdict, scam_type, source_kind):
    with pytest.raises(TypeError):
        AlertSummary(verdict, scam_type, source_kind)


def test_alert_summary_equality_and_hashing():
    assert AlertSummary(*OK) == AlertSummary(*OK)
    assert len({AlertSummary(*OK), AlertSummary(*OK)}) == 1


# --- the enums -------------------------------------------------------------------------------------------------------


def test_verdict_has_only_the_two_alertable_values():
    assert {v.value for v in AlertVerdict} == {"likely_scam", "suspicious"}


def test_source_kind_is_only_text_or_image():
    assert {v.value for v in SourceKind} == {"text", "image"}


def test_there_is_no_unknown_or_none_value_to_stand_in_for_missing_data():
    for enum_type in (AlertVerdict, ScamCategory, SourceKind):
        assert not {v.value for v in enum_type} & {"unknown", "none"}


def test_scam_categories_are_exactly_the_whitelist_the_llm_is_asked_to_use():
    values = {c.value for c in ScamCategory}
    assert "other" in values
    for value in values:
        assert value in SYSTEM_PROMPT, f"{value} is not a label the analyzer prompt knows about"
    assert len(values) == 15


# --- PersonRef ---------------------------------------------------------------------------------------------------------------


def test_person_ref_accepts_a_normal_user():
    ref = PersonRef(5_000_000_000, "Priya", "hi")
    assert (ref.telegram_user_id, ref.display_name, ref.language) == (5_000_000_000, "Priya", "hi")
    assert PersonRef(1, "x").language is None


@pytest.mark.parametrize("bad_id", [0, -1, True, "123", 1.5, None])
def test_person_ref_rejects_bad_ids(bad_id):
    with pytest.raises(ValueError):
        PersonRef(bad_id, "Priya")  # type: ignore[arg-type]


def test_person_ref_requires_a_string_name():
    with pytest.raises(TypeError):
        PersonRef(1, None)  # type: ignore[arg-type]


def test_person_ref_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        PersonRef(1, "x").display_name = "y"  # type: ignore[misc]


# --- plans and results ---------------------------------------------------------------------------------------------------------


def test_alert_plan_defaults_describe_an_empty_plan():
    plan = AlertPlan(AlertOutcome.NO_HELPERS, AlertKind.NORMAL)
    assert plan.alert_id is None and plan.summary is None
    assert plan.deliveries == () and plan.unreachable_names == ()


def test_alert_plan_can_carry_an_optional_summary():
    plan = AlertPlan(AlertOutcome.READY, AlertKind.EMERGENCY, 1, "Priya", AlertSummary(*OK))
    assert plan.summary == AlertSummary(*OK)
    assert AlertPlan(AlertOutcome.READY, AlertKind.EMERGENCY, 1, "Priya", None).summary is None


def test_outcomes_are_a_closed_set():
    assert {o.value for o in AlertOutcome} == {"ready", "no_helpers", "deduped", "throttled", "disabled"}
    assert {o.value for o in ConsentOutcome} == {
        "ok", "invalid", "self_invite", "already_linked", "too_many_pending", "too_many_helpers",
    }  # fmt: skip


def test_results_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        PersonInfo(1, "a", "en").display_name = "b"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        AlertPlan(AlertOutcome.READY, AlertKind.NORMAL).outcome = AlertOutcome.DISABLED  # type: ignore[misc]


def test_alert_plans_carry_identity_and_ids_only_no_message_content():
    # A structural guard on the objects that flow from Family Mode back to the channel layer: adding a
    # content-bearing field to either of them must be a deliberate change that updates this list.
    assert [f.name for f in dataclasses.fields(AlertPlan)] == [
        "outcome", "alert_kind", "alert_id", "protected_name", "summary", "deliveries", "unreachable_names",
    ]
    assert [f.name for f in dataclasses.fields(DeliveryPlan)] == [
        "delivery_id", "guardian_telegram_user_id", "guardian_name", "guardian_language",
    ]
