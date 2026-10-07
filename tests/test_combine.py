import itertools

import pytest

from app.analyzer import llm
from app.analyzer.combine import (
    AnalysisResult,
    Verdict,
    analyze_message,
    combine,
    most_cautious,
    rules_verdict,
)
from app.analyzer.llm import LLMAnalysis
from app.analyzer.rules import RuleHit, Severity


def hit(severity: Severity, name: str = "r") -> RuleHit:
    return RuleHit(rule=f"{name}_{severity}", severity=severity, description=f"{severity} flag", matches=["x"])


def llm_says(verdict: str, red_flags: list[str] | None = None) -> LLMAnalysis:
    return LLMAnalysis(
        verdict=verdict,
        scam_type="other",
        red_flags=red_flags or [],
        explanation="explanation",
        what_to_do=["step"],
    )


NOB, SUS, SCAM = Verdict.NO_OBVIOUS_RED_FLAGS, Verdict.SUSPICIOUS, Verdict.LIKELY_SCAM


def test_verdict_ranking():
    assert SCAM.rank > SUS.rank > NOB.rank
    assert most_cautious(NOB, SCAM, SUS) is SCAM
    assert most_cautious(NOB, SUS) is SUS
    assert most_cautious(NOB) is NOB


@pytest.mark.parametrize(
    ("hits", "expected"),
    [
        ([], NOB),
        ([hit(Severity.MEDIUM)], SUS),
        ([hit(Severity.HIGH)], SUS),
        ([hit(Severity.CRITICAL)], SCAM),
        ([hit(Severity.MEDIUM), hit(Severity.HIGH)], SUS),
        ([hit(Severity.MEDIUM), hit(Severity.CRITICAL)], SCAM),
    ],
)
def test_rules_verdict(hits, expected):
    assert rules_verdict(hits) is expected


def test_critical_never_overridden_by_no_obvious_red_flags():
    result = combine([hit(Severity.CRITICAL)], llm_says("no_obvious_red_flags"))
    assert result.verdict is SCAM
    assert result.llm_verdict is NOB
    assert result.raised_by_rules is True


def test_critical_with_llm_suspicious_is_likely_scam():
    assert combine([hit(Severity.CRITICAL)], llm_says("suspicious")).verdict is SCAM


def test_high_raises_to_at_least_suspicious():
    result = combine([hit(Severity.HIGH)], llm_says("no_obvious_red_flags"))
    assert result.verdict is SUS
    assert result.raised_by_rules is True


def test_high_does_not_lower_llm_likely_scam():
    result = combine([hit(Severity.HIGH)], llm_says("likely_scam"))
    assert result.verdict is SCAM
    assert result.raised_by_rules is False


def test_no_hits_does_not_lower_llm():
    assert combine([], llm_says("likely_scam")).verdict is SCAM
    assert combine([], llm_says("suspicious")).verdict is SUS


def test_no_hits_and_llm_clear():
    result = combine([], llm_says("no_obvious_red_flags"))
    assert result.verdict is NOB
    assert result.raised_by_rules is False


def test_medium_with_llm_clear_is_suspicious():
    assert combine([hit(Severity.MEDIUM)], llm_says("no_obvious_red_flags")).verdict is SUS


def test_llm_failure_becomes_suspicious():
    result = combine([], None)
    assert result.verdict is SUS
    assert result.llm_failed is True
    assert result.llm_verdict is None
    assert result.explanation is None


def test_llm_failure_with_critical_is_likely_scam():
    result = combine([hit(Severity.CRITICAL)], None)
    assert result.verdict is SCAM
    assert result.llm_failed is True


@pytest.mark.parametrize(
    ("severities", "llm_verdict"),
    list(
        itertools.product(
            [(), (Severity.MEDIUM,), (Severity.HIGH,), (Severity.CRITICAL,), (Severity.MEDIUM, Severity.CRITICAL)],
            [None, "no_obvious_red_flags", "suspicious", "likely_scam"],
        )
    ),
)
def test_rules_never_lower_and_floor_always_holds(severities, llm_verdict):
    hits = [hit(s) for s in severities]
    llm_result = llm_says(llm_verdict) if llm_verdict else None
    result = combine(hits, llm_result)

    effective_llm = Verdict(llm_verdict) if llm_verdict else SUS
    assert result.verdict.rank >= effective_llm.rank  # never lowered
    assert result.verdict.rank >= rules_verdict(hits).rank  # floor holds
    assert result.verdict is most_cautious(rules_verdict(hits), effective_llm)


def test_result_is_structured():
    hits = [hit(Severity.HIGH, "short")]
    result = combine(hits, llm_says("likely_scam", red_flags=["Asks for money", "high flag"]))
    assert isinstance(result, AnalysisResult)
    assert result.rule_hits == hits
    assert result.rules_verdict is SUS
    assert result.scam_type == "other"
    assert result.explanation == "explanation"
    assert result.what_to_do == ["step"]
    # rule descriptions first, LLM flags appended without duplicates
    assert result.red_flags == ["high flag", "Asks for money"]
    data = result.model_dump(mode="json")
    assert data["verdict"] == "likely_scam"
    assert data["rule_hits"][0]["severity"] == "high"


@pytest.mark.anyio
async def test_analyze_message_runs_rules_and_llm(monkeypatch):
    calls = []

    async def fake_analyze(**kwargs):
        calls.append(kwargs)
        return llm_says("no_obvious_red_flags")

    monkeypatch.setattr(llm, "analyze", fake_analyze)
    result = await analyze_message("Please share the OTP", language="hi")
    assert calls == [{"text": "Please share the OTP", "image_bytes": None, "language": "hi"}]
    assert [h.rule for h in result.rule_hits] == ["otp_request"]
    assert result.verdict is SCAM


@pytest.mark.anyio
async def test_analyze_message_image_only(monkeypatch):
    async def fake_analyze(**kwargs):
        return llm_says("suspicious")

    monkeypatch.setattr(llm, "analyze", fake_analyze)
    result = await analyze_message(image_bytes=b"\x89PNG\r\n\x1a\n")
    assert result.rule_hits == []
    assert result.verdict is SUS
