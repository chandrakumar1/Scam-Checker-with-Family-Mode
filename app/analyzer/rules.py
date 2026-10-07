"""Deterministic scam rules.

Rules are a cautious floor under the LLM: they can raise a verdict, never
lower it. Patterns cover English, Hindi (Devanagari) and Roman Hindi.

Matching runs on normalised text (NFKC, zero-width characters removed,
lower-cased, newlines treated as sentence breaks). Gaps between a verb and its
object never cross a sentence boundary, which keeps "Your OTP is 4821. Do not
share it." from looking like a request.
"""

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Severity(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    severity: Severity
    description: str
    # Any one of these matching triggers the rule.
    patterns: tuple[re.Pattern[str], ...] = ()
    # Alternative trigger: every group must have at least one match.
    combo: tuple[tuple[re.Pattern[str], ...], ...] = ()
    # Discard `patterns` matches that are negated ("never share your OTP").
    negatable: bool = False


class RuleHit(BaseModel):
    rule: str
    severity: Severity
    description: str
    matches: list[str]


def _p(*parts: str) -> re.Pattern[str]:
    return re.compile("".join(parts), re.IGNORECASE)


# --- shared building blocks -------------------------------------------------

# 0..n words that stay inside one sentence.
_GAP = r"(?:[^\s.!?।]+\s+)"
# Characters up to a sentence end.
_SPAN = r"[^.!?।]"
# Roman Hindi imperative endings: karo, karein, kariye, kar do, kar dijiye ...
_ROMAN_IMP = r"(?:o|ein|en|iye|ye|yein|\s+do|\s+dein|\s+dijiye|\s+de|\s+dena|\s+lo|\s+lijiye|\s+lein)\b"

_SECRET_EN = (
    r"(?:o\.?\s?t\.?\s?p|one[\s-]?time[\s-]?pass(?:word|code)|upi\s?pin|m-?pin|"
    r"atm\s?pin|(?:debit\s|credit\s)?card\s?pin|cvv2?|cvc|pass\s?word|passcode|"
    r"security\s+code|verification\s+code|\d[\s-]?digit\s+code)"
)
_SECRET_HI = r"(?:ओटीपी|ओ\.टी\.पी|यूपीआई\s?पिन|एटीएम\s?पिन|पिन(?!\s?कोड)|सीवीवी|पासवर्ड)"
_SECRET = rf"(?:{_SECRET_EN}|{_SECRET_HI})"

_ROMAN_GIVE = (
    rf"(?:bata{_ROMAN_IMP}|bata\b|bhej{_ROMAN_IMP}|share\s+kar{_ROMAN_IMP}|"
    rf"send\s+kar{_ROMAN_IMP}|forward\s+kar{_ROMAN_IMP}|enter\s+kar{_ROMAN_IMP}|"
    rf"de\s+do\b|de\s+dijiye\b|dijiye\b|bol{_ROMAN_IMP}|daal{_ROMAN_IMP}|dal{_ROMAN_IMP})"
)
_HI_GIVE = (
    r"(?:बताएं|बताएँ|बताइए|बताइये|बताओ|बता\s?दें|बता\s?दो|बता\s?दीजिए|"
    r"भेजें|भेजिए|भेजो|भेज\s?दें|भेज\s?दो|भेज\s?दीजिए|"
    r"शेयर\s?करें|शेयर\s?करो|शेयर\s?कीजिए|शेयर\s?कर\s?दें|साझा\s?करें|साझा\s?कीजिए|"
    r"दे\s?दें|दे\s?दो|दें|दीजिए|डालें|डालिए|डालो|दर्ज\s?करें|दर्ज\s?कीजिए|बोलें|बोलिए)"
)

_ROMAN_INSTALL = (
    rf"(?:install\s+kar{_ROMAN_IMP}|download\s+kar{_ROMAN_IMP}|"
    rf"daal{_ROMAN_IMP}|khol{_ROMAN_IMP}|chala{_ROMAN_IMP})"
)
_HI_INSTALL = (
    r"(?:इंस्टॉल\s?करें|इंस्टाल\s?करें|इंस्टॉल\s?कीजिए|इंस्टॉल\s?करो|इंस्टॉल\s?कर\s?लें|"
    r"डाउनलोड\s?करें|डाउनलोड\s?कीजिए|डाउनलोड\s?करो|डाउनलोड\s?कर\s?लें|खोलें|डालें)"
)

# A match is discarded if its own text contains a negation word...
_NEG_IN_SPAN = re.compile(
    r"(?:^|[\s,])(?:not|never|don'?t|mat|na|nahi|nahin|nhi|न|ना|मत|नहीं|नही)(?=[\s,.!?।]|$)"
)
# ...or it is directly preceded by one, or by a sender promise ("we will").
_NEG_BEFORE = re.compile(
    r"(?:\b(?:not|never|don'?t|dont|cannot|can'?t|won'?t|shouldn'?t|no\s+one|nobody)|"
    r"\b(?:we|i|bank)\s+(?:will|would|shall|have|has)|\bwe'?ll|\bi'?ll)"
    r"\s+(?:ever\s+|also\s+)?(?:to\s+|be\s+)?$"
)


# --- rules ------------------------------------------------------------------

OTP_REQUEST = Rule(
    name="otp_request",
    severity=Severity.CRITICAL,
    description="Asks you to share an OTP, UPI PIN, CVV or password",
    negatable=True,
    patterns=(
        # share / tell / give ... OTP
        _p(
            r"\b(?:share|tell|give|provide|forward|disclose|read\s+out|reply\s+with|"
            r"let\s+(?:me|us)\s+know|send\s+(?:me|us|back))\s+",
            _GAP, r"{0,4}?", _SECRET,
        ),
        # OTP ... send it to me
        _p(
            _SECRET, r"\s+", _GAP, r"{0,6}?",
            r"(?:share|send|forward|tell|give|read)\s+(?:it\s+|that\s+|this\s+)?"
            r"(?:to\s+|with\s+)?(?:me|us)\b",
        ),
        # enter OTP in the link / form / here
        _p(
            r"\b(?:enter|type|fill)\s+", _GAP, r"{0,3}?", _SECRET,
            r"\s+(?:here|below|(?:in|on|at|into)\s+(?:the\s+|this\s+|that\s+|our\s+|given\s+|below\s+)?"
            r"(?:link|form|page|website|site|url))",
        ),
        # enter UPI PIN to receive money
        _p(
            r"(?:\b(?:enter|type|daal|dal)|डाल|दर्ज)", _SPAN, r"{0,40}",
            r"(?:upi\s?pin|m-?pin|\bpin\b|यूपीआई\s?पिन|पिन)", _SPAN, r"{0,40}",
            r"(?:to\s+receive|to\s+get|to\s+claim|receive|paane|pane|प्राप्त|पाने)",
        ),
        # what is your OTP / OTP kya hai
        _p(r"\bwhat(?:'s|\s+is)\s+(?:the|your)\s+", _SECRET),
        _p(_SECRET, r"\s+", _GAP, r"{0,2}?(?:kya\s+(?:hai|h|aaya)\b|क्या\s+(?:है|आया))"),
        # OTP bata do / OTP बताइए
        _p(_SECRET, r"\s+", _GAP, r"{0,4}?", _ROMAN_GIVE),
        _p(_SECRET, r"\s+", _GAP, r"{0,4}?", _HI_GIVE),
    ),
)

_APPS = (
    r"(?:any\s?desk|team\s?viewer|quick\s?support\s+app|quicksupport|rust\s?desk|"
    r"air\s?droid|ammyy|ultra\s?viewer|एनी\s?डेस्क|टीम\s?व्यूअर|टीम\s?व्यूवर|"
    r"क्विक\s?सपोर्ट|रस्ट\s?डेस्क)"
)

REMOTE_ACCESS_APP = Rule(
    name="remote_access_app",
    severity=Severity.CRITICAL,
    description="Asks you to install a screen-sharing / remote-control app",
    negatable=True,
    patterns=(
        _p(r"\b(?:install|download|open|use|run|get|set\s?up|setup)\s+", _GAP, r"{0,3}?", _APPS),
        _p(_APPS, r"\s+", _GAP, r"{0,4}?", _ROMAN_INSTALL),
        _p(_APPS, r"\s*", _GAP, r"{0,4}?", _HI_INSTALL),
    ),
)

_APK = r"(?:\bapk\b|एपीके)"

APK_LINK = Rule(
    name="apk_link",
    severity=Severity.CRITICAL,
    description="Sends an Android app file (APK) to download or install",
    negatable=True,
    patterns=(
        _p(r"[\w-]+\.apk\b"),
        _p(r"\b(?:install|download|open)\s+", _GAP, r"{0,3}?", _APK),
        _p(_APK, r"\s+", _GAP, r"{0,4}?(?:install|download|open)\s+(?:it|this|the\s+app|now)\b"),
        _p(_APK, r"\s+", _GAP, r"{0,4}?", _ROMAN_INSTALL),
        _p(_APK, r"\s*", _GAP, r"{0,4}?", _HI_INSTALL),
    ),
)

AUTHORITY_THREAT = Rule(
    name="authority_threat",
    severity=Severity.CRITICAL,
    description="Threatens you with police, CBI, customs or 'digital arrest'",
    patterns=(
        _p(r"\b(?:digital|virtual|online)\s+(?:arrest|giraftari|giraftaari)"),
        _p(r"डिजिटल\s+(?:अरेस्ट|गिरफ्तारी|गिरफ़्तारी)"),
    ),
    combo=(
        (
            _p(
                r"\b(?:police|cbi|c\.b\.i|enforcement\s+directorate|ed\s+officer|customs|"
                r"narcotics|ncb|cyber\s?cell|cyber\s+crime|crime\s+branch|trai|income\s+tax|"
                r"court|judge)\b"
            ),
            _p(r"पुलिस|सीबीआई|कस्टम|नारकोटिक्स|साइबर\s+सेल|क्राइम\s+ब्रांच|अदालत|कोर्ट"),
        ),
        (
            _p(
                r"\b(?:arrest\w*|warrant|fir|f\.i\.r|case\s+(?:has\s+been\s+)?(?:registered|filed|booked)|"
                r"jail|legal\s+action|money\s+laundering|drugs|narcotics|illegal|seized|"
                r"giraftar\w*|kanooni\s+karyawahi)\b"
            ),
            _p(
                r"गिरफ्तार|गिरफ़्तार|वारंट|एफआईआर|केस\s+दर्ज|मुकदमा|जेल|मनी\s+लॉन्ड्रिंग|"
                r"ड्रग्स|गैरकानूनी|अवैध|कानूनी\s+कार्रवाई"
            ),
        ),
        (
            _p(
                r"\b(?:you|your|yours|aap|aapke|aapka|aapki|aapko|aapne|tum|tumhare|"
                r"tumhara|tumhe)\b"
            ),
            _p(r"आप|तुम"),
        ),
    ),
)

SHORTENED_LINK = Rule(
    name="shortened_link",
    severity=Severity.HIGH,
    description="Contains a shortened link that hides where it really goes",
    patterns=(
        _p(
            r"(?<![\w./-])(?:https?://)?(?:www\.)?"
            r"(?:bit\.ly|tinyurl\.com|is\.gd|v\.gd|cutt\.ly|rb\.gy|t\.ly|shorturl\.at|"
            r"ow\.ly|goo\.gl|tiny\.cc|rebrand\.ly|s\.id)/\S+"
        ),
    ),
)

URGENCY_WITH_MOBILE = Rule(
    name="urgency_with_mobile",
    severity=Severity.HIGH,
    description="Pressures you to act fast and gives a personal mobile number",
    combo=(
        (
            _p(
                r"\b(?:urgent(?:ly)?|immediately|right\s+now|asap|at\s+once|"
                r"within\s+\d+\s*(?:hours?|hrs?|minutes?|mins?)|today\s+itself|"
                r"last\s+(?:chance|date|warning)|otherwise|turant|jaldi|foran|fauran|"
                r"warna|varna|abhi\s+(?:call|contact|phone|sampark))\b"
            ),
            _p(r"तुरंत|तुरन्त|फौरन|जल्दी|आज\s+ही|वरना|अभी\s+(?:कॉल|संपर्क|फोन)"),
        ),
        (_p(r"(?<!\d)(?:\+?91[\s-]?|0)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"),),
    ),
)

CLASSIC_LURE = Rule(
    name="classic_lure",
    severity=Severity.MEDIUM,
    description="Uses a common scam story (KYC, electricity, parcel, prize, task job, refund)",
    patterns=(
        # KYC expiry
        _p(
            r"(?:\bkyc\b|केवाईसी|के\.वाई\.सी)", _SPAN, r"{0,40}",
            r"(?:expir|update|pending|suspend|block|band\s+ho|incomplete|nahi\s+hua|"
            r"अपडेट|समाप्त|बंद|ब्लॉक|लंबित|अधूरा)",
        ),
        _p(r"\bupdate\s+(?:your\s+)?kyc\b"),
        # electricity disconnection
        _p(
            r"(?:\belectricity|\bbijli|बिजली|\bpower)", _SPAN, r"{0,40}",
            r"(?:will\s+be\s+(?:disconnected|cut)|disconnect|kaat\s+di|kaat\s+diya|kat\s+jaye|"
            r"kat\s+jayegi|काट\s+दी|काट\s+दिया|कट\s+जाएगा|कट\s+जाएगी|बंद\s+कर\s+दी)",
        ),
        _p(r"\belectricity\s+bill", _SPAN, r"{0,40}(?:not\s+updated|was\s+not\s+updated|previous\s+month)"),
        # parcel / customs
        _p(
            r"(?:\bparcel|\bpackage|\bcourier|\bshipment|\bconsignment|पार्सल|कूरियर)", _SPAN, r"{0,60}",
            r"(?:customs|held|seized|on\s+hold|undeliver|could\s+not\s+be\s+delivered|returned|"
            r"address\s+(?:is\s+)?(?:incomplete|incorrect|wrong)|\bpay\b|\bfee\b|charges?\b|drugs|illegal|"
            r"कस्टम|रोक|जब्त|शुल्क|ड्रग्स|पता\s+गलत|रुका)",
        ),
        # lottery / prize
        _p(
            r"\b(?:lottery|lucky\s+draw|jackpot|prize\s+money|cash\s+prize|you\s+(?:have\s+)?won|"
            r"you'?ve\s+won|kbc|kaun\s+banega\s+crorepati|inaam|(?:lakh|crore)\s+jeete)\b"
        ),
        _p(r"\baapne\s+", _SPAN, r"{0,30}jeet"),
        _p(r"लॉटरी|लकी\s+ड्रा|इनाम|पुरस्कार\s+राशि|आपने\s+", _SPAN, r"{0,30}जीत"),
        # part-time task job
        _p(
            r"\bpart[\s-]?time", _SPAN, r"{0,60}",
            r"(?:earn|income|daily|per\s+day|₹|\brs\.?|task|kamai|kamao|kamaye|commission)",
        ),
        _p(
            r"(?:\bearn|\bkamao|\bkamaye|\bkamaiye|कमाएं|कमाइए|कमाओ)", _SPAN, r"{0,40}",
            r"(?:daily|per\s+day|rozana|\broz\b|ghar\s+baithe|प्रतिदिन|रोज़|रोज|घर\s+बैठे)",
        ),
        _p(r"\bghar\s+baithe\s+(?:kamao|kamaye|paise|earn)|घर\s+बैठे\s+(?:कमाएं|कमाइए|पैसे|कमाई)"),
        _p(
            r"\b(?:like|subscribe|rate|review)", _SPAN, r"{0,30}",
            r"(?:video|youtube|hotel|product|google\s+map)", _SPAN, r"{0,40}",
            r"(?:earn|paid|\bpay|₹|\brs\.?|paise)",
        ),
        _p(r"\btask", _SPAN, r"{0,30}(?:commission|earn|₹|\brs\.?)"),
        _p(r"पार्ट\s+टाइम"),
        # refund
        _p(
            r"(?:\brefund|रिफंड|रिफ़ंड|paisa\s+wapas|पैसा\s+वापस)", _SPAN, r"{0,60}",
            r"(?:click|link|claim|verify|fill|form|details|share|provide|\bupi\b|लिंक|क्लिक|भरें|विवरण)",
        ),
        _p(r"\b(?:claim|receive)\s+(?:your\s+)?refund"),
    ),
)

RULES: tuple[Rule, ...] = (
    OTP_REQUEST,
    REMOTE_ACCESS_APP,
    APK_LINK,
    AUTHORITY_THREAT,
    SHORTENED_LINK,
    URGENCY_WITH_MOBILE,
    CLASSIC_LURE,
)


# --- matching ---------------------------------------------------------------

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).lower()
    text = re.sub(r"\s*\n\s*", " . ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _is_negated(text: str, match: re.Match[str]) -> bool:
    if _NEG_IN_SPAN.search(match.group(0)):
        return True
    prefix = text[max(0, match.start() - 30) : match.start()]
    return bool(_NEG_BEFORE.search(prefix))


def _match_rule(rule: Rule, text: str) -> list[str]:
    found: list[str] = []
    for pattern in rule.patterns:
        for m in pattern.finditer(text):
            if rule.negatable and _is_negated(text, m):
                continue
            found.append(m.group(0).strip())

    if rule.combo:
        combo_found: list[str] = []
        for group in rule.combo:
            group_found = [m.group(0).strip() for p in group for m in p.finditer(text)]
            if not group_found:
                break
            combo_found.extend(group_found)
        else:
            found.extend(combo_found)

    return list(dict.fromkeys(found))


def check_rules(text: str) -> list[RuleHit]:
    """Return every rule that fires on `text`, most severe first."""
    if not text or not text.strip():
        return []
    normalized = normalize(text)
    hits = [
        RuleHit(rule=rule.name, severity=rule.severity, description=rule.description, matches=matches)
        for rule in RULES
        if (matches := _match_rule(rule, normalized))
    ]
    return sorted(hits, key=lambda h: h.severity.rank, reverse=True)
