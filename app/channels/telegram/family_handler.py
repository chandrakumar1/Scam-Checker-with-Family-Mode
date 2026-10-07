"""The Telegram side of Family Mode.

Routes /family, /leave, /deleteme, the invitation deep link (`/start fam_<token>`),
the consent buttons, and "the user blocked the bot" events. All decisions about
who may do what live in app.family; this module only turns Telegram input into
those calls and the results into messages.

Privacy: nothing here logs names, ids, tokens, button data or message text. It
logs only outcome labels and exception class names.

Button data (`fam:<action>[:<ref>]`) is untrusted: a modified client can send
anything. It is validated for shape, the presser's id must equal the private
chat it came from, and ownership of the thing being acted on is checked by
FamilyService against that presser, never against the button.
"""

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.channels.telegram.client import TelegramClient, TelegramError
from app.channels.telegram.models import CallbackQuery, ChatMemberUpdated, Message, User
from app.db.models import LinkStatus
from app.family import tokens
from app.family.service import AsyncFamilyService, utcnow
from app.family.types import ConsentOutcome, HelperInfo, PersonRef
from app.replies.family_formatter import button_label, family_menu, family_message, popup
from app.replies.language import Language, choose_language

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "fam"
MAX_CALLBACK_DATA_BYTES = 64  # Telegram's limit

# Actions that act on one link, and so carry its random reference.
REF_ACTIONS = frozenset({"acc", "dec", "yes", "no", "rm", "lv"})
# Actions that carry nothing.
PLAIN_ACTIONS = frozenset({"add", "addy", "addn", "delc", "deln"})
# Actions that create or destroy something and so need a recently sent message,
# so an old button cannot be tapped by accident long afterwards.
FRESH_ACTIONS = frozenset({"addy", "delc"})
CONFIRM_FRESHNESS = timedelta(minutes=30)
_CLOCK_SKEW = timedelta(seconds=60)

_COMMAND = re.compile(r"^/([A-Za-z0-9_]+)(?:@\w+)?$")
_BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")

OUTCOME_IGNORED = "family:ignored"


def parse_command(text: str | None) -> tuple[str, str] | None:
    """('start', 'fam_xyz') for '/start fam_xyz'; None if the text is not a command."""
    if not text or not text.startswith("/"):
        return None
    parts = text.split(maxsplit=1)
    match = _COMMAND.match(parts[0])
    if not match:
        return None
    return match.group(1).lower(), (parts[1].strip() if len(parts) > 1 else "")


def parse_callback_data(data: object) -> tuple[str, str | None] | None:
    """(action, ref) for well-formed Family button data; None for anything else."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
        return None
    parts = data.split(":")
    if parts[0] != CALLBACK_PREFIX:
        return None
    if len(parts) == 2 and parts[1] in PLAIN_ACTIONS:
        return parts[1], None
    if len(parts) == 3 and parts[1] in REF_ACTIONS and tokens.is_valid_ref(parts[2]):
        return parts[1], parts[2]
    return None


def callback_data(action: str, ref: str | None = None) -> str:
    data = f"{CALLBACK_PREFIX}:{action}" + (f":{ref}" if ref else "")
    assert len(data.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES
    return data


def _button(text: str, action: str, ref: str | None = None) -> dict[str, str]:
    return {"text": text, "callback_data": callback_data(action, ref)}


def _keyboard(*rows: list[dict[str, str]]) -> dict[str, Any]:
    return {"inline_keyboard": [list(row) for row in rows]}


def person_ref(user: User) -> PersonRef:
    name = " ".join(part for part in (user.first_name, user.last_name) if part) or user.username or ""
    return PersonRef(user.id, name, user.language_code)


_person_ref = person_ref  # the original private name, still used throughout this module


def _display_name(user: User | None) -> str:
    if user is None:
        return ""
    return " ".join(part for part in (user.first_name, user.last_name) if part) or user.username or ""


class FamilyTelegramHandler:
    def __init__(
        self,
        client: TelegramClient,
        family: AsyncFamilyService,
        *,
        bot_username: str | None = None,
        invite_ttl_hours: float = 48.0,
        max_helpers: int = 5,
        max_pending_invites: int = 3,
        confirm_freshness: timedelta = CONFIRM_FRESHNESS,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client
        self._family = family
        self._bot_username_cached = bot_username
        self._invite_ttl_hours = invite_ttl_hours
        self._max_helpers = max_helpers
        self._max_pending = max_pending_invites
        self._freshness = confirm_freshness
        self._clock = clock

    # -- recognising family input -------------------------------------------------------------------------

    def matches(self, message: Message) -> bool:
        """True if this message is one of ours. Pure and cheap, so the caller can
        decide before doing anything else."""
        parsed = parse_command(message.text)
        if parsed is None:
            return False
        name, arg = parsed
        if name in ("family", "leave", "deleteme"):
            return True
        return name == "start" and arg.startswith(tokens.INVITE_PREFIX)

    # -- messages (commands and the deep link) -----------------------------------------------------------------

    async def handle_message(self, message: Message) -> str:
        sender = message.from_
        assert sender is not None  # the caller has already required a human sender
        name, arg = parse_command(message.text) or ("", "")
        lang = choose_language(None, sender.language_code)
        chat_id = message.chat.id

        if name == "family":
            return await self._cmd_family(sender, chat_id, message.message_id, lang)
        if name == "leave":
            return await self._cmd_leave(sender, chat_id, message.message_id, lang)
        if name == "deleteme":
            return await self._cmd_deleteme(chat_id, message.message_id, lang)
        return await self._open_invite(sender, chat_id, message.message_id, arg, lang)

    async def _cmd_family(self, sender: User, chat_id: int, reply_to: int, lang: Language) -> str:
        await self._family.upsert_person(_person_ref(sender))
        helpers = await self._family.list_helpers(sender.id)
        protected = await self._family.list_protected(sender.id)
        await self._send(
            chat_id, family_menu(helpers, protected, lang), self._menu_keyboard(helpers, lang), reply_to=reply_to
        )
        return "family:menu"

    def _menu_keyboard(self, helpers: tuple[HelperInfo, ...], lang: Language) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        for helper in helpers:
            name = helper.person.display_name if helper.person else ""
            if helper.status is LinkStatus.AWAITING_PROTECTED:
                rows.append(
                    [_button(button_label("allow", lang, name=name), "yes", helper.link_ref),
                     _button(button_label("dont_allow", lang), "no", helper.link_ref)]
                )  # fmt: skip
            elif helper.status is LinkStatus.INVITED:
                rows.append([_button(button_label("cancel_invite", lang), "rm", helper.link_ref)])
            else:
                rows.append([_button(button_label("remove", lang, name=name), "rm", helper.link_ref)])
        rows.append([_button(button_label("add", lang), "add")])
        return _keyboard(*rows)

    async def _cmd_leave(self, sender: User, chat_id: int, reply_to: int, lang: Language) -> str:
        protected = await self._family.list_protected(sender.id)
        if not protected:
            await self._send(chat_id, family_message("nothing_to_leave", lang), reply_to=reply_to)
            return "family:leave_nothing"
        rows = [
            [_button(button_label("stop_helping", lang, name=p.person.display_name if p.person else ""), "lv", p.link_ref)]
            for p in protected
        ]
        await self._send(chat_id, family_message("leave_prompt", lang), _keyboard(*rows), reply_to=reply_to)
        return "family:leave_prompt"

    async def _cmd_deleteme(self, chat_id: int, reply_to: int, lang: Language) -> str:
        keyboard = _keyboard(
            [_button(button_label("delete_yes", lang), "delc"), _button(button_label("no", lang), "deln")]
        )
        await self._send(chat_id, family_message("delete_prompt", lang), keyboard, reply_to=reply_to)
        return "family:delete_prompt"

    async def _open_invite(self, sender: User, chat_id: int, reply_to: int, arg: str, lang: Language) -> str:
        token = tokens.parse_start_payload(arg.split()[0]) if arg else None
        if token is None:
            await self._send(chat_id, family_message("invite_invalid", lang), reply_to=reply_to)
            return "family:invite_invalid"

        result = await self._family.open_invite(_person_ref(sender), token)
        if result.outcome is ConsentOutcome.OK:
            assert result.protected is not None and result.link_ref is not None
            keyboard = _keyboard(
                [_button(button_label("accept", lang), "acc", result.link_ref),
                 _button(button_label("decline", lang), "dec", result.link_ref)]
            )  # fmt: skip
            await self._send(
                chat_id, family_message("helper_prompt", lang, name=result.protected.display_name), keyboard, reply_to=reply_to
            )
            return "family:invite_opened"

        key = {ConsentOutcome.SELF_INVITE: "invite_self", ConsentOutcome.ALREADY_LINKED: "invite_already"}.get(
            result.outcome, "invite_invalid"
        )
        await self._send(chat_id, family_message(key, lang), reply_to=reply_to)
        return f"family:{key}"

    # -- button presses -------------------------------------------------------------------------------------------

    async def handle_callback(self, query: CallbackQuery) -> str:
        """Process a button press and acknowledge it exactly once, whatever happens."""
        popup_text: str | None = None
        try:
            outcome, popup_text = await self._dispatch_callback(query)
            return outcome
        finally:
            try:
                await self._client.answer_callback_query(query.id, popup_text)
            except TelegramError as exc:
                logger.warning("could not acknowledge a button press: %s", type(exc).__name__)

    async def _dispatch_callback(self, query: CallbackQuery) -> tuple[str, str | None]:
        actor = query.from_
        lang = choose_language(None, actor.language_code)
        stale = popup("stale", lang)
        if actor.is_bot:
            return OUTCOME_IGNORED, None

        parsed = parse_callback_data(query.data)
        if parsed is None:
            return "family:bad_data", stale
        message = query.message
        if message is None or message.chat.type != "private" or message.chat.id != actor.id:
            return "family:bad_context", stale  # not the presser's own private chat: do nothing at all

        action, ref = parsed
        chat_id, message_id = message.chat.id, message.message_id
        if action in FRESH_ACTIONS and not self._is_fresh(message):
            await self._remove_buttons(chat_id, message_id)
            if action == "delc":
                await self._send(chat_id, family_message("delete_stale", lang))
            return "family:stale", stale

        if action == "add":  # nothing changes yet, so the menu keeps its buttons
            keyboard = _keyboard(
                [_button(button_label("add_yes", lang), "addy"), _button(button_label("no", lang), "addn")]
            )
            await self._send(chat_id, family_message("add_notice", lang), keyboard)
            return "family:add_notice", None

        await self._remove_buttons(chat_id, message_id)  # every other press settles this message
        if action == "addn":
            await self._send(chat_id, family_message("add_cancelled", lang))
            return "family:add_cancelled", None
        if action == "addy":
            return await self._create_invite(actor, chat_id, lang)
        if action in ("acc", "dec"):
            return await self._guardian_responds(actor, chat_id, ref, action == "acc", lang, stale)
        if action in ("yes", "no"):
            return await self._protected_confirms(actor, chat_id, ref, action == "yes", lang, stale)
        if action == "rm":
            return await self._remove_helper(actor, chat_id, ref, lang, stale)
        if action == "lv":
            return await self._leave_link(actor, chat_id, ref, lang, stale)
        if action == "deln":
            await self._send(chat_id, family_message("delete_cancelled", lang))
            return "family:delete_cancelled", None
        return await self._delete_me(actor, chat_id, lang)  # "delc"

    def _is_fresh(self, message: Message) -> bool:
        if message.date is None:
            return False
        age = self._clock() - datetime.fromtimestamp(message.date, UTC)
        return -_CLOCK_SKEW <= age <= self._freshness

    # -- the individual actions -----------------------------------------------------------------------------------

    async def _create_invite(self, actor: User, chat_id: int, lang: Language) -> tuple[str, str | None]:
        username = await self._bot_username()
        if username is None:  # without it we could not make a working link: create nothing
            await self._send(chat_id, family_message("unavailable", lang))
            return "family:unavailable", None
        result = await self._family.create_invite(_person_ref(actor))
        if result.outcome is ConsentOutcome.OK:
            assert result.token is not None
            link = f"https://t.me/{username}?start={tokens.start_payload(result.token)}"
            await self._send(chat_id, family_message("invite_created", lang, link=link, hours=self._invite_ttl_hours))
            return "family:invite_created", None
        if result.outcome is ConsentOutcome.TOO_MANY_PENDING:
            await self._send(chat_id, family_message("invite_limit_pending", lang, n=self._max_pending))
            return "family:limit_pending", None
        await self._send(chat_id, family_message("invite_limit_helpers", lang, n=self._max_helpers))
        return "family:limit_helpers", None

    async def _guardian_responds(
        self, actor: User, chat_id: int, ref: str | None, accept: bool, lang: Language, stale: str
    ) -> tuple[str, str | None]:
        assert ref is not None
        result = await self._family.guardian_respond(actor.id, ref, accept)
        if result.outcome is not ConsentOutcome.OK or result.protected is None or result.guardian is None:
            return "family:stale", stale
        protected, guardian = result.protected, result.guardian

        if not accept:
            await self._send(chat_id, family_message("helper_declined", lang, name=protected.display_name))
            await self._notify(
                protected.telegram_user_id, family_message("helper_declined_notice", protected.language, name=guardian.display_name)
            )
            return "family:declined", None

        await self._send(chat_id, family_message("helper_wait", lang, name=protected.display_name))
        keyboard = _keyboard(
            [_button(button_label("yes_them", protected.language), "yes", ref),
             _button(button_label("no", protected.language), "no", ref)]
        )  # fmt: skip
        prompt = family_message("confirm_prompt", protected.language, name=guardian.display_name, handle=actor.username)
        if not await self._notify(protected.telegram_user_id, prompt, keyboard):
            # Never leave the helper waiting for a question that was not delivered.
            await self._notify(actor.id, family_message("unreachable_notice", lang, name=protected.display_name))
        return "family:accepted", None

    async def _protected_confirms(
        self, actor: User, chat_id: int, ref: str | None, confirm: bool, lang: Language, stale: str
    ) -> tuple[str, str | None]:
        assert ref is not None
        result = await self._family.protected_confirm(actor.id, ref, confirm)
        if result.outcome is ConsentOutcome.TOO_MANY_HELPERS:
            await self._send(chat_id, family_message("invite_limit_helpers", lang, n=self._max_helpers))
            return "family:limit_helpers", None
        if result.outcome is not ConsentOutcome.OK or result.protected is None or result.guardian is None:
            return "family:stale", stale
        protected, guardian = result.protected, result.guardian

        if confirm:
            await self._send(chat_id, family_message("confirmed_protected", lang, name=guardian.display_name))
            await self._notify(
                guardian.telegram_user_id, family_message("confirmed_helper", guardian.language, name=protected.display_name)
            )
            return "family:confirmed", None
        await self._send(chat_id, family_message("not_confirmed_protected", lang, name=guardian.display_name))
        await self._notify(
            guardian.telegram_user_id, family_message("not_confirmed_helper", guardian.language, name=protected.display_name)
        )
        return "family:not_confirmed", None

    async def _remove_helper(
        self, actor: User, chat_id: int, ref: str | None, lang: Language, stale: str
    ) -> tuple[str, str | None]:
        assert ref is not None
        # Only the protected person may use this button, whatever the data says.
        if not any(h.link_ref == ref for h in await self._family.list_helpers(actor.id)):
            return "family:stale", stale
        result = await self._family.revoke(actor.id, ref)
        if result.outcome is not ConsentOutcome.OK or result.protected is None:
            return "family:stale", stale
        if result.guardian is None:
            await self._send(chat_id, family_message("invite_cancelled", lang))
            return "family:invite_cancelled", None
        await self._send(chat_id, family_message("removed_protected", lang, name=result.guardian.display_name))
        await self._notify(
            result.guardian.telegram_user_id,
            family_message("removed_notice_helper", result.guardian.language, name=result.protected.display_name),
        )
        return "family:removed", None

    async def _leave_link(
        self, actor: User, chat_id: int, ref: str | None, lang: Language, stale: str
    ) -> tuple[str, str | None]:
        assert ref is not None
        if not any(h.link_ref == ref for h in await self._family.list_protected(actor.id)):
            return "family:stale", stale  # only a helper of that link may use this button
        result = await self._family.revoke(actor.id, ref)
        if result.outcome is not ConsentOutcome.OK or result.protected is None or result.guardian is None:
            return "family:stale", stale
        await self._send(chat_id, family_message("left_helper", lang, name=result.protected.display_name))
        await self._notify(
            result.protected.telegram_user_id,
            family_message("left_notice_protected", result.protected.language, name=result.guardian.display_name),
        )
        return "family:left", None

    async def _delete_me(self, actor: User, chat_id: int, lang: Language) -> tuple[str, str | None]:
        result = await self._family.delete_person(actor.id)
        if not result.deleted:
            await self._send(chat_id, family_message("delete_nothing", lang))
            return "family:delete_nothing", None
        name = _display_name(actor)
        for helper in result.notify_helpers:
            await self._notify(helper.telegram_user_id, family_message("delete_notice_helper", helper.language, name=name))
        for person in result.notify_protected:
            await self._notify(person.telegram_user_id, family_message("delete_notice_protected", person.language, name=name))
        await self._send(chat_id, family_message("delete_done", lang))
        return "family:deleted", None

    # -- the bot was blocked or unblocked --------------------------------------------------------------------------

    async def handle_chat_member(self, event: ChatMemberUpdated) -> str:
        if event.chat.type != "private":
            return OUTCOME_IGNORED
        status = event.new_chat_member.status
        if status == "kicked":
            blocked = True
        elif status == "member":
            blocked = False
        else:
            return OUTCOME_IGNORED

        result = await self._family.mark_blocked(event.chat.id, blocked)
        if result.changed and blocked:
            name = _display_name(event.from_)
            for person in result.notify_protected:
                await self._notify(person.telegram_user_id, family_message("blocked_notice", person.language, name=name))
        return "family:blocked" if blocked else "family:unblocked"

    # -- helpers ----------------------------------------------------------------------------------------------------

    async def _bot_username(self) -> str | None:
        """The bot's @username: configured, or looked up once with getMe."""
        if self._bot_username_cached:
            return self._bot_username_cached
        try:
            me = await self._client.get_me()
        except TelegramError as exc:
            logger.warning("could not look up the bot username: %s", type(exc).__name__)
            return None
        username = me.get("username") if isinstance(me, dict) else None
        if isinstance(username, str) and _BOT_USERNAME.fullmatch(username):
            self._bot_username_cached = username
            return username
        return None

    async def _send(
        self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None, *, reply_to: int | None = None
    ) -> None:
        await self._client.send_message(chat_id, text, reply_to_message_id=reply_to, reply_markup=keyboard)

    async def _notify(self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None) -> bool:
        """Message the OTHER person. Best effort: a state change that already
        happened is never undone because this failed."""
        try:
            await self._send(chat_id, text, keyboard)
            return True
        except TelegramError as exc:
            logger.warning("could not notify the other person: %s", type(exc).__name__)
            return False

    async def _remove_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            await self._client.remove_buttons(chat_id, message_id)
        except TelegramError as exc:  # e.g. the message is too old to edit; cosmetic only
            logger.debug("could not remove buttons: %s", type(exc).__name__)
