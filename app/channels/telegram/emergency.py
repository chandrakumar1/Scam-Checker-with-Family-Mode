"""The Emergency flow: /emergency and the `em:` buttons.

Always available, and deliberately independent of everything that could fail:
- It is NOT gated by Family Mode, and it runs outside the analysis queue.
- Saying Yes sends the static 1930 guidance FIRST, before any database or
  Family call. If Family Mode is off, there are no helpers, or the database is
  down, the person still gets the guidance plus an explicit message saying
  what could and could not be done. No branch ends silently.

Button data is `em:<action>:<token>` (actions o=open, y=yes, n=no). It is
untrusted input: the token is verified (format, constant-time signature,
version, owner, age <= 24h) and the press must come from the owner's own
private chat. Anything else changes nothing and earns only a generic popup.

Privacy: nothing here logs names, ids, button data, tokens or message text.
Only outcome labels and exception class names / HTTP status codes.
"""

import logging
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.channels.telegram.callback_tokens import CallbackTokenCodec, TokenStatus
from app.channels.telegram.client import TelegramClient, TelegramError
from app.channels.telegram.family_alerts import FamilyAlertSender
from app.channels.telegram.family_handler import parse_command, person_ref
from app.channels.telegram.models import CallbackQuery, Message, Update, User
from app.family.service import utcnow
from app.family.types import AlertOutcome, AlertPlan, AlertSummary
from app.replies.emergency_formatter import (
    button_label,
    emergency_message,
    guidance_message,
    popup,
    question_message,
)
from app.replies.language import Language, choose_language

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = "em"
MAX_CALLBACK_DATA_BYTES = 64  # Telegram's limit
_DATA = re.compile(r"em:([oyn]):([A-Za-z0-9_-]{39})")


def parse_emergency_data(data: object) -> tuple[str, str] | None:
    """(action, token) for well-formed emergency button data; None for anything else."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_DATA_BYTES:
        return None
    match = _DATA.fullmatch(data)
    return (match.group(1), match.group(2)) if match else None


def _data(action: str, token: str) -> str:
    data = f"{CALLBACK_PREFIX}:{action}:{token}"
    assert len(data.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES
    return data


class EmergencyHandler:
    def __init__(
        self,
        client: TelegramClient,
        codec: CallbackTokenCodec,
        alerts: FamilyAlertSender | None = None,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._client = client
        self._codec = codec
        self._alerts = alerts  # None: Family Mode is off or unavailable; emergencies still work
        self._clock = clock

    # -- which updates are ours ------------------------------------------------------------------------------

    def owns(self, update: Update) -> bool:
        """True for /emergency and for `em:` button presses. Pure and cheap, so
        the caller can route these around the analysis queue before anything else."""
        query = update.callback_query
        if query is not None:
            return not query.from_.is_bot and isinstance(query.data, str) and query.data.startswith(CALLBACK_PREFIX + ":")
        message = update.message
        if message is None or message.chat.type != "private" or message.from_ is None or message.from_.is_bot:
            return False
        parsed = parse_command(message.text)
        return parsed is not None and parsed[0] == "emergency"

    # -- the button on scam-check replies -----------------------------------------------------------------------------

    def keyboard(self, user_id: int, summary: AlertSummary | None, language: Language) -> dict[str, Any]:
        token = self._codec.issue(user_id, summary, self._clock())
        return {"inline_keyboard": [[{"text": button_label("emergency", language), "callback_data": _data("o", token)}]]}

    def _question_keyboard(self, user_id: int, summary: AlertSummary | None, language: Language) -> dict[str, Any]:
        token = self._codec.issue(user_id, summary, self._clock())  # fresh, so it does not expire mid-conversation
        return {
            "inline_keyboard": [
                [
                    {"text": button_label("yes", language), "callback_data": _data("y", token)},
                    {"text": button_label("no", language), "callback_data": _data("n", token)},
                ]
            ]
        }

    # -- entry point -----------------------------------------------------------------------------------------------------

    async def handle(self, update: Update) -> str:
        if update.callback_query is not None:
            return await self._handle_callback(update.update_id, update.callback_query)
        assert update.message is not None
        return await self._handle_command(update.message)

    async def last_resort(self, update: Update) -> None:
        """Something unexpected broke: make sure the person still gets the 1930 steps."""
        target = self._reply_target(update)
        if target is not None:
            await self._send_guidance(*target)

    @staticmethod
    def _reply_target(update: Update) -> tuple[int, Language] | None:
        query, message = update.callback_query, update.message
        if query is not None:
            origin = query.message
            if origin is None or origin.chat.type != "private" or origin.chat.id != query.from_.id:
                return None
            return origin.chat.id, choose_language(None, query.from_.language_code)
        if message is not None and message.from_ is not None and message.chat.type == "private":
            return message.chat.id, choose_language(None, message.from_.language_code)
        return None

    # -- /emergency ------------------------------------------------------------------------------------------------------

    async def _handle_command(self, message: Message) -> str:
        sender = message.from_
        assert sender is not None
        lang = choose_language(None, sender.language_code)
        # No database, no Family call, no analysis: just ask.
        await self._client.send_message(
            message.chat.id,
            question_message(lang),
            reply_to_message_id=message.message_id,
            reply_markup=self._question_keyboard(sender.id, None, lang),
        )
        return "emergency:question"

    # -- button presses ----------------------------------------------------------------------------------------------------

    async def _handle_callback(self, update_id: int, query: CallbackQuery) -> str:
        """Process a press and acknowledge it exactly once, whatever happens."""
        popup_text: str | None = None
        try:
            outcome, popup_text = await self._dispatch(update_id, query)
            return outcome
        finally:
            try:
                await self._client.answer_callback_query(query.id, popup_text)
            except TelegramError as exc:
                logger.warning("could not acknowledge a button press: %s", type(exc).__name__)

    async def _dispatch(self, update_id: int, query: CallbackQuery) -> tuple[str, str | None]:
        actor = query.from_
        lang = choose_language(None, actor.language_code)
        invalid = popup("invalid", lang)

        parsed = parse_emergency_data(query.data)
        if parsed is None:
            return "emergency:invalid", invalid
        origin = query.message
        if origin is None or origin.chat.type != "private" or origin.chat.id != actor.id:
            return "emergency:bad_context", invalid  # not the presser's own private chat: change nothing

        action, token = parsed
        check = self._codec.verify(token, actor.id, self._clock())
        if check.status in (TokenStatus.INVALID, TokenStatus.WRONG_USER):
            return "emergency:invalid", invalid  # one generic answer: no hint why
        chat_id, message_id = origin.chat.id, origin.message_id
        if check.status is TokenStatus.EXPIRED:
            await self._remove_buttons(chat_id, message_id)
            return "emergency:expired", popup("expired", lang)

        summary = check.summary
        if action == "o":
            # Nothing is decided yet, so the original button stays where it is.
            await self._say(
                chat_id, question_message(lang), self._question_keyboard(actor.id, summary, lang)
            )
            return "emergency:question", None

        await self._remove_buttons(chat_id, message_id)  # the choice is made: take the buttons away
        if action == "n":
            await self._say(chat_id, emergency_message("no_response", lang))
            return "emergency:no", None
        return await self._confirm_yes(update_id, actor, chat_id, lang, summary), None

    # -- "Yes, I already sent money or details" ------------------------------------------------------------------------------

    async def _confirm_yes(
        self, update_id: int, actor: User, chat_id: int, lang: Language, summary: AlertSummary | None
    ) -> str:
        # FIRST, before any database or Family operation: the static safety steps.
        await self._send_guidance(chat_id, lang)

        if self._alerts is None:
            await self._say(chat_id, emergency_message("family_off", lang))
            return "emergency:family_off"
        try:
            plan = await self._alerts.request_emergency(person_ref(actor), summary, update_id)
            if plan is None:
                await self._say(chat_id, emergency_message("error", lang))
                return "emergency:error"
            return await self._report(plan, chat_id, lang)
        except Exception as exc:  # anything unexpected: still say what happened
            logger.warning("emergency alert failed unexpectedly: %s", type(exc).__name__)
            await self._say(chat_id, emergency_message("error", lang))
            return "emergency:error"

    async def _report(self, plan: AlertPlan, chat_id: int, lang: Language) -> str:
        """Tell the person exactly what happened, in every case."""
        if plan.outcome is AlertOutcome.NO_HELPERS:
            await self._say(chat_id, emergency_message("no_helpers", lang))
            return "emergency:no_helpers"
        if plan.outcome is AlertOutcome.DISABLED:
            await self._say(chat_id, emergency_message("family_off", lang))
            return "emergency:family_off"
        if plan.outcome is AlertOutcome.DEDUPED:
            await self._say(chat_id, emergency_message("deduped", lang))
            return "emergency:deduped"
        if plan.outcome is not AlertOutcome.READY:  # THROTTLED never applies to emergencies; be safe anyway
            await self._say(chat_id, emergency_message("error", lang))
            return "emergency:error"

        report = await self._alerts.deliver(plan)  # type: ignore[union-attr]
        if report.reached and not report.not_reached:
            await self._say(chat_id, emergency_message("told_all", lang, names=list(report.reached)))
            return "emergency:told_all"
        if report.reached:
            await self._say(
                chat_id,
                emergency_message("told_some", lang, reached=list(report.reached), failed=list(report.not_reached)),
            )
            return "emergency:told_some"
        await self._say(chat_id, emergency_message("told_none", lang))
        return "emergency:told_none"

    # -- sending helpers --------------------------------------------------------------------------------------------------------

    async def _send_guidance(self, chat_id: int, lang: Language) -> bool:
        """The 1930 steps. If Telegram rejects the first attempt, try once more."""
        text = guidance_message(lang)
        for attempt in (1, 2):
            try:
                await self._client.send_message(chat_id, text)
                return True
            except TelegramError as exc:
                status = getattr(exc, "error_code", None)
                logger.warning(
                    "emergency guidance send failed (attempt %d): %s status=%s", attempt, type(exc).__name__, status
                )
        return False

    async def _say(self, chat_id: int, text: str, keyboard: dict[str, Any] | None = None) -> bool:
        """Best-effort message to the person; a failure is logged (class/status only) and never raised."""
        try:
            await self._client.send_message(chat_id, text, reply_markup=keyboard)
            return True
        except TelegramError as exc:
            logger.warning("emergency message could not be sent: %s status=%s", type(exc).__name__, getattr(exc, "error_code", None))
            return False

    async def _remove_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            await self._client.remove_buttons(chat_id, message_id)
        except TelegramError as exc:  # e.g. the message is too old to edit; cosmetic only
            logger.debug("could not remove buttons: %s", type(exc).__name__)
