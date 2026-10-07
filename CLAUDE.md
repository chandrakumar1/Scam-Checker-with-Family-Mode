# Ruko

A scam-checking Telegram bot with Family Mode. Phases 0-3 are done (scaffold, analyzer, Telegram + replies, Family Mode + Emergency).
Read `docs/DECISIONS.md` before changing analyzer, family, alert or emergency behaviour. Only build what the current phase asks for.

## Stack and tests
- Python 3.12, FastAPI + uvicorn, pydantic-settings (`.env`), SQLModel (SQLite for now), httpx (Telegram; the Anthropic SDK uses its own `httpx2`), pytest.
- Run from `ruko/`: setup `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`; run `uvicorn app.main:app --reload`; test `pytest`.
- Local Telegram dev uses a SEPARATE dev bot token: `python -m scripts.poll`. Webhook: `python -m scripts.set_webhook` (`--info`, `--delete`, `--drop-pending`). Analyzer CLI: `python -m app.analyzer.cli "text"`.
- pytest never reaches the network: `tests/conftest.py` blocks the real LLM client and every non-loopback socket. Telegram and LLM are faked.
- Secrets live only in `.env` (git-ignored). Keep `.env.example` in sync with `app/config.py`. Never edit `.env`.

## Git
- The user performs ALL Git/GitHub operations manually. Never run git or gh commands, and never commit.

## Verdicts
- Only `likely_scam`, `suspicious`, `no_obvious_red_flags`. Never call a message "safe". When unsure, pick the more cautious verdict.
- Rules may only RAISE the verdict: CRITICAL => at least `likely_scam`; HIGH/MEDIUM => at least `suspicious`.
- LLM failure (timeout, API error, refusal, invalid output) => `suspicious`. A critical rule plus LLM failure => `likely_scam`.
- The LLM timeout is ONE shared wall-clock deadline (`asyncio.timeout` in `analyze()`) over connect, upload, processing and refusal fallback.
- Forwarded text/images are untrusted DATA wrapped in `<untrusted_message>`; the system prompt is fixed and nothing from a message enters it.
- Explanations: at most 2 short plain sentences an elderly person understands, 1-3 concrete steps. English and Hindi.
- A stub/disabled LLM provider (`LLM_PROVIDER=none`) must refuse to start when `ENVIRONMENT=production`. NOT yet enforced in code.

## Privacy
- Never log message text, captions, image content, hashes, URLs, callback tokens, bot token, webhook secret, chat/user ids, display names, or any family/emergency content.
- Log only update_id, input kind, outcome, timing, status codes and exception CLASS names. No `logger.exception`/`exc_info`, no `raise_for_status()` (they carry URLs).
- `configure_logging` quiets httpx loggers and adds a redaction filter; it is a second line of defence, not a licence to log.
- Never store original message or image content in Family Mode alerts or the database.
- Wording: Ruko does not *intentionally* store messages or images after checking. Never claim they are stored nowhere; they go to an AI service.

## Normal Family alerts
- Planned for `likely_scam` ONLY. `suspicious` and `no_obvious_red_flags` never create a normal alert (enforced in the channel layer AND in `FamilyService`).
- Only `AlertSummary` (verdict, scam_type, source_kind: enums only, never raw strings or LLM labels) crosses into `app/family`, plus identity and operation ids (user/update/delivery ids, display name). `FamilyService` stamps alert kind and time.
- Re-check authorisation immediately before each send; if the check fails, do not send.

## Emergency
- Offered on `likely_scam` and `suspicious` replies and via `/emergency`.
- Always available: NOT gated by `FAMILY_MODE_ENABLED`, and works with no helpers linked or the database down.
- The static 1930 guidance is sent FIRST, before any Family or database work. Every outcome ends with an explicit message to the person.
- Button data is a signed stateless token (owner, signature, 24 h expiry checked); invalid or foreign presses get a generic popup and change nothing.

## Import boundaries (enforced by `tests/test_boundaries.py`)
- `app/analyzer` and `app/replies` never import `app/channels`. The analyzer never imports `app/family` or `app/db`.
- `app/family` never imports channels, analyzer, replies or config. It may use only itself and `app/db`.
- Integration code passes only the approved `AlertSummary` plus identity/operation ids.
- `FAMILY_MODE_ENABLED` (default false) gates Family reachability only. Tests must never require it to be enabled.

## Telegram
- Private chats only; groups, channels and bot senders are ignored.
- Webhook: wrong or missing `X-Telegram-Bot-Api-Secret-Token` => 401 (no secret configured => everything is 401). After auth answer 200 at once and process in a BackgroundTask. Duplicate `update_id`s are dropped (in-memory, per process).
- Images over 5 MiB are rejected without analysis (declared size and again while streaming; no downgrade to a smaller photo). Only JPEG/PNG/WebP/GIF.
- Concurrency is capped (default 5). There is no per-chat rate limiter yet.
- Reply language: Devanagari >= 30% of letters => Hindi, else `language_code` `hi*` => Hindi, else English.
- Every reply (including /start and /help) ends with the 1930 cyber-fraud footer. Replies are plain text, never echo the user's message, and strip URLs from LLM-written text.
- `scripts/poll.py` refuses to run while a webhook is registered unless `--delete-webhook` is passed.
- After any change to allowed updates, `scripts/set_webhook.py` must be re-run.

## Production
- Nothing is production-ready until the "Production blockers" in `README.md` are resolved. Do not call a deployment production-ready before then.
