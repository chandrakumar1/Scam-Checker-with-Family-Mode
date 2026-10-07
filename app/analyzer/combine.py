"""Combine rule hits and the LLM opinion into one final verdict.

Ranking: likely_scam > suspicious > no_obvious_red_flags.
- CRITICAL rule hit => at least likely_scam.
- HIGH or MEDIUM rule hit => at least suspicious.
- LLM failure (None) counts as "suspicious".
- The final verdict is the more cautious of the rules floor and the LLM.
  Rules can raise the LLM's verdict but never lower it.
"""

from enum import StrEnum

from pydantic import BaseModel

from app.analyzer import llm
from app.analyzer.llm import LLMAnalysis
from app.analyzer.rules import RuleHit, Severity, check_rules


class Verdict(StrEnum):
    NO_OBVIOUS_RED_FLAGS = "no_obvious_red_flags"
    SUSPICIOUS = "suspicious"
    LIKELY_SCAM = "likely_scam"

    @property
    def rank(self) -> int:
        return _VERDICT_RANK[self]


_VERDICT_RANK = {
    Verdict.NO_OBVIOUS_RED_FLAGS: 0,
    Verdict.SUSPICIOUS: 1,
    Verdict.LIKELY_SCAM: 2,
}

_SEVERITY_FLOOR = {
    Severity.CRITICAL: Verdict.LIKELY_SCAM,
    Severity.HIGH: Verdict.SUSPICIOUS,
    Severity.MEDIUM: Verdict.SUSPICIOUS,
}


class AnalysisResult(BaseModel):
    verdict: Verdict
    rules_verdict: Verdict
    llm_verdict: Verdict | None
    llm_failed: bool
    # True when the rules floor is stricter than what the LLM said, so the
    # LLM's explanation may understate the risk.
    raised_by_rules: bool
    rule_hits: list[RuleHit]
    scam_type: str | None
    red_flags: list[str]
    explanation: str | None
    what_to_do: list[str]


def most_cautious(*verdicts: Verdict) -> Verdict:
    return max(verdicts, key=lambda v: v.rank)


def rules_verdict(hits: list[RuleHit]) -> Verdict:
    return most_cautious(
        Verdict.NO_OBVIOUS_RED_FLAGS, *(_SEVERITY_FLOOR[h.severity] for h in hits)
    )


def combine(hits: list[RuleHit], llm_result: LLMAnalysis | None) -> AnalysisResult:
    floor = rules_verdict(hits)
    llm_verdict = Verdict(llm_result.verdict) if llm_result else None
    effective_llm = llm_verdict if llm_verdict is not None else Verdict.SUSPICIOUS
    final = most_cautious(floor, effective_llm)

    red_flags = [h.description for h in hits]
    if llm_result:
        red_flags += [f for f in llm_result.red_flags if f not in red_flags]

    return AnalysisResult(
        verdict=final,
        rules_verdict=floor,
        llm_verdict=llm_verdict,
        llm_failed=llm_result is None,
        raised_by_rules=llm_verdict is not None and final.rank > llm_verdict.rank,
        rule_hits=hits,
        scam_type=llm_result.scam_type if llm_result else None,
        red_flags=red_flags,
        explanation=llm_result.explanation if llm_result else None,
        what_to_do=llm_result.what_to_do if llm_result else [],
    )


async def analyze_message(
    text: str | None = None,
    image_bytes: bytes | None = None,
    language: str = "en",
) -> AnalysisResult:
    """Full analyzer path: rules + LLM, combined."""
    hits = check_rules(text) if text else []
    llm_result = await llm.analyze(text=text, image_bytes=image_bytes, language=language)
    return combine(hits, llm_result)
