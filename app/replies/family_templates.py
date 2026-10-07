"""Text for the Family Mode flows, in English and Hindi.

Placeholders are filled by family_formatter, which sanitises every name first.
The Hindi uses passive or neutral wording so it does not depend on anyone's gender.

Wording rules (same as the rest of Ruko): never claim a message is "safe";
plain short sentences; and say only what is true. A family helper learns that
the person may have received a scam and what kind, never what the message said.
"""

LANGS = ("en", "hi")

FALLBACK_NAME = {"en": "Family member", "hi": "परिवार का सदस्य"}

# --- messages (each ends with the standard 1930 footer when sent) --------------------------------------------

MESSAGES: dict[str, dict[str, str]] = {
    # /family menu
    "menu_intro": {
        "en": (
            "👨‍👩‍👧 Family helpers\n\n"
            "A family helper is someone you trust. I message them if I think you received a scam message. "
            "They never see your messages, photos or links."
        ),
        "hi": (
            "👨‍👩‍👧 पारिवारिक सहायक\n\n"
            "पारिवारिक सहायक वह व्यक्ति है जिस पर आप भरोसा करते हैं। अगर लगे कि आपको ठगी वाला संदेश मिला है, "
            "तो उन्हें खबर दी जाती है। वे आपके संदेश, फ़ोटो या लिंक कभी नहीं देखते।"
        ),
    },
    "menu_no_helpers": {
        "en": "You have no family helpers yet.",
        "hi": "अभी आपका कोई पारिवारिक सहायक नहीं है।",
    },
    "menu_helpers_title": {"en": "Your family helpers:", "hi": "आपके पारिवारिक सहायक:"},
    "menu_helping_title": {"en": "You are a family helper for:", "hi": "आप इनके पारिवारिक सहायक हैं:"},
    "menu_helping_hint": {"en": "To stop, send /leave.", "hi": "रुकने के लिए /leave भेजें।"},
    # informed consent, before an invitation is made
    "add_notice": {
        "en": (
            "Before I make an invitation link, please read this.\n\n"
            "If a relative becomes your family helper:\n"
            "• I will message them when I think you received a scam message.\n"
            "• They will only learn that, and what kind of scam it looked like (for example \"an OTP scam\"). "
            "They will NOT see your messages, photos or links.\n"
            "• I will always tell you when I alert them.\n"
            "• You can remove them any time with /family, and they can stop any time.\n\n"
            "Do you agree?"
        ),
        "hi": (
            "निमंत्रण का लिंक बनाने से पहले कृपया यह पढ़ लें।\n\n"
            "अगर आपका कोई रिश्तेदार आपका पारिवारिक सहायक बनता है:\n"
            "• जब लगेगा कि आपको ठगी वाला संदेश मिला है, तो उन्हें खबर दी जाएगी।\n"
            "• उन्हें सिर्फ़ इतना पता चलेगा, और ठगी किस तरह की लगी (जैसे \"OTP की ठगी\")। "
            "वे आपके संदेश, फ़ोटो या लिंक नहीं देखेंगे।\n"
            "• उन्हें खबर दी जाएगी तो आपको भी बताया जाएगा।\n"
            "• आप उन्हें कभी भी /family से हटा सकते हैं, और वे भी कभी भी रुक सकते हैं।\n\n"
            "क्या आप सहमत हैं?"
        ),
    },
    "invite_created": {
        "en": (
            "Here is your invitation. Send the message below to your relative (for example on WhatsApp):\n\n"
            "— — —\n"
            "Hi, I use Ruko to check scam messages. Please be my family helper. "
            "Tap this link and press Start:\n{link}\n"
            "— — —\n\n"
            "The link works once and expires in {hours} hours. "
            "After they accept, I will ask you to confirm that it is really them."
        ),
        "hi": (
            "यह आपका निमंत्रण है। नीचे का संदेश अपने रिश्तेदार को भेजें (जैसे WhatsApp पर):\n\n"
            "— — —\n"
            "नमस्ते! कृपया रुको पर मेरे पारिवारिक सहायक बनिए। यह लिंक दबाइए और Start दबाइए:\n{link}\n"
            "— — —\n\n"
            "लिंक एक ही बार काम करता है और {hours} घंटे में खत्म हो जाता है। "
            "जब वे हाँ कहेंगे, तब आपसे पूछा जाएगा कि क्या यही सही व्यक्ति हैं।"
        ),
    },
    "invite_limit_pending": {
        "en": "You already have {n} invitations waiting. Use one or cancel one in /family first.",
        "hi": "आपके {n} निमंत्रण पहले से इंतज़ार में हैं। पहले /family में किसी को इस्तेमाल करें या रद्द करें।",
    },
    "invite_limit_helpers": {
        "en": "You already have the most family helpers allowed ({n}). Remove one in /family first.",
        "hi": "आपके पास पारिवारिक सहायकों की अधिकतम संख्या ({n}) पहले से है। पहले /family में किसी को हटाएँ।",
    },
    "unavailable": {
        "en": "Family helpers are not available right now. Please try again later.",
        "hi": "पारिवारिक सहायक की सुविधा अभी उपलब्ध नहीं है। कृपया बाद में फिर कोशिश करें।",
    },
    "add_cancelled": {"en": "OK, no invitation was made.", "hi": "ठीक है, कोई निमंत्रण नहीं बनाया गया।"},
    # the person who opened an invitation link
    "invite_invalid": {
        "en": "This invitation link is no longer valid. Please ask your relative for a new one.",
        "hi": "यह निमंत्रण लिंक अब मान्य नहीं है। कृपया अपने रिश्तेदार से नया लिंक माँगें।",
    },
    "invite_self": {
        "en": "This is your own invitation. Send it to a relative, not to yourself.",
        "hi": "यह आपका अपना निमंत्रण है। इसे किसी रिश्तेदार को भेजें, खुद को नहीं।",
    },
    "invite_already": {
        "en": "You are already connected with this person, or waiting for them to answer.",
        "hi": "आप इस व्यक्ति से पहले से जुड़े हैं, या उनके जवाब का इंतज़ार है।",
    },
    "helper_prompt": {
        "en": (
            "“{name}” asked you to be their family helper on Ruko.\n\n"
            "Ruko checks messages for scams. If you accept:\n"
            "• I will message you when I think “{name}” received a scam message, and tell you what to do.\n"
            "• You will NOT see their messages, photos or links.\n"
            "• “{name}” will always be told when you are alerted.\n"
            "• Either of you can stop at any time (you can send /leave).\n\n"
            "Do you accept?"
        ),
        "hi": (
            "“{name}” ने आपसे रुको पर अपना पारिवारिक सहायक बनने को कहा है।\n\n"
            "रुको संदेशों में ठगी पहचानने में मदद करता है। अगर आप स्वीकार करते हैं:\n"
            "• जब लगेगा कि “{name}” को ठगी वाला संदेश मिला है, तो आपको खबर दी जाएगी और बताया जाएगा कि क्या करना है।\n"
            "• आप उनके संदेश, फ़ोटो या लिंक नहीं देखेंगे।\n"
            "• आपको खबर दी जाएगी तो “{name}” को भी बताया जाएगा।\n"
            "• आप दोनों में से कोई भी कभी भी रुक सकता है (आप /leave भेज सकते हैं)।\n\n"
            "क्या आप स्वीकार करते हैं?"
        ),
    },
    "helper_wait": {
        "en": "Thank you. Now “{name}” is being asked to confirm that it is you. You will be told when they answer.",
        "hi": "धन्यवाद। अब “{name}” से पुष्टि करने को कहा जा रहा है कि यह आप ही हैं। उनके जवाब देने पर आपको बताया जाएगा।",
    },
    "helper_declined": {
        "en": "OK. You did not accept, so you are not a family helper for “{name}”.",
        "hi": "ठीक है। आपने स्वीकार नहीं किया, इसलिए आप “{name}” के पारिवारिक सहायक नहीं हैं।",
    },
    "helper_declined_notice": {
        "en": "“{name}” did not accept your invitation.",
        "hi": "“{name}” ने आपका निमंत्रण स्वीकार नहीं किया।",
    },
    # the protected person confirms who accepted
    "confirm_prompt": {
        "en": (
            "“{name}”{handle} accepted your invitation to be your family helper.\n\n"
            "Is this the person you meant? Only press Yes if you know them."
        ),
        "hi": (
            "“{name}”{handle} ने आपका पारिवारिक सहायक बनने का निमंत्रण स्वीकार किया है।\n\n"
            "क्या आपने इन्हीं को चुना था? केवल तभी हाँ दबाएँ जब आप इन्हें जानते हों।"
        ),
    },
    "confirmed_protected": {
        "en": (
            "Done. “{name}” is now your family helper. I will message them if I think you received a scam, "
            "and I will always tell you when I do. You can remove them any time with /family."
        ),
        "hi": (
            "हो गया। “{name}” अब आपके पारिवारिक सहायक हैं। अगर लगेगा कि आपको ठगी वाला संदेश मिला है, तो उन्हें खबर दी जाएगी, "
            "और ऐसा होने पर आपको भी बताया जाएगा। आप उन्हें कभी भी /family से हटा सकते हैं।"
        ),
    },
    "confirmed_helper": {
        "en": (
            "You are now a family helper for “{name}”. I will message you if I think they received a scam. "
            "You can stop any time with /leave."
        ),
        "hi": (
            "आप अब “{name}” के पारिवारिक सहायक हैं। अगर लगेगा कि उन्हें ठगी वाला संदेश मिला है, तो आपको खबर दी जाएगी। "
            "आप कभी भी /leave से रुक सकते हैं।"
        ),
    },
    "not_confirmed_protected": {
        "en": "OK. “{name}” was not added as your family helper.",
        "hi": "ठीक है। “{name}” को आपके पारिवारिक सहायक के रूप में नहीं जोड़ा गया।",
    },
    "not_confirmed_helper": {
        "en": "“{name}” did not confirm, so you are not their family helper.",
        "hi": "“{name}” ने पुष्टि नहीं की, इसलिए आप उनके पारिवारिक सहायक नहीं हैं।",
    },
    "unreachable_notice": {
        "en": "“{name}” could not be reached to confirm. Please ask them to open Ruko and send /family.",
        "hi": "पुष्टि के लिए “{name}” तक संदेश नहीं पहुँच सका। कृपया उनसे कहें कि रुको खोलकर /family भेजें।",
    },
    # ending a link
    "removed_protected": {
        "en": "I removed “{name}” as your family helper.",
        "hi": "मैंने “{name}” को आपके पारिवारिक सहायक से हटा दिया।",
    },
    "invite_cancelled": {"en": "The invitation was cancelled.", "hi": "निमंत्रण रद्द कर दिया गया।"},
    "removed_notice_helper": {
        "en": "“{name}” removed you as their family helper. You will not get alerts about them any more.",
        "hi": "“{name}” ने आपको अपने पारिवारिक सहायक से हटा दिया है। अब आपको उनके बारे में खबर नहीं मिलेगी।",
    },
    "leave_prompt": {
        "en": "You are a family helper for the people below. Press a button to stop helping someone.",
        "hi": "आप नीचे दिए लोगों के पारिवारिक सहायक हैं। किसी की मदद बंद करने के लिए बटन दबाएँ।",
    },
    "nothing_to_leave": {
        "en": "You are not a family helper for anyone.",
        "hi": "आप किसी के पारिवारिक सहायक नहीं हैं।",
    },
    "left_helper": {
        "en": "You are no longer a family helper for “{name}”.",
        "hi": "आप अब “{name}” के पारिवारिक सहायक नहीं हैं।",
    },
    "left_notice_protected": {
        "en": "“{name}” stopped being your family helper. They will not get alerts any more.",
        "hi": "“{name}” अब आपके पारिवारिक सहायक नहीं रहे। उन्हें अब खबर नहीं मिलेगी।",
    },
    # /deleteme
    "delete_prompt": {
        "en": (
            "This will delete everything Ruko keeps about you for Family Mode: your family links and your alert "
            "history. The people linked with you will be told. This cannot be undone.\n\n"
            "Delete it?"
        ),
        "hi": (
            "इससे पारिवारिक सुविधा के लिए रुको में आपके बारे में जो कुछ रखा है, सब मिट जाएगा: आपके पारिवारिक संबंध और "
            "खबरों का रिकॉर्ड। आपसे जुड़े लोगों को बताया जाएगा। इसे वापस नहीं किया जा सकता।\n\n"
            "क्या मिटा दें?"
        ),
    },
    "delete_done": {
        "en": "Done. Your Family Mode data was deleted.",
        "hi": "हो गया। पारिवारिक सुविधा में आपका डेटा मिटा दिया गया।",
    },
    "delete_nothing": {
        "en": "Ruko has no Family Mode data about you.",
        "hi": "पारिवारिक सुविधा में रुको के पास आपका कोई डेटा नहीं है।",
    },
    "delete_cancelled": {"en": "OK, nothing was deleted.", "hi": "ठीक है, कुछ नहीं मिटाया गया।"},
    "delete_stale": {
        "en": "That button is too old. Send /deleteme again if you still want to delete your data.",
        "hi": "वह बटन बहुत पुराना है। अगर आप अब भी अपना डेटा मिटाना चाहते हैं, तो /deleteme फिर से भेजें।",
    },
    "delete_notice_helper": {
        "en": "“{name}” deleted their Family Mode data, so you are no longer their family helper.",
        "hi": "“{name}” ने पारिवारिक सुविधा में अपना डेटा मिटा दिया है, इसलिए आप अब उनके पारिवारिक सहायक नहीं हैं।",
    },
    "delete_notice_protected": {
        "en": "“{name}” deleted their Family Mode data, so they are no longer your family helper.",
        "hi": "“{name}” ने पारिवारिक सुविधा में अपना डेटा मिटा दिया है, इसलिए वे अब आपके पारिवारिक सहायक नहीं हैं।",
    },
    # the bot was blocked by a helper
    "blocked_notice": {
        "en": (
            "I cannot reach “{name}” any more (they may have blocked me), so they cannot get alerts. "
            "Please contact them yourself."
        ),
        "hi": (
            "अब “{name}” तक संदेश नहीं पहुँच सकता (शायद उन्होंने मुझे ब्लॉक कर दिया है), इसलिए उन्हें खबर नहीं मिलेगी। "
            "कृपया उनसे खुद संपर्क करें।"
        ),
    },
    "error": {
        "en": "Sorry, something went wrong. Please try again in a minute.",
        "hi": "माफ़ कीजिए, कुछ गड़बड़ हो गई। कृपया एक मिनट बाद फिर कोशिश करें।",
    },
}

# --- short pop-ups shown over a pressed button (no footer: they are tiny) --------------------------------------------

POPUPS = {
    "stale": {
        "en": "This button no longer works. Send /family to start again.",
        "hi": "यह बटन अब काम नहीं करता। फिर से शुरू करने के लिए /family भेजें।",
    },
}

# --- button labels ------------------------------------------------------------------------------------------------------

BUTTONS: dict[str, dict[str, str]] = {
    "add": {"en": "➕ Add a family helper", "hi": "➕ पारिवारिक सहायक जोड़ें"},
    "add_yes": {"en": "✅ Yes, make a link", "hi": "✅ हाँ, लिंक बनाएँ"},
    "no": {"en": "No", "hi": "नहीं"},
    "remove": {"en": "Remove “{name}”", "hi": "“{name}” को हटाएँ"},
    "cancel_invite": {"en": "Cancel invitation", "hi": "निमंत्रण रद्द करें"},
    "allow": {"en": "✅ Allow “{name}”", "hi": "✅ “{name}” को अनुमति दें"},
    "dont_allow": {"en": "❌ Don't allow", "hi": "❌ अनुमति न दें"},
    "accept": {"en": "✅ Accept", "hi": "✅ स्वीकार करें"},
    "decline": {"en": "❌ No thanks", "hi": "❌ नहीं, धन्यवाद"},
    "yes_them": {"en": "✅ Yes, it is them", "hi": "✅ हाँ, यही हैं"},
    "stop_helping": {"en": "Stop helping “{name}”", "hi": "“{name}” की मदद बंद करें"},
    "delete_yes": {"en": "🗑 Yes, delete", "hi": "🗑 हाँ, मिटाएँ"},
}

# --- /family menu: what each link status says ---------------------------------------------------------------------------

STATUS_WORDS = {
    "active": {"en": "active", "hi": "चालू"},
    "awaiting_guardian": {"en": "waiting for them to accept", "hi": "उनके स्वीकार करने का इंतज़ार"},
    "awaiting_protected": {"en": "waiting for YOUR confirmation", "hi": "आपकी पुष्टि का इंतज़ार"},
    "invited": {"en": "invitation not opened yet", "hi": "निमंत्रण अभी खोला नहीं गया"},
}
INVITATION_LABEL = {"en": "Invitation", "hi": "निमंत्रण"}
