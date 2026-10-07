"""Local development: receive Telegram updates with getUpdates (no ngrok needed).

    python -m scripts.poll [--delete-webhook] [--poll-timeout 30]

Use a SEPARATE development bot (its own token from @BotFather). Telegram will
not deliver getUpdates while a webhook is registered, so if one is, this script
refuses to start unless you pass --delete-webhook; that keeps an accidental run
with your production token from silently unregistering the production webhook.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python scripts/poll.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import ValidationError  # noqa: E402

from app.channels.telegram.client import (  # noqa: E402
    TelegramAPIError,
    TelegramClient,
    TelegramError,
    TelegramNetworkError,
    make_http_client,
    telegram_allowed_updates,
)
from app.channels.telegram.models import Update  # noqa: E402
from app.channels.telegram.service import TelegramService  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.logging_config import configure_logging  # noqa: E402
from app.wiring import build_telegram_runtime  # noqa: E402

logger = logging.getLogger("scripts.poll")

MAX_BACKOFF_SECONDS = 30.0
SHUTDOWN_GRACE_SECONDS = 20.0


class PollingError(Exception):
    """A problem that polling cannot recover from; the message is for the user."""


async def poll_loop(
    client: TelegramClient,
    service: TelegramService,
    *,
    stop: asyncio.Event,
    poll_timeout: int = 30,
    sleep=asyncio.sleep,
) -> None:
    """Fetch updates until `stop` is set, handing each new one to the service
    as its own task (bounded by the service's concurrency limit)."""
    offset: int | None = None
    backoff = 1.0
    tasks: set[asyncio.Task] = set()
    try:
        while not stop.is_set():
            try:
                raw_updates = await client.get_updates(offset=offset, timeout=poll_timeout)
                backoff = 1.0
            except TelegramAPIError as exc:
                if exc.error_code == 401:
                    raise PollingError("Telegram rejected the bot token (401). Check TELEGRAM_BOT_TOKEN.") from None
                if exc.error_code == 409:
                    raise PollingError(
                        "Telegram reports a conflict (409): a webhook is registered for this bot, "
                        "or another poller is running. Stop the other poller, or use --delete-webhook."
                    ) from None
                delay = exc.retry_after if exc.error_code == 429 and exc.retry_after else backoff
                logger.warning("getUpdates failed (HTTP %s); retrying in %.0fs", exc.error_code, delay)
                await sleep(delay)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except TelegramNetworkError as exc:
                logger.warning("getUpdates network error (%s); retrying in %.0fs", exc.cause, backoff)
                await sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            for raw in raw_updates:
                # Advance past this update first, so it is handled at most once.
                update_id = raw.get("update_id") if isinstance(raw, dict) else None
                if isinstance(update_id, int):
                    offset = max(offset or 0, update_id + 1)
                try:
                    update = Update.model_validate(raw)
                except ValidationError as exc:
                    logger.warning("skipping an update with an unexpected shape (%d errors)", exc.error_count())
                    continue
                if service.accept(update):
                    task = asyncio.create_task(service.handle_update(update))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
    finally:
        if tasks:
            await asyncio.wait(set(tasks), timeout=SHUTDOWN_GRACE_SECONDS)


async def prepare(client: TelegramClient, *, delete_webhook: bool) -> str:
    """Check the token and the webhook state. Returns the bot's @username."""
    try:
        me = await client.get_me()
        info = await client.get_webhook_info()
        has_webhook = bool(info.get("url"))
        if has_webhook and not delete_webhook:
            raise PollingError(
                "A webhook is registered for this bot, and polling cannot work while it is. "
                "Use a separate development bot for local testing. If you are sure, re-run "
                "with --delete-webhook to remove it."
            )
        if has_webhook:
            await client.delete_webhook()
            print("Webhook deleted.")
    except TelegramAPIError as exc:
        if exc.error_code == 401:
            raise PollingError("Telegram rejected the bot token (401). Check TELEGRAM_BOT_TOKEN.") from None
        raise PollingError(f"Telegram request failed ({exc.method}, HTTP {exc.status}).") from None
    except TelegramNetworkError as exc:
        raise PollingError(f"Could not reach Telegram ({exc.cause}).") from None
    return str(me.get("username") or "your bot")


async def run(settings: Settings, *, delete_webhook: bool = False, poll_timeout: int = 30) -> int:
    if settings.telegram_bot_token is None:
        print("error: TELEGRAM_BOT_TOKEN is not set (put your DEV bot's token in .env).", file=sys.stderr)
        return 1

    configure_logging(settings)
    async with make_http_client() as http:
        client = TelegramClient(
            settings.telegram_bot_token,
            http,
            base_url=settings.telegram_api_base_url,
            download_timeout=settings.telegram_download_timeout_seconds,
            allowed_updates=telegram_allowed_updates(settings.family_mode_enabled),
        )
        runtime = await build_telegram_runtime(settings, client)
        try:
            username = await prepare(client, delete_webhook=delete_webhook)
            print(f"Polling as @{username}. Press Ctrl-C to stop.")
            await poll_loop(client, runtime.service, stop=asyncio.Event(), poll_timeout=poll_timeout)
        except PollingError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except TelegramError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        finally:
            runtime.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.poll", description="Poll Telegram for local development.")
    parser.add_argument("--delete-webhook", action="store_true", help="remove a registered webhook first")
    parser.add_argument("--poll-timeout", type=int, default=30, help="long-poll seconds (default 30)")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(get_settings(), delete_webhook=args.delete_webhook, poll_timeout=args.poll_timeout))
    except KeyboardInterrupt:
        print("Stopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
