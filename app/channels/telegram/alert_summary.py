"""Turns an analysis result into the bounded AlertSummary that may cross into Family Mode.

This is the ONLY place that sees rule names and the LLM's free-form scam_type.
It consumes them here and passes on nothing but three enum values:

    verdict      <- the final verdict (only likely_scam / suspicious can alert)
    scam_type    <- a whitelisted ScamCategory (never the LLM's raw string)
    source_kind  <- whether the check involved an image

Category precedence: a CRITICAL rule's category (deterministic), then the LLM's
label if it is in the whitelist, else OTHER.
"""

from app.analyzer.combine import AnalysisResult, Verdict
from app.analyzer.rules import Severity
from app.family.types import AlertSummary, AlertVerdict, ScamCategory, SourceKind

_VERDICTS = {
    Verdict.LIKELY_SCAM: AlertVerdict.LIKELY_SCAM,
    Verdict.SUSPICIOUS: AlertVerdict.SUSPICIOUS,
}

# Rule name -> category, for the rules whose category is unambiguous.
_RULE_CATEGORIES = {
    "otp_request": ScamCategory.OTP_FRAUD,
    "remote_access_app": ScamCategory.REMOTE_ACCESS,
    "apk_link": ScamCategory.MALICIOUS_APK,
    "authority_threat": ScamCategory.DIGITAL_ARREST,
}

_WHITELIST = {category.value: category for category in ScamCategory}


def category_of(result: AnalysisResult) -> ScamCategory:
    for hit in result.rule_hits:  # most severe first
        if hit.severity is Severity.CRITICAL and hit.rule in _RULE_CATEGORIES:
            return _RULE_CATEGORIES[hit.rule]
    label = result.scam_type
    if isinstance(label, str):
        return _WHITELIST.get(label.strip().lower(), ScamCategory.OTHER)
    return ScamCategory.OTHER


def build_alert_summary(result: AnalysisResult, *, has_image: bool) -> AlertSummary | None:
    """None for a message with no obvious red flags: nothing about it can alert anyone."""
    verdict = _VERDICTS.get(result.verdict)
    if verdict is None:
        return None
    return AlertSummary(verdict, category_of(result), SourceKind.IMAGE if has_image else SourceKind.TEXT)
