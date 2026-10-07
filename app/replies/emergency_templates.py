"""Text for the Emergency flow and for family alerts, in English and Hindi.

Rules for this file:
- Everything an alert to a helper can say is here, as fixed wording. The only
  things ever filled in are the protected person's sanitised name and, when a
  bounded AlertSummary exists, one fixed phrase from CATEGORY_PHRASES. Nothing
  free-form (message text, rule names, LLM text, URLs) has a place to go.
- The emergency guidance is static: it needs no database, no LLM, no network
  beyond Telegram itself.
- Neutral wording ("they", passive Hindi) so it does not depend on anyone's gender.
"""

QUESTION = {
    "en": "Did you already send money, OTP, PIN, password, or card details?",
    "hi": "क्या आपने पैसे, OTP, पिन, पासवर्ड या कार्ड की जानकारी पहले ही भेज दी है?",
}

BUTTONS = {
    "emergency": {"en": "🆘 Emergency help", "hi": "🆘 आपातकालीन मदद"},
    "yes": {"en": "Yes", "hi": "हाँ"},
    "no": {"en": "No", "hi": "नहीं"},
}

# Shown over a pressed button (no footer: they must stay tiny).
POPUPS = {
    "invalid": {
        "en": "This button does not work. Send /emergency if you need help.",
        "hi": "यह बटन काम नहीं करता। मदद चाहिए तो /emergency भेजें।",
    },
    "expired": {
        "en": "This button has expired. Send /emergency if you need help.",
        "hi": "यह बटन पुराना हो गया है। मदद चाहिए तो /emergency भेजें।",
    },
}

# Messages that are sent WITHOUT the standard footer, because they carry the
# 1930 steps themselves (or are inserted into a message that already has one).
NO_FOOTER = frozenset({"guidance", "emergency_alert", "emergency_alert_typed", "told_notice"})

MESSAGES: dict[str, dict[str, str]] = {
    # --- to the protected person
    "guidance": {
        "en": (
            "🆘 ACT NOW. Do these 3 things:\n\n"
            "1. Call 1930 (the cyber fraud helpline) right now.\n"
            "2. Call your bank, on the number printed on your card, and ask them to block your card and UPI.\n"
            "3. Do not send any more money, whatever anyone tells you.\n\n"
            "Call someone you trust now."
        ),
        "hi": (
            "🆘 अभी यह करें। ये 3 काम कीजिए:\n\n"
            "1. साइबर ठगी हेल्पलाइन 1930 पर अभी फ़ोन करें।\n"
            "2. अपने बैंक को फ़ोन करें (कार्ड पर छपे नंबर पर) और कार्ड तथा UPI बंद करवाएँ।\n"
            "3. और पैसे न भेजें, कोई कुछ भी कहे।\n\n"
            "अभी किसी भरोसेमंद व्यक्ति को फ़ोन करें।"
        ),
    },
    "no_response": {
        "en": (
            "Good. Please do not send any money, and do not tell anyone your OTP, PIN, password or card details. "
            "If you ever need help, send /emergency."
        ),
        "hi": (
            "अच्छा है। कृपया पैसे न भेजें, और किसी को अपना OTP, पिन, पासवर्ड या कार्ड की जानकारी न बताएँ। "
            "कभी मदद चाहिए तो /emergency भेजें।"
        ),
    },
    "told_all": {
        "en": "I have told {names}. They will try to call you. Please call 1930 and your bank now.",
        "hi": "मैंने {names} को खबर दे दी है। वे आपको फ़ोन करने की कोशिश करेंगे। कृपया अभी 1930 और अपने बैंक को फ़ोन करें।",
    },
    "told_some": {
        "en": (
            "I told {reached}, but could not reach {failed}. Please call {failed} yourself, "
            "and call 1930 and your bank now."
        ),
        "hi": (
            "मैंने {reached} को खबर दे दी, लेकिन {failed} तक संदेश नहीं पहुँचा। कृपया {failed} को खुद फ़ोन करें, "
            "और अभी 1930 तथा अपने बैंक को फ़ोन करें।"
        ),
    },
    "told_none": {
        "en": "I could not reach your family helper(s). Please call them yourself, or call someone you trust now.",
        "hi": (
            "आपके पारिवारिक सहायक तक संदेश नहीं पहुँच सका। कृपया उन्हें खुद फ़ोन करें, "
            "या अभी किसी भरोसेमंद व्यक्ति को फ़ोन करें।"
        ),
    },
    "no_helpers": {
        "en": (
            "You have no family helper linked, so I could not alert anyone. "
            "Call someone you trust now. You can add a helper with /family."
        ),
        "hi": (
            "आपका कोई पारिवारिक सहायक जुड़ा नहीं है, इसलिए किसी को खबर नहीं दी जा सकी। "
            "अभी किसी भरोसेमंद व्यक्ति को फ़ोन करें। आप /family से सहायक जोड़ सकते हैं।"
        ),
    },
    "family_off": {
        "en": "Family alerts are not available, so I could not alert anyone. Call someone you trust now.",
        "hi": "पारिवारिक खबर की सुविधा उपलब्ध नहीं है, इसलिए किसी को खबर नहीं दी जा सकी। अभी किसी भरोसेमंद व्यक्ति को फ़ोन करें।",
    },
    "deduped": {
        "en": "I already alerted your family helper a moment ago. Please call them, and call 1930 now.",
        "hi": (
            "आपके पारिवारिक सहायक को अभी कुछ देर पहले ही खबर दी जा चुकी है। "
            "कृपया उन्हें फ़ोन करें, और अभी 1930 पर फ़ोन करें।"
        ),
    },
    "error": {
        "en": "I could not alert your family helpers right now. Please call someone you trust now.",
        "hi": "अभी आपके पारिवारिक सहायकों को खबर नहीं दी जा सकी। कृपया अभी किसी भरोसेमंद व्यक्ति को फ़ोन करें।",
    },
    # --- added to a scam-check reply, and the follow-up when a normal alert could not be delivered
    "told_notice": {
        "en": "I am also telling your family helper(s): {names}.",
        "hi": "आपके पारिवारिक सहायक को भी खबर दी जा रही है: {names}।",
    },
    "normal_alert_failed": {
        "en": "I could not reach {names}. Please call them yourself.",
        "hi": "{names} तक संदेश नहीं पहुँच सका। कृपया उन्हें खुद फ़ोन करें।",
    },
    # --- to a family helper
    "normal_alert": {
        "en": (
            "⚠️ “{name}” may have received a scam message just now. It looked like {phrase}.\n\n"
            "Please call “{name}” on a number you already have and check that they are OK."
        ),
        "hi": (
            "⚠️ “{name}” को अभी ठगी वाला संदेश मिला हो सकता है। यह {phrase} जैसा लगा।\n\n"
            "कृपया “{name}” को उस नंबर पर फ़ोन करें जो आपके पास पहले से है, और पूछें कि वे ठीक हैं।"
        ),
    },
    "emergency_alert": {
        "en": (
            "🆘 EMERGENCY. “{name}” says they may have already sent money or shared an OTP, PIN, password or "
            "card details after a scam message.\n\n"
            "Call “{name}” right now, on a number you already have. "
            "Help them call 1930 and their bank to block their card and UPI."
        ),
        "hi": (
            "🆘 आपातकाल। “{name}” का कहना है कि ठगी वाले संदेश के बाद उन्होंने शायद पैसे भेज दिए हैं या "
            "OTP, पिन, पासवर्ड या कार्ड की जानकारी बता दी है।\n\n"
            "“{name}” को अभी फ़ोन करें, उस नंबर पर जो आपके पास पहले से है। "
            "1930 और उनके बैंक को फ़ोन करके उनका कार्ड और UPI बंद करवाने में उनकी मदद करें।"
        ),
    },
    "emergency_alert_typed": {
        "en": (
            "🆘 EMERGENCY. “{name}” says they may have already sent money or shared an OTP, PIN, password or "
            "card details after a scam message. It looked like {phrase}.\n\n"
            "Call “{name}” right now, on a number you already have. "
            "Help them call 1930 and their bank to block their card and UPI."
        ),
        "hi": (
            "🆘 आपातकाल। “{name}” का कहना है कि ठगी वाले संदेश के बाद उन्होंने शायद पैसे भेज दिए हैं या "
            "OTP, पिन, पासवर्ड या कार्ड की जानकारी बता दी है। यह {phrase} जैसा लगा था।\n\n"
            "“{name}” को अभी फ़ोन करें, उस नंबर पर जो आपके पास पहले से है। "
            "1930 और उनके बैंक को फ़ोन करके उनका कार्ड और UPI बंद करवाने में उनकी मदद करें।"
        ),
    },
}

# The one place a scam category becomes words. Each entry completes
# "It looked like ..." and nothing else can be inserted into an alert.
CATEGORY_PHRASES: dict[str, dict[str, str]] = {
    "otp_fraud": {"en": "an OTP or PIN scam", "hi": "OTP या पिन की ठगी"},
    "remote_access": {"en": "a scam asking to install a screen-sharing app", "hi": "स्क्रीन-शेयरिंग ऐप डलवाने वाली ठगी"},
    "malicious_apk": {"en": "a scam with a fake app file", "hi": "नकली ऐप फ़ाइल वाली ठगी"},
    "digital_arrest": {"en": "a \"digital arrest\" scam", "hi": "\"डिजिटल अरेस्ट\" की ठगी"},
    "impersonation": {"en": "a scam pretending to be someone else", "hi": "किसी और का रूप धरने वाली ठगी"},
    "kyc_update": {"en": "a fake KYC scam", "hi": "नकली KYC वाली ठगी"},
    "electricity_bill": {"en": "a fake electricity bill scam", "hi": "नकली बिजली बिल वाली ठगी"},
    "parcel_customs": {"en": "a fake parcel or customs scam", "hi": "नकली पार्सल या कस्टम वाली ठगी"},
    "lottery_prize": {"en": "a fake prize or lottery scam", "hi": "नकली इनाम या लॉटरी वाली ठगी"},
    "task_job": {"en": "a fake online job scam", "hi": "नकली ऑनलाइन नौकरी वाली ठगी"},
    "refund": {"en": "a fake refund scam", "hi": "नकली रिफ़ंड वाली ठगी"},
    "investment": {"en": "an investment scam", "hi": "निवेश की ठगी"},
    "loan": {"en": "a loan scam", "hi": "लोन की ठगी"},
    "sextortion": {"en": "a blackmail scam", "hi": "धमकी देकर पैसे माँगने वाली ठगी"},
    "other": {"en": "a scam", "hi": "ठगी"},
}
