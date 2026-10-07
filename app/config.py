import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Telegram only accepts these characters in a webhook secret token.
_WEBHOOK_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
# Telegram bot usernames: 5-32 characters, letters, digits and underscores.
_BOT_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{5,32}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Ruko"
    environment: str = "development"
    database_url: str = "sqlite:///./ruko.db"

    # LLM analyzer. "none" disables the LLM; the analyzer then relies on rules
    # and treats the missing LLM opinion as "suspicious".
    llm_provider: Literal["anthropic", "none"] = "anthropic"
    # Optional: if unset, the provider SDK resolves credentials from its own
    # environment (e.g. ANTHROPIC_API_KEY).
    llm_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_timeout_seconds: float = 15.0
    llm_max_tokens: int = 4096

    # Telegram channel. Everything is optional so the app (and /health) starts
    # without a bot; the webhook is simply disabled until a token is set.
    telegram_bot_token: SecretStr | None = None
    # Sent back by Telegram in X-Telegram-Bot-Api-Secret-Token on every webhook
    # call. With no secret configured the webhook rejects everything.
    telegram_webhook_secret: SecretStr | None = None
    # Public https base URL; only used by scripts/set_webhook.py.
    public_base_url: str | None = None
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_download_timeout_seconds: float = 10.0
    telegram_max_concurrent_analyses: int = 5

    # Family Mode (Phase 3). Off by default: with it off no database is opened
    # and the bot behaves exactly as in Phase 2.
    family_mode_enabled: bool = False
    # The bot's @username (without the @), used to build invite links. If unset
    # it is looked up once with getMe the first time it is needed.
    telegram_bot_username: str | None = None
    family_invite_ttl_hours: float = Field(default=48.0, gt=0)
    family_consent_ttl_hours: float = Field(default=24.0, gt=0)
    family_max_helpers: int = Field(default=5, ge=1)
    family_max_pending_invites: int = Field(default=3, ge=1)
    family_alert_max_per_hour: int = Field(default=5, ge=1)
    family_alert_dedupe_minutes: float = Field(default=30.0, gt=0)
    family_alert_retention_days: float = Field(default=30.0, gt=0)
    # Emergency flow (used by the emergency stage that follows).
    emergency_dedupe_seconds: float = Field(default=120.0, gt=0)
    emergency_button_ttl_hours: float = Field(default=24.0, gt=0)

    @field_validator(
        "telegram_bot_token", "telegram_webhook_secret", "public_base_url", "telegram_bot_username", mode="before"
    )
    @classmethod
    def _blank_means_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def _valid_webhook_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not _WEBHOOK_SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 1-256 characters from A-Z a-z 0-9 _ -")
        return value

    @field_validator("telegram_bot_username")
    @classmethod
    def _valid_bot_username(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().removeprefix("@")
        if not _BOT_USERNAME_RE.fullmatch(value):
            raise ValueError("TELEGRAM_BOT_USERNAME must be 5-32 letters, digits or underscores (without the @)")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
