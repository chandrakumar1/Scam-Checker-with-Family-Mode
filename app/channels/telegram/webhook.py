"""POST /webhook/telegram.

Order of checks: secret token (401) -> bot configured (503) -> parse -> de-dup
-> schedule background processing -> return 200 immediately. Telegram retries
anything that is not a quick 2xx, so after authentication we answer 200 even
for bodies we cannot use.
"""

import hmac
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import ValidationError

from app.channels.telegram.models import Update
from app.channels.telegram.service import TelegramService
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


def verify_secret_token(
    settings: Settings = Depends(get_settings),
    secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> None:
    """Reject the call unless the secret header matches. With no secret
    configured, everything is rejected (fail closed)."""
    expected = settings.telegram_webhook_secret
    if expected is None or not secret_token:
        raise HTTPException(status_code=401, detail="unauthorized")
    if not hmac.compare_digest(secret_token.encode(), expected.get_secret_value().encode()):
        raise HTTPException(status_code=401, detail="unauthorized")


def get_telegram_service(request: Request) -> TelegramService:
    service = getattr(request.app.state, "telegram_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="telegram bot is not configured")
    return service


@router.post("/webhook/telegram", dependencies=[Depends(verify_secret_token)])
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    service: TelegramService = Depends(get_telegram_service),
) -> dict[str, bool]:
    try:
        payload: Any = await request.json()
    except ValueError:
        logger.warning("webhook body was not valid JSON; ignored")
        return {"ok": True}

    try:
        update = Update.model_validate(payload)
    except ValidationError as exc:
        # Never log the error text: pydantic echoes the offending input.
        logger.warning("webhook update did not match the expected shape (%d errors); ignored", exc.error_count())
        return {"ok": True}

    if service.accept(update):
        background_tasks.add_task(service.handle_update, update)
    return {"ok": True}
