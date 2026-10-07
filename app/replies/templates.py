"""All user-facing text, in English and Hindi.

Wording rules:
- Never call a message "safe", "genuine", "verified" or similar. We can only
  say we found (or did not find) warning signs.
- Short, plain sentences an elderly reader can follow.
- The privacy line says only what is true: messages are sent to an AI service
  to be checked, and Ruko does not intentionally store them afterwards.
"""

# --- appended to every reply -----------------------------------------------------

FOOTER = {
    "en": (
        "If you already shared an OTP or PIN, or paid money, call your bank right away, "
        "then call the cyber fraud helpline 1930 (cybercrime.gov.in)."
    ),
    "hi": (
        "अगर आपने OTP या पिन बता दिया है, या पैसे भेज दिए हैं, तो तुरंत अपने बैंक को फ़ोन करें, "
        "फिर साइबर ठगी हेल्पलाइन 1930 पर फ़ोन करें (cybercrime.gov.in)।"
    ),
}

# --- static messages ---------------------------------------------------------------

START = {
    "en": (
        "👋 Hello! I am Ruko. I am here to help you check whether a message might be a scam.\n\n"
        "Forward or paste any message here, or send a screenshot or photo of it. "
        "You will get a reply saying whether it shows warning signs and what to do.\n\n"
        "I can make mistakes. Whatever a message says, never tell anyone your OTP, PIN or password."
    ),
    "hi": (
        "👋 नमस्ते! मैं रुको हूँ। यह जाँचने में आपकी मदद के लिए हूँ कि कोई संदेश ठगी (स्कैम) तो नहीं।\n\n"
        "कोई भी संदेश यहाँ फ़ॉरवर्ड करें या पेस्ट करें, या उसका स्क्रीनशॉट या फ़ोटो भेजें। "
        "आपको जवाब मिलेगा कि उसमें खतरे के निशान हैं या नहीं, और क्या करना चाहिए।\n\n"
        "मुझसे गलती हो सकती है। संदेश में कुछ भी लिखा हो, किसी को भी अपना OTP, पिन या पासवर्ड कभी न बताएँ।"
    ),
}

HELP = {
    "en": (
        "ℹ️ How to use Ruko\n\n"
        "• Forward or paste a message here.\n"
        "• Or send a screenshot or photo of it (up to 5 MB).\n"
        "• You will be told if it looks like a scam, looks suspicious, or if no obvious warning signs were found.\n\n"
        "I cannot check voice notes, videos, PDFs or other files yet.\n\n"
        "Privacy: to check your message or photo, it is sent to an AI service. "
        "Ruko does not intentionally store your messages or images after checking.\n\n"
        "Remember: no bank or official will ever ask you for an OTP, PIN or password."
    ),
    "hi": (
        "ℹ️ रुको कैसे इस्तेमाल करें\n\n"
        "• कोई भी संदेश यहाँ फ़ॉरवर्ड करें या पेस्ट करें।\n"
        "• या उसका स्क्रीनशॉट या फ़ोटो भेजें (5 MB तक)।\n"
        "• जवाब में बताया जाएगा कि वह ठगी लगती है, संदिग्ध है, या कोई साफ़ खतरे का निशान नहीं मिला।\n\n"
        "अभी वॉइस नोट, वीडियो, PDF या दूसरी फ़ाइलें नहीं जाँची जा सकतीं।\n\n"
        "गोपनीयता: जाँच के लिए आपका संदेश या फ़ोटो एक AI सेवा को भेजा जाता है। "
        "रुको जाँच के बाद आपके संदेश या फ़ोटो को जानबूझकर सहेजता नहीं है।\n\n"
        "याद रखें: कोई भी बैंक या अधिकारी आपसे OTP, पिन या पासवर्ड नहीं माँगता।"
    ),
}

HELP_EMERGENCY = {
    "en": "If you think you already lost money or shared an OTP, PIN or password, send /emergency.",
    "hi": "अगर आपको लगे कि आपके पैसे जा चुके हैं या आपने OTP, पिन या पासवर्ड बता दिया है, तो /emergency भेजें।",
}

HELP_FAMILY = {
    "en": (
        "Family helpers: add a relative who gets a message if you may have received a scam. Send /family. "
        "To stop helping someone send /leave, and to delete your family data send /deleteme."
    ),
    "hi": (
        "पारिवारिक सहायक: किसी रिश्तेदार को जोड़ें, जिन्हें खबर मिलेगी अगर आपको ठगी वाला संदेश मिला हो। /family भेजें। "
        "किसी की मदद बंद करने के लिए /leave, और अपना पारिवारिक डेटा मिटाने के लिए /deleteme भेजें।"
    ),
}

UNSUPPORTED = {
    "en": (
        "I can only check text messages, screenshots and photos right now. "
        "Please send the message as text, or as a screenshot."
    ),
    "hi": (
        "अभी सिर्फ़ टेक्स्ट संदेश, स्क्रीनशॉट और फ़ोटो की जाँच हो सकती है। "
        "कृपया संदेश को टेक्स्ट की तरह, या स्क्रीनशॉट बनाकर भेजें।"
    ),
}

IMAGE_TOO_LARGE = {
    "en": (
        "That picture is too big for me (the limit is 5 MB). "
        "Please send a smaller screenshot, or copy the text of the message and paste it here."
    ),
    "hi": (
        "यह फ़ोटो बहुत बड़ी है (सीमा 5 MB है)। "
        "कृपया छोटा स्क्रीनशॉट भेजें, या संदेश का लिखा हुआ हिस्सा कॉपी करके यहाँ पेस्ट करें।"
    ),
}

IMAGE_UNREADABLE = {
    "en": (
        "I could not read that picture. Please send a screenshot as a JPG, PNG, WebP or GIF image, "
        "or paste the text of the message."
    ),
    "hi": (
        "यह फ़ोटो पढ़ी नहीं जा सकी। कृपया JPG, PNG, WebP या GIF तस्वीर में स्क्रीनशॉट भेजें, "
        "या संदेश का लिखा हुआ हिस्सा पेस्ट करें।"
    ),
}

ERROR = {
    "en": (
        "Sorry, I could not check this message properly just now. Please be careful: "
        "do not open any link, and do not tell anyone your OTP, PIN or password. "
        "You can try sending it again in a minute."
    ),
    "hi": (
        "माफ़ कीजिए, अभी इस संदेश की ठीक से जाँच नहीं हो पाई। सावधान रहें: "
        "कोई लिंक न खोलें, और किसी को OTP, पिन या पासवर्ड न बताएँ। "
        "एक मिनट बाद इसे फिर से भेजकर देखें।"
    ),
}

# --- analysis replies --------------------------------------------------------------

HEADLINE = {
    "likely_scam": {
        "en": "🚨 This looks like a SCAM.",
        "hi": "🚨 यह ठगी (स्कैम) लगती है।",
    },
    "suspicious": {
        "en": "⚠️ Be careful. This message looks suspicious.",
        "hi": "⚠️ सावधान! यह संदेश संदिग्ध लगता है।",
    },
    "no_obvious_red_flags": {
        "en": "🔎 I did not find obvious warning signs.",
        "hi": "🔎 मुझे कोई साफ़ खतरे का निशान नहीं मिला।",
    },
}

# Always shown with the "no obvious red flags" verdict.
CLEAR_NOTE = {
    "en": "That does not mean the message can be trusted. Scammers can make messages look very real.",
    "hi": "इसका मतलब यह नहीं कि संदेश पर भरोसा किया जा सकता है। ठग संदेश को बहुत सच्चा जैसा बना सकते हैं।",
}

SECTION_WHY = {"en": "Why:", "hi": "क्यों:"}
SECTION_TODO = {"en": "What to do:", "hi": "क्या करें:"}

LINK_REMOVED = {"en": "[link removed]", "hi": "[लिंक हटाया गया]"}

LLM_UNAVAILABLE = {
    "en": "I could not fully check this message right now, so please treat it as suspicious.",
    "hi": "अभी इस संदेश की पूरी जाँच नहीं हो पाई, इसलिए इसे संदिग्ध मानें।",
}

# Used when the LLM's own explanation cannot be shown (it failed, or the rules
# raised the verdict above what it said).
GENERIC_STEPS = {
    "likely_scam": {
        "en": [
            "Do not open any link or file in it.",
            "Do not tell anyone your OTP, PIN, password or card number.",
            "Do not send money. Block the sender.",
        ],
        "hi": [
            "इसका कोई लिंक या फ़ाइल न खोलें।",
            "किसी को OTP, पिन, पासवर्ड या कार्ड नंबर न बताएँ।",
            "पैसे न भेजें। भेजने वाले को ब्लॉक कर दें।",
        ],
    },
    "suspicious": {
        "en": [
            "Do not open links, and do not tell anyone your OTP, PIN or password.",
            "To check, call the person or company on a number you already have, "
            "such as the one on your bank card or the official website.",
        ],
        "hi": [
            "लिंक न खोलें, और किसी को OTP, पिन या पासवर्ड न बताएँ।",
            "जाँच के लिए उस व्यक्ति या कंपनी को ऐसे नंबर पर फ़ोन करें जो आपके पास पहले से है, "
            "जैसे बैंक कार्ड या आधिकारिक वेबसाइट पर लिखा नंबर।",
        ],
    },
    "no_obvious_red_flags": {
        "en": [
            "Still, never tell anyone your OTP, PIN or password.",
            "If the message asks for money or personal details, ask the sender by calling "
            "a number you already have.",
        ],
        "hi": [
            "फिर भी किसी को OTP, पिन या पासवर्ड न बताएँ।",
            "अगर संदेश में पैसे या निजी जानकारी माँगी गई है, तो भेजने वाले को ऐसे नंबर पर फ़ोन करके पूछें "
            "जो आपके पास पहले से है।",
        ],
    },
}

# Plain-language reason for each rule that fired (keyed by Rule.name).
RULE_REASONS = {
    "otp_request": {
        "en": "It asks you for an OTP, PIN, CVV or password. Banks and officials never ask for these.",
        "hi": "इसमें OTP, पिन, CVV या पासवर्ड माँगा गया है। बैंक या सरकारी अधिकारी ये कभी नहीं माँगते।",
    },
    "remote_access_app": {
        "en": (
            "It asks you to install a screen-sharing app such as AnyDesk or TeamViewer. "
            "This lets scammers see and control your phone."
        ),
        "hi": (
            "इसमें AnyDesk या TeamViewer जैसा ऐप डालने को कहा गया है। "
            "इससे ठग आपका फ़ोन देख सकते हैं और चला सकते हैं।"
        ),
    },
    "apk_link": {
        "en": "It asks you to download an app file (APK). Fake apps can steal your money and information.",
        "hi": "इसमें ऐप फ़ाइल (APK) डाउनलोड करने को कहा गया है। नकली ऐप आपके पैसे और जानकारी चुरा सकते हैं।",
    },
    "authority_threat": {
        "en": (
            "It scares you with the police, CBI or customs, or talks of a \"digital arrest\". "
            "Real officers never do this on a phone call or chat."
        ),
        "hi": (
            "इसमें पुलिस, CBI या कस्टम का डर दिखाया गया है, या \"डिजिटल अरेस्ट\" की बात की गई है। "
            "असली अधिकारी फ़ोन या चैट पर ऐसा कभी नहीं करते।"
        ),
    },
    "shortened_link": {
        "en": "It has a shortened link, which hides where it really goes.",
        "hi": "इसमें छोटा किया हुआ लिंक है, जिससे पता नहीं चलता कि वह असल में कहाँ ले जाएगा।",
    },
    "urgency_with_mobile": {
        "en": "It rushes you to act fast and gives a personal mobile number to contact.",
        "hi": "इसमें जल्दी करने का दबाव है और संपर्क के लिए एक निजी मोबाइल नंबर दिया गया है।",
    },
    "classic_lure": {
        "en": (
            "It uses a common scam story, such as KYC, an electricity bill, a parcel, "
            "a prize, an easy online job or a refund."
        ),
        "hi": (
            "इसमें ठगों की आम कहानी है, जैसे KYC, बिजली का बिल, पार्सल, इनाम, "
            "आसान ऑनलाइन नौकरी या रिफ़ंड।"
        ),
    },
}
