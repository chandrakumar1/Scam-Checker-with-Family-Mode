"""Planning and delivering family alerts (normal and emergency).

`request_*` ask FamilyService to plan an alert (alert + pending deliveries are
written before anything is sent) under a timeout, so a slow or broken database
can never hold up a reply. `deliver` then sends to each helper, re-checking
authorisation IMMEDIATELY before each send, and records the outcome.

Nothing in here can fail the caller: every database or Telegram problem is
contained, logged by exception class (and HTTP status) only, and reported back
as "not reached".

Privacy: no names, ids, message text, tokens or URLs are ever logged.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.channels.telegram.client import TelegramAPIError, TelegramClient, TelegramError
from app.db.models import AlertKind, DeliveryStatus
from app.family.service import AsyncFamilyService
from app.family.types import AlertPlan, AlertSummary, DeliveryPlan, PersonRef
from app.replies.emergency_formatter import emergency_alert_message, normal_alert_message

logger = logging.getLogger(__name__)

# How long a scam-check reply will wait for the family side before moving on without it.
NORMAL_PLAN_TIMEOUT_SECONDS = 3.0
EMERGENCY_PLAN_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    reached: tuple[str, ...] = ()  # helper display names the alert was delivered to
    not_reached: tuple[str, ...] = ()  # helpers skipped, blocked, revoked, or whose delivery failed


class FamilyAlertSender:
    def __init__(
        self,
        client: TelegramClient,
        family: AsyncFamilyService,
        *,
        normal_timeout: float = NORMAL_PLAN_TIMEOUT_SECONDS,
        emergency_timeout: float = EMERGENCY_PLAN_TIMEOUT_SECONDS,
    ) -> None:
        self._client = client
        self._family = family
        self._normal_timeout = normal_timeout
        self._emergency_timeout = emergency_timeout

    # -- planning -----------------------------------------------------------------------------------------------

    async def request_normal(self, person: PersonRef, summary: AlertSummary, update_id: int) -> AlertPlan | None:
        """A normal alert plan, or None if the family side failed or was too slow."""
        try:
            return await asyncio.wait_for(
                self._family.request_normal_alert(person, summary, update_id), self._normal_timeout
            )
        except Exception as exc:  # never let the family side affect the scam-check reply
            logger.warning("family alert planning failed: %s", type(exc).__name__)
            return None

    async def request_emergency(self, person: PersonRef, summary: AlertSummary | None, update_id: int) -> AlertPlan | None:
        """An emergency alert plan, or None if the family side failed or was too slow."""
        try:
            return await asyncio.wait_for(
                self._family.request_emergency_alert(person, summary, update_id), self._emergency_timeout
            )
        except Exception as exc:
            logger.warning("emergency alert planning failed: %s", type(exc).__name__)
            return None

    # -- delivery ---------------------------------------------------------------------------------------------------

    async def deliver(self, plan: AlertPlan) -> DeliveryReport:
        """Send a planned alert. Never raises (except cancellation)."""
        results = await asyncio.gather(*(self._deliver_one(plan, delivery) for delivery in plan.deliveries))
        reached = tuple(d.guardian_name for d, ok in zip(plan.deliveries, results) if ok)
        failed = tuple(d.guardian_name for d, ok in zip(plan.deliveries, results) if not ok)
        return DeliveryReport(reached, tuple(plan.unreachable_names) + failed)

    async def _deliver_one(self, plan: AlertPlan, delivery: DeliveryPlan) -> bool:
        # Re-read authorisation right now: a helper removed or blocked a moment
        # ago must not receive this. If the check itself fails, do not send.
        try:
            if not await self._family.authorize_delivery(delivery.delivery_id):
                return False
        except Exception as exc:
            logger.warning("authorising a delivery failed: %s", type(exc).__name__)
            return False

        text = self._text(plan, delivery)
        if text is None:
            return False
        try:
            await self._client.send_message(delivery.guardian_telegram_user_id, text)
        except TelegramAPIError as exc:
            logger.warning("alert delivery failed: %s status=%s", type(exc).__name__, exc.error_code)
            await self._record(delivery.delivery_id, DeliveryStatus.FAILED, exc.error_code)
            if exc.error_code == 403:  # the helper blocked the bot
                await self._mark_blocked(delivery.guardian_telegram_user_id)
            return False
        except TelegramError as exc:
            logger.warning("alert delivery failed: %s", type(exc).__name__)
            await self._record(delivery.delivery_id, DeliveryStatus.FAILED, None)
            return False

        await self._record(delivery.delivery_id, DeliveryStatus.SENT, None)
        return True

    @staticmethod
    def _text(plan: AlertPlan, delivery: DeliveryPlan) -> str | None:
        if plan.alert_kind is AlertKind.EMERGENCY:
            return emergency_alert_message(plan.protected_name, plan.summary, delivery.guardian_language)
        if plan.summary is None:  # a normal alert always has a summary; never improvise one
            return None
        return normal_alert_message(plan.protected_name, plan.summary, delivery.guardian_language)

    async def _record(self, delivery_id: int, status: DeliveryStatus, error_code: int | None) -> None:
        try:
            await self._family.record_delivery(delivery_id, status, error_code)
        except Exception as exc:  # the message already went (or failed); losing the note must not matter
            logger.warning("recording a delivery failed: %s", type(exc).__name__)

    async def _mark_blocked(self, telegram_user_id: int) -> None:
        try:
            await self._family.mark_blocked(telegram_user_id, True)
        except Exception as exc:
            logger.warning("marking a helper unreachable failed: %s", type(exc).__name__)
