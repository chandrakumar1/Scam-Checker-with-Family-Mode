"""Logging setup that keeps secrets out of logs.

httpx logs every request URL at INFO, and a Telegram Bot API URL contains the
bot token, so those loggers are raised to WARNING. As defence in depth a
filter also rewrites anything that looks like a bot token (or a configured
secret) in whatever does get logged.
"""

import logging
import re
from collections.abc import Iterable

from app.config import Settings

NOISY_LOGGERS = ("httpx", "httpx2", "httpcore")
FILTERED_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")  # "" = root

_BOT_TOKEN_IN_URL = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")
_MIN_SECRET_LENGTH = 8


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    text = _BOT_TOKEN_IN_URL.sub("bot<redacted>", text)
    for secret in secrets:
        if len(secret) >= _MIN_SECRET_LENGTH:
            text = text.replace(secret, "<redacted>")
    return text


class RedactSecretsFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message, self._secrets)
        if cleaned != message:
            record.msg = cleaned
            record.args = None
        return True


def configure_logging(settings: Settings) -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("app").setLevel(logging.INFO)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    secrets = [
        secret.get_secret_value()
        for secret in (settings.telegram_bot_token, settings.telegram_webhook_secret)
        if secret is not None
    ]
    for name in FILTERED_LOGGERS:
        for handler in logging.getLogger(name).handlers:
            # Replace any filter from an earlier call so changed secrets apply.
            for existing in [f for f in handler.filters if isinstance(f, RedactSecretsFilter)]:
                handler.removeFilter(existing)
            handler.addFilter(RedactSecretsFilter(secrets))
