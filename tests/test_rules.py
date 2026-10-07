import pytest

from app.analyzer.rules import RULES, RuleHit, Severity, check_rules


def rule_names(text: str) -> set[str]:
    return {hit.rule for hit in check_rules(text)}


# rule name -> (positive examples, negative examples)
CASES: dict[str, tuple[list[str], list[str]]] = {
    "otp_request": (
        [
            "Please share the OTP you received to complete verification.",
            "Sir, tell me the OTP that came on your phone.",
            "Your OTP is 4821. Do not share it with anyone. Our executive will call, please tell him the OTP.",
            "Please send me your CVV and card number.",
            "What is your ATM PIN?",
            "Enter your UPI PIN to receive Rs 5000 cashback.",
            "Enter the OTP in the link below to stop the transaction.",
            "OTP aaya hoga, wo bata do jaldi",
            "Aapke phone par jo OTP aaya hai woh bhej dijiye",
            "Apna UPI PIN daal do payment receive karne ke liye",
            "कृपया अपना ओटीपी बताइए",
            "आपके मोबाइल पर आया OTP भेज दीजिए",
        ],
        [
            "Your OTP is 4821. Do not share it with anyone.",
            "Your OTP for login is 739201. It is valid for 10 minutes. Never share your OTP with anyone, including bank staff.",
            "We will send you an OTP on your registered mobile number.",
            "Use OTP 552913 to log in to your account. Valid for 5 minutes.",
            "Enter OTP 4821 to log in. Do not share it.",
            "My password expired so I reset it yesterday.",
            "Please update the pin code in your address.",
            "OTP kisi ko mat batao, bank kabhi nahi poochta.",
            "Kabhi bhi apna UPI PIN kisi ko na batayein.",
            "आपका OTP 4821 है। इसे किसी के साथ साझा न करें।",
            "ओटीपी किसी को न बताएं।",
        ],
    ),
    "remote_access_app": (
        [
            "Please install AnyDesk so I can help you with the refund.",
            "Download TeamViewer QuickSupport from Play Store and tell me the code.",
            "Use AnyDesk so our engineer can fix your bank app.",
            "AnyDesk download karo abhi",
            "RustDesk app install kar lijiye",
            "कृपया एनीडेस्क इंस्टॉल करें",
        ],
        [
            "Our office uses TeamViewer for IT support meetings.",
            "Never install AnyDesk if a stranger asks you to.",
            "Thanks for the quick support yesterday!",
            "AnyDesk ek remote desktop software hai.",
            "मैंने आज अखबार में टीमव्यूअर के बारे में पढ़ा।",
        ],
    ),
    "apk_link": (
        [
            "Install SBI_Rewards.apk to claim your points",
            "Download now: http://pm-kisan-update.in/files/PMKisan.apk",
            "Please download the APK I sent on WhatsApp",
            "Ye APK file download karo aur install karo",
            "यह एपीके फाइल इंस्टॉल करें",
        ],
        [
            "Download our app from the Google Play Store.",
            "Never download an APK sent by strangers.",
            "APK files can install malware on your phone.",
            "Don't install unknown .apk files.",
            "Mera phone Android hai, kaunsa app achha hai?",
            "एपीके क्या होता है?",
        ],
    ),
    "authority_threat": (
        [
            "This is CBI. An arrest warrant has been issued in your name.",
            "You are under digital arrest. Do not disconnect the video call.",
            "Customs has seized a parcel in your name containing drugs.",
            "Mumbai police: aapke naam par FIR darj hai, turant payment karo",
            "आप डिजिटल अरेस्ट में हैं।",
            "आपके पार्सल में ड्रग्स मिले हैं, कस्टम विभाग आपको गिरफ्तार करेगा",
        ],
        [
            "The police helpline number is 112.",
            "The police arrested the thief yesterday.",
            "Mera bhai police mein hai, aap kaise ho?",
            "Court ki tareekh agle mahine hai.",
            "सीबीआई ने आज एक प्रेस कॉन्फ्रेंस की।",
        ],
    ),
    "shortened_link": (
        [
            "Click bit.ly/3xYz12 to verify your account",
            "Update here: https://tinyurl.com/sbi-kyc-2024",
            "Link: cutt.ly/xyz",
            "अपना इनाम लेने के लिए is.gd/abc123 पर जाएं",
        ],
        [
            "Visit https://www.onlinesbi.sbi for net banking.",
            "I want a little bit. Ly said she will come.",
            "Check https://example.com/is.gd/page",
            "Is bit.ly a safe website?",
        ],
    ),
    "urgency_with_mobile": (
        [
            "Urgent! Call me immediately on 98765 43210",
            "Your account will be blocked within 24 hours. Contact 7012345678",
            "Beta, mera phone kharab ho gaya, jaldi is number par paise bhejo 9876543210",
            "तुरंत इस नंबर पर कॉल करें: +91 87654 32109",
        ],
        [
            "Please come home immediately, dinner is ready.",
            "My new number is 98765 43210, save it.",
            "Call our toll-free number 1800-123-4567 urgently for help.",
            "Your OTP is 482913. Valid for 10 minutes.",
            "जल्दी आना, खाना तैयार है।",
        ],
    ),
    "classic_lure": (
        [
            "Dear customer, your KYC has expired. Update now to avoid account block.",
            "Aapka KYC pending hai, turant update karein",
            "प्रिय ग्राहक, आपका केवाईसी अपडेट नहीं है",
            "Dear consumer, your electricity will be disconnected tonight at 9:30 pm.",
            "आपकी बिजली आज रात काट दी जाएगी",
            "Your parcel is held at customs. Pay Rs 49 to release it.",
            "Congratulations! You have won Rs 25 lakh in KBC lucky draw.",
            "Part time job: earn ₹3000 daily by liking YouTube videos",
            "Ghar baithe kamaye 5000 rozana",
            "Your refund of Rs 2340 is ready, click the link to claim it.",
        ],
        [
            "I completed my KYC at the bank yesterday.",
            "There was a power cut in our area yesterday.",
            "Mera parcel kal aa jayega.",
            "India won the match yesterday.",
            "Mera beta part-time job karta hai.",
            "Your refund of Rs 499 has been credited to your account.",
            "रिफंड आपके खाते में जमा हो गया है।",
            "मैंने बिजली का बिल ऑनलाइन भर दिया।",
        ],
    ),
}

EXPECTED_SEVERITY = {
    "otp_request": Severity.CRITICAL,
    "remote_access_app": Severity.CRITICAL,
    "apk_link": Severity.CRITICAL,
    "authority_threat": Severity.CRITICAL,
    "shortened_link": Severity.HIGH,
    "urgency_with_mobile": Severity.HIGH,
    "classic_lure": Severity.MEDIUM,
}

POSITIVES = [(rule, text) for rule, (pos, _) in CASES.items() for text in pos]
NEGATIVES = [(rule, text) for rule, (_, neg) in CASES.items() for text in neg]


def test_every_rule_has_cases():
    assert set(CASES) == {r.name for r in RULES}
    for rule, (pos, neg) in CASES.items():
        assert len(pos) >= 3, rule
        assert len(neg) >= 3, rule


@pytest.mark.parametrize(("rule", "text"), POSITIVES)
def test_rule_fires(rule, text):
    assert rule in rule_names(text)


@pytest.mark.parametrize(("rule", "text"), NEGATIVES)
def test_rule_does_not_fire(rule, text):
    assert rule not in rule_names(text)


def test_genuine_otp_message_triggers_nothing():
    assert check_rules("Your OTP is 4821. Do not share it with anyone.") == []


@pytest.mark.parametrize(("rule", "severity"), EXPECTED_SEVERITY.items())
def test_rule_severity(rule, severity):
    text = CASES[rule][0][0]
    hit = next(h for h in check_rules(text) if h.rule == rule)
    assert hit.severity is severity


def test_hits_are_structured():
    hits = check_rules("Please share the OTP you received.")
    assert len(hits) == 1
    hit = hits[0]
    assert isinstance(hit, RuleHit)
    assert hit.rule == "otp_request"
    assert hit.severity is Severity.CRITICAL
    assert hit.description
    assert hit.matches == ["share the otp"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "URGENT: Your KYC has expired. Click bit.ly/kyc-upd8 and share the OTP or call 9876543210 immediately.",
            {"otp_request", "shortened_link", "urgency_with_mobile", "classic_lure"},
        ),
        (
            "CBI officer: arrest warrant in your name. Install AnyDesk for verification.",
            {"authority_threat", "remote_access_app"},
        ),
        ("Download the APK from bit.ly/app-update", {"apk_link", "shortened_link"}),
        (
            "Aapka parcel customs mein ruka hai, AnyDesk install karo aur OTP batao",
            {"classic_lure", "remote_access_app", "otp_request"},
        ),
        (
            "आपका केवाईसी अपडेट नहीं है, तुरंत 9876543210 पर कॉल करें और ओटीपी बताइए",
            {"classic_lure", "urgency_with_mobile", "otp_request"},
        ),
    ],
)
def test_multiple_rules_fire(text, expected):
    assert rule_names(text) == expected


def test_hits_sorted_most_severe_first():
    hits = check_rules(
        "URGENT: Your KYC has expired. Click bit.ly/kyc-upd8 and share the OTP or call 9876543210 immediately."
    )
    ranks = [h.severity.rank for h in hits]
    assert ranks == sorted(ranks, reverse=True)
    assert hits[0].severity is Severity.CRITICAL
    assert hits[-1].severity is Severity.MEDIUM


@pytest.mark.parametrize(
    "text",
    [
        "Please share the O.T.P you received",
        "Please share the O​TP you received",  # zero-width space
        "PLEASE SHARE THE OTP",
    ],
)
def test_obfuscation_is_normalised(text):
    assert "otp_request" in rule_names(text)


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_empty_text(text):
    assert check_rules(text) == []
