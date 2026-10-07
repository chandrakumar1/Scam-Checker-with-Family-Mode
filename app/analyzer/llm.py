"""Provider-neutral LLM wrapper.

`analyze()` is the only entry point the rest of the app uses. The provider
SDK is confined to one function per provider, selected by
`settings.llm_provider`. Any failure (timeout, API error, refusal, invalid or
off-schema JSON) returns None; the combiner treats that as "suspicious".

Timeout: `settings.llm_timeout_seconds` is ONE wall-clock deadline for the
whole provider call, enforced with `asyncio.timeout` in `analyze()`. It covers
connecting, uploading the request, model processing, any server-side refusal
fallback (which runs inside the same HTTP request) and reading the response.
The HTTP-level timeouts inside the SDK are per-step limits, not a total budget,
so they are only a secondary guard.
"""

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from app.analyzer.prompts import SYSTEM_PROMPT, build_user_prompt
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

LLMVerdict = Literal["likely_scam", "suspicious", "no_obvious_red_flags"]


class LLMAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: LLMVerdict
    scam_type: str
    red_flags: list[str]
    explanation: str
    what_to_do: list[str]


class Image(BaseModel):
    data: bytes
    media_type: str


# An async provider takes (system prompt, user prompt, optional image,
# settings) and returns the model's raw JSON text, or None if it produced no
# usable answer. It does not enforce the overall deadline; analyze() does.
Provider = Callable[[str, str, Image | None, Settings], Awaitable[str | None]]

# Cap on the HTTP connect step, inside the overall deadline.
CONNECT_TIMEOUT_SECONDS = 5.0


def detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# --- Anthropic ---------------------------------------------------------------

_ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _anthropic_client(settings: Settings) -> anthropic.AsyncAnthropic:
    api_key = settings.llm_api_key.get_secret_value() if settings.llm_api_key else None
    total = settings.llm_timeout_seconds
    # The SDK applies these limits to each network step separately (connect,
    # each write, each read), so they are NOT a total budget: a response that
    # keeps trickling in can outlive them. The real wall-clock deadline is the
    # asyncio.timeout in analyze(). max_retries=0 so the SDK never re-sends
    # the request and quietly multiplies the wait.
    return anthropic.AsyncAnthropic(
        api_key=api_key or None,
        timeout=anthropic.Timeout(total, connect=min(CONNECT_TIMEOUT_SECONDS, total)),
        max_retries=0,
    )


async def _anthropic_complete(
    system: str, user_prompt: str, image: Image | None, settings: Settings
) -> str | None:
    content: list[dict] = []
    if image is not None:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": image.media_type,
                    "data": base64.standard_b64encode(image.data).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": user_prompt})

    client = _anthropic_client(settings)
    try:
        response = await client.beta.messages.create(
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={
                "effort": settings.llm_effort,
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(LLMAnalysis),
                },
            },
            # On a safety-classifier refusal, retry server-side on Anthropic's
            # recommended fallback model instead of failing outright.
            betas=[_ANTHROPIC_FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.APITimeoutError:
        logger.warning("LLM request timed out after %ss", settings.llm_timeout_seconds)
        return None
    except anthropic.APIStatusError as exc:
        logger.warning("LLM API error: status %s", exc.status_code)
        return None
    except anthropic.APIConnectionError:
        logger.warning("LLM connection error")
        return None
    finally:
        # Also runs when the overall deadline cancels this coroutine.
        try:
            await client.close()
        except Exception as exc:
            logger.debug("LLM client close failed: %s", type(exc).__name__)

    if response.stop_reason in ("refusal", "max_tokens"):
        logger.warning("LLM gave no usable answer: stop_reason=%s", response.stop_reason)
        return None

    # After a fallback the serving model's answer is the last text block.
    texts = [block.text for block in response.content if block.type == "text"]
    return texts[-1] if texts else None


PROVIDERS: dict[str, Provider] = {
    "anthropic": _anthropic_complete,
}


# --- public interface --------------------------------------------------------


async def analyze(
    text: str | None = None,
    image_bytes: bytes | None = None,
    language: str = "en",
    *,
    settings: Settings | None = None,
) -> LLMAnalysis | None:
    """Ask the configured LLM for a scam verdict. Returns None on any failure.

    The whole provider call shares one `settings.llm_timeout_seconds`
    wall-clock deadline; hitting it cancels the call and returns None.
    """
    settings = settings or get_settings()

    if not (text and text.strip()) and not image_bytes:
        return None

    provider = PROVIDERS.get(settings.llm_provider)
    if provider is None:
        logger.info("LLM disabled (provider=%s)", settings.llm_provider)
        return None

    image = None
    if image_bytes:
        media_type = detect_image_type(image_bytes)
        if media_type is None:
            logger.warning("Unsupported image format; skipping LLM")
            return None
        image = Image(data=image_bytes, media_type=media_type)

    user_prompt = build_user_prompt(text, language, has_image=image is not None)

    try:
        async with asyncio.timeout(settings.llm_timeout_seconds):
            raw = await provider(SYSTEM_PROMPT, user_prompt, image, settings)
    except TimeoutError:
        logger.warning("LLM call exceeded the %ss overall deadline", settings.llm_timeout_seconds)
        return None
    except Exception as exc:  # never let a provider bug break the analyzer
        # Log only the exception type: messages can echo request details.
        logger.warning("LLM provider failed: %s", type(exc).__name__)
        return None

    if raw is None:
        return None

    try:
        return LLMAnalysis.model_validate_json(raw)
    except ValidationError:
        logger.warning("LLM returned invalid or off-schema JSON")
        return None
