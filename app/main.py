import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.channels.telegram.client import TelegramClient, make_http_client, telegram_allowed_updates
from app.channels.telegram.webhook import router as telegram_router
from app.config import get_settings
from app.logging_config import configure_logging
from app.wiring import TelegramRuntime, build_telegram_runtime

settings = get_settings()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging(settings)

    http = None
    runtime: TelegramRuntime | None = None
    app.state.telegram_service = None
    if settings.telegram_bot_token is None:
        logger.warning("TELEGRAM_BOT_TOKEN is not set; /webhook/telegram is disabled")
        if settings.family_mode_enabled:
            logger.warning("FAMILY_MODE_ENABLED is on but TELEGRAM_BOT_TOKEN is not set; Family Mode is inactive")
    else:
        if settings.telegram_webhook_secret is None:
            logger.warning("TELEGRAM_WEBHOOK_SECRET is not set; every webhook call will be rejected")
        http = make_http_client()
        client = TelegramClient(
            settings.telegram_bot_token,
            http,
            base_url=settings.telegram_api_base_url,
            download_timeout=settings.telegram_download_timeout_seconds,
            allowed_updates=telegram_allowed_updates(settings.family_mode_enabled),
        )
        runtime = await build_telegram_runtime(settings, client)
        app.state.telegram_service = runtime.service

    try:
        yield
    finally:
        if http is not None:
            await http.aclose()
        if runtime is not None:
            runtime.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(telegram_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
