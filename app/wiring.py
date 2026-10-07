"""Builds the Telegram service and, only when Family Mode is on, its database and handler.

Shared by the web app (app.main) and the local poller (scripts.poll), so both
behave the same.

- Family Mode off (the default): no database is opened and no file is created;
  the service is the Phase 2 service plus the always-on Emergency feature.
- Family Mode on: the database is created if needed, expired invitations are
  cleaned up and old alert rows purged, and the Family handler is attached.
- If Family Mode cannot start (for example the database file is not writable)
  the problem is logged and the bot carries on WITHOUT it. Checking messages
  for scams is the core job and must never depend on this feature.

The database URL is never logged: on other databases it can contain a password.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Engine

from app.channels.telegram.callback_tokens import CallbackTokenCodec
from app.channels.telegram.client import TelegramClient
from app.channels.telegram.emergency import EmergencyHandler
from app.channels.telegram.family_alerts import FamilyAlertSender
from app.channels.telegram.family_handler import FamilyTelegramHandler
from app.channels.telegram.service import TelegramService
from app.config import Settings
from app.db.engine import init_db, make_engine
from app.family.policy import FamilyPolicy
from app.family.service import AsyncFamilyService, FamilyService

logger = logging.getLogger(__name__)


@dataclass
class TelegramRuntime:
    service: TelegramService
    engine: Engine | None = None

    @property
    def family_enabled(self) -> bool:
        return self.engine is not None

    def close(self) -> None:
        if self.engine is not None:
            self.engine.dispose()
            self.engine = None


def build_family_policy(settings: Settings) -> FamilyPolicy:
    return FamilyPolicy(
        invite_ttl=timedelta(hours=settings.family_invite_ttl_hours),
        consent_ttl=timedelta(hours=settings.family_consent_ttl_hours),
        max_helpers=settings.family_max_helpers,
        max_pending_invites=settings.family_max_pending_invites,
        alert_max_per_hour=settings.family_alert_max_per_hour,
        alert_dedupe_window=timedelta(minutes=settings.family_alert_dedupe_minutes),
        alert_retention=timedelta(days=settings.family_alert_retention_days),
        emergency_dedupe_window=timedelta(seconds=settings.emergency_dedupe_seconds),
    )


def _warn_if_sqlite_in_production(settings: Settings) -> None:
    if settings.environment.strip().lower() == "production" and settings.database_url.lower().startswith("sqlite"):
        logger.warning(
            "Family Mode is using SQLite while ENVIRONMENT=production. Family links live in one file: keep it on "
            "persistent storage and back it up, or point DATABASE_URL at a managed database, before real use."
        )


def _build_codec(settings: Settings) -> CallbackTokenCodec | None:
    if settings.telegram_bot_token is None:
        return None  # nothing to sign with; cannot happen in a running bot, which always has a token
    return CallbackTokenCodec(settings.telegram_bot_token, ttl=timedelta(hours=settings.emergency_button_ttl_hours))


def _emergency(client: TelegramClient, codec: CallbackTokenCodec | None, alerts: FamilyAlertSender | None) -> EmergencyHandler | None:
    return EmergencyHandler(client, codec, alerts) if codec is not None else None


async def build_telegram_runtime(settings: Settings, client: TelegramClient) -> TelegramRuntime:
    """Emergency is ALWAYS built, whatever FAMILY_MODE_ENABLED says: it is a safety
    feature that must work with Family Mode off, with no helpers, and with the
    database down. Only the family consent flow and the alerts depend on the flag."""
    service_options = {"max_concurrent": settings.telegram_max_concurrent_analyses}
    codec = _build_codec(settings)
    if not settings.family_mode_enabled:
        return TelegramRuntime(TelegramService(client, emergency=_emergency(client, codec, None), **service_options))

    engine: Engine | None = None
    try:
        _warn_if_sqlite_in_production(settings)
        engine = make_engine(settings.database_url)
        init_db(engine, settings.database_url)
        policy = build_family_policy(settings)
        family = AsyncFamilyService(FamilyService(engine, enabled=True, policy=policy))
        handler = FamilyTelegramHandler(
            client,
            family,
            bot_username=settings.telegram_bot_username,
            invite_ttl_hours=settings.family_invite_ttl_hours,
            max_helpers=policy.max_helpers,
            max_pending_invites=policy.max_pending_invites,
        )
        alerts = FamilyAlertSender(client, family)
    except Exception as exc:
        logger.error("Family Mode could not start (%s); continuing without it", type(exc).__name__)
        if engine is not None:
            engine.dispose()
        # Emergency still works; it simply has no family to alert.
        return TelegramRuntime(TelegramService(client, emergency=_emergency(client, codec, None), **service_options))

    try:
        expired, purged = await family.maintenance()
        logger.info("Family Mode started; cleaned up %d expired links and %d old alerts", expired, purged)
    except Exception as exc:  # housekeeping only: never a reason to refuse to start
        logger.warning("Family Mode housekeeping failed (%s); continuing", type(exc).__name__)

    service = TelegramService(
        client, family=handler, alerts=alerts, emergency=_emergency(client, codec, alerts), **service_options
    )
    return TelegramRuntime(service, engine)
