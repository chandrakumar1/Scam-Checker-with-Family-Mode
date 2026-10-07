"""End to end: Telegram updates in, messages out, through the real TelegramService,
FamilyTelegramHandler and FamilyService (temporary SQLite file). No network."""

from datetime import timedelta

import pytest

from app.family.policy import FamilyPolicy
from app.replies import family_templates as FT
from app.replies import templates as T
from app.replies.family_formatter import family_message
from tests.family_telegram_helpers import ANOUK, BOT_USERNAME, MEERA, PRATT, QUENTIN, ZEBRINA, Harness

pytestmark = pytest.mark.anyio


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.close()


def stale_answer(lang="en"):
    return FT.POPUPS["stale"][lang]


# --- /family -----------------------------------------------------------------------------------------------------


async def test_family_menu_for_someone_with_no_helpers(h):
    await h.say(ZEBRINA, "/family", message_id=77)
    [reply] = h.sent_to(ZEBRINA)
    assert reply["text"].startswith(FT.MESSAGES["menu_intro"]["en"])
    assert FT.MESSAGES["menu_no_helpers"]["en"] in reply["text"]
    assert reply["text"].endswith(T.FOOTER["en"])
    assert reply["reply_parameters"]["message_id"] == 77
    assert [b["callback_data"] for b in h.buttons(reply)] == ["fam:add"]
    assert h.analyzer.calls == []  # a command is never analysed
    assert [p.telegram_user_id for p in h.people()] == [ZEBRINA.id]


async def test_family_menu_in_hindi_for_a_hindi_user(h):
    await h.say(QUENTIN, "/family")
    assert FT.MESSAGES["menu_intro"]["hi"] in h.last_to(QUENTIN)["text"]
    assert h.buttons(h.last_to(QUENTIN))[0]["text"] == FT.BUTTONS["add"]["hi"]


async def test_family_menu_with_the_bot_username_suffix(h):
    await h.say(ZEBRINA, f"/family@{BOT_USERNAME}")
    assert FT.MESSAGES["menu_intro"]["en"] in h.last_to(ZEBRINA)["text"]


async def test_every_button_on_every_message_is_valid_and_small(h):
    from app.channels.telegram.family_handler import parse_callback_data

    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(ZEBRINA, "/family")
    await h.say(QUENTIN, "/leave")
    await h.say(ZEBRINA, "/deleteme")
    seen = 0
    for payload in h.tg.delivered:
        for button in h.buttons(payload):
            assert parse_callback_data(button["callback_data"]) is not None
            assert len(button["callback_data"].encode()) <= 64 and 0 < len(button["text"]) <= 64
            seen += 1
    assert seen > 8


# --- adding a helper: informed consent, then an invitation --------------------------------------------------------------


async def test_pressing_add_explains_what_is_shared_and_asks_for_agreement(h):
    await h.say(ZEBRINA, "/family")
    await h.tap(ZEBRINA, "fam:add")
    notice = h.last_to(ZEBRINA)
    assert notice["text"].startswith(FT.MESSAGES["add_notice"]["en"])
    assert [b["callback_data"] for b in h.buttons(notice)] == ["fam:addy", "fam:addn"]
    assert h.statuses() == []  # nothing is created until they agree
    assert h.removed_buttons() == []  # the menu keeps its buttons: nothing has changed yet
    assert len(h.answers()) == 1


async def test_declining_the_notice_creates_nothing_and_removes_its_buttons(h):
    await h.say(ZEBRINA, "/family")
    await h.tap(ZEBRINA, "fam:add")
    await h.tap(ZEBRINA, "fam:addn", message_id=501)
    assert h.last_to(ZEBRINA)["text"] == family_message("add_cancelled", "en")
    assert h.statuses() == []
    assert h.removed_buttons() == [{"chat_id": ZEBRINA.id, "message_id": 501, "reply_markup": {"inline_keyboard": []}}]


async def test_agreeing_creates_an_invitation_with_a_working_deep_link(h):
    await h.say(ZEBRINA, "/family")
    await h.tap(ZEBRINA, "fam:add")
    await h.tap(ZEBRINA, "fam:addy", message_id=502)

    link, payload = h.invite_link(ZEBRINA)
    assert link == f"https://t.me/{BOT_USERNAME}?start={payload}"
    assert h.last_to(ZEBRINA)["text"].count(link) == 1 and "48 hours" in h.last_to(ZEBRINA)["text"]
    assert h.statuses() == ["invited"]
    assert h.only_link().token_hash is not None and payload not in h.only_link().token_hash
    assert h.removed_buttons()[-1]["message_id"] == 502  # the notice's buttons are gone
    assert h.last_to(ZEBRINA)["text"].endswith(T.FOOTER["en"])


async def test_the_invitation_message_is_plain_text_without_a_link_preview(h):
    await h.invite(ZEBRINA)
    payload = h.last_to(ZEBRINA)
    assert "parse_mode" not in payload and payload["link_preview_options"] == {"is_disabled": True}


async def test_invitation_limit_is_reported_with_the_number(tmp_path):
    harness = Harness(tmp_path, policy=FamilyPolicy(max_pending_invites=2))
    try:
        await harness.invite(ZEBRINA)
        await harness.invite(ZEBRINA)
        await harness.say(ZEBRINA, "/family")
        await harness.tap(ZEBRINA, "fam:add")
        await harness.tap(ZEBRINA, "fam:addy")
        assert harness.last_to(ZEBRINA)["text"] == family_message("invite_limit_pending", "en", n=2)
        assert harness.statuses() == ["invited", "invited"]
    finally:
        harness.close()


async def test_helper_limit_is_reported_with_the_number(tmp_path):
    harness = Harness(tmp_path, policy=FamilyPolicy(max_helpers=1))
    try:
        await harness.link_up(ZEBRINA, QUENTIN)
        await harness.say(ZEBRINA, "/family")
        await harness.tap(ZEBRINA, "fam:add")
        await harness.tap(ZEBRINA, "fam:addy")
        assert harness.last_to(ZEBRINA)["text"] == family_message("invite_limit_helpers", "en", n=1)
    finally:
        harness.close()


async def test_stale_agree_button_does_not_create_an_invitation(h):
    await h.say(ZEBRINA, "/family")
    await h.tap(ZEBRINA, "fam:add")
    await h.tap(ZEBRINA, "fam:addy", age=timedelta(minutes=31), message_id=503)
    assert h.statuses() == []
    assert h.answers()[-1]["text"] == stale_answer()
    assert h.removed_buttons()[-1]["message_id"] == 503
    assert not any("t.me" in text for text in h.texts_to(ZEBRINA))


async def test_the_agree_button_works_up_to_thirty_minutes(h):
    await h.say(ZEBRINA, "/family")
    await h.tap(ZEBRINA, "fam:addy", age=timedelta(minutes=29, seconds=59))
    assert h.statuses() == ["invited"]


async def test_no_invitation_is_made_when_the_bot_username_is_unknown(tmp_path):
    harness = Harness(tmp_path, bot_username=None)
    try:
        harness.tg.queue("getMe", *[__import__("tests.telegram_fakes", fromlist=["api_error"]).api_error(500, "boom")] * 3)
        await harness.say(ZEBRINA, "/family")
        await harness.tap(ZEBRINA, "fam:addy")
        assert harness.last_to(ZEBRINA)["text"] == family_message("unavailable", "en")
        assert harness.statuses() == []  # without a working link nothing is created
    finally:
        harness.close()


async def test_bot_username_is_looked_up_once_and_remembered(tmp_path):
    harness = Harness(tmp_path, bot_username=None)
    try:
        await harness.invite(ZEBRINA)
        await harness.invite(ZEBRINA)
        assert len(harness.tg.method_calls("getMe")) == 1
        assert harness.invite_link(ZEBRINA)[0].startswith("https://t.me/ruko_dev_bot?start=fam_")
    finally:
        harness.close()


# --- the helper opens the link ------------------------------------------------------------------------------------------------


async def test_opening_the_link_asks_the_helper_to_accept(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}", message_id=88)
    prompt = h.last_to(QUENTIN)
    assert prompt["text"].startswith(family_message("helper_prompt", "hi", name="Zebrina").split("\n\n")[0])
    assert "“Zebrina”" in prompt["text"] and prompt["reply_parameters"]["message_id"] == 88
    assert [b["callback_data"].split(":")[1] for b in h.buttons(prompt)] == ["acc", "dec"]
    assert h.statuses() == ["awaiting_guardian"]
    assert h.sent_to(ZEBRINA)[-1]["text"] != prompt["text"]  # the protected person is not told yet


async def test_the_display_name_is_the_first_and_last_name(h):
    payload = await h.invite(PRATT)
    await h.say(QUENTIN, f"/start {payload}")
    assert "“Zebrina Pratt”" in h.last_to(QUENTIN)["text"]
    assert [p.display_name for p in h.people()] == ["Zebrina Pratt", "Quentin"]


async def test_a_user_with_only_a_username_is_shown_by_it(h):
    from tests.family_telegram_helpers import User

    nameless = User(3003, "", "just_a_handle", "en")
    payload = await h.invite(ZEBRINA)
    await h.say(nameless, f"/start {payload}")
    await h.tap(nameless, h.data_of(nameless, "acc"))
    assert "“just_a_handle”" in h.last_to(ZEBRINA)["text"]


async def test_the_link_works_via_the_start_command_with_the_bot_suffix(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start@{BOT_USERNAME} {payload}")
    assert h.statuses() == ["awaiting_guardian"]


@pytest.mark.parametrize(
    "payload",
    ["fam_", "fam_short", "fam_" + "x" * 22, "fam_" + "x" * 30, "fam_%%%%%%%%%%%%%%%%%%%%%%"],
)
async def test_a_bad_or_unknown_link_gets_the_generic_message(h, payload):
    await h.say(QUENTIN, f"/start {payload}")
    assert h.last_to(QUENTIN)["text"] == family_message("invite_invalid", "hi")
    assert h.people() == [] and h.statuses() == []  # nothing is recorded for a failed open


async def test_an_expired_link_gets_the_same_generic_message(h):
    payload = await h.invite(ZEBRINA)
    h.clock.advance(hours=49)
    await h.say(QUENTIN, f"/start {payload}")
    assert h.last_to(QUENTIN)["text"] == family_message("invite_invalid", "hi")


async def test_a_link_cannot_be_reused(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    await h.say(ANOUK, f"/start {payload}")
    assert h.last_to(ANOUK)["text"] == family_message("invite_invalid", "en")
    assert h.statuses() == ["awaiting_guardian"]


async def test_your_own_invitation_is_refused_and_still_usable_by_someone_else(h):
    payload = await h.invite(ZEBRINA)
    await h.say(ZEBRINA, f"/start {payload}")
    assert h.last_to(ZEBRINA)["text"] == family_message("invite_self", "en")
    assert h.statuses() == ["invited"]
    await h.say(QUENTIN, f"/start {payload}")
    assert h.statuses() == ["awaiting_guardian"]


async def test_an_existing_helper_is_told_they_are_already_connected(h):
    await h.link_up(ZEBRINA, QUENTIN)
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    assert h.last_to(QUENTIN)["text"] == family_message("invite_already", "hi")


async def test_a_plain_start_and_other_payloads_are_still_the_welcome_message(h):
    from app.replies.formatter import start_message

    await h.say(ZEBRINA, "/start")
    await h.say(ZEBRINA, "/start something_else")
    assert h.texts_to(ZEBRINA) == [start_message("en")] * 2
    assert h.people() == []


# --- the full consent flow ------------------------------------------------------------------------------------------------------


async def test_full_consent_flow_through_telegram(h):
    payload = await h.invite(ZEBRINA)

    # 1. the helper opens the link and accepts
    await h.say(QUENTIN, f"/start {payload}")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "acc"), message_id=601)
    assert h.last_to(QUENTIN)["text"] == family_message("helper_wait", "hi", name="Zebrina")
    assert h.statuses() == ["awaiting_protected"]

    # 2. the protected person is asked to confirm WHO accepted, with name and @username
    prompt = h.last_to(ZEBRINA)
    assert "“Quentin” (@quentin_r) accepted" in prompt["text"]
    assert [b["callback_data"].split(":")[1] for b in h.buttons(prompt)] == ["yes", "no"]
    assert h.removed_buttons()[-1]["message_id"] == 601

    # 3. the protected person confirms
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "yes"), message_id=602)
    assert h.last_to(ZEBRINA)["text"] == family_message("confirmed_protected", "en", name="Quentin")
    assert h.last_to(QUENTIN)["text"] == family_message("confirmed_helper", "hi", name="Zebrina")
    assert h.statuses() == ["active"]
    assert h.removed_buttons()[-1]["message_id"] == 602

    # 4. none of it went to the analyzer, and each of the four presses (add, addy, acc, yes) was acknowledged once
    assert h.analyzer.calls == []
    assert len(h.answers()) == 4


async def test_every_press_is_acknowledged_exactly_once(h):
    payload = await h.invite(ZEBRINA)  # presses: add, addy
    await h.say(QUENTIN, f"/start {payload}")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "acc"))  # press 3
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "yes"))  # press 4
    ids = [call["callback_query_id"] for call in h.answers()]
    assert len(ids) == 4 and len(set(ids)) == 4


async def test_every_message_in_the_flow_ends_with_the_1930_footer(h):
    await h.link_up(ZEBRINA, QUENTIN)
    footers = {T.FOOTER["en"], T.FOOTER["hi"]}
    for payload in h.tg.delivered:
        assert any(payload["text"].endswith(footer) for footer in footers), payload["text"][:40]


async def test_the_other_person_is_told_in_their_own_language(h):
    await h.link_up(ZEBRINA, QUENTIN)  # Zebrina: English, Quentin: Hindi
    assert FT.MESSAGES["confirmed_helper"]["hi"].split("{")[0] in h.last_to(QUENTIN)["text"]
    assert FT.MESSAGES["confirmed_protected"]["en"].split("{")[0] in h.last_to(ZEBRINA)["text"]


async def test_the_helper_declines(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "dec"))
    assert h.last_to(QUENTIN)["text"] == family_message("helper_declined", "hi", name="Zebrina")
    assert h.last_to(ZEBRINA)["text"] == family_message("helper_declined_notice", "en", name="Quentin")
    assert h.statuses() == ["declined"]


async def test_the_protected_person_says_it_is_not_them(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "acc"))
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "no"))
    assert h.last_to(ZEBRINA)["text"] == family_message("not_confirmed_protected", "en", name="Quentin")
    assert h.last_to(QUENTIN)["text"] == family_message("not_confirmed_helper", "hi", name="Zebrina")
    assert h.statuses() == ["declined"]


async def test_the_helper_is_not_a_helper_until_the_protected_person_confirms(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "acc"))
    assert h.statuses() == ["awaiting_protected"]
    assert h.family._clock is h.clock  # (sanity) and no alert path can reach an unconfirmed helper
    from app.family.types import AlertSummary, AlertVerdict, PersonRef, ScamCategory, SourceKind

    plan = h.family.request_normal_alert(
        PersonRef(ZEBRINA.id, "Zebrina"), AlertSummary(AlertVerdict.LIKELY_SCAM, ScamCategory.OTP_FRAUD, SourceKind.TEXT), 1
    )
    assert plan.deliveries == ()


async def test_a_helper_whose_prompt_could_not_be_delivered_is_told(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    h.tg.blocked_chats.add(ZEBRINA.id)  # Zebrina blocked the bot in the meantime
    await h.tap(QUENTIN, h.data_of(QUENTIN, "acc"))
    assert h.last_to(QUENTIN)["text"] == family_message("unreachable_notice", "hi", name="Zebrina")
    assert h.statuses() == ["awaiting_protected"]  # the link is still there, to be confirmed later


async def test_the_menu_offers_allow_and_dont_allow_so_a_lost_prompt_is_recoverable(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "acc"))
    await h.say(ZEBRINA, "/family")
    menu = h.last_to(ZEBRINA)
    assert "“Quentin” — waiting for YOUR confirmation" in menu["text"]
    assert [b["text"] for b in h.buttons(menu)][:2] == [FT.BUTTONS["allow"]["en"].format(name="Quentin"), FT.BUTTONS["dont_allow"]["en"]]
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "yes"))
    assert h.statuses() == ["active"]


# --- ending a link ---------------------------------------------------------------------------------------------------------------------


async def test_the_protected_person_removes_a_helper_from_the_menu(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(ZEBRINA, "/family")
    menu = h.last_to(ZEBRINA)
    assert "“Quentin” — active" in menu["text"]
    assert h.buttons(menu)[0]["text"] == FT.BUTTONS["remove"]["en"].format(name="Quentin")
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "rm"), message_id=700)
    assert h.last_to(ZEBRINA)["text"] == family_message("removed_protected", "en", name="Quentin")
    assert h.last_to(QUENTIN)["text"] == family_message("removed_notice_helper", "hi", name="Zebrina")
    assert h.statuses() == ["revoked"] and h.only_link().ended_by == "protected"
    assert h.removed_buttons()[-1]["message_id"] == 700


async def test_cancelling_an_unopened_invitation_kills_its_link(h):
    payload = await h.invite(ZEBRINA)
    await h.say(ZEBRINA, "/family")
    assert "Invitation — invitation not opened yet" in h.last_to(ZEBRINA)["text"]
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "rm"))
    assert h.last_to(ZEBRINA)["text"] == family_message("invite_cancelled", "en")
    await h.say(QUENTIN, f"/start {payload}")
    assert h.last_to(QUENTIN)["text"] == family_message("invite_invalid", "hi")


async def test_the_helper_leaves_with_slash_leave(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(QUENTIN, "/leave")
    prompt = h.last_to(QUENTIN)
    assert prompt["text"].startswith(FT.MESSAGES["leave_prompt"]["hi"])
    assert h.buttons(prompt)[0]["text"] == FT.BUTTONS["stop_helping"]["hi"].format(name="Zebrina")
    await h.tap(QUENTIN, h.data_of(QUENTIN, "lv"))
    assert h.last_to(QUENTIN)["text"] == family_message("left_helper", "hi", name="Zebrina")
    assert h.last_to(ZEBRINA)["text"] == family_message("left_notice_protected", "en", name="Quentin")
    assert h.only_link().ended_by == "guardian" and h.statuses() == ["revoked"]


async def test_leave_with_nobody_to_leave(h):
    await h.say(ANOUK, "/leave")
    assert h.last_to(ANOUK)["text"] == family_message("nothing_to_leave", "en")
    assert h.buttons(h.last_to(ANOUK)) == []


async def test_a_helper_of_two_people_gets_a_button_for_each(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.link_up(ANOUK, QUENTIN)
    await h.say(QUENTIN, "/leave")
    labels = [b["text"] for b in h.buttons(h.last_to(QUENTIN))]
    assert labels == [FT.BUTTONS["stop_helping"]["hi"].format(name=n) for n in ("Zebrina", "Anouk")]


async def test_the_menu_for_a_helper_lists_who_they_help(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(QUENTIN, "/family")
    assert "• “Zebrina”" in h.last_to(QUENTIN)["text"] and "/leave" in h.last_to(QUENTIN)["text"]


# --- /deleteme ------------------------------------------------------------------------------------------------------------------------------


async def test_deleteme_asks_first_and_deletes_only_on_confirmation(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.link_up(ZEBRINA, ANOUK)
    await h.say(ZEBRINA, "/deleteme")
    prompt = h.last_to(ZEBRINA)
    assert prompt["text"].startswith(FT.MESSAGES["delete_prompt"]["en"])
    assert [b["callback_data"] for b in h.buttons(prompt)] == ["fam:delc", "fam:deln"]
    assert len(h.people()) == 3  # nothing deleted yet

    await h.tap(ZEBRINA, "fam:delc", message_id=800)
    assert h.last_to(ZEBRINA)["text"] == family_message("delete_done", "en")
    assert [p.display_name for p in h.people()] == ["Quentin", "Anouk"]
    assert h.statuses() == []
    assert h.last_to(QUENTIN)["text"] == family_message("delete_notice_helper", "hi", name="Zebrina")
    assert h.last_to(ANOUK)["text"] == family_message("delete_notice_helper", "en", name="Zebrina")
    assert h.removed_buttons()[-1]["message_id"] == 800


async def test_deleting_a_helper_tells_the_people_they_protected(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(QUENTIN, "/deleteme")
    await h.tap(QUENTIN, "fam:delc")
    assert h.last_to(ZEBRINA)["text"] == family_message("delete_notice_protected", "en", name="Quentin")


async def test_deleteme_can_be_cancelled(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(ZEBRINA, "/deleteme")
    await h.tap(ZEBRINA, "fam:deln", message_id=801)
    assert h.last_to(ZEBRINA)["text"] == family_message("delete_cancelled", "en")
    assert h.statuses() == ["active"] and len(h.people()) == 2
    assert h.removed_buttons()[-1]["message_id"] == 801


async def test_an_old_delete_confirmation_does_nothing(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(ZEBRINA, "/deleteme")
    await h.tap(ZEBRINA, "fam:delc", age=timedelta(minutes=45), message_id=802)
    assert h.last_to(ZEBRINA)["text"] == family_message("delete_stale", "en")
    assert h.statuses() == ["active"] and len(h.people()) == 2
    assert h.answers()[-1]["text"] == stale_answer()
    assert h.removed_buttons()[-1]["message_id"] == 802


async def test_deleteme_with_nothing_to_delete(h):
    await h.say(ANOUK, "/deleteme")
    await h.tap(ANOUK, "fam:delc")
    assert h.last_to(ANOUK)["text"] == family_message("delete_nothing", "en")


async def test_after_deleteme_the_person_can_start_again(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.say(ZEBRINA, "/deleteme")
    await h.tap(ZEBRINA, "fam:delc")
    await h.link_up(ZEBRINA, QUENTIN)
    assert h.statuses() == ["active"]


# --- the bot is blocked ------------------------------------------------------------------------------------------------------------------------


async def test_when_a_helper_blocks_the_bot_the_protected_person_is_told_once(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.member(QUENTIN, "kicked")
    assert h.last_to(ZEBRINA)["text"] == family_message("blocked_notice", "en", name="Quentin")
    count = len(h.sent_to(ZEBRINA))
    await h.member(QUENTIN, "kicked")  # a repeated event
    assert len(h.sent_to(ZEBRINA)) == count
    assert next(p for p in h.people() if p.display_name == "Quentin").bot_blocked is True


async def test_unblocking_is_quiet_and_clears_the_flag(h):
    await h.link_up(ZEBRINA, QUENTIN)
    await h.member(QUENTIN, "kicked")
    count = len(h.sent_to(ZEBRINA))
    await h.member(QUENTIN, "member")
    assert len(h.sent_to(ZEBRINA)) == count
    assert next(p for p in h.people() if p.display_name == "Quentin").bot_blocked is False


@pytest.mark.parametrize("status", ["left", "administrator", "restricted", "creator"])
async def test_other_status_changes_are_ignored(h, status):
    await h.link_up(ZEBRINA, QUENTIN)
    before = len(h.tg.delivered)
    await h.member(QUENTIN, status)
    assert len(h.tg.delivered) == before
    assert next(p for p in h.people() if p.display_name == "Quentin").bot_blocked is False


async def test_block_events_for_strangers_and_groups_do_nothing(h):
    await h.member(ANOUK, "kicked")  # never used family mode
    await h.member(QUENTIN, "kicked", chat_type="group")
    assert h.tg.calls == [] and h.people() == []


async def test_only_active_helpers_trigger_a_warning(h):
    payload = await h.invite(ZEBRINA)
    await h.say(QUENTIN, f"/start {payload}")  # only awaiting_guardian
    before = len(h.sent_to(ZEBRINA))
    await h.member(QUENTIN, "kicked")
    assert len(h.sent_to(ZEBRINA)) == before


# --- a failed notification never undoes a completed change -----------------------------------------------------------------------------------


async def test_removal_stands_even_if_the_helper_cannot_be_notified(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.blocked_chats.add(QUENTIN.id)
    await h.say(ZEBRINA, "/family")
    await h.tap(ZEBRINA, h.data_of(ZEBRINA, "rm"))
    assert h.statuses() == ["revoked"]
    assert h.last_to(ZEBRINA)["text"] == family_message("removed_protected", "en", name="Quentin")


async def test_deletion_stands_even_if_notifications_fail(h):
    await h.link_up(ZEBRINA, QUENTIN)
    h.tg.blocked_chats.add(QUENTIN.id)
    await h.say(ZEBRINA, "/deleteme")
    await h.tap(ZEBRINA, "fam:delc")
    assert h.last_to(ZEBRINA)["text"] == family_message("delete_done", "en")
    assert [p.display_name for p in h.people()] == ["Quentin"]


async def test_names_with_urls_are_cleaned_before_being_shown_to_the_other_person(h):
    from tests.family_telegram_helpers import User

    sneaky = User(3001, "Anouk http://evil.example/pay", "anouk_x", "en")
    payload = await h.invite(ZEBRINA)
    await h.say(sneaky, f"/start {payload}")
    await h.tap(sneaky, h.data_of(sneaky, "acc"))
    prompt = h.last_to(ZEBRINA)["text"]
    assert "evil.example" not in prompt and "“Anouk”" in prompt
    assert "evil.example" not in " ".join(h.texts_to(ZEBRINA) + h.texts_to(sneaky))


async def test_a_name_that_is_only_a_link_gets_a_neutral_name(h):
    from tests.family_telegram_helpers import User

    sneaky = User(3002, "www.free-money.example", None, "en")
    payload = await h.invite(ZEBRINA)
    await h.say(sneaky, f"/start {payload}")
    await h.tap(sneaky, h.data_of(sneaky, "acc"))
    assert f"“{FT.FALLBACK_NAME['en']}” accepted" in h.last_to(ZEBRINA)["text"]
    assert "free-money" not in " ".join(h.texts_to(ZEBRINA))
