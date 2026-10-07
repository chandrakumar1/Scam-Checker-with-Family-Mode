"""Telegram message handling, shared by the webhook and the local poller.

`accept()` de-duplicates an update; `handle_update()` does all the work and
never raises. Privacy: this module never logs message text, captions, image
bytes, chat/user ids or URLs. It logs update_id, input kind, outcome, timing
and exception class names only.
"""

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from app.analyzer.combine import AnalysisResult, analyze_message
from app.analyzer.llm import detect_image_type
from app.channels.telegram.alert_summary import build_alert_summary
from app.channels.telegram.client import FileTooLargeError, TelegramClient
from app.channels.telegram.dedup import SeenUpdates
from app.channels.telegram.emergency import EmergencyHandler
from app.channels.telegram.family_alerts import FamilyAlertSender
from app.channels.telegram.family_handler import FamilyTelegramHandler, person_ref
from app.channels.telegram.models import CallbackQuery, ChatMemberUpdated, Message, PhotoSize, Update, User
from app.family.types import AlertOutcome, AlertPlan, AlertSummary, AlertVerdict
from app.replies.emergency_formatter import emergency_message, told_helpers_notice
from app.replies.family_formatter import family_error_message
from app.replies.formatter import (
    error_message,
    format_analysis,
    help_message,
    image_too_large_message,
    image_unreadable_message,
    start_message,
    unsupported_input_message,
)
from app.replies.language import Language, choose_language

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MiB
SUPPORTED_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
TYPING_INTERVAL_SECONDS = 4.0  # Telegram shows "typing" for ~5 seconds per action
EMERGENCY_CONCURRENCY = 10  # emergencies have their own small limit, separate from the analysis queue
MAX_HIDDEN_LINKS = 10
MAX_HIDDEN_LINK_CHARS = 500

_COMMAND = re.compile(r"^/([A-Za-z0-9_]+)(?:@\w+)?$")

Analyzer = Callable[..., Awaitable[AnalysisResult]]


@dataclass
class _ImageRef:
    file_id: str
    declared_size: int | None


@dataclass
class _Ctx:
    """What we know so far, so the error fallback can still reply sensibly."""

    chat_id: int | None = None
    reply_to: int | None = None
    language: Language = "en"
    kind: str = "unknown"


class _Reject(Exception):
    """An input we refuse to analyze; `reason` picks the reply."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _parse_command(message: Message) -> str | None:
    if not message.text or not message.text.startswith("/"):
        return None
    match = _COMMAND.match(message.text.split(maxsplit=1)[0])
    return match.group(1).lower() if match else None


def _largest_photo(photos: list[PhotoSize]) -> PhotoSize:
    return max(photos, key=lambda p: (p.width * p.height, p.file_size or 0))


def _analysis_text(message: Message, text: str) -> str:
    """The text for the analyzer: what the user sees, plus any URLs hidden
    behind link text ("Claim your prize" -> https://bit.ly/x), which the rules
    would otherwise never see."""
    entities = message.entities if message.text is not None else message.caption_entities
    urls: list[str] = []
    for entity in entities:
        if entity.type == "text_link" and entity.url and entity.url not in urls:
            urls.append(entity.url)
    lines = [text] if text else []
    lines += [f"[link: {url[:MAX_HIDDEN_LINK_CHARS]}]" for url in urls[:MAX_HIDDEN_LINKS]]
    return "\n".join(lines)


class TelegramService:
    def __init__(
        self,
        client: TelegramClient,
        *,
        analyze: Analyzer = analyze_message,
        seen: SeenUpdates | None = None,
        max_concurrent: int = 5,
        max_image_bytes: int = MAX_IMAGE_BYTES,
        typing_interval: float = TYPING_INTERVAL_SECONDS,
        family: FamilyTelegramHandler | None = None,
        alerts: FamilyAlertSender | None = None,
        emergency: EmergencyHandler | None = None,
    ) -> None:
        self._client = client
        self._analyze = analyze
        self._seen = seen if seen is not None else SeenUpdates()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_image_bytes = max_image_bytes
        self._typing_interval = typing_interval
        # None means Family Mode is off: every path below then behaves exactly as in Phase 2.
        self._family = family
        self._alerts = alerts  # plans and delivers normal family alerts (only with Family Mode on)
        # Emergency is always available and never waits behind analyses.
        self._emergency = emergency
        self._emergency_semaphore = asyncio.Semaphore(EMERGENCY_CONCURRENCY)

    # -- entry points ---------------------------------------------------------------

    def accept(self, update: Update) -> bool:
        """False if this update_id was already seen (a Telegram redelivery)."""
        if self._seen.seen_before(update.update_id):
            logger.info("update %s is a duplicate; ignored", update.update_id)
            return False
        return True

    async def handle_update(self, update: Update) -> None:
        """Process one update end to end. Never raises (except cancellation)."""
        if self._emergency is not None and self._emergency.owns(update):
            await self._handle_emergency(update)  # before the analysis queue: it must never wait behind it
            return
        started = time.monotonic()
        ctx = _Ctx()
        async with self._semaphore:
            try:
                outcome = await self._handle(update, ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("update %s failed (kind=%s): %s", update.update_id, ctx.kind, type(exc).__name__)
                await self._send_fallback_error(ctx)
                return
        logger.info(
            "update %s handled kind=%s outcome=%s ms=%d",
            update.update_id, ctx.kind, outcome, (time.monotonic() - started) * 1000,
        )  # fmt: skip

    # -- emergency ------------------------------------------------------------------

    async def _handle_emergency(self, update: Update) -> None:
        """/emergency and its buttons, on their own small semaphore (not the analysis one)."""
        assert self._emergency is not None
        started = time.monotonic()
        async with self._emergency_semaphore:
            try:
                outcome = await self._emergency.handle(update)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("update %s failed (kind=emergency): %s", update.update_id, type(exc).__name__)
                await self._emergency.last_resort(update)  # whatever broke, the 1930 steps still go out
                return
        logger.info(
            "update %s handled kind=emergency outcome=%s ms=%d",
            update.update_id, outcome, (time.monotonic() - started) * 1000,
        )  # fmt: skip

    # -- handling -------------------------------------------------------------------

    async def _handle_callback(self, query: CallbackQuery, ctx: _Ctx) -> str:
        """A button press. Only Family Mode uses buttons so far."""
        if self._family is None or query.from_.is_bot:
            return "ignored"
        ctx.kind = "family"
        ctx.language = choose_language(None, query.from_.language_code)
        message = query.message
        if message is not None and message.chat.type == "private" and message.chat.id == query.from_.id:
            ctx.chat_id = message.chat.id  # only a genuine private-chat press may get a fallback reply
        return await self._family.handle_callback(query)

    async def _handle_chat_member(self, event: ChatMemberUpdated, ctx: _Ctx) -> str:
        """The bot's status in a chat changed (a user blocked or unblocked it)."""
        if self._family is None:
            return "ignored"
        ctx.kind = "family"
        return await self._family.handle_chat_member(event)

    async def _handle(self, update: Update, ctx: _Ctx) -> str:
        if update.callback_query is not None:
            return await self._handle_callback(update.callback_query, ctx)
        if update.my_chat_member is not None:
            return await self._handle_chat_member(update.my_chat_member, ctx)

        message = update.message
        if message is None or message.chat.type != "private":
            return "ignored"
        sender = message.from_
        if sender is None or sender.is_bot:
            return "ignored"

        ctx.chat_id = message.chat.id
        ctx.reply_to = message.message_id
        language_code = sender.language_code
        ctx.language = choose_language(None, language_code)

        if self._family is not None and self._family.matches(message):
            ctx.kind = "family"
            return await self._family.handle_message(message)

        command = _parse_command(message)
        if command is not None:
            ctx.kind = "command"
            reply = (
                start_message(ctx.language)
                if command == "start"
                else help_message(ctx.language, family=self._family is not None)
            )
            await self._reply(ctx, reply)
            return f"command:{command if command == 'start' else 'help'}"

        # Classify the input.
        image: _ImageRef | None = None
        unsupported = False
        if message.photo:
            largest = _largest_photo(message.photo)
            image = _ImageRef(largest.file_id, largest.file_size)
        elif message.document is not None:
            document = message.document
            if (document.mime_type or "").lower() in SUPPORTED_IMAGE_MIME_TYPES:
                image = _ImageRef(document.file_id, document.file_size)
            else:
                unsupported = True
        elif message.has_unsupported_media:
            unsupported = True

        text = ((message.text if message.text is not None else message.caption) or "").strip()
        if image is None and not unsupported and not text:
            if message.text is None:
                return "ignored"  # service message: nothing for us to answer
            unsupported = True  # blank text

        ctx.language = choose_language(text or None, language_code)
        if unsupported:
            ctx.kind = "unsupported"
            await self._reply(ctx, unsupported_input_message(ctx.language))
            return "unsupported"

        ctx.kind = "image" if image else "text"
        async with self._typing(ctx.chat_id):
            image_bytes: bytes | None = None
            if image is not None:
                try:
                    image_bytes = await self._fetch_image(image)
                except _Reject as rejected:
                    if rejected.reason == "too_large":
                        await self._reply(ctx, image_too_large_message(ctx.language))
                    else:
                        await self._reply(ctx, image_unreadable_message(ctx.language))
                    return f"rejected:{rejected.reason}"

            result = await self._analyze(
                text=_analysis_text(message, text) or None,
                image_bytes=image_bytes,
                language=ctx.language,
            )

        # The bounded summary is the only thing that leaves analysis. It is built
        # for every likely_scam / suspicious reply (the Emergency button needs it).
        summary = build_alert_summary(result, has_image=image_bytes is not None)
        plan = await self._plan_alert(sender, summary, update.update_id)

        keyboard = None
        if self._emergency is not None and summary is not None:
            keyboard = self._emergency.keyboard(sender.id, summary, ctx.language)
        notice = None
        if plan is not None and plan.outcome is AlertOutcome.READY and plan.deliveries:
            notice = told_helpers_notice([d.guardian_name for d in plan.deliveries], ctx.language)

        # The reply to the person always goes first, whatever the family side did.
        reply_error: Exception | None = None
        try:
            await self._reply(ctx, format_analysis(result, ctx.language, notice), keyboard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reply_error = exc  # re-raised below, but only after the helpers have been told

        if plan is not None and plan.outcome is AlertOutcome.READY:
            await self._deliver_alert(plan, ctx)
        if reply_error is not None:
            raise reply_error
        return f"analyzed:{result.verdict.value}"

    async def _plan_alert(self, sender: User, summary: AlertSummary | None, update_id: int) -> AlertPlan | None:
        """A normal family alert is planned ONLY for a likely scam; never for suspicious."""
        if self._alerts is None or summary is None or summary.verdict is not AlertVerdict.LIKELY_SCAM:
            return None
        return await self._alerts.request_normal(person_ref(sender), summary, update_id)

    async def _deliver_alert(self, plan: AlertPlan, ctx: _Ctx) -> None:
        """Send a planned alert, then tell the person if anyone could not be reached.
        Contained: a problem here must never turn into the 'could not check' fallback."""
        assert self._alerts is not None
        try:
            report = await self._alerts.deliver(plan)
            if report.not_reached:
                await self._reply_plain(ctx, emergency_message("normal_alert_failed", ctx.language, names=list(report.not_reached)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("delivering a family alert failed: %s", type(exc).__name__)

    async def _reply_plain(self, ctx: _Ctx, text: str) -> None:
        assert ctx.chat_id is not None
        await self._client.send_message(ctx.chat_id, text)

    async def _fetch_image(self, ref: _ImageRef) -> bytes:
        """Download an image, enforcing the size limit at every step. Anything
        over the limit is rejected before analysis."""
        limit = self._max_image_bytes
        if ref.declared_size is not None and ref.declared_size > limit:
            raise _Reject("too_large")

        info = await self._client.get_file(ref.file_id)
        if info.file_size is not None and info.file_size > limit:
            raise _Reject("too_large")
        if not info.file_path:
            raise _Reject("unreadable")

        try:
            data = await self._client.download_file(info.file_path, limit)
        except FileTooLargeError:
            raise _Reject("too_large") from None

        if detect_image_type(data) is None:
            raise _Reject("unreadable")
        return data

    # -- helpers --------------------------------------------------------------------

    async def _reply(self, ctx: _Ctx, text: str, keyboard: dict | None = None) -> None:
        assert ctx.chat_id is not None
        await self._client.send_message(ctx.chat_id, text, reply_to_message_id=ctx.reply_to, reply_markup=keyboard)

    async def _send_fallback_error(self, ctx: _Ctx) -> None:
        if ctx.chat_id is None:
            return
        # A failed /family or button press must not say "I could not check this message".
        text = family_error_message(ctx.language) if ctx.kind == "family" else error_message(ctx.language)
        try:
            await self._reply(ctx, text)
        except Exception as exc:
            logger.warning("could not send fallback reply: %s", type(exc).__name__)

    @contextlib.asynccontextmanager
    async def _typing(self, chat_id: int) -> AsyncIterator[None]:
        """Keep the "typing…" indicator alive while we work."""

        async def beat() -> None:
            while True:
                try:
                    await self._client.send_chat_action(chat_id)
                except Exception as exc:  # cosmetic only
                    logger.debug("typing action failed: %s", type(exc).__name__)
                await asyncio.sleep(self._typing_interval)

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.wait([task])
