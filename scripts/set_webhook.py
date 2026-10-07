"""Register (or remove / inspect) the Telegram webhook.

    python -m scripts.set_webhook                  # set it to PUBLIC_BASE_URL/webhook/telegram
    python -m scripts.set_webhook --info           # show Telegram's view of the webhook
    python -m scripts.set_webhook --delete         # remove it
    python -m scripts.set_webhook --drop-pending   # with set/delete: discard queued updates

The bot token and the webhook secret are never printed.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlparse

if __package__ in (None, ""):  # allow `python scripts/set_webhook.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.channels.telegram.client import (  # noqa: E402
    TelegramClient,
    TelegramError,
    make_http_client,
    telegram_allowed_updates,
)
from app.config import Settings, get_settings  # noqa: E402

WEBHOOK_PATH = "/webhook/telegram"
INFO_FIELDS = ("url", "pending_update_count", "max_connections", "last_error_date", "last_error_message")


def build_webhook_url(public_base_url: str | None) -> str:
    if not public_base_url or not public_base_url.strip():
        raise ValueError("PUBLIC_BASE_URL is not set")
    base = public_base_url.strip()
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("PUBLIC_BASE_URL must be an https URL, e.g. https://ruko.example.com")
    if parsed.query or parsed.fragment:
        raise ValueError("PUBLIC_BASE_URL must not contain a query string or fragment")
    return base.rstrip("/") + WEBHOOK_PATH


def _print_info(info: dict) -> None:
    for field in INFO_FIELDS:
        if info.get(field) not in (None, ""):
            print(f"  {field}: {info[field]}")


async def run(
    client: TelegramClient,
    settings: Settings,
    *,
    delete: bool = False,
    info: bool = False,
    drop_pending: bool = False,
) -> int:
    try:
        if info:
            _print_info(await client.get_webhook_info())
            return 0

        if delete:
            await client.delete_webhook(drop_pending_updates=drop_pending)
            print("Webhook deleted.")
            return 0

        url = build_webhook_url(settings.public_base_url)
        if settings.telegram_webhook_secret is None:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET is not set; refusing to register an unauthenticated webhook")
        await client.set_webhook(
            url,
            settings.telegram_webhook_secret.get_secret_value(),
            drop_pending_updates=drop_pending,
        )
        print(f"Webhook set to {url}")
        _print_info(await client.get_webhook_info())
        return 0
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        print("error: TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 1
    async with make_http_client() as http:
        client = TelegramClient(
            settings.telegram_bot_token,
            http,
            base_url=settings.telegram_api_base_url,
            allowed_updates=telegram_allowed_updates(settings.family_mode_enabled),
        )
        return await run(client, settings, delete=args.delete, info=args.info, drop_pending=args.drop_pending)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.set_webhook", description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--delete", action="store_true", help="remove the webhook")
    group.add_argument("--info", action="store_true", help="show the current webhook status")
    parser.add_argument("--drop-pending", action="store_true", help="discard updates queued at Telegram")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
